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
import csv
import io
import time

# =========================
# 設定
# =========================
JST = timezone(timedelta(hours=9))
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

tasks_data = {}
PLACE_LIST = [
    "パシフィック", "オイルリグ", "アーティファクト",
    "飛行場", "客船", "ユニオン", "パレト", "ボブキャット","市長の工場","アート"
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
# ヘルパー
# =========================
def _safe_value(v):
    if v is None: return None
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

# =========================
# Discord Bot 起動
# =========================
@bot.event
async def on_ready():
    await bot.tree.sync()
    if not check_tasks.is_running():
        check_tasks.start()
    print(f"Bot logged in as {bot.user}")

# =========================
# /time
# =========================
@bot.tree.command(name="time", description="受注時間をセットする")
@app_commands.describe(
    name="場所を選択してください",
    minutes="何分後に受注が開始しますか？"
)
@app_commands.choices(
    name=[app_commands.Choice(name=p, value=p) for p in PLACE_LIST]
)
async def time_cmd(interaction: discord.Interaction, name: app_commands.Choice[str], minutes: int):
    if minutes < 1 or minutes > 1440:
        return await interaction.response.send_message(
            "分の指定は 1〜1440 の間で入力してください。",
            ephemeral=True
        )
    now = datetime.now(JST)
    target_time = now + timedelta(minutes=minutes)
    tasks_data[name.value] = {"time": target_time, "channel": interaction.channel.id}
    await interaction.response.send_message(
        f"{name.value} は {target_time.strftime('%H時%M分')} に受注開始です。",
        ephemeral=True
    )

# =========================
# /list
# =========================
@bot.tree.command(name="list", description="現在登録されているタスクを一覧表示します")
async def list_cmd(interaction: discord.Interaction):
    if not tasks_data:
        return await interaction.response.send_message("現在登録されているタスクはありません。", ephemeral=True)
    msg = "【登録タスク一覧】\n"
    for name, data in tasks_data.items():
        time_str = data["time"].strftime("%H:%M")
        msg += f"・**{name}**：{time_str}\n"
    await interaction.response.send_message(msg, ephemeral=True)

# =========================
# /reset
# =========================
@bot.tree.command(name="reset", description="登録されている全てのタスクを削除します")
async def reset_cmd(interaction: discord.Interaction):
    tasks_data.clear()
    await interaction.response.send_message("すべてのタスクを削除しました。", ephemeral=True)

# =========================
# /resetin
# =========================
@bot.tree.command(name="resetin", description="特定のタスクを選択して削除します")
@app_commands.describe(name="削除するタスク名を入力してください")
async def resetin_cmd(interaction: discord.Interaction, name: str):
    if name not in tasks_data:
        return await interaction.response.send_message("そのタスクは存在しません。", ephemeral=True)
    del tasks_data[name]
    await interaction.response.send_message(f"**{name}** を削除しました。", ephemeral=True)

@resetin_cmd.autocomplete("name")
async def autocomplete_name(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=n, value=n)
        for n in tasks_data.keys()
        if current.lower() in n.lower()
    ]

# =========================
# /craft
# =========================
@bot.tree.command(name="craft", description="必要素材を計算して表示します")
@app_commands.describe(
    category="道具 or 武器",
    type="種別を選択",
    item="作りたいアイテム",
    count="作る個数"
)
@app_commands.choices(
    category=[
        app_commands.Choice(name="道具", value="道具"),
        app_commands.Choice(name="武器", value="武器")
    ]
)
async def craft_cmd(interaction: discord.Interaction, category: app_commands.Choice[str], type: str, item: str, count: int):
    await interaction.response.defer(ephemeral=True)
    sheet = await get_csv(category.value)
    if not sheet:
        return await interaction.followup.send("シートの読み込みに失敗しました。")
    
    def find_col(cols, target):
        for c in cols:
            if c is None: continue
            if target in c.replace("\u3000", "").strip():
                return c
        return None

    columns = sheet[0].keys()
    name_col = find_col(columns, "名前")
    make_col = find_col(columns, "１回での作成個数")
    if not name_col:
        return await interaction.followup.send("シートに '名前' 列が見つかりません。")

    target = next((row for row in sheet if (row.get(name_col) or "").replace("\u3000","").strip() == (item or "").strip()), None)
    if not target:
        return await interaction.followup.send("そのアイテムはシートにありません。")

    make_per_once = float(target.get(make_col, "1") or 1)
    craft_times = math.ceil(count / make_per_once)

    msg = f"### **{item} を {count}個 作るための必要素材**\n"
    msg += f"作成回数：**{craft_times} 回**\n\n"
    for key, value in target.items():
        if key in (name_col, make_col, "種別"): continue
        try: v = float(value)
        except Exception: continue
        if v <= 0: continue
        need = v * craft_times
        if float(need).is_integer(): need = int(need)
        msg += f"- {key}：{need}\n"
    await interaction.followup.send(msg)

# =========================
# Autocomplete type
# =========================
@craft_cmd.autocomplete("type")
async def autocomplete_type(interaction: discord.Interaction, current: str):
    category = _find_option_in_data(interaction.data, "category")
    if category == "道具":
        types = ["小型", "大型", "その他"]
    elif category == "武器":
        types = ["弾", "武器", "アタッチメント", "その他"]
    else:
        types = ["小型", "大型", "その他", "弾", "武器", "アタッチメント"]
    filtered = [t for t in types if current.lower() in t.lower()]
    return [app_commands.Choice(name=t, value=t) for t in filtered[:25]]

# =========================
# Autocomplete item
# =========================
@craft_cmd.autocomplete("item")
async def autocomplete_item(interaction: discord.Interaction, current: str):
    category = _find_option_in_data(interaction.data, "category")
    type_sel = _find_option_in_data(interaction.data, "type")
    urls = []
    if category == "道具": urls = ["道具"]
    elif category == "武器": urls = ["武器"]
    else: urls = ["道具","武器"]
    
    candidates = []
    def normalize(s): return "" if s is None else str(s).replace("\u3000","").strip().lower()
    
    for cat in urls:
        sheet = await get_csv(cat)
        if not sheet: continue
        name_col = next((c for c in sheet[0].keys() if "名前" in c.replace("\u3000","").strip()), None)
        type_col = next((c for c in sheet[0].keys() if "種別" in c.replace("\u3000","").strip()), None)
        if not name_col or not type_col: continue
        for row in sheet:
            row_name = (row.get(name_col) or "").replace("\u3000","").strip()
            row_type = row.get(type_col)
            if not row_name: continue
            if not type_sel or normalize(row_type) == normalize(type_sel):
                candidates.append(row_name)
    
    if current:
        candidates = [n for n in candidates if current.lower() in n.lower()]
    return [app_commands.Choice(name=n, value=n) for n in candidates[:25]]

# =========================
# タスク実行ループ
# =========================
@tasks.loop(minutes=1)
async def check_tasks():
    now = datetime.now(JST)
    remove_list = []
    for name, data in tasks_data.items():
        notify_time = data["time"] - timedelta(minutes=15)
        if notify_time <= now:
            channel = bot.get_channel(data["channel"])
            if channel:
                await channel.send(f"@here **{name}** の受注15分前です！")
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
