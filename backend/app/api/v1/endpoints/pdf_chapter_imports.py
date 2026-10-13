"""PDF 章节拆分与批量导入的 HTTP 入口。

三步走,每一步都留有人工校正的位置:

    POST /parse-toc   上传 PDF → 返回目录预览(含识别来源、页码偏移、未识别原因)
        ↓ 前端:用户改章节名 / 起止页码 / 增删章节 / 勾选要导的章节
    POST /tasks       提交校正后的方案 → 建任务,返回任务 id(此时还没跑)
        ↓ 前端:确认无误
    POST /tasks/{id}/run   真正开始跑(进度靠 GET 轮询)

把"建任务"和"跑任务"分开,是因为跑起来要真金白银调 OCR;让用户在跑之前有一次
反悔的机会,比跑完了再删题便宜得多。

**源文件路径的安全**:`POST /tasks` 收的是上一步返回的绝对路径,客户端可控。
所有传入路径都会被 `_resolve_source_path` 约束在 `settings.UPLOAD_DIR` 内,
避免任意文件读取。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import models
from app.api import deps
from app.core.config import settings
from app.models import (
    BookChapter,
    BookSection,
    PdfChapterImportItem,
    PdfChapterImportTask,
    SourceBook,
)
from app.models.pdf_chapter_import import ChapterImportItemStatus, ChapterImportTaskStatus
from app.services.importing.pdf_split import ChapterRange, page_count
from app.services.importing.pdf_toc import resolve_toc, build_chapter_ranges
from app.services.importing.pdf_chapter_import import (
    ChapterImportError,
    PdfChapterImportService,
)

router = APIRouter()


# --------------------------------------------------------------------------- #
# 请求 / 响应契约
# --------------------------------------------------------------------------- #

class ChapterInput(BaseModel):
    """前端回传的一个章节区间。children 非空时按小节拆分导入。"""

    title: str
    start_page: int
    end_page: int
    children: list["ChapterInput"] = Field(default_factory=list)


class CreateTaskRequest(BaseModel):
    source_path: str
    book_title: str
    book_id: Optional[int] = None
    subject_id: Optional[int] = None
    mode: str = "extract"
    chapters: list[ChapterInput]


def _to_chapter_ranges(chapters: list[ChapterInput]) -> list[ChapterRange]:
    return [
        ChapterRange(
            title=chapter.title,
            start_page=chapter.start_page,
            end_page=chapter.end_page,
            children=_to_chapter_ranges(chapter.children) if chapter.children else [],
        )
        for chapter in chapters
    ]


def _chapter_payload(chapter: ChapterRange) -> dict[str, Any]:
    return {
        "title": chapter.title,
        "start_page": chapter.start_page,
        "end_page": chapter.end_page,
        "children": [_chapter_payload(c) for c in chapter.children],
    }


def _item_payload(item: PdfChapterImportItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title,
        "chapter_id": item.chapter_id,
        "section_id": item.section_id,
        "start_page": item.start_page,
        "end_page": item.end_page,
        "order_index": item.order_index,
        "status": item.status.value if hasattr(item.status, "value") else str(item.status),
        "attempt_count": item.attempt_count,
        "error_message": item.error_message,
        "questions_count": item.questions_count,
        "import_task_id": item.import_task_id,
    }


def _task_payload(task: PdfChapterImportTask, *, with_items: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": task.id,
        "book_id": task.book_id,
        "book_title": task.book.title if task.book else None,
        "subject_id": task.subject_id,
        "mode": task.mode,
        "source_filename": task.source_filename,
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "error_message": task.error_message,
        "total_items": task.total_items,
        "completed_items": task.completed_items,
        "failed_items": task.failed_items,
        "skipped_items": task.skipped_items,
        "imported_questions": task.imported_questions,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }
    if with_items and task.items is not None:
        payload["items"] = [_item_payload(i) for i in task.items]
    return payload


# --------------------------------------------------------------------------- #
# 路径安全
# --------------------------------------------------------------------------- #

def _resolve_source_path(raw: str) -> Path:
    """把客户端传回的路径约束在 UPLOAD_DIR 内,杜绝任意文件读取。"""
    try:
        path = Path(raw).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="非法的源文件路径")
    root = Path(settings.UPLOAD_DIR).resolve()
    if path != root and root not in path.parents:
        raise HTTPException(status_code=400, detail="非法的源文件路径")
    return path


async def _save_upload(file: UploadFile) -> Path:
    session_id = str(uuid.uuid4())
    upload_dir = Path(settings.UPLOAD_DIR) / session_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = file.filename or "upload.pdf"
    path = upload_dir / filename
    content = await file.read()
    await asyncio.to_thread(path.write_bytes, content)
    return path


def _service() -> PdfChapterImportService:
    return PdfChapterImportService()


# --------------------------------------------------------------------------- #
# ① 上传 + 目录识别
# --------------------------------------------------------------------------- #

@router.post("/parse-toc", response_model=dict)
async def parse_pdf_toc(
    db: deps.SessionDep,
    file: UploadFile = File(...),
    page_offset: Optional[int] = Query(
        None, description="印刷页码 → PDF 物理页的偏移;不传则自动推断"
    ),
    scan_pages: int = Query(10, ge=1, le=60, description="卷首扫描多少页找目录"),
    current_user: models.User = Depends(deps.get_current_active_user),
) -> Any:
    """上传 PDF 并识别目录。书签优先,其次是目录页文字层,最后才 OCR。"""
    filename = file.filename or ""
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="只支持 .pdf 文件")

    path = await _save_upload(file)
    try:
        data = await asyncio.to_thread(path.read_bytes)
        total = page_count(data)
        from app.services.ocr import DocumentOcrChain

        resolution = await resolve_toc(
            data,
            db=db,
            ocr_chain=DocumentOcrChain(),
            page_offset=page_offset,
            scan_pages=scan_pages,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"目录识别失败: {exc}") from exc

    return {
        "file_path": str(path),
        "filename": filename,
        "book_title": Path(filename).stem,
        "page_count": total,
        "toc_source": resolution.source,
        "page_offset": resolution.page_offset,
        "message": resolution.message,
        "chapters": [_chapter_payload(c) for c in build_chapter_ranges(resolution.entries, total)],
    }


# --------------------------------------------------------------------------- #
# ② 建任务 / 跑任务 / 重试
# --------------------------------------------------------------------------- #

@router.post("/tasks", response_model=dict)
async def create_chapter_import_task(
    *,
    db: deps.SessionDep,
    payload: CreateTaskRequest,
    current_user: models.User = Depends(deps.get_current_active_user),
) -> Any:
    """按校正后的章节方案建任务。此时不跑 OCR,等 /run 才真正开始。"""
    source_path = _resolve_source_path(payload.source_path)
    if not payload.chapters:
        raise HTTPException(status_code=400, detail="请至少选择一个章节")

    try:
        task = await _service().create_task(
            db,
            user_id=current_user.id,
            subject_id=payload.subject_id,
            source_path=str(source_path),
            book_title=payload.book_title,
            ranges=_to_chapter_ranges(payload.chapters),
            mode=payload.mode or "extract",
            book_id=payload.book_id,
            source_filename=source_path.name,
        )
    except ChapterImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _task_payload(task)


@router.get("/tasks", response_model=dict)
async def list_chapter_import_tasks(
    *,
    db: deps.SessionDep,
    page: int = 1,
    size: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    current_user: models.User = Depends(deps.get_current_active_user),
) -> Any:
    conditions = []
    if status:
        conditions.append(PdfChapterImportTask.status == status)
    if not current_user.is_superuser:
        conditions.append(PdfChapterImportTask.user_id == current_user.id)

    stmt = (
        select(PdfChapterImportTask)
        .options(selectinload(PdfChapterImportTask.book))
        .order_by(PdfChapterImportTask.created_at.desc())
    )
    if conditions:
        stmt = stmt.where(*conditions)

    count_stmt = select(func.count()).select_from(PdfChapterImportTask)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total = (await db.execute(count_stmt)).scalar_one()

    tasks = (
        await db.execute(stmt.offset((page - 1) * size).limit(size))
    ).scalars().unique().all()

    return {
        "items": [_task_payload(t, with_items=False) for t in tasks],
        "total": total,
        "page": page,
        "size": size,
    }


@router.get("/tasks/{task_id}", response_model=dict)
async def get_chapter_import_task(
    *,
    db: deps.SessionDep,
    task_id: int,
    current_user: models.User = Depends(deps.get_current_active_user),
) -> Any:
    task = await _get_task_or_404(db, task_id, current_user)
    return _task_payload(task)


@router.post("/tasks/{task_id}/run", response_model=dict)
async def run_chapter_import_task(
    *,
    db: deps.SessionDep,
    task_id: int,
    force: bool = Query(False, description="为 true 时连已完成的章节也重跑"),
    current_user: models.User = Depends(deps.get_current_active_user),
) -> Any:
    """执行导入。默认跳过已完成章节(断点续传),force=true 则全部重跑。"""
    task = await _get_task_or_404(db, task_id, current_user)
    if task.status == ChapterImportTaskStatus.PROCESSING:
        raise HTTPException(status_code=400, detail="任务正在执行中")
    try:
        task = await _service().run_task(db, task.id, only_pending=not force)
    except ChapterImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _task_payload(task)


@router.post("/tasks/{task_id}/cancel", response_model=dict)
async def cancel_chapter_import_task(
    *,
    db: deps.SessionDep,
    task_id: int,
    current_user: models.User = Depends(deps.get_current_active_user),
) -> Any:
    """把尚未跑的子任务标记为取消;已在跑的章节跑完即止。"""
    task = await _get_task_or_404(db, task_id, current_user)
    for item in task.items or []:
        if item.status in (ChapterImportItemStatus.PENDING, ChapterImportItemStatus.FAILED):
            item.status = ChapterImportItemStatus.CANCELLED
    task.status = ChapterImportTaskStatus.CANCELLED
    await db.commit()
    await db.refresh(task)
    return _task_payload(task)


@router.post("/items/{item_id}/retry", response_model=dict)
async def retry_chapter_import_item(
    *,
    db: deps.SessionDep,
    item_id: int,
    current_user: models.User = Depends(deps.get_current_active_user),
) -> Any:
    """重跑单个失败/取消的章节。"""
    item = await db.get(PdfChapterImportItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="子任务不存在")
    task = await _get_task_or_404(db, item.task_id, current_user)
    try:
        item = await _service().retry_item(db, item_id)
    except ChapterImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.refresh(task)
    return {"item": _item_payload(item), "task": _task_payload(task, with_items=False)}


@router.delete("/tasks/{task_id}", response_model=dict)
async def delete_chapter_import_task(
    *,
    db: deps.SessionDep,
    task_id: int,
    current_user: models.User = Depends(deps.get_current_active_user),
) -> Any:
    """删除任务记录。已入库的题目不受影响(题目的 import_task_id 指向独立记录)。"""
    task = await _get_task_or_404(db, task_id, current_user)
    await db.delete(task)
    await db.commit()
    return {"message": "任务已删除", "id": task_id}


async def _get_task_or_404(
    db: AsyncSession, task_id: int, current_user: models.User
) -> PdfChapterImportTask:
    stmt = (
        select(PdfChapterImportTask)
        .options(
            selectinload(PdfChapterImportTask.items),
            selectinload(PdfChapterImportTask.book),
        )
        # 见 PdfChapterImportService._load_task:不加这个,identity map 里的旧实例
        # 不会重新填充关系,序列化时会撞上 MissingGreenlet。
        .execution_options(populate_existing=True)
        .where(PdfChapterImportTask.id == task_id)
    )
    task = (await db.execute(stmt)).scalars().first()
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="没有权限访问该任务")
    return task


# --------------------------------------------------------------------------- #
# ③ 书籍 / 章节查询
# --------------------------------------------------------------------------- #

@router.get("/books", response_model=dict)
async def list_books(
    *,
    db: deps.SessionDep,
    subject_id: Optional[int] = None,
    current_user: models.User = Depends(deps.get_current_active_user),
) -> Any:
    stmt = select(SourceBook).order_by(SourceBook.created_at.desc())
    if subject_id is not None:
        stmt = stmt.where(SourceBook.subject_id == subject_id)
    books = (await db.execute(stmt)).scalars().all()
    return {
        "items": [
            {
                "id": b.id,
                "title": b.title,
                "subject_id": b.subject_id,
                "filename": b.filename,
                "page_count": b.page_count,
                "created_at": b.created_at,
            }
            for b in books
        ]
    }


@router.get("/books/{book_id}/chapters", response_model=dict)
async def list_book_chapters(
    *,
    db: deps.SessionDep,
    book_id: int,
    current_user: models.User = Depends(deps.get_current_active_user),
) -> Any:
    book = await db.get(SourceBook, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="书籍不存在")

    chapters = (
        await db.execute(
            select(BookChapter)
            .where(BookChapter.book_id == book_id)
            .order_by(BookChapter.order_index, BookChapter.id)
        )
    ).scalars().all()
    sections = (
        await db.execute(
            select(BookSection)
            .where(BookSection.book_id == book_id)
            .order_by(BookSection.order_index, BookSection.id)
        )
    ).scalars().all()

    by_chapter: dict[int, list[dict[str, Any]]] = {}
    for section in sections:
        by_chapter.setdefault(section.chapter_id, []).append(
            {
                "id": section.id,
                "title": section.title,
                "start_page": section.start_page,
                "end_page": section.end_page,
                "order_index": section.order_index,
            }
        )

    return {
        "book": {"id": book.id, "title": book.title, "page_count": book.page_count},
        "chapters": [
            {
                "id": c.id,
                "title": c.title,
                "start_page": c.start_page,
                "end_page": c.end_page,
                "order_index": c.order_index,
                "sections": by_chapter.get(c.id, []),
            }
            for c in chapters
        ],
    }
