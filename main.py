import os
import asyncio
import math
import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread
import aiohttp
import sqlite3
import csv
import io
import time
from openai import OpenAI
import os
import random


# =========================
# 設定
# =========================
JST = timezone(timedelta(hours=9))
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
client = OpenAI()
tasks_data = {}
join_tasks = {}

PLACE_LIST = [
    "パシフィック", "オイルリグ", "アーティファクト", "飛行場", "客船",
    "ユニオン", "パレト", "ボブキャット", "市長の工場"
]

# 道具 & 武器シート（CSV 出力 URL を利用）
TOOL_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRH53VZ7iL7EFXNhkGTmRBS0JdE6oAjex51ape3cqOoXnuoR7RGATJlq_TaLupYmT4YJB2Luaa5NwXx/pub?gid=0&single=true&output=csv"
WEAPON_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRH53VZ7iL7EFXNhkGTmRBS0JdE6oAjex51ape3cqOoXnuoR7RGATJlq_TaLupYmT4YJB2Luaa5NwXx/pub?gid=793378898&single=true&output=csv"


# =========================
# CSVダウンロード
# =========================
async def fetch_csv(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as r:
            text = await r.text()

    f = io.StringIO(text)
    reader = csv.DictReader(f)
    return [row for row in reader]


# CSVキャッシュ（5分有効）
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

# =========================
# SQLite 初期化
# =========================
conn = sqlite3.connect("ai_memory.db")
cur = conn.cursor()

# ユーザーごとの要約
cur.execute("""
CREATE TABLE IF NOT EXISTS user_summary (
    user_id INTEGER PRIMARY KEY,
    summary TEXT
)
""")

# 会話ログ（3件たまったら要約）
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

# =========================
# AI メモリ関係
# =========================
def save_chat(user_id: int, message: str):
    conn = sqlite3.connect("ai_memory.db")
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chat_log (user_id, message) VALUES (?, ?)",
        (user_id, message)
    )
    conn.commit()
    conn.close()


def get_recent_chats(user_id: int, limit=3):
    conn = sqlite3.connect("ai_memory.db")
    cur = conn.cursor()
    cur.execute(
        "SELECT message FROM chat_log WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
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
    cur.execute(
        "SELECT summary FROM user_summary WHERE user_id=?",
        (user_id,)
    )
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


# =========================
# ヘルパー
# =========================
def _safe_value(v):
    if v is None:
        return None
    try:
        val = getattr(v, "value", v)
    except Exception:
        val = v
    if isinstance(val, str):
        return val.strip()
    return val


def _find_option_in_data(interaction_data, name):
    if not isinstance(interaction_data, dict):
        return None

    opts = interaction_data.get("options", [])
    for opt in opts:
        if opt.get("name") == name and "value" in opt:
            return opt.get("value")
        if "options" in opt:
            v = _find_option_in_data(opt, name)
            if v is not None:
                return v
    return None
ZUNDAMON_SYSTEM = """
あなたはずんだもんです。
語尾は必ず「〜なのだ」「〜なのだよ」になります。
JSON形式では返さず、必ず普通の文章だけで返答してください。
"""

# =========================
# Discord Bot 起動
# =========================
@bot.event
async def on_ready():
    await bot.tree.sync()
    if not check_tasks.is_running():
        check_tasks.start()
    if not check_join_tasks.is_running():
        check_join_tasks.start()
    print(f"Bot logged in as {bot.user}")

# =========================
# /ai
# =========================
@bot.tree.command(name="ai", description="ずんだもんとおしゃべりするのだ")
@app_commands.describe(message="ずんだもんに話しかける内容")
async def ai_cmd(interaction: discord.Interaction, message: str):

    await interaction.response.defer(ephemeral=False)

    user_id = interaction.user.id

    # ① 会話を保存
    save_chat(user_id, message)

    # ② 過去要約を取得
    summary = get_summary(user_id)

    # ③ 直近3件を取得
    recent_chats = get_recent_chats(user_id)

    messages = [
        {"role": "system", "content": ZUNDAMON_SYSTEM},
    ]

    if summary:
        messages.append({
            "role": "system",
            "content": f"このユーザーの傾向メモ（非公開）:\n{summary}"
        })

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

        # ④ 3件たまったら要約
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
        await interaction.followup.send(
            "ごめんなのだ…今はうまく答えられないのだ 💦"
        )
        print("AI error:", e)


# =========================
# /dice（ちんちろ）
# =========================
@bot.tree.command(name="dice", description="ちんちろを振るのだ")
async def chinchiro_cmd(interaction: discord.Interaction):

    await interaction.response.defer(ephemeral=False)

    def roll_dice():
        return sorted([random.randint(1, 6) for _ in range(3)])

    def judge(dice):
        a, b, c = dice

        if dice == [1, 1, 1]:
            return "🎉 ピンゾロ"
        if dice == [1, 2, 3]:
            return "💀 ヒフミ"
        if dice == [4, 5, 6]:
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

    for i in range(1, 4):
        dice = roll_dice()
        role = judge(dice)
        dice_text = "・".join(map(str, dice))

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

# =========================
# /join
# =========================
@bot.tree.command(name="join", description="参加募集をするのだ")
@app_commands.describe(
    place="場所",
    time_str="締切時間（HH:MM）※0で時間なし",
    count="募集人数"
)
@app_commands.choices(
    place=[app_commands.Choice(name=p, value=p) for p in PLACE_LIST]
)
async def join_cmd(
    interaction: discord.Interaction,
    place: app_commands.Choice[str],
    time_str: str,
    count: int
):
    now = datetime.now(JST)

    # =========================
    # 締切時間 判定
    # =========================
    if time_str == "0":
        target_time = None
        time_text = "締切なし"
    else:
        try:
            hour, minute = map(int, time_str.split(":"))
        except ValueError:
            return await interaction.response.send_message(
                "時間は HH:MM 形式、または 0 を入力するのだ",
                ephemeral=True
            )

        target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target_time <= now:
            target_time += timedelta(days=1)

        time_text = f"{target_time.strftime('%H:%M')}〆なのだ"

    # interaction 応答（必須）
    await interaction.response.send_message("募集を開始したのだ", ephemeral=True)

    # =========================
    # 募集メッセージ
    # =========================
    msg = await interaction.channel.send(
        f"@everyone {place.value} @{count} {time_text}\n"
        f"👍で参加なのだ"
    )
    await msg.add_reaction("👍")

    join_tasks.clear()  # 1件のみ
    join_tasks[msg.id] = {
        "place": place.value,
        "time": target_time,  # None 可
        "count": count,
        "members": set(),
        "channel": interaction.channel.id,
        "message_id": msg.id
    }

    
# =========================
# /join 実行時：/time の1時間以内タスクを削除
# =========================
now = datetime.now(JST)
remove_targets = []

for name, data in tasks_data.items():
    diff = abs((data["time"] - now).total_seconds())
    if diff <= 3600:  # 1時間 = 3600秒
        remove_targets.append(name)

for name in remove_targets:
    del tasks_data[name]

# =========================
# 👍 リアクション参加
# =========================
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
    message = await channel.fetch_message(payload.message_id)

    # 募集人数到達
    if len(data["members"]) >= data["count"]:
        await channel.send(f"{data['place']} 〆なのだ")
        del join_tasks[payload.message_id]
        return

# =========================
# /jointime
# =========================
@bot.tree.command(name="jointime", description="締切なし募集に時間と人数を設定して再募集するのだ")
@app_commands.describe(
    time_str="締切時間（HH:MM）",
    count="募集人数"
)
async def jointime_cmd(
    interaction: discord.Interaction,
    time_str: str,
    count: int
):
    try:
        hour, minute = map(int, time_str.split(":"))
    except ValueError:
        return await interaction.response.send_message(
            "時間は HH:MM 形式で入力するのだ",
            ephemeral=True
        )

    now = datetime.now(JST)
    target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target_time <= now:
        target_time += timedelta(days=1)

    # 締切なし募集を探す
    no_time_task = None
    for msg_id, data in join_tasks.items():
        if data["time"] is None:
            no_time_task = msg_id
            break

    if not no_time_task:
        return await interaction.response.send_message(
            "締切なしの募集がないのだ",
            ephemeral=True
        )

    del join_tasks[no_time_task]

    # 全体再募集
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

# =========================
# /joinf
# =========================
@bot.tree.command(name="joinf", description="全ての募集を締切なのだ")
async def joinf_cmd(interaction: discord.Interaction):

    # 募集データを全削除
    join_tasks.clear()

    # チャンネルに〆だけ送信
    await interaction.channel.send("@everyone〆なのだ")

# =========================
# 時間締切監視
# =========================
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

# =========================
# /time
# =========================
@bot.tree.command(name="time", description="受注時間をセットするのだ")
@app_commands.describe(
    name="場所を選ぶのだ",
    minutes="何分後に受注が開始するのだ？"
)
@app_commands.choices(
    name=[app_commands.Choice(name=p, value=p) for p in PLACE_LIST]
)
async def time_cmd(interaction: discord.Interaction, name: app_commands.Choice[str], minutes: int):
    if minutes < 1 or minutes > 1440:
        return await interaction.response.send_message(
            "分の指定は 1〜1440 の間で入力するのだ",
            ephemeral=False
        )

    now = datetime.now(JST)
    target_time = now + timedelta(minutes=minutes)

    tasks_data[name.value] = {"time": target_time, "channel": interaction.channel.id}

    await interaction.response.send_message(
        f"{name.value} は {target_time.strftime('%H時%M分')} に受注開始なのだ。",
        ephemeral=False
    )


# =========================
# /list
# =========================
@bot.tree.command(name="list", description="現在登録されているタスクを一覧表示するのだ")
async def list_cmd(interaction: discord.Interaction):
    if not tasks_data:
        return await interaction.response.send_message(
            "現在登録されているタスクはないのだ",
            ephemeral=False
        )

    msg = "【登録タスク一覧】\n"
    for name, data in tasks_data.items():
        time_str = data["time"].strftime("%H:%M")
        msg += f"・**{name}**：{time_str}\n"

    await interaction.response.send_message(msg, ephemeral=False)


# =========================
# /reset
# =========================
@bot.tree.command(name="reset", description="登録されている全てのタスクを消すのだ")
async def reset_cmd(interaction: discord.Interaction):
    tasks_data.clear()
    await interaction.response.send_message("すべてのタスクを消したのだ", ephemeral=False)


# =========================
# /resetin
# =========================
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


# =======================================================
# /craft（カテゴリ → 種別 → アイテム）
# =======================================================
@bot.tree.command(name="craft", description="必要素材を計算して表示するのだ")
@app_commands.describe(
    category="道具 or 武器",
    type="種別を選択",
    item="作りたいアイテム",
    count="作る個数"
)
@app_commands.choices(
    category=[
        app_commands.Choice(name="道具", value="道具"),
        app_commands.Choice(name="武器", value="武器"),
    ]
)
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

    target = next(
        (row for row in sheet if (row.get(name_col) or "").replace("\u3000", "").strip() == (item or "").strip()),
        None
    )
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


# =======================================================
# Autocomplete：type
# =======================================================
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


# =======================================================
# Autocomplete：item
# =======================================================
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


# =========================
# タスク実行ループ
# =========================
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


# =========================
# Flask Keep Alive
# =========================
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!", 200


def run():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


def keep_alive():
    t = Thread(target=run)
    t.start()


# =========================
# Bot 起動
# =========================
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
    keep_alive()
    asyncio.run(start())
























































