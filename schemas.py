from typing import List, Optional

from pydantic import BaseModel, Field

from config import DEFAULT_MODEL


class MessageItem(BaseModel):
    id: Optional[int] = None
    role: str
    content: str
    fileName: Optional[str] = None
    imagePreview: Optional[str] = None
    fileContext: Optional[str] = None
    model: Optional[str] = None
    providerName: Optional[str] = None
    tokenCount: Optional[int] = None


class ChatBody(BaseModel):
    conversation_id: str = ""
    prompt: str = ""
    messages: List[MessageItem] = Field(default_factory=list)
    message_id: Optional[int] = None
    api_base_url: str = ""
    api_auth_token: str = ""
    api_model: str = DEFAULT_MODEL
    api_profile_name: str = ""
    route_mode: str = "direct"  # cc=Claude Code本地代理, direct=第三方API直连流式


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
