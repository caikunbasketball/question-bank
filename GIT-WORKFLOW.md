# GIT-WORKFLOW.md —— 本仓库 Git 管理规范

> **适用对象**：在本机操作 QuestionBank 仓库的所有人与自动化任务。
> **效力**：本文件是提交/推送行为的唯一裁决依据。规则一旦生效即严格执行；需要调整时**先改本文件，再按新规则执行**。
> **最后更新**：2026-10-11

---

## 一、仓库拓扑

本机只有**一份** git 仓库，通过 worktree 分出**三个**工作副本，共用同一个 `.git`。

| 工作副本 | 绝对路径 | 分支 | 用途 |
| --- | --- | --- | --- |
| 主仓库 | `C:/Users/26532/Documents/QuestionBank` | `main` | 长期主工作区，含 `_maintenance/`、`启动题库系统.bat` 等本机专用文件 |
| WB 副本 A | `C:/Users/26532/WorkBuddy/Worktrees/QuestionBank/main-1b4da262` | `workbuddy/main-1b4da262` | WorkBuddy 会话工作区 |
| WB 副本 B | `C:/Users/26532/WorkBuddy/Worktrees/QuestionBank/main-a676f316` | `workbuddy/main-a676f316` | WorkBuddy 会话工作区 |

远程仓库：

| 远程名 | 地址 | 权限 |
| --- | --- | --- |
| `origin` | `git@github.com:caikunbasketball/question-bank.git`（SSH） | 自己的 fork，**允许推送** |
| `upstream` | `https://github.com/gygy-open/question-bank.git` | 原项目，**只读，永不推送** |

**推论**：提交发生在任一副本，都进同一个仓库对象库；但**分支是各自的**，副本 A 的提交不会自动出现在副本 B 或 `main` 上。

---

## 二、分支策略

1. 每个工作副本在**自己的分支**上提交，不直接在 `main` 上开发。
2. 分支命名：`workbuddy/<副本标识>`（由 WorkBuddy 自动创建，保持现状即可）。
3. 合入 `main` 属于**人工决策**，自动化任务**不执行** merge / rebase / PR 创建。
4. **禁止**在 `main` 上直接提交（主仓库若出现改动，先在主仓库切分支或交由人工处理，自动化仅报告）。

---

## 三、提交信息规范

采用 [Conventional Commits](https://www.conventionalcommits.org/)，**type 用英文、描述用中文**（与仓库既有历史保持一致）。

| type | 用途 |
| --- | --- |
| `feat` | 新功能 |
| `fix` | 缺陷修复 |
| `refactor` | 重构（不改变外部行为） |
| `docs` | 文档 |
| `chore` | 构建、依赖、配置、杂项 |
| `style` | 纯格式调整 |
| `test` | 测试 |
| `perf` | 性能优化 |

格式：`<type>(<scope>): <中文描述>`，scope 可选（如 `frontend`、`import`、`media`、`git`）。

示例：

- `feat(git): 建立 git 管理规范与自动化流程`
- `fix(frontend): 修复知识点树递归歧义`
- `chore: 忽略 WorkBuddy 会话数据目录`

**拆分原则**：一个提交只做一件事。互不相关的改动禁止揉进同一个提交；禁止 `git add -A` 一把梭。

---

## 四、红线（禁止事项）

| # | 禁止行为 | 原因 |
| --- | --- | --- |
| 1 | 向 `upstream` 推送任何内容 | 无权限且非本仓库维护者 |
| 2 | `git push --force` / `--force-with-lease` | 除非陈老师当场明确授权 |
| 3 | 自动 `merge` / `rebase` upstream 到本地分支 | 冲突需人工判断 |
| 4 | 提交 `.env`（`.env.example` 除外）、`*.db`/`*.sqlite*`、`chromadb_data/`、`backend/uploads/`、`backend/static/media/`、`node_modules/`、`.venv/`、`_maintenance/`、`.workbuddy/` | 敏感信息 / 运行数据 / 本机专用 |
| 5 | 提交任何密钥、令牌、密码、SSH 私钥 | 泄露不可逆 |
| 6 | `git reset --hard`、`git clean -fd`、`git checkout --` 丢弃未提交内容 | 不可恢复 |
| 7 | 提交单文件 > 50 MB | 污染历史，难以回退 |
| 8 | 改写已推送的历史 | 影响他人 |

发现红线候选文件时：**不提交，写进报告交人工处置**。

---

## 五、自动化执行流程（每次运行按此顺序）

操作对象：上表**三个工作副本全部扫描**。

### 步骤 1 —— 侦察

对每个副本执行 `git -C <路径> status --porcelain --branch`，记录：当前分支、已跟踪改动、未跟踪文件。

- 三个副本全部干净 → 输出「无待提交改动」并结束。
- 有改动 → 进入步骤 2。

### 步骤 2 —— 分流

把改动分成三类：

| 类别 | 处理 |
| --- | --- |
| 应提交（源码、文档、配置、测试） | 进入校验 |
| 应忽略（落入 `.gitignore` 的运行数据、临时文件） | 不动，仅在报告中说明 |
| 可疑（疑似密钥、超大文件、垃圾文件） | **不提交**，列入「待人工处置」 |

### 步骤 3 —— 校验（按改动范围触发，跳过必须标注）

| 改动路径 | 校验命令 | 跳过条件 |
| --- | --- | --- |
| `backend/` | `cd backend && uv run pytest -q` | 无 `.venv` 或依赖未安装 |
| `frontend/` | `cd frontend && pnpm test` | 无 `node_modules/` |

校验失败 → **不提交该部分改动**，保留现场，报告中说明失败详情。
校验跳过 → 允许提交，但报告中必须写明「校验未执行（原因：…）」。

### 步骤 4 —— 提交

按逻辑单元逐个暂存并提交（`git add <具体文件>`，禁止 `git add -A`），提交信息按第三节规范。

### 步骤 5 —— 推送

```bash
git -C <路径> push origin HEAD
```

- 仅推 `origin`，目标为当前分支同名远程分支。
- 失败时重试最多 **3** 次（间隔 5s / 15s / 30s）。本机网络存在偶发丢包（`Connection closed by 198.18.0.10`），首次失败多为抖动，重试通常可恢复。
- 3 次仍失败 → **保留本地提交**（已提交内容不丢），在报告中标明「推送失败，待下次重试」。

### 步骤 6 —— 上游侦察（只读）

```bash
git -C <路径> fetch upstream
git -C <路径> rev-list --left-right --count HEAD...upstream/main
```

仅**报告**本地领先/落后数量。**不合并、不 rebase**。落后 > 0 时在报告中提示「上游有 N 个新提交，待人工评估」。

### 步骤 7 —— 报告

输出固定结构报告：

1. 各副本分支与状态
2. 本次提交列表（哈希 + 提交信息）
3. 推送结果（成功 / 重试次数 / 失败原因）
4. 跳过的校验及原因
5. 上游差异数量
6. **待人工处置**清单（红线候选、冲突、失败推送）

---

## 六、异常处置

| 情形 | 处置 |
| --- | --- |
| 推送被拒（non-fast-forward） | 停止推送，报告中说明；**不自动 pull/merge** |
| 出现冲突 | 停止，保留现场，报告冲突文件清单 |
| 发现疑似密钥 | 停止该部分提交，报告中高亮，人工确认后再处理 |
| 网络持续不可达 | 完成本地提交，推送留待下次；连续 3 次任务均失败则报告中升级提示 |
| 校验失败 | 不提交该部分，报告失败用例 |

---

## 七、人工保留动作

以下动作**只由陈老师决策**，自动化不代劳：

- 把 `workbuddy/*` 分支合入 `main`
- 从 `upstream` 同步更新（merge / rebase / cherry-pick）
- 向 `upstream` 提交 PR
- 任何 `--force` 推送
- 删除分支、回滚已推送的提交

---

## 八、配套的自动化任务

| 项目 | 内容 |
| --- | --- |
| 名称 | QuestionBank 仓库自动 git 管理 |
| 频率 | 工作日（周一至周五）21:30 |
| 范围 | 三个工作副本全部扫描 |
| 动作 | 校验 → 分类提交 → 推送 `origin` → 侦察 `upstream` → 出报告 |
| 边界 | 只推自己的 fork；不碰 `upstream`；不做合并 |
| 修改方式 | 在本对话里说明变更点，或直接改本文件第八节 |

---

## 九、本机环境备忘

| 项 | 值 |
| --- | --- |
| git 用户 | `2653268235` / `2653268235@qq.com`（全局） |
| SSH 密钥 | `~/.ssh/id_ed25519`（`~/.ssh/config` 已固定 Host github.com） |
| 凭据助手 | `manager`（全局） |
| 全局 URL 改写 | `url.git@github.com:.insteadOf https://github.com/` |
| 已知网络现象 | 偶发 `Connection closed by 198.18.0.10 port 22`（本机代理 fake-IP），表现为一次性失败，重试即恢复 |
| 后端工具链 | `uv` 0.12.5，Python 3.13+，测试 `uv run pytest` |
| 前端工具链 | `pnpm` 9.15.1，Node 22，测试 `pnpm test`（vitest） |
| 换行符 | `.gitattributes` 设定 `* text=auto eol=lf` |
