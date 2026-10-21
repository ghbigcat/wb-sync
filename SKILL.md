---
name: wb-sync
description: "在同一 WorkBuddy 账号的不同电脑之间，用 Git 私有仓库同步「可迁移资产与产物」：技能(skills)、身份/记忆(SOUL/IDENTITY/USER/MEMORY)、自动化(automations)、MCP 配置模板、以及指定产物目录。当用户提到换电脑、多机同步、公司电脑与家里电脑、备份 skills、同步 WorkBuddy 配置、迁移 WorkBuddy 环境、另一台机器没有我的技能、产物传不过去，都应加载本 skill。触发词：WorkBuddy同步、多机同步、换电脑、迁移配置、同步skills、同步产物、wb-sync。"
agent_created: true
version: 1.0.0
license: MIT
---

# wb-sync — WorkBuddy 多机资产与产物同步

## 这个 skill 解决什么

WorkBuddy 的**对话记录默认云端同步**，但下面这些东西**只存在本机**：

- `~/.workbuddy/skills/` 下的自建技能
- `SOUL.md` / `IDENTITY.md` / `USER.md` / `MEMORY.md` 身份与记忆
- `automations/` 定时任务定义
- `mcp.json` 连接器配置
- 会话产物（PPT、Excel、报告等实际文件）

换一台电脑、或同时用公司机与家用机时，这些不会自己过去。

> **注意区分**：设置里的「设备连接 / 允许同账号设备远程连接」是**远程查看与遥控**，
> 任务仍跑在原来那台电脑上，产物不会落到新机器本地，技能也不会安装到新机器。
> 它不能替代本 skill。

`wb-sync` 用**一个 Git 私有仓库**做中转，把这批资产与产物真正搬到另一台机器。

## 设计原则（决定了它为什么安全）

| 原则 | 实现 |
|---|---|
| 不碰运行态 | `workbuddy.db`、`binaries/`、`traces/`、`logs/`、`cache/`、`blobs/`、`plugins/` 等硬编码拒绝，配置也无法覆盖 |
| 不污染安装目录 | 独立 staging 目录 `~/.wb-sync/repo`，只在这里 `git init`，`~/.workbuddy` 保持干净 |
| 不泄漏密钥 | `mcp.json` 落仓前自动脱敏为 `${ENV}` 模板；`*.key` / `credentials/` / `keyblob` 一律拒收 |
| 可回滚 | `pull` 前自动备份到 `~/.wb-sync/backups/<时间戳>/`，保留最近 N 份 |
| 幂等 | 增量镜像：跳过未变化文件，只清理本次不再收集的残留，重复 push 秒级完成 |

## 快速开始

### 1. 建一个空的私有仓库

GitHub 新建 **Private** 仓库，例如 `wb-sync-data`（不要勾选 README 初始化）。

### 2. 本机初始化

```bash
python wb-sync.py init --repo https://github.com/<you>/wb-sync-data.git
```

要一起同步产物目录（可多个，用系统路径分隔符）：

```bash
# Windows（分号分隔）
python wb-sync.py init --repo https://github.com/<you>/wb-sync-data.git --artifacts "D:\Output;E:\Deliverables"

# macOS / Linux（冒号分隔）
python wb-sync.py init --repo https://github.com/<you>/wb-sync-data.git --artifacts "$HOME/Deliverables"
```

### 3. 配 token（三选一）

| 方式 | 命令 |
|---|---|
| 已登录 GitHub CLI（最省事） | `gh auth login`，脚本自动取 `gh auth token` |
| 环境变量 | `export WB_SYNC_TOKEN=ghp_xxx` / `setx WB_SYNC_TOKEN ghp_xxx` |
| 复用现有凭据 | 已配置 `credential.helper` 的机器可直接用 |

token 只需 **contents: write** 权限。

### 4. 推送 / 拉取

```bash
python wb-sync.py status      # 先看会传什么、多大、有没有敏感告警
python wb-sync.py push        # 本机 -> 仓库
python wb-sync.py pull --init # 另一台机首次：仓库 -> 本机
python wb-sync.py pull        # 之后每次拉取
```

### 5. 另一台机器

把本目录（或整个 `~/.workbuddy/skills/wb-sync`）复制过去，或直接：

```bash
python wb-sync.py install     # 安装到 ~/.workbuddy/skills/wb-sync
```

然后 `init`（指向同一个仓库）+ `pull --init`。

## 命令一览

| 命令 | 作用 | 是否联网 |
|---|---|---|
| `init` | 生成配置、初始化 staging 仓库、写 `.gitignore` | 否 |
| `status` | 预览分层统计、体积、超限跳过项、敏感告警 | 否 |
| `push` | 收集 -> 脱敏 -> 提交 -> 推送 | 是 |
| `pull` | 拉取 -> 备份 -> 应用 -> 可回滚 | 是 |
| `doctor` | 检查 git / token / 目录 / 硬禁止项污染 | 否 |
| `install` | 安装到 `~/.workbuddy/skills/wb-sync` | 否 |

所有命令支持 `--config <path>`、`--dry-run`、`--log <file>`。

## 同步范围（默认）

```
staging 仓库结构
├── identity/        SOUL.md IDENTITY.md USER.md MEMORY.md
├── skills/          ~/.workbuddy/skills/**（全量，含自建与第三方）
├── automations/     ~/.workbuddy/automations/**
├── mcp/             mcp.json 的脱敏模板
├── artifacts/       <产物根目录>/**(按阈值与排除规则筛选)
├── SYNC-MANIFEST.json
└── .gitignore
```

**永不入库**（硬拒绝，写进配置也无效）：
`workbuddy.db*`、`keyblob`、`device-id`、`credentials/`、`binaries/`、`blobs/`、
`cache/`、`logs/`、`traces/`、`plugins/`、`workspace/`、`local_storage/`、
`security/`、`changes-detail/`、`file-history/`、`node_modules/`、`*.key`、`*.pem`

理由见 `references/manifest.md`。

## 配置要点

见 `config.example.json`。最常改的三个：

| 字段 | 说明 |
|---|---|
| `artifacts.roots` | 要同步的产物根目录数组 |
| `artifacts.max_file_mb` | 单文件体积上限，默认 50；大文件（视频、安装包）建议外置到网盘 |
| `artifacts.exclude` | 额外排除，支持 `**` 通配 |

## 典型工作流

**每天下班前（本机 -> 仓库）**

```bash
python wb-sync.py push
```

**回家开机（仓库 -> 本机）**

```bash
python wb-sync.py pull
```

**装了新技能后**

```bash
python wb-sync.py status   # 确认新技能在列表里
python wb-sync.py push
```

## 坑位清单

| 坑 | 表现 | 处理 |
|---|---|---|
| `workbuddy.db` 双方同时写 | SQLite 锁冲突、无法合并 | 本 skill 已硬拒绝该文件；如需历史任务，走云端而非本工具 |
| 大产物撑爆仓库 | GitHub 单仓建议 < 1GB | 调低 `max_file_mb`，或把视频类目录排除，改用网盘 |
| 两机同时改同一技能 | Git 冲突 | push 前先 `pull`；冲突时按 Git 常规流程解决 |
| token 无写权限 | `403` | PAT 勾选 contents: write；fine-grained 只授权目标仓库 |
| 沙箱环境联网 git 静默失败 | 命令成功但零输出 | 联网部分需在沙箱外执行；见下方说明 |
| 换机器后路径不同 | 配置里残留旧绝对路径 | `workbuddy_dir` / `artifacts.roots` 用 `~` 或按机器改配置 |
| `mcp.json` 落地后不能用 | 值是 `${ENV}` 占位 | 填真实值或设对应环境变量；本工具只传结构，不传密钥 |
| staging 整目录删除被拦 | `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] count:1792` | 已改为**增量镜像**（只删本次不再收集的残留文件），不再整目录 `rmtree` |
| 重复 push 慢 | 每次都重拷数百 MB | 同上：大小 + mtime 相同即跳过，只有疑似变化才回退内容哈希 |
| staging 进入损坏状态 | 文件残留、`git add` 结果异常 | `push --reset` 强制整目录清空一次（会绕过增量逻辑） |

### 关于沙箱内执行

若在受限沙箱（如 WorkBuddy 自身的沙箱）里运行，git 的 https 传输可能被子进程策略掐断，
表现为**命令返回成功但无任何输出**。此时：

1. `status` / `doctor` 可在沙箱内跑（纯本地）
2. `push` / `pull` 需要放开沙箱，且**不要拆成多次调用**——一次进程内完成全部网络动作，
   否则会触发多次授权。

脚本已内置：`GIT_TERMINAL_PROMPT=0`、`GCM_INTERACTIVE=never`、禁用 credential helper、
token 内嵌 URL、每步超时。避免凭据管理器弹窗导致命令挂死。

## 安全边界

- 仓库**必须 Private**。即便已脱敏，`skills/` 与 `identity/` 仍含个人信息。
- `mcp.json` 只传结构，密钥替换为 `${KEY_NAME}`；恢复后需自行填值。
- 本工具不做加密。若需更高保障，可对仓库内容再做一层 `git-crypt`。
- 备份目录 `~/.wb-sync/backups/` 含明文旧版本，按需定期清理。

## 与其他方案的关系

| 方案 | 形态 | 差异 |
|---|---|---|
| 设置里的「设备连接」 | 官方 | 远程查看/遥控，不落地文件、不装技能 |
| 云盘同步 `~/.workbuddy` | 手动 | 简单但 `workbuddy.db` 易冲突，无版本历史 |
| 手动拷贝整个目录 | 手动 | 一次性，不持续 |
| **wb-sync** | skill | Git 版本化、按层白名单、默认脱敏、可回滚 |
