"""PDF 目录识别:书签优先,无书签再认目录页。

三级降级,每一级都比上一级贵,能不花钱就不花:

    ① PDF 书签(doc.get_toc)      —— 零成本、最准,书签页码本来就是物理页
    ② 文本层取"目录"页 + 正则     —— 零成本,但只适用于有文字层的 PDF
    ③ OCR 目录页 + 正则           —— 要调第一层/第二层模型,扫描件才走到这里

关于**印刷页码 vs 物理页码**:教科书目录上印的页码和 PDF 的物理页码常常对不上,
前面隔着封面、扉页、目录。书签不受影响(get_toc 给的就是物理页),②③ 解析出来
的是印刷页,需要加一个 offset。

offset 怎么来,按可信度排序:

    ① 目录页所在位置(最可靠):目录印在 PDF 第 k 页,正文必然在它后面,
       于是 offset ≈ k。这条有物理依据,不靠猜。
    ② 让最多条目落进 [1, total] 的 offset(兜底):只在印刷页码**超出** PDF
       页数时才分辨得出——若所有印刷页本来就在范围内,offset=0 与 offset=3
       的命中数相同,这条判据是失效的。它的作用是把明显离谱的页码拉回来。
    ③ 用户手工填:预览页始终可以覆盖,这也是最终兜底。

第②条的局限必须写在这里,免得后来者误以为它总能算出正确答案。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from .pdf_split import ChapterRange, open_pdf, page_count

logger = logging.getLogger(__name__)

# 文本层扫描时最多往前看多少页找"目录"字样。目录一般都在卷首。
DEFAULT_TOC_SCAN_PAGES = 10
# 文本层找不到目录页时,退而取前 N 页。
DEFAULT_FALLBACK_HEAD_PAGES = 6
# 印刷页码与物理页码的最大可能偏移。教科书前言+目录通常不超过 30 页。
MAX_OFFSET = 40

SOURCE_BOOKMARK = "bookmark"
SOURCE_TEXT = "text"
SOURCE_OCR = "ocr"
SOURCE_NONE = "none"

_CN_NUMERALS = "一二三四五六七八九十百千两"
# 目录行:编号(第X章 / 1.2.3)+ 标题 + 引导点 + 页码
_TOC_LINE_RE = re.compile(
    r"^\s*"
    r"(?:"
    r"(?P<cn>第\s*[" + _CN_NUMERALS + r"0-9]+\s*[章节篇讲编部])"
    r"|"
    r"(?P<num>\d{1,2}(?:[.\-]\d{1,2})*)"
    r")"
    r"[\s、.．:：、]*"
    r"(?P<title>.*?)"
    r"[\s.。·•・…─-]*"
    r"(?P<page>\d{1,4})"
    r"\s*$"
)
# 宽松模式:没有编号,但有一串引导点 + 行尾页码(如"前言 …… 1")。
_TOC_LOOSE_RE = re.compile(
    r"^\s*(?P<title>[^\d].*?)[\s.。·•・…]{2,}(?P<page>\d{1,4})\s*$"
)
# 找目录页用的关键词。
_TOC_MARKER_RE = re.compile(r"目\s*录|contents|table\s+of\s+contents", re.IGNORECASE)
# 明显不是目录标题的行,避免把页眉页脚、版权行当章节。
_NOISE_RE = re.compile(
    r"^(?:第?\s*\d+\s*页?$|[.\s·•…。\-—_]*$|copyright|版权|ISBN|责任编辑|封面设计|图书在版编目)",
    re.IGNORECASE,
)


@dataclass
class TocEntry:
    """一条目录条目。"""

    title: str
    # 修正后的 PDF 物理页(1-based)。落库与切分都用这个。
    page: int
    # 目录上印的页码,未加 offset。排障与前端展示"原始页码"时用。
    raw_page: int
    level: int = 1  # 1=章,2=节,3=小节
    source: str = SOURCE_NONE

    def __post_init__(self) -> None:
        self.title = (self.title or "").strip()


@dataclass
class TocResolution:
    """一次目录识别的结果。"""

    entries: list[TocEntry] = field(default_factory=list)
    source: str = SOURCE_NONE
    page_count: int = 0
    # 实际采用的页码偏移(印刷页 → 物理页)。前端可以带着它回显给用户。
    page_offset: int = 0
    # 各级尝试的说明,用于前端"为什么没识别出来"的提示。
    message: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.entries)


# --------------------------------------------------------------------------- #
# ① 书签
# --------------------------------------------------------------------------- #

def read_bookmarks(data: bytes) -> list[TocEntry]:
    """读 PDF 书签。get_toc() 返回 [level, title, page, ...],page 已是 1-based 物理页。"""
    try:
        with open_pdf(data) as doc:
            raw_toc = doc.get_toc()
    except Exception as exc:  # noqa: BLE001 - 损坏文件不该炸掉整条识别链
        logger.warning("读取 PDF 书签失败: %s", exc)
        return []

    entries: list[TocEntry] = []
    for item in raw_toc or []:
        level = int(item[0]) if item[0] else 1
        title = (item[1] or "").strip()
        page = int(item[2]) if len(item) > 2 else 0
        if not title or page <= 0:
            continue
        entries.append(
            TocEntry(title=title, page=page, raw_page=page, level=max(1, level), source=SOURCE_BOOKMARK)
        )
    return entries


# --------------------------------------------------------------------------- #
# ② / ③ 目录页文本 → 条目
# --------------------------------------------------------------------------- #

def _level_from_match(cn: Optional[str], num: Optional[str]) -> int:
    """从编号形式判断层级:"第三章"→1、"第三节"→2、"1.2"→2、"1.2.3"→3。"""
    if cn:
        return 2 if "节" in cn else 1
    if num:
        return num.count(".") + num.count("-") + 1
    return 1


def parse_toc_text(text: str, *, source: str) -> list[TocEntry]:
    """从一段文本里抽出目录行。先严格模式,一条都没有再试宽松模式。"""
    if not text:
        return []

    entries: list[TocEntry] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or len(line) > 200:
            continue
        if _NOISE_RE.match(line):
            continue

        match = _TOC_LINE_RE.match(line)
        if match:
            title = (match.group("title") or "").strip()
            # 标题里残留引导点是正常的(上方正则已吃掉尾部),这里再清一次行首行尾。
            title = title.strip(" .。·•・…─-")
            if not title:
                continue
            level = _level_from_match(match.group("cn"), match.group("num"))
            entries.append(
                TocEntry(
                    title=title,
                    page=int(match.group("page")),
                    raw_page=int(match.group("page")),
                    level=level,
                    source=source,
                )
            )

    if not entries:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or len(line) > 200 or _NOISE_RE.match(line):
                continue
            match = _TOC_LOOSE_RE.match(line)
            if not match:
                continue
            title = (match.group("title") or "").strip(" .。·•・…─-")
            if not title:
                continue
            entries.append(
                TocEntry(
                    title=title,
                    page=int(match.group("page")),
                    raw_page=int(match.group("page")),
                    level=1,
                    source=source,
                )
            )

    return entries


def _find_toc_page_indices(data: bytes, max_scan: int) -> tuple[list[int], bool]:
    """在卷首找含"目录"字样的页。

    返回 ``(页索引列表, 是否真的找到目录字样)``。第二个返回值很关键:没找到时
    退而取卷首几页,那种情况下"目录页位置"不能拿来推断页码偏移。
    """
    with open_pdf(data) as doc:
        total = min(doc.page_count, max_scan)
        for i in range(total):
            try:
                head = (doc[i].get_text("text") or "")[:400]
            except Exception:  # noqa: BLE001 - 单页取文本失败不影响其他页
                continue
            if _TOC_MARKER_RE.search(head):
                # 目录通常不止一页,多取两页把溢出部分也收进来。
                return [i, i + 1, i + 2], True
    return list(range(min(DEFAULT_FALLBACK_HEAD_PAGES, max_scan))), False


def extract_text_layer(data: bytes, page_indices: list[int]) -> str:
    """直接抽取 PDF 文字层(免费)。扫描件会返回几乎空的内容。"""
    chunks: list[str] = []
    with open_pdf(data) as doc:
        for i in page_indices:
            if i >= doc.page_count:
                continue
            try:
                chunks.append(doc[i].get_text("text") or "")
            except Exception:  # noqa: BLE001
                continue
    return "\n".join(chunks)


def infer_page_offset(
    entries: list[TocEntry], total_pages: int, *, toc_page_index: Optional[int] = None
) -> int:
    """推断"印刷页码 → 物理页码"的偏移。

    ``toc_page_index`` 是目录所在页的 0-based 索引。给了它就直接取 ``index + 1``
    ——目录印在这一页,正文只能从它后面开始,这是有物理依据的推断。

    没有目录页位置时退而求其次:挑一个让最多条目落在 [1, total_pages] 内的
    offset。注意这条判据**只在印刷页码超出 PDF 范围时才有分辨力**;所有印刷页
    本来就在范围内时,各个 offset 命中数相同,会平局取 0。
    """
    if not entries or total_pages <= 0:
        return 0

    if toc_page_index is not None:
        return max(0, int(toc_page_index) + 1)

    best_offset, best_hits = 0, -1
    for offset in range(0, MAX_OFFSET + 1):
        hits = sum(1 for e in entries if 1 <= e.raw_page + offset <= total_pages)
        if hits > best_hits:
            best_offset, best_hits = offset, hits
    return best_offset


def _apply_offset(entries: list[TocEntry], offset: int, total_pages: int) -> list[TocEntry]:
    """套用 offset 并丢弃仍然越界的条目;同时要求页码非递减(目录是有序的)。"""
    kept: list[TocEntry] = []
    last_page = 0
    for entry in entries:
        page = entry.raw_page + offset
        if page < 1 or (total_pages and page > total_pages):
            continue
        if page < last_page:
            logger.debug("丢弃页码回退的目录条目: %s(%s)", entry.title, entry.raw_page)
            continue
        entry.page = page
        last_page = page
        kept.append(entry)
    return kept


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #

async def resolve_toc(
    data: bytes,
    *,
    db=None,
    ocr_chain=None,
    page_offset: Optional[int] = None,
    scan_pages: int = DEFAULT_TOC_SCAN_PAGES,
) -> TocResolution:
    """识别目录。书签 → 文本层 → OCR,逐级降级。

    ``page_offset`` 为 None 时自动推断;给了具体值(含 0)就以用户给的为准
    —— 前端预览页让用户改过之后必须能锁住这个值。
    """
    total = page_count(data)
    resolution = TocResolution(page_count=total)

    # ① 书签
    bookmarks = read_bookmarks(data)
    if bookmarks:
        resolution.entries = bookmarks
        resolution.source = SOURCE_BOOKMARK
        resolution.page_offset = 0
        logger.info("目录来自 PDF 书签,共 %d 条", len(bookmarks))
        return resolution

    # ② 文本层。不设"字符数够不够"的门槛——直接解析,解析不出再谈 OCR。
    #    早先的版本要求文本层至少 200 字才肯解析,结果只有四五行的短目录被误判成
    #    扫描件,白白去烧 OCR 的钱。
    indices, toc_marker_found = _find_toc_page_indices(data, scan_pages)
    text = extract_text_layer(data, indices)
    entries = parse_toc_text(text, source=SOURCE_TEXT)
    source = SOURCE_TEXT if entries else SOURCE_NONE

    # ③ OCR 兜底:扫描件(文本层为空)或文本层解析不出条目时
    if not entries and db is not None and ocr_chain is not None:
        try:
            from .pdf_split import slice_pages

            head_pages = indices or list(range(min(DEFAULT_FALLBACK_HEAD_PAGES, total)))
            if head_pages:
                toc_pdf = slice_pages(data, head_pages[0] + 1, head_pages[-1] + 1)
                result = await ocr_chain.parse(
                    document=toc_pdf, filename="toc.pdf", db=db, page_images=None
                )
                entries = parse_toc_text(result.markdown, source=SOURCE_OCR)
                source = SOURCE_OCR if entries else SOURCE_NONE
                if not entries and result.primary_error:
                    resolution.message = f"目录页 OCR 失败: {result.primary_error}"
        except Exception as exc:  # noqa: BLE001 - OCR 失败不该让整个上传 500
            logger.warning("目录页 OCR 失败: %s", exc)
            resolution.message = f"目录页 OCR 失败: {exc}"

    if not entries:
        resolution.source = SOURCE_NONE
        if not resolution.message:
            resolution.message = (
                "未能自动识别目录:PDF 没有书签,目录页也没解析出条目。"
                "可在下方手工添加章节,或调整扫描页数后重试。"
            )
        return resolution

    offset = (
        page_offset
        if page_offset is not None
        else infer_page_offset(
            entries,
            total,
            # 只有真的找到"目录"字样时,目录页位置才是有依据的偏移来源。
            toc_page_index=indices[0] if (toc_marker_found and indices) else None,
        )
    )
    kept = _apply_offset(entries, offset, total)
    if not kept:
        resolution.source = SOURCE_NONE
        resolution.message = "目录条目页码全部超出 PDF 范围,请手工指定起始页码。"
        return resolution

    resolution.entries = kept
    resolution.source = source
    resolution.page_offset = offset
    if not resolution.message:
        resolution.message = f"目录来源:{_source_label(source)}(识别 {len(kept)} 条,页码偏移 {offset})"
    return resolution


def _source_label(source: str) -> str:
    return {
        SOURCE_BOOKMARK: "PDF 书签",
        SOURCE_TEXT: "目录页文字层",
        SOURCE_OCR: "目录页 OCR",
        SOURCE_NONE: "未识别",
    }.get(source, source)


# --------------------------------------------------------------------------- #
# 条目 → 章节区间
# --------------------------------------------------------------------------- #

def build_chapter_ranges(entries: list[TocEntry], total_pages: int) -> list[ChapterRange]:
    """把扁平目录条目整理成"章 → 小节"两级区间。

    - 章的区间 = 本章起始页 → 下一章起始页 - 1,最后一章到 total_pages。
    - 小节同理,且被下一章的起始页截断(不会串到下一章去)。
    - 目录只有一级时(全是 level 1),每章产出一个整章区间,不再拆小节。
    """
    if not entries:
        return []

    # 只有一级条目时把它当章;否则 level 1 是章、更深的挂到最近的章下面。
    has_chapter_level = any(e.level == 1 for e in entries)
    chapters: list[ChapterRange] = []
    current: Optional[ChapterRange] = None

    for entry in entries:
        if not has_chapter_level or entry.level == 1:
            current = ChapterRange(
                title=entry.title, start_page=entry.page, end_page=entry.page, level=1
            )
            chapters.append(current)
        elif current is not None:
            current.children.append(
                ChapterRange(
                    title=entry.title, start_page=entry.page, end_page=entry.page, level=entry.level
                )
            )
        else:
            # 目录第一条就是小节(没有章):补一个同名章把它们兜住。
            current = ChapterRange(title=entry.title, start_page=entry.page, end_page=entry.page)
            chapters.append(current)
            current.children.append(
                ChapterRange(title=entry.title, start_page=entry.page, end_page=entry.page, level=entry.level)
            )

    # 用"下一条的起始页 - 1"闭合区间。
    for idx, chapter in enumerate(chapters):
        next_start = chapters[idx + 1].start_page - 1 if idx + 1 < len(chapters) else total_pages
        chapter.end_page = max(chapter.start_page, next_start)

        for jdx, section in enumerate(chapter.children):
            if jdx + 1 < len(chapter.children):
                section.end_page = max(section.start_page, chapter.children[jdx + 1].start_page - 1)
            else:
                # 本章最后一个小节延伸到本章末尾,但不越过下一章。
                section.end_page = max(section.start_page, chapter.end_page)

    # 丢掉零宽或重复的区间(目录里两条指向同一页时会出现)。
    cleaned: list[ChapterRange] = []
    for chapter in chapters:
        if chapter.start_page > total_pages:
            continue
        chapter.children = [
            s for s in chapter.children if s.start_page <= total_pages and s.start_page <= s.end_page
        ]
        cleaned.append(chapter)
    return cleaned
