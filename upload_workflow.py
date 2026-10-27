#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补传 .github/workflows/build.yml"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = sys.argv[1] if len(sys.argv) > 1 else ""
API = "https://api.github.com"
OWNER, NAME, BRANCH = "jaynoz0420", "baidu-netdisk-downloader-fnos", "main"
REPO = os.path.dirname(os.path.abspath(__file__))
REL = ".github/workflows/build.yml"
LOCAL = os.path.join(REPO, ".github", "workflows", "build.yml")


def req(method, path, body=None, tries=4):
    data = json.dumps(body).encode() if body is not None else None
    for a in range(tries):
        r = urllib.request.Request(API + path, data=data, method=method)
        r.add_header("Authorization", "Bearer " + TOKEN)
        r.add_header("Accept", "application/vnd.github+json")
        r.add_header("X-GitHub-Api-Version", "2022-11-28")
        r.add_header("User-Agent", "nasdl-publisher")
        if data:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=60) as x:
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
        print("用法: python upload_workflow.py <TOKEN>")
        return 1
    data = open(LOCAL, "rb").read()
    print("本地: %s  %d 字节" % (REL, len(data)))

    api = "/repos/%s/%s/contents/%s" % (
        OWNER, NAME, urllib.parse.quote(REL))

    st, cur = req("GET", api + "?ref=" + BRANCH)
    body = {
        "message": "ci: 添加 GitHub Actions 自动构建工作流",
        "content": base64.b64encode(data).decode(),
        "branch": BRANCH,
    }
    if st == 200 and isinstance(cur, dict) and cur.get("sha"):
        body["sha"] = cur["sha"]
        print("文件已存在, 执行更新")

    st2, res = req("PUT", api, body)
    print()
    if st2 in (200, 201):
        c = res.get("content", {})
        print("✓ 上传成功")
        print("  path: %s" % c.get("path"))
        print("  size: %s B" % c.get("size"))
        print("  sha : %s" % (c.get("sha") or "")[:12])
        print("  提交: %s" % (res.get("commit", {}).get("sha") or "")[:12])
        print()
        print("  Actions 页面: https://github.com/%s/%s/actions"
              % (OWNER, NAME))
        return 0
    print("✗ 失败 %d" % st2)
    print(" ", str(res)[:400])
    return 1


if __name__ == "__main__":
    sys.exit(main())
