"""三层模型方案 —— OCR 链的单测。

不联网、不依赖真实供应商:第一层用假 client、第二层用假转写函数,
专门验证"什么时候兜底、兜底兜哪些页、什么时候不兜底"这三条判定。
"""

import pytest

import app.services.ocr.chain as chain_module
from app.services.ocr.chain import DocumentOcrChain
from app.services.ocr.contracts import OcrError, OcrResult
from app.services.ocr.paddleocr import _normalize_job_url, _safe_relpath

# 造两条都超过"单页可用阈值(20 字)"的文本,避免测试里反复数汉字。
PAGE_A = "第一页" + "内容" * 15       # 33 字
PAGE_B = "第二页" + "内容" * 15       # 33 字
WHOLE_BODY = "一份很长很长的第一层产出" * 20

# --- 假件 ----------------------------------------------------------------


class _FakePaddle:
    """假的第一层:直接返回预置的 OcrResult。"""

    def __init__(self, result: OcrResult):
        self._result = result

    async def parse(self, *, data, filename=None, file_url=None):  # noqa: ANN001
        return self._result


class _BoomPaddle:
    """假的第一层:抛 OcrError。"""

    async def parse(self, *, data, filename=None, file_url=None):  # noqa: ANN001
        raise OcrError("HTTP 401 访问令牌无效")


def _patch_providers(monkeypatch, *, ocr=("paddleocr", {"API_KEY": "t"}), vision=("openai", {"API_KEY": "v"})):
    async def _ocr(db):
        return ocr

    async def _vision(db):
        return vision

    monkeypatch.setattr(chain_module, "resolve_ocr_provider", _ocr)
    monkeypatch.setattr(chain_module, "resolve_vision_provider", _vision)


# --- 主路径 -------------------------------------------------------------- #


async def test_complete_primary_result_is_used_without_fallback(monkeypatch):
    _patch_providers(monkeypatch)
    primary = OcrResult(
        markdown=f"{PAGE_A}\n\n{PAGE_B}",
        page_texts=[PAGE_A, PAGE_B],
        page_count=2,
    )

    async def _should_not_be_called(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("产出完整时不应触发第二层")

    chain = DocumentOcrChain(
        paddle_client_factory=lambda cfg: _FakePaddle(primary),
        vision_transcriber=_should_not_be_called,
    )
    result = await chain.parse(document=b"x", filename="a.pdf", db=None, page_images=[b"p1", b"p2"])

    assert result.source == "paddleocr"
    assert result.used_fallback is False
    assert result.fallback_pages == []


async def test_weak_pages_trigger_page_level_merge_only(monkeypatch):
    """一级只吃下了第 1 页,第 2 页为空 —— 只应对第 2 页调第二层。"""
    _patch_providers(monkeypatch)
    primary = OcrResult(markdown=PAGE_A, page_texts=[PAGE_A, ""], page_count=2)
    seen: list[int] = []

    async def _transcriber(images, *, config, filename=None):  # noqa: ANN001
        seen.append(len(images))
        return ["第二页由兜底补齐的内容" for _ in images], None

    chain = DocumentOcrChain(
        paddle_client_factory=lambda cfg: _FakePaddle(primary),
        vision_transcriber=_transcriber,
    )
    result = await chain.parse(document=b"x", filename="a.pdf", db=None, page_images=[b"p1", b"p2"])

    assert seen == [1], "只应把缺失的那一页交给第二层"
    assert result.fallback_pages == [2]
    assert "第二页由兜底补齐的内容" in result.markdown
    assert PAGE_A in result.markdown


async def test_primary_failure_falls_back_to_vision(monkeypatch):
    _patch_providers(monkeypatch)
    pages = [b"p1", b"p2", b"p3"]

    async def _transcriber(images, *, config, filename=None):  # noqa: ANN001
        return [f"整卷兜底第{i + 1}页的内容足够长以通过可用性判断" for i in range(len(images))], None

    chain = DocumentOcrChain(
        paddle_client_factory=lambda cfg: _BoomPaddle(),
        vision_transcriber=_transcriber,
    )
    result = await chain.parse(document=b"x", filename="a.pdf", db=None, page_images=pages)

    assert result.source == "vision"
    assert result.used_fallback is True
    assert result.fallback_pages == [1, 2, 3]
    assert "整卷兜底第3页" in result.markdown
    # 第一层的失败原因要保留下来,排障时才有线索。
    assert "401" in (result.primary_error or "")


async def test_keeps_primary_when_fallback_is_not_better(monkeypatch):
    """一级产出可用但页数对不上;兜底内容更少 —— 不应把好结果换掉。"""
    _patch_providers(monkeypatch)
    primary = OcrResult(
        markdown=WHOLE_BODY,
        page_texts=[WHOLE_BODY, PAGE_B],
        page_count=2,
    )

    async def _transcriber(images, *, config, filename=None):  # noqa: ANN001
        return ["短"], None

    chain = DocumentOcrChain(
        paddle_client_factory=lambda cfg: _FakePaddle(primary),
        vision_transcriber=_transcriber,
    )
    result = await chain.parse(document=b"x", filename="a.pdf", db=None, page_images=[b"p1", b"p2", b"p3"])

    assert result.source == "paddleocr"
    assert result.markdown.startswith("一份很长很长的第一层产出")


async def test_raises_when_both_layers_yield_nothing(monkeypatch):
    _patch_providers(monkeypatch, ocr=(None, {}), vision=(None, {}))

    chain = DocumentOcrChain(paddle_client_factory=lambda cfg: _FakePaddle(OcrResult()))
    with pytest.raises(OcrError) as exc:
        await chain.parse(document=b"x", filename="a.pdf", db=None, page_images=[b"p1"])

    assert "文档解析失败" in str(exc.value)


async def test_missing_ocr_layer_uses_vision_for_whole_doc(monkeypatch):
    """未接入 PaddleOCR 时(老部署),整卷交给第二层,不该报错。"""
    _patch_providers(monkeypatch, ocr=(None, {}))

    async def _transcriber(images, *, config, filename=None):  # noqa: ANN001
        return [f"第{i + 1}页内容" for i in range(len(images))], None

    chain = DocumentOcrChain(
        paddle_client_factory=lambda cfg: _FakePaddle(OcrResult()),
        vision_transcriber=_transcriber,
    )
    result = await chain.parse(document=b"x", filename="a.png", db=None, page_images=[b"p1"])

    assert result.source == "vision"
    assert "第1页内容" in result.markdown


# --- 工具函数 ------------------------------------------------------------ #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"),
        ("https://paddleocr.aistudio-app.com", "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"),
        ("https://paddleocr.aistudio-app.com/api/v2/", "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"),
        (
            "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs",
            "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs",
        ),
    ],
)
def test_job_url_normalization(raw, expected):
    assert _normalize_job_url(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("images/a.jpg", "images/a.jpg"),
        ("../../etc/passwd", "etc/passwd"),
        ("/abs/path/x.png", "abs/path/x.png"),
        ("a/../../b.png", "b.png"),
    ],
)
def test_safe_relpath_blocks_traversal(raw, expected):
    assert _safe_relpath(raw) == expected
