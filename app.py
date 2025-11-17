from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import os
from uuid import uuid4
# 确保导入了所有需要的工具函数和 Path
from tools import get_player, get_music, music_dir, get_path, verify_name, download_status, check_music_open, \
    edit_play_queue, Path
from downloader import download_task, extract_url
from typing import Dict, Union, List, Any
from dotenv import load_dotenv
import shutil
import re
import dc  # 导入 dc 以调用 dc.start()
import threading  # 确保 threading 导入

# --- 修复：确保 Bot 命令和事件在 Bot 启动前加载 ---
# 必须先导入命令和事件文件，才能让 Discord Bot 注册这些命令
import dc_command
import dc_event

# ----------------------------------------------------


load_dotenv()

app = Flask(__name__)
# 优化：为 SECRET_KEY 提供更鲁棒的默认值
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", uuid4().hex)
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

connected_sids = set()


# --- Utility Functions ---

def get_player_data() -> Dict[str, Union[str, list[str]]]:
    """获取播放器状态 (兼容 web 界面)"""
    try:
        player_data = get_player()
        return {"updated_type": "player_status_updated", "data": player_data}
    except Exception as e:
        return {"updated_type": "player_status_updated", "error": f"获取播放器状态失败: {str(e)}"}


def get_music_data() -> Dict[str, Union[str, List[Dict]]]:
    """获取音乐列表 (兼容 web 界面)"""
    # 此函数会调用 tools.py 中的 get_music()，它现在依赖缓存。
    try:
        # 不带参数调用 get_music()，它将返回缓存数据
        music_list = get_music()
        safe_music_list = []
        if music_list:
            for item in music_list:
                safe_item = {
                    "type": item["type"],
                    "name": item["name"],  # 播放列表的完整相对路径
                    "music": item.get("music", []),
                    "song_count": len(item.get("music", [])) if item["type"] == "playlist" else 1
                }
                safe_music_list.append(safe_item)

        return {"updated_type": "music_list_updated", "music_list": safe_music_list}
    except Exception as e:
        return {"updated_type": "music_list_updated", "error": f"获取音乐列表失败: {str(e)}"}


# --- Routes ---

@app.route('/')
def index():
    """主页"""
    return render_template('index.html')


@app.route('/api/download', methods=['POST'])
def download_route():
    """处理下载请求 (Web API)"""
    data = request.json
    url = data.get('url')
    playlist = data.get('playlist')

    if not url:
        return jsonify({"success": False, "message": "URL 不能为空"}), 400

    valid_url = extract_url(url)
    if not valid_url:
        return jsonify({"success": False, "message": "请输入正确的视频链接"}), 400

    if playlist:
        if verify_name(playlist) != playlist:
            return jsonify({"success": False, "message": "文件夹名不能包含特殊字符: <>:\"\\|?* (但允许 /)"}), 400

    try:
        task_id = uuid4().hex

        folder_path = get_path(music_dir, playlist, "%(title)s.%(ext)s") if playlist else get_path(music_dir,
                                                                                                   filename="%(title)s.%(ext)s")

        download_task.put({"id": task_id, "url": valid_url, "folder": folder_path})

        return jsonify({"success": True, "message": "下载任务已添加", "id": task_id})
    except Exception as e:
        print(f"ERROR in download_route: {e}")
        return jsonify({"success": False, "message": f"添加下载任务失败: {str(e)}"}), 500


@app.route('/api/delete_music', methods=['POST'])
def delete_music_route():
    """处理删除请求 (Web API)"""
    data = request.json
    name = data.get('name')

    if not name:
        return jsonify({"success": False, "message": "名称不能为空"}), 400

    try:
        # 这里需要调用 get_music() 确保删除逻辑基于最新的文件列表
        music_data = get_music(check="force_rescan")  # 删除前强制刷新索引
        if not music_data:
            return jsonify({"success": False, "message": f"未找到 `{name}`"}), 404

        is_playlist = any(m["name"] == name and m["type"] == "playlist" for m in music_data)
        is_root_song = any(m["name"] == name and m["type"] == "mp3" for m in music_data)
        is_playlist_song = "/" in name

        # 检查是否正在播放
        if check_music_open(name):
            return jsonify({"success": False, "message": f"`{name}` 正在播放中，请先停止播放。"}), 400

        if is_playlist:
            # 删除整个播放列表
            path_to_delete = get_path(music_dir, subfolder=name)
            if path_to_delete.exists():
                shutil.rmtree(path_to_delete)
                edit_play_queue(playlist=name)
                # 删除后，强制刷新索引并通知 Web 客户端
                get_music(check="force_rescan")
                threading.Thread(target=lambda: socketio.emit("update_status", get_music_data())).start()
                return jsonify({"success": True, "message": f"已删除播放列表: {name}", "deleted_type": "playlist"})
            else:
                return jsonify({"success": False, "message": f"播放列表目录不存在: {name}"}), 404

        elif is_root_song:
            # 删除根目录单曲
            found_music = next((m for m in music_data if m["name"] == name and m["type"] == "mp3"), None)
            if found_music and found_music['paths']:
                path_to_delete = found_music['paths'][0]
                if path_to_delete.exists():
                    os.remove(path_to_delete)
                    edit_play_queue(music=path_to_delete)
                    # 删除后，强制刷新索引并通知 Web 客户端
                    get_music(check="force_rescan")
                    threading.Thread(target=lambda: socketio.emit("update_status", get_music_data())).start()
                    return jsonify({"success": True, "message": f"已删除单曲: {name}", "deleted_type": "song"})
                else:
                    return jsonify({"success": False, "message": f"文件不存在: {name}"}), 404
            else:
                return jsonify({"success": False, "message": f"未找到单曲文件: {name}"}), 404

        elif is_playlist_song:
            # 删除播放列表中的单曲 (格式: 列表路径/歌曲名)
            playlist_name, song_name = name.rsplit("/", 1)

            found_song = False
            for m in music_data:
                if m['name'] == playlist_name and m['type'] == 'playlist':
                    for i, s_name in enumerate(m['music']):
                        if s_name == song_name:
                            path_to_delete = m['paths'][i]
                            if path_to_delete.exists():
                                os.remove(path_to_delete)
                                edit_play_queue(music=path_to_delete)
                                found_song = True
                                break
                    if found_song:
                        # 删除后，强制刷新索引并通知 Web 客户端
                        get_music(check="force_rescan")
                        threading.Thread(target=lambda: socketio.emit("update_status", get_music_data())).start()
                        return jsonify({"success": True, "message": f"已删除 {playlist_name} 中的歌曲: {song_name}",
                                        "deleted_type": "playlist_song"})

            return jsonify({"success": False, "message": f"未找到歌曲: {name}"}), 404

        else:
            return jsonify({"success": False, "message": f"未找到或名称格式不正确: {name}"}), 404

    except Exception as e:
        print(f"ERROR in delete_music_route: {e}")
        return jsonify({"success": False, "message": f"删除音乐失败: {str(e)}"}), 500


# --- SocketIO and Background Tasks ---

@socketio.on('connect')
def handle_connect():
    """处理客户端连接"""
    connected_sids.add(request.sid)
    # 首次连接时发送最新的音乐和播放器状态
    socketio.emit("update_status", get_player_data(), to=request.sid)
    socketio.emit("update_status", get_music_data(), to=request.sid)
    print(f"SocketIO Client connected: {request.sid}")


@socketio.on('disconnect')
def handle_disconnect():
    """处理客户端断开连接"""
    connected_sids.discard(request.sid)
    print(f"SocketIO Client disconnected: {request.sid}")


def update_status_thread():
    """每隔 0.5 秒推送一次播放器状态更新，【不再轮询音乐列表】"""
    last_player_data = {}

    with app.app_context():
        while True:
            # 1. 播放器状态检查 (每 0.5 秒)
            try:
                current_player_data = get_player()
                data = {"updated_type": "player_status_updated", "data": current_player_data}
            except Exception as e:
                data = {"updated_type": "player_status_updated", "error": str(e)}

            if current_player_data != last_player_data:
                for sid in list(connected_sids):
                    socketio.emit("update_status", data, to=sid)
                last_player_data = current_player_data

            # 【注意：音乐列表轮询逻辑已移除】

            socketio.sleep(0.5)


# --- Main Execution ---

# 启动状态更新线程
threading.Thread(target=update_status_thread, daemon=True).start()

if __name__ == '__main__':
    # 启动 Flask 和 SocketIO
    print("等待音乐索引初始化...")
    # 1. 首次启动时先进行一次音乐扫描，填充索引 (只执行一次)
    get_music(check="force_rescan")
    print("音乐索引初始化完成，启动 Discord Bot。")

    # 2. 启动 Discord Bot 线程 (确保在 music index 初始化后)
    dc.start()

    print("DEBUG: 已切换到手动刷新机制监控音乐目录。")

    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)