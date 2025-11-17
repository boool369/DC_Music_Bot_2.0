from typing import Optional, List, Dict, Union
from pathlib import Path
import os
import subprocess
import re
import time
from dc_config import messages, music_player
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# --- 全局变量和缓存 ---
downloaded = []
# 从 .env 读取 MUSIC_DIR
music_dir = os.getenv("MUSIC_DIR", "mp3")

# --- 索引缓存（实现启动时索引和手动刷新）---
_music_cache: List[Dict[str, Union[str, list]]] = []
_last_scan_time: float = 0


# --------------------


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


def get_path(root_dir: str, subfolder: Optional[str] = None, filename: Optional[str] = None) -> Path:
    """构建 Path 对象"""
    p = Path(root_dir)
    if subfolder:
        p /= subfolder
    if filename:
        p /= filename
    return p


def verify_name(name: str) -> str:
    """验证文件名或路径中是否存在非法字符"""
    # 非法字符：< > : " | ? *
    name = re.sub(r'[<>:"|?*]', '', name)
    name = name.strip()
    return name


def time_to_seconds(time_str: str) -> float:
    """将 mm:ss 或 s 转换为总秒数"""
    if not time_str:
        return 0.0
    if ':' in time_str:
        parts = time_str.split(':')
        if len(parts) == 2:
            minutes, seconds = map(float, parts)
            return minutes * 60 + seconds
        elif len(parts) == 3:
            hours, minutes, seconds = map(float, parts)
            return hours * 3600 + minutes * 60 + seconds
    try:
        return float(time_str)
    except ValueError:
        return 0.0


def get_music_duration(file_path: Path) -> tuple[float, str, str]:
    """获取音乐时长（秒、mm:ss 格式、h:mm:ss 格式）"""
    try:
        # 使用 ffprobe 获取时长
        command = [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            str(file_path)
        ]
        # 在 Windows 上隐藏命令行窗口
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        result = subprocess.run(command, capture_output=True, text=True, check=True, creationflags=creationflags)
        duration_sec = float(result.stdout.strip())

        # 格式化
        duration_int = int(duration_sec)
        h = duration_int // 3600
        m = (duration_int % 3600) // 60
        s = duration_int % 60

        mm_ss = f"{m:d}:{s:02d}"
        h_mm_ss = f"{h:d}:{m:02d}:{s:02d}" if h > 0 else mm_ss

        return duration_sec, mm_ss, h_mm_ss

    except Exception:
        # 错误时返回默认值
        return 0.0, "0:00", "0:00"


def get_name(path: Path) -> str:
    """从路径获取歌曲或列表名称"""
    relative_path = path.relative_to(Path(music_dir))
    if len(relative_path.parts) > 1:
        # 播放列表歌曲: "列表名/歌曲名"
        return f"{relative_path.parts[0]}/{path.stem}"
    else:
        # 根目录歌曲: "歌曲名"
        return path.stem


# --- 优化后的 get_music 函数 ---
def get_music(check: Optional[str] = None) -> Optional[List[Dict[str, Union[str, list]]]]:
    """
    返回播放列表和音乐 (使用 rglob 递归查找多媒体文件)。
    如果 check="force_rescan"，则强制重新扫描文件系统。
    """
    global _music_cache, _last_scan_time

    # 检查是否需要强制重新扫描
    if check != "force_rescan" and _music_cache:
        # 如果不是强制刷新，且缓存不为空，则直接返回缓存
        return _music_cache

    music = []

    # 清空缓存，准备重新扫描
    _music_cache = []
    _last_scan_time = time.time()

    music_path = Path(music_dir)
    if not music_path.exists():
        print(f"WARNING: Music directory {music_dir} does not exist.")
        return None

    # rglob 递归查找所有 .mp3, .m4a, .flac 文件
    all_files = list(music_path.rglob('*.mp3')) + list(music_path.rglob('*.m4a')) + list(music_path.rglob('*.flac'))

    playlists = {}

    for file_path in all_files:
        # 计算相对于 music_dir 的路径
        try:
            relative_path = file_path.relative_to(music_path)
        except ValueError:
            # 文件不在 music_dir 下，跳过
            continue

        # 检查是否在子文件夹中（即播放列表）
        if len(relative_path.parts) > 1:
            # 播放列表：文件夹名是播放列表名
            playlist_name = relative_path.parts[0]
            song_name = file_path.stem

            if playlist_name not in playlists:
                playlists[playlist_name] = {
                    "type": "playlist",
                    "name": playlist_name,
                    "music": [],  # 歌曲名称列表 (仅文件名)
                    "paths": []  # 歌曲的绝对路径列表 (Path对象)
                }

            playlists[playlist_name]["music"].append(song_name)
            playlists[playlist_name]["paths"].append(file_path)

        else:
            # 根目录歌曲 (单曲)
            music.append({
                "type": "mp3",
                "name": file_path.stem,
                "paths": [file_path]  # 单曲的绝对路径
            })

    # 将播放列表添加到结果中
    music.extend(list(playlists.values()))

    # 更新缓存
    _music_cache = music

    # 打印日志
    print(f"DEBUG: Music index refreshed. Found {len(music)} items (including playlists).")

    return music


# ----------------------------------------------------


def get_player() -> Dict[str, Union[str, int]]:
    """获取播放器状态"""
    # 假设 music_player.bot 已经从 dc_config 正确导入
    vc = None
    if music_player.play_queue and hasattr(music_player, 'bot') and music_player.bot:
        try:
            # 尝试通过 guild_id 获取 voice_client，但这行代码依赖于 music_player 中存储 guild_id
            # 简化：直接尝试从 bot.voice_clients 中查找
            if music_player.bot.voice_clients:
                vc = music_player.bot.voice_clients[0]
        except Exception:
            pass  # 无法获取 vc

    current_time_str = "0:00"
    total_time_str = "0:00"
    current_path = None
    status = "空闲"
    playlist_name = None
    current_music = None

    if music_player.play_queue:
        current_path = music_player.play_queue[music_player.current_track_index]
        current_music = current_path.stem

        # 尝试获取播放列表名
        try:
            relative_path = current_path.relative_to(Path(music_dir))
            if len(relative_path.parts) > 1:
                playlist_name = relative_path.parts[0]
        except ValueError:
            pass

        if vc and vc.is_playing():
            status = "播放中"
        elif vc and vc.is_paused():
            status = "暂停"

        # 只有在播放或暂停时才计算时间
        if status != "空闲":
            _, total_time_str, _ = get_music_duration(current_path)
            # 注意：实际播放进度在 discord.py 中难以准确获取，这里保持简化
            current_time_str = "0:00"

            if not total_time_str:
                total_time_str = "0:00"

    # 格式化播放模式
    mode_text = messages['playback_mode'].get(music_player.playback_mode, '未知模式')

    player_data = {
        "status": status,
        "current_path": str(current_path) if current_path else None,
        "playlist_name": playlist_name,
        "current_music": current_music,
        "current_time": current_time_str,
        "total_time": total_time_str,
        "playback_mode": music_player.playback_mode,
        "playback_mode_text": mode_text,
        "current_volume": f"{int(music_player.current_volume * 100)}%"
    }
    return player_data


def check_music_open(name: str) -> bool:
    """检查音乐是否被占用 (播放中)"""
    player_data = get_player()
    current_path = player_data.get("current_path")

    if player_data.get('status') == '空闲' or not current_path:
        return False

    current_path_obj = Path(current_path)

    # 1. 检查是否是单曲删除 (name 是 stem)
    if current_path_obj.stem == name:
        return True

    # 2. 检查是否是播放列表歌曲删除 (name 是 "列表名/歌曲名")
    if "/" in name:
        playlist_name, song_name = name.rsplit("/", 1)
        try:
            relative_path = current_path_obj.relative_to(Path(music_dir))
            if len(relative_path.parts) > 1 and relative_path.parts[
                0] == playlist_name and current_path_obj.stem == song_name:
                return True
        except ValueError:
            pass

    # 3. 检查是否是整个播放列表删除 (name 是列表名)
    if player_data.get("playlist_name") == name:
        return True

    return False


def edit_play_queue(music: Optional[Path] = None, music_name: Optional[str] = None, playlist: Optional[str] = None):
    """从播放队列中移除歌曲或播放列表"""

    if not music_player.play_queue:
        return

    # 1. 删除单个文件 (Path 对象比较)
    if music and music in music_player.play_queue:
        music_player.play_queue.remove(music)

    # 2. 删除整个播放列表 (清除队列中属于该播放列表的所有歌曲)
    elif playlist:
        # 获取播放列表的目录 Path
        playlist_dir = get_path(music_dir, subfolder=playlist)

        # 过滤掉所有位于该播放列表目录下的歌曲
        new_queue = []
        for path in music_player.play_queue:
            try:
                # 如果路径相对于 playlist_dir 失败，说明它不在该列表下
                path.relative_to(playlist_dir)
                # 如果成功，说明在该列表下，不加入新队列
            except ValueError:
                new_queue.append(path)

        music_player.play_queue = new_queue

    # 重新调整当前播放索引，确保它不会越界
    if music_player.play_queue:
        if music_player.current_track_index >= len(music_player.play_queue):
            music_player.current_track_index = max(0, len(music_player.play_queue) - 1)
    else:
        music_player.current_track_index = 0