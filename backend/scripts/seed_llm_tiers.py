"""按三层方案一键写入 AI 供应商 / 模型 / 激活槽。

三层模型方案:

    第一层  PaddleOCR-VL-1.6            百度飞桨 AI Studio   主 OCR 文档解析
    第二层  Qwen3-VL-32B-Instruct       硅基流动             识别失败/不完整时补充
    第三层  DeepSeek V4.1 Flash         深度求索官方         题目结构化、语义理解、分类归属

    ┌──────────────────────────────────────────────────────────┐
    │ 图片/PDF → ① PaddleOCR-VL → ② Qwen3-VL(兜底) →          │
    │            markdown → ③ DeepSeek(结构化/分类) → 题目      │
    └──────────────────────────────────────────────────────────┘

凭证来源(按优先级):
    1. 命令行参数  --ocr-token / --vision-key / --text-key
    2. 环境变量    PADDLEOCR_AISTUDIO_TOKEN / SILICONFLOW_API_KEY / DEEPSEEK_API_KEY
                   (backend/.env 会被自动加载;该文件已在 .gitignore 中)
    3. 缺失则跳过该供应商(不会写半份配置)

用法:
    cd backend
    uv run python scripts/seed_llm_tiers.py
    uv run python scripts/seed_llm_tiers.py --list        # 只查看当前配置,不写入
    uv run python scripts/seed_llm_tiers.py --force-reset # 先清空 AI 配置再写入

幂等:以「供应商名称」和「(供应商, 模型名)」为键做 upsert,可重复执行。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 允许 `python scripts/seed_llm_tiers.py` 直接跑。
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.db.session import SessionLocal, engine  # noqa: E402
from app.models.ai_config import AIModel, AIProvider  # noqa: E402
from app.models.system_setting import SystemSetting  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("seed_llm_tiers")

# --------------------------------------------------------------------------- #
# 常量:模型标识符均已核对官方文档(2026-10)
# --------------------------------------------------------------------------- #

PADDLE_OCR_MODEL = "PaddleOCR-VL-1.6"
PADDLE_OCR_BASE_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"

QWEN_VL_MODEL = "Qwen/Qwen3-VL-32B-Instruct"
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"

# DeepSeek-V4.1-Flash 的官方 API 模型名是 deepseek-flash
# (旧名 deepseek-v4-flash / deepseek-chat / deepseek-reasoner 已下线或即将下线)。
DEEPSEEK_TEXT_MODEL = "deepseek-flash"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


@dataclass
class ProviderSpec:
    tier: str
    name: str
    interface_type: str
    base_url: str
    api_key: str
    models: list[tuple[str, dict]] = field(default_factory=list)
    setting_key: str = ""


def parse_env_text(text: str) -> dict[str, str]:
    """解析 .env 文本为键值对(纯函数,便于单测)。

    只支持最简单的 ``KEY=VALUE``;忽略空行与 ``#`` 注释;值两侧的引号会被剥掉。
    """
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


def _load_dotenv() -> None:
    """把 backend/.env 读进 os.environ。

    不引第三方依赖:pydantic-settings 的 env_file 只在 Settings 构造时生效,
    这里需要拿普通环境变量,自己解析一份即可。已存在的环境变量优先,不覆盖。
    """
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for key, value in parse_env_text(env_path.read_text(encoding="utf-8")).items():
        os.environ.setdefault(key, value)


def build_specs(args: argparse.Namespace) -> tuple[list[ProviderSpec], list[str]]:
    """组装三家供应商配置,返回 (specs, 缺失说明)。"""
    _load_dotenv()
    missing: list[str] = []

    ocr_token = args.ocr_token or os.getenv("PADDLEOCR_AISTUDIO_TOKEN", "")
    vision_key = args.vision_key or os.getenv("SILICONFLOW_API_KEY", "")
    text_key = args.text_key or os.getenv("DEEPSEEK_API_KEY", "")

    specs: list[ProviderSpec] = []

    if ocr_token:
        specs.append(ProviderSpec(
            tier="第一层", name="百度飞桨 AI Studio", interface_type="paddleocr",
            base_url=PADDLE_OCR_BASE_URL, api_key=ocr_token,
            models=[(PADDLE_OCR_MODEL, {})],
            setting_key="AI_OCR_MODEL_ID",
        ))
    else:
        missing.append("第一层 PaddleOCR-VL (PADDLEOCR_AISTUDIO_TOKEN / --ocr-token)")

    if vision_key:
        specs.append(ProviderSpec(
            tier="第二层", name="硅基流动 (SiliconFlow)", interface_type="openai",
            base_url=SILICONFLOW_BASE_URL, api_key=vision_key,
            models=[(QWEN_VL_MODEL, {"is_vision_capable": True})],
            setting_key="AI_VISION_MODEL_ID",
        ))
    else:
        missing.append("第二层 Qwen3-VL (SILICONFLOW_API_KEY / --vision-key)")

    if text_key:
        specs.append(ProviderSpec(
            tier="第三层", name="DeepSeek 官方", interface_type="openai",
            base_url=DEEPSEEK_BASE_URL, api_key=text_key,
            models=[(DEEPSEEK_TEXT_MODEL, {})],
            setting_key="AI_TEXT_MODEL_ID",
        ))
    else:
        missing.append("第三层 DeepSeek (DEEPSEEK_API_KEY / --text-key)")

    return specs, missing


async def _get_or_create_provider(db: AsyncSession, spec: ProviderSpec) -> tuple[AIProvider, bool]:
    provider = (
        await db.execute(select(AIProvider).where(AIProvider.name == spec.name))
    ).scalars().first()

    if provider is None:
        provider = AIProvider(
            name=spec.name,
            interface_type=spec.interface_type,
            base_url=spec.base_url,
            api_key=spec.api_key,
            is_active=True,
        )
        db.add(provider)
        await db.flush()
        return provider, True

    # 已存在:更新协议 / 地址 / 密钥(密钥可能轮换),并确保启用。
    provider.interface_type = spec.interface_type
    provider.base_url = spec.base_url
    provider.api_key = spec.api_key
    provider.is_active = True
    await db.flush()
    return provider, False


async def _get_or_create_model(db: AsyncSession, provider: AIProvider, name: str, flags: dict) -> tuple[AIModel, bool]:
    model = (
        await db.execute(
            select(AIModel).where(AIModel.provider_id == provider.id, AIModel.name == name)
        )
    ).scalars().first()

    if model is None:
        model = AIModel(
            provider_id=provider.id,
            name=name,
            is_vision_capable=bool(flags.get("is_vision_capable", False)),
            is_embedding_model=bool(flags.get("is_embedding_model", False)),
        )
        db.add(model)
        await db.flush()
        return model, True

    model.is_vision_capable = bool(flags.get("is_vision_capable", model.is_vision_capable))
    model.is_embedding_model = bool(flags.get("is_embedding_model", model.is_embedding_model))
    await db.flush()
    return model, False


async def _set_setting(db: AsyncSession, key: str, value: str, description: str) -> None:
    setting = (await db.execute(select(SystemSetting).where(SystemSetting.key == key))).scalars().first()
    if setting is None:
        db.add(SystemSetting(key=key, value=value, description=description))
    else:
        setting.value = value
    await db.flush()


async def seed(db: AsyncSession, *, specs: list[ProviderSpec], list_only: bool) -> None:
    if list_only:
        await _print_current(db)
        return

    rows: list[tuple[str, str, str, str, str]] = []
    for spec in specs:
        provider, p_created = await _get_or_create_provider(db, spec)
        for model_name, flags in spec.models:
            model, m_created = await _get_or_create_model(db, provider, model_name, flags)
            if spec.setting_key:
                await _set_setting(
                    db,
                    spec.setting_key,
                    str(model.id),
                    f"Active AI Model ID ({spec.setting_key})",
                )
            rows.append((
                spec.tier,
                f"{spec.name} / {model_name}",
                spec.interface_type,
                "新建" if (p_created or m_created) else "幂等更新",
                f"id={model.id}",
            ))

    await db.commit()
    _print_table(rows)


async def _print_current(db: AsyncSession) -> None:
    providers = (await db.execute(select(AIProvider).order_by(AIProvider.id))).scalars().all()
    models = (await db.execute(select(AIModel).order_by(AIModel.id))).scalars().all()

    print("\n当前 AI 供应商:")
    for p in providers:
        print(f"  #{p.id:<3} {p.name:<28} {p.interface_type:<10} active={p.is_active} {p.base_url or ''}")
    print("\n当前模型:")
    for m in models:
        tags = []
        if m.is_vision_capable:
            tags.append("识图")
        if m.is_embedding_model:
            tags.append("向量")
        print(f"  #{m.id:<3} provider={m.provider_id:<3} {m.name:<32} {'/'.join(tags)}")
    print("\n激活槽:")
    settings = (await db.execute(
        select(SystemSetting).where(SystemSetting.key.like("AI_%_MODEL_ID"))
    )).scalars().all()
    for s in settings:
        print(f"  {s.key:<24} = {s.value or '(空)'}")


def _print_table(rows: list[tuple[str, str, str, str, str]]) -> None:
    if not rows:
        print("没有可写入的供应商(凭证全部缺失)。")
        return
    headers = ("层级", "供应商 / 模型", "协议", "结果", "ID")
    widths = [max(len(str(r[i])) for r in rows + [headers]) for i in range(len(headers))]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print("\n" + line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)))
    print("\n✅ 三层模型配置写入完成(可重复执行)。")


async def _reset(db: AsyncSession) -> None:
    """清空既有 AI 配置,用于从杂乱状态重新开始。"""
    await db.execute(AIModel.__table__.delete())
    await db.execute(AIProvider.__table__.delete())
    await db.execute(
        SystemSetting.__table__.delete().where(SystemSetting.key.like("AI_%_MODEL_ID"))
    )
    await db.commit()
    print("已清空 AI 供应商 / 模型 / 激活槽。")


async def main() -> int:
    parser = argparse.ArgumentParser(description="按三层方案写入 AI 供应商与激活槽")
    parser.add_argument("--ocr-token", help="百度飞桨 AI Studio Access Token(第一层)")
    parser.add_argument("--vision-key", help="硅基流动 API Key(第二层)")
    parser.add_argument("--text-key", help="DeepSeek API Key(第三层)")
    parser.add_argument("--list", action="store_true", help="只列出当前配置,不写入")
    parser.add_argument("--force-reset", action="store_true", help="写入前清空既有 AI 配置")
    args = parser.parse_args()

    from app.core.config import get_db_url

    if not get_db_url():
        print("❌ 数据库尚未配置。请先启动一次后端完成初始化(或设置 DB_URL),再运行本脚本。")
        return 2

    specs, missing = build_specs(args)
    if missing and not args.list:
        print("⚠️  以下层级的凭证缺失,本次不会写入:")
        for m in missing:
            print(f"    - {m}")
        print("    (可写进 backend/.env 或用命令行参数传入)\n")

    async with SessionLocal() as db:
        try:
            if args.force_reset and not args.list:
                await _reset(db)
            await seed(db, specs=specs, list_only=args.list)
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            logger.error("写入失败: %s", exc)
            import traceback

            traceback.print_exc()
            return 1
    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
