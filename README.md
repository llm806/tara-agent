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
