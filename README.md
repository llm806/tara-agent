# Tara Agent

Tara Agent 是面向 Tara Oceans 数据的分析系统，支持自然语言问答、数据分析、结果展示和链路追溯。

## 环境要求

- Docker Desktop
- Python 3.12 或 3.13
- uv
- Node.js 22
- pnpm 10.12.4（与前端 `packageManager` 一致）

## 配置环境变量

在项目根目录打开 PowerShell，创建本地配置：

```powershell
Copy-Item .env.example .env
```

打开 `.env`：

1. 修改 `POSTGRES_PASSWORD`。
2. 将 `TARA_DATABASE_URL` 中的数据库密码改成相同的值。
3. 添加 DeepSeek API Key：

```text
DEEPSEEK_API_KEY=你的密钥
```

只需维护根目录这一份 `.env`。
若本机已有 `backend/.env`，其中同名配置会覆盖根目录配置；环境变量优先于这两份文件。
`.env` 和 API Key 保持本地，不提交到 Git。

## 准备数据集

GitHub 仓库不包含原始科学数据和预处理产物。运行完整分析需自行准备以下四份 TSV：

| 数据集文件 | 说明 |
| --- | --- |
| `context_general.tsv` | 样本位置、时间、深度和粒径等背景信息 |
| `context_stat.tsv` | 温度、营养盐等环境变量 |
| `TARA-Oceans_18S-V4_dada2_table.tsv` | 18S V4 分类、序列及各样本的 ASV read count |
| `TARA-Oceans_18S-V9_dada2_table.tsv` | 18S V9 分类、序列及各样本的 ASV read count |

在仓库根目录创建 `Tara_4_Core_Datasets/`，将四份文件直接放入该目录。
若文件为 `.tsv.gz`，先解压为上述 `.tsv`。保留原始文件名、表头和内容。
也可在根目录 `.env` 设置 `TARA_DATASET_DIR` 指向其他绝对路径。

首次初始化中的 `uv run tara-data` 会校验数据并生成 `backend/data/processed/` 下的
Parquet 和 `manifest.json`，记录源文件哈希、处理版本与样本覆盖率。
成功后再启动后端；缺少数据或校验失败时，完整分析功能不可用。
原始数据与派生产物均已被 Git 忽略，不要提交到仓库。更多处理约定见 [后端说明](backend/README.md)。

## 首次初始化

在项目根目录依次执行：

```powershell
docker compose up -d postgres
cd backend
uv sync --locked
uv run alembic upgrade head
uv run tara-data
cd ../frontend
pnpm install --frozen-lockfile
```

`uv run tara-data` 会生成后端使用的数据文件。原始数据没有变化时，不需要重复执行。

## 启动项目

打开三个 PowerShell 终端，分别执行以下命令。

### 数据库

在项目根目录：

```powershell
docker compose up -d postgres
```

### 后端

在项目根目录：

```powershell
cd backend
uv run fastapi dev
```

### 前端

在项目根目录：

```powershell
cd frontend
pnpm dev
```

启动完成后：

- 系统页面：`http://localhost:3000`
- 后端健康检查：`http://localhost:8000/api/v1/health`

前端默认通过同源 `/api` 代理连接本机后端 `8000` 端口，无需额外配置 API 地址。
访问页面后可注册账号或使用开发环境的游客入口。

## 开发验证

后端（在 `backend/` 中执行）：

```powershell
uv run ruff check .
uv run pytest -m "not integration"
```

前端（在 `frontend/` 中执行）：

```powershell
pnpm lint
pnpm typecheck
pnpm build
```

普通后端测试使用测试数据和模型替身，不需要完整 Tara 数据或真实模型 API Key。
运行集成测试时，在 `backend/` 执行 `uv run pytest -m integration`：
真实数据验收需要根目录的四份 TSV；数据库用例另需设置 `$env:TARA_RUN_DATABASE_TESTS="1"`，
并将 `TARA_DATABASE_URL` 指向已执行迁移的本地测试数据库。未满足条件的相应用例会跳过，
跳过不表示已通过验收。数据库测试会写入并清理测试记录。

拉取更新后按需重新同步依赖；存在新增数据库迁移时，先在 `backend/` 执行
`uv run alembic upgrade head`，再启动服务。共享环境已执行的迁移只追加新版本，不改写旧文件。
后端分层与数据契约见 [backend/README.md](backend/README.md)，前端职责见 [frontend/README.md](frontend/README.md)。

## 协同开发与 AOCI

开始开发前阅读 `AGENTS.md` 和 `tara-agent方案文档.md`。仓库共享 `aoci.txt`、
`aoci.meta.txt`、`aoci.code.txt`，以及 `.aoci/.gitignore`、`config.json` 和
`baseline.json`；可选的数据库基线与 Curation 决策按 `.aoci/.gitignore` 放行。
Ledger、草稿、事务、恢复记录和本机宿主配置保持本地。

本仓库当前使用 AOCI-CODE `v0.1.0-rc14`。新成员按
[官方安装说明](https://github.com/aoci-spec/aoci-code/blob/v0.1.0-rc14/docs/install.md)
安装并校验同版本二进制，然后在仓库根目录配置本机 Agent：

```powershell
# 将占位路径替换为本机已校验的 AOCI 二进制路径。
& '<本机 AOCI 二进制绝对路径>' --repo . init --agent codex
& '<本机 AOCI 二进制绝对路径>' --repo . verify --json
& '<本机 AOCI 二进制绝对路径>' --repo . check --json
```

使用其他宿主时按[官方集成说明](https://github.com/aoci-spec/aoci-code/blob/v0.1.0-rc14/docs/agent-integrations.md)
选择对应 Agent。宿主配置包含本机路径，不提交到 Git；当前会话未加载 MCP 时刷新或重新打开项目。
已有索引和基线随仓库克隆，遇到漂移时交给实时 Guide 处理，不用 `scan --force` 重建基线。

每次开发由 Agent 读取当前认知、调查源码并完成验证，最终通过 AOCI 维护受影响条目。
提交时一起审查代码、认知索引和基线差异；合并冲突后重新执行 Verify、Check 和 Guide，
不手工拼接基线哈希，也不覆盖另一成员尚未完成的事务。
