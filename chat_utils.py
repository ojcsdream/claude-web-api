import json
from pathlib import Path
from typing import List, Optional

from config import (
    IMAGE_EXTS,
    MAX_CONTEXT_CHARS,
    MAX_CONTEXT_MESSAGES,
    VISION_CONTEXT_CHARS,
    VISION_CONTEXT_MESSAGES,
)
from schemas import MessageItem


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
        if "\u4e00" <= ch <= "\u9fff":
            chinese += 1
        else:
            other += 1

    return max(1, chinese + other // 4)


def estimate_round_tokens(input_text: str, output_text: str, image_count: int = 0) -> int:
    # 图片 token 很难精确，不同模型差异很大，这里给每张图一个保守估算值
    return estimate_tokens(input_text) + estimate_tokens(output_text) + image_count * 1000


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
                fileContext=getattr(msg, "fileContext", None),
            )
        )
        total += len(content)

    return list(reversed(result))


def parse_image_preview_paths(image_preview: Optional[str]) -> list[str]:
    if not image_preview:
        return []

    try:
        paths = json.loads(image_preview)
    except Exception:
        paths = [image_preview]

    if not isinstance(paths, list):
        paths = [paths]

    local_paths = []
    for path in paths:
        if not isinstance(path, str) or not path.strip():
            continue
        p = path.strip()
        if p.startswith("./uploads/"):
            local_paths.append(p)
        elif p.startswith("/uploads/"):
            local_paths.append("." + p)

    return local_paths


def collect_history_image_paths(messages: List[MessageItem]) -> list[str]:
    paths = []
    for msg in messages[-VISION_CONTEXT_MESSAGES:]:
        if msg.role != "user":
            continue
        for path in parse_image_preview_paths(getattr(msg, "imagePreview", None)):
            if path not in paths:
                paths.append(path)
    return paths


def format_message_for_context(msg: MessageItem) -> str:
    text = (msg.content or "").strip()
    pieces = [text] if text else []

    file_name = getattr(msg, "fileName", None)
    file_context = getattr(msg, "fileContext", None)
    image_paths = parse_image_preview_paths(getattr(msg, "imagePreview", None))

    if file_context:
        pieces.append(f"[历史上传文件: {file_name or '未命名文件'}]\n{file_context}")

    if image_paths:
        pieces.append(
            "[历史上传图片]\n"
            + (f"文件名: {file_name}\n" if file_name else "")
            + "本地图片路径: "
            + ", ".join(image_paths)
        )

    return "\n\n".join(pieces).strip()


def build_vision_text_history(messages: List[MessageItem]) -> str:
    recent = messages[-VISION_CONTEXT_MESSAGES:]
    parts = []
    total = 0

    for msg in recent:
        text = format_message_for_context(msg)
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

    return "\n".join(parts).strip()


def build_chat_prompt(
    messages: List[MessageItem],
    prompt: str,
    file_name: Optional[str] = None,
    file_text: Optional[str] = None,
    image_rel_path: Optional[str] = None,
) -> str:
    messages = trim_context_messages(messages)

    parts = [
        "你正在同一个连续对话中回答用户。必须结合下面按时间顺序给出的最近聊天上下文，而不是只看最后一句。",
        "如果用户当前问题包含“这个、那个、他、她、它、他们、上述、刚才、前面、你说的”等指代，先从聊天上下文中确定真实主体，再回答。",
        "如果当前问题是在追问、比较、补充、让你继续、让你联网查某个上下文里的对象，你必须沿用上下文主题。",
        "如果上下文不足以确定指代对象，请先简短说明不确定点，再给出你能确定的回答。",
        "请直接回答，不要重复角色标签。",
        r"如果涉及数学公式，请使用标准 LaTeX。独立公式必须用 $$...$$ 包裹，行内公式用 $...$。分式必须使用 \frac{分子}{分母}，不要使用 a/b 这种斜杠分式；平方根必须使用 \sqrt{}；推导公式尽量使用 align 环境。",
        "",
        "最近聊天上下文：",
    ]

    for msg in messages:
        role = "用户" if msg.role == "user" else "助手"
        content = format_message_for_context(msg)
        if content:
            parts.append(f"{role}: {content}")
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
        parts.append("当前用户问题：")
        parts.append(prompt.strip())
        parts.append("")
        parts.append("助手:")

    return "\n".join(parts).strip()
