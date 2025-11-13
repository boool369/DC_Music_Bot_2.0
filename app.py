from flask import Flask, render_template, request, Response
from flask_socketio import SocketIO
import os
from uuid import uuid4
from tools import get_player, get_music, music_dir, get_path, verify_name, download_status, check_music_open, \
    edit_play_queue, Path
from downloader import download_task, extract_url
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent
from typing import Dict, Union, List, Any
from dotenv import load_dotenv
import shutil
import re
import dc  # 确保导入 dc

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY")
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

connected_sids = set()


def get_player_data() -> Dict[str, str]:
    """获取播放器状态"""
    try:
        player_data = get_player()
        music = player_data.get("current_music")
        if not music:
            return {"updated_type": "player_status_updated", "message": "当前没有正在播放的音乐。"}

        msg_lines = ["<strong><i class='fas fa-compact-disc'></i> 播放器状态</strong>"]
        msg_lines.append(f"- 当前曲目: <code class='player-info-value'>{music}</code>")
        msg_lines.append(
            f"- 当前音量: <code class='player-info-value'>{player_data.get('current_volume', 'N/A')}</code>")
        msg_lines.append(
            f"- 播放模式: <code class='player-info-value'>{player_data.get('playback_mode', 'N/A')}</code>")

        playlist = player_data.get("playlist_name")
        if playlist:
            msg_lines.append(f"- 当前播放列表: <code class='player-info-value'>{playlist}</code>")
        queue = player_data.get("play_queue")

        if queue:
            msg_lines.append("<br><strong><i class='fas fa-list-ol'></i> 播放队列:</strong>")
            for i, track in enumerate(queue, start=1):
                msg_lines.append(f"{i}. <code class='player-info-value'>{track}</code>")

        return {"updated_type": "player_status_updated", "message": "<br>".join(msg_lines)}
    except Exception as e:
        return {"updated_type": "player_status_updated", "error": f"查看播放器状态时出错: {str(e)}"}


def get_music_data() -> Dict[str, Union[str, List[Dict[str, Any]]]]:
    """
    获取音乐数据，将平铺的播放列表路径 ('RJ1473335/mp3') 转换成 Web 界面可用的树状结构。
    """
    try:
        music_data = get_music()
        if not music_data:
            return {"updated_type": "music_library_updated", "message": "当前没有任何音乐或播放列表。"}

        music_tree = {}  # 用于构建树状结构

        for music in music_data:
            m_type = music.get("type")
            m_name = music.get("name")  # 完整相对路径 (e.g., 'RJ1473335/mp3') 或单曲名

            if m_type == "playlist":
                # 将完整路径分解，构建树
                parts = m_name.split('/')
                current_level = music_tree

                # 遍历路径的每一部分
                for i, part in enumerate(parts):
                    path_so_far = "/".join(parts[:i + 1])
                    is_playable_folder = (i == len(parts) - 1)

                    if part not in current_level:
                        current_level[part] = {
                            "name": part,
                            "value": path_so_far,  # 完整相对路径 (内部使用)
                            # 只有最深层级才是播放列表，上层是普通文件夹
                            "type": "playlist" if is_playable_folder else "folder",
                            "children": {}
                        }

                    if is_playable_folder:
                        # 最后一层，添加歌曲列表
                        current_level[part]["songs"] = [
                            # Web 端的 value 是文件的 Web 路径，用于删除和播放
                            {"name": song_name, "value": f"/mp3/{path_so_far}/{song_name}.mp3", "type": "song"}
                            for song_name in music['music']
                        ]
                        # 播放列表文件夹的 value 应该是完整路径 /mp3/RJ1473335/mp3
                        current_level[part]["value"] = f"/mp3/{path_so_far}"
                        current_level[part]["name"] = f"{part} ({len(music['music'])} 首)"  # 文件夹名显示歌曲数
                    else:
                        # 普通文件夹的 value 应该是完整路径 /mp3/RJ1473335
                        current_level[part]["value"] = f"/mp3/{path_so_far}"

                    # 移动到下一层
                    current_level = current_level[part]["children"]

            elif m_type == "mp3":
                # 根目录单曲
                music_tree[m_name] = {
                    "name": m_name,
                    "value": f"/mp3/{m_name}.mp3",
                    "type": "mp3",
                    "path": f"/mp3/{m_name}.mp3",  # Web 访问路径
                    "children": {}  # 不包含子项
                }

        # 将树状结构转换为 Web UI 列表
        def tree_to_list(node: Dict) -> List[Dict]:
            items = []
            # 排序：文件夹/播放列表在前，mp3 在后
            sorted_keys = sorted(node.keys(), key=lambda k: (node[k]["type"] == "mp3"), reverse=False)

            for key in sorted_keys:
                data = node[key]
                item = {
                    "name": data["name"],
                    "value": data["value"],
                    "type": data["type"],
                    "songs": data.get("songs", [])
                }
                # 递归处理子项
                if data["children"]:
                    item["children"] = tree_to_list(data["children"])

                items.append(item)
            return items

        return {"updated_type": "music_library_updated", "music_list": tree_to_list(music_tree)}
    except Exception as e:
        return {"updated_type": "music_library_updated", "error": f"查看音乐和播放列表时出错: {str(e)}"}


@app.route("/")
def index() -> Response:
    """返回网站主页面"""
    return render_template("index.html")


@app.route("/mp3/download", methods=['POST'])
def download_event() -> Dict[str, str]:
    """处理提交的下载请求"""
    try:
        data = request.json
        url = data.get("url")
        playlist = data.get("playlist")  # Web UI 传来的 playlist value 是 /mp3/path/to/playlist

        if not url:
            return {"status": "error", "message": "URL 不能为空"}, 400
        if not extract_url(url):
            return {"status": "error", "message": "无效的视频链接格式"}, 400

        # 验证播放列表名，如果存在
        if playlist:
            # 提取相对路径 (e.g., 'RJ1473335/mp3')
            relative_playlist_path = re.sub(r'^/mp3/', '', playlist)
            cleaned_playlist = verify_name(relative_playlist_path)

            if not cleaned_playlist or cleaned_playlist != relative_playlist_path:
                return {"status": "error", "message": f'播放列表文件夹名 "{relative_playlist_path}" 包含无效字符'}, 400

            # 使用相对路径进行下载
            playlist_for_download = relative_playlist_path
        else:
            playlist_for_download = None

        download_id = uuid4().hex
        for connected in list(connected_sids):
            sid = dict(connected)['sid']
            if data['sid'] == sid:
                connected_sids.remove(connected)
                connected_sids.add(
                    frozenset({"sid": sid, "id": download_id, "playlist": playlist_for_download}.items()))

                # 传入完整的 playlist 路径
                folder_path = get_path(music_dir, playlist_for_download,
                                       "%(title)s.%(ext)s") if playlist_for_download else get_path(music_dir,
                                                                                                   filename="%(title)s.%(ext)s")

                download_task.put({
                    "id": download_id,
                    "url": url,
                    "folder": folder_path
                })
                return {"status": "pending", "message": "下载任务已提交"}

        return {"status": "error", "message": "请确保以连接服务器"}
    except Exception as e:
        return {"status": "error", "message": f"下载请求处理失败: {str(e)}"}, 500


@app.route("/mp3/delete", methods=['POST'])
def delete_event() -> Dict[str, str]:
    """处理提交的删除请求"""
    try:
        data = request.json
        item_path = data.get("item_path")

        if not item_path:
            return {"status": "error", "message": "删除对象不能为空"}, 400

        # 移除前缀，获取相对路径
        relative_path_str = re.sub(r'^/mp3/', '', item_path)

        # 构建实际文件系统路径
        path_to_delete = get_path(music_dir, filename=relative_path_str)

        if path_to_delete.is_file():
            # 删除单曲 (不论根目录还是子目录)
            name_to_check = path_to_delete.stem  # 歌曲名

            if check_music_open(name_to_check):
                return {"status": "error",
                        "message": f"{name_to_check} 在播放中，前往 Discord 机器人所在公会使用 /leave 后才可以删除"}, 400

            # 播放列表名是文件父目录的完整相对路径 (e.g., 'RJ1473335/mp3')
            playlist_path = str(path_to_delete.parent.relative_to(Path(music_dir))).replace(os.path.sep, '/')
            playlist_name = playlist_path if playlist_path != '.' else None

            os.remove(path_to_delete)
            # edit_play_queue 需要 Path 对象和播放列表名（完整路径）
            edit_play_queue(path_to_delete, music_name=name_to_check, playlist=playlist_name)
            return {"status": "success", "message": f"已成功删除 {name_to_check}"}

        elif path_to_delete.is_dir():
            # 删除整个播放列表（文件夹），path_to_delete 是完整的目录路径

            # name_to_check 是完整相对路径 (e.g., 'RJ1473335/mp3')
            name_to_check = relative_path_str

            if check_music_open(name_to_check):
                return {"status": "error",
                        "message": f"播放列表 {name_to_check} 正在播放中，使用 /leave 后才可以删除"}, 400

            shutil.rmtree(path_to_delete)
            # edit_play_queue 只需要 playlist 名（完整相对路径）
            edit_play_queue(playlist=name_to_check)
            return {"status": "success", "message": f"已成功删除播放列表 {name_to_check}"}

        else:
            return {"status": "error", "message": f"未找到要删除的对象 {item_path}"}, 400

    except Exception as e:
        return {"status": "error", "message": f"删除请求处理失败: {str(e)}"}, 500


@socketio.on("disconnect")
def disconnect_handler():
    """客户端断开连接"""
    sid = request.sid
    for connected in list(connected_sids):
        if sid == dict(connected)['sid']:
            connected_sids.discard(connected)
            break


def download_status_update():
    """根据 ID 获取下载进度，推送给对应的客户端"""
    while True:
        for connected in list(connected_sids):
            connected_data = dict(connected)
            id = connected_data.get("id")
            if id:
                status_data = download_status(query_id=id)
                if status_data:
                    status_data['updated_type'] = "download_status_updated"
                    sid = connected_data.get("sid")
                    socketio.emit("update_status", status_data, to=sid)
                    if status_data.get("extra") == 100:
                        title = status_data.get("title")
                        playlist = connected_data.get("playlist")

                        # 这里的路径是 download_task 传入的路径模板
                        cleaned_title = verify_name(title)
                        if playlist:
                            final_path = get_path(music_dir, playlist, f"{cleaned_title}.mp3")
                        else:
                            final_path = get_path(music_dir, filename=f"{cleaned_title}.mp3")

                        # edit_play_queue 需要 Path 对象和播放列表名（完整路径）
                        edit_play_queue(final_path, cleaned_title, playlist)
                        connected_sids.discard(connected)
                        connected_sids.add(frozenset({"sid": sid}.items()))

        socketio.sleep(0.5)


@socketio.on("update_status")
def update_status_handler():
    """客户端请求监听状态"""
    sid = request.sid
    socketio.emit("update_status", get_player_data(), to=sid)
    socketio.emit("update_status", get_music_data(), to=sid)
    connected_sids.add(frozenset({"sid": sid}.items()))


def player_status_update():
    """播放器状态变化，推送给所有监听中的客户端"""
    last_data = None

    while True:
        try:
            player_data = get_player_data()
            data = player_data
        except Exception as e:
            data = {"error": str(e)}

        if player_data != last_data:
            for sid in list(connected_sids):
                socketio.emit("update_status", data, to=dict(sid)['sid'])
            last_data = player_data

        socketio.sleep(0.5)


class MusicDirEventHandler(FileSystemEventHandler):
    """音乐目录监听器"""

    def on_any_event(self, event: FileSystemEvent):
        """音乐目录数据变化，推送给所有监听中的客户端"""
        if event.event_type in ["created", "deleted", "moved"]:
            # 延时 0.5 秒，确保文件系统操作完成
            socketio.sleep(0.5)
            data = get_music_data()
            for sid in list(connected_sids):
                socketio.emit("update_status", data, to=dict(sid)['sid'])


def start_music_observer():
    """启动音乐目录监听"""
    try:
        os.makedirs(music_dir, exist_ok=True)
        event_handler = MusicDirEventHandler()

        observer = Observer()
        observer.schedule(event_handler, music_dir, recursive=True)
        observer.start()
    except Exception as e:
        print(f"音乐目录监听启动失败: {e}")


start_music_observer()
socketio.start_background_task(target=player_status_update)
socketio.start_background_task(target=download_status_update)
# 允许非安全 Werkzeug 重载器，避免出现问题
socketio.run(app, host="0.0.0.0", port=5000, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)