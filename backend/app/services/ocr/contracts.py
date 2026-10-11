"""OCR 层共享契约。

``OcrResult`` 是三层链里"解析阶段"的统一产物:一份 markdown(可以带图片引用)
加上"这份 markdown 是谁产出的"的溯源标记。下游(结构化抽取)只认 markdown,
溯源标记用于排障与人工复核时判断可信度。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


class OcrError(RuntimeError):
    """OCR 层异常。message 会原样出现在用户可见的导入失败原因里,不要塞堆栈。"""


# 产出层级标记,顺序即优先级。
SOURCE_PADDLEOCR = "paddleocr"        # 第一层:PaddleOCR-VL 主解析
SOURCE_VISION_FALLBACK = "vision"     # 第二层:视觉大模型(Qwen3-VL 等)兜底转写
SOURCE_NONE = "none"                  # 两层都没产出可用内容


@dataclass
class OcrResult:
    """一次文档解析的产物。"""

    markdown: str = ""
    source: str = SOURCE_NONE
    page_count: int = 0
    # 逐页 markdown。保留分页是为了能做"页级合并"——第一层只吃下部分页面时,
    # 只对缺失/过短的页去调第二层,而不是整卷重跑。
    page_texts: list[str] = field(default_factory=list)
    # 第一层失败原因(若第二层顶上,这里保留第一层的失败信息,便于排障)。
    primary_error: Optional[str] = None
    # markdown 里引用的图片:包内相对路径 → 图片字节。落盘由调用方决定,
    # OCR 层不感知 /static/media 这类 URL 约定。
    assets: dict[str, bytes] = field(default_factory=dict)

    @property
    def page_lengths(self) -> list[int]:
        return [len(p or "") for p in self.page_texts]

    @property
    def empty(self) -> bool:
        return not self.markdown.strip()

    @property
    def usable(self) -> bool:
        """是否算"解析成功"。空内容、纯空白、纯图片挂载都算失败。"""
        return bool(self.markdown.strip())


def strip_markdown_noise(markdown: str) -> str:
    """去掉 OCR 常见噪声,用于质量判定与后续抽取。

    只做保守清洗:统一换行、去掉零宽字符、压缩 3 个以上连续空行。
    不删内容——排版风格交给下游,这里只保证"没有肉眼看不见的脏字符"。
    """
    if not markdown:
        return ""
    text = markdown.replace("\r\n", "\n").replace("\r", "\n")
    for ch in ("\u200b", "\ufeff", "\u200e", "\u200f"):
        text = text.replace(ch, "")
    while "\n\n\n\n" in text:
        text = text.replace("\n\n\n\n", "\n\n\n")
    return text.strip()
