from pathlib import Path
from urllib.parse import quote
import os
import json
import uuid
import time
import hashlib
import hmac
import secrets
import smtplib
import re
from email.message import EmailMessage

from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, Cookie, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from chat_utils import (
    build_chat_prompt,
    build_vision_text_history,
    collect_history_image_paths,
    estimate_round_tokens,
    format_message_for_context,
    is_image_file,
    parse_image_preview_paths,
)
from config import BASE_DIR, DEFAULT_MODEL, STATIC_DIR, UPLOAD_DIR, VISION_IMAGE_EXTS
from db import (
    db_add_message,
    db_create_session,
    db_create_conversation,
    db_create_user,
    db_delete_api_profile,
    db_delete_session,
    db_delete_sessions_by_user,
    db_delete_other_sessions_by_user,
    db_delete_last_assistant_message,
    db_delete_message_and_after_raw,
    db_ensure_conversation,
    db_get_email_verification_code,
    db_get_session_user,
    db_get_message_by_id,
    db_get_messages,
    db_get_messages_before_id,
    db_get_regenerate_history,
    db_get_single_user_auth,
    db_get_user_auth_by_id,
    db_list_api_profiles,
    db_list_system_prompts,
    db_get_user_by_username,
    db_get_user_by_email,
    db_save_email_verification_code,
    db_verify_email_code,
    db_update_user_password_by_email,
    db_update_user_password_by_id,
    db_update_user_profile,
    db_user_owns_conversation,
    db_mark_message_superseded,
    db_save_api_profile,
    db_save_system_prompt,
    db_set_default_api_profile,
    db_set_system_prompt_enabled,
    db_delete_system_prompt,
    db_update_title_if_needed,
    get_conn,
    init_db,
    now_ms,
)
from schemas import (
    AuthBody,
    ApiProfileBody,
    ChatBody,
    ConversationCreateBody,
    ConversationPinBody,
    ConversationRenameBody,
    PasswordChangeBody,
    SystemPromptBody,
    UserProfileUpdateBody,
)
from services import (
    build_fallback_search_queries,
    enhance_prompt_with_url_fetch,
    extract_urls_from_text,
    is_responses_native_document,
    looks_like_search_request,
    load_uploaded_text_from_path,
    run_search_tool_round,
    save_uploaded_file_dual_paths,
    stream_direct_and_save,
    stream_direct_api_text,
    stream_direct_responses_api_text,
    build_responses_input_payload,
    stream_direct_vision_api_text,
    stream_direct_vision_and_save,
)
from admin_backend import register_admin_backend


def load_local_env_file(path: Path):
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
            cleaned = cleaned[1:-1]
        os.environ[key] = cleaned


load_local_env_file(BASE_DIR / ".env.multi")

init_db()

app = FastAPI(title="Claude Web")

AUTH_COOKIE_NAME = "cw_multi_session"
AUTH_SESSION_TTL_MS = 1000 * 60 * 60 * 24 * 30
EMAIL_CODE_TTL_MS = 1000 * 60 * 10
EMAIL_CODE_RESEND_COOLDOWN_MS = 1000 * 60
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
AUTH_LOGIN_RESEND_COOLDOWN_MS = 1000 * 10
SINGLE_USER_ID = os.environ.get("CLAUDE_WEB_SINGLE_USER_ID", "").strip()
SINGLE_USERNAME = os.environ.get("CLAUDE_WEB_SINGLE_USERNAME", "local").strip() or "local"
SINGLE_EMAIL = os.environ.get("CLAUDE_WEB_SINGLE_EMAIL", "").strip()


def get_single_user():
    row = db_get_single_user_auth()
    if row:
        return dict(row)
    raise HTTPException(status_code=500, detail="单用户初始化失败")


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_email_identifier(value: str) -> bool:
    return bool(EMAIL_RE.match(normalize_email(value)))


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000).hex()
    return f"pbkdf2_sha256$200000${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt, digest = (encoded or "").split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(rounds)).hex()
        return hmac.compare_digest(check, digest)
    except Exception:
        return False


def set_auth_cookie(response: Response, token: str):
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


def public_user(row):
    return {"id": row["id"], "username": row["username"], "email": row["email"] if "email" in row.keys() else "", "created_at": row["created_at"]}


def smtp_config():
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", "").strip() or user
    port = int(os.environ.get("SMTP_PORT", "587") or "587")
    use_ssl = (os.environ.get("SMTP_SSL", "0").strip().lower() in {"1", "true", "yes"})
    if not host or not sender:
        return None
    return {"host": host, "port": port, "user": user, "password": password, "sender": sender, "ssl": use_ssl}


def send_verification_email(email: str, code: str):
    cfg = smtp_config()
    if not cfg:
        raise HTTPException(status_code=503, detail="邮箱服务未配置，请设置 SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/SMTP_FROM")

    msg = EmailMessage()
    msg["Subject"] = "Claude Web 注册验证码"
    msg["From"] = cfg["sender"]
    msg["To"] = email
    msg.set_content(f"你的注册验证码是：{code}\n\n验证码 10 分钟内有效。如果不是你本人操作，请忽略这封邮件。")

    if cfg["ssl"]:
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=12)
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=12)
    try:
        if not cfg["ssl"]:
            server.starttls()
        if cfg["user"]:
            server.login(cfg["user"], cfg["password"])
        server.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise HTTPException(status_code=502, detail="SMTP 登录失败。Gmail 通常需要使用 App Password，而不是账户登录密码。") from e
    except smtplib.SMTPException as e:
        raise HTTPException(status_code=502, detail=f"SMTP 发送失败: {e}") from e
    finally:
        server.quit()


def require_current_user(cw_multi_session: str = Cookie(default="")):
    return get_single_user()


def require_conversation_owner(conversation_id: str, user_id: str):
    if not db_user_owns_conversation(user_id, conversation_id):
        raise HTTPException(status_code=404, detail="会话不存在")


@app.get("/api/auth/me")
def auth_me(user=Depends(require_current_user)):
    return {"ok": True, "user": public_user(user), "single_user_mode": True}


def validate_username_or_raise(username: str):
    if len(username) < 3 or len(username) > 32:
        raise HTTPException(status_code=400, detail="用户名长度需要在 3 到 32 位之间")
    if not username.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="用户名只能包含字母、数字、横线和下划线")


@app.patch("/api/auth/me")
def auth_update_me(body: UserProfileUpdateBody, user=Depends(require_current_user)):
    username = normalize_username(body.username)
    email = normalize_email(body.email)
    validate_username_or_raise(username)
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="请输入有效邮箱")

    existing_username = db_get_user_by_username(username)
    if existing_username and existing_username["id"] != user["id"]:
        raise HTTPException(status_code=409, detail="用户名已被占用")

    existing_email = db_get_user_by_email(email)
    if existing_email and existing_email["id"] != user["id"]:
        raise HTTPException(status_code=409, detail="邮箱已被占用")

    updated = db_update_user_profile(user["id"], username, email)
    return {"ok": True, "user": public_user(updated)}


@app.post("/api/auth/change-password")
def auth_change_password(body: PasswordChangeBody, cw_multi_session: str = Cookie(default=""), user=Depends(require_current_user)):
    new_password = body.new_password or ""
    confirm_password = body.confirm_password or ""
    verification_code = (body.verification_code or "").strip()
    auth_user = db_get_user_auth_by_id(user["id"])
    if not auth_user:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    email = normalize_email(auth_user["email"] or "")
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="当前账号未绑定有效邮箱，无法通过邮箱验证修改密码")
    if not verification_code:
        raise HTTPException(status_code=400, detail="请输入邮箱验证码")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    if new_password != confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")
    if verify_password(new_password, auth_user["password_hash"]):
        raise HTTPException(status_code=400, detail="新密码不能和当前密码相同")
    if not db_verify_email_code(email, "change_password", verification_code):
        raise HTTPException(status_code=400, detail="邮箱验证码不正确或已过期")

    db_update_user_password_by_id(user["id"], hash_password(new_password))
    if cw_multi_session:
        db_delete_other_sessions_by_user(user["id"], cw_multi_session)
    else:
        db_delete_sessions_by_user(user["id"])
    return {"ok": True, "message": "密码已更新"}


@app.post("/api/auth/send-password-change-code")
def auth_send_password_change_code(user=Depends(require_current_user)):
    email = normalize_email(user.get("email") or "")
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="当前账号未绑定有效邮箱，无法发送验证码")
    previous = db_get_email_verification_code(email, "change_password")
    if previous and previous["created_at"]:
        elapsed = int(time.time() * 1000) - int(previous["created_at"])
        if elapsed < EMAIL_CODE_RESEND_COOLDOWN_MS:
            wait_s = max(1, int((EMAIL_CODE_RESEND_COOLDOWN_MS - elapsed + 999) / 1000))
            raise HTTPException(status_code=429, detail=f"请等待 {wait_s} 秒后再重新发送验证码")

    code = f"{secrets.randbelow(1_000_000):06d}"
    db_save_email_verification_code(email, "change_password", code, int(time.time() * 1000) + EMAIL_CODE_TTL_MS)
    send_verification_email(email, code)
    return {"ok": True, "message": "验证码已发送", "email": email}


@app.get("/api/auth/account-summary")
def auth_account_summary(cw_multi_session: str = Cookie(default=""), user=Depends(require_current_user)):
    conn = get_conn()
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM auth_sessions WHERE user_id=? AND expires_at>?) AS active_sessions,
            (SELECT COUNT(*) FROM auth_sessions WHERE user_id=? AND token<>? AND expires_at>?) AS other_sessions,
            (SELECT COUNT(*) FROM conversations WHERE user_id=?) AS conversations,
            (SELECT COUNT(*) FROM messages JOIN conversations ON conversations.id = messages.conversation_id WHERE conversations.user_id=?) AS messages,
            (SELECT COUNT(*) FROM api_profiles WHERE user_id=?) AS api_profiles,
            (SELECT COUNT(*) FROM system_prompts WHERE user_id=?) AS system_prompts
        """,
        (user["id"], now_ms(), user["id"], cw_multi_session, now_ms(), user["id"], user["id"], user["id"], user["id"]),
    ).fetchone()
    conn.close()
    return {"ok": True, "summary": dict(row)}


@app.post("/api/auth/logout-other-sessions")
def auth_logout_other_sessions(cw_multi_session: str = Cookie(default=""), user=Depends(require_current_user)):
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS total FROM auth_sessions WHERE user_id=? AND token<>? AND expires_at>?",
        (user["id"], cw_multi_session, now_ms()),
    ).fetchone()
    conn.close()
    db_delete_other_sessions_by_user(user["id"], cw_multi_session)
    return {"ok": True, "closed_sessions": int(row["total"] if row else 0)}


@app.post("/api/auth/send-code")
def auth_send_code(body: AuthBody):
    email = normalize_email(body.email)
    purpose = (body.purpose or "register").strip().lower()
    if purpose not in {"register", "reset"}:
        raise HTTPException(status_code=400, detail="不支持的验证码用途")
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="请输入有效邮箱")
    existing = db_get_user_by_email(email)
    if purpose == "register" and existing:
        raise HTTPException(status_code=409, detail="邮箱已被注册")
    if purpose == "reset" and not existing:
        raise HTTPException(status_code=404, detail="该邮箱尚未注册")
    previous = db_get_email_verification_code(email)
    if previous and previous["created_at"]:
        elapsed = int(time.time() * 1000) - int(previous["created_at"])
        if elapsed < EMAIL_CODE_RESEND_COOLDOWN_MS:
            wait_s = max(1, int((EMAIL_CODE_RESEND_COOLDOWN_MS - elapsed + 999) / 1000))
            raise HTTPException(status_code=429, detail=f"请等待 {wait_s} 秒后再重新发送验证码")

    code = f"{secrets.randbelow(1_000_000):06d}"
    db_save_email_verification_code(email, purpose, code, int(time.time() * 1000) + EMAIL_CODE_TTL_MS)
    send_verification_email(email, code)
    return {"ok": True, "message": "验证码已发送", "purpose": purpose}


@app.post("/api/auth/register")
def auth_register(body: AuthBody, response: Response):
    username = normalize_username(body.username)
    email = normalize_email(body.email)
    password = body.password or ""
    confirm_password = body.confirm_password or ""
    verification_code = (body.verification_code or "").strip()
    validate_username_or_raise(username)
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="请输入有效邮箱")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    if password != confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的密码不一致")
    if not verification_code:
        raise HTTPException(status_code=400, detail="请输入邮箱验证码")
    if db_get_user_by_username(username):
        raise HTTPException(status_code=409, detail="用户名已存在")
    if db_get_user_by_email(email):
        raise HTTPException(status_code=409, detail="邮箱已被注册")
    if not db_verify_email_code(email, "register", verification_code):
        raise HTTPException(status_code=400, detail="邮箱验证码不正确或已过期")

    uid = db_create_user(username, email, hash_password(password))
    token = secrets.token_urlsafe(32)
    db_create_session(token, uid, int(time.time() * 1000) + AUTH_SESSION_TTL_MS)
    set_auth_cookie(response, token)
    return {"ok": True, "user": {"id": uid, "username": username, "email": email, "created_at": int(time.time() * 1000)}}


@app.post("/api/auth/reset-password")
def auth_reset_password(body: AuthBody):
    email = normalize_email(body.email)
    password = body.password or ""
    confirm_password = body.confirm_password or ""
    verification_code = (body.verification_code or "").strip()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="请输入有效邮箱")
    user = db_get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail="该邮箱尚未注册")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    if password != confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的密码不一致")
    if not verification_code:
        raise HTTPException(status_code=400, detail="请输入邮箱验证码")
    if not db_verify_email_code(email, "reset", verification_code):
        raise HTTPException(status_code=400, detail="邮箱验证码不正确或已过期")

    db_update_user_password_by_email(email, hash_password(password))
    db_delete_sessions_by_user(user["id"])
    return {"ok": True, "message": "密码已重置，请重新登录"}


@app.post("/api/auth/login")
def auth_login(body: AuthBody, response: Response):
    identifier = (body.username or "").strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="请输入用户名或邮箱")
    row = db_get_user_by_email(identifier) if is_email_identifier(identifier) else db_get_user_by_username(normalize_username(identifier))
    if not row or not verify_password(body.password or "", row["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码不正确")

    token = secrets.token_urlsafe(32)
    db_create_session(token, row["id"], int(time.time() * 1000) + AUTH_SESSION_TTL_MS)
    set_auth_cookie(response, token)
    return {"ok": True, "user": {"id": row["id"], "username": row["username"], "email": row["email"] or "", "created_at": row["created_at"]}}


@app.post("/api/auth/logout")
def auth_logout(response: Response, cw_multi_session: str = Cookie(default="")):
    if cw_multi_session:
        db_delete_session(cw_multi_session)
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return {"ok": True}


def iter_search_status_lines(enabled: bool, sources=None):
    if not enabled:
        return
    yield "\n[[STATUS:planning_search]]\n"
    yield f"\n[[STATUS:{search_status_key(sources)}]]\n"
    if sources:
        yield "\n[[STATUS:reading_sources]]\n"


def emit_sources_marker(sources):
    if not sources:
        return ""
    return "\n[[SOURCES:" + json.dumps(sources, ensure_ascii=False) + "]]\n"


def resolve_search_tool_context(
    user_prompt: str,
    history,
    force: bool,
    api_base_url: str,
    api_auth_token: str,
    api_model: str,
):
    return run_search_tool_round(
        user_prompt,
        context_messages=history,
        api_base_url=api_base_url,
        api_auth_token=api_auth_token,
        api_model=api_model,
        force=force,
        max_results=4,
    )


def should_autonomous_search(prompt: str, force: bool = False) -> bool:
    if force:
        return True
    return bool(looks_like_search_request(prompt or ""))


def should_autonomous_search_with_context(prompt: str, history=None, force: bool = False) -> bool:
    if should_autonomous_search(prompt, force=force):
        return True
    value = (prompt or "").strip().lower()
    if not value or not history:
        return False
    reference_words = (
        "这个", "这个事", "这件事", "这家公司", "这个公司", "这款", "这个模型",
        "他", "她", "它", "他们", "它们", "其", "该", "上述", "前面", "刚才", "你说的",
        "this", "that", "it", "they", "them", "above",
    )
    recency_words = (
        "最新", "最近", "新闻", "消息", "动态", "进展", "现在", "目前",
        "latest", "recent", "news", "update", "updates", "current",
    )
    return any(w in value for w in reference_words) and any(w in value for w in recency_words)


def search_status_key(sources) -> str:
    return "github_mcp" if sources and all(item.get("provider") == "github-mcp" for item in sources) else "searching"


def build_responses_search_plan(user_prompt, history):
    return {
        "should_search": True,
        "search_queries": build_fallback_search_queries(user_prompt, context_messages=history, max_queries=3),
        "parse_links": extract_urls_from_text(user_prompt, max_urls=2),
        "tool": "responses_web_search",
    }


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
@app.get("/")
def index():
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/lite")
def lite_index():
    return FileResponse(
        STATIC_DIR / "lite.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/api/health")
def health():
    return {"ok": True, "message": "backend running", "default_model": DEFAULT_MODEL}


@app.get("/api/startup")
def startup_status():
    return {
        "ok": True,
        "message": "local backend ready",
        "default_model": DEFAULT_MODEL,
        "project_dir": str(BASE_DIR),
        "static_dir": str(STATIC_DIR),
        "upload_dir": str(UPLOAD_DIR),
    }






@app.post("/api/profiles/test")
def test_api_profile(body: ApiProfileBody, user=Depends(require_current_user)):
    try:
        reply = "".join(stream_direct_api_text(
            "只回复：API配置可用",
            body.base_url,
            body.auth_token,
            body.model or DEFAULT_MODEL,
            body.protocol or "",
        )).strip()
        failed = "[直连" in reply and "失败]" in reply
        return {
            "ok": bool(reply) and not failed,
            "reply": reply or "无输出",
        }
    except Exception as e:
        return {
            "ok": False,
            "reply": str(e),
        }




def do_fetch_models_from_profile(base_url: str, token: str):
    import urllib.request
    import urllib.error

    base_url = (base_url or "").strip().rstrip("/")
    token = (token or "").strip()

    if not base_url or not token:
        return {
            "ok": False,
            "models": [],
            "error": "缺少 API URL 或 API Key"
        }

    url = base_url + "/v1/models"

    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "x-api-key": token,
            "Authorization": "Bearer " + token,
            "anthropic-version": "2023-06-01",
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(raw)

        models = []

        for item in data.get("data", []):
            if isinstance(item, dict) and item.get("id"):
                models.append(item.get("id"))
            elif isinstance(item, str):
                models.append(item)

        for item in data.get("models", []):
            if isinstance(item, str):
                models.append(item)
            elif isinstance(item, dict) and item.get("id"):
                models.append(item.get("id"))

        models = list(dict.fromkeys(models))

        return {
            "ok": True,
            "models": models,
            "raw": data,
        }

    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")
        hint = ""
        if e.code == 403 and ("1010" in err or "cloudflare" in err.lower()):
            hint = "\n\n提示：服务端返回 Cloudflare/WAF 403，通常是请求头或当前出口 IP 被风控拦截。已使用浏览器式 User-Agent 重试；如果仍失败，需要在接入商后台放行当前 IP，或让接入商关闭该 API 路径的 Browser Integrity/WAF 规则。"
        return {
            "ok": False,
            "models": [],
            "error": f"HTTP {e.code}: " + (err or str(e)) + hint,
        }
    except Exception as e:
        return {
            "ok": False,
            "models": [],
            "error": str(e),
        }


@app.post("/api/profiles/models")
def fetch_api_profile_models_post(body: ApiProfileBody, user=Depends(require_current_user)):
    return do_fetch_models_from_profile(body.base_url, body.auth_token)


@app.get("/api/profiles/models")
def fetch_api_profile_models_get(profile_id: str = "", user=Depends(require_current_user)):
    if not profile_id:
        return {
            "ok": False,
            "models": [],
            "error": "缺少 profile_id"
        }

    conn = get_conn()
    row = conn.execute(
        "SELECT base_url, auth_token FROM api_profiles WHERE id=? AND user_id=?",
        (profile_id, user["id"]),
    ).fetchone()
    conn.close()

    if not row:
        return {
            "ok": False,
            "models": [],
            "error": "接入商不存在"
        }

    return do_fetch_models_from_profile(row["base_url"], row["auth_token"])


@app.get("/api/profiles")
def list_api_profiles(user=Depends(require_current_user)):
    return {
        "ok": True,
        "profiles": db_list_api_profiles(user["id"]),
    }


@app.post("/api/profiles")
def create_api_profile(body: ApiProfileBody, user=Depends(require_current_user)):
    pid = db_save_api_profile("", body, user["id"])
    return {"ok": True, "id": pid}


@app.put("/api/profiles/{profile_id}")
def update_api_profile(profile_id: str, body: ApiProfileBody, user=Depends(require_current_user)):
    pid = db_save_api_profile(profile_id, body, user["id"])
    return {"ok": True, "id": pid}


@app.delete("/api/profiles/{profile_id}")
def delete_api_profile(profile_id: str, user=Depends(require_current_user)):
    db_delete_api_profile(profile_id, user["id"])
    return {"ok": True}


@app.post("/api/profiles/{profile_id}/default")
def set_default_api_profile(profile_id: str, user=Depends(require_current_user)):
    db_set_default_api_profile(profile_id, user["id"])
    return {"ok": True}


@app.get("/api/system-prompts")
def list_system_prompts(user=Depends(require_current_user)):
    return {
        "ok": True,
        "prompts": db_list_system_prompts(user["id"]),
    }


@app.post("/api/system-prompts")
def create_system_prompt(body: SystemPromptBody, user=Depends(require_current_user)):
    pid = db_save_system_prompt("", body, user["id"])
    return {"ok": True, "id": pid}


@app.put("/api/system-prompts/{prompt_id}")
def update_system_prompt(prompt_id: str, body: SystemPromptBody, user=Depends(require_current_user)):
    pid = db_save_system_prompt(prompt_id, body, user["id"])
    return {"ok": True, "id": pid}


@app.post("/api/system-prompts/{prompt_id}/enabled")
def set_system_prompt_enabled(prompt_id: str, body: SystemPromptBody, user=Depends(require_current_user)):
    db_set_system_prompt_enabled(prompt_id, body.enabled, user["id"])
    return {"ok": True}


@app.delete("/api/system-prompts/{prompt_id}")
def delete_system_prompt(prompt_id: str, user=Depends(require_current_user)):
    db_delete_system_prompt(prompt_id, user["id"])
    return {"ok": True}




@app.get("/api/search")
def search_messages(q: str = "", conversation_id: str = "", scope: str = "all", limit: int = 50, user=Depends(require_current_user)):
    keyword = (q or "").strip()

    if not keyword:
        return {"ok": True, "results": [], "query": "", "scope": scope}

    like = f"%{keyword}%"
    max_limit = max(1, min(int(limit or 50), 100))
    search_single_conversation = (scope or "").strip().lower() == "conversation" and bool(conversation_id)

    where_parts = [
        "conversations.user_id=?",
        "(messages.content LIKE ? OR conversations.title LIKE ? OR messages.file_name LIKE ? OR messages.file_context LIKE ?)"
    ]
    params = [user["id"], like, like, like, like]

    if search_single_conversation:
        where_parts.append("messages.conversation_id=?")
        params.append(conversation_id)

    params.append(max_limit)

    conn = get_conn()
    rows = conn.execute(
        f"""
        SELECT
            messages.id AS message_id,
            messages.conversation_id AS conversation_id,
            messages.role AS role,
            messages.content AS content,
            messages.file_name AS file_name,
            messages.file_context AS file_context,
            messages.created_at AS created_at,
            conversations.title AS conversation_title
        FROM messages
        JOIN conversations ON conversations.id = messages.conversation_id
        WHERE {" AND ".join(where_parts)}
        ORDER BY messages.created_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    conn.close()

    results = []

    for row in rows:
        fields = [
            ("content", row["content"] or ""),
            ("title", row["conversation_title"] or ""),
            ("file_name", row["file_name"] or ""),
            ("file_context", row["file_context"] or ""),
        ]
        matched_field = "content"
        content = row["content"] or ""
        idx = content.lower().find(keyword.lower())

        if idx < 0:
            for field_name, field_text in fields[1:]:
                field_idx = field_text.lower().find(keyword.lower())
                if field_idx >= 0:
                    matched_field = field_name
                    content = field_text
                    idx = field_idx
                    break

        if idx >= 0:
            start = max(0, idx - 40)
            end = min(len(content), idx + len(keyword) + 80)
            snippet = content[start:end]
        else:
            snippet = content[:120]

        results.append({
            "message_id": row["message_id"],
            "conversation_id": row["conversation_id"],
            "conversation_title": row["conversation_title"],
            "role": row["role"],
            "snippet": snippet,
            "matched_field": matched_field,
            "file_name": row["file_name"],
            "created_at": row["created_at"],
        })

    return {
        "ok": True,
        "results": results,
        "query": keyword,
        "scope": "conversation" if search_single_conversation else "all",
    }


@app.get("/api/conversations")
def list_conversations(user=Depends(require_current_user)):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, title, created_at, updated_at, is_pinned FROM conversations WHERE user_id=? ORDER BY is_pinned DESC, updated_at DESC",
        (user["id"],),
    ).fetchall()
    conn.close()
    return {
        "ok": True,
        "conversations": [dict(row) for row in rows],
    }


@app.post("/api/conversations")
def create_conversation(body: ConversationCreateBody, user=Depends(require_current_user)):
    cid = db_create_conversation(body.title or "新对话", user["id"])
    return {"ok": True, "id": cid}


@app.get("/api/conversations/{conversation_id}/messages")
def get_conversation_messages(conversation_id: str, user=Depends(require_current_user)):
    require_conversation_owner(conversation_id, user["id"])
    msgs = db_get_messages(conversation_id)
    return {
        "ok": True,
        "messages": [m.model_dump() if hasattr(m, "model_dump") else m.dict() for m in msgs],
    }




@app.delete("/api/conversations/{conversation_id}/messages/{message_id}/after")
def delete_message_and_after(conversation_id: str, message_id: int, user=Depends(require_current_user)):
    require_conversation_owner(conversation_id, user["id"])
    conn = get_conn()

    row = conn.execute(
        "SELECT id FROM messages WHERE conversation_id=? AND id=?",
        (conversation_id, message_id),
    ).fetchone()

    if not row:
        conn.close()
        return {"ok": False, "message": "消息不存在"}

    conn.execute(
        "DELETE FROM messages WHERE conversation_id=? AND id>=?",
        (conversation_id, message_id),
    )

    conn.execute(
        "UPDATE conversations SET updated_at=? WHERE id=?",
        (now_ms(), conversation_id),
    )

    conn.commit()
    conn.close()

    return {"ok": True}


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user=Depends(require_current_user)):
    require_conversation_owner(conversation_id, user["id"])
    conn = get_conn()
    conn.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
    conn.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
    conn.commit()
    conn.close()
    return {"ok": True}




@app.post("/api/conversations/{conversation_id}/pin")
def pin_conversation(conversation_id: str, body: ConversationPinBody, user=Depends(require_current_user)):
    require_conversation_owner(conversation_id, user["id"])
    conn = get_conn()
    conn.execute(
        "UPDATE conversations SET is_pinned=?, updated_at=? WHERE id=? AND user_id=?",
        (1 if body.pinned else 0, now_ms(), conversation_id, user["id"]),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/conversations/{conversation_id}/rename")
def rename_conversation(conversation_id: str, body: ConversationRenameBody, user=Depends(require_current_user)):
    require_conversation_owner(conversation_id, user["id"])
    conn = get_conn()
    conn.execute(
        "UPDATE conversations SET title=?, updated_at=? WHERE id=? AND user_id=?",
        (body.title.strip()[:50] or "新对话", now_ms(), conversation_id, user["id"]),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/echo")
def echo(body: ChatBody, user=Depends(require_current_user)):
    try:
        cid = db_ensure_conversation(body.conversation_id, user_id=user["id"])
        history = db_get_messages(cid)
        final_prompt = build_chat_prompt(history, body.prompt)

        db_add_message(cid, "user", body.prompt)
        db_update_title_if_needed(cid, body.prompt, user["id"])

        reply = "".join(stream_direct_api_text(
            final_prompt,
            body.api_base_url,
            body.api_auth_token,
            body.api_model or DEFAULT_MODEL,
            body.api_protocol or "",
            body.system_prompt,
        )).strip()
        db_add_message(cid, "assistant", reply, model=body.api_model or DEFAULT_MODEL, provider_name=body.api_profile_name or "")

        return {"ok": True, "conversation_id": cid, "reply": reply or "无输出"}
    except Exception as e:
        return {"ok": False, "reply": f"调用失败: {e}"}


@app.post("/api/chat/stream")
def chat_stream(body: ChatBody, user=Depends(require_current_user)):
    cid = db_ensure_conversation(body.conversation_id, user_id=user["id"])
    history = db_get_messages(cid)
    db_add_message(cid, "user", body.prompt)
    db_update_title_if_needed(cid, body.prompt, user["id"])
    protocol = (body.api_protocol or "").strip().lower()
    if not protocol:
        protocol = "completions" if (body.api_model or "").lower().startswith("gpt") or "gpt-" in (body.api_model or "").lower() else "claude"

    def gen():
        has_urls = bool(extract_urls_from_text(body.prompt))
        search_intent = should_autonomous_search_with_context(body.prompt, history, force=body.web_search)
        should_fetch_urls = has_urls and protocol != "responses"
        should_prepare_search = search_intent or should_fetch_urls
        yield "\n[[STATUS:thinking]]\n"
        if should_prepare_search:
            yield "\n[[STATUS:parsing]]\n"
            if search_intent:
                yield "\n[[STATUS:planning_search]]\n"

        effective_prompt = body.prompt if protocol == "responses" else enhance_prompt_with_url_fetch(body.prompt)
        sources = []
        plan = {}
        observation = ""

        if search_intent and protocol == "responses":
            plan = build_responses_search_plan(body.prompt, history)
            should_search = True
        elif search_intent or should_fetch_urls:
            observation, sources, plan = resolve_search_tool_context(
                body.prompt,
                history,
                body.web_search,
                body.api_base_url,
                body.api_auth_token,
                body.api_model or DEFAULT_MODEL,
            )
            should_search = bool(plan.get("should_search") or plan.get("parse_links"))
        else:
            should_search = False

        if should_search and (protocol != "responses" or observation):
            yield f"\n[[STATUS:{search_status_key(sources)}]]\n"
            if plan.get("parse_links"):
                yield "\n[[STATUS:reading_sources]]\n"
            if observation:
                marker = emit_sources_marker(sources)
                if marker:
                    yield marker
                effective_prompt = (
                    observation
                    + "\n\n现在基于上述工具调用结果和最近聊天上下文，直接回答用户。"
                )

        final_prompt = build_chat_prompt(history, effective_prompt)

        yield from stream_direct_and_save(
            cid,
            final_prompt,
            body.api_base_url,
            body.api_auth_token,
            body.api_model or DEFAULT_MODEL,
            protocol,
            body.api_profile_name or "",
            system_prompt=body.system_prompt,
            sources=json.dumps(sources, ensure_ascii=False) if sources else "",
            use_web_search=True if protocol == "responses" else bool(should_search and not observation),
        )

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")






def _save_regenerated_answer(cid, answer, api_model, provider_name, old_message_id, final_prompt="", image_count=0, sources=None):
    token_count = estimate_round_tokens(final_prompt, answer, image_count=image_count)
    new_id = db_add_message(
        cid,
        "assistant",
        answer,
        model=api_model,
        provider_name=provider_name,
        token_count=token_count,
        sources=sources,
    )
    if old_message_id:
        db_mark_message_superseded(old_message_id, new_id)
    return new_id


def _stream_and_save_regenerated_answer(inner_gen, cid, api_model, provider_name, old_message_id, final_prompt="", sources=None):
    full = ""
    for chunk in inner_gen:
        full += chunk
        yield chunk
    _save_regenerated_answer(cid, full, api_model, provider_name, old_message_id, final_prompt, sources=sources)


def _build_regenerate_prompt_with_search(
    context_messages,
    last_user_prompt,
    web_search=False,
    system_prompt="",
    api_base_url="",
    api_auth_token="",
    api_model=DEFAULT_MODEL,
    api_protocol="",
):
    sources = []
    observation = ""
    plan = {}
    protocol = (api_protocol or "").strip().lower()
    if not protocol:
        protocol = "completions" if (api_model or "").lower().startswith("gpt") or "gpt-" in (api_model or "").lower() else "claude"

    search_intent = should_autonomous_search_with_context(
        last_user_prompt,
        context_messages,
        force=web_search,
    )
    should_fetch_urls = bool(extract_urls_from_text(last_user_prompt)) and protocol != "responses"
    if search_intent:
        if protocol == "responses":
            plan = build_responses_search_plan(last_user_prompt, context_messages)
        else:
            observation, sources, plan = resolve_search_tool_context(
                last_user_prompt,
                context_messages,
                web_search,
                api_base_url,
                api_auth_token,
                api_model,
            )
    elif should_fetch_urls:
        observation, sources, plan = resolve_search_tool_context(
            last_user_prompt,
            context_messages,
            web_search,
            api_base_url,
            api_auth_token,
            api_model,
        )

    final_user_prompt = last_user_prompt
    if observation:
        final_user_prompt = (
            observation
            + "\n\n现在基于上述工具调用结果和最近聊天上下文，直接回答用户。"
        )

    final_prompt = build_chat_prompt(
        messages=context_messages,
        prompt=final_user_prompt,
    )
    return final_prompt, sources, plan


@app.post("/api/chat/regenerate_from_stream")
def regenerate_from_stream(body: ChatBody, user=Depends(require_current_user)):
    cid = db_ensure_conversation(body.conversation_id, user_id=user["id"])

    if body.message_id is None:
        return StreamingResponse(
            iter(["缺少 message_id"]),
            media_type="text/plain; charset=utf-8",
        )

    target = db_get_message_by_id(cid, body.message_id)

    if not target:
        return StreamingResponse(
            iter(["目标消息不存在"]),
            media_type="text/plain; charset=utf-8",
        )

    # 取目标消息之前的历史
    before = db_get_messages_before_id(cid, body.message_id)

    # 找目标助手消息前最近的一条用户消息
    last_user_index = -1
    for i in range(len(before) - 1, -1, -1):
        if before[i].role == "user":
            last_user_index = i
            break

    if last_user_index == -1:
        return StreamingResponse(
            iter(["没有找到可重新回答的用户消息"]),
            media_type="text/plain; charset=utf-8",
        )

    context_messages = before[:last_user_index]
    last_user_msg = before[last_user_index]
    last_user_prompt = last_user_msg.content
    last_image_preview = last_user_msg.imagePreview
    # 如果不保留旧版本，删除目标消息以及之后所有消息
    keep_old = body.keep_old
    if not keep_old:
        db_delete_message_and_after_raw(cid, body.message_id)

    # 如果上一条用户消息带有图片，走视觉流程重新回答
    if last_image_preview:
        try:
            web_paths = json.loads(last_image_preview)
            # /uploads/xxx.jpg → ./uploads/xxx.jpg，供 call_direct_vision_api 定位本地文件
            local_image_paths = ["." + p for p in web_paths if p.startswith("/")]
        except Exception:
            local_image_paths = []

        if local_image_paths:
            vision_prompt_parts = []
            text_history = build_vision_text_history(context_messages)
            if text_history:
                vision_prompt_parts.append("以下是最近的文字对话历史，仅用于理解用户问题，不可替代图片内容：")
                vision_prompt_parts.append(text_history)
                vision_prompt_parts.append("")

            vision_prompt_parts.append(f"用户本次上传了 {len(local_image_paths)} 张图片。")
            vision_prompt_parts.append("你必须读取当前上传图片的像素内容，并结合上面的文字历史回答。")
            vision_prompt_parts.append("不要只根据聊天历史、文件名、路径或上下文猜测图片内容。")
            vision_prompt_parts.append("如果你没有看到图片内容，请明确回复：我没有读取到图片内容。")
            vision_prompt_parts.append("")
            vision_prompt_parts.append("用户问题：" + (last_user_prompt.strip() or "请分析我上传的图片。"))
            vision_prompt = "\n".join(vision_prompt_parts)

            _api_base_url = body.api_base_url
            _api_auth_token = body.api_auth_token
            _api_model = body.api_model or DEFAULT_MODEL
            _api_profile_name = body.api_profile_name or ""
            _cid = cid

            def gen_vision():
                prefix = f"正在分析 {len(local_image_paths)} 张图片...\n\n"
                yield prefix
                full = ""
                try:
                    for chunk in stream_direct_vision_api_text(
                        vision_prompt,
                        local_image_paths,
                        _api_base_url,
                        _api_auth_token,
                        _api_model,
                        system_prompt=body.system_prompt,
                    ):
                        full += chunk
                        yield chunk
                    token_count = estimate_round_tokens(vision_prompt, full, image_count=len(local_image_paths))
                    db_add_message(
                        _cid,
                        "assistant",
                        full,
                        model=_api_model,
                        provider_name=_api_profile_name,
                        token_count=token_count,
                    )
                except Exception as e:
                    error_msg = (
                        "【视觉接口调用失败】\n\n"
                        + str(e)
                        + "\n\n这说明当前接入商或模型可能不支持图片视觉输入，"
                        + "或者它的视觉接口格式不是 OpenAI/Anthropic 标准格式。"
                    )
                    db_add_message(
                        _cid,
                        "assistant",
                        error_msg,
                        model=_api_model,
                        provider_name=_api_profile_name,
                    )
                    yield error_msg

            return StreamingResponse(gen_vision(), media_type="text/plain; charset=utf-8")

    final_prompt, sources, plan = _build_regenerate_prompt_with_search(
        context_messages,
        last_user_prompt,
        body.web_search,
        body.system_prompt,
        body.api_base_url,
        body.api_auth_token,
        body.api_model or DEFAULT_MODEL,
        body.api_protocol or "",
    )

    _api_model = body.api_model or DEFAULT_MODEL
    _api_profile_name = body.api_profile_name or ""

    if keep_old:
        def gen_direct():
            yield from iter_search_status_lines(bool(plan.get("should_search") or plan.get("parse_links")), sources)
            inner = stream_direct_api_text(
                final_prompt,
                body.api_base_url,
                body.api_auth_token,
                _api_model,
                body.api_protocol or "",
                body.system_prompt,
                use_web_search=True if (body.api_protocol or "").strip().lower() == "responses" else bool(plan.get("should_search") and not sources),
            )
            yield from _stream_and_save_regenerated_answer(
                inner, cid, _api_model,
                (_api_profile_name + "｜直连流式") if _api_profile_name else "直连流式",
                body.message_id, final_prompt, sources=json.dumps(sources, ensure_ascii=False) if sources else "",
            )
        return StreamingResponse(gen_direct(), media_type="text/plain; charset=utf-8")

    def gen_stream():
        yield from iter_search_status_lines(bool(plan.get("should_search") or plan.get("parse_links")), sources)
        yield from stream_direct_and_save(
                cid,
                final_prompt,
                body.api_base_url,
                body.api_auth_token,
                _api_model,
                body.api_protocol or "",
                _api_profile_name,
                system_prompt=body.system_prompt,
                sources=json.dumps(sources, ensure_ascii=False) if sources else "",
                use_web_search=True if (body.api_protocol or "").strip().lower() == "responses" else bool(plan.get("should_search") and not sources),
            )

    return StreamingResponse(gen_stream(), media_type="text/plain; charset=utf-8")


@app.post("/api/chat/regenerate_stream")
def regenerate_stream(body: ChatBody, user=Depends(require_current_user)):
    cid = db_ensure_conversation(body.conversation_id, user_id=user["id"])

    keep_old = body.keep_old
    old_assistant_id = None

    if keep_old:
        # 获取最后一条助手消息的ID，用于后续标记
        conn = get_conn()
        row = conn.execute(
            "SELECT id FROM messages WHERE conversation_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
            (cid,),
        ).fetchone()
        conn.close()
        if row:
            old_assistant_id = row["id"]
    else:
        # 删除当前对话最后一条助手回复，让新模型重新回答
        db_delete_last_assistant_message(cid)

    history = db_get_regenerate_history(cid)

    # 找最后一条用户消息作为当前问题
    last_user_index = -1
    for i in range(len(history) - 1, -1, -1):
        if history[i].role == "user":
            last_user_index = i
            break

    if last_user_index == -1:
        return StreamingResponse(
            iter(["没有可重新回答的用户消息"]),
            media_type="text/plain; charset=utf-8",
        )

    # 上下文是最后一个用户消息之前的所有消息
    context_messages = history[:last_user_index]
    last_user_msg = history[last_user_index]
    last_user_prompt = last_user_msg.content

    # 检查最后一条用户消息是否带有图片
    last_user_images = parse_image_preview_paths(getattr(last_user_msg, "imagePreview", None))

    if last_user_images:
        vision_prompt = build_vision_text_history(context_messages)
        if vision_prompt:
            vision_prompt += "\n\n"
        vision_prompt += "用户问题：" + (last_user_prompt.strip() or "请分析我上传的图片。")
        _api_base_url = body.api_base_url
        _api_auth_token = body.api_auth_token
        _api_model = body.api_model or DEFAULT_MODEL
        _api_profile_name = body.api_profile_name or ""
        _cid = cid

        def gen_vision():
            prefix = f"正在分析 {len(last_user_images)} 张图片...\n\n"
            yield prefix
            full = ""
            try:
                for chunk in stream_direct_vision_api_text(
                    vision_prompt,
                    last_user_images,
                    _api_base_url,
                    _api_auth_token,
                    _api_model,
                    system_prompt=body.system_prompt,
                ):
                    full += chunk
                    yield chunk
                token_count = estimate_round_tokens(vision_prompt, full, image_count=len(last_user_images))
                db_add_message(
                    _cid,
                    "assistant",
                    full,
                    model=_api_model,
                    provider_name=_api_profile_name,
                    token_count=token_count,
                )
            except Exception as e:
                error_msg = (
                    "【视觉接口调用失败】\n\n"
                    + str(e)
                    + "\n\n这说明当前接入商或模型可能不支持图片视觉输入，"
                    + "或者它的视觉接口格式不是 OpenAI/Anthropic 标准格式。"
                )
                db_add_message(
                    _cid,
                    "assistant",
                    error_msg,
                    model=_api_model,
                    provider_name=_api_profile_name,
                )
                yield error_msg

        return StreamingResponse(gen_vision(), media_type="text/plain; charset=utf-8")

    final_prompt, sources, plan = _build_regenerate_prompt_with_search(
        context_messages,
        last_user_prompt,
        body.web_search,
        body.system_prompt,
        body.api_base_url,
        body.api_auth_token,
        body.api_model or DEFAULT_MODEL,
        body.api_protocol or "",
    )

    _api_model = body.api_model or DEFAULT_MODEL
    _api_profile_name = body.api_profile_name or ""

    if keep_old and old_assistant_id:
        def gen_direct():
            yield from iter_search_status_lines(bool(plan.get("should_search") or plan.get("parse_links")), sources)
            inner = stream_direct_api_text(
                final_prompt,
                body.api_base_url,
                body.api_auth_token,
                _api_model,
                body.api_protocol or "",
                body.system_prompt,
                use_web_search=True if (body.api_protocol or "").strip().lower() == "responses" else bool(plan.get("should_search") and not sources),
            )
            yield from _stream_and_save_regenerated_answer(
                inner, cid, _api_model,
                (_api_profile_name + "｜直连流式") if _api_profile_name else "直连流式",
                old_assistant_id, final_prompt, sources=json.dumps(sources, ensure_ascii=False) if sources else "",
            )
        return StreamingResponse(gen_direct(), media_type="text/plain; charset=utf-8")

    def gen_regen():
        yield from iter_search_status_lines(bool(plan.get("should_search") or plan.get("parse_links")), sources)
        yield from stream_direct_and_save(
                cid,
                final_prompt,
                body.api_base_url,
                body.api_auth_token,
                _api_model,
                body.api_protocol or "",
                _api_profile_name,
                system_prompt=body.system_prompt,
                sources=json.dumps(sources, ensure_ascii=False) if sources else "",
                use_web_search=True if (body.api_protocol or "").strip().lower() == "responses" else bool(plan.get("should_search") and not sources),
            )

    return StreamingResponse(gen_regen(), media_type="text/plain; charset=utf-8")


@app.post("/api/chat/upload_stream")
async def chat_upload_stream(
    conversation_id: str = Form(""),
    prompt: str = Form(""),
    messages_json: str = Form("[]"),
    web_search: bool = Form(False),
    web_search_explicit: bool = Form(False),
    api_base_url: str = Form(""),
    api_auth_token: str = Form(""),
    api_model: str = Form(DEFAULT_MODEL),
    api_protocol: str = Form(""),
    api_profile_name: str = Form(""),
    system_prompt: str = Form(""),
    files: list[UploadFile] = File([]),
    user=Depends(require_current_user),
):
    cid = db_ensure_conversation(conversation_id, user_id=user["id"])
    history = db_get_messages(cid)
    protocol = (api_protocol or "").strip().lower()
    if not protocol:
        protocol = "completions" if (api_model or "").lower().startswith("gpt") or "gpt-" in (api_model or "").lower() else "claude"

    text_files = []
    image_files = []
    document_files = []

    all_names = []
    web_image_paths = []
    local_image_paths = []

    for file in files:
        if file is None or not file.filename:
            continue

        fname = file.filename or "uploaded_file"

        try:
            if is_image_file(fname):
                if Path(fname).suffix.lower() not in VISION_IMAGE_EXTS:
                    raise HTTPException(
                        status_code=400,
                        detail="公网模式下暂只支持 jpg/jpeg/png/webp 图片，请先转换后再上传。",
                    )
                original, local_path, web_path = await save_uploaded_file_dual_paths(file)

                image_files.append({
                    "name": original,
                    "local_path": local_path,
                    "web_path": web_path,
                })

                all_names.append(original)
                web_image_paths.append(web_path)
                local_image_paths.append(local_path)
            else:
                original, local_path, web_path = await save_uploaded_file_dual_paths(file)
                if protocol == "responses" and is_responses_native_document(original):
                    document_files.append({
                        "name": original,
                        "local_path": local_path,
                        "web_path": web_path,
                    })
                else:
                    text = load_uploaded_text_from_path(local_path)
                    if not text:
                        text = "(文件为空，或不是可直接按 UTF-8 读取的文本文件)"
                    text_files.append({
                        "name": original,
                        "text": text,
                        "local_path": local_path,
                        "web_path": web_path,
                    })
                all_names.append(original)
        except HTTPException:
            raise
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    user_prompt = prompt.strip() or "请分析我上传的文件/图片。"

    file_names_str = ", ".join(all_names) if all_names else None

    image_preview_db = None
    if web_image_paths:
        image_preview_db = json.dumps(web_image_paths, ensure_ascii=False)

    file_context_db = None
    if text_files:
        file_context_parts = []
        for item in text_files:
            file_context_parts.append(f"文件名: {item['name']}")
            file_context_parts.append("本地文件路径: " + item["local_path"])
            file_context_parts.append("文件内容：")
            file_context_parts.append(item["text"])
            file_context_parts.append("")
        file_context_db = "\n".join(file_context_parts).strip()
    elif document_files and protocol == "responses":
        file_context_db = "\n".join(
            [f"文件名: {item['name']}\n本地文件路径: {item['local_path']}" for item in document_files]
        ).strip()

    # 先保存用户消息，让网页里能看到图片，也让后续上下文能读取附件
    db_add_message(
        cid,
        "user",
        prompt,
        file_name=file_names_str,
        image_preview=image_preview_db,
        file_context=file_context_db,
    )

    db_update_title_if_needed(cid, prompt or file_names_str or "新对话", user["id"])

    sources = []
    search_observation = ""
    search_intent = should_autonomous_search_with_context(
        user_prompt,
        history,
        force=web_search,
    )
    should_fetch_urls = bool(extract_urls_from_text(user_prompt)) and protocol != "responses"
    should_search = False
    if search_intent:
        if protocol == "responses":
            _plan = build_responses_search_plan(user_prompt, history)
            should_search = True
        else:
            search_observation, sources, _plan = resolve_search_tool_context(
                user_prompt,
                history,
                web_search,
                api_base_url,
                api_auth_token,
                api_model or DEFAULT_MODEL,
            )
            should_search = bool(_plan.get("should_search") or _plan.get("parse_links"))
    elif should_fetch_urls:
        search_observation, sources, _plan = resolve_search_tool_context(
            user_prompt,
            history,
            web_search,
            api_base_url,
            api_auth_token,
            api_model or DEFAULT_MODEL,
        )
        should_search = bool(_plan.get("should_search") or _plan.get("parse_links"))

    responses_file_items = [
        {
            "name": item["name"],
            "local_path": item["local_path"],
        }
        for item in (text_files + document_files)
    ]

    # 有图片时，强制走 base64 视觉 API
    if image_files:
        vision_prompt_parts = []

        text_history = build_vision_text_history(history)
        if text_history:
            vision_prompt_parts.append("以下是最近的文字对话历史，仅用于理解用户问题，不可替代图片内容：")
            vision_prompt_parts.append(text_history)
            vision_prompt_parts.append("")

        vision_prompt_parts.append(f"用户本次上传了 {len(image_files)} 张图片。")
        vision_prompt_parts.append("你必须读取当前上传图片的像素内容，并结合上面的文字历史回答。")
        vision_prompt_parts.append("不要只根据聊天历史、文件名、路径或上下文猜测图片内容。")
        vision_prompt_parts.append("如果你没有看到图片内容，请明确回复：我没有读取到图片内容。")
        vision_prompt_parts.append("")

        if search_observation:
            vision_prompt_parts.append(search_observation)
            vision_prompt_parts.append("搜索工具结果只作为参考；回答图片问题时仍必须优先读取当前图片内容。")
            vision_prompt_parts.append("")

        for idx, item in enumerate(image_files, 1):
            vision_prompt_parts.append(f"图片 {idx} 文件名：{item['name']}（仅用于区分，不可据此判断内容）")

        vision_prompt_parts.append("")
        vision_prompt_parts.append("用户问题：" + user_prompt)

        if text_files or document_files:
            vision_prompt_parts.append("")
            vision_prompt_parts.append("用户还上传了以下附件，可作为辅助信息：")
            for item in text_files:
                vision_prompt_parts.append(f"文件名: {item['name']}")
                vision_prompt_parts.append(item["text"])
                vision_prompt_parts.append("")
            for item in document_files:
                vision_prompt_parts.append(f"文件名: {item['name']}")
                vision_prompt_parts.append("这是当前上传的文档附件；如模型支持，请直接读取附件内容。")
                vision_prompt_parts.append("")

        vision_prompt = "\n".join(vision_prompt_parts)

        def gen():
            prefix = f"正在分析 {len(local_image_paths)} 张图片...\n\n"
            full = ""
            yield from iter_search_status_lines(should_search, sources)
            yield prefix
            try:
                for chunk in stream_direct_vision_api_text(
                    vision_prompt,
                    local_image_paths,
                    api_base_url,
                    api_auth_token,
                    api_model or DEFAULT_MODEL,
                    protocol,
                    system_prompt=system_prompt,
                    file_items=responses_file_items if protocol == "responses" else None,
                    use_web_search=True if protocol == "responses" else bool(should_search and not search_observation),
                ):
                    full += chunk
                    yield chunk

                final_answer = full

                token_count = estimate_round_tokens(
                    vision_prompt,
                    final_answer,
                    image_count=len(local_image_paths),
                )

                db_add_message(
                    cid,
                    "assistant",
                    final_answer,
                    model=api_model or DEFAULT_MODEL,
                    provider_name=api_profile_name or "",
                    token_count=token_count,
                    sources=json.dumps(sources, ensure_ascii=False) if sources else "",
                )

            except Exception as e:
                final_answer = (
                    "【视觉接口调用失败】\n\n"
                    + str(e)
                    + "\n\n这说明当前接入商或模型可能不支持图片视觉输入，"
                    + "或者它的视觉接口格式不是 OpenAI/Anthropic 标准格式。"
                )

                token_count = estimate_round_tokens(
                    vision_prompt,
                    final_answer,
                    image_count=len(local_image_paths),
                )

                db_add_message(
                    cid,
                    "assistant",
                    final_answer,
                    model=api_model or DEFAULT_MODEL,
                    provider_name=api_profile_name or "",
                    token_count=token_count,
                    sources=json.dumps(sources, ensure_ascii=False) if sources else "",
                )

                yield final_answer

        return StreamingResponse(
            gen(),
            media_type="text/plain; charset=utf-8",
        )

    # 没有图片时：文本文件/普通文字根据线路分流
    extra_parts = []

    if text_files:
        extra_parts.append("用户上传了以下文本/代码文件，请阅读内容后回答：")
        extra_parts.append("")
        for item in text_files:
            extra_parts.append(f"文件名: {item['name']}")
            extra_parts.append("文件内容：")
            extra_parts.append(item["text"])
            extra_parts.append("")
    if document_files and protocol == "responses":
        extra_parts.append("用户还上传了以下文档附件，请直接读取这些附件内容后回答：")
        extra_parts.append("")
        for item in document_files:
            extra_parts.append(f"文件名: {item['name']}")
        extra_parts.append("")

    final_user_prompt = "\n".join(extra_parts).strip()
    if final_user_prompt:
        final_user_prompt += "\n\n用户问题：" + user_prompt
    else:
        final_user_prompt = user_prompt

    if search_observation:
        final_user_prompt = (
            search_observation
            + "\n\n现在基于上述工具调用结果、附件内容和最近聊天上下文，直接回答用户。\n"
            + final_user_prompt
        )

    final_prompt = build_chat_prompt(
        messages=history,
        prompt=final_user_prompt,
    )

    if protocol == "responses" and (text_files or document_files):
        def gen_responses_file_upload():
            yield from iter_search_status_lines(should_search, sources)
            yield f"正在读取 {len(text_files) + len(document_files)} 个附件...\n\n"
            full = ""
            try:
                input_payload = build_responses_input_payload(
                    final_prompt,
                    file_items=responses_file_items,
                )
                for chunk in stream_direct_responses_api_text(
                    final_prompt,
                    api_base_url,
                    api_auth_token,
                    api_model or DEFAULT_MODEL,
                    system_prompt=system_prompt,
                    max_output_tokens=4096,
                    use_web_search=True if protocol == "responses" else bool(should_search and not search_observation),
                    input_payload=input_payload,
                ):
                    full += chunk
                    yield chunk
            finally:
                token_count = estimate_round_tokens(final_prompt, full)
                db_add_message(
                    cid,
                    "assistant",
                    full,
                    model=api_model or DEFAULT_MODEL,
                    provider_name=(api_profile_name or "") + "｜直连流式",
                    token_count=token_count,
                    sources=json.dumps(sources, ensure_ascii=False) if sources else "",
                )

        return StreamingResponse(
            gen_responses_file_upload(),
            media_type="text/plain; charset=utf-8",
        )

    def gen_text_upload():
        yield from iter_search_status_lines(should_search, sources)
        if text_files:
            yield f"正在读取 {len(text_files)} 个附件...\n\n"
        yield from stream_direct_and_save(
            cid,
            final_prompt,
            api_base_url,
            api_auth_token,
            api_model or DEFAULT_MODEL,
            protocol,
            api_profile_name or "",
            system_prompt=system_prompt,
            sources=json.dumps(sources, ensure_ascii=False) if sources else "",
            use_web_search=True if protocol == "responses" else bool(should_search and not search_observation),
        )

    return StreamingResponse(
        gen_text_upload(),
        media_type="text/plain; charset=utf-8",
    )




@app.get("/api/conversations/{conversation_id}/export.md")
def export_conversation_markdown(conversation_id: str, user=Depends(require_current_user)):
    from fastapi.responses import Response, PlainTextResponse

    try:
        conn = get_conn()

        conv = conn.execute(
            "SELECT title FROM conversations WHERE id=? AND user_id=?",
            (conversation_id, user["id"]),
        ).fetchone()

        if not conv:
            conn.close()
            raise HTTPException(status_code=404, detail="会话不存在")

        rows = conn.execute(
            """
            SELECT role, content, file_name, model, provider_name, created_at
            FROM messages
            WHERE conversation_id=?
            ORDER BY id ASC
            """,
            (conversation_id,),
        ).fetchall()

        conn.close()

        title = "对话导出"
        if conv and "title" in conv.keys() and conv["title"]:
            title = conv["title"]

        lines = []
        lines.append(f"# {title}")
        lines.append("")

        for row in rows:
            role = "用户" if row["role"] == "user" else "助手"
            lines.append(f"## {role}")
            lines.append("")

            file_name = row["file_name"] if "file_name" in row.keys() else ""
            if file_name:
                lines.append(f"> 附件: {file_name}")
                lines.append("")

            if row["role"] == "assistant":
                meta = []
                provider_name = row["provider_name"] if "provider_name" in row.keys() else ""
                model = row["model"] if "model" in row.keys() else ""

                if provider_name:
                    meta.append(f"接入商: {provider_name}")
                if model:
                    meta.append(f"模型: {model}")
                if meta:
                    lines.append("> " + " · ".join(meta))
                    lines.append("")

            lines.append(row["content"] or "")
            lines.append("")

        md = "\n".join(lines)
        # Add a UTF-8 BOM so downloaded Markdown opens correctly in Windows
        # editors that still guess ANSI/GBK for plain text files.
        md_bytes = ("\ufeff" + md).encode("utf-8")

        # HTTP Header 只能安全放 latin-1/ASCII，中文文件名必须用 filename*
        ascii_name = "".join(
            ch if ch.isascii() and (ch.isalnum() or ch in "-_.") else "_"
            for ch in title
        ).strip("._")[:50] or conversation_id or "conversation"

        utf8_name = quote(f"{title}.md")

        return Response(
            content=md_bytes,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f"attachment; filename=\"{ascii_name}.md\"; filename*=UTF-8''{utf8_name}",
                "X-Content-Type-Options": "nosniff",
            }
        )

    except Exception as e:
        return PlainTextResponse(
            f"导出失败：{e}",
            status_code=500
        )

register_admin_backend(app)
