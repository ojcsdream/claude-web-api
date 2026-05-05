from pathlib import Path
import json
import os
import subprocess
import uuid

from fastapi import UploadFile

from chat_utils import estimate_round_tokens
from config import BASE_DIR, DEFAULT_MODEL, UPLOAD_DIR
from db import db_add_message

def load_claude_settings_env() -> dict:
    env = {}
    settings_paths = [
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
        BASE_DIR / ".claude" / "settings.json",
        BASE_DIR / ".claude" / "settings.local.json",
    ]

    for path in settings_paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        values = data.get("env", {})
        if not isinstance(values, dict):
            continue

        for key, value in values.items():
            if isinstance(key, str) and isinstance(value, str):
                env[key] = value

    return env


def make_env(api_base_url: str = "", api_auth_token: str = ""):
    env = os.environ.copy()
    env.update(load_claude_settings_env())
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


def build_api_url(base_url: str, endpoint: str) -> str:
    """
    兼容两种 base_url 写法：
    1. https://api.xxx.com
    2. https://api.xxx.com/v1

    endpoint 示例：
    /v1/chat/completions
    /v1/messages
    """
    base = (base_url or "").strip().rstrip("/")
    ep = endpoint if endpoint.startswith("/") else "/" + endpoint

    if base.endswith("/v1") and ep.startswith("/v1/"):
        return base + ep[3:]

    return base + ep


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
        url = build_api_url(base_url, "/v1/chat/completions")
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
            "Accept": "text/event-stream, application/json, text/plain, */*",
            "Authorization": "Bearer " + token,
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
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
            yield (
                "\n[直连OpenAI流式接口失败]\n"
                f"HTTP {getattr(e, 'code', '')} {getattr(e, 'reason', '')}\n"
                f"请求地址: {url}\n"
                + (err or str(e))
            )
        except Exception as e:
            yield (
                "\n[直连OpenAI流式接口失败]\n"
                f"请求地址: {url}\n"
                + str(e)
            )

        return

    # Anthropic-compatible / Claude-compatible
    url = build_api_url(base_url, "/v1/messages")
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
        "Accept": "text/event-stream, application/json, text/plain, */*",
        "x-api-key": token,
        "Authorization": "Bearer " + token,
        "anthropic-version": "2023-06-01",
        "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
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
        yield (
            "\n[直连Claude流式接口失败]\n"
            f"HTTP {getattr(e, 'code', '')} {getattr(e, 'reason', '')}\n"
            f"请求地址: {url}\n"
            + (err or str(e))
        )
    except Exception as e:
        yield (
            "\n[直连Claude流式接口失败]\n"
            f"请求地址: {url}\n"
            + str(e)
        )


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


def load_uploaded_text_from_path(local_path: str) -> str:
    abs_path = local_upload_path_to_abs(local_path)
    try:
        raw = abs_path.read_bytes()
    except Exception:
        return ""

    if len(raw) > 1024 * 1024:
        raw = raw[:1024 * 1024]

    return raw.decode("utf-8", errors="ignore").strip()


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
