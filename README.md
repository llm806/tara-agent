# Tara Agent

Tara Agent 面向 Tara Oceans 数据，支持自然语言问答、数据分析、表格与图表展示，以及分析过程查看。

当前团队开发方式：**科学数据和计算服务部署在校内服务器，本机只运行 PostgreSQL、后端和前端，通过 SSH 隧道调用数据服务。本机不放置数据集，也不运行数据预处理。**

本机命令使用 PowerShell，服务器命令使用 Bash；“项目根目录”指包含本文件的 `tara-agent/` 目录。服务器地址、账号、SSH 别名和数据服务凭据向维护者获取，不写入共享文档或 Git。访问服务器前，先连接校内网络或学校提供的 VPN。

## 1. 首次配置

### 1.1 安装工具

- Docker Desktop：运行本机 PostgreSQL，执行命令前先打开。
- Python 3.12 或 3.13、uv：运行后端。
- Node.js 22、pnpm 10.12.4：运行前端。
- OpenSSH 客户端：建立数据服务隧道。

### 1.2 配置数据库、模型和数据服务

在项目根目录执行；已有 `.env` 时保留原配置，不要覆盖：

```powershell
Copy-Item .env.example .env
```

编辑根目录 `.env`：将 `POSTGRES_PASSWORD` 和 `TARA_DATABASE_URL` 中的密码改成同一个自定义密码，再添加：

```dotenv
DEEPSEEK_API_KEY=你的模型密钥
TARA_DATA_SERVICE_URL=http://127.0.0.1:18011
TARA_DATA_SERVICE_TOKEN_FILE=~/.tara-data-service.token
```

将维护者提供的凭据文件保存为用户目录下的 `.tara-data-service.token`，文件内容为数据服务令牌。使用维护者提供的 SSH 配置，确认可以通过连接别名登录服务器。下文的 `18011` 为本机隧道端口，`8011` 为服务器数据服务端口；实际端口不同时同步调整 URL 和隧道命令。

本地开发无需配置 `TARA_DATASET_DIR`、`TARA_PROCESSED_DATA_DIR` 或 `TARA_MATOU_DATA_DIR`。若已有 `backend/.env`，同名配置会覆盖根目录 `.env`；终端环境变量优先于两份文件。密钥、凭据和 `.env` 不提交到 Git。

### 1.3 初始化数据库和依赖

在项目根目录依次执行，每条成功后再执行下一条：

```powershell
docker compose up -d --wait postgres
cd backend
uv sync --locked
uv run alembic upgrade head
cd ../frontend
pnpm install --frozen-lockfile
```

以上仅首次执行；本机无需下载 TSV、MATOU 或派生数据，也无需执行 `uv run tara-data`。

## 2. 日常开发：启动项目

前提：Docker Desktop 已打开，维护者已启动服务器数据服务。下面各终端均从项目根目录开始。

### 2.1 建立 SSH 隧道（终端一）

将占位符替换为维护者提供的 SSH 连接别名，保持终端运行：

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18011:127.0.0.1:8011 '<SSH连接别名>'
```

成功后终端通常没有输出。隧道关闭后，本地应用将无法访问科学数据服务。

### 2.2 启动数据库和后端（终端二）

```powershell
docker compose up -d --wait postgres
cd backend
uv run fastapi dev
```

后端默认监听 `http://localhost:8000`，修改代码后自动重新加载。修改 `.env` 后需重启后端。

### 2.3 启动前端（终端三）

```powershell
cd frontend
pnpm dev
```

打开 [http://localhost:3000](http://localhost:3000)，注册账号或使用开发环境的游客入口。前端默认连接本机后端，无需创建前端配置文件；页面与 API 统一使用 `localhost`。

### 2.4 检查连接与停止

另开终端检查本机后端就绪状态：

```powershell
Invoke-RestMethod http://localhost:8000/api/v1/ready
```

HTTP 200 表示就绪。若返回 503，查看 [健康状态](http://localhost:8000/api/v1/health) 和后端日志，依次检查校内网络、SSH 隧道、凭据、服务器服务及两端科学代码版本。

首次接入或升级后，可在 `backend/` 执行以下命令，验证远程真实数据、核心工具与来源链路；无需本机数据集：

```powershell
uv run python deploy/check_remote_data.py
```

结束开发时，在前端、后端和隧道终端分别按 `Ctrl+C`；共享数据服务由维护者管理。本机 API 端口变更后的前端配置见[前端说明](frontend/README.md)。

## 3. 服务器：启动、更新与新增数据

本节由数据服务维护者在 Linux 服务器执行，命令使用 Bash。将 `<...>` 替换为本机实际值；代码目录指含 `pyproject.toml` 的后端目录，服务端口须与本机 SSH 隧道的目标端口一致。下例使用 `8011`，已有部署以实际端口为准。

### 3.1 启动现有数据服务

当前使用 `deploy/start_data_service.sh`，监听 `127.0.0.1:8011`。在服务器终端执行：

```bash
cd '<当前服务版本的后端目录>'
export TARA_MATOU_DATA_DIR='<已准备好的MATOU派生目录>'
test -f "$TARA_MATOU_DATA_DIR/paper_task2/manifest.json" && TARA_DATA_SERVICE_PORT=8011 bash deploy/start_data_service.sh
```

出现 `Uvicorn running on http://127.0.0.1:8011` 表示已启动。若参考清单不存在，上述命令不会启动服务，应先按[后端说明](backend/README.md#论文图9图10功能对照)准备论文参考资料。该清单检查只确认文件存在，资料有效性仍由服务和验收检查。

保持终端运行，按 `Ctrl+C` 停止；服务已运行时不重复启动。脚本负责加载部署所需的数据路径、令牌和锁定依赖，无需手动启动数据库或模型。首次在其他服务器部署时，维护者需先检查脚本中的本机路径、uv 位置、临时目录和凭据文件，不照搬其他机器配置；真实连接信息和凭据不写入 README。

### 3.2 更新服务

1. 保留当前可用代码版本和数据目录，将与本机一致的新版本放入新的服务器代码目录；离线环境使用配套发布包及依赖。检查新版本启动脚本的部署配置，保持原端口、令牌及有效数据路径。
2. 在原服务终端按 `Ctrl+C` 停止服务；使用进程管理器的部署按原方式停止。
3. 进入新版本后端目录，使用当前脚本启动（脚本会同步锁定依赖）：

```bash
cd '<新版本服务器后端代码目录>'
export TARA_MATOU_DATA_DIR='<已准备好的MATOU派生目录>'
test -f "$TARA_MATOU_DATA_DIR/paper_task2/manifest.json" && TARA_DATA_SERVICE_PORT=8011 bash deploy/start_data_service.sh
```

4. 重启本机后端，保持 SSH 隧道开启，在本机 `backend/` 执行 `uv run python deploy/check_remote_data.py`；再检查 `/api/v1/ready`。启用 MATOU 的服务另执行 `uv run python deploy/check_remote_matou.py`。验收通过后再清理旧版本；失败则恢复旧代码和原数据配置。

仅改前端或普通 API 时不必更新数据服务；科学代码、工具契约或计算依赖变化时必须同步两端版本。

### 3.3 更新或新增数据集

| 情况 | 服务器操作 |
| --- | --- |
| 更新已支持的核心 TSV | 原文件保留，新数据放入独立目录；在服务器后端终端将 `TARA_DATASET_DIR` 和 `TARA_PROCESSED_DATA_DIR` 导出为新的原始及派生目录，运行 `uv run --locked tara-data`；成功后更新启动脚本中的对应路径 |
| 接入或更新已支持的 MATOU 数据 | 按[MATOU 数据准备](backend/README.md#matou-数据准备)生成并验证新派生目录，再将启动命令中的 `TARA_MATOU_DATA_DIR` 指向新目录 |
| 新增其他类型数据集 | 先明确格式、来源、版本、样本映射和分析需求，再实现数据目录描述、读取适配、必要预处理及工具接入；不能仅复制文件就自动开放分析 |

核心 TSV 更新的准备命令（在服务器后端目录执行）：

```bash
export TARA_DATASET_DIR='<新版核心原始数据目录>'
export TARA_PROCESSED_DATA_DIR='<新的核心派生目录>'
uv run --locked tara-data
```

准备新核心派生目录时，已使用的研究资料包也须按新数据版本重新准备并核验，步骤见[研究任务说明](backend/RESEARCH_TASKS.md)。不要直接复制旧清单。

数据准备成功后，更新启动脚本或启动命令的数据路径（注意脚本内的赋值会覆盖同名环境变量），重启服务器服务及本机后端，按第 3.2 节验收。日常启动不重复预处理；新增数据不要求开发者下载数据集。

## 4. 本机拉取更新后

先停止本机前后端，根据变化执行对应命令，再按第 2 节启动：

| 更新内容 | 执行目录 | 操作 |
| --- | --- | --- |
| 后端依赖或 `uv.lock` 变化 | `backend/` | `uv sync --locked` |
| 前端依赖或 `pnpm-lock.yaml` 变化 | `frontend/` | `pnpm install --frozen-lockfile` |
| 新增数据库迁移 | `backend/` | 数据库运行时执行 `uv run alembic upgrade head` |
| 科学计算代码、计算依赖或数据版本变化 | 服务器及本机 | 维护者同步数据服务版本，按需更新服务器数据，重启服务及本机后端，再运行远程验收 |

普通前端或 API 改动无需更新科学数据服务。本机不执行数据预处理；服务器的数据准备与维护见[后端说明](backend/README.md#校内独立数据服务)。已执行过的数据库迁移不改写，结构变化通过新增迁移完成。

## 5. 修改代码后：检查改动

后端：在 `backend/` 执行，普通测试使用小型测试数据和模拟模型，不需要完整数据集或真实模型密钥。

```powershell
uv run ruff check .
uv run pytest -m "not integration"
```

前端：在 `frontend/` 执行。

```powershell
pnpm lint
pnpm typecheck
pnpm build
```

远程真实数据验证使用第 2.4 节的 `check_remote_data.py`。直接读取原始 TSV 的集成测试在具备数据的服务器环境执行，本机无需为这些测试下载数据。数据库集成测试需要独立测试数据库，配置方法见[后端说明](backend/README.md#真实数据与数据库测试)；跳过的测试不代表验证通过。

## 6. 协同开发与部署

开始修改前阅读[开发指南](AGENTS.md)和[项目方案](tara-agent方案文档.md)。后端数据处理与服务器维护见[后端说明](backend/README.md)，前端展示职责见[前端说明](frontend/README.md)。

完整应用的 Docker 部署见[部署指南](deploy/README.md)。该指南描述持有数据的完整部署环境，使用正式构建；本机连接校内数据服务的日常开发按本文操作。
