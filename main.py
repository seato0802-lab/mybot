import os
from pathlib import Path
from dotenv import load_dotenv

# .env を確実に読む
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

from openai import OpenAI

# OpenAIクライアント（★ここが重要）
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

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


def get_ai_summary_from_sheet(user_id: int) -> str:
    u = store.get_user(user_id)
    return (u.get("ai_summary") or "").strip()

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

# -----------------------------
# ダンジョン設定（チャンネル制限なし）
# -----------------------------
DUNGEON_SHEET_NAME = os.getenv("GS_DUNGEON_SHEET_NAME", "dungeon")
DUNGEON_CHANNEL_ID = None  # ✅ チャンネル設定なし（どこでもOK）
DUNGEON_MESSAGE_ID_KEY = "dungeon_entry_message_id"
DUNGEON_CHANNEL_ID_KEY = "dungeon_entry_channel_id"

# ワールド上限
WORLD_CAP = {1: 50, 2: 100, 3: 150, 4: 200}
T_CENTER_BY_SEG = [0.55, 0.65, 0.75, 0.85, 0.93]  # 20区切り

# 特殊効果（確定仕様）
EFFECT_TYPES = ["NONE", "HP_UP", "HEAL_ON_KILL", "INSTAKILL", "SHIELD", "DEBUFF_RESIST"]

# ✅ すべて 5段階
HP_UP_TABLE         = [20, 40, 60, 80, 100]   # 基礎HP100固定 → 最大200
HEAL_ON_KILL_TABLE  = [10, 20, 30, 40, 50]    # %
INSTAKILL_TABLE     = [1, 7, 13, 19, 25]      # %
SHIELD_TABLE        = [10, 20, 30, 40, 50]    # ✅ 最大50（毎バトル全回復、shield_nowは保存しない）
DEBUFF_RESIST_TABLE = [20, 40, 60, 80, 100]   # %

DUNGEON_HEADERS = [
    "user_id",
    "world",
    "floor",
    "hp",
    "weapon_name",
    "weapon_atk",
    "weapon_def",
    "weapon_spd",
    "effect_type",
    "effect_lv",
    "effect_value",
    "debuff_zone",         # 0/1（必要なら運用）
    "current_debuffs",     # 文字列（必要なら運用）
]


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _rand_int(a: float, b: float) -> int:
    lo = int(min(a, b))
    hi = int(max(a, b))
    if hi < lo:
        hi = lo
    return random.randint(lo, hi)


def _fmt_effect(effect_type: str, effect_value: int) -> str:
    if effect_type == "NONE":
        return "なし"
    if effect_type == "HP_UP":
        return f"最大HP +{effect_value}"
    if effect_type == "HEAL_ON_KILL":
        return f"撃破時回復 {effect_value}%"
    if effect_type == "INSTAKILL":
        return f"即死 {effect_value}%"
    if effect_type == "SHIELD":
        return f"シールド {effect_value}（毎バトル全回復）"
    if effect_type == "DEBUFF_RESIST":
        return f"状態異常耐性 {effect_value}%"
    return "なし"


def _effect_lv_unlock(world: int) -> int:
    # ✅ W1=Lv1のみ / W2=Lv1-2 / W3=Lv1-3 / W4=Lv1-4 / W5+=Lv1-5
    if world >= 5:
        return 5
    return max(1, min(5, int(world)))


def _pick_effect(world: int) -> tuple[str, int, int]:
    # ✅ 6択同率（A）・11連保証なし
    etype = random.choice(EFFECT_TYPES)
    if etype == "NONE":
        return "NONE", 0, 0

    max_lv = _effect_lv_unlock(world)
    lv = random.randint(1, max_lv)

    if etype == "HP_UP":
        return etype, lv, HP_UP_TABLE[lv - 1]
    if etype == "HEAL_ON_KILL":
        return etype, lv, HEAL_ON_KILL_TABLE[lv - 1]
    if etype == "INSTAKILL":
        return etype, lv, INSTAKILL_TABLE[lv - 1]
    if etype == "SHIELD":
        return etype, lv, SHIELD_TABLE[lv - 1]
    if etype == "DEBUFF_RESIST":
        return etype, lv, DEBUFF_RESIST_TABLE[lv - 1]

    return "NONE", 0, 0


def _weapon_t(world: int, floor: int) -> float:
    # 敵生成と同じ思想 + 20内微上振れ
    f = max(1, min(100, int(floor)))
    seg = (f - 1) // 20
    in_seg = (f - 1) % 20

    t_center = T_CENTER_BY_SEG[seg]
    micro = (in_seg / 19.0) * 0.03  # 20区切り内で少し上振れ
    t = _clamp(random.uniform(t_center - 0.06, t_center + 0.06) + micro, 0.40, 0.98)
    return t


def roll_weapon(world: int, floor: int) -> dict:
    cap = WORLD_CAP.get(int(world), 50)
    t = _weapon_t(world, floor)

    atk = _rand_int(cap * (t - 0.08), cap * (t + 0.08))
    df  = _rand_int(cap * (t - 0.08), cap * (t + 0.08))
    spd = _rand_int(cap * (t - 0.08), cap * (t + 0.08))

    etype, elv, eval_ = _pick_effect(world)

    weapon_id = f"W{int(world)}_{int(time.time()*1000)}_{random.randint(100,999)}"
    name = _pretty_weapon_name(world)

    return {
        "id": weapon_id,          # ✅内部用（保存しなくてもOK）
        "name": name,
        "atk": max(0, atk),
        "def": max(0, df),
        "spd": max(1, spd),
        "effect_type": etype,
        "effect_lv": elv,
        "effect_value": eval_,
    }


def boss_flags(floor: int) -> tuple[bool, bool]:
    # 5=中ボス, 10=ボス, 15=中ボス...
    is_boss = (floor % 10 == 0)
    is_midboss = (floor % 10 == 5)
    return is_boss, is_midboss


# ✅ ボスも段階を踏む：10区切りの段階
def boss_stage(floor: int) -> int:
    # 1-10:0 / 11-20:1 / 21-30:2 ...
    return (max(1, int(floor)) - 1) // 10


# ✅ ボス倍率（序盤は弱く、後半で強く）
BOSS_MULT = [
    {"hp": 1.15, "atk": 1.05, "def": 1.03, "spd": 1.05},  # 1-10
    {"hp": 1.25, "atk": 1.07, "def": 1.05, "spd": 1.07},  # 11-20
    {"hp": 1.40, "atk": 1.10, "def": 1.07, "spd": 1.10},  # 21-30
    {"hp": 1.55, "atk": 1.12, "def": 1.09, "spd": 1.12},  # 31-40
    {"hp": 1.70, "atk": 1.15, "def": 1.12, "spd": 1.15},  # 41+
]


def generate_enemy(world: int, floor: int, debuff_zone: bool = False) -> dict:
    w = int(world)
    f = max(1, min(100, int(floor)))
    seg = (f - 1) // 20
    is_boss, is_midboss = boss_flags(f)

    ceil = weapon_ceiling(w, f)
    wmax_atk = int(ceil["atk"])
    wmax_def = int(ceil["def"])
    wmax_spd = int(ceil["spd"])
    cap = int(ceil["cap"])

    # -------------------------
    # 強さレンジ
    # -------------------------
    if is_midboss:
        r_lo, r_hi = 0.86, 0.95
        hp_mul_lo, hp_mul_hi = 0.95, 1.10
    elif is_boss:
        r_lo, r_hi = 0.92, 1.02
        hp_mul_lo, hp_mul_hi = 1.05, 1.25
    else:
        r_lo, r_hi = 0.78, 0.88
        hp_mul_lo, hp_mul_hi = 0.80, 0.98

    r = random.uniform(r_lo, r_hi)

    enemy_atk = max(1, int(wmax_atk * r))
    enemy_def = max(0, int(wmax_def * (r - 0.08)))
    enemy_spd = max(1, int(wmax_spd * (r - 0.02)))

    base_hp = int(cap * random.uniform(1.6, 2.2) * (0.92 + 0.03 * seg))
    max_hp = int(base_hp * random.uniform(hp_mul_lo, hp_mul_hi))
    max_hp = max(10, max_hp)

    # ✅ W1序盤補正
    if w == 1 and seg == 0:
        enemy_atk = max(1, int(enemy_atk * 0.85))
        enemy_def = max(0, int(enemy_def * 0.80))
        enemy_spd = max(1, int(enemy_spd * 0.90))
        max_hp = max(10, int(max_hp * (0.85 if not is_boss else 0.92)))

    if w == 1 and seg == 1:
        enemy_atk = max(1, int(enemy_atk * 0.90))
        enemy_def = max(0, int(enemy_def * 0.92))
        enemy_spd = max(1, int(enemy_spd * 0.95))
        max_hp = max(10, int(max_hp * 0.92))

    # -------------------------
    # ✅ ここから「名前＆画像」差し込み
    # -------------------------
    kind = "boss" if is_boss else "midboss" if is_midboss else "mob"
    base_name, image_url = _pick_enemy_visual(w, kind)

    prefix = "ボス" if is_boss else "中ボス" if is_midboss else "敵"
    display_name = f"{prefix}：{base_name}"

    return {
        # 表示用
        "name": display_name,          # 例: "中ボス：怒ったずんだもん"
        "base_name": base_name,        # 例: "怒ったずんだもん"
        "image_url": image_url,        # サムネ用URL（無ければNone）

        # ステータス
        "hp": max_hp,
        "max_hp": max_hp,
        "atk": enemy_atk,
        "def": enemy_def,
        "spd": enemy_spd,

        # フラグ
        "is_boss": is_boss,
        "is_midboss": is_midboss,
        "debuff_zone": bool(debuff_zone),
    }

def _combat_damage(attacker_atk: int, defender_def: int) -> int:
    atk = max(1, int(attacker_atk))
    df = max(0, int(defender_def))
    base = atk - df
    floor_dmg = max(1, int(atk * 0.10))  # ✅ 最低でも攻撃の10%は通る
    return max(floor_dmg, base)


def calc_player_max_hp(effect_type: str, effect_value: int) -> int:
    hp_up = int(effect_value) if effect_type == "HP_UP" else 0
    return 100 + hp_up

def _apply_heal_on_kill(sess: dict):
    """特殊効果：撃破時回復（HEAL_ON_KILL）"""
    if sess.get("effect_type") != "HEAL_ON_KILL":
        return
    pct = int(sess.get("effect_value", 0) or 0)
    if pct <= 0:
        return

    max_hp = int(sess.get("max_hp", 100) or 100)
    now_hp = int(sess.get("player_hp", 0) or 0)
    heal = int(max_hp * (pct / 100.0))
    if heal <= 0:
        return

    new_hp = min(max_hp, now_hp + heal)
    real = new_hp - now_hp
    if real > 0:
        sess["player_hp"] = new_hp
        _push_log(sess, f"✨ 特殊効果：撃破時回復でHPが {real} 回復したのだ。")


def _roll_instakill(sess: dict) -> bool:
    """特殊効果：即死（INSTAKILL）判定。成功なら True。"""
    if sess.get("effect_type") != "INSTAKILL":
        return False
    p = int(sess.get("effect_value", 0) or 0)
    if p <= 0:
        return False
    # 1〜100 で判定
    return random.randint(1, 100) <= p


def _debuff_resist_pct(sess: dict) -> int:
    """特殊効果：状態異常耐性%（DEBUFF_RESIST）"""
    if sess.get("effect_type") != "DEBUFF_RESIST":
        return 0
    return max(0, min(100, int(sess.get("effect_value", 0) or 0)))


def _try_apply_debuff(sess: dict, debuff: str, base_chance_pct: int) -> bool:
    """
    デバフ付与（デバフゾーン等で使用）
    - base_chance_pct: 0〜100
    - DEBUFF_RESIST がある場合、付与確率を (1 - resist) 倍する
    """
    base = max(0, min(100, int(base_chance_pct)))
    resist = _debuff_resist_pct(sess)
    # 実効確率
    eff = int(base * (1.0 - resist / 100.0))

    if eff <= 0:
        _push_log(sess, "🛡 状態異常耐性でデバフを無効化したのだ。")
        return False

    if random.randint(1, 100) <= eff:
        cur = sess.setdefault("current_debuffs", [])
        if debuff not in cur:
            cur.append(debuff)
        _push_log(sess, f"⚠️ デバフ付与：{debuff}（{eff}%判定）")
        return True

    # 失敗（ログ出すと騒がしいので通常は出さない）
    return False


def _apply_debuffs_start_of_turn(sess: dict) -> tuple[int, int, int]:
    """
    ターン開始時デバフ効果を反映して「一時的な atk/def/spd」を返す。
    例：weak=攻撃低下、slow=速度低下、poison=割合ダメ
    """
    atk = int(sess.get("atk", 0) or 0)
    df  = int(sess.get("def", 0) or 0)
    spd = int(sess.get("spd", 0) or 0)

    debuffs = sess.get("current_debuffs") or []

    # 攻撃低下
    if "weak" in debuffs:
        atk = max(0, int(atk * 0.85))

    # 速度低下
    if "slow" in debuffs:
        spd = max(1, int(spd * 0.85))

    # 毒（最大HPの5%）
    if "poison" in debuffs:
        max_hp = int(sess.get("max_hp", 100) or 100)
        now_hp = int(sess.get("player_hp", 0) or 0)
        dmg = max(1, int(max_hp * 0.05))
        sess["player_hp"] = max(0, now_hp - dmg)
        _push_log(sess, f"☠️ 毒で {dmg} ダメージを受けたのだ。")

    return atk, df, spd


def _reset_shield_for_battle(sess: dict):
    """SHIELDは毎バトル全回復：battle開始・次フロア開始で必ずここを通す"""
    # sess に shield_max が無い場合は effect から復元できる
    shield_max = int(sess.get("shield_max", 0) or 0)
    if shield_max <= 0:
        shield_max = get_player_shield_max(sess.get("effect_type", "NONE"), int(sess.get("effect_value", 0) or 0))
        sess["shield_max"] = int(shield_max)

    sess["shield_now"] = int(shield_max)

def get_player_shield_max(effect_type: str, effect_value: int) -> int:
    return int(effect_value) if effect_type == "SHIELD" else 0

def weapon_ceiling(world: int, floor: int) -> dict:
    """
    そのworld/floorで出うる武器ステータスの「理論最大値（天井）」を返す。
    roll_weapon() が使う t の上振れ側 + (t+0.08) を使う。
    """
    w = int(world)
    f = max(1, min(100, int(floor)))
    cap = WORLD_CAP.get(w, 50)

    seg = (f - 1) // 20
    in_seg = (f - 1) % 20
    t_center = T_CENTER_BY_SEG[seg]
    micro = (in_seg / 19.0) * 0.03

    # roll_weapon は uniform(t_center±0.06)+micro を clamp してるので上側を取る
    t_max = _clamp((t_center + 0.06) + micro, 0.40, 0.98)

    # roll_weapon は cap*(t±0.08) の範囲なので上側を取る
    stat_max = int(cap * (t_max + 0.08))

    # DEF/SPD/ATK 全部同じレンジで出る仕様なので同値でOK
    return {"atk": stat_max, "def": stat_max, "spd": stat_max, "cap": cap, "t_max": t_max}

def get_checkpoint_floor(floor: int) -> int:
    """
    1-20  -> 1
    21-40 -> 21
    41-60 -> 41
    ...
    """
    f = max(1, int(floor))
    return ((f - 1) // 20) * 20 + 1

WEAPON_PREFIX_BY_WORLD = {
    1: ["木製", "初心者", "訓練用", "旅人の", "見習いの"],
    2: ["鉄の", "堅牢な", "戦士の", "鋼の", "獣狩りの"],
    3: ["紅蓮の", "黒鉄の", "雷鳴の", "影の", "蒼天の"],
    4: ["神威の", "覇王の", "星喰いの", "冥府の", "天翔ける"],
    5: ["混沌の", "終焉の", "無限の", "原初の", "禁忌の"],
}

WEAPON_BASE_NAMES = [
    "剣", "大剣", "短剣", "槍", "斧", "弓", "杖", "槌", "鎌", "双剣"
]

WEAPON_SUFFIX = [
    "・改", "・真打", "・零式", "・極", "・試作", "・二号", "・特注"
]

def _pretty_weapon_name(world: int) -> str:
    w = int(world)
    prefix = random.choice(WEAPON_PREFIX_BY_WORLD.get(w, ["謎の"]))
    base = random.choice(WEAPON_BASE_NAMES)

    # 40%でサフィックスを付ける
    name = f"{prefix}{base}"
    if random.random() < 0.40:
        name += random.choice(WEAPON_SUFFIX)
    return name

# -----------------------------
# ダンジョン報酬（コイン）
# -----------------------------
DUNGEON_COIN_REWARD = {
    "mob":      (3, 8),      # 雑魚：数コイン
    "midboss":  (25, 60),    # 中ボス：数十コイン
    "boss":     (150, 350),  # ボス：数百コイン
}

def _calc_dungeon_coin_reward(enemy: dict) -> int:
    if enemy.get("is_boss"):
        lo, hi = DUNGEON_COIN_REWARD["boss"]
    elif enemy.get("is_midboss"):
        lo, hi = DUNGEON_COIN_REWARD["midboss"]
    else:
        lo, hi = DUNGEON_COIN_REWARD["mob"]

    return random.randint(lo, hi)

# -----------------------------
# 敵プール（W1〜W4は個別、W5は全混ぜ）
# kind: "mob" / "midboss" / "boss"
# url: GoogleDriveの直リンク推奨（ https://drive.google.com/uc?id=FILE_ID ）
# -----------------------------
ENEMY_POOLS: dict[int, dict[str, list[dict[str, str]]]] = {
    1: {
        "mob": [
            {"name": "ずんだもん", "url": "https://drive.google.com/uc?id=1U7bZD4pBtj0peuLWwz_zCVitMFWGc7mw"},
        ],
        "midboss": [
            {"name": "怒ったずんだもん", "url": "https://drive.google.com/uc?id=1YtKq-eQV0IvhPJ8MxEkO3jS2OPdc0nu5"},
        ],
        "boss": [
            {"name": "強いずんだもん", "url": "https://drive.google.com/uc?id=1Tnpc5uEXcz6-FIbr8HzLMDK87AOTTZEt"},
        ],
    },
    2: {
        "mob": [
            {"name": "すらいむっぽい", "url": "https://drive.google.com/uc?id=1zKScQi03ZnEhoJZHqjH5bDt2Yn4qr-Q9"},
        ],
        "midboss": [
            {"name": "めたるっぽい", "url": "https://drive.google.com/uc?id=1mn9cBans3KMQ2lLb40p4ib-aRnMY1YzO"},
        ],
        "boss": [
            {"name": "きんぐっぽい", "url": "https://drive.google.com/uc?id=1HPR7Lra32Z9xhop7OA3hR0MhHjBFmEIN"},
        ],
    },
    3: {
        "mob": [
            {"name": "サンドンズ", "url": "https://drive.google.com/uc?id=1uzwhFGOKjcm5Twx_by-R6iRCpA3vr4n6"},
        ],
        "midboss": [
            {"name": "スタッカート吉中", "url": "https://drive.google.com/uc?id=1RPSwhfSy6MbYDHOeKU8YBb7e53mknIv4"},
        ],
        "boss": [
            {"name": "アンダッシュ高田", "url": "https://drive.google.com/uc?id=1v-ugvqIdIgMHX0pzaXN0mHKB5cAD1_Ix"},
        ],
    },
    4: {
        "mob": [
            {"name": "フランクリン・クリントン", "url": "https://drive.google.com/uc?id=13bcdB3_luils1oXzQUrEupO0w2baz4v_"},
        ],
        "midboss": [
            {"name": "マイケル・デ・サンタ", "url": "https://drive.google.com/uc?id=1l_rbjyYMdKdWcbxnOslCMjKUTzd6ZIz7"},
        ],
        "boss": [
            {"name": "シ・ミズー", "url": "https://drive.google.com/uc?id=117v4LldiWXsr_NURJT09kj7L3yUIgoKy"},
        ],
    },
}

def _pick_enemy_visual(world: int, kind: str) -> tuple[str, str | None]:
    """
    W1〜W4: そのワールドのプールから抽選
    W5: W1〜W4の同kindを全混ぜして抽選
    """
    w = int(world)

    if w == 5:
        mixed: list[dict[str, str]] = []
        for ww in (1, 2, 3, 4):
            mixed.extend(ENEMY_POOLS.get(ww, {}).get(kind, []) or [])
        arr = mixed
    else:
        arr = ENEMY_POOLS.get(w, {}).get(kind, []) or []

    if not arr:
        return "敵", None

    pick = random.choice(arr)
    return pick.get("name", "敵"), pick.get("url")

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

ROLE_AI_FRIEND = 1463389642392080540  # 友達
ROLE_AI_BFF   = 1463389735748632791  # 親友
ROLE_AI_FAMILY = 1463389801171521546  # 家族

# ダンジョン称号（直書きID）
ROLE_DUNGEON_CLEAR = 1465252347742650479  # 🏆 ダンジョン踏破者（W4-100）
ROLE_DUNGEON_END   = 1465252405791821895  # 🌑 終焉到達者（W5-100）

AWARD_NUMA_CLEAR = "AWARD_NUMA_CLEAR"
AWARD_NUMA_LEGEND = "AWARD_NUMA_LEGEND"

AWARD_AI_FRIEND = "ai_zunda_friend"
AWARD_AI_BFF    = "ai_zunda_bff"
AWARD_AI_FAMILY = "ai_zunda_family"

AWARD_DUNGEON_CLEAR = "AWARD_DUNGEON_CLEAR"  # W4-100
AWARD_DUNGEON_END   = "AWARD_DUNGEON_END"    # W5-100

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
    
    ROLE_AI_FRIEND,
    ROLE_AI_BFF,
    ROLE_AI_FAMILY,
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
    "ai_chat_count",
    "ai_summary",
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
        if not GS_SPREADSHEET_ID:
            raise RuntimeError("GS_SPREADSHEET_ID が未設定です")

        if not os.path.exists("gs_service_account.json"):
            raise RuntimeError("gs_service_account.json が見つかりません（~/bot に置いてください）")

        creds = Credentials.from_service_account_file(
            "gs_service_account.json",
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
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
            "ai_chat_count": i("ai_chat_count", 0),
            "ai_summary": s("ai_summary", ""),
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

# -----------------------------
# DungeonStore（Sheets）
# -----------------------------
class DungeonStore:
    def __init__(self, sheets_store: SheetsStore):
        self.s = sheets_store
        self.ws = None
        self._lock = Lock()
        self._uid_to_row: dict[int, int] = {}

    def init(self):
        if not self.s.sh:
            raise RuntimeError("SheetsStore が init されていないのだ")
        with self._lock:
            try:
                self.ws = self.s.sh.worksheet(DUNGEON_SHEET_NAME)
            except Exception:
                self.ws = self.s.sh.add_worksheet(title=DUNGEON_SHEET_NAME, rows=3000, cols=30)

            header = self.ws.row_values(1)
            if not header:
                self.ws.update("A1", [DUNGEON_HEADERS])
                header = DUNGEON_HEADERS
            else:
                merged = list(header)
                for h in DUNGEON_HEADERS:
                    if h not in merged:
                        merged.append(h)
                if merged != header:
                    self.ws.update("A1", [merged])
                header = merged

            self._reindex()

    def _reindex(self):
        values = self.ws.get_all_values()
        header = values[0] if values else []
        if not header:
            return
        uid_col = header.index("user_id") if "user_id" in header else 0
        uid_to_row = {}
        for row_idx, row in enumerate(values[1:], start=2):
            if not row:
                continue
            uid = self.s._to_int_maybe(row[uid_col] if uid_col < len(row) else "")
            if uid > 0 and uid not in uid_to_row:
                uid_to_row[uid] = row_idx
        self._uid_to_row = uid_to_row

    def _ensure_user_row(self, uid: int) -> dict:
        # 初期状態：W1-F1、HP100、初期武器はガチャで決める運用でもOK
        # ここでは「最低限動く」ために初期武器を固定で持たせる（後で変更可能）
        return {
            "user_id": uid,
            "world": 1,
            "floor": 1,
            "hp": 100,
            "weapon_name": "初期武器",
            "weapon_atk": 10,
            "weapon_def": 10,
            "weapon_spd": 10,
            "effect_type": "NONE",
            "effect_lv": 0,
            "effect_value": 0,
            "debuff_zone": 0,
            "current_debuffs": "",
        }

    def load_user(self, uid: int) -> dict:
        with self._lock:
            header = self.ws.row_values(1)
            if not header:
                self.ws.update("A1", [DUNGEON_HEADERS])
                header = DUNGEON_HEADERS

            row_idx = self._uid_to_row.get(uid)
            if row_idx is None:
                u = self._ensure_user_row(uid)
                values = [u.get(h, "") for h in header]
                # user_idは文字列でE+対策
                if "user_id" in header:
                    values[header.index("user_id")] = str(uid)
                self.ws.append_row(values)
                self._reindex()
                return u

            row = self.ws.row_values(row_idx)
            r = {h: (row[i] if i < len(row) else "") for i, h in enumerate(header)}

            def gi(k, default=0):
                try:
                    return int(self.s._to_int_maybe(r.get(k)) or default)
                except Exception:
                    return default

            def gs(k, default=""):
                v = r.get(k)
                return str(v).strip() if v is not None else default

            return {
                "user_id": uid,
                "world": gi("world", 1),
                "floor": gi("floor", 1),
                "hp": gi("hp", 100),
                "weapon_name": gs("weapon_name", "初期武器"),
                "weapon_atk": gi("weapon_atk", 10),
                "weapon_def": gi("weapon_def", 10),
                "weapon_spd": gi("weapon_spd", 10),
                "effect_type": gs("effect_type", "NONE").upper() or "NONE",
                "effect_lv": gi("effect_lv", 0),
                "effect_value": gi("effect_value", 0),
                "debuff_zone": gi("debuff_zone", 0),
                "current_debuffs": gs("current_debuffs", ""),
            }

    def save_user_fields(self, uid: int, patch: dict):
        with self._lock:
            header = self.ws.row_values(1)
            if not header:
                self.ws.update("A1", [DUNGEON_HEADERS])
                header = DUNGEON_HEADERS

            row_idx = self._uid_to_row.get(uid)
            if row_idx is None:
                # なければ作ってから更新
                _ = self.load_user(uid)
                row_idx = self._uid_to_row.get(uid)

            if row_idx is None:
                return

            # 1行分の値を取得→上書き→まとめてupdate
            cur = self.ws.row_values(row_idx)
            values = [cur[i] if i < len(cur) else "" for i in range(len(header))]
            for k, v in patch.items():
                if k not in header:
                    continue
                col = header.index(k)
                if k == "user_id":
                    values[col] = str(uid)
                else:
                    values[col] = str(v)

            start_a1 = rowcol_to_a1(row_idx, 1)
            end_a1 = rowcol_to_a1(row_idx, len(header))
            self.ws.update(range_name=f"{start_a1}:{end_a1}", values=[values])

    def save_after_battle(self, uid: int, *, world: int, floor: int, hp: int):
        self.save_user_fields(uid, {"world": int(world), "floor": int(floor), "hp": int(hp)})

    def save_weapon(self, uid: int, w: dict):
        patch = {
            "weapon_name": w["name"],
            "weapon_atk": int(w["atk"]),
            "weapon_def": int(w["def"]),
            "weapon_spd": int(w["spd"]),
            "effect_type": w["effect_type"],
            "effect_lv": int(w.get("effect_lv", 0) or 0),
            "effect_value": int(w.get("effect_value", 0) or 0),
        }
        self.save_user_fields(uid, patch)


dungeon_store = DungeonStore(store)

async def dungeon_init_async():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, dungeon_store.init)

async def dungeon_load_user_async(uid: int) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: dungeon_store.load_user(uid))

async def dungeon_save_after_battle_async(uid: int, world: int, floor: int, hp: int):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: dungeon_store.save_after_battle(uid, world=world, floor=floor, hp=hp))

async def dungeon_save_weapon_async(uid: int, w: dict):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: dungeon_store.save_weapon(uid, w))
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

async def notify_title_earned_only_user(
    interaction: discord.Interaction,
    member: discord.Member | discord.User,
    message: str,
):
    """
    称号獲得の通知：DMなし、チャンネルにも流さず、本人にだけephemeralで通知する。
    interaction が失効している可能性があるので、response/followup両対応。
    """
    # 念のため「本人以外」には送らない（interaction.userと違うmemberが来た場合の安全策）
    if interaction.user.id != member.id:
        return

    text = f"🏅 称号を獲得したのだ！\n{message}"

    try:
        # まだ応答してなければ response で返す
        if not interaction.response.is_done():
            await interaction.response.send_message(text, ephemeral=True)
        else:
            await interaction.followup.send(text, ephemeral=True)
    except discord.NotFound:
        # Unknown interaction 等（10062）で失効していたら諦める
        return
    except discord.errors.InteractionResponded:
        # 既にresponse済みならfollowupへ
        try:
            await interaction.followup.send(text, ephemeral=True)
        except Exception:
            pass
    except Exception:
        # 通知は落ちても本処理を止めない
        return

async def maybe_award_hidden_titles(
    interaction: discord.Interaction,
    u: dict,
    just_events: set[str],
):
    member = interaction.user

    # guild外（DM等）は称号処理しない
    if not isinstance(member, discord.Member):
        return

    async def award_once(key: str, role_id: int | None, message: str):
        if role_id is None:
            return

        role = member.guild.get_role(role_id)
        if role is None:
            return

        earned = (key in award_keys_set(u))
        has_role = any(r.id == role_id for r in member.roles)

        # ✅ 既に獲得済みなら「通知しない」
        # ただしロールが外れてたら付け直す（通知なし）
        if earned:
            if not has_role:
                try:
                    await apply_title_role(member, role_id)
                except Exception:
                    pass
            return

        # ✅ 未獲得の時だけ：記録→付与→通知
        add_title_to_inventory(u, role_id)
        u["title_role_id"] = role_id
        set_award_key(u, key)

        try:
            await apply_title_role(member, role_id)
        except Exception:
            # 付与失敗なら通知しない（荒れ防止）
            return

        await sheets_upsert_async(u)
        await notify_title_earned_only_user(interaction, member, message)
        
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
    # 沼（NUMA）
    # =========================================================
    if "NUMA_CLEAR" in just_events:
        await award_once(
            AWARD_NUMA_CLEAR,
            ROLE_NUMA_CLEAR,
            "🎉🎉🎉\n✨【沼踏破】✨\n\n沼を最後まで突破したのだ！\n🕳️「沼踏破者」\nを獲得したのだよ！\n🎉🎉🎉",
        )

    if "NUMA_LEGEND" in just_events:
        await award_once(
            AWARD_NUMA_LEGEND,
            ROLE_NUMA_LEGEND,
            "🎉🎉🎉\n✨【沼の伝説】✨\n\n通過玉が1発で沼を制覇したのだ…！\n👑「沼を支配せし者」\nを獲得したのだよ！\n🎉🎉🎉",
        )

# =========================================================
# AI称号（ずんだもん）付与
# 条件: ai_chat_count の回数で付与（好きに変更OK）
# =========================================================
    ai_cnt = int(u.get("ai_chat_count", 0) or 0)

    if ai_cnt >= 10:
        await award_once(
            AWARD_AI_FRIEND,
            ROLE_AI_FRIEND,
            "🎉✨称号獲得✨\n\n🌱 ずんだもんの友達なのだ！\nを獲得したのだ！",
        )

    if ai_cnt >= 50:
        await award_once(
            AWARD_AI_BFF,
            ROLE_AI_BFF,
            "🎉✨称号獲得✨\n\n🫛 ずんだもんの親友なのだ！\nを獲得したのだ！",
        )

    if ai_cnt >= 100:
        await award_once(
            AWARD_AI_FAMILY,
            ROLE_AI_FAMILY,
            "🎉✨称号獲得✨\n\n💚 ずんだもんの家族なのだ！\nを獲得したのだ！",
        )

# =========================================================
# ダンジョン
# =========================================================
    if "DUNGEON_CLEAR" in just_events:
        await award_once(
            AWARD_DUNGEON_CLEAR,
            ROLE_DUNGEON_CLEAR,
            "🎉🎉🎉\n✨【ダンジョン踏破】✨\n\nワールド4のフロア100を突破したのだ！\n🏆「ダンジョン踏破者」\nを獲得したのだよ！\n🎉🎉🎉",
        )

    if "DUNGEON_END" in just_events:
        await award_once(
            AWARD_DUNGEON_END,
            ROLE_DUNGEON_END,
            "🎉🎉🎉\n✨【終焉到達】✨\n\nワールド5のフロア100を突破したのだ…！\n🌑「終焉到達者」\nを獲得したのだよ！\n🎉🎉🎉",
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
    await interaction.response.defer(ephemeral=True)

    user_id = interaction.user.id

    # 直近ログ（SQLite）
    save_chat(user_id, message)
    recent_chats = get_recent_chats(user_id)

    # ★ 要約は Sheets から読む
    summary = get_ai_summary_from_sheet(user_id)

    messages = [{"role": "system", "content": ZUNDAMON_SYSTEM}]
    if summary:
        messages.append(
            {"role": "system", "content": f"このユーザーの傾向メモ（非公開）:\n{summary}"}
        )
    for m in recent_chats:
        messages.append({"role": "user", "content": m})

    new_summary: str | None = None

    try:
        loop = asyncio.get_running_loop()

        # ずんだもん返信
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

        await interaction.followup.send(
            f"🗣 **あなた**：{message}\n\n🟢 **ずんだもん**：{reply}"
        )

        # ★ 3発たまったら要約生成
        if len(recent_chats) >= 3:
            summary_prompt = [
                {"role": "system", "content": ZUNDAMON_SYSTEM},
                {
                    "role": "system",
                    "content": "以下の会話から、この人の話し方や好みを短く要約してください。",
                },
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
            clear_chats(user_id)

    except Exception as e:
        print("AI error:", e)
        traceback.print_exc()
        return await interaction.followup.send(
            "ごめんなのだ…今はうまく答えられないのだ 💦"
        )

    # =========================
    # Sheets 更新（回数・要約）
    # =========================
    async with get_user_lock(user_id):
        u = store.get_user(user_id)

        # 回数
        u["ai_chat_count"] = int(u.get("ai_chat_count", 0)) + 1

        # 要約（あれば上書き）
        if new_summary:
            u["ai_summary"] = new_summary

        await sheets_upsert_async(u)

    # ★ AI称号チェック（獲得者のみ通知）
    await maybe_award_hidden_titles(interaction, u, {"AI_CHAT"})


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
# ブラックジャック（完全差し替え版：ephemeral 1枚を編集し続ける / ボタン永久化しない）
# + BJVIP（追加：VIPルールあり / 終了後はVIPに戻る）
# =========================================================
import random
import time
import asyncio
import traceback

import discord
from discord.ext import tasks

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

# ===== VIP 設定 =====
BJVIP_MIN_BET = 10000
BJVIP_SESSION_TIMEOUT_SEC = 20 * 60


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

# =========================================================
# VIP用：ディーラーが狙った点数を作るためのヘルパ
# =========================================================

def _card_points(card: tuple[str, str]) -> int:
    r, _s = card
    if r in ("J", "Q", "K"):
        return 10
    if r == "A":
        return 1  # ここでは1扱い（hand_valueが11化する）
    return int(r)


def pop_card_with_points(deck: list[tuple[str, str]], pts: int) -> tuple[str, str] | None:
    """
    pts 点になるカードをデッキから探して1枚取り出す（VIP用チート）
    なければ None
    """
    for i, c in enumerate(deck):
        if _card_points(c) == pts:
            return deck.pop(i)
    return None


def dealer_draw_to_target(session: dict, target_total: int):
    """
    ディーラーが target_total（17～21）になるようにカードを引く
    ・通常は17で止まる
    ・VIPでは target_total まで例外的に引く
    """
    dealer = session["dealer"]
    deck = session["deck"]

    target_total = max(17, min(int(target_total), 21))

    for _ in range(12):  # 無限ループ防止
        dv = hand_value(dealer)
        if dv >= target_total or dv > 21:
            break

        need = target_total - dv

        # ① 一発でピッタリを狙う
        pick = pop_card_with_points(deck, need)
        if pick:
            dealer.append(pick)
            continue

        # ② 21狙いのときは「16まで寄せる」も試す
        if target_total == 21 and dv <= 12:
            to16 = 16 - dv
            if 1 <= to16 <= 10:
                pick2 = pop_card_with_points(deck, to16)
                if pick2:
                    dealer.append(pick2)
                    continue

        # ③ それでも無理なら普通に引く
        dealer.append(draw_card(deck))

# =========================================================
# セッション管理（通常BJ / VIP）
# =========================================================
bj_sessions: dict[int, dict] = {}
BJ_SESSION_TIMEOUT_SEC = 20 * 60

bjvip_sessions: dict[int, dict] = {}


def touch_bj_session(uid: int):
    s = bj_sessions.get(uid)
    if s:
        s["last_action_ts"] = time.time()


def touch_bjvip_session(uid: int):
    s = bjvip_sessions.get(uid)
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


@tasks.loop(minutes=2)
async def cleanup_bjvip_sessions():
    now = time.time()
    remove_ids = []
    for uid, s in list(bjvip_sessions.items()):
        last_ts = float(s.get("last_action_ts", now))
        if now - last_ts > BJVIP_SESSION_TIMEOUT_SEC:
            remove_ids.append(uid)

    for uid in remove_ids:
        try:
            async with get_user_lock(uid):
                s = bjvip_sessions.get(uid)
                if not s:
                    continue
                refund = int(sum(s.get("bets", []) or [0]))
                u = store.get_user(uid)
                u["coins"] = int(u.get("coins", 0) or 0) + refund
                await sheets_upsert_async(u)
                bjvip_sessions.pop(uid, None)
        except Exception:
            traceback.print_exc()


# =========================================================
# 表示（通常 / VIP）
# =========================================================
def bj_state_text(session: dict, show_dealer_all: bool = False) -> str:
    dealer = session["dealer"]
    if show_dealer_all:
        dealer_txt = f"{fmt_cards(dealer)}（{hand_value(dealer)}）"
    else:
        dealer_txt = f"{dealer[0][1]}{dealer[0][0]} ??"

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

    return (
        "🎴 ブラックジャックなのだ\n\n"
        f"ディーラー：{dealer_txt}\n\n"
        + "\n".join(lines)
    )


def bjvip_state_text(session: dict, show_dealer_all: bool = False) -> str:
    # 見た目は通常BJと同じ構成、タイトルだけVIP
    base = bj_state_text(session, show_dealer_all=show_dealer_all)
    return base.replace("🎴 ブラックジャックなのだ", "💎 BJVIP なのだ")


# =========================================================
# 編集ヘルパ（通常 / VIP 共通で使う）
# =========================================================
async def bj_edit(interaction: discord.Interaction, *, content: str, view: discord.ui.View | None):
    """
    「今押されたボタンが載ってるメッセージ」を同じまま編集し続ける。
    followup.send を増やさないことでズレ/残りを防ぐ。
    """
    try:
        if interaction.response.is_done():
            await interaction.edit_original_response(content=content, view=view)
        else:
            await interaction.response.edit_message(content=content, view=view)
    except discord.NotFound:
        return
    except Exception:
        traceback.print_exc()


# =========================================================
# ベット入力（通常BJ / VIP）
# =========================================================
class BetModal(discord.ui.Modal, title="掛け金を入力するのだ"):
    bet = discord.ui.TextInput(label="掛け金（数字）", placeholder="例：100", required=True)

    def __init__(self, uid: int):
        super().__init__()
        self.uid = int(uid)

    async def on_submit(self, interaction: discord.Interaction):
        # ✅ まず 3秒以内に「元メッセージ」を編集してACKする（deferしない）
        try:
            await interaction.response.edit_message(content="掛け金を処理中なのだ…", view=None)
        except Exception:
            # ここで失敗するなら、そもそも編集対象メッセージが無い/古いボタン等
            traceback.print_exc()
            return

        try:
            if interaction.user.id != self.uid:
                # もう response は使えないので edit_original_response で戻す
                await interaction.edit_original_response(
                    content="これはあなたの操作ではないのだ",
                    view=BJBetView(interaction.user.id),
                )
                return

            if not is_in_channel(interaction, BJ_CHANNEL_ID):
                await interaction.edit_original_response(
                    content="このチャンネルでは使えないのだ",
                    view=BJBetView(interaction.user.id),
                )
                return

            async with get_user_lock(interaction.user.id):
                u = store.get_user(interaction.user.id)

                try:
                    bet_val = int(str(self.bet.value).strip())
                except Exception:
                    await interaction.edit_original_response(
                        content=f"現在の残高：{int(u.get('coins', 0) or 0)} コイン\n数字を入力するのだ",
                        view=BJBetView(interaction.user.id),
                    )
                    return

                if bet_val <= 0:
                    await interaction.edit_original_response(
                        content=f"現在の残高：{int(u.get('coins', 0) or 0)} コイン\n1以上で入力するのだ",
                        view=BJBetView(interaction.user.id),
                    )
                    return

                if bet_val > int(u.get("coins", 0) or 0):
                    await interaction.edit_original_response(
                        content=f"現在の残高：{int(u.get('coins', 0) or 0)} コイン\nコインが足りないのだ",
                        view=BJBetView(interaction.user.id),
                    )
                    return

                MAX_BJ_BET = 1000
                if bet_val > MAX_BJ_BET:
                    await interaction.edit_original_response(
                        content=f"掛け金は最大 {MAX_BJ_BET} までなのだ\n現在の残高：{int(u.get('coins', 0) or 0)} コイン",
                        view=BJBetView(interaction.user.id),
                    )
                    return

                # 既にセッションがある場合は消す（念のため）
                bj_sessions.pop(interaction.user.id, None)

                # コイン差し引き
                u["coins"] = int(u.get("coins", 0) or 0) - bet_val
                await sheets_upsert_async(u)

                # セッション開始
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

            # ✅ 同じメッセージをゲーム画面へ編集（方針そのまま）
            await interaction.edit_original_response(
                content=bj_state_text(session),
                view=build_bj_action_view(interaction.user.id, session),
            )

            # 特殊ケース
            u2 = store.get_user(interaction.user.id)  # 最新を取り直す
            if hand_value(session["dealer"]) == 21:
                await bj_finish(interaction, u2, immediate_dealer_bj=True)
                return

            if session["is_natural_bj"][0]:
                session["finished_hands"][0] = True
                await bj_dealer_turn(interaction, u2)
                return

        except Exception as e:
            print("BetModal on_submit error:", e)
            traceback.print_exc()
            try:
                await interaction.edit_original_response(
                    content="掛け金処理でエラーが出たのだ…（ログを確認してほしいのだ）",
                    view=BJBetView(interaction.user.id),
                )
            except Exception:
                pass

class BetModalVIP(discord.ui.Modal, title="BJVIP 掛け金を入力するのだ"):
    bet = discord.ui.TextInput(label="掛け金（最低10000）", placeholder="例：10000", required=True)

    def __init__(self, uid: int):
        super().__init__()
        self.uid = int(uid)

    async def on_submit(self, interaction: discord.Interaction):
        # ✅ まず3秒制限回避：このモーダルを開いた元メッセージを即編集してACK
        try:
            await interaction.response.edit_message(content="VIP掛け金を処理中なのだ…", view=None)
        except Exception:
            traceback.print_exc()
            return

        try:
            if interaction.user.id != self.uid:
                await interaction.edit_original_response(
                    content="これはあなたの操作ではないのだ",
                    view=BJVIPBetView(interaction.user.id),
                )
                return

            if not is_in_channel(interaction, BJ_CHANNEL_ID):
                await interaction.edit_original_response(
                    content="このチャンネルでは使えないのだ",
                    view=BJVIPBetView(interaction.user.id),
                )
                return

            async with get_user_lock(interaction.user.id):
                u = store.get_user(interaction.user.id)

                try:
                    bet_val = int(str(self.bet.value).strip())
                except Exception:
                    await interaction.edit_original_response(
                        content="数字で入力するのだ",
                        view=BJVIPBetView(interaction.user.id),
                    )
                    return

                if bet_val < BJVIP_MIN_BET:
                    await interaction.edit_original_response(
                        content=f"VIPは最低 {BJVIP_MIN_BET} からなのだ\n現在の残高：{int(u.get('coins',0) or 0)}",
                        view=BJVIPBetView(interaction.user.id),
                    )
                    return

                if bet_val > int(u.get("coins", 0) or 0):
                    await interaction.edit_original_response(
                        content=f"コインが足りないのだ（残高：{int(u.get('coins',0) or 0)}）",
                        view=BJVIPBetView(interaction.user.id),
                    )
                    return

                # 混在防止：通常BJは消す
                bj_sessions.pop(interaction.user.id, None)

                # 既存VIPが残ってたら返金して潰す（事故防止）
                old = bjvip_sessions.pop(interaction.user.id, None)
                if old:
                    refund = int(sum(old.get("bets", []) or [0]))
                    u["coins"] = int(u.get("coins", 0) or 0) + refund

                # 差し引き
                u["coins"] = int(u.get("coins", 0) or 0) - bet_val
                await sheets_upsert_async(u)

                # VIP セッション開始
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
                    "vip": True,
                    "vip_win21": None,
                }

                deal_initial_vip(session, interaction.user.id)
                bjvip_sessions[interaction.user.id] = session

            # ✅ 同じメッセージをVIPゲーム画面へ編集（方式そのまま）
            await interaction.edit_original_response(
                content=bjvip_state_text(session),
                view=build_bjvip_action_view(interaction.user.id, session),
            )

        except Exception:
            traceback.print_exc()
            try:
                await interaction.edit_original_response(
                    content="VIP掛け金処理でエラーが出たのだ…",
                    view=BJVIPBetView(interaction.user.id),
                )
            except Exception:
                pass

async def safe_modal_feedback(interaction: discord.Interaction, *, content: str, view: discord.ui.View):
    """
    Modal submit で edit_message ができない場合があるので、
    できなければ ephemeral 返信にフォールバックするのだ
    """
    try:
        # message があるなら編集（理想）
        if interaction.message:
            await interaction.response.edit_message(content=content, view=view)
        else:
            await interaction.response.send_message(content, view=view, ephemeral=True)
    except discord.InteractionResponded:
        # 既に response 済みなら original を編集（できる場合）
        try:
            await interaction.edit_original_response(content=content, view=view)
        except Exception:
            pass
    except Exception:
        traceback.print_exc()
        try:
            await interaction.response.send_message(content, view=view, ephemeral=True)
        except Exception:
            pass

# =========================================================
# 掛け金画面 View（通常 / VIP）
# =========================================================
class BJBetView(discord.ui.View):
    """掛け金入力画面（非永久）"""
    def __init__(self, uid: int):
        super().__init__(timeout=180)
        self.uid = int(uid)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.uid:
            try:
                await interaction.response.send_message("これはあなたの操作ではないのだ", ephemeral=True)
            except Exception:
                pass
            return False
        return True

    @discord.ui.button(label="💰 掛け金を入力", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_modal(BetModal(uid=self.uid))
        except Exception:
            traceback.print_exc()

    @discord.ui.button(label="💎 VIPで入場", style=discord.ButtonStyle.danger)
    async def vip_enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            u = store.get_user(interaction.user.id)
            await bj_edit(
                interaction,
                content=(
                    f"💎 **BJVIP 開始なのだ**\n"
                    f"現在の残高：{int(u.get('coins', 0) or 0)} コイン\n\n"
                    f"下のボタンから掛け金を入力するのだ（最低 {BJVIP_MIN_BET}）"
                ),
                view=BJVIPBetView(interaction.user.id),
            )
        except Exception:
            traceback.print_exc()
            try:
                await interaction.response.send_message("VIP入場でエラーが出たのだ…", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await bj_edit(interaction, content="終了したのだ", view=None)


class BJVIPBetView(discord.ui.View):
    """VIP掛け金入力画面（非永久）"""
    def __init__(self, uid: int):
        super().__init__(timeout=180)
        self.uid = int(uid)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.uid:
            try:
                await interaction.response.send_message("これはあなたの操作ではないのだ", ephemeral=True)
            except Exception:
                pass
            return False
        return True

    @discord.ui.button(label="💎 VIP掛け金を入力", style=discord.ButtonStyle.danger)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_modal(BetModalVIP(uid=self.uid))
        except Exception:
            traceback.print_exc()

    @discord.ui.button(label="戻る", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        u = store.get_user(interaction.user.id)
        await bj_edit(
            interaction,
            content=(
                f"🎴 ブラックジャック開始なのだ\n"
                f"現在の残高：{int(u.get('coins', 0) or 0)} コイン\n\n"
                "下のボタンから掛け金を入力するのだ"
            ),
            view=BJBetView(interaction.user.id),
        )

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await bj_edit(interaction, content="終了したのだ", view=None)

# =========================================================
# 入口（あなたの元コード通り：persistent）
# =========================================================
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
                return await interaction.response.send_message("このチャンネルでは使えないのだ", ephemeral=True)

            u = store.get_user(interaction.user.id)
            await interaction.response.send_message(
                f"🎴 ブラックジャック開始なのだ\n現在の残高：{int(u.get('coins', 0) or 0)} コイン\n\n"
                "下のボタンから掛け金を入力するのだ",
                view=BJBetView(interaction.user.id),
                ephemeral=True,
            )
        except Exception:
            traceback.print_exc()
            try:
                await interaction.followup.send("開始でエラーが出たのだ…", ephemeral=True)
            except Exception:
                pass


# =========================================================
# アクションView（通常BJ：元のまま）
# =========================================================
class BJActionView(discord.ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=900)
        self.uid = uid

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.uid:
            try:
                await interaction.response.send_message("これはあなたの操作ではないのだ", ephemeral=True)
            except Exception:
                pass
            return False

        if interaction.user.id not in bj_sessions:
            try:
                await interaction.response.send_message("セッションがないのだ（終了したのだ）", ephemeral=True)
            except Exception:
                pass
            return False

        return True

    @discord.ui.button(label="ヒット", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        u = store.get_user(interaction.user.id)
        await bj_hit(interaction, u)

    @discord.ui.button(label="スタンド", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        u = store.get_user(interaction.user.id)
        await bj_stand(interaction, u)

    @discord.ui.button(label="ダブルダウン", style=discord.ButtonStyle.danger)
    async def double(self, interaction: discord.Interaction, button: discord.ui.Button):
        u = store.get_user(interaction.user.id)
        await bj_double(interaction, u)

    @discord.ui.button(label="スプリット", style=discord.ButtonStyle.success)
    async def split(self, interaction: discord.Interaction, button: discord.ui.Button):
        u = store.get_user(interaction.user.id)
        await bj_split(interaction, u)


def build_bj_action_view(uid: int, session: dict) -> discord.ui.View:
    view = BJActionView(uid)

    active = session["active"]
    hand = session["hands"][active]
    can_split = (len(session["hands"]) == 1) and (len(hand) == 2) and (hand[0][0] == hand[1][0])
    can_double = (len(session["hands"]) == 1) and (len(hand) == 2) and (not session["doubled"][active])

    for item in view.children:
        if isinstance(item, discord.ui.Button):
            if item.label == "スプリット":
                item.disabled = not can_split
            elif item.label == "ダブルダウン":
                item.disabled = not can_double

    return view


# =========================================================
# VIP アクションView（追加）
# =========================================================
class BJVIPActionView(discord.ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=900)
        self.uid = int(uid)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.uid:
            try:
                await interaction.response.send_message("これはあなたの操作ではないのだ", ephemeral=True)
            except Exception:
                pass
            return False

        if interaction.user.id not in bjvip_sessions:
            try:
                await interaction.response.send_message("VIPセッションがないのだ（終了したのだ）", ephemeral=True)
            except Exception:
                pass
            return False

        return True

    @discord.ui.button(label="ヒット", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        u = store.get_user(interaction.user.id)
        await bjvip_hit(interaction, u)

    @discord.ui.button(label="スタンド", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        u = store.get_user(interaction.user.id)
        await bjvip_stand(interaction, u)

    @discord.ui.button(label="ダブルダウン", style=discord.ButtonStyle.danger)
    async def double(self, interaction: discord.Interaction, button: discord.ui.Button):
        u = store.get_user(interaction.user.id)
        await bjvip_double(interaction, u)

    @discord.ui.button(label="スプリット", style=discord.ButtonStyle.success)
    async def split(self, interaction: discord.Interaction, button: discord.ui.Button):
        u = store.get_user(interaction.user.id)
        await bjvip_split(interaction, u)


def build_bjvip_action_view(uid: int, session: dict) -> discord.ui.View:
    view = BJVIPActionView(uid)

    active = session["active"]
    hand = session["hands"][active]
    can_split = (len(session["hands"]) == 1) and (len(hand) == 2) and (hand[0][0] == hand[1][0])
    can_double = (len(session["hands"]) == 1) and (len(hand) == 2) and (not session["doubled"][active])

    for item in view.children:
        if isinstance(item, discord.ui.Button):
            if item.label == "スプリット":
                item.disabled = not can_split
            elif item.label == "ダブルダウン":
                item.disabled = not can_double

    return view


# =========================================================
# 終了View（通常 / VIP）
# =========================================================
class BJEndView(discord.ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=180)
        self.uid = uid

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.uid:
            await interaction.response.send_message("これはあなたの操作ではないのだ", ephemeral=True)
            return False

        if interaction.user.id not in bj_sessions:
            u = store.get_user(interaction.user.id)
            await bj_edit(
                interaction,
                content=(
                    f"🎴 セッションが終了/期限切れなのだ\n"
                    f"現在の残高：{int(u.get('coins',0) or 0)} コイン\n\n"
                    "もう一度掛け金を入力するのだ"
                ),
                view=BJBetView(interaction.user.id),
            )
            return False

        return True
    @discord.ui.button(label="🎴 もう一回スタート", style=discord.ButtonStyle.primary)
    async def restart(self, interaction: discord.Interaction, button: discord.ui.Button):
        u = store.get_user(interaction.user.id)
        await bj_edit(
            interaction,
            content=(
                f"🎴 もう一回やるのだ\n現在の残高：{int(u.get('coins', 0) or 0)} コイン\n\n"
                "下のボタンから掛け金を入力するのだ"
            ),
            view=BJBetView(interaction.user.id),
        )

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def quit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await bj_edit(interaction, content="終了したのだ", view=None)


class BJVIPEndView(discord.ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=180)
        self.uid = int(uid)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.uid:
            await interaction.response.send_message("これはあなたの操作ではないのだ", ephemeral=True)
            return False

        if interaction.user.id not in bjvip_sessions:
            u = store.get_user(interaction.user.id)
            await bj_edit(
                interaction,
                content=(
                    f"💎 VIPセッションが終了/期限切れなのだ\n"
                    f"現在の残高：{int(u.get('coins',0) or 0)} コイン\n\n"
                    f"もう一度掛け金を入力するのだ（最低 {BJVIP_MIN_BET}）"
                ),
                view=BJVIPBetView(interaction.user.id),
            )
            return False

        return True
    @discord.ui.button(label="💎 VIPでもう一回", style=discord.ButtonStyle.danger)
    async def restart_vip(self, interaction: discord.Interaction, button: discord.ui.Button):
        u = store.get_user(interaction.user.id)
        await bj_edit(
            interaction,
            content=(
                f"💎 **BJVIP もう一回なのだ**\n"
                f"現在の残高：{int(u.get('coins', 0) or 0)} コイン\n\n"
                f"下のボタンから掛け金を入力するのだ（最低 {BJVIP_MIN_BET}）"
            ),
            view=BJVIPBetView(interaction.user.id),
        )

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def quit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await bj_edit(interaction, content="終了したのだ", view=None)


# =========================================================
# 進行ロジック：通常BJ（あなたの元のまま）
# =========================================================
async def bj_render(interaction: discord.Interaction, u: dict, show_dealer_all: bool = False):
    session = bj_sessions.get(interaction.user.id)
    if not session:
        return await bj_edit(interaction, content="セッションがないのだ", view=None)

    touch_bj_session(interaction.user.id)
    view = None if show_dealer_all else build_bj_action_view(interaction.user.id, session)

    await bj_edit(
        interaction,
        content=bj_state_text(session, show_dealer_all=show_dealer_all),
        view=view,
    )


# =========================================================
# 進行ロジック：通常BJ（ロックでレース防止版）
#  - セッション辞書の更新/参照も get_user_lock(uid) の中でやる
# =========================================================

async def bj_hit(interaction: discord.Interaction, u: dict):
    uid = interaction.user.id

    # --- ロック内：状態更新だけ ---
    async with get_user_lock(uid):
        session = bj_sessions.get(uid)
        if not session:
            return await bj_edit(interaction, content="セッションがないのだ", view=None)

        touch_bj_session(uid)

        i = session["active"]
        session["hands"][i].append(draw_card(session["deck"]))
        v = hand_value(session["hands"][i])

        if v > 21:
            session["finished_hands"][i] = True
            need_next = True
        else:
            need_next = False

    # --- ロック外：表示/進行 ---
    await bj_render(interaction, store.get_user(uid))

    if need_next:
        await bj_next_or_dealer(interaction, store.get_user(uid))

async def bj_stand(interaction: discord.Interaction, u: dict):
    uid = interaction.user.id

    async with get_user_lock(uid):
        session = bj_sessions.get(uid)
        if not session:
            return await bj_edit(interaction, content="セッションがないのだ", view=None)

        touch_bj_session(uid)
        session["finished_hands"][session["active"]] = True

    await bj_next_or_dealer(interaction, store.get_user(uid))

async def bj_double(interaction: discord.Interaction, u: dict):
    uid = interaction.user.id

    async with get_user_lock(uid):
        session = bj_sessions.get(uid)
        if not session:
            return await bj_edit(interaction, content="セッションがないのだ", view=None)

        touch_bj_session(uid)

        if len(session["hands"]) != 1:
            return await bj_edit(
                interaction,
                content="スプリット後はダブルダウンできないのだ",
                view=build_bj_action_view(uid, session),
            )

        i = session["active"]
        hand = session["hands"][i]
        if len(hand) != 2 or session["doubled"][i]:
            # 状態変化なし
            pass
        else:
            add = int(session["bets"][i])
            u_local = store.get_user(uid)
            if int(u_local.get("coins", 0) or 0) < add:
                return await bj_edit(
                    interaction,
                    content="コインが足りないのだ",
                    view=build_bj_action_view(uid, session),
                )

            u_local["coins"] = int(u_local.get("coins", 0) or 0) - add
            session["bets"][i] += add
            session["doubled"][i] = True
            await sheets_upsert_async(u_local)

            session["hands"][i].append(draw_card(session["deck"]))
            session["finished_hands"][i] = True

    await bj_next_or_dealer(interaction, store.get_user(uid))

async def bj_split(interaction: discord.Interaction, u: dict):
    uid = interaction.user.id

    async with get_user_lock(uid):
        session = bj_sessions.get(uid)
        if not session:
            return await bj_edit(interaction, content="セッションがないのだ", view=None)

        touch_bj_session(uid)

        if len(session["hands"]) != 1:
            return await bj_edit(
                interaction,
                content="もうスプリット済みなのだ",
                view=build_bj_action_view(uid, session),
            )

        hand = session["hands"][0]
        if len(hand) != 2 or hand[0][0] != hand[1][0]:
            # 状態変化なし
            pass
        else:
            bet = int(session["bets"][0])

            u_local = store.get_user(uid)
            if int(u_local.get("coins", 0) or 0) < bet:
                return await bj_edit(
                    interaction,
                    content="スプリット分のコインが足りないのだ",
                    view=build_bj_action_view(uid, session),
                )

            u_local["coins"] = int(u_local.get("coins", 0) or 0) - bet
            await sheets_upsert_async(u_local)

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

    await bj_render(interaction, store.get_user(uid))

async def bj_next_or_dealer(interaction: discord.Interaction, u: dict):
    session = bj_sessions.get(interaction.user.id)
    if not session:
        return

    for idx, fin in enumerate(session["finished_hands"]):
        if not fin:
            session["active"] = idx
            await bj_render(interaction, u)
            return

    await bj_dealer_turn(interaction, u)


async def bj_dealer_turn(interaction: discord.Interaction, u: dict):
    session = bj_sessions.get(interaction.user.id)
    if not session:
        return await bj_edit(interaction, content="セッションがないのだ", view=None)

    touch_bj_session(interaction.user.id)

    dealer = session["dealer"]

    await bj_edit(
        interaction,
        content="ディーラーのターンなのだ\n\n" + bj_state_text(session, show_dealer_all=True),
        view=None,
    )

    # ✅ 17以上で必ず止まる（ソフト17も止めるならこのままでOK）
    while hand_value(dealer) < 17:
        await asyncio.sleep(0.6)
        dealer.append(draw_card(session["deck"]))
        await bj_edit(
            interaction,
            content="ディーラーがヒットしたのだ\n\n" + bj_state_text(session, show_dealer_all=True),
            view=None,
        )

    await bj_finish(interaction, u, immediate_dealer_bj=False)

async def bj_finish(interaction: discord.Interaction, u: dict, immediate_dealer_bj: bool):
    uid = interaction.user.id

    # セッション取得はロック内推奨（レース防止）
    async with get_user_lock(uid):
        session = bj_sessions.get(uid)
        if not session:
            return await bj_edit(interaction, content="セッションがないのだ", view=None)

        touch_bj_session(uid)

        dealer_val = hand_value(session["dealer"])
        dealer_bust = dealer_val > 21

        payout_total = 0
        profit = 0
        results: list[str] = []

        for idx, hand in enumerate(session["hands"]):
            bet = int(session["bets"][idx])
            v = hand_value(hand)

            payout = 0

            # プレイヤーバースト
            if v > 21:
                results.append(f"手札{idx+1}：負け（バースト）")
                payout = 0

            # ディーラー即BJ（初手2枚21）
            elif immediate_dealer_bj:
                player_natural = (
                    idx < len(session.get("is_natural_bj", []))
                    and bool(session["is_natural_bj"][idx])
                )
                if player_natural:
                    results.append(f"手札{idx+1}：引き分け（両者BJ）")
                    payout = bet  # 返金
                else:
                    results.append(f"手札{idx+1}：負け（ディーラーBJ）")
                    payout = 0

            # 通常判定
            else:
                player_natural = (
                    idx < len(session.get("is_natural_bj", []))
                    and bool(session["is_natural_bj"][idx])
                )

                # プレイヤーBJ（2枚21）で、ディーラーがBJじゃないなら 3:2 払い（好みで変更OK）
                if player_natural and dealer_val != 21:
                    results.append(f"手札{idx+1}：勝ち（ブラックジャック）")
                    payout = bet + int(bet * 1.5)

                # ディーラーバースト
                elif dealer_bust:
                    results.append(f"手札{idx+1}：勝ち（ディーラーバースト）")
                    payout = bet * 2

                # 数値比較
                else:
                    if v > dealer_val:
                        results.append(f"手札{idx+1}：勝ち")
                        payout = bet * 2
                    elif v == dealer_val:
                        results.append(f"手札{idx+1}：引き分け")
                        payout = bet
                    else:
                        results.append(f"手札{idx+1}：負け")
                        payout = 0

            payout_total += payout
            profit += (payout - bet)

        # 払戻し反映
        u_local = store.get_user(uid)
        u_local["coins"] = int(u_local.get("coins", 0) or 0) + payout_total

        # 統計（勝ち/連勝などはあなたの設計に合わせて最低限）
        just_events = set()
        if profit > 0:
            u_local["bj_win_streak"] = int(u_local.get("bj_win_streak", 0) or 0) + 1
            just_events.add("BJ_WIN_EVENT")
            if profit >= 1000:
                just_events.add("BJ_BIGWIN_EVENT")
        else:
            # 引き分け/負けで連勝は止める（好みで「引き分けは維持」も可）
            u_local["bj_win_streak"] = 0
            if profit <= -1000:
                just_events.add("BJ_BIGLOSE_EVENT")

        u_local["bj_play_count"] = int(u_local.get("bj_play_count", 0) or 0) + 1

        await sheets_upsert_async(u_local)

        # セッション終了
        bj_sessions.pop(uid, None)

    # 表示（ロック外）
    sign = "+" if profit > 0 else ""
    text = (
        "🎴 **結果なのだ！**\n\n"
        + bj_state_text(session, show_dealer_all=True)
        + "\n\n"
        + "\n".join(results)
        + f"\n\n💰 収支：{sign}{profit}（払戻合計：{payout_total}）なのだ\n"
        + f"残高：{u_local['coins']} コインなのだ"
    )

    await bj_edit(interaction, content=text, view=BJEndView(uid))

    # 称号チェック（通知は獲得時だけ）
    try:
        await maybe_award_hidden_titles(interaction, u_local, just_events)
    except Exception:
        traceback.print_exc()

# =========================================================
# VIP 進行ロジック（追加）
# =========================================================
def vip_dealer_target_by_player_best(player_best: int) -> str:
    """
    VIP用：プレイヤーの最高値（バースト除く）に応じて、
    ディーラーの狙う結果を確率で決めるのだ。

    戻り値:
      "WIN"  : プレイヤー勝ち（ディーラーが低い or バースト）
      "PUSH" : 引き分け
      "LOSE" : プレイヤー負け（ディーラーが上）
    """
    r = random.random()

    # 21：勝利50% / 引き分け50%（負けは無し）
    if player_best >= 21:
        if r < 0.50:
            return "WIN"
        return "PUSH"

    # 20：負け30% / 引き分け30% / 勝利40%
    if player_best == 20:
        if r < 0.40:
            return "WIN"
        if r < 0.70:
            return "PUSH"
        return "LOSE"

    # 19：負け50% / 引き分け25% / 勝利25%
    if player_best == 19:
        if r < 0.25:
            return "WIN"
        if r < 0.50:
            return "PUSH"
        return "LOSE"

    # 18：負け70% / 引き分け20% / 勝利10%
    if player_best == 18:
        if r < 0.10:
            return "WIN"
        if r < 0.30:
            return "PUSH"
        return "LOSE"

    # 17：負け80% / 引き分け10% / 勝利10%
    if player_best == 17:
        if r < 0.10:
            return "WIN"
        if r < 0.20:
            return "PUSH"
        return "LOSE"

    # 16以下：勝利は「ディーラーがバースト(0.1%)」のみ、それ以外は負け
    # ※ "WIN" を引いた時は、後段のディーラー生成でバーストを狙わせる必要がある
    if r < 0.001:  # 0.1% = 0.001
        return "WIN"
    return "LOSE"

def _dealer_play_to_target_from_current(dealer: list[tuple[str, str]], deck: list[tuple[str, str]],
                                       target: str, player_best: int) -> None:
    """
    既に配られている dealer(2枚) から追加で引いて、
    target（WIN/PUSH/LOSE）をそれっぽく狙うのだ。
    ※ どうしても無理なら自然に 17 まで引く挙動に寄せる
    """
    # ディーラーは通常「17以上で停止」前提に寄せる
    # PUSH を狙うなら、最低17以上で player_best と同値を狙う
    # WIN/LOSE は player_best との大小関係を狙う
    max_steps = 10

    def natural_dealer():
        while hand_value(dealer) < 17:
            dealer.append(draw_card(deck))

    # プレイヤーが 17 未満なら PUSH は現実的に作れない（通常ディーラーが17で止まるため）
    if target == "PUSH" and player_best < 17:
        target = "LOSE"

    # まず「今のdealerがすでに条件を満たしてる」ならそのままにする
    dv = hand_value(dealer)
    if dv > 21:
        return  # もうバースト

    if target == "PUSH":
        if dv == player_best and dv >= 17:
            return

    if target == "WIN":
        # ディーラーがバースト or 17〜player_best-1 にしたい
        if dv >= 17 and dv < player_best:
            return

    # target == "LOSE" の処理の最後あたり（フォールバック前）に追加
    if target == "LOSE":
        # バーストしてたら作り直して 17〜21 に寄せる（見た目の整合性優先）
        if hand_value(dealer) > 21:
            # 作り直し（何回か試す）
            for _ in range(10):
                dealer[:] = [draw_card(deck), draw_card(deck)]
                while hand_value(dealer) < 17:
                    dealer.append(draw_card(deck))
                if hand_value(dealer) <= 21:
                    break

    # 追加で引いて狙う
    for _ in range(max_steps):
        dv = hand_value(dealer)
        if dv > 21:
            return

        if target == "PUSH":
            # player_best に近づけたい：足りないなら引く、超えたら諦めて自然進行
            if dv < player_best:
                dealer.append(draw_card(deck))
                continue
            if dv == player_best and dv >= 17:
                return
            # 超えたら自然進行へ
            break

        if target == "WIN":
            # 17未満なら引く。17以上で player_best 未満ならOK。
            if dv < 17:
                dealer.append(draw_card(deck))
                continue
            if dv < player_best:
                return
            # 強すぎたら（dv >= player_best）→ バースト狙いに寄せてもう少し引く
            dealer.append(draw_card(deck))
            continue

        if target == "LOSE":
            # 17未満なら引く。17以上で player_best より上ならOK。足りないなら引く。
            if dv < 17:
                dealer.append(draw_card(deck))
                continue
            if player_best >= 21:
                # 21に寄せたい
                if dv == 21:
                    return
                dealer.append(draw_card(deck))
                continue
            if dv > player_best:
                return
            dealer.append(draw_card(deck))
            continue

    # 最後に自然なディーラー挙動へフォールバック
    natural_dealer()

async def bjvip_render(interaction: discord.Interaction, u: dict, show_dealer_all: bool = False):
    session = bjvip_sessions.get(interaction.user.id)
    if not session:
        return await bj_edit(interaction, content="VIPセッションがないのだ", view=None)

    touch_bjvip_session(interaction.user.id)
    view = None if show_dealer_all else build_bjvip_action_view(interaction.user.id, session)

    await bj_edit(
        interaction,
        content=bjvip_state_text(session, show_dealer_all=show_dealer_all),
        view=view,
    )


# =========================================================
# VIP 進行ロジック（完全差し替え：ロック統一 / 引き分け返金 / バースト勝ち）
# =========================================================

def vip_dealer_target_by_player_best(player_best: int) -> str:
    """
    VIP用：プレイヤーの最高値（バースト除く）に応じて、
    ディーラーの狙う結果を確率で決めるのだ。

    戻り値:
      "WIN"  : プレイヤー勝ち（ディーラーが低い or バースト）
      "PUSH" : 引き分け
      "LOSE" : プレイヤー負け（ディーラーが上）
    """
    r = random.random()

    if player_best >= 21:
        if r < 0.55:
            return "WIN"
        if r < 0.75:
            return "PUSH"
        return "LOSE"

    if player_best == 20:
        if r < 0.35:
            return "WIN"
        if r < 0.60:
            return "PUSH"
        return "LOSE"

    if player_best == 19:
        if r < 0.30:
            return "WIN"
        if r < 0.45:
            return "PUSH"
        return "LOSE"

    if player_best == 18:
        if r < 0.25:
            return "WIN"
        if r < 0.35:
            return "PUSH"
        return "LOSE"

    if player_best == 17:
        if r < 0.18:
            return "WIN"
        if r < 0.25:
            return "PUSH"
        return "LOSE"

    if r < 0.10:
        return "WIN"
    return "LOSE"


def _dealer_play_to_target_from_current(
    dealer: list[tuple[str, str]],
    deck: list[tuple[str, str]],
    target: str,
    player_best: int
) -> None:
    """
    既に配られている dealer(2枚) から追加で引いて、
    target（WIN/PUSH/LOSE）をそれっぽく狙うのだ。
    """
    max_steps = 10

    def natural_dealer():
        while hand_value(dealer) < 17:
            dealer.append(draw_card(deck))

    if target == "PUSH" and player_best < 17:
        target = "LOSE"

    dv = hand_value(dealer)
    if dv > 21:
        return

    if target == "PUSH":
        if dv == player_best and dv >= 17:
            return

    if target == "WIN":
        if dv >= 17 and dv < player_best:
            return

    if target == "LOSE":
        if dv >= 17 and dv <= 21 and dv > player_best:
            return
        if player_best >= 21 and dv == 21:
            return

    for _ in range(max_steps):
        dv = hand_value(dealer)
        if dv > 21:
            return

        if target == "PUSH":
            if dv < player_best:
                dealer.append(draw_card(deck))
                continue
            if dv == player_best and dv >= 17:
                return
            break

        if target == "WIN":
            if dv < 17:
                dealer.append(draw_card(deck))
                continue
            if dv < player_best:
                return
            dealer.append(draw_card(deck))
            continue

        if target == "LOSE":
            if dv < 17:
                dealer.append(draw_card(deck))
                continue
            if player_best >= 21:
                if dv == 21:
                    return
                dealer.append(draw_card(deck))
                continue
            if dv > player_best:
                return
            dealer.append(draw_card(deck))
            continue

    natural_dealer()


async def bjvip_render_nolock(interaction: discord.Interaction, session: dict, *, show_dealer_all: bool = False):
    """
    ※ 呼び出し側で get_user_lock(uid) を持ってる前提なのだ
    """
    view = None if show_dealer_all else build_bjvip_action_view(interaction.user.id, session)
    await bj_edit(
        interaction,
        content=bjvip_state_text(session, show_dealer_all=show_dealer_all),
        view=view,
    )


async def bjvip_next_or_finish_nolock(interaction: discord.Interaction, u: dict, session: dict):
    """
    ※ 呼び出し側で get_user_lock(uid) を持ってる前提
    """
    for idx, fin in enumerate(session["finished_hands"]):
        if not fin:
            session["active"] = idx
            await bjvip_render_nolock(interaction, session, show_dealer_all=False)
            return

    await bjvip_dealer_turn_and_finish_nolock(interaction, u, session)


async def bjvip_dealer_turn_and_finish_nolock(interaction: discord.Interaction, u: dict, session: dict):
    """
    VIPは「狙った見た目」にディーラーを引かせる（ただし最終判定は比較で整合）
    ※ 呼び出し側で get_user_lock(uid) を持ってる前提
    """
    touch_bjvip_session(interaction.user.id)

    vals = [hand_value(h) for h in session["hands"]]
    safe_vals = [v for v in vals if v <= 21]
    player_best = max(safe_vals) if safe_vals else 0

    target = vip_dealer_target_by_player_best(player_best)

    dealer = session["dealer"]
    _dealer_play_to_target_from_current(dealer, session["deck"], target, player_best)

    await bj_edit(
        interaction,
        content="💎 ディーラーのターンなのだ\n\n" + bjvip_state_text(session, show_dealer_all=True),
        view=None,
    )
    await asyncio.sleep(0.6)

    await bjvip_finish_nolock(interaction, u, session)


async def bjvip_finish_nolock(interaction: discord.Interaction, u: dict, session: dict):
    """
    VIPの勝敗判定（整合保証）
    - dealer_bust ならバーストしてない手札は必ず勝ち
    - 同値なら引き分け（返金）
    - 通常比較で勝敗
    ※ 呼び出し側で get_user_lock(uid) を持ってる前提
    """
    uid = interaction.user.id
    touch_bjvip_session(uid)

    dealer_val = hand_value(session["dealer"])
    dealer_bust = dealer_val > 21

    payout_total = 0
    profit = 0
    results: list[str] = []

    for idx, hand in enumerate(session["hands"]):
        bet = int(session["bets"][idx])
        v = hand_value(hand)

        payout = 0

        if v > 21:
            results.append(f"手札{idx+1}：負け（バースト）")
            payout = 0

        elif dealer_bust:
            # ✅ ディーラーがバーストなら、バーストしてない手札は必ず勝ち
            if idx < len(session.get("is_natural_bj", [])) and session["is_natural_bj"][idx]:
                payout = (bet * 5) // 2
                results.append(f"手札{idx+1}：勝ち（ディーラーバースト / BJ 3:2）")
            else:
                payout = bet * 2
                results.append(f"手札{idx+1}：勝ち（ディーラーバースト）")

        else:
            # ✅ 比較で勝敗／引き分け
            if v > dealer_val:
                if idx < len(session.get("is_natural_bj", [])) and session["is_natural_bj"][idx]:
                    payout = (bet * 5) // 2
                    results.append(f"手札{idx+1}：勝ち（BJ 3:2）")
                else:
                    payout = bet * 2
                    results.append(f"手札{idx+1}：勝ち（VIP）")
            elif v < dealer_val:
                payout = 0
                results.append(f"手札{idx+1}：負け（VIP）")
            else:
                # ✅ 引き分けは返金
                payout = bet
                results.append(f"手札{idx+1}：引き分け（返金）")

        payout_total += payout
        profit += (payout - bet)

    # ✅ コイン加算・統計更新は「1回だけ」
    u_local = store.get_user(uid)
    u_local["coins"] = int(u_local.get("coins", 0) or 0) + payout_total
    u_local["bj_play_count"] = int(u_local.get("bj_play_count", 0) or 0) + 1
    u_local["bj_win_streak"] = int(u_local.get("bj_win_streak", 0) or 0)
    u_local["total_earned"] = int(u_local.get("total_earned", 0) or 0)

    just_events = set()
    if profit > 0:
        u_local["bj_win_streak"] += 1
        u_local["total_earned"] += profit
        just_events.add("BJ_WIN_EVENT")
        if profit >= 1000:
            just_events.add("BJ_BIGWIN_EVENT")
    elif profit < 0:
        u_local["bj_win_streak"] = 0
        if profit <= -1000:
            just_events.add("BJ_BIGLOSE_EVENT")
    else:
        # 引き分けのみでも streak を切るなら0、維持したいならここを変えてOK
        u_local["bj_win_streak"] = 0

    await sheets_upsert_async(u_local)

    msg = (
        "💎 BJVIP 結果なのだ\n\n"
        f"ディーラー：{fmt_cards(session['dealer'])}（{dealer_val}）\n"
        + "\n".join(results)
        + f"\n\n残高：{u_local['coins']} コインなのだ\n\n次はどうするのだ？"
    )

    await bj_edit(interaction, content=msg, view=BJVIPEndView(uid))

    try:
        await maybe_award_hidden_titles(interaction, u_local, just_events=just_events)
    except Exception:
        traceback.print_exc()

    # セッション終了
    bjvip_sessions.pop(uid, None)


# ---------------------------------------------------------
# VIP：ボタン操作（ここが入口）…全部ロックで直列化
# ---------------------------------------------------------

async def bjvip_hit(interaction: discord.Interaction, u: dict):
    uid = interaction.user.id
    async with get_user_lock(uid):
        session = bjvip_sessions.get(uid)
        if not session:
            return await bj_edit(interaction, content="VIPセッションがないのだ", view=None)

        touch_bjvip_session(uid)

        i = session["active"]
        session["hands"][i].append(draw_card(session["deck"]))
        v = hand_value(session["hands"][i])

        if v > 21:
            session["finished_hands"][i] = True
            await bjvip_render_nolock(interaction, session, show_dealer_all=False)
            await bjvip_next_or_finish_nolock(interaction, store.get_user(uid), session)
            return

        await bjvip_render_nolock(interaction, session, show_dealer_all=False)


async def bjvip_stand(interaction: discord.Interaction, u: dict):
    uid = interaction.user.id
    async with get_user_lock(uid):
        session = bjvip_sessions.get(uid)
        if not session:
            return await bj_edit(interaction, content="VIPセッションがないのだ", view=None)

        touch_bjvip_session(uid)

        i = session["active"]
        session["finished_hands"][i] = True
        await bjvip_next_or_finish_nolock(interaction, store.get_user(uid), session)


async def bjvip_double(interaction: discord.Interaction, u: dict):
    uid = interaction.user.id
    async with get_user_lock(uid):
        session = bjvip_sessions.get(uid)
        if not session:
            return await bj_edit(interaction, content="VIPセッションがないのだ", view=None)

        touch_bjvip_session(uid)

        if len(session["hands"]) != 1:
            return await bj_edit(
                interaction,
                content="スプリット後はダブルダウンできないのだ",
                view=build_bjvip_action_view(uid, session),
            )

        i = session["active"]
        hand = session["hands"][i]
        if len(hand) != 2 or session["doubled"][i]:
            return await bjvip_render_nolock(interaction, session, show_dealer_all=False)

        add = int(session["bets"][i])
        u_local = store.get_user(uid)
        if int(u_local.get("coins", 0) or 0) < add:
            return await bj_edit(
                interaction,
                content="コインが足りないのだ",
                view=build_bjvip_action_view(uid, session),
            )

        u_local["coins"] = int(u_local.get("coins", 0) or 0) - add
        session["bets"][i] += add
        session["doubled"][i] = True
        await sheets_upsert_async(u_local)

        session["hands"][i].append(draw_card(session["deck"]))
        session["finished_hands"][i] = True

        await bjvip_next_or_finish_nolock(interaction, u_local, session)


async def bjvip_split(interaction: discord.Interaction, u: dict):
    uid = interaction.user.id
    async with get_user_lock(uid):
        session = bjvip_sessions.get(uid)
        if not session:
            return await bj_edit(interaction, content="VIPセッションがないのだ", view=None)

        touch_bjvip_session(uid)

        if len(session["hands"]) != 1:
            return await bj_edit(
                interaction,
                content="もうスプリット済みなのだ",
                view=build_bjvip_action_view(uid, session),
            )

        hand = session["hands"][0]
        if len(hand) != 2 or hand[0][0] != hand[1][0]:
            return await bjvip_render_nolock(interaction, session, show_dealer_all=False)

        bet = int(session["bets"][0])
        u_local = store.get_user(uid)
        if int(u_local.get("coins", 0) or 0) < bet:
            return await bj_edit(
                interaction,
                content="スプリット分のコインが足りないのだ",
                view=build_bjvip_action_view(uid, session),
            )

        u_local["coins"] = int(u_local.get("coins", 0) or 0) - bet
        await sheets_upsert_async(u_local)

        c1, c2 = hand[0], hand[1]

        session["hands"] = [[c1], [c2]]
        session["bets"] = [bet, bet]
        session["finished_hands"] = [False, False]
        session["doubled"] = [False, False]
        session["active"] = 0
        session["was_split"] = True
        session["is_natural_bj"] = [False, False]  # スプリット後BJを3:2扱いしないならこれでOK

        session["hands"][0].append(draw_card(session["deck"]))
        session["hands"][1].append(draw_card(session["deck"]))

        await bjvip_render_nolock(interaction, session, show_dealer_all=False)


async def bjvip_next_or_finish(interaction: discord.Interaction, u: dict):
    session = bjvip_sessions.get(interaction.user.id)
    if not session:
        return

    for idx, fin in enumerate(session["finished_hands"]):
        if not fin:
            session["active"] = idx
            await bjvip_render(interaction, u)
            return

    await bjvip_dealer_turn_and_finish(interaction, u)
    
def vip_win_prob(v: int) -> float:
    """
    VIP勝率（体感重視）
    """
    if v >= 21:
        return 0.75   # 21はかなり勝てる
    if v == 20:
        return 0.55
    if v == 19:
        return 0.40
    if v == 18:
        return 0.30
    if v == 17:
        return 0.20
    return 0.08       # 16以下もワンチャンあり
    
bjvip_natural_streak: dict[int, int] = {}

def deal_initial_vip(session: dict, uid: int):
    """
    VIP用初期配布
    ・自然BJが2連続以上出たら抑制
    """
    deck = session["deck"]
    streak = bjvip_natural_streak.get(uid, 0)

    for _ in range(5):
        session["hands"][0] = [draw_card(deck), draw_card(deck)]
        session["dealer"] = [draw_card(deck), draw_card(deck)]
        is_bj = (hand_value(session["hands"][0]) == 21)
        session["is_natural_bj"][0] = is_bj

        if streak >= 2 and is_bj:
            continue
        break

    if session["is_natural_bj"][0]:
        bjvip_natural_streak[uid] = streak + 1
    else:
        bjvip_natural_streak[uid] = 0

def _vip_pick_win21(session: dict) -> bool:
    """
    VIPルール：
    - 21のときだけ 50%で勝ち
    - それ以外は勝たせない
    1ラウンド1回だけ決めて固定（splitでブレないように）
    """
    if session.get("vip_win21") is None:
        session["vip_win21"] = (random.random() < 0.5)
    return bool(session["vip_win21"])


def _vip_build_dealer_hand(session: dict, target_mode: str):
    """
    target_mode:
      - "WIN21": ディーラーは 17〜20 くらい（21未満）を目指す（ユーザー21勝ち演出）
      - "LOSE": ディーラーはなるべく 21 か、または 18〜21で堅く勝つように見せる
    見た目が固定にならないように、目標値を毎回揺らす
    """
    deck = session["deck"]

    # 初期2枚は“それっぽい”ランダム
    dealer = [draw_card(deck), draw_card(deck)]

    def try_make(min_v: int, max_v: int, max_draw: int = 8):
        nonlocal dealer
        for _ in range(20):  # 作り直しリトライ
            dealer = [draw_card(deck), draw_card(deck)]
            for _d in range(max_draw):
                v = hand_value(dealer)
                if min_v <= v <= max_v:
                    return dealer
                if v > max_v:
                    break
                dealer.append(draw_card(deck))
        return dealer

    if target_mode == "WIN21":
        # 17-20のどこかに寄せる（固定にしない）
        goal = random.choice([17, 18, 19, 20])
        try_make(goal, goal)
    else:
        # 21 or 19-21のどこかに寄せる（負け演出）
        goal = random.choice([19, 20, 21, 21])
        try_make(goal, goal)

    session["dealer"] = dealer


async def bjvip_dealer_turn_and_finish(interaction: discord.Interaction, u: dict):
    session = bjvip_sessions.get(interaction.user.id)
    if not session:
        return await bj_edit(interaction, content="VIPセッションがないのだ", view=None)

    touch_bjvip_session(interaction.user.id)

    # プレイヤーの最高値（バースト除外）
    vals = [hand_value(h) for h in session["hands"]]
    safe_vals = [v for v in vals if v <= 21]
    player_best = max(safe_vals) if safe_vals else 0

    # 勝敗ターゲットを決定（あなたが設定した確率関数）
    target = vip_dealer_target_by_player_best(player_best)

    # 狙うディーラー最終値を決める
    if target == "PUSH":
        target_total = max(17, min(player_best, 21))

    elif target == "LOSE":
        # プレイヤーに勝つ → 21優先
        if player_best >= 20:
            target_total = 21
        else:
            target_total = min(21, max(17, player_best + 1))

    else:  # "WIN"
        hi = min(20, player_best - 1)
        target_total = 17 if hi < 17 else random.randint(17, hi)

    # ✅ ここで「狙って引く」
    dealer_draw_to_target(session, target_total)

    # 表示
    await bj_edit(
        interaction,
        content="💎 ディーラーのターンなのだ\n\n" + bjvip_state_text(session, show_dealer_all=True),
        view=None,
    )
    await asyncio.sleep(0.6)

    await bjvip_finish(interaction, u)

async def bjvip_finish(interaction: discord.Interaction, u: dict):
    session = bjvip_sessions.get(interaction.user.id)
    if not session:
        return await bj_edit(interaction, content="VIPセッションがないのだ", view=None)

    touch_bjvip_session(interaction.user.id)

    dealer_val = hand_value(session["dealer"])
    dealer_bust = dealer_val > 21

    payout_total = 0
    profit = 0
    results = []

    for idx, hand in enumerate(session["hands"]):
        bet = int(session["bets"][idx])
        v = hand_value(hand)

        payout = 0

        if v > 21:
            results.append(f"手札{idx+1}：負け（バースト）")
            payout = 0

        elif dealer_bust:
            # ディーラーがバーストなら勝ち
            if session["is_natural_bj"][idx]:
                payout = (bet * 5) // 2
                results.append(f"手札{idx+1}：勝ち（ディーラーバースト / BJ 3:2）")
            else:
                payout = bet * 2
                results.append(f"手札{idx+1}：勝ち（ディーラーバースト）")

        else:
            # ✅ ここが重要：比較で勝敗＆引き分け
            if v > dealer_val:
                if session["is_natural_bj"][idx]:
                    payout = (bet * 5) // 2
                    results.append(f"手札{idx+1}：勝ち（BJ 3:2）")
                else:
                    payout = bet * 2
                    results.append(f"手札{idx+1}：勝ち（VIP）")
            elif v < dealer_val:
                payout = 0
                results.append(f"手札{idx+1}：負け（VIP）")
            else:
                # ✅ 引き分けは返金
                payout = bet
                results.append(f"手札{idx+1}：引き分け（返金）")

        payout_total += payout
        profit += (payout - bet)

    just_events = set()
    async with get_user_lock(interaction.user.id):
        u["coins"] = int(u.get("coins", 0) or 0) + payout_total
        u["bj_play_count"] = int(u.get("bj_play_count", 0) or 0) + 1
        u["bj_win_streak"] = int(u.get("bj_win_streak", 0) or 0)
        u["total_earned"] = int(u.get("total_earned", 0) or 0)

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
            # 引き分けのみ（profit==0）なら streak を 0 にする/維持したいならここで調整OK
            u["bj_win_streak"] = 0

        await sheets_upsert_async(u)

    msg = (
        "💎 BJVIP 結果なのだ\n\n"
        f"ディーラー：{fmt_cards(session['dealer'])}（{dealer_val}）\n"
        + "\n".join(results)
        + f"\n\n残高：{u['coins']} コインなのだ\n\n次はどうするのだ？"
    )

    await bj_edit(interaction, content=msg, view=BJVIPEndView(interaction.user.id))

    try:
        await maybe_award_hidden_titles(interaction, u, just_events=just_events)
    except Exception:
        traceback.print_exc()

    bjvip_sessions.pop(interaction.user.id, None)


# ---------------------------------------------------------
# setup（入口メッセージだけ永久）
# ---------------------------------------------------------
@bot.tree.command(name="setup_bj", description="ブラックジャック入口メッセージを設置するのだ（最初の1回のみ）")
async def setup_bj_cmd(interaction: discord.Interaction):
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
        "※ゲーム中の操作は個人表示（ephemeral）で進むのだ\n"
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
#
# ✅ 変更点（今回）
# 1) 入札を「競り上げ式」に変更：
#    - 最大(total)に達したら即終了
#    - パスすると脱落、残り1人になったら終了
#    - 最大じゃない入札なら、まだ上げられるので「もう1巡」する
#
# 2) めくり順を変更：
#    - 落札者はまず「自分の山(pile)を残り枚数の範囲で全部めくる」
#    - 自分の山が空になってから他人の山を選べる
#
# ✅ DMスパム対策
# - _skull_broadcast は「各人の status DM を編集して更新」する
# - 操作ボタンを押したら、そのDMのボタンを消す（view=None）
# =========================================================
# =========================================================
# スカル（Skull） DMゲーム：/skull と /skullsolo
#  - DMは各プレイヤー1枚だけ（screen_msg）を編集し続ける
#  - 上部ログに「CPUの行動 / 入札 / めくり」も追記して見やすくする
#
# ✅ 追加仕様（あなたの要望）
# 1) 入札：最後の番手が最大じゃなくても入札したら、もう1巡して上書きのチャンスを作る
#    逆に、誰かが総枚数（最大）で入札した時点で即終了（そこで入札修了）
#
# 2) めくり：最初に「自分の山(自分が置いたpile)」を全部めくり切ってから、他人をめくる
#    （自分が2枚置いてたら2枚とも先にオープン→その後に他人を選べる）
# =========================================================

SKULL_SOLO_ENTRY_FEE = 50
SKULL_SOLO_WIN_REWARD = 100

NPC_ACTION_DELAY_SEC = 1.2
SKULL_VIEW_TIMEOUT_SEC = 120
SKULL_TURN_TIMEOUT_SEC = 240
SKULL_GAME_CLEANUP_SEC = 20 * 60

SKULL_LOG_MAX_LINES = 6  # 上部ログ表示の最大行数

_skull_lobbies: dict[int, dict] = {}   # lobby_message_id -> lobby dict
_skull_games: dict[str, dict] = {}     # game_id -> game dict


def _skull_now() -> float:
    return time.time()


def _skull_gid() -> str:
    return f"skull_{int(time.time()*1000)}_{random.randint(1000,9999)}"


async def dm_send_safe(user: discord.abc.User, content: str, *, view: discord.ui.View | None = None):
    try:
        if isinstance(user, (discord.User, discord.Member)):
            ch = user.dm_channel or await user.create_dm()
            return await ch.send(content, view=view)
    except Exception:
        return None


async def dm_edit_safe(msg: discord.Message, *, content: str, view: discord.ui.View | None):
    try:
        await msg.edit(content=content, view=view)
    except Exception:
        return


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


def _skull_deck_init() -> list[str]:
    return ["flower", "flower", "flower", "skull"]


def _skull_card_emoji(c: str) -> str:
    return "🌸" if c == "flower" else "💀"


def _skull_card_name(c: str) -> str:
    return "花" if c == "flower" else "スカル"


def _skull_alive_cards(p: dict) -> int:
    return len(p.get("hand", []))


def _skull_alive_players(game: dict) -> list[dict]:
    return [p for p in game["players"] if _skull_alive_cards(p) > 0 and not p.get("eliminated")]


def _skull_visible_table(game: dict) -> str:
    parts = []
    for p in game["players"]:
        score = int(p.get("score", 0) or 0)
        mark = "✅" if score >= 1 else "　"  # 1点以上でチェック
        parts.append(
            f"- {mark} {_skull_public_name(p)}：{len(p.get('pile', []))}枚（残り手札{_skull_alive_cards(p)}）"
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


def _skull_log(game: dict, line: str):
    """上部ログに追記（古いのは消す）"""
    game.setdefault("log_lines", [])
    game["log_lines"].append(str(line))
    if len(game["log_lines"]) > SKULL_LOG_MAX_LINES:
        game["log_lines"] = game["log_lines"][-SKULL_LOG_MAX_LINES:]


def _skull_screen_title(game: dict) -> str:
    if game.get("is_solo"):
        return (
            "🃏 スカル（ソロ）\n"
            f"勝てば +{SKULL_SOLO_WIN_REWARD}、負けたら0なのだ\n"
            "タイムアウト時は全額返金なのだ"
        )
    pot = int(game.get("pot", 0) or 0)
    return (
        "🃏 スカル（マルチ）\n"
        f"pot：{pot} コイン（勝者総取り）\n"
        "タイムアウト時は全額返金なのだ"
    )

async def skull_dm_get_or_create_screen(p: dict, game: dict) -> discord.Message | None:
    """各プレイヤーのDM画面を1枚だけ作り、p['screen_msg']に保持"""
    uobj = p.get("user_obj")
    if not uobj:
        return None
    msg: discord.Message | None = p.get("screen_msg")
    if msg is not None:
        return msg
    msg = await dm_send_safe(uobj, _skull_screen_title(game))
    p["screen_msg"] = msg
    return msg


def skull_build_screen_text(game: dict, *, prompt: str = "", viewer_uid: int | None = None) -> str:
    """
    1枚DM本文：タイトル + 区切り + ログ + 区切り + 現在の場 + 自分の手札 + 区切り + プロンプト
    viewer_uid: この画面を見る人（人によって手札表示が変わる）
    """
    title = _skull_screen_title(game).rstrip()

    # ---ログ---
    logs = game.get("log_lines") or []
    log_block = "\n".join(logs).strip() if logs else "（まだログはないのだ）"

    # 現在の場
    table = _skull_visible_table(game)

    # 自分の手札（花/どくろ枚数）
    hand_line = "自分の手札：-"
    if viewer_uid is not None:
        vp = _skull_player(game, int(viewer_uid))
        if vp and vp.get("type") == "human":
            hand = vp.get("hand", []) or []
            flowers = sum(1 for c in hand if c == "flower")
            skulls = sum(1 for c in hand if c == "skull")
            hand_line = f"自分の手札：🌸 花{flowers}枚 / 💀 どくろ{skulls}枚"

    # プロンプト（ここに入札やあなたの番が来る）
    prompt_block = prompt.strip() if prompt else ""

    parts = [
        title,
        "───ログ───",
        log_block,
        "────────",
        "現在の場:",
        table,
        "",
        hand_line,
    ]

    if prompt_block:
        parts += ["────────", prompt_block]

    return "\n".join(parts).strip() + "\n"

async def _skull_render_all(
    game: dict,
    *,
    actor_uid: int | None = None,
    actor_prompt: str = "",
    actor_view: discord.ui.View | None = None,
):
    for p in _skull_humans(game):
        msg = await skull_dm_get_or_create_screen(p, game)
        if not msg:
            continue

        uid = int(p["uid"])

        if actor_uid is not None and uid == int(actor_uid):
            text = skull_build_screen_text(game, prompt=actor_prompt, viewer_uid=uid)
            await dm_edit_safe(msg, content=text, view=actor_view)
        else:
            text = skull_build_screen_text(game, prompt="", viewer_uid=uid)
            await dm_edit_safe(msg, content=text, view=None)

def _skull_reset_round(game: dict):
    # pileは0に戻し、round_handを「現在の手札」から作り直す
    for p in game["players"]:
        p["pile"] = []
        p["round_hand"] = list(p.get("hand", []))  # このラウンド内で置ける残り

    game["phase"] = "place"
    game["bids"] = {}
    game["highest_bid_uid"] = None
    game["highest_bid"] = 0

    # 入札制御（新）
    game["bid_passed"] = set()
    game["bid_last_raiser_uid"] = None
    game["bid_wrapped_after_raise"] = False
    game["bid_last_action_uid"] = None

    # reveal
    game["reveals_left"] = 0
    game["reveal_target_uid"] = None

    # reveal: 自分の山を先に全部めくる
    game["reveal_must_clear_own_first"] = True

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
    _skull_log(game, f"🧾 終了：{reason}")
    await _skull_render_all(game)
    _skull_games.pop(game_id, None)


def _skull_check_auto_win(game: dict) -> tuple[bool, int | None]:
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
    total = _skull_all_placed_count(game)
    if total <= len(_skull_alive_players(game)):
        return random.random() < 0.10
    return random.random() < 0.20


def _npc_choose_bid(game: dict, p: dict) -> int:
    total = _skull_all_placed_count(game)
    if total <= 0:
        return 0
    current = int(game.get("highest_bid", 0) or 0)
    min_bid = current + 1
    if min_bid > total:
        return 0
    # NPCは最大入札しにくい
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
# DM View：配置 or 入札開始
# ---------------------------------------------------------
class SkullPlaceOrBidView(discord.ui.View):
    def __init__(self, game_id: str, actor_uid: int, *, can_start_bid: bool):
        super().__init__(timeout=SKULL_VIEW_TIMEOUT_SEC)
        self.game_id = str(game_id)
        self.actor_uid = int(actor_uid)
        self._can_start_bid = bool(can_start_bid)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return int(interaction.user.id) == self.actor_uid and self.game_id in _skull_games

    @discord.ui.button(label="🌸 花を置く", style=discord.ButtonStyle.primary)
    async def place_flower(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 押した瞬間に古いviewを消して二重操作を防ぐ
        try:
            await interaction.response.edit_message(view=None)
        except Exception:
            try:
                await interaction.response.defer()
            except Exception:
                pass
        await skull_place_card(interaction, self.game_id, self.actor_uid, "flower")

    @discord.ui.button(label="💀 スカルを置く", style=discord.ButtonStyle.danger)
    async def place_skull(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.edit_message(view=None)
        except Exception:
            try:
                await interaction.response.defer()
            except Exception:
                pass
        await skull_place_card(interaction, self.game_id, self.actor_uid, "skull")

    @discord.ui.button(label="💰 入札開始", style=discord.ButtonStyle.success)
    async def start_bid_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._can_start_bid:
            try:
                await interaction.response.send_message("まだ全員が1枚置いてないのだ", ephemeral=True)
            except Exception:
                pass
            return
        try:
            await interaction.response.edit_message(view=None)
        except Exception:
            try:
                await interaction.response.defer()
            except Exception:
                pass
        await skull_start_bidding_from_player(interaction, self.game_id, self.actor_uid)


# ---------------------------------------------------------
# DM View：入札（パス or 最高+1以上）
# ---------------------------------------------------------
class SkullBidView(discord.ui.View):
    def __init__(self, game_id: str, actor_uid: int, max_bid: int, min_bid: int):
        super().__init__(timeout=SKULL_VIEW_TIMEOUT_SEC)
        self.game_id = game_id
        self.actor_uid = int(actor_uid)
        self.max_bid = int(max_bid)
        self.min_bid = int(min_bid)

        opts = [discord.SelectOption(label="パス（0）", value="0")]
        if self.min_bid <= self.max_bid:
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
        # view消して二重操作防止
        try:
            await interaction.response.edit_message(view=None)
        except Exception:
            try:
                await interaction.response.defer()
            except Exception:
                pass
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
        try:
            await interaction.response.edit_message(view=None)
        except Exception:
            try:
                await interaction.response.defer()
            except Exception:
                pass
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

        # いったん元のbetを返金してソロfeeに切替
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

    await ch.send("✅ 募集締切：マルチで開始するのだ（DMを見るのだ）")
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
        "screen_msg": None,
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
        "bid_passed": set(),
        "bid_last_raiser_uid": None,
        "bid_wrapped_after_raise": False,
        "bid_last_action_uid": None,
        "reveals_left": 0,
        "reveal_target_uid": None,
        "reveal_must_clear_own_first": True,
        "log_lines": [],
        "last_action_ts": _skull_now(),
        "turn_deadline_ts": _skull_now() + SKULL_TURN_TIMEOUT_SEC,
        "await_kind": None,
        "await_uid": None,
        "await_ts": 0.0,
    }
    _skull_games[gid] = game

    _skull_log(game, "🟢 ソロ開始なのだ")
    _skull_reset_round(game)  # round_hand初期化など
    # reset_roundでstarter_idxが進むので、ソロ開始直後は固定にしたい場合はここで戻してもOK
    game["starter_idx"] = 0
    game["current_idx"] = 0

    await _skull_render_all(game)
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
            "screen_msg": None,
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
        "bid_passed": set(),
        "bid_last_raiser_uid": None,
        "bid_wrapped_after_raise": False,
        "bid_last_action_uid": None,
        "reveals_left": 0,
        "reveal_target_uid": None,
        "reveal_must_clear_own_first": True,
        "log_lines": [],
        "last_action_ts": _skull_now(),
        "turn_deadline_ts": _skull_now() + SKULL_TURN_TIMEOUT_SEC,
        "await_kind": None,
        "await_uid": None,
        "await_ts": 0.0,
    }
    _skull_games[gid] = game

    _skull_log(game, "🟢 マルチ開始なのだ")
    _skull_reset_round(game)
    game["starter_idx"] = 0
    game["current_idx"] = 0

    await _skull_render_all(game)
    await skull_round_start(gid)


# ---------------------------------------------------------
# ラウンド開始（配置フェーズ）
# ---------------------------------------------------------
async def skull_round_start(game_id: str):
    game = _skull_games.get(game_id)
    if not game:
        return

    ok, winner_uid = _skull_check_auto_win(game)
    if ok and winner_uid is not None:
        winner = _skull_player(game, winner_uid)
        if winner and winner.get("type") == "human":
            await _skull_payout_winner(game, winner_uid)
            _skull_log(game, f"🏆 勝者：{_skull_public_name(winner)}")
        await _skull_end_game(game_id, "最後の1人になったのだ（勝利）")
        return

    _skull_touch(game)
    game["phase"] = "place"
    game["bids"] = {}
    game["highest_bid_uid"] = None
    game["highest_bid"] = 0
    game["bid_passed"] = set()
    game["bid_last_raiser_uid"] = None
    game["bid_wrapped_after_raise"] = False
    game["bid_last_action_uid"] = None
    game["reveals_left"] = 0
    game["reveal_target_uid"] = None
    game["reveal_must_clear_own_first"] = True
    _skull_clear_await(game)

    _skull_log(game, "🔻 配置フェーズなのだ")
    await _skull_render_all(game)

    await skull_next_place_turn(game_id)


async def skull_npc_place_one(game_id: str, npc_uid: int):
    game = _skull_games.get(game_id)
    if not game:
        return
    npc = _skull_player(game, npc_uid)
    if not npc:
        return

    rh = npc.get("round_hand")
    if not rh:
        npc["round_hand"] = list(npc.get("hand", []))
        rh = npc["round_hand"]
    if not rh:
        return

    card = _npc_choose_place_card(npc)
    try:
        rh.remove(card)
    except ValueError:
        pass
    npc["round_hand"] = rh

    npc.setdefault("pile", [])
    npc["pile"].append(card)
    _skull_touch(game)

    _skull_log(game, f"📌 {_skull_public_name(npc)} が1枚置いたのだ")
    await _skull_render_all(game)
    await asyncio.sleep(NPC_ACTION_DELAY_SEC)


async def skull_next_place_turn(game_id: str):
    game = _skull_games.get(game_id)
    if not game:
        return
    _skull_touch(game)

    if _skull_now() > float(game.get("turn_deadline_ts", 0) or 0):
        await _skull_refund_all(game)
        await _skull_end_game(game_id, "タイムアウトで全額返金したのだ")
        return

    n = len(game["players"])

    for _ in range(n * 6):
        all_one = _skull_all_have_at_least_one(game)
        p = game["players"][game["current_idx"]]

        # 脱落/手札0はスキップ
        if _skull_alive_cards(p) <= 0 or p.get("eliminated"):
            game["current_idx"] = (game["current_idx"] + 1) % n
            continue

        # 人間の番
        if p["type"] == "human":
            uid = int(p["uid"])

            # ✅ 二重通知ガード：
            # 直近数秒なら「同じ案内を送り続けない」ためreturn
            # でも await が古いまま残って固まるのを防ぐため、古ければ解除して再送する
            if game.get("await_kind") == "place_or_bid" and int(game.get("await_uid") or 0) == uid:
                if _skull_now() - float(game.get("await_ts", 0) or 0) < 3.0:
                    return
                _skull_clear_await(game)  # 古いawaitは捨てて再送に進む

            can_start_bid = bool(all_one)
            view = SkullPlaceOrBidView(game_id, uid, can_start_bid=can_start_bid)

            prompt = (
                "🃏 **あなたの番なのだ**\n「1枚置く」か「入札開始」を選ぶのだ"
                if all_one
                else "🃏 **あなたの番なのだ**\nまずは最低1枚置くのだ（入札はまだできないのだ）"
            )

            await _skull_render_all(game, actor_uid=uid, actor_prompt=prompt, actor_view=view)
            _skull_set_await(game, kind="place_or_bid", uid=uid)
            game["turn_deadline_ts"] = _skull_now() + SKULL_TURN_TIMEOUT_SEC
            return

        # NPC：全員1枚後は「たまに入札開始」
        if all_one and _npc_should_start_bid(game, p):
            await skull_start_bidding_internal(game_id, starter_uid=int(p["uid"]))
            return

        # NPC：1枚置く
        await skull_npc_place_one(game_id, int(p["uid"]))
        game["current_idx"] = (game["current_idx"] + 1) % n

    # 異常系：安全に次ラウンド
    _skull_reset_round(game)
    await skull_round_start(game_id)

async def skull_place_card(interaction: discord.Interaction, game_id: str, actor_uid: int, card: str):
    game = _skull_games.get(game_id)
    if not game:
        return

    if _skull_now() > float(game.get("turn_deadline_ts", 0) or 0):
        await _skull_refund_all(game)
        await _skull_end_game(game_id, "タイムアウトで全額返金したのだ")
        return

    p = _skull_player(game, actor_uid)
    if not p or p.get("type") != "human":
        return

    if game.get("phase") != "place":
        return

    if card not in ("flower", "skull"):
        return

    rh = p.get("round_hand") or []
    if card not in rh:
        _skull_log(game, f"⚠️ {_skull_public_name(p)}：そのカードはもう置けないのだ")
        await _skull_render_all(game, actor_uid=actor_uid, actor_prompt="⚠️ そのカードはこのラウンドではもう置けないのだ", actor_view=None)
        return

    rh.remove(card)
    p["round_hand"] = rh
    p["pile"].append(card)

    _skull_clear_await(game)
    _skull_touch(game)

    _skull_log(game, f"📌 {_skull_public_name(p)} が1枚置いたのだ")
    await _skull_render_all(game)

    game["current_idx"] = (game["current_idx"] + 1) % len(game["players"])
    await skull_next_place_turn(game_id)


# ---------------------------------------------------------
# 入札開始（人間ボタン / NPC内部）
# ---------------------------------------------------------
async def skull_start_bidding_from_player(interaction: discord.Interaction, game_id: str, actor_uid: int):
    game = _skull_games.get(game_id)
    if not game:
        return

    if game.get("phase") != "place":
        return

    if not _skull_all_have_at_least_one(game):
        return

    p = _skull_player(game, actor_uid)
    if not p or p.get("type") != "human":
        return

    _skull_clear_await(game)
    _skull_log(game, f"💰 {_skull_public_name(p)} が入札開始したのだ")
    await _skull_render_all(game)
    await asyncio.sleep(0.6)

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

    # 入札制御（新）
    game["bid_passed"] = set()
    game["bid_last_raiser_uid"] = None
    game["bid_wrapped_after_raise"] = False
    game["bid_last_action_uid"] = None

    _skull_clear_await(game)

    total = _skull_all_placed_count(game)
    starter = _skull_player(game, starter_uid)
    _skull_log(game, f"💰 入札開始：開始者 {_skull_public_name(starter) if starter else starter_uid} / 総枚数 {total}")
    await _skull_render_all(game)

    # スターター位置へ
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
# 入札フェーズ（改修版：1巡保証 / 最大入札で即終了）
# ---------------------------------------------------------
def _skull_bid_active_uids(game: dict) -> list[int]:
    alive = _skull_alive_players(game)
    return [int(p["uid"]) for p in alive]

def _skull_should_end_bidding_early(game: dict) -> bool:
    """
    最高入札者以外の全員が「パス済み」になったら、即入札終了。
    ※パス=0 は game["bids"][uid]=0 で表現している前提
    """
    highest_uid = game.get("highest_bid_uid")
    highest = int(game.get("highest_bid", 0) or 0)
    if not highest_uid or highest <= 0:
        return False

    alive = _skull_alive_players(game)
    if not alive:
        return True

    # 最高入札者以外の alive が全員パス(=0)しているなら終了
    for p in alive:
        uid = int(p["uid"])
        if uid == int(highest_uid):
            continue
        if uid not in game["bids"]:
            return False  # まだ入札してない人がいる
        if int(game["bids"][uid]) != 0:
            return False  # パス以外がいる
    return True

def _skull_bid_should_finish(game: dict) -> bool:
    """終了条件：最大入札 or（最高入札者以外が全員パス）or 周回完了（raise後に一巡）"""
    total = _skull_all_placed_count(game)
    highest = int(game.get("highest_bid", 0) or 0)
    if highest >= total and total > 0:
        return True

    active = set(_skull_bid_active_uids(game))
    passed: set[int] = set(game.get("bid_passed") or set())
    highest_uid = game.get("highest_bid_uid")

    if highest_uid is not None:
        # 最高入札者以外が全員パスなら終了
        others = active - {int(highest_uid)}
        if others and others.issubset(passed):
            return True

    # raise後に一巡して最高入札者に戻ってきたら終了
    if game.get("bid_last_raiser_uid") is not None and bool(game.get("bid_wrapped_after_raise")):
        # wrappedフラグが立った状態で「次に回すべき相手が最高入札者/最後のレイザー」で来ているなら終える
        # 実際の判定は skull_next_bid_turn で行う（ここでは補助）
        return False

    return False


async def skull_next_bid_turn(game_id: str):
    game = _skull_games.get(game_id)
    if not game:
        return
    _skull_touch(game)

    if _skull_now() > float(game.get("turn_deadline_ts", 0) or 0):
        await _skull_refund_all(game)
        await _skull_end_game(game_id, "タイムアウトで全額返金したのだ")
        return

    # ✅ 追加：早期終了（最高入札者以外が全員パス）
    if _skull_should_end_bidding_early(game):
        await skull_finish_bidding(game_id)
        return

    alive = _skull_alive_players(game)

    # 全員が一度入札（パス含む）したら通常終了
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
            if game.get("await_kind") == "bid" and int(game.get("await_uid") or 0) == uid:
                # 二重通知抑止。固まり対策は place と同じ方針で必要なら入れてOK
                if _skull_now() - float(game.get("await_ts", 0) or 0) < 3.0:
                    return
                _skull_clear_await(game)

            view = SkullBidView(
                game_id,
                uid,
                max_bid=total,
                min_bid=min_bid if min_bid <= total else total + 1
            )
            txt = f"💰 あなたの入札なのだ（最大 {total} / 現在最高 {current}）\nパスするとこのラウンドは入札に戻れないのだ"
            await dm_send_safe(p["user_obj"], txt, view=view)
            _skull_set_await(game, kind="bid", uid=uid)
            game["turn_deadline_ts"] = _skull_now() + SKULL_TURN_TIMEOUT_SEC
            return

        # NPC
        bid = _npc_choose_bid(game, p)
        game["bids"][uid] = int(bid)

        if bid > int(game.get("highest_bid", 0) or 0):
            game["highest_bid"] = int(bid)
            game["highest_bid_uid"] = uid

        humans = _skull_humans(game)
        if humans:
            await npc_action_sequence(humans[0]["user_obj"], [f"🤖 {_skull_public_name(p)} は **{bid}** で入札したのだ"])

        game["current_idx"] = (game["current_idx"] + 1) % n

        # ✅ 追加：NPCの行動直後も早期終了チェック
        if _skull_should_end_bidding_early(game):
            await skull_finish_bidding(game_id)
            return

        break

    await skull_next_bid_turn(game_id)


async def skull_apply_bid_internal(game_id: str, actor_uid: int, bid: int, *, is_human: bool):
    game = _skull_games.get(game_id)
    if not game:
        return

    p = _skull_player(game, actor_uid)
    if not p:
        return

    total = _skull_all_placed_count(game)
    current = int(game.get("highest_bid", 0) or 0)
    min_bid = current + 1

    # 正規化
    bid = int(bid)

    # パス
    if bid == 0:
        game.setdefault("bid_passed", set()).add(int(actor_uid))
        _skull_log(game, f"💰 {_skull_public_name(p)}：パス(0)")
        await _skull_render_all(game)
        game["bid_last_action_uid"] = int(actor_uid)
        return

    # 不正（安全）
    if bid < min_bid or bid > total:
        # NPCは不正にならないはずだが保険
        game.setdefault("bid_passed", set()).add(int(actor_uid))
        _skull_log(game, f"⚠️ {_skull_public_name(p)}：不正入札→パス扱い")
        await _skull_render_all(game)
        game["bid_last_action_uid"] = int(actor_uid)
        return

    # 入札（raise）
    game["highest_bid"] = bid
    game["highest_bid_uid"] = int(actor_uid)
    game["bid_last_raiser_uid"] = int(actor_uid)
    game["bid_wrapped_after_raise"] = False  # raiseしたので周回フラグをリセット

    _skull_log(game, f"💰 {_skull_public_name(p)}：**{bid}** で入札")
    await _skull_render_all(game)
    game["bid_last_action_uid"] = int(actor_uid)

    # raise後、次のターンが回って「最後のレイザーに戻ってきたら終了」判定を立てるため、
    # skull_submit_bid / skull_next_bid_turn 側で current_idx更新時にフラグを立てる（下でやる）


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

    # ✅ 追加：ここで早期終了できるなら即終了
    if _skull_should_end_bidding_early(game):
        await skull_finish_bidding(game_id)
        return

    await skull_next_bid_turn(game_id)

async def skull_finish_bidding(game_id: str):
    game = _skull_games.get(game_id)
    if not game:
        return
    _skull_touch(game)

    highest_uid = game.get("highest_bid_uid")
    highest = int(game.get("highest_bid", 0) or 0)

    if not highest_uid or highest <= 0:
        _skull_log(game, "🌀 全員パスっぽいのだ…ラウンドをやり直すのだ")
        await _skull_render_all(game)
        _skull_reset_round(game)
        await skull_round_start(game_id)
        return

    # 確定ログ（入札も上部に残る）
    bidder = _skull_player(game, int(highest_uid))
    _skull_log(game, f"🏁 入札確定：{_skull_public_name(bidder) if bidder else highest_uid} が {highest} を宣言")
    await _skull_render_all(game)

    game["phase"] = "reveal"
    game["reveals_left"] = int(highest)
    game["reveal_target_uid"] = int(highest_uid)

    # ✅ 自分の山を先に全部めくる
    game["reveal_must_clear_own_first"] = True

    game["turn_deadline_ts"] = _skull_now() + SKULL_TURN_TIMEOUT_SEC
    _skull_clear_await(game)

    await skull_prompt_reveal_target(game_id)


# ---------------------------------------------------------
# めくりフェーズ（改修：自分のpileを先に全部）
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
        _skull_reset_round(game)
        await skull_round_start(game_id)
        return

    # ✅ まず自分の山(pile)を全部めくるまで強制
    if bool(game.get("reveal_must_clear_own_first")) and len(actor.get("pile", [])) > 0:
        _skull_log(game, f"🫴 {_skull_public_name(actor)}：まず自分の山をめくるのだ（残り {game['reveals_left']}）")
        await _skull_render_all(game)
        await asyncio.sleep(0.6)
        await skull_resolve_reveal(game_id, uid, uid)
        return

    # 自分の山が空になったら、他人も選べる
    if bool(game.get("reveal_must_clear_own_first")) and len(actor.get("pile", [])) == 0:
        game["reveal_must_clear_own_first"] = False

    # choices作成
    choices = []
    for p in _skull_alive_players(game):
        if len(p.get("pile", [])) > 0:
            choices.append((int(p["uid"]), _skull_public_name(p)))

    if not choices:
        _skull_log(game, "🌀 場にめくれるカードが無いのだ…ラウンドやり直しなのだ")
        await _skull_render_all(game)
        _skull_reset_round(game)
        await skull_round_start(game_id)
        return

    # NPC
    if actor["type"] == "npc":
        t_uid = _npc_choose_reveal_target(game, actor)
        t = _skull_player(game, t_uid) or {"name": str(t_uid)}
        _skull_log(game, f"🫴 {_skull_public_name(actor)} が {_skull_public_name(t)} をめくるのだ")
        await _skull_render_all(game)
        await asyncio.sleep(NPC_ACTION_DELAY_SEC)
        await skull_resolve_reveal(game_id, uid, t_uid)
        return

    # 人間：選択UI
    if game.get("await_kind") == "reveal" and int(game.get("await_uid") or 0) == uid:
        return

    view = SkullRevealTargetView(game_id, uid, choices)
    prompt = f"🫴 **めくる対象を選ぶのだ**（残り {game['reveals_left']} 枚）"
    await _skull_render_all(game, actor_uid=uid, actor_prompt=prompt, actor_view=view)
    _skull_set_await(game, kind="reveal", uid=uid)
    game["turn_deadline_ts"] = _skull_now() + SKULL_TURN_TIMEOUT_SEC


async def skull_choose_reveal_target(interaction: discord.Interaction, game_id: str, actor_uid: int, target_uid: int):
    game = _skull_games.get(game_id)
    if not game:
        return

    if _skull_now() > float(game.get("turn_deadline_ts", 0) or 0):
        await _skull_refund_all(game)
        await _skull_end_game(game_id, "タイムアウトで全額返金したのだ")
        return

    if int(game.get("reveal_target_uid")) != int(actor_uid):
        return

    actor = _skull_player(game, int(actor_uid))
    target = _skull_player(game, int(target_uid))
    if not actor or not target:
        return

    # ✅ 自分の山を先に全部めくる（未完了なら他人選択不可）
    if bool(game.get("reveal_must_clear_own_first")) and len(actor.get("pile", [])) > 0 and int(target_uid) != int(actor_uid):
        _skull_log(game, "⚠️ 先に自分の山を全部めくるのだ")
        await _skull_render_all(game, actor_uid=actor_uid, actor_prompt="⚠️ 先に自分の山を全部めくるのだ", actor_view=None)
        await asyncio.sleep(0.6)
        await skull_prompt_reveal_target(game_id)
        return

    if target.get("eliminated") or len(target.get("pile", [])) <= 0:
        _skull_log(game, "⚠️ その人の山にめくれるカードがないのだ")
        await _skull_render_all(game, actor_uid=actor_uid, actor_prompt="⚠️ その人の山にめくれるカードがないのだ", actor_view=None)
        return

    _skull_clear_await(game)

    _skull_log(game, f"🫴 {_skull_public_name(actor)} が {_skull_public_name(target)} をめくるのだ")
    await _skull_render_all(game)
    await asyncio.sleep(0.6)

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

    card = target["pile"].pop()  # 伏せ山の上から
    game["reveals_left"] -= 1

    _skull_log(
        game,
        f"🃏 めくった：{_skull_public_name(target)} → {_skull_card_emoji(card)} {_skull_card_name(card)}（残り {max(0, int(game['reveals_left']))}）"
    )
    await _skull_render_all(game)
    await asyncio.sleep(0.8)

    if card == "skull":
        _skull_log(game, f"💥 スカルを踏んだのだ！ {_skull_public_name(actor)} はペナルティなのだ")
        await _skull_render_all(game)
        await asyncio.sleep(0.8)

        if len(actor.get("hand", [])) > 0:
            lost = random.choice(actor["hand"])
            actor["hand"].remove(lost)

        if len(actor.get("hand", [])) <= 0:
            actor["eliminated"] = True
            _skull_log(game, f"🪦 {_skull_public_name(actor)} は手札0枚で脱落なのだ")
            await _skull_render_all(game)
            await asyncio.sleep(0.8)

            if game.get("is_solo"):
                # ソロ敗北
                await _skull_end_game(game_id, "ソロ敗北なのだ")
                return

        _skull_reset_round(game)
        await skull_round_start(game_id)
        return

    # まだめくり残りがある
    if int(game["reveals_left"]) > 0:
        game["turn_deadline_ts"] = _skull_now() + SKULL_TURN_TIMEOUT_SEC
        await skull_prompt_reveal_target(game_id)
        return

    # 成功：得点+1
    actor["score"] = int(actor.get("score", 0) or 0) + 1
    _skull_log(game, f"✅ 成功なのだ！ {_skull_public_name(actor)} の得点：{actor['score']}")
    await _skull_render_all(game)
    await asyncio.sleep(0.8)

    # 勝利判定（2点）
    if actor["score"] >= 2:
        if actor["type"] == "human":
            await _skull_payout_winner(game, int(actor["uid"]))
        await _skull_end_game(game_id, "ゲーム終了なのだ（2点先取）")
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
        "screen_msg": None,
    }

    await interaction.followup.send("✅ ソロを開始するのだ（DMを見るのだ）", ephemeral=True)
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

# =============================
# Dungeon Battle Core (fixed)
# =============================

dungeon_sessions: dict[int, dict] = {}  # user_id -> session

LOG_KEEP = 5
AUTO_TICK_SEC = 0.8  # ログが流れる速度（好みで調整）
dungeon_auto_tasks: dict[int, asyncio.Task] = {}


def _push_log(sess: dict, text: str):
    """ダンジョン戦闘ログを追加（最大 LOG_KEEP 件）"""
    if not sess:
        return
    logs = sess.setdefault("logs", [])
    logs.append(text)
    if len(logs) > LOG_KEEP:
        del logs[:-LOG_KEEP]


def _combat_damage(attacker_atk: int, defender_def: int) -> int:
    # 超シンプル（後で調整可）：攻撃-防御の最低1
    return max(1, int(attacker_atk) - int(defender_def))


def _speed_first(player_spd: int, enemy_spd: int) -> bool:
    # SPDが高い方が先攻。同値は50%
    if player_spd > enemy_spd:
        return True
    if player_spd < enemy_spd:
        return False
    return random.random() < 0.5


def _checkpoint_floor(floor: int) -> int:
    f = max(1, min(100, int(floor)))
    # 1,21,41,61,81 ...
    return 1 + ((f - 1) // 20) * 20


async def _finish_battle(uid: int, result: str, interaction=None):
    """
    戦闘終了処理
    - 勝利時：コイン付与＆保存
    - 敗北時：武器を失う → 初期武器に戻す
    - 進行度保存
    - 勝敗ログはここでだけ入れる（重複防止）
    - コインログは勝敗ログの一番最後
    """
    sess = dungeon_sessions.get(uid)
    if not sess:
        return

    u = store.get_user(uid)
    if not u:
        return

    if result == "win":
        enemy = sess.get("enemy", {})
        reward = _calc_dungeon_coin_reward(enemy)

        before = int(u.get("coins", 0))
        u["coins"] = before + int(reward)

        # ✅ 勝敗 → コイン（最後）
        _push_log(sess, "✅ 勝利！")
        _push_log(sess, f"💰 コインを {reward} 枚手に入れたのだ！")

    else:
        # ✅ 敗北ログ
        _push_log(sess, "💀 敗北…")
        # ✅ 武器ロスト表示（これだけ追加）
        _push_log(sess, "⚔️ 敗北したため武器を失ったのだ。")

        # ✅ 初期武器へ戻す（あなたの _ensure_user_row を利用）
        init = None
        try:
            if hasattr(store, "_ensure_user_row"):
                init = store._ensure_user_row(uid)  # あなたが貼った初期武器定義
        except Exception:
            init = None

        # _ensure_user_row が取れない環境でも落ちない保険
        if not init:
            init = {
                "weapon_name": "初期武器",
                "weapon_atk": 10,
                "weapon_def": 10,
                "weapon_spd": 10,
                "effect_type": "NONE",
                "effect_lv": 0,
                "effect_value": 0,
            }

        # ✅ セッション側を初期武器に差し替え（次戦から反映）
        sess["weapon_name"] = init["weapon_name"]
        sess["atk"] = int(init["weapon_atk"])
        sess["def"] = int(init["weapon_def"])
        sess["spd"] = int(init["weapon_spd"])
        sess["effect_type"] = init.get("effect_type", "NONE")
        sess["effect_lv"] = int(init.get("effect_lv", 0) or 0)
        sess["effect_value"] = int(init.get("effect_value", 0) or 0)

    # -----------------------------
    # ダンジョン進行保存（world/floor/hp + 可能なら武器も）
    # -----------------------------
    try:
        # ✅ dungeon_save_after_battle_async が「武器も受け取れる」実装ならここで一緒に保存される
        await dungeon_save_after_battle_async(
            uid=uid,
            world=int(sess.get("world", 1)),
            floor=int(sess.get("floor", 1)),
            hp=int(sess.get("player_hp", 0)),
            weapon_name=str(sess.get("weapon_name", "")),
            weapon_atk=int(sess.get("atk", 0)),
            weapon_def=int(sess.get("def", 0)),
            weapon_spd=int(sess.get("spd", 0)),
            effect_type=str(sess.get("effect_type", "NONE")),
            effect_lv=int(sess.get("effect_lv", 0) or 0),
            effect_value=int(sess.get("effect_value", 0) or 0),
        )
    except TypeError:
        # ✅ 既存の dungeon_save_after_battle_async が world/floor/hp しか受けない場合は従来通り保存
        try:
            await dungeon_save_after_battle_async(
                uid=uid,
                world=int(sess.get("world", 1)),
                floor=int(sess.get("floor", 1)),
                hp=int(sess.get("player_hp", 0)),
            )
        except Exception as e:
            print("[DUNGEON SAVE ERROR]", type(e).__name__, e)
    except Exception as e:
        print("[DUNGEON SAVE ERROR]", type(e).__name__, e)

    # -----------------------------
    # ユーザーデータ保存（coins反映）
    # -----------------------------
    try:
        await sheets_upsert_async(u)
    except Exception as e:
        print("[USER SAVE ERROR]", type(e).__name__, e)

    # 保険：coins だけ強制更新
    try:
        await _force_update_user_coins_async(uid, int(u.get("coins", 0)))
    except Exception as e:
        print("[COINS FORCE SAVE ERROR]", type(e).__name__, e)

    # -----------------------------
    # ユーザーデータ保存（coins/武器反映）
    # -----------------------------
    try:
        await sheets_upsert_async(u)
    except Exception as e:
        print("[USER SAVE ERROR]", type(e).__name__, e)

    # 保険：coins だけ強制更新（武器は upsert で反映させる）
    try:
        await _force_update_user_coins_async(uid, int(u.get("coins", 0)))
    except Exception as e:
        print("[COINS FORCE SAVE ERROR]", type(e).__name__, e)

def _build_battle_text(sess: dict) -> str:
    enemy = sess["enemy"]
    world = int(sess.get("world", 1))
    floor = int(sess.get("floor", 1))

    pname = sess.get("player_name", "あなた")

    logs = sess.get("logs", [])
    log_text = "\n".join(logs[-LOG_KEEP:]) if logs else "（ログなし）"

    effect = _fmt_effect(
        sess.get("effect_type", "NONE"),
        int(sess.get("effect_value", 0) or 0),
    )

    # 表示名は base_name 優先（無ければ name）
    ename = enemy.get("base_name") or enemy.get("name", "敵")

    return (
        f"現在のフロア：{world}-{floor}\n"
        f"敵：{ename}\n"
        f"HP：{int(enemy.get('hp',0))} / {int(enemy.get('max_hp',0))}\n"
        f"攻撃力：{int(enemy.get('atk',0))}\n"
        "――――――――――\n"
        f"{log_text}\n"
        "――――――――――\n"
        f"{pname}  HP：{int(sess.get('player_hp',0))} / {int(sess.get('max_hp',0))}\n"
        f"攻撃力：{int(sess.get('atk',0))}  防御力：{int(sess.get('def',0))}  素早さ：{int(sess.get('spd',0))}\n"
        f"特殊効果：{effect}\n"
    )


def _build_battle_embed(sess: dict) -> discord.Embed:
    enemy = sess.get("enemy") or {}
    url = enemy.get("image_url") or enemy.get("url")

    e = discord.Embed(
        title="🗺 ダンジョン",
        description=_build_battle_text(sess),
    )
    if url:
        e.set_thumbnail(url=url)
    return e

class DungeonAfterView(discord.ui.View):
    def __init__(self, uid: int, message: discord.Message | None = None):
        super().__init__(timeout=180)
        self.uid = uid
        self.message = message  # ✅ timeout/quit/go_next 全部で使う

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # ✅ ここで必ず message を掴む（ephemeralでもOK）
        if not getattr(self, "message", None):
            self.message = interaction.message
        return interaction.user.id == self.uid

    def _resume_floor_on_exit(self, sess: dict) -> tuple[int, int]:
        """
        ✅ 終了後に再開する (world, floor) を決定
        - 勝利時：次フロア（100Fなら次ワールドの1F）
        - 敗北時：checkpoint 済みの floor をそのまま
        """
        world = int(sess.get("world", 1) or 1)
        floor = int(sess.get("floor", 1) or 1)

        if sess.get("battle_result") == "win":
            if floor >= 100:
                return world + 1, 1
            return world, min(100, floor + 1)

        return world, floor

    async def _save_floor_on_exit(self, uid: int, sess: dict):
        """
        ✅ 終了時に (world, floor) を保存（勝利後なら次フロア or 次ワールド1F）
        """
        save_world, save_floor = self._resume_floor_on_exit(sess)

        try:
            await dungeon_save_after_battle_async(
                uid=uid,
                world=int(save_world),
                floor=int(save_floor),
                hp=int(sess.get("player_hp", 0)),
            )
        except Exception as e:
            print("[DUNGEON EXIT SAVE ERROR]", type(e).__name__, e)

        _push_log(
            sess,
            f"📍 次回は {int(save_world)}-{int(save_floor)}F から開始なのだ。",
        )

    @discord.ui.button(label="➡️ 次のフロアへ", style=discord.ButtonStyle.primary)
    async def go_next(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id

        # ✅ message を必ず掴む
        if not getattr(self, "message", None):
            self.message = interaction.message

        async with get_user_lock(uid):
            sess = dungeon_sessions.get(uid)
            if not sess:
                # interaction.response を使うのはここだけ（まだ応答してないのでOK）
                await interaction.response.edit_message(
                    content="セッションが見つからないのだ。",
                    embed=None,
                    view=None,
                )
                return

            effect_type = sess.get("effect_type", "NONE")
            effect_value = int(sess.get("effect_value", 0) or 0)

            # ✅ 直前結果を保持して解除（勝利/敗北どちらでも再戦できるように）
            prev_result = sess.get("battle_result")
            sess.pop("battle_result", None)
            sess["finished"] = False

            if prev_result == "win":
                # ✅ 100F勝利なら 次ワールド1Fへ
                cur_floor = int(sess.get("floor", 1) or 1)
                cur_world = int(sess.get("world", 1) or 1)

                if cur_floor >= 100:
                    sess["world"] = cur_world + 1
                    sess["floor"] = 1
                else:
                    sess["floor"] = cur_floor + 1

                world = int(sess.get("world", 1) or 1)
                floor = int(sess.get("floor", 1) or 1)

                debuff_zone = bool(sess.get("debuff_zone", 0))
                sess["enemy"] = generate_enemy(world, floor, debuff_zone=debuff_zone)

                # ✅ バトル開始扱い：シールドを回復
                sess["shield_now"] = int(get_player_shield_max(effect_type, effect_value))

                _push_log(sess, "➡️ 次のフロアへ進んだのだ！")
                if sess["shield_now"] > 0:
                    _push_log(sess, "🛡 シールドが全回復したのだ。")

            else:
                # ✅ 敗北時：チェックポイントに戻して再戦
                checkpoint = _checkpoint_floor(int(sess.get("floor", 1) or 1))
                sess["floor"] = checkpoint
                sess["player_hp"] = int(sess.get("max_hp", 100) or 100)

                debuff_zone = bool(sess.get("debuff_zone", 0))
                sess["enemy"] = generate_enemy(
                    int(sess.get("world", 1) or 1),
                    checkpoint,
                    debuff_zone=debuff_zone,
                )

                sess["shield_now"] = int(get_player_shield_max(effect_type, effect_value))

                _push_log(sess, f"💀 敗北したためチェックポイント（{checkpoint}F）に戻ったのだ。")
                _push_log(sess, "✨ HPを全回復したのだ。")
                if sess["shield_now"] > 0:
                    _push_log(sess, "🛡 シールドが全回復したのだ。")

            # ✅ ここで戦闘再開用の表示を確定
            embed = _build_battle_embed(sess)

        # ✅ まず「このメッセージ」を更新（interaction.responseは1回だけ安全に使う）
        # 既に応答済みの場合があるので message.edit を優先する
        try:
            if getattr(self, "message", None):
                await self.message.edit(content="", embed=embed, view=None)
            else:
                await interaction.response.edit_message(content="", embed=embed, view=None)
        except Exception as e:
            print("[GO_NEXT EDIT ERROR]", type(e).__name__, e)

        # ✅ オート再開（メッセージ基準で回すのが安定）
        old = dungeon_auto_tasks.pop(uid, None)
        if old and not old.done():
            old.cancel()

        # あなた側に _auto_battle_loop_msg(uid, message) がある構成なので、それを使う
        if getattr(self, "message", None):
            dungeon_auto_tasks[uid] = asyncio.create_task(
                _auto_battle_loop_msg(uid, self.message)
            )
        else:
            # 最低限落とさない
            print("[GO_NEXT] message is None -> cannot start _auto_battle_loop_msg")

    @discord.ui.button(label="🚪 やめる", style=discord.ButtonStyle.secondary)
    async def quit(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id

        # ✅ message を必ず掴む
        if not getattr(self, "message", None):
            self.message = interaction.message

        # ✅ オート戦闘停止（ロック外でOK）
        t = dungeon_auto_tasks.pop(uid, None)
        if t and not t.done():
            t.cancel()

        async with get_user_lock(uid):
            sess = dungeon_sessions.get(uid)
            if not sess:
                try:
                    await interaction.response.edit_message(
                        content="ダンジョンを終了したのだ。",
                        embed=None,
                        view=None,
                    )
                except Exception:
                    pass
                return

            # ✅ 終了時に次回開始地点を保存（100F勝利なら次ワールド1F）
            await self._save_floor_on_exit(uid, sess)

            _push_log(sess, "🚪 ダンジョンを終了したのだ。")
            embed = _build_battle_embed(sess)

            dungeon_sessions.pop(uid, None)

        # ✅ 終了状態に編集
        try:
            if getattr(self, "message", None):
                await self.message.edit(content="", embed=embed, view=None)
            else:
                await interaction.response.edit_message(content="", embed=embed, view=None)
        except Exception as e:
            print("[QUIT EDIT ERROR]", type(e).__name__, e)

    async def on_timeout(self):
        uid = self.uid

        # ✅ オート戦闘停止
        t = dungeon_auto_tasks.pop(uid, None)
        if t and not t.done():
            t.cancel()

        async with get_user_lock(uid):
            sess = dungeon_sessions.get(uid)
            if not sess:
                return

            await self._save_floor_on_exit(uid, sess)

            _push_log(sess, "⌛ 操作が行われなかったため、ダンジョンを終了したのだ。")
            embed = _build_battle_embed(sess)

            dungeon_sessions.pop(uid, None)

        if not getattr(self, "message", None):
            print("[DUNGEON TIMEOUT] message is None -> cannot edit")
            return

        try:
            await self.message.edit(content="", embed=embed, view=None)
        except Exception as e:
            print("[DUNGEON TIMEOUT EDIT ERROR]", type(e).__name__, e)

def _battle_one_turn(uid: int) -> str | None:
    """
    1ターン進める。
    戻り値: None=継続 / "win" / "lose"
    """
    sess = dungeon_sessions.get(uid)
    if not sess:
        return "lose"

    enemy = sess["enemy"]

    pname = sess.get("player_name", "あなた")
    ename = enemy.get("base_name") or enemy.get("name", "敵")

    # デバフのターン開始効果（毒など）＋一時ステ補正
    atk_tmp, def_tmp, spd_tmp = _apply_debuffs_start_of_turn(sess)

    def apply_damage_to_player(dmg: int):
        s = int(sess.get("shield_now", 0) or 0)
        use = min(s, dmg)
        sess["shield_now"] = s - use
        dmg -= use
        if dmg > 0:
            sess["player_hp"] = max(0, int(sess["player_hp"]) - dmg)

    def apply_damage_to_enemy(dmg: int):
        enemy["hp"] = max(0, int(enemy["hp"]) - dmg)

    # ✅ すでに終わってる（勝敗ログは出さない / 返り値だけ返す）
    if int(enemy["hp"]) <= 0:
        return "win"
    if int(sess["player_hp"]) <= 0:
        return "lose"

    player_first = _speed_first(int(spd_tmp), int(enemy["spd"]))

    def maybe_enemy_apply_debuff():
        if not bool(sess.get("debuff_zone", 0)):
            return
        r = random.random()
        if r < 0.20:
            _try_apply_debuff(sess, "poison", 20)
        elif r < 0.35:
            _try_apply_debuff(sess, "weak", 15)
        elif r < 0.50:
            _try_apply_debuff(sess, "slow", 15)

    if player_first:
        # INSTAKILL（勝利ログは出さない / 返り値だけ）
        if _roll_instakill(sess):
            enemy["hp"] = 0
            _push_log(sess, "💀 特殊効果：即死が発動したのだ！")
            _apply_heal_on_kill(sess)
            return "win"

        dmg = _combat_damage(int(atk_tmp), int(enemy["def"]))
        apply_damage_to_enemy(dmg)
        _push_log(sess, f"{pname}の攻撃！ {ename}に {dmg} ダメージ。")

        if int(enemy["hp"]) <= 0:
            _apply_heal_on_kill(sess)
            return "win"

        dmg2 = _combat_damage(int(enemy["atk"]), int(def_tmp))
        apply_damage_to_player(dmg2)
        _push_log(sess, f"{ename}の攻撃！ {pname}に {dmg2} ダメージ。")
        maybe_enemy_apply_debuff()

        if int(sess["player_hp"]) <= 0:
            return "lose"

    else:
        dmg2 = _combat_damage(int(enemy["atk"]), int(def_tmp))
        apply_damage_to_player(dmg2)
        _push_log(sess, f"{ename}の攻撃！ {pname}に {dmg2} ダメージ。")
        maybe_enemy_apply_debuff()

        if int(sess["player_hp"]) <= 0:
            return "lose"

        if _roll_instakill(sess):
            enemy["hp"] = 0
            _push_log(sess, "💀 特殊効果：即死が発動したのだ！")
            _apply_heal_on_kill(sess)
            return "win"

        dmg = _combat_damage(int(atk_tmp), int(enemy["def"]))
        apply_damage_to_enemy(dmg)
        _push_log(sess, f"{pname}の攻撃！ {ename}に {dmg} ダメージ。")

        if int(enemy["hp"]) <= 0:
            _apply_heal_on_kill(sess)
            return "win"

    return None


async def start_battle_step(interaction: discord.Interaction):
    uid = interaction.user.id

    # followup じゃなく response で “戦闘メッセ” を作る
    await interaction.response.send_message("準備中なのだ…", ephemeral=True)
    await interaction.original_response()  # msg を直接使わなくてもOK（edit_original_responseで更新）

    async with get_user_lock(uid):
        loader = globals().get("dungeon_load_user_cached_async")
        try:
            state = await loader(uid) if callable(loader) else await dungeon_load_user_async(uid)
        except Exception as e:
            await interaction.edit_original_response(
                content=f"❌ ダンジョン状態の読み込みに失敗したのだ…\n```{type(e).__name__}: {e}```",
                embed=None,
                view=None,
            )
            return

        try:
            effect_type = state.get("effect_type", "NONE")
            effect_value = int(state.get("effect_value", 0) or 0)

            max_hp = calc_player_max_hp(effect_type, effect_value)
            hp = min(int(state.get("hp", 0) or 0), int(max_hp))
            shield_max = get_player_shield_max(effect_type, effect_value)

            world = int(state.get("world", 1) or 1)
            floor = int(state.get("floor", 1) or 1)
            debuff_zone = bool(state.get("debuff_zone", 0))
        except Exception as e:
            await interaction.edit_original_response(
                content=f"❌ ステータス計算で失敗したのだ…\n```{type(e).__name__}: {e}```",
                embed=None,
                view=None,
            )
            return

        try:
            enemy = generate_enemy(world, floor, debuff_zone=debuff_zone)
        except Exception as e:
            await interaction.edit_original_response(
                content=f"❌ 敵の生成に失敗したのだ…\n```{type(e).__name__}: {e}```",
                embed=None,
                view=None,
            )
            return

        sess = {
            "world": world,
            "floor": floor,
            "player_name": interaction.user.display_name,
            "player_hp": int(hp),
            "max_hp": int(max_hp),
            "shield_now": int(shield_max),
            "atk": int(state.get("weapon_atk", 0) or 0),
            "def": int(state.get("weapon_def", 0) or 0),
            "spd": int(state.get("weapon_spd", 0) or 0),
            "effect_type": effect_type,
            "effect_value": effect_value,
            "effect_lv": int(state.get("effect_lv", 0) or 0),
            "enemy": enemy,
            "logs": ["戦闘開始なのだ！"],
            "debuff_zone": int(debuff_zone),
        }
        dungeon_sessions[uid] = sess

    embed = _build_battle_embed(dungeon_sessions[uid])
    await interaction.edit_original_response(content="", embed=embed, view=None)

    old = dungeon_auto_tasks.pop(uid, None)
    if old and not old.done():
        old.cancel()

    dungeon_auto_tasks[uid] = asyncio.create_task(
        _auto_battle_loop_interaction(uid, interaction)
    )


async def _auto_battle_loop_interaction(uid: int, interaction: discord.Interaction):
    """
    ✅ interaction は維持したまま動かす版
    - 可能なら interaction.message を直接 edit（最優先）
    - それが無理なら edit_original_response にフォールバック
    """
    async def _edit(embed: discord.Embed | None = None, view: discord.ui.View | None = None):
        # 1) ボタン押下なら interaction.message がほぼ必ずある
        msg = getattr(interaction, "message", None)
        if msg is not None:
            await msg.edit(content="", embed=embed, view=view)
            return

        # 2) それ以外（スラッシュの original_response 等）
        try:
            await interaction.edit_original_response(content="", embed=embed, view=view)
        except Exception:
            # 3) 最後の手段：未応答なら response.edit_message
            try:
                await interaction.response.edit_message(content="", embed=embed, view=view)
            except Exception as e:
                raise e

    try:
        while True:
            async with get_user_lock(uid):
                sess = dungeon_sessions.get(uid)
                if not sess:
                    return

                result = _battle_one_turn(uid)
                embed = _build_battle_embed(sess)

            # ✅ 戦闘中更新
            try:
                await _edit(embed=embed, view=None)
            except discord.HTTPException as e:
                print("[AUTO EDIT ERROR]", type(e).__name__, e)
                return

            if result in ("win", "lose"):
                # ✅ 報酬処理（勝敗ログ/コインログは _finish_battle だけが入れる前提）
                await _finish_battle(uid, result, interaction=None)

                async with get_user_lock(uid):
                    sess = dungeon_sessions.get(uid)
                    if not sess:
                        return
                    sess["battle_result"] = result
                    sess["finished"] = True
                    final_embed = _build_battle_embed(sess)

                # ✅ 最終結果を表示
                try:
                    await _edit(embed=final_embed, view=None)
                except Exception:
                    pass

                # ✅ ボタン付与（interaction.message を優先するので止まりにくい）
                try:
                    await _edit(embed=final_embed, view=DungeonAfterView(uid, message=getattr(interaction, "message", None)))
                except Exception as e:
                    print("[AFTER VIEW ERROR]", type(e).__name__, e)

                return

            await asyncio.sleep(AUTO_TICK_SEC)

    except asyncio.CancelledError:
        return

async def _force_update_user_coins_async(uid: int, coins: int):
    """
    coins が sheets_upsert_async で反映されない環境向けの保険。
    ws_users / store.ws_users / store.ws などから users ワークシートを探して coins を更新する。
    """
    ws = None

    for name in ("ws_users", "USERS_WS", "users_ws"):
        obj = globals().get(name)
        if obj is not None:
            ws = obj
            break

    if ws is None and hasattr(store, "ws_users"):
        ws = getattr(store, "ws_users")
    if ws is None and hasattr(store, "ws"):
        ws = getattr(store, "ws")

    if ws is None:
        raise RuntimeError("users ワークシート（ws_users 等）が見つからないのだ。")

    def _sync():
        headers = ws.row_values(1)
        if "user_id" not in headers or "coins" not in headers:
            raise RuntimeError(f"usersヘッダに user_id/coins が無い: {headers}")

        col_uid = headers.index("user_id") + 1
        col_coins = headers.index("coins") + 1

        uid_list = ws.col_values(col_uid)
        row = None
        s_uid = str(uid)
        for i, v in enumerate(uid_list, start=1):
            if str(v) == s_uid:
                row = i
                break
        if row is None:
            raise RuntimeError(f"user_id={uid} の行が見つからないのだ。")

        ws.update_cell(row, col_coins, str(int(coins)))

    return await asyncio.to_thread(_sync)

def _resume_floor_on_exit(sess: dict) -> int:
    """
    終了時に保存する floor を決める。
    - 直前が勝利（battle_result==win）なら「次のフロア」を保存
    - それ以外は現在の floor を保存
    """
    cur = int(sess.get("floor", 1) or 1)

    if sess.get("battle_result") == "win":
        return min(100, cur + 1)

    return cur

def _next_world_floor(world: int, floor: int) -> tuple[int, int]:
    w = int(world)
    f = int(floor)

    if f >= 100:
        return w + 1, 1
    return w, f + 1

async def _auto_battle_loop_interaction(uid: int, interaction: discord.Interaction):
    try:
        while True:
            async with get_user_lock(uid):
                sess = dungeon_sessions.get(uid)
                if not sess:
                    return

                result = _battle_one_turn(uid)
                embed = _build_battle_embed(sess)

            # ✅ 戦闘中の更新（毎ターン）
            try:
                await interaction.edit_original_response(
                    content="",
                    embed=embed,
                    view=None,
                )
            except discord.HTTPException as e:
                print("[AUTO EDIT ERROR]", type(e).__name__, e)
                return

            # ✅ 勝敗確定
            if result in ("win", "lose"):
                # 報酬・保存・ログ（勝敗ログは _finish_battle の中だけ）
                await _finish_battle(uid, result, interaction=None)

                # 戦闘終了フラグ付与＋最終表示を作成
                async with get_user_lock(uid):
                    sess = dungeon_sessions.get(uid)
                    if not sess:
                        return

                    sess["battle_result"] = result
                    sess["finished"] = True
                    final_embed = _build_battle_embed(sess)

                # ✅ 最終結果（コイン/敗北ログ込み）を表示
                try:
                    await interaction.edit_original_response(
                        content="",
                        embed=final_embed,
                        view=None,
                    )
                except Exception:
                    pass

                # ✅ 結果後ボタン（次へ/やめる）を付与
                try:
                    msg = await interaction.original_response()
                    await interaction.edit_original_response(
                        view=DungeonAfterView(uid, message=msg)
                    )
                except Exception as e:
                    print("[AFTER VIEW ERROR]", type(e).__name__, e)

                return

            await asyncio.sleep(AUTO_TICK_SEC)

    except asyncio.CancelledError:
        return

# -----------------------------
# ガチャUI（結果表示→Select→確定）
# -----------------------------
def build_gacha_embed(user: discord.User, weapons: list[dict], world: int, floor: int, is_11: bool) -> discord.Embed:
    title = "🎲 11連ガチャ結果" if is_11 else "🎲 1連ガチャ結果"
    e = discord.Embed(title=title, description=f"{user.mention} の結果（W{world}-{floor}）")
    lines = []
    for i, w in enumerate(weapons, start=1):
        lines.append(
            f"**{i}. {w['name']}**\n"
            f"ATK **{w['atk']}** / DEF **{w['def']}** / SPD **{w['spd']}**\n"
            f"効果：{_fmt_effect(w['effect_type'], w['effect_value'])}"
        )
    e.add_field(name="候補", value="\n\n".join(lines)[:1024], inline=False)
    e.set_footer(text="下のリストから装備する武器を選ぶのだ（変更しない も選べるのだ）")
    return e

class GachaCountView(discord.ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=60)
        self.uid = uid

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.uid

    @discord.ui.button(label="🎲 1回", style=discord.ButtonStyle.primary)
    async def one(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await do_gacha(interaction, 1)

    @discord.ui.button(label="🎲 11連", style=discord.ButtonStyle.success)
    async def eleven(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await do_gacha(interaction, 11)

    @discord.ui.button(label="❌ キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="ガチャをキャンセルしたのだ。", view=None)

class GachaSelectView(discord.ui.View):
    def __init__(self, *, uid: int, weapons: list[dict], on_apply_weapon):
        super().__init__(timeout=120)
        self.uid = uid
        self.weapons = weapons
        self.on_apply_weapon = on_apply_weapon

        options: list[discord.SelectOption] = []
        for idx, w in enumerate(weapons, start=1):
            label = f"{idx}. {w['name']}"
            desc = f"ATK{w['atk']} DEF{w['def']} SPD{w['spd']} / {_fmt_effect(w['effect_type'], w['effect_value'])}"
            options.append(discord.SelectOption(label=label[:100], description=desc[:100], value=str(idx)))

        options.append(discord.SelectOption(label="変更しない", description="現在の武器のままにする", value="keep"))

        sel = discord.ui.Select(
            placeholder="装備する武器を選ぶのだ（変更しない も可）",
            min_values=1,
            max_values=1,
            options=options,
        )
        sel.callback = self._on_select
        self.add_item(sel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.uid

    async def _on_select(self, interaction: discord.Interaction):
        v = interaction.data.get("values", ["keep"])[0]
    
        if v == "keep":
            chosen = None
            msg = "変更しない を選んだのだ。確定するのだ？"
        else:
            chosen = self.weapons[int(v) - 1]
            msg = (
                f"この武器に変更するのだ？\n"
                f"**{chosen['name']}**\n"
                f"ATK {chosen['atk']} / DEF {chosen['def']} / SPD {chosen['spd']}\n"
                f"効果：{_fmt_effect(chosen['effect_type'], chosen['effect_value'])}"
            )

        async def on_confirm(i: discord.Interaction, w: dict | None):
            await self.on_apply_weapon(i, w)

        # ✅ ここで「確認ボタン付きメッセージ」に更新される
        await interaction.response.edit_message(
            content=msg,
            embed=None,
            view=GachaConfirmView(uid=self.uid, chosen_weapon=chosen, on_confirm=on_confirm),
        )

class GachaConfirmView(discord.ui.View):
    def __init__(self, *, uid: int, chosen_weapon: dict | None, on_confirm):
        super().__init__(timeout=120)
        self.uid = uid
        self.chosen_weapon = chosen_weapon
        self.on_confirm = on_confirm  # async function (interaction, weapon_or_none)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.uid

    @discord.ui.button(label="✅ 確定", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)  # ← ここ“だけ”でOK
        await self.on_confirm(interaction, self.chosen_weapon)

    @discord.ui.button(label="↩ 戻る", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 「戻る」は、ひとまず閉じる（戻り先を作るなら後で拡張）
        await interaction.response.edit_message(
            content="戻ったのだ。もう一度ガチャを引き直すのだ。",
            embed=None,
            view=None,
        )

async def do_gacha(interaction: discord.Interaction, n: int):
    uid = interaction.user.id
    async with get_user_lock(uid):
        state = await dungeon_load_user_async(uid)
        weapons = [roll_weapon(state["world"], state["floor"]) for _ in range(n)]
        embed = build_gacha_embed(
            interaction.user, weapons, state["world"], state["floor"], is_11=(n == 11)
        )

        async def apply_weapon(i: discord.Interaction, w: dict | None):
            # ✅ defer は「確定ボタン側」でやってるので、ここでは絶対にしない

            if w is None:
                await i.edit_original_response(content="変更しなかったのだ。", embed=None, view=None)
                return

            await dungeon_save_weapon_async(uid, w)

            st = await dungeon_load_user_async(uid)
            new_max = calc_player_max_hp(st["effect_type"], st["effect_value"])
            new_hp = min(int(st["hp"]), int(new_max))
            if new_hp != int(st["hp"]):
                await dungeon_save_after_battle_async(uid, st["world"], st["floor"], new_hp)

            await i.edit_original_response(content=f"✅ **{w['name']}** に変更したのだ！", embed=None, view=None)

    await interaction.edit_original_response(
        content=None,
        embed=embed,
        view=GachaSelectView(uid=uid, weapons=weapons, on_apply_weapon=apply_weapon),
    )

# -----------------------------
# エントリーUI（永続）
# -----------------------------
class DungeonEntryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🏰 ダンジョンに入る", style=discord.ButtonStyle.primary, custom_id="dungeon:enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await start_battle_step(interaction)

    @discord.ui.button(label="🗡 武器確認", style=discord.ButtonStyle.secondary, custom_id="dungeon:weapon")
    async def weapon_check(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        state = await dungeon_load_user_async(uid)
        effect = _fmt_effect(state["effect_type"], int(state["effect_value"] or 0))
        msg = (
            f"🗡 **現在の武器**\n"
            f"名前：{state['weapon_name']}\n"
            f"ATK {state['weapon_atk']} / DEF {state['weapon_def']} / SPD {state['weapon_spd']}\n"
            f"特殊効果：{effect}\n"
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🎲 ガチャ", style=discord.ButtonStyle.secondary, custom_id="dungeon:gacha")
    async def gacha(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ✅ 公開メッセージは触らない。個人メッセージを作る
        await interaction.response.send_message(
            "何連ガチャを引くのだ？",
            view=GachaCountView(interaction.user.id),
            ephemeral=True,
        )
        
@bot.tree.command(name="setup_dungeon", description="ダンジョン入口メッセージを設置するのだ（管理者のみ）")
async def setup_dungeon_cmd(interaction: discord.Interaction):
    if not is_admin_user(interaction):
        await safe_send(interaction, "管理者だけ使えるのだ。", ephemeral=True)
        return

    # ✅ チャンネル制限をしてないなら、このチェック自体いらない
    # もし DUNGEON_CHANNEL_ID=None 運用なら is_in_channel は True 返す実装にするか、ここを外す
    if not is_in_channel(interaction, DUNGEON_CHANNEL_ID):
        await safe_send(interaction, "設定したチャンネルで実行するのだ。", ephemeral=True)
        return

    await safe_defer(interaction, ephemeral=True)

    embed = discord.Embed(
        title="🏰 ダンジョン",
        description="入る/次に進むで戦闘が始まるのだ。\nガチャで武器を更新できるのだ。",
    )
    msg = await interaction.channel.send(embed=embed, view=DungeonEntryView())

    # ✅ ここが「関数の中」なので await OK
    await sheets_save_config_once_async(DUNGEON_CHANNEL_ID_KEY, str(interaction.channel_id))
    await sheets_save_config_once_async(DUNGEON_MESSAGE_ID_KEY, str(msg.id))

    await safe_send(interaction, "設置したのだ。", ephemeral=True)

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

            # ✅ ダンジョンのSheets初期化（dungeonシート作成/ヘッダ整備/インデックス）
            try:
                await dungeon_init_async()
                print("DungeonStore initialized")
            except Exception as e:
                print("DungeonStore init failed:", e)
                traceback.print_exc()
                try:
                    await bot.close()
                finally:
                    os._exit(1)

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
        bot.add_view(NumaEntryView())

        # ✅ ダンジョン永続Viewを追加
        bot.add_view(DungeonEntryView())

        VIEWS_READY = True

    if not check_tasks.is_running():
        check_tasks.start()
    if not check_join_tasks.is_running():
        check_join_tasks.start()
    if not cleanup_bj_sessions.is_running():
        cleanup_bj_sessions.start()
    if not cleanup_bjvip_sessions.is_running():
        cleanup_bjvip_sessions.start()

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















































