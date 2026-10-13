"""PDF 目录识别与按章节拆分的单元测试。

这两块是纯计算(除 PyMuPDF 外无外部依赖),所以全部用真 PDF 跑,不 mock——
目录正则和页码区间换算是最容易在真实文件上翻车的地方,mock 掉就白测了。
"""
import pymupdf
import pytest

from app.services.importing.pdf_split import (
    ChapterRange,
    PdfSplitError,
    flatten_ranges,
    normalize_range,
    page_count,
    rasterize,
    sha256,
    slice_pages,
)
from app.services.importing.pdf_toc import (
    build_chapter_ranges,
    infer_page_offset,
    parse_toc_text,
    read_bookmarks,
    TocEntry,
)


def _make_pdf(pages: int = 12, *, toc_lines=None) -> bytes:
    """造一份 PDF。给了 toc_lines 就把它们写到第 1 页(模拟目录页)。"""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        if i == 0 and toc_lines:
            for j, line in enumerate(toc_lines):
                page.insert_text((72, 100 + j * 24), line)
        else:
            page.insert_text((72, 100), f"body {i}")
    data = doc.tobytes()
    doc.close()
    return data


TOC_LINES = [
    "目  录",
    "第一章  集合与函数 ................. 1",
    "  1.1  集合的概念 .................. 2",
    "  1.2  函数的单调性 ................ 4",
    "第二章  三角函数 ................... 7",
    "  2.1  任意角与弧度制 .............. 8",
    "第三章  数列 ...................... 10",
]


# --------------------------------------------------------------------------- #
# 目录文本 → 条目
# --------------------------------------------------------------------------- #

def test_parse_toc_text_recognises_chapter_and_section():
    entries = parse_toc_text("\n".join(TOC_LINES), source="text")

    titles = [(e.title, e.raw_page, e.level) for e in entries]
    assert titles == [
        ("集合与函数", 1, 1),
        ("集合的概念", 2, 2),
        ("函数的单调性", 4, 2),
        ("三角函数", 7, 1),
        ("任意角与弧度制", 8, 2),
        ("数列", 10, 1),
    ]


def test_parse_toc_text_ignores_noise_lines():
    text = "\n".join(
        [
            "目  录",
            "责任编辑 张三",
            "ISBN 978-7-04-000000-1",
            "第一章  集合 .............. 3",
        ]
    )
    entries = parse_toc_text(text, source="text")
    assert [e.title for e in entries] == ["集合"]


def test_parse_toc_text_falls_back_to_loose_pattern():
    """没有编号但有一串引导点的条目(如"前言 …… 1")也要能收进来。"""
    entries = parse_toc_text("前  言 ……………………… 2", source="text")
    assert [(e.title, e.raw_page) for e in entries] == [("前  言", 2)]


def test_parse_toc_text_empty_input():
    assert parse_toc_text("", source="text") == []


# --------------------------------------------------------------------------- #
# 页码偏移推断
# --------------------------------------------------------------------------- #

def test_infer_page_offset_prefers_toc_page_position():
    """目录印在 PDF 第 3 页(索引 2)→ 正文只能从它后面开始,偏移取 3。

    这条是有物理依据的推断,优先级高于下面"命中最多"的兜底。
    """
    entries = [TocEntry(title="A", page=0, raw_page=1)]
    assert infer_page_offset(entries, 20, toc_page_index=2) == 3


def test_infer_page_offset_hits_fallback_when_printed_pages_overflow():
    """没有目录页位置时,只有印刷页码超出 PDF 范围这条判据能起作用。

    printed=13/14 在 12 页的 PDF 里放不下,任何非负 offset 都救不回来 → 取 0。
    这条测试的价值在于记录这条判据的**边界**:印刷页全在范围内时它分辨不出
    真实偏移(见下一条)。
    """
    entries = [TocEntry(title=f"c{p}", page=0, raw_page=p) for p in (13, 14)]
    assert infer_page_offset(entries, 12) == 0


def test_infer_page_offset_cannot_disambiguate_when_all_pages_fit():
    """记录已知局限:印刷页全落在范围内时,命中数判据会平局取 0。

    这不是 bug,是这条启发式的能力边界——真要拿准偏移,得靠目录页位置或用户手填。
    """
    entries = [TocEntry(title=f"c{p}", page=0, raw_page=p) for p in (1, 4, 7)]
    # offset=0 与 offset=3 都让三条全落在 [1,12] 内,平局取最小的那个。
    assert infer_page_offset(entries, 12) == 0


def test_infer_page_offset_no_entries():
    assert infer_page_offset([], 10) == 0


# --------------------------------------------------------------------------- #
# 条目 → 区间
# --------------------------------------------------------------------------- #

def test_build_chapter_ranges_closes_at_next_chapter():
    entries = [
        TocEntry(title="集合与函数", page=1, raw_page=1, level=1),
        TocEntry(title="集合的概念", page=2, raw_page=2, level=2),
        TocEntry(title="函数的单调性", page=4, raw_page=4, level=2),
        TocEntry(title="三角函数", page=7, raw_page=7, level=1),
        TocEntry(title="任意角与弧度制", page=8, raw_page=8, level=2),
        TocEntry(title="数列", page=10, raw_page=10, level=1),
    ]
    ranges = build_chapter_ranges(entries, 12)

    assert [(c.title, c.start_page, c.end_page) for c in ranges] == [
        ("集合与函数", 1, 6),
        ("三角函数", 7, 9),
        ("数列", 10, 12),
    ]
    # 小节不会串到下一章去。
    assert [(s.title, s.start_page, s.end_page) for s in ranges[0].children] == [
        ("集合的概念", 2, 3),
        ("函数的单调性", 4, 6),
    ]
    assert ranges[2].children == []


def test_build_chapter_ranges_treats_flat_toc_as_chapters():
    """目录只有一级时,每条都是章,不凭空造小节。"""
    entries = [
        TocEntry(title="A", page=1, raw_page=1, level=2),
        TocEntry(title="B", page=5, raw_page=5, level=2),
    ]
    ranges = build_chapter_ranges(entries, 9)
    assert [(c.title, c.start_page, c.end_page) for c in ranges] == [("A", 1, 4), ("B", 5, 9)]


def test_build_chapter_ranges_empty():
    assert build_chapter_ranges([], 10) == []


# --------------------------------------------------------------------------- #
# 拆分
# --------------------------------------------------------------------------- #

def test_page_count_and_slice():
    data = _make_pdf(12)
    assert page_count(data) == 12

    sub = slice_pages(data, 7, 9)
    assert page_count(sub) == 3


def test_slice_pages_is_1_based_closed_interval():
    """第 1 页到第 1 页应当只有一页——off-by-one 会在这里露馅。"""
    data = _make_pdf(5)
    assert page_count(slice_pages(data, 1, 1)) == 1


def test_rasterize_returns_png_per_page():
    data = _make_pdf(3)
    pages = rasterize(data, dpi=72)
    assert len(pages) == 3
    assert all(p.startswith(b"\x89PNG") for p in pages)


def test_sha256_is_stable_and_distinct():
    data = _make_pdf(4)
    assert sha256(data) == sha256(data)
    assert sha256(data) != sha256(_make_pdf(5))


def test_normalize_range_clamps_and_swaps():
    assert normalize_range(0, 5, 10) == (1, 5)       # 下界越界
    assert normalize_range(8, 99, 10) == (8, 10)     # 上界越界
    assert normalize_range(9, 3, 10) == (3, 9)       # 倒置 → 交换
    assert normalize_range(4, 4, 10) == (4, 4)       # 单页合法


def test_normalize_range_rejects_garbage():
    with pytest.raises(PdfSplitError):
        normalize_range("abc", 3, 10)  # type: ignore[arg-type]


def test_slice_pages_rejects_corrupt_pdf():
    with pytest.raises(PdfSplitError):
        page_count(b"not a pdf at all")


# --------------------------------------------------------------------------- #
# 章 → 小节展开
# --------------------------------------------------------------------------- #

def test_flatten_ranges_expands_sections():
    ranges = [
        ChapterRange(
            title="第一章",
            start_page=1,
            end_page=6,
            children=[
                ChapterRange(title="1.1", start_page=2, end_page=3, level=2),
                ChapterRange(title="1.2", start_page=4, end_page=6, level=2),
            ],
        ),
        ChapterRange(title="第二章", start_page=7, end_page=9),
    ]
    flat = flatten_ranges(ranges, 9)

    assert [(f.title, f.start_page, f.end_page, f.chapter_index, f.section_index) for f in flat] == [
        ("1.1", 2, 3, 0, 0),
        ("1.2", 4, 6, 0, 1),
        ("第二章", 7, 9, 1, None),
    ]


def test_flatten_ranges_normalizes_dirty_pages():
    ranges = [ChapterRange(title="整本", start_page=1, end_page=999)]
    flat = flatten_ranges(ranges, 20)
    assert (flat[0].start_page, flat[0].end_page) == (1, 20)


def test_bookmarks_are_read_when_present():
    """PDF 自带书签时 get_toc 应能读出条目。"""
    doc = pymupdf.open()
    for i in range(6):
        doc.new_page(width=595, height=842)
    doc.set_toc([[1, "第一章 集合", 1], [2, "1.1 概念", 2], [1, "第二章 函数", 4]])
    data = doc.tobytes()
    doc.close()

    entries = read_bookmarks(data)
    assert [(e.title, e.page, e.level) for e in entries] == [
        ("第一章 集合", 1, 1),
        ("1.1 概念", 2, 2),
        ("第二章 函数", 4, 1),
    ]
