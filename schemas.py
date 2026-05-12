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
    supersededBy: Optional[int] = None
    sources: Optional[str] = None


class ChatBody(BaseModel):
    conversation_id: str = ""
    prompt: str = ""
    system_prompt: str = ""
    messages: List[MessageItem] = Field(default_factory=list)
    message_id: Optional[int] = None
    api_base_url: str = ""
    api_auth_token: str = ""
    api_model: str = DEFAULT_MODEL
    api_profile_name: str = ""
    web_search: bool = False
    web_search_explicit: bool = False
    keep_old: bool = False  # 重新生成时保留旧回复，作为版本历史


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


class SystemPromptBody(BaseModel):
    title: str = "系统提示词"
    content: str = ""
    enabled: bool = False
