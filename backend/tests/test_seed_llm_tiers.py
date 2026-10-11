"""三层模型一键配置脚本的回归测试(scripts/seed_llm_tiers.py)。

只验证"写库逻辑正确且幂等"——不真的联网,也不校验密钥有效性。
"""

import argparse

import pytest
from sqlalchemy import select

from app.models.ai_config import AIModel, AIProvider
from app.models.system_setting import SystemSetting
import scripts.seed_llm_tiers as seed_module
from scripts.seed_llm_tiers import (
    DEEPSEEK_TEXT_MODEL,
    PADDLE_OCR_MODEL,
    QWEN_VL_MODEL,
    ProviderSpec,
    build_specs,
    seed,
)


@pytest.fixture(autouse=True)
def _isolate_dotenv(monkeypatch):
    """测试里不读真实的 backend/.env,否则结果取决于本机凭证。

    单独用一个测试验证 .env 加载本身(见 test_load_dotenv_feeds_credentials)。
    """
    monkeypatch.setattr(seed_module, "_load_dotenv", lambda: None)


def _args(**kwargs) -> argparse.Namespace:
    base = {
        "ocr_token": None,
        "vision_key": None,
        "text_key": None,
        "list": False,
        "force_reset": False,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


def _three_tier_specs() -> list[ProviderSpec]:
    return [
        ProviderSpec(
            tier="第一层", name="百度飞桨 AI Studio", interface_type="paddleocr",
            base_url="https://paddleocr.aistudio-app.com/api/v2/ocr/jobs", api_key="tok",
            models=[(PADDLE_OCR_MODEL, {})], setting_key="AI_OCR_MODEL_ID",
        ),
        ProviderSpec(
            tier="第二层", name="硅基流动 (SiliconFlow)", interface_type="openai",
            base_url="https://api.siliconflow.cn/v1", api_key="sk-v",
            models=[(QWEN_VL_MODEL, {"is_vision_capable": True})], setting_key="AI_VISION_MODEL_ID",
        ),
        ProviderSpec(
            tier="第三层", name="DeepSeek 官方", interface_type="openai",
            base_url="https://api.deepseek.com/v1", api_key="sk-t",
            models=[(DEEPSEEK_TEXT_MODEL, {})], setting_key="AI_TEXT_MODEL_ID",
        ),
    ]


def test_build_specs_reports_missing_credentials(monkeypatch):
    for var in ("PADDLEOCR_AISTUDIO_TOKEN", "SILICONFLOW_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    specs, missing = build_specs(_args())
    assert specs == []
    assert len(missing) == 3


def test_build_specs_maps_layers_to_activation_slots(monkeypatch):
    monkeypatch.setenv("PADDLEOCR_AISTUDIO_TOKEN", "tok")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-v")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-t")

    specs, missing = build_specs(_args())
    assert missing == []

    by_tier = {s.tier: s for s in specs}
    # 第一层必须是 paddleocr 协议 —— 它不能用 OpenAI 协议承载。
    assert by_tier["第一层"].interface_type == "paddleocr"
    assert by_tier["第一层"].setting_key == "AI_OCR_MODEL_ID"
    assert by_tier["第二层"].setting_key == "AI_VISION_MODEL_ID"
    assert by_tier["第二层"].models[0][1]["is_vision_capable"] is True
    assert by_tier["第三层"].setting_key == "AI_TEXT_MODEL_ID"


def test_cli_args_override_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")

    specs, _ = build_specs(_args(text_key="from-cli"))
    assert [s for s in specs if s.tier == "第三层"][0].api_key == "from-cli"


def test_parse_env_text_handles_comments_quotes_and_blanks():
    text = """
# 三层模型凭证
PADDLEOCR_AISTUDIO_TOKEN=tok-1

SILICONFLOW_API_KEY="sk-quoted"

DEEPSEEK_API_KEY = 'sk-single'
   # 另一段注释
"""
    parsed = seed_module.parse_env_text(text)
    assert parsed == {
        "PADDLEOCR_AISTUDIO_TOKEN": "tok-1",
        "SILICONFLOW_API_KEY": "sk-quoted",
        "DEEPSEEK_API_KEY": "sk-single",
    }


def test_dotenv_values_feed_specs(monkeypatch):
    """走一遍"解析 .env → 组装 specs"的完整链路(不打桩 _,直接注入环境)。"""
    for var in ("PADDLEOCR_AISTUDIO_TOKEN", "SILICONFLOW_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    parsed = seed_module.parse_env_text(
        "PADDLEOCR_AISTUDIO_TOKEN=tok\nSILICONFLOW_API_KEY=sk-v\nDEEPSEEK_API_KEY=sk-t\n"
    )
    for key, value in parsed.items():
        monkeypatch.setenv(key, value)

    monkeypatch.setattr(seed_module, "_load_dotenv", lambda: None)
    specs, missing = build_specs(_args())

    assert missing == []
    assert {s.tier for s in specs} == {"第一层", "第二层", "第三层"}


async def test_seed_writes_providers_models_and_active_slots(db_session):
    await seed(db_session, specs=_three_tier_specs(), list_only=False)

    providers = (await db_session.execute(select(AIProvider))).scalars().all()
    assert {p.interface_type for p in providers} == {"paddleocr", "openai"}

    models = (await db_session.execute(select(AIModel))).scalars().all()
    assert len(models) == 3

    settings = {
        s.key: s.value
        for s in (await db_session.execute(select(SystemSetting))).scalars().all()
    }
    ocr_model = (await db_session.execute(
        select(AIModel).where(AIModel.name == PADDLE_OCR_MODEL)
    )).scalars().one()
    vision_model = (await db_session.execute(
        select(AIModel).where(AIModel.name == QWEN_VL_MODEL)
    )).scalars().one()
    text_model = (await db_session.execute(
        select(AIModel).where(AIModel.name == DEEPSEEK_TEXT_MODEL)
    )).scalars().one()

    assert settings["AI_OCR_MODEL_ID"] == str(ocr_model.id)
    assert settings["AI_VISION_MODEL_ID"] == str(vision_model.id)
    assert settings["AI_TEXT_MODEL_ID"] == str(text_model.id)


async def test_seed_is_idempotent(db_session):
    """重复执行不应产生重复供应商/模型。"""
    await seed(db_session, specs=_three_tier_specs(), list_only=False)
    await seed(db_session, specs=_three_tier_specs(), list_only=False)

    providers = (await db_session.execute(select(AIProvider))).scalars().all()
    models = (await db_session.execute(select(AIModel))).scalars().all()
    assert len(providers) == 3
    assert len(models) == 3


async def test_seed_updates_rotated_key(db_session):
    """密钥轮换后重跑脚本应就地更新,而不是新建供应商。"""
    specs = _three_tier_specs()
    await seed(db_session, specs=specs, list_only=False)

    specs[2].api_key = "sk-rotated"
    await seed(db_session, specs=specs, list_only=False)

    provider = (await db_session.execute(
        select(AIProvider).where(AIProvider.name == "DeepSeek 官方")
    )).scalars().one()
    assert provider.api_key == "sk-rotated"
    assert len((await db_session.execute(select(AIProvider))).scalars().all()) == 3


async def test_ocr_slot_rejects_non_paddleocr_model(db_session):
    """把 OpenAI 兼容模型选成 OCR 主解析必须被拦住。"""
    from app.capabilities.errors import Invalid
    from app.services.ai_config_service import validate_active_model

    provider = AIProvider(name="zz-openai-provider", interface_type="openai", api_key="k")
    model = AIModel(provider=provider, name="some-chat-model")
    db_session.add_all([provider, model])
    await db_session.commit()

    with pytest.raises(Invalid, match="paddleocr"):
        await validate_active_model(db_session, key="AI_OCR_MODEL_ID", value=str(model.id))


async def test_ocr_slot_accepts_paddleocr_model(db_session):
    from app.services.ai_config_service import validate_active_model

    provider = AIProvider(
        name="zz-paddle-provider", interface_type="paddleocr", api_key="k"
    )
    model = AIModel(provider=provider, name=PADDLE_OCR_MODEL)
    db_session.add_all([provider, model])
    await db_session.commit()

    # 不抛异常即为通过。
    await validate_active_model(db_session, key="AI_OCR_MODEL_ID", value=str(model.id))
