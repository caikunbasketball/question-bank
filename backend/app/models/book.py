"""书籍 / 章节 / 小节:PDF 按章节拆分导入的归属骨架。

设计要点:

- **章节与知识点正交,不互相替代**。``KnowledgePoint`` 是学科内的能力树(「导数的
  几何意义」),书的目录是物理结构(「第三章 导数」)。一道题同时属于某一章、
  又能挂多个知识点,两套体系各自独立管理。
- **题目上三个外键全记**。book_id / chapter_id / section_id 一次写全,是为了
  既能按书统计、也能按章筛选题,不必每次都 join 回父级再过滤。冗余换来的是
  查询直面一层。
- **页码 1-based 闭区间**。PyMuPDF 用 0-based 半开区间,转换只在拆分边界处发生,
  不让这套约定泄漏进数据库。
"""
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship
from datetime import datetime

from .base import Base


class SourceBook(Base):
    """一本书:一次 PDF 章节导入的载体。"""

    __tablename__ = "source_books"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=True, index=True)
    filename = Column(String(255), nullable=True)
    # 整份 PDF 的 SHA-256。仅用于"这本书已经导过"的提示,不做近似查重。
    content_sha256 = Column(String(64), nullable=True, index=True)
    page_count = Column(Integer, nullable=True)
    created_by = Column(Integer, ForeignKey("user.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    chapters = relationship(
        "BookChapter", back_populates="book", cascade="all, delete-orphan"
    )
    sections = relationship(
        "BookSection", back_populates="book", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_source_books_subject_title", "subject_id", "title"),)


class BookChapter(Base):
    """章:目录的一级条目。"""

    __tablename__ = "book_chapters"

    id = Column(Integer, primary_key=True, index=True)
    book_id = Column(
        Integer, ForeignKey("source_books.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = Column(String(255), nullable=False)
    order_index = Column(Integer, nullable=False, default=0)
    start_page = Column(Integer, nullable=False)
    end_page = Column(Integer, nullable=False)

    book = relationship("SourceBook", back_populates="chapters")
    sections = relationship(
        "BookSection", back_populates="chapter", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_book_chapters_book_order", "book_id", "order_index"),)


class BookSection(Base):
    """小节:章下面的二级条目。

    即使某章没有小节,也会记一条与章同页码区间的"整章"小节,让题目归属
    永远有最细的一层可挂(见 pdf_split 的 section 折叠逻辑)。
    """

    __tablename__ = "book_sections"

    id = Column(Integer, primary_key=True, index=True)
    book_id = Column(
        Integer, ForeignKey("source_books.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_id = Column(
        Integer, ForeignKey("book_chapters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = Column(String(255), nullable=False)
    order_index = Column(Integer, nullable=False, default=0)
    start_page = Column(Integer, nullable=False)
    end_page = Column(Integer, nullable=False)

    book = relationship("SourceBook", back_populates="sections")
    chapter = relationship("BookChapter", back_populates="sections")

    __table_args__ = (Index("ix_book_sections_chapter_order", "chapter_id", "order_index"),)
