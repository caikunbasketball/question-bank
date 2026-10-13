"""PDF 章节批量导入:把"勾选若干章节"变成一次可断点续传、可重试的批量任务。

单个章节的处理就是既有导入主干的一次复用,没有另起炉灶:

    切片 ──► DocumentOcrChain ──► AIExtractor ──► QuestionClassifier ──► 落库
           ①PaddleOCR-VL 主解析    ③文本模型抽题    ③文本模型分类归属
           ②视觉模型按需兜底

三个工程性语义都落在 **item(章节)粒度**上,不是整个任务粒度:

- **失败重试**:某章 OCR 挂了不影响其他章;重试只重跑这一个 item。
- **断点续传**:OCR 结果连同内容指纹一起缓存。重跑时指纹没变就直接复用缓存,
  不再花第二次 API 的钱——OCR 是这条链上最贵的一步。
- **重复导入检测**:切片字节的 SHA-256 在这本书下已经有过成功记录,就标 SKIPPED,
  避免同一章被导两遍、题重复进库。

注意"重复"和"断点续传"是不同的事:前者是**别的 item 已经导过同样的内容**,
后者是**这个 item 自己上次跑到一半**。判据不同,不能合并成一条。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.book import BookChapter, BookSection, SourceBook
from app.models.import_task import ImportTask, ImportTaskStatus
from app.models.pdf_chapter_import import (
    ChapterImportItemStatus,
    ChapterImportTaskStatus,
    PdfChapterImportItem,
    PdfChapterImportTask,
)
from app.models.question import QuestionStatus
from app.services.importing.classify import QuestionClassifier
from app.services.importing.contracts import ImportDefaults
from app.services.importing.extract import AIExtractor
from app.services.importing.normalize import QuestionImporter
from app.services.importing.pdf_split import (
    ChapterRange,
    content_fingerprint,
    flatten_ranges,
    page_count,
    rasterize,
    sha256,
    slice_pages,
)
from app.services.ocr import DocumentOcrChain

logger = logging.getLogger(__name__)

# 单个章节的页数上限。超过它通常是用户在预览页把区间拉错了,而不是真的有这么长的
# 一章——拦下来比让它烧掉一整章的 OCR 配额划算。
MAX_CHAPTER_PAGES = 120


class ChapterImportError(RuntimeError):
    """章节导入失败。message 会原样进 item.error_message,不要塞堆栈。"""


class PdfChapterImportService:
    """章节批量导入的编排层。"""

    def __init__(
        self,
        *,
        extractor: Optional[AIExtractor] = None,
        classifier: Optional[QuestionClassifier] = None,
        importer: Optional[QuestionImporter] = None,
        ocr_chain: Optional[DocumentOcrChain] = None,
    ) -> None:
        self._extractor = extractor or AIExtractor()
        self._classifier = classifier or QuestionClassifier()
        self._importer = importer or QuestionImporter()
        self._ocr_chain = ocr_chain or DocumentOcrChain()

    # ------------------------------------------------------------------ #
    # 建任务
    # ------------------------------------------------------------------ #

    async def create_task(
        self,
        db: AsyncSession,
        *,
        user_id: Optional[int],
        subject_id: Optional[int],
        source_path: str,
        book_title: str,
        ranges: list[ChapterRange],
        mode: str = "extract",
        book_id: Optional[int] = None,
        source_filename: Optional[str] = None,
    ) -> PdfChapterImportTask:
        """落库"书 → 章 → 小节"骨架,并为每个待导入区间建一条子任务。

        ``ranges`` 是前端校正后的结果(用户可能改过章节名和页码),这里只做一次
        normalize,之后落库、切分拿到的都是同一份干净页码。
        """
        if not ranges:
            raise ChapterImportError("没有选择任何章节")

        path = Path(source_path)
        if not path.is_file():
            raise ChapterImportError(f"源 PDF 不存在: {source_path}")

        raw = path.read_bytes()
        total = page_count(raw)
        book = await self._get_or_create_book(
            db,
            book_id=book_id,
            title=book_title,
            subject_id=subject_id,
            filename=source_filename or path.name,
            content_sha256=sha256(raw),
            page_count_value=total,
            user_id=user_id,
        )

        # 章 → 小节落库,并记住每个 range 对应的 ORM 行。
        chapter_rows: list[BookChapter] = []
        # 每个章的小节行列表;整章(无小节)的章也有一条"整章"小节,保证题目
        # 永远有最细的一层可挂。
        section_rows: list[list[BookSection]] = []

        for order, chapter_range in enumerate(ranges):
            start, end = chapter_range.start_page, chapter_range.end_page
            chapter = BookChapter(
                book_id=book.id,
                title=chapter_range.title,
                order_index=order,
                start_page=start,
                end_page=end,
            )
            db.add(chapter)
            await db.flush()
            chapter_rows.append(chapter)

            if chapter_range.children:
                rows = []
                for s_order, section_range in enumerate(chapter_range.children):
                    section = BookSection(
                        book_id=book.id,
                        chapter_id=chapter.id,
                        title=section_range.title,
                        order_index=s_order,
                        start_page=section_range.start_page,
                        end_page=section_range.end_page,
                    )
                    db.add(section)
                    rows.append(section)
                section_rows.append(rows)
            else:
                section = BookSection(
                    book_id=book.id,
                    chapter_id=chapter.id,
                    title=chapter_range.title,
                    order_index=0,
                    start_page=start,
                    end_page=end,
                )
                db.add(section)
                section_rows.append([section])

        await db.flush()

        task = PdfChapterImportTask(
            book_id=book.id,
            user_id=user_id,
            subject_id=subject_id,
            mode=mode,
            source_path=str(path),
            source_filename=source_filename or path.name,
            status=ChapterImportTaskStatus.PENDING,
            total_items=0,
        )
        db.add(task)
        await db.flush()

        flat = flatten_ranges(ranges, total)
        for order, item_range in enumerate(flat):
            chapter = chapter_rows[item_range.chapter_index]
            if item_range.section_index is None:
                section = section_rows[item_range.chapter_index][0]
            else:
                section = section_rows[item_range.chapter_index][item_range.section_index]
            db.add(
                PdfChapterImportItem(
                    task_id=task.id,
                    chapter_id=chapter.id,
                    section_id=section.id,
                    title=item_range.title,
                    start_page=item_range.start_page,
                    end_page=item_range.end_page,
                    order_index=order,
                    status=ChapterImportItemStatus.PENDING,
                )
            )
        task.total_items = len(flat)
        await db.commit()
        # 带 items 重新读一次:调用方(API 序列化、测试断言)紧接着就要访问
        # task.items,在 async 会话里靠 lazy load 会抛 MissingGreenlet。
        return await self._load_task(db, task.id)

    async def _get_or_create_book(
        self,
        db: AsyncSession,
        *,
        book_id: Optional[int],
        title: str,
        subject_id: Optional[int],
        filename: str,
        content_sha256: str,
        page_count_value: int = 0,
        user_id: Optional[int] = None,
    ) -> SourceBook:
        """指定 book_id 就复用;否则按 (subject, title) 复用,没有才新建。"""
        if book_id is not None:
            book = await db.get(SourceBook, book_id)
            if book is None:
                raise ChapterImportError(f"指定的书籍不存在: {book_id}")
            return book

        stmt = select(SourceBook).where(
            SourceBook.title == title, SourceBook.subject_id == subject_id
        )
        existing = (await db.execute(stmt)).scalars().first()
        if existing is not None:
            return existing

        book = SourceBook(
            title=title,
            subject_id=subject_id,
            filename=filename,
            content_sha256=content_sha256,
            page_count=page_count_value,
            created_by=user_id,
        )
        db.add(book)
        await db.flush()
        return book

    # ------------------------------------------------------------------ #
    # 跑任务 / 重试
    # ------------------------------------------------------------------ #

    async def run_task(
        self, db: AsyncSession, task_id: int, *, only_pending: bool = True
    ) -> PdfChapterImportTask:
        """按序处理每个子任务。

        ``only_pending=True`` 时跳过已完成的 item——这就是断点续传:任务中断后
        重跑,已入库的章节不会再来一遍。单章失败不中断整批。
        """
        task = await self._load_task(db, task_id)
        task.status = ChapterImportTaskStatus.PROCESSING
        task.error_message = None
        await db.commit()

        for item in task.items:
            if (
                only_pending
                and item.status == ChapterImportItemStatus.COMPLETED
            ):
                continue
            if item.status == ChapterImportItemStatus.CANCELLED:
                continue
            await self._process_item(db, task, item)

        self._refresh_counters(task)
        await db.commit()
        # 重新读一次而不是 refresh:refresh 不保证关系集合是填充好的。
        return await self._load_task(db, task_id)

    async def retry_item(self, db: AsyncSession, item_id: int) -> PdfChapterImportItem:
        """重跑单个失败的子任务,并顺带刷新父任务计数。"""
        item = await db.get(PdfChapterImportItem, item_id)
        if item is None:
            raise ChapterImportError(f"子任务不存在: {item_id}")
        if item.status not in (
            ChapterImportItemStatus.FAILED,
            ChapterImportItemStatus.CANCELLED,
            ChapterImportItemStatus.PENDING,
        ):
            raise ChapterImportError("只有失败、取消或未开始的子任务可以重试")

        task = await self._load_task(db, item.task_id)
        item.status = ChapterImportItemStatus.PENDING
        item.error_message = None
        await db.commit()

        await self._process_item(db, task, item)
        self._refresh_counters(task)
        await db.commit()
        await db.refresh(item)
        return item

    async def _load_task(self, db: AsyncSession, task_id: int) -> PdfChapterImportTask:
        """读任务并**强制**带上子项。

        ``populate_existing`` 不是可选项:同一个 session 里,刚建完任务的那个对象
        还躺在 identity map 中,它的 items 集合处于"未加载"状态。此时再 select 一次,
        SQLAlchemy 会直接返回那个旧实例而不重新填充关系,selectinload 形同虚设,
        下游一访问 task.items 就在 async 会话里抛 MissingGreenlet。
        """
        stmt = (
            select(PdfChapterImportTask)
            .options(
                selectinload(PdfChapterImportTask.items),
                selectinload(PdfChapterImportTask.book),
            )
            .execution_options(populate_existing=True)
            .where(PdfChapterImportTask.id == task_id)
        )
        task = (await db.execute(stmt)).scalars().first()
        if task is None:
            raise ChapterImportError(f"导入任务不存在: {task_id}")
        return task

    @staticmethod
    def _refresh_counters(task: PdfChapterImportTask) -> None:
        """根据子项状态重算进度与整任务状态。

        状态判定:有失败 → 有成功则 PARTIAL、否则 FAILED;没有失败 → COMPLETED
        (跳过算完成的一种,因为"这章已经导过"本来就是预期结果)。
        """
        items = task.items or []
        task.total_items = len(items)
        task.completed_items = sum(
            1 for i in items if i.status == ChapterImportItemStatus.COMPLETED
        )
        task.failed_items = sum(1 for i in items if i.status == ChapterImportItemStatus.FAILED)
        task.skipped_items = sum(1 for i in items if i.status == ChapterImportItemStatus.SKIPPED)
        task.imported_questions = sum(i.questions_count or 0 for i in items)

        if task.failed_items:
            task.status = (
                ChapterImportTaskStatus.PARTIAL
                if (task.completed_items or task.skipped_items)
                else ChapterImportTaskStatus.FAILED
            )
        else:
            task.status = ChapterImportTaskStatus.COMPLETED

    # ------------------------------------------------------------------ #
    # 单个章节
    # ------------------------------------------------------------------ #

    async def _process_item(
        self,
        db: AsyncSession,
        task: PdfChapterImportTask,
        item: PdfChapterImportItem,
    ) -> None:
        item.status = ChapterImportItemStatus.PROCESSING
        item.attempt_count = (item.attempt_count or 0) + 1
        item.error_message = None
        await db.commit()

        try:
            source = Path(task.source_path)
            if not source.is_file():
                raise ChapterImportError("源 PDF 已不存在,无法继续导入")
            raw = source.read_bytes()

            chapter_pdf = slice_pages(raw, item.start_page, item.end_page)
            span = item.end_page - item.start_page + 1
            if span > MAX_CHAPTER_PAGES:
                raise ChapterImportError(
                    f"章节跨度 {span} 页超出上限 {MAX_CHAPTER_PAGES} 页,请缩小区间后重试"
                )
            # 用页面内容指纹而非切片字节的 sha:后者每次切片都不一样,查重会失效。
            content_hash = content_fingerprint(chapter_pdf)
            item.content_sha256 = content_hash

            # 重复导入检测:这本书下已有别的 item 成功导入过完全相同的内容。
            duplicate = await self._find_duplicate(db, task.book_id, content_hash, exclude_item_id=item.id)
            if duplicate is not None:
                item.status = ChapterImportItemStatus.SKIPPED
                item.error_message = f"内容重复,已于子任务 #{duplicate} 导入过,本次跳过"
                item.questions_count = 0
                await db.commit()
                logger.info("章节「%s」内容重复,跳过导入", item.title)
                return

            markdown, ocr_meta = await self._ocr_chapter(db, chapter_pdf, item, content_hash)
            questions = await self._extract_questions(db, task, markdown)
            if not questions:
                item.status = ChapterImportItemStatus.COMPLETED
                item.questions_count = 0
                item.ocr_meta = json.dumps(ocr_meta, ensure_ascii=False)
                await db.commit()
                logger.info("章节「%s」未抽取到题目", item.title)
                return

            classifications = await self._classifier.classify(
                questions,
                db=db,
                book_id=task.book_id,
                subject_id=task.subject_id,
                chapter_id=item.chapter_id,
            )
            await self._persist_questions(db, task, item, questions, classifications, ocr_meta)
        except ChapterImportError as exc:
            item.status = ChapterImportItemStatus.FAILED
            item.error_message = str(exc)
            await db.commit()
            logger.warning("章节「%s」导入失败: %s", item.title, exc)
        except Exception as exc:  # noqa: BLE001 - 单章失败不能拖垮整批
            logger.exception("章节「%s」导入异常", item.title)
            item.status = ChapterImportItemStatus.FAILED
            item.error_message = f"{type(exc).__name__}: {exc}"
            await db.commit()

    async def _ocr_chapter(
        self,
        db: AsyncSession,
        chapter_pdf: bytes,
        item: PdfChapterImportItem,
        content_hash: str,
    ) -> tuple[str, dict]:
        """跑 OCR 链,能复用缓存就复用。

        缓存命中条件:上次跑过、指纹一致、且确实有内容。OCR 是整条链上最贵的一步,
        断点续传真正的省钱点在这里。
        """
        cached_hash = item.content_sha256
        if (
            item.ocr_markdown
            and item.ocr_markdown.strip()
            and cached_hash == content_hash
        ):
            logger.info("章节「%s」复用已缓存的 OCR 结果(断点续传)", item.title)
            try:
                meta = json.loads(item.ocr_meta or "{}")
            except (TypeError, ValueError):
                meta = {}
            meta["from_cache"] = True
            return item.ocr_markdown, meta

        page_images = rasterize(chapter_pdf)
        result = await self._ocr_chain.parse(
            document=chapter_pdf,
            filename=f"{item.title}.pdf",
            db=db,
            page_images=page_images,
        )
        markdown = self._persist_assets(result.markdown, result.assets, f"ch{item.id}")
        meta = {
            "source": result.source,
            "page_count": result.page_count,
            "used_fallback": result.used_fallback,
            "fallback_pages": result.fallback_pages,
            "primary_error": result.primary_error,
            "from_cache": False,
        }
        item.ocr_markdown = markdown
        item.ocr_meta = json.dumps(meta, ensure_ascii=False)
        logger.info(
            "章节「%s」OCR 完成: source=%s fallback=%s",
            item.title, result.source, result.fallback_pages,
        )
        return markdown, meta

    @staticmethod
    def _persist_assets(markdown: str, assets: dict, media_id: str) -> str:
        """OCR 图片落盘并改写成公开 URL。复用 doc_processor 的实现,不另写一套。"""
        if not assets:
            return markdown
        # 延迟导入:doc_processor 会拉起 pandoc 依赖链,只在真的有图片时才需要。
        from app.services.doc_processor import persist_ocr_assets

        return persist_ocr_assets(markdown, assets, media_id)

    async def _extract_questions(
        self, db: AsyncSession, task: PdfChapterImportTask, markdown: str
    ) -> list[dict]:
        if not markdown.strip():
            return []
        extraction = await self._extractor.extract(
            markdown,
            db,
            filename=task.source_filename,
            mode=task.mode or "extract",
            subject_id=task.subject_id,
        )
        return list(extraction.questions or [])

    async def _persist_questions(
        self,
        db: AsyncSession,
        task: PdfChapterImportTask,
        item: PdfChapterImportItem,
        questions: list[dict],
        classifications,
        ocr_meta: dict,
    ) -> None:
        """分类结果回写题目,再整批落库。"""
        source_label = f"{task.source_filename or 'PDF'} · {item.title}"
        import_task = ImportTask(
            user_id=task.user_id,
            file_path=task.source_path,
            original_filename=f"{task.source_filename or 'pdf'}#{item.title}",
            file_type="pdf",
            status=ImportTaskStatus.PENDING,
            source="pdf_chapter",
            description=f"章节导入:{source_label}(第 {item.start_page}-{item.end_page} 页)",
            mode=task.mode or "extract",
        )
        db.add(import_task)
        await db.flush()

        pending_review = 0
        for index, question in enumerate(questions):
            classification = classifications[index] if index < len(classifications) else None
            question["book_id"] = task.book_id
            question["chapter_id"] = item.chapter_id
            question["section_id"] = item.section_id
            if classification is not None:
                if classification.q_type:
                    question["q_type"] = classification.q_type
                if classification.knowledge_point_ids:
                    # 分类器给了明确结果才覆盖抽取阶段的知识点,否则保留原结果。
                    question["knowledge_point_ids"] = classification.knowledge_point_ids
                if classification.needs_review:
                    pending_review += 1
                    # 落在题目自己的列上,而不是塞进 ai_suggested_tags——后者会被
                    # 转成标签,拿"置信度过低"这种文案当标签名只会污染标签体系。
                    question["classification_review"] = json.dumps(
                        {
                            "confidence": round(classification.confidence, 3),
                            "reason": classification.review_reason or "分类待人工确认",
                        },
                        ensure_ascii=False,
                    )

        report = await self._importer.import_batch(
            db,
            questions,
            user_id=task.user_id,
            import_task_id=import_task.id,
            defaults=ImportDefaults(
                subject_id=task.subject_id,
                status=QuestionStatus.PENDING,
                source=source_label,
                book_id=task.book_id,
                chapter_id=item.chapter_id,
                section_id=item.section_id,
            ),
        )

        import_task.status = ImportTaskStatus.COMPLETED
        import_task.result_summary = json.dumps(
            {
                "count": report.saved_count,
                "failed": report.failed_count,
                "pending_review": pending_review,
                "ocr": ocr_meta,
            },
            ensure_ascii=False,
        )

        item.status = ChapterImportItemStatus.COMPLETED
        item.questions_count = report.saved_count
        item.import_task_id = import_task.id
        item.ocr_meta = json.dumps(ocr_meta, ensure_ascii=False)
        if report.failed_count:
            item.error_message = f"{report.failed_count} 道题入库失败(其余已导入)"
        await db.commit()
        logger.info(
            "章节「%s」导入完成:入库 %d 题,待审核 %d 题",
            item.title, report.saved_count, pending_review,
        )

    @staticmethod
    async def _find_duplicate(
        db: AsyncSession, book_id: int, content_hash: str, *, exclude_item_id: int
    ) -> Optional[int]:
        """这本书下是否已有别的子任务成功导入过完全相同的内容。"""
        stmt = (
            select(PdfChapterImportItem.id)
            .join(PdfChapterImportTask, PdfChapterImportItem.task_id == PdfChapterImportTask.id)
            .where(
                PdfChapterImportTask.book_id == book_id,
                PdfChapterImportItem.content_sha256 == content_hash,
                PdfChapterImportItem.status == ChapterImportItemStatus.COMPLETED,
                PdfChapterImportItem.id != exclude_item_id,
            )
            .limit(1)
        )
        return (await db.execute(stmt)).scalar_one_or_none()


pdf_chapter_import_service = PdfChapterImportService()
