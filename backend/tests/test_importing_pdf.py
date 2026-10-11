"""PDF 摄取与 OCR 图片落盘的验证。"""

import uuid

import pytest

from app.core.config import settings
from app.services.doc_processor import _persist_assets
from app.services.importing.ingest import PdfIngestor

fitz = pytest.importorskip("fitz", reason="需要 PyMuPDF")


def _make_pdf(tmp_path, pages: int = 2):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"Page {i + 1}")
    path = tmp_path / "sample.pdf"
    doc.save(str(path))
    doc.close()
    return path


async def test_pdf_ingestor_rasterizes_every_page(tmp_path):
    path = _make_pdf(tmp_path, pages=3)
    doc = await PdfIngestor().ingest(path, task_id="pdf-unit")

    assert doc.filename == "sample.pdf"
    # 原始字节原样保留,交给第一层 PaddleOCR-VL。
    assert doc.document is not None and doc.document.startswith(b"%PDF")
    # 逐页栅格图:页数一致,且都是 PNG。
    assert doc.page_images is not None and len(doc.page_images) == 3
    assert all(img.startswith(b"\x89PNG") for img in doc.page_images)
    # 审核页要能直接展示原图。
    assert doc.image_url == "/static/media/pdf-unit/page_001.png"
    assert (settings.MEDIA_DIR / "pdf-unit" / "page_003.png").exists()


async def test_pdf_ingestor_rejects_over_page_cap(tmp_path, monkeypatch):
    """页数超限要在栅格化之前拦下,避免误传的大文件把 OCR 配额打光。"""
    import app.services.importing.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "PDF_MAX_PAGES", 2)
    path = _make_pdf(tmp_path, pages=3)

    with pytest.raises(ValueError, match="页数过多"):
        await PdfIngestor().ingest(path, task_id="pdf-overcap")


def test_persist_assets_rewrites_ocr_image_refs():
    """PaddleOCR 返回的是远端临时链接,必须换成 /static/media 本地路径。"""
    task_id = f"asset-{uuid.uuid4().hex[:8]}"
    markdown = "题干见图 ![](images/fig1.jpg)\n\n另外一张 ![图](images/fig2.png)"
    assets = {
        "images/fig1.jpg": b"\xff\xd8jpeg-bytes",
        "images/fig2.png": b"\x89PNG-png-bytes",
    }

    out = _persist_assets(markdown, assets, task_id)

    assert f"/static/media/{task_id}/ocr/fig1.jpg" in out
    assert f"/static/media/{task_id}/ocr/fig2.png" in out
    assert "images/fig1.jpg" not in out
    assert (settings.MEDIA_DIR / task_id / "ocr" / "fig1.jpg").read_bytes() == b"\xff\xd8jpeg-bytes"


def test_persist_assets_noop_without_assets():
    markdown = "没有图片的题干 ![](images/x.jpg)"
    assert _persist_assets(markdown, {}, "t") == markdown
