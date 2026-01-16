import os
import asyncio
import math
import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timedelta, timezone, date
from flask import Flask
from threading import Thread, Lock
import aiohttp
import sqlite3
import csv
import io
import time
import random
import json

from openai import OpenAI

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# =========================================================
# 設定
# =========================================================
JST = timezone(timedelta(hours=9))

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
client = OpenAI()

# 既存
tasks_data = {}
join_tasks = {}

PLACE_LIST = [
    "パシフィック", "オイルリグ", "アーティファクト", "飛行場", "客船",
    "ユニオン", "パレト", "ボブキャット", "市長の工場"
]

# ---------------------------------------------------------
# /craft 用（既存のまま）
# ---------------------------------------------------------
TOOL_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRH53VZ7iL7EFXNhkGTmRBS0JdE6oAjex51ape3cqOoXnuoR7RGATJlq_TaLupYmT4YJB2Luaa5NwXx/pub?gid=0&single=true&output=csv"
WEAPON_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRH53VZ7iL7EFXNhkGTmRBS0JdE6oAjex51ape3cqOoXnuoR7RGATJlq_TaLupYmT4YJB2Luaa5NwXx/pub?gid=793378898&single=true&output=csv"

async def fetch_csv(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as r:
            text = await r.text()
    f = io.StringIO(text)
    reader = csv.DictReader(f)
    return [row for row in reader]

CSV_CACHE = {"道具": [], "武器": [], "timestamp": 0}

async def get_csv(category: str):
    now = time.time()
    if CSV_CACHE["timestamp"] and now - CSV_CACHE["timestamp"] < 300:
        return CSV_CACHE[category]

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
"""

# =========================================================
# 既存：AIメモリ(SQLite)（そのまま）
# =========================================================
def init_ai_memory_db():
    conn = sqlite3.connect("ai_memory.db")
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_summary (
        user_id INTEGER PRIMARY KEY,
        summary TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS chat_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        message TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()

def save_chat(user_id: int, message: str):
    conn = sqlite3.connect("ai_memory.db")
    cur = conn.cursor()
    cur.execute("INSERT INTO chat_log (user_id, message) VALUES (?, ?)", (user_id, message))
    conn.commit()
    conn.close()

def get_recent_chats(user_id: int, limit=3):
    conn = sqlite3.connect("ai_memory.db")
    cur = conn.cursor()
    cur.execute("SELECT message FROM chat_log WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))
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
    cur.execute("""
    INSERT INTO user_summary (user_id, summary)
    VALUES (?, ?)
    ON CONFLICT(user_id) DO UPDATE SET summary=excluded.summary
    """, (user_id, summary))
    conn.commit()
    conn.close()

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

SHOP_CHANNEL_ID = _env_int("SHOP_CHANNEL_ID")
BJ_CHANNEL_ID = _env_int("BJ_CHANNEL_ID")
ADMIN_CHANNEL_ID = _env_int("ADMIN_CHANNEL_ID")

def _role_env(name: str):
    return _env_int(name)

TITLE_ROLE_1000 = _role_env("TITLE_ROLE_1000")
TITLE_ROLE_5000 = _role_env("TITLE_ROLE_5000")
TITLE_ROLE_10000 = _role_env("TITLE_ROLE_10000")
TITLE_ROLE_100000 = _role_env("TITLE_ROLE_100000")

ROLE_DAIKICHI_10 = _role_env("ROLE_DAIKICHI_10")
ROLE_DAIKYO_10   = _role_env("ROLE_DAIKYO_10")

ROLE_JP_FIRST    = _role_env("ROLE_JP_FIRST")
ROLE_JP_MULTI    = _role_env("ROLE_JP_MULTI")
ROLE_BAR_MISS    = _role_env("ROLE_BAR_MISS")

ROLE_BJ_FIRSTWIN = _role_env("ROLE_BJ_FIRSTWIN")
ROLE_BJ_3STREAK  = _role_env("ROLE_BJ_3STREAK")
ROLE_BJ_BIGWIN   = _role_env("ROLE_BJ_BIGWIN")
ROLE_BJ_BIGLOSE  = _role_env("ROLE_BJ_BIGLOSE")
ROLE_BJ_100PLAY  = _role_env("ROLE_BJ_100PLAY")

SHOP_ITEMS = [
    {"key": "title_1000",   "name": "🌱 ずんだ見習い",  "price": 1000,   "role_id": TITLE_ROLE_1000},
    {"key": "title_5000",   "name": "🌿 ずんだ常連",    "price": 5000,   "role_id": TITLE_ROLE_5000},
    {"key": "title_10000",  "name": "🧠 ずんだの策士",  "price": 10000,  "role_id": TITLE_ROLE_10000},
    {"key": "title_100000", "name": "👑 ずんだの伝説",  "price": 100000, "role_id": TITLE_ROLE_100000},
]

MANAGED_TITLE_ROLES = set(
    rid for rid in [
        TITLE_ROLE_1000, TITLE_ROLE_5000, TITLE_ROLE_10000, TITLE_ROLE_100000,
        ROLE_DAIKICHI_10, ROLE_DAIKYO_10,
        ROLE_JP_FIRST, ROLE_JP_MULTI, ROLE_BAR_MISS,
        ROLE_BJ_FIRSTWIN, ROLE_BJ_3STREAK, ROLE_BJ_BIGWIN, ROLE_BJ_BIGLOSE, ROLE_BJ_100PLAY
    ] if isinstance(rid, int) and rid > 0
)

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

class SheetsStore:
    def __init__(self):
        self._lock = Lock()
        self.gc = None
        self.sh = None
        self.ws_users = None
        self.ws_config = None
        self.users = {}
        self.config = {}

    def init(self):
        if not GS_SERVICE_ACCOUNT_JSON or not GS_SPREADSHEET_ID:
            raise RuntimeError("GS_SERVICE_ACCOUNT_JSON / GS_SPREADSHEET_ID が未設定です")

        info = json.loads(GS_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        self.gc = gspread.authorize(creds)
        self.sh = self.gc.open_by_key(GS_SPREADSHEET_ID)

        try:
            self.ws_users = self.sh.worksheet(GS_SHEET_NAME)
        except Exception:
            self.ws_users = self.sh.add_worksheet(title=GS_SHEET_NAME, rows=2000, cols=30)

        try:
            self.ws_config = self.sh.worksheet("設定")
        except Exception:
            self.ws_config = self.sh.add_worksheet(title="設定", rows=200, cols=5)

        self._ensure_headers()
        self._load_config()
        self._load_users()

    def _ensure_headers(self):
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

    def _load_users(self):
        with self._lock:
            rows = self.ws_users.get_all_records()
        users = {}
        for r in rows:
            try:
                uid = int(r.get("user_id") or 0)
            except Exception:
                continue
            if uid <= 0:
                continue
            users[uid] = self._normalize_user_row(uid, r)
        self.users = users

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

    def _find_user_row_index(self, uid: int):
        with self._lock:
            col = self.ws_users.col_values(1)
        for idx, v in enumerate(col[1:], start=2):
            try:
                if int(v) == uid:
                    return idx
            except Exception:
                continue
        return None

    def upsert_user(self, u: dict):
        with self._lock:
            header = self.ws_users.row_values(1)
        values = [u.get(h, "") for h in header]

        idx = self._find_user_row_index(u["user_id"])
        with self._lock:
            if idx is None:
                self.ws_users.append_row(values)
            else:
                end_col = chr(ord("A") + len(values) - 1)
                self.ws_users.update(f"A{idx}:{end_col}{idx}", [values])

store = SheetsStore()

async def sheets_init_async():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, store.init)

async def sheets_upsert_async(u: dict):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: store.upsert_user(u))

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
    to_remove = []
    for r in member.roles:
        if r.id in MANAGED_TITLE_ROLES:
            to_remove.append(r)
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

async def ai_fortune_message() -> tuple[str, str]:
    fortune = random.choices(
        population=FORTUNE_CHOICES,
        weights=[5, 12, 16, 25, 18, 16, 8],
        k=1
    )[0]
    prompt = [
        {"role": "system", "content": ZUNDAMON_SYSTEM},
        {"role": "user", "content": f"今日の占い結果は「{fortune}」なのだ。短めに一言コメントしてほしいのだ。"}
    ]
    loop = asyncio.get_running_loop()
    resp = await loop.run_in_executor(
        None,
        lambda: client.chat.completions.create(
            model="gpt-4o-mini",
            messages=prompt,
            max_tokens=80,
            temperature=0.8
        )
    )
    msg = resp.choices[0].message.content.strip()
    return fortune, msg

async def maybe_award_hidden_titles(interaction: discord.Interaction, u: dict, just_events: set[str]):
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
            "🎉🎉🎉\n✨【偉業達成】✨\n\nあなたは「大吉」を10回引いたのだ！\n特別ロール\n🌱「ずんだの加護を受けし者」\nを獲得したのだよ！\n🎉🎉🎉"
        )

    if u["daikyo_count"] >= 10:
        await award_once(
            "AWARD_DAIKYO_10",
            ROLE_DAIKYO_10,
            "🎉🎉🎉\n✨【逆境の証】✨\n\nあなたは「大凶」を10回も引いたのだ…\nここまで来ると才能なのだよ！\n💀「ずんだに試されし者」\nを獲得したのだ！\n🎉🎉🎉"
        )

    if u["jackpot_count"] >= 1:
        await award_once(
            "AWARD_JP_FIRST",
            ROLE_JP_FIRST,
            "🎉🎉🎉\n✨【奇跡の瞬間】✨\n\n/diceでジャックポットを\n初めて引き当てたのだ！\n🎰「ずんだの寵愛を受けし者」\nを獲得したのだよ！\n🎉🎉🎉"
        )

    if u["jackpot_count"] >= 3:
        await award_once(
            "AWARD_JP_MULTI_3",
            ROLE_JP_MULTI,
            "🎉🎉🎉\n✨【常識外れ】✨\n\nあなたはジャックポットを\n何度も引き当てたのだ…！\nこれはもう偶然じゃないのだ！\n🎰🎰「ずんだに選ばれし者」\nを獲得したのだよ！\n🎉🎉🎉"
        )

    # BAR外れは「初回のみ」＝ award_keys で初回だけ付与・演出
    if "BAR_MISS_EVENT" in just_events:
        await award_once(
            "AWARD_BAR_MISS",
            ROLE_BAR_MISS,
            "🎉🎉🎉\n✨【惜敗の極み】✨\n\n7・7・BARの後、\n期待を背負って外したのだ…！\n🍀「ずんだに弄ばれし者」\nを獲得したのだよ！\n🎉🎉🎉"
        )

    if u["bj_win_streak"] >= 1 and "BJ_WIN_EVENT" in just_events:
        await award_once(
            "AWARD_BJ_FIRSTWIN",
            ROLE_BJ_FIRSTWIN,
            "🎉🎉🎉\n✨【初勝利】✨\n\nブラックジャックで\n初めて勝利したのだ！\n🎴「ずんだの勝負師見習い」\nを獲得したのだよ！\n🎉🎉🎉"
        )

    if u["bj_win_streak"] >= 3:
        await award_once(
            "AWARD_BJ_3STREAK",
            ROLE_BJ_3STREAK,
            "🎉🎉🎉\n✨【波に乗れ】✨\n\nブラックジャックで\n3連勝を達成したのだ！\n🔥「ずんだの勝負師」\nを獲得したのだよ！\n🎉🎉🎉"
        )

    if u["bj_play_count"] >= 100:
        await award_once(
            "AWARD_BJ_100PLAY",
            ROLE_BJ_100PLAY,
            "🎉🎉🎉\n✨【熟練の域】✨\n\nブラックジャックを\n100回以上プレイしたのだ！\n🃏「ずんだのブラックジャック職人」\nを獲得したのだよ！\n🎉🎉🎉"
        )

    if "BJ_BIGWIN_EVENT" in just_events:
        await award_once(
            "AWARD_BJ_BIGWIN",
            ROLE_BJ_BIGWIN,
            "🎉🎉🎉\n✨【一攫千金】✨\n\nブラックジャックで\n1回の勝負で\n1,000コイン以上を\n獲得したのだ！\n💎「ずんだの大勝負師」\nを獲得したのだよ！\n🎉🎉🎉"
        )
    if "BJ_BIGLOSE_EVENT" in just_events:
        await award_once(
            "AWARD_BJ_BIGLOSE",
            ROLE_BJ_BIGLOSE,
            "🎉🎉🎉\n✨【破滅への道】✨\n\nブラックジャックで\n一度に1,000コイン以上\n失ったのだ……\n💀「ずんだの破滅王」\nを獲得したのだよ！\n🎉🎉🎉"
        )

# =========================================================
# 入口メッセージ（ショップ・BJ）
# =========================================================
class ShopEntryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🛒 ショップを開く", style=discord.ButtonStyle.primary, custom_id="shop_open_btn")
    async def shop_open(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_in_channel(interaction, SHOP_CHANNEL_ID):
            return await interaction.response.send_message("このチャンネルでは使えないのだ", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        u = store.get_user(interaction.user.id)

        owned = title_inventory(u)
        options = []
        for it in SHOP_ITEMS:
            rid = it.get("role_id")
            if not rid:
                continue
            if rid in owned:
                continue
            options.append(discord.SelectOption(
                label=f"{it['name']}（{it['price']}）",
                value=it["key"],
                description="購入するのだ"
            ))

        msg = f"🏷️ 称号ショップ\n\n現在の残高：{u['coins']} コイン\n"
        if not options:
            msg += "\n購入できる称号はないのだ"
            return await interaction.followup.send(msg, ephemeral=True)

        view = discord.ui.View(timeout=60)
        select = ShopBuySelect(options)
        view.add_item(select)
        await interaction.followup.send(msg + "\n購入する称号を選ぶのだ", view=view, ephemeral=True)

    @discord.ui.button(label="🎖️ 称号を付与する", style=discord.ButtonStyle.secondary, custom_id="shop_title_assign_btn")
    async def title_assign(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_in_channel(interaction, SHOP_CHANNEL_ID):
            return await interaction.response.send_message("このチャンネルでは使えないのだ", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        u = store.get_user(interaction.user.id)
        owned = title_inventory(u)
        options = []
        for rid in sorted(owned):
            role = interaction.guild.get_role(rid)
            if not role:
                continue
            options.append(discord.SelectOption(label=role.name, value=str(rid)))

        if not options:
            return await interaction.followup.send("付与できる称号がないのだ", ephemeral=True)

        view = discord.ui.View(timeout=60)
        view.add_item(TitleAssignSelect(options))
        await interaction.followup.send("付与する称号を選ぶのだ", view=view, ephemeral=True)

    @discord.ui.button(label="🎁 ログインボーナス", style=discord.ButtonStyle.success, custom_id="shop_daily_btn")
    async def daily(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_in_channel(interaction, SHOP_CHANNEL_ID):
            return await interaction.response.send_message("このチャンネルでは使えないのだ", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        u = store.get_user(interaction.user.id)

        today = datetime.now(JST).date()
        last = None
        if u.get("last_login_ymd"):
            try:
                y, m, d = map(int, u["last_login_ymd"].split("-"))
                last = date(y, m, d)
            except Exception:
                last = None

        if last == today:
            return await interaction.followup.send("今日はもう受け取っているのだ", ephemeral=True)

        if last == (today - timedelta(days=1)):
            u["login_streak"] += 1
        else:
            u["login_streak"] = 1

        u["login_total"] += 1
        u["last_login_ymd"] = today.strftime("%Y-%m-%d")

        base = 10
        extra = calc_login_extra(u["login_streak"])
        streak_gain = base + extra

        fortune, fortune_msg = await ai_fortune_message()
        fortune_gain = FORTUNE_COIN.get(fortune, 0)

        u["coins"] += (streak_gain + fortune_gain)
        u["total_earned"] += (streak_gain + fortune_gain)

        if fortune == "大吉":
            u["daikichi_count"] += 1
        if fortune == "大凶":
            u["daikyo_count"] += 1

        await sheets_upsert_async(u)

        msg = (
            f"🎁 ログインボーナスなのだ\n\n"
            f"連続ログイン：{u['login_streak']}日\n"
            f"+{streak_gain} コイン\n\n"
            f"🔮 今日の占い：{fortune}\n"
            f"{fortune_msg}\n"
            f"+{fortune_gain} コイン\n\n"
            f"現在の残高：{u['coins']} コイン"
        )
        await interaction.followup.send(msg, ephemeral=True)

        await maybe_award_hidden_titles(interaction, u, just_events=set())

    @discord.ui.button(label="💰 残高", style=discord.ButtonStyle.secondary, custom_id="shop_balance_btn")
    async def balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_in_channel(interaction, SHOP_CHANNEL_ID):
            return await interaction.response.send_message("このチャンネルでは使えないのだ", ephemeral=True)
        u = store.get_user(interaction.user.id)
        await interaction.response.send_message(f"現在の残高：{u['coins']} コインなのだ", ephemeral=True)

class ShopBuySelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(placeholder="購入する称号を選ぶのだ", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        u = store.get_user(interaction.user.id)

        key = self.values[0]
        item = next((x for x in SHOP_ITEMS if x["key"] == key), None)
        if not item or not item.get("role_id"):
            return await interaction.followup.send("その商品は無効なのだ", ephemeral=True)

        rid = item["role_id"]
        price = item["price"]

        owned = title_inventory(u)
        if rid in owned:
            return await interaction.followup.send("それはもう購入済みなのだ", ephemeral=True)

        if u["coins"] < price:
            return await interaction.followup.send("コインが足りないのだ", ephemeral=True)

        u["coins"] -= price
        add_title_to_inventory(u, rid)
        u["title_role_id"] = rid

        member = interaction.user
        if not isinstance(member, discord.Member):
            member = await interaction.guild.fetch_member(interaction.user.id)

        await apply_title_role(member, rid)
        await sheets_upsert_async(u)

        await interaction.followup.send(
            f"🎉 {item['name']} を購入したのだ！\n残高：{u['coins']} コインなのだ",
            ephemeral=True
        )

class TitleAssignSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(placeholder="付与する称号を選ぶのだ", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
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
        await interaction.followup.send(f"🎖️ {role.name if role else '称号'} を付与したのだ", ephemeral=True)

class BjEntryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎴 スタート", style=discord.ButtonStyle.primary, custom_id="bj_start_entry_btn")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_in_channel(interaction, BJ_CHANNEL_ID):
            return await interaction.response.send_message("このチャンネルでは使えないのだ", ephemeral=True)
        u = store.get_user(interaction.user.id)
        await interaction.response.send_modal(BetModal(balance=u["coins"]))

# =========================================================
# /setup_shop と /setup_bj （最初の1回のみ）
# =========================================================
@bot.tree.command(name="setup_shop", description="ショップ入口メッセージを設置するのだ（最初の1回のみ）")
async def setup_shop_cmd(interaction: discord.Interaction):
    if not is_admin_user(interaction):
        return await interaction.response.send_message("権限がないのだ", ephemeral=True)
    if SHOP_CHANNEL_ID and interaction.channel_id != SHOP_CHANNEL_ID:
        return await interaction.response.send_message("指定のショップチャンネルで実行するのだ", ephemeral=True)

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

    # =========================================================
    # 初期配布：全員に100コイン（未登録ユーザーのみ）※1回だけ
    # =========================================================
    already = store.config.get("initial_airdrop_done")
    if not already:
        await interaction.followup.send("初期コイン配布を開始するのだ（100コイン）", ephemeral=True)

        count_new = 0
        for member in interaction.guild.members:
            if member.bot:
                continue

            u = store.get_user(member.id)

            # すでに管理シートに存在する（＝過去に一度でも保存されている）ならスキップ
            # 判定は last_login_ymd や owned_title_role_ids では揺れるので、
            # 「user_idが存在するか」をシート側でチェックするのが理想だが、
            # ここでは「初回のみ」＆「未登録=coins=0のまま」想定で安全寄りにする
            if u.get("coins", 0) != 0 or u.get("login_total", 0) != 0 or u.get("total_earned", 0) != 0:
                continue

            u["coins"] = 100
            u["total_earned"] += 100
            await sheets_upsert_async(u)
            count_new += 1

        # 1回だけ実行するフラグ（シート「設定」に保存）
        await sheets_save_config_once_async("initial_airdrop_done", "1")

        await interaction.followup.send(
            f"初期配布が完了したのだ（新規 {count_new} 人に100コイン）",
            ephemeral=True
        )

    await interaction.followup.send("ショップ入口を設置したのだ", ephemeral=True)

@bot.tree.command(name="setup_bj", description="ブラックジャック入口メッセージを設置するのだ（最初の1回のみ）")
async def setup_bj_cmd(interaction: discord.Interaction):
    if not is_admin_user(interaction):
        return await interaction.response.send_message("権限がないのだ", ephemeral=True)
    if BJ_CHANNEL_ID and interaction.channel_id != BJ_CHANNEL_ID:
        return await interaction.response.send_message("指定のブラックジャックチャンネルで実行するのだ", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    if store.config.get("bj_entry_message_id"):
        return await interaction.followup.send("ブラックジャック入口はもう設置済みなのだ", ephemeral=True)

    content = (
        "🎴 ブラックジャック（ずんだもんカジノ）\n\n"
        "・スタートを押して掛け金を入力するのだ\n"
        "・初期手札ブラックジャックは 3:2（1.5倍利益）なのだ\n"
        "・スプリットは最初の手札が合計20の時だけなのだ\n"
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
                temperature=0.8
            )
        )
        reply = response.choices[0].message.content.strip()

        await interaction.followup.send(
            f"🗣 **あなた**：{message}\n\n"
            f"🟢 **ずんだもん**：{reply}"
        )

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
                    temperature=0.5
                )
            )
            new_summary = s.choices[0].message.content.strip()
            save_summary(user_id, new_summary)
            clear_chats(user_id)

    except Exception as e:
        await interaction.followup.send("ごめんなのだ…今はうまく答えられないのだ 💦")
        print("AI error:", e)

# =========================================================
# /dice（既存：表示形式そのまま＋裏で称号判定）
# =========================================================
@bot.tree.command(name="dice", description="ちんちろを振るのだ")
async def chinchiro_cmd(interaction: discord.Interaction):

    await interaction.response.defer(ephemeral=False)

    # =========================
    # /dice コイン消費（5）
    # =========================
    DICE_COST = 5
    u = store.get_user(interaction.user.id)

    if u["coins"] < DICE_COST:
        return await interaction.followup.send(
            f"コインが足りないのだ（必要：{DICE_COST} / 残高：{u['coins']}）"
        )

    u["coins"] -= DICE_COST
    await sheets_upsert_async(u)

    BASE_JACKPOT_RATE = 1 / 5000
    BOOSTED_JACKPOT_RATE = 1 / 500
    SEVEN_BAR_RATE = 1 / 3000

    def roll_dice(turn, jackpot_boost):
        r = random.random()
        jackpot_rate = BOOSTED_JACKPOT_RATE if jackpot_boost else BASE_JACKPOT_RATE

        if r < jackpot_rate:
            return ["7", "7", "7"]

        if turn < 3 and r < jackpot_rate + SEVEN_BAR_RATE:
            return ["7", "7", "BAR"]

        return sorted([str(random.randint(1, 6)) for _ in range(3)])

    def judge(dice):
        if dice == ["7", "7", "7"]:
            return "🎰 ジャックポット！"
        if dice == ["7", "7", "BAR"]:
            return None

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

    results_text = []
    role = None
    seven_bar_triggered = False
    had_seven_bar = False
    final_dice = None

    for i in range(1, 4):
        dice = roll_dice(i, seven_bar_triggered)
        final_dice = dice
        role = judge(dice)
        dice_text = "・".join(dice)

        if dice == ["7", "7", "BAR"]:
            seven_bar_triggered = True
            had_seven_bar = True

        if role:
            results_text.append(f"{i}回目：🎲 {dice_text} → **{role}**")
            break
        else:
            results_text.append(f"{i}回目：🎲 {dice_text} → 役なし")

    if not role:
        role = "❌ メなし"

    await interaction.followup.send(
        "🎲 **ちんちろ結果なのだ！**\n"
        + "\n".join(results_text)
        + f"\n\n👉 **最終結果：{role}**"
    )

    try:
        u = store.get_user(interaction.user.id)
        just_events = set()

        if final_dice == ["7", "7", "7"]:
            u["jackpot_count"] += 1
            just_events.add("JP_EVENT")

        if had_seven_bar and final_dice != ["7", "7", "7"]:
            just_events.add("BAR_MISS_EVENT")

        await sheets_upsert_async(u)

        # 本人だけに称号演出
        await interaction.followup.send(" ", ephemeral=True)
        await maybe_award_hidden_titles(interaction, u, just_events=just_events)

    except Exception as e:
        print("dice update error:", e)

# =========================================================
# /join（既存：表示形式を変えない）
# =========================================================
@bot.tree.command(name="join", description="参加募集をするのだ")
@app_commands.describe(place="場所", time_str="締切時間（HH:MM）※0で時間なし", count="募集人数")
@app_commands.choices(place=[app_commands.Choice(name=p, value=p) for p in PLACE_LIST])
async def join_cmd(interaction: discord.Interaction, place: app_commands.Choice[str], time_str: str, count: int):
    now = datetime.now(JST)

    if time_str == "0":
        target_time = None
        time_text = "締切なし"
    else:
        try:
            hour, minute = map(int, time_str.split(":"))
        except ValueError:
            return await interaction.response.send_message("時間は HH:MM 形式、または 0 を入力するのだ", ephemeral=True)

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

    join_tasks.clear()
    join_tasks[msg.id] = {
        "place": place.value,
        "time": target_time,
        "count": count,
        "members": set(),
        "channel": interaction.channel.id,
        "message_id": msg.id
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
    if payload.user_id == bot.user.id:
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
    if len(data["members"]) >= data["count"]:
        await channel.send(f"{data['place']} 〆なのだ")
        del join_tasks[payload.message_id]

@bot.tree.command(name="jointime", description="締切なし募集に時間と人数を設定して再募集するのだ")
@app_commands.describe(time_str="締切時間（HH:MM）", count="募集人数")
async def jointime_cmd(interaction: discord.Interaction, time_str: str, count: int):
    try:
        hour, minute = map(int, time_str.split(":"))
    except ValueError:
        return await interaction.response.send_message("時間は HH:MM 形式で入力するのだ", ephemeral=True)

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
        "message_id": msg.id
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
        ephemeral=False
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
    ]

# =========================================================
# /craft（既存：表示形式は変えない）
# =========================================================
@bot.tree.command(name="craft", description="必要素材を計算して表示するのだ")
@app_commands.describe(category="道具 or 武器", type="種別を選択", item="作りたいアイテム", count="作る個数")
@app_commands.choices(category=[app_commands.Choice(name="道具", value="道具"), app_commands.Choice(name="武器", value="武器")])
async def craft_cmd(interaction: discord.Interaction, category: app_commands.Choice[str], type: str, item: str, count: int):
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

    target = next((row for row in sheet if (row.get(name_col) or "").replace("\u3000", "").strip() == (item or "").strip()), None)
    if not target:
        return await interaction.followup.send("そのアイテムはシートにありません")

    make_per_once = float(target.get(make_col, "1") or 1)
    craft_times = math.ceil(count / make_per_once)

    msg = f"### **{item} を {count}個 作るための必要素材**\n"
    msg += f"作成回数：**{craft_times} 回**\n\n"

    for key, value in target.items():
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
            if not type_sel or normalize(row_type) == normalize(type_sel):
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

# =========================================================
# ブラックジャック
# =========================================================
SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

def draw_card(deck: list[tuple[str, str]]) -> tuple[str, str]:
    if not deck:
        return (random.choice(RANKS), random.choice(SUITS))
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

def new_deck() -> list[tuple[str, str]]:
    deck = [(r, s) for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck

bj_sessions = {}

class BetModal(discord.ui.Modal, title="掛け金を入力するのだ"):
    bet = discord.ui.TextInput(label="掛け金（数字）", placeholder="例：100", required=True)

    def __init__(self, balance: int):
        super().__init__()
        self.balance = balance

    async def on_submit(self, interaction: discord.Interaction):
        if not is_in_channel(interaction, BJ_CHANNEL_ID):
            return await interaction.response.send_message("このチャンネルでは使えないのだ", ephemeral=True)

        u = store.get_user(interaction.user.id)
        try:
            bet_val = int(str(self.bet.value).strip())
        except Exception:
            return await interaction.response.send_message(f"現在の残高：{u['coins']} コイン\n数字を入力するのだ", ephemeral=True)

        if bet_val <= 0:
            return await interaction.response.send_message(f"現在の残高：{u['coins']} コイン\n1以上で入力するのだ", ephemeral=True)

        if bet_val > u["coins"]:
            return await interaction.response.send_message(f"現在の残高：{u['coins']} コイン\nコインが足りないのだ", ephemeral=True)

        u["coins"] -= bet_val
        await sheets_upsert_async(u)

        session = {
            "deck": new_deck(),
            "dealer": [],
            "hands": [[]],
            "bets": [bet_val],
            "active": 0,
            "can_split": False,
            "finished_hands": [False],
            "doubled": [False],
            "was_split": False,          # スプリットしたか
            "is_natural_bj": [False],    # 初期手札BJ（手ごと）
        }

        deck = session["deck"]
        session["hands"][0] = [draw_card(deck), draw_card(deck)]
        session["dealer"] = [draw_card(deck), draw_card(deck)]

        # スプリット条件：初期手札合計20のみ
        session["can_split"] = (hand_value(session["hands"][0]) == 20)

        # 初期手札ブラックジャック（2枚で21）
        session["is_natural_bj"][0] = (hand_value(session["hands"][0]) == 21)

        bj_sessions[interaction.user.id] = session

        await interaction.response.send_message("配札したのだ", ephemeral=True)

        # ディーラーが21なら即終了
        if hand_value(session["dealer"]) == 21:
            await bj_finish(interaction, u, immediate_dealer_bj=True)
            return

        # プレイヤーが初期BJなら即スタンド扱い（ディーラーへ）
        if session["is_natural_bj"][0]:
            session["finished_hands"][0] = True
            await bj_dealer_turn(interaction, u)
            return

        await bj_send_state(interaction, u)

class BJActionView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=120)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="ヒット", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        u = store.get_user(interaction.user.id)
        await bj_hit(interaction, u)

    @discord.ui.button(label="スタンド", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        u = store.get_user(interaction.user.id)
        await bj_stand(interaction, u)

    @discord.ui.button(label="ダブルダウン", style=discord.ButtonStyle.danger)
    async def double(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        u = store.get_user(interaction.user.id)
        await bj_double(interaction, u)

    @discord.ui.button(label="スプリット", style=discord.ButtonStyle.success)
    async def split(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        u = store.get_user(interaction.user.id)
        await bj_split(interaction, u)

def bj_state_text(session: dict) -> str:
    dealer = session["dealer"]
    dealer_open = f"{dealer[0][1]}{dealer[0][0]} ??"

    lines = []
    for idx, hand in enumerate(session["hands"]):
        v = hand_value(hand)
        mark = "👉" if idx == session["active"] else "  "
        nat = "（BJ）" if (idx < len(session.get("is_natural_bj", [])) and session["is_natural_bj"][idx]) else ""
        lines.append(f"{mark}手札{idx+1}：{fmt_cards(hand)}（{v}）{nat}  賭け：{session['bets'][idx]}")

    return (
        f"🎴 ブラックジャックなのだ\n\n"
        f"ディーラー：{dealer_open}\n\n"
        + "\n".join(lines)
    )

async def bj_send_state(interaction: discord.Interaction, u: dict):
    session = bj_sessions.get(interaction.user.id)
    if not session:
        return await interaction.followup.send("セッションがないのだ", ephemeral=True)

    view = BJActionView(interaction.user.id)
    active = session["active"]
    hand = session["hands"][active]

    can_split = session.get("can_split", False) and len(session["hands"]) == 1 and len(hand) == 2 and hand_value(hand) == 20

    # ★要望：スプリット後はダブルダウン不可
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

    i = session["active"]
    session["finished_hands"][i] = True
    await interaction.followup.send(f"スタンドしたのだ\n{bj_state_text(session)}", ephemeral=True)
    await bj_next_or_dealer(interaction, u)

async def bj_double(interaction: discord.Interaction, u: dict):
    session = bj_sessions.get(interaction.user.id)
    if not session:
        return await interaction.followup.send("セッションがないのだ", ephemeral=True)

    # ★要望：スプリット後はダブルダウン不可
    if len(session["hands"]) != 1:
        return await interaction.followup.send("スプリット後はダブルダウンできないのだ", ephemeral=True)

    i = session["active"]
    hand = session["hands"][i]
    if len(hand) != 2 or session["doubled"][i]:
        return await interaction.followup.send("今はダブルダウンできないのだ", ephemeral=True)

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

    if len(session["hands"]) != 1:
        return await interaction.followup.send("もうスプリット済みなのだ", ephemeral=True)

    hand = session["hands"][0]
    if len(hand) != 2 or hand_value(hand) != 20:
        return await interaction.followup.send("スプリット条件を満たしていないのだ", ephemeral=True)

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
    session["is_natural_bj"] = [False, False]  # スプリット後は初期BJ扱いしない

    session["hands"][0].append(draw_card(session["deck"]))
    session["hands"][1].append(draw_card(session["deck"]))

    await interaction.followup.send(f"スプリットしたのだ\n{bj_state_text(session)}", ephemeral=True)

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

    dealer = session["dealer"]
    await interaction.followup.send(
        f"ディーラーのターンなのだ\nディーラー：{fmt_cards(dealer)}（{hand_value(dealer)}）",
        ephemeral=True
    )

    while hand_value(dealer) < 17:
        await asyncio.sleep(0.6)
        dealer.append(draw_card(session["deck"]))
        await interaction.followup.send(
            f"ディーラーがヒットしたのだ\nディーラー：{fmt_cards(dealer)}（{hand_value(dealer)}）",
            ephemeral=True
        )

    await bj_finish(interaction, u, immediate_dealer_bj=False)

async def bj_finish(interaction: discord.Interaction, u: dict, immediate_dealer_bj: bool):
    session = bj_sessions.get(interaction.user.id)
    if not session:
        return await interaction.followup.send("セッションがないのだ", ephemeral=True)

    dealer_val = hand_value(session["dealer"])
    dealer_bust = dealer_val > 21

    payout_total = 0
    profit = 0
    results = []

    for idx, hand in enumerate(session["hands"]):
        bet = session["bets"][idx]
        v = hand_value(hand)

        # プレイヤーバースト
        if v > 21:
            results.append(f"手札{idx+1}：負け（バースト）")
            profit -= bet
            continue

        # ディーラー即BJ
        if immediate_dealer_bj:
            results.append(f"手札{idx+1}：負け（ディーラー21）")
            profit -= bet
            continue

        # ディーラーバースト
        if dealer_bust:
            # 初期手札BJ(2枚で21)のみ3:2（利益1.5倍）
            if idx < len(session.get("is_natural_bj", [])) and session["is_natural_bj"][idx]:
                payout = (bet * 5) // 2  # bet*2.5（整数化：切り捨て）
                payout_total += payout
                results.append(f"手札{idx+1}：勝ち（BJ 3:2）")
                profit += (payout - bet)
            else:
                payout_total += bet * 2
                results.append(f"手札{idx+1}：勝ち（ディーラーバースト）")
                profit += bet
            continue

        # 比較
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

    u["coins"] += payout_total
    u["bj_play_count"] += 1

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

    view = BJEndView(interaction.user.id)
    await interaction.followup.send("次はどうするのだ？", view=view, ephemeral=True)

    bj_sessions.pop(interaction.user.id, None)

class BJEndView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=120)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="🎴 もう一回スタート", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        u = store.get_user(interaction.user.id)
        await interaction.response.send_modal(BetModal(balance=u["coins"]))

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def quit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("終了したのだ", ephemeral=True)

# =========================================================
# 起動イベント
# =========================================================
@bot.event
async def on_ready():
    try:
        await sheets_init_async()
        print("Sheets connected.")
    except Exception as e:
        print("Sheets init error:", e)

    await bot.tree.sync()

    if not check_tasks.is_running():
        check_tasks.start()
    if not check_join_tasks.is_running():
        check_join_tasks.start()

    print(f"Bot logged in as {bot.user}")

# =========================================================
# Flask Keep Alive
# =========================================================
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!", 200

def run():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

def keep_alive():
    t = Thread(target=run)
    t.start()

# =========================================================
# Bot 起動
# =========================================================
async def start():
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        print("DISCORD_TOKEN not set!")
        return

    while True:
        try:
            await bot.start(TOKEN)
        except Exception as e:
            print("Error:", e)
            await asyncio.sleep(5)

if __name__ == "__main__":
    init_ai_memory_db()
    keep_alive()
    asyncio.run(start())
