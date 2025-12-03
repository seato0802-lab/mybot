import os
import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread

JST = timezone(timedelta(hours=9))
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

tasks_data = {}
PLACE_LIST = [
    "パシフィック", "オイルリグ", "アーティファクト",
    "飛行場", "客船", "ユニオン", "パレト", "ボブキャット"
]

# --- Discord Bot ---
@bot.event
async def on_ready():
    await bot.tree.sync()
    if not check_tasks.is_running():
        check_tasks.start()
    print(f"Bot logged in as {bot.user}")

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
    tasks_data[name.value] = {
        "time": target_time,
        "channel": interaction.channel.id
    }
    await interaction.response.send_message(
        f"{name.value} は {target_time.strftime('%H時%M分')} に受注開始です。"
    )
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

@bot.tree.command(name="list", description="現在登録されているタスクを一覧表示します")
async def list_cmd(interaction: discord.Interaction):
    if not tasks_data:
        return await interaction.response.send_message("現在登録されているタスクはありません。")

    msg = "【登録タスク一覧】\n"
    for name, data in tasks_data.items():
        time_str = data["time"].strftime("%H:%M")
        msg += f"・**{name}**：{time_str}\n"

    await interaction.response.send_message(msg)

@bot.tree.command(name="reset", description="登録されている全てのタスクを削除します")
async def reset_cmd(interaction: discord.Interaction):
    tasks_data.clear()
    await interaction.response.send_message("すべてのタスクを削除しました。")

@bot.tree.command(name="resetin", description="特定のタスクを選択して削除します")
@app_commands.describe(name="削除するタスク名を選択してください")
@app_commands.choices(name=lambda: [
    app_commands.Choice(name=n, value=n) for n in tasks_data.keys()
])
async def resetin_cmd(interaction: discord.Interaction, name: app_commands.Choice[str]):
    if name.value not in tasks_data:
        return await interaction.response.send_message("そのタスクは既に存在しません。")

    del tasks_data[name.value]
    await interaction.response.send_message(f"**{name.value}** を削除しました。")


# --- keep_alive (Flask) ---
app = Flask('')
@app.route('/')
def home():
    return "Bot is running!", 200
def run():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
def keep_alive():
    t = Thread(target=run)
    t.start()

# --- Bot start ---
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



