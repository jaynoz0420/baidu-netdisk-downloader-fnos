#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证仓库最终状态 + 触发并观察 Actions 运行"""
import json
import sys
import time
import urllib.error
import urllib.request

TOKEN = sys.argv[1] if len(sys.argv) > 1 else ""
API = "https://api.github.com"
OWNER, NAME = "jaynoz0420", "baidu-netdisk-downloader-fnos"


def req(method, path, body=None, tries=4):
    data = json.dumps(body).encode() if body is not None else None
    for _ in range(tries):
        r = urllib.request.Request(API + path, data=data, method=method)
        r.add_header("Authorization", "Bearer " + TOKEN)
        r.add_header("Accept", "application/vnd.github+json")
        r.add_header("X-GitHub-Api-Version", "2022-11-28")
        r.add_header("User-Agent", "nasdl-verify")
        if data:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=45) as x:
                raw = x.read().decode()
                return x.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                j = json.loads(raw)
            except Exception:
                j = {"raw": raw[:250]}
            if e.code >= 500:
                time.sleep(2)
                continue
            return e.code, j
        except Exception:
            time.sleep(2)
            continue
    return 0, {}


def show_files():
    print("=== 仓库文件树 ===")

    def walk(path="", depth=0):
        p = "/repos/%s/%s/contents/%s" % (OWNER, NAME, path)
        st, items = req("GET", p)
        if st != 200 or not isinstance(items, list):
            return
        for it in sorted(items, key=lambda x: (x["type"] != "dir", x["name"])):
            if depth == 0 and it["name"] in ("src", "docs", "tools", ".github"):
                print("  " * depth + "%-24s %s" % (it["name"] + "/", "dir"))
                if it["name"] != ".github":
                    walk(it["path"], depth + 1)
                else:
                    st2, sub = req("GET", "/repos/%s/%s/contents/%s"
                                   % (OWNER, NAME, it["path"]))
                    if isinstance(sub, list):
                        for s in sub:
                            print("  " * (depth + 1) + s["name"] + "/")
            elif depth == 0:
                print("  %-24s %s %d B" % (
                    it["name"], it["type"], it.get("size", 0)))

    walk()


def check_workflow():
    print()
    print("=== Workflow 文件 ===")
    st, j = req("GET", "/repos/%s/%s/contents/.github/workflows/build.yml"
                % (OWNER, NAME))
    if st == 200:
        print("  ✓ 已存在  %d B" % j.get("size", 0))
        print("  %s" % j.get("html_url"))
    else:
        print("  ✗ 缺失 (%d)" % st)

    print()
    print("=== 触发 Actions ===")
    st, r = req("POST", "/repos/%s/%s/actions/workflows/build.yml/dispatches"
                % (OWNER, NAME), {"ref": "main"})
    if st in (200, 201, 204):
        print("  ✓ 已触发")
        return True
    print("  ! 触发失败 (%d): %s" % (st, str(r)[:200]))
    print("  (workflow_dispatch 可能需要先手动启用 workflow)")
    return False


def watch_runs():
    print()
    print("=== 观察运行结果 ===")
    for i in range(12):
        time.sleep(10)
        st, runs = req("GET", "/repos/%s/%s/actions/runs?per_page=3"
                       % (OWNER, NAME))
        if st != 200:
            print("  查询失败 %d" % st)
            continue
        lst = runs.get("workflow_runs", [])
        if not lst:
            print("  [%2ds] 暂无运行记录" % ((i + 1) * 10))
            continue
        r0 = lst[0]
        print("  [%2ds] %s  status=%s  conclusion=%s"
              % ((i + 1) * 10, r0.get("name", "")[:20],
                 r0.get("status"), r0.get("conclusion") or "-"))
        if r0.get("status") == "completed":
            print("  URL: %s" % r0.get("html_url"))
            return r0
    print("  (超时, 请到 Actions 页面查看)")
    return None


def main():
    if not TOKEN:
        print("用法: python verify_final.py <TOKEN>")
        return 1

    st, rp = req("GET", "/repos/%s/%s" % (OWNER, NAME))
    print("=== 仓库 ===")
    print("  %s" % rp.get("html_url"))
    print("  描述: %s" % rp.get("description"))
    print("  可见性: %s" % ("private" if rp.get("private") else "public"))
    print("  默认分支: %s" % rp.get("default_branch"))
    print()

    show_files()
    check_workflow()
    watch_runs()

    print()
    print("=== Release ===")
    st, rel = req("GET", "/repos/%s/%s/releases" % (OWNER, NAME))
    if isinstance(rel, list):
        for r in rel:
            print("  %s" % r["tag_name"])
            for a in r.get("assets", []):
                print("    %s  %d KB" % (a["name"], a["size"] // 1024))
                print("    %s" % a["browser_download_url"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
