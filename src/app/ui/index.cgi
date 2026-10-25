#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fnOS ThirdParty CGI 入口
========================
fnOS 通过 /cgi/ThirdParty/{appname}/index.cgi 调用本程序, 并把后端端口
以 HTTP 反向代理方式转发。

本 CGI 的作用: 把 fnOS 传入的请求反向代理到本应用自己的 HTTP 服务
(127.0.0.1:APP_PORT), 这样桌面图标就能正常打开页面。

fnOS 传入的环境变量 (标准 CGI):
  REQUEST_METHOD / QUERY_STRING / PATH_INFO / CONTENT_TYPE / CONTENT_LENGTH
  HTTP_*  (请求头)
stdin: 请求体
stdout: "Status: 200 OK\r\nContent-Type: ...\r\n\r\n" + body
"""
import os
import socket
import sys
import http.client

BACKEND_HOST = "127.0.0.1"


def read_backend_port():
    """端口来源: 环境变量 -> 端口文件 -> 默认 18080"""
    p = os.environ.get("NASDL_PORT")
    if p and p.isdigit():
        return int(p)
    for f in ("/usr/local/apps/@appdata/baidu.nasdl/backend_port",
              os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "..", "var", "backend_port")):
        try:
            with open(f) as fh:
                v = fh.read().strip()
            if v.isdigit():
                return int(v)
        except Exception:
            pass
    return 18080


BACKEND_PORT = read_backend_port()


def read_stdin():
    try:
        n = int(os.environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        n = 0
    if n <= 0:
        return b""
    chunks = []
    left = n
    while left > 0:
        b = sys.stdin.buffer.read(min(65536, left))
        if not b:
            break
        chunks.append(b)
        left -= len(b)
    return b"".join(chunks)


def build_path():
    # fnOS 传入形如 PATH_INFO=/cgi/ThirdParty/baidu.nasdl/xxx
    # 若页面基址是 /cgi/ThirdParty/{app}/index.cgi/, 相对资源会带
    # index.cgi 段, 去掉后再转给后端
    pi = os.environ.get("PATH_INFO", "") or ""
    parts = [p for p in pi.split("/") if p]

    try:
        idx = parts.index("ThirdParty")
        parts = parts[idx + 2:]       # 去掉 cgi/ThirdParty/<appname>
    except ValueError:
        parts = []

    parts = [p for p in parts if p != "index.cgi"]
    path = "/" + "/".join(parts)

    qs = os.environ.get("QUERY_STRING", "")
    if qs:
        path += "?" + qs
    return path


def build_headers():
    h = {}
    for k, v in os.environ.items():
        if k.startswith("HTTP_"):
            name = k[5:].replace("_", "-").title()
            h[name] = v
    if os.environ.get("CONTENT_TYPE"):
        h["Content-Type"] = os.environ["CONTENT_TYPE"]
    if os.environ.get("CONTENT_LENGTH"):
        h["Content-Length"] = os.environ["CONTENT_LENGTH"]
    # 透传 Host 便于后端生成绝对地址
    h.setdefault("Host", "%s:%d" % (BACKEND_HOST, BACKEND_PORT))
    h["Connection"] = "close"
    return h


def main():
    method = os.environ.get("REQUEST_METHOD", "GET").upper()
    path = build_path()
    body = read_stdin() if method in ("POST", "PUT", "PATCH") else b""
    headers = build_headers()

    try:
        conn = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=300)
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
        reason = resp.reason or "OK"
        skip = {"connection", "transfer-encoding", "content-length",
                "keep-alive", "server", "date"}
        out = sys.stdout.buffer
        out.write(("Status: %d %s\r\n" % (status, reason)).encode())
        for k, v in resp.getheaders():
            if k.lower() in skip:
                continue
            out.write(("%s: %s\r\n" % (k, v)).encode("utf-8", "replace"))
        out.write(("Content-Length: %d\r\n" % len(data)).encode())
        out.write(b"\r\n")
        out.write(data)
        out.flush()
        conn.close()
    except ConnectionRefusedError:
        msg = b"backend not running"
        sys.stdout.buffer.write(
            b"Status: 502 Bad Gateway\r\nContent-Type: text/plain\r\n"
            b"Content-Length: " + str(len(msg)).encode() + b"\r\n\r\n" + msg)
        sys.stdout.buffer.flush()
    except Exception as e:
        msg = ("cgi proxy error: %s" % e).encode("utf-8", "replace")
        sys.stdout.buffer.write(
            b"Status: 500 Internal Server Error\r\nContent-Type: text/plain\r\n"
            b"Content-Length: " + str(len(msg)).encode() + b"\r\n\r\n" + msg)
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
