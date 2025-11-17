import discord
from discord import app_commands, Interaction, FFmpegPCMAudio, VoiceClient
import asyncio
from typing import Optional, List, Callable, Awaitable
import random
import platform
# 导入必要的配置和工具
from dc_config import bot, music_player, messages
from tools import get_music, Path
import os

# 🚀 关键修复：解决 discord.py 中 PCMVolumeTransformer 清理时缺失 'original' 属性的 Bug，并防止递归。
try:
    # 临时保存原始的 __init__ 方法
    _original_init = discord.PCMVolumeTransformer.__init__


    # 定义新的 __init__ 方法
    def _new_init(self, original, volume=1.0):
        # 显式地调用原始的 __init__ 方法，避免递归
        _original_init(self, original, volume)

        # 确保 original 属性存在
        if not hasattr(self, 'original'):
            self.original = original


    # 用新的方法替换类的方法
    discord.PCMVolumeTransformer.__init__ = _new_init
    print("DEBUG: Applied PCMVolumeTransformer 'original' attribute fix.")
except Exception as e:
    # 如果补丁失败，打印警告，但不阻止程序运行
    print(f"CRITICAL FIX ERROR: Failed to apply discord.py PCMVolumeTransformer patch: {e}")

# --- FFMPEG Options ---
FFMPEG_BEFORE_OPTIONS = '-re'

# 根据操作系统自动切换配置
# 优化：移除冗余参数，并【添加 -bufsize 64k 作为推流缓冲】
if platform.system() == 'Windows':
    # Windows 稳定配置：强制映射音频流 (-map 0:a)，添加缓冲
    FFMPEG_OPTIONS = '-vn -map 0:a -bufsize 64k'
    print("DEBUG: FFMPEG configured for Windows (Optimized + Buffered).")
else:
    # Linux/MacOS (Posix) 稳定配置：添加缓冲和日志级别控制
    FFMPEG_OPTIONS = '-vn -map 0:a -loglevel warning -bufsize 64k'
    print(f"DEBUG: FFMPEG configured for {platform.system()} (Optimized + Buffered).")


# ---------------------------------

async def ensure_voice(interaction: Interaction, check_voice: bool = False) -> Optional[VoiceClient]:
    """确保 bot 加入语音频道"""
    try:
        voice_state = interaction.user.voice

        if voice_state is None or voice_state.channel is None:
            if interaction.response.is_done():
                await interaction.followup.send("🚨 你需要先加入一个语音频道哦！", ephemeral=True)
            else:
                await interaction.response.send_message("🚨 你需要先加入一个语音频道哦！", ephemeral=True)
            return None

        channel = voice_state.channel
        vc = interaction.guild.voice_client

        if vc is None or not vc.is_connected():
            connect_kwargs = {"reconnect": True, "timeout": 60, "self_deaf": True}
            vc = await channel.connect(**connect_kwargs)
            print(f"DEBUG: Bot successfully connected/reconnected to {channel.name}.")
            await interaction.followup.send(f"✅ 已成功加入频道: **{channel.name}** 🚀", ephemeral=True)

        elif vc.channel != channel:
            await vc.move_to(channel)
            print(f"DEBUG: Bot successfully moved to {channel.name}.")
            await interaction.followup.send(f"✅ 已移动到频道: **{channel.name}** 🎶", ephemeral=True)

        return vc
    except Exception as e:
        error_msg = f"❌ 连接语音频道时出错: {e}。请检查您的网络和全局代理设置！"
        print(f"ERROR in ensure_voice: {e}")
        if not interaction.response.is_done():
            await interaction.response.send_message(error_msg, ephemeral=True)
        else:
            await interaction.followup.send(error_msg, ephemeral=True)
        return None


def play_track(voice_client: VoiceClient, path: Path, seek_time: int = 0):
    """
    停止当前播放并开始播放新曲目。
    使用标准 FFmpegPCMAudio 实现。
    """
    # 停止当前播放，防止堆叠
    voice_client.stop()

    before_options = FFMPEG_BEFORE_OPTIONS
    if seek_time > 0:
        before_options = f'-ss {seek_time} {FFMPEG_BEFORE_OPTIONS}'

    # --- 标准路径 ---
    ffmpeg_input_path = str(path)

    print(f"\n[FINAL PLAY DEBUG] FFmpeg Input Path: {ffmpeg_input_path}")
    print(f"[FINAL PLAY DEBUG] File Exists: {path.exists()}")
    print("-" * 30)

    # 新增异步调度函数，处理播放冲突
    async def schedule_next_track_async(error):
        """异步调度下一首歌曲，在播放完成后执行"""
        if error:
            print(f"FFMPEG ERROR during playback: {error}")

        if music_player.manual_skip:
            music_player.manual_skip = False
            return

        if not music_player.play_queue:
            if voice_client and voice_client.is_playing():
                voice_client.stop()
            return

        # 1. 队列/模式逻辑 (计算下一首的索引)
        if music_player.playback_mode == "loop_one":
            pass
        elif music_player.playback_mode == "loop_all":
            music_player.current_track_index = (music_player.current_track_index + 1) % len(music_player.play_queue)
        elif music_player.playback_mode == "shuffle":
            next_index = music_player.current_track_index
            if len(music_player.play_queue) > 1:
                while next_index == music_player.current_track_index:
                    next_index = random.randint(0, len(music_player.play_queue) - 1)
            music_player.current_track_index = next_index
        elif music_player.playback_mode == "no_loop":
            if music_player.current_track_index + 1 < len(music_player.play_queue):
                music_player.current_track_index += 1
            else:
                music_player.play_queue = []
                music_player.current_track_index = 0
                if voice_client and voice_client.is_playing():
                    voice_client.stop()
                return

        # 2. 关键修复：延迟并播放下一首
        next_path = music_player.play_queue[music_player.current_track_index]
        print(f"[PLAY_TRACK DEBUG] Scheduling next track: {Path(next_path).stem}")

        # 【核心修复】强制等待 0.1 秒，确保前一首歌的 stop() 清理完成，避免 ClientException
        await asyncio.sleep(0.1)

        # 递归调用 play_track 来播放下一首
        voice_client.loop.run_in_executor(None, lambda: play_track(voice_client, next_path))

    def after_playing_callback(error):
        """播放完成后执行的回调函数 (在单独的线程中运行)"""
        # 将异步调度任务安全地提交给 Bot 的主事件循环
        coro = schedule_next_track_async(error)
        asyncio.run_coroutine_threadsafe(coro, voice_client.loop)

    # FFmpeg 音频源：标准启动方式 (使用 FFmpegPCMAudio)
    try:
        raw_source = FFmpegPCMAudio(
            source=ffmpeg_input_path,
            before_options=before_options,
            options=FFMPEG_OPTIONS,  # 使用优化后的 FFMPEG_OPTIONS
        )
    except Exception as e:
        print(f"CRITICAL ERROR: Failed to create FFmpegPCMAudio source: {e}")
        return

    # 音量控制器
    source = discord.PCMVolumeTransformer(raw_source, music_player.current_volume)

    # 播放
    voice_client.play(source, after=after_playing_callback)


def autocomplete_music_callback(include_music: bool = False, include_playlist_music: bool = False) -> Callable[
    [Interaction, str], Awaitable[List[app_commands.Choice[str]]]]:
    # 此函数保持不变
    async def autocomplete_music(interaction: Interaction, current: str) -> List[app_commands.Choice[str]]:
        """补全播放列表和音乐选项 """
        music_data = get_music()
        choices = []

        if music_data:
            current_lower = current.lower()

            for music in music_data:
                if len(choices) >= 25:
                    break

                type = music.get("type")
                name = music["name"]

                if type == "mp3" and not include_music:
                    continue

                if type == "playlist_song_temp":
                    continue

                display_name = name
                if type == "playlist":
                    display_name = f"💽 {name} (播放列表)"

                if not current or current_lower in display_name.lower() or current_lower in name.lower():
                    choices.append(app_commands.Choice(name=display_name, value=name))

                if len(choices) >= 25:
                    continue

                if type == "playlist" and include_playlist_music:
                    for song_name in music["music"]:
                        song_value = f"{music['name']}/{song_name}"
                        if not current or current_lower in song_name.lower() or current_lower in song_value.lower():
                            choices.append(app_commands.Choice(name=f"├ 🎵 {song_name}", value=song_value))

                            if len(choices) >= 25:
                                break

        return choices[:25]

    return autocomplete_music