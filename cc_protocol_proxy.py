import json
import os
import time
import uuid
import urllib.error
import urllib.request
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse


router = APIRouter()


def _build_url(base_url: str, endpoint: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    ep = endpoint if endpoint.startswith("/") else "/" + endpoint
    if base.endswith("/v1") and ep.startswith("/v1/"):
        return base + ep[3:]
    return base + ep


def _proxy_config() -> dict[str, str]:
    return {
        "base_url": os.environ.get("CC_PROXY_OPENAI_BASE_URL", "https://api.codemax.store").strip(),
        "api_key": os.environ.get("CC_PROXY_OPENAI_API_KEY", "").strip(),
        "model": os.environ.get("CC_PROXY_OPENAI_MODEL", "gpt-5.5").strip(),
    }


def _content_blocks_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)

    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        typ = block.get("type")
        if typ == "text":
            parts.append(str(block.get("text", "")))
        elif typ == "tool_result":
            value = block.get("content", "")
            if isinstance(value, str):
                parts.append(value)
            else:
                parts.append(json.dumps(value, ensure_ascii=False))
        elif "text" in block:
            parts.append(str(block.get("text", "")))
    return "\n".join(p for p in parts if p)


def _anthropic_messages_to_openai(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    converted = []
    for msg in messages or []:
        role = msg.get("role", "user")
        if role not in {"user", "assistant", "system"}:
            role = "user"
        content = _content_blocks_to_text(msg.get("content", ""))
        converted.append({"role": role, "content": content})
    return converted


def _error_response(message: str, status_code: int = 500) -> JSONResponse:
    return JSONResponse(
        {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": message,
            },
        },
        status_code=status_code,
    )


def _openai_completion(body: dict[str, Any]) -> dict[str, Any]:
    cfg = _proxy_config()
    if not cfg["api_key"]:
        raise RuntimeError("CC_PROXY_OPENAI_API_KEY is not set")

    system_text = body.get("system", "")
    messages = _anthropic_messages_to_openai(body.get("messages", []))
    if system_text:
        if isinstance(system_text, list):
            system_text = _content_blocks_to_text(system_text)
        messages.insert(0, {"role": "system", "content": str(system_text)})

    payload = {
        "model": cfg["model"] or body.get("model") or "gpt-5.5",
        "messages": messages,
        "max_tokens": body.get("max_tokens", 4096),
        "temperature": body.get("temperature", 0.3),
        "stream": False,
    }

    url = _build_url(cfg["base_url"], "/v1/chat/completions")
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Authorization": "Bearer " + cfg["api_key"],
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=300) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
        return json.loads(raw)


def _anthropic_response_from_openai(openai_obj: dict[str, Any], model: str) -> dict[str, Any]:
    choice = (openai_obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = message.get("content") or ""
    usage = openai_obj.get("usage") or {}
    return {
        "id": "msg_" + uuid.uuid4().hex,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


def _stream_anthropic_events(result: dict[str, Any], model: str):
    msg_id = "msg_" + uuid.uuid4().hex
    usage = result.get("usage") or {}
    choice = (result.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = message.get("content") or ""

    def event(name: str, data: dict[str, Any]) -> str:
        return "event: " + name + "\n" + "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"

    yield event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": int(usage.get("prompt_tokens") or 0), "output_tokens": 0},
            },
        },
    )
    yield event("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    if text:
        yield event("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})
    yield event("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": int(usage.get("completion_tokens") or 0)},
        },
    )
    yield event("message_stop", {"type": "message_stop"})


@router.get("/v1/models")
def list_models():
    cfg = _proxy_config()
    model = cfg["model"] or "gpt-5.5"
    return {"data": [{"id": model, "type": "model", "display_name": model}]}


@router.post("/v1/messages")
async def messages(request: Request):
    try:
        body = await request.json()
        cfg = _proxy_config()
        result = _openai_completion(body)
        model = cfg["model"] or body.get("model") or "gpt-5.5"
        if body.get("stream"):
            return StreamingResponse(
                _stream_anthropic_events(result, model),
                media_type="text/event-stream; charset=utf-8",
            )
        return _anthropic_response_from_openai(result, model)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        return _error_response(f"Upstream HTTP {e.code}: {detail or str(e)}", e.code)
    except Exception as e:
        return _error_response(str(e), 500)


@router.get("/api/cc-proxy/health")
def health():
    cfg = _proxy_config()
    return {
        "ok": True,
        "upstream": cfg["base_url"],
        "model": cfg["model"],
        "has_key": bool(cfg["api_key"]),
        "time": int(time.time()),
    }


@router.head("/")
def root_head():
    return Response(status_code=200)
