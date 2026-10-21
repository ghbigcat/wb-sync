# wb-sync 同步范围与取舍

本文说明「传什么、不传什么、为什么」，以及这些边界在代码里的落地位置。

## 一、分层模型

| 层 | staging 路径 | 源路径 | 默认 | 说明 |
|---|---|---|---|---|
| identity | `identity/` | `~/.workbuddy/{SOUL,IDENTITY,USER,MEMORY}.md` | 开 | 只取顶层 4 个文件，不递归根目录 |
| skills | `skills/` | `~/.workbuddy/skills/**` | 开 | 自建与第三方技能一并迁移 |
| automations | `automations/` | `~/.workbuddy/automations/**` | 开 | 定时任务定义（不含运行日志） |
| mcp | `mcp/mcp.json` | `~/.workbuddy/mcp.json` | 开 | 落仓前脱敏为模板 |
| artifacts | `artifacts/<根目录标签>/` | 用户指定 | 关 | 需显式开启，按体积阈值筛选 |

`identity` 层特意只做「顶层精确匹配」(source = `"."`)：如果对 `~/.workbuddy` 做递归，
会立刻扫到 `binaries/`（数 GB）、`traces/`（数百 MB）等目录。

## 二、硬拒绝清单

以下名称在收集阶段直接过滤，**无论配置怎么写都不会被同步**。

### 目录

```
binaries           工作空间二进制运行时（4GB 级）
blobs              产物内部缓存副本（产物本身走 artifacts 层）
cache              各类缓存
logs               运行日志
traces             调用链追踪
plugins            插件缓存（含多版本副本）
workspace          WorkBuddy 内部工作区（GB 级）
credential*        凭据
security           证书与安全材料
local_storage      本地存储（含登录态）
changes-detail     文件变更明细
changes-index      变更索引
file-history       文件历史快照
file-tree-manifests
shell-snapshots
storage
connectors-marketplace
skills-marketplace
skills-archive
plugin-marketplace-state
plugin-marketplace-state-new
app  appearance-resources  vendor  pending-telemetry
node_modules  __pycache__  .git
```

### 文件

```
workbuddy.db  workbuddy.db-shm  workbuddy.db-wal
keyblob
device-id
login-device-nonce
edge-sync-mapping-v4.db (+ -shm / -wal)
user-state.json  last-launch.json  failover.json
```

### 通配

```
*.key  *.pem  *.pfx  *.p12  .master.key
_*    *.tmp  .DS_Store
```

## 三、为什么排除这些

| 对象 | 理由 |
|---|---|
| `workbuddy.db`（SQLite） | 双机同时写会产生锁冲突，Git 无法合并二进制数据库；对话记录本身云端同步 |
| `binaries/` | 每台机器按平台/架构应有自己的运行时，同步既浪费又可能不兼容 |
| `plugins/` / `skills-marketplace/` | 插件市场缓存，体量大且可重新下载；且含多版本副本 |
| `blobs/` | 产物内部副本，与真实产物目录重复 |
| `logs/` `traces/` `changes-*` `file-history/` | 调试与审计数据，机器绑定，无迁移价值 |
| `local_storage/` `security/` `credentials/` `keyblob` | 登录态与密钥，跨机复用既无效也不安全 |
| `user-state.json` `last-launch.json` | 本机启动计数与状态，迁移会干扰 UI 行为 |

## 四、脱敏规则

`assets.mcp.mode = "template"` 时，写入 staging 前逐层遍历 JSON：

1. 键名命中 `token|secret|password|passwd|api_key|authorization|bearer|private`（不区分大小写）
   且值为非空字符串 -> 替换为 `${KEY_NAME}`
2. 值形如 `ghp_` / `ghs_` / `sk-` / `xoxb-` 等已知前缀 -> 同样替换
3. 其它键原样保留

例：

```json
{"mcpServers": {"gh": {"env": {"GITHUB_TOKEN": "ghp_real", "MODE": "fast"}}}}
```

落仓后：

```json
{"mcpServers": {"gh": {"env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}", "MODE": "fast"}}}}
```

`pull` 时仅当本机**不存在** `mcp.json` 才落地模板，避免覆盖已有真值。

## 五、产物层

- `artifacts.roots` 中每个根目录在 staging 下映射为 `artifacts/<目录名标签>/`
- 标签 = 目录名，非字母数字字符替换为 `_`（保证跨平台可移植）
- 单文件超过 `max_file_mb` 直接跳过，并在 `status` 中列出
- `pull` 时按同一标签映射回本机对应根目录

## 六、备份与回滚

- 触发点：`pull` 且 `options.backup_before_pull = true`
- 路径：`~/.wb-sync/backups/<YYYYMMDD-HHMMSS>/`
- 内容：各 `assets.*.source` 的完整副本（同样应用硬拒绝过滤）
- 保留：最近 `options.backup_keep`（默认 5）份，更早的自动删除

回滚方式：把备份目录里的对应层覆盖回 `~/.workbuddy`。

## 七、与代码的对应关系

| 规则 | 代码位置 |
|---|---|
| 硬拒绝目录/文件/通配 | `HARD_DENY_DIRS` / `HARD_DENY_FILES` / `HARD_DENY_GLOBS` |
| 收集逻辑与 include/exclude | `collect()` |
| 脱敏 | `sanitize_mcp()` |
| 镜像回写 | `prune_staging()`（只删残留）+ `apply_items()`（跳过未变化） |
| 备份 | `backup_targets()` |
| 应用回本机 | `apply_from_staging()` |
