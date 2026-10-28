#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度网盘 NAS 不限速下载器 —— 后端服务
=====================================

零第三方依赖 (仅标准库), 适配飞牛 fnOS (Python 3.11)。

功能:
  - 扫码登录 / cookie 登录 / 已有登录态复用
  - 网盘目录浏览、文件列表
  - 1MB 分片 + 32 线程并发下载 (绕开 CDN 大 Range 风控)
  - 下载任务管理: 进度、暂停、删除、断点续传
  - 本地磁盘文件浏览

启动:
  python3 server.py --port <PORT> [--sock <PATH>] --data <DATA_DIR>
"""

import argparse
import base64
import hashlib
import html
import json
import mimetypes
import os
import re
import signal
import socket
import socketserver
import ssl
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ============================================================
#  常量
# ============================================================
APP_ID = "250528"
APP_NAME = "百度网盘下载器"
VERSION = "1.8.1"

UA_PC = ("netdisk;P2SP;3.0.0.8;netdisk;11.12.0;PC;PC-Windows;10.0.19045;"
         "WindowsBaiduYunGuanJia")
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

CHUNK = 1024 * 1024          # 1MB —— 超过 1MB 会触发 CDN 403
DEFAULT_CONNS = 64           # 单任务连接数 (实测 64 最快)
MAX_CONNS = 128

# 全局并发策略
# 实测 (同一账号、空载、纯网络读):
#   16 连接 ->  9.2 MB/s
#   32 连接 -> 17.7 MB/s
#   64 连接 -> 22.8 MB/s   ← 最优
#   96 连接 -> 19.7 MB/s   ← 反而下降 (百度按账号总带宽限速)
# 所以总预算取 64; 任务越多每个分到的越少, 但合计别超过 64。
TOTAL_CONNS_BUDGET = 64      # 全局总连接上限 (实测最优)
MAX_ACTIVE_TASKS = 1         # 同时运行的任务数 (默认 1 个, 跑满最快)
HARD_MAX_ACTIVE = 3          # 同时任务数硬上限

SSL_CTX = ssl.create_default_context()

# 运行期状态
STATE = {
    "data_dir": None,
    "config": {},
    "tasks": {},           # task_id -> Task
    "lock": threading.Lock(),
    "qr": {},              # qr_id -> dict
    # 全局连接预算: 每个分片请求开始前 acquire, 结束后 release
    "conn_sem": None,      # 延迟创建 (需要知道预算值)
    "sched_lock": threading.Lock(),
    "queue": [],           # 等待运行的任务 id (FIFO)
}


# ============================================================
#  工具
# ============================================================
def log(msg):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


def now():
    return time.time()


def http_req(url, headers=None, timeout=30, method="GET", data=None,
             ctx=SSL_CTX):
    h = {"User-Agent": UA_BROWSER, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, method=method, data=data)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read(), dict(r.headers), r.status


def http_text(url, headers=None, timeout=30, data=None, method="GET"):
    body, hdr, st = http_req(url, headers, timeout, method, data)
    return body.decode("utf-8", "replace")


def fmt_size(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%.0f %s" % (n, u)) if u == "B" else ("%.2f %s" % (n, u))
        n /= 1024.0


def fmt_eta(seconds):
    """把剩余秒数格式化成易读文本

    1 分钟以内精确到秒, 1 小时以内精确到分, 再长就到小时/天。
    """
    try:
        s = int(seconds)
    except Exception:
        return ""
    if s <= 0:
        return ""
    if s < 60:
        return "%d 秒" % s
    if s < 3600:
        m = s // 60
        r = s % 60
        return "%d 分 %d 秒" % (m, r) if m < 10 else "%d 分" % m
    if s < 86400:
        h = s // 3600
        m = (s % 3600) // 60
        return "%d 小时 %d 分" % (h, m) if h < 10 else "%d 小时" % h
    d = s // 86400
    h = (s % 86400) // 3600
    return "%d 天 %d 小时" % (d, h) if d < 10 else "%d 天" % d


# ============================================================
#  配置持久化
# ============================================================
def cfg_path():
    return os.path.join(STATE["data_dir"], "config.json")


def load_config():
    try:
        with open(cfg_path(), "r", encoding="utf-8") as f:
            STATE["config"] = json.load(f)
    except Exception:
        STATE["config"] = {}
    c = STATE["config"]
    c.setdefault("cookie", "")
    c.setdefault("bdstoken", "")
    c.setdefault("uk", "")
    c.setdefault("user_name", "")
    c.setdefault("download_dir", "")
    c.setdefault("conns", DEFAULT_CONNS)
    c.setdefault("total_conns", TOTAL_CONNS_BUDGET)
    c.setdefault("max_active_tasks", MAX_ACTIVE_TASKS)
    c.setdefault("speed_limit_mb", 0)      # 总下载限速 (MB/s), 0 = 不限
    c.setdefault("last_out_dir", "")       # 上次下载用的目录 (弹窗默认值)
    # 清理旧的磁盘优化配置项 (已移除)
    for _k in ("io_idle", "write_buffer_mb", "write_rate_mb",
               "use_staging", "staging_dir"):
        c.pop(_k, None)
    # 自愈: 清洗历史 cookie 里的换行/重复项
    fixed = clean_cookie(c.get("cookie", ""))
    if fixed != c.get("cookie", ""):
        log("已自动清洗配置文件中的 cookie")
        c["cookie"] = fixed
        try:
            save_config()
        except Exception:
            pass
    return c


def save_config():
    tmp = cfg_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE["config"], f, ensure_ascii=False, indent=2)
    os.replace(tmp, cfg_path())


# ============================================================
#  百度网盘 API
# ============================================================
def clean_cookie(raw):
    """清洗 cookie: 去掉换行/多余空白, 去重同名项

    HTTP header 不能含换行, 否则会报 Invalid header value。
    从浏览器或进程内存提取的 cookie 常带换行和重复项。
    """
    if not raw:
        return ""
    s = str(raw).replace("\r", ";").replace("\n", ";")
    parts = []
    seen = set()
    for seg in s.split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        name = seg.split("=", 1)[0].strip()
        if not name or name in seen:
            continue
        seen.add(name)
        parts.append(seg)
    return "; ".join(parts)


def bd_headers(ua=UA_PC):
    c = STATE["config"]
    return {
        "User-Agent": ua,
        "Cookie": clean_cookie(c.get("cookie", "")),
        "Referer": "https://pan.baidu.com/disk/main",
    }


def bd_bdstoken():
    """获取或刷新 bdstoken"""
    c = STATE["config"]
    if c.get("bdstoken"):
        return c["bdstoken"]
    try:
        t = http_text(
            "https://pan.baidu.com/api/gettemplatevariable"
            '?fields=["bdstoken"]&clienttype=0&app_id=%s&web=1' % APP_ID,
            headers=bd_headers(UA_BROWSER), timeout=20)
        j = json.loads(t)
        tok = (j.get("result") or {}).get("bdstoken", "")
        if tok:
            c["bdstoken"] = tok
            save_config()
        return tok
    except Exception as e:
        log("获取 bdstoken 失败: %s" % e)
        return ""


def bd_userinfo():
    """用 cookie 换用户信息 (验证登录态)"""
    c = STATE["config"]
    tok = bd_bdstoken()
    t = http_text("https://pan.baidu.com/api/gettemplatevariable"
                  '?fields=["bdstoken","uk"]&clienttype=0&app_id=%s&web=1&bdstoken=%s'
                  % (APP_ID, tok),
                  headers=bd_headers(UA_BROWSER), timeout=20)
    j = json.loads(t)
    r = j.get("result") or {}
    uk = r.get("uk") or ""
    if uk:
        c["uk"] = str(uk)
    # 取昵称
    try:
        t2 = http_text("https://pan.baidu.com/rest/2.0/xpan/nas?method=uinfo",
                       headers=bd_headers(), timeout=15)
        j2 = json.loads(t2)
        name = j2.get("baidu_name") or j2.get("netdisk_name") or ""
        if name:
            c["user_name"] = name
    except Exception:
        pass
    save_config()
    return {"uk": c.get("uk", ""), "name": c.get("user_name", ""),
            "errno": j.get("errno", -1)}


def bd_list(dirpath, order="time", desc=True):
    """列出网盘目录

    order: time(修改时间) / name(名称) / size(大小)
    desc : True 表示倒序
    """
    tok = bd_bdstoken()
    # 百度接口对 order/desc 的组合有限制 (某些组合返回 errno=1),
    # 所以服务端只用最稳的组合取数, 最终顺序完全由本地排序决定
    bd_order = "time" if order == "time" else "name"
    bd_desc = "1" if (order == "time" and desc) else "0"

    def _fetch(o, d):
        pp = {
            "dir": dirpath, "order": o, "desc": d,
            "num": "1000", "start": "0", "web": "1",
            "app_id": APP_ID, "bdstoken": tok,
        }
        uu = "https://pan.baidu.com/api/list?" + urllib.parse.urlencode(pp)
        tt = http_text(uu, headers=bd_headers(UA_BROWSER), timeout=25)
        return json.loads(tt)

    j = _fetch(bd_order, bd_desc)
    if j.get("errno") not in (0, None):
        j = _fetch("name", "0")
    if j.get("errno") not in (0, None):
        raise RuntimeError("list errno=%s msg=%s"
                           % (j.get("errno"), j.get("errmsg")))
    out = []
    for it in (j.get("list") or []):
        out.append({
            "fs_id": str(it.get("fs_id")),
            "name": it.get("server_filename", ""),
            "path": it.get("path", ""),
            "isdir": bool(int(it.get("isdir") or 0)),
            "size": int(it.get("size") or 0),
            "size_h": fmt_size(it.get("size")),
            "mtime": int(it.get("server_mtime") or 0),
            "mtime_h": time.strftime("%Y-%m-%d %H:%M",
                                     time.localtime(int(it.get("server_mtime") or 0)))
            if it.get("server_mtime") else "",
            "md5": it.get("md5", ""),
        })

    # 服务端已排序, 这里再做一次本地排序确保稳定
    # 目录始终排在文件前面
    if order == "time":
        out.sort(key=lambda x: (not x["isdir"], -x["mtime"]))
    elif order == "size":
        out.sort(key=lambda x: (not x["isdir"],
                                -(x["size"] if not x["isdir"] else 0)))
    else:  # name
        out.sort(key=lambda x: (not x["isdir"], x["name"].lower()))
        if desc:
            dirs = [x for x in out if x["isdir"]]
            files = [x for x in out if not x["isdir"]]
            dirs.sort(key=lambda x: x["name"].lower(), reverse=True)
            files.sort(key=lambda x: x["name"].lower(), reverse=True)
            out = dirs + files

    return {"errno": 0, "list": out, "path": dirpath,
            "order": order, "desc": desc}


def set_io_idle():
    """把当前进程的 I/O 优先级降到 idle

    下载器大量顺序写盘, 而手机/SMB 播放视频需要低延迟读取。
    两者共用同一个底层设备时, 默认同一 I/O 优先级会让读请求排队,
    表现就是视频卡顿。设为 idle 后, 只有系统 I/O 空闲时才写盘,
    优先保证读取流畅 (代价: 下载速度可能略降)。
    """
    try:
        if os.geteuid() != 0:
            return False
    except AttributeError:
        return False

    pid = os.getpid()

    # 优先用 ionice 命令 (glibc 未必导出 ioprio_set 符号)
    try:
        import subprocess
        r = subprocess.run(["ionice", "-c", "3", "-p", str(pid)],
                           capture_output=True, timeout=5)
        if r.returncode == 0:
            return True
    except Exception:
        pass

    # 退回 syscall (部分架构有此符号)
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        fn = getattr(libc, "ioprio_set", None) or \
            getattr(libc, "syscall", None)
        if fn is None:
            return False
        IOPRIO_WHO_PROCESS = 1
        prio = 3 << 13           # IOPRIO_CLASS_IDLE
        if getattr(libc, "ioprio_set", None):
            return fn(IOPRIO_WHO_PROCESS, 0, prio) == 0
        # syscall(SYS_ioprio_set=251, ...)
        return fn(251, IOPRIO_WHO_PROCESS, 0, prio) == 0
    except Exception as e:
        log("设置 I/O idle 失败: %s" % e)
        return False


def preallocate(fd, size):
    """真正预分配磁盘空间, 避免边写边扩容产生碎片

    稀疏文件 (truncate) 在 btrfs 上会导致写入时频繁分配新块,
    加剧磁头/元数据操作; 用 posix_fallocate 一次性占位可显著
    降低写入期间的随机 I/O。
    """
    try:
        os.posix_fallocate(fd, 0, size)
        return True
    except (AttributeError, OSError):
        try:
            os.ftruncate(fd, size)
        except Exception:
            pass
        return False


def _pid_alive(pid):
    """判断 pid 是否存活"""
    import errno as _errno
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == _errno.EPERM
    except Exception:
        return False


def _read_lock(lock_path):
    """读锁文件, 返回 dict; 内容兼容旧的纯 pid 格式"""
    try:
        raw = open(lock_path, "r").read().strip()
    except Exception:
        return None
    if not raw:
        return None
    # 新格式: JSON;  旧格式: 纯数字
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except Exception:
            return None
    try:
        return {"pid": int(raw)}
    except Exception:
        return None


def _write_lock(lock_path, port):
    """写入锁文件 (原子)"""
    data = json.dumps({"pid": os.getpid(), "port": port,
                       "app": APP_NAME, "ts": int(time.time())})
    tmp = lock_path + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(data)
        os.replace(tmp, lock_path)
        return True
    except Exception as e:
        log("写锁文件失败: %s" % e)
        return False


def acquire_single_lock(lock_path, port):
    """确保本进程成为唯一的服务实例

    背景: fnOS 的 appcenter 与自己手动启动的进程会交叉出现。若我们检测到
    已有实例就静默退出, appcenter 会判定"本地应用启动失败"(错误码 10330) ——
    即使旧进程其实还在服务。

    因此这里采用**接管**策略: 检测到旧实例就让它退出 (SIGTERM → SIGKILL),
    由本进程接管端口与 socket。这样无论谁触发启动, 最终都是一个完整、
    由 appcenter 管理的实例 (同时有 Unix socket 和 TCP 端口)。

    下载任务支持断点续传, 接管不会丢失进度。

    返回 True 表示成功接管。
    """
    info = _read_lock(lock_path)
    if info:
        old_pid = int(info.get("pid") or 0)
        if old_pid and old_pid != os.getpid() and _pid_alive(old_pid):
            log("检测到旧实例 pid=%d, 接管中…" % old_pid)
            try:
                os.kill(old_pid, signal.SIGTERM)
                for _ in range(25):        # 最多等 5 秒
                    if not _pid_alive(old_pid):
                        break
                    time.sleep(0.2)
                if _pid_alive(old_pid):
                    log("旧实例未响应 SIGTERM, 强制结束")
                    os.kill(old_pid, signal.SIGKILL)
                    time.sleep(1.0)
                log("旧实例已退出, 由本进程接管")
            except Exception as e:
                log("结束旧实例失败: %s" % e)
        # 清理陈旧锁
        try:
            os.remove(lock_path)
        except OSError:
            pass

    _write_lock(lock_path, port)
    return True


def release_single_lock(lock_path):
    """退出时删除锁文件 (仅当锁里记的是自己)"""
    try:
        info = _read_lock(lock_path)
        if info and int(info.get("pid") or 0) == os.getpid():
            os.remove(lock_path)
    except Exception:
        pass


def resolve_owner_hint(start_path):
    """从某个路径向上推导应使用的 uid/gid

    下载器以 root 运行, 直接创建会得到 root:root。这里沿路径向上
    找到第一个「非 root」属主的祖先, 把它作为参照。
    """
    try:
        if os.geteuid() != 0:
            return None
    except AttributeError:
        return None

    try:
        probe = os.path.abspath(start_path)
        for _ in range(50):
            if os.path.exists(probe):
                try:
                    st = os.stat(probe)
                    if st.st_uid != 0 or st.st_gid != 0:
                        # 找到非 root 参照
                        if st.st_uid != 0:
                            return (st.st_uid, st.st_gid)
                except OSError:
                    pass
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
    except Exception:
        pass
    return None


def apply_owner(path, owner):
    """把 uid/gid 应用到路径 (目录则递归), 只改属主不动权限"""
    if not owner:
        return
    uid, gid = owner
    try:
        if os.geteuid() != 0:
            return
    except AttributeError:
        return

    targets = [path]
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            for root, dirs, files in os.walk(path):
                targets.extend(os.path.join(root, d) for d in dirs)
                targets.extend(os.path.join(root, f) for f in files)
    except Exception:
        pass

    for t in targets:
        try:
            os.chown(t, uid, gid)
        except (OSError, AttributeError):
            pass


def inherit_owner(path):
    """让新建的文件/目录继承上级的属主 (兼容旧调用)"""
    apply_owner(path, resolve_owner_hint(path))


def bd_dlink(fs_id):
    """取下载直链

    重要: 必须用与下载请求一致的 UA (UA_PC) 取链接。
    用浏览器 UA 取的 dlink 会绑定浏览器身份, 改用 PC 客户端 UA 下载
    会返回 403 Forbidden (实测确认)。
    """
    tok = bd_bdstoken()
    u = ("https://pan.baidu.com/api/filemetas?"
         + urllib.parse.urlencode({
             "dlink": "1", "fsids": "[%s]" % fs_id, "thumb": "0",
             "extra": "1", "bdstoken": tok,
         }, safe="[]"))
    t = http_text(u, headers=bd_headers(UA_PC), timeout=25)
    j = json.loads(t)
    info = j.get("info") or []
    if not info:
        raise RuntimeError("filemetas 失败 errno=%s msg=%s"
                           % (j.get("errno"), j.get("errmsg")))
    it = info[0]
    return it["dlink"].replace("\\/", "/"), int(it["size"])


def bd_walk(dirpath, max_files=5000, max_depth=12, _depth=0,
            _collected=None):
    """递归展开目录, 返回所有文件 (含相对路径)

    返回: [{"fs_id","name","path","size","rel_dir"}, ...]
    rel_dir 是相对于起始目录的路径, 用于在本地重建目录结构
    """
    if _collected is None:
        _collected = []
    if _depth > max_depth or len(_collected) >= max_files:
        return _collected

    try:
        d = bd_list(dirpath, order="name", desc=False)
    except Exception as e:
        log("walk %s 失败: %s" % (dirpath, e))
        return _collected

    base = dirpath.rstrip("/")
    for it in d.get("list") or []:
        if len(_collected) >= max_files:
            break
        if it["isdir"]:
            bd_walk(it["path"], max_files, max_depth, _depth + 1, _collected)
        else:
            rel = it["path"][len(base):].lstrip("/")
            rel_dir = os.path.dirname(rel)
            _collected.append({
                "fs_id": it["fs_id"],
                "name": it["name"],
                "path": it["path"],
                "size": it["size"],
                "rel_dir": rel_dir,
            })
    return _collected


# ------------------------------------------------------------
#  扫码登录
# ------------------------------------------------------------
def _qr_strip_callback(text):
    """去掉 JSONP 包装: tangram_guid_xxx({...})  ->  {...}"""
    t = (text or "").strip()
    m = re.match(r'^[\w$.]+\((.*)\)\s*;?\s*$', t, re.S)
    if m:
        return m.group(1)
    return t


def _qr_ts():
    return int(time.time() * 1000)


def _qr_make_session():
    """为一次扫码登录创建独立的会话 (cookie jar + opener)

    申请二维码、轮询状态、兑换 BDUSS 必须共用同一个会话,
    否则百度不认这是同一个登录流程。
    """
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=SSL_CTX))
    return jar, opener


def qr_new():
    """申请二维码

    关键参数 (实测必须):
      - tpl=mn       而不是 netdisk
      - apiver=v3
      - callback=... JSONP 包装, 百度据此判断是网页端
      - gid / tt / _
    缺了 callback 时, 返回的是裸 JSON, 后续轮询拿不到确认票据。

    另外: 申请二维码 / 轮询 / 兑换 三处必须共用同一个 cookie 会话。
    """
    cb = "tangram_guid_%d" % _qr_ts()
    params = {
        "lp": "pc",
        "qrloginfrom": "pc",
        "gid": "00000000-0000-0000-0000-000000000000",
        "callback": cb,
        "apiver": "v3",
        "tt": str(_qr_ts()),
        "tpl": "mn",
        "_": str(_qr_ts()),
    }
    u = ("https://passport.baidu.com/v2/api/getqrcode?"
         + urllib.parse.urlencode(params))

    jar, opener = _qr_make_session()
    try:
        req = urllib.request.Request(u, headers={
            "User-Agent": UA_BROWSER,
            "Referer": "https://passport.baidu.com/v2/?login",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9"})
        with opener.open(req, timeout=20) as r:
            t = r.read().decode("utf-8", "replace")
    except Exception as e:
        raise RuntimeError("请求二维码失败: %s" % e)

    j = json.loads(_qr_strip_callback(t))
    sign = j.get("sign", "")
    imgurl = j.get("imgurl", "")
    if not sign:
        raise RuntimeError("getqrcode 失败: %s" % t[:200])
    if not imgurl.startswith("http"):
        imgurl = "https://" + imgurl
    try:
        req2 = urllib.request.Request(imgurl, headers={
            "User-Agent": UA_BROWSER,
            "Referer": "https://passport.baidu.com/"})
        with opener.open(req2, timeout=20) as r2:
            img = r2.read()
    except Exception:
        img = b""

    qid = uuid.uuid4().hex
    st = {"sign": sign, "callback": cb, "created": now(), "status": 0,
          "msg": "请使用百度 App 扫码登录", "user": None,
          "running": True, "stop": False,
          "jar": jar, "opener": opener}
    STATE["qr"][qid] = st
    # 后台线程负责长轮询, 前端查询立即返回
    threading.Thread(target=_qr_worker, args=(qid, st), daemon=True).start()
    return {
        "qr_id": qid,
        "sign": sign,
        "image": "data:image/png;base64," + base64.b64encode(img).decode(),
    }


def _qr_bdusslogin(v_ticket, opener):
    """用扫码票据兑换 BDUSS

    百度真正的兑换接口是 /v3/login/main/qrbdusslogin,
    把票据放在 bduss 参数里。成功后 Set-Cookie 里带 BDUSS / STOKEN。

    实测: 直接请求即可拿到 cookie, sig/shaOne 缺失也能工作。
    """
    params = {
        "alg": "v3",
        "apiver": "v3",
        "bduss": v_ticket,
        "loginVersion": "v4",
        "qrcode": "1",
        "time": str(_qr_ts() // 1000),
        "tpl": "mn",
        "tt": str(_qr_ts()),
        "u": "https%3A%2F%2Fwww.baidu.com%2F",
        "elapsed": "1234",
        "v": str(_qr_ts()),
        "callback": "bd__cbs__odmford",
    }
    u = ("https://passport.baidu.com/v3/login/main/qrbdusslogin?"
         + urllib.parse.urlencode(params))
    try:
        req = urllib.request.Request(u, headers={
            "User-Agent": UA_BROWSER,
            "Referer": "https://passport.baidu.com/v2/?login",
            "Accept": "*/*",
        })
        with opener.open(req, timeout=25) as r:
            body = r.read().decode("utf-8", "replace")
            hdr = dict(r.headers)
    except Exception as e:
        log("qrbdusslogin 请求失败: %s" % e)
        return "", ""

    # 从响应 JSONP 里判断是否成功
    try:
        j = json.loads(_qr_strip_callback(body))
        code = str(j.get("code", ""))
        ei = j.get("errInfo") or {}
        if code not in ("110000", "0"):
            log("qrbdusslogin code=%s errInfo=%s" % (code, ei))
        if ei.get("no") not in (None, "0", 0):
            log("qrbdusslogin errInfo=%s" % ei)
    except Exception:
        pass

    return body, hdr.get("Set-Cookie", "")


def _qr_cookie_from_jar(jar):
    """从 cookie jar 拼出 cookie 串 (BDUSS + STOKEN 必需)"""
    want = ("BDUSS", "STOKEN", "PTOKEN", "UBI")
    got = {}
    for c in jar:
        n = c.name.upper()
        for w in want:
            if n == w and w not in got:
                got[w] = c.value
    parts = []
    if "BDUSS" in got:
        parts.append("BDUSS=" + got["BDUSS"])
    if "STOKEN" in got:
        parts.append("STOKEN=" + got["STOKEN"])
    if "PTOKEN" in got:
        parts.append("PTOKEN=" + got["PTOKEN"])
    if "UBI" in got:
        parts.append("UBI=" + got["UBI"])
    return "; ".join(parts), got


def _qr_worker(qid, st):
    """后台长轮询百度通道, 把状态写进 st

    必须复用 qr_new() 里创建的会话 (jar + opener):
    申请二维码 / 轮询 / 兑换 BDUSS 是同一个登录流程, 换会话会导致
    百度不返回确认票据。
    """
    jar = st.get("jar")
    opener = st.get("opener")
    if jar is None or opener is None:
        jar, opener = _qr_make_session()
        st["jar"], st["opener"] = jar, opener

    deadline = now() + 300
    scanned = False
    while not st.get("stop") and now() < deadline:
        if st.get("status") in (2, -1, -2):
            return
        try:
            # 关键: 轮询必须用与申请二维码一致的 tpl=mn + callback + apiver=v3,
            # 否则百度返回的 channel_v 里没有确认票据 v
            pp = {
                "channel_id": st["sign"],
                "tpl": "mn",
                "gid": "00000000-0000-0000-0000-000000000000",
                "callback": st.get("callback", ""),
                "apiver": "v3",
                "tt": str(_qr_ts()),
                "_": str(_qr_ts()),
            }
            u = ("https://passport.baidu.com/channel/unicast?"
                 + urllib.parse.urlencode(pp))
            req = urllib.request.Request(u, headers={
                "User-Agent": UA_BROWSER,
                "Referer": "https://passport.baidu.com/v2/?login",
                "Accept": "*/*"})
            # 已扫码后用短超时, 快速轮询; 未扫码用长超时
            to = 10 if scanned else 30
            with opener.open(req, timeout=to) as r:
                t = r.read().decode("utf-8", "replace")
            j = json.loads(_qr_strip_callback(t))
        except Exception:
            # 长轮询超时属正常
            continue

        if j.get("errno") != 0:
            continue

        try:
            inner = json.loads(j.get("channel_v") or "{}")
        except Exception:
            continue

        s = inner.get("status")
        v_ticket = inner.get("v") or ""

        # 重要: 百度在「已扫码待确认」时返回 status=1,
        # 而**用户点确认之后返回的是 status=0 + v 票据** (不是 status=2).
        # 且 v 票据的有效窗口极短 (实测约 2 秒), 必须立刻兑换。
        # 因此只要响应里带了 v, 就先尝试兑换 BDUSS, 不要被 status 值拦住。
        if s == 1:
            if not scanned:
                log("qr 已扫码, 等待手机确认")
            scanned = True
            st["status"] = 1
            st["msg"] = "已扫码, 请在手机上确认"
            if not v_ticket:
                continue
            # status=1 但带 v: 也直接兑换
            log("qr status=1 且带 v, 尝试兑换")

        if s in (3, 4, 5):
            st["status"] = -1
            st["msg"] = "已取消或过期, 请刷新二维码"
            return

        # 没有 v 且 status 不是 2 → 继续等
        if not v_ticket and s != 2:
            continue

        # 记录原始 channel_v 便于排查
        st["raw_v"] = j.get("channel_v")
        log("qr 收到票据 status=%r v=%r" % (s, str(v_ticket)[:80]))

        # === 兑换 BDUSS: 百度真正的接口是 /v3/login/main/qrbdusslogin ===
        bduss = ""
        stoken = ""
        try:
            body, setck = _qr_bdusslogin(v_ticket, opener)
            cookiestr, got = _qr_cookie_from_jar(jar)
            if got.get("BDUSS"):
                bduss = got["BDUSS"]
                stoken = got.get("STOKEN", "")
                log("qrbdusslogin 成功, BDUSS 长度 %d, STOKEN %s"
                    % (len(bduss), "有" if stoken else "无"))
        except Exception as e:
            log("qrbdusslogin 异常: %s" % e)

        # 兜底: 从响应体里正则抓 BDUSS
        if not bduss:
            try:
                m = re.search(r"[Bb][Dd][Uu][Ss][Ss]=([A-Za-z0-9_%+/=\-]{20,400})",
                              body if "body" in dir() else "")
                if m:
                    bduss = m.group(1)
                    log("从响应体正则取到 BDUSS")
            except Exception:
                pass

        if not bduss:
            log("qr 未取得 bduss, status=%r v=%r, jar=%r"
                % (s, str(v_ticket)[:60],
                   [(c.name, c.value[:20]) for c in jar]))
            st["status"] = -2
            st["msg"] = "未取得 BDUSS, 请重试"
            return

        # 组装完整 cookie (BDUSS + STOKEN 都需要)
        ck = "BDUSS=" + bduss
        if stoken:
            ck += "; STOKEN=" + stoken
        STATE["config"]["cookie"] = ck
        STATE["config"]["bdstoken"] = ""
        save_config()

        try:
            info = bd_userinfo()
        except Exception:
            info = {}
        if not info.get("uk"):
            log("扫码后 uinfo 校验失败, 但仍保存 cookie")
        else:
            log("扫码登录成功: %s (uk=%s)"
                % (info.get("name") or "(无昵称)", info.get("uk")))
        st["user"] = info
        st["status"] = 2
        st["msg"] = "登录成功"
        return

    if st.get("status") not in (2, -2):
        st["status"] = -1
        st["msg"] = "二维码已过期, 请刷新"


def qr_poll(qid):
    """查询扫码状态 (立即返回, 不阻塞)"""
    st = STATE["qr"].get(qid)
    if not st:
        return {"status": -1, "msg": "二维码已失效, 请刷新"}
    r = {"status": st.get("status", 0), "msg": st.get("msg", "等待扫码")}
    if st.get("user"):
        r["user"] = st["user"]
    if st.get("raw_v"):
        r["debug_raw_v"] = str(st["raw_v"])[:500]
    if st.get("status") in (2, -1, -2):
        st["stop"] = True
        # 保留短暂时间供前端读取最终状态
        def _cleanup():
            time.sleep(30)
            STATE["qr"].pop(qid, None)
        threading.Thread(target=_cleanup, daemon=True).start()
    return r


def cookie_login(cookie_text):
    """cookie 登录: 接受完整 cookie 串或仅 BDUSS"""
    ck = clean_cookie(cookie_text)
    if not ck:
        raise RuntimeError("cookie 为空")
    if "BDUSS=" not in ck:
        ck = "BDUSS=" + ck
    if not re.search(r"BDUSS=[A-Za-z0-9_%+/=\-]{20,}", ck):
        raise RuntimeError("cookie 里没有有效的 BDUSS")
    STATE["config"]["cookie"] = ck
    STATE["config"]["bdstoken"] = ""
    save_config()
    info = bd_userinfo()
    if not info.get("uk"):
        raise RuntimeError("cookie 无效或已过期")
    return info


def logout():
    STATE["config"]["cookie"] = ""
    STATE["config"]["bdstoken"] = ""
    STATE["config"]["uk"] = ""
    STATE["config"]["user_name"] = ""
    save_config()
    return {"ok": True}


# ============================================================
#  全局调度器: 控制同时运行的任务数 + 全局连接预算
# ============================================================
class GlobalRateLimiter(object):
    """全局下载限速器 (所有任务共享同一个速率窗口)

    用令牌桶思想: 按目标速率匀速发放额度, 任务每下载一块就消费对应
    字节数, 超额时短暂 sleep。这样总速率被限制在设定值, 不会因为
    多任务并发而翻倍。

    速率为 0 表示不限速。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.t0 = None
        self.consumed = 0
        self.rate = 0            # bytes/s

    def set_rate(self, mb_per_sec):
        try:
            v = float(mb_per_sec or 0)
        except Exception:
            v = 0.0
        with self.lock:
            self.rate = int(v * 1048576) if v > 0 else 0
            self.t0 = None
            self.consumed = 0

    def refresh(self):
        """从配置重新读取速率 (配置变更后调用)"""
        try:
            self.set_rate(STATE["config"].get("speed_limit_mb") or 0)
        except Exception:
            pass

    def consume(self, nbytes):
        """消费 nbytes 额度, 超速时阻塞等待"""
        if not self.rate:
            return
        try:
            now_t = time.time()
            wait = 0.0
            with self.lock:
                if self.t0 is None:
                    self.t0 = now_t
                    self.consumed = 0
                self.consumed += nbytes
                elapsed = now_t - self.t0
                expected = self.consumed / float(self.rate)
                if expected > elapsed:
                    wait = expected - elapsed
                elif elapsed > 10.0:
                    # 窗口过长就重置, 避免历史欠账累积
                    # (窗口要足够长, 太短会让限速形同虚设)
                    self.t0 = now_t
                    self.consumed = 0
            if wait > 0:
                time.sleep(wait)
        except Exception:
            pass


_LIMITER = GlobalRateLimiter()


def global_limiter():
    return _LIMITER


def get_conn_sem():
    """获取全局连接信号量 (懒创建)"""
    if STATE.get("conn_sem") is None:
        with STATE["sched_lock"]:
            if STATE.get("conn_sem") is None:
                budget = int(STATE["config"].get("total_conns")
                             or TOTAL_CONNS_BUDGET)
                budget = max(4, min(budget, 128))
                STATE["conn_sem"] = threading.BoundedSemaphore(budget)
                log("全局连接预算: %d" % budget)
    return STATE["conn_sem"]


def submit_task(task):
    """把任务放进等待队列"""
    with STATE["sched_lock"]:
        if task.id not in STATE["queue"]:
            STATE["queue"].append(task.id)
    _pump_scheduler()


def _pump_scheduler():
    """按并发上限启动排队中的任务, 并统一分配连接预算"""
    with STATE["sched_lock"]:
        running = [t for t in STATE["tasks"].values()
                   if t.state == "running"]
        max_active = int(STATE["config"].get("max_active_tasks")
                         or MAX_ACTIVE_TASKS)
        max_active = max(1, min(max_active, HARD_MAX_ACTIVE))

        to_start = []
        while STATE["queue"] and len(running) + len(to_start) < max_active:
            tid = STATE["queue"].pop(0)
            t = STATE["tasks"].get(tid)
            if not t or t.state in ("done", "error"):
                continue
            if t.state == "paused":
                continue
            to_start.append(t)

    for t in to_start:
        try:
            log("调度: 启动任务 %s (%s)" % (t.id, t.name[:40]))
            t._start_real()
        except Exception as e:
            log("调度: 启动 %s 失败: %s" % (t.id, e))

    # 统一重算所有运行中任务的连接数 (新启动的 + 原有的)
    try:
        rebalance_conns()
    except Exception as e:
        log("重分配失败: %s" % e)

    # 启动后等它们进入 running 再排下一轮
    if to_start:
        def _later():
            time.sleep(2)
            _pump_scheduler()
        threading.Thread(target=_later, daemon=True).start()


def rebalance_conns():
    """重新计算所有运行中任务的连接数并下发

    下载线程会周期性读取 conns_effective, 所以这里改值即可动态生效。
    """
    budget = int(STATE["config"].get("total_conns") or TOTAL_CONNS_BUDGET)
    budget = max(4, min(budget, 128))
    with STATE["sched_lock"]:
        running = [t for t in STATE["tasks"].values()
                   if t.state == "running"]
    n = max(1, len(running))
    share = max(4, budget // n)
    for t in running:
        eff = min(t.conns, share)
        if getattr(t, "conns_effective", None) != eff:
            t.conns_effective = eff
            log("重分配: 任务 %s -> %d 连接 (共 %d 个任务, 预算 %d)"
                % (t.id, eff, n, budget))
    return n, share


def on_task_finished(task):
    """任务结束/暂停后, 触发调度下一个, 并重算剩余任务的连接数"""
    with STATE["sched_lock"]:
        if task.id in STATE["queue"]:
            try:
                STATE["queue"].remove(task.id)
            except ValueError:
                pass
    def _later():
        time.sleep(0.5)
        try:
            rebalance_conns()
        except Exception:
            pass
        _pump_scheduler()
    threading.Thread(target=_later, daemon=True).start()


# ============================================================
#  下载引擎
# ============================================================
class Task(object):
    def __init__(self, fs_id, name, path, size, out_dir, conns):
        self.id = uuid.uuid4().hex[:12]
        self.fs_id = str(fs_id)
        self.name = name
        self.path = path
        self.size = int(size)
        self.out_dir = out_dir
        self.conns = max(1, min(int(conns or DEFAULT_CONNS), MAX_CONNS))
        # 实际使用的连接数, 由调度器按全局预算均分后写入
        self.conns_effective = self.conns
        # 用 fs_id 派生唯一键, 供 SSD 中转文件命名
        self.fsid_key = str(fs_id).replace("/", "_")[:40]

        self.out_path = os.path.join(out_dir, name)
        self.part_path = self.out_path + ".part"
        self.meta_path = self.out_path + ".meta.json"

        # 创建时就解析好属主, 避免多任务并发时互相看到 root 目录而判断错
        self.owner = resolve_owner_hint(out_dir)

        self.chunk = CHUNK
        self.nchunks = (self.size + CHUNK - 1) // CHUNK
        self.done = set()
        self.state = "pending"        # pending/running/paused/done/error
        self.msg = ""
        self.bytes_done = 0
        self.speed = 0.0
        self.created = now()
        self.started = 0
        self.finished = 0
        self.dlink = ""
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._wfd = None          # 复用写入句柄 (pwrite), 减元数据操作
        self._wr_lock = threading.Lock()
        self._wbuf = []           # [(offset, bytes)] 待落盘缓冲
        self._wbuf_bytes = 0
        # 攒够一块再落盘 (顺序写更省磁盘操作); 4MB 兼顾效率与暂停响应速度
        self._wbuf_limit = 4 * 1024 * 1024
        # 下载限速由全局 limiter 统一处理, 这里不再单独控制
        self._rate_bytes = 0
        self._rate_t0 = None
        self._rate_written = 0

    # --- 序列化 ---
    def to_dict(self):
        prog = (self.bytes_done / self.size * 100) if self.size else 0

        # 剩余时间估算
        # 用「已下载字节 / 已耗时」算平均速度, 比瞬时速度稳定,
        # 避免因为网络抖动导致剩余时间来回跳。
        remain = max(0, self.size - self.bytes_done)
        eta = 0
        eta_h = ""
        elapsed = (self.finished or now()) - self.started if self.started else 0
        if self.state == "done":
            eta = 0
            eta_h = "已完成"
        elif self.state in ("running", "pending") and remain > 0:
            avg = 0.0
            if elapsed > 1 and self.bytes_done > 0:
                avg = self.bytes_done / elapsed          # bytes/s
            # 平均速度还没建立起来时, 退回瞬时速度
            if avg <= 0 and self.speed > 0:
                avg = self.speed * 1048576
            if avg > 0:
                eta = int(remain / avg)
                eta_h = fmt_eta(eta)
            else:
                eta = 0
                eta_h = "计算中…"
        elif self.state == "paused":
            eta_h = "已暂停"
        elif self.state == "error":
            eta_h = "已中断"
        else:
            eta_h = ""

        return {
            "id": self.id,
            "fs_id": self.fs_id,
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "size_h": fmt_size(self.size),
            "out_path": self.out_path,
            "state": self.state,
            "msg": self.msg,
            "progress": round(prog, 2),
            "bytes_done_h": fmt_size(self.bytes_done),
            "bytes_remain_h": fmt_size(remain),
            "speed": round(self.speed, 2),
            "speed_h": "%.2f MB/s" % self.speed,
            "eta": eta,
            "eta_h": eta_h,
            "nchunks": self.nchunks,
            "done_chunks": len(self.done),
            "conns": self.conns,
            "created": self.created,
            "elapsed": int(elapsed) if self.started else 0,
        }

    # --- 断点 ---
    def load_meta(self):
        if os.path.exists(self.meta_path) and os.path.exists(self.part_path):
            try:
                m = json.load(open(self.meta_path))
                if m.get("total") == self.size and m.get("chunk") == self.chunk:
                    self.done = set(m.get("done", []))
                    return True
            except Exception:
                pass
        return False

    def save_meta(self):
        try:
            tmp = self.meta_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"total": self.size, "chunk": self.chunk,
                           "done": sorted(self.done)}, f)
            os.replace(tmp, self.meta_path)
            # 断点续传的元数据也放到对应用户名下, 避免 root 残留
            apply_owner(self.meta_path, self.owner)
        except Exception:
            pass

    def prep(self):
        created_dirs = []
        # 逐级创建, 记录新目录用于修正属主
        d = os.path.abspath(self.out_dir)
        stack = []
        while d and d != "/" and not os.path.isdir(d):
            stack.append(d)
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        try:
            os.makedirs(self.out_dir, exist_ok=True)
        except Exception:
            pass

        # 最终确认属主: 缓存的优先, 否则现算一次
        own = self.owner or resolve_owner_hint(self.out_dir)
        self.owner = own
        # 新建的每一层目录都用同一个属主, 避免并发竞态
        for newdir in reversed(stack):
            apply_owner(newdir, own)
        apply_owner(self.out_dir, own)

        # 实际写入路径
        self.write_path = self.part_path

        if not os.path.exists(self.write_path):
            # 用 posix_fallocate 真正预分配, 避免稀疏文件边写边扩容
            # 造成大量元数据操作与碎片化写入 (会拖慢其他客户端读)
            try:
                fd = os.open(self.write_path,
                             os.O_CREAT | os.O_WRONLY, 0o644)
                try:
                    ok = preallocate(fd, self.size)
                    if not ok:
                        log("task %s 预分配退回 truncate" % self.id)
                finally:
                    os.close(fd)
            except Exception as e:
                log("task %s 预分配失败: %s" % (self.id, e))
                try:
                    with open(self.write_path, "wb") as f:
                        f.truncate(self.size)
                except Exception:
                    pass
        # 无论新建还是复用, 都确保属主正确
        apply_owner(self.write_path, own)
        if os.path.exists(self.meta_path):
            apply_owner(self.meta_path, own)

        self.load_meta()
        self.bytes_done = len(self.done) * self.chunk
        if self.bytes_done > self.size:
            self.bytes_done = self.size

    def refresh_dlink(self):
        try:
            self.dlink, _ = bd_dlink(self.fs_id)
            log("task %s 刷新 dlink" % self.id)
            return True
        except Exception as e:
            log("task %s 刷新 dlink 失败: %s" % (self.id, e))
            return False

    def start(self):
        """提交到调度队列, 由全局调度器控制同时运行的任务数"""
        if self._thread and self._thread.is_alive():
            return
        if self.state in ("running", "done"):
            return
        self._stop.clear()
        self.state = "queued"
        self.msg = "排队中"
        submit_task(self)

    def _start_real(self):
        """真正启动下载线程 (由调度器调用)

        按「全局预算 / 同时运行任务数」均分连接数, 而不是让所有任务的
        线程去抢同一个信号量 —— Python 的 Semaphore 不保证公平, 抢不到
        的任务会一直是 0 片。
        """
        self._stop.clear()

        budget = int(STATE["config"].get("total_conns") or TOTAL_CONNS_BUDGET)
        budget = max(4, min(budget, 128))
        max_active = int(STATE["config"].get("max_active_tasks")
                         or MAX_ACTIVE_TASKS)
        max_active = max(1, min(max_active, HARD_MAX_ACTIVE))

        # 每个任务分到的连接数
        share = max(4, budget // max_active)
        eff = min(self.conns, share)
        self.conns_effective = eff

        self.state = "running"
        nthreads = max(1, getattr(self, "conns_effective", self.conns))
        if nthreads < self.conns:
            self.msg = "并发 %d/%d (预算均分)" % (nthreads, self.conns)
        else:
            self.msg = ""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def pause(self):
        self._stop.set()
        self._close_wfd()

    def _run(self):
        try:
            if self.state != "running":
                self.state = "running"
            self.started = now()
            self.prep()
            if not self.dlink:
                if not self.refresh_dlink():
                    raise RuntimeError("获取下载链接失败")

            import queue as _q
            q = _q.Queue()
            for i in range(self.nchunks):
                if i not in self.done:
                    q.put(i)
            total_todo = q.qsize()
            if total_todo == 0:
                self._finish()
                return

            self.t0 = now()
            self.t_bytes = self.bytes_done
            fail = [0]
            stats_lock = threading.Lock()

            # 活跃线程计数: 用于动态控制实际并发 (conns_effective 可下调)
            self._active = 0
            self._active_lock = threading.Lock()

            def worker(wid):
                while not self._stop.is_set():
                    # 只有序号在 conns_effective 之内的线程才工作,
                    # 这样调度器下调连接数时多余的线程自动停手
                    eff = max(1, getattr(self, "conns_effective", self.conns))
                    if wid >= eff:
                        time.sleep(0.5)
                        continue
                    try:
                        idx = q.get_nowait()
                    except Exception:
                        return
                    start = idx * self.chunk
                    end = min(start + self.chunk - 1, self.size - 1)
                    want = end - start + 1
                    ok = False
                    last_err = ""
                    for attempt in range(5):
                        if self._stop.is_set():
                            return
                        try:
                            url = self.dlink
                            if not url:
                                raise IOError("dlink 为空")
                            req = urllib.request.Request(url, headers={
                                "User-Agent": UA_PC,
                                "Cookie": clean_cookie(
                                    STATE["config"].get("cookie", "")),
                                "Referer": "https://pan.baidu.com/",
                                "Range": "bytes=%d-%d" % (start, end),
                                "Accept": "*/*",
                                "Accept-Encoding": "identity",
                            })
                            with urllib.request.urlopen(req, timeout=90,
                                                        context=SSL_CTX) as r:
                                buf = bytearray()
                                while len(buf) < want:
                                    b = r.read(min(65536, want - len(buf)))
                                    if not b:
                                        break
                                    buf.extend(b)
                            if len(buf) != want:
                                raise IOError("短读 %d/%d" % (len(buf), want))
                            # 全局下载限速: 超额时在这里阻塞等待
                            if self._stop.is_set():
                                return
                            self._throttle_write(want)
                            # 任务已被删除时不要继续写 (会抛 FileNotFoundError)
                            if self._stop.is_set():
                                return
                            try:
                                # 攒够缓冲再落盘: 1MB 一片单独 pwrite 会让
                                # HDD 磁头频繁跳转, 换成按 8MB 批量顺序写
                                self._wbuf.append((start, buf))
                                self._wbuf_bytes += want
                                if self._wbuf_bytes >= self._wbuf_limit:
                                    self._flush_wbuf()
                            except FileNotFoundError:
                                # .part 被删了 (任务被删除), 安静退出
                                self._stop.set()
                                return
                            with self._lock:
                                self.done.add(idx)
                                self.bytes_done += want
                            ok = True
                            break
                        except Exception as e:
                            if self._stop.is_set():
                                return
                            last_err = "%s: %s" % (type(e).__name__, e)
                            # 第一次失败就记录, 便于排查
                            if attempt == 0:
                                self._err_counters = getattr(
                                    self, "_err_counters", {})
                                key = last_err[:100]
                                self._err_counters[key] = \
                                    self._err_counters.get(key, 0) + 1
                                if self._err_counters[key] <= 2:
                                    log("task %s 分片 %d 失败: %s"
                                        % (self.id, idx, last_err[:200]))
                            if "403" in str(e) or "Forbidden" in str(e):
                                self.refresh_dlink()
                                time.sleep(0.5)
                            else:
                                time.sleep(1 + attempt)
                    if not ok:
                        with stats_lock:
                            fail[0] += 1
                        if fail[0] == 1:
                            self.msg = "首片失败: %s" % last_err[:150]

            # 线程数固定为用户设置的上限; 实际并发由 conns_effective 控制,
            # 这样调度器可以在运行期间动态均分, 不必重启任务
            nthreads = max(1, self.conns)
            ths = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(nthreads)]
            for t in ths:
                t.start()

            # 进度
            while any(t.is_alive() for t in ths):
                time.sleep(2)
                el = now() - self.t0
                if el > 0:
                    self.speed = (self.bytes_done - self.t_bytes) / el / 1048576
                if int(el) % 12 < 2:
                    self.save_meta()
                if self._stop.is_set():
                    break

            self._stop.set()
            for t in ths:
                t.join(timeout=5)
            # 线程都停了, 把残留缓冲刷盘再记 meta
            try:
                self._flush_wbuf()
            except Exception as e:
                log("task %s 刷缓冲失败: %s" % (self.id, e))
            self.save_meta()

            if fail[0] > 0 and len(self.done) < self.nchunks:
                self.state = "error"
                self.msg = "%d 片下载失败, 可重试续传" % fail[0]
                self._fix_owners()
                on_task_finished(self)
                return
            if len(self.done) >= self.nchunks:
                self._finish()
            else:
                self.state = "paused"
                self.msg = "已暂停"
                self._fix_owners()
        except Exception as e:
            self.state = "error"
            self.msg = str(e)[:300]
            log("task %s 异常: %s\n%s" % (self.id, e, traceback.format_exc()))
            self._fix_owners()
        finally:
            # 无论成功失败暂停, 都让调度器补位
            if self.state in ("done", "error", "paused"):
                on_task_finished(self)

    def _fix_owners(self):
        """收尾: 把任务涉及的所有条目属主统一修正, 兜底消除 root 残留"""
        try:
            apply_owner(self.out_path, self.owner)
            apply_owner(self.part_path, self.owner)
            apply_owner(self.meta_path, self.owner)
            apply_owner(self.meta_path + ".tmp", self.owner)
            # 目录本身也确保正确 (但不递归整棵, 避免多任务互相拖慢)
            if os.path.isdir(self.out_dir):
                own = self.owner
                if own:
                    try:
                        if os.geteuid() == 0:
                            os.chown(self.out_dir, own[0], own[1])
                    except (OSError, AttributeError):
                        pass
        except Exception:
            pass

    def _throttle_write(self, nbytes):
        """下载限速: 控制每秒下载量

        速率取自配置 speed_limit_mb (MB/s), 0 表示不限速。
        所有任务共享同一个限速窗口 (由全局 limiter 处理)。
        """
        try:
            global_limiter().consume(nbytes)
        except Exception:
            pass

    def _close_wfd(self):
        """关闭复用的写入句柄"""
        try:
            self._flush_wbuf()
        except Exception:
            pass
        fd = getattr(self, "_wfd", None)
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
            self._wfd = None

    def _flush_wbuf(self):
        """把写缓冲里攒下的分片按偏移排序后一次性连续写成

        按偏移排序后写, 能把随机写变成接近顺序的写,
        大幅减少 HDD 磁头寻道, 从而降低对其他客户端读取的影响。
        """
        with self._wr_lock:
            if not self._wbuf:
                return
            items = self._wbuf
            self._wbuf = []
            self._wbuf_bytes = 0
            wp = getattr(self, "write_path", self.part_path)
            try:
                if self._wfd is None:
                    self._wfd = os.open(wp, os.O_WRONLY)
                # 按偏移升序排列, 相邻的合并成一次写
                items.sort(key=lambda x: x[0])
                merged = []
                for off, data in items:
                    if merged and merged[-1][0] + len(merged[-1][1]) == off:
                        merged[-1] = (merged[-1][0], merged[-1][1] + data)
                    else:
                        merged.append((off, data))
                for off, data in merged:
                    os.pwrite(self._wfd, data, off)
            except (FileNotFoundError, OSError) as e:
                # pwrite 不可用 (Windows) 时退回普通写入
                try:
                    with open(wp, "r+b") as f:
                        for off, data in sorted(items, key=lambda x: x[0]):
                            f.seek(off)
                            f.write(data)
                    if self._wfd is not None:
                        try:
                            os.close(self._wfd)
                        except Exception:
                            pass
                        self._wfd = None
                except FileNotFoundError:
                    self._stop.set()
                except Exception:
                    raise

    def _finish(self):
        try:
            self._close_wfd()
            os.replace(self.part_path, self.out_path)
            # 完成后的成品文件继承属主, 保证 fnOS 用户能操作
            apply_owner(self.out_path, self.owner)
            try:
                os.remove(self.meta_path)
            except OSError:
                pass
            self.state = "done"
            self.finished = now()
            self.speed = 0
            self.bytes_done = self.size
            self.msg = "完成"
            log("task %s 完成: %s" % (self.id, self.out_path))
        except Exception as e:
            self.state = "error"
            self.msg = "重命名失败: %s" % e


# ============================================================
#  HTTP 服务
# ============================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "BaiduNasDL/" + VERSION
    protocol_version = "HTTP/1.1"

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8",
              extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, obj)

    def _err(self, msg, code=400):
        self._json({"errno": code, "errmsg": str(msg)}, code)

    def _body_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return {}

    def _query(self):
        p = urllib.parse.urlparse(self.path)
        return p.path, urllib.parse.parse_qs(p.query)

    # ---------- 路由 ----------
    def do_GET(self):
        path, qs = self._query()
        try:
            if path in ("/", "/index.html"):
                return self.serve_static("index.html")
            if path.startswith("/static/"):
                return self.serve_static(path[8:])

            if path == "/api/ping":
                return self._json({"ok": True, "version": VERSION,
                                   "name": APP_NAME})

            if path == "/api/status":
                c = STATE["config"]
                return self._json({
                    "logged_in": bool(c.get("cookie")),
                    "uk": c.get("uk", ""),
                    "name": c.get("user_name", ""),
                    "download_dir": c.get("download_dir", ""),
                    "conns": c.get("conns", DEFAULT_CONNS),
                    "version": VERSION,
                })

            if path == "/api/qr/new":
                return self._json(qr_new())

            if path == "/api/qr/poll":
                qid = (qs.get("qr_id") or [""])[0]
                return self._json(qr_poll(qid))

            if path == "/api/qr/debug":
                out = {}
                for k, v in STATE["qr"].items():
                    out[k] = {kk: (str(vv)[:300] if vv is not None else None)
                              for kk, vv in v.items()}
                return self._json({"count": len(out), "items": out})

            if path == "/api/list":
                d = (qs.get("dir") or ["/"])[0]
                order = (qs.get("order") or ["time"])[0]
                desc_s = (qs.get("desc") or ["1"])[0]
                desc = desc_s not in ("0", "false", "False")
                return self._json(bd_list(d, order=order, desc=desc))

            if path == "/api/tasks":
                with STATE["lock"]:
                    ts = [t.to_dict() for t in STATE["tasks"].values()]
                # 队列顺序: 排队中的任务按「即将下载」的先后排列
                with STATE["sched_lock"]:
                    q_order = {tid: i for i, tid in enumerate(STATE["queue"])}

                # 分组优先级: 运行中 < 排队中 < 暂停 < 已完成/出错
                def _rank(t):
                    st = t.get("state")
                    if st == "running":
                        return 0
                    if st in ("queued", "pending"):
                        return 1
                    if st == "paused":
                        return 2
                    if st == "error":
                        return 3
                    return 4          # done

                def _sort_key(t):
                    r = _rank(t)
                    if r == 1:
                        # 排队中: 按队列位置 (越小越先下载)
                        return (r, q_order.get(t["id"], 9999), 0)
                    if r == 0:
                        # 运行中: 按开始时间, 先开始的在前
                        return (r, 0, t.get("created") or 0)
                    if r == 2:
                        # 暂停: 按创建时间倒序 (最近的在上面)
                        return (r, 0, -(t.get("created") or 0))
                    # 已完成/出错: 按创建时间倒序
                    return (r, 0, -(t.get("created") or 0))

                ts.sort(key=_sort_key)
                return self._json({"errno": 0, "tasks": ts})

            if path == "/api/config":
                c = STATE["config"]
                return self._json({
                    "download_dir": c.get("download_dir", ""),
                    "last_out_dir": c.get("last_out_dir", ""),
                    "conns": c.get("conns", DEFAULT_CONNS),
                    "total_conns": c.get("total_conns", TOTAL_CONNS_BUDGET),
                    "max_active_tasks": c.get("max_active_tasks",
                                              MAX_ACTIVE_TASKS),
                    "speed_limit_mb": c.get("speed_limit_mb", 0),
                })

            if path == "/api/browse":
                d = (qs.get("dir") or [""])[0]
                return self._json(self.browse_local(d))

            if path == "/api/task":
                tid = (qs.get("id") or [""])[0]
                t = STATE["tasks"].get(tid)
                if not t:
                    return self._err("任务不存在", 404)
                return self._json(t.to_dict())

            return self._err("not found", 404)
        except Exception as e:
            log("GET %s 失败: %s" % (path, e))
            return self._err(str(e), 500)

    def do_POST(self):
        path, qs = self._query()
        try:
            if path == "/api/login/cookie":
                d = self._body_json()
                info = cookie_login(d.get("cookie", ""))
                return self._json({"ok": True, "user": info})

            if path == "/api/logout":
                return self._json(logout())

            if path == "/api/download":
                d = self._body_json()
                fs_id = d.get("fs_id")
                name = d.get("name") or ""
                size = int(d.get("size") or 0)
                path_ = d.get("path") or ""
                if not fs_id:
                    return self._err("缺少 fs_id")
                c = STATE["config"]
                out_dir = d.get("out_dir") or c.get("download_dir") or ""
                if not out_dir:
                    return self._err("未设置下载目录")
                if not os.path.isdir(out_dir):
                    return self._err("下载目录不存在: %s" % out_dir)

                p = path_ or ("/" + name)
                # 单文件下载: 直接放到用户选择的目录下, 不再按网盘路径
                # 重建子目录 (否则会在目标目录里多出"我的资源/蒸蒸日上"等层级)
                # 文件夹下载走批量接口, 那里会正常创建对应的文件夹
                t = Task(fs_id, name or ("file_%s" % fs_id), p, size,
                         out_dir, d.get("conns") or c.get("conns"))
                with STATE["lock"]:
                    STATE["tasks"][t.id] = t
                t.start()
                # 记住本次目录, 供下次弹窗默认值使用
                try:
                    c["last_out_dir"] = out_dir
                    save_config()
                except Exception:
                    pass
                return self._json({"ok": True, "task": t.to_dict()})

            if path == "/api/download/batch":
                return self.api_download_batch()

            if path == "/api/tasks/batch":
                return self.api_tasks_batch()

            if path == "/api/task/pause":
                d = self._body_json()
                t = STATE["tasks"].get(d.get("id"))
                if not t:
                    return self._err("任务不存在", 404)
                t.pause()
                t.state = "paused"
                t.msg = "已暂停"
                on_task_finished(t)
                return self._json({"ok": True})

            if path == "/api/task/resume":
                d = self._body_json()
                t = STATE["tasks"].get(d.get("id"))
                if not t:
                    return self._err("任务不存在", 404)
                t.start()
                return self._json({"ok": True})

            if path == "/api/task/delete":
                d = self._body_json()
                tid = d.get("id")
                t = STATE["tasks"].get(tid)
                if not t:
                    return self._err("任务不存在", 404)
                # 只置停止标记, 不阻塞
                try:
                    t._stop.set()
                except Exception:
                    pass
                with STATE["lock"]:
                    STATE["tasks"].pop(tid, None)
                with STATE["sched_lock"]:
                    if tid in STATE["queue"]:
                        try:
                            STATE["queue"].remove(tid)
                        except ValueError:
                            pass
                rm = bool(d.get("remove_file"))

                def _cleanup(task=t, rm=rm):
                    try:
                        task._close_wfd()
                    except Exception:
                        pass
                    if rm:
                        for p in (task.part_path, task.out_path,
                                  task.meta_path):
                            try:
                                os.remove(p)
                            except OSError:
                                pass
                threading.Thread(target=_cleanup, daemon=True).start()
                on_task_finished(t)
                return self._json({"ok": True})

            if path == "/api/config":
                d = self._body_json()
                c = STATE["config"]
                if "download_dir" in d:
                    c["download_dir"] = d["download_dir"]
                if "conns" in d:
                    c["conns"] = max(1, min(int(d["conns"]), MAX_CONNS))
                if "total_conns" in d:
                    c["total_conns"] = max(4, min(int(d["total_conns"]), 128))
                    # 预算变了, 重建信号量
                    STATE["conn_sem"] = None
                    get_conn_sem()
                if "max_active_tasks" in d:
                    c["max_active_tasks"] = max(
                        1, min(int(d["max_active_tasks"]), HARD_MAX_ACTIVE))
                    _pump_scheduler()
                if "speed_limit_mb" in d:
                    c["speed_limit_mb"] = max(
                        0, min(float(d["speed_limit_mb"]), 500))
                    # 立即应用到全局限速器
                    global_limiter().refresh()
                save_config()
                return self._json({"ok": True, **self._public_cfg()})

            if path == "/api/out_dir":
                # 记住本次下载使用的目录, 作为下次弹窗的默认值
                d = self._body_json()
                v = (d.get("dir") or "").strip()
                if v and os.path.isdir(v):
                    STATE["config"]["last_out_dir"] = v
                    save_config()
                    return self._json({"ok": True, "last_out_dir": v})
                return self._err("目录不存在: %s" % v)

            if path == "/api/mkdir":
                d = self._body_json()
                p = d.get("path", "")
                if not p:
                    return self._err("缺少 path")
                os.makedirs(p, exist_ok=True)
                inherit_owner(p)
                return self._json({"ok": True})

            return self._err("not found", 404)
        except Exception as e:
            log("POST %s 失败: %s\n%s" % (path, e, traceback.format_exc()))
            return self._err(str(e), 500)

    # ---------- 静态 ----------
    def serve_static(self, rel):
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
        rel = rel.lstrip("/")
        target = os.path.normpath(os.path.join(base, rel))
        if not target.startswith(os.path.abspath(base)):
            return self._err("forbidden", 403)
        if not os.path.isfile(target):
            return self._err("not found", 404)
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if target.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        if target.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        with open(target, "rb") as f:
            data = f.read()
        self._send(200, data, ctype)

    # ---------- 批量下载 ----------
    # ---------- 批量任务操作 ----------
    def api_tasks_batch(self):
        """批量暂停 / 继续 / 删除任务

        请求体:
          {"action": "pause"|"resume"|"delete",
           "ids": ["id1","id2", ...],
           "remove_file": true/false}     # 仅 delete 用

        不传 ids 时, 对该 action 适用的「全部」任务生效:
          pause  -> 所有 running / queued
          resume -> 所有 paused
          delete -> 所有 done / error / paused
        """
        d = self._body_json()
        action = (d.get("action") or "").strip().lower()
        ids = d.get("ids")
        remove_file = bool(d.get("remove_file"))
        if action not in ("pause", "resume", "delete"):
            return self._err("action 必须是 pause/resume/delete")

        with STATE["lock"]:
            all_tasks = list(STATE["tasks"].values())

        if ids:
            targets = [t for t in all_tasks if t.id in set(ids)]
        else:
            # 未指定 id: 按 action 作用到所有符合条件的任务
            if action == "pause":
                targets = [t for t in all_tasks
                           if t.state in ("running", "queued", "pending")]
            elif action == "resume":
                targets = [t for t in all_tasks
                           if t.state in ("paused", "error", "queued")]
            else:
                # delete 不传 id 时删除全部任务
                targets = list(all_tasks)

        if not targets:
            return self._json({"ok": True, "affected": 0,
                               "action": action,
                               "msg": "没有符合条件的任务"})

        affected = 0
        errors = []
        for t in targets:
            try:
                if action == "pause":
                    t.pause()
                    if t.state not in ("done", "error"):
                        t.state = "paused"
                        t.msg = "已暂停"
                    # 从调度队列移除, 避免被自动拉起
                    with STATE["sched_lock"]:
                        if t.id in STATE["queue"]:
                            try:
                                STATE["queue"].remove(t.id)
                            except ValueError:
                                pass
                    affected += 1
                elif action == "resume":
                    # 不阻塞等待旧线程, 直接重置状态后交给调度器拉起
                    try:
                        t._stop.set()
                    except Exception:
                        pass
                    old = t._thread
                    t._thread = None
                    t.state = "paused"
                    t.msg = "已暂停"

                    def _restart(task=t, prev=old):
                        # 等旧线程自然结束 (最多 5 秒), 再重新提交
                        try:
                            if prev is not None and prev.is_alive():
                                prev.join(timeout=5)
                        except Exception:
                            pass
                        try:
                            task._close_wfd()
                        except Exception:
                            pass
                        try:
                            task._stop.clear()
                            task.start()
                        except Exception as e:
                            log("恢复任务 %s 失败: %s" % (task.id, e))
                    threading.Thread(target=_restart, daemon=True).start()
                    affected += 1
                else:  # delete
                    # 暂停只是置停止标记, 不阻塞
                    try:
                        t._stop.set()
                    except Exception:
                        pass
                    with STATE["lock"]:
                        STATE["tasks"].pop(t.id, None)
                    with STATE["sched_lock"]:
                        if t.id in STATE["queue"]:
                            try:
                                STATE["queue"].remove(t.id)
                            except ValueError:
                                pass
                    # 收尾工作 (flush 缓冲 / 删文件) 放到后台, 避免接口卡住
                    def _cleanup(task=t, rm=remove_file):
                        try:
                            task._close_wfd()
                        except Exception:
                            pass
                        if rm:
                            for p_ in (task.part_path, task.out_path,
                                       task.meta_path,
                                       task.meta_path + ".tmp"):
                                try:
                                    os.remove(p_)
                                except OSError:
                                    pass
                    threading.Thread(target=_cleanup, daemon=True).start()
                    affected += 1
            except Exception as e:
                errors.append({"id": t.id, "err": str(e)[:120]})

        log("批量操作 %s: 命中 %d 个任务" % (action, affected))

        # 触发调度重新排布
        def _kick():
            time.sleep(0.3)
            try:
                rebalance_conns()
            except Exception:
                pass
            _pump_scheduler()
        threading.Thread(target=_kick, daemon=True).start()

        return self._json({
            "ok": True,
            "action": action,
            "matched": len(targets),
            "affected": affected,
            "errors": errors[:20],
        })

    # ---------- 批量下载 ----------
    def api_download_batch(self):
        """批量下载: 接收勾选的条目 (文件或文件夹)

        请求体:
          {
            "items": [
               {"fs_id":"...","name":"...","path":"/x","size":123,"isdir":false},
               {"fs_id":"...","name":"...","path":"/dir","isdir":true}
            ],
            "out_dir": "可选, 覆盖默认下载目录",
            "conns":   可选
          }

        文件夹会被递归展开, 并在本地创建同名目录结构。
        """
        d = self._body_json()
        items = d.get("items") or []
        if not items:
            return self._err("没有选中条目")

        cfg = STATE["config"]
        out_dir = d.get("out_dir") or cfg.get("download_dir") or ""
        if not out_dir:
            return self._err("未设置下载目录")
        if not os.path.isdir(out_dir):
            return self._err("下载目录不存在: %s" % out_dir)

        conns = d.get("conns") or cfg.get("conns")

        # 1) 展开成文件清单
        flat = []          # [{fs_id, name, path, size, rel_dir}]
        skipped = []
        for it in items:
            p = it.get("path") or ""
            if not p:
                skipped.append({"name": it.get("name", "?"),
                                "reason": "缺少路径"})
                continue
            if it.get("isdir"):
                base_name = it.get("name") or os.path.basename(p.rstrip("/"))
                files = bd_walk(p)
                if not files:
                    skipped.append({"name": base_name, "reason": "空文件夹"})
                    continue
                for f in files:
                    # 本地目录 = 文件夹名 + 内部相对路径
                    rd = os.path.join(base_name, f["rel_dir"]) \
                        if f["rel_dir"] else base_name
                    flat.append({
                        "fs_id": f["fs_id"],
                        "name": f["name"],
                        "path": f["path"],
                        "size": f["size"],
                        "rel_dir": rd,
                    })
            else:
                flat.append({
                    "fs_id": it.get("fs_id"),
                    "name": it.get("name") or os.path.basename(p),
                    "path": p,
                    "size": int(it.get("size") or 0),
                    "rel_dir": "",
                })

        if not flat:
            return self._err("展开后没有可下载的文件")

        # 2) 去重 (同一 fs_id 只下一次)
        seen = set()
        uniq = []
        dup = 0
        for f in flat:
            if f["fs_id"] in seen:
                dup += 1
                continue
            seen.add(f["fs_id"])
            uniq.append(f)

        # 3) 预先创建所有需要的目录 (避免多线程并发 makedirs 竞争),
        #    并统一使用一个属主, 避免并发时互相看到 root 目录判断错
        own = resolve_owner_hint(out_dir)
        need_dirs = set()
        for f in uniq:
            if f["rel_dir"]:
                need_dirs.add(os.path.join(out_dir, f["rel_dir"]))
        for d_ in sorted(need_dirs):
            try:
                os.makedirs(d_, exist_ok=True)
            except Exception:
                pass
        # 从深到浅逐层修正属主 (用同一个 own)
        for d_ in sorted(need_dirs, key=lambda x: -x.count(os.sep)):
            apply_owner(d_, own)

        # 4) 建任务
        created = []
        failed = []
        for f in uniq:
            try:
                target_dir = os.path.join(out_dir, f["rel_dir"]) \
                    if f["rel_dir"] else out_dir
                t = Task(f["fs_id"], f["name"], f["path"], f["size"],
                         target_dir, conns)
                t.owner = own          # 复用同一个属主, 彻底消除竞态
                with STATE["lock"]:
                    STATE["tasks"][t.id] = t
                t.start()
                created.append({"id": t.id, "name": f["name"],
                                "out": t.out_path})
            except Exception as e:
                failed.append({"name": f["name"], "reason": str(e)[:120]})

        log("批量下载: 选中 %d 项 -> 展开 %d 个文件 (去重 %d), 建任务 %d"
            % (len(items), len(flat), dup, len(created)))

        # 记住本次目录, 供下次弹窗默认值使用
        try:
            cfg2 = STATE["config"]
            cfg2["last_out_dir"] = out_dir
            save_config()
        except Exception:
            pass

        return self._json({
            "ok": True,
            "selected": len(items),
            "expanded": len(flat),
            "deduped": dup,
            "created": len(created),
            "tasks": created[:200],
            "skipped": skipped,
            "failed": failed,
        })

    # ---------- 本地浏览 ----------
    def browse_local(self, d):
        if not d:
            c = STATE["config"]
            d = c.get("download_dir") or "/"
        d = os.path.abspath(d)
        if not os.path.isdir(d):
            d = "/"
        items = []
        try:
            for name in os.listdir(d):
                if name.startswith("."):
                    continue
                full = os.path.join(d, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                isdir = os.path.isdir(full)
                items.append({
                    "name": name,
                    "path": full,
                    "isdir": isdir,
                    "size": st.st_size if not isdir else 0,
                    "size_h": fmt_size(st.st_size) if not isdir else "",
                    "mtime": int(st.st_mtime),
                    "mtime_h": time.strftime("%Y-%m-%d %H:%M",
                                             time.localtime(st.st_mtime)),
                })
        except PermissionError:
            pass
        # 目录在前, 组内按修改时间倒序 (最近改过的在上)
        items.sort(key=lambda x: (not x["isdir"], -x["mtime"]))
        parent = os.path.dirname(d.rstrip("/")) or "/"
        return {"path": d, "parent": parent, "items": items}

    def _public_cfg(self):
        c = STATE["config"]
        return {"download_dir": c.get("download_dir", ""),
                "last_out_dir": c.get("last_out_dir", ""),
                "conns": c.get("conns", DEFAULT_CONNS),
                "total_conns": c.get("total_conns", TOTAL_CONNS_BUDGET),
                "max_active_tasks": c.get("max_active_tasks",
                                          MAX_ACTIVE_TASKS),
                "speed_limit_mb": c.get("speed_limit_mb", 0)}


# ============================================================
#  Unix Socket 服务 (Windows 不支持, 自动降级)
# ============================================================
_HAS_UNIX = hasattr(socketserver, "UnixStreamServer")

if _HAS_UNIX:
    class UnixHTTPServer(socketserver.ThreadingMixIn,
                         socketserver.UnixStreamServer):
        daemon_threads = True
        allow_reuse_address = True

        def __init__(self, sock_path, handler):
            self.sock_path = sock_path
            try:
                os.unlink(sock_path)
            except OSError:
                pass
            socketserver.UnixStreamServer.__init__(self, sock_path, handler)
            try:
                os.chmod(sock_path, 0o777)
            except OSError:
                pass

        def server_bind(self):
            socketserver.UnixStreamServer.server_bind(self)
            self.server_name = "localhost"
            self.server_port = 0
else:
    UnixHTTPServer = None


# ============================================================
#  入口
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--sock", default="")
    ap.add_argument("--data", default="")
    ap.add_argument("--download-dir", default="")
    args = ap.parse_args()

    data_dir = args.data or os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "..", "var")
    os.makedirs(data_dir, exist_ok=True)
    STATE["data_dir"] = data_dir
    os.makedirs(os.path.join(data_dir, "tasks"), exist_ok=True)

    load_config()
    if args.download_dir:
        STATE["config"]["download_dir"] = args.download_dir
        save_config()

    # 初始化全局限速器
    global_limiter().refresh()

    log("%s %s 启动" % (APP_NAME, VERSION))
    log("数据目录: %s" % data_dir)
    log("下载目录: %s" % STATE["config"].get("download_dir", "(未设置)"))
    _sl = STATE["config"].get("speed_limit_mb") or 0
    log("总限速: %s" % (("%.1f MB/s" % float(_sl)) if _sl else "不限速"))

    # 单实例: 检测到旧实例就接管 (让它退出), 保证最终只有一个完整实例
    # 同时持有 Unix socket 与 TCP 端口。若静默退出, appcenter 会报 10330。
    lock_path = os.path.join(data_dir, "server.lock")
    try:
        acquire_single_lock(lock_path, args.port or 18080)
    except Exception as e:
        log("单实例检查异常 (继续启动): %s" % e)

    # 同时监听 Unix socket (fnOS 反代) 与 TCP 端口 (直连兜底)
    servers = []

    if args.sock and UnixHTTPServer is not None:
        try:
            s_sock = UnixHTTPServer(args.sock, Handler)
            servers.append(s_sock)
            log("监听 Unix socket: %s" % args.sock)
        except Exception as e:
            log("Unix socket 启动失败: %s" % e)

    tcp_port = args.port or 18080
    try:
        s_tcp = ThreadingHTTPServer(("0.0.0.0", tcp_port), Handler)
        s_tcp.daemon_threads = True
        servers.append(s_tcp)
        log("监听 TCP: 0.0.0.0:%d" % tcp_port)
    except Exception as e:
        log("TCP %d 启动失败: %s" % (tcp_port, e))

    if not servers:
        log("无可用监听方式, 退出")
        release_single_lock(lock_path)
        return

    # 除第一个外都放后台线程
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for srv in servers:
            try:
                srv.shutdown()
            except Exception:
                pass
        if args.sock:
            try:
                os.unlink(args.sock)
            except OSError:
                pass
        release_single_lock(lock_path)


if __name__ == "__main__":
    main()
