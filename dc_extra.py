import discord
from discord import app_commands, Interaction, FFmpegPCMAudio, VoiceClient
import asyncio
from typing import Optional, List, Callable, Awaitable
import random
# 导入必要的配置和工具
from dc_config import bot, music_player, messages
from tools import get_music, Path, music_dir

# --- FFMPEG Options ---
FFMPEG_BEFORE_OPTIONS = '-re'
# 关键修复：移除 -ac/-ar 警告，并增加 -bufsize 64k 以优化播放流畅度
FFMPEG_OPTIONS = '-vn -bufsize 64k -loglevel warning'


# ---------------------------------

async def ensure_voice(interaction: Interaction, check_voice: bool = False) -> Optional[VoiceClient]:
    """确保 bot 加入语音频道"""
    try:
        # 移除 interaction.response.defer()，由 dc_command.py 负责 defer
        voice_state = interaction.user.voice

        if voice_state is None or voice_state.channel is None:
            await interaction.followup.send("你需要先加入一个语音频道！", ephemeral=True)
            return None

        channel = voice_state.channel
        vc = interaction.guild.voice_client

        if vc is None or not vc.is_connected():
            vc = await channel.connect()
            await interaction.followup.send(f"已加入频道: {channel.name}", ephemeral=True)
        elif vc.channel != channel:
            await vc.move_to(channel)
            await interaction.followup.send(f"已移动到频道: {channel.name}", ephemeral=True)

        if check_voice and (not vc.is_playing() and not vc.is_paused()):
            await interaction.followup.send("当前没有正在播放的音频。", ephemeral=True)
            return None

        return vc
    except Exception as e:
        if not interaction.response.is_done():
            await interaction.response.send_message(f"连接语音频道时出错: {e}", ephemeral=True)
        else:
            await interaction.followup.send(f"连接语音频道时出错: {e}", ephemeral=True)
        return None


def play_track(voice_client: VoiceClient, path: Path, seek_time: int = 0):
    """
    停止当前播放并开始播放新曲目，使用 FFmpeg 优化配置。
    注意：此函数是同步的。
    """
    voice_client.stop()

    before_options = FFMPEG_BEFORE_OPTIONS
    if seek_time > 0:
        before_options = f'-ss {seek_time} {FFMPEG_BEFORE_OPTIONS}'

    # 路径使用 str() 转换，并使用新的 FFMPEG_OPTIONS
    source = FFmpegPCMAudio(
        source=str(path),
        before_options=before_options,
        options=FFMPEG_OPTIONS
    )

    source = discord.PCMVolumeTransformer(source, music_player.current_volume)

    def after_playing_callback(error):
        """
        播放完成后执行的回调函数 (在单独的线程中运行)
        """
        if music_player.manual_skip:
            music_player.manual_skip = False
            return

        if not music_player.play_queue:
            return

        # 1. 队列/模式逻辑
        if music_player.playback_mode == "loop_one":
            pass  # 索引不变

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
                music_player.play_queue = []  # 队列结束，清空
                music_player.current_track_index = 0
                return  # 结束播放

        # 2. 调度下一首歌的播放到主线程的执行器
        next_path = music_player.play_queue[music_player.current_track_index]

        # 使用 run_in_executor 包装同步的 play_track 调用，并用 run_coroutine_threadsafe 调度
        # 修复了 Future exception was never retrieved 错误
        asyncio.run_coroutine_threadsafe(
            bot.loop.run_in_executor(None, lambda: play_track(voice_client, next_path)),
            bot.loop
        )

        if error:
            print(f"播放时发生错误: {error}")

    # 修复：直接将 after_playing_callback 作为 after 参数传入
    voice_client.play(source, after=after_playing_callback)


def autocomplete_music_callback(include_music: bool = False, include_playlist_music: bool = False) -> Callable[
    [Interaction, str], Awaitable[List[app_commands.Choice[str]]]]:
    """autocomplete_music 回调函数"""

    async def autocomplete_music(interaction: Interaction, current: str) -> List[app_commands.Choice[str]]:
        """补全播放列表和音乐选项 """
        music_data = get_music()
        choices = []

        if music_data:
            for music in music_data:
                type = music.get("type")
                name = music["name"]  # Name is the full relative path (e.g., 'RJ1473335/mp3') or single song name

                if type == "mp3" and not include_music:
                    continue

                if type == "playlist_song_temp":
                    continue

                display_name = name
                if type == "playlist":
                    # 显示完整的相对路径，模拟层级感
                    display_name = f"{name} (播放列表)"

                # 1. 添加播放列表/根目录 mp3
                if current.lower() in display_name.lower() or current.lower() in name.lower():
                    choices.append(app_commands.Choice(name=display_name, value=name))

                # 2. 添加播放列表中的单曲 (主要用于 /delete_music)
                if type == "playlist" and include_playlist_music:
                    for song_name in music["music"]:
                        # 歌曲选项的 value 格式为 '列表路径/歌曲名'
                        song_value = f"{music['name']}/{song_name}"
                        if current.lower() in song_name.lower() or current.lower() in song_value.lower():
                            choices.append(app_commands.Choice(name=f"{music['name']}/{song_name}", value=song_value))

        return choices[:25]  # Discord limit

    return autocomplete_music