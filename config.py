from pathlib import Path

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
