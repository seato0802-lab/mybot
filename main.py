import os
import io
import re
import csv
import json
import time
import math
import random
import asyncio
import sqlite3
import traceback
from datetime import datetime, timedelta, timezone, date
from threading import Thread, Lock
from collections import OrderedDict

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands
from flask import Flask
from openai import OpenAI

# Google Sheets
import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials


# =========================================================
# 設定
# =========================================================
JST = timezone(timedelta(hours=9))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # /setup_shop の初期配布などで members を触るのでON推奨

bot = commands.Bot(command_prefix="!", intents=intents)
client = OpenAI()

tasks_data: dict[str, dict] = {}
join_tasks: dict[int, dict] = {}

PLACE_LIST = [
    "パシフィック",
    "オイルリグ",
    "アーティファクト",
    "飛行場",
    "客船",
    "ユニオン",
    "パレト",
    "ボブキャット",
    "市長の工場",
]

# =========================================================
# 起動フラグ（✅ ここは必ずグローバル：インデント無し）
# =========================================================
STORE_READY = False
VIEWS_READY = False

# =========================================================
# ユーザー単位ロック（コイン/セッションの競合防止）
#  - ロック辞書が増え続けないよう LRU + locked 回避で掃除
# =========================================================
_user_locks: "OrderedDict[int, asyncio.Lock]" = OrderedDict()
_USER_LOCKS_MAX = 3000  # サーバ規模に応じて調整


def get_user_lock(uid: int) -> asyncio.Lock:
    lock = _user_locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[uid] = lock
    else:
        _user_locks.move_to_end(uid, last=True)

    if len(_user_locks) > _USER_LOCKS_MAX:
        overflow = len(_user_locks) - _USER_LOCKS_MAX
        removed = 0
        for old_uid in list(_user_locks.keys()):
            if removed >= overflow:
                break
            if old_uid == uid:
                continue
            lk = _user_locks.get(old_uid)
            if lk and not lk.locked():
                _user_locks.pop(old_uid, None)
                removed += 1
    return lock


# ---------------------------------------------------------
# /craft 用（既存のまま）
# ---------------------------------------------------------
TOOL_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vRH53VZ7iL7EFXNhkGTmRBS0JdE6oAjex51ape3cqOoXnuoR7RGATJlq_TaLupYmT4YJB2Luaa5NwXx/"
    "pub?gid=0&single=true&output=csv"
)
WEAPON_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vRH53VZ7iL7EFXNhkGTmRBS0JdE6oAjex51ape3cqOoXnuoR7RGATJlq_TaLupYmT4YJB2Luaa5NwXx/"
    "pub?gid=793378898&single=true&output=csv"
)


async def fetch_csv(url: str):
    timeout = aiohttp.ClientTimeout(total=6)  # 6秒で諦める
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as r:
            r.raise_for_status()
            text = await r.text()
    f = io.StringIO(text)
    reader = csv.DictReader(f)
    return [row for row in reader]

CSV_CACHE = {"道具": [], "武器": [], "timestamp": 0}


async def get_csv(category: str):
    now = time.time()
    if CSV_CACHE["timestamp"] and now - CSV_CACHE["timestamp"] < 300:
        return CSV_CACHE.get(category, [])
    url = TOOL_URL if category == "道具" else WEAPON_URL
    sheet = await fetch_csv(url)
    if sheet:
        CSV_CACHE[category] = sheet
        CSV_CACHE["timestamp"] = now
    return sheet


# =========================================================
# ずんだもんシステムプロンプト
# =========================================================
ZUNDAMON_SYSTEM = """
あなたはずんだもんです。
語尾は必ず「〜なのだ」「〜なのだよ」になります。
JSON形式では返さず、必ず普通の文章だけで返答してください。
""".strip()


# =========================================================
# 既存：AIメモリ(SQLite)
# =========================================================
def init_ai_memory_db():
    conn = sqlite3.connect("ai_memory.db")
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_summary (
            user_id INTEGER PRIMARY KEY,
            summary TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # ✅ 抽選イベント（Sheetsには保存しない）
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS lottery_events (
            message_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            created_by INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            ends_at INTEGER NOT NULL,
            winners_count INTEGER NOT NULL,
            reward_coins INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open' -- open / closed
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS lottery_entries (
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            joined_at INTEGER NOT NULL,
            PRIMARY KEY (message_id, user_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS lottery_winners (
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reward_coins INTEGER NOT NULL,
            decided_at INTEGER NOT NULL,
            PRIMARY KEY (message_id, user_id)
        )
        """
    )

    conn.commit()
    conn.close()

def save_chat(user_id: int, message: str):
    conn = sqlite3.connect("ai_memory.db")
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chat_log (user_id, message) VALUES (?, ?)",
        (user_id, message),
    )
    conn.commit()
    conn.close()


def get_recent_chats(user_id: int, limit=3):
    conn = sqlite3.connect("ai_memory.db")
    cur = conn.cursor()
    cur.execute(
        "SELECT message FROM chat_log WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in reversed(rows)]


def clear_chats(user_id: int):
    conn = sqlite3.connect("ai_memory.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM chat_log WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def get_summary(user_id: int):
    conn = sqlite3.connect("ai_memory.db")
    cur = conn.cursor()
    cur.execute("SELECT summary FROM user_summary WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else ""


def save_summary(user_id: int, summary: str):
    conn = sqlite3.connect("ai_memory.db")
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO user_summary (user_id, summary) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET summary=excluded.summary
        """,
        (user_id, summary),
    )
    conn.commit()
    conn.close()

# =========================================================
# 抽選（Lottery）SQLite ユーティリティ
# =========================================================

_LOTTERY_DB_LOCK = Lock()

def _db_connect():
    return sqlite3.connect("ai_memory.db")

def lottery_create_event(message_id: int, channel_id: int, guild_id: int, created_by: int,
                         ends_at_unix: int, winners_count: int, reward_coins: int):
    now = int(time.time())
    with _LOTTERY_DB_LOCK:
        conn = _db_connect()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO lottery_events
            (message_id, channel_id, guild_id, created_by, created_at, ends_at, winners_count, reward_coins, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (message_id, channel_id, guild_id, created_by, now, ends_at_unix, winners_count, reward_coins),
        )
        conn.commit()
        conn.close()

def lottery_add_entry(message_id: int, user_id: int) -> bool:
    now = int(time.time())
    with _LOTTERY_DB_LOCK:
        conn = _db_connect()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO lottery_entries (message_id, user_id, joined_at) VALUES (?, ?, ?)",
                (message_id, user_id, now),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

def lottery_get_event(message_id: int):
    with _LOTTERY_DB_LOCK:
        conn = _db_connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT message_id, channel_id, guild_id, created_by, ends_at, winners_count, reward_coins, status "
            "FROM lottery_events WHERE message_id=?",
            (message_id,),
        )
        row = cur.fetchone()
        conn.close()
    if not row:
        return None
    return {
        "message_id": int(row[0]),
        "channel_id": int(row[1]),
        "guild_id": int(row[2]),
        "created_by": int(row[3]),
        "ends_at": int(row[4]),
        "winners_count": int(row[5]),
        "reward_coins": int(row[6]),
        "status": str(row[7]),
    }

def lottery_list_entries(message_id: int) -> list[int]:
    with _LOTTERY_DB_LOCK:
        conn = _db_connect()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM lottery_entries WHERE message_id=?", (message_id,))
        rows = cur.fetchall()
        conn.close()
    return [int(r[0]) for r in rows]

def lottery_close_event(message_id: int):
    with _LOTTERY_DB_LOCK:
        conn = _db_connect()
        cur = conn.cursor()
        cur.execute("UPDATE lottery_events SET status='closed' WHERE message_id=?", (message_id,))
        conn.commit()
        conn.close()

def lottery_save_winners(message_id: int, winners: list[int], reward: int):
    now = int(time.time())
    with _LOTTERY_DB_LOCK:
        conn = _db_connect()
        cur = conn.cursor()
        for uid in winners:
            cur.execute(
                """
                INSERT OR REPLACE INTO lottery_winners
                (message_id, user_id, reward_coins, decided_at)
                VALUES (?, ?, ?, ?)
                """,
                (message_id, int(uid), int(reward), now),
            )
        conn.commit()
        conn.close()

def lottery_get_open_events_due(now_unix: int) -> list[int]:
    with _LOTTERY_DB_LOCK:
        conn = _db_connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT message_id FROM lottery_events WHERE status='open' AND ends_at <= ?",
            (int(now_unix),),
        )
        rows = cur.fetchall()
        conn.close()
    return [int(r[0]) for r in rows]

def dealer_hit_threshold_by_balance(balance: int) -> int:
    """
    ディーラーがどこまでヒットするか。
    残高が多いほどディーラーが強くなる＝プレイヤーが負けやすい。
    """
    if balance >= 100000:
        return 20
    if balance >= 50000:
        return 19
    if balance >= 20000:
        return 18
    return 17

# 沼・補填と同じ運営ロール
NUMA_SETUP_ROLE_ID = 1462688366431567872

def send_numa(channel, content: str):
    return channel.send(
        content,
        allowed_mentions=discord.AllowedMentions.none(),
    )

def has_numa_setup_role(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return any(r.id == NUMA_SETUP_ROLE_ID for r in member.roles)

# =========================================================
# Google Sheets 永続ストア
# =========================================================
def _env_int(name: str, default=None):
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except Exception:
        return default

GS_SERVICE_ACCOUNT_JSON = os.getenv("GS_SERVICE_ACCOUNT_JSON", "")
GS_SPREADSHEET_ID = os.getenv("GS_SPREADSHEET_ID", "")
GS_SHEET_NAME = os.getenv("GS_SHEET_NAME", "管理")
GS_COINS_SHEET_NAME = os.getenv("GS_COINS_SHEET_NAME", "読み取り用")
COINS_HEADERS = ["user_id", "coins"]

SHOP_CHANNEL_ID = _env_int("SHOP_CHANNEL_ID")
BJ_CHANNEL_ID = _env_int("BJ_CHANNEL_ID")
ADMIN_CHANNEL_ID = _env_int("ADMIN_CHANNEL_ID")
SEATO_USER_ID = _env_int("SEATO_USER_ID")

def _role_env(name: str):
    return _env_int(name)

TITLE_ROLE_1000    = 1462689131695050817  # 見習い
TITLE_ROLE_5000    = 1462688704220106849  # 常連
TITLE_ROLE_10000   = 1462689358925533300  # 策士
TITLE_ROLE_100000  = 1462689551435698280  # 伝説

ROLE_JP_FIRST   = 1462689604824989941  # 寵愛を受けし者
ROLE_JP_MULTI   = 1462689718364930171  # 選ばれし者
ROLE_BAR_MISS   = 1462689753760534618  # 弄ばれし者

ROLE_DAIKICHI_10 = 1462689802100146300  # 加護を受けし者
ROLE_DAIKYO_10   = 1462690066714460274  # 試されし者

ROLE_BJ_FIRSTWIN = 1462690190572261407  # 勝負師見習い
ROLE_BJ_3STREAK  = 1462690294779871405  # 勝負師
ROLE_BJ_100PLAY  = 1462690412559863932  # ブラックジャック職人
ROLE_BJ_BIGWIN   = 1462690562044854425  # 大勝負師
ROLE_BJ_BIGLOSE  = 1462690685944856677  # 破滅王

ROLE_NUMA_CLEAR  = 1462810553553780796  # 沼踏破者
ROLE_NUMA_LEGEND = 1462810693156737087  # 沼を支配せし者

AWARD_NUMA_CLEAR = "AWARD_NUMA_CLEAR"
AWARD_NUMA_LEGEND = "AWARD_NUMA_LEGEND"

SHOP_ITEMS = [
    {"key": "title_1000", "name": "🌱 ずんだ見習い", "price": 1000, "type": "role", "role_name": "ずんだ見習い"},
    {"key": "title_5000", "name": "🌿 ずんだ常連", "price": 5000, "type": "role", "role_name": "ずんだ常連"},
    {"key": "title_10000", "name": "🧠 ずんだの策士", "price": 10000, "type": "role", "role_name": "ずんだの策士"},
    {"key": "title_100000", "name": "👑 ずんだの伝説", "price": 100000, "type": "role", "role_name": "ずんだの伝説"},

    {"key": "item_1", "name": "アーマー50枚", "price": 2000, "type": "item", "notify": True, "repeatable": True},
    {"key": "item_2", "name": "5.56mm弾1000発（9mm弾に変更可）", "price": 2000, "type": "item", "notify": True, "repeatable": True},
    {"key": "item_3", "name": "武器１本（アタッチメント自由）", "price": 5000, "type": "item", "notify": True, "repeatable": True},
]

MANAGED_TITLE_ROLES = {
    TITLE_ROLE_1000,
    TITLE_ROLE_5000,
    TITLE_ROLE_10000,
    TITLE_ROLE_100000,

    ROLE_JP_FIRST,
    ROLE_JP_MULTI,
    ROLE_BAR_MISS,

    ROLE_DAIKICHI_10,
    ROLE_DAIKYO_10,

    ROLE_BJ_FIRSTWIN,
    ROLE_BJ_3STREAK,
    ROLE_BJ_100PLAY,
    ROLE_BJ_BIGWIN,
    ROLE_BJ_BIGLOSE,

    ROLE_NUMA_CLEAR,
    ROLE_NUMA_LEGEND,
}

USER_HEADERS = [
    "user_id",
    "coins",
    "title_role_id",
    "login_streak",
    "login_total",
    "daikichi_count",
    "daikyo_count",
    "bj_play_count",
    "bj_win_streak",
    "total_earned",
    "jackpot_count",
    "last_login_ymd",
    "owned_title_role_ids",
    "award_keys",
]


def normalize_spreadsheet_id(s: str) -> str:
    # ✅ URLでもIDでも受ける（/edit?usp=sharing 等が混ざっても吸収）
    if not s:
        return ""
    s = str(s).strip()

    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", s)
    if m:
        return m.group(1)

    s = s.split("/edit")[0]
    s = s.split("?")[0]
    return s.strip()


def _parse_ymd(s: str):
    try:
        y, m, d = map(int, (s or "").split("-"))
        return date(y, m, d)
    except Exception:
        return None


def merge_user_rows(base: dict, incoming: dict) -> dict:
    """
    同じ user_id の行が複数ある場合に、情報を「安全側」でまとめる。
    - coins は "大きい方"（足し算すると不正増殖になるため）
    - 各種カウント類は max
    - last_login_ymd は新しい日付
    - title_role_id は非0優先（incomingが非0なら上書き）
    - owned_title_role_ids / award_keys は和集合（CSV結合）
    """
    out = dict(base)

    out["coins"] = max(int(base.get("coins", 0) or 0), int(incoming.get("coins", 0) or 0))

    for k in [
        "login_streak", "login_total", "daikichi_count", "daikyo_count",
        "bj_play_count", "bj_win_streak", "total_earned", "jackpot_count"
    ]:
        out[k] = max(int(base.get(k, 0) or 0), int(incoming.get(k, 0) or 0))

    btr = int(base.get("title_role_id", 0) or 0)
    itr = int(incoming.get("title_role_id", 0) or 0)
    out["title_role_id"] = itr if itr != 0 else btr

    bd = _parse_ymd(str(base.get("last_login_ymd", "") or ""))
    id_ = _parse_ymd(str(incoming.get("last_login_ymd", "") or ""))
    if bd and id_:
        out["last_login_ymd"] = (id_ if id_ > bd else bd).strftime("%Y-%m-%d")
    elif id_:
        out["last_login_ymd"] = id_.strftime("%Y-%m-%d")
    elif bd:
        out["last_login_ymd"] = bd.strftime("%Y-%m-%d")
    else:
        out["last_login_ymd"] = str(base.get("last_login_ymd", "") or "") or str(incoming.get("last_login_ymd", "") or "")

    def csv_set(v):
        return set([x.strip() for x in str(v or "").split(",") if x.strip()])

    owned = csv_set(base.get("owned_title_role_ids", "")) | csv_set(incoming.get("owned_title_role_ids", ""))
    out["owned_title_role_ids"] = ",".join(sorted(owned)) if owned else ""

    awards = csv_set(base.get("award_keys", "")) | csv_set(incoming.get("award_keys", ""))
    out["award_keys"] = ",".join(sorted(awards)) if awards else ""

    return out

from decimal import Decimal, InvalidOperation

class SheetsStore:
    """
    - users(管理)シート: coins以外含む全データの正
    - coins(読み取り用)シート: coinsだけの正（A=user_id, B=coins）
    """
    def __init__(self):
        self._lock = Lock()
        self.gc = None
        self.sh = None

        self.ws_users = None     # 管理
        self.ws_config = None    # 設定
        self.ws_coins = None     # 読み取り用

        self.users: dict[int, dict] = {}
        self.config: dict[str, str] = {}

        self._uid_to_row: dict[int, int] = {}         # 管理: user_id -> row
        self._uid_to_row_coins: dict[int, int] = {}   # 読み取り用: user_id -> row

    # -----------------------------
    # 内部ユーティリティ
    # -----------------------------
    def _to_int_maybe(self, v) -> int:
        s = str(v or "").strip()
        if not s:
            return 0
        if s.startswith("'"):
            s = s[1:].strip()

        # 科学表記に対応（4.169E+17）
        try:
            if "e" in s.lower() or "." in s:
                d = Decimal(s)
                return int(d.to_integral_value())
        except InvalidOperation:
            pass

        m = re.search(r"\d+", s)
        return int(m.group(0)) if m else 0

    def _find_col_idx(self, header: list[str], key: str):
        k = (key or "").strip().lower()
        for i, h in enumerate(header):
            if (h or "").strip().lower() == k:
                return i
        return None

    # -----------------------------
    # init
    # -----------------------------
    def init(self):
        if not GS_SERVICE_ACCOUNT_JSON or not GS_SPREADSHEET_ID:
            raise RuntimeError("GS_SERVICE_ACCOUNT_JSON / GS_SPREADSHEET_ID が未設定です")

        info = json.loads(GS_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        self.gc = gspread.authorize(creds)

        sid = normalize_spreadsheet_id(GS_SPREADSHEET_ID)
        if not sid:
            raise RuntimeError("GS_SPREADSHEET_ID が空なのだ（IDかURLを設定するのだ）")
        self.sh = self.gc.open_by_key(sid)

        # 管理
        try:
            self.ws_users = self.sh.worksheet(GS_SHEET_NAME)
        except Exception:
            self.ws_users = self.sh.add_worksheet(title=GS_SHEET_NAME, rows=2000, cols=30)

        # 設定
        try:
            self.ws_config = self.sh.worksheet("設定")
        except Exception:
            self.ws_config = self.sh.add_worksheet(title="設定", rows=200, cols=5)

        # coins
        try:
            self.ws_coins = self.sh.worksheet(GS_COINS_SHEET_NAME)
        except Exception:
            self.ws_coins = self.sh.add_worksheet(title=GS_COINS_SHEET_NAME, rows=2000, cols=5)

        self._ensure_headers_users()
        self._ensure_headers_coins()
        self._load_config()
        self._load_users_and_index()
        self._load_coins_and_apply()

    # -----------------------------
    # headers
    # -----------------------------
    def _ensure_headers_users(self):
        with self._lock:
            header = self.ws_users.row_values(1)
            if not header:
                self.ws_users.update("A1", [USER_HEADERS])
                return
            if header != USER_HEADERS:
                merged = list(header)
                for h in USER_HEADERS:
                    if h not in merged:
                        merged.append(h)
                self.ws_users.update("A1", [merged])

    def _ensure_headers_coins(self):
        with self._lock:
            header = self.ws_coins.row_values(1)
            if not header:
                self.ws_coins.update("A1", [COINS_HEADERS])
                return
            if len(header) < 2 or header[0] != "user_id" or header[1] != "coins":
                self.ws_coins.update("A1", [COINS_HEADERS + header[2:]])

    # -----------------------------
    # config
    # -----------------------------
    def _load_config(self):
        with self._lock:
            values = self.ws_config.get_all_values()
            cfg = {}
            for r in values[1:]:
                if len(r) >= 2 and r[0]:
                    cfg[r[0]] = r[1]
            self.config = cfg

    def _save_config_kv(self, key: str, value: str):
        with self._lock:
            values = self.ws_config.get_all_values()
            if not values:
                self.ws_config.update("A1", [["key", "value"]])
                values = self.ws_config.get_all_values()

            for idx, row in enumerate(values[1:], start=2):
                if len(row) >= 1 and row[0] == key:
                    self.ws_config.update(f"B{idx}", [[value]])
                    self.config[key] = value
                    return

            self.ws_config.append_row([key, value])
            self.config[key] = value

    def save_config_once(self, key: str, value: str) -> bool:
        if key in self.config and self.config[key]:
            return False
        self._save_config_kv(key, value)
        return True

    # -----------------------------
    # users load (管理)
    # -----------------------------
    def _normalize_user_row(self, uid: int, r: dict):
        def i(name, default=0):
            try:
                return int(r.get(name) or default)
            except Exception:
                return default

        def s(name, default=""):
            v = r.get(name)
            return str(v).strip() if v is not None else default

        return {
            "user_id": uid,
            "coins": i("coins", 0),
            "title_role_id": i("title_role_id", 0),
            "login_streak": i("login_streak", 0),
            "login_total": i("login_total", 0),
            "daikichi_count": i("daikichi_count", 0),
            "daikyo_count": i("daikyo_count", 0),
            "bj_play_count": i("bj_play_count", 0),
            "bj_win_streak": i("bj_win_streak", 0),
            "total_earned": i("total_earned", 0),
            "jackpot_count": i("jackpot_count", 0),
            "last_login_ymd": s("last_login_ymd", ""),
            "owned_title_role_ids": s("owned_title_role_ids", ""),
            "award_keys": s("award_keys", ""),
        }

    def _load_users_and_index(self):
        with self._lock:
            header = self.ws_users.row_values(1)
            if not header:
                self.ws_users.update("A1", [USER_HEADERS])
                header = USER_HEADERS

            all_values = self.ws_users.get_all_values()
            users: dict[int, dict] = {}
            uid_to_row: dict[int, int] = {}
            duplicates: dict[int, list[int]] = {}

            for row_idx, row in enumerate(all_values[1:], start=2):
                if not row:
                    continue

                r = {h: (row[i] if i < len(row) else "") for i, h in enumerate(header)}
                uid = self._to_int_maybe(r.get("user_id"))
                if uid <= 0:
                    continue

                incoming = self._normalize_user_row(uid, r)

                if uid not in users:
                    users[uid] = incoming
                    uid_to_row[uid] = row_idx
                else:
                    users[uid] = merge_user_rows(users[uid], incoming)
                    duplicates.setdefault(uid, []).append(row_idx)

            self.users = users
            self._uid_to_row = uid_to_row

    # -----------------------------
    # coins load (読み取り用)
    # -----------------------------
    def _load_coins_and_apply(self):
        with self._lock:
            values = self.ws_coins.get_all_values()
            if not values or len(values) < 2:
                self._uid_to_row_coins = {}
                print("[Coins] empty sheet or only header")
                return

            header = values[0]
            uid_col = self._find_col_idx(header, "user_id")
            coin_col = self._find_col_idx(header, "coins")
            if uid_col is None:
                uid_col = 0
            if coin_col is None:
                coin_col = 1

            uid_to_row: dict[int, int] = {}
            coins_map: dict[int, int] = {}

            for row_idx, row in enumerate(values[1:], start=2):
                if not row:
                    continue
                uid = self._to_int_maybe(row[uid_col] if uid_col < len(row) else "")
                if uid <= 0:
                    continue
                coins = self._to_int_maybe(row[coin_col] if coin_col < len(row) else "")
                uid_to_row[uid] = row_idx
                coins_map[uid] = coins

            self._uid_to_row_coins = uid_to_row

            for uid, c in coins_map.items():
                if uid in self.users:
                    self.users[uid]["coins"] = c
                else:
                    self.users[uid] = {
                        "user_id": uid,
                        "coins": c,
                        "title_role_id": 0,
                        "login_streak": 0,
                        "login_total": 0,
                        "daikichi_count": 0,
                        "daikyo_count": 0,
                        "bj_play_count": 0,
                        "bj_win_streak": 0,
                        "total_earned": 0,
                        "jackpot_count": 0,
                        "last_login_ymd": "",
                        "owned_title_role_ids": "",
                        "award_keys": "",
                    }

    # -----------------------------
    # public
    # -----------------------------
    def get_user(self, uid: int):
        u = self.users.get(uid)
        if not u:
            u = {
                "user_id": uid,
                "coins": 0,
                "title_role_id": 0,
                "login_streak": 0,
                "login_total": 0,
                "daikichi_count": 0,
                "daikyo_count": 0,
                "bj_play_count": 0,
                "bj_win_streak": 0,
                "total_earned": 0,
                "jackpot_count": 0,
                "last_login_ymd": "",
                "owned_title_role_ids": "",
                "award_keys": "",
            }
            self.users[uid] = u
        return u

    def _upsert_coin(self, uid: int, coins: int):
        uid_str = str(uid)  # ← ' を付けない
        with self._lock:
            idx = self._uid_to_row_coins.get(uid)
            if idx is None:
                self.ws_coins.append_row([uid_str, int(coins)])
                self._uid_to_row_coins[uid] = len(self.ws_coins.get_all_values())
            else:
                # A列も念のため上書き（行ズレ対策）
                self.ws_coins.update(
                    range_name=f"A{idx}:B{idx}",
                    values= [[uid_str, int(coins)]],
                )

    def upsert_user(self, u: dict):
        with self._lock:
            header = self.ws_users.row_values(1)
            if not header:
                self.ws_users.update("A1", [USER_HEADERS])
                header = USER_HEADERS

            uid = int(u["user_id"])
            values = [u.get(h, "") for h in header]

            # user_id を文字列（E+対策）
            if "user_id" in header:
                values[header.index("user_id")] = str(uid)

            # title_role_id も文字列（E+18対策）
            if "title_role_id" in header:
                try:
                    rid = int(u.get("title_role_id", 0) or 0)
                except Exception:
                    rid = 0
                values[header.index("title_role_id")] = f"'{rid}" if rid > 0 else "0"

            idx = self._uid_to_row.get(uid)
            if idx is None:
                self.ws_users.append_row(values)
                self._uid_to_row[uid] = len(self.ws_users.get_all_values())
            else:
                start_a1 = rowcol_to_a1(idx, 1)
                end_a1 = rowcol_to_a1(idx, len(values))
                self.ws_users.update(
                    range_name=f"{start_a1}:{end_a1}",
                    values=[values],
                )

        # coins は必ず同期（←この行は def upsert_user と同じ深さじゃなくてOK）
        self._upsert_coin(uid, int(u.get("coins", 0) or 0))

store = SheetsStore()

async def sheets_init_async():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, store.init)


async def sheets_upsert_async(u: dict):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: store.upsert_user(u))


async def sheets_reload_users_async():
    loop = asyncio.get_running_loop()

    def _reload():
        store._load_users_and_index()   # 管理
        store._load_coins_and_apply()   # 読み取り用（coins上書き）

    await loop.run_in_executor(None, _reload)


async def sheets_save_config_once_async(key: str, value: str) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: store.save_config_once(key, value))


# =========================================================
# 権限＆チャンネルチェック
# =========================================================
def is_admin_user(interaction: discord.Interaction) -> bool:
    try:
        return interaction.user.guild_permissions.administrator
    except Exception:
        return False


def is_in_channel(interaction: discord.Interaction, channel_id):
    if channel_id is None:
        return True
    try:
        return interaction.channel_id == channel_id
    except Exception:
        return True

async def safe_defer(interaction: discord.Interaction, *, ephemeral: bool = True) -> bool:
    """
    defer を安全に行う。
    成功したら True。interaction が死んでいた等で失敗したら False。
    """
    try:
        await interaction.response.defer(ephemeral=ephemeral)
        return True
    except (discord.NotFound, discord.errors.InteractionResponded):
        return False
    except Exception:
        traceback.print_exc()
        return False


async def safe_send(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool = True,
    view: discord.ui.View | None = None,
):
    try:
        if interaction.response.is_done():
            if view is None:
                return await interaction.followup.send(content, ephemeral=ephemeral)
            return await interaction.followup.send(content, ephemeral=ephemeral, view=view)

        # response 側
        if view is None:
            return await interaction.response.send_message(content, ephemeral=ephemeral)
        return await interaction.response.send_message(content, ephemeral=ephemeral, view=view)

    except (discord.NotFound, discord.errors.InteractionResponded):
        # interaction が失効してても落とさない
        try:
            if view is None:
                return await interaction.followup.send(content, ephemeral=ephemeral)
            return await interaction.followup.send(content, ephemeral=ephemeral, view=view)
        except Exception:
            return None
None

# =========================================================
# コイン・称号ユーティリティ
# =========================================================
def parse_ids_csv(s: str) -> set[int]:
    out = set()
    if not s:
        return out
    for p in s.split(","):
        p = p.strip()
        if not p:
            continue
        try:
            out.add(int(p))
        except Exception:
            pass
    return out


def ids_to_csv(ids: set[int]) -> str:
    return ",".join(str(i) for i in sorted(ids))


def award_keys_set(u: dict) -> set[str]:
    return set([x.strip() for x in (u.get("award_keys") or "").split(",") if x.strip()])


def set_award_key(u: dict, key: str):
    ks = award_keys_set(u)
    ks.add(key)
    u["award_keys"] = ",".join(sorted(ks))


def title_inventory(u: dict) -> set[int]:
    return parse_ids_csv(u.get("owned_title_role_ids", ""))


def add_title_to_inventory(u: dict, role_id: int):
    inv = title_inventory(u)
    inv.add(role_id)
    u["owned_title_role_ids"] = ids_to_csv(inv)


async def remove_managed_titles(member: discord.Member):
    to_remove = [r for r in member.roles if r.id in MANAGED_TITLE_ROLES]
    if to_remove:
        try:
            await member.remove_roles(*to_remove, reason="Bot称号の付け替え")
        except Exception:
            pass


async def apply_title_role(member: discord.Member, role_id: int):
    await remove_managed_titles(member)
    role = member.guild.get_role(role_id)
    if role:
        await member.add_roles(role, reason="Bot称号付与")

def _norm_role_name(s: str) -> str:
    if s is None:
        return ""
    s = str(s)

    # 前後空白除去 + 全角スペースを半角へ
    s = s.replace("\u3000", " ").strip()

    # 連続スペース圧縮
    s = re.sub(r"\s+", " ", s)

    # ゼロ幅文字を除去（よく事故る）
    s = s.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")

    # 大文字小文字差を吸収（英字が混ざってもOK）
    return s.casefold()


def find_role_by_name(guild: discord.Guild, role_name: str) -> discord.Role | None:
    target = _norm_role_name(role_name)

    # 1) まず完全一致（正規化後）
    for r in guild.roles:
        if _norm_role_name(r.name) == target:
            return r

    # 2) 次に「含む」（正規化後）※少し緩める
    for r in guild.roles:
        if target and target in _norm_role_name(r.name):
            return r

    return None

def calc_login_extra(streak: int) -> int:
    s = max(1, streak)
    if s == 1:
        b1 = 0
    elif s == 2:
        b1 = 3
    elif s in (3, 4):
        b1 = 5
    elif s in (5, 6):
        b1 = 8
    else:
        b1 = 10

    if s <= 7:
        b2 = 0
    else:
        d = s - 7
        if d == 1:
            b2 = 3
        elif d == 2:
            b2 = 5
        elif d == 3:
            b2 = 8
        else:
            b2 = 10
    return min(b1 + b2, 20)


# =========================================================
# 占い（AI）
# =========================================================
FORTUNE_CHOICES = ["大吉", "中吉", "小吉", "吉", "末吉", "凶", "大凶"]
FORTUNE_COIN = {"大吉": 10, "中吉": 6, "小吉": 4, "吉": 3, "末吉": 2, "凶": 1, "大凶": 0}

async def ai_fortune_message() -> tuple[str, str, str]:
    fortune = random.choices(
        population=FORTUNE_CHOICES,
        weights=[5, 12, 16, 25, 18, 16, 8],
        k=1,
    )[0]

    # 大凶だけ「難しい/入手困難」指定、それ以外は日常品
    lucky_rule = (
        "大凶のときは、現実に入手が難しい・レア・困難な物にしてほしい。"
        if fortune == "大凶"
        else
        "大凶以外のときは、日常で手に入る身近な物にしてほしい。"
    )

    try:
        prompt = [
            {"role": "system", "content": ZUNDAMON_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"今日の占い結果は「{fortune}」なのだ。\n"
                    "短めの一言コメントと、ラッキーアイテムを1つ出してほしいのだ。\n"
                    f"{lucky_rule}\n"
                    "出力形式は次の2行だけにしてほしいのだ（余計な説明は禁止なのだ）:\n"
                    "コメント：<一言>\n"
                    "ラッキー：<アイテム名>\n"
                    "アイテム名は10〜20文字程度で、記号や絵文字は使わないでほしいのだ。"
                ),
            },
        ]

        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model="gpt-4o-mini",
                messages=prompt,
                max_tokens=120,
                temperature=0.9,
            ),
        )

        out = (resp.choices[0].message.content or "").strip()

        # 2行形式から安全に抽出
        fortune_msg = ""
        lucky_item = ""

        for line in out.splitlines():
            line = line.strip()
            if line.startswith("コメント："):
                fortune_msg = line.replace("コメント：", "", 1).strip()
            elif line.startswith("ラッキー："):
                lucky_item = line.replace("ラッキー：", "", 1).strip()

        # フォールバック
        if not fortune_msg:
            fortune_msg = "今日は肩の力を抜くのだよ。"
        if not lucky_item:
            lucky_item = "ハンカチ" if fortune != "大凶" else "入手困難な限定アイテム"

        # 念のためのクリーニング（長すぎ/空白だけ対策）
        lucky_item = re.sub(r"\s+", " ", lucky_item).strip()
        if len(lucky_item) > 30:
            lucky_item = lucky_item[:30].strip()

        return fortune, fortune_msg, lucky_item

    except Exception as e:
        print("ai_fortune_message error:", e)
        traceback.print_exc()

    # 完全フォールバック
    fallback_msg = {
        "大吉": "今日は強気でいくのだ！",
        "中吉": "いい流れなのだよ。",
        "小吉": "コツコツが勝つのだ。",
        "吉": "安定がいちばんのだ。",
        "末吉": "焦らずいくのだよ。",
        "凶": "慎重に行動するのだ。",
        "大凶": "今日は守りに徹するのだ…！",
    }
    lucky_item = "ハンカチ" if fortune != "大凶" else "入手困難な限定アイテム"
    return fortune, fallback_msg.get(fortune, "無理せずいくのだ。"), lucky_item


async def maybe_award_hidden_titles(
    interaction: discord.Interaction, u: dict, just_events: set[str]
):
    member = interaction.user
    if not isinstance(member, discord.Member):
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            return

    async def award_once(key: str, role_id, message: str):
        if role_id is None:
            return
        if key in award_keys_set(u):
            return
        add_title_to_inventory(u, role_id)
        u["title_role_id"] = role_id
        set_award_key(u, key)
        await apply_title_role(member, role_id)
        await sheets_upsert_async(u)
        try:
            await interaction.followup.send(message, ephemeral=True)
        except Exception:
            pass

    if u["daikichi_count"] >= 10:
        await award_once(
            "AWARD_DAIKICHI_10",
            ROLE_DAIKICHI_10,
            "🎉🎉🎉\n✨【偉業達成】✨\n\nあなたは「大吉」を10回引いたのだ！\n特別ロール\n🌱「ずんだの加護を受けし者」\nを獲得したのだよ！\n🎉🎉🎉",
        )

    if u["daikyo_count"] >= 10:
        await award_once(
            "AWARD_DAIKYO_10",
            ROLE_DAIKYO_10,
            "🎉🎉🎉\n✨【逆境の証】✨\n\nあなたは「大凶」を10回も引いたのだ…\nここまで来ると才能なのだよ！\n💀「ずんだに試されし者」\nを獲得したのだ！\n🎉🎉🎉",
        )

    if u["jackpot_count"] >= 1:
        await award_once(
            "AWARD_JP_FIRST",
            ROLE_JP_FIRST,
            "🎉🎉🎉\n✨【奇跡の瞬間】✨\n\n/diceでジャックポットを\n初めて引き当てたのだ！\n🎰「ずんだの寵愛を受けし者」\nを獲得したのだよ！\n🎉🎉🎉",
        )

    if u["jackpot_count"] >= 3:
        await award_once(
            "AWARD_JP_MULTI_3",
            ROLE_JP_MULTI,
            "🎉🎉🎉\n✨【常識外れ】✨\n\nあなたはジャックポットを\n何度も引き当てたのだ…！\nこれはもう偶然じゃないのだ！\n🎰🎰「ずんだに選ばれし者」\nを獲得したのだよ！\n🎉🎉🎉",
        )

    if "BAR_MISS_EVENT" in just_events:
        await award_once(
            "AWARD_BAR_MISS",
            ROLE_BAR_MISS,
            "🎉🎉🎉\n✨【惜敗の極み】✨\n\n7・7・BARの後、\n期待を背負って外したのだ…！\n🍀「ずんだに弄ばれし者」\nを獲得したのだよ！\n🎉🎉🎉",
        )

    if u["bj_win_streak"] >= 1 and "BJ_WIN_EVENT" in just_events:
        await award_once(
            "AWARD_BJ_FIRSTWIN",
            ROLE_BJ_FIRSTWIN,
            "🎉🎉🎉\n✨【初勝利】✨\n\nブラックジャックで\n初めて勝利したのだ！\n🎴「ずんだの勝負師見習い」\nを獲得したのだよ！\n🎉🎉🎉",
        )

    if u["bj_win_streak"] >= 3:
        await award_once(
            "AWARD_BJ_3STREAK",
            ROLE_BJ_3STREAK,
            "🎉🎉🎉\n✨【波に乗れ】✨\n\nブラックジャックで\n3連勝を達成したのだ！\n🔥「ずんだの勝負師」\nを獲得したのだよ！\n🎉🎉🎉",
        )

    if u["bj_play_count"] >= 100:
        await award_once(
            "AWARD_BJ_100PLAY",
            ROLE_BJ_100PLAY,
            "🎉🎉🎉\n✨【熟練の域】✨\n\nブラックジャックを\n100回以上プレイしたのだ！\n🃏「ずんだのブラックジャック職人」\nを獲得したのだよ！\n🎉🎉🎉",
        )

    if "BJ_BIGWIN_EVENT" in just_events:
        await award_once(
            "AWARD_BJ_BIGWIN",
            ROLE_BJ_BIGWIN,
            "🎉🎉🎉\n✨【一攫千金】✨\n\nブラックジャックで\n1回の勝負で\n1,000コイン以上を\n獲得したのだ！\n💎「ずんだの大勝負師」\nを獲得したのだよ！\n🎉🎉🎉",
        )

    if "BJ_BIGLOSE_EVENT" in just_events:
        await award_once(
            "AWARD_BJ_BIGLOSE",
            ROLE_BJ_BIGLOSE,
            "🎉🎉🎉\n✨【破滅への道】✨\n\nブラックジャックで\n一度に1,000コイン以上\n失ったのだ……\n💀「ずんだの破滅王」\nを獲得したのだよ！\n🎉🎉🎉",
        )


# =========================================================
# 入口メッセージ（ショップ・BJ）
# =========================================================
async def notify_exchange(interaction: discord.Interaction, buyer: discord.User, item_name: str, price: int):
    if not SEATO_USER_ID:
        return

    try:
        target = bot.get_user(SEATO_USER_ID)
        if target is None:
            target = await bot.fetch_user(SEATO_USER_ID)
        if target is None:
            return

        dm = target.dm_channel
        if dm is None:
            dm = await target.create_dm()

        guild_name = interaction.guild.name if interaction.guild else "DM"
        channel_name = getattr(interaction.channel, "name", "不明")

        await dm.send(
            "🔔 交換通知なのだ\n"
            f"- ユーザー：{buyer}（{buyer.id}）\n"
            f"- アイテム：{item_name}\n"
        )

    except discord.Forbidden:
        print("[notify_exchange] DM拒否で送信できないのだ")
    except Exception:
        traceback.print_exc()

class ShopBuySelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="購入/交換する商品を選ぶのだ",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        # ここでは「選んだ」だけ。購入確定はボタン。
        parent: "ShopBuyConfirmView" = self.view  # type: ignore
        parent.selected_key = self.values[0]

        item = next((x for x in SHOP_ITEMS if x["key"] == parent.selected_key), None)
        if not item:
            return await interaction.response.send_message("その商品は無効なのだ", ephemeral=True)

        name = item["name"]
        price = int(item["price"])

        await interaction.response.send_message(
            f"✅ 選択したのだ：**{name}**（{price}コイン）\n"
            f"下の **確定** を押すと購入/交換になるのだ。",
            ephemeral=True,
        )

class ShopBuyConfirmView(discord.ui.View):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(timeout=120)
        self.selected_key: str | None = None
        self.add_item(ShopBuySelect(options))

    @discord.ui.button(label="✅ 確定", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 404 Unknown interaction 対策：defer自体が失敗することがある
        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.NotFound, discord.errors.InteractionResponded):
            # すでに応答済み or 失効してたら何もしない（ここで落とさない）
            pass

        if not self.selected_key:
            try:
                return await interaction.followup.send("先に商品を選ぶのだ", ephemeral=True)
            except Exception:
                return

        item = next((x for x in SHOP_ITEMS if x["key"] == self.selected_key), None)
        if not item:
            try:
                return await interaction.followup.send("その商品は無効なのだ", ephemeral=True)
            except Exception:
                return

        async with get_user_lock(interaction.user.id):
            u = store.get_user(interaction.user.id)

            name = item["name"]
            price = int(item["price"])
            rid = item.get("role_id")
            item_type = item.get("type", "role" if rid else "item")

            if int(u.get("coins", 0) or 0) < price:
                try:
                    return await interaction.followup.send("コインが足りないのだ", ephemeral=True)
                except Exception:
                    return

            # -------------------------
            # ロール商品
            # -------------------------
            if item_type != "item":
                if interaction.guild is None:
                    try:
                        return await interaction.followup.send(
                            "サーバー内でのみ購入できる称号なのだ",
                            ephemeral=True,
                        )
                    except Exception:
                        return

                role_obj = None
                role_id = int(item.get("role_id") or 0)

                # role_id が無ければ role_name から探す
                if role_id <= 0:
                    role_name = (item.get("role_name") or "").strip()
                    if role_name:
                        role_obj = find_role_by_name(interaction.guild, role_name)
                        role_id = role_obj.id if role_obj else 0

                        # デバッグ出したいならここ（必要なときだけ）
                        if role_obj is None:
                            print("[ShopRole] NOT FOUND:", role_name)

                if role_id <= 0:
                    try:
                        return await interaction.followup.send(
                            "この称号ロールがサーバーに見つからないのだ（ロール名が一致してるか確認なのだ）",
                            ephemeral=True,
                        )
                    except Exception:
                        return

                owned = title_inventory(u)
                if role_id in owned:
                    try:
                        return await interaction.followup.send("それはもう購入済みなのだ", ephemeral=True)
                    except Exception:
                        return

                # 反映
                u["coins"] -= price
                add_title_to_inventory(u, role_id)
                u["title_role_id"] = role_id

                member = interaction.user
                if not isinstance(member, discord.Member):
                    member = await interaction.guild.fetch_member(interaction.user.id)

                await apply_title_role(member, role_id)
                await sheets_upsert_async(u)

                try:
                    return await interaction.followup.send(
                        (
                            "🎁 **購入完了**なのだ！\n"
                            f"{name}\n"
                            f"消費：-{price} コイン\n"
                            f"残高：{u['coins']} コインなのだ\n"
                            "👍 完了なのだ"
                        ),
                        ephemeral=True,
                    )
                except Exception:
                    return

            # -------------------------
            # 交換アイテム（ここは1回だけ）
            # -------------------------
            u["coins"] -= price
            await sheets_upsert_async(u)

            if item.get("notify"):
                await notify_exchange(interaction, interaction.user, name, price)

            try:
                return await interaction.followup.send(
                    (
                        "🎁 **交換完了**なのだ！\n"
                        f"{name}\n"
                        f"消費：-{price} コイン\n"
                        f"残高：{u['coins']} コインなのだ\n"
                    ),
                    ephemeral=True,
                )
            except Exception:
                return

    @discord.ui.button(label="❌ キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_message("キャンセルしたのだ", ephemeral=True)
        except (discord.NotFound, discord.errors.InteractionResponded):
            try:
                await interaction.followup.send("キャンセルしたのだ", ephemeral=True)
            except Exception:
                pass
        self.stop()

class TitleAssignSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="付与する称号を選ぶのだ",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.NotFound, discord.errors.InteractionResponded):
            pass

        u = store.get_user(interaction.user.id)

        rid = int(self.values[0])
        owned = title_inventory(u)

        if rid not in owned:
            return await interaction.followup.send("その称号は持っていないのだ", ephemeral=True)

        member = interaction.user
        if not isinstance(member, discord.Member):
            member = await interaction.guild.fetch_member(interaction.user.id)

        await apply_title_role(member, rid)
        u["title_role_id"] = rid
        await sheets_upsert_async(u)

        role = interaction.guild.get_role(rid)
        await interaction.followup.send(
            f"🎖️ {role.name if role else '称号'} を付与したのだ",
            ephemeral=True,
        )


class ShopEntryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🛒 ショップを開く",
        style=discord.ButtonStyle.primary,
        custom_id="shop_open_btn",
    )
    async def shop_open(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_in_channel(interaction, SHOP_CHANNEL_ID):
            return await interaction.response.send_message(
                "このチャンネルでは使えないのだ",
                ephemeral=True,
            )

        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.NotFound, discord.errors.InteractionResponded):
            pass

        u = store.get_user(interaction.user.id)
        owned = title_inventory(u)

        lines = []
        options = []

        for it in SHOP_ITEMS:
            name = it["name"]
            price = int(it["price"])
            item_type = it.get("type", "item")

            status = []

            if item_type == "role":
                role_name = (it.get("role_name") or "").strip()
                if interaction.guild and role_name:
                    role = find_role_by_name(interaction.guild, role_name)
                    if role and role.id in owned:
                        status.append("購入済み")

            if u["coins"] < price:
                status.append("残高不足")

            if item_type == "item" and it.get("repeatable", True):
                status.append("何度でも交換可")

            status_text = f"（{' / '.join(status)}）" if status else ""
            lines.append(f"- {name}：{price}コイン {status_text}")

            can_buy = (u["coins"] >= price) and ("購入済み" not in status)
            if can_buy:
                options.append(
                    discord.SelectOption(
                        label=f"{name}（{price}）",
                        value=it["key"],
                    )
                )

        msg = (
            "🏷️ ショップ/交換所なのだ\n\n"
            f"現在の残高：{u['coins']} コイン\n\n"
            "【商品一覧】\n" + "\n".join(lines)
        )

        if not options:
            return await interaction.followup.send(
                msg + "\n\n（購入/交換できる商品が今はないのだ）",
                ephemeral=True,
            )

        view = ShopBuyConfirmView(options)
        await interaction.followup.send(
            msg + "\n\n商品を選んで、✅確定 を押すのだ",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="🎖️ 称号を付与する",
        style=discord.ButtonStyle.secondary,
        custom_id="shop_title_assign_btn",
    )
    async def title_assign(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_in_channel(interaction, SHOP_CHANNEL_ID):
            return await interaction.response.send_message(
                "このチャンネルでは使えないのだ",
                ephemeral=True,
            )

        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.NotFound, discord.errors.InteractionResponded):
            pass

        u = store.get_user(interaction.user.id)
        owned = title_inventory(u)

        opts = []
        for rid in sorted(owned):
            role = interaction.guild.get_role(rid) if interaction.guild else None
            if not role:
                continue
            opts.append(discord.SelectOption(label=role.name, value=str(rid)))

        if not opts:
            return await interaction.followup.send("付与できる称号がないのだ", ephemeral=True)

        view = discord.ui.View(timeout=60)
        view.add_item(TitleAssignSelect(opts))
        await interaction.followup.send("付与する称号を選ぶのだ", view=view, ephemeral=True)

    @discord.ui.button(
        label="🎁 ログインボーナス",
        style=discord.ButtonStyle.success,
        custom_id="shop_daily_btn",
    )
    async def daily(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        async def send_ephemeral(text: str):
            try:
                if interaction.response.is_done():
                    return await interaction.followup.send(text, ephemeral=True)
                return await interaction.response.send_message(text, ephemeral=True)
            except Exception:
                return None

        if not is_in_channel(interaction, SHOP_CHANNEL_ID):
            return await send_ephemeral("このチャンネルでは使えないのだ")

        try:
            async with get_user_lock(interaction.user.id):
                u = store.get_user(interaction.user.id)

                today = datetime.now(JST).date()
                last = None
                ymd = u.get("last_login_ymd")

                if ymd:
                    try:
                        y, m, d = map(int, ymd.split("-"))
                        last = date(y, m, d)
                    except Exception:
                        last = None

                if last == today:
                    return await send_ephemeral("今日はもう受け取っているのだ")

                if last == (today - timedelta(days=1)):
                    u["login_streak"] += 1
                else:
                    u["login_streak"] = 1

                u["login_total"] += 1
                u["last_login_ymd"] = today.strftime("%Y-%m-%d")

                base = 10
                extra = calc_login_extra(u["login_streak"])
                streak_gain = base + extra

                # ✅ 3つ返るので 3つで受ける
                fortune, fortune_msg, lucky_item = await ai_fortune_message()
                fortune_gain = FORTUNE_COIN.get(fortune, 0)

                total_gain = streak_gain + fortune_gain
                u["coins"] += total_gain
                u["total_earned"] += total_gain

                if fortune == "大吉":
                    u["daikichi_count"] += 1
                if fortune == "大凶":
                    u["daikyo_count"] += 1

                await sheets_upsert_async(u)

                msg = (
                    "🎁 ログインボーナスなのだ\n\n"
                    f"連続ログイン：{u['login_streak']}日\n"
                    f"+{streak_gain} コイン（通常+10 / 連続+{extra}）\n\n"
                    f"🔮 今日の占い：{fortune}\n"
                    f"{fortune_msg}\n"
                    f"ラッキーアイテム：{lucky_item}\n"
                    f"+{fortune_gain} コイン\n\n"
                    f"現在の残高：{u['coins']} コインなのだ"
                )

                await send_ephemeral(msg)

                await maybe_award_hidden_titles(interaction, u, just_events=set())

        except Exception as e:
            print("shop daily error:", e)
            traceback.print_exc()
            await send_ephemeral("ログイン処理でエラーが出たのだ…（Renderログを見てほしいのだ）")

    @discord.ui.button(
        label="💰 残高",
        style=discord.ButtonStyle.secondary,
        custom_id="shop_balance_btn",
    )
    async def balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        u = store.get_user(interaction.user.id)
        try:
            await interaction.response.send_message(
                f"現在の残高：{u['coins']} コインなのだ",
                ephemeral=True,
            )
        except (discord.NotFound, discord.errors.InteractionResponded):
            try:
                await interaction.followup.send(
                    f"現在の残高：{u['coins']} コインなのだ",
                    ephemeral=True,
                )
            except Exception:
                pass

# =========================================================
# 抽選参加 View
# =========================================================
class LotteryJoinView(discord.ui.View):
    def __init__(self, message_id: int):
        super().__init__(timeout=None)
        self.message_id = int(message_id)

    @discord.ui.button(
        label="🎟️ 抽選に参加",
        style=discord.ButtonStyle.success,
        custom_id="lottery_join_btn",
    )
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        ev = lottery_get_event(self.message_id)
        if not ev or ev["status"] != "open":
            return await interaction.response.send_message(
                "この抽選はもう締め切ったのだ",
                ephemeral=True,
            )

        ok = lottery_add_entry(self.message_id, interaction.user.id)
        if not ok:
            return await interaction.response.send_message(
                "もう参加済みなのだ",
                ephemeral=True,
            )

        cnt = len(lottery_list_entries(self.message_id))
        await interaction.response.send_message(
            f"参加したのだ！（現在 {cnt} 人）",
            ephemeral=True,
        )

# =========================================================
# /setup_shop と /setup_bj （最初の1回のみ）
# =========================================================
@bot.tree.command(name="setup_shop", description="ショップ入口メッセージを設置するのだ（最初の1回のみ）")
async def setup_shop_cmd(interaction: discord.Interaction):
    if not is_admin_user(interaction):
        return await interaction.response.send_message("権限がないのだ", ephemeral=True)
    if SHOP_CHANNEL_ID and interaction.channel_id != SHOP_CHANNEL_ID:
        return await interaction.response.send_message(
            "指定のショップチャンネルで実行するのだ",
            ephemeral=True,
        )

    await interaction.response.defer(ephemeral=True)

    if store.config.get("shop_entry_message_id"):
        return await interaction.followup.send("ショップ入口はもう設置済みなのだ", ephemeral=True)

    content = (
        "🏷️ ずんだもんショップ\n\n"
        "・称号を購入できるのだ\n"
        "・ログインボーナスが受け取れるのだ\n"
        "・残高確認もできるのだ\n"
    )
    msg = await interaction.channel.send(content, view=ShopEntryView())
    ok = await sheets_save_config_once_async("shop_entry_message_id", str(msg.id))
    if not ok:
        return await interaction.followup.send("もう設置済みなのだ", ephemeral=True)

    await interaction.followup.send("ショップ入口を設置したのだ", ephemeral=True)

@bot.tree.command(name="starter100", description="初回特典で100コインを受け取るのだ（1回限定）")
async def starter100_cmd(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.NotFound:
        return

    try:
        async with get_user_lock(interaction.user.id):
            u = store.get_user(interaction.user.id)

            if "STARTER100_USED" in award_keys_set(u):
                return await interaction.followup.send("もう受け取っているのだ", ephemeral=True)

            u["coins"] += 100
            u["total_earned"] += 100
            set_award_key(u, "STARTER100_USED")

            await sheets_upsert_async(u)

        await interaction.followup.send(
            f"🎁 初回特典なのだ！\n+100コイン\n\n現在の残高：{u['coins']} コインなのだ",
            ephemeral=True,
        )

    except Exception as e:
        print("starter100 error:", e)
        traceback.print_exc()
        try:
            await interaction.followup.send("処理に失敗したのだ…（ログ確認なのだ）", ephemeral=True)
        except Exception:
            pass

# =========================================================
# /ai（既存：表示形式そのまま）
# =========================================================
@bot.tree.command(name="ai", description="ずんだもんとおしゃべりするのだ")
@app_commands.describe(message="ずんだもんに話しかける内容")
async def ai_cmd(interaction: discord.Interaction, message: str):
    await interaction.response.defer(ephemeral=False)

    user_id = interaction.user.id
    save_chat(user_id, message)
    summary = get_summary(user_id)
    recent_chats = get_recent_chats(user_id)

    messages = [{"role": "system", "content": ZUNDAMON_SYSTEM}]
    if summary:
        messages.append({"role": "system", "content": f"このユーザーの傾向メモ（非公開）:\n{summary}"})
    for m in recent_chats:
        messages.append({"role": "user", "content": m})

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                max_tokens=200,
                temperature=0.8,
            ),
        )
        reply = (response.choices[0].message.content or "").strip()
        await interaction.followup.send(f"🗣 **あなた**：{message}\n\n🟢 **ずんだもん**：{reply}")

        if len(recent_chats) >= 3:
            summary_prompt = [
                {"role": "system", "content": ZUNDAMON_SYSTEM},
                {"role": "system", "content": "以下の会話から、この人の話し方や好みを短く要約してください。"},
            ]
            for m in recent_chats:
                summary_prompt.append({"role": "user", "content": m})

            s = await loop.run_in_executor(
                None,
                lambda: client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=summary_prompt,
                    max_tokens=150,
                    temperature=0.5,
                ),
            )
            new_summary = (s.choices[0].message.content or "").strip()
            save_summary(user_id, new_summary)
            clear_chats(user_id)

    except Exception as e:
        await interaction.followup.send("ごめんなのだ…今はうまく答えられないのだ 💦")
        print("AI error:", e)
        traceback.print_exc()

@bot.tree.command(name="lottery", description="抽選を作成するのだ（管理者専用）")
@app_commands.describe(
    minutes="締め切りまでの分数",
    winners="当選人数",
    reward="当選者1人あたりのコイン",
)
async def lottery_cmd(interaction: discord.Interaction, minutes: int, winners: int, reward: int):
    # ✅ 管理者チェック
    if not is_admin_user(interaction):
        return await interaction.response.send_message(
            "このコマンドは管理者のみ使用できるのだ",
            ephemeral=True,
        )

    if minutes < 1:
        return await interaction.response.send_message(
            "minutes は 1 以上で入力するのだ",
            ephemeral=True,
        )

    if winners < 1:
        return await interaction.response.send_message(
            "winners は 1 以上で入力するのだ",
            ephemeral=True,
        )

    if reward < 1:
        return await interaction.response.send_message(
            "reward は 1 以上で入力するのだ",
            ephemeral=True,
        )

    ends_at = datetime.now(JST) + timedelta(minutes=minutes)

    await interaction.response.defer(ephemeral=True)

    msg = await interaction.channel.send(
        "🎟️ **抽選開始なのだ！**\n\n"
        f"締切：{ends_at.strftime('%Y-%m-%d %H:%M')}（JST）\n"
        f"当選人数：{winners}\n"
        f"報酬：{reward} コイン（1人あたり）\n\n"
        "下のボタンで参加するのだ！",
        view=LotteryJoinView(message_id=0),
    )

    lottery_create_event(
        message_id=msg.id,
        channel_id=msg.channel.id,
        guild_id=interaction.guild_id or 0,
        created_by=interaction.user.id,
        ends_at_unix=int(ends_at.timestamp()),
        winners_count=winners,
        reward_coins=reward,
    )

    await msg.edit(view=LotteryJoinView(message_id=msg.id))
    await interaction.followup.send("抽選を作成したのだ", ephemeral=True)

# =========================================================
# 沼（NUMA）
# =========================================================
NUMA_DENOMS = [2, 4, 8, 16, 32, 64]
NUMA_CONFIG_POT_KEY = "numa_pot"

NUMA_LOCK = asyncio.Lock()

# 称号（直書きID）
ROLE_NUMA_CLEAR  = 1462810553553780796  # 沼踏破者
ROLE_NUMA_LEGEND = 1462810693156737087  # 沼を支配せし者

AWARD_NUMA_CLEAR  = "AWARD_NUMA_CLEAR"
AWARD_NUMA_LEGEND = "AWARD_NUMA_LEGEND"


class NumaEntryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🕳️ 沼スタート",
        style=discord.ButtonStyle.danger,
        custom_id="numa_start_btn",
    )
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if NUMA_LOCK.locked():
            return await interaction.response.send_message(
                "今は誰かが沼に挑戦中なのだ…！",
                ephemeral=True,
            )

        await interaction.response.send_modal(NumaBallModal())


class NumaBallModal(discord.ui.Modal, title="投入する玉数を入力するのだ"):
    balls = discord.ui.TextInput(
        label="玉数（1発 = 100コイン）",
        placeholder="例：10",
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            balls = int(self.balls.value)
            if balls <= 0:
                raise ValueError
        except Exception:
            return await interaction.response.send_message(
                "正しい数字を入力するのだ",
                ephemeral=True,
            )

        async with NUMA_LOCK:
            await run_numa_game(interaction, balls)


async def run_numa_game(interaction: discord.Interaction, balls: int):
    channel = interaction.channel
    user = interaction.user

    # コイン消費
    async with get_user_lock(user.id):
        u = store.get_user(user.id)
        cost = balls * 100
        if u["coins"] < cost:
            return await channel.send(
                f"{user.display_name} コインが足りないのだ…",
                allowed_mentions=discord.AllowedMentions.none(),
            )

        u["coins"] -= cost
        await sheets_upsert_async(u)

    # pot 読み書き（Sheets 設定）
    pot = int(store.config.get(NUMA_CONFIG_POT_KEY, "0") or 0)
    pot += balls
    store._save_config_kv(NUMA_CONFIG_POT_KEY, str(pot))

    alive = balls
    one_ball_announced = False
    legend_clear = False

    await channel.send(
        "🕳️ **沼スタートなのだ**\n"
        f"挑戦者：{user.display_name}\n"
        f"投入：{balls} 発\n"
        f"現在の pot：{pot} 発",
        allowed_mentions=discord.AllowedMentions.none(),
    )

    for r, denom in enumerate(NUMA_DENOMS, start=1):
        before = alive
        passed = sum(1 for _ in range(alive) if random.random() < (1 / denom))
        alive = passed

        await channel.send(
            f"🎯 **ラウンド {r}/{len(NUMA_DENOMS)}**\n"
            f"{before} 発 → {alive} 発",
            allowed_mentions=discord.AllowedMentions.none(),
        )

        if alive == 1 and not one_ball_announced:
            one_ball_announced = True
            await channel.send(
                "⚠️⚠️⚠️ **ざわ…ざわ…** ⚠️⚠️⚠️\n"
                "💠 通過玉が……**1発だけ** になったのだ！",
                allowed_mentions=discord.AllowedMentions.none(),
            )

        if r == len(NUMA_DENOMS) and alive >= 1:
            if alive == 1:
                legend_clear = True
            break

        if alive <= 0:
            await channel.send(
                "🕳️ 沼に飲み込まれたのだ……",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await asyncio.sleep(0.6)

    # 成功
    reward = pot * 100
    store._save_config_kv(NUMA_CONFIG_POT_KEY, "0")

    async with get_user_lock(user.id):
        u = store.get_user(user.id)
        u["coins"] += reward
        await sheets_upsert_async(u)

    await channel.send(
        "🎉🎉🎉 **沼制覇なのだ！！** 🎉🎉🎉\n"
        f"{user.display_name}\n"
        f"報酬：{reward} コイン\n"
        "potは 0 に戻したのだ",
        allowed_mentions=discord.AllowedMentions.none(),
    )

    # 隠し称号判定
    just_events = {"NUMA_CLEAR"}
    if legend_clear:
        just_events.add("NUMA_LEGEND")

    await maybe_award_hidden_titles(interaction, u, just_events)


@bot.tree.command(
    name="setup_numa",
    description="沼の入口を設置するのだ（指定ロール保持者のみ）"
)
async def setup_numa_cmd(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message(
            "サーバー内でのみ使えるのだ",
            ephemeral=True,
        )

    if not has_numa_setup_role(interaction.user):
        return await interaction.response.send_message(
            "このコマンドを使う権限がないのだ",
            ephemeral=True,
        )

    await interaction.channel.send(
        "🕳️ **沼**\n"
        "・玉を投入して運命に挑むのだ\n"
        "・最後まで残れば超高額報酬なのだ\n",
        view=NumaEntryView(),
        allowed_mentions=discord.AllowedMentions.none(),
    )

    await interaction.response.send_message(
        "沼を設置したのだ",
        ephemeral=True,
    )

# =========================================================
# /dice クールタイム（ユーザーごと）
# =========================================================
DICE_COOLDOWN_SEC = 10
_dice_last_ts: dict[int, float] = {}

# =========================================================
# /dice（ちんちろ）
# =========================================================
@bot.tree.command(name="dice", description="ちんちろを振るのだ")
async def chinchiro_cmd(interaction: discord.Interaction):
    # ✅ ここで落ちても無視して続行 or 終了する
    try:
        await interaction.response.defer(ephemeral=False)
    except discord.NotFound:
        return  # interactionが期限切れ
    except discord.errors.InteractionResponded:
        pass    # すでに応答済みならOK
    except Exception:
        return

    DICE_COST = 5

    def roll_dice(turn: int, jackpot_boost: bool) -> list[str]:
        BASE_JACKPOT_RATE = 1 / 2000
        BOOSTED_JACKPOT_RATE = 1 / 100
        SEVEN_BAR_RATE = 1 / 1000
        PEE_RATE = 1 / 500

        r = random.random()
        jackpot_rate = BOOSTED_JACKPOT_RATE if jackpot_boost else BASE_JACKPOT_RATE

        # 🎰 JP
        if r < jackpot_rate:
            return ["7", "7", "7"]

        # 7-7-BAR（次回ブーストのトリガー）
        if turn < 3 and r < jackpot_rate + SEVEN_BAR_RATE:
            return ["7", "7", "BAR"]

        # 💦 しょんべん（サイコロを振らない事故）
        if r < jackpot_rate + SEVEN_BAR_RATE + PEE_RATE:
            return ["💦 しょんべん"]

        # 通常ちんちろ
        return sorted([str(random.randint(1, 6)) for _ in range(3)])

    def judge(dice: list[str]) -> str | None:
        # 特殊目
        if dice == ["7", "7", "7"]:
            return "🎰 ジャックポット！"
        if dice == ["7", "7", "BAR"]:
            return None
        if dice == ["💦 しょんべん"]:
            return "💦 しょんべん"

        # 通常ちんちろ
        nums = sorted(map(int, dice))
        a, b, c = nums

        if nums == [1, 1, 1]:
            return "🎉 ピンゾロ"
        if a == b == c:
            return f"🌪 {a}のアラシ"
        if nums == [1, 2, 3]:
            return "💀 ヒフミ"
        if nums == [4, 5, 6]:
            return "🔥 シゴロ"

        if a == b and b != c:
            return f"👉 目：{c}"
        if b == c and a != b:
            return f"👉 目：{a}"
        if a == c and b != a:
            return f"👉 目：{b}"
        return None

    async with get_user_lock(interaction.user.id):
        u = store.get_user(interaction.user.id)

        if int(u.get("coins", 0) or 0) < DICE_COST:
            return await interaction.followup.send(
                f"コインが足りないのだ（必要：{DICE_COST} / 残高：{u['coins']}）"
            )

        # 参加費
        u["coins"] = int(u.get("coins", 0) or 0) - DICE_COST

        results_text: list[str] = []
        role: str | None = None
        seven_bar_triggered = False
        had_seven_bar = False
        final_dice: list[str] | None = None

        for i in range(1, 4):
            dice = roll_dice(i, seven_bar_triggered)
            final_dice = dice

            # 7-7-BAR トリガー
            if dice == ["7", "7", "BAR"]:
                seven_bar_triggered = True
                had_seven_bar = True

            # 役判定
            role = judge(dice)
            dice_text = "・".join(dice)

            if role is None:
                results_text.append(f"{i}回目：🎲 {dice_text} → 役なし")
                continue

            # 💦 しょんべんは即終了（結果表示も専用に）
            if role == "💦 しょんべん":
                results_text.append(f"{i}回目：💦 **しょんべん**（サイコロが器から落ちたのだ…）")
                break

            results_text.append(f"{i}回目：🎲 {dice_text} → **{role}**")
            break

        if role is None:
            role = "❌ メなし"

        # コイン増減
        delta = 0
        if role == "🎉 ピンゾロ":
            delta = 70
        elif role == "🔥 シゴロ":
            delta = 10
        elif role and "のアラシ" in role:
            try:
                left = role.split("の")[0].replace("🌪", "").strip()
                num = int(left)
                delta = num * 5
            except Exception:
                delta = 0
        elif role == "🎰 ジャックポット！":
            delta = 1000
        elif role == "💦 しょんべん":
            delta = -100
        elif role == "💀 ヒフミ":
            delta = -10
        elif role.startswith("👉 目："):
            try:
                num = int(role.replace("👉 目：", "").strip())
                if num <= 2:
                    delta = 1
                elif num <= 4:
                    delta = 3
                else:
                    delta = 5
            except Exception:
                delta = 0

        if delta != 0:
            u["coins"] = int(u.get("coins", 0) or 0) + delta
            if delta > 0:
                u["total_earned"] = int(u.get("total_earned", 0) or 0) + delta

        after_all = int(u.get("coins", 0) or 0)

        just_events = set()
        if final_dice == ["7", "7", "7"]:
            u["jackpot_count"] = int(u.get("jackpot_count", 0) or 0) + 1
            just_events.add("JP_EVENT")
        if had_seven_bar and final_dice != ["7", "7", "7"]:
            just_events.add("BAR_MISS_EVENT")

        await sheets_upsert_async(u)

        sign = "+" if delta > 0 else ""
        msg = (
            "🎲 **ちんちろ結果なのだ！**\n"
            + "\n".join(results_text)
            + f"\n\n👉 **最終結果：{role}**\n\n"
            f"💰 コイン変動：参加費 -{DICE_COST} / 役の増減 {sign}{delta} なのだ\n"
            f"残高：{after_all} コインなのだ"
        )

    await interaction.followup.send(msg)

    try:
        await maybe_award_hidden_titles(interaction, u, just_events=just_events)
    except Exception as e:
        print("dice award error:", e)
        traceback.print_exc()


# =========================================================
# 抽選締切処理
# =========================================================
async def run_lottery_close(message_id: int):
    ev = lottery_get_event(message_id)
    if not ev or ev["status"] != "open":
        return

    channel = bot.get_channel(ev["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(ev["channel_id"])
        except Exception:
            return

    entries = lottery_list_entries(message_id)
    if not entries:
        lottery_close_event(message_id)
        await channel.send("🎟️ 抽選結果なのだ\n参加者がいなかったのだ…！")
        return

    winners_n = min(ev["winners_count"], len(entries))
    reward = ev["reward_coins"]

    winners = random.sample(entries, k=winners_n)

    lottery_close_event(message_id)
    lottery_save_winners(message_id, winners, reward)

    for uid in winners:
        async with get_user_lock(uid):
            u = store.get_user(uid)
            u["coins"] += reward
            u["total_earned"] += reward
            await sheets_upsert_async(u)

    mentions = " ".join(f"<@{uid}>" for uid in winners)
    await channel.send(
        "🎟️ **抽選結果なのだ！**\n"
        f"当選者：{mentions}\n"
        f"{reward} コインを付与したのだ！"
    )

# =========================================================
# /join（既存：表示形式を変えない）
# =========================================================
@bot.tree.command(name="join", description="参加募集をするのだ")
@app_commands.describe(place="場所", time_str="締切時間（HH:MM）※0で時間なし", count="募集人数")
@app_commands.choices(place=[app_commands.Choice(name=p, value=p) for p in PLACE_LIST])
async def join_cmd(
    interaction: discord.Interaction,
    place: app_commands.Choice[str],
    time_str: str,
    count: int,
):
    now = datetime.now(JST)

    if time_str == "0":
        target_time = None
        time_text = "締切なし"
    else:
        try:
            hour, minute = map(int, time_str.split(":"))
        except ValueError:
            return await interaction.response.send_message(
                "時間は HH:MM 形式、または 0 を入力するのだ",
                ephemeral=True,
            )

        target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target_time <= now:
            target_time += timedelta(days=1)
        time_text = f"{target_time.strftime('%H:%M')}〆なのだ"

    await interaction.response.send_message("募集を開始したのだ", ephemeral=True)

    msg = await interaction.channel.send(
        f"@everyone {place.value} @{count} {time_text}\n"
        f"👍で参加なのだ"
    )
    await msg.add_reaction("👍")

    join_tasks[msg.id] = {
        "place": place.value,
        "time": target_time,
        "count": count,
        "members": set(),
        "channel": interaction.channel.id,
        "message_id": msg.id,
    }

    remove_targets = []
    for name, data in tasks_data.items():
        diff = abs((data["time"] - now).total_seconds())
        if diff <= 3600:
            remove_targets.append(name)
    for name in remove_targets:
        del tasks_data[name]


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if bot.user and payload.user_id == bot.user.id:
        return
    if str(payload.emoji) != "👍":
        return
    if payload.message_id not in join_tasks:
        return

    data = join_tasks[payload.message_id]
    user_id = payload.user_id
    if user_id in data["members"]:
        return

    data["members"].add(user_id)
    channel = bot.get_channel(data["channel"])
    if channel and len(data["members"]) >= data["count"]:
        await channel.send(f"{data['place']} 〆なのだ")
        del join_tasks[payload.message_id]


@bot.tree.command(name="jointime", description="締切なし募集に時間と人数を設定して再募集するのだ")
@app_commands.describe(time_str="締切時間（HH:MM）", count="募集人数")
async def jointime_cmd(interaction: discord.Interaction, time_str: str, count: int):
    try:
        hour, minute = map(int, time_str.split(":"))
    except ValueError:
        return await interaction.response.send_message(
            "時間は HH:MM 形式で入力するのだ",
            ephemeral=True,
        )

    now = datetime.now(JST)
    target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target_time <= now:
        target_time += timedelta(days=1)

    no_time_task = None
    for msg_id, data in join_tasks.items():
        if data["time"] is None:
            no_time_task = msg_id
            break

    if not no_time_task:
        return await interaction.response.send_message("締切なしの募集がないのだ", ephemeral=True)

    del join_tasks[no_time_task]

    await interaction.response.send_message("再募集を行ったのだ", ephemeral=True)

    msg = await interaction.channel.send(
        f"@everyone 再募集 @{count} {target_time.strftime('%H:%M')}〆なのだ\n"
        f"👍で参加なのだ"
    )
    await msg.add_reaction("👍")

    join_tasks[msg.id] = {
        "place": "再募集",
        "time": target_time,
        "count": count,
        "members": set(),
        "channel": interaction.channel.id,
        "message_id": msg.id,
    }


@bot.tree.command(name="joinf", description="全ての募集を締切なのだ")
async def joinf_cmd(interaction: discord.Interaction):
    join_tasks.clear()
    await interaction.channel.send("@everyone〆なのだ")


@tasks.loop(seconds=30)
async def check_join_tasks():
    now = datetime.now(JST)
    for msg_id, data in list(join_tasks.items()):
        if data["time"] is None:
            continue
        if now >= data["time"]:
            channel = bot.get_channel(data["channel"])
            if channel:
                await channel.send(f"{data['place']} 〆なのだ")
            del join_tasks[msg_id]


# =========================================================
# /time /list /reset /resetin（既存：表示形式を変えない）
# =========================================================
@bot.tree.command(name="time", description="受注時間をセットするのだ")
@app_commands.describe(name="場所を選ぶのだ", minutes="何分後に受注が開始するのだ？")
@app_commands.choices(name=[app_commands.Choice(name=p, value=p) for p in PLACE_LIST])
async def time_cmd(interaction: discord.Interaction, name: app_commands.Choice[str], minutes: int):
    if minutes < 1 or minutes > 1440:
        return await interaction.response.send_message("分の指定は 1〜1440 の間で入力するのだ", ephemeral=False)

    now = datetime.now(JST)
    target_time = now + timedelta(minutes=minutes)
    tasks_data[name.value] = {"time": target_time, "channel": interaction.channel.id}
    await interaction.response.send_message(
        f"{name.value} は {target_time.strftime('%H時%M分')} に受注開始なのだ。",
        ephemeral=False,
    )


@bot.tree.command(name="list", description="現在登録されているタスクを一覧表示するのだ")
async def list_cmd(interaction: discord.Interaction):
    if not tasks_data:
        return await interaction.response.send_message("現在登録されているタスクはないのだ", ephemeral=False)

    msg = "【登録タスク一覧】\n"
    for name, data in tasks_data.items():
        time_str = data["time"].strftime("%H:%M")
        msg += f"・**{name}**：{time_str}\n"
    await interaction.response.send_message(msg, ephemeral=False)


@bot.tree.command(name="reset", description="登録されている全てのタスクを消すのだ")
async def reset_cmd(interaction: discord.Interaction):
    tasks_data.clear()
    await interaction.response.send_message("すべてのタスクを消したのだ", ephemeral=False)


@bot.tree.command(name="resetin", description="特定のタスクを選択して消すのだ")
@app_commands.describe(name="削除するタスク名を入力するのだ")
async def resetin_cmd(interaction: discord.Interaction, name: str):
    if name not in tasks_data:
        return await interaction.response.send_message("そのタスクはないのだ", ephemeral=False)
    del tasks_data[name]
    await interaction.response.send_message(f"**{name}** を消したのだ", ephemeral=False)


@resetin_cmd.autocomplete("name")
async def autocomplete_name(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=n, value=n)
        for n in tasks_data.keys()
        if current.lower() in n.lower()
    ][:25]


# =========================================================
# /craft（既存：表示形式は変えない）
# =========================================================
@bot.tree.command(name="craft", description="必要素材を計算して表示するのだ")
@app_commands.describe(category="道具 or 武器", type="種別を選択", item="作りたいアイテム", count="作る個数")
@app_commands.choices(category=[app_commands.Choice(name="道具", value="道具"), app_commands.Choice(name="武器", value="武器")])
async def craft_cmd(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
    type: str,
    item: str,
    count: int,
):
    await interaction.response.defer(ephemeral=True)
    sheet = await get_csv(category.value)
    if not sheet:
        return await interaction.followup.send("シートの読み込みに失敗しました")

    def find_col(cols, target):
        for c in cols:
            if c is None:
                continue
            if target in c.replace("\u3000", "").strip():
                return c
        return None

    columns = sheet[0].keys()
    name_col = find_col(columns, "名前")
    make_col = find_col(columns, "１回での作成個数")
    if not name_col:
        return await interaction.followup.send("シートに '名前' 列が見つかりません")

    target_row = next(
        (
            row
            for row in sheet
            if (row.get(name_col) or "").replace("\u3000", "").strip() == (item or "").strip()
        ),
        None,
    )
    if not target_row:
        return await interaction.followup.send("そのアイテムはシートにありません")

    make_per_once = float(target_row.get(make_col, "1") or 1)
    craft_times = math.ceil(count / make_per_once)

    msg = f"### **{item} を {count}個 作るための必要素材**\n"
    msg += f"作成回数：**{craft_times} 回**\n\n"

    for key, value in target_row.items():
        if key in (name_col, make_col, "種別"):
            continue
        try:
            v = float(value)
        except Exception:
            continue
        if v <= 0:
            continue
        need = v * craft_times
        if float(need).is_integer():
            need = int(need)
        msg += f"- {key}：{need}\n"

    await interaction.followup.send(msg)


@craft_cmd.autocomplete("type")
async def autocomplete_type(interaction: discord.Interaction, current: str):
    def find_option(data, name):
        if not isinstance(data, dict):
            return None
        for opt in data.get("options", []):
            if opt.get("name") == name and "value" in opt:
                return opt["value"]
            if "options" in opt:
                v = find_option(opt, name)
                if v is not None:
                    return v
        return None

    category = find_option(interaction.data, "category")
    if not category:
        types = ["小型", "大型", "その他", "弾", "武器", "アタッチメント"]
    elif category == "道具":
        types = ["小型", "大型", "その他"]
    else:
        types = ["弾", "武器", "アタッチメント", "その他"]

    filtered = [t for t in types if current.lower() in t.lower()][:25]
    return [app_commands.Choice(name=t, value=t) for t in filtered]


@craft_cmd.autocomplete("item")
async def autocomplete_item(interaction: discord.Interaction, current: str):
    def find_option(data, name):
        if not isinstance(data, dict):
            return None
        for opt in data.get("options", []):
            if opt.get("name") == name and "value" in opt:
                return opt["value"]
            if "options" in opt:
                v = find_option(opt, name)
                if v is not None:
                    return v
        return None

    category = find_option(interaction.data, "category")
    type_sel = find_option(interaction.data, "type")

    urls = []
    if category == "道具":
        urls = ["道具"]
    elif category == "武器":
        urls = ["武器"]
    else:
        urls = ["道具", "武器"]

    candidates = []

    def normalize(s):
        if s is None:
            return ""
        return str(s).replace("\u3000", "").strip().lower()

    for cat in urls:
        sheet = await get_csv(cat)
        if not sheet:
            continue

        def find_col(cols, target):
            for c in cols:
                if c is None:
                    continue
                if target in c.replace("\u3000", "").strip():
                    return c
            return None

        columns = sheet[0].keys()
        name_col = find_col(columns, "名前")
        type_col = find_col(columns, "種別")
        if not name_col or not type_col:
            continue

        for row in sheet:
            row_name = (row.get(name_col) or "").replace("\u3000", "").strip()
            row_type = row.get(type_col)
            if not row_name:
                continue
            if (not type_sel) or normalize(row_type) == normalize(type_sel):
                candidates.append(row_name)

    if current:
        candidates = [n for n in candidates if current.lower() in n.lower()]
    candidates = candidates[:25]
    return [app_commands.Choice(name=n, value=n) for n in candidates]


# =========================================================
# 通知タスク（既存）
# =========================================================
@tasks.loop(minutes=1)
async def check_tasks():
    now = datetime.now(JST)
    remove_list = []
    for name, data in tasks_data.items():
        notify_time = data["time"] - timedelta(minutes=10)
        if notify_time <= now:
            channel = bot.get_channel(data["channel"])
            if channel:
                await channel.send(f"@everyone **{name}** の受注10分前なのだ！")
            remove_list.append(name)
    for name in remove_list:
        del tasks_data[name]

@tasks.loop(seconds=30)
async def lottery_watcher():
    due = lottery_get_open_events_due(int(time.time()))
    for mid in due:
        await run_lottery_close(mid)

# =========================================================
# ブラックジャック
# =========================================================
SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


def new_deck() -> list[tuple[str, str]]:
    deck = [(r, s) for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck


def draw_card(deck: list[tuple[str, str]]) -> tuple[str, str]:
    if not deck:
        deck.extend(new_deck())
    return deck.pop()


def hand_value(cards: list[tuple[str, str]]) -> int:
    vals = []
    aces = 0
    for r, s in cards:
        if r in ("J", "Q", "K"):
            vals.append(10)
        elif r == "A":
            aces += 1
            vals.append(1)
        else:
            vals.append(int(r))
    total = sum(vals)
    for _ in range(aces):
        if total + 10 <= 21:
            total += 10
    return total


def fmt_cards(cards: list[tuple[str, str]]) -> str:
    return " ".join([f"{s}{r}" for r, s in cards])


bj_sessions: dict[int, dict] = {}
BJ_SESSION_TIMEOUT_SEC = 20 * 60


def touch_bj_session(uid: int):
    s = bj_sessions.get(uid)
    if s:
        s["last_action_ts"] = time.time()


@tasks.loop(minutes=2)
async def cleanup_bj_sessions():
    now = time.time()
    remove_ids = []
    for uid, s in list(bj_sessions.items()):
        last_ts = float(s.get("last_action_ts", now))
        if now - last_ts > BJ_SESSION_TIMEOUT_SEC:
            remove_ids.append(uid)

    for uid in remove_ids:
        try:
            async with get_user_lock(uid):
                s = bj_sessions.get(uid)
                if not s:
                    continue
                refund = int(sum(s.get("bets", []) or [0]))
                u = store.get_user(uid)
                u["coins"] = int(u.get("coins", 0)) + refund
                await sheets_upsert_async(u)
                bj_sessions.pop(uid, None)
        except Exception as e:
            print("cleanup_bj_sessions error:", e)
            traceback.print_exc()


class BetModal(discord.ui.Modal, title="掛け金を入力するのだ"):
    bet = discord.ui.TextInput(label="掛け金（数字）", placeholder="例：100", required=True)

    def __init__(self, balance: int):
        super().__init__()
        self.balance = balance

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if not is_in_channel(interaction, BJ_CHANNEL_ID):
                return await interaction.response.send_message("このチャンネルでは使えないのだ", ephemeral=True)

            async with get_user_lock(interaction.user.id):
                u = store.get_user(interaction.user.id)
                try:
                    bet_val = int(str(self.bet.value).strip())
                except Exception:
                    return await interaction.response.send_message(
                        f"現在の残高：{u['coins']} コイン\n数字を入力するのだ",
                        ephemeral=True,
                    )

                if bet_val <= 0:
                    return await interaction.response.send_message(
                        f"現在の残高：{u['coins']} コイン\n1以上で入力するのだ",
                        ephemeral=True,
                    )

                if bet_val > u["coins"]:
                    return await interaction.response.send_message(
                        f"現在の残高：{u['coins']} コイン\nコインが足りないのだ",
                        ephemeral=True,
                    )
                    
                MAX_BJ_BET = 1000  # ← どこかグローバルに置くのがおすすめ
                    
                if bet_val > MAX_BJ_BET:
                    return await interaction.response.send_message(
                        f"掛け金は最大 {MAX_BJ_BET} までなのだ",
                        ephemeral=True,
                    )    

                u["coins"] -= bet_val
                await sheets_upsert_async(u)

                session = {
                    "deck": new_deck(),
                    "dealer": [],
                    "hands": [[]],
                    "bets": [bet_val],
                    "active": 0,
                    "finished_hands": [False],
                    "doubled": [False],
                    "was_split": False,
                    "is_natural_bj": [False],
                    "last_action_ts": time.time(),
                }

                deck = session["deck"]
                session["hands"][0] = [draw_card(deck), draw_card(deck)]
                session["dealer"] = [draw_card(deck), draw_card(deck)]
                session["is_natural_bj"][0] = (hand_value(session["hands"][0]) == 21)

                bj_sessions[interaction.user.id] = session
                await interaction.response.send_message("配札したのだ", ephemeral=True)

            if hand_value(session["dealer"]) == 21:
                await bj_finish(interaction, u, immediate_dealer_bj=True)
                return

            if session["is_natural_bj"][0]:
                session["finished_hands"][0] = True
                await bj_dealer_turn(interaction, u)
                return

            await bj_send_state(interaction, u)

        except Exception as e:
            print("BetModal on_submit error:", e)
            traceback.print_exc()
            try:
                await interaction.response.send_message(
                    "掛け金処理でエラーが出たのだ…（ログを確認してほしいのだ）",
                    ephemeral=True,
                )
            except Exception:
                pass


class BjEntryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🎴 スタート",
        style=discord.ButtonStyle.primary,
        custom_id="bj_start_entry_btn",
    )
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not is_in_channel(interaction, BJ_CHANNEL_ID):
                return await interaction.response.send_message(
                    "このチャンネルでは使えないのだ",
                    ephemeral=True,
                )

            # ここで重い処理をしない
            u = store.get_user(interaction.user.id)

            # ✅ 重要：send_modal の前に defer しない
            await interaction.response.send_modal(BetModal(balance=int(u.get("coins", 0) or 0)))

        except discord.NotFound:
            # interaction が失効していたら何もしない
            return
        except discord.errors.InteractionResponded:
            # すでにackされていたら何もしない（40060回避）
            return
        except Exception as e:
            print("bj start error:", e)
            traceback.print_exc()
            # 応答できるなら response、無理なら followup
            try:
                await interaction.response.send_message("開始でエラーが出たのだ…", ephemeral=True)
            except Exception:
                try:
                    await interaction.followup.send("開始でエラーが出たのだ…", ephemeral=True)
                except Exception:
                    pass

class BJActionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id in bj_sessions

    @discord.ui.button(label="ヒット", style=discord.ButtonStyle.primary, custom_id="bj_hit_btn")
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        u = store.get_user(interaction.user.id)
        await bj_hit(interaction, u)

    @discord.ui.button(label="スタンド", style=discord.ButtonStyle.secondary, custom_id="bj_stand_btn")
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        u = store.get_user(interaction.user.id)
        await bj_stand(interaction, u)

    @discord.ui.button(label="ダブルダウン", style=discord.ButtonStyle.danger, custom_id="bj_double_btn")
    async def double(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        u = store.get_user(interaction.user.id)
        await bj_double(interaction, u)

    @discord.ui.button(label="スプリット", style=discord.ButtonStyle.success, custom_id="bj_split_btn")
    async def split(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        u = store.get_user(interaction.user.id)
        await bj_split(interaction, u)


class BJEndView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)

    @discord.ui.button(
        label="🎴 もう一回スタート",
        style=discord.ButtonStyle.primary,
        custom_id="bj_restart_btn",
    )
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not is_in_channel(interaction, BJ_CHANNEL_ID):
                return await interaction.response.send_message(
                    "このチャンネルでは使えないのだ",
                    ephemeral=True,
                )

            u = store.get_user(interaction.user.id)

            # ⚠️ send_modal 前に defer しない
            await interaction.response.send_modal(
                BetModal(balance=int(u.get("coins", 0) or 0))
            )

        except discord.NotFound:
            return
        except discord.errors.InteractionResponded:
            return
        except Exception as e:
            print("bj restart error:", e)
            traceback.print_exc()
            try:
                await interaction.followup.send(
                    "開始でエラーが出たのだ…",
                    ephemeral=True,
                )
            except Exception:
                pass

    @discord.ui.button(
        label="やめる",
        style=discord.ButtonStyle.secondary,
        custom_id="bj_quit_btn",
    )
    async def quit(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_message(
                "終了したのだ",
                ephemeral=True,
            )
        except discord.NotFound:
            try:
                await interaction.followup.send(
                    "終了したのだ",
                    ephemeral=True,
                )
            except Exception:
                pass
        except discord.errors.InteractionResponded:
            try:
                await interaction.followup.send(
                    "終了したのだ",
                    ephemeral=True,
                )
            except Exception:
                pass
        except Exception as e:
            print("bj quit error:", e)
            traceback.print_exc()
            try:
                await interaction.followup.send(
                    "終了処理でエラーが出たのだ…",
                    ephemeral=True,
                )
            except Exception:
                pass



def bj_state_text(session: dict) -> str:
    dealer = session["dealer"]
    dealer_open = f"{dealer[0][1]}{dealer[0][0]} ??"
    lines = []
    for idx, hand in enumerate(session["hands"]):
        v = hand_value(hand)
        mark = "👉" if idx == session["active"] else " "
        nat = ""
        if idx < len(session.get("is_natural_bj", [])) and session["is_natural_bj"][idx]:
            nat = "（BJ）"
        lines.append(
            f"{mark}手札{idx+1}：{fmt_cards(hand)}（{v}）{nat} 賭け：{session['bets'][idx]}"
        )
    return f"🎴 ブラックジャックなのだ\n\nディーラー：{dealer_open}\n\n" + "\n".join(lines)


async def bj_send_state(interaction: discord.Interaction, u: dict):
    session = bj_sessions.get(interaction.user.id)
    if not session:
        return await interaction.followup.send("セッションがないのだ", ephemeral=True)

    touch_bj_session(interaction.user.id)

    view = BJActionView()
    active = session["active"]
    hand = session["hands"][active]

    can_split = (len(session["hands"]) == 1) and (len(hand) == 2) and (hand[0][0] == hand[1][0])
    can_double = (len(session["hands"]) == 1) and (len(hand) == 2) and (not session["doubled"][active])

    for item in view.children:
        if isinstance(item, discord.ui.Button) and item.label == "スプリット":
            item.disabled = not can_split
        if isinstance(item, discord.ui.Button) and item.label == "ダブルダウン":
            item.disabled = not can_double

    await interaction.followup.send(bj_state_text(session), view=view, ephemeral=True)


async def bj_hit(interaction: discord.Interaction, u: dict):
    session = bj_sessions.get(interaction.user.id)
    if not session:
        return await interaction.followup.send("セッションがないのだ", ephemeral=True)

    touch_bj_session(interaction.user.id)

    i = session["active"]
    session["hands"][i].append(draw_card(session["deck"]))
    v = hand_value(session["hands"][i])

    await interaction.followup.send(f"ヒットしたのだ\n{bj_state_text(session)}", ephemeral=True)

    if v > 21:
        session["finished_hands"][i] = True
        await interaction.followup.send("バーストしたのだ", ephemeral=True)
        await bj_next_or_dealer(interaction, u)


async def bj_stand(interaction: discord.Interaction, u: dict):
    session = bj_sessions.get(interaction.user.id)
    if not session:
        return await interaction.followup.send("セッションがないのだ", ephemeral=True)

    touch_bj_session(interaction.user.id)

    i = session["active"]
    session["finished_hands"][i] = True
    await interaction.followup.send(f"スタンドしたのだ\n{bj_state_text(session)}", ephemeral=True)
    await bj_next_or_dealer(interaction, u)


async def bj_double(interaction: discord.Interaction, u: dict):
    session = bj_sessions.get(interaction.user.id)
    if not session:
        return await interaction.followup.send("セッションがないのだ", ephemeral=True)

    touch_bj_session(interaction.user.id)

    if len(session["hands"]) != 1:
        return await interaction.followup.send("スプリット後はダブルダウンできないのだ", ephemeral=True)

    i = session["active"]
    hand = session["hands"][i]
    if len(hand) != 2 or session["doubled"][i]:
        return await interaction.followup.send("今はダブルダウンできないのだ", ephemeral=True)

    async with get_user_lock(interaction.user.id):
        add = session["bets"][i]
        if u["coins"] < add:
            return await interaction.followup.send("コインが足りないのだ", ephemeral=True)

        u["coins"] -= add
        session["bets"][i] += add
        session["doubled"][i] = True
        await sheets_upsert_async(u)

    session["hands"][i].append(draw_card(session["deck"]))
    session["finished_hands"][i] = True

    await interaction.followup.send(f"ダブルダウンしたのだ\n{bj_state_text(session)}", ephemeral=True)
    await bj_next_or_dealer(interaction, u)


async def bj_split(interaction: discord.Interaction, u: dict):
    session = bj_sessions.get(interaction.user.id)
    if not session:
        return await interaction.followup.send("セッションがないのだ", ephemeral=True)

    touch_bj_session(interaction.user.id)

    if len(session["hands"]) != 1:
        return await interaction.followup.send("もうスプリット済みなのだ", ephemeral=True)

    hand = session["hands"][0]
    if len(hand) != 2 or hand[0][0] != hand[1][0]:
        return await interaction.followup.send("スプリット条件を満たしていないのだ", ephemeral=True)

    async with get_user_lock(interaction.user.id):
        bet = session["bets"][0]
        if u["coins"] < bet:
            return await interaction.followup.send("スプリット分のコインが足りないのだ", ephemeral=True)

        u["coins"] -= bet
        await sheets_upsert_async(u)

    c1, c2 = hand[0], hand[1]
    session["hands"] = [[c1], [c2]]
    session["bets"] = [bet, bet]
    session["finished_hands"] = [False, False]
    session["doubled"] = [False, False]
    session["active"] = 0
    session["was_split"] = True
    session["is_natural_bj"] = [False, False]

    session["hands"][0].append(draw_card(session["deck"]))
    session["hands"][1].append(draw_card(session["deck"]))

    await interaction.followup.send(f"スプリットしたのだ\n{bj_state_text(session)}", ephemeral=True)
    await bj_send_state(interaction, u)


async def bj_next_or_dealer(interaction: discord.Interaction, u: dict):
    session = bj_sessions.get(interaction.user.id)
    if not session:
        return
    for idx, fin in enumerate(session["finished_hands"]):
        if not fin:
            session["active"] = idx
            return await bj_send_state(interaction, u)
    await bj_dealer_turn(interaction, u)


async def bj_dealer_turn(interaction: discord.Interaction, u: dict):
    session = bj_sessions.get(interaction.user.id)
    if not session:
        return

    touch_bj_session(interaction.user.id)

    dealer = session["dealer"]
    await interaction.followup.send(
        f"ディーラーのターンなのだ\nディーラー：{fmt_cards(dealer)}（{hand_value(dealer)}）",
        ephemeral=True,
    )

    threshold = dealer_hit_threshold_by_balance(int(u.get("coins", 0) or 0))
    while hand_value(dealer) < threshold:
        await asyncio.sleep(0.6)
        dealer.append(draw_card(session["deck"]))
        await interaction.followup.send(
            f"ディーラーがヒットしたのだ\nディーラー：{fmt_cards(dealer)}（{hand_value(dealer)}）",
            ephemeral=True,
        )

    await bj_finish(interaction, u, immediate_dealer_bj=False)


async def bj_finish(interaction: discord.Interaction, u: dict, immediate_dealer_bj: bool):
    session = bj_sessions.get(interaction.user.id)
    if not session:
        return await interaction.followup.send("セッションがないのだ", ephemeral=True)

    touch_bj_session(interaction.user.id)

    dealer_val = hand_value(session["dealer"])
    dealer_bust = dealer_val > 21

    payout_total = 0
    profit = 0
    results = []

    for idx, hand in enumerate(session["hands"]):
        bet = session["bets"][idx]
        v = hand_value(hand)

        if v > 21:
            results.append(f"手札{idx+1}：負け（バースト）")
            profit -= bet
            continue

        if immediate_dealer_bj:
            results.append(f"手札{idx+1}：負け（ディーラー21）")
            profit -= bet
            continue

        if dealer_bust:
            if idx < len(session.get("is_natural_bj", [])) and session["is_natural_bj"][idx]:
                payout = (bet * 5) // 2
                payout_total += payout
                results.append(f"手札{idx+1}：勝ち（BJ 3:2）")
                profit += (payout - bet)
            else:
                payout_total += bet * 2
                results.append(f"手札{idx+1}：勝ち（ディーラーバースト）")
                profit += bet
            continue

        if v > dealer_val:
            if idx < len(session.get("is_natural_bj", [])) and session["is_natural_bj"][idx]:
                payout = (bet * 5) // 2
                payout_total += payout
                results.append(f"手札{idx+1}：勝ち（BJ 3:2）")
                profit += (payout - bet)
            else:
                payout_total += bet * 2
                results.append(f"手札{idx+1}：勝ち")
                profit += bet
        elif v < dealer_val:
            results.append(f"手札{idx+1}：負け")
            profit -= bet
        else:
            payout_total += bet
            results.append(f"手札{idx+1}：引き分け")

    for idx, hand in enumerate(session["hands"]):
        bet = int(session["bets"][idx])
        v = hand_value(hand)

        # その手の払戻（戻ってくるコインの総額）
        payout = 0

        # バースト＝没収
        if v > 21:
            results.append(f"手札{idx+1}：負け（バースト）")
            payout = 0

        # ディーラーが初手21
        elif immediate_dealer_bj:
            results.append(f"手札{idx+1}：負け（ディーラー21）")
            payout = 0

        # ディーラーバースト
        elif dealer_bust:
            if idx < len(session.get("is_natural_bj", [])) and session["is_natural_bj"][idx]:
                # BJ 3:2
                payout = (bet * 5) // 2
                results.append(f"手札{idx+1}：勝ち（BJ 3:2）")
            else:
                payout = bet * 2
                results.append(f"手札{idx+1}：勝ち（ディーラーバースト）")

        # 通常比較
        else:
            if v > dealer_val:
                if idx < len(session.get("is_natural_bj", [])) and session["is_natural_bj"][idx]:
                    payout = (bet * 5) // 2
                    results.append(f"手札{idx+1}：勝ち（BJ 3:2）")
                else:
                    payout = bet * 2
                    results.append(f"手札{idx+1}：勝ち")
            elif v < dealer_val:
                payout = 0
                results.append(f"手札{idx+1}：負け")
            else:
                # ✅ 引き分け：賭け金そのまま返却
                payout = bet
                results.append(f"手札{idx+1}：引き分け")

        payout_total += payout
        profit += (payout - bet)


    async with get_user_lock(interaction.user.id):
        u["coins"] += payout_total
        u["bj_play_count"] += 1

    async with get_user_lock(interaction.user.id):
        u["coins"] = int(u.get("coins", 0))
        u["bj_play_count"] = int(u.get("bj_play_count", 0))
        u["bj_win_streak"] = int(u.get("bj_win_streak", 0))
        u["total_earned"] = int(u.get("total_earned", 0))

        just_events = set()
        if profit > 0:
            u["bj_win_streak"] += 1
            u["total_earned"] += profit
            just_events.add("BJ_WIN_EVENT")
            if profit >= 1000:
                just_events.add("BJ_BIGWIN_EVENT")
        elif profit < 0:
            u["bj_win_streak"] = 0
            if profit <= -1000:
                just_events.add("BJ_BIGLOSE_EVENT")
        else:
            u["bj_win_streak"] = 0

        await sheets_upsert_async(u)

    msg = (
        "🎴 結果なのだ\n\n"
        f"ディーラー：{fmt_cards(session['dealer'])}（{dealer_val}）\n"
        + "\n".join(results)
        + f"\n\n残高：{u['coins']} コインなのだ"
    )
    await interaction.followup.send(msg, ephemeral=True)

    await maybe_award_hidden_titles(interaction, u, just_events=just_events)
    await interaction.followup.send("次はどうするのだ？", view=BJEndView(), ephemeral=True)

    bj_sessions.pop(interaction.user.id, None)


@bot.tree.command(name="setup_bj", description="ブラックジャック入口メッセージを設置するのだ（最初の1回のみ）")
async def setup_bj_cmd(interaction: discord.Interaction):
    if not is_admin_user(interaction):
        return await interaction.response.send_message("権限がないのだ", ephemeral=True)
    if BJ_CHANNEL_ID and interaction.channel_id != BJ_CHANNEL_ID:
        return await interaction.response.send_message(
            "指定のブラックジャックチャンネルで実行するのだ",
            ephemeral=True,
        )

    await interaction.response.defer(ephemeral=True)
    if store.config.get("bj_entry_message_id"):
        return await interaction.followup.send("ブラックジャック入口はもう設置済みなのだ", ephemeral=True)

    content = (
        "🎴 ブラックジャック（ずんだもんカジノ）\n\n"
        "・スタートを押して掛け金を入力するのだ\n"
        "・初期手札ブラックジャックは 3:2（1.5倍利益）なのだ\n"
        "・スプリットは同じランク2枚のときだけなのだ\n"
        "・ダブルダウンは1枚引いて終了なのだ（スプリット後は不可）\n"
    )
    msg = await interaction.channel.send(content, view=BjEntryView())
    ok = await sheets_save_config_once_async("bj_entry_message_id", str(msg.id))
    if not ok:
        return await interaction.followup.send("もう設置済みなのだ", ephemeral=True)

    await interaction.followup.send("ブラックジャック入口を設置したのだ", ephemeral=True)


# =========================================================
# 運営コマンド（付与・取り上げ）
# =========================================================
@bot.tree.command(name="admin_grant", description="ロールを付与するのだ（運営用）")
@app_commands.describe(user="対象ユーザー", role="付与するロール")
async def admin_grant_cmd(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    if not is_admin_user(interaction):
        return await interaction.response.send_message("権限がないのだ", ephemeral=True)
    if ADMIN_CHANNEL_ID and interaction.channel_id != ADMIN_CHANNEL_ID:
        return await interaction.response.send_message("このチャンネルでは使えないのだ", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    try:
        await user.add_roles(role, reason="admin_grant")
        await interaction.followup.send(f"{user.display_name} に {role.name} を付与したのだ", ephemeral=True)
    except Exception:
        await interaction.followup.send("付与に失敗したのだ", ephemeral=True)


@bot.tree.command(name="admin_revoke", description="ロールを取り上げるのだ（運営用）")
@app_commands.describe(user="対象ユーザー", role="取り上げるロール")
async def admin_revoke_cmd(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    if not is_admin_user(interaction):
        return await interaction.response.send_message("権限がないのだ", ephemeral=True)
    if ADMIN_CHANNEL_ID and interaction.channel_id != ADMIN_CHANNEL_ID:
        return await interaction.response.send_message("このチャンネルでは使えないのだ", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    try:
        await user.remove_roles(role, reason="admin_revoke")
        await interaction.followup.send(f"{user.display_name} から {role.name} を取り上げたのだ", ephemeral=True)
    except Exception:
        await interaction.followup.send("取り上げに失敗したのだ", ephemeral=True)


@bot.tree.command(name="reload_coins", description="読み取り用シートからコインを再読込するのだ")
async def reload_coins_cmd(interaction: discord.Interaction):
    ok = await safe_defer(interaction, ephemeral=True)
    if not ok:
        return

    try:
        await sheets_reload_users_async()
    except Exception as e:
        print("[reload_coins] error:", e)
        traceback.print_exc()
        return await safe_send(interaction, "再読込に失敗したのだ…（Renderログ確認なのだ）", ephemeral=True)

    uid = interaction.user.id
    u = store.get_user(uid)
    row = store._uid_to_row_coins.get(uid)

    await safe_send(
        interaction,
        f"✅ 再読込したのだ\n"
        f"- あなたのID: {uid}\n"
        f"- coinsシート行: {row if row else '見つからない'}\n"
        f"- 現在の残高: {u['coins']} コインなのだ",
        ephemeral=True,
    )

# =========================================================
# /hoten（補填コマンド：指定ロール保持者のみ）
# =========================================================
HOTEN_ROLE_ID = 1462688366431567872

def has_hoten_role(member: discord.abc.User) -> bool:
    # DMなどで Member じゃない場合は false
    if not isinstance(member, discord.Member):
        return False
    return any(r.id == HOTEN_ROLE_ID for r in member.roles)

@bot.tree.command(
    name="hoten",
    description="（補填）指定ユーザーにコインを付与するのだ（権限ロール限定）"
)
@app_commands.describe(
    user="補填する相手",
    coins="付与するコイン数（1以上）",
    note="メモ（任意）"
)
async def hoten_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    coins: int,
    note: str = "",
):
    # まず権限チェック
    caller = interaction.user
    if not isinstance(caller, discord.Member):
        return await safe_send(interaction, "サーバー内でのみ使えるのだ", ephemeral=True)

    if not has_hoten_role(caller):
        return await safe_send(interaction, "このコマンドを使う権限がないのだ", ephemeral=True)

    if coins <= 0:
        return await safe_send(interaction, "coins は 1以上にしてほしいのだ", ephemeral=True)

    ok = await safe_defer(interaction, ephemeral=True)
    if not ok:
        return

    # 付与処理（ユーザーロック）
    try:
        async with get_user_lock(user.id):
            u = store.get_user(user.id)

            before = int(u.get("coins", 0) or 0)
            u["coins"] = before + int(coins)

            await sheets_upsert_async(u)

        # 実行結果
        memo = f"\nメモ：{note}" if note.strip() else ""
        await safe_send(
            interaction,
            "✅ 補填したのだ\n"
            f"- 対象：{user.mention}（{user.id}）\n"
            f"- 付与：+{coins} コイン\n"
            f"- 残高：{u['coins']} コイン{memo}",
            ephemeral=True,
        )

    except Exception as e:
        print("[hoten] error:", e)
        traceback.print_exc()
        await safe_send(
            interaction,
            "補填に失敗したのだ…（ログ確認なのだ）",
            ephemeral=True,
        )

# =========================================================
# スカル（Skull） DMゲーム：/skull と /skullsolo
#  - 参加時に徴収（軽さ優先）
#  - lobby締切後：参加1人なら自動ソロ化（bet無視→50徴収）
#  - タイムアウト時：全額返金
#  - 勝利報酬：ソロ=100 / マルチ=総額（pot）
#  - NPC行動は1人ずつ2秒間隔でDM送信
#
# ✅ 仕様修正（重要）
#  - 配置で手札は減らない（ラウンド終了で戻る想定）
#  - ただし「同一ラウンド中」は同じカードを複数回置けない（round_handで管理）
#  - 全員が最低1枚置いた後：各ターン「追加で置く」or「入札開始」を選べる
#  - 入札は「パス(0)」or「最高額+1以上」だけ選べる
#  - 💀を踏んだ時だけ手札が1枚減る（永久）
#  - 手札0枚は脱落（ソロなら即敗北、マルチは継続。最後の1人なら即勝利）
#  - 同じターン案内DMが二重に飛ばないように awaiting ガードを追加
# =========================================================

SKULL_SOLO_ENTRY_FEE = 50
SKULL_SOLO_WIN_REWARD = 100

NPC_ACTION_DELAY_SEC = 2.0
SKULL_VIEW_TIMEOUT_SEC = 90
SKULL_TURN_TIMEOUT_SEC = 180
SKULL_GAME_CLEANUP_SEC = 20 * 60

_skull_lobbies: dict[int, dict] = {}   # lobby_message_id -> lobby dict
_skull_games: dict[str, dict] = {}     # game_id -> game dict


def _skull_now() -> float:
    return time.time()


def _skull_gid() -> str:
    return f"skull_{int(time.time()*1000)}_{random.randint(1000,9999)}"


async def dm_send_safe(
    user: discord.abc.User,
    content: str,
    *,
    view: discord.ui.View | None = None,
):
    try:
        if isinstance(user, (discord.User, discord.Member)):
            ch = user.dm_channel or await user.create_dm()
            return await ch.send(content, view=view)
    except Exception:
        return None


async def npc_action_sequence(dm_user: discord.abc.User, lines: list[str]):
    for line in lines:
        await dm_send_safe(dm_user, line)
        await asyncio.sleep(NPC_ACTION_DELAY_SEC)


def _skull_public_name(p: dict) -> str:
    return str(p.get("name") or f"Player{p.get('uid','?')}")


def _skull_is_human(p: dict) -> bool:
    return p.get("type") == "human"


def _skull_humans(game: dict) -> list[dict]:
    return [p for p in game["players"] if _skull_is_human(p)]


def _skull_player(game: dict, uid: int) -> dict | None:
    for p in game["players"]:
        if int(p.get("uid", 0)) == int(uid):
            return p
    return None


async def _skull_broadcast(game: dict, text: str, *, view_for: dict[int, discord.ui.View] | None = None):
    view_for = view_for or {}
    for p in _skull_humans(game):
        uobj = p.get("user_obj")
        if not uobj:
            continue
        await dm_send_safe(uobj, text, view=view_for.get(int(p["uid"])))


def _skull_deck_init() -> list[str]:
    return ["flower", "flower", "flower", "skull"]


def _skull_card_emoji(c: str) -> str:
    return "🌸" if c == "flower" else "💀"


def _skull_card_name(c: str) -> str:
    return "花" if c == "flower" else "スカル"


def _skull_alive_cards(p: dict) -> int:
    # 手札（永久保持分）の残り
    return len(p.get("hand", []))


def _skull_alive_players(game: dict) -> list[dict]:
    return [p for p in game["players"] if _skull_alive_cards(p) > 0 and not p.get("eliminated")]


def _skull_visible_table(game: dict) -> str:
    parts = []
    for p in game["players"]:
        parts.append(
            f"- {_skull_public_name(p)}：{len(p.get('pile', []))}枚（残り手札{_skull_alive_cards(p)}）"
        )
    return "\n".join(parts)


def _skull_all_placed_count(game: dict) -> int:
    return sum(len(p.get("pile", [])) for p in game["players"])


def _skull_all_have_at_least_one(game: dict) -> bool:
    alive = _skull_alive_players(game)
    return bool(alive) and all(len(p.get("pile", [])) >= 1 for p in alive)


def _skull_touch(game: dict):
    game["last_action_ts"] = _skull_now()


def _skull_set_await(game: dict, *, kind: str, uid: int):
    game["await_kind"] = str(kind)
    game["await_uid"] = int(uid)
    game["await_ts"] = _skull_now()


def _skull_clear_await(game: dict):
    game["await_kind"] = None
    game["await_uid"] = None
    game["await_ts"] = 0.0


def _skull_reset_round(game: dict):
    # pileは0に戻し、round_handを「現在の手札」から作り直す
    for p in game["players"]:
        p["pile"] = []
        p["round_hand"] = list(p.get("hand", []))  # このラウンド内で置ける残り
    game["phase"] = "place"
    game["bids"] = {}
    game["highest_bid_uid"] = None
    game["highest_bid"] = 0
    game["reveals_left"] = 0
    game["reveal_target_uid"] = None
    game["starter_idx"] = (game.get("starter_idx", 0) + 1) % len(game["players"])
    game["current_idx"] = game["starter_idx"]
    game["turn_deadline_ts"] = _skull_now() + SKULL_TURN_TIMEOUT_SEC
    _skull_clear_await(game)
    _skull_touch(game)


async def _skull_refund_all(game: dict):
    for p in _skull_humans(game):
        uid = int(p["uid"])
        fee = int(p.get("paid_fee", 0) or 0)
        if fee <= 0:
            continue
        async with get_user_lock(uid):
            u = store.get_user(uid)
            u["coins"] = int(u.get("coins", 0) or 0) + fee
            await sheets_upsert_async(u)
        p["paid_fee"] = 0


async def _skull_payout_winner(game: dict, winner_uid: int):
    is_solo = bool(game.get("is_solo"))
    reward = SKULL_SOLO_WIN_REWARD if is_solo else int(game.get("pot", 0) or 0)

    async with get_user_lock(winner_uid):
        u = store.get_user(winner_uid)
        u["coins"] = int(u.get("coins", 0) or 0) + reward
        u["total_earned"] = int(u.get("total_earned", 0) or 0) + reward
        await sheets_upsert_async(u)


async def _skull_end_game(game_id: str, reason: str):
    game = _skull_games.get(game_id)
    if not game:
        return
    _skull_games.pop(game_id, None)
    await _skull_broadcast(game, f"🧾 スカル終了なのだ\n理由：{reason}")


def _skull_check_auto_win(game: dict) -> tuple[bool, int | None]:
    # 脱落が進んで最後の1人になったら勝利（マルチ/ソロ共通）
    alive = _skull_alive_players(game)
    if len(alive) == 1:
        return True, int(alive[0]["uid"])
    return False, None


@tasks.loop(seconds=20)
async def skull_timeout_watcher():
    now = _skull_now()
    for gid, game in list(_skull_games.items()):
        last = float(game.get("last_action_ts", now))
        if now - last > SKULL_GAME_CLEANUP_SEC:
            await _skull_refund_all(game)
            await _skull_end_game(gid, "長時間操作がなかったため全額返金して終了したのだ")


# ---------------------------------------------------------
# NPCロジック（軽さ優先）
# ---------------------------------------------------------
def _npc_choose_place_card(p: dict) -> str:
    rh = p.get("round_hand") or []
    if not rh:
        return "flower"
    return random.choice(rh)


def _npc_should_start_bid(game: dict, npc: dict) -> bool:
    # 全員1枚置いた後：たまに入札開始（控えめ）
    # 場が増えたら始めやすい
    total = _skull_all_placed_count(game)
    if total <= len(_skull_alive_players(game)):
        return random.random() < 0.10
    return random.random() < 0.20


def _npc_choose_bid(game: dict, p: dict) -> int:
    total = _skull_all_placed_count(game)
    if total <= 0:
        return 0
    # 現在の最高に+1以上が必要
    current = int(game.get("highest_bid", 0) or 0)
    min_bid = current + 1
    if min_bid > total:
        return 0
    # 弱気に：上げても+1か+2くらい
    max_bid = min(total, min_bid + 1)
    if random.random() < 0.35:
        return 0
    return random.randint(min_bid, max_bid)


def _npc_choose_reveal_target(game: dict, npc: dict) -> int:
    alive = _skull_alive_players(game)
    cand = [p for p in alive if len(p.get("pile", [])) > 0]
    if not cand:
        return int(npc["uid"])
    return int(random.choice(cand)["uid"])


# ---------------------------------------------------------
# DM View：配置 or 入札開始（重要）
# ---------------------------------------------------------
class SkullPlaceOrBidView(discord.ui.View):
    def __init__(self, game_id: str, actor_uid: int, *, can_start_bid: bool):
        super().__init__(timeout=SKULL_VIEW_TIMEOUT_SEC)
        self.game_id = game_id
        self.actor_uid = int(actor_uid)

        # 入札開始を無効化したい場合
        self.start_bid_btn.disabled = (not can_start_bid)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return int(interaction.user.id) == self.actor_uid and self.game_id in _skull_games

    @discord.ui.button(label="🌸 花を置く", style=discord.ButtonStyle.primary)
    async def place_flower(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await skull_place_card(interaction, self.game_id, self.actor_uid, "flower")

    @discord.ui.button(label="💀 スカルを置く", style=discord.ButtonStyle.danger)
    async def place_skull(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await skull_place_card(interaction, self.game_id, self.actor_uid, "skull")

    @discord.ui.button(label="💰 入札開始", style=discord.ButtonStyle.success)
    async def start_bid_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await skull_start_bidding_from_player(interaction, self.game_id, self.actor_uid)


# ---------------------------------------------------------
# DM View：入札（パス or 最高+1以上のみ）
# ---------------------------------------------------------
class SkullBidView(discord.ui.View):
    def __init__(self, game_id: str, actor_uid: int, max_bid: int, min_bid: int):
        super().__init__(timeout=SKULL_VIEW_TIMEOUT_SEC)
        self.game_id = game_id
        self.actor_uid = int(actor_uid)
        self.max_bid = int(max_bid)
        self.min_bid = int(min_bid)

        opts = [discord.SelectOption(label="パス（0）", value="0")]
        for n in range(self.min_bid, self.max_bid + 1):
            opts.append(discord.SelectOption(label=str(n), value=str(n)))
        self.add_item(SkullBidSelect(opts, game_id, actor_uid))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return int(interaction.user.id) == self.actor_uid and self.game_id in _skull_games


class SkullBidSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption], game_id: str, actor_uid: int):
        super().__init__(placeholder="入札数を選ぶのだ", min_values=1, max_values=1, options=options)
        self.game_id = game_id
        self.actor_uid = int(actor_uid)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        bid = int(self.values[0])
        await skull_submit_bid(interaction, self.game_id, self.actor_uid, bid)


# ---------------------------------------------------------
# DM View：めくり対象選択
# ---------------------------------------------------------
class SkullRevealTargetView(discord.ui.View):
    def __init__(self, game_id: str, actor_uid: int, choices: list[tuple[int, str]]):
        super().__init__(timeout=SKULL_VIEW_TIMEOUT_SEC)
        self.game_id = game_id
        self.actor_uid = int(actor_uid)

        opts = [discord.SelectOption(label=name, value=str(uid)) for uid, name in choices]
        self.add_item(SkullRevealTargetSelect(opts, game_id, actor_uid))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return int(interaction.user.id) == self.actor_uid and self.game_id in _skull_games


class SkullRevealTargetSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption], game_id: str, actor_uid: int):
        super().__init__(placeholder="どのプレイヤーの山からめくるのだ？", min_values=1, max_values=1, options=options)
        self.game_id = game_id
        self.actor_uid = int(actor_uid)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        target_uid = int(self.values[0])
        await skull_choose_reveal_target(interaction, self.game_id, self.actor_uid, target_uid)


# ---------------------------------------------------------
# ロビー View（Join）
# ---------------------------------------------------------
class SkullLobbyView(discord.ui.View):
    def __init__(self, lobby_msg_id: int, deadline_ts: float):
        timeout = max(5, int(deadline_ts - _skull_now()))
        super().__init__(timeout=timeout)
        self.lobby_msg_id = int(lobby_msg_id)

    async def on_timeout(self):
        await skull_close_lobby(self.lobby_msg_id)

    @discord.ui.button(label="🎟️ 参加", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await skull_lobby_join(interaction, self.lobby_msg_id)

    @discord.ui.button(label="❌ 辞退", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await skull_lobby_leave(interaction, self.lobby_msg_id)


# ---------------------------------------------------------
# ロビー操作
# ---------------------------------------------------------
async def skull_lobby_join(interaction: discord.Interaction, lobby_msg_id: int):
    lobby = _skull_lobbies.get(int(lobby_msg_id))
    if not lobby or lobby.get("status") != "open":
        return await interaction.followup.send("この募集は締め切ったのだ", ephemeral=True)

    uid = int(interaction.user.id)
    if uid in lobby["players"]:
        return await interaction.followup.send("もう参加済みなのだ", ephemeral=True)

    fee = int(lobby["bet"])
    async with get_user_lock(uid):
        u = store.get_user(uid)
        if int(u.get("coins", 0) or 0) < fee:
            return await interaction.followup.send(
                f"コインが足りないのだ（必要:{fee} / 残高:{u['coins']}）",
                ephemeral=True,
            )
        u["coins"] -= fee
        await sheets_upsert_async(u)

    lobby["players"][uid] = {
        "uid": uid,
        "type": "human",
        "name": interaction.user.display_name,
        "paid_fee": fee,
        "user_obj": interaction.user,
    }
    lobby["pot"] += fee

    await interaction.followup.send(f"参加したのだ！（参加費 -{fee}）", ephemeral=True)
    await skull_update_lobby_message(lobby_msg_id)


async def skull_lobby_leave(interaction: discord.Interaction, lobby_msg_id: int):
    lobby = _skull_lobbies.get(int(lobby_msg_id))
    if not lobby or lobby.get("status") != "open":
        return await interaction.followup.send("この募集は締め切ったのだ", ephemeral=True)

    uid = int(interaction.user.id)
    p = lobby["players"].pop(uid, None)
    if not p:
        return await interaction.followup.send("参加してないのだ", ephemeral=True)

    fee = int(p.get("paid_fee", 0) or 0)
    if fee > 0:
        async with get_user_lock(uid):
            u = store.get_user(uid)
            u["coins"] = int(u.get("coins", 0) or 0) + fee
            await sheets_upsert_async(u)
        lobby["pot"] -= fee

    await interaction.followup.send("辞退したのだ（返金したのだ）", ephemeral=True)
    await skull_update_lobby_message(lobby_msg_id)


async def skull_update_lobby_message(lobby_msg_id: int):
    lobby = _skull_lobbies.get(int(lobby_msg_id))
    if not lobby:
        return
    ch = bot.get_channel(lobby["channel_id"]) or await bot.fetch_channel(lobby["channel_id"])
    try:
        msg = await ch.fetch_message(int(lobby_msg_id))
    except Exception:
        return

    names = [f"<@{uid}>" for uid in lobby["players"].keys()]
    joined = " ".join(names) if names else "（まだいないのだ）"

    ends_text = datetime.fromtimestamp(lobby["deadline_ts"], JST).strftime("%Y-%m-%d %H:%M")
    bet = int(lobby["bet"])
    pot = int(lobby["pot"])

    await msg.edit(
        content=(
            "🪙 **スカル募集なのだ**\n\n"
            f"参加費（マルチ時）：{bet} コイン\n"
            f"締切：{ends_text}（JST）\n"
            f"現在の参加者：{len(lobby['players'])}人\n"
            f"{joined}\n\n"
            f"現在のpot：{pot} コインなのだ\n"
            "（締切時に1人だけなら自動ソロに切替、参加費は50にするのだ）"
        )
    )


async def skull_close_lobby(lobby_msg_id: int):
    lobby = _skull_lobbies.get(int(lobby_msg_id))
    if not lobby or lobby.get("status") != "open":
        return
    lobby["status"] = "closed"

    ch = bot.get_channel(lobby["channel_id"]) or await bot.fetch_channel(lobby["channel_id"])
    players = list(lobby["players"].values())

    if len(players) <= 0:
        try:
            await ch.send("🪦 スカル募集締切なのだ\n参加者がいなかったのだ…！")
        except Exception:
            pass
        _skull_lobbies.pop(int(lobby_msg_id), None)
        return

    if len(players) == 1:
        human = players[0]
        uid = int(human["uid"])

        old_fee = int(human.get("paid_fee", 0) or 0)
        if old_fee > 0:
            async with get_user_lock(uid):
                u = store.get_user(uid)
                u["coins"] = int(u.get("coins", 0) or 0) + old_fee
                await sheets_upsert_async(u)

        async with get_user_lock(uid):
            u = store.get_user(uid)
            if int(u.get("coins", 0) or 0) < SKULL_SOLO_ENTRY_FEE:
                await ch.send("🪦 ソロに切替しようとしたけど、50コインが足りないのだ…（中止なのだ）")
                _skull_lobbies.pop(int(lobby_msg_id), None)
                return
            u["coins"] -= SKULL_SOLO_ENTRY_FEE
            await sheets_upsert_async(u)

        human["paid_fee"] = SKULL_SOLO_ENTRY_FEE
        await ch.send("✅ 募集締切：参加者1人なので **自動ソロ** に切り替えるのだ（参加費50）")
        await skull_start_solo(human_player=human)
        _skull_lobbies.pop(int(lobby_msg_id), None)
        return

    await ch.send("✅ 募集締切：マルチで開始するのだ（DMに送るのだ）")
    await skull_start_multi(players=players, pot=int(lobby["pot"]), bet=int(lobby["bet"]))
    _skull_lobbies.pop(int(lobby_msg_id), None)


# ---------------------------------------------------------
# ゲーム開始：ソロ（NPC3人）
# ---------------------------------------------------------
async def skull_start_solo(human_player: dict):
    gid = _skull_gid()
    human_uid = int(human_player["uid"])

    npcs = [
        {"uid": -1, "type": "npc", "name": "ずんだもん", "hand": _skull_deck_init(), "round_hand": [], "pile": [], "score": 0},
        {"uid": -2, "type": "npc", "name": "すごいずんだもん", "hand": _skull_deck_init(), "round_hand": [], "pile": [], "score": 0},
        {"uid": -3, "type": "npc", "name": "大魔神", "hand": _skull_deck_init(), "round_hand": [], "pile": [], "score": 0},
    ]

    human = {
        "uid": human_uid,
        "type": "human",
        "name": human_player.get("name") or "あなた",
        "hand": _skull_deck_init(),
        "round_hand": [],
        "pile": [],
        "score": 0,
        "paid_fee": int(human_player.get("paid_fee", SKULL_SOLO_ENTRY_FEE) or 0),
        "user_obj": human_player.get("user_obj"),
    }

    game = {
        "id": gid,
        "is_solo": True,
        "pot": 0,
        "players": [human] + npcs,
        "starter_idx": 0,
        "current_idx": 0,
        "phase": "place",
        "bids": {},
        "highest_bid_uid": None,
        "highest_bid": 0,
        "reveals_left": 0,
        "reveal_target_uid": None,
        "last_action_ts": _skull_now(),
        "turn_deadline_ts": _skull_now() + SKULL_TURN_TIMEOUT_SEC,
        "await_kind": None,
        "await_uid": None,
        "await_ts": 0.0,
    }
    _skull_games[gid] = game

    await dm_send_safe(human["user_obj"], "🃏 **スカル（ソロ）開始なのだ**\n勝てば +100、負けたら0なのだ\nタイムアウト時は全額返金なのだ")
    _skull_reset_round(game)
    await skull_round_start(gid)


# ---------------------------------------------------------
# ゲーム開始：マルチ
# ---------------------------------------------------------
async def skull_start_multi(players: list[dict], pot: int, bet: int):
    gid = _skull_gid()
    plist = []
    for p in players:
        uid = int(p["uid"])
        plist.append({
            "uid": uid,
            "type": "human",
            "name": p.get("name") or f"User{uid}",
            "hand": _skull_deck_init(),
            "round_hand": [],
            "pile": [],
            "score": 0,
            "paid_fee": int(p.get("paid_fee", bet) or 0),
            "user_obj": p.get("user_obj"),
        })

    game = {
        "id": gid,
        "is_solo": False,
        "pot": int(pot),
        "bet": int(bet),
        "players": plist,
        "starter_idx": 0,
        "current_idx": 0,
        "phase": "place",
        "bids": {},
        "highest_bid_uid": None,
        "highest_bid": 0,
        "reveals_left": 0,
        "reveal_target_uid": None,
        "last_action_ts": _skull_now(),
        "turn_deadline_ts": _skull_now() + SKULL_TURN_TIMEOUT_SEC,
        "await_kind": None,
        "await_uid": None,
        "await_ts": 0.0,
    }
    _skull_games[gid] = game

    await _skull_broadcast(
        game,
        "🃏 **スカル（マルチ）開始なのだ**\n"
        f"pot：{pot} コイン（勝者総取り）\n"
        "タイムアウト時は全額返金なのだ",
    )
    _skull_reset_round(game)
    await skull_round_start(gid)


# ---------------------------------------------------------
# ラウンド開始（配置フェーズ）
# ---------------------------------------------------------
async def skull_round_start(game_id: str):
    game = _skull_games.get(game_id)
    if not game:
        return

    # 自動勝利チェック（最後の1人）
    ok, winner_uid = _skull_check_auto_win(game)
    if ok and winner_uid is not None:
        winner = _skull_player(game, winner_uid)
        if winner and winner.get("type") == "human":
            await _skull_payout_winner(game, winner_uid)
            if game.get("is_solo"):
                await dm_send_safe(winner["user_obj"], f"🎉 ソロ勝利なのだ！ +{SKULL_SOLO_WIN_REWARD} コインなのだ")
            else:
                await _skull_broadcast(game, f"🏆 勝者：{_skull_public_name(winner)}\n総額 {int(game.get('pot',0))} コインを付与したのだ！")
        await _skull_end_game(game_id, "最後の1人になったのだ（勝利）")
        return

    _skull_touch(game)
    game["phase"] = "place"
    game["bids"] = {}
    game["highest_bid_uid"] = None
    game["highest_bid"] = 0
    game["reveals_left"] = 0
    game["reveal_target_uid"] = None
    _skull_clear_await(game)

    await _skull_broadcast(
        game,
        "🔻 **配置フェーズ** なのだ\n"
        "各自、カードを伏せて置くのだ\n"
        "（全員が最低1枚置いた後は、追加で置くか入札開始を選べるのだ）\n\n"
        "現在の場:\n" + _skull_visible_table(game),
    )

    await skull_next_place_turn(game_id)


async def skull_next_place_turn(game_id: str):
    game = _skull_games.get(game_id)
    if not game:
        return
    _skull_touch(game)

    # 期限切れなら返金して終了
    if _skull_now() > float(game.get("turn_deadline_ts", 0) or 0):
        await _skull_refund_all(game)
        await _skull_end_game(game_id, "タイムアウトで全額返金したのだ")
        return

    n = len(game["players"])
    all_one = _skull_all_have_at_least_one(game)

    # 次の行動者を探す（再帰しない：二重DMの原因を潰す）
    for _ in range(n):
        p = game["players"][game["current_idx"]]

        # 脱落/手札0はスキップ
        if _skull_alive_cards(p) <= 0 or p.get("eliminated"):
            game["current_idx"] = (game["current_idx"] + 1) % n
            continue

        # 人間：DM提示（awaitガード付き）
        if p["type"] == "human":
            uid = int(p["uid"])

            # 既に同じ人に同じ待ちを出してるなら再送しない
            if game.get("await_kind") == "place_or_bid" and int(game.get("await_uid") or 0) == uid:
                return

            can_start_bid = bool(all_one)
            view = SkullPlaceOrBidView(game_id, uid, can_start_bid=can_start_bid)

            if all_one:
                msg = "🃏 あなたの番なのだ：\n「1枚置く」か「入札開始」を選ぶのだ"
            else:
                msg = "🃏 あなたの番なのだ：\nまずは最低1枚置くのだ（入札はまだできないのだ）"

            await dm_send_safe(p["user_obj"], msg, view=view)
            _skull_set_await(game, kind="place_or_bid", uid=uid)
            game["turn_deadline_ts"] = _skull_now() + SKULL_TURN_TIMEOUT_SEC
            return

       # NPC：最低1枚置くまでは置く。全員1枚後は「置く/入札開始」判断
        else:
            if not all_one:
                await skull_npc_place_one(game_id, int(p["uid"]))
                game["current_idx"] = (game["current_idx"] + 1) % n
        # ✅ 次へ進める（ここが無いと止まる）
                await skull_next_place_turn(game_id)
                return

        # 全員1枚後：たまに入札開始
            if _npc_should_start_bid(game, p):
                await skull_start_bidding_internal(game_id, starter_uid=int(p["uid"]))
                return

                await skull_npc_place_one(game_id, int(p["uid"]))
                game["current_idx"] = (game["current_idx"] + 1) % n
        # ✅ 次へ進める（ここが無いと止まる）
                await skull_next_place_turn(game_id)
                return

    # 全員スキップされた（あり得る）→安全に次ラウンド
    _skull_reset_round(game)
    await skull_round_start(game_id)


async def skull_npc_place_one(game_id: str, npc_uid: int):
    game = _skull_games.get(game_id)
    if not game:
        return
    npc = _skull_player(game, npc_uid)
    if not npc:
        return
    rh = npc.get("round_hand") or []
    if not rh:
        return

    card = _npc_choose_place_card(npc)
    # round_hand から消す（同一ラウンドで重複配置を防ぐ）
    try:
        rh.remove(card)
    except ValueError:
        pass
    npc["round_hand"] = rh
    npc["pile"].append(card)
    _skull_touch(game)

    humans = _skull_humans(game)
    if humans:
        await npc_action_sequence(humans[0]["user_obj"], [f"🤖 {_skull_public_name(npc)} はカードを1枚伏せて置いたのだ"])


async def skull_place_card(interaction: discord.Interaction, game_id: str, actor_uid: int, card: str):
    game = _skull_games.get(game_id)
    if not game:
        return await interaction.followup.send("ゲームが見つからないのだ", ephemeral=True)

    if _skull_now() > float(game.get("turn_deadline_ts", 0) or 0):
        await _skull_refund_all(game)
        await _skull_end_game(game_id, "タイムアウトで全額返金したのだ")
        return

    p = _skull_player(game, actor_uid)
    if not p or p.get("type") != "human":
        return await interaction.followup.send("あなたの番ではないのだ", ephemeral=True)

    if game.get("phase") != "place":
        return await interaction.followup.send("今は配置フェーズじゃないのだ", ephemeral=True)

    # 全員が最低1枚置くまでは入札開始不可（View側でも無効化済み）
    if card not in ("flower", "skull"):
        return await interaction.followup.send("不正なカードなのだ", ephemeral=True)

    rh = p.get("round_hand") or []
    if card not in rh:
        return await interaction.followup.send("そのカードはこのラウンドではもう置けないのだ", ephemeral=True)

    rh.remove(card)
    p["round_hand"] = rh
    p["pile"].append(card)

    _skull_clear_await(game)
    _skull_touch(game)

    await interaction.followup.send(f"✅ **{_skull_card_name(card)}** を伏せて置いたのだ", ephemeral=True)

    await _skull_broadcast(
        game,
        f"📌 {_skull_public_name(p)} がカードを1枚置いたのだ\n\n現在の場:\n{_skull_visible_table(game)}"
    )

    game["current_idx"] = (game["current_idx"] + 1) % len(game["players"])
    await skull_next_place_turn(game_id)


# ---------------------------------------------------------
# 入札開始（人間ボタン / NPC内部）
# ---------------------------------------------------------
async def skull_start_bidding_from_player(interaction: discord.Interaction, game_id: str, actor_uid: int):
    game = _skull_games.get(game_id)
    if not game:
        return await interaction.followup.send("ゲームがないのだ", ephemeral=True)

    if game.get("phase") != "place":
        return await interaction.followup.send("今は入札開始できないのだ", ephemeral=True)

    if not _skull_all_have_at_least_one(game):
        return await interaction.followup.send("まだ全員が1枚置いてないのだ（入札はまだなのだ）", ephemeral=True)

    p = _skull_player(game, actor_uid)
    if not p or p.get("type") != "human":
        return await interaction.followup.send("あなたの番じゃないのだ", ephemeral=True)

    # awaitガード解除
    _skull_clear_await(game)
    await interaction.followup.send("✅ 入札を開始するのだ", ephemeral=True)
    await skull_start_bidding_internal(game_id, starter_uid=int(actor_uid))


async def skull_start_bidding_internal(game_id: str, starter_uid: int):
    game = _skull_games.get(game_id)
    if not game:
        return
    _skull_touch(game)

    game["phase"] = "bid"
    game["bids"] = {}
    game["highest_bid_uid"] = None
    game["highest_bid"] = 0
    _skull_clear_await(game)

    starter = _skull_player(game, starter_uid)
    await _skull_broadcast(
        game,
        "💰 **入札開始なのだ**\n"
        f"開始者：{_skull_public_name(starter) if starter else starter_uid}\n"
        f"このラウンドの総枚数：{_skull_all_placed_count(game)}\n\n"
        "現在の場:\n" + _skull_visible_table(game),
    )

    # 入札順：入札開始した人から
    idx = 0
    for i, pp in enumerate(game["players"]):
        if int(pp["uid"]) == int(starter_uid):
            idx = i
            break
    game["starter_idx"] = idx
    game["current_idx"] = idx
    game["turn_deadline_ts"] = _skull_now() + SKULL_TURN_TIMEOUT_SEC

    await skull_next_bid_turn(game_id)


# ---------------------------------------------------------
# 入札フェーズ
# ---------------------------------------------------------
async def skull_next_bid_turn(game_id: str):
    game = _skull_games.get(game_id)
    if not game:
        return
    _skull_touch(game)

    if _skull_now() > float(game.get("turn_deadline_ts", 0) or 0):
        await _skull_refund_all(game)
        await _skull_end_game(game_id, "タイムアウトで全額返金したのだ")
        return

    alive = _skull_alive_players(game)
    if all(int(p["uid"]) in game["bids"] for p in alive):
        await skull_finish_bidding(game_id)
        return

    n = len(game["players"])
    for _ in range(n):
        p = game["players"][game["current_idx"]]
        uid = int(p["uid"])

        if _skull_alive_cards(p) <= 0 or p.get("eliminated"):
            game["current_idx"] = (game["current_idx"] + 1) % n
            continue

        if uid in game["bids"]:
            game["current_idx"] = (game["current_idx"] + 1) % n
            continue

        total = _skull_all_placed_count(game)
        current = int(game.get("highest_bid", 0) or 0)
        min_bid = current + 1

        if p["type"] == "human":
            # awaitガード（二重DM防止）
            if game.get("await_kind") == "bid" and int(game.get("await_uid") or 0) == uid:
                return

            view = SkullBidView(game_id, uid, max_bid=total, min_bid=min_bid if min_bid <= total else total + 1)
            txt = f"💰 あなたの入札なのだ（最大 {total} / 現在最高 {current}）"
            await dm_send_safe(p["user_obj"], txt, view=view)
            _skull_set_await(game, kind="bid", uid=uid)
            game["turn_deadline_ts"] = _skull_now() + SKULL_TURN_TIMEOUT_SEC
            return

        # NPC
        bid = _npc_choose_bid(game, p)
        game["bids"][uid] = bid
        if bid > int(game.get("highest_bid", 0) or 0):
            game["highest_bid"] = bid
            game["highest_bid_uid"] = uid

        humans = _skull_humans(game)
        if humans:
            await npc_action_sequence(humans[0]["user_obj"], [f"🤖 {_skull_public_name(p)} は **{bid}** で入札したのだ"])

        game["current_idx"] = (game["current_idx"] + 1) % n
        break

    await skull_next_bid_turn(game_id)


async def skull_submit_bid(interaction: discord.Interaction, game_id: str, actor_uid: int, bid: int):
    game = _skull_games.get(game_id)
    if not game:
        return await interaction.followup.send("ゲームがないのだ", ephemeral=True)

    if _skull_now() > float(game.get("turn_deadline_ts", 0) or 0):
        await _skull_refund_all(game)
        await _skull_end_game(game_id, "タイムアウトで全額返金したのだ")
        return

    if game.get("phase") != "bid":
        return await interaction.followup.send("今は入札フェーズじゃないのだ", ephemeral=True)

    p = _skull_player(game, actor_uid)
    if not p or p.get("type") != "human":
        return await interaction.followup.send("あなたの番ではないのだ", ephemeral=True)

    uid = int(actor_uid)
    if uid in game["bids"]:
        return await interaction.followup.send("もう入札したのだ", ephemeral=True)

    total = _skull_all_placed_count(game)
    current = int(game.get("highest_bid", 0) or 0)
    min_bid = current + 1

    if bid != 0:
        if bid < min_bid or bid > total:
            return await interaction.followup.send(f"入札が不正なのだ（パス=0 か {min_bid}〜{total}）", ephemeral=True)

    game["bids"][uid] = int(bid)
    if bid > current:
        game["highest_bid"] = int(bid)
        game["highest_bid_uid"] = uid

    _skull_clear_await(game)
    _skull_touch(game)

    await interaction.followup.send(f"✅ 入札：{bid} なのだ", ephemeral=True)
    await _skull_broadcast(game, f"💰 {_skull_public_name(p)} が **{bid}** で入札したのだ")

    game["current_idx"] = (game["current_idx"] + 1) % len(game["players"])
    await skull_next_bid_turn(game_id)


async def skull_finish_bidding(game_id: str):
    game = _skull_games.get(game_id)
    if not game:
        return
    _skull_touch(game)

    highest_uid = game.get("highest_bid_uid")
    highest = int(game.get("highest_bid", 0) or 0)

    if not highest_uid or highest <= 0:
        await _skull_broadcast(game, "🌀 全員パスっぽいのだ…ラウンドをやり直すのだ")
        _skull_reset_round(game)
        await skull_round_start(game_id)
        return

    game["phase"] = "reveal"
    game["reveals_left"] = int(highest)
    game["reveal_target_uid"] = int(highest_uid)

    bidder = _skull_player(game, int(highest_uid))
    await _skull_broadcast(
        game,
        "🏁 **入札確定なのだ**\n"
        f"落札者：{_skull_public_name(bidder) if bidder else highest_uid}\n"
        f"めくる枚数：{highest}\n"
        "ここからは落札者がめくるのだ",
    )

    await skull_prompt_reveal_target(game_id)


# ---------------------------------------------------------
# めくりフェーズ
# ---------------------------------------------------------
async def skull_prompt_reveal_target(game_id: str):
    game = _skull_games.get(game_id)
    if not game:
        return
    _skull_touch(game)

    if _skull_now() > float(game.get("turn_deadline_ts", 0) or 0):
        await _skull_refund_all(game)
        await _skull_end_game(game_id, "タイムアウトで全額返金したのだ")
        return

    uid = int(game["reveal_target_uid"])
    actor = _skull_player(game, uid)
    if not actor or actor.get("eliminated") or _skull_alive_cards(actor) <= 0:
        # 落札者が脱落してたら（基本ないが）次ラウンドへ
        _skull_reset_round(game)
        await skull_round_start(game_id)
        return

    choices = []
    for p in _skull_alive_players(game):
        if len(p.get("pile", [])) > 0:
            choices.append((int(p["uid"]), _skull_public_name(p)))

    if not choices:
        await _skull_broadcast(game, "場にめくれるカードが無いのだ…ラウンドやり直しなのだ")
        _skull_reset_round(game)
        await skull_round_start(game_id)
        return

    if actor["type"] == "npc":
        t_uid = _npc_choose_reveal_target(game, actor)
        humans = _skull_humans(game)
        if humans:
            t = _skull_player(game, t_uid) or {"name": str(t_uid)}
            await npc_action_sequence(humans[0]["user_obj"], [f"🤖 {_skull_public_name(actor)} は **{_skull_public_name(t)}** をめくるのだ"])
        await skull_resolve_reveal(game_id, uid, t_uid)
        return

    # awaitガード
    if game.get("await_kind") == "reveal" and int(game.get("await_uid") or 0) == uid:
        return

    view = SkullRevealTargetView(game_id, uid, choices)
    await dm_send_safe(actor["user_obj"], f"🫴 めくる対象を選ぶのだ（残り {game['reveals_left']} 枚）", view=view)
    _skull_set_await(game, kind="reveal", uid=uid)
    game["turn_deadline_ts"] = _skull_now() + SKULL_TURN_TIMEOUT_SEC


async def skull_choose_reveal_target(interaction: discord.Interaction, game_id: str, actor_uid: int, target_uid: int):
    game = _skull_games.get(game_id)
    if not game:
        return await interaction.followup.send("ゲームがないのだ", ephemeral=True)

    if _skull_now() > float(game.get("turn_deadline_ts", 0) or 0):
        await _skull_refund_all(game)
        await _skull_end_game(game_id, "タイムアウトで全額返金したのだ")
        return

    if int(game.get("reveal_target_uid")) != int(actor_uid):
        return await interaction.followup.send("今はあなたのめくり番じゃないのだ", ephemeral=True)

    target = _skull_player(game, int(target_uid))
    if not target or target.get("eliminated") or len(target.get("pile", [])) <= 0:
        return await interaction.followup.send("その人の山にめくれるカードがないのだ", ephemeral=True)

    _skull_clear_await(game)

    await interaction.followup.send(f"✅ **{_skull_public_name(target)}** をめくるのだ", ephemeral=True)
    await _skull_broadcast(game, f"🫴 {_skull_public_name(_skull_player(game, actor_uid) or {'name':actor_uid})} が **{_skull_public_name(target)}** をめくるのだ")

    await skull_resolve_reveal(game_id, actor_uid, target_uid)


async def skull_resolve_reveal(game_id: str, actor_uid: int, target_uid: int):
    game = _skull_games.get(game_id)
    if not game:
        return
    _skull_touch(game)

    actor = _skull_player(game, int(actor_uid))
    target = _skull_player(game, int(target_uid))
    if not actor or not target or target.get("eliminated") or len(target.get("pile", [])) <= 0:
        return

    card = target["pile"].pop()
    game["reveals_left"] -= 1

    await _skull_broadcast(
        game,
        f"🃏 めくったのだ：{_skull_public_name(target)} のカード → **{_skull_card_emoji(card)} {_skull_card_name(card)}**\n"
        f"残りめくり：{max(0, int(game['reveals_left']))}枚"
    )

    # 💀 なら失敗：手札（永久）を1枚失う
    if card == "skull":
        await _skull_broadcast(game, f"💥 **スカルを踏んだのだ！**\n{_skull_public_name(actor)} はペナルティなのだ")

        if len(actor.get("hand", [])) > 0:
            lost = random.choice(actor["hand"])
            actor["hand"].remove(lost)

        # 手札0なら脱落
        if len(actor.get("hand", [])) <= 0:
            actor["eliminated"] = True
            await _skull_broadcast(game, f"🪦 {_skull_public_name(actor)} は手札0枚で脱落なのだ")

            if game.get("is_solo"):
                human = _skull_humans(game)[0]
                await dm_send_safe(human["user_obj"], "🪦 ソロスカル：手札が尽きたのだ…負けなのだ（報酬0）")
                await _skull_end_game(game_id, "ソロ敗北なのだ")
                return

        # ラウンド終了 → 次ラウンド
        _skull_reset_round(game)
        await skull_round_start(game_id)
        return

    # 花で、まだめくりが残るなら続行
    if int(game["reveals_left"]) > 0:
        game["turn_deadline_ts"] = _skull_now() + SKULL_TURN_TIMEOUT_SEC
        await skull_prompt_reveal_target(game_id)
        return

    # 成功：得点+1
    actor["score"] = int(actor.get("score", 0) or 0) + 1
    await _skull_broadcast(game, f"✅ **成功なのだ！** {_skull_public_name(actor)} の得点：{actor['score']}")

    # 勝利条件：2点
    if actor["score"] >= 2:
        if actor["type"] == "human":
            await _skull_payout_winner(game, int(actor["uid"]))
            if game.get("is_solo"):
                await dm_send_safe(actor["user_obj"], f"🎉 ソロ勝利なのだ！ +{SKULL_SOLO_WIN_REWARD} コインなのだ")
            else:
                await _skull_broadcast(game, f"🏆 勝者：{_skull_public_name(actor)}\n総額 {int(game.get('pot',0))} コインを付与したのだ！")
        else:
            humans = _skull_humans(game)
            if humans:
                await dm_send_safe(humans[0]["user_obj"], "🪦 ソロスカル：NPCが先に2点取ったのだ…負けなのだ（報酬0）")
        await _skull_end_game(game_id, "ゲーム終了なのだ")
        return

    _skull_reset_round(game)
    await skull_round_start(game_id)


# ---------------------------------------------------------
# /skullsolo（即ソロ）
# ---------------------------------------------------------
@bot.tree.command(name="skullsolo", description="スカルをソロで遊ぶのだ（参加費50）")
async def skullsolo_cmd(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        pass

    uid = int(interaction.user.id)

    async with get_user_lock(uid):
        u = store.get_user(uid)
        if int(u.get("coins", 0) or 0) < SKULL_SOLO_ENTRY_FEE:
            return await interaction.followup.send("コインが足りないのだ（必要50）", ephemeral=True)
        u["coins"] -= SKULL_SOLO_ENTRY_FEE
        await sheets_upsert_async(u)

    human = {
        "uid": uid,
        "type": "human",
        "name": interaction.user.display_name,
        "paid_fee": SKULL_SOLO_ENTRY_FEE,
        "user_obj": interaction.user,
    }

    await interaction.followup.send("✅ ソロを開始するのだ（DMを見てほしいのだ）", ephemeral=True)
    await skull_start_solo(human_player=human)


# ---------------------------------------------------------
# /skull（募集）
# ---------------------------------------------------------
@bot.tree.command(name="skull", description="スカル募集をするのだ（締切あり）")
@app_commands.describe(
    bet="参加費（マルチ時）",
    minutes="締切までの分数（1以上）",
)
async def skull_cmd(interaction: discord.Interaction, bet: int, minutes: int):
    if minutes < 1:
        return await interaction.response.send_message("minutesは1以上なのだ", ephemeral=True)
    if bet < 1:
        return await interaction.response.send_message("betは1以上なのだ", ephemeral=True)

    deadline_ts = _skull_now() + (minutes * 60)
    await interaction.response.defer(ephemeral=True)

    msg = await interaction.channel.send(
        "🪙 **スカル募集なのだ**\n準備中なのだ…",
        view=SkullLobbyView(lobby_msg_id=0, deadline_ts=deadline_ts),
    )

    lobby = {
        "status": "open",
        "channel_id": int(msg.channel.id),
        "guild_id": int(interaction.guild_id or 0),
        "created_by": int(interaction.user.id),
        "created_at": int(_skull_now()),
        "deadline_ts": float(deadline_ts),
        "bet": int(bet),
        "pot": 0,
        "players": {},
    }
    _skull_lobbies[int(msg.id)] = lobby

    await msg.edit(view=SkullLobbyView(lobby_msg_id=msg.id, deadline_ts=deadline_ts))
    await skull_update_lobby_message(msg.id)

    await interaction.followup.send("募集を作成したのだ", ephemeral=True)

# =========================================================
# 起動イベント
# =========================================================
@bot.event
async def on_ready():
    global STORE_READY, VIEWS_READY

    if not STORE_READY:
        try:
            await sheets_init_async()
            STORE_READY = True
            print("SheetsStore initialized")
        except Exception as e:
            print("SheetsStore init failed:", e)
            traceback.print_exc()
            try:
                await bot.close()
            finally:
                os._exit(1)

    try:
        await bot.tree.sync()
    except Exception as e:
        print("tree sync failed:", e)
        traceback.print_exc()

    # ✅ 永続View登録（再接続でも重複登録しない）
    if not VIEWS_READY:
        bot.add_view(ShopEntryView())
        bot.add_view(BjEntryView())
        bot.add_view(BJActionView())
        bot.add_view(NumaEntryView())
        VIEWS_READY = True

    if not check_tasks.is_running():
        check_tasks.start()
    if not check_join_tasks.is_running():
        check_join_tasks.start()
    if not cleanup_bj_sessions.is_running():
        cleanup_bj_sessions.start()

    print(f"Bot logged in as {bot.user}")
    
    if not lottery_watcher.is_running():
        lottery_watcher.start()

    if not skull_timeout_watcher.is_running():
        skull_timeout_watcher.start()

# =========================================================
# Flask Keep Alive
# =========================================================
app = Flask("")


@app.route("/")
def home():
    return "Bot is running!", 200


def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()


# =========================================================
# Bot 起動（✅ bot.run に一本化）
# =========================================================
if __name__ == "__main__":
    init_ai_memory_db()
    keep_alive()

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("DISCORD_TOKEN not set!")
        raise SystemExit(1)

    bot.run(token)




