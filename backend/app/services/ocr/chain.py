"""三层模型方案里的"解析阶段"编排:第一层主解析 → 第二层兜底补充。

    ┌────────────────────────────────────────────────────────────┐
    │ 文档(图片 / PDF)                                            │
    │        │                                                   │
    │        ▼                                                   │
    │  ① PaddleOCR-VL 主解析  ──► 产出质量校验                     │
    │        │ 失败 / 部分页为空 / 页数对不上                       │
    │        ▼                                                   │
    │  ② 视觉大模型(如 Qwen3-VL)转写 ──► 页级合并 / 整卷兜底        │
    │        │                                                   │
    │        ▼                                                   │
    │  markdown + 图片资源  ──► 交给第三层(文本模型)做结构化抽题     │
    └────────────────────────────────────────────────────────────┘

关键取舍:
- **页级合并**,不是"整卷重跑"。第一层只吃下部分页面时,只对缺失页调第二层,
  既省钱又不会把已经准确的页面覆盖成较差的版本。
- **页数对不上就不合并**。无法对齐页码时硬合并会串页,宁可整卷交给第二层。
- 两层都空才算失败,抛 ``OcrError``;单层异常只降级不中断。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.importing.provider_config import resolve_ocr_provider, resolve_vision_provider

from .contracts import (
    OcrError,
    OcrResult,
    SOURCE_NONE,
    SOURCE_PADDLEOCR,
    SOURCE_VISION_FALLBACK,
    strip_markdown_noise,
)
from .paddleocr import PaddleOcrClient
from .vision_fallback import transcribe_pages

logger = logging.getLogger(__name__)

# 单页被判定为"没识别出来"的字符阈值。低于它才值得花第二层的钱。
DEFAULT_MIN_CHARS_PER_PAGE = 20
# 整卷可用性下限:低于这个字符数直接认为第一层没干活。
DEFAULT_MIN_TOTAL_CHARS = 30


@dataclass
class OcrChainResult:
    """解析阶段的最终产物。"""

    markdown: str = ""
    source: str = SOURCE_NONE
    assets: dict[str, bytes] = field(default_factory=dict)
    page_count: int = 0
    used_fallback: bool = False
    primary_error: Optional[str] = None
    # 哪些页用了第二层(1-based),便于人工复核时重点看这几页。
    fallback_pages: list[int] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.markdown.strip())


class DocumentOcrChain:
    """把"主 OCR → 视觉兜底"编成一条可复用的链。配置每次调用现取(支持热切换)。"""

    def __init__(
        self,
        *,
        min_chars_per_page: int = DEFAULT_MIN_CHARS_PER_PAGE,
        min_total_chars: int = DEFAULT_MIN_TOTAL_CHARS,
        paddle_client_factory: Optional[Callable[[dict], Any]] = None,
        vision_transcriber: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.min_chars_per_page = min_chars_per_page
        self.min_total_chars = min_total_chars
        self._paddle_client_factory = paddle_client_factory or _default_paddle_client
        self._transcribe = vision_transcriber or transcribe_pages

    async def parse(
        self,
        *,
        document: bytes,
        filename: Optional[str],
        db: AsyncSession,
        page_images: Optional[list[bytes]] = None,
    ) -> OcrChainResult:
        """``document`` 是原始文件(整份 PDF 或单张图片);``page_images`` 是栅格化后的逐页图,供第二层使用。"""
        primary = await self._run_primary(document, filename, db)
        if primary is None:
            primary_error: Optional[str] = "主 OCR 未配置或未启用"
        elif not primary.usable:
            # 保留第一层自己的失败原因(HTTP 401 / 超时 / 任务失败…),这比"产出为空"有用得多。
            primary_error = primary.primary_error or "主 OCR 产出为空"
        else:
            primary_error = None

        # 整卷都合格,直接收工(省钱的主路径)。
        if primary is not None and self._whole_doc_ok(primary, page_images):
            return OcrChainResult(
                markdown=strip_markdown_noise(primary.markdown),
                source=SOURCE_PADDLEOCR,
                assets=primary.assets,
                page_count=primary.page_count,
            )

        # ---- 页级合并:第一层吃下了部分页面时,只补缺的页 ---------------- #
        if (
            primary is not None
            and primary.usable
            and page_images
            and primary.page_count == len(page_images)
        ):
            merged, weak_pages, assets = await self._merge_with_fallback(
                primary, page_images, db, filename
            )
            if merged.strip():
                source = SOURCE_PADDLEOCR if not weak_pages else f"{SOURCE_PADDLEOCR}+{SOURCE_VISION_FALLBACK}"
                return OcrChainResult(
                    markdown=strip_markdown_noise(merged),
                    source=source,
                    assets=assets,
                    page_count=primary.page_count,
                    used_fallback=bool(weak_pages),
                    primary_error=primary_error,
                    fallback_pages=[i + 1 for i in weak_pages],
                )

        # ---- 第二层:整卷兜底 ------------------------------------------ #
        # 只在兜底确实能带来更多内容时才采用它,避免把一个已经不错的
        # 一级结果换成更差的版本(视觉模型对长文档的稳定性不如专用 OCR)。
        pages = page_images or [document]
        fallback_md, fb_error = await self._run_fallback(pages, db, filename)
        primary_md = strip_markdown_noise(primary.markdown) if (primary is not None and primary.usable) else ""

        if fallback_md.strip() and len(fallback_md.strip()) > len(primary_md) * 1.2:
            return OcrChainResult(
                markdown=strip_markdown_noise(fallback_md),
                source=SOURCE_VISION_FALLBACK,
                page_count=len(pages),
                used_fallback=True,
                primary_error=primary_error or fb_error,
                fallback_pages=list(range(1, len(pages) + 1)),
            )

        if primary_md:
            logger.info("视觉兜底未带来更多内容,保留主 OCR 结果(%d 字)", len(primary_md))
            return OcrChainResult(
                markdown=primary_md,
                source=SOURCE_PADDLEOCR,
                assets=primary.assets if primary is not None else {},
                page_count=primary.page_count if primary is not None else len(pages),
            )

        reason = "；".join(x for x in (primary_error, fb_error) if x) or "未产出任何可用内容"
        raise OcrError(f"文档解析失败:{reason}")

    # ------------------------------------------------------------------ #
    # 第一层
    # ------------------------------------------------------------------ #

    async def _run_primary(
        self, document: bytes, filename: Optional[str], db: AsyncSession
    ) -> Optional[OcrResult]:
        interface_type, config = await resolve_ocr_provider(db)
        if interface_type != "paddleocr" or not config.get("API_KEY"):
            logger.info("未配置 PaddleOCR 主解析(interface_type=%s)", interface_type)
            return None

        try:
            client = self._paddle_client_factory(config)
            return await client.parse(data=document, filename=filename)
        except OcrError as exc:
            logger.warning("PaddleOCR 主解析失败: %s", exc)
            return OcrResult(markdown="", source=SOURCE_NONE, primary_error=str(exc))
        except Exception:  # noqa: BLE001 - 兜底路径必须能接住任何异常
            logger.exception("PaddleOCR 主解析异常")
            return OcrResult(markdown="", source=SOURCE_NONE, primary_error="主 OCR 调用异常")

    def _whole_doc_ok(self, primary: OcrResult, page_images: Optional[list[bytes]]) -> bool:
        text = primary.markdown.strip()
        if len(text) < self.min_total_chars:
            return False
        if not page_images:
            return True
        # 页数对不上 → 可能漏页,不能直接信。
        if primary.page_count and primary.page_count != len(page_images):
            return False
        return all(len(p.strip()) >= self.min_chars_per_page for p in primary.page_texts)

    # ------------------------------------------------------------------ #
    # 页级合并
    # ------------------------------------------------------------------ #

    async def _merge_with_fallback(
        self,
        primary: OcrResult,
        page_images: list[bytes],
        db: AsyncSession,
        filename: Optional[str],
    ) -> tuple[str, list[int], dict[str, bytes]]:
        weak_idx = [
            i for i, text in enumerate(primary.page_texts)
            if len(text.strip()) < self.min_chars_per_page
        ]
        if not weak_idx:
            return primary.markdown, [], primary.assets

        _, config = await resolve_vision_provider(db)
        if not config.get("API_KEY"):
            logger.warning("存在 %d 页未识别,但未配置视觉兜底模型,保留主 OCR 结果", len(weak_idx))
            return primary.markdown, [], primary.assets

        weak_images = [page_images[i] for i in weak_idx]
        page_texts, error = await self._transcribe(weak_images, config=config, filename=filename)
        if error and not any(t.strip() for t in page_texts):
            logger.warning("视觉兜底未补齐缺失页: %s", error)
            return primary.markdown, [], primary.assets

        # transcribe_pages 与入参严格同序同长,可以精确回填,不需要切分启发式。
        patched = list(primary.page_texts)
        filled = 0
        for slot, idx in enumerate(weak_idx):
            text = page_texts[slot] if slot < len(page_texts) else ""
            if text.strip():
                patched[idx] = text
                filled += 1

        merged = "\n\n".join(p for p in patched if p and p.strip())
        logger.info("页级合并完成:主 OCR %d 页,视觉兜底补齐 %d/%d 页", len(patched), filled, len(weak_idx))
        return merged, weak_idx if filled else [], primary.assets

    # ------------------------------------------------------------------ #
    # 第二层
    # ------------------------------------------------------------------ #

    async def _run_fallback(
        self, pages: list[bytes], db: AsyncSession, filename: Optional[str]
    ) -> tuple[str, Optional[str]]:
        _, config = await resolve_vision_provider(db)
        if not config.get("API_KEY"):
            return "", "未配置视觉兜底模型(AI_VISION_MODEL_ID)"
        page_texts, error = await self._transcribe(pages, config=config, filename=filename)
        if error and not any(t.strip() for t in page_texts):
            return "", error
        return "\n\n".join(t for t in page_texts if t.strip()), error


def _default_paddle_client(config: dict) -> PaddleOcrClient:
    return PaddleOcrClient(
        token=config.get("API_KEY", ""),
        model=config.get("MODEL_NAME") or "PaddleOCR-VL-1.6",
        job_url=config.get("BASE_URL") or "",
    )
