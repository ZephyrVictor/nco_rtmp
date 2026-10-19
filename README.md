# NCO 监考与录屏归档

面向 ICO 录屏要求的监考服务：接收辅助直播、展示监考墙、保存辅助录像，并支持本地 MKV 原件断点上传、哈希校验、完整解码和人工审核。

**当前状态：2026-10-19 已按要求停止服务器上的五个监考容器，本机测试 SSH 隧道已关闭。** 配置、数据库、原件、辅助录像和验收证据保留。Prometheus 的监考采集任务仍保留，停服期间显示 down 属于预期。

## 设计与规则

选手宿主机 OBS 同时生成本地原件和独立编码的辅助直播。服务器使用 Nginx HTTPS/HTTP2 网关、FastAPI 管理 API、SRS 接收与 FLV 分片、tusd 断点上传，以及独立校验 worker；元数据采用 SQLite WAL。

本地原件按提供的 PDF 设置为：单个完整显示器采集、原生画布、等比例输出且至少 1280×720、整数 1 FPS、Advanced、MKV、x264 CRF 24 High、FFmpeg AAC 44100 Hz 单声道，两天各连续录制 7 小时。故障须立即通知志愿者并尽快恢复。服务器辅助录像不替代本地原件，参数校验也不能证明完整屏幕和真实比赛时间覆盖。

## 项目结构

```text
.
├── README.md                              # 项目入口与维护说明
├── .gitignore                             # 密钥、运行数据与构建产物排除规则
├── service/                               # 实际部署的服务代码
│   ├── app/
│   │   ├── main.py                        # API、权限、名单、上传钩子与审核
│   │   ├── core.py                        # 数据库、会话、规则与共用逻辑
│   │   ├── worker.py                      # 直播监控、归档补录与持久化校验队列
│   │   ├── media.py                       # 原件哈希、参数和完整解码校验
│   │   ├── backup.py                      # 独立存储备份与哈希复核
│   │   └── cli.py                         # 健康检查和备份命令
│   ├── config/                            # SRS 和 Nginx 模板
│   ├── web/
│   │   ├── src/                           # 监考管理前端 JS/CSS/HTML
│   │   ├── browser-*.mjs                   # 浏览器界面、直播、上传和并发验收
│   │   ├── build.mjs                      # esbuild 生产构建
│   │   └── package*.json                  # npm 依赖与锁文件
│   ├── tests/                             # 单元、集成及分级并发测试
│   ├── compose.yaml                       # 五个服务与可选负载测试容器
│   ├── deployment-root-compose.yaml       # 服务器数据根目录 Compose 入口
│   ├── Dockerfile                         # Python/FFmpeg 校验运行环境
│   ├── requirements.in / requirements.txt # 依赖声明与锁定版本
│   ├── bootstrap.py                       # 首次生成密钥、环境配置和数据目录
│   ├── setup_tls.py                       # 私有 CA 和 HTTPS 证书配置
│   ├── connect_monitoring.py              # 接入现有 Prometheus
│   ├── grafana-dashboard.json             # 可导入的 Grafana 仪表盘
│   ├── settle_acceptance.py               # 停用测试身份，保留录像和审计
│   └── README.md                          # 详细部署、运维、备份和回退手册
├── configure_local_obs.py                  # 配置独立 ICO2026 OBS profile
├── obs_control.py                          # OBS WebSocket 调试和试录工具
├── capacity_probe.py                       # 网络与磁盘容量测试工具
├── fault_test.py                           # 远程停机恢复、续传和浏览器验收
├── acceptance_status.py                    # 只读采集服务器健康与验收证据
├── ICO-2026-recording-audit.md              # 整改前规则审计
├── ICO-2026-optimization-and-capacity-plan.md # 优化计划与初始容量测量
├── deployment-and-acceptance.md            # 已部署内容、测试结果和待验收事项
├── capacity-results/                       # 本地原始容量/压测结果，不纳入 Git
└── test-results/                           # 截图、报告、数据库快照，不纳入 Git
```

## 当前部署与启停

服务器为 `ncoai@202.112.51.208`，数据根目录 `/data/2027nco`，源码目录 `/data/2027nco/service`。服务器根目录已有 Compose 包装入口；本地对应文件为 `service/deployment-root-compose.yaml`。

查看状态或停止服务：

```sh
ssh ncoai@202.112.51.208 'cd /data/2027nco && docker compose ps -a'
ssh ncoai@202.112.51.208 'cd /data/2027nco && docker compose stop --timeout 30'
```

需要恢复运行时执行：

```sh
ssh ncoai@202.112.51.208 'cd /data/2027nco && docker compose up -d'
```

启动后管理入口为 `https://10.77.0.1:18443`，仅限 WireGuard 专网。也可建立本机隧道：

```sh
ssh -N -L 127.0.0.1:18443:127.0.0.1:18443 ncoai@202.112.51.208
```

然后访问 `https://127.0.0.1:18443`。目前使用私有 CA，正式使用前需配置受信任证书或分发 CA。初始管理员为 `admin`，密码保存于服务器 `service/secrets/admin_password`；本机已有私密副本 `.admin-password`。密码和证书不纳入 Git。

新机器部署须先按 [运维手册](service/README.md) 准备数据目录、UID/GID、专网地址、密钥和 TLS，再构建启动。Compose 当前绑定服务器路径及 UID/GID 1001，不能假定直接在 Mac 原样启动即可运行。部署 `.env`、证书和数据必须独立备份，Git 不是业务数据备份。

## 开发与验证

后端要求 Python 3.13 和 FFmpeg/ffprobe；本机已有 `service/.venv`。新环境可执行：

```sh
cd service
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
```

构建前端需要 Node.js/npm：

```sh
cd service/web
npm ci
npm run build
```

集成、并发、故障和浏览器测试需要服务启动、测试凭据及相应网络连接。`fault_test.py` 会主动停止和重启服务，负载测试会生成直播和录像；仅在验收环境或维护窗口执行。相关命令见 [运维手册](service/README.md) 和测试脚本。

目前脚本使用现有服务器、路径和屏幕尺寸；迁移时先检查配置。`capacity_probe.py` 有磁盘写入和带宽测试，`configure_local_obs.py` 会修改本机 OBS 配置，`connect_monitoring.py` 会修改已有 Prometheus 配置，不作为普通开发初始化步骤自动运行。

## 验收状态

- 最终 FLV 配置通过 200 路推流 + 64 路观看、约 91 秒短测，平均入站约 202 Mbps，观看错误 0，200 份辅助录像全部完整解码。
- 此前 260 路推流 + 84 路观看短测通过；最新 65 项集成、19 项故障恢复、15 项界面检查通过，真实浏览器 16 路画面及断网恢复通过。
- 测试结束时 `/data` 剩余约 24.41 TiB，无校验积压；这只是测量时点，不是持续保证。
- 尚未完成 macOS OBS 屏幕录制权限授权后的实际全屏试录、真实赛场网络与 7 小时演练、独立存储全量备份恢复及 RAID/UPS 检查，不能宣称赛事全面合规。

完整结果见 [部署与验收报告](deployment-and-acceptance.md)。其中服务健康快照记录的是主动停服前状态；当前运行状态以本 README 的停服说明为准。原始 JSON、截图和录屏保留于本机，克隆仓库不会包含这些被忽略的文件。原始规则 PDF 位于用户提供的本机路径，未复制进仓库。

## Git 维护

本目录使用 Git 管理，默认分支 `main`，远程 `origin` 为 [ZephyrVictor/nco_rtmp](https://github.com/ZephyrVictor/nco_rtmp)。源码、依赖锁文件、配置模板和 Markdown 文档纳入管理；密码、证书、数据库、录像、原始测试结果、虚拟环境、npm 依赖和构建输出均忽略。

日常修改后先检查并提交明确的文件：

```sh
git status --short
git diff
git add README.md service/app/main.py
git diff --cached --check
git diff --cached
git commit -m "Describe the change"
git push origin main
```

首次基线提交使用自动化作者 `Codex <codex@localhost>`，未修改全局 Git 身份。后续可用自己的身份在当前仓库配置 `git config user.name` 和 `git config user.email`。
