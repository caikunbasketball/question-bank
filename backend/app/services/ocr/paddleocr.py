"""第一层:百度飞桨 AI Studio 的 PaddleOCR-VL(异步 job 接口)客户端。

接口不是 OpenAI 协议,而是一个"提交 job → 轮询状态 → 取 jsonl 结果"的三段式:

    1. POST   {JOB_URL}           -> data.jobId
    2. GET    {JOB_URL}/{jobId}   -> data.state ∈ pending|running|done|failed
    3. GET    data.resultUrl.jsonUrl -> 每行一个 JSON,取 result.layoutParsingResults[].markdown

所以它没法挂进 ``AIProvider``(那个抽象只认 chat / embeddings),单独成类。

设计取舍:
- 单文件、无本地状态;重试由上层(导入任务)决定,这里只做"超时即失败"。
- 图片资源默认下载:题库里的题干图不能是外链(会过期),下载后交给调用方落盘。
- 任何 HTTP/JSON 异常统一转 ``OcrError``,message 里带状态码与响应片段,便于排障。
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import posixpath
from pathlib import PurePosixPath
from typing import Any, Optional

from .contracts import OcrError, OcrResult, strip_markdown_noise

logger = logging.getLogger(__name__)

DEFAULT_JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
DEFAULT_MODEL = "PaddleOCR-VL-1.6"

# 单次 HTTP 请求超时(提交 / 轮询 / 下载),与整体轮询预算分开算。
_HTTP_TIMEOUT_S = 60.0
# 单个资源(图片)下载超时。
_ASSET_TIMEOUT_S = 30.0
# 资源数量与体积上限,防止异常响应把磁盘打满。
_MAX_ASSETS = 500
_MAX_ASSET_BYTES = 20 * 1024 * 1024
# 轮询失败容忍次数(网络抖动不直接判死)。
_MAX_POLL_ERRORS = 5


class PaddleOcrClient:
    """PaddleOCR-VL 异步 job 接口封装。只用 ``parse()`` 一个入口。"""

    def __init__(
        self,
        *,
        token: str,
        model: str = DEFAULT_MODEL,
        job_url: str = DEFAULT_JOB_URL,
        poll_interval: float = 5.0,
        timeout_s: float = 900.0,
        use_doc_orientation_classify: bool = False,
        use_doc_unwarping: bool = False,
        use_chart_recognition: bool = False,
        download_assets: bool = True,
    ) -> None:
        if not token:
            raise OcrError("PaddleOCR 未配置访问令牌(Access Token)")
        self.token = token
        self.model = model or DEFAULT_MODEL
        # base_url 允许只配域名,这里统一补到 jobs 端点。
        self.job_url = _normalize_job_url(job_url)
        self.poll_interval = max(1.0, float(poll_interval))
        self.timeout_s = max(30.0, float(timeout_s))
        self.optional_payload = {
            "useDocOrientationClassify": bool(use_doc_orientation_classify),
            "useDocUnwarping": bool(use_doc_unwarping),
            "useChartRecognition": bool(use_chart_recognition),
        }
        self.download_assets = download_assets

    # ------------------------------------------------------------------ #
    # 公开入口
    # ------------------------------------------------------------------ #

    async def parse(
        self,
        *,
        data: Optional[bytes] = None,
        filename: Optional[str] = None,
        file_url: Optional[str] = None,
    ) -> OcrResult:
        """解析一个本地文件(``data``)或远端 URL(``file_url``)。二者择一。"""
        if not data and not file_url:
            raise OcrError("PaddleOCR 需要提供文件内容或文件 URL")

        async with _client() as http:
            job_id = await self._submit(http, data=data, filename=filename, file_url=file_url)
            jsonl_url = await self._wait_for_result(http, job_id)
            return await self._collect(http, jsonl_url, job_id)

    # ------------------------------------------------------------------ #
    # 三段式实现
    # ------------------------------------------------------------------ #

    async def _submit(
        self,
        http: "Any",
        *,
        data: Optional[bytes],
        filename: Optional[str],
        file_url: Optional[str],
    ) -> str:
        headers = {"Authorization": f"bearer {self.token}"}

        if file_url:
            headers["Content-Type"] = "application/json"
            payload = {
                "fileUrl": file_url,
                "model": self.model,
                "optionalPayload": self.optional_payload,
            }
            resp = await http.post(self.job_url, json=payload, headers=headers)
        else:
            # multipart:optionalPayload 必须序列化成字符串。
            form = {"model": self.model, "optionalPayload": json.dumps(self.optional_payload)}
            name = filename or "upload.bin"
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            files = {"file": (name, data, ctype)}
            resp = await http.post(self.job_url, headers=headers, data=form, files=files)

        body = _safe_json(resp)
        if resp.status_code != 200:
            raise OcrError(_http_error("提交 OCR 任务失败", resp, body))

        job_id = (body or {}).get("data", {}).get("jobId")
        if not job_id:
            raise OcrError(f"提交 OCR 任务未返回 jobId,响应片段: {_snippet(body)}")
        logger.info("PaddleOCR job submitted: %s (model=%s)", job_id, self.model)
        return job_id

    async def _wait_for_result(self, http: "Any", job_id: str) -> str:
        headers = {"Authorization": f"bearer {self.token}"}
        url = f"{self.job_url}/{job_id}"
        deadline = asyncio.get_running_loop().time() + self.timeout_s
        errors = 0
        last_progress = ""

        while True:
            if asyncio.get_running_loop().time() > deadline:
                raise OcrError(
                    f"OCR 任务超时(>{int(self.timeout_s)}s),job={job_id}"
                    + (f",最后进度:{last_progress}" if last_progress else "")
                )

            try:
                resp = await http.get(url, headers=headers)
            except Exception as exc:  # noqa: BLE001 - 网络抖动允许有限重试
                errors += 1
                if errors > _MAX_POLL_ERRORS:
                    raise OcrError(f"轮询 OCR 任务连续失败 {errors} 次: {exc}") from exc
                await asyncio.sleep(self.poll_interval)
                continue

            body = _safe_json(resp)
            if resp.status_code != 200:
                raise OcrError(_http_error("查询 OCR 任务失败", resp, body))

            payload = (body or {}).get("data", {}) or {}
            state = payload.get("state")

            if state == "done":
                jsonl_url = (payload.get("resultUrl") or {}).get("jsonUrl")
                if not jsonl_url:
                    raise OcrError(f"OCR 任务已完成但未返回结果地址,job={job_id}")
                logger.info("PaddleOCR job done: %s", job_id)
                return jsonl_url

            if state == "failed":
                raise OcrError(f"OCR 任务失败: {payload.get('errorMsg') or '未提供失败原因'}")

            # pending / running / 未知态:记录进度后继续等。
            progress = payload.get("extractProgress") or {}
            if progress:
                last_progress = (
                    f"{progress.get('extractedPages', '?')}/{progress.get('totalPages', '?')} 页"
                )
            await asyncio.sleep(self.poll_interval)

    async def _collect(self, http: "Any", jsonl_url: str, job_id: str) -> OcrResult:
        try:
            resp = await http.get(jsonl_url)
        except Exception as exc:  # noqa: BLE001
            raise OcrError(f"下载 OCR 结果失败: {exc}") from exc
        if resp.status_code != 200:
            raise OcrError(f"下载 OCR 结果失败: HTTP {resp.status_code}")

        pages: list[str] = []
        assets: dict[str, bytes] = {}
        asset_urls: dict[str, str] = {}

        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("PaddleOCR 结果存在无法解析的行,job=%s", job_id)
                continue

            result = (record or {}).get("result") or {}
            for page in result.get("layoutParsingResults") or []:
                markdown = (page.get("markdown") or {})
                text = strip_markdown_noise(markdown.get("text") or "")
                pages.append(text)
                for rel_path, url in (markdown.get("images") or {}).items():
                    if isinstance(url, str) and rel_path not in asset_urls:
                        asset_urls[rel_path] = url

        markdown = "\n\n".join(p for p in pages if p)

        if self.download_assets and asset_urls:
            assets = await self._download_assets(http, asset_urls, job_id)

        return OcrResult(
            markdown=markdown,
            source="paddleocr",
            page_count=len(pages),
            page_texts=pages,
            assets=assets,
        )

    async def _download_assets(
        self, http: "Any", asset_urls: dict[str, str], job_id: str
    ) -> dict[str, bytes]:
        assets: dict[str, bytes] = {}
        for rel_path, url in list(asset_urls.items())[:_MAX_ASSETS]:
            try:
                resp = await http.get(url)
                if resp.status_code != 200:
                    logger.warning("OCR 图片下载失败(%s): HTTP %s", rel_path, resp.status_code)
                    continue
                if len(resp.content) > _MAX_ASSET_BYTES:
                    logger.warning("OCR 图片超过体积上限,跳过: %s", rel_path)
                    continue
                assets[_safe_relpath(rel_path)] = resp.content
            except Exception as exc:  # noqa: BLE001 - 单张图失败不影响整卷
                logger.warning("OCR 图片下载异常(%s): %s", rel_path, exc)
        logger.info("PaddleOCR 下载图片 %d 张,job=%s", len(assets), job_id)
        return assets


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _client() -> "Any":
    """延迟导入 httpx,缺依赖时给出可执行的修复提示。"""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - 依赖缺失属环境问题
        raise OcrError("缺少 httpx 依赖,请在后端目录执行 uv sync 后重试") from exc
    return httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, follow_redirects=True)


def _normalize_job_url(base_url: Optional[str]) -> str:
    """把用户在界面上填的 Base URL 归一到 jobs 端点。

    容忍三种写法:完整 jobs 端点、只到 ``/api/v2``、只给域名——都收敛到同一处,
    避免因尾斜杠或路径层级差一层而拼出 404。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return DEFAULT_JOB_URL
    if url.endswith("/ocr/jobs"):
        return url
    if "/api/v2" in url:
        base = url[: url.index("/api/v2") + len("/api/v2")]
        return f"{base}/ocr/jobs"
    return DEFAULT_JOB_URL


def _safe_relpath(rel_path: str) -> str:
    """防 zip-slip:把资源路径压成纯相对路径,去掉 ../ 与绝对前缀。"""
    cleaned = posixpath.normpath(rel_path.replace("\\", "/")).lstrip("/")
    parts = [p for p in PurePosixPath(cleaned).parts if p not in ("..", ".")]
    return "/".join(parts) or "asset"


def _safe_json(resp: "Any") -> Optional[dict]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _snippet(body: Optional[dict], limit: int = 300) -> str:
    if body is None:
        return "<非 JSON 响应>"
    text = json.dumps(body, ensure_ascii=False)
    return text[:limit]


def _http_error(prefix: str, resp: "Any", body: Optional[dict]) -> str:
    hint = ""
    if resp.status_code in (401, 403):
        hint = "(访问令牌无效或已过期,请检查百度 AI Studio Access Token)"
    return f"{prefix}: HTTP {resp.status_code}{hint} {_snippet(body)}"
