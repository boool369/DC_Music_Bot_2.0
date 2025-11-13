import discord
from discord import app_commands, Interaction
import asyncio
from tools import download_status, get_music, music_dir, get_path, verify_name, get_music_duration, get_name, \
    get_player, check_music_open, edit_play_queue, Path
from dc_config import tree, music_choice, messages, music_player
from dc_extra import autocomplete_music_callback, ensure_voice, play_track
from downloader import download_task
from uuid import uuid4
import shutil
import os


@tree.command(name="leave", description="离开语音频道")
async def leave(interaction: Interaction):
    try:
        vc = interaction.guild.voice_client
        if vc is not None and vc.is_connected():
            await vc.disconnect()
            await interaction.response.send_message(f"已离开语音频道。", ephemeral=True)
        else:
            await interaction.response.send_message("当前没有连接到任何语音频道。", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"离开语音频道时出错: {e}", ephemeral=True)


@tree.command(name="download", description="下载视频为 mp3 可选播放列表")
@app_commands.describe(url="YouTube 或 Bilibili 视频链接", playlist="播放列表")
@app_commands.autocomplete(playlist=autocomplete_music_callback())
async def download(interaction: Interaction, url: str, playlist: str = None):
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)

        if playlist:
            # playlist 现在是完整相对路径 (e.g., 'RJ1473335/mp3')，这里只需要验证，verify_name 已经修改为允许 /
            if verify_name(playlist) != playlist:
                await interaction.followup.send('文件夹名不能包含特殊字符: `<>:"\\|?*`')
                return

        id = uuid4().hex
        # get_path 自动处理 music_dir
        folder_path = get_path(music_dir, playlist, "%(title)s.%(ext)s") if playlist else get_path(music_dir,
                                                                                                   filename="%(title)s.%(ext)s")

        download_task.put({"id": id, "url": url, "folder": folder_path})
        message = await interaction.followup.send("处理中...")

        dots = ["", ".", "..", "..."]
        dot_index = 0

        while True:
            await asyncio.sleep(0.01)
            data = download_status(query_id=id)
            if data is None:
                dot_index = (dot_index + 1) % len(dots)
                # 关键修改：使用 follow.up.send/edit 而不是 response.send/edit
                await message.edit(content=f"处理中{dots[dot_index]}")
                continue

            status = data.get("status")
            extra = data.get("extra")
            title = data.get("title")

            if status == "error":
                await message.edit(content=f"错误: {extra}")
                return
            elif status == "downloading":
                filled = int(extra / 10)
                bar = "[" + "█" * filled + "░" * (10 - filled) + "]"
                await message.edit(content=(f"{playlist} / " if playlist else "") + f"{title} : {bar} {extra:.0f}%")
                if extra == 100:
                    # title 需要清理非法字符
                    cleaned_title = verify_name(title)
                    final_path = get_path(music_dir, playlist, f"{cleaned_title}.mp3")
                    edit_play_queue(final_path, cleaned_title, playlist)
                    return
    except Exception as e:
        await interaction.followup.send(f"下载时出错: {e}", ephemeral=True)


@tree.command(name="music_view", description="查看所有音乐和播放列表中的歌曲")
async def music_view(interaction: Interaction):
    try:
        music_data = get_music()
        if not music_data:
            await interaction.response.send_message("当前没有任何音乐或播放列表。", ephemeral=True)
            return

        msg_lines = ["**音乐列表**"]
        for music in music_data:
            type = music.get("type")
            name = music.get("name")  # 播放列表的完整相对路径 或 根目录歌曲名

            if type == "playlist":
                # 只显示播放列表的路径和歌曲数，防止消息过长
                msg_lines.append(f"\n**{name}** ({len(music['music'])} 首)")
            else:
                msg_lines.append(f"- **{name}** (单曲)")

        await interaction.response.send_message("\n".join(msg_lines), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"查看音乐和播放列表时出错: {e}", ephemeral=True)


@tree.command(name="delete_music", description="删除单曲、播放列表中的单曲或整个播放列表")
@app_commands.describe(name="要删除的音乐或播放列表")
@app_commands.autocomplete(name=autocomplete_music_callback(True, True))
async def delete_music(interaction: Interaction, name: str):
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)

        music_data = get_music()
        if not music_data:
            await interaction.followup.send(f"未找到 `{name}`", ephemeral=True)
            return

        is_playlist = any(m["name"] == name and m["type"] == "playlist" for m in music_data)
        is_root_song = any(m["name"] == name and m["type"] == "mp3" for m in music_data)
        is_playlist_song = "/" in name

        # 删除整个播放列表（完整路径）
        if is_playlist:
            if check_music_open(name):
                await interaction.followup.send(f"`{name}` 在播放中，使用 /leave 后才可以删除", ephemeral=True)
                return

            path = get_path(music_dir, subfolder=name)
            shutil.rmtree(path)
            edit_play_queue(playlist=name)
            await interaction.followup.send(f"已成功删除播放列表 `{name}`", ephemeral=True)

        # 删除根目录单曲
        elif is_root_song:
            if check_music_open(name):
                await interaction.followup.send(f"`{name}` 在播放中，使用 /leave 后才可以删除", ephemeral=True)
                return

            path = get_path(music_dir, filename=f"{name}.mp3")
            os.remove(path)
            edit_play_queue(music=path, music_name=name, playlist=None)
            await interaction.followup.send(f"已成功删除单曲 `{name}`", ephemeral=True)

        # 删除播放列表中的单曲 (格式: 列表路径/歌曲名)
        elif is_playlist_song:
            playlist_name, song_name = name.rsplit("/", 1)

            if check_music_open(song_name):
                await interaction.followup.send(f"`{song_name}` 在播放中，使用 /leave 后才可以删除", ephemeral=True)
                return

            path = get_path(music_dir, playlist_name, f"{song_name}.mp3")
            os.remove(path)
            # edit_play_queue 传入 Path 对象，和单曲所在的播放列表名
            edit_play_queue(music=path, music_name=song_name, playlist=playlist_name)
            await interaction.followup.send(f"已成功删除播放列表 `{playlist_name}` 中的歌曲 `{song_name}`",
                                            ephemeral=True)

        else:
            await interaction.followup.send(f"未找到 `{name}` 或格式错误", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"删除音乐时出错: {e}", ephemeral=True)


@tree.command(name="player_view", description="查看当前音乐播放器状态")
async def player_view(interaction: Interaction):
    try:
        player_data = get_player()
        music = player_data.get("current_music")

        if not music:
            await interaction.response.send_message("当前没有正在播放的音乐。", ephemeral=True)
            return

        msg_lines = ["**播放器状态**"]
        msg_lines.append(f"- 当前曲目: `{music}`")
        msg_lines.append(f"- 当前音量: `{player_data['current_volume']}`")
        msg_lines.append(f"- 播放模式: `{player_data['playback_mode']}`")

        playlist = player_data.get("playlist_name")
        if playlist:
            msg_lines.append(f"- 当前播放列表: `{playlist}`")

        queue = player_data.get("play_queue")
        if queue:
            msg_lines.append("\n**播放队列:**")
            for i, track in enumerate(queue, start=1):
                msg_lines.append(f"{i}. `{track}`")

        await interaction.response.send_message("\n".join(msg_lines), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"查看播放器状态时出错: {e}", ephemeral=True)


@tree.command(name="music", description="控制音乐播放、音量、切歌等操作")
@app_commands.choices(action=music_choice)
@app_commands.describe(action="播放控制操作", name="歌曲或播放列表名称", volume_level="音量 0~100",
                       seek_time="跳转到指定时间，格式为秒或 mm:ss")
@app_commands.autocomplete(name=autocomplete_music_callback(include_music=True))
async def music_control(interaction: Interaction, action: app_commands.Choice[str], name: str = None,
                        volume_level: app_commands.Range[int, 0, 100] = None, seek_time: str = None):
    try:
        value = action.value
        # 统一在命令开始时 defer，以防 ensure_voice 失败
        await interaction.response.defer(ephemeral=True, thinking=True)

        vc = await ensure_voice(interaction, value not in ["play"])
        if vc is None:
            return

        if value == "play":
            # name 是播放列表的完整路径 (e.g., 'RJ1473335/mp3') 或单曲名 (e.g., 'root_song')
            if verify_name(name) != name:  # verify_name 现在返回清理后的字符串，用于检查是否包含非法字符
                await interaction.followup.send('文件名不能包含特殊字符: `<>:"\\|?*`')
                return

            music_data = get_music(name)
            if not music_data:
                await interaction.followup.send(f"未找到 `{name}`", ephemeral=True)
                return

            else:
                data = music_data[0]
                music_paths = data.get("paths")  # 获取 Path 对象列表
                music_names = data.get("music")

                if not music_paths:
                    await interaction.followup.send(f"播放列表 `{name}` 中没有找到任何音乐", ephemeral=True)
                    return

                music_player.play_queue = music_paths  # 设置 Path 对象到播放队列
                music_player.current_track_index = 0

                # 获取第一首歌信息 (Path 对象)
                current_track_path = music_player.play_queue[0]
                current_track_name = music_names[0] if data.get("type") == "playlist" else data.get("name")

                _, minutes, sec = get_music_duration(current_track_path)
                play_track(vc, current_track_path)

                type = data.get("type")

                if type == "playlist":
                    response_msg = f"正在播放播放列表 `{name}` / 歌曲 `{current_track_name}`"
                else:  # mp3
                    response_msg = f"正在播放歌曲 `{name}`"

                await interaction.followup.send(response_msg + f"，{minutes} 分 {sec} 秒", ephemeral=True)

        elif value in ["pause", "resume"]:
            vc.pause() if value == "pause" else vc.resume()
            await interaction.followup.send(f"{messages['pause_resume'][value]}播放", ephemeral=True)

        elif value in ["next", "previous"]:
            if not music_player.play_queue:
                await interaction.followup.send("播放队列为空，无法切歌！", ephemeral=True)
                return

            # 先更新索引
            if value == "next" and music_player.current_track_index + 1 < len(music_player.play_queue):
                music_player.current_track_index += 1
            elif value == "previous" and music_player.current_track_index > 0:
                music_player.current_track_index -= 1
            else:
                await interaction.followup.send(f"已经是{messages['next_previous'][value][0]}一首了！", ephemeral=True)
                return

            # 获取新歌曲 Path
            current_track_path = music_player.play_queue[music_player.current_track_index]
            _, minutes, sec = get_music_duration(current_track_path)

            play_track(vc, current_track_path)
            music_player.manual_skip = True
            await interaction.followup.send(
                f"{messages['next_previous'][value][1]}: `{current_track_path.stem}`，{minutes} 分 {sec} 秒",
                ephemeral=True)

        elif value == "volume":
            if volume_level is None:
                await interaction.followup.send("请提供 0 到 100 之间的音量值！", ephemeral=True)
                return
            music_player.current_volume = volume_level / 100
            if vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
                vc.source.volume = music_player.current_volume
            await interaction.followup.send(f"音量已设置为 {volume_level}%", ephemeral=True)

        elif value in ["loop_one", "loop_all", "shuffle", "no_loop"]:
            music_player.playback_mode = value
            await interaction.followup.send(f"已设置为{messages['playback_mode'][value]}模式", ephemeral=True)

        elif value == "seek":
            if seek_time is None:
                await interaction.followup.send("请输入跳转时间，例如 90 或 1:30", ephemeral=True)
                return

            if not music_player.play_queue:
                await interaction.followup.send("播放队列为空，无法跳转。", ephemeral=True)
                return

            path = music_player.play_queue[music_player.current_track_index]
            duration_sec, _, _ = get_music_duration(path)

            seconds = 0
            try:
                if ":" in seek_time:
                    mins, secs = map(int, seek_time.strip().split(":"))
                    seconds = mins * 60 + secs
                else:
                    seconds = int(seek_time)
            except ValueError:
                await interaction.followup.send("无效时间格式，请输入秒数或 mm:ss 格式。", ephemeral=True)
                return

            if seconds >= duration_sec:
                seconds = int(duration_sec) - 1

            min_jump = seconds // 60
            sec_jump = seconds % 60
            await interaction.followup.send(f"跳转到 `{min_jump} 分 {sec_jump} 秒`", ephemeral=True)
            play_track(vc, path, seconds)
            music_player.manual_skip = True

    except Exception as e:
        # 如果前面 defer 成功，用 followup.send
        if interaction.response.is_done():
            await interaction.followup.send(f"处理音乐控制时出错: {e}", ephemeral=True)
        else:  # 否则用 response.send
            await interaction.response.send_message(f"处理音乐控制时出错: {e}", ephemeral=True)