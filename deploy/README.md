# Docker 一键启动

本指南用于在本机验证完整部署，并在后续云服务器上使用同一套配置。前后端使用正式构建，不会在修改源码后自动刷新；日常编码仍按根目录 README 的开发流程启动。

需要 Docker 和 Docker Compose；Windows 使用 Docker Desktop，并切换为 Linux 容器。后续命令都在项目根目录执行，PowerShell 和 Linux 终端均可使用 Docker 命令。

## 1. 首次运行：准备配置和数据

先按[根目录的数据说明](../README.md#13-放置数据集)准备四份 TSV。Docker 不会下载数据，也不会把数据集和密钥打入镜像。

在 PowerShell 执行一次：

```powershell
# 创建部署配置。已有 deploy/.env 时保留原文件。
Copy-Item deploy/.env.example deploy/.env
```

Linux 上用 `cp deploy/.env.example deploy/.env` 完成相同操作。

打开 `deploy/.env`，修改 `POSTGRES_PASSWORD` 和 `DEEPSEEK_API_KEY`；数据放在其他位置时，修改 `TARA_SOURCE_DATA_DIR`。密码无需再填入数据库连接地址，启动程序会正确处理密码中的特殊字符。

其余默认配置用于本机验证：网站地址为 `http://localhost:8080`，只允许本机访问，游客入口开放，可进入所有访客共享的测试空间。生产环境仍禁止游客登录。必须使用这个地址访问，后端会检查注册、登录和聊天请求来自哪个网站。

## 2. 启动：一条命令

```powershell
# 构建前后端镜像并在后台启动；等待所有服务健康检查通过。
docker compose --env-file deploy/.env -f compose.production.yaml up -d --build --wait --wait-timeout 300
```

首次运行会下载镜像和依赖。启动顺序是：数据库可用 → 后端检查配置、更新表结构、预处理数据 → 前后端健康检查通过 → Caddy 开放网站入口。

原始数据与处理代码版本没有变化时，预处理会校验并复用已有结果。配置、迁移或数据处理失败时，后端不会进入可用状态，启动命令会报告失败。首次构建或数据处理较慢时，可增大最后的等待秒数。

打开 `http://localhost:8080`，注册账号后检查：

1. 能登录并提交一个科学问题，例如“查询地中海表层样本”。
2. 能查看分析结果、地图或表格，以及分析过程。
3. 刷新页面后，消息和分析记录仍然存在。
4. 停止后重新启动，仍能登录并找到原来的记录。

健康检查确认数据、数据库和 Agent 已装配，实际模型连接是否正常要通过一次真实问题验证。

## 3. 后续启动、更新和停止

```powershell
# 日常重新启动，复用已有镜像。
docker compose --env-file deploy/.env -f compose.production.yaml up -d --wait --wait-timeout 300

# 修改代码并准备部署更新时，重新构建并启动。
docker compose --env-file deploy/.env -f compose.production.yaml up -d --build --wait --wait-timeout 300

# 停止整套应用并删除容器，保留数据库、处理数据和证书。
docker compose --env-file deploy/.env -f compose.production.yaml down
```

更新可能短暂停机。数据库迁移只应用尚未执行的版本，不会每次重新建库。替换原始数据或修改预处理代码后，应停止整套应用，再执行更新命令，确保 API 重新加载处理结果。

配置和数据位于容器之外。不要加 `down -v`，它会删除数据卷，连同数据库、处理数据和证书一起删除。

## 4. 查看状态和排查失败

```powershell
# 显示每个容器的运行和健康状态。
docker compose --env-file deploy/.env -f compose.production.yaml ps

# 查看后端最近日志，定位配置、迁移、数据处理或模型调用错误。
docker compose --env-file deploy/.env -f compose.production.yaml logs --tail 100 backend
```

网页连接失败时，可将最后一个服务名改为 `caddy` 或 `frontend` 查看对应日志。`http://localhost:8080/api/v1/ready` 返回 HTTP 200 时，后端已就绪。

## 5. 数据保存和备份

| 内容 | 保存位置 | 备份方法 |
| --- | --- | --- |
| 原始 TSV | `TARA_SOURCE_DATA_DIR` 指定的主机目录，容器只读 | 保留一份独立原始文件副本 |
| 用户、会话、消息、分析链路 | Docker 卷 `tara-agent-deploy_postgres_data` | 用 PostgreSQL 导出，并将导出文件复制到独立存储 |
| 预处理结果 | Docker 卷 `tara-agent-deploy_processed_data` | 可由原始数据重新生成；保留生成版本可复现历史分析 |
| HTTPS 证书和 Caddy 配置状态 | `tara-agent-deploy_caddy_data`、`tara-agent-deploy_caddy_config` | 纳入服务器备份；丢失后需重新申请证书 |

数据卷不会因重建容器消失，但服务器磁盘损坏仍会丢失数据。上云时必须配置定时数据库导出、独立存储和恢复验证。

使用示例中的数据库名称和账号时，以下命令生成一次数据库备份；改过名称时相应替换：

```powershell
# 在数据库容器内生成可供 pg_restore 恢复的备份文件。
docker compose --env-file deploy/.env -f compose.production.yaml exec -T postgres pg_dump -U tara_agent -d tara_agent -Fc -f /tmp/tara-agent.dump

# 将备份复制到本机；用不同文件名保存各次备份，再复制到独立存储。
docker compose --env-file deploy/.env -f compose.production.yaml cp postgres:/tmp/tara-agent.dump ./tara-agent.dump
```

备份文件含产品数据，不提交到 Git。恢复时先在独立空数据库用 `pg_restore` 验证，再处理正式数据库。

这套部署仅增加 Caddy 作为网站入口，不引入付费软件服务。容器共用服务器 CPU、内存和磁盘，日志限制为每服务三份、每份 10 MB。数据库不可用时登录和分析无法正常保存；Caddy 不可用时网页入口无法访问。日常开发仍可采用原有前后端启动方式。

## 6. 第二步：部署到云服务器

本地验证通过后，准备 Ubuntu 云服务器、域名、Docker、原始数据目录和数据库备份，再修改 `deploy/.env`：

```dotenv
# 替换为自己的正式域名；两个配置必须指向同一个网站。
TARA_ENVIRONMENT=production
TARA_SITE_ADDRESS=tara.example.com
TARA_PUBLIC_ORIGIN=https://tara.example.com
TARA_BIND_ADDRESS=0.0.0.0
TARA_HTTP_PORT=80
TARA_HTTPS_PORT=443
TARA_SOURCE_DATA_DIR=/data/tara/raw
```

域名须解析到服务器，网络放行 80 和 443，Caddy 才能自动申请和续期公开 HTTPS 证书。生产环境登录 Cookie 只通过 HTTPS 发送；不要用 HTTP 代替。

保持部署项目名称和数据卷不变，使用第 2 节同一命令启动。完成首次云部署和备份恢复验证后，再接 GitHub Actions 自动构建和部署；当前尚未加入自动部署流程。

配置依据：[Docker 启动顺序](https://docs.docker.com/compose/how-tos/startup-order/)、[Next.js 正式运行文件](https://nextjs.org/docs/app/api-reference/config/next-config-js/output)、[uv 容器安装](https://docs.astral.sh/uv/guides/integration/docker/)、[Caddy HTTPS](https://caddyserver.com/docs/automatic-https)。
