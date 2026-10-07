# Tara Agent

Tara Agent 面向 Tara Oceans 数据，支持自然语言问答、数据分析、表格与图表展示，以及分析过程查看。

本文说明本地开发流程。命令使用 PowerShell；“项目根目录”指包含本文件的 `tara-agent/` 目录。

需要一条命令启动前端、后端、数据库和网站入口时，按 [Docker 部署指南](deploy/README.md)操作。该方式使用正式构建，适合验证部署；下面的流程适合日常修改代码。

## 1. 首次复现：准备环境和数据

新电脑或新克隆的项目按本节依次操作，完成后进入第 2 节启动项目。

### 1.1 安装工具

- Docker Desktop：运行 PostgreSQL 数据库，执行后续命令前先打开它。
- Python 3.12 或 3.13、uv：运行后端和安装 Python 依赖。
- Node.js 22、pnpm 10.12.4：运行前端和安装前端依赖。

### 1.2 创建本地配置

在项目根目录执行。已有 `.env` 时跳过，保留自己的配置。

```powershell
# 将配置示例复制为本机使用的配置文件。
Copy-Item .env.example .env
```

打开根目录 `.env`，完成两项配置：

1. 将 `POSTGRES_PASSWORD` 换成自己的数据库密码，同时替换 `TARA_DATABASE_URL` 中的密码，保持一致。
2. 添加模型服务密钥：

```text
DEEPSEEK_API_KEY=你的密钥
```

本地运行只需这份 `.env`，无需创建前端配置文件。若已有 `backend/.env`，其中同名配置会覆盖根目录配置；终端环境变量优先于两份文件。配置文件和密钥不提交到 Git。

### 1.3 放置数据集

使用团队校内数据服务时，科学数据保留在服务器，不需要在本机放置这四份 TSV，也跳过下一节的 `uv run tara-data`。先完成数据库、依赖初始化，再按第 2.4 节配置远程连接。

GitHub 仓库不包含科学数据文件，克隆代码后需自行准备以下四份 TSV：

| 数据集文件 | 内容 |
| --- | --- |
| `context_general.tsv` | 样本位置、采样时间、深度和粒径等信息 |
| `context_stat.tsv` | 温度、营养盐等环境信息 |
| `TARA-Oceans_18S-V4_dada2_table.tsv` | 18S V4 生物分类、序列及各样本的测序读数 |
| `TARA-Oceans_18S-V9_dada2_table.tsv` | 18S V9 生物分类、序列及各样本的测序读数 |

在项目根目录创建 `Tara_4_Core_Datasets/`，将四份文件直接放入其中。若文件为 `.tsv.gz`，先解压为上述 `.tsv`；保留原始文件名、表头和内容。

数据放在其他位置时，在根目录 `.env` 中设置 `TARA_DATASET_DIR=数据文件夹的绝对路径`。原始数据和处理后的文件均不提交到 Git。

### 1.4 初始化数据库和后端

在项目根目录依次执行，每条命令成功后再执行下一条：

```powershell
# 在后台启动数据库；首次运行会下载镜像并创建数据库。
# --wait 等待数据库健康检查通过后返回，再进行下面的建表操作。
docker compose up -d --wait postgres

# 进入后端目录，后续 uv 命令均在这里执行。
cd backend

# 按 uv.lock 中的固定版本安装后端依赖。
uv sync --locked

# 创建数据库表，并应用仓库中已有的数据库结构变更。
uv run alembic upgrade head

# 本机持有科学数据时执行；使用第 2.4 节远程服务时跳过此命令。
uv run tara-data
```

本机数据处理结果默认保存在 `backend/data/processed/`，包括 Parquet 数据文件和记录来源、处理版本的 `manifest.json`。本机模式须完成数据处理；远程模式须配置连接并保持服务器服务及 SSH 隧道可用。

### 1.5 安装前端依赖

在同一个终端接着执行：

```powershell
# 从 backend 目录切换到 frontend 目录。
cd ../frontend

# 按 pnpm-lock.yaml 中的固定版本安装前端依赖。
pnpm install --frozen-lockfile
```

初始化完成。首次运行和之后的日常开发都按下一节启动。

## 2. 日常开发：启动项目

打开 Docker Desktop。依赖、数据库结构和数据没有变化时，直接执行本节，不需要重复初始化。

### 2.1 启动数据库

在项目根目录执行；数据库已在运行时可跳过。命令返回后即可关闭这个终端。

```powershell
# 在后台启动数据库，并等待它可以接受连接；保留已有数据库内容。
docker compose up -d --wait postgres
```

### 2.2 启动后端

打开一个终端，从项目根目录执行：

```powershell
# 进入后端目录。
cd backend

# 启动后端开发服务，默认端口为 8000；修改代码后自动重新加载。
uv run fastapi dev
```

### 2.3 启动前端

另开一个终端，从项目根目录执行：

```powershell
# 进入前端目录。
cd frontend

# 启动前端开发服务，默认端口为 3000；修改代码后自动更新页面。
pnpm dev
```

保持前后端终端运行，打开 `http://localhost:3000`，注册账号或使用开发环境的游客入口。前端默认连接本机后端，无需额外配置 API 地址。

后端状态可在 `http://localhost:8000/api/v1/health` 查看。结束开发时，在前后端终端分别按 `Ctrl+C` 停止服务。

### 2.4 使用校内数据服务：团队日常操作

服务器运行科学数据服务，开发者本机运行数据库、后端和前端。服务器地址、账号、实际目录和凭据由维护者私下提供，不写入共享文档或 Git。

**服务端：** 维护者先配置运行环境中的 `TARA_DATASET_DIR`、`TARA_PROCESSED_DATA_DIR` 和 `TARA_DATA_SERVICE_TOKEN`（至少 32 字符）；启用 MATOU 时另设置 `TARA_MATOU_DATA_DIR`。在服务器代码的 `backend/` 目录启动：

```bash
uv run --locked python -m uvicorn tara_agent.data_service.app:create_app --factory --host 127.0.0.1 --port 8011 --workers 1
```

服务已运行时无需重复启动。此命令前台运行，保持终端开启；按 `Ctrl+C` 停止，未配置开机自启。已有部署按维护者的私有配置运行。

**本机首次配置：** 向维护者获取凭据文件，保存在用户目录；在 `backend/.env` 配置下列两项，保留原有数据库、模型和认证配置：

```dotenv
TARA_DATA_SERVICE_URL=http://127.0.0.1:18011
TARA_DATA_SERVICE_TOKEN_FILE=~/.tara-data-service.token
```

本机无需科学数据或 `TARA_MATOU_DATA_DIR`。凭据和 `.env` 不提交；修改配置后重启后端。

**每日启动：** 用维护者提供的 SSH 配置建立隧道，将下方占位符替换为自己的 SSH 连接别名，保持窗口运行：

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18011:127.0.0.1:8011 '<SSH连接别名>'
```

随后按第 2.1–2.3 节启动本机应用，不需要重新上传或处理科学数据。端口以团队私下约定为准，变更时同步调整隧道和服务 URL。

**检查与停止：** 查看本机 `/api/v1/ready`，HTTP 200 表示就绪。结束开发时，在本机前后端和隧道窗口按 `Ctrl+C`；共享服务由维护者管理。科学代码、计算依赖或数据清单更新后，同步服务器版本并重启两端后端。首次接入与升级验收见[后端说明](backend/README.md#校内独立数据服务)。本机 API 端口变更后的前端配置见[前端说明](frontend/README.md)，页面与 API 统一使用 `localhost`。

## 3. 拉取更新后：只处理发生变化的部分

先停止前后端服务，根据本次更新执行对应操作，然后按第 2 节重新启动。数据库结构更新前需确保数据库正在运行。

| 更新内容 | 执行目录 | 命令与用途 |
| --- | --- | --- |
| 后端依赖或 `uv.lock` 变化 | `backend/` | `uv sync --locked`：安装更新后的后端依赖 |
| 前端依赖或 `pnpm-lock.yaml` 变化 | `frontend/` | `pnpm install --frozen-lockfile`：安装更新后的前端依赖 |
| 新增数据库迁移文件 | `backend/` | `uv run alembic upgrade head`：应用新的数据库结构变更 |
| 原始数据变化或处理结果丢失 | `backend/` | `uv run tara-data`：检查并生成所需数据文件 |
| 修改预处理代码后需要重新生成结果 | `backend/` | `uv run tara-data --force`：即使原始数据未变，也重新处理 |

只有普通业务代码变化时，直接重新启动即可。团队已经执行过的数据库迁移文件不改写，需要改变数据库结构时新增迁移文件。

## 4. 修改代码后：检查改动

检查自己修改的部分；这些命令用于验证代码，不是启动项目的步骤。

后端：在 `backend/` 执行。

```powershell
# 检查 Python 代码规范和常见错误。
uv run ruff check .

# 运行普通后端测试，不需要完整数据集或真实模型密钥。
uv run pytest -m "not integration"
```

前端：在 `frontend/` 执行。

```powershell
# 检查前端代码规范和常见错误。
pnpm lint

# 检查 TypeScript 类型是否一致。
pnpm typecheck

# 检查前端能否完成生产构建。
pnpm build
```

修改数据处理、数据库或完整分析流程时，还需在 `backend/` 执行 `uv run pytest -m integration`，验证真实数据和数据库相关行为。真实数据测试要求四份 TSV 位于项目根目录的 `Tara_4_Core_Datasets/`。数据库测试另需将 `TARA_DATABASE_URL` 指向已完成迁移的本地测试数据库，并设置 `$env:TARA_RUN_DATABASE_TESTS="1"` 来开启；测试会写入并清理测试记录。条件不满足时相应用例会跳过，跳过不代表验证通过。

## 5. 协同开发约定

开始修改前阅读 [开发指南](AGENTS.md) 和 [项目方案](tara-agent方案文档.md)。后端结构与数据处理细节见 [后端说明](backend/README.md)，前端展示职责见 [前端说明](frontend/README.md)。
