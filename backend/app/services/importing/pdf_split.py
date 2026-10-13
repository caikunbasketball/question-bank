"""按章节切分 PDF。

只有两个动作,但边界必须精确:

- **页码一律 1-based 闭区间**。PyMuPDF 内部是 0-based 闭区间,转换只发生在本文件的
  四个函数里,外面的世界(数据库、API、前端)只说"第 3 页到第 12 页"。
- **用户会手工改页码**。目录解析出的区间只是初稿,前端允许改章节名和起止页,
  所以 `normalize_range` 必须能容忍越界、倒置、单页这三种脏输入,而不是抛异常。
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Optional

from .contracts import PDF_RASTER_DPI

logger = logging.getLogger(__name__)


class PdfSplitError(ValueError):
    """切分失败。message 会原样出现在用户可见的失败原因里,不要塞堆栈。"""


def open_pdf(data: bytes):
    """打开 PDF,返回可 `with` 的 Document。损坏文件转成中文 PdfSplitError。"""
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - 依赖缺失属环境问题
        raise PdfSplitError(
            "缺少 PyMuPDF 依赖,PDF 拆分不可用。请在后端目录执行 uv sync 后重试"
        ) from exc
    try:
        return pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - 损坏文件要转成用户可读的中文原因
        raise PdfSplitError(f"PDF 无法打开或已损坏: {exc}") from exc


def page_count(data: bytes) -> int:
    """返回总页数。"""
    with open_pdf(data) as doc:
        return doc.page_count


def normalize_range(start_page: int, end_page: int, total_pages: int) -> tuple[int, int]:
    """把可能是脏数据的页码区间收敛到合法闭区间。

    处理三种用户输入:越界(截断到 1..total)、倒置(start > end 时交换)、
    单页(start == end 合法,即只导一页)。无法收敛时抛 PdfSplitError。
    """
    try:
        start = int(start_page)
        end = int(end_page)
    except (TypeError, ValueError) as exc:
        raise PdfSplitError(f"页码必须是整数: {start_page} ~ {end_page}") from exc

    if start > end:
        start, end = end, start

    start = max(1, start)
    if total_pages > 0:
        end = min(end, total_pages)
        start = min(start, total_pages)

    if end < start:
        raise PdfSplitError(f"页码区间非法: {start_page} ~ {end_page}(共 {total_pages} 页)")
    return start, end


def slice_pages(data: bytes, start_page: int, end_page: int) -> bytes:
    """切出 [start_page, end_page](1-based 闭区间),返回一份新的 PDF 字节。

    新文件只含这几页,交给第一层 OCR 时不会因为"整本书太大"被拒。
    """
    total = page_count(data)
    start, end = normalize_range(start_page, end_page, total)
    import pymupdf

    with open_pdf(data) as src, pymupdf.open() as dst:
        dst.insert_pdf(src, from_page=start - 1, to_page=end - 1)
        return dst.tobytes()


def rasterize(data: bytes, *, dpi: int = PDF_RASTER_DPI) -> list[bytes]:
    """逐页栅格化为 PNG 字节,供第二层视觉兜底使用。"""
    pages: list[bytes] = []
    with open_pdf(data) as doc:
        if doc.page_count == 0:
            raise PdfSplitError("PDF 不含任何页面")
        for page in doc:
            pages.append(page.get_pixmap(dpi=dpi).tobytes("png"))
    return pages


def sha256(data: bytes) -> str:
    """原始文件字节的 SHA-256。用于整份 PDF(同一个文件上传多次字节不变)。"""
    return hashlib.sha256(data).hexdigest()


def content_fingerprint(data: bytes) -> str:
    """页面内容指纹:**不能**直接用切片字节做 sha。

    同一个区间切两次,产出的 PDF 字节是不一样的——对象编号、xref 顺序、写入时
    间戳都会变,跟页面内容无关。拿那种字节做重复检测,同一章导一百次也检测不出
    重复。所以这里按"文字层 + 嵌入图片"逐页算:

    - 文字层覆盖有文字的 PDF(教科书正文、目录);
    - 图片字节覆盖扫描件(整页就是一张图,文字层为空)。

    两者相加足以区分"内容变了"和"只是重新打包了一次"。
    """
    digest = hashlib.sha256()
    with open_pdf(data) as doc:
        for page in doc:
            digest.update((page.get_text("text") or "").encode("utf-8", "replace"))
            digest.update(b"\x00")
            try:
                for image in page.get_images(full=True):
                    extracted = doc.extract_image(image[0])
                    digest.update(hashlib.sha256(extracted.get("image") or b"").digest())
            except Exception:  # noqa: BLE001 - 单页图片损坏不该让指纹算不出来
                pass
            digest.update(b"\x01")
    return digest.hexdigest()


@dataclass
class ChapterRange:
    """一个待导入的章节区间。

    ``children`` 是该章下的小节;为空表示"整章导一次",不按小节再切。
    """

    title: str
    start_page: int
    end_page: int
    level: int = 1
    children: list["ChapterRange"] = field(default_factory=list)

    @property
    def page_span(self) -> int:
        return max(0, self.end_page - self.start_page + 1)


@dataclass
class FlatRange:
    """展开后的一条待导入区间。

    保留 ``chapter_index`` / ``section_index`` 而不是直接塞对象引用,是为了让调用方
    能按同一个下标去取自己建好的 ORM 行——建表和展开用的是同一批 range,靠位置
    对齐比靠对象身份可靠。``section_index`` 为 None 表示"整章导一次"。
    """

    title: str
    start_page: int
    end_page: int
    chapter_index: int
    section_index: Optional[int] = None


def flatten_ranges(ranges: list[ChapterRange], total_pages: int) -> list[FlatRange]:
    """把"章 → 小节"两级展开成一维待导入清单。

    章有小节时只导出小节(小节区间并起来就是整章,不会漏页);章没有小节时导出
    整章。所有区间在这里统一做一次 normalize,后续落库与切分拿到的都是干净页码。
    """
    flat: list[FlatRange] = []
    for chapter_index, chapter in enumerate(ranges):
        if chapter.children:
            for section_index, section in enumerate(chapter.children):
                start, end = normalize_range(section.start_page, section.end_page, total_pages)
                flat.append(
                    FlatRange(
                        title=section.title,
                        start_page=start,
                        end_page=end,
                        chapter_index=chapter_index,
                        section_index=section_index,
                    )
                )
        else:
            start, end = normalize_range(chapter.start_page, chapter.end_page, total_pages)
            flat.append(
                FlatRange(
                    title=chapter.title,
                    start_page=start,
                    end_page=end,
                    chapter_index=chapter_index,
                )
            )
    return flat
