# 百度网盘下载器 for 飞牛 fnOS

面向飞牛 NAS（fnOS）的第三方百度网盘下载工具。带 Web 界面，在浏览器里浏览网盘、
选择文件、批量下载，支持断点续传和多线程加速。

> ⚠️ 非官方项目。仅供个人在自己拥有的百度网盘账号和 NAS 上使用。

---

## 特性

- **Web 界面** — 装在 fnOS 上，从桌面图标打开，手机/电脑都能用
- **扫码或 Cookie 登录** — 支持百度账号扫码，也可粘贴 Cookie
- **网盘浏览** — 目录树浏览，按修改时间/名称排序
- **批量下载** — 支持多选文件和文件夹，文件夹自动递归展开
- **目录选择** — 每次下载前弹窗选保存位置，默认记住上次的目录
- **多线程加速** — 1MB 分片 + 最高 64 并发，实测 35～38 MB/s
- **断点续传** — 中断后继续，不重下已完成的分片
- **任务管理** — 队列调度、批量暂停/继续/删除、实时总速度面板
- **限速** — 可设置总下载限速，避免占满带宽
- **零第三方依赖** — 纯 Python 3 标准库，不需要 pip install

---

## 安装

### 前置条件

- 飞牛 fnOS（`os_min_version` 见 `src/manifest`）
- x86_64 架构
- 建议 4GB 以上内存

### 方式一：直接安装 fpk

1. 从 [Releases](../../releases) 下载最新的 `.fpk` 文件
2. fnOS 应用中心 → 手动安装 → 选择该 fpk
3. 安装完成后从桌面图标打开

### 方式二：自行构建

```bash
git clone <this-repo>.git
cd <repo>
python3 build.py            # 输出到 dist/
```

生成的 fpk 可直接在应用中心安装。

---

## 使用

### 1. 登录

打开应用后有两种登录方式：

**扫码登录**（推荐）：用百度网盘 App 扫二维码，在手机上确认。

**Cookie 登录**：如果你已经在电脑上登录了百度网盘，可以直接提取 Cookie 粘贴进来。
仓库里的 `tools/extract_cookie.py` 可以在 Windows 上从客户端内存读取
`BDUSS` / `STOKEN`。

### 2. 浏览和下载

- 点文件夹进入下一级
- 单个文件点右侧「下载」
- 或勾选多个项目后点「下载选中」
- **每次都会弹窗确认保存目录**，默认是上次用过的目录

### 3. 文件保存位置

| 下载方式 | 保存到 |
|---|---|
| 单个文件 | `目标目录/文件名` |
| 单个文件夹 | `目标目录/文件夹名/文件` |
| 批量（纯文件） | `目标目录/文件`（平铺） |
| 批量（含文件夹） | 每层文件夹各建同名目录 |

不会重建网盘里的完整路径，只在必要时建一层同名文件夹。

---

## 配置项

设置页可调：

| 项目 | 默认 | 说明 |
|---|---|---|
| 默认下载目录 | `/vol1/1001/Backup` | 首次弹窗的起始位置 |
| 单任务并发 | 64 | 单个任务的最大连接数 |
| 总连接预算 | 64 | 所有任务合计的连接上限 |
| 同时下载任务数 | 1 | 同时运行的任务数，最高 3 |
| 总下载限速 | 0（不限） | MB/s，所有任务合计上限 |

### 关于并发

实测（同一账号，空载）：

| 连接数 | 速度 |
|---|---|
| 16 | 9.2 MB/s |
| 32 | 17.7 MB/s |
| **64** | **22.8 MB/s** ← 最优 |
| 96 | 19.7 MB/s |

百度对**账号总带宽**有限制，超过 64 连接后反而变慢。所以默认总预算是 64；
多个任务同时跑时按任务数均分。

---

## 项目结构

```
.
├── src/                     # fpk 源码树
│   ├── manifest             # 应用元数据
│   ├── app/
│   │   ├── server.py        # 后端（HTTP 服务 + 下载引擎）
│   │   ├── web/             # 前端（原生 JS，无框架）
│   │   └── ui/              # fnOS 图标入口（CGI）
│   ├── cmd/                 # fnOS 应用生命周期脚本
│   ├── config/              # 权限与资源声明
│   └── wizard/              # 卸载向导
├── build.py                 # 打包脚本
├── tools/
│   └── extract_cookie.py    # Windows 端 Cookie 提取工具
└── docs/
    └── 使用说明.md
```

### 架构说明

```
浏览器 ──HTTP──> fnOS nginx ──> index.cgi ──> server.py (127.0.0.1:18080)
                                                    │
                                                    ├── 百度网盘 API
                                                    └── 本地磁盘（分片并发写入）
```

- `server.py` 同时监听 Unix socket（给 fnOS 反代用）和 TCP 18080（直连兜底）
- `ui/index.cgi` 是一个 CGI 反向代理，把 fnOS 的 `/cgi/ThirdParty/...` 请求
  转发到后端
- 下载引擎用 1MB 分片 + N 线程并发，每片一个 HTTP Range 请求

### API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ping` | 健康检查 |
| GET | `/api/status` | 登录状态 |
| POST | `/api/login/cookie` | Cookie 登录 |
| GET | `/api/list?dir=&order=&desc=` | 列网盘目录 |
| GET | `/api/browse?dir=` | 列本地目录 |
| POST | `/api/download` | 单文件下载 |
| POST | `/api/download/batch` | 批量下载（递归展开文件夹） |
| GET | `/api/tasks` | 任务列表 |
| POST | `/api/tasks/batch` | 批量暂停/继续/删除 |
| GET/POST | `/api/config` | 读写设置 |

---

## 常见问题

**应用中心点「启用」报「本地应用启动失败」（10330）？**

v1.7.0 已修复。原因是旧版单实例锁发现存活进程就静默退出，appcenter 把
"进程启动后立刻消失"判定为失败。现在改为**接管模式**：终止旧实例后自己完整
启动，appcenter 看到的是一次成功的启动。

**接口时好时坏、任务列表对不上？**

多个进程同时运行导致。检查：

```bash
pgrep -af 'server.py'      # 应该只有 1 个
```

有多个时清理一次：

```bash
pkill -9 -f 'nasdl/server.py'
rm -f /var/run/baidu.nasdl.sock /vol1/@appdata/baidu.nasdl/server.lock
appcenter-cli start baidu.nasdl
```

**下载速度很慢，只有几百 KB/s？**

- 检查总连接预算是否被调太低（建议 64）
- 百度会对同一账号限速，多个任务同时跑会分摊带宽
- 试试把「同时下载任务数」设为 1，让单个任务占满带宽

**下载时手机播放 NAS 视频卡顿？**

下载写盘会占用磁盘 I/O。可以在设置里给「总下载限速」设个值（比如 15 MB/s），
给读操作留出带宽。

**Cookie 登录失败？**

Cookie 里必须包含 `BDUSS`，建议连 `STOKEN` 一起带上。Cookie 有时效性，
过期后重新提取。

---

## 技术细节

### 为什么用 1MB 分片

百度 CDN 对 HTTP Range 大小有风控：

| Range 大小 | 结果 |
|---|---|
| ≤ 1 MB | 200 OK |
| ≥ 10 MB | 403 Forbidden |

所以固定 1MB 分片，靠并发数提升总速率。

### 扫码登录的三个坑

百度的扫码登录流程有三个容易踩的地方，本项目都已踩过并修正：

**1. 参数必须是 `tpl=mn`，不是 `netdisk`**

```
GET https://passport.baidu.com/v2/api/getqrcode
    ?lp=pc&qrloginfrom=pc&tpl=mn&apiver=v3
    &callback=tangram_guid_<ts>
    &gid=00000000-0000-0000-0000-000000000000&tt=<ts>&_=<ts>
```

`callback` 是 JSONP 包装名，百度据此判断请求来自网页端。用 `tpl=netdisk`
或不带 `callback` 时能拿到二维码，但**后续轮询永远收不到确认票据**。

**2. 确认后的状态是 `status=0` + `v`，不是 `status=2`**

轮询 `channel/unicast` 的返回：

| 阶段 | channel_v 内容 |
|---|---|
| 未扫码 | `{"errno":1}` |
| 已扫码待确认 | `{"status":1}` |
| **用户已确认** | **`{"status":0,"v":"<32位票据>"}`** |

所以判断依据应该是「**响应里有没有 `v`**」，而不是 status 等于几。

**3. `v` 票据必须用 `qrbdusslogin` 兑换，且只有约 2 秒有效期**

```
GET https://passport.baidu.com/v3/login/main/qrbdusslogin
    ?alg=v3&apiver=v3&bduss=<v票据>
    &loginVersion=v4&qrcode=1&tpl=mn
    &u=https%3A%2F%2Fwww.baidu.com%2F
    &tt=<ts>&time=<ts>&v=<ts>&elapsed=1234
    &callback=bd__cbs__odmford
```

成功时 `Set-Cookie` 会带 `BDUSS` + `STOKEN`。注意 `v` 票据放在 **`bduss` 参数**里
（名字容易误解，它不是 BDUSS 本身）。

另外，申请二维码 / 轮询 / 兑换这三步必须**共用同一个 cookie 会话**，
否则百度不认为是同一次登录流程。

### 取链接时的 UA 必须一致

`filemetas` 接口取 dlink 时用什么 UA，下载时就必须用同一个 UA，否则全部 403。
本项目统一用 `netdisk;P2SP;...` 客户端 UA。

### 文件属主继承

下载器以 root 运行，但创建的文件会**继承目标目录的属主**，不会留一堆
`root:root` 的文件让普通用户改不了。

---

## 许可

MIT
