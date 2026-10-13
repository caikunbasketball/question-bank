"""第三层的分类归属:题型 / 章节 / 小节 / 知识点 → 已有 ID。

这一层的**唯一职责**是"把题目挂到数据库里已经存在的分类上",不负责创造分类。
所以整个模块围着一条铁律转:

    **模型返回的任何 ID,只要不在候选集里,一律丢弃并标记待审核。**

宁可让一道题挂着"待审核"等人看一眼,也不能让一个编造的 ID 混进题库——后者
会静默地污染按章节筛选、按知识点统计的每一个结果,而且极难发现。

置信度、待审核标记是给人看的信号,不是把关的闸门:真正的闸门是
`_validate()` 里那几次集合比对,模型说什么都不影响这道闸。

用到的是 ``AI_TEXT_MODEL_ID`` 槽(分类模型,如 DeepSeek Flash),与抽题共用同一
个文本槽,不额外占一个配置位。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_point import KnowledgePoint
from app.models.question import QuestionType
from app.services.ai_provider import AIProvider, get_ai_provider
from app.services.importing.provider_config import resolve_text_provider

logger = logging.getLogger(__name__)

# 单批题目数。题目多了 prompt 会超出模型的有效注意力,分类质量反而下降。
MAX_QUESTIONS_PER_BATCH = 15
# 单题送进 prompt 的内容长度上限。公式在题干里,Latex 很占字符,给宽一点。
CONTENT_SNIPPET_CHARS = 600
# 每题向量召回的知识点候选数。
KP_RECALL_LIMIT = 8
# 向量库不可用时的兜底:直接取学科内前 N 个知识点当候选。
KP_FALLBACK_LIMIT = 200
# 低于此置信度一律标待审核(模型自己说没把握,就别替它拍板)。
MIN_AUTO_CONFIDENCE = 0.6

QUESTION_TYPE_VALUES = [t.value for t in QuestionType]

# 模型可用的题型说明,写进 prompt 帮它区分易混的填空/解答。
_Q_TYPE_HINTS = {
    "single_choice": "单选题(只有一个正确选项)",
    "multiple_choice": "多选题(有两个或以上正确选项)",
    "true_false": "判断题(只有对/错两种作答)",
    "fill_in_the_blank": "填空题(留空待填,可能有多个空)",
    "free_response": "解答题/计算题/证明题(需写出过程)",
}


@dataclass
class CategoryOption:
    """一个可选分类。"""

    id: int
    title: str
    # 小节的所属章,用于校验 section 是否真的挂在该章下。
    parent_id: Optional[int] = None


@dataclass
class ClassificationCandidates:
    """本次分类允许使用的 ID 全集。模型只能从这里挑。"""

    chapters: list[CategoryOption] = field(default_factory=list)
    sections: list[CategoryOption] = field(default_factory=list)
    question_types: list[str] = field(default_factory=lambda: list(QUESTION_TYPE_VALUES))

    @property
    def chapter_ids(self) -> set[int]:
        return {c.id for c in self.chapters}

    @property
    def section_ids(self) -> set[int]:
        return {s.id for s in self.sections}


@dataclass
class QuestionClassification:
    """一道题的分类结果。"""

    q_type: Optional[str] = None
    chapter_id: Optional[int] = None
    section_id: Optional[int] = None
    # 可多个(一道题跨几个知识点很常见)。
    knowledge_point_ids: list[int] = field(default_factory=list)
    confidence: float = 0.0
    needs_review: bool = True
    review_reason: Optional[str] = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "q_type": self.q_type,
            "chapter_id": self.chapter_id,
            "section_id": self.section_id,
            "knowledge_point_ids": list(self.knowledge_point_ids),
            "confidence": round(self.confidence, 3),
            "needs_review": self.needs_review,
            "review_reason": self.review_reason,
        }


class ClassifierError(RuntimeError):
    """分类层异常。message 会进任务失败原因,不要塞堆栈。"""


# --------------------------------------------------------------------------- #
# 候选集
# --------------------------------------------------------------------------- #

async def load_candidates(
    db: AsyncSession, *, book_id: int, chapter_id: Optional[int] = None
) -> ClassificationCandidates:
    """读出这本书的章节 / 小节清单,作为模型可选 ID 的白名单。

    ``chapter_id`` 给了就只取该章的小节——章节导入本来就是一章一章跑的,
    把无关章节塞进 prompt 只会增加模型挑错的机会。
    """
    from app.models.book import BookChapter, BookSection

    chapters = (
        await db.execute(
            select(BookChapter)
            .where(BookChapter.book_id == book_id)
            .order_by(BookChapter.order_index, BookChapter.id)
        )
    ).scalars().all()

    stmt = select(BookSection).where(BookSection.book_id == book_id)
    if chapter_id is not None:
        stmt = stmt.where(BookSection.chapter_id == chapter_id)
    sections = (await db.execute(stmt.order_by(BookSection.order_index, BookSection.id))).scalars().all()

    return ClassificationCandidates(
        chapters=[CategoryOption(id=c.id, title=c.title) for c in chapters],
        sections=[
            CategoryOption(id=s.id, title=s.title, parent_id=s.chapter_id) for s in sections
        ],
    )


async def _recall_knowledge_points(
    db: AsyncSession, *, subject_id: Optional[int], query: str, kp_ids: Optional[set[int]] = None
) -> list[CategoryOption]:
    """给一道题召回知识点候选。

    优先走向量检索(题库已有 RAG 通道);向量模型没配或检索失败时,退回学科内
    前 KP_FALLBACK_LIMIT 个知识点。两条路都返回**已存在的**知识点,绝不新建。
    """
    try:
        from app.services.kp_retriever import KnowledgePointRetriever

        results = await KnowledgePointRetriever.retrieve(
            query=query, subject_id=subject_id, limit=KP_RECALL_LIMIT
        )
        docs = (results or {}).get("documents") or []
        ids = (results or {}).get("ids") or []
        if docs and docs[0] and ids and ids[0]:
            options: list[CategoryOption] = []
            for text, raw_id in zip(docs[0], ids[0]):
                try:
                    options.append(CategoryOption(id=int(raw_id), title=str(text).strip()))
                except (TypeError, ValueError):
                    continue
            if options:
                return options
    except Exception as exc:  # noqa: BLE001 - 向量库不可用时静默降级
        logger.debug("知识点向量召回不可用,退回学科全量: %s", exc)

    stmt = select(KnowledgePoint).order_by(KnowledgePoint.id).limit(KP_FALLBACK_LIMIT)
    if subject_id is not None:
        stmt = select(KnowledgePoint).where(
            KnowledgePoint.subject_id == subject_id
        ).order_by(KnowledgePoint.id).limit(KP_FALLBACK_LIMIT)
    rows = (await db.execute(stmt)).scalars().all()
    return [CategoryOption(id=kp.id, title=kp.name) for kp in rows]


# --------------------------------------------------------------------------- #
# prompt
# --------------------------------------------------------------------------- #

def _snippet(content: Any) -> str:
    text = str(content or "")
    if len(text) > CONTENT_SNIPPET_CHARS:
        return text[:CONTENT_SNIPPET_CHARS] + " …(截断)"
    return text


def build_prompt(
    questions: Sequence[Mapping[str, Any]],
    candidates: ClassificationCandidates,
    kp_options: Sequence[Sequence[CategoryOption]],
) -> str:
    """构造分类 prompt。

    知识点候选是**逐题**给的(每道题召回的候选不同),章节 / 小节 / 题型是全局的。
    """
    lines: list[str] = []
    lines.append("你是题库分类助手。请为下列题目判定题型并匹配到**已存在的**分类 ID。")
    lines.append("")
    lines.append("## 可用题型(只能从这些值里选)")
    for value in candidates.question_types:
        lines.append(f"- {value}:{_Q_TYPE_HINTS.get(value, '')}")
    lines.append("")
    lines.append("## 本书章节(id | 标题)")
    if candidates.chapters:
        for c in candidates.chapters:
            lines.append(f"- {c.id} | {c.title}")
    else:
        lines.append("- (无)")
    lines.append("")
    lines.append("## 本书小节(id | 所属章id | 标题)")
    if candidates.sections:
        for s in candidates.sections:
            lines.append(f"- {s.id} | 章{s.parent_id} | {s.title}")
    else:
        lines.append("- (无)")
    lines.append("")
    lines.append("## 待分类题目")
    for idx, question in enumerate(questions):
        lines.append(f"### 题目 {idx}")
        lines.append(f"题干:{_snippet(question.get('content'))}")
        if question.get("options"):
            lines.append(f"选项:{_snippet(question.get('options'))}")
        if question.get("answer"):
            lines.append(f"答案:{_snippet(question.get('answer'))}")
        options = kp_options[idx] if idx < len(kp_options) else []
        if options:
            lines.append("可选知识点(只能从这些 id 里选,可多选,0-5 个):")
            for opt in options:
                lines.append(f"  - {opt.id} | {opt.title}")
        else:
            lines.append("可选知识点:(无)")
        lines.append("")

    lines.append("## 输出要求")
    lines.append("只输出一个 JSON 对象,不要任何解释文字,格式如下:")
    lines.append(
        '{"results":[{"index":0,"q_type":"single_choice","chapter_id":3,'
        '"section_id":7,"knowledge_point_ids":[12,15],"confidence":0.86,'
        '"review_reason":""}]}'
    )
    lines.append("")
    lines.append("规则:")
    lines.append("1. 所有 ID 必须来自上面给出的清单,**禁止输出清单中不存在的 ID**。")
    lines.append("2. 不确定就填 null(知识点填空数组),并在 review_reason 里说明为什么不确定。")
    lines.append("3. 一道题只能有一个题型、一个章节、一个小节;知识点可以 0 到 5 个。")
    lines.append("4. section_id 必须属于所选的 chapter_id,否则把 section_id 填 null。")
    lines.append("5. confidence 取 0 到 1 的小数,表示你对这次判定的把握。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 校验:真正的闸门
# --------------------------------------------------------------------------- #

def _coerce_id(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _validate(
    raw: Mapping[str, Any],
    candidates: ClassificationCandidates,
    kp_allowed: Optional[set[int]],
) -> QuestionClassification:
    """把模型的一行输出收敛成可信的分类。

    任何不在候选集里的 ID 都会被丢掉,并把 needs_review 置 True——这是本模块
    存在的理由,不要为了"提高命中率"放松这里的任何一条。
    """
    reasons: list[str] = []
    result = QuestionClassification()

    # 题型
    q_type = str(raw.get("q_type") or "").strip()
    if q_type in QUESTION_TYPE_VALUES:
        result.q_type = q_type
    elif q_type:
        reasons.append(f"题型取值非法({q_type}),已置空")

    # 章节
    chapter_id = _coerce_id(raw.get("chapter_id"))
    if chapter_id is not None:
        if chapter_id in candidates.chapter_ids:
            result.chapter_id = chapter_id
        else:
            reasons.append(f"章节 ID {chapter_id} 不在本书章节清单内,已丢弃")

    # 小节:必须在清单内,且必须属于已确认的章节
    section_id = _coerce_id(raw.get("section_id"))
    if section_id is not None:
        matched = next((s for s in candidates.sections if s.id == section_id), None)
        if matched is None:
            reasons.append(f"小节 ID {section_id} 不在本书小节清单内,已丢弃")
        elif result.chapter_id is not None and matched.parent_id != result.chapter_id:
            reasons.append(f"小节 {section_id} 不属于章节 {result.chapter_id},已丢弃")
        else:
            result.section_id = section_id
            # 小节确定了,章节跟着它走(模型可能只给了小节)。
            if result.chapter_id is None and matched.parent_id is not None:
                if matched.parent_id in candidates.chapter_ids:
                    result.chapter_id = matched.parent_id

    # 知识点:可多值,逐个比对
    raw_kps = raw.get("knowledge_point_ids")
    allowed = kp_allowed if kp_allowed is not None else set()
    if isinstance(raw_kps, (list, tuple)):
        kept: list[int] = []
        for value in raw_kps:
            kp_id = _coerce_id(value)
            if kp_id is None:
                continue
            if kp_allowed is not None and kp_id not in allowed:
                reasons.append(f"知识点 ID {kp_id} 不在候选项内,已丢弃")
                continue
            if kp_id not in kept:
                kept.append(kp_id)
        result.knowledge_point_ids = kept[:5]

    # 置信度
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    result.confidence = max(0.0, min(1.0, confidence))

    # 待审核判定:模型说没把握 / 关键归属缺失 / 有字段被丢弃
    if result.confidence < MIN_AUTO_CONFIDENCE:
        reasons.append(f"置信度 {result.confidence:.2f} 低于 {MIN_AUTO_CONFIDENCE}")
    if result.q_type is None:
        reasons.append("题型未确定")
    if result.chapter_id is None and candidates.chapters:
        reasons.append("章节未确定")

    result.needs_review = bool(reasons)
    result.review_reason = "；".join(reasons) if reasons else None
    return result


# --------------------------------------------------------------------------- #
# 分类器
# --------------------------------------------------------------------------- #

class QuestionClassifier:
    """批量分类:召回候选 → 调模型 → 校验 → 落结构。"""

    def __init__(self, provider_factory=None) -> None:
        self._provider_factory = provider_factory or get_ai_provider

    async def classify(
        self,
        questions: Sequence[Mapping[str, Any]],
        *,
        db: AsyncSession,
        book_id: int,
        subject_id: Optional[int] = None,
        chapter_id: Optional[int] = None,
    ) -> list[QuestionClassification]:
        """返回与 questions 等长、同序的分类结果。任何异常都降级为"全部待审核"。"""
        if not questions:
            return []

        candidates = await load_candidates(db, book_id=book_id, chapter_id=chapter_id)
        provider_name, config = await resolve_text_provider(db)
        if provider_name is None:
            logger.warning("未配置分类模型(AI_TEXT_MODEL_ID),题目将全部标记待审核")
            return [
                QuestionClassification(review_reason="未配置分类模型") for _ in questions
            ]

        results: list[QuestionClassification] = []
        provider = self._provider_factory(provider_name)

        for start in range(0, len(questions), MAX_QUESTIONS_PER_BATCH):
            batch = list(questions[start : start + MAX_QUESTIONS_PER_BATCH])
            results.extend(
                await self._classify_batch(batch, db, provider, config, candidates, subject_id)
            )
        return results

    async def _classify_batch(
        self,
        batch: list[Mapping[str, Any]],
        db: AsyncSession,
        provider: AIProvider,
        config: dict,
        candidates: ClassificationCandidates,
        subject_id: Optional[int],
    ) -> list[QuestionClassification]:
        kp_options: list[list[CategoryOption]] = []
        for question in batch:
            query = " ".join(
                str(question.get(key) or "")
                for key in ("content", "analysis", "summary")
            ).strip() or str(question.get("content") or "")
            kp_options.append(
                await _recall_knowledge_points(db, subject_id=subject_id, query=query[:500])
            )

        prompt = build_prompt(batch, candidates, kp_options)
        payload = await provider.classify_json(prompt, config)

        raw_by_index: dict[int, Mapping[str, Any]] = {}
        if isinstance(payload, dict):
            raw_list = payload.get("results")
            if isinstance(raw_list, list):
                for item in raw_list:
                    if not isinstance(item, Mapping):
                        continue
                    idx = _coerce_id(item.get("index"))
                    if idx is not None:
                        raw_by_index[idx] = item
            elif "index" in payload:
                idx = _coerce_id(payload.get("index"))
                if idx is not None:
                    raw_by_index[idx] = payload

        out: list[QuestionClassification] = []
        for idx, question in enumerate(batch):
            allowed = {opt.id for opt in kp_options[idx]}
            raw = raw_by_index.get(idx)
            if raw is None:
                out.append(
                    QuestionClassification(review_reason="分类模型未返回本题结果")
                )
                continue
            out.append(_validate(raw, candidates, allowed))
        return out
