from pathlib import Path
from urllib.parse import quote
import os
import subprocess
import json
import uuid
import time

from fastapi import FastAPI, UploadFile, File, Form, Depends
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
from config import BASE_DIR, DEFAULT_MODEL, STATIC_DIR, UPLOAD_DIR
from db import (
    db_add_message,
    db_create_conversation,
    db_delete_api_profile,
    db_delete_last_assistant_message,
    db_delete_message_and_after_raw,
    db_ensure_conversation,
    db_get_message_by_id,
    db_get_messages,
    db_get_messages_before_id,
    db_get_regenerate_history,
    db_list_api_profiles,
    db_save_api_profile,
    db_set_default_api_profile,
    db_update_title_if_needed,
    get_conn,
    init_db,
    now_ms,
)
from schemas import (
    AgentBody,
    ApiProfileBody,
    ChatBody,
    ConversationCreateBody,
    ConversationPinBody,
    ConversationRenameBody,
    TerminalBody,
)
from services import (
    call_direct_vision_api,
    enhance_prompt_with_url_fetch,
    load_uploaded_text_from_path,
    make_env,
    run_claude,
    save_uploaded_file_dual_paths,
    stream_and_save,
    stream_direct_and_save,
    stream_direct_api_text,
)

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
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/lite")
def lite_index():
    return FileResponse(STATIC_DIR / "lite.html")


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
    if (body.route_mode or "direct") == "direct":
        effective_prompt = enhance_prompt_with_url_fetch(body.prompt)

    history_image_paths = collect_history_image_paths(history)

    db_add_message(cid, "user", body.prompt)
    db_update_title_if_needed(cid, body.prompt)

    if history_image_paths:
        vision_prompt = build_vision_text_history(history)
        if vision_prompt:
            vision_prompt += "\n\n"
        vision_prompt += "用户问题：" + (effective_prompt.strip() or "请继续结合历史图片回答。")

        _api_base_url = body.api_base_url
        _api_auth_token = body.api_auth_token
        _api_model = body.api_model or DEFAULT_MODEL
        _api_profile_name = body.api_profile_name or ""
        _cid = cid

        def gen_history_vision():
            try:
                answer = call_direct_vision_api(
                    vision_prompt,
                    history_image_paths,
                    _api_base_url,
                    _api_auth_token,
                    _api_model,
                )
                debug = (
                    f"【历史视觉上下文｜图片数: {len(history_image_paths)}"
                    f"｜模型: {_api_model}"
                    f"｜接入商: {_api_profile_name or _api_base_url}】\n\n"
                )
                final_answer = debug + answer
                token_count = estimate_round_tokens(vision_prompt, final_answer, image_count=len(history_image_paths))
                db_add_message(
                    _cid, "assistant", final_answer,
                    model=_api_model,
                    provider_name=_api_profile_name,
                    token_count=token_count,
                )
                yield final_answer
            except Exception as e:
                final_answer = (
                    "【历史视觉接口调用失败】\n\n"
                    + str(e)
                    + "\n\n将改用普通文字上下文继续回答。"
                )
                fallback_prompt = build_chat_prompt(history, effective_prompt)
                fallback_full = ""
                for chunk in stream_direct_api_text(
                    fallback_prompt,
                    _api_base_url,
                    _api_auth_token,
                    _api_model,
                ):
                    fallback_full += chunk
                    yield chunk
                if fallback_full:
                    db_add_message(
                        _cid, "assistant", fallback_full,
                        model=_api_model,
                        provider_name=(_api_profile_name or "") + "｜直连流式",
                        token_count=estimate_round_tokens(fallback_prompt, fallback_full),
                    )
                else:
                    db_add_message(_cid, "assistant", final_answer, model=_api_model, provider_name=_api_profile_name)
                    yield final_answer

        return StreamingResponse(gen_history_vision(), media_type="text/plain; charset=utf-8")

    final_prompt = build_chat_prompt(history, effective_prompt)

    if (body.route_mode or "direct") == "direct":
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
    last_user_msg = before[last_user_index]
    last_user_prompt = last_user_msg.content
    last_image_preview = last_user_msg.imagePreview

    # 删除目标消息以及之后所有消息
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
                try:
                    answer = call_direct_vision_api(
                        vision_prompt,
                        local_image_paths,
                        _api_base_url,
                        _api_auth_token,
                        _api_model,
                    )
                    debug = (
                        f"【重新回答｜视觉直连｜图片数: {len(local_image_paths)}"
                        f"｜模型: {_api_model}"
                        f"｜接入商: {_api_profile_name or _api_base_url}】\n\n"
                    )
                    final_answer = debug + answer
                    token_count = estimate_round_tokens(vision_prompt, final_answer, image_count=len(local_image_paths))
                    db_add_message(
                        _cid, "assistant", final_answer,
                        model=_api_model,
                        provider_name=_api_profile_name,
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
                    db_add_message(_cid, "assistant", final_answer, model=_api_model, provider_name=_api_profile_name)
                    yield final_answer

            return StreamingResponse(gen_vision(), media_type="text/plain; charset=utf-8")

    final_prompt = build_chat_prompt(
        messages=context_messages,
        prompt=last_user_prompt,
    )

    if (body.route_mode or "direct") == "direct":
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
    last_user_msg = history[last_user_index]
    last_user_prompt = last_user_msg.content

    history_images = collect_history_image_paths(context_messages)
    last_user_images = parse_image_preview_paths(getattr(last_user_msg, "imagePreview", None))
    all_history_images = []
    for path_item in history_images + last_user_images:
        if path_item not in all_history_images:
            all_history_images.append(path_item)

    if all_history_images:
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
            try:
                answer = call_direct_vision_api(
                    vision_prompt,
                    all_history_images,
                    _api_base_url,
                    _api_auth_token,
                    _api_model,
                )
                debug = (
                    f"【重新回答｜历史视觉上下文｜图片数: {len(all_history_images)}"
                    f"｜模型: {_api_model}"
                    f"｜接入商: {_api_profile_name or _api_base_url}】\n\n"
                )
                final_answer = debug + answer
                token_count = estimate_round_tokens(vision_prompt, final_answer, image_count=len(all_history_images))
                db_add_message(
                    _cid, "assistant", final_answer,
                    model=_api_model,
                    provider_name=_api_profile_name,
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
                db_add_message(_cid, "assistant", final_answer, model=_api_model, provider_name=_api_profile_name)
                yield final_answer

        return StreamingResponse(gen_vision(), media_type="text/plain; charset=utf-8")

    final_prompt = build_chat_prompt(
        messages=context_messages,
        prompt=last_user_prompt,
    )

    if (body.route_mode or "direct") == "direct":
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
    route_mode: str = Form("direct"),
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
            original, local_path, web_path = await save_uploaded_file_dual_paths(file)
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

    # 先保存用户消息，让网页里能看到图片，也让后续上下文能读取附件
    db_add_message(
        cid,
        "user",
        prompt,
        file_name=file_names_str,
        image_preview=image_preview_db,
        file_context=file_context_db,
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

                route_label = "专用直连视觉" if (route_mode or "direct") == "direct" else "CC线路视觉"
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
    if (route_mode or "direct") == "direct":
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
    from fastapi.responses import Response, PlainTextResponse

    try:
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

        # HTTP Header 只能安全放 latin-1/ASCII，中文文件名必须用 filename*
        ascii_name = "".join(
            ch if ch.isascii() and (ch.isalnum() or ch in "-_.") else "_"
            for ch in title
        ).strip("._")[:50] or conversation_id or "conversation"

        utf8_name = quote(f"{title}.md")

        return Response(
            content=md,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f"attachment; filename=\"{ascii_name}.md\"; filename*=UTF-8''{utf8_name}"
            }
        )

    except Exception as e:
        return PlainTextResponse(
            f"导出失败：{e}",
            status_code=500
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



# ===== CLAUDE WEB ADMIN BACKEND PATCH START =====
# 管理员后台：请求日志、日志详情、接入商线路质量检测

import urllib.request
import urllib.error
from fastapi import Request, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse

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
    """
    管理员后台认证。

    当前认证方式：固定密码
    密码：114514

    注意：
    为了兼容现有前端请求头，Header 名仍然叫 X-Admin-Token，
    但实际上传递的是管理员密码。
    """
    if str(x_admin_token or "").strip() != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="管理员密码无效")
    return True


def admin_mask_secret_text(text: str, max_len: int = 2000) -> str:
    if text is None:
        return ""

    import re

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


init_admin_tables()


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


@app.middleware("http")
async def admin_request_logger(request: Request, call_next):
    start_time = time.time()

    method = request.method
    path = request.url.path
    query_string = request.url.query or ""
    client_ip = request.client.host if request.client else ""
    user_agent = request.headers.get("user-agent", "")

    request_summary = ""
    route_mode = ""
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
                    route_mode = str(data.get("route_mode", "") or "")
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

# ===== CLAUDE WEB ADMIN BACKEND PATCH END =====




# ===== CC DEBUG PATCH START =====
import shutil
from fastapi import Query

CC_DEBUG_LOG = BASE_DIR / "logs" / "cc-debug.log"
CC_DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)

def _cc_redact_env_value(key: str, value: str) -> str:
    k = (key or "").upper()
    if any(x in k for x in ["KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH"]):
        return "[REDACTED]"
    return value

def _cc_env_snapshot(source_env=None):
    source_env = source_env or os.environ
    keys = [
        "HOME",
        "PATH",
        "TERM",
        "SHELL",
        "USER",
        "LOGNAME",
        "PWD",
        "LANG",
        "LC_ALL",
        "XDG_RUNTIME_DIR",
        "DISPLAY",
        "SSH_AUTH_SOCK",
        "CLAUDE_HOME",
        "ANTHROPIC_API_KEY",
    ]
    env = {}
    for k in keys:
        v = source_env.get(k, "")
        if v:
            env[k] = _cc_redact_env_value(k, v)

    # 额外记录所有和 Claude 相关的环境变量名，但做脱敏
    for k, v in source_env.items():
        ku = k.upper()
        if "CLAUDE" in ku or "ANTHROPIC" in ku:
            env[k] = _cc_redact_env_value(k, v)

    return env

def _write_cc_debug_log(title: str, payload: dict):
    try:
        CC_DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CC_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n=== {title} ===\n")
            f.write(json.dumps(payload, ensure_ascii=False, indent=2))
            f.write("\n")
    except Exception:
        pass

@app.get("/api/debug/cc-env")
def debug_cc_env():
    claude_path = shutil.which("claude")
    version = ""
    version_err = ""

    if claude_path:
        try:
            r = subprocess.run(
                [claude_path, "--version"],
                cwd=str(BASE_DIR),
                capture_output=True,
                text=True,
                timeout=15,
                env=os.environ.copy(),
            )
            version = (r.stdout or "").strip() or (r.stderr or "").strip()
            if not version:
                version = f"exit={r.returncode}"
        except Exception as e:
            version_err = str(e)

    data = {
        "ok": True,
        "cwd": str(Path.cwd()),
        "project_dir": str(BASE_DIR),
        "claude_path": claude_path,
        "claude_version": version,
        "claude_version_error": version_err,
        "process_env": _cc_env_snapshot(),
        "effective_claude_env": _cc_env_snapshot(make_env()),
    }

    _write_cc_debug_log("cc-env", data)
    return data

@app.post("/api/debug/cc-test")
def debug_cc_test(prompt: str = Query(default="只回复：OK")):
    claude_path = shutil.which("claude")
    if not claude_path:
        data = {
            "ok": False,
            "error": "claude not found in PATH",
            "cwd": str(Path.cwd()),
            "project_dir": str(BASE_DIR),
            "env": _cc_env_snapshot(),
        }
        _write_cc_debug_log("cc-test-missing", data)
        return data

    env = make_env()
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("PWD", str(BASE_DIR))

    cmd = [claude_path, "-p", prompt]
    start = time.time()

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

        data = {
            "ok": proc.returncode == 0,
            "cmd": cmd,
            "cwd": str(BASE_DIR),
            "returncode": proc.returncode,
            "elapsed_ms": int((time.time() - start) * 1000),
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
            "env": _cc_env_snapshot(),
        }
        _write_cc_debug_log("cc-test", data)
        return data

    except subprocess.TimeoutExpired as e:
        data = {
            "ok": False,
            "cmd": cmd,
            "cwd": str(BASE_DIR),
            "timeout": True,
            "elapsed_ms": int((time.time() - start) * 1000),
            "stdout": (e.stdout or "").strip() if isinstance(e.stdout, str) else "",
            "stderr": (e.stderr or "").strip() if isinstance(e.stderr, str) else "",
            "env": _cc_env_snapshot(),
            "error": "timeout",
        }
        _write_cc_debug_log("cc-test-timeout", data)
        return data

    except Exception as e:
        data = {
            "ok": False,
            "cmd": cmd,
            "cwd": str(BASE_DIR),
            "elapsed_ms": int((time.time() - start) * 1000),
            "error": str(e),
            "env": _cc_env_snapshot(),
        }
        _write_cc_debug_log("cc-test-exception", data)
        return data

# ===== CC DEBUG PATCH END =====
