#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb-sync 端到端自测：用本地裸仓库模拟远端，验证
  1. init / push / pull 全链路可跑通
  2. 运行态文件（workbuddy.db / keyblob / binaries / credentials）不入库
  3. mcp.json 中的 token 被脱敏为 ${ENV}
  4. 第二台机器能拿到 identity / skills / mcp 模板

用法： python tests/e2e.py [--keep]
退出码 0 = 全部通过。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "wb-sync.py")
PY = sys.executable

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (("  -> " + detail) if detail and not ok else ""))


def find_git():
    g = shutil.which("git")
    if g:
        return g
    home = os.path.expanduser("~")
    base = os.path.join(home, ".workbuddy", "binaries", "PortableGit", "versions")
    if os.path.isdir(base):
        for ver in sorted(os.listdir(base), reverse=True):
            for sub in ("mingw64/bin/git.exe", "cmd/git.exe"):
                p = os.path.join(base, ver, sub.replace("/", os.sep))
                if os.path.isfile(p):
                    return p
    raise SystemExit("[X] 未找到 git")


def git_env(git_exe):
    env = dict(os.environ)
    d = os.path.dirname(git_exe)
    env["PATH"] = d + os.pathsep + env.get("PATH", "")
    env["GIT_EXEC_PATH"] = d
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "wb-sync-test"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "test@localhost"
    return env


def run(args, logfile):
    p = subprocess.run([PY, SCRIPT] + args + ["--log", logfile],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    txt = open(logfile, encoding="utf-8").read() if os.path.isfile(logfile) else ""
    return p.returncode, txt


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def build_fixture(root):
    git = find_git()
    env = git_env(git)
    subprocess.run([git, "init", "--bare", "-b", "main",
                    os.path.join(root, "remote.git")], capture_output=True, env=env, check=True)

    a = os.path.join(root, "A", ".workbuddy")
    write(os.path.join(a, "SOUL.md"), "# SOUL\n")
    write(os.path.join(a, "USER.md"), "# USER\n")
    write(os.path.join(a, "skills", "demo", "SKILL.md"), "---\nname: demo\n---\n")
    write(os.path.join(a, "skills", "demo", "run.py"), "print('ok')\n")
    write(os.path.join(a, "binaries", "big.bin"), "x" * 2048)
    write(os.path.join(a, "workbuddy.db"), "SQLite" * 100)
    write(os.path.join(a, "keyblob"), "secret")
    write(os.path.join(a, "credentials", "c.json"), '{"token":"abc"}')
    write(os.path.join(a, "mcp.json"), json.dumps({
        "mcpServers": {"gh": {"command": "npx",
                              "env": {"GITHUB_TOKEN": "ghp_faketoken1234567890", "PLAIN": "keep"}}}
    }, ensure_ascii=False, indent=2))

    b = os.path.join(root, "B", ".workbuddy")
    os.makedirs(b, exist_ok=True)

    def cfg(machine, wb):
        return {
            "repo": os.path.join(root, "remote.git").replace("\\", "/"),
            "branch": "main",
            "staging_dir": os.path.join(root, machine, "staging"),
            "workbuddy_dir": wb,
            "assets": {
                "identity": {"enabled": True, "source": ".", "include": ["SOUL.md", "USER.md", "MEMORY.md"]},
                "skills": {"enabled": True, "source": "skills", "include": ["*"], "exclude": []},
                "automations": {"enabled": True, "source": "automations", "include": ["**/*"]},
                "mcp": {"enabled": True, "source": "mcp.json", "mode": "template"},
            },
            "artifacts": {"enabled": False, "roots": [], "exclude": [], "max_file_mb": 50},
            "options": {"backup_before_pull": False, "backup_keep": 1,
                        "commit_message": "test: {date}"},
        }

    ca, cb = os.path.join(root, "cfgA.json"), os.path.join(root, "cfgB.json")
    write(ca, json.dumps(cfg("A", a), ensure_ascii=False, indent=2))
    write(cb, json.dumps(cfg("B", b), ensure_ascii=False, indent=2))
    return git, env, a, b, ca, cb


def main():
    keep = "--keep" in sys.argv
    root = tempfile.mkdtemp(prefix="wb-sync-e2e-")
    try:
        git, env, a, b, ca, cb = build_fixture(root)

        rc, _ = run(["init", "--config", ca, "--repo", os.path.join(root, "remote.git").replace("\\", "/")],
                    os.path.join(root, "l1.txt"))
        check("init 退出码 0", rc == 0, str(rc))

        rc, _ = run(["push", "--config", ca], os.path.join(root, "l2.txt"))
        check("push 退出码 0", rc == 0, str(rc))

        p = subprocess.run([git, "-C", os.path.join(root, "remote.git"),
                            "ls-tree", "-r", "--name-only", "main"],
                           capture_output=True, text=True, env=env)
        names = p.stdout or ""
        for bad in ("workbuddy.db", "keyblob", "binaries", "credentials", ".bin"):
            check("禁止项未入库: " + bad, bad not in names)
        for good in ("identity/SOUL.md", "skills/demo/SKILL.md", "mcp/mcp.json"):
            check("已入库: " + good, good in names)

        p2 = subprocess.run([git, "-C", os.path.join(root, "remote.git"), "show", "main:mcp/mcp.json"],
                            capture_output=True, text=True, env=env)
        body = p2.stdout or ""
        check("mcp token 已脱敏", "ghp_faketoken" not in body and "${GITHUB_TOKEN}" in body)
        check("mcp 非敏感键保留", '"keep"' in body)

        rc, _ = run(["pull", "--config", cb, "--init"], os.path.join(root, "l3.txt"))
        check("pull 退出码 0", rc == 0, str(rc))

        for rel in ("SOUL.md", "USER.md", "skills/demo/SKILL.md", "skills/demo/run.py", "mcp.json"):
            check("B 机落地: " + rel, os.path.isfile(os.path.join(b, rel.replace("/", os.sep))))

        rc, _ = run(["doctor", "--config", ca], os.path.join(root, "l4.txt"))
        check("doctor 未报硬禁止项污染", "硬禁止项混入" not in open(os.path.join(root, "l4.txt"),
                                                               encoding="utf-8").read())
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print("fixture kept at: " + root)

    failed = [r for r in RESULTS if not r[1]]
    print("\n%d passed, %d failed" % (len(RESULTS) - len(failed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
