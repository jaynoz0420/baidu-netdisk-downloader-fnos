#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最终交付验证"""
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


def main():
    if not TOKEN:
        print("用法: python final_verify2.py <TOKEN>")
        return 1

    print("=" * 58)
    print("最终交付验证")
    print("=" * 58)

    st, rp = req("GET", "/repos/%s/%s" % (OWNER, NAME))
    print()
    print("仓库: %s" % rp.get("html_url"))
    print("描述: %s" % rp.get("description"))
    print("可见性: %s" % ("private" if rp.get("private") else "public"))
    print("分支: %s" % rp.get("default_branch"))

    # 用 git tree API 列全部文件（递归）
    st, tree = req("GET", "/repos/%s/%s/git/trees/main?recursive=1"
                   % (OWNER, NAME))
    if st == 200:
        files = [t for t in tree.get("tree", []) if t["type"] == "blob"]
        dirs = [t for t in tree.get("tree", []) if t["type"] == "tree"]
        print()
        print("文件总数: %d   目录数: %d" % (len(files), len(dirs)))

        bad = [f["path"] for f in files
               if f["path"].endswith(".pyc") or "__pycache__" in f["path"]
               or f["path"].startswith("publish") or f["path"].startswith("verify_")]
        print("异常文件: %s" % (bad if bad else "无 ✓"))

        print()
        print("--- 文件清单 ---")
        for f in sorted(files, key=lambda x: x["path"]):
            print("  %-46s %7d B" % (f["path"], f.get("size", 0)))

    # Actions
    print()
    print("--- GitHub Actions ---")
    st, runs = req("GET", "/repos/%s/%s/actions/runs?per_page=3"
                   % (OWNER, NAME))
    if st == 200:
        lst = runs.get("workflow_runs", [])
        if not lst:
            print("  暂无运行记录")
        for r in lst[:3]:
            print("  %-10s %-10s %s" % (
                r.get("name", "")[:10],
                r.get("status"),
                r.get("conclusion") or "-"))
            print("    %s" % r.get("html_url"))
        if lst and lst[0].get("conclusion") == "success":
            art = req("GET", "/repos/%s/%s/actions/runs/%s/artifacts"
                      % (OWNER, NAME, lst[0]["id"]))
            if art[0] == 200:
                arts = art[1].get("artifacts", [])
                print("  构建产物: %s" % (
                    [a["name"] for a in arts] if arts else "无"))

    # Release
    print()
    print("--- Release ---")
    st, rel = req("GET", "/repos/%s/%s/releases" % (OWNER, NAME))
    if isinstance(rel, list):
        for r in rel:
            print("  %s  (%d 资产)" % (r["tag_name"], len(r.get("assets", []))))
            for a in r.get("assets", []):
                print("    %s  %d KB  %d 次下载" % (
                    a["name"], a["size"] // 1024, a.get("download_count", 0)))

    print()
    print("=" * 58)
    return 0


if __name__ == "__main__":
    sys.exit(main())
