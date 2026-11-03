# Backup Manager v0.2.0

集中维护的 SQLite 备份、恢复与 Docker Compose 管理工具。每个服务目录只维护一个备份配置 `.backup.yaml`，通过统一 HTTP 入口执行；运行库、快照、Compose 文件和 Git 凭据由服务本身提供。

支持 Linux / macOS、Python 3.10+、Git 和 Docker Compose v2。Python 依赖见 `requirements.txt`。`vacuum` 方法需要 SQLite 3.27+；较旧的 SQLite 可使用 `backup_api`。

## 单个服务的配置

在服务目录放置 `.backup.yaml`：

```yaml
version: 1

databases:
  - type: sqlite
    source: data/app.db
    snapshot: backups/app.db
    method: vacuum               # vacuum 或 backup_api
    timeout_seconds: 300         # 每个数据库备份的 SQLite 操作时间预算
    restore_mode: '0600'
    # restore_uid: 1000          # 按容器实际运行身份设置
    # restore_gid: 1000

restore:
  policy: if_missing

git:
  enabled: true
  remote: origin
  branch: master                 # 必须与服务仓库当前分支一致
  message: 'backup: snapshot'
  paths:                         # 额外提交的确切文件，可省略
    - docker-compose.yaml

deploy:
  enabled: true
  compose_file: docker-compose.yaml
```

所有路径相对于服务目录。快照文件和 `.backup.yaml` 自动加入提交，`git.paths` 只需列额外文件；旧配置重复列出快照和配置文件也兼容。该列表不接受目录、通配符、Git pathspec 或源数据库及其边车文件。敏感文件只有在你明确配置时才会纳入，因此不要加入 `.env`、密钥等文件。

工具根据 `source` 自动维护 Git 本地的 `.git/info/exclude` 生成块，忽略源库、WAL/SHM/journal、任务锁和自身临时文件，保留用户原有规则。无需另外维护备份专用 `.gitignore`。如果运行数据已经被 Git 跟踪，工具会停止并要求手动处理，不会自动删除已有跟踪记录。快照必须位于可被 Git 暂存的位置，工具不会强制绕过现有忽略规则。

只有备份需求时，可以设置 `deploy.enabled: false`；不使用 Git 时设置 `git.enabled: false`。配置中错误的类型、重复 YAML 键、未知字段及文件路径冲突会在执行前被拒绝。

完整示例见 `examples/memos/.backup.yaml` 和 `examples/linkding/.backup.yaml`。应用数据库位置、Compose 挂载和容器 UID/GID 需要按实际服务核对。

## 通过 curl 执行

Debian/Ubuntu 最小安装环境建议先安装系统 PyYAML，启动器会直接使用它，无需创建虚拟环境：

```bash
apt-get update && apt-get install -y python3-yaml
python3 -c 'import yaml; print(yaml.__version__)'
```

如果希望由启动器管理虚拟环境，先安装与当前解释器匹配的 venv 包，例如 Python 3.13 使用 `python3.13-venv`。缺少 ensurepip 时，失败的创建操作可能留下只有 Python、没有 pip 的缓存目录；修复后的启动器会探测并重新创建不完整的环境，无需手动删除缓存。系统安装 `python3-yaml` 也可以直接绕过这种残留环境。

修改推送到中央仓库后，可直接使用 Raw 入口：

```bash
cd /srv/my-service
curl -fsSL https://raw.githubusercontent.com/ahzknarf/backup_manager/master/service.sh | bash -s -- backup
```

启动器默认从实际仓库 `ahzknarf/backup_manager` 的 `master` 分支读取开发清单。所有命令都可以通过 `bash -s --` 传入：

```bash
# 在服务目录内执行；URL 为下一节启用 Pages 后的入口
curl -fsSL https://ahzknarf.github.io/backup_manager/run | bash -s -- check
curl -fsSL https://ahzknarf.github.io/backup_manager/run | bash -s -- check --strict
curl -fsSL https://ahzknarf.github.io/backup_manager/run | bash -s -- backup
curl -fsSL https://ahzknarf.github.io/backup_manager/run | bash -s -- restore --dry-run
curl -fsSL https://ahzknarf.github.io/backup_manager/run | bash -s -- restore
curl -fsSL https://ahzknarf.github.io/backup_manager/run | bash -s -- up
curl -fsSL https://ahzknarf.github.io/backup_manager/run | bash -s -- down

# 可以从任意目录指定服务位置；全局选项放在子命令之前
curl -fsSL https://ahzknarf.github.io/backup_manager/run | bash -s -- --repo /srv/my-service --lock-timeout 30 backup
```

`check` 校验配置和已经存在的数据库；尚未生成快照时只报告状态。`check --strict` 要求至少配置一个数据库，且每个快照均存在、非空并通过 SQLite 校验；如果启用部署，还要求 Compose 文件存在。源库不存在而快照完好的状态可通过严格检查，表示可以尝试恢复；孤立边车文件会使检查失败。

`restore` 只恢复缺失的源库，保留已有文件。`up` 先检查部署开关和 Compose 文件，再恢复缺失数据库并执行 `docker compose up -d`。`down` 保留 Docker volumes。禁用部署时，`up/down` 都会在修改数据之前退出。

`curl | bash` 会执行远程入口脚本，入口发布源需要可信；清单中的 SHA-256 校验保护下载完整性，不替代发布源的信任或签名验证。入口获取失败时，外层调用方还应通过 `pipefail` 检测 curl 的失败状态。

## 发布短入口

仓库提供 `.github/workflows/pages.yml`，由手动运行工作流或推送 `v*` 标签触发。它先运行测试并核对清单，再生成和部署仅含启动器、清单的 Pages 站点。

1. 将修改提交并推送到这个仓库。
2. 在 GitHub 仓库的 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。
3. 在 **Actions → Publish backup launcher → Run workflow** 发布已经提交的版本。
4. 发布成功后使用 `https://ahzknarf.github.io/backup_manager/run`。本地存在工作流文件并不代表该地址已经上线。

发布出的 `run` 自动从同一站点获取 `stable.json`；清单固定到该次发布的完整 Git commit URL，并校验 `backup.py` 的 SHA-256，避免脚本和清单在分支更新期间不一致。源码 Raw 入口使用的是开发清单；稳定节点建议使用 Pages 入口。

如果有自己的域名，在 Pages 设置中配置自定义域名、DNS 和 HTTPS，再重新运行发布工作流。工作流会根据 Pages 实际站点地址生成清单入口。例如配置完成后可以使用：

```bash
curl -fsSL https://bk.example.com/run | bash -s -- backup
```

`bk.example.com` 是示意域名。也可通过现有 HTTPS 服务反向代理 `/run`；如果只是把 GitHub 入口重定向到短域名，它仍会使用原入口内的清单地址。DNS CNAME 到 Raw 主机本身不能完成 HTTPS 与路径映射。

## 缓存、离线与更新

启动器先准备 PyYAML：优先使用当前 Python 已有的依赖，其次复用缓存 venv 中可正常导入的依赖。需要安装时，会先确认缓存解释器是虚拟环境且有可用的 pip；缺失或不完整时，重新执行 venv 创建，再安装固定的 `PyYAML==6.0.3` 并验证导入。如果创建失败，会显示系统依赖安装命令，不会直接向缺失 pip 的环境安装包。本地模式不会安装依赖。

每个清单地址使用独立缓存；引擎以 SHA-256 命名，不会覆盖正在执行的旧版本。只有清单字段、下载文件哈希和 Python 语法全部通过校验后，才原子更新当前清单，同时保留上一可用版本。清单下载失败、更新文件缺失、哈希不符或缓存损坏时，尝试使用已经验证的当前版本或上一版本；没有任何可验证版本则退出。

| 环境变量 | 用途 |
| --- | --- |
| `BACKUP_MANAGER_MANIFEST_URL` | 覆盖中央清单地址，必须是 HTTPS |
| `BACKUP_MANAGER_CACHE_DIR` | 公用缓存目录，默认 `~/.cache/backup-manager` |
| `BACKUP_MANAGER_OFFLINE=1` | 使用已验证缓存，不下载清单、引擎或 pip 包 |
| `BACKUP_MANAGER_LOCAL` | 使用指定的本地 `backup.py`，跳过远程分发与依赖安装 |

首次运行需要网络和可用的 Python venv/pip。`curl` 获取入口本身需要网络；离线时需执行集中安装的 `service.sh`，或直接用相应 Python 调用已缓存的引擎。不同清单地址的缓存互不借用；从旧版缓存结构升级后需要先联网初始化一次。

任务级锁位于服务目录 `.backup-manager.lock`，覆盖检查、备份、Git、恢复和 Compose 操作。默认遇到重叠任务立即失败，可用 `--lock-timeout` 等待。锁文件是自动生成的运行文件，正常退出后仍保留，请勿删除正在使用的锁文件。

## 本地开发与发布校验

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
bash -n service.sh

# 修改 backup.py 后更新开发清单版本、URL 与 SHA
.venv/bin/python scripts/release.py manifest
.venv/bin/python scripts/release.py manifest --check

# 本地调用服务目录，不下载远程工具
.venv/bin/python backup.py --repo /srv/my-service check
BACKUP_MANAGER_LOCAL="$PWD/backup.py" PATH="$PWD/.venv/bin:$PATH" bash service.sh --repo /srv/my-service backup

# 构建已提交版本的 Pages 产物；输出目录必须为空
.venv/bin/python scripts/release.py pages --base-url https://ahzknarf.github.io/backup_manager --output dist
```

`backup.py` 的 `VERSION` 是发布版本来源；新增功能时更新它，然后运行 `manifest` 命令。CI 会核对仓库中的 `stable.json`，避免忘记更新 SHA。Pages 构建还会拒绝引擎或启动器与指定提交不一致的工作区，防止未提交代码被错误标记为旧提交。

测试使用临时 SQLite、本地 Git bare 仓库和模拟下载；Docker 状态通过命令结果模拟，测试不会启动或停止生产容器。覆盖路径碰撞、WAL 数据、超时、任务锁、恢复竞态、Git 失败重试、缓存回退和发布产物。

## 备份与恢复边界

- 目前覆盖 SQLite、Git 和 Compose；PostgreSQL、MySQL、附件目录、匿名 volume、加密密钥、PVE 调度尚未实现，需要单独备份。
- SQLite 热备份包含已经提交的 WAL 数据；快照必须是独立数据库，存在 WAL/SHM/journal 边车文件时拒绝替换或恢复。
- `timeout_seconds` 限制单库备份的 SQLite 工作和锁等待；文件系统 I/O、fsync 或内核阻塞不属于可精确中断的时间预算。
- Git 在创建快照前检查分支、既有暂存内容、运行库跟踪状态和远端历史，拒绝自动合并远端领先或分叉。
- 自动提交使用独立暂存索引，并占用 Git 标准 index lock。暂存或提交失败保留原索引；提交成功而推送失败时，下次任务会重推已有提交。进程被强制终止后若残留 Git index lock，需要先确认无 Git 进程，再人工处理。
- 恢复使用原子、不覆盖目标的链接操作；容器运行、暂停、重启、状态未知或查询失败时拒绝恢复。`deploy.enabled: false` 的手动恢复由调用者确保数据库没有被其他进程使用。
- 多个数据库分别快照，未实现跨库全局一致性；部分备份失败时不提交 Git，但已经成功生成的快照会保留。多个数据库恢复也不是整体事务，需做真实的恢复演练。
- SQLite `quick_check` 验证结构完整性，不验证应用业务内容。Git 备份保留历史数据，仓库应私有并配置恰当的访问权限、容量管理和无交互认证。
