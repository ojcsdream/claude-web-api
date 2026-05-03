from pathlib import Path
import os
import subprocess
import json
import uuid
import time
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import init_db, get_conn

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

DEFAULT_MODEL = "claude-opus-4-7"

# 极速模式：限制每次传给模型的历史上下文
# 数值越小越快，但长期记忆越弱
MAX_CONTEXT_MESSAGES = 100
MAX_CONTEXT_CHARS = 180000

# 图片请求时也带文字历史，但不重复发送旧图片
VISION_CONTEXT_MESSAGES = 20
VISION_CONTEXT_CHARS = 30000

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

init_db()

app = FastAPI(title="Claude Web")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


class MessageItem(BaseModel):
    id: Optional[int] = None
    role: str
    content: str
    fileName: Optional[str] = None
    imagePreview: Optional[str] = None
    model: Optional[str] = None
    providerName: Optional[str] = None
    tokenCount: Optional[int] = None


class ChatBody(BaseModel):
    conversation_id: str = ""
    prompt: str = ""
    messages: List[MessageItem] = []
    message_id: Optional[int] = None
    api_base_url: str = ""
    api_auth_token: str = ""
    api_model: str = DEFAULT_MODEL
    api_profile_name: str = ""


class ConversationCreateBody(BaseModel):
    title: str = "新对话"


class ConversationRenameBody(BaseModel):
    title: str


class ConversationPinBody(BaseModel):
    pinned: bool = True


class ApiProfileBody(BaseModel):
    name: str
    base_url: str
    auth_token: str
    model: str = DEFAULT_MODEL
    is_default: bool = False




def estimate_tokens(text: str) -> int:
    """
    粗略 token 估算：
    - 中文：约 1 字 ≈ 1 token
    - 英文/代码：约 4 字符 ≈ 1 token
    混合场景取折中算法。
    """
    if not text:
        return 0

    chinese = 0
    other = 0

    for ch in text:
        if "\\u4e00" <= ch <= "\\u9fff":
            chinese += 1
        else:
            other += 1

    return max(1, chinese + other // 4)


def estimate_round_tokens(input_text: str, output_text: str, image_count: int = 0) -> int:
    # 图片 token 很难精确，不同模型差异很大，这里给每张图一个保守估算值
    return estimate_tokens(input_text) + estimate_tokens(output_text) + image_count * 1000


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return uuid.uuid4().hex


def db_create_conversation(title: str = "新对话") -> str:
    cid = new_id()
    ts = now_ms()
    conn = get_conn()
    conn.execute(
        "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (cid, title or "新对话", ts, ts),
    )
    conn.commit()
    conn.close()
    return cid


def db_ensure_conversation(cid: str, title: str = "新对话") -> str:
    if cid:
        conn = get_conn()
        row = conn.execute("SELECT id FROM conversations WHERE id=?", (cid,)).fetchone()
        if row:
            conn.close()
            return cid
        conn.close()

    return db_create_conversation(title)


def db_add_message(
    conversation_id: str,
    role: str,
    content: str,
    file_name: Optional[str] = None,
    image_preview: Optional[str] = None,
    model: Optional[str] = None,
    provider_name: Optional[str] = None,
    token_count: Optional[int] = None,
):
    ts = now_ms()
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO messages (conversation_id, role, content, file_name, image_preview, model, provider_name, token_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (conversation_id, role, content or "", file_name, image_preview, model, provider_name, token_count, ts),
    )
    conn.execute(
        "UPDATE conversations SET updated_at=? WHERE id=?",
        (ts, conversation_id),
    )
    conn.commit()
    conn.close()


def db_update_title_if_needed(conversation_id: str, title_source: str):
    conn = get_conn()
    row = conn.execute("SELECT title FROM conversations WHERE id=?", (conversation_id,)).fetchone()
    if row and (row["title"].startswith("新对话") or row["title"].strip() == ""):
        title = (title_source or "新对话").strip().replace("\n", " ")[:18] or "新对话"
        conn.execute(
            "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
            (title, now_ms(), conversation_id),
        )
        conn.commit()
    conn.close()




def db_list_api_profiles():
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, name, base_url, auth_token, model, is_default, created_at, updated_at FROM api_profiles ORDER BY is_default DESC, updated_at DESC"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def db_save_api_profile(profile_id: str, body: ApiProfileBody):
    ts = now_ms()
    pid = profile_id or new_id()

    conn = get_conn()

    if body.is_default:
        conn.execute("UPDATE api_profiles SET is_default=0")

    old = conn.execute("SELECT id FROM api_profiles WHERE id=?", (pid,)).fetchone()

    if old:
        conn.execute(
            """
            UPDATE api_profiles
            SET name=?, base_url=?, auth_token=?, model=?, is_default=?, updated_at=?
            WHERE id=?
            """,
            (
                body.name.strip() or "未命名接入商",
                body.base_url.strip(),
                body.auth_token.strip(),
                body.model.strip() or DEFAULT_MODEL,
                1 if body.is_default else 0,
                ts,
                pid,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO api_profiles
            (id, name, base_url, auth_token, model, is_default, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pid,
                body.name.strip() or "未命名接入商",
                body.base_url.strip(),
                body.auth_token.strip(),
                body.model.strip() or DEFAULT_MODEL,
                1 if body.is_default else 0,
                ts,
                ts,
            ),
        )

    conn.commit()
    conn.close()
    return pid


def db_delete_api_profile(profile_id: str):
    conn = get_conn()
    conn.execute("DELETE FROM api_profiles WHERE id=?", (profile_id,))
    conn.commit()
    conn.close()


def db_set_default_api_profile(profile_id: str):
    conn = get_conn()
    conn.execute("UPDATE api_profiles SET is_default=0")
    conn.execute(
        "UPDATE api_profiles SET is_default=1, updated_at=? WHERE id=?",
        (now_ms(), profile_id),
    )
    conn.commit()
    conn.close()




def db_delete_last_assistant_message(conversation_id: str):
    conn = get_conn()
    row = conn.execute(
        """
        SELECT id FROM messages
        WHERE conversation_id=? AND role='assistant'
        ORDER BY id DESC
        LIMIT 1
        """,
        (conversation_id,),
    ).fetchone()

    if row:
        conn.execute("DELETE FROM messages WHERE id=?", (row["id"],))
        conn.commit()

    conn.close()


def db_get_regenerate_history(conversation_id: str) -> List[MessageItem]:
    """
    用于重新回答：
    - 如果最后一条是 assistant，先在外部删除
    - 返回完整历史，此时最后一条通常是 user
    """
    return db_get_messages(conversation_id)


def db_get_messages(conversation_id: str) -> List[MessageItem]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, role, content, file_name, image_preview, model, provider_name, token_count FROM messages WHERE conversation_id=? ORDER BY id ASC",
        (conversation_id,),
    ).fetchall()
    conn.close()

    return [
        MessageItem(
            id=row["id"],
            role=row["role"],
            content=row["content"],
            fileName=row["file_name"],
            imagePreview=row["image_preview"],
            model=row["model"] if "model" in row.keys() else None,
            providerName=row["provider_name"] if "provider_name" in row.keys() else None,
            tokenCount=row["token_count"] if "token_count" in row.keys() else None,
        )
        for row in rows
    ]




def db_delete_message_and_after_raw(conversation_id: str, message_id: int):
    conn = get_conn()
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


def db_get_message_by_id(conversation_id: str, message_id: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT id, role, content FROM messages WHERE conversation_id=? AND id=?",
        (conversation_id, message_id),
    ).fetchone()
    conn.close()
    return row


def db_get_messages_before_id(conversation_id: str, message_id: int) -> List[MessageItem]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, role, content, file_name, image_preview, model, provider_name, token_count FROM messages WHERE conversation_id=? AND id<? ORDER BY id ASC",
        (conversation_id, message_id),
    ).fetchall()
    conn.close()

    return [
        MessageItem(
            id=row["id"],
            role=row["role"],
            content=row["content"],
            fileName=row["file_name"],
            imagePreview=row["image_preview"],
            model=row["model"] if "model" in row.keys() else None,
            providerName=row["provider_name"] if "provider_name" in row.keys() else None,
            tokenCount=row["token_count"] if "token_count" in row.keys() else None,
        )
        for row in rows
    ]


def is_image_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_EXTS


def trim_context_messages(messages: List[MessageItem]) -> List[MessageItem]:
    # 只保留最近 N 条，并限制总字符数，显著降低长对话延迟
    recent = messages[-MAX_CONTEXT_MESSAGES:]
    result = []
    total = 0

    for msg in reversed(recent):
        content = msg.content or ""
        remain = MAX_CONTEXT_CHARS - total

        if remain <= 0:
            break

        if len(content) > remain:
            content = content[-remain:]

        result.append(
            MessageItem(
                role=msg.role,
                content=content,
                fileName=getattr(msg, "fileName", None),
                imagePreview=getattr(msg, "imagePreview", None),
            )
        )
        total += len(content)

    return list(reversed(result))




def build_vision_text_history(messages: List[MessageItem]) -> str:
    """
    图片请求时附带最近文字上下文。
    不重复发送历史图片，只发送当前用户上传的图片。
    """
    recent = messages[-VISION_CONTEXT_MESSAGES:]
    parts = []
    total = 0

    for msg in recent:
        text = (msg.content or "").strip()
        if not text:
            continue

        remain = VISION_CONTEXT_CHARS - total
        if remain <= 0:
            break

        if len(text) > remain:
            text = text[-remain:]

        role = "用户" if msg.role == "user" else "助手"
        parts.append(f"{role}: {text}")
        total += len(text)

    return "\\n".join(parts).strip()


def build_chat_prompt(
    messages: List[MessageItem],
    prompt: str,
    file_name: Optional[str] = None,
    file_text: Optional[str] = None,
    image_rel_path: Optional[str] = None,
) -> str:
    messages = trim_context_messages(messages)

    parts = [
        "基于下面最近的聊天上下文继续回答。",
        "请直接回答，不要重复角色标签。",
        "如果涉及数学公式，请使用标准 LaTeX。独立公式必须用 $$...$$ 包裹，行内公式用 $...$。分式必须使用 \frac{分子}{分母}，不要使用 a/b 这种斜杠分式；平方根必须使用 \sqrt{}；推导公式尽量使用 align 环境。",
        ""
    ]

    for msg in messages:
        role = "用户" if msg.role == "user" else "助手"
        parts.append(f"{role}: {msg.content}")
        parts.append("")

    if image_rel_path:
        parts.append(f"用户上传了图片文件: {file_name}")
        parts.append(f"本地图片路径: {image_rel_path}")
        parts.append("请务必读取这个本地图片路径中的图片内容，不要只根据文件名猜测。")
        parts.append("如果你无法读取图片，请明确说明无法读取，而不要编造图片内容。")
        parts.append("")

    elif file_name and file_text:
        parts.append(f"用户上传了文件: {file_name}")
        parts.append("以下是文件内容：")
        parts.append("")
        parts.append(file_text)
        parts.append("")

    if prompt.strip():
        parts.append(f"用户: {prompt.strip()}")
        parts.append("")
        parts.append("助手:")

    return "\n".join(parts).strip()


def make_env(api_base_url: str = "", api_auth_token: str = ""):
    env = os.environ.copy()
    if api_base_url.strip():
        env["ANTHROPIC_BASE_URL"] = api_base_url.strip()
    if api_auth_token.strip():
        env["ANTHROPIC_AUTH_TOKEN"] = api_auth_token.strip()
    return env


def run_claude(
    prompt: str,
    api_base_url: str = "",
    api_auth_token: str = "",
    api_model: str = DEFAULT_MODEL,
) -> str:
    proc = subprocess.run(
        ["claude", "--model", api_model or DEFAULT_MODEL, "-p", prompt],
        capture_output=True,
        text=True,
        timeout=300,
        env=make_env(api_base_url, api_auth_token),
        cwd=str(BASE_DIR),
    )

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode != 0:
        raise RuntimeError(stderr or stdout or f"claude exited with code {proc.returncode}")

    return stdout or "(无输出)"


def stream_claude_text(
    prompt: str,
    api_base_url: str = "",
    api_auth_token: str = "",
    api_model: str = DEFAULT_MODEL,
):
    proc = subprocess.Popen(
        ["claude", "--model", api_model or DEFAULT_MODEL, "-p", prompt],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=0,
        env=make_env(api_base_url, api_auth_token),
        cwd=str(BASE_DIR),
    )

    try:
        if proc.stdout is not None:
            while True:
                chunk = proc.stdout.read(32)
                if chunk == "":
                    break
                yield chunk
        proc.wait(timeout=5)
        if proc.returncode != 0:
            yield f"\n[claude exited with code {proc.returncode}]\n"
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.poll() is None:
            proc.kill()


def stream_and_save(
    conversation_id: str,
    final_prompt: str,
    api_base_url: str,
    api_auth_token: str,
    api_model: str,
    provider_name: str = "",
):
    full = ""
    for ch in stream_claude_text(final_prompt, api_base_url, api_auth_token, api_model):
        full += ch
        yield ch
    token_count = estimate_round_tokens(final_prompt, full)

    db_add_message(
        conversation_id,
        "assistant",
        full,
        model=api_model,
        provider_name=provider_name,
        token_count=token_count,
    )




def guess_media_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in [".jpg", ".jpeg"]:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    if ext == ".gif":
        return "image/gif"
    return "image/jpeg"


def local_upload_path_to_abs(local_path: str) -> Path:
    # local_path like ./uploads/xxx.jpg
    clean = local_path.replace("./", "", 1)
    return BASE_DIR / clean


def call_direct_vision_api(
    prompt: str,
    image_local_paths: list[str],
    api_base_url: str,
    api_auth_token: str,
    api_model: str,
) -> str:
    """
    真正把图片作为 base64 视觉输入发给 API。
    GPT 模型走 OpenAI chat.completions。
    Claude 模型走 Anthropic messages。
    """
    import base64
    import urllib.request
    import urllib.error

    base_url = api_base_url.strip().rstrip("/")
    token = api_auth_token.strip()
    model = api_model.strip() or DEFAULT_MODEL

    if not base_url:
        raise RuntimeError("缺少 API URL")
    if not token:
        raise RuntimeError("缺少 API Key")
    if not image_local_paths:
        raise RuntimeError("没有图片")

    images = []
    for local_path in image_local_paths:
        abs_path = local_upload_path_to_abs(local_path)
        if not abs_path.exists():
            raise RuntimeError(f"图片不存在: {local_path}")

        raw = abs_path.read_bytes()
        b64 = base64.b64encode(raw).decode("utf-8")
        media_type = guess_media_type(local_path)

        images.append({
            "local_path": local_path,
            "media_type": media_type,
            "base64": b64,
        })

    lower_model = model.lower()

    # GPT / OpenAI compatible
    if lower_model.startswith("gpt") or "gpt-" in lower_model:
        content = [{"type": "text", "text": prompt}]

        for img in images:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{img['media_type']};base64,{img['base64']}"
                }
            })

        body = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": content
                }
            ],
            "temperature": 0.3
        }

        url = base_url + "/v1/chat/completions"

        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))

            return (
                data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
            ) or "(视觉接口无输出)"

        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError("OpenAI视觉接口失败: " + (err or str(e)))

    # Anthropic compatible
    content = [{"type": "text", "text": prompt}]

    for img in images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img["media_type"],
                "data": img["base64"],
            }
        })

    body = {
        "model": model,
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": content,
            }
        ]
    }

    url = base_url + "/v1/messages"

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": token,
            "Authorization": "Bearer " + token,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))

        parts = []
        for item in data.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))

        return "\\n".join(parts).strip() or "(视觉接口无输出)"

    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError("Anthropic视觉接口失败: " + (err or str(e)))


async def read_uploaded_text(file: UploadFile) -> str:
    raw = await file.read()
    if len(raw) > 1024 * 1024:
        raw = raw[:1024 * 1024]
    text = raw.decode("utf-8", errors="ignore").strip()
    return text or "(文件为空，或不是可直接按 UTF-8 读取的文本文件)"




async def save_uploaded_file_dual_paths(file: UploadFile) -> tuple[str, str, str]:
    """
    返回:
    - original_name: 原文件名
    - local_path: 给 claude code 读取的本地相对路径 ./uploads/xxx
    - web_path: 给浏览器显示的 URL 路径 /uploads/xxx
    """
    original_name = file.filename or "uploaded_file"
    suffix = Path(original_name).suffix or ".bin"
    safe_name = f"{uuid.uuid4().hex}{suffix}"
    save_path = UPLOAD_DIR / safe_name

    raw = await file.read()
    save_path.write_bytes(raw)

    local_path = f"./uploads/{safe_name}"
    web_path = f"/uploads/{safe_name}"

    return original_name, local_path, web_path


async def save_uploaded_file(file: UploadFile) -> tuple[str, str]:
    original_name = file.filename or "uploaded_file"
    suffix = Path(original_name).suffix
    safe_name = f"{uuid.uuid4().hex}{suffix}"
    save_path = UPLOAD_DIR / safe_name

    raw = await file.read()
    save_path.write_bytes(raw)

    rel_path = f"./uploads/{safe_name}"
    return original_name, rel_path


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "message": "backend running", "default_model": DEFAULT_MODEL}






@app.post("/api/profiles/test")
def test_api_profile(body: ApiProfileBody):
    try:
        reply = run_claude(
            "只回复：API配置可用",
            api_base_url=body.base_url,
            api_auth_token=body.auth_token,
            api_model=body.model or DEFAULT_MODEL,
        )
        return {
            "ok": True,
            "reply": reply,
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
            "x-api-key": token,
            "Authorization": "Bearer " + token,
            "anthropic-version": "2023-06-01",
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
        return {
            "ok": False,
            "models": [],
            "error": f"HTTP {e.code}: " + (err or str(e)),
        }
    except Exception as e:
        return {
            "ok": False,
            "models": [],
            "error": str(e),
        }


@app.post("/api/profiles/models")
def fetch_api_profile_models_post(body: ApiProfileBody):
    return do_fetch_models_from_profile(body.base_url, body.auth_token)


@app.get("/api/profiles/models")
def fetch_api_profile_models_get(profile_id: str = ""):
    if not profile_id:
        return {
            "ok": False,
            "models": [],
            "error": "缺少 profile_id"
        }

    conn = get_conn()
    row = conn.execute(
        "SELECT base_url, auth_token FROM api_profiles WHERE id=?",
        (profile_id,),
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
def list_api_profiles():
    return {
        "ok": True,
        "profiles": db_list_api_profiles(),
    }


@app.post("/api/profiles")
def create_api_profile(body: ApiProfileBody):
    pid = db_save_api_profile("", body)
    return {"ok": True, "id": pid}


@app.put("/api/profiles/{profile_id}")
def update_api_profile(profile_id: str, body: ApiProfileBody):
    pid = db_save_api_profile(profile_id, body)
    return {"ok": True, "id": pid}


@app.delete("/api/profiles/{profile_id}")
def delete_api_profile(profile_id: str):
    db_delete_api_profile(profile_id)
    return {"ok": True}


@app.post("/api/profiles/{profile_id}/default")
def set_default_api_profile(profile_id: str):
    db_set_default_api_profile(profile_id)
    return {"ok": True}




@app.get("/api/search")
def search_messages(q: str = ""):
    keyword = (q or "").strip()

    if not keyword:
        return {"ok": True, "results": []}

    like = f"%{keyword}%"

    conn = get_conn()
    rows = conn.execute(
        """
        SELECT
            messages.id AS message_id,
            messages.conversation_id AS conversation_id,
            messages.role AS role,
            messages.content AS content,
            messages.created_at AS created_at,
            conversations.title AS conversation_title
        FROM messages
        JOIN conversations ON conversations.id = messages.conversation_id
        WHERE messages.content LIKE ?
           OR conversations.title LIKE ?
           OR messages.file_name LIKE ?
        ORDER BY messages.created_at DESC
        LIMIT 50
        """,
        (like, like, like),
    ).fetchall()
    conn.close()

    results = []

    for row in rows:
        content = row["content"] or ""
        idx = content.lower().find(keyword.lower())

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
            "created_at": row["created_at"],
        })

    return {
        "ok": True,
        "results": results,
    }


@app.get("/api/conversations")
def list_conversations():
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, title, created_at, updated_at, is_pinned FROM conversations ORDER BY is_pinned DESC, updated_at DESC"
    ).fetchall()
    conn.close()
    return {
        "ok": True,
        "conversations": [dict(row) for row in rows],
    }


@app.post("/api/conversations")
def create_conversation(body: ConversationCreateBody):
    cid = db_create_conversation(body.title or "新对话")
    return {"ok": True, "id": cid}


@app.get("/api/conversations/{conversation_id}/messages")
def get_conversation_messages(conversation_id: str):
    msgs = db_get_messages(conversation_id)
    return {
        "ok": True,
        "messages": [m.model_dump() for m in msgs],
    }




@app.delete("/api/conversations/{conversation_id}/messages/{message_id}/after")
def delete_message_and_after(conversation_id: str, message_id: int):
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
def delete_conversation(conversation_id: str):
    conn = get_conn()
    conn.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
    conn.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
    conn.commit()
    conn.close()
    return {"ok": True}




@app.post("/api/conversations/{conversation_id}/pin")
def pin_conversation(conversation_id: str, body: ConversationPinBody):
    conn = get_conn()
    conn.execute(
        "UPDATE conversations SET is_pinned=?, updated_at=? WHERE id=?",
        (1 if body.pinned else 0, now_ms(), conversation_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/conversations/{conversation_id}/rename")
def rename_conversation(conversation_id: str, body: ConversationRenameBody):
    conn = get_conn()
    conn.execute(
        "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
        (body.title.strip()[:50] or "新对话", now_ms(), conversation_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/echo")
def echo(body: ChatBody):
    try:
        cid = db_ensure_conversation(body.conversation_id)
        history = db_get_messages(cid)
        final_prompt = build_chat_prompt(history, body.prompt)

        db_add_message(cid, "user", body.prompt)
        db_update_title_if_needed(cid, body.prompt)

        reply = run_claude(
            final_prompt,
            api_base_url=body.api_base_url,
            api_auth_token=body.api_auth_token,
            api_model=body.api_model or DEFAULT_MODEL,
        )
        db_add_message(cid, "assistant", reply)

        return {"ok": True, "conversation_id": cid, "reply": reply}
    except Exception as e:
        return {"ok": False, "reply": f"调用失败: {e}"}


@app.post("/api/chat/stream")
def chat_stream(body: ChatBody):
    cid = db_ensure_conversation(body.conversation_id)
    history = db_get_messages(cid)

    final_prompt = build_chat_prompt(history, body.prompt)

    db_add_message(cid, "user", body.prompt)
    db_update_title_if_needed(cid, body.prompt)

    return StreamingResponse(
        stream_and_save(
            cid,
            final_prompt,
            body.api_base_url,
            body.api_auth_token,
            body.api_model or DEFAULT_MODEL,
            body.api_profile_name or "",
        ),
        media_type="text/plain; charset=utf-8",
    )






@app.post("/api/chat/regenerate_from_stream")
def regenerate_from_stream(body: ChatBody):
    cid = db_ensure_conversation(body.conversation_id)

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
    last_user_prompt = before[last_user_index].content

    # 删除目标消息以及之后所有消息
    db_delete_message_and_after_raw(cid, body.message_id)

    final_prompt = build_chat_prompt(
        messages=context_messages,
        prompt=last_user_prompt,
    )

    return StreamingResponse(
        stream_and_save(
            cid,
            final_prompt,
            body.api_base_url,
            body.api_auth_token,
            body.api_model or DEFAULT_MODEL,
            body.api_profile_name or "",
        ),
        media_type="text/plain; charset=utf-8",
    )


@app.post("/api/chat/regenerate_stream")
def regenerate_stream(body: ChatBody):
    cid = db_ensure_conversation(body.conversation_id)

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
    last_user_prompt = history[last_user_index].content

    final_prompt = build_chat_prompt(
        messages=context_messages,
        prompt=last_user_prompt,
    )

    return StreamingResponse(
        stream_and_save(
            cid,
            final_prompt,
            body.api_base_url,
            body.api_auth_token,
            body.api_model or DEFAULT_MODEL,
            body.api_profile_name or "",
        ),
        media_type="text/plain; charset=utf-8",
    )


@app.post("/api/chat/upload_stream")
async def chat_upload_stream(
    conversation_id: str = Form(""),
    prompt: str = Form(""),
    messages_json: str = Form("[]"),
    api_base_url: str = Form(""),
    api_auth_token: str = Form(""),
    api_model: str = Form(DEFAULT_MODEL),
    api_profile_name: str = Form(""),
    files: list[UploadFile] = File([]),
):
    cid = db_ensure_conversation(conversation_id)
    history = db_get_messages(cid)

    text_files = []
    image_files = []

    all_names = []
    web_image_paths = []
    local_image_paths = []

    for file in files:
        if file is None or not file.filename:
            continue

        fname = file.filename or "uploaded_file"

        if is_image_file(fname):
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
            text = await read_uploaded_text(file)
            text_files.append({
                "name": fname,
                "text": text,
            })
            all_names.append(fname)

    user_prompt = prompt.strip() or "请分析我上传的文件/图片。"

    file_names_str = ", ".join(all_names) if all_names else None

    image_preview_db = None
    if web_image_paths:
        image_preview_db = json.dumps(web_image_paths, ensure_ascii=False)

    # 先保存用户消息，让网页里能看到图片
    db_add_message(
        cid,
        "user",
        prompt,
        file_name=file_names_str,
        image_preview=image_preview_db,
    )

    db_update_title_if_needed(cid, prompt or file_names_str or "新对话")

    # 有图片时，强制走 base64 视觉 API，不再走 claude CLI 读路径
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

        for idx, item in enumerate(image_files, 1):
            vision_prompt_parts.append(f"图片 {idx} 文件名：{item['name']}（仅用于区分，不可据此判断内容）")

        vision_prompt_parts.append("")
        vision_prompt_parts.append("用户问题：" + user_prompt)

        if text_files:
            vision_prompt_parts.append("")
            vision_prompt_parts.append("用户还上传了以下文本文件，可作为辅助信息：")
            for item in text_files:
                vision_prompt_parts.append(f"文件名: {item['name']}")
                vision_prompt_parts.append(item["text"])
                vision_prompt_parts.append("")

        vision_prompt = "\n".join(vision_prompt_parts)

        def gen():
            try:
                answer = call_direct_vision_api(
                    vision_prompt,
                    local_image_paths,
                    api_base_url,
                    api_auth_token,
                    api_model or DEFAULT_MODEL,
                )

                debug = (
                    f"【视觉接口已调用｜图片数: {len(local_image_paths)}"
                    f"｜模型: {api_model or DEFAULT_MODEL}"
                    f"｜接入商: {api_profile_name or api_base_url}】\n\n"
                )

                final_answer = debug + answer

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
                )

                yield final_answer

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
                )

                yield final_answer

        return StreamingResponse(
            gen(),
            media_type="text/plain; charset=utf-8",
        )

    # 没有图片时，文本文件继续走 claude CLI
    extra_parts = []

    if text_files:
        extra_parts.append("用户上传了以下文本/代码文件，请阅读内容后回答：")
        extra_parts.append("")
        for item in text_files:
            extra_parts.append(f"文件名: {item['name']}")
            extra_parts.append("文件内容：")
            extra_parts.append(item["text"])
            extra_parts.append("")

    final_user_prompt = "\n".join(extra_parts).strip()
    if final_user_prompt:
        final_user_prompt += "\n\n用户问题：" + user_prompt
    else:
        final_user_prompt = user_prompt

    final_prompt = build_chat_prompt(
        messages=history,
        prompt=final_user_prompt,
    )

    return StreamingResponse(
        stream_and_save(
            cid,
            final_prompt,
            api_base_url,
            api_auth_token,
            api_model or DEFAULT_MODEL,
            api_profile_name or "",
        ),
        media_type="text/plain; charset=utf-8",
    )



@app.get("/api/conversations/{conversation_id}/export.md")
def export_conversation_markdown(conversation_id: str):
    from fastapi.responses import Response

    conn = get_conn()

    conv = conn.execute(
        "SELECT title FROM conversations WHERE id=?",
        (conversation_id,),
    ).fetchone()

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

    title = conv["title"] if conv else "对话导出"

    lines = []
    lines.append(f"# {title}")
    lines.append("")

    for row in rows:
        role = "用户" if row["role"] == "user" else "助手"
        lines.append(f"## {role}")
        lines.append("")

        if row["file_name"]:
            lines.append(f"> 附件: {row['file_name']}")
            lines.append("")

        if row["role"] == "assistant":
            meta = []
            if "provider_name" in row.keys() and row["provider_name"]:
                meta.append(f"接入商: {row['provider_name']}")
            if "model" in row.keys() and row["model"]:
                meta.append(f"模型: {row['model']}")
            if meta:
                lines.append("> " + " · ".join(meta))
                lines.append("")

        lines.append(row["content"] or "")
        lines.append("")

    md = "\\n".join(lines)

    safe_name = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in title)[:50] or conversation_id

    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}.md"'
        }
    )
