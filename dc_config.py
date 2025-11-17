import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import os

load_dotenv()
token = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.all()

# --- 核心：确保 Bot 主连接使用全局代理 ---
proxy_url = os.getenv("PROXY_URL")
bot_kwargs = {"command_prefix": "/", "intents": intents}

if proxy_url:
    # 启用主连接代理，用于 Bot 启动和接收/发送命令
    bot_kwargs["proxy"] = proxy_url
    print(f"✅ Bot 主连接已配置全局代理: {proxy_url}")
else:
    print("⚠️ 未检测到 PROXY_URL，Bot 将尝试直连。")

bot = commands.Bot(**bot_kwargs)
tree = bot.tree


class MusicPlayer:
    def __init__(self):
        """音乐播放器核心状态"""
        self.play_queue = []
        self.current_track_index = 0
        self.current_volume = 0.60
        self.playback_mode = "no_loop"
        self.manual_skip = False


music_player = MusicPlayer()

# --- 优化后的提示消息字典 ---
messages = {
    "play": {
        "mp3": "正在播放单曲",
        "playlist": "正在加载播放列表"
    },
    "pause_resume": {
        "pause": "⏸️ 已暂停",
        "resume": "▶️ 继续播放"
    },
    "next_previous": {
        "next": ["队列尾", "下一首 ⏭️"],
        "previous": ["队列头", "上一首 ⏮️"]
    },
    "playback_mode": {
        "loop_one": "🔂 单曲循环",
        "loop_all": "🔁 列表循环",
        "shuffle": "🔀 随机播放",
        "no_loop": "➡️ 播放完停止",
    }
}

music_choice = [
    app_commands.Choice(name="播放 🎶", value="play"),
    app_commands.Choice(name="暂停 ⏸️", value="pause"),
    app_commands.Choice(name="恢复 ▶️", value="resume"),
    app_commands.Choice(name="下一首 ⏭️", value="next"),
    app_commands.Choice(name="上一首 ⏮️", value="previous"),
    app_commands.Choice(name="音量 🔊", value="volume"),
    app_commands.Choice(name="跳转 🕒", value="seek"),
    app_commands.Choice(name="单曲循环 🔂", value="loop_one"),
    app_commands.Choice(name="列表循环 🔁", value="loop_all"),
    app_commands.Choice(name="随机播放 🔀", value="shuffle"),
    app_commands.Choice(name="不循环 ➡️", value="no_loop"),
]

voice_timeout_tasks = {}