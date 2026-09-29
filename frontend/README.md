# Tara Agent 前端

前端使用 Next.js、React 和 TypeScript，负责用户操作和结果展示。科学计算由后端完成。

## 首次运行与日常开发

首次运行按[根目录 README](../README.md)准备工具、配置、数据和数据库，并安装前后端依赖。数据库和后端启动后，在 `frontend/` 执行：

```powershell
# 启动前端开发服务；修改代码后自动更新页面。
pnpm dev
```

访问 `http://localhost:3000`。日常开发直接运行上述命令；只有前端依赖或锁文件变化时，才需要在本目录执行 `pnpm install --frozen-lockfile`，按锁定版本重新安装依赖。

本地开发无需创建 `.env.local`。前端默认将 `/api` 请求转发到 `http://127.0.0.1:8000/api`。

## 当前功能与代码位置

| 功能 | 主要文件 |
| --- | --- |
| 注册、登录、游客进入、退出 | `src/components/auth-gate.tsx` |
| 聊天与逐步显示回答、恢复历史消息 | `src/components/chat-workspace.tsx` |
| 新建、切换、重命名、置顶和批量删除会话 | `src/components/session-sidebar.tsx` |
| 首页问题建议与“换一批” | `src/components/question-suggestions.tsx` |
| 分析表格与 CSV 下载 | `src/components/result-table.tsx` |
| 地图、柱状图、散点图与 PNG 下载 | `src/components/analysis-chart.tsx` |
| 会话内分析记录、执行步骤与节点详情 | `src/components/trace-workspace.tsx` |
| API 请求与前后端数据类型 | `src/lib/api.ts`、`src/lib/types.ts` |

## 修改后检查

以下命令在 `frontend/` 执行，用于检查改动，不是启动步骤：

```powershell
# 检查代码规范和常见错误。
pnpm lint

# 检查 TypeScript 类型。
pnpm typecheck

# 检查能否完成生产构建。
pnpm build
```

修改交互后，还应在页面验证对应操作，包括加载失败、登录失效和刷新后的显示。修改聊天流时，检查成功、失败和中断时的提示；修改分析展示时，检查表格、图表和来源信息。

## 配置与开发约定

- 只有需要更改 API 地址或通过外部域名访问开发服务器时，才在本目录创建 `.env.local`；可配置项见 [`.env.example`](.env.example)。`TARA_ALLOWED_DEV_ORIGIN` 填域名，不含协议。修改配置后重启前端。
- `NEXT_PUBLIC_` 开头的配置会公开给浏览器，不放模型密钥或数据库密码。
- API 调用集中在 `src/lib/api.ts`；修改接口数据结构时，同步更新后端定义与 `src/lib/types.ts`。
- 前端展示后端返回的统计结果、来源、警告和图表数据，不自行重算科学结果。
- 聊天逐步接收后端事件；连接结束但未收到完成或错误事件时，应显示异常，不能当作成功。
- 分析过程页面按真实父子关系展示同一棵执行树，不另建一套科学步骤记录。

团队规范与后续方向见 [AGENTS.md](../AGENTS.md) 和[项目方案](../tara-agent方案文档.md)。
