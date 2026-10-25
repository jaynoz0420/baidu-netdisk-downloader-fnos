#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度网盘 PC 客户端 cookie 提取器 (Windows)
==========================================

用途
----
从 PC 版百度网盘进程内存读取 BDUSS / STOKEN, 供 NAS 端不限速下载器使用。

原理
----
百度网盘 PC 客户端把登录态放在进程内存, 不以明文文件落盘。
通过 OpenProcess + ReadProcessMemory 扫描可读内存区, 正则匹配
"BDUSS=..." 与 "STOKEN=..." 即可取出。

要求
----
- Windows, 需管理员权限运行 (OpenProcess 需要 PROCESS_VM_READ)
- PC 版百度网盘已登录并运行

用法
----
  python baidu_pc_cookie.py
输出:
  pc_BDUSS.txt / pc_STOKEN.txt / pc_cookie_str.txt  (当前目录)

注意
----
cookie 等价于账号登录凭证, 请勿外传。用完后建议在百度网盘
"账号安全" 里退出所有设备, 使旧 cookie 失效。
"""
import ctypes, ctypes.wintypes as wt
import re, subprocess, sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

k32.OpenProcess.restype = wt.HANDLE
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD),
                ("RegionSize", ctypes.c_size_t),
                ("State", wt.DWORD),
                ("Protect", wt.DWORD),
                ("Type", wt.DWORD)]


k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                               ctypes.POINTER(MBI), ctypes.c_size_t]

MEM_COMMIT = 0x1000
READABLE = (0x02, 0x04, 0x20, 0x40, 0x80)

PATS = {
    "BDUSS": re.compile(rb"BDUSS=[A-Za-z0-9_%+/=\-]{40,300}"),
    "STOKEN": re.compile(rb"STOKEN=[A-Za-z0-9]{20,100}"),
    "pair": re.compile(rb"BDUSS=[^;\x00]{40,300};[^\x00]{0,200}STOKEN=[A-Za-z0-9]{20,100}"),
}


def pids_of(hint):
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                         capture_output=True).stdout.decode("utf-8", "replace")
    res = []
    for line in out.splitlines():
        p = [x.strip('"') for x in line.split('","')]
        if len(p) >= 2 and hint.lower() in p[0].lower():
            try:
                res.append((int(p[1]), p[0]))
            except ValueError:
                pass
    return res


def scan(pid):
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None, "OpenProcess 失败 (需要管理员权限)"
    found = {k: set() for k in PATS}
    mbi = MBI()
    addr, total = 0, 0
    try:
        while k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                 ctypes.sizeof(mbi)):
            base, size = mbi.BaseAddress or 0, mbi.RegionSize
            if base == 0 and size == 0:
                break
            if (mbi.State == MEM_COMMIT and (mbi.Protect & 0xFF) in READABLE
                    and 0 < size <= 256 * 1024 * 1024):
                buf = ctypes.create_string_buffer(size)
                read = ctypes.c_size_t(0)
                if k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size,
                                         ctypes.byref(read)) and read.value:
                    data = buf.raw[:read.value]
                    total += len(data)
                    for k, pat in PATS.items():
                        if len(found[k]) < 5:
                            for m in pat.finditer(data):
                                found[k].add(m.group().decode("utf-8", "replace"))
            addr = base + size
    finally:
        k32.CloseHandle(h)
    return found, "扫描 %d MB" % (total // 1048576)


def main():
    targets = pids_of("BaiduNetdisk.exe") + pids_of("baidunetdiskhost")
    if not targets:
        print("未找到百度网盘进程, 请先启动并登录 PC 版百度网盘")
        return 1
    print("候选进程: %s" % targets)

    acc = {k: set() for k in PATS}
    for pid, name in targets:
        print("\n--- %s (pid=%d) ---" % (name, pid))
        f, note = scan(pid)
        print("   ", note)
        if not f:
            continue
        for k in PATS:
            if f[k]:
                print("    %s: %d" % (k, len(f[k])))
                acc[k] |= f[k]

    print("\n" + "=" * 60)
    ok = False
    for k in PATS:
        if not acc[k]:
            continue
        ok = True
        best = sorted(acc[k], key=len, reverse=True)[0]
        fn = "pc_%s.txt" % k
        open(fn, "w", encoding="utf-8").write(best)
        print("\n[%s] -> %s" % (k, fn))
        print("  %s" % best[:200])

    if not ok:
        print("\n未提取到 cookie。请以管理员身份运行, 并确认已登录。")
        return 2
    print("\n完成。把 pc_cookie_str.txt 的内容填进下载器的 COOKIE 变量即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
