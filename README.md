# Tara Agent

Tara Agent 是面向 Tara Oceans 数据的分析系统，支持自然语言问答、数据分析、结果展示和链路追溯。

## 环境要求

- Docker Desktop
- Python 3.12 或 3.13
- uv
- Node.js 22
- pnpm 10

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

## 首次初始化

在项目根目录依次执行：

```powershell
docker compose up -d postgres
cd backend
uv sync
uv run alembic upgrade head
uv run tara-data
cd ../frontend
pnpm install
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
