# AI 供应商与模型

AI 相关配置（供应商、模型、API Key）**存储在数据库中**，通过界面管理，可**热切换**、无需重启。此页功能为**超级管理员专属**。

## 三层模型方案（推荐）

图片 / PDF 的解析与抽题按**三层**分工，各层职责单一、可独立更换：

| 层级 | 职责 | 建议模型 | 激活槽 |
|------|------|---------|--------|
| 第一层 | **主 OCR 文档解析**：把版面转成 Markdown（公式、表格） | `PaddleOCR-VL-1.6`（百度飞桨 AI Studio） | `AI_OCR_MODEL_ID` |
| 第二层 | **识别失败或不完整时补充**：一级漏页/空页时补位转写 | `Qwen/Qwen3-VL-32B-Instruct`（硅基流动） | `AI_VISION_MODEL_ID` |
| 第三层 | **题目结构化、语义理解、分类归属** | `deepseek-flash`（DeepSeek 官方） | `AI_TEXT_MODEL_ID` |

数据流：

```
图片 / PDF
   │
   ▼
① PaddleOCR-VL ──► 产出质量校验
   │  失败 / 部分页为空 / 页数对不上
   ▼
② Qwen3-VL ──► 页级合并（只补缺失的页）
   │
   ▼
  Markdown
   │
   ▼
③ DeepSeek ──► 结构化题目 + 知识点分类
```

::: tip 降级是自动的
任何一层缺失都不会让导入失败：没配第一层就直接用第二层；一级只吃下部分页面时，
**只对缺失页**调用第二层（省钱，也不会覆盖已经准确的页面）。只有两层都拿不到内容才报错。
:::

### 一键写入三层配置

在三层凭证写进 `backend/.env`（该文件已被 `.gitignore` 忽略）后执行：

```bash
cd backend
uv run python scripts/seed_llm_tiers.py
```

```ini
# backend/.env
PADDLEOCR_AISTUDIO_TOKEN=...   # 百度飞桨 AI Studio Access Token
SILICONFLOW_API_KEY=...        # 硅基流动
DEEPSEEK_API_KEY=...           # DeepSeek 官方
```

脚本幂等，可重复执行；也可用 `--ocr-token / --vision-key / --text-key` 临时传入，
或 `--list` 只查看当前配置。

## 供应商（Provider）

一个供应商代表一个 AI 服务来源。字段：

| 字段 | 说明 |
|------|------|
| 名称 | 显示名（如「官方 OpenAI」） |
| 接口类型 | `openai` / `gemini` / `paddleocr` |
| Base URL | 自定义 API 地址 |
| API Key | 密钥 |
| 是否启用 | 开关 |

三种接口类型：

| 接口类型 | 协议 | 典型用途 |
|---------|------|---------|
| `openai` | OpenAI 兼容（Chat Completions） | 文本模型、视觉模型；覆盖 DeepSeek、硅基流动、通义、Kimi、私有部署等 |
| `gemini` | Google Gemini 原生 | Gemini 系列 |
| `paddleocr` | 百度飞桨 AI Studio 的**异步 job 接口**，非 OpenAI 协议 | **仅**用于「文档 OCR 主解析」槽位 |

::: warning `paddleocr` 不能当对话模型用
它是三段式异步任务接口（提交 job → 轮询 → 取结果），与 Chat Completions 不兼容。
若把它选成文本或视觉模型，系统会直接报错而不是静默退化。
:::

## 模型（Model）

每个供应商下可配置多个模型。字段：

| 字段 | 说明 |
|------|------|
| 名称 | 模型标识（如 `gpt-4o`、`deepseek-flash`、`Qwen/Qwen3-VL-32B-Instruct`） |
| 支持视觉 | `is_vision_capable`，可处理图片输入 |
| 用于向量 | `is_embedding_model`，用作 Embedding 向量化 |

## 激活模型

系统区分四类「激活模型」，分别对应不同功能：

| 用途 | 系统参数键 | 影响功能 |
|------|-----------|---------|
| 文档 OCR 主解析 | `AI_OCR_MODEL_ID` | 图片 / PDF 导入的**第一层**（须为 `paddleocr` 协议供应商的模型） |
| 视觉兜底转写 | `AI_VISION_MODEL_ID` | 图片 / PDF 导入的**第二层**；含图对话 |
| 文本 | `AI_TEXT_MODEL_ID` | **第三层**：题目结构化、分类归属；[AI 对话](/features/chat)、文本类导入抽取 |
| 向量 | `AI_EMBEDDING_MODEL_ID` | [知识点 RAG](/features/knowledge-points) |

在界面「高级：各场景模型分配」中为每类选择对应模型并保存，即刻生效。

::: tip 建议配置
- **文档 OCR 主解析**：图片与 PDF 解析质量的地基，优先接入。
- **视觉兜底**：需勾选「支持视觉」。
- **文本模型**：用于抽取与对话，建议选能力较强的通用模型。
- **Embedding 模型**：知识点检索必需，需勾选「用于向量」。
:::

## 安全提示

- API Key 属敏感信息，请妥善保管，避免泄露。**不要把明文密钥提交进仓库**；
  一键写入脚本读取的 `backend/.env` 已在 `.gitignore` 中忽略。
- 更换 Embedding 模型后，知识点会按新模型重新向量化。
