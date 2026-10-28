#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""删除仓库里误传的临时脚本"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = sys.argv[1] if len(sys.argv) > 1 else ""
API = "https://api.github.com"
OWNER, NAME, BR = "jaynoz0420", "baidu-netdisk-downloader-fnos", "main"

# 不该出现在仓库里的文件
REMOVE = ["upload_workflow.py", "verify_final.py", "final_verify2.py",
          "publish.py", "publish2.py", "publish3.py", "push_existing.py",
          "dev_notes.md", "_tmp_check.txt"]


def req(method, path, body=None, tries=4):
    data = json.dumps(body).encode() if body is not None else None
    for _ in range(tries):
        r = urllib.request.Request(API + path, data=data, method=method)
        r.add_header("Authorization", "Bearer " + TOKEN)
        r.add_header("Accept", "application/vnd.github+json")
        r.add_header("X-GitHub-Api-Version", "2022-11-28")
        r.add_header("User-Agent", "nasdl-clean")
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
                j = {"raw": raw[:200]}
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
        print("用法: python cleanup_repo.py <TOKEN>")
        return 1

    print("=== 检查并删除临时脚本 ===")
    removed = 0
    for rel in REMOVE:
        api = "/repos/%s/%s/contents/%s" % (
            OWNER, NAME, urllib.parse.quote(rel))
        st, cur = req("GET", api + "?ref=" + BR)
        if st != 200:
            continue
        sha = cur.get("sha")
        if not sha:
            continue
        st2, res = req("DELETE", api, {
            "message": "chore: 移除临时脚本 %s" % rel,
            "sha": sha,
            "branch": BR,
        })
        if st2 in (200, 201):
            print("  ✓ 已删除 %s" % rel)
            removed += 1
        else:
            print("  ✗ %s 失败: %s" % (rel, str(res)[:120]))

    print()
    print("共删除 %d 个" % removed)

    # 验证最终文件列表
    print()
    print("=== 最终文件列表 ===")
    st, tree = req("GET", "/repos/%s/%s/git/trees/main?recursive=1"
                   % (OWNER, NAME))
    if st == 200:
        files = [t["path"] for t in tree.get("tree", []) if t["type"] == "blob"]
        print("  文件总数: %d" % len(files))
        bad = [f for f in files
               if f.endswith(".pyc") or "__pycache__" in f
               or "/publish" in f or f.startswith("publish")
               or f.startswith("upload_") or f.startswith("verify_")
               or f.startswith("final_verify")]
        if bad:
            print("  ! 仍有临时文件: %s" % bad)
        else:
            print("  ✓ 无临时文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
