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

### 核心查询工具

| 工具 | 功能 |
| --- | --- |
| `find_samples` | 按海区、深度、粒径、温度等条件查找样本 |
| `get_sample_info` | 查看一个样本的采样和环境信息 |
| `find_taxa` | 在指定 V4 或 V9 数据中查询生物分类及测序读数 |
| `taxon_abundance` | 计算指定类群的测序读数和样本内相对丰度 |
| `diversity_analysis` | 计算 ASV 丰富度与 Shannon 指数，支持样本分组汇总 |
| `environment_association` | 计算类群相对丰度与指定环境变量的 Spearman 相关 |

配置有效的 `TARA_MATOU_DATA_DIR` 后，另提供 `find_function_samples`、`function_profile`、`function_atlas`、`compare_function_signals` 和 `retrieve_gene_sequences`。支持单样本候选信号、总体 Top20/Top100、原始样本功能谱、同采样编码的 DNA/RNA 相对贡献描述和已准备序列查询；不提供 RNA/DNA 活性或环境关联。

开发与解释结果时必须保留以下边界：

- V4 和 V9 分开分析；测序读数不能直接解释为细胞数量。
- 分类查询默认按分类层级精确匹配，忽略大小写；子串匹配需要显式选择。
- 多样性基于未经稀释抽样的测序读数，Shannon 使用自然对数；分组汇总不等于组间显著性检验。
- 环境关联排除缺少所需环境值的样本；当前单次相关分析不做多重检验校正。
- 缺失值、无法计算的结果和适用限制通过结构化警告返回，不能忽略或改写为有效数值。

### 独立使用 MCP

网页分析时，API 已在进程内连接 MCP 工具，无需额外启动 MCP 服务。只有外部 MCP 客户端需要独立连接时，才在 `backend/` 使用：

```powershell
# 通过标准输入输出提供当前已接入的只读分析工具。
uv run tara-mcp
```

MCP 层校验参数并调用分析服务，不直接实现算法。模型不能执行任意 Python、Shell 或 SQL。

## 工作流、会话与分析记录

- LangGraph 先识别请求，再选择单步或有界多步骤分析。多步骤记录固定用户目标，依据工具结果与检查反馈继续规划，默认最多六次调用、八轮规划、240 秒等待及两次执行前契约纠正。完成检查覆盖全部目标并引用真实步骤；阻断目标、澄清、重复调用或预算用尽返回 `partial`，保留结果。实际执行与数据验证失败不自动重试；本地超时不能强制取消已开始的远端计算。目标和证据检查不等于科学结论的自动证明。
- 首页建议按任务类型均衡轮换，标明数据范围；MATOU 问题采用工具返回的真实类群与非空样本，综合问题只在模型提供多步骤接口时推荐。论文读取与引用尚未接入。
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

## Docker 部署

按 [Docker 部署指南](../deploy/README.md)运行整套应用。后端容器以普通用户运行，启动时自动检查配置、执行数据库迁移和数据预处理，然后启动 API；原始数据只读，数据库与处理数据保存在独立数据卷中。配置或初始化失败时不会通过就绪检查。

## 校内独立数据服务

已提供 `tara_agent.data_service.app:create_app`。服务复用核心工具及配置后启用的 MATOU 工具，提供认证后的 `/health`、`/tools`、`/call` 和 `/suggestions`；不需要模型密钥或 PostgreSQL。请求工具名受白名单限制，工具参数由 MCP Schema 校验。`/call` 绑定科学代码摘要与核心数据 generation；MATOU 调用另绑定其独立清单摘要。客户端核验科学代码与计算依赖版本，拒绝不匹配的计算。前端及普通 API 改动无需更新科学服务。

维护者启动、开发者配置、SSH 隧道及日常检查，统一见[根目录 README 第 2.4 节](../README.md#24-使用校内数据服务团队日常操作)。其中端口仅为示例；实际连接信息、目录和凭据由维护者私下提供。

接口均要求 `Authorization: Bearer <token>`；凭据私下传输到用户目录，不提交或发到聊天。服务串行执行分析，处理结果和原始数据留在服务器。健康检查包含数据清单和代码摘要，工具调用可返回继承指定父节点的观测事件。配置远程 URL 后，本机 API、Agent、健康检查和问题建议均使用数据服务，不加载本机科学数据；连接失败明确报错，不回退本机。数据版本在后端进程内绑定，变更后需验证并重启本机后端。当前启动脚本前台运行，未提供开机自启或进程托管。

在本地 `backend/` 执行 `uv run python deploy/check_remote_data.py`，验证真实样本数量、六种查询计算、科学来源和执行节点，以及问题建议。通过后按原开发流程启动本地 PostgreSQL、后端和前端；模型密钥和产品数据库仍配置在本地。每日保持服务器数据服务运行，启动 SSH 隧道和本地应用即可，无需重传数据、重新预处理或下载凭据。

服务器完整应用不设置远程 URL，使用进程内 MCP 路径。两种路径复用同一科学实现并有结果相等的自动测试，浮点跨平台验收使用明确容差。MATOU 候选分析已接入远程网关和单步/多步骤聊天；真实服务器转换与远程工具的结构、来源和算术已核验。完整产品验收需真实模型与数据库，独立备份恢复另行验证。

MATOU 只在实际持有派生产物的数据服务中配置 `TARA_MATOU_DATA_DIR`，远程模式的本地后端无需配置本机 MATOU 目录。先运行下方转换命令，再同步科学代码、设置此目录并重启服务。`deploy/start_data_service.sh` 可通过 `TARA_DATA_SERVICE_PORT` 使用另一个回环端口，验收新发布版本时保留原服务。更新后通过 SSH 隧道在本地运行 `uv run python deploy/check_remote_matou.py`，核验两个实验的实际样本、来源、份额算术和同树观测；可用 `--meta-g-sample`、`--meta-t-sample` 指定原始编号。不指定时仅为接口验收选择各实验首个非空样本，不作为科研样本选择。真实验收不证明单位、功能归属或配对。

### MATOU 数据准备

MATOU 的科学边界与工作流方向见[方案第 3.4 节和第 5.5 节](../tara-agent方案文档.md)。原始 Gzip 可流式读取，不要求先解压；数据准备与应用接入分别验证。

`deploy/inspect_matou_taxonomy.py` 使用 Python 3.11 及以上的标准库，可独立运行。只读探查使用 `--input` 和 `--taxon`；提取基因清单与 Pfam 时再提供 `--pfam`、`--expected-taxonomy-sha256` 和 `--output-dir`。参数说明：

```bash
python3 deploy/inspect_matou_taxonomy.py --help
```

准备模式完整读取 taxonomy/Pfam，检查目标基因编号唯一性并保留全部对应 domain hits；不做功能定量或 DNA/RNA 比较。产物为 `gene_ids.tsv`、`pfam_hits.tsv` 和 `report.json`，包含来源、版本、参数、摘要与验证范围。数量和耗时保存在运行报告中。

输出必须位于原始目录之外的新目录；已有目录不覆盖，只有完整 `report.json` 才表示准备成功。失败或中断目录不可用于后续分析。默认目标基因数量上限可通过参数调整，不能将它视为内存配额。

服务器使用发布包时，其布局可能不同于本地仓库；可将独立脚本上传到已确认的目录后运行，不假设服务器存在根级 `backend/`。

`deploy/inspect_matou_occurrences.py` 使用标准库，只读试读定量压缩表的前缀。通过重复 `--input` 顺序读取多份文件，`--max-rows` 控制每份文件的行数；输出结构、数值范围、局部重复、样本编号示例和读取速度。前缀不是随机样本，报告不代表全表质量、完整样本清单、定量单位或配对关系，也不计算功能丰度。

`deploy/extract_matou_occurrences.py` 使用标准库完整筛选 MetaG/MetaT。通过 `--prepared-dir` 复用准备脚本 v2 的成功报告及基因清单，提供 `--meta-g`、`--meta-t` 和新的 `--output-dir`；其余保护参数见 `--help`。脚本顺序读取两个源文件，校验清单摘要、全部记录和 Gzip，保留原始数值及未获 Pfam 注释的目标基因，并生成两份筛选压缩表、`samples.tsv` 和最后发布的 `report.json`。

全量筛选验证样本数据块连续、块内 geneID 严格递增以证明键唯一；遇到重复或无序时停止核查，不自动排序、合并或去重。原始目录和已有产物不覆盖，磁盘保留额度及基因、样本数量受参数限制；中断目录不可用于分析。完整样本清单不代表已核验定量单位、缺失值语义或 DNA/RNA 配对。

`deploy/inspect_matou_pfam.py --prepared-dir <准备目录>` 核验准备脚本 v2 的 Pfam 派生产物及摘要，区分原始命中、唯一 gene–Pfam 和基因计数，并报告多次命中、翻译框与质量分布。可用 `--output-dir` 在新目录导出去重的 `gene_pfam.tsv` 与最后发布的 `report.json`；映射保留所有原始 gene–Pfam 对，记录跨命中和翻译框的最小 i-Evalue，不用于结构域排列解析。重复 `--sensitivity-evalue` 可比较候选覆盖，默认比较 `1e-5` 和 `1e-3`。

质量分箱和敏感性条件只是探查统计，不自动采用为筛选规则或冒充家族的官方 gathering 阈值。多次命中可能来自重复结构域或不同翻译框，不直接认定为错误记录；不同 Pfam 可共享基因。此脚本不修改输入，不计算功能丰度；写出目录无完整报告时不可用于后续分析。

`deploy/inspect_matou_sample_values.py` 使用标准库核验 MetaG/MetaT 各自首个完整样本。提供 `--raw-dir`、`--occurrences-dir`、`--prepared-dir` 和新的 `--output-dir`，分别指向原始压缩表、全量筛选 v1 产物、分类准备 v2 产物和检查结果目录。脚本核对样本清单、原值一致性、样本数值总和及 Pfam 注释覆盖的目标类群信号比例；同次校验已准备 Pfam 的完整摘要，不重扫全部定量记录。行数、目标基因数、Pfam 命中数和总时间受参数限制；未读完样本或验证失败时不发布成功报告。

首个样本不是随机抽样，其总和不能证明全表归一化或确定单位、缩放和缺失记录含义。不同 Pfam 共享基因时，逐功能相加可能超过类群总信号；敏感性条件仍只是候选检查。此脚本不核验配对或计算 RNA/DNA 比值。原始数据只读，结果与检查范围保存在新目录的 `report.json`。

`analysis/function_signal.py` 与 `MatouFunctionService` 提供单样本候选功能分析。读取器验证清单、产物摘要和样本范围；算法返回指定 Pfam 或 Top N 的数值之和、全部已提供类群记录内的份额及注释覆盖。E-value 条件显式记录；共享基因不分摊，未出现记录不补零，零分母份额为空。结果携带分类筛选、来源、摘要和方法，服务与数据访问继承同一 Trace；多步骤流程可组合样本检索及不同候选条件下的独立计算，不扩展为未经核验的配对定量。

在安装锁定依赖的后端环境中，使用 `python -m tara_agent.data.matou_cli`：

```bash
# 完整读取已提取子集，按样本生成 Parquet；仅接受新输出目录。
python -m tara_agent.data.matou_cli prepare --prepared-dir <分类准备目录> --occurrences-dir <定量提取目录> --mapping-dir <候选映射目录> --output-dir <新的功能数据目录>

# 列出真实样本，再按原编号查询；MetaG 和 MetaT 分开使用。
python -m tara_agent.data.matou_cli samples --data-dir <功能数据目录> --assay MetaG
python -m tara_agent.data.matou_cli analyze --data-dir <功能数据目录> --assay MetaG --sample <样本编号> --top-n 20
```

转换绑定分类准备 v2、定量提取 v1 和候选映射 v2 的成功报告，校验输入摘要、目标基因范围、唯一键和逐样本行数。仅在全部通过后发布 `manifest.json`；无完整清单的目录不可查询。输入只读，数值明确转换为 Float64，非零数下溢时停止；原始字符串保留在已有提取文件中。资源保护参数见 `prepare --help`，行数上限不是内存额度。查询读取对应样本及候选映射，不重扫压缩长表，也不推断单位、补零、配对或关联环境数据。

### 复杂任务二的准备与执行

在持有上述 MATOU 派生产物的服务器上，一次性准备紧凑跨样本缓存。程序只读访问现有基因产物，分批写入新的 `study-<id>` 子目录，最后原子发布 `study_manifest.json`，不覆盖已有研究清单。资源上限由 `prepare-study --help` 查看；默认磁盘保留 2 GiB、最长 12 小时、最多两千万家族记录。中断目录不进入查询。大范围查询必须有与 E 条件一致的缓存，未准备时不会在聊天中重新扫描全部基因。

```bash
python -m tara_agent.data.matou_cli prepare-study --data-dir <功能数据目录>
python -m tara_agent.data.matou_cli atlas --data-dir <功能数据目录> --assay MetaT --top-n 100
python -m tara_agent.data.matou_cli compare --data-dir <功能数据目录> --pfam PF00504 --pfam PF03382 --pfam PF00313
```

需要核酸序列时，在首次准备中明确给出 FASTA 和目标 Pfam；原始 FASTA 只读，完整读取和标题校验后才发布。序列准备覆盖指定 Pfam 的候选基因，不宣称覆盖全部类群序列。目标基因缺失、重复/无序标题、非法核酸字符、超时或超额时不发布成功清单。

```bash
python -m tara_agent.data.matou_cli prepare-study --data-dir <功能数据目录> --fasta <MATOU-v1.5.fna.gz> --sequence-pfam PF00504 --sequence-pfam PF03382 --sequence-pfam PF00313
python -m tara_agent.data.matou_cli sequences --data-dir <功能数据目录> --pfam PF00504 --limit 20
```

已经有功能缓存、但没有序列时，用 `deploy/prepare_matou_sequences.py` 在独立新目录升级，不覆盖当前数据，也不重算所有样本功能谱：

```bash
python deploy/prepare_matou_sequences.py --data-dir <已有功能目录> --fasta <MATOU-v1.5.fna.gz> --output-dir <新序列功能目录> --pfam PF00504 --pfam PF03382 --pfam PF00313
```

默认核对 [Genoscope 发布页](https://www.genoscope.cns.fr/tara/#MATOU-1.5)的压缩核酸 FASTA MD5 `045fd2cda0e99e3b6ee52b78ea572da2`，然后完整验证 FASTA 标题、目标 geneID、核酸字符及 SHA-256。现有样本、映射和功能缓存复制后逐件核对清单摘要；不硬链接旧产物，避免影响在线读取的指纹。新主清单最后发布，中断输出没有主清单、不可供服务使用。复制会额外占用旧派生产物的磁盘空间；每次写入前保留指定空间。默认提取碱基上限一亿、最长十二小时，旧目录继续可用。产物范围仅为指定 Pfam 的候选基因。

用户明确选择不做官网 MD5 比对时，可加 `--skip-publisher-md5`，直接进入完整读取与目标提取，不额外扫描 MD5。该选项与 `--expected-fasta-md5` 互斥；报告将 MD5 及其引用留空，并记为 `publisher_md5_verification: not_performed`、`publisher_md5_skip_reason: operator_request`，不能宣称通过官方摘要比对。标题映射、核酸字符、目标覆盖、文件稳定性、来源 SHA-256 和派生产物完整性仍由程序自动处理。2026-10-06 直接读取官方 v1.5 链接确认大小为 25,170,318,919 bytes、首标题为 `>MATOU-v1.5.1`；不能只凭网页所列 MD5 将同大小文件判为 v1。

官方 FASTA 中含小写碱基。提取器接受大小写 IUPAC DNA 字母，将派生序列统一为大写，不改变碱基身份；非法字符仍报出行号、geneID 与字符。研究清单的 `sequence_selection` 和升级报告记录 `sequence_case: normalized_to_uppercase`、`soft_mask_case_preserved: false`：输出不保留原文件中可能表示软屏蔽的字母大小写，原始文件只读且来源 SHA-256 保留。修改提取器属于科学代码变更，须同步服务器的 `analysis/function_study_prepare.py`；切换完成后重启数据服务和本地后端，避免代码版本不同。

升级完成后，将服务 `TARA_MATOU_DATA_DIR` 切换至新目录，重启数据服务及本地后端。仅修改部署脚本时无需同步科学服务；修改提取器时须同步对应科学代码。本地执行 `uv run python deploy/check_remote_sequences.py --output <新报告.json>`，核对三个目标家族的非空序列、geneID 查询一致性、分页及来源绑定。该命令不调用模型或浏览器，二者须单独验收。

准备完成后同步分析代码和依赖，并重启数据服务及本地后端。请求分别绑定主清单与研究清单摘要，版本变化拒绝混用。恢复 SSH 隧道后，本地可执行真实工具验收：

```bash
uv run python deploy/check_remote_function_task.py --output <新验收报告.json>
```

聊天可使用：“分析当前硅藻全部 MetaT 样本的 Top100 候选 Pfam 和各样本贡献；比较 PF00504、PF03382、PF00313 在对应采样编码的 MetaG/MetaT 相对信号，报告覆盖、缺失、相关和波动。”需要序列时再明确要求查询指定 Pfam/geneID。前端显示总体排名、样本谱、比较摘要及散点图，CSV 导出保留全部返回行；长表分页展示，序列查询页可下载 FASTA。

科学口径与限制见方案文档第3.4节：总体排名是原始样本等权的已观测贡献，分母包括未注释基因，家族可共享基因；不是生物学零的缺失保持空值。对应规则来自 [作者版本固定的代码](https://github.com/JJPierellaKarlusich/Diatom_patters/blob/2647f6be2cd709f48d4dcde314be3783978afa97/metaT/scripts/organizador.R)，保存版本和 SHA-256，精确保留批次及原始过滤码，只比较 DNA11/cDNA14。结果是采样编码对应的相对信号，不证明同一提取物，不是 RNA/DNA 活性、差异表达显著性或论文精确复现。论文正文与作者脚本在部分分母和合并处理上存在差异，本实现使用全部类群记录作分母并明确不做这些合并。

真实验收已通过：校内 MATOU 服务返回 581 个 MetaT 样本的 Top100 和 318 个合格对应采样编码；六个原始样本、十八项目标家族结果与缓存交叉核对一致。2026-10-06 的真实 HTTP 验收由 DeepSeek 模型调用真实服务和 PostgreSQL，完成总体排名、三家族比较和三次序列查询，共五次工具调用、四张图、四十九个闭合 Trace 节点；历史重新读取保留完整结果，未认证读取被拒绝。多步骤摘要保留目标家族统计、序列编号及完整结果行数，避免把上下文省略误判为结果缺失。

当前真实序列产物包含 PF00504、PF03382、PF00313 的 61,933 个去重 geneID，研究清单 SHA-256 为 `31781d5f5f267990d04118b4028e4b0c1929704e0c7d89b91a34eab07feb499b`；三个家族分别可查询 53,097、4,633、4,205 条序列，共享基因使家族计数之和可超过去重总数。geneID 查询、分页、来源绑定已通过；浏览器实际下载的五条 PF00504 FASTA 与持久化结果逐条一致，刷新后重新打开会话仍保留结果和导出按钮。按操作者选择未执行官网 MD5 比对，记录原文件 SHA-256 和大写归一信息；不声称官方校验通过。该验收不包含官方 gathering 阈值重注释或论文精确复现。后端测试为 395 通过、8 跳过；跳过不等于验收通过。

全范围分析可直接省略 `sample_names` 或 `sampling_keys`，无需先翻页列举全部编号。比较摘要的 `median_fraction_difference` 是逐对应编码的 MetaT 份额减 MetaG 份额后再取中位数，称为“对应份额差的中位数”。回答模型输出额度为 8192 tokens，发生长度截断时保留计算结果并返回部分完成，不能把被截断的回答当作完整成功。

## 属/种组成与站点丰度更新

本次扩展原 `taxon_abundance` 契约（工具资源版本 1.1.0）：支持完整所选样本排序、站点等权均值/最大值排名，以及指定样本的属/种/ASV组成。具体分母、未知分类和分页规则见项目方案 2.2.1。

远程科学服务须同步以下文件后重启现有服务；保留原数据目录、MATOU目录、端口与凭据设置，无需重建数据：

- `src/tara_agent/analysis/abundance_details.py`（新增）
- `src/tara_agent/analysis/compute.py`
- `src/tara_agent/analysis/compute_models.py`
- `src/tara_agent/mcp/tools.py`

多查询路由和回答修改在本地 Agent 层生效。科学服务版本未同步时仍拒绝调用，不能绕过版本核验。同步完成后重启本地后端，并在本地 `backend/` 执行：

```powershell
.venv/Scripts/python.exe deploy/check_remote_data.py
```

此命令验收六类核心工具及新增站点排名、双样本属组成的远程结果与来源。随后以 V9 明确提问：查询 TARA_A100000032 的位置背景，并比较 TARA_A100000005 与 TARA_A100000032 中硅藻的属级组成。真实模型和浏览器验收须在远程版本对齐后进行；本次上下文工程未扩展。

## 论文图9、图10功能对照（新增）

`function_study` 返回合并功能类别后的 Top100 排名、相对转录贡献、粒径/海区分配表，以及 DUF285、LHC 总体和亚家族的 MetaG/MetaT 相对信号。PLS 使用固定环境变量、标准化和两个成分，返回完整输入、缺失排除数、变量相关坐标、样本坐标和解释比例；不进行显著性或预测能力检验。缺少资料、常量变量或有效观测不足时明确显示未完成。

在科学服务的 `backend/` 目录准备一次作者资料。`<MATOU目录>` 为正在使用的派生数据目录；下载源目录及输出目录须尚不存在。作者版本固定为 `2647f6be2cd709f48d4dcde314be3783978afa97`，原始数据只读。`--matou-dir` 会离线扫描当前逐基因信号并准备 LHC 亚家族，不在聊天请求中扫描。

```bash
uv sync
uv run python deploy/prepare_function_reference.py --download --source-dir /tmp/tara-task2-sources --output-dir <MATOU目录>/paper_task2 --matou-dir <MATOU目录>
```

随后重启科学服务和本地后端，使代码、依赖和参考清单一致。MCP 默认 `source=current_data`；只有用户明确要求论文参考重算时使用 `source=paper_reference`，不能静默切换来源。服务未同步或资料未准备不等于任务已上线。

独立导出当前数据结果供审核（输出目录须不存在）：

```bash
uv run python deploy/check_function_study.py --matou-dir <MATOU目录> --output-dir <审核输出目录>
```

仅核对作者公开汇总数据时：

```bash
uv run python deploy/check_function_study.py --source paper_reference --normalization author_script --reference-dir <MATOU目录>/paper_task2 --output-dir <参考审核目录>
```

作者图9脚本以保留 Pfam 信号归一化，与图注所说的硅藻总转录信号分母不同。当前默认 `normalization=taxon_total`：以包含未注释基因的硅藻总信号归一化，再对采样组等权平均，保留功能不再二次归一化。明确选择 `author_script` 才返回保留功能中的贡献份额；参考重算只支持该口径。共享基因和多结构域不分摊，不能当作互斥基因组成或 RNA/DNA 活性。粒径、海区比例的分母为成功映射的该功能相对信号，映射覆盖率另列。

作者参考重算的真实验证：477 个采样组，Top100 合计 56.3069%，核糖体相关 9.2177%（137 Pfam），泛素相关 4.2037%（公开表合并41 Pfam，论文文字写47），LHC/PF00504 为 2.8383%；DUF285 PLS 有416个完整采样组。参考数据不能还原 LHC 亚家族图10e，因此保留未完成状态。当前数据保留 DNA11/cDNA14，合并等价粒径和重复后计算，不能宣称与作者所有实验协议精确复现。当前远程数据、真实模型与完整图10e尚未在本次验收中运行。

## 三个研究任务的扩展与服务器升级

新增 `community_analysis` 和 `function_environment`，以及原 `function_study` 的显式分母选项。完整科学口径、前提、服务器资料准备和验收见 [RESEARCH_TASKS.md](RESEARCH_TASKS.md)。九个数据集仍在服务器，本机通过原 SSH 隧道调用；新工具不要求把原始数据下载到本机。科研代码变化必须同步服务端并重启两端后端，不能绕过代码或资料版本校验。
