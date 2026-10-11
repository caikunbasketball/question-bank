"""第二层:视觉大模型兜底转写(OpenAI 兼容协议,如硅基流动的 Qwen3-VL-32B-Instruct)。

定位是"补充"而非"主解析":只在第一层 PaddleOCR-VL 失败或产出明显不完整时启用。
因此 prompt 刻意保守——**只做转写,不做抽题、不做解答**,输出与第一层对齐:
纯 markdown(公式用 LaTeX、表格用 pipe 表格),这样下游结构化阶段完全无感。

按页并发请求并保序;单页失败只丢该页(记录日志),不让整卷失败。
"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Optional

from .contracts import OcrError, strip_markdown_noise

logger = logging.getLogger(__name__)

# 并发页数:过高会触发上游限流,过低则长文档太慢。
_MAX_CONCURRENCY = 3

_TRANSCRIBE_PROMPT = """你是一个文档 OCR 引擎。请把这张页面图片**逐字转写**为 Markdown。

硬性要求:
1. 只做转写,不要抽题、不要解答、不要总结、不要评论。
2. 数学公式一律用 LaTeX 表示:行内用 $...$,独立成行用 $$...$$。
3. 表格用 Markdown pipe 表格(| a | b |);图片位置用 `![](图片说明)` 占位。
4. 保持原文的题号、选项标号(A. B. C. D.)、(1)(2) 等编号原样。
5. 看不清的字用 `␣` 占位,不要凭常识补全——补全比留空更危险。
6. 直接输出 Markdown 正文,不要包裹 ```markdown 代码块,不要任何前后缀说明。"""


async def transcribe_pages(
    pages: list[bytes],
    *,
    config: dict,
    filename: Optional[str] = None,
) -> tuple[list[str], Optional[str]]:
    """把逐页图片交给视觉模型转写。

    返回 ``(page_texts, error)``:``page_texts`` 与 ``pages`` **严格同序同长**
    (失败的页留空串),这样调用方能做精确的页级回填,不依赖任何切分启发式。
    ``error`` 非空表示整体不可用(未配置 / 依赖缺失 / 全部页失败)。
    """
    api_key = (config or {}).get("API_KEY")
    base_url = (config or {}).get("BASE_URL") or None
    model_name = (config or {}).get("MODEL_NAME") or "Qwen/Qwen3-VL-32B-Instruct"

    if not api_key:
        return [], "视觉兜底模型未配置 API Key"
    if not pages:
        return [], "无可用页面图像"

    try:
        from openai import AsyncOpenAI
    except ImportError:
        return [], "缺少 openai 依赖,无法调用视觉兜底模型"

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
    results: list[str] = [""] * len(pages)

    async def _one(idx: int, image: bytes) -> None:
        async with semaphore:
            try:
                results[idx] = await _transcribe_one(
                    client, model_name, image, idx, len(pages), filename
                )
            except Exception as exc:  # noqa: BLE001 - 单页失败降级为留空
                logger.warning("视觉兜底:第 %d 页转写失败: %s", idx + 1, exc)

    await asyncio.gather(*[_one(i, img) for i, img in enumerate(pages)])

    if not any(r.strip() for r in results):
        return results, "视觉兜底模型未产出任何内容"
    return results, None


async def _transcribe_one(
    client: "Any",
    model_name: str,
    image: bytes,
    idx: int,
    total: int,
    filename: Optional[str],
) -> str:
    mime = _guess_mime(image)
    b64 = base64.b64encode(image).decode("ascii")
    header = f"(文件:{filename} 第 {idx + 1}/{total} 页)" if filename else f"(第 {idx + 1}/{total} 页)"

    resp = await client.chat.completions.create(
        model=model_name,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"{header}\n{_TRANSCRIBE_PROMPT}"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ],
        temperature=0,
    )
    content = resp.choices[0].message.content if resp.choices else None
    return strip_markdown_noise(_strip_code_fence(content or ""))


def _strip_code_fence(text: str) -> str:
    """模型偶尔会把整页裹进 ```markdown,去掉它。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        first_nl = stripped.find("\n")
        if first_nl != -1:
            stripped = stripped[first_nl + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped


def _guess_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


__all__ = ["transcribe_pages", "OcrError"]
