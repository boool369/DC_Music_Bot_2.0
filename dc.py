# dc.py

import asyncio
import threading
from dc_config import bot, token
import discord # 确保导入 discord 以便处理异常

# 注意：dc_command 和 dc_event 不在此处导入，由 app.py 负责。

async def run_bot():
    """在 asyncio 循环中运行 Discord Bot"""
    print("DEBUG: 正在启动 Discord Bot...")
    try:
        # 直接使用 bot.start(token) 是最稳定的连接方式
        await bot.start(token)
    except discord.errors.LoginFailure as e:
        print(f"CRITICAL ERROR: Discord Token 登录失败，请检查 .env 中的 DISCORD_BOT_TOKEN: {e}")
    except Exception as e:
        print(f"ERROR: Discord Bot 运行失败: {e}")
    finally:
        # bot.start 阻塞直到 bot 停止
        print("DEBUG: Discord Bot 已停止。")


def start_bot():
    """Bot 线程的入口函数"""
    try:
        asyncio.run(run_bot())
    except Exception as e:
        print(f"ERROR: 启动 Discord Bot 失败 (线程/环境问题): {e}")


def start():
    """供外部（app.py）调用的启动 Bot 线程的函数"""
    print("DEBUG: 启动 Bot 线程。")
    threading.Thread(target=start_bot, daemon=True).start()