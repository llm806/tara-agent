# Tara Agent

Tara Agent 是面向 Tara Oceans 数据的分析 Agent：后端提供可验证的科学分析、MCP 工具和 Agent，前端负责对话与结果展示。

## 启动准备

- Docker Desktop、Python 3.12 或 3.13、[uv](https://docs.astral.sh/uv/getting-started/installation/)、Node.js 22 和 pnpm 10（项目指定 pnpm 10.12.4）。

## 1. 初始化数据库

在项目根目录打开 PowerShell，首次使用时创建本地配置并启动 PostgreSQL：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up -d postgres
cd backend
uv sync
uv run alembic upgrade head
cd ..
```

`.env` 仅用于本机且不会提交到 Git。部署环境必须通过密钥管理服务提供强密码和
`TARA_DATABASE_URL`，不能沿用示例值。数据库文件保存在 Docker 命名卷中，执行
`docker compose down` 不会删除数据。

## 2. 初始化并启动后端

在项目根目录打开第一个 PowerShell 终端：

```powershell
cd backend
uv sync
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

在 `backend/.env` 中把 `DEEPSEEK_API_KEY` 改为自己的密钥；如果该文件已存在，不要覆盖。然后预处理数据并启动服务：

```powershell
uv run tara-data
uv run fastapi dev
```

预处理把只读 TSV 验证并生成到 `backend/data/processed/`；重复运行会跳过未变化的数据。后端运行在 `http://localhost:8000`，可访问 `http://localhost:8000/api/v1/health` 检查状态。保持此终端运行。

## 3. 启动前端

在项目根目录打开第二个 PowerShell 终端：

```powershell
cd frontend
if (-not (Test-Path .env.local)) { Copy-Item .env.example .env.local }
pnpm install
pnpm dev
```

打开 `http://localhost:3000`。开发环境可点击“游客进入”；也可以创建独立账户，其数据只对账户本人可见。游客入口在生产环境强制关闭。前端默认连接当前页面主机的 `8000` 端口，也可通过 `frontend/.env.local` 中的 `NEXT_PUBLIC_API_BASE_URL` 指定后端地址。

日常重新启动时，先在根目录运行 `docker compose up -d postgres`，再分别在 `backend/`
运行 `uv run fastapi dev`、在 `frontend/` 运行 `pnpm dev`。数据库表结构发生变化时运行
`uv run alembic upgrade head`；仅当原始数据或依赖变化时重新执行相应的预处理或安装命令。
