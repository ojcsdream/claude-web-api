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
    route_mode: str = "cc"  # cc=Claude Code本地代理, direct=第三方API直连流式


class ConversationCreateBody(BaseModel):
    title: str = "新对话"


class ConversationRenameBody(BaseModel):
    title: str

class TerminalBody(BaseModel):
    command: str
    cwd: str = "/root"
    timeout: int = 60

class AgentBody(BaseModel):
    conversation_id: str = ""
    task: str
    cwd: str = "/root"
    max_steps: int = 8
    timeout: int = 120
    api_base_url: str = ""
    api_auth_token: str = ""
    api_model: str = DEFAULT_MODEL
    api_profile_name: str = ""




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
        ["claude", "--dangerously-skip-permissions", "--model", api_model or DEFAULT_MODEL, "-p", prompt],
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
        ["claude", "--dangerously-skip-permissions", "--model", api_model or DEFAULT_MODEL, "-p", prompt],
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



def extract_urls_from_text(text: str, max_urls: int = 3):
    """
    从文本中提取 http/https 链接。
    """
    import re
    urls = re.findall(r'https?://[^\s\]\)\}，。、“”‘’<>"]+', text or "")
    result = []
    for u in urls:
        u = u.rstrip(".,;:!?，。；：！？")
        if u and u not in result:
            result.append(u)
    return result[:max_urls]


def fetch_webpage_text(url: str, max_chars: int = 12000) -> str:
    """
    读取网页并提取正文纯文本。
    只使用 Python 标准库，避免新增依赖。
    """
    import urllib.request
    import re
    from html.parser import HTMLParser

    class SimpleTextParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
            self.skip_tag = None

        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            if tag in ("script", "style", "noscript", "svg"):
                self.skip_tag = tag
            if tag in ("p", "br", "div", "section", "article", "li", "h1", "h2", "h3"):
                self.parts.append("\n")

        def handle_endtag(self, tag):
            tag = tag.lower()
            if self.skip_tag == tag:
                self.skip_tag = None
            if tag in ("p", "div", "section", "article", "li"):
                self.parts.append("\n")

        def handle_data(self, data):
            if self.skip_tag:
                return
            data = data.strip()
            if data:
                self.parts.append(data + " ")

        def get_text(self):
            return "".join(self.parts)

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 Claude-Web/1.0",
                "Accept": "text/html,text/plain,*/*",
            },
            method="GET",
        )

        with urllib.request.urlopen(req, timeout=15) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(1024 * 1024 * 2)

        charset = "utf-8"
        m = re.search(r"charset=([\w\-]+)", content_type, re.I)
        if m:
            charset = m.group(1)

        html = raw.decode(charset, errors="ignore")

        if "text/plain" in content_type:
            text = html
        else:
            parser = SimpleTextParser()
            parser.feed(html)
            text = parser.get_text()

        text = re.sub(r"\s+", " ", text).strip()

        if not text:
            return "[网页读取成功，但没有提取到有效正文]"

        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[网页内容过长，已截断]"

        return text

    except Exception as e:
        return f"[读取网页失败：{e}]"


def enhance_prompt_with_url_fetch(user_prompt: str) -> str:
    """
    直连模式专用：
    如果用户输入里包含 URL，则读取网页内容并拼接到 prompt。
    """
    urls = extract_urls_from_text(user_prompt)
    if not urls:
        return user_prompt

    parts = []
    parts.append("用户输入中包含网页链接。以下是后端自动读取到的网页内容，请结合这些内容回答。")
    parts.append("")

    for i, url in enumerate(urls, 1):
        parts.append(f"【网页 {i}】{url}")
        parts.append(fetch_webpage_text(url))
        parts.append("")

    parts.append("用户原始问题：")
    parts.append(user_prompt)

    return "\n".join(parts)


def stream_direct_api_text(
    prompt: str,
    api_base_url: str,
    api_auth_token: str,
    api_model: str = DEFAULT_MODEL,
):
    """
    第三方 API 直连真流式。
    - GPT/OpenAI兼容模型：走 /v1/chat/completions stream
    - Claude/Anthropic兼容模型：走 /v1/messages stream
    注意：这条线路不经过 Claude Code，所以不能操作本地文件。
    """
    import urllib.request
    import urllib.error

    base_url = api_base_url.strip().rstrip("/")
    token = api_auth_token.strip()
    model = (api_model or DEFAULT_MODEL).strip()

    if not base_url:
        yield "直连模式缺少 API URL"
        return
    if not token:
        yield "直连模式缺少 API Key"
        return

    lower_model = model.lower()

    # OpenAI-compatible / GPT-compatible
    if lower_model.startswith("gpt") or "gpt-" in lower_model:
        url = base_url + "/v1/chat/completions"
        body = {
            "model": model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "stream": True,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue

                    data = line[5:].strip()
                    if data == "[DONE]":
                        break

                    try:
                        obj = json.loads(data)
                        delta = (
                            obj.get("choices", [{}])[0]
                            .get("delta", {})
                            .get("content", "")
                        )
                        if delta:
                            yield delta
                    except Exception:
                        continue

        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="ignore")
            yield "\n[直连OpenAI流式接口失败]\n" + (err or str(e))
        except Exception as e:
            yield "\n[直连OpenAI流式接口失败]\n" + str(e)

        return

    # Anthropic-compatible / Claude-compatible
    url = base_url + "/v1/messages"
    body = {
        "model": model,
        "max_tokens": 4096,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "stream": True,
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": token,
        "Authorization": "Bearer " + token,
        "anthropic-version": "2023-06-01",
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                if not line.startswith("data:"):
                    continue

                data = line[5:].strip()
                if data == "[DONE]":
                    break

                try:
                    obj = json.loads(data)
                    typ = obj.get("type")

                    # Anthropic 标准流式文本增量
                    if typ == "content_block_delta":
                        delta = obj.get("delta", {})
                        text = delta.get("text", "")
                        if text:
                            yield text

                    # 兼容某些代理把文本放在 completion/content 里
                    elif "completion" in obj:
                        text = obj.get("completion") or ""
                        if text:
                            yield text

                except Exception:
                    continue

    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")
        yield "\n[直连Claude流式接口失败]\n" + (err or str(e))
    except Exception as e:
        yield "\n[直连Claude流式接口失败]\n" + str(e)


def stream_direct_and_save(
    conversation_id: str,
    final_prompt: str,
    api_base_url: str,
    api_auth_token: str,
    api_model: str,
    provider_name: str = "",
):
    full = ""
    for chunk in stream_direct_api_text(
        final_prompt,
        api_base_url,
        api_auth_token,
        api_model,
    ):
        full += chunk
        yield chunk

    token_count = estimate_round_tokens(final_prompt, full)

    db_add_message(
        conversation_id,
        "assistant",
        full,
        model=api_model,
        provider_name=(provider_name or "") + "｜直连流式",
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

    effective_prompt = body.prompt
    if (body.route_mode or "cc") == "direct":
        effective_prompt = enhance_prompt_with_url_fetch(body.prompt)

    final_prompt = build_chat_prompt(history, effective_prompt)

    db_add_message(cid, "user", body.prompt)
    db_update_title_if_needed(cid, body.prompt)

    if (body.route_mode or "cc") == "direct":
        return StreamingResponse(
            stream_direct_and_save(
                cid,
                final_prompt,
                body.api_base_url,
                body.api_auth_token,
                body.api_model or DEFAULT_MODEL,
                body.api_profile_name or "",
            ),
            media_type="text/plain; charset=utf-8",
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

    if (body.route_mode or "cc") == "direct":
        return StreamingResponse(
            stream_direct_and_save(
                cid,
                final_prompt,
                body.api_base_url,
                body.api_auth_token,
                body.api_model or DEFAULT_MODEL,
                body.api_profile_name or "",
            ),
            media_type="text/plain; charset=utf-8",
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

    if (body.route_mode or "cc") == "direct":
        return StreamingResponse(
            stream_direct_and_save(
                cid,
                final_prompt,
                body.api_base_url,
                body.api_auth_token,
                body.api_model or DEFAULT_MODEL,
                body.api_profile_name or "",
            ),
            media_type="text/plain; charset=utf-8",
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
    route_mode: str = Form("cc"),
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

                route_label = "专用直连视觉" if (route_mode or "cc") == "direct" else "CC线路视觉"
                debug = (
                    f"【{route_label}已调用｜图片数: {len(local_image_paths)}"
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

    final_user_prompt = "\n".join(extra_parts).strip()
    if final_user_prompt:
        final_user_prompt += "\n\n用户问题：" + user_prompt
    else:
        final_user_prompt = user_prompt

    final_prompt = build_chat_prompt(
        messages=history,
        prompt=final_user_prompt,
    )

    # 专用直连线路：文本文件也走真流式 API
    if (route_mode or "cc") == "direct":
        return StreamingResponse(
            stream_direct_and_save(
                cid,
                final_prompt,
                api_base_url,
                api_auth_token,
                api_model or DEFAULT_MODEL,
                api_profile_name or "",
            ),
            media_type="text/plain; charset=utf-8",
        )

    # CC 本地代理线路：继续走 Claude Code
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


@app.post("/api/terminal/run")
def run_terminal_command(body: TerminalBody):
    import subprocess
    import os
    import time

    command = (body.command or "").strip()
    cwd = (body.cwd or "/root").strip()
    timeout = int(body.timeout or 60)

    if not command:
        return {
            "ok": False,
            "output": "命令为空"
        }

    if timeout < 1:
        timeout = 1
    if timeout > 300:
        timeout = 300

    if not os.path.exists(cwd):
        cwd = "/root"

    start = time.time()

    try:
        proc = subprocess.run(
            ["bash", "-lc", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )

        elapsed = round(time.time() - start, 3)

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        output = ""
        if stdout:
            output += stdout
        if stderr:
            if output:
                output += "\\n"
            output += stderr

        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "cwd": cwd,
            "elapsed": elapsed,
            "output": output or "(无输出)"
        }

    except subprocess.TimeoutExpired as e:
        out = ""
        if e.stdout:
            out += e.stdout
        if e.stderr:
            out += "\\n" + e.stderr

        return {
            "ok": False,
            "code": -1,
            "cwd": cwd,
            "elapsed": timeout,
            "output": "命令超时。\\n" + out
        }

    except Exception as e:
        return {
            "ok": False,
            "code": -2,
            "cwd": cwd,
            "elapsed": 0,
            "output": "执行失败：" + str(e)
        }


def execute_agent_shell(command: str, cwd: str = "/root", timeout: int = 120):
    import subprocess
    import os
    import time

    command = (command or "").strip()
    cwd = (cwd or "/root").strip()

    if not command:
        return {
            "ok": False,
            "code": -2,
            "cwd": cwd,
            "elapsed": 0,
            "output": "命令为空"
        }

    if not os.path.exists(cwd):
        cwd = "/root"

    timeout = max(1, min(int(timeout or 120), 300))

    start = time.time()

    try:
        proc = subprocess.run(
            ["bash", "-lc", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )

        elapsed = round(time.time() - start, 3)
        output = ""

        if proc.stdout:
            output += proc.stdout
        if proc.stderr:
            if output:
                output += "\\n"
            output += proc.stderr

        # 防止一次输出过大撑爆上下文
        if len(output) > 20000:
            output = output[:20000] + "\\n...[输出过长，已截断]"

        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "cwd": cwd,
            "elapsed": elapsed,
            "output": output or "(无输出)"
        }

    except subprocess.TimeoutExpired as e:
        output = ""
        if e.stdout:
            output += e.stdout
        if e.stderr:
            output += "\\n" + e.stderr

        return {
            "ok": False,
            "code": -1,
            "cwd": cwd,
            "elapsed": timeout,
            "output": "命令超时。\\n" + output
        }

    except Exception as e:
        return {
            "ok": False,
            "code": -3,
            "cwd": cwd,
            "elapsed": 0,
            "output": "执行失败：" + str(e)
        }


def extract_agent_json(text: str):
    import json
    import re

    raw = (text or "").strip()

    # 优先解析 ```json ... ```
    m = re.search(r"```json\\s*([\\s\\S]*?)```", raw)
    if m:
        raw = m.group(1).strip()
    else:
        # 否则找第一个 JSON 对象
        m = re.search(r"\\{[\\s\\S]*\\}", raw)
        if m:
            raw = m.group(0).strip()

    try:
        return json.loads(raw)
    except Exception:
        return {
            "action": "final",
            "answer": text
        }


def build_agent_prompt(task: str, cwd: str, transcript: list):
    parts = []

    parts.append("你现在是一个拥有本地 Ubuntu Linux 终端完整权限的 AI Agent。")
    parts.append("你运行在用户手机 Termux/proot Ubuntu 容器中。")
    parts.append("用户已经明确授权你使用本地终端执行命令、读写文件、联网、安装依赖、修改项目。")
    parts.append("你可以根据任务自主决定下一步要执行的 shell 命令。")
    parts.append("")
    parts.append("重要规则：")
    parts.append("1. 如果需要操作终端，必须只输出 JSON。")
    parts.append("2. 不要输出 Markdown，不要解释 JSON 外的内容。")
    parts.append("3. 每次最多执行一条命令。")
    parts.append("4. 如果任务已经完成，输出 final。")
    parts.append("")
    parts.append("JSON 格式只能是以下两种之一：")
    parts.append("")
    parts.append('{"action":"run","cwd":"/root","command":"ls -la","reason":"查看当前目录"}')
    parts.append("")
    parts.append('{"action":"final","answer":"任务完成，结果是..."}')
    parts.append("")
    parts.append(f"当前默认工作目录: {cwd}")
    parts.append("")
    parts.append("用户任务：")
    parts.append(task)
    parts.append("")

    if transcript:
        parts.append("此前执行记录：")
        for i, item in enumerate(transcript, 1):
            parts.append(f"步骤 {i}:")
            parts.append("命令:")
            parts.append(item.get("command", ""))
            parts.append("退出码:")
            parts.append(str(item.get("code", "")))
            parts.append("输出:")
            parts.append(item.get("output", ""))
            parts.append("")

    parts.append("请给出下一步 JSON。")

    return "\\n".join(parts)


@app.post("/api/agent/run")
def run_local_agent(body: AgentBody):
    task = (body.task or "").strip()

    if not task:
        return {
            "ok": False,
            "answer": "任务为空",
            "steps": []
        }

    cid = db_ensure_conversation(body.conversation_id)
    cwd = body.cwd or "/root"
    max_steps = max(1, min(int(body.max_steps or 8), 20))
    timeout = max(1, min(int(body.timeout or 120), 300))

    steps = []
    final_answer = ""

    # 保存用户任务
    db_add_message(
        cid,
        "user",
        task,
        file_name=None,
        image_preview=None,
    )

    for step_index in range(max_steps):
        prompt = build_agent_prompt(task, cwd, steps)

        model_reply = run_claude(
            prompt,
            api_base_url=body.api_base_url,
            api_auth_token=body.api_auth_token,
            api_model=body.api_model or DEFAULT_MODEL,
        )

        action = extract_agent_json(model_reply)

        if action.get("action") == "final":
            final_answer = action.get("answer") or model_reply
            break

        if action.get("action") != "run":
            final_answer = model_reply
            break

        command = (action.get("command") or "").strip()
        step_cwd = (action.get("cwd") or cwd or "/root").strip()
        reason = action.get("reason") or ""

        result = execute_agent_shell(
            command=command,
            cwd=step_cwd,
            timeout=timeout,
        )

        cwd = result.get("cwd") or step_cwd

        steps.append({
            "step": step_index + 1,
            "reason": reason,
            "cwd": cwd,
            "command": command,
            "ok": result.get("ok"),
            "code": result.get("code"),
            "elapsed": result.get("elapsed"),
            "output": result.get("output"),
        })

    if not final_answer:
        # 如果达到最大步骤还没 final，让模型总结一次
        summary_prompt = []
        summary_prompt.append("请根据以下终端执行记录，总结任务当前完成情况。")
        summary_prompt.append("如果已经完成，请说明结果；如果未完成，请说明卡在哪里。")
        summary_prompt.append("")
        summary_prompt.append("用户任务：")
        summary_prompt.append(task)
        summary_prompt.append("")
        summary_prompt.append("执行记录：")
        for item in steps:
            summary_prompt.append(f"步骤 {item['step']}: {item['command']}")
            summary_prompt.append(f"退出码: {item['code']}")
            summary_prompt.append("输出:")
            summary_prompt.append(item["output"])
            summary_prompt.append("")

        final_answer = run_claude(
            "\\n".join(summary_prompt),
            api_base_url=body.api_base_url,
            api_auth_token=body.api_auth_token,
            api_model=body.api_model or DEFAULT_MODEL,
        )

    # 保存 AI Agent 最终回答
    answer_for_db = "本地终端 Agent 执行完成。\\n\\n"

    for item in steps:
        answer_for_db += f"### 步骤 {item['step']}\\n"
        if item.get("reason"):
            answer_for_db += f"原因: {item['reason']}\\n"
        answer_for_db += f"cwd: {item['cwd']}\\n"
        answer_for_db += f"命令:\\n```bash\\n{item['command']}\\n```\\n"
        answer_for_db += f"退出码: {item['code']}\\n"
        answer_for_db += f"输出:\\n```text\\n{item['output']}\\n```\\n\\n"

    answer_for_db += "### 最终结果\\n"
    answer_for_db += final_answer

    db_add_message(
        cid,
        "assistant",
        answer_for_db,
        model=body.api_model or DEFAULT_MODEL,
        provider_name=body.api_profile_name or "",
    )

    return {
        "ok": True,
        "conversation_id": cid,
        "answer": final_answer,
        "steps": steps,
    }
