import os
import shutil
import socket
import sys
import traceback
import threading
import time
import zipfile
from pathlib import Path


_SERVER_THREAD = None
_SERVER_ERROR = None
_SERVER_STAGE = "not started"
_APP_DIR = None


def _log(message: str):
    try:
        if _APP_DIR:
            path = Path(_APP_DIR) / "android-startup.log"
            with path.open("a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except Exception:
        pass


def _remove_app_code(target: Path):
    preserved = {
        "chat.db",
        "admin-token.txt",
        "uploads",
        ".android-assets-ready",
        ".android-assets-version",
    }
    if not target.exists():
        return
    for child in target.iterdir():
        if child.name in preserved:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _copy_tree_from_assets(asset_zip: str, target_dir: str):
    target = Path(target_dir)
    version = str(Path(asset_zip).stat().st_mtime_ns)
    version_file = target / ".android-assets-version"
    if version_file.exists() and version_file.read_text(encoding="utf-8").strip() == version:
        return

    tmp = target.with_name(target.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(asset_zip) as zf:
        zf.extractall(tmp)

    uploads = tmp / "uploads"
    uploads.mkdir(exist_ok=True)

    if target.exists():
        _remove_app_code(target)
        for child in tmp.iterdir():
            dst = target / child.name
            if dst.exists():
                continue
            shutil.move(str(child), str(dst))
        shutil.rmtree(tmp)
    else:
        tmp.rename(target)
    (target / ".android-assets-ready").write_text("ready", encoding="utf-8")
    (target / ".android-assets-version").write_text(version, encoding="utf-8")


def prepare(asset_zip: str, data_dir: str):
    global _APP_DIR, _SERVER_STAGE
    _SERVER_STAGE = "preparing assets"
    app_dir = Path(data_dir) / "claude-web"
    app_dir.mkdir(parents=True, exist_ok=True)
    _copy_tree_from_assets(asset_zip, str(app_dir))
    _APP_DIR = str(app_dir)
    _log("assets ready: " + str(app_dir))
    return str(app_dir)


def _pick_port(host: str, preferred: int) -> int:
    for port in [preferred, 8766, 8767, 88765, 18080]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((host, int(port)))
                return int(port)
        except OSError:
            continue
    raise RuntimeError("没有可用的本地端口")


def start(asset_zip: str, data_dir: str, host: str = "127.0.0.1", port: int = 8765):
    app_dir = prepare(asset_zip, data_dir)
    return start_prepared(app_dir, host, port)


def start_prepared(app_dir: str, host: str = "127.0.0.1", port: int = 8765):
    global _SERVER_THREAD, _SERVER_ERROR, _SERVER_STAGE, _APP_DIR
    if _SERVER_THREAD and _SERVER_THREAD.is_alive():
        return f"http://{host}:{port}/"

    app_dir = Path(app_dir)
    _APP_DIR = str(app_dir)
    port = _pick_port(host, int(port))

    os.environ["CLAUDE_WEB_BASE_DIR"] = str(app_dir)
    os.chdir(app_dir)
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))

    def run():
        global _SERVER_ERROR, _SERVER_STAGE
        try:
            _SERVER_STAGE = "importing backend"
            _log("importing backend")
            import importlib
            import uvicorn
            app_module = importlib.import_module("app")
            _SERVER_STAGE = f"starting uvicorn on {host}:{port}"
            _log(_SERVER_STAGE)
            uvicorn.run(
                app_module.app,
                host=host,
                port=int(port),
                log_level="warning",
                access_log=False,
            )
            _SERVER_STAGE = "uvicorn stopped"
            _log("uvicorn stopped")
        except Exception as exc:
            _SERVER_ERROR = "".join(traceback.format_exception(exc))
            _SERVER_STAGE = "failed"
            _log(_SERVER_ERROR)

    _SERVER_ERROR = None
    _SERVER_STAGE = "thread starting"
    _SERVER_THREAD = threading.Thread(target=run, name="claude-web-uvicorn", daemon=True)
    _SERVER_THREAD.start()
    return f"http://{host}:{port}/"


def status():
    if _SERVER_ERROR:
        return _SERVER_ERROR
    if _SERVER_THREAD and _SERVER_THREAD.is_alive():
        return _SERVER_STAGE
    return _SERVER_STAGE or "stopped"


def wait_until_ready(url: str, timeout_seconds: float = 20.0):
    import urllib.request

    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        if _SERVER_ERROR:
            return _SERVER_ERROR
        try:
            with urllib.request.urlopen(url + "api/health", timeout=1.5) as resp:
                if resp.status == 200:
                    return "ready"
        except Exception as exc:
            last_error = repr(exc)
        time.sleep(0.25)
    detail = status()
    if detail and detail not in ("running", "stopped"):
        return f"{last_error or 'timeout'}\n\n启动阶段: {detail}"
    return last_error or "timeout"
