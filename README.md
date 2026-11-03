# Backup Manager MVP v0.2.0

单仓库备份/恢复工具。各服务仓库只维护 `.backup.yaml`，中心仓库发布 `backup.py` 和 `stable.json`。本版不需要 PVE 才能运行。

## 是否新建仓库？

**建议新建独立的 `backup-manager` 仓库。** 用于存放和发布统一工具、测试与版本清单；各服务自己的 `.backup.yaml` 留在各自的仓库。工具可部署到现有主仓库，但工具版本会和服务数据版本耦合，不推荐。

## 交付文件

- `backup.py`: SQLite VACUUM INTO / backup API、原子快照替换、quick_check、仅缺失恢复、Git Commit/Push、Compose up/down/check。
- `service.sh`: 通过 URL 运行的通用启动器，按 `stable.json` 下载和 SHA-256 验证工具；支持离线缓存；首次缺少 PyYAML 时创建公用 venv 安装依赖。
- `stable.json`: 发布清单，含工具 URL、版本、SHA-256。
- `examples/memos/.backup.yaml`、`examples/linkding/.backup.yaml`: 配置示例。
- `tests/test_mvp.py`: 自动化基础测试。

## 新建中央仓库并发布

1. 创建一个 **推荐公开读取** 的 `backup-manager` 仓库（不用公开服务数据仓库）。上传 `backup.py`、`stable.json`。最初也可把全部项目推入这个仓库。
2. `stable.json` 中将 `OWNER` 换成实际 GitHub 用户或组织；确保 `url` 指向真实的 `backup.py` 下载路径。更新 `backup.py` 后，用 `sha256sum backup.py` 计算新值并更新 `stable.json.sha256`；若发布新功能，请同时改 `version`。
3. 各业务仓库只加入 `.backup.yaml`，调整路径、分支、权限及 Compose 文件。`service.sh` 放在统一工具仓库通过 URL 下载。
4. 在服务仓库根目录执行 `curl -fsSL https://raw.githubusercontent.com/OWNER/backup-manager/main/service.sh | bash -s -- check`（将 OWNER 替换成实际用户名）。其他子命令同理。

**更新策略**：中央仓库提交新的 `backup.py`，更新 `stable.json` 的版本与 SHA-256；各节点下一次执行时会读取清单、校验并缓存新版工具。公网 URL 必须 HTTPS；脚本执行中央发布的代码，务必限制中央仓库写权限。生产环境建议将稳定清单提交变更纳入审查，先在测试节点验证。若下载清单失败，则使用本地缓存的清单和已缓存文件。

**私有中央仓库**：匿名 HTTPS raw 地址不可用。请先在内部受控 HTTPS 域名发布这两个文件，或者修改启动器使用带身份验证的下载方式。不要将 GitHub Token 写进业务仓库。

## 本地测试（不访问远程工具 URL）

在服务仓库内（本地脚本通过标准输入执行）：

```bash
BACKUP_MANAGER_LOCAL=/path/to/backup-manager/backup.py bash /path/to/backup-manager/service.sh check
BACKUP_MANAGER_LOCAL=/path/to/backup-manager/backup.py bash /path/to/backup-manager/service.sh restore
BACKUP_MANAGER_LOCAL=/path/to/backup-manager/backup.py bash /path/to/backup-manager/service.sh backup
```

本地模式需先安装 PyYAML：`python3 -m pip install 'PyYAML>=6,<7'`（推荐在虚拟环境中执行）。

## 命令

```bash
# 以下命令应在含 .backup.yaml 的仓库根目录执行；OWNER 是占位符
curl -fsSL https://raw.githubusercontent.com/OWNER/backup-manager/main/service.sh | bash -s -- check
curl -fsSL https://raw.githubusercontent.com/OWNER/backup-manager/main/service.sh | bash -s -- backup
curl -fsSL https://raw.githubusercontent.com/OWNER/backup-manager/main/service.sh | bash -s -- restore
curl -fsSL https://raw.githubusercontent.com/OWNER/backup-manager/main/service.sh | bash -s -- restore --dry-run
curl -fsSL https://raw.githubusercontent.com/OWNER/backup-manager/main/service.sh | bash -s -- up
```

注意：`curl | bash` 是将下载的启动器直接执行；只有在你完全信任中央仓库且允许其执行代码时才使用。HTTPS 和 `stable.json` 的 SHA-256 仅验证 `backup.py`，不能防范中央发布源本身被恶意修改。更严格的生产方案应为启动器固定校验签名、下载后独立验证，或经内部可信发布服务提供。首次运行需要联网，之后如需离线执行需使用已缓存的引擎直接调用。

`backup.py` 自身也可运行：`python3 backup.py --repo /path/to/service backup`。

## 重要安全和恢复约束

- **已有源数据库永不覆盖**。源文件不存在，但存在孤立的 `-wal/-shm`，会停止以避免数据损坏。恢复操作会校验快照，并要求 Docker Compose 没有正在运行的服务。
- 若数据库缺失且快照也缺失，`up` 会失败而不是启动一个空数据库。
- `.gitignore` 忽略运行中的数据库和 `-wal/-shm` 文件；只提交快照。正确的容器内 UID/GID 可能需要在 `restore_uid`, `restore_gid`, `restore_mode` 中指定。
- `git.paths` 明确列出允许自动暂存的路径，防止 `git add .` 导致敏感信息或临时文件被意外提交。`.env` 这类敏感文件不要随意配置进备份路径。
- Git 在提交之前 fetch 远端并拒绝自动合并远端领先/分叉的提交；若有既存 staged 更改，会拒绝生成备份提交。网络失败后下次仍会尝试 push 之前的提交。需要本地配置 Git user.name/user.email 以及无交互 Git Push 凭据。
- **还不是完整灾难恢复工具**：本版本仅支持 SQLite 与 Docker Compose；不管理 PostgreSQL、MySQL、文件附件、容器内匿名 volume、加密密钥、集中节点发现或 PVE Submodule 指针更新。
- 多个数据库分别快照，并非严格全局同一时刻；一次恢复也不对多个文件实现整体事务式回滚。
- 推荐在非生产克隆上测试 `restore/up`，并确认持久数据挂载和容器权限。不要在正在运行的数据库服务上强制复制覆盖数据库文件。

## 后续迭代

1. 仓库发现与 PVE SSH 调度（定时执行每个服务仓库中的 `./service.sh backup`）。
2. 主仓库 Submodule 统一记录各服务备份成功提交的版本。
3. 增加文件附件/配置的可验证备份、PostgreSQL 支持、Restic、日志通知和恢复清单。

## 零文件启动方式与限制

业务仓库无需 `service.sh`，只保留 `.backup.yaml`。统一维护的 `service.sh` 是 HTTP 下载入口（仍需 GitHub 用户名换成实际值），然后它获取 `stable.json` 和 `backup.py`；更新 `service.sh` 会在**下一次调用**时生效，更新 `backup.py` 必须同步更新清单和 SHA-256。正在执行的任务不会热更新。

`bash -s --` 后的参数直接透传给 `backup.py`。务必在仓库根目录运行，不支持随意从其他目录调用。脚本的执行身份必须具有 SQLite 文件读写权限和（启用时）Git Push 权限。`backup.py` 依赖 PyYAML；如系统没有则启动器会尝试创建虚拟环境并安装。

当前示例仓库是公开读取的分发方案；私有仓库的 Raw URL 不提供匿名访问。
