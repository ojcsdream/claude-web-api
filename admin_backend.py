import json
import re
import time
import urllib.error
import urllib.request
import uuid

from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from config import BASE_DIR, STATIC_DIR, UPLOAD_DIR
from db import get_conn


ADMIN_TOKEN_FILE = BASE_DIR / "admin-token.txt"


def get_admin_token_value() -> str:
    if not ADMIN_TOKEN_FILE.exists():
        token = uuid.uuid4().hex + uuid.uuid4().hex
        ADMIN_TOKEN_FILE.write_text(token, encoding="utf-8")
        return token
    return ADMIN_TOKEN_FILE.read_text(encoding="utf-8").strip()


ADMIN_TOKEN = get_admin_token_value()
ADMIN_PASSWORD = "114514"


def require_admin_token(x_admin_token: str = Header(default="")):
    if str(x_admin_token or "").strip() != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="管理员密码无效")
    return True


def admin_mask_secret_text(text: str, max_len: int = 2000) -> str:
    if text is None:
        return ""

    t = str(text)

    patterns = [
        r'("api_auth_token"\s*:\s*")[^"]+(")',
        r'("auth_token"\s*:\s*")[^"]+(")',
        r'("Authorization"\s*:\s*")[^"]+(")',
        r'("authorization"\s*:\s*")[^"]+(")',
        r'("x-api-key"\s*:\s*")[^"]+(")',
        r'(Bearer\s+)[A-Za-z0-9_\-\.]+',
        r'(sk-[A-Za-z0-9_\-]{8})[A-Za-z0-9_\-]+',
    ]

    for pat in patterns:
        try:
            t = re.sub(
                pat,
                lambda m: (
                    m.group(1)
                    + "[REDACTED]"
                    + (m.group(2) if len(m.groups()) >= 2 else "")
                ),
                t,
            )
        except Exception:
            pass

    if len(t) > max_len:
        t = t[:max_len] + f"\n...[truncated {len(t) - max_len} chars]"

    return t


def init_admin_tables():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            method TEXT,
            path TEXT,
            query_string TEXT,
            status_code INTEGER,
            duration_ms INTEGER,
            client_ip TEXT,
            user_agent TEXT,
            route_mode TEXT,
            api_model TEXT,
            api_profile_name TEXT,
            request_summary TEXT,
            error_message TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def save_admin_request_log(
    method: str,
    path: str,
    query_string: str = "",
    status_code: int = 0,
    duration_ms: int = 0,
    client_ip: str = "",
    user_agent: str = "",
    route_mode: str = "",
    api_model: str = "",
    api_profile_name: str = "",
    request_summary: str = "",
    error_message: str = "",
):
    try:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO admin_request_logs
            (
                method,
                path,
                query_string,
                status_code,
                duration_ms,
                client_ip,
                user_agent,
                route_mode,
                api_model,
                api_profile_name,
                request_summary,
                error_message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                method,
                path,
                query_string,
                status_code,
                duration_ms,
                client_ip,
                user_agent,
                route_mode,
                api_model,
                api_profile_name,
                request_summary,
                error_message,
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def admin_build_profile_test_url(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/v1"):
        return base + "/models"
    return base + "/v1/models"


def admin_calc_quality_percent(status_code: int, latency_ms: int, error_text: str = "") -> int:
    if 200 <= status_code < 300:
        if latency_ms <= 500:
            return 100
        if latency_ms <= 1000:
            return 92
        if latency_ms <= 2000:
            return 82
        if latency_ms <= 3500:
            return 70
        if latency_ms <= 6000:
            return 58
        return 45

    if status_code in (401, 403):
        if latency_ms <= 2000:
            return 55
        return 40

    if status_code in (404, 405):
        return 35

    if status_code >= 500:
        return 25

    if error_text:
        return 10

    return 20


def admin_quality_color(percent: int, status_code: int = 0) -> str:
    if 200 <= status_code < 300 and percent >= 75:
        return "green"
    if percent >= 45:
        return "yellow"
    return "red"


def admin_quality_label(color: str) -> str:
    if color == "green":
        return "良好"
    if color == "yellow":
        return "一般"
    return "不可用"


def admin_test_one_api_profile(profile: dict) -> dict:
    name = profile.get("name", "")
    base_url = profile.get("base_url", "")
    token = profile.get("auth_token", "")
    model = profile.get("model", "")
    test_url = admin_build_profile_test_url(base_url)

    start = time.time()
    status_code = 0
    error_text = ""

    if not test_url:
        return {
            "id": profile.get("id"),
            "name": name,
            "base_url": base_url,
            "model": model,
            "test_url": "",
            "latency_ms": 0,
            "status_code": 0,
            "quality": 0,
            "color": "red",
            "label": "缺少地址",
            "error": "base_url is empty",
        }

    try:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        }

        if token:
            headers["Authorization"] = "Bearer " + token

        req = urllib.request.Request(
            test_url,
            headers=headers,
            method="GET",
        )

        with urllib.request.urlopen(req, timeout=8) as resp:
            status_code = getattr(resp, "status", 0) or resp.getcode()
            try:
                resp.read(512)
            except Exception:
                pass

    except urllib.error.HTTPError as e:
        status_code = getattr(e, "code", 0) or 0
        try:
            error_text = e.read().decode("utf-8", errors="ignore")[:500]
        except Exception:
            error_text = str(e)

    except Exception as e:
        error_text = str(e)

    latency_ms = int((time.time() - start) * 1000)
    percent = admin_calc_quality_percent(status_code, latency_ms, error_text)
    color = admin_quality_color(percent, status_code)

    return {
        "id": profile.get("id"),
        "name": name,
        "base_url": base_url,
        "model": model,
        "test_url": test_url,
        "latency_ms": latency_ms,
        "status_code": status_code,
        "quality": percent,
        "color": color,
        "label": admin_quality_label(color),
        "error": admin_mask_secret_text(error_text, 500),
    }


def register_admin_backend(app):
    init_admin_tables()

    @app.middleware("http")
    async def admin_request_logger(request: Request, call_next):
        start_time = time.time()

        method = request.method
        path = request.url.path
        query_string = request.url.query or ""
        client_ip = request.client.host if request.client else ""
        user_agent = request.headers.get("user-agent", "")

        request_summary = ""
        route_mode = "direct" if path.startswith("/api/chat") else ""
        api_model = ""
        api_profile_name = ""
        error_message = ""
        status_code = 0

        skip_log = (
            path.startswith("/static/")
            or path.startswith("/uploads/")
            or path == "/favicon.ico"
        )

        try:
            content_type = request.headers.get("content-type", "")

            if not skip_log and "multipart/form-data" not in content_type:
                body = await request.body()
                if body:
                    raw = body.decode("utf-8", errors="ignore")
                    request_summary = admin_mask_secret_text(raw, 2000)

                    try:
                        data = json.loads(raw)
                        api_model = str(data.get("api_model", "") or "")
                        api_profile_name = str(data.get("api_profile_name", "") or "")
                    except Exception:
                        pass

            elif not skip_log and "multipart/form-data" in content_type:
                request_summary = "[multipart/form-data upload skipped]"

            response = await call_next(request)
            status_code = response.status_code
            return response

        except Exception as e:
            status_code = 500
            error_message = str(e)
            raise

        finally:
            if not skip_log:
                duration_ms = int((time.time() - start_time) * 1000)
                save_admin_request_log(
                    method=method,
                    path=path,
                    query_string=query_string,
                    status_code=status_code,
                    duration_ms=duration_ms,
                    client_ip=client_ip,
                    user_agent=user_agent,
                    route_mode=route_mode,
                    api_model=api_model,
                    api_profile_name=api_profile_name,
                    request_summary=request_summary,
                    error_message=error_message,
                )

    @app.get("/admin")
    def admin_page():
        return FileResponse(STATIC_DIR / "admin.html")

    @app.get("/admin/live")
    def admin_live_page():
        return FileResponse(STATIC_DIR / "admin-live.html")

    @app.get("/api/admin/token-hint")
    def admin_token_hint():
        return {
            "ok": True,
            "message": "管理员后台已启用。请输入管理员密码进入。"
        }

    @app.get("/api/admin/stats")
    def admin_stats(_: bool = Depends(require_admin_token)):
        conn = get_conn()

        total = conn.execute(
            "SELECT COUNT(*) AS c FROM admin_request_logs"
        ).fetchone()["c"]

        errors = conn.execute(
            "SELECT COUNT(*) AS c FROM admin_request_logs WHERE status_code >= 400"
        ).fetchone()["c"]

        chat_count = conn.execute(
            "SELECT COUNT(*) AS c FROM admin_request_logs WHERE path LIKE '/api/chat%'"
        ).fetchone()["c"]

        avg_row = conn.execute(
            "SELECT AVG(duration_ms) AS avg_ms FROM admin_request_logs"
        ).fetchone()

        recent = conn.execute(
            """
            SELECT created_at, method, path, status_code, duration_ms
            FROM admin_request_logs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        conn.close()

        return {
            "total": total,
            "errors": errors,
            "chat_count": chat_count,
            "avg_ms": int(avg_row["avg_ms"] or 0),
            "recent": dict(recent) if recent else None,
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    @app.get("/api/admin/live-stream")
    def admin_live_stream(_: bool = Depends(require_admin_token)):
        def build_snapshot():
            conn = get_conn()
            try:
                stats = admin_stats(True)
                recent_rows = conn.execute(
                    """
                    SELECT id, created_at, method, path, query_string, status_code, duration_ms, client_ip, route_mode,
                           api_model, api_profile_name, request_summary, error_message
                    FROM admin_request_logs
                    ORDER BY id DESC
                    LIMIT 40
                    """
                ).fetchall()
                latest_error = conn.execute(
                    """
                    SELECT id, created_at, method, path, status_code, duration_ms, api_model, api_profile_name, error_message
                    FROM admin_request_logs
                    WHERE status_code >= 400 OR (error_message IS NOT NULL AND error_message != '')
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                recent_errors = conn.execute(
                    """
                    SELECT id, created_at, method, path, status_code, duration_ms, error_message
                    FROM admin_request_logs
                    WHERE status_code >= 400 OR (error_message IS NOT NULL AND error_message != '')
                    ORDER BY id DESC
                    LIMIT 10
                    """
                ).fetchall()
                top_paths = conn.execute(
                    """
                    SELECT path, COUNT(*) AS count, AVG(duration_ms) AS avg_ms
                    FROM admin_request_logs
                    GROUP BY path
                    ORDER BY count DESC, avg_ms DESC
                    LIMIT 8
                    """
                ).fetchall()
            finally:
                conn.close()

            return {
                "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "stats": stats,
                "recent": [dict(r) for r in recent_rows],
                "latest_error": dict(latest_error) if latest_error else None,
                "recent_errors": [dict(r) for r in recent_errors],
                "top_paths": [dict(r) for r in top_paths],
            }

        def event_stream():
            last_payload = ""
            while True:
                snapshot = build_snapshot()
                payload = json.dumps(snapshot, ensure_ascii=False)
                if payload != last_payload:
                    last_payload = payload
                    yield f"data: {payload}\n\n"
                else:
                    yield ": keep-alive\n\n"
                time.sleep(2)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.get("/api/admin/logs")
    def admin_logs(
        _: bool = Depends(require_admin_token),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        only_errors: int = Query(default=0),
        path: str = Query(default=""),
    ):
        conn = get_conn()

        where = []
        params = []

        if only_errors:
            where.append("status_code >= 400")

        if path:
            where.append("path LIKE ?")
            params.append(f"%{path}%")

        where_sql = ""
        if where:
            where_sql = "WHERE " + " AND ".join(where)

        rows = conn.execute(
            f"""
            SELECT
                id,
                created_at,
                method,
                path,
                query_string,
                status_code,
                duration_ms,
                client_ip,
                route_mode,
                api_model,
                api_profile_name,
                error_message
            FROM admin_request_logs
            {where_sql}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()

        conn.close()

        return {
            "items": [dict(r) for r in rows],
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/admin/logs/{log_id}")
    def admin_log_detail(log_id: int, _: bool = Depends(require_admin_token)):
        conn = get_conn()
        row = conn.execute(
            """
            SELECT *
            FROM admin_request_logs
            WHERE id=?
            """,
            (log_id,),
        ).fetchone()
        conn.close()

        if not row:
            raise HTTPException(status_code=404, detail="Log not found")

        return dict(row)

    @app.post("/api/admin/logs/clear")
    def admin_logs_clear(
        _: bool = Depends(require_admin_token),
        mode: str = Query(default="old"),
    ):
        conn = get_conn()

        if mode == "all":
            conn.execute("DELETE FROM admin_request_logs")
            deleted_mode = "all"
        else:
            conn.execute(
                """
                DELETE FROM admin_request_logs
                WHERE created_at < datetime('now', '-7 days')
                """
            )
            deleted_mode = "older_than_7_days"

        conn.commit()
        conn.close()

        return {
            "ok": True,
            "mode": deleted_mode,
        }

    @app.get("/api/admin/profile-health")
    def admin_profile_health(_: bool = Depends(require_admin_token)):
        conn = get_conn()
        rows = conn.execute(
            """
            SELECT id, name, base_url, auth_token, model, is_default
            FROM api_profiles
            ORDER BY is_default DESC, id ASC
            """
        ).fetchall()
        conn.close()

        results = []
        for row in rows:
            profile = dict(row)
            result = admin_test_one_api_profile(profile)
            result["is_default"] = profile.get("is_default", 0)
            results.append(result)

        return {
            "items": results,
            "tested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    @app.get("/api/admin/system")
    def admin_system(_: bool = Depends(require_admin_token)):
        db_size = 0
        try:
            db_file = BASE_DIR / "chat.db"
            if db_file.exists():
                db_size = db_file.stat().st_size
        except Exception:
            pass

        upload_count = 0
        try:
            upload_count = len(list(UPLOAD_DIR.glob("*")))
        except Exception:
            pass

        cloudflare_url = ""
        try:
            cf_file = BASE_DIR / "cloudflare-url.txt"
            if cf_file.exists():
                cloudflare_url = cf_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass

        return {
            "project_dir": str(BASE_DIR),
            "static_dir": str(STATIC_DIR),
            "upload_dir": str(UPLOAD_DIR),
            "db_size": db_size,
            "upload_count": upload_count,
            "cloudflare_url": cloudflare_url,
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
