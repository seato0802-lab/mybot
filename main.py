import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timedelta

# Bot の設定
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# タスク一覧（名前ごとに管理）
tasks_data = {}  # { name: {"time": datetime, "channel": channel_id} }

# 起動時にスラッシュコマンドを同期
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")

# /time コマンド
@bot.tree.command(name="time", description="受注時間をセットする")
async def time_cmd(interaction: discord.Interaction, name: str, minutes: int):
    now = datetime.now()
    target_time = now + timedelta(minutes=minutes)

    # 名前ごとのタスク保存
    tasks_data[name] = {
        "time": target_time,
        "channel": interaction.channel.id
    }

    # 表示用の時刻整形
    time_str = target_time.strftime("%H時%M分")

    await interaction.response.send_message(
        f"**{name}** は **{time_str}** に受注開始です。"
    )

# 1分ごとにタスクを確認
@tasks.loop(minutes=1)
async def check_tasks():
    now = datetime.now()

    to_remove = []

    for name, data in tasks_data.items():
        notify_time = data["time"] - timedelta(minutes=15)

        # 時間になったら通知
        if notify_time <= now:
            channel = bot.get_channel(data["channel"])
            if channel:
                await channel.send(f"@here **{name}** の受注15分前です！")
            to_remove.append(name)

    # 発火したタスクを削除
    for name in to_remove:
        del tasks_data[name]

# Bot 起動時にループ開始
@bot.event
async def on_ready():
    check_tasks.start()
    print(f"Bot logged in as {bot.user}")

# 起動
bot.run(os.getenv("DISCORD_TOKEN"))
