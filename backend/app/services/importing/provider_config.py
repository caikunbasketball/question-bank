"""抽取阶段:AI provider 配置解析。

从 DB 动态配置(system_settings + ai_providers/ai_models)解析当前激活的
文本 / 视觉 / 文档 OCR 模型,产出 provider 名与调用 config(API_KEY/BASE_URL/MODEL_NAME)。

三层模型方案与激活槽的对应关系:

    第一层  PaddleOCR-VL 主解析      -> AI_OCR_MODEL_ID
    第二层  视觉大模型兜底转写        -> AI_VISION_MODEL_ID
    第三层  文本模型结构化 / 分类归属  -> AI_TEXT_MODEL_ID

向量模型(AI_EMBEDDING_MODEL_ID)不在这条链上,由 embedding 服务自行解析。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.crud.crud_system_setting import system_setting
from app.models.ai_config import AIModel

# 激活槽键名(与 app.services.ai_config_service.ACTIVE_MODEL_KEYS 保持一致)
TEXT_MODEL_KEY = "AI_TEXT_MODEL_ID"
VISION_MODEL_KEY = "AI_VISION_MODEL_ID"
OCR_MODEL_KEY = "AI_OCR_MODEL_ID"


async def _resolve_by_key(db: AsyncSession, key: str) -> tuple[Optional[str], dict]:
    """按 system_settings 键解析激活模型,返回 (provider_name, config)。"""
    setting = await system_setting.get_by_key(db, key)
    if not setting or not setting.value:
        return None, {}

    try:
        model_id = int(setting.value)
    except ValueError:
        return None, {}

    stmt = select(AIModel).options(selectinload(AIModel.provider)).where(AIModel.id == model_id)
    result = await db.execute(stmt)
    model = result.scalar_one_or_none()
    if not model:
        return None, {}

    provider = model.provider
    if provider is None or not provider.is_active:
        return None, {}

    return provider.interface_type, {
        "API_KEY": provider.api_key,
        "BASE_URL": provider.base_url,
        "MODEL_NAME": model.name,
    }


async def resolve_active_provider(
    db: AsyncSession, *, is_vision: bool = False
) -> tuple[Optional[str], dict]:
    """返回 (provider_name, config);无激活配置时返回 (None, {})。

    保留 ``is_vision`` 布尔入参是为了兼容既有调用方与单测;新代码请直接用
    ``resolve_vision_provider`` / ``resolve_ocr_provider``。
    """
    return await _resolve_by_key(db, VISION_MODEL_KEY if is_vision else TEXT_MODEL_KEY)


async def resolve_vision_provider(db: AsyncSession) -> tuple[Optional[str], dict]:
    """第二层:视觉兜底转写模型。"""
    return await _resolve_by_key(db, VISION_MODEL_KEY)


async def resolve_ocr_provider(db: AsyncSession) -> tuple[Optional[str], dict]:
    """第一层:文档主 OCR 模型(PaddleOCR-VL)。"""
    return await _resolve_by_key(db, OCR_MODEL_KEY)


async def resolve_text_provider(db: AsyncSession) -> tuple[Optional[str], dict]:
    """第三层:题目结构化 / 分类归属的文本模型。"""
    return await _resolve_by_key(db, TEXT_MODEL_KEY)
