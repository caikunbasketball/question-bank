"""PDF 章节批量导入:服务编排 + API。

服务部分全部用 mock 替掉 OCR / 抽题 / 分类(它们各自有单测,而且会真花钱调 API),
只验证**编排逻辑**本身:骨架建得对不对、重复能不能挡住、断点续传省不省钱、
失败重试会不会拖垮整批、进度数字准不准。

API 部分走真实 HTTP,重点验证权限与"源文件路径不得越出上传目录"这条安全边界。
"""
import uuid

import pymupdf
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.security import create_access_token
from app.models import (
    BookChapter,
    BookSection,
    PdfChapterImportItem,
    PdfChapterImportTask,
    SourceBook,
)
from app.models.pdf_chapter_import import ChapterImportItemStatus, ChapterImportTaskStatus
from app.models.question import Question
from app.models.user import User
from app.services.importing.classify import QuestionClassification
from app.services.importing.contracts import ExtractionResult
from app.services.importing.pdf_chapter_import import PdfChapterImportService
from app.services.importing.pdf_split import ChapterRange
from app.services.ocr.chain import OcrChainResult

API = "/api/v1/pdf-chapter-imports"

SAMPLE_QUESTION = {
    "q_type": "single_choice",
    "content": "已知集合 A={1,2},则 A 的子集个数为",
    "options": ["2", "4", "6", "8"],
    "answer": "B",
    "analysis": "子集个数为 2^n = 4",
    "difficulty": 2,
}


# --------------------------------------------------------------------------- #
# 假件
# --------------------------------------------------------------------------- #

class FakeOcrChain:
    def __init__(self, markdown="第一章 习题\n\n1. 已知集合 A={1,2},求子集个数。"):
        self.markdown = markdown
        self.calls = 0

    async def parse(self, *, document, filename, db, page_images=None):
        self.calls += 1
        return OcrChainResult(
            markdown=self.markdown,
            source="paddleocr",
            page_count=len(page_images or []) or 1,
            assets={},
        )


class FakeExtractor:
    def __init__(self, questions=None, fail_times=0):
        self.questions = questions if questions is not None else [dict(SAMPLE_QUESTION)]
        self.fail_times = fail_times
        self.calls = 0

    async def extract(self, content, db, *, image_data=None, filename=None, mode="extract", subject_id=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("抽题服务暂时不可用")
        return ExtractionResult(questions=[dict(q) for q in self.questions], paper=None)


class FakeClassifier:
    def __init__(self, classification=None):
        self.classification = classification or QuestionClassification(
            q_type="single_choice", confidence=0.9, needs_review=False
        )
        self.calls = 0

    async def classify(self, questions, *, db, book_id, subject_id=None, chapter_id=None):
        self.calls += 1
        out = []
        for _ in questions:
            clone = QuestionClassification(
                q_type=self.classification.q_type,
                chapter_id=self.classification.chapter_id,
                section_id=self.classification.section_id,
                knowledge_point_ids=list(self.classification.knowledge_point_ids),
                confidence=self.classification.confidence,
                needs_review=self.classification.needs_review,
                review_reason=self.classification.review_reason,
            )
            out.append(clone)
        return out


def _service(**overrides) -> PdfChapterImportService:
    kwargs = {
        "ocr_chain": FakeOcrChain(),
        "extractor": FakeExtractor(),
        "classifier": FakeClassifier(),
    }
    kwargs.update(overrides)
    return PdfChapterImportService(**kwargs)


def _build_pdf(tmp_path, pages: int = 12, name: str = None) -> str:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"page {i + 1}")
    path = tmp_path / (name or f"{uuid.uuid4().hex[:8]}.pdf")
    doc.save(str(path))
    doc.close()
    return str(path)


def _ranges(with_sections: bool = True) -> list[ChapterRange]:
    if with_sections:
        return [
            ChapterRange(
                title="第一章 集合",
                start_page=1,
                end_page=6,
                children=[
                    ChapterRange(title="1.1 集合的概念", start_page=2, end_page=3, level=2),
                    ChapterRange(title="1.2 子集", start_page=4, end_page=6, level=2),
                ],
            ),
            ChapterRange(title="第二章 函数", start_page=7, end_page=9),
        ]
    return [ChapterRange(title="第一章 集合", start_page=1, end_page=6)]


async def _seed_user(db_session, username: str = None) -> User:
    user = User(
        username=username or f"u{uuid.uuid4().hex[:8]}",
        full_name="tester",
        hashed_password="x",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


# --------------------------------------------------------------------------- #
# 建任务:骨架
# --------------------------------------------------------------------------- #

async def test_create_task_builds_book_chapter_section_skeleton(db_session, tmp_path):
    path = _build_pdf(tmp_path)
    user = await _seed_user(db_session)

    task = await _service().create_task(
        db_session,
        user_id=user.id,
        subject_id=None,
        source_path=path,
        book_title="高中数学必修一",
        ranges=_ranges(),
    )

    assert task.total_items == 3  # 两个小节 + 一整章
    assert task.status == ChapterImportTaskStatus.PENDING

    book = await db_session.get(SourceBook, task.book_id)
    assert book.title == "高中数学必修一"
    assert book.page_count == 12

    chapters = (await db_session.execute(
        select(BookChapter).where(BookChapter.book_id == book.id).order_by(BookChapter.order_index)
    )).scalars().all()
    assert [c.title for c in chapters] == ["第一章 集合", "第二章 函数"]

    sections = (await db_session.execute(
        select(BookSection).where(BookSection.book_id == book.id).order_by(BookSection.order_index)
    )).scalars().all()
    # 第二章没有小节,也会补一条"整章"小节,保证题目有最细的一层可挂。
    assert {s.title for s in sections} == {"1.1 集合的概念", "1.2 子集", "第二章 函数"}


async def test_create_task_reuses_existing_book_by_title(db_session, tmp_path):
    path = _build_pdf(tmp_path)
    first = await _service().create_task(
        db_session, user_id=None, subject_id=None, source_path=path, book_title="同一本书", ranges=_ranges(False)
    )
    second = await _service().create_task(
        db_session, user_id=None, subject_id=None, source_path=path, book_title="同一本书", ranges=_ranges(False)
    )
    assert first.book_id == second.book_id


async def test_create_task_rejects_empty_selection(db_session, tmp_path):
    path = _build_pdf(tmp_path)
    with pytest.raises(Exception, match="没有选择任何章节"):
        await _service().create_task(
            db_session, user_id=None, subject_id=None, source_path=path, book_title="x", ranges=[]
        )


async def test_create_task_rejects_missing_file(db_session, tmp_path):
    with pytest.raises(Exception, match="不存在"):
        await _service().create_task(
            db_session,
            user_id=None,
            subject_id=None,
            source_path=str(tmp_path / "nope.pdf"),
            book_title="x",
            ranges=_ranges(False),
        )


# --------------------------------------------------------------------------- #
# 跑任务
# --------------------------------------------------------------------------- #

async def test_run_task_imports_questions_and_links_chapter(db_session, tmp_path):
    path = _build_pdf(tmp_path)
    user = await _seed_user(db_session)
    service = _service()

    task = await service.create_task(
        db_session, user_id=user.id, subject_id=None, source_path=path, book_title="链接校验", ranges=_ranges()
    )
    task = await service.run_task(db_session, task.id)

    assert task.status == ChapterImportTaskStatus.COMPLETED
    assert task.completed_items == 3
    assert task.failed_items == 0
    assert task.imported_questions == 3  # 每个章节 1 道题

    questions = (await db_session.execute(select(Question))).scalars().all()
    assert len(questions) == 3
    for question in questions:
        assert question.book_id == task.book_id
        assert question.chapter_id is not None
        assert question.section_id is not None
        assert question.status == "pending"


async def test_run_task_marks_no_questions_as_completed(db_session, tmp_path):
    """抽题结果为空不是错误——这一章可能真的只有例题。不应记为失败。"""
    path = _build_pdf(tmp_path)
    service = _service(extractor=FakeExtractor(questions=[]))
    task = await service.create_task(
        db_session, user_id=None, subject_id=None, source_path=path, book_title="空章节", ranges=_ranges(False)
    )
    task = await service.run_task(db_session, task.id)

    assert task.status == ChapterImportTaskStatus.COMPLETED
    assert task.completed_items == 1
    assert task.imported_questions == 0


async def test_run_task_is_partial_when_one_chapter_fails(db_session, tmp_path):
    """一章挂掉不能拖垮整批:必须继续跑完,并如实报 PARTIAL。"""
    path = _build_pdf(tmp_path)
    # 第一次 extract 失败(第一章),第二次成功(第二章)。
    service = _service(extractor=FakeExtractor(fail_times=1))
    task = await service.create_task(
        db_session, user_id=None, subject_id=None, source_path=path, book_title="部分失败", ranges=_ranges()
    )
    task = await service.run_task(db_session, task.id)

    # fail_times=1 只让第一次抽题调用失败,即第一章的第一个小节;其余照常完成。
    assert task.status == ChapterImportTaskStatus.PARTIAL
    assert task.failed_items == 1
    assert task.completed_items == 2
    assert task.imported_questions == 2


# --------------------------------------------------------------------------- #
# 重复导入检测 / 断点续传
# --------------------------------------------------------------------------- #

async def test_duplicate_content_is_skipped(db_session, tmp_path):
    """同一区间导两遍:第二次必须被挡住,不能重复进题。"""
    path = _build_pdf(tmp_path)
    service = _service()
    ranges = [
        ChapterRange(title="章A", start_page=1, end_page=3),
        ChapterRange(title="章B(与 A 同区间)", start_page=1, end_page=3),
    ]
    task = await service.create_task(
        db_session, user_id=None, subject_id=None, source_path=path, book_title="查重", ranges=ranges
    )
    task = await service.run_task(db_session, task.id)

    assert task.completed_items == 1
    assert task.skipped_items == 1
    assert task.imported_questions == 1

    skipped = [i for i in task.items if i.status == ChapterImportItemStatus.SKIPPED]
    assert "重复" in (skipped[0].error_message or "")


async def test_resume_reuses_cached_ocr(db_session, tmp_path):
    """断点续传的核心收益:重跑已完成章节时不再花第二次 OCR 的钱。"""
    path = _build_pdf(tmp_path)
    ocr = FakeOcrChain()
    service = _service(ocr_chain=ocr)

    task = await service.create_task(
        db_session, user_id=None, subject_id=None, source_path=path, book_title="续传", ranges=_ranges(False)
    )
    await service.run_task(db_session, task.id)
    assert ocr.calls == 1

    # 人为把它退回未完成(模拟任务中断),保留已缓存的 OCR 结果。
    item = task.items[0]
    item.status = ChapterImportItemStatus.PENDING
    await db_session.commit()

    await service.run_task(db_session, task.id)
    assert ocr.calls == 1, "断点续传应当复用缓存,不该再调一次 OCR"


async def test_run_task_skips_completed_items(db_session, tmp_path):
    path = _build_pdf(tmp_path)
    ocr = FakeOcrChain()
    service = _service(ocr_chain=ocr)
    task = await service.create_task(
        db_session, user_id=None, subject_id=None, source_path=path, book_title="跳过完成", ranges=_ranges(False)
    )
    await service.run_task(db_session, task.id)
    await service.run_task(db_session, task.id)  # 第二次:没有待办
    assert ocr.calls == 1


async def test_force_rerun_reprocesses_completed(db_session, tmp_path):
    """force 重跑会重新处理已完成章节,但内容没变时仍复用 OCR 缓存——
    重跑的目的不是再花一次 OCR 的钱。"""
    path = _build_pdf(tmp_path)
    ocr = FakeOcrChain()
    extractor = FakeExtractor()
    service = _service(ocr_chain=ocr, extractor=extractor)
    task = await service.create_task(
        db_session, user_id=None, subject_id=None, source_path=path, book_title="强制重跑", ranges=_ranges(False)
    )
    await service.run_task(db_session, task.id)
    assert extractor.calls == 1

    await service.run_task(db_session, task.id, only_pending=False)
    assert extractor.calls == 2, "force 重跑应当重新抽题"
    assert ocr.calls == 1, "内容未变,不该重复调用 OCR"


# --------------------------------------------------------------------------- #
# 重试
# --------------------------------------------------------------------------- #

async def test_retry_failed_item_recovers(db_session, tmp_path):
    path = _build_pdf(tmp_path)
    extractor = FakeExtractor(fail_times=1)
    service = _service(extractor=extractor)
    task = await service.create_task(
        db_session, user_id=None, subject_id=None, source_path=path, book_title="重试", ranges=_ranges(False)
    )
    task = await service.run_task(db_session, task.id)
    assert task.failed_items == 1

    item = await service.retry_item(db_session, task.items[0].id)

    assert item.status == ChapterImportItemStatus.COMPLETED
    assert item.attempt_count >= 2
    assert item.questions_count == 1
    assert (await db_session.get(PdfChapterImportTask, task.id)).failed_items == 0


async def test_retry_rejects_completed_item(db_session, tmp_path):
    path = _build_pdf(tmp_path)
    service = _service()
    task = await service.create_task(
        db_session, user_id=None, subject_id=None, source_path=path, book_title="重复重试", ranges=_ranges(False)
    )
    await service.run_task(db_session, task.id)

    with pytest.raises(Exception, match="可以重试"):
        await service.retry_item(db_session, task.items[0].id)


# --------------------------------------------------------------------------- #
# 分类待审核
# --------------------------------------------------------------------------- #

async def test_needs_review_is_recorded_on_question(db_session, tmp_path):
    """分类器没把握时,题目要带上待审核标记,而不是静默入库。"""
    path = _build_pdf(tmp_path)
    service = _service(
        classifier=FakeClassifier(
            QuestionClassification(confidence=0.2, needs_review=True, review_reason="置信度过低")
        )
    )
    task = await service.create_task(
        db_session, user_id=None, subject_id=None, source_path=path, book_title="待审核", ranges=_ranges(False)
    )
    await service.run_task(db_session, task.id)

    question = (await db_session.execute(select(Question))).scalars().first()
    assert question is not None
    assert question.classification_review, "待审核的题目必须留下痕迹"
    assert "置信度过低" in question.classification_review


async def test_high_confidence_leaves_no_review_marker(db_session, tmp_path):
    """分类可信时不要留下待审核标记,否则审核队列会被噪声淹没。"""
    path = _build_pdf(tmp_path)
    service = _service(
        classifier=FakeClassifier(QuestionClassification(confidence=0.95, needs_review=False))
    )
    task = await service.create_task(
        db_session, user_id=None, subject_id=None, source_path=path, book_title="可信分类", ranges=_ranges(False)
    )
    await service.run_task(db_session, task.id)

    question = (await db_session.execute(select(Question))).scalars().first()
    assert question is not None
    assert question.classification_review is None


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

async def _auth_headers(user: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(subject=user.id)}"}


async def test_api_parse_toc_returns_chapters(client, db_session, tmp_path):
    user = await _seed_user(db_session)
    path = tmp_path / "toc.pdf"

    # 必须指定 china-s:PyMuPDF 的默认字体没有汉字字形,中文会被写成 '·',
    # 那样测的就不是目录解析而是字体缺字了。
    doc = pymupdf.open()
    for i in range(12):
        page = doc.new_page(width=595, height=842)
        if i == 0:
            for j, line in enumerate(
                [
                    "目  录",
                    "第一章  集合与函数 ................. 1",
                    "  1.1  集合的概念 .................. 2",
                    "第二章  三角函数 ................... 7",
                ]
            ):
                page.insert_text((72, 100 + j * 24), line, fontname="china-s", fontsize=11)
        else:
            page.insert_text((72, 100), f"page {i + 1}")
    doc.save(str(path))
    doc.close()

    with path.open("rb") as fh:
        response = await client.post(
            f"{API}/parse-toc",
            files={"file": ("toc.pdf", fh.read(), "application/pdf")},
            headers=await _auth_headers(user),
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["page_count"] == 12
    assert payload["toc_source"] == "text"
    assert payload["chapters"], "应当解析出章节"


async def test_api_rejects_non_pdf_upload(client, db_session, tmp_path):
    user = await _seed_user(db_session)
    response = await client.post(
        f"{API}/parse-toc",
        files={"file": ("a.txt", b"hello", "text/plain")},
        headers=await _auth_headers(user),
    )
    assert response.status_code == 400


async def test_api_create_and_run_task(client, db_session, tmp_path):
    user = await _seed_user(db_session)
    upload_dir = settings.UPLOAD_DIR / uuid.uuid4().hex[:8]
    upload_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = upload_dir / "book.pdf"
    pdf_path.write_bytes(_pdf_bytes(9))

    created = await client.post(
        f"{API}/tasks",
        json={
            "source_path": str(pdf_path),
            "book_title": "API 教材",
            "chapters": [
                {"title": "第一章", "start_page": 1, "end_page": 4, "children": []},
                {"title": "第二章", "start_page": 5, "end_page": 9, "children": []},
            ],
        },
        headers=await _auth_headers(user),
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["id"]
    assert created.json()["total_items"] == 2

    detail = await client.get(f"{API}/tasks/{task_id}", headers=await _auth_headers(user))
    assert detail.status_code == 200
    assert len(detail.json()["items"]) == 2


async def test_api_rejects_path_outside_upload_dir(client, db_session, tmp_path):
    """源文件路径来自客户端——必须挡住目录穿越。"""
    user = await _seed_user(db_session)
    response = await client.post(
        f"{API}/tasks",
        json={
            "source_path": "C:/Windows/System32/config/SAM",
            "book_title": "越权",
            "chapters": [{"title": "x", "start_page": 1, "end_page": 1, "children": []}],
        },
        headers=await _auth_headers(user),
    )
    assert response.status_code == 400
    assert "非法" in response.json()["detail"]


async def test_api_task_isolation_between_users(client, db_session, tmp_path):
    user_a = await _seed_user(db_session)
    user_b = await _seed_user(db_session)
    upload_dir = settings.UPLOAD_DIR / uuid.uuid4().hex[:8]
    upload_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = upload_dir / "b.pdf"
    pdf_path.write_bytes(_pdf_bytes(4))

    created = await client.post(
        f"{API}/tasks",
        json={
            "source_path": str(pdf_path),
            "book_title": "私有任务",
            "chapters": [{"title": "章", "start_page": 1, "end_page": 2, "children": []}],
        },
        headers=await _auth_headers(user_a),
    )
    task_id = created.json()["id"]

    forbidden = await client.get(f"{API}/tasks/{task_id}", headers=await _auth_headers(user_b))
    assert forbidden.status_code == 403


async def test_api_books_and_chapters(client, db_session, tmp_path):
    user = await _seed_user(db_session)
    path = _build_pdf(tmp_path)
    await _service().create_task(
        db_session, user_id=user.id, subject_id=None, source_path=path, book_title="目录查询", ranges=_ranges()
    )

    books = await client.get(f"{API}/books", headers=await _auth_headers(user))
    assert books.status_code == 200
    book_id = next(b["id"] for b in books.json()["items"] if b["title"] == "目录查询")

    chapters = await client.get(f"{API}/books/{book_id}/chapters", headers=await _auth_headers(user))
    assert chapters.status_code == 200
    payload = chapters.json()
    assert [c["title"] for c in payload["chapters"]] == ["第一章 集合", "第二章 函数"]
    assert payload["chapters"][0]["sections"][0]["title"] == "1.1 集合的概念"


def _pdf_bytes(pages: int = 6) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"page {i + 1}")
    data = doc.tobytes()
    doc.close()
    return data
