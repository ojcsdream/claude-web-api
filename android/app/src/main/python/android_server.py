import os
import shutil
import threading
import time
import zipfile
from pathlib import Path


_SERVER_THREAD = None
_SERVER_ERROR = None


def _copy_tree_from_assets(asset_zip: str, target_dir: str):
    target = Path(target_dir)
    marker = target / ".android-assets-ready"
    if marker.exists():
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
        for child in tmp.iterdir():
            dst = target / child.name
            if dst.exists():
                continue
            shutil.move(str(child), str(dst))
        shutil.rmtree(tmp)
    else:
        tmp.rename(target)
    (target / ".android-assets-ready").write_text("ready", encoding="utf-8")


def start(asset_zip: str, data_dir: str, host: str = "127.0.0.1", port: int = 8765):
    global _SERVER_THREAD, _SERVER_ERROR
    if _SERVER_THREAD and _SERVER_THREAD.is_alive():
        return f"http://{host}:{port}/"

    app_dir = Path(data_dir) / "claude-web"
    app_dir.mkdir(parents=True, exist_ok=True)
    _copy_tree_from_assets(asset_zip, str(app_dir))

    os.environ["CLAUDE_WEB_BASE_DIR"] = str(app_dir)
    os.chdir(app_dir)

    def run():
        global _SERVER_ERROR
        try:
            import uvicorn
            uvicorn.run(
                "app:app",
                host=host,
                port=int(port),
                log_level="warning",
                access_log=False,
            )
        except Exception as exc:
            _SERVER_ERROR = repr(exc)

    _SERVER_ERROR = None
    _SERVER_THREAD = threading.Thread(target=run, name="claude-web-uvicorn", daemon=True)
    _SERVER_THREAD.start()
    return f"http://{host}:{port}/"


def status():
    if _SERVER_ERROR:
        return _SERVER_ERROR
    if _SERVER_THREAD and _SERVER_THREAD.is_alive():
        return "running"
    return "stopped"


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
    return last_error or "timeout"
