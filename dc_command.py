import discord
from discord import app_commands, Interaction
import asyncio
import re  # 引入re模块用于URL和时间解析
from tools import download_status, get_music, music_dir, get_path, verify_name, get_music_duration, get_name, \
    get_player, check_music_open, edit_play_queue, Path
from dc_config import tree, music_choice, messages, music_player
from dc_extra import autocomplete_music_callback, ensure_voice, play_track
from downloader import download_task
from uuid import uuid4
from typing import Optional, List, Callable, Awaitable
import shutil
import os
# 【重要】导入 app 模块中的 SocketIO 相关函数，用于通知 Web 界面更新
import app
import random
from app import socketio, get_music_data, connected_sids


# =========================================================================
# === 实用工具函数 (确保命令可运行) ===
# =========================================================================

def extract_url(url: str) -> Optional[str]:
    """
    从输入字符串中提取有效的URL。
    用于 /download 命令。
    """
    # 查找以 http:// 或 https:// 开头的链接
    url_pattern = re.compile(r"https?://[^\s]+")
    match = url_pattern.search(url)
    if match:
        extracted_url = match.group(0)
        # 进一步检查是否为YouTube或Bilibili (可以根据需求调整)
        if "youtube.com" in extracted_url or "youtu.be" in extracted_url or "bilibili.com" in extracted_url:
            return extracted_url
        # 如果不是YouTube或Bilibili，但格式正确，也允许
        return extracted_url
    return None

def time_to_seconds(time_str: str) -> int:
    """
    将时间字符串转换为秒数。
    支持格式: 秒数(e.g., '90'), mm:ss (e.g., '1:30'), h:mm:ss (e.g., '1:01:30')
    用于 /play 和 /seek 命令。
    """
    if not time_str:
        return 0

    time_str = time_str.strip()
    
    try:
        # 尝试直接解析为整数秒
        return int(float(time_str))
    except ValueError:
        # 尝试解析为时间格式
        parts = time_str.split(':')
        seconds = 0
        if 1 < len(parts) <= 3:
            # mm:ss 或 h:mm:ss
            for i, part in enumerate(reversed(parts)):
                seconds += int(part) * (60 ** i)
            return seconds
        else:
            raise ValueError("无效的时间格式，请使用秒数或 mm:ss / h:mm:ss 格式。")


# =========================================================================
# === 新增命令：/refresh (手动刷新索引) ===
# =========================================================================

@tree.command(name="refresh", description="手动刷新音乐文件索引 (用于 Web 界面和命令补全)")
async def refresh_music_index(interaction: Interaction):
    """手动刷新音乐索引"""
    # 延迟响应，让用户知道操作正在进行
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        # 调用 get_music() 并传入 "force_rescan" 参数，强制重新扫描文件并更新全局索引
        get_music(check="force_rescan")

        # 通知 Web 客户端更新列表 (避免循环依赖，通过 app 模块访问)
        if app.socketio:
            music_data = app.get_music_data()
            for sid in list(app.connected_sids):
                app.socketio.emit("update_status", music_data, to=sid)

        await interaction.followup.send("✅ 音乐文件索引已成功刷新！Web 界面和命令选项已更新。", ephemeral=True)
        print("DEBUG: Music index manually refreshed.")

    except Exception as e:
        await interaction.followup.send(f"❌ 刷新音乐索引失败: {e}", ephemeral=True)
        print(f"ERROR: Failed to refresh music index: {e}")


# =========================================================================
# === /status 命令 (美化显示) ===
# =========================================================================

@tree.command(name="status", description="查看当前播放状态、音量和队列信息")
async def status_command(interaction: Interaction):
    """查看当前播放状态，美化显示"""
    await interaction.response.defer(ephemeral=False)

    player_data = get_player()

    current_path_str = player_data.get("current_path")
    current_time_str = player_data.get("current_time", "0:00")
    total_time_str = player_data.get("total_time", "0:00")
    
    # 获取当前模式的中文文本
    playback_mode_key = player_data.get('playback_mode', 'no_loop')
    playback_mode_text = messages['playback_mode'].get(playback_mode_key, '播放完停止')


    # 构造美化的响应
    response_lines = [
        f"🎧 **播放器状态**",
        f"🎶 **当前状态:** `{player_data.get('status', '空闲')}`",
        f"🔊 **音量:** `{player_data.get('current_volume', '60%') * 100:.0f}%`",
        f"🔄 **循环模式:** `{playback_mode_text}`",
        "---"
    ]

    if current_path_str and player_data.get('status') != '空闲':
        # 提取歌曲名称和播放列表名称
        current_music_name = Path(current_path_str).stem
        playlist_name = player_data.get("playlist_name")

        # 正在播放的信息
        if playlist_name:
            response_lines.append(f"📦 **播放列表:** `{playlist_name}`")
        response_lines.append(f"🎵 **正在播放:** `{current_music_name}`")
        response_lines.append(f"⏱️ **进度:** `{current_time_str} / {total_time_str}` (注意：进度显示可能不精确)")

        # 队列信息
        queue_len = len(music_player.play_queue)
        current_index = music_player.current_track_index
        if queue_len > 0:
            remaining = queue_len - (current_index + 1)
            response_lines.append(f"📑 **播放队列:** 当前第 `{current_index + 1}` 首, 剩余 `{remaining}` 首")

    elif player_data.get('status') == '空闲':
        response_lines.append("当前没有音乐在播放。")

    await interaction.followup.send("\n".join(response_lines))


# =========================================================================
# === 其他命令保持不变 ===
# =========================================================================

@tree.command(name="leave", description="离开语音频道")
async def leave(interaction: Interaction):
    try:
        vc = interaction.guild.voice_client
        if vc is not None and vc.is_connected():
            # 优化：添加停止播放确保连接关闭干净
            if vc.is_playing():
                vc.stop()
            await vc.disconnect()
            await interaction.response.send_message(f"已离开语音频道。", ephemeral=True)
        else:
            await interaction.response.send_message("当前没有连接到任何语音频道。", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"离开语音频道时出错: {e}", ephemeral=True)


@tree.command(name="download", description="下载视频为 mp3 可选播放列表")
@app_commands.describe(url="YouTube 或 Bilibili 视频链接", playlist="播放列表")
@app_commands.autocomplete(playlist=autocomplete_music_callback(include_music=False, include_playlist_music=False))
async def download_command(interaction: Interaction, url: str, playlist: Optional[str] = None):
    # 保持不变
    await interaction.response.defer(thinking=True, ephemeral=True)

    valid_url = extract_url(url)
    if not valid_url:
        await interaction.followup.send("请输入正确的视频链接。", ephemeral=True)
        return

    if playlist:
        if verify_name(playlist) != playlist:
            await interaction.followup.send("文件夹名不能包含特殊字符: <>:\"\\|?* (但允许 /)。", ephemeral=True)
            return

    try:
        task_id = uuid4().hex

        folder_path = get_path(music_dir, playlist, "%(title)s.%(ext)s") if playlist else get_path(music_dir,
                                                                                                 filename="%(title)s.%(ext)s")

        download_task.put({"id": task_id, "url": valid_url, "folder": folder_path})

        await interaction.followup.send(f"✅ 下载任务已添加！任务ID: `{task_id}`，请使用 `/download_status` 命令查看进度。",
                                        ephemeral=False)

    except Exception as e:
        await interaction.followup.send(f"❌ 添加下载任务失败: {e}", ephemeral=True)


@tree.command(name="download_status", description="查询下载进度")
@app_commands.describe(task_id="下载任务ID")
async def download_status_command(interaction: Interaction, task_id: str):
    # 保持不变
    await interaction.response.defer(thinking=True, ephemeral=False)

    status = download_status(query_id=task_id)

    if not status:
        await interaction.followup.send(f"❌ 未找到 ID 为 `{task_id}` 的下载任务或任务已完成。", ephemeral=True)
        return

    message = f"下载任务ID: `{task_id}`\n"
    if status.get("status") == "downloading":
        message += f"▶️ **状态:** 下载中\n"
        message += f"📦 **进度:** `{status.get('progress', '0.0%')}`\n"
        message += f"⏳ **预计剩余时间:** `{status.get('eta', '未知')}`\n"
    elif status.get("status") == "finished":
        # 下载完成，强制刷新索引并通知 Web 客户端
        get_music(check="force_rescan")
        if app.socketio:
            music_data = app.get_music_data()
            # 广播给所有连接的客户端
            app.socketio.emit("update_status", music_data) 

        message += f"✅ **状态:** 下载完成\n"
        message += f"📁 **文件:** `{status.get('filename')}`"
    elif status.get("status") == "error":
        message += f"❌ **状态:** 失败\n"
        message += f"⚠️ **原因:** `{status.get('message', '未知错误')}`"

    await interaction.followup.send(message)


@tree.command(name="play", description="播放音乐")
@app_commands.describe(name="歌曲或播放列表名称", seek_time="跳转时间 (例如 1:30 或 90)")
@app_commands.autocomplete(name=autocomplete_music_callback(include_music=True, include_playlist_music=True))
async def play_command(interaction: Interaction, name: str, seek_time: Optional[str] = None):
    # 保持不变
    await interaction.response.defer(thinking=True)

    try:
        vc = await ensure_voice(interaction, check_voice=True)
        if not vc:
            # ensure_voice 已经发送了错误消息
            return

        music_data = get_music()
        if not music_data:
            await interaction.followup.send("❌ 音乐库为空，请先下载音乐。", ephemeral=True)
            return

        # 1. 查找匹配的歌曲或列表
        found_item = None
        is_playlist_song = "/" in name

        if is_playlist_song:
            # 尝试匹配播放列表中的单曲
            playlist_name, song_name_stem = name.rsplit("/", 1)
            for item in music_data:
                if item["type"] == "playlist" and item["name"] == playlist_name:
                    if song_name_stem in item["music"]:
                        # 找到歌曲在列表中的索引
                        song_index = item["music"].index(song_name_stem)
                        # 构造 FoundItem 以便后续处理
                        found_item = {
                            "type": "playlist_song",
                            "name": song_name_stem,
                            "path": item["paths"][song_index],
                            "playlist_name": playlist_name
                        }
                        break

        if not found_item:
            # 尝试匹配根目录单曲或播放列表
            for item in music_data:
                if item["name"] == name:
                    found_item = item
                    break

        if not found_item:
            await interaction.followup.send(f"❌ 未找到歌曲或播放列表：`{name}`", ephemeral=True)
            return

        # 2. 设置播放队列
        music_player.play_queue = []
        music_player.current_track_index = 0

        initial_path = None

        if found_item["type"] == "playlist":
            # 播放列表
            paths = found_item["paths"]
            if not paths:
                await interaction.followup.send(f"❌ 播放列表 `{name}` 为空。", ephemeral=True)
                return

            # 设置整个播放列表为队列
            music_player.play_queue = paths

            # 修改为：播放列表默认顺序播放模式启动 (播放完停止)
            music_player.playback_mode = "no_loop"
            music_player.current_track_index = 0  # 从第一首开始顺序播放
            initial_path = music_player.play_queue[music_player.current_track_index]

            await interaction.followup.send(
                f"✅ {messages['play']['playlist']}：**{found_item['name']}**。已自动开启 **顺序播放** 模式。",
                ephemeral=False)

        else:  # 单曲或播放列表中的单曲
            if found_item["type"] == "playlist_song":
                initial_path = found_item["path"]
                music_player.play_queue.append(initial_path)
                music_player.playback_mode = "no_loop"  # 单曲默认播放完停止
                await interaction.followup.send(
                    f"✅ {messages['play']['mp3']}：**{found_item['playlist_name']}/{found_item['name']}**。",
                    ephemeral=False)
            elif found_item["type"] == "mp3":
                initial_path = found_item["paths"][0]
                music_player.play_queue.append(initial_path)
                music_player.playback_mode = "no_loop"
                await interaction.followup.send(f"✅ {messages['play']['mp3']}：**{found_item['name']}**。",
                                               ephemeral=False)

        # 3. 处理跳转
        seek_seconds = 0
        if seek_time:
            seek_seconds = time_to_seconds(seek_time)
            if seek_seconds > 0:
                music_player.manual_skip = True

        # 4. 播放
        play_track(vc, initial_path, int(seek_seconds))

    except Exception as e:
        error_msg = f"❌ 播放时发生错误: {e}"
        print(f"ERROR in play_command: {e}")
        if interaction.response.is_done():
            await interaction.followup.send(error_msg, ephemeral=True)
        else:
            await interaction.response.send_message(error_msg, ephemeral=True)


@tree.command(name="next", description="播放下一首音乐")
async def next_command(interaction: Interaction):
    # 保持不变
    await interaction.response.defer(thinking=True, ephemeral=True)

    vc = interaction.guild.voice_client
    if vc is None or not vc.is_connected():
        await interaction.followup.send("❌ Bot 未连接到语音频道。", ephemeral=True)
        return

    if not music_player.play_queue:
        await interaction.followup.send("❌ 播放队列为空。", ephemeral=True)
        return

    queue_len = len(music_player.play_queue)
    next_index = music_player.current_track_index + 1

    if music_player.playback_mode == "loop_all":
        next_index = (music_player.current_track_index + 1) % queue_len
    elif music_player.playback_mode == "shuffle":
        # 随机模式下的下一首
        if queue_len > 1:
            next_index = music_player.current_track_index
            while next_index == music_player.current_track_index:
                next_index = random.randint(0, queue_len - 1)
        else:
            next_index = 0  # 只有一首时，还是它自己
    elif next_index >= queue_len:
        # no_loop 或 loop_one，且到达队列尾
        await interaction.followup.send(messages['next_previous']['next'][0], ephemeral=True)
        # 停止播放，但不清空队列
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        music_player.current_track_index = queue_len - 1
        return

    music_player.current_track_index = next_index
    next_path = music_player.play_queue[next_index]
    play_track(vc, next_path)
    music_player.manual_skip = True  # 标记为手动跳过

    await interaction.followup.send(f"✅ {messages['next_previous']['next'][1]}：**{Path(next_path).stem}**",
                                   ephemeral=True)


@tree.command(name="previous", description="播放上一首音乐")
async def previous_command(interaction: Interaction):
    # 保持不变
    await interaction.response.defer(thinking=True, ephemeral=True)

    vc = interaction.guild.voice_client
    if vc is None or not vc.is_connected():
        await interaction.followup.send("❌ Bot 未连接到语音频道。", ephemeral=True)
        return

    if not music_player.play_queue:
        await interaction.followup.send("❌ 播放队列为空。", ephemeral=True)
        return

    queue_len = len(music_player.play_queue)

    # 随机模式无法播放“上一首”，使用普通模式逻辑
    if music_player.playback_mode == "shuffle":
        await interaction.followup.send("在随机播放模式下，无法精确播放上一首。", ephemeral=True)
        return

    previous_index = music_player.current_track_index - 1

    if previous_index < 0:
        if music_player.playback_mode == "loop_all":
            # 列表循环模式，回到队列尾
            previous_index = queue_len - 1
        else:
            # 到达队列头
            await interaction.followup.send(messages['next_previous']['previous'][0], ephemeral=True)
            # 停止播放，但不清空队列
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            music_player.current_track_index = 0
            return

    music_player.current_track_index = previous_index
    previous_path = music_player.play_queue[previous_index]
    play_track(vc, previous_path)
    music_player.manual_skip = True  # 标记为手动跳过

    await interaction.followup.send(f"✅ {messages['next_previous']['previous'][1]}：**{Path(previous_path).stem}**",
                                   ephemeral=True)


@tree.command(name="pause", description="暂停或恢复播放")
async def pause_command(interaction: Interaction):
    # 保持不变
    await interaction.response.defer(ephemeral=True)
    vc = interaction.guild.voice_client

    if vc is None or not vc.is_connected():
        await interaction.followup.send("❌ Bot 未连接到语音频道。", ephemeral=True)
        return

    if vc.is_playing():
        vc.pause()
        await interaction.followup.send(messages['pause_resume']['pause'], ephemeral=True)
    elif vc.is_paused():
        vc.resume()
        await interaction.followup.send(messages['pause_resume']['resume'], ephemeral=True)
    else:
        await interaction.followup.send("❌ 当前没有音乐在播放或暂停。", ephemeral=True)


@tree.command(name="volume", description="设置播放音量 (0-100)")
@app_commands.describe(volume="音量百分比 (0-100)")
async def volume_command(interaction: Interaction, volume: int):
    # 保持不变
    await interaction.response.defer(ephemeral=True)

    if not 0 <= volume <= 100:
        await interaction.followup.send("❌ 音量必须在 0 到 100 之间。", ephemeral=True)
        return

    music_player.current_volume = volume / 100.0

    vc = interaction.guild.voice_client
    if vc and vc.is_playing() and vc.source:
        # discord.py的FFmpegOpusAudio source有一个volume属性
        vc.source.volume = music_player.current_volume

    await interaction.followup.send(f"🔊 音量已设置为 `{volume}%`。", ephemeral=True)


@tree.command(name="mode", description="设置播放模式")
@app_commands.describe(mode="播放模式")
@app_commands.choices(mode=music_choice)
async def mode_command(interaction: Interaction, mode: app_commands.Choice[str]):
    # 保持不变
    await interaction.response.defer(ephemeral=True)

    mode_value = mode.value
    music_player.playback_mode = mode_value
    mode_text = messages['playback_mode'].get(mode_value, '未知模式')

    await interaction.followup.send(f"🔄 播放模式已设置为 **{mode_text}**。", ephemeral=True)


@tree.command(name="seek", description="跳转到歌曲指定时间")
@app_commands.describe(seek_time="跳转时间 (例如 1:30 或 90)")
async def seek_command(interaction: Interaction, seek_time: str):
    # 保持不变
    await interaction.response.defer(ephemeral=True)

    try:
        vc = interaction.guild.voice_client
        if vc is None or not vc.is_connected() or not vc.is_playing():
            await interaction.followup.send("❌ 当前没有音乐在播放，无法跳转。", ephemeral=True)
            return

        if not seek_time:
            await interaction.followup.send("请输入跳转时间，例如 90 或 1:30", ephemeral=True)
            return

        if not music_player.play_queue:
            await interaction.followup.send("播放队列为空，无法跳转。", ephemeral=True)
            return

        path = music_player.play_queue[music_player.current_track_index]
        duration_sec, _, _ = get_music_duration(path)

        seconds = 0
        try:
            seconds = time_to_seconds(seek_time)

            # 检查是否为负数或超出范围
            if seconds < 0:
                seconds = 0
            if seconds >= duration_sec:
                seconds = int(duration_sec) - 1  # 跳转到最后一秒

        except ValueError as ve:
            await interaction.followup.send(f"❌ 无效时间格式：{ve}", ephemeral=True)
            return

        min_jump = int(seconds) // 60
        sec_jump = int(seconds) % 60
        
        # 确保跳转时间不会导致播放结束，但又要接近尾声
        if seconds >= duration_sec:
            seconds = max(0, int(duration_sec) - 1)
            await interaction.followup.send(f"⚠️ 跳转时间超过歌曲长度，已自动跳转到末尾：`{min_jump} 分 {sec_jump} 秒`", ephemeral=True)
        else:
            await interaction.followup.send(f"✅ 跳转到 `{min_jump} 分 {sec_jump} 秒`", ephemeral=True)

        # 关键：调用 play_track 重新启动带 seek 参数的播放
        play_track(vc, path, int(seconds))
        music_player.manual_skip = True

    except Exception as e:
        # 如果前面 defer 成功，用 followup.send
        error_msg = f"❌ 跳转出错: {e}"
        print(f"ERROR in seek_command: {e}")
        if interaction.response.is_done():
            await interaction.followup.send(error_msg, ephemeral=True)
        else:
            await interaction.response.send_message(error_msg, ephemeral=True)
