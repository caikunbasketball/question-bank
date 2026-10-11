from typing import Literal, Optional, List

from pydantic import BaseModel, field_validator

# 支持的供应商协议。``paddleocr`` 是文档解析专用通道(百度飞桨 AI Studio),
# 不能用作文本/视觉对话模型——槽位校验在 services.ai_config_service 里兜。
InterfaceType = Literal["openai", "gemini", "paddleocr"]

class AIModelBase(BaseModel):
    name: str
    is_vision_capable: bool = False
    is_embedding_model: bool = False

    @field_validator('is_vision_capable', 'is_embedding_model', mode='before')
    @classmethod
    def set_default_false(cls, v):
        return v if v is not None else False

class AIModelCreate(AIModelBase):
    pass

class AIModelUpdate(BaseModel):
    name: Optional[str] = None
    is_vision_capable: Optional[bool] = None
    is_embedding_model: Optional[bool] = None

class AIModel(AIModelBase):
    id: int
    provider_id: int

    class Config:
        from_attributes = True

class AIProviderBase(BaseModel):
    name: str
    interface_type: InterfaceType
    base_url: Optional[str] = None
    is_active: bool = True

class AIProviderCreate(AIProviderBase):
    api_key: str
    models: List[AIModelCreate] = []

class AIProviderUpdate(BaseModel):
    name: Optional[str] = None
    interface_type: Optional[InterfaceType] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    is_active: Optional[bool] = None

class AIProvider(AIProviderBase):
    id: int
    models: List[AIModel] = []

    class Config:
        from_attributes = True
