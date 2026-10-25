#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按 fnOS 官方 fpk 的字节级格式打包
=================================
官方 tar 头特征 (ustar):
  - 目录项 name 为 "." / "cmd" / "config" / "wizard" (无尾斜杠, 但 typeflag=5)
  - mode 八进制 7 位 "%07o\\0"
  - uid/gid 为 "0000000\\0"
  - 所有文件 mode=644, 目录 mode=755
  - uname/gname = "root"
  - magic "ustar\\0" + version "00"

用法:
    python3 build.py                 # 输出到 dist/
    python3 build.py -o out.fpk      # 指定输出文件
"""
import argparse
import gzip
import io
import os
import re
import struct
import sys
import tarfile
import time
import zlib

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
DIST = os.path.join(HERE, "dist")


def read_version():
    """从 manifest 读取版本号"""
    try:
        txt = open(os.path.join(SRC, "manifest"), encoding="utf-8").read()
        m = re.search(r'version="([^"]+)"', txt)
        return m.group(1) if m else "0.0.0"
    except Exception:
        return "0.0.0"


def read_appname():
    try:
        txt = open(os.path.join(SRC, "manifest"), encoding="utf-8").read()
        m = re.search(r'appname="([^"]+)"', txt)
        return m.group(1) if m else "app"
    except Exception:
        return "app"


VERSION = read_version()
APPNAME = read_appname()
OUT = os.path.join(DIST, "%s-%s.fpk" % (APPNAME, VERSION))

TEXT_FILES = [
    "manifest", "cmd/main", "cmd/config", "cmd/common", "cmd/service",
    "cmd/package", "cmd/install_init", "cmd/install_callback",
    "cmd/uninstall_init", "cmd/uninstall_callback",
    "cmd/upgrade_init", "cmd/upgrade_callback",
    "config/privilege", "config/resource", "wizard/uninstall",
    "app/server.py", "app/ui/config", "app/ui/index.cgi",
    "app/web/index.html", "app/web/app.js", "app/web/style.css",
]


def norm(path):
    if not os.path.isfile(path):
        return
    raw = open(path, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    open(path, "wb").write(raw.replace(b"\r\n", b"\n"))


def octal(val, width):
    """生成 '%0*o\0' 形式的八进制字段"""
    s = ("%0*o" % (width - 1, val)).encode()
    return s[:width - 1].ljust(width - 1, b"0") + b"\x00"


def tar_header(name, size, mode, typflag=b"0", mtime=0):
    h = bytearray(512)
    nb = name.encode("utf-8")[:100]
    h[0:len(nb)] = nb
    h[100:108] = octal(mode, 8)
    h[108:116] = octal(0, 8)          # uid
    h[116:124] = octal(0, 8)          # gid
    h[124:136] = octal(size, 12)
    h[136:148] = octal(mtime, 12)
    h[148:156] = b" " * 8             # 先占位
    h[156:157] = typflag
    h[257:263] = b"ustar\x00"
    h[263:265] = b"00"
    h[265:269] = b"root"
    h[297:301] = b"root"
    chk = sum(h)
    h[148:156] = ("%06o" % chk).encode() + b"\x00" + b" "
    return bytes(h)


def pad(data):
    r = len(data) % 512
    return data + (b"\x00" * (512 - r) if r else b"")


def build_app_tgz():
    """app/ 打成 app.tgz, 内层用 ./ 前缀 (与官方 app.tgz 一致)"""
    app_dir = os.path.join(SRC, "app")
    chunks = []

    def add(name, data, mode):
        chunks.append(tar_header(name, len(data), mode, b"0"))
        chunks.append(pad(data))

    def adddir(name):
        chunks.append(tar_header(name, 0, 0o755, b"5"))

    adddir("./")
    for root, dirs, files in os.walk(app_dir):
        dirs.sort()
        rel_root = os.path.relpath(root, app_dir)
        if rel_root == ".":
            rel_root = ""
        if rel_root:
            adddir("./" + rel_root.replace("\\", "/") + "/")
        for fn in sorted(files):
            if fn.endswith(".pyc") or "__pycache__" in fn:
                continue
            full = os.path.join(root, fn)
            rel = (rel_root + "/" + fn).lstrip("/").replace("\\", "/")
            data = open(full, "rb").read()
            # .py 与 .cgi 需要可执行
            mode = 0o755 if (fn.endswith(".py") or fn.endswith(".cgi")) else 0o644
            add("./" + rel, data, mode)

    chunks.append(b"\x00" * 1024)
    raw = b"".join(chunks)

    g = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", compresslevel=9,
                       fileobj=g, mtime=0) as gz:
        gz.write(raw)
    out = bytearray(g.getvalue())
    if len(out) > 9:
        out[9] = 255
    return bytes(out)


def build():
    for rel in TEXT_FILES:
        norm(os.path.join(SRC, rel.replace("/", os.sep)))
    print("文本整合为 LF")

    app_tgz = build_app_tgz()
    print("app.tgz = %d 字节" % len(app_tgz))

    # 外层: 与官方成员顺序完全一致
    items = [
        (".", None, 0o755, b"5"),
        ("ICON.PNG", os.path.join(SRC, "ICON.PNG"), 0o644, b"0"),
        ("ICON_256.PNG", os.path.join(SRC, "ICON_256.PNG"), 0o644, b"0"),
        ("app.tgz", app_tgz, 0o644, b"0"),
        ("cmd", None, 0o755, b"5"),
    ]
    for n in ("common", "config", "install_callback", "install_init",
              "main", "package", "service", "uninstall_callback",
              "uninstall_init", "upgrade_callback", "upgrade_init"):
        items.append(("cmd/" + n, os.path.join(SRC, "cmd", n), 0o644, b"0"))
    items += [
        ("config", None, 0o755, b"5"),
        ("config/privilege", os.path.join(SRC, "config", "privilege"), 0o644, b"0"),
        ("config/resource", os.path.join(SRC, "config", "resource"), 0o644, b"0"),
        ("manifest", os.path.join(SRC, "manifest"), 0o644, b"0"),
        ("wizard", None, 0o755, b"5"),
        ("wizard/uninstall", os.path.join(SRC, "wizard", "uninstall"), 0o644, b"0"),
    ]

    now = int(time.time())
    chunks = []
    for name, path, mode, typ in items:
        if typ == b"5":
            chunks.append(tar_header(name, 0, mode, b"5", now))
        else:
            if isinstance(path, bytes):
                data = path
            else:
                if not os.path.isfile(path):
                    print("  [缺失] %s" % name)
                    continue
                data = open(path, "rb").read()
            chunks.append(tar_header(name, len(data), mode, b"0", now))
            chunks.append(pad(data))
    chunks.append(b"\x00" * 1024)
    tar_bytes = b"".join(chunks)

    # 外层 gzip
    fname = ("%s_%s_x86.fpk\x00" % (APPNAME, VERSION)).encode()
    out = bytearray()
    out += b"\x1f\x8b\x08\x08"
    out += b"\x00\x00\x00\x00"
    out += b"\x02\xff"
    out += fname
    co = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS, 9)
    out += co.compress(tar_bytes) + co.flush()
    out += struct.pack("<II", zlib.crc32(tar_bytes) & 0xFFFFFFFF,
                       len(tar_bytes) & 0xFFFFFFFF)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "wb").write(bytes(out))
    print("写出 %s  (%d 字节)" % (OUT, len(out)))

    # 自检: 用 tarfile 读回
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    buf = d.decompress(open(OUT, "rb").read()) + d.flush()
    tf = tarfile.open(fileobj=io.BytesIO(buf))
    names = tf.getnames()
    print("回读成员 %d 个: %s" % (len(names), names[:6]))
    assert "manifest" in names and "app.tgz" in names
    print("自检通过")


def main():
    global OUT
    ap = argparse.ArgumentParser(description="打包 fnOS fpk")
    ap.add_argument("-o", "--output", default="", help="输出 fpk 路径")
    args = ap.parse_args()
    if args.output:
        OUT = os.path.abspath(args.output)
    build()


if __name__ == "__main__":
    main()
