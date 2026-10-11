"""文档处理门面:把"取文件 → (可选)OCR 解析 → 抽取 → 归一化"串成对 API 友好的方法。

三层模型方案的落点就在 ``process_image`` / ``process_pdf``:

    图片 / PDF ──► DocumentOcrChain ──► markdown ──► AIExtractor(文本路径) ──► 题目
                  ① PaddleOCR-VL                   ③ DeepSeek 结构化
                  ② Qwen3-VL 兜底

docx / md 本来就是文本,不走 OCR 层,直接进第三层结构化。
"""
import logging
import re
from pathlib import Path
from typing import BinaryIO, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.importing.extract import AIExtractor, ExtractionStrategy, TemplateExtractor
from app.services.importing.ingest import (
    CanonicalDoc,
    DocxIngestor,
    ImageIngestor,
    MarkdownArchiveIngestor,
    MarkdownIngestor,
    PdfIngestor,
)
from app.services.importing.provider_config import resolve_text_provider, resolve_vision_provider
from app.services.ocr import DocumentOcrChain

logger = logging.getLogger(__name__)


class DocProcessor:
    def __init__(self):
        self.client = None
        self._ai_extractor = AIExtractor()
        self._template_extractor = TemplateExtractor()
        self._docx_ingestor = DocxIngestor()
        self._markdown_ingestor = MarkdownIngestor()
        self._markdown_archive_ingestor = MarkdownArchiveIngestor()
        self._image_ingestor = ImageIngestor()
        self._pdf_ingestor = PdfIngestor()
        self._ocr_chain = DocumentOcrChain()

    def _extractor_for(self, method: str) -> ExtractionStrategy:
        return self._template_extractor if method == "structured" else self._ai_extractor

    # ------------------------------------------------------------------ #
    # 文本源(docx / md / zip)
    # ------------------------------------------------------------------ #

    async def process_markdown(self, content: str, db: AsyncSession, filename: str = None, task_id: str = None, mode: str = "extract", method: str = "ai", subject_id: Optional[int] = None) -> dict:
        """
        Process markdown content directly and extract questions.
        
        Args:
            content: Markdown text content
            db: Database session
            filename: Optional filename
            task_id: Optional task ID (if not provided, a new one will be generated)
            mode: Processing mode ("extract" or "solve")
            method: Parsing method ("ai" for AI extraction, "structured" for tag-based parsing)
        
        Returns:
            Dict with task_id, content, and extracted questions
        """
        doc = await self._markdown_ingestor.ingest(content, task_id=task_id, filename=filename)
        extraction = await self._extractor_for(method).extract(
            doc.markdown, db, filename=doc.filename, mode=mode, subject_id=subject_id
        )
        return {
            "task_id": doc.task_id,
            "content": doc.markdown,
            "questions": extraction.questions,
            "paper": extraction.paper,
        }

    async def process_markdown_archive(self, file_path: Path, db: AsyncSession, task_id: str = None, mode: str = "extract", method: str = "ai", subject_id: Optional[int] = None) -> dict:
        """Extract a markdown archive (zip with local images) and parse questions.

        Local image refs are rewritten to /static/media/{task_id}/... before extraction; multiple
        .md files in the archive are concatenated into one document.
        """
        doc = await self._markdown_archive_ingestor.ingest(file_path, task_id=task_id)
        extraction = await self._extractor_for(method).extract(
            doc.markdown, db, filename=doc.filename, mode=mode, subject_id=subject_id
        )
        return {
            "task_id": doc.task_id,
            "content": doc.markdown,
            "questions": extraction.questions,
            "paper": extraction.paper,
        }

    async def process_docx(self, file_path: Path, db: AsyncSession = None, task_id: str = None, mode: str = "extract", method: str = "ai", subject_id: Optional[int] = None) -> dict:
        """Convert docx to markdown, extract media, and parse questions."""
        doc = await self._docx_ingestor.ingest(file_path, task_id=task_id)
        extraction = await self._extractor_for(method).extract(
            doc.markdown, db, filename=doc.filename, mode=mode, subject_id=subject_id
        )
        return {
            "task_id": doc.task_id,
            "content": doc.markdown,
            "questions": extraction.questions,
            "paper": extraction.paper,
        }

    # ------------------------------------------------------------------ #
    # 图像源 / PDF 源 —— 走三层链
    # ------------------------------------------------------------------ #

    async def process_image(self, image_file: BinaryIO, db: AsyncSession, task_id: str = None, mode: str = "extract", subject_id: Optional[int] = None) -> dict:
        """图片 → OCR 链(①PaddleOCR-VL ②Qwen3-VL) → markdown → ③文本模型抽题。"""
        doc = await self._image_ingestor.ingest(image_file, task_id=task_id)
        return await self._process_visual_doc(doc, db, mode=mode, subject_id=subject_id, image_url=doc.image_url)

    async def process_pdf(self, file_path: Path, db: AsyncSession, task_id: str = None, mode: str = "extract", subject_id: Optional[int] = None) -> dict:
        """PDF → 逐页栅格化 → OCR 链 → markdown → ③文本模型抽题。"""
        doc = await self._pdf_ingestor.ingest(file_path, task_id=task_id)
        return await self._process_visual_doc(doc, db, mode=mode, subject_id=subject_id, image_url=doc.image_url)

    async def _process_visual_doc(
        self,
        doc: CanonicalDoc,
        db: AsyncSession,
        *,
        mode: str,
        subject_id: Optional[int],
        image_url: str,
    ) -> dict:
        """图像/PDF 共用主干:OCR 链产出 markdown,再交给第三层做结构化抽取。"""
        text_provider, _ = await resolve_text_provider(db)
        vision_provider, _ = await resolve_vision_provider(db)

        # 兼容性兜底:老部署可能只配了视觉模型、没配文本模型。这种情况下 OCR 链
        # 仍然能产出 markdown,但第三层无处可去——退回"视觉模型一步到位抽题"的旧行为,
        # 避免把原本能跑的图片导入变成报错。
        if text_provider is None and vision_provider is not None:
            logger.info("未配置文本模型,图片导入退回视觉模型直抽的旧路径")
            extraction = await self._ai_extractor.extract(
                "", db, image_data=doc.image_data or (doc.page_images or [None])[0], mode=mode, subject_id=subject_id
            )
            return {
                "task_id": doc.task_id,
                "image_url": image_url,
                "content": "",
                "questions": extraction.questions,
                "paper": extraction.paper,
            }

        markdown, ocr_meta = await self._ocr_to_markdown(doc, db)
        extraction = await self._ai_extractor.extract(
            markdown, db, filename=doc.filename, mode=mode, subject_id=subject_id
        )
        return {
            "task_id": doc.task_id,
            "image_url": image_url,
            "content": markdown,
            "ocr": ocr_meta,
            "questions": extraction.questions,
            "paper": extraction.paper,
        }

    async def _ocr_to_markdown(self, doc: CanonicalDoc, db: AsyncSession) -> tuple[str, dict]:
        """跑三层链的前两层,把图片资源落盘并把引用改写成 /static/media 路径。"""
        document = doc.document or doc.image_data or b""
        result = await self._ocr_chain.parse(
            document=document,
            filename=doc.filename,
            db=db,
            page_images=doc.page_images,
        )
        markdown = _persist_assets(result.markdown, result.assets, doc.task_id)
        meta = {
            "source": result.source,
            "page_count": result.page_count,
            "used_fallback": result.used_fallback,
            "fallback_pages": result.fallback_pages,
            "primary_error": result.primary_error,
        }
        logger.info(
            "OCR 链完成: source=%s pages=%s fallback=%s",
            result.source,
            result.page_count,
            result.fallback_pages,
        )
        return markdown, meta


_ASSET_MARKDOWN_RE = re.compile(r"(!\[[^\]]*\]\()([^)\s]+)((?:\s+\"[^\"]*\")?\))")


def _persist_assets(markdown: str, assets: dict[str, bytes], task_id: str) -> str:
    """把 OCR 返回的图片字节落到 media 目录,并把 markdown 里的引用改写成公开 URL。

    PaddleOCR 的图片是远端临时链接,必须落到本地,否则题库里的题图迟早变死链。
    """
    if not assets:
        return markdown

    asset_dir = settings.MEDIA_DIR / task_id / "ocr"
    asset_dir.mkdir(parents=True, exist_ok=True)

    url_by_name: dict[str, str] = {}
    for rel_path, content in assets.items():
        safe_rel = rel_path.replace("\\", "/").lstrip("/")
        # 只取文件名,避免 ../ 之类的相对层级把文件写到别处。
        filename = safe_rel.rsplit("/", 1)[-1]
        if not filename:
            continue
        (asset_dir / filename).write_bytes(content)
        public_url = f"/static/media/{task_id}/ocr/{filename}"
        url_by_name[rel_path] = public_url
        url_by_name[filename] = public_url

    def _sub(match: re.Match) -> str:
        raw = match.group(2)
        for key, public_url in url_by_name.items():
            if raw == key or raw.endswith("/" + key):
                return f"{match.group(1)}{public_url}{match.group(3)}"
        return match.group(0)

    return _ASSET_MARKDOWN_RE.sub(_sub, markdown)


doc_processor = DocProcessor()
