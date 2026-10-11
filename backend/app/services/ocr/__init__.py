"""文档 OCR 层:第一层 PaddleOCR-VL 主解析、第二层视觉大模型兜底。

三层模型方案里的"前线两人"都在这里:
- ``paddleocr.PaddleOcrClient``  : 百度飞桨 AI Studio 的异步 job OCR 接口(PaddleOCR-VL)
- ``vision_fallback``           : 兼容 OpenAI 协议的视觉模型(如 Qwen3-VL)兜底转写
- ``chain.DocumentOcrChain``    : 把两者编成"主 → 兜底"的降级链,并做产出质量校验

第三层(题目结构化 / 语义理解 / 分类归属)不在这里,由既有的
``app.services.ai_provider`` 文本路径承担。
"""

from .chain import DocumentOcrChain, OcrChainResult  # noqa: F401
from .contracts import OcrError, OcrResult  # noqa: F401

__all__ = ["DocumentOcrChain", "OcrChainResult", "OcrError", "OcrResult"]
