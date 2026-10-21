#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb-sync — WorkBuddy 多机资产与产物同步（Git 原生 / skill 形态 / 跨平台）

设计约束：
  1. 只同步「可迁移资产 + 产物」，绝不碰运行态文件（workbuddy.db / caches / traces）。
  2. 镜像式 staging：不污染 ~/.workbuddy，push 只写 staging 目录，pull 前先备份。
  3. 敏感内容默认脱敏：mcp.json 落盘为模板，credentials/ 与 *.key 一律拒收。
  4. Git 网络层复用已验证方案：内嵌 token URL + 禁用 credential helper + 超时。

用法：
  wb-sync.py init            生成配置（首次）
  wb-sync.py status          预览差异、体积、敏感项告警
  wb-sync.py push            收集 -> 提交 -> 推送
  wb-sync.py pull            拉取 -> 备份 -> 应用
  wb-sync.py doctor          环境与冲突体检
  wb-sync.py install         把本 skill 安装到 ~/.workbuddy/skills/wb-sync

所有命令均支持 --config <path>、--dry-run、--log <file>。
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

IS_WIN = os.name == "nt"
HOME = os.path.expanduser("~")
DEFAULT_WORKBUDDY = os.path.join(HOME, ".workbuddy")
DEFAULT_CONFIG = os.path.join(HOME, ".wb-sync", "config.json")
DEFAULT_STAGING = os.path.join(HOME, ".wb-sync", "repo")
BACKUP_ROOT = os.path.join(HOME, ".wb-sync", "backups")

# ---------------------------------------------------------------- 默认策略
# 这些是「永不入仓」的运行态与机器绑定目录，任何配置都无法覆盖。
HARD_DENY_DIRS = {
    "binaries", "blobs", "cache", "logs", "traces", "plugins", "workspace",
    "credentials", "security", "local_storage", "node_modules", "__pycache__",
    ".git", "changes-detail", "changes-index", "file-history", "file-tree-manifests",
    "shell-snapshots", "storage", "connectors-marketplace", "skills-marketplace",
    "skills-archive", "plugin-marketplace-state", "plugin-marketplace-state-new",
    "app", "appearance-resources", "vendor", "pending-telemetry",
}
HARD_DENY_FILES = {
    "workbuddy.db", "workbuddy.db-shm", "workbuddy.db-wal",
    "keyblob", "device-id", "login-device-nonce",
    "edge-sync-mapping-v4.db", "edge-sync-mapping-v4.db-shm", "edge-sync-mapping-v4.db-wal",
    "user-state.json", "last-launch.json", "failover.json",
}
HARD_DENY_GLOBS = ["*.key", "*.pem", "*.pfx", "*.p12", ".master.key", "_*", "*.tmp", ".DS_Store"]

# staging 中受管的顶层目录（增量镜像只在这些目录内做残留清理）
ASSET_KEYS = ("identity", "skills", "automations", "mcp", "artifacts")

# 默认产物目录排除
DEFAULT_ARTIFACT_EXCLUDES = [
    "**/node_modules/**", "**/.git/**", "**/__pycache__/**", "**/.venv/**",
    "**/.ref/**", "**/dist/**", "**/build/**", "**/.next/**",
    "**/*.tmp", "**/*.log", "**/_*", "**/.DS_Store", "**/Thumbs.db",
]


def now_stamp(fmt="%Y-%m-%d %H:%M:%S"):
    return dt.datetime.now().strftime(fmt)


def human(nbytes):
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024 or unit == "GB":
            return ("%.1f %s" % (nbytes, unit)) if unit != "B" else ("%d B" % nbytes)
        nbytes /= 1024.0


# ---------------------------------------------------------------- 日志
class Log:
    def __init__(self, path=None):
        self.lines = []
        self.path = path

    def __call__(self, msg=""):
        self.lines.append(str(msg))
        print(msg)

    def flush(self):
        if not self.path:
            return
        d = os.path.dirname(os.path.abspath(self.path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lines) + "\n")


# ---------------------------------------------------------------- 配置
def default_config():
    return {
        "version": 1,
        "repo": "",
        "branch": "main",
        "staging_dir": DEFAULT_STAGING,
        "workbuddy_dir": DEFAULT_WORKBUDDY,
        "token_env": "WB_SYNC_TOKEN",
        "assets": {
            "identity": {
                "enabled": True,
                "source": ".",
                "include": ["SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md"],
            },
            "skills": {
                "enabled": True,
                "source": "skills",
                "include": ["*"],
                "exclude": [],
            },
            "automations": {
                "enabled": True,
                "source": "automations",
                "include": ["**/*"],
                "exclude": [],
            },
            "mcp": {
                "enabled": True,
                "source": "mcp.json",
                "mode": "template",
            },
        },
        "artifacts": {
            "enabled": False,
            "roots": [],
            "include": ["**/*"],
            "exclude": DEFAULT_ARTIFACT_EXCLUDES,
            "max_file_mb": 50,
        },
        "options": {
            "backup_before_pull": True,
            "backup_keep": 5,
            "commit_message": "wb-sync: {date}",
        },
    }


def deep_merge(base, override):
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path, log):
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        user = json.load(f)
    cfg = deep_merge(default_config(), user)
    cfg["workbuddy_dir"] = os.path.expanduser(cfg["workbuddy_dir"])
    cfg["staging_dir"] = os.path.expanduser(cfg["staging_dir"])
    cfg["artifacts"]["roots"] = [os.path.expanduser(r) for r in cfg["artifacts"].get("roots", [])]
    return cfg


def save_config(cfg, path):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------------------------------------------------------------- Git
class Git:
    # Windows 上 schannel 后端传大包常报 "server closed abruptly (missing close_notify"，
    # 这组参数把它压到可控范围：加大 postBuffer、锁 HTTP/1.1、放宽低速判定。
    HTTP_HARDEN = [
        "http.postBuffer=524288000",
        "http.version=HTTP/1.1",
        "http.lowSpeedLimit=1000",
        "http.lowSpeedTime=600",
    ]

    def __init__(self, cwd, log, timeout=180):
        self.cwd, self.log, self.timeout = cwd, log, timeout
        self.exe, self.portable = self._find()
        self.env = self._env()
        self.url = ""

    def _find(self):
        cands = [shutil.which("git")]
        for base in (os.path.join(HOME, ".workbuddy", "binaries", "PortableGit", "versions"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git")):
            if os.path.isdir(base):
                for ver in sorted(os.listdir(base), reverse=True):
                    for sub in ("mingw64/bin/git.exe", "cmd/git.exe", "bin/git.exe"):
                        cands.append(os.path.join(base, ver, sub.replace("/", os.sep)))
        cands += [r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files (x86)\Git\cmd\git.exe",
                  "/usr/bin/git", "/usr/local/bin/git", "/opt/homebrew/bin/git"]
        for c in cands:
            if c and os.path.isfile(c):
                return c, "PortableGit" in c
        raise SystemExit("[X] 未找到 git，请先安装 https://git-scm.com/download/win 或 brew install git")

    def _env(self):
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        env["GIT_ASKPASS"] = "echo"
        if self.portable:
            mingw = os.path.dirname(self.exe)
            root = os.path.dirname(os.path.dirname(mingw))
            env["GIT_EXEC_PATH"] = mingw
            env["PATH"] = mingw + os.pathsep + os.path.join(root, "cmd") + os.pathsep + env.get("PATH", "")
        return env

    def set_remote(self, url, token_env="WB_SYNC_TOKEN"):
        """token 只内嵌在内存中的 URL，不写入 .git/config。"""
        tok = os.environ.get(token_env, "").strip()
        if not tok:
            tok = self._gh_token()
        if tok and url.startswith("https://") and "@" not in url.split("//", 1)[1].split("/", 1)[0]:
            self.url = "https://x-access-token:%s@%s" % (tok, url[len("https://"):])
        else:
            self.url = url
        return bool(tok)

    def _gh_token(self):
        try:
            p = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=30, env=self.env)
            return (p.stdout or "").strip()
        except Exception:
            return ""

    def run(self, *args, echo=True, allow_fail=False, timeout=None, extra_cfg=()):
        cmd = [self.exe, "-c", "credential.helper=", "-c", "core.askPass="]
        for kv in list(self.HTTP_HARDEN) + list(extra_cfg):
            cmd += ["-c", kv]
        cmd += ["-C", self.cwd] + list(args)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=self.env, timeout=timeout or self.timeout)
            so, se, rc = (p.stdout or "").strip(), (p.stderr or "").strip(), p.returncode
        except subprocess.TimeoutExpired:
            so, se, rc = "", "[TIMEOUT] git %s 超过 %ss" % (" ".join(args), timeout or self.timeout), 124
        if echo and (so or se):
            self.log((so + "\n" + se).strip())
        if rc != 0 and not allow_fail:
            self.log("[X] git %s 失败 (exit %s)" % (" ".join(args), rc))
        return rc, so, se


# ---------------------------------------------------------------- 文件收集
def _skip_name(name):
    for pat in HARD_DENY_GLOBS:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def ensure_parent(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def force_rmtree(path):
    """Windows 上只读/被占用文件会让 rmtree 失败，逐个去掉只读属性后重试。
    仅在显式需要整目录清除时使用（如 --reset）。"""
    def onerr(func, p, exc):
        try:
            os.chmod(p, 0o666)
            func(p)
        except Exception:
            pass
    if os.path.isdir(path):
        shutil.rmtree(path, onerror=onerr)


def _hash_file(path, chunk=262144):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def _file_same(src, dest, mtime_tol=2.0):
    """判断 dest 是否已是 src 的副本。先比大小+mtime（copy2 保留 mtime），
    只有大小相同但 mtime 不同才回退到内容哈希，避免每次 push 重拷数百 MB。"""
    try:
        a, b = os.stat(src), os.stat(dest)
    except OSError:
        return False
    if a.st_size != b.st_size:
        return False
    if abs(a.st_mtime - b.st_mtime) <= mtime_tol:
        return True
    try:
        return _hash_file(src) == _hash_file(dest)
    except OSError:
        return False



def collect(cfg, log, artifact_size_report=None):
    """返回 [(abs_src, rel_dest)]。rel_dest 相对于 staging 根。"""
    wb = cfg["workbuddy_dir"]
    items = []

    for key, spec in cfg["assets"].items():
        if not spec.get("enabled"):
            continue

        # source="." 表示「workbuddy 根目录下的若干单文件」，只按 include 精确取顶层文件，
        # 绝不递归（否则会扫到 binaries/ traces/ 等数 GB 目录）。
        if spec.get("source") == ".":
            for name in spec.get("include") or []:
                full = os.path.join(wb, name)
                if os.path.isfile(full) and not _skip_name(name):
                    items.append((full, os.path.join(key, name)))
            continue

        src = os.path.join(wb, spec["source"])
        if not os.path.exists(src):
            continue
        dest_base = key  # identity / skills / automations / mcp

        if os.path.isfile(src):
            items.append((src, os.path.join(dest_base, os.path.basename(src))))
            continue

        for base, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in HARD_DENY_DIRS and not _skip_name(d)]
            for fn in files:
                if _skip_name(fn):
                    continue
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, src)
                if not match_any(rel.replace(os.sep, "/"), spec.get("include") or ["**/*"]):
                    continue
                if match_any(rel.replace(os.sep, "/"), spec.get("exclude") or []):
                    continue
                items.append((full, os.path.join(dest_base, rel)))

    art = cfg["artifacts"]
    if art.get("enabled"):
        maxb = int(art.get("max_file_mb", 50)) * 1024 * 1024
        skipped = []
        for root in art.get("roots", []):
            if not os.path.isdir(root):
                log("[!] 产物目录不存在，跳过: %s" % root)
                continue
            root_tag = "artifacts/" + re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(root) or "root")
            for base, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in HARD_DENY_DIRS and not _skip_name(d)]
                for fn in files:
                    if _skip_name(fn):
                        continue
                    full = os.path.join(base, fn)
                    rel = os.path.relpath(full, root).replace(os.sep, "/")
                    # include 作为「扩展名/路径白名单」：不匹配即跳过
                    if not match_any(rel, art.get("include") or ["**/*"]):
                        continue
                    if match_any(rel, art.get("exclude") or []):
                        continue
                    try:
                        if os.path.getsize(full) > maxb:
                            skipped.append((rel, os.path.getsize(full)))
                            continue
                    except OSError:
                        continue
                    items.append((full, os.path.join(root_tag, rel)))
        if skipped and artifact_size_report is not None:
            artifact_size_report.extend(skipped)
    return items


def match_any(rel, patterns):
    if not patterns:
        return False
    for pat in patterns:
        p = pat.replace("\\", "/")
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p.replace("**/", "")):
            return True
        if p.endswith("/**") and rel.startswith(p[:-3]):
            return True
    return False


# ---------------------------------------------------------------- 脱敏
SECRET_KEY_RE = re.compile(r"(token|secret|password|passwd|api[_-]?key|authorization|bearer|private)", re.I)


def mask_value(v):
    s = str(v)
    if len(s) <= 8:
        return "***"
    return s[:4] + "..." + s[-2:]


def sanitize_mcp(src, dest, log):
    try:
        with open(src, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log("[!] mcp.json 解析失败，跳过: %s" % e)
        return False

    masked = 0

    def walk(node):
        nonlocal masked
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if isinstance(v, str) and SECRET_KEY_RE.search(k) and v:
                    envref = "${%s}" % re.sub(r"[^A-Za-z0-9]", "_", k).upper()
                    out[k] = envref
                    masked += 1
                elif isinstance(v, str) and re.match(r"^(gh[ps]_|sk-|xox[bpas]-)", v):
                    out[k] = "${%s}" % re.sub(r"[^A-Za-z0-9]", "_", k).upper()
                    masked += 1
                else:
                    out[k] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    safe = walk(data)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(safe, f, ensure_ascii=False, indent=2)
        f.write("\n")
    log("     mcp.json 已脱敏为模板（%d 处敏感值 -> ${ENV}）" % masked)
    return True


def sensitive_hits(items):
    hits = []
    for src, _ in items:
        base = os.path.basename(src).lower()
        if base in HARD_DENY_FILES or _skip_name(os.path.basename(src)):
            hits.append(src)
    return hits


# ---------------------------------------------------------------- staging
def apply_items(items, cfg, log, dry_run=False):
    """增量写入 staging：内容未变的文件跳过，单文件失败不中断整体。"""
    staging = cfg["staging_dir"]
    written = 0
    unchanged = 0
    failed = []
    for src, rel in items:
        dest = os.path.join(staging, rel)
        if os.path.basename(src) == "mcp.json" and rel.startswith("mcp" + os.sep):
            if not dry_run:
                try:
                    sanitize_mcp(src, dest, log)
                except Exception as e:
                    failed.append((src, repr(e)))
                    continue
            written += 1
            continue
        if dry_run:
            written += 1
            continue
        try:
            if _file_same(src, dest):
                unchanged += 1
                continue
            ensure_parent(dest)
            if os.path.exists(dest):
                os.chmod(dest, 0o666)
            shutil.copy2(src, dest)
            written += 1
        except Exception as e:
            failed.append((src, repr(e)))
    log("[i] staging 写入 %d 个，未变化跳过 %d 个" % (written, unchanged))
    if failed:
        log("[!] %d 个文件复制失败（已跳过，不影响其余）:" % len(failed))
        for s, e in failed[:10]:
            log("    %s  <- %s" % (e, s))
        if len(failed) > 10:
            log("    ... 另有 %d 个（明细见 SYNC-SKIPPED.json）" % (len(failed) - 10))
    return written, failed


def prune_staging(cfg, items, log, dry_run=False):
    """增量镜像：只删除「本次不再收集」的残留文件，而不是整目录 rmtree。
    这样既不触发环境的安全删除阈值，也让重复 push 变成秒级操作。"""
    staging = cfg["staging_dir"]
    expected = set()
    for _, rel in items:
        expected.add(rel.replace(os.sep, "/"))
    removed = 0
    for key in ASSET_KEYS:
        d = os.path.join(staging, key)
        if not os.path.isdir(d):
            continue
        for base, dirs, files in os.walk(d, topdown=False):
            for fn in files:
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, staging).replace(os.sep, "/")
                if rel in expected:
                    continue
                removed += 1
                if dry_run:
                    continue
                try:
                    os.chmod(full, 0o666)
                except OSError:
                    pass
                try:
                    os.remove(full)
                except OSError as e:
                    log("[!] 残留清理失败 %s: %s" % (rel, e))
            if not dry_run and base != d:
                try:
                    if not os.listdir(base):
                        os.rmdir(base)
                except OSError:
                    pass
    if removed:
        log("[i] 增量镜像：清理 %d 个已不再收集的残留文件" % removed)
    return removed


def write_skipped(cfg, failed, log):
    """把无法读取/复制的文件写成审计清单。安全策略拒读的文件是「应当不同步」的，
    这里只做记录，不做任何绕过尝试。"""
    path = os.path.join(cfg["staging_dir"], "SYNC-SKIPPED.json")
    try:
        if not failed:
            if os.path.exists(path):
                os.remove(path)
            return
        data = {
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "count": len(failed),
            "reason": sorted({e for _, e in failed}),
            "items": [{"src": s, "error": e} for s, e in failed],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log("[i] 已记录 %d 个跳过项 -> SYNC-SKIPPED.json" % len(failed))
    except OSError as e:
        log("[!] 跳过清单写入失败: %s" % e)


def safe_size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def write_manifest(cfg, items, log):
    staging = cfg["staging_dir"]
    manifest = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "machine": os.environ.get("COMPUTERNAME") or (os.uname().nodename if hasattr(os, "uname") else ""),
        "count": len(items),
        "total_bytes": sum(safe_size(s) for s, _ in items),
        "items": [{"dest": rel, "src": src, "bytes": safe_size(src)} for src, rel in items],
    }
    os.makedirs(staging, exist_ok=True)
    with open(os.path.join(staging, "SYNC-MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def write_gitignore(staging):
    content = """# 由 wb-sync 自动生成
workbuddy.db*
*.db-shm
*.db-wal
keyblob
credentials/
binaries/
cache/
logs/
traces/
node_modules/
__pycache__/
*.key
*.pem
.DS_Store
Thumbs.db
"""
    with open(os.path.join(staging, ".gitignore"), "w", encoding="utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------- 命令
def ensure_git_identity(g, log):
    """干净的机器上 git 可能没有 user.email，导致 commit 直接失败。只写 staging 仓库本地配置。"""
    rc, _, _ = g.run("config", "user.email", allow_fail=True, echo=False)
    if rc != 0:
        g.run("config", "user.email", "wb-sync@localhost", allow_fail=True, echo=False)
        g.run("config", "user.name", "wb-sync", allow_fail=True, echo=False)
        log("[i] 已为 staging 仓库设置本地 git 身份 wb-sync@localhost")


def cmd_init(cfg, cfg_path, log, args):
    log("=" * 60)
    log("初始化 wb-sync  %s" % now_stamp())
    log("=" * 60)

    if args.repo:
        cfg["repo"] = args.repo
    if args.artifacts:
        cfg["artifacts"]["enabled"] = True
        cfg["artifacts"]["roots"] = [os.path.abspath(p) for p in args.artifacts.split(os.pathsep)]

    save_config(cfg, cfg_path)
    log("[OK] 配置已写入: %s" % cfg_path)

    staging = cfg["staging_dir"]
    os.makedirs(staging, exist_ok=True)
    if not os.path.isdir(os.path.join(staging, ".git")):
        g = Git(staging, log)
        g.run("init", "-b", cfg["branch"], allow_fail=True)
        ensure_git_identity(g, log)
        log("[OK] staging 仓库已初始化: %s" % staging)
    write_gitignore(staging)

    if cfg["repo"]:
        log("")
        log("下一步：")
        log("  1) 设置 token: export WB_SYNC_TOKEN=<github token>   （或已登录 gh）")
        log("  2) 首次推送:   wb-sync.py push")
        log("  3) 另一台机:   wb-sync.py pull --init")
    else:
        log("")
        log("[!] 未设置 repo。用 --repo https://github.com/<you>/<repo>.git 重新 init，")
        log("    或直接编辑 %s" % cfg_path)
    return 0


def cmd_status(cfg, log, args):
    if not cfg:
        log("[X] 未找到配置，先运行: wb-sync.py init --repo <url>")
        return 1
    log("=" * 60)
    log("状态预览  %s" % now_stamp())
    log("=" * 60)
    log("workbuddy : %s" % cfg["workbuddy_dir"])
    log("staging   : %s" % cfg["staging_dir"])
    log("repo      : %s" % (cfg["repo"] or "(未设置)"))
    log("产物同步  : %s" % ("开启 " + ", ".join(cfg["artifacts"]["roots"]) if cfg["artifacts"]["enabled"] else "关闭"))
    log("")

    big_skip = []
    items = collect(cfg, log, big_skip)
    total = sum(safe_size(s) for s, _ in items)
    log("待同步文件: %d 个，合计 %s" % (len(items), human(total)))
    log("")

    by_layer = {}
    for src, rel in items:
        layer = rel.split(os.sep)[0]
        n, b = by_layer.get(layer, (0, 0))
        by_layer[layer] = (n + 1, b + safe_size(src))
    log("%-14s %8s %12s" % ("层", "文件数", "体积"))
    for layer, (n, b) in sorted(by_layer.items()):
        log("%-14s %8d %12s" % (layer, n, human(b)))

    if big_skip:
        log("")
        log("[i] 超过体积阈值、已跳过 %d 个（调 artifacts.max_file_mb 可放宽）:" % len(big_skip))
        for rel, sz in big_skip[:10]:
            log("    %-60s %s" % (rel[:60], human(sz)))
        if len(big_skip) > 10:
            log("    ... 另有 %d 个" % (len(big_skip) - 10))

    hits = sensitive_hits(items)
    if hits:
        log("")
        log("[!] 敏感文件告警（默认策略应已拦截，请复核）:")
        for h in hits[:10]:
            log("    " + h)

    g = Git(cfg["staging_dir"], log)
    if os.path.isdir(os.path.join(cfg["staging_dir"], ".git")):
        log("")
        log("-- staging 仓库当前改动 --")
        g.run("status", "-sb", allow_fail=True)
    return 0


def push_with_retry(g, refspec, log, timeout=1800):
    """大包推送在 Windows schannel 下常报 server closed abruptly，
    第二次换 openssl 后端重试（PortableGit 自带 OpenSSL 支持）。"""
    rc, so, se = g.run("push", g.url, refspec, allow_fail=True, timeout=timeout, echo=False)
    if rc == 0:
        log("[OK] 已推送")
        return 0
    log("[!] 第 1 次推送未成功（exit %s）：%s" % (rc, (se or so).splitlines()[-1] if (se or so) else ""))
    log("[!] 改用 openssl TLS 后端重试（针对 schannel: server closed abruptly）...")
    rc2, so2, se2 = g.run("push", g.url, refspec, allow_fail=True, timeout=timeout, echo=False,
                          extra_cfg=["http.sslBackend=openssl"])
    if rc2 == 0:
        log("[OK] 已推送（openssl 后端）")
        return 0
    tail = (se2 or so2).strip().splitlines()[-3:]
    for line in tail:
        log("    " + line)
    log("[X] 推送失败：检查 token 权限（contents:write）、网络，或仓库体积是否超过限制")
    return rc2


def cmd_push(cfg, log, args):
    if not cfg:
        log("[X] 未找到配置，先运行 init")
        return 1
    staging = cfg["staging_dir"]
    os.makedirs(staging, exist_ok=True)
    write_gitignore(staging)

    log("=" * 60)
    log("推送  %s" % now_stamp())
    log("=" * 60)

    big_skip = []
    items = collect(cfg, log, big_skip)
    if not items:
        log("[!] 没有可同步的内容，检查配置里的 sources 是否存在")
        return 1

    if getattr(args, "reset", False):
        log("[!] --reset：整目录清空 staging 受管目录（仅用于排除损坏状态）")
        for key in ASSET_KEYS:
            force_rmtree(os.path.join(staging, key))
    prune_staging(cfg, items, log, dry_run=args.dry_run)
    n, failed = apply_items(items, cfg, log, dry_run=args.dry_run)
    write_skipped(cfg, failed, log)
    manifest = write_manifest(cfg, items, log)
    log("[i] 收集 %d 个文件，合计 %s" % (n, human(manifest["total_bytes"])))
    if args.dry_run:
        log("[dry-run] 未写入 staging，也未提交")
        return 0

    g = Git(staging, log)
    ensure_git_identity(g, log)
    g.run("add", "-A")
    rc, _, _ = g.run("diff", "--cached", "--quiet", allow_fail=True, echo=False)
    if rc == 0:
        log("[i] 无变化，跳过提交")
    else:
        msg = cfg["options"]["commit_message"].format(date=now_stamp())
        g.run("commit", "-m", msg)

    if not cfg["repo"]:
        log("[X] 未配置 repo，无法推送。已完成本地 staging 提交。")
        return 1

    token_ok = g.set_remote(cfg["repo"], cfg.get("token_env", "WB_SYNC_TOKEN"))
    if not token_ok:
        log("[!] 未取到 token（环境变量 %s 或 gh auth token），尝试匿名推送" % cfg.get("token_env"))
    rc = push_with_retry(g, "HEAD:refs/heads/%s" % cfg["branch"], log)
    return rc


def backup_targets(cfg, log):
    stamp = now_stamp("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP_ROOT, stamp)
    os.makedirs(dest, exist_ok=True)
    wb = cfg["workbuddy_dir"]
    saved = []
    for key, spec in cfg["assets"].items():
        if not spec.get("enabled"):
            continue

        if spec.get("source") == ".":
            tgt_dir = os.path.join(dest, key)
            for name in spec.get("include") or []:
                full = os.path.join(wb, name)
                if os.path.isfile(full):
                    os.makedirs(tgt_dir, exist_ok=True)
                    shutil.copy2(full, os.path.join(tgt_dir, name))
                    saved.append(full)
            continue

        src = os.path.join(wb, spec["source"])
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest, key + "__" + os.path.basename(src)))
            saved.append(src)
        elif os.path.isdir(src):
            tgt = os.path.join(dest, key)
            shutil.copytree(src, tgt, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*HARD_DENY_DIRS, *[p.strip("*") for p in HARD_DENY_GLOBS]))
            saved.append(src)
    log("[i] 已备份 %d 个源目录 -> %s" % (len(saved), dest))

    bdir = BACKUP_ROOT
    snaps = sorted([d for d in os.listdir(bdir) if os.path.isdir(os.path.join(bdir, d))])
    keep = int(cfg["options"].get("backup_keep", 5))
    for old in snaps[:-keep]:
        shutil.rmtree(os.path.join(bdir, old), ignore_errors=True)
    return dest


def cmd_pull(cfg, log, args):
    if not cfg:
        log("[X] 未找到配置")
        return 1
    staging = cfg["staging_dir"]

    if args.init and not os.path.isdir(os.path.join(staging, ".git")):
        if not cfg["repo"]:
            log("[X] 未配置 repo，无法初始化")
            return 1
        os.makedirs(staging, exist_ok=True)
        g = Git(staging, log)
        g.run("init", "-b", cfg["branch"], allow_fail=True)

    g = Git(staging, log)
    if not cfg["repo"]:
        log("[X] 未配置 repo")
        return 1
    g.set_remote(cfg["repo"], cfg.get("token_env", "WB_SYNC_TOKEN"))

    log("=" * 60)
    log("拉取  %s" % now_stamp())
    log("=" * 60)
    rc, _, _ = g.run("fetch", g.url, "+refs/heads/%s:refs/remotes/wbsync/%s" % (cfg["branch"], cfg["branch"]),
                     allow_fail=True)
    if rc != 0:
        log("[X] 拉取失败")
        return rc

    rc, so, _ = g.run("rev-parse", "--verify", "refs/remotes/wbsync/%s" % cfg["branch"], allow_fail=True, echo=False)
    if rc != 0:
        log("[!] 远端分支为空（首次推送前），无事可做")
        return 0

    if args.dry_run:
        log("-- 将要变更的文件（diff）--")
        g.run("diff", "--stat", "HEAD", "refs/remotes/wbsync/%s" % cfg["branch"], allow_fail=True)
        log("[dry-run] 未应用任何改动")
        return 0

    if cfg["options"].get("backup_before_pull", True):
        backup_targets(cfg, log)

    rc, _, _ = g.run("checkout", "-B", cfg["branch"],
                     "refs/remotes/wbsync/%s" % cfg["branch"], allow_fail=True)
    if rc != 0:
        g.run("merge", "--ff-only", "refs/remotes/wbsync/%s" % cfg["branch"], allow_fail=True)

    applied = apply_from_staging(cfg, log)
    log("")
    log("[OK] 已应用 %d 个文件到本机" % applied)
    log("[i] 回滚：备份在 %s" % BACKUP_ROOT)
    return 0


def apply_from_staging(cfg, log):
    staging = cfg["staging_dir"]
    wb = cfg["workbuddy_dir"]
    count = 0

    layer_map = {"identity": ".", "skills": "skills", "automations": "automations"}
    for layer, rel_base in layer_map.items():
        src_root = os.path.join(staging, layer)
        if not os.path.isdir(src_root):
            continue
        dst_root = os.path.join(wb, rel_base) if rel_base != "." else wb
        for base, dirs, files in os.walk(src_root):
            dirs[:] = [d for d in dirs if d not in HARD_DENY_DIRS]
            for fn in files:
                src = os.path.join(base, fn)
                rel = os.path.relpath(src, src_root)
                dst = os.path.join(dst_root, rel)
                ensure_parent(dst)
                shutil.copy2(src, dst)
                count += 1

    # mcp.json 模板：只有本机没有时才落地，避免覆盖真值
    mcp_tpl = os.path.join(staging, "mcp", "mcp.json")
    mcp_dst = os.path.join(wb, "mcp.json")
    if os.path.isfile(mcp_tpl) and not os.path.isfile(mcp_dst):
        shutil.copy2(mcp_tpl, mcp_dst)
        log("[i] mcp.json 模板已落地（含 ${ENV} 占位，需填入真实值）")
        count += 1

    art = cfg["artifacts"]
    if art.get("enabled"):
        aroot = os.path.join(staging, "artifacts")
        if os.path.isdir(aroot):
            roots = art.get("roots") or []
            tags = [("artifacts/" + re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(r) or "root"), r) for r in roots]
            for tag, target in tags:
                s = os.path.join(staging, tag)
                if not os.path.isdir(s):
                    continue
                for base, dirs, files in os.walk(s):
                    for fn in files:
                        src = os.path.join(base, fn)
                        rel = os.path.relpath(src, s)
                        dst = os.path.join(target, rel)
                        ensure_parent(dst)
                        shutil.copy2(src, dst)
                        count += 1
    return count


def cmd_doctor(cfg, log, args):
    log("=" * 60)
    log("体检  %s" % now_stamp())
    log("=" * 60)
    problems = 0

    g = Git(cfg["staging_dir"] if cfg else HOME, log)
    log("[OK] git: %s" % g.exe)
    log("[%s] GIT_EXEC_PATH: %s" % ("OK" if g.env.get("GIT_EXEC_PATH") else "--", g.env.get("GIT_EXEC_PATH", "(未设置)")))

    tok = os.environ.get((cfg or {}).get("token_env", "WB_SYNC_TOKEN"), "")
    if not tok:
        tok = g._gh_token()
    if tok:
        log("[OK] token: 已获取 (len=%d)" % len(tok))
    else:
        log("[!] token: 未获取 -> 设置环境变量或 gh auth login")
        problems += 1

    if cfg:
        wb = cfg["workbuddy_dir"]
        log("[%s] workbuddy 目录: %s" % ("OK" if os.path.isdir(wb) else "X", wb))
        if not os.path.isdir(wb):
            problems += 1

        # 硬禁止项污染检查：任何被收集文件的路径里出现运行态目录即为失败
        probe = collect(cfg, Log(), None)
        forbidden_prefixes = tuple(HARD_DENY_DIRS) + tuple(HARD_DENY_FILES)
        polluted = [s for s, _ in probe
                    if any(("%s%s%s" % (os.sep, d, os.sep)) in s.replace("/", os.sep) for d in HARD_DENY_DIRS)
                    or os.path.basename(s) in HARD_DENY_FILES]
        if polluted:
            log("[X] 硬禁止项混入同步集 %d 个，例如:" % len(polluted))
            for p in polluted[:5]:
                log("    " + p)
            problems += 1
        else:
            log("[OK] 硬禁止项检查通过（%d 个待同步文件）" % len(probe))

        log("[%s] staging: %s" % ("OK" if os.path.isdir(cfg["staging_dir"]) else "!", cfg["staging_dir"]))
        if cfg["repo"]:
            log("[OK] repo: %s" % cfg["repo"])
        else:
            log("[!] repo 未设置")
            problems += 1

    log("")
    if problems:
        log("发现 %d 项需要处理" % problems)
        return 1
    log("[OK] 体检通过")
    return 0


def cmd_install(log, args):
    here = os.path.dirname(os.path.abspath(__file__))
    dest = os.path.join(DEFAULT_WORKBUDDY, "skills", "wb-sync")
    os.makedirs(dest, exist_ok=True)
    for name in ("SKILL.md", "wb-sync.py", "config.example.json", "README.md", "LICENSE"):
        src = os.path.join(here, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest, name))
    refs = os.path.join(here, "references")
    if os.path.isdir(refs):
        shutil.copytree(refs, os.path.join(dest, "references"), dirs_exist_ok=True)
    log("[OK] 已安装到 %s" % dest)
    log("     重启 WorkBuddy 后生效（或新会话直接可用）")
    return 0


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 多机资产与产物同步")
    ap.add_argument("action", choices=["init", "status", "push", "pull", "doctor", "install"])
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--log", default=None)
    ap.add_argument("--repo", default="")
    ap.add_argument("--artifacts", default="", help="产物根目录，多路径用 %s 分隔" % os.pathsep)
    ap.add_argument("--init", action="store_true", help="pull 时允许初始化空仓库")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", action="store_true", help="push 前整目录清空 staging（仅疑似损坏时使用）")
    args = ap.parse_args()

    log = Log(args.log or os.path.join(os.getcwd(), "_wb_sync.log"))
    cfg = load_config(args.config, log)
    if cfg is None and args.action != "install":
        cfg = default_config()

    try:
        if args.action == "install":
            rc = cmd_install(log, args)
        elif args.action == "init":
            rc = cmd_init(cfg, args.config, log, args)
        elif args.action == "status":
            rc = cmd_status(cfg, log, args)
        elif args.action == "push":
            rc = cmd_push(cfg, log, args)
        elif args.action == "pull":
            rc = cmd_pull(cfg, log, args)
        else:
            rc = cmd_doctor(cfg, log, args)
    except KeyboardInterrupt:
        log("\n[!] 已中断")
        rc = 130
    except Exception as e:
        import traceback
        log("[X] 未捕获异常: %r" % e)
        log(traceback.format_exc())
        rc = 1

    log.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main())
