# wb-sync

把 WorkBuddy 的**技能、身份记忆、自动化、产物**，在同一账号的不同电脑之间用 Git 私有仓库同步。

Sync your WorkBuddy **skills, identity/memory, automations and deliverables** across
computers via a private Git repo.

---

## 为什么需要它

WorkBuddy 的对话记录走云端，但下面这些**只在本机**，换电脑就没了：

- `~/.workbuddy/skills/` 自建技能
- `SOUL.md` / `IDENTITY.md` / `USER.md` / `MEMORY.md`
- `automations/` 定时任务
- `mcp.json` 连接器配置
- 会话产物（PPT / Excel / 报告等实体文件）

设置里的「设备连接 → 允许同账号设备远程连接」是**远程查看与遥控**：任务仍在原电脑执行，
产物不会落到新机器，技能也不会装到新机器。它是遥控器，不是同步。

## 特性

| 特性 | 说明 |
|---|---|
| 分层白名单 | 只同步 5 类可迁移资产，运行态文件硬拒绝（配置也改不了） |
| 不污染安装目录 | 独立 staging 仓库 `~/.wb-sync/repo`，`~/.workbuddy` 保持干净 |
| 默认脱敏 | `mcp.json` 中 token / secret 自动替换为 `${ENV}`；`*.key`、`credentials/` 拒收 |
| 可回滚 | `pull` 前自动备份，保留最近 N 份 |
| 幂等镜像 | 增量同步：跳过未变化文件，只清理不再收集的残留，重复 push 秒级完成 |
| 跨平台 | Windows / macOS / Linux，只依赖 Python 3.8+ 与 git |
| 零配置依赖 | token 可复用 `gh auth token`，无需另建 PAT |

## 安装

```bash
# 从 GitHub 直接安装成 WorkBuddy skill（推荐，一行）
git clone https://github.com/ghbigcat/wb-sync.git ~/.workbuddy/skills/wb-sync

# 或用发布包解压到 ~/.workbuddy/skills/wb-sync
```

装好后在 WorkBuddy 里说「同步我的产物」即可触发；也可直接用命令行：

```bash
# 方式一：直接跑
python wb-sync.py doctor

# 方式二：装成 WorkBuddy skill
python wb-sync.py install     # -> ~/.workbuddy/skills/wb-sync
```

要求：Python 3.8+、git。Windows 若用 WorkBuddy 自带 PortableGit，脚本会自动探测并设置
`GIT_EXEC_PATH`。

## 使用

```bash
# 1. 建一个 Private 空仓库（如 wb-sync-data），不要勾选初始化 README
# 2. 初始化（产物目录可选，多个用系统路径分隔符）
python wb-sync.py init --repo https://github.com/<you>/wb-sync-data.git \
                       --artifacts "D:/WorkBuddy-Output"

# 3. 配 token（三选一）
gh auth login                     # 最省事，脚本自动取 gh auth token
export WB_SYNC_TOKEN=ghp_xxx      # Windows: setx WB_SYNC_TOKEN ghp_xxx

# 4. 日常
python wb-sync.py status          # 预览：传什么、多大、有无敏感告警
python wb-sync.py push            # 本机 -> 仓库
python wb-sync.py pull            # 仓库 -> 本机
python wb-sync.py doctor          # 体检
```

另一台机器：复制本目录过去，`init` 指向**同一个仓库**，然后 `pull --init`。

## 命令

| 命令 | 作用 | 联网 |
|---|---|---|
| `init` | 生成配置、初始化 staging、写 `.gitignore` | 否 |
| `status` | 分层统计、体积、超限跳过项、敏感告警 | 否 |
| `push` | 收集 -> 脱敏 -> 提交 -> 推送 | 是 |
| `pull` | 拉取 -> 备份 -> 应用 | 是 |
| `doctor` | git / token / 目录 / 禁止项污染检查 | 否 |
| `install` | 安装到 `~/.workbuddy/skills/wb-sync` | 否 |

通用参数：`--config <path>`、`--dry-run`、`--log <file>`。

## 同步范围

```
identity/       SOUL.md IDENTITY.md USER.md MEMORY.md
skills/         ~/.workbuddy/skills/**
automations/    ~/.workbuddy/automations/**
mcp/            mcp.json 的脱敏模板
artifacts/      <产物根目录>/**（按阈值与排除规则）
SYNC-MANIFEST.json
```

**硬拒绝**（写进配置也无效）：
`workbuddy.db*`、`keyblob`、`device-id`、`credentials/`、`binaries/`、`blobs/`、`cache/`、
`logs/`、`traces/`、`plugins/`、`workspace/`、`local_storage/`、`security/`、
`changes-detail/`、`file-history/`、`node_modules/`、`*.key`、`*.pem`

完整清单与取舍理由见 [`references/manifest.md`](references/manifest.md)。

## 常见问题

**Q：为什么不直接同步 `workbuddy.db`？**
它是 SQLite，两台机同时写会锁冲突甚至损坏；而且对话记录本来就云端同步。本工具只搬
「云端没有、又必须带走」的部分。

**Q：大文件（视频、安装包）怎么办？**
调低 `artifacts.max_file_mb`（默认 50）把大文件排除，另行放到网盘或对象存储。Git 仓库
建议控制在 1GB 以内。

**Q：`mcp.json` 恢复后连不上？**
这是设计如此：仓库里存的是 `${ENV}` 占位，真实密钥不出本机。恢复后填入真值或设环境变量。

**Q：两机同时改同一个技能怎么办？**
`push` 前先 `pull`。真冲突时按 Git 常规流程解决，备份在 `~/.wb-sync/backups/`。

**Q：在受限沙箱里 push 没反应？**
沙箱可能掐断 git 的 https 子进程，表现为「命令成功但零输出」。`status` / `doctor` 可正常跑；
`push` / `pull` 需在放开网络限制的环境执行，且一次进程内完成全部网络动作。

## 与其它方案对比

| 方案 | 形态 | 差异 |
|---|---|---|
| 官方「设备连接」 | 内置 | 远程查看/遥控，文件不落地 |
| 云盘同步 `~/.workbuddy` | 手动 | 简单，但数据库冲突、无版本历史 |
| 整目录手动拷贝 | 手动 | 一次性，不持续 |
| 本工具 | skill | Git 版本化、分层白名单、默认脱敏、可回滚 |

## License

MIT
