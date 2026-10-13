"""PDF 章节批量导入的任务与子项。

拆成两张表是因为"断点续传"和"重复导入检测"都是**章节级**的语义:

    PdfChapterImportTask  一次提交(用户勾选了 5 个章节 → 1 个 task)
      └─ PdfChapterImportItem  每章一条,各自记状态 / 重试次数 / 内容指纹

把状态放在子项上,重跑时就只需要挑 status != completed 的继续跑,已经入库的
章节不会被重抽一遍——这是"断点续传"和"失败重试"能共用的同一套判据。

与既有 ``ImportTask`` 的关系:``ImportTask`` 是**单份文件**的导入记录,题目表
的 ``import_task_id`` 指向它。章节导入每题同样会建一条 ImportTask(保证题目溯源
链路不变),再由 Item 记住这个 id,这样删章节任务不会破坏题目溯源。
"""
import enum

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Enum as SQLEnum, Index
from sqlalchemy.orm import relationship
from datetime import datetime

from .base import Base


class ChapterImportTaskStatus(str, enum.Enum):
    PENDING = "pending"        # 已创建,未开始
    PROCESSING = "processing"  # 正在跑
    COMPLETED = "completed"    # 全部子项终态(成功 / 跳过)
    FAILED = "failed"          # 全部子项都失败
    PARTIAL = "partial"        # 部分成功:有完成也有失败
    CANCELLED = "cancelled"


class ChapterImportItemStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    # 重复导入检测命中:这本书的这一章已经导过,不再重抽。
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


def _enum_col(python_enum):
    """统一用 values_callable 存 value 而非 name,与项目既有模型保持一致。"""
    return SQLEnum(python_enum, values_callable=lambda x: [e.value for e in x])


class PdfChapterImportTask(Base):
    """一次"勾选若干章节 → 批量导入"的提交。"""

    __tablename__ = "pdf_chapter_import_tasks"

    id = Column(Integer, primary_key=True, index=True)
    book_id = Column(
        Integer, ForeignKey("source_books.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = Column(Integer, ForeignKey("user.id"), nullable=True, index=True)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=True)
    mode = Column(String(20), default="extract")  # extract / solve,与 ImportTask.mode 对齐

    # 原始 PDF 落盘路径。章节 PDF 是运行时从它切出来的,不单独持久化。
    source_path = Column(String(512), nullable=False)
    source_filename = Column(String(255), nullable=True)

    status = Column(
        _enum_col(ChapterImportTaskStatus),
        default=ChapterImportTaskStatus.PENDING,
        index=True,
    )
    error_message = Column(Text, nullable=True)

    # 进度计数器。冗余存一份是为了列表页不必 join 子项表就能显示进度。
    total_items = Column(Integer, nullable=False, default=0)
    completed_items = Column(Integer, nullable=False, default=0)
    failed_items = Column(Integer, nullable=False, default=0)
    skipped_items = Column(Integer, nullable=False, default=0)
    imported_questions = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    book = relationship("SourceBook")
    items = relationship(
        "PdfChapterImportItem",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="PdfChapterImportItem.order_index",
    )


class PdfChapterImportItem(Base):
    """一个章节的导入子任务。"""

    __tablename__ = "pdf_chapter_import_items"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(
        Integer,
        ForeignKey("pdf_chapter_import_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chapter_id = Column(Integer, ForeignKey("book_chapters.id"), nullable=True, index=True)
    section_id = Column(Integer, ForeignKey("book_sections.id"), nullable=True, index=True)

    # 冗余存标题与页码:章节行可能随后被用户改名,但子项要保留提交时的快照,
    # 否则"这一项当时导的是哪几页"就说不清了。
    title = Column(String(255), nullable=False)
    start_page = Column(Integer, nullable=False)
    end_page = Column(Integer, nullable=False)
    order_index = Column(Integer, nullable=False, default=0)

    status = Column(
        _enum_col(ChapterImportItemStatus),
        default=ChapterImportItemStatus.PENDING,
        index=True,
    )
    # 重试次数。0 表示还没跑过,>0 只在失败重试时递增。
    attempt_count = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)
    questions_count = Column(Integer, nullable=False, default=0)

    # 该章节切出的 PDF 字节的 SHA-256。重复导入检测的唯一判据。
    content_sha256 = Column(String(64), nullable=True, index=True)
    # 本子项实际落库的 ImportTask id(题目溯源用)。
    import_task_id = Column(Integer, ForeignKey("import_tasks.id"), nullable=True)

    # 断点续传缓存:OCR 已产出的 markdown 与元信息。重跑时若已有缓存且章节内容
    # 指纹一致,直接跳过 OCR,省掉整段 API 开销。
    ocr_markdown = Column(Text, nullable=True)
    ocr_meta = Column(Text, nullable=True)  # JSON 字符串

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    task = relationship("PdfChapterImportTask", back_populates="items")

    __table_args__ = (
        Index("ix_pdf_chapter_items_task_order", "task_id", "order_index"),
        # 同一本书同一章的重复检测:靠 (task, chapter/section) 定位,不建唯一约束,
        # 因为失败重试会 legitimate 地产生同一章的新子项。
        Index("ix_pdf_chapter_items_chapter", "chapter_id", "section_id"),
    )
