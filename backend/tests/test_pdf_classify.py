"""AI 分类归属的校验闸门。

需求里最容易做坏也最要命的一条是"不得编造分类 ID"——模型幻觉出来的 ID 会静默地
污染按章节筛选、按知识点统计的每一个结果。所以这组测试的重心不是"模型能不能答对",
而是**模型答错了会不会被拦下来**。
"""
import pytest

from app.services.importing.classify import (
    CategoryOption,
    ClassificationCandidates,
    QuestionClassification,
    build_prompt,
    load_candidates,
    _validate,
)


def _candidates() -> ClassificationCandidates:
    return ClassificationCandidates(
        chapters=[CategoryOption(id=1, title="第一章 集合"), CategoryOption(id=2, title="第二章 函数")],
        sections=[
            CategoryOption(id=10, title="1.1 集合的概念", parent_id=1),
            CategoryOption(id=20, title="2.1 函数的单调性", parent_id=2),
        ],
    )


# --------------------------------------------------------------------------- #
# ID 校验
# --------------------------------------------------------------------------- #

def test_valid_classification_passes_through():
    result = _validate(
        {
            "q_type": "single_choice",
            "chapter_id": 1,
            "section_id": 10,
            "knowledge_point_ids": [101, 102],
            "confidence": 0.9,
        },
        _candidates(),
        {101, 102, 103},
    )
    assert result.q_type == "single_choice"
    assert result.chapter_id == 1
    assert result.section_id == 10
    assert result.knowledge_point_ids == [101, 102]
    assert result.needs_review is False


def test_fabricated_chapter_id_is_dropped():
    """模型编了个不存在的章节 ID —— 必须丢掉,而不是照单全收。"""
    result = _validate(
        {"q_type": "single_choice", "chapter_id": 999, "confidence": 0.95},
        _candidates(),
        set(),
    )
    assert result.chapter_id is None
    assert result.needs_review is True
    assert "999" in (result.review_reason or "")


def test_fabricated_knowledge_point_ids_are_dropped():
    result = _validate(
        {
            "q_type": "single_choice",
            "chapter_id": 1,
            "knowledge_point_ids": [101, 99999],
            "confidence": 0.9,
        },
        _candidates(),
        {101},
    )
    assert result.knowledge_point_ids == [101]
    assert result.needs_review is True


def test_section_not_belonging_to_chapter_is_dropped():
    """小节 20 属于第 2 章,却配了第 1 章 —— 章节归属自相矛盾,丢小节。"""
    result = _validate(
        {"q_type": "free_response", "chapter_id": 1, "section_id": 20, "confidence": 0.9},
        _candidates(),
        set(),
    )
    assert result.chapter_id == 1
    assert result.section_id is None
    assert result.needs_review is True


def test_section_alone_infers_its_chapter():
    """只给了小节也能反推出章节,不必要求模型两个都填。"""
    result = _validate(
        {"q_type": "free_response", "section_id": 20, "confidence": 0.9},
        _candidates(),
        set(),
    )
    assert result.section_id == 20
    assert result.chapter_id == 2


def test_illegal_q_type_is_dropped():
    result = _validate({"q_type": "nonsense_type", "chapter_id": 1, "confidence": 0.9}, _candidates(), set())
    assert result.q_type is None
    assert result.needs_review is True


def test_low_confidence_marks_review():
    result = _validate(
        {"q_type": "single_choice", "chapter_id": 1, "confidence": 0.2},
        _candidates(),
        set(),
    )
    assert result.chapter_id == 1  # 归属本身没错,仍然保留
    assert result.needs_review is True
    assert "置信度" in (result.review_reason or "")


def test_missing_chapter_marks_review_when_book_has_chapters():
    result = _validate({"q_type": "single_choice", "confidence": 0.9}, _candidates(), set())
    assert result.chapter_id is None
    assert result.needs_review is True


def test_knowledge_points_capped_at_five():
    result = _validate(
        {
            "q_type": "single_choice",
            "chapter_id": 1,
            "knowledge_point_ids": [1, 2, 3, 4, 5, 6, 7],
            "confidence": 0.9,
        },
        _candidates(),
        {1, 2, 3, 4, 5, 6, 7},
    )
    assert len(result.knowledge_point_ids) == 5


def test_duplicate_knowledge_points_collapse():
    result = _validate(
        {"q_type": "single_choice", "chapter_id": 1, "knowledge_point_ids": [7, 7, 8], "confidence": 0.9},
        _candidates(),
        {7, 8},
    )
    assert result.knowledge_point_ids == [7, 8]


def test_garbage_values_do_not_crash():
    """模型返回 null / 字符串 / 布尔值时必须能兜住,不能抛异常。"""
    result = _validate(
        {"q_type": None, "chapter_id": "abc", "section_id": True, "knowledge_point_ids": "x", "confidence": "high"},
        _candidates(),
        set(),
    )
    assert result.chapter_id is None
    assert result.section_id is None
    assert result.knowledge_point_ids == []
    assert result.confidence == 0.0
    assert result.needs_review is True


# --------------------------------------------------------------------------- #
# prompt
# --------------------------------------------------------------------------- #

def test_build_prompt_includes_only_existing_ids():
    prompt = build_prompt(
        [{"content": "已知集合 A={1,2},求 A 的子集个数。", "options": ["2", "4"]}],
        _candidates(),
        [[CategoryOption(id=101, title="子集")]],
    )
    assert "101" in prompt
    assert "第一章 集合" in prompt
    assert "single_choice" in prompt
    # 关键约束必须写进 prompt:不允许编造 ID。
    assert "禁止输出清单中不存在的 ID" in prompt


def test_build_prompt_handles_missing_sections():
    prompt = build_prompt([{"content": "题干"}], ClassificationCandidates(), [[]])
    assert "(无)" in prompt


# --------------------------------------------------------------------------- #
# 候选集加载
# --------------------------------------------------------------------------- #

async def test_load_candidates_scoped_to_book(db_session):
    from app.models.book import BookChapter, BookSection, SourceBook

    book = SourceBook(title="教材A", subject_id=None)
    db_session.add(book)
    await db_session.flush()
    other = SourceBook(title="教材B", subject_id=None)
    db_session.add(other)
    await db_session.flush()

    chapter = BookChapter(book_id=book.id, title="第一章", order_index=0, start_page=1, end_page=5)
    db_session.add(chapter)
    await db_session.flush()
    db_session.add(
        BookSection(book_id=book.id, chapter_id=chapter.id, title="1.1", order_index=0, start_page=1, end_page=2)
    )
    # 别的书的内容不该出现在候选集里
    foreign = BookChapter(book_id=other.id, title="别的章", order_index=0, start_page=1, end_page=5)
    db_session.add(foreign)
    await db_session.commit()

    candidates = await load_candidates(db_session, book_id=book.id)

    assert [c.id for c in candidates.chapters] == [chapter.id]
    assert [s.id for s in candidates.sections][0] != foreign.id
    assert candidates.sections[0].parent_id == chapter.id


async def test_load_candidates_can_scope_to_one_chapter(db_session):
    from app.models.book import BookChapter, BookSection, SourceBook

    book = SourceBook(title="教材C", subject_id=None)
    db_session.add(book)
    await db_session.flush()
    c1 = BookChapter(book_id=book.id, title="第一章", order_index=0, start_page=1, end_page=5)
    c2 = BookChapter(book_id=book.id, title="第二章", order_index=1, start_page=6, end_page=9)
    db_session.add_all([c1, c2])
    await db_session.flush()
    db_session.add_all(
        [
            BookSection(book_id=book.id, chapter_id=c1.id, title="1.1", order_index=0, start_page=1, end_page=2),
            BookSection(book_id=book.id, chapter_id=c2.id, title="2.1", order_index=0, start_page=6, end_page=7),
        ]
    )
    await db_session.commit()

    candidates = await load_candidates(db_session, book_id=book.id, chapter_id=c2.id)

    # 章节仍然全给(模型需要看到上下文),但小节只给目标章的。
    assert len(candidates.chapters) == 2
    assert [s.title for s in candidates.sections] == ["2.1"]
