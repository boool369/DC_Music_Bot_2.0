from typing import Optional, List, Dict, Union
from pathlib import Path
import os
import subprocess
import re
import time
from dc_config import messages, music_player
import psutil
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

downloaded = []
# 从 .env 读取 MUSIC_DIR
music_dir = os.getenv("MUSIC_DIR", "mp3")


def download_status(status: Optional[Dict[str, Union[str, float]]] = None, query_id: Optional[str] = None) -> Optional[
    Dict[str, Union[str, float]]]:
    """记录或按 ID 查询下载进度，自动清除超时项"""
    now = time.time()

    global downloaded
    downloaded = [
        item for item in downloaded
        if isinstance(item.get("timestamp"), (int, float)) and now - item["timestamp"] < 300
    ]

    if status:
        status["timestamp"] = now
        downloaded.append(status)
    elif query_id:
        for i, item in enumerate(downloaded):
            if item.get("id") == query_id:
                return downloaded.pop(i)
    return None


def get_music(check: Optional[str] = None) -> Optional[List[Dict[str, Union[str, list]]]]:
    """返回播放列表和音乐 (使用 rglob 递归查找多媒体文件)"""
    music = []
    music_path = Path(music_dir)

    # 支持的音频文件后缀
    AUDIO_EXTENSIONS = ['*.mp3', '*.ogg', '*.opus', '*.webm']

    if music_path.exists():
        playlists = {}
        root_music = []

        # rglob 递归查找
        for ext in AUDIO_EXTENSIONS:
            for item in music_path.rglob(ext):
                if item.is_file():
                    try:
                        relative_path = item.relative_to(music_path)
                    except ValueError:
                        continue

                    if len(relative_path.parts) == 1:
                        # 根目录下的文件
                        root_music.append({"path": item, "name": item.stem})
                    elif len(relative_path.parts) >= 2:
                        # 使用包含音乐文件的父目录的完整相对路径作为播放列表名
                        parent_dir_path = str(item.parent.relative_to(music_path)).replace(os.path.sep, '/')
                        song_name = item.stem

                        if parent_dir_path not in playlists:
                            playlists[parent_dir_path] = []

                        # 无论文件在多深的子目录，都归入这个父目录播放列表
                        playlists[parent_dir_path].append({"path": item, "name": song_name})

        # 1. 构造播放列表数据
        for name, songs in playlists.items():
            songs.sort(key=lambda x: x["name"])
            paths = [song["path"] for song in songs]

            music.append({
                "type": "playlist",
                "name": name,  # 完整路径，例如 'RJ1473335/mp3'
                "music": [song["name"] for song in songs],
                "paths": paths
            })

        # 2. 构造根目录音乐数据
        root_music.sort(key=lambda x: x["name"])
        for song in root_music:
            music.append({
                "type": "mp3",
                "name": song["name"],
                "music": [song["name"]],
                "paths": [song["path"]]
            })

        # 3. 过滤 (如果提供了 check)
        if check:
            filtered_music = []

            # 尝试匹配播放列表名或根目录音乐名 (check 现在只需要是 'RJ1473335/mp3' 或 'root_song')
            for m in music:
                if m["name"] and check.lower() == m["name"].lower():
                    filtered_music.append(m)
                    break

            # 尝试匹配播放列表中的单曲 (name: "播放列表名/歌曲名")
            if not filtered_music:
                if "/" in check:
                    # 仅分割一次
                    playlist_check, song_check = check.rsplit("/", 1)
                else:
                    playlist_check, song_check = None, check

                for m in music:
                    # 只有当 check 中包含 / 且 playlist_check 匹配时才播放单曲
                    if m["type"] == "playlist" and playlist_check and playlist_check.lower() == m[
                        "name"].lower() and song_check.lower() in [s.lower() for s in m["music"]]:
                        try:
                            index = [s.lower() for s in m["music"]].index(song_check.lower())
                            song_name = m["music"][index]

                            filtered_music.append({
                                "type": "playlist_song_temp",
                                "name": song_name,
                                "playlist_folder": m["name"],  # 播放列表的完整路径
                                "music": [song_name],
                                "paths": [m["paths"][index]]
                            })
                        except ValueError:
                            pass
                        break

            return filtered_music if filtered_music else None

        return music
    return None


def get_path(folder: Optional[str] = None, subfolder: Optional[str] = None, filename: Optional[str] = None) -> Path:
    """返回合成的路径 (使用 Path 对象连接)"""
    base_path = Path(folder) if folder else Path('.')
    parts = [part for part in [subfolder, filename] if part]
    return base_path.joinpath(*parts)


def verify_name(name: str) -> str:
    """清理文件名或文件夹名中的非法字符，允许 / 作为路径分隔符"""
    # 允许 / 作为分隔符，只清理其他非法字符
    name = re.sub(r'[<>:"\\|?*]', '', name)
    return name.strip()


def get_name(name: str) -> str:
    """获取不带扩展和路径名称"""
    itemname = os.path.basename(name)
    return os.path.splitext(itemname)[0]


def get_music_duration(filepath: Path) -> tuple[float, int, int]:
    """使用 ffprobe 获取音频长度"""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(filepath)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT
    )
    total_seconds = float(result.stdout)
    minutes = int(total_seconds) // 60
    seconds = int(total_seconds) % 60
    return total_seconds, minutes, seconds


def get_player() -> dict[str, Union[str, list[str], None]]:
    """获取播放器状态"""
    first_item = music_player.play_queue[music_player.current_track_index] if music_player.play_queue else None

    # 从 Path 中提取播放列表名（完整相对路径）
    from_playlist = first_item and first_item.parent != Path(music_dir)

    if from_playlist and first_item:
        try:
            # 播放列表名现在是文件父目录的完整相对路径 (e.g., 'RJ1473335/mp3')
            playlist_name = str(first_item.parent.relative_to(Path(music_dir))).replace(os.path.sep, '/')
        except ValueError:
            playlist_name = None
    else:
        playlist_name = None

    player_data = {
        # play_queue 现在只返回歌曲名
        "play_queue": [p.stem for p in music_player.play_queue] if from_playlist else None,
        # 播放列表名现在是完整路径
        "playlist_name": playlist_name,
        "current_music": first_item.stem if first_item else None,
        "playback_mode": messages['playback_mode'][music_player.playback_mode],
        "current_volume": f"{int(music_player.current_volume * 100)}%"
    }
    return player_data


def check_music_open(name: str) -> bool:
    """检查音乐是否被占用 (使用新的播放列表逻辑)"""
    player_data = get_player()
    current_music = player_data.get("current_music")
    playlist_name = player_data.get("playlist_name")

    if current_music:
        # 检查是否正在播放该播放列表（完整路径）或该单曲
        if playlist_name == name or current_music == name:
            # 简化检查，仅检查播放器状态
            return True
    return False


def edit_play_queue(music: Optional[Path] = None, music_name: Optional[str] = None, playlist: Optional[str] = None):
    """修改播放队列 (使用新的播放列表逻辑)"""

    if not music_player.play_queue:
        return

    # 1. 删除单个文件 (Path 对象比较)
    if music and music in music_player.play_queue:
        music_player.play_queue.remove(music)

    # 2. 删除整个播放列表 (清除队列中属于该播放列表的所有歌曲)
    elif playlist:  # playlist 现在是完整相对路径 (e.g., 'RJ1473335/mp3')
        # 匹配播放列表名（完整相对路径）
        music_player.play_queue = [
            p for p in music_player.play_queue
            if str(p.parent.relative_to(Path(music_dir))).replace(os.path.sep, '/') != playlist
        ]

    # 删除后重置索引
    if music_player.play_queue:
        music_player.current_track_index = min(music_player.current_track_index, len(music_player.play_queue) - 1)
    else:
        music_player.current_track_index = 0