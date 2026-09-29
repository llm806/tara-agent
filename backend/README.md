# Tara Agent 后端

后端负责用户认证、会话保存、Agent 工作流、数据查询与科学计算，并提供 HTTP API 和 MCP 工具。

## 首次运行与日常开发

首次运行按[根目录 README](../README.md)准备配置、数据和数据库，安装依赖、创建数据库表并处理数据。数据库启动后，在 `backend/` 执行：

```powershell
# 启动后端开发服务；修改代码后自动重新加载。
uv run fastapi dev
```

默认端口为 `8000`：

- `http://localhost:8000/docs`：查看当前接口、参数和返回结构。
- `http://localhost:8000/api/v1/health`：查看数据、数据库和 Agent 的状态。
- `http://localhost:8000/api/v1/ready`：检查是否可以提供完整服务；未就绪时返回 HTTP 503。

日常启动不需要重复安装依赖、建表或处理数据。拉取更新后的条件与命令见根目录 README。

模型密钥 `DEEPSEEK_API_KEY` 和数据库地址 `TARA_DATABASE_URL` 放在根目录 `.env`。若已有 `backend/.env`，同名配置以它为准；终端环境变量优先于两份文件。其他后端配置见 [`.env.example`](.env.example)。

## 代码职责

| 目录或文件 | 职责 |
| --- | --- |
| `src/tara_agent/api/` | HTTP 路由、输入输出校验和认证检查 |
| `src/tara_agent/auth/` | 注册、登录、游客身份和登录会话 |
| `src/tara_agent/agent/` | 请求分流、工具选择、回答、历史分析引用和问题建议 |
| `src/tara_agent/mcp/` | MCP 工具接口，调用分析服务 |
| `src/tara_agent/analysis/` | 可独立测试的数据查询和科学计算 |
| `src/tara_agent/data/` | 原始数据校验、预处理、来源记录和处理后数据读取 |
| `src/tara_agent/domain/` | 各层共用的科学数据类型、来源信息和警告 |
| `src/tara_agent/observability/` | 统一执行记录、节点关系和记录器 |
| `src/tara_agent/persistence/` | PostgreSQL 模型、事务和用户数据访问 |
| `src/tara_agent/config.py` | 环境变量读取与配置校验 |
| `migrations/` | 按版本追加的数据库结构变更 |

## 数据处理与科学计算

原始 TSV 保持只读。运行时查询和计算读取校验后的 Parquet 文件，不直接读取原始 TSV。四份数据的名称和放置方式见根目录 README。

以下命令在 `backend/` 执行，仅在需要生成或更新处理结果时使用：

```powershell
# 校验原始数据并生成处理结果；数据与处理版本未变且产物有效时跳过。
uv run tara-data

# 需要强制重跑或检查重复处理的一致性时使用；不是日常启动步骤。
uv run tara-data --force
```

默认输出目录为 `backend/data/processed/`。`manifest.json` 记录源文件和结果文件的校验值、处理版本、数据规模及样本覆盖情况；数据文件位于对应的 `generation-<fingerprint>/` 子目录。输出目录可通过 `TARA_PROCESSED_DATA_DIR` 配置，必须与原始数据目录分开。

### 当前六个工具

| 工具 | 功能 |
| --- | --- |
| `find_samples` | 按海区、深度、粒径、温度等条件查找样本 |
| `get_sample_info` | 查看一个样本的采样和环境信息 |
| `find_taxa` | 在指定 V4 或 V9 数据中查询生物分类及测序读数 |
| `taxon_abundance` | 计算指定类群的测序读数和样本内相对丰度 |
| `diversity_analysis` | 计算 ASV 丰富度与 Shannon 指数，支持样本分组汇总 |
| `environment_association` | 计算类群相对丰度与指定环境变量的 Spearman 相关 |

开发与解释结果时必须保留以下边界：

- V4 和 V9 分开分析；测序读数不能直接解释为细胞数量。
- 分类查询默认按分类层级精确匹配，忽略大小写；子串匹配需要显式选择。
- 多样性基于未经稀释抽样的测序读数，Shannon 使用自然对数；分组汇总不等于组间显著性检验。
- 环境关联排除缺少所需环境值的样本；当前单次相关分析不做多重检验校正。
- 缺失值、无法计算的结果和适用限制通过结构化警告返回，不能忽略或改写为有效数值。

### 独立使用 MCP

网页分析时，API 已在进程内连接 MCP 工具，无需额外启动 MCP 服务。只有外部 MCP 客户端需要独立连接时，才在 `backend/` 使用：

```powershell
# 通过标准输入输出提供六个只读分析工具，供 MCP 客户端启动和连接。
uv run tara-mcp
```

MCP 层校验参数并调用分析服务，不直接实现算法。模型不能执行任意 Python、Shell 或 SQL。

## 工作流、会话与分析记录

- LangGraph 先区分直接回答、数据分析、澄清和超出能力范围的请求。当前每次新分析选择一个白名单工具；多工具、多步骤科研流程尚未实现。
- 多轮对话使用近期已完成消息和相关历史分析摘要。规划只接收本次实际选中的引用，不将历史明细全部发送给模型。
- 一个用户问题对应一个 Trace（分析执行记录），包含一棵按真实父子关系组织的节点树。通过 `TraceRecorder` 记录工作流、模型、MCP 工具、分析服务和数据访问，不直接拼接数据库中的节点记录。
- 科学步骤和技术参数保存在同一棵树中。记录包含来源、筛选条件、结果摘要及必要的资源版本；成功和失败都要保存状态。
- 节点调试输入输出会遮盖密钥并限制长度；聊天恢复使用保存的完整业务回复，不能依赖可能截断的调试内容。
- PostgreSQL 保存用户、登录会话、聊天消息和分析记录。密码使用 Argon2 哈希，登录令牌只保存哈希；会话与分析记录的读写必须校验用户归属。游客是共享开发账户，生产环境禁用游客入口。
- 已在团队环境执行的数据库迁移不改写；新增结构变更通过新迁移完成。

## 修改后检查

以下命令都在 `backend/` 执行，不是服务启动步骤。

### 常规检查

```powershell
# 检查 Python 代码规范和常见错误。
uv run ruff check .

# 运行普通测试，使用小型测试数据和模拟模型，无需完整数据集或真实密钥。
uv run pytest -m "not integration"
```

### 真实数据与数据库测试

修改数据处理、权限、持久化或完整分析流程时，按改动运行相关集成测试：

```powershell
# 运行真实数据与数据库集成测试；未满足运行条件的用例会跳过。
uv run pytest -m integration
```

真实数据测试要求项目根目录的 `Tara_4_Core_Datasets/` 中有四份原始 TSV；当前这些测试不会使用自定义 `TARA_DATASET_DIR`。

数据库测试需先准备本地测试数据库。在单独的测试终端中，将 `TARA_DATABASE_URL` 设为该数据库的连接地址，再执行：

```powershell
# 为测试数据库创建或更新表结构。
uv run alembic upgrade head

# 开启数据库测试；只影响当前 PowerShell 终端。
$env:TARA_RUN_DATABASE_TESTS="1"

# 运行集成测试，包括已开启的数据库用例。
uv run pytest -m integration
```

测试会写入并清理记录。完成后关闭该测试终端，避免后续开发服务沿用测试数据库配置。测试跳过不表示验证通过；修改公共接口、数据库或数据格式时，同步更新相关类型、迁移、测试和文档。

团队规范与演进方向见 [AGENTS.md](../AGENTS.md) 和[项目方案](../tara-agent方案文档.md)。
