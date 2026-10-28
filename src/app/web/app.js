/* 百度网盘下载器 —— 前端逻辑 */
(function () {
  "use strict";

  // API 基址: 兼容直连(/) 与 fnOS CGI 通道(/cgi/ThirdParty/<app>/index.cgi/)
  var API = (window.__BASE__ || "/").replace(/\/$/, "");
  var cloudPath = "/";
  var localPath = "";
  var pollTimer = null;
  var qrTimer = null;
  var qrId = null;

  /* ---------------- 工具 ---------------- */
  function $(id) { return document.getElementById(id); }

  function toast(msg, ms) {
    var t = $("toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(t._t);
    t._t = setTimeout(function () { t.classList.remove("show"); }, ms || 2200);
  }

  function api(path, opts) {
    opts = opts || {};
    var url = API + path;
    return fetch(url, {
      method: opts.method || "GET",
      headers: { "Content-Type": "application/json" },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      credentials: "same-origin"
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) {
          var e = new Error(j.errmsg || ("HTTP " + r.status));
          e.data = j;
          throw e;
        }
        return j;
      });
    });
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function fileIcon(item) {
    if (item.isdir) return "📁";
    var n = (item.name || "").toLowerCase();
    if (/\.(mkv|mp4|avi|mov|wmv|flv|ts|m2ts)$/.test(n)) return "🎬";
    if (/\.(mp3|flac|wav|aac|m4a|ape)$/.test(n)) return "🎵";
    if (/\.(jpg|jpeg|png|gif|bmp|webp)$/.test(n)) return "🖼";
    if (/\.(zip|rar|7z|tar|gz)$/.test(n)) return "📦";
    if (/\.(pdf|doc|docx|txt|xls|xlsx|ppt|pptx)$/.test(n)) return "📄";
    return "📄";
  }

  /* ---------------- 视图切换 ---------------- */
  function switchView(name) {
    ["cloud", "tasks", "local", "settings"].forEach(function (v) {
      var el = $("view" + v.charAt(0).toUpperCase() + v.slice(1));
      if (el) el.style.display = (v === name) ? "" : "none";
    });
    document.querySelectorAll(".main-tabs .tab").forEach(function (t) {
      t.classList.toggle("active", t.dataset.view === name);
    });
    if (name === "tasks") loadTasks();
    if (name === "local") loadLocal(localPath);
    if (name === "settings") loadSettings();
  }

  /* ---------------- 登录 ---------------- */
  function refreshStatus() {
    return api("/api/status").then(function (s) {
      if (s.logged_in) {
        $("loginView").style.display = "none";
        $("mainView").style.display = "";
        $("btnLogout").style.display = "";
        $("userInfo").textContent = s.name || s.uk || "已登录";
        $("verBadge").textContent = "v" + s.version;
        if (!cloudPath) cloudPath = "/";
        loadCloud(cloudPath);
      } else {
        $("loginView").style.display = "";
        $("mainView").style.display = "none";
        $("btnLogout").style.display = "none";
        $("userInfo").textContent = "未登录";
        startQr();
      }
    }).catch(function (e) {
      toast("连接服务失败: " + e.message);
    });
  }

  function startQr() {
    stopQr();
    qrId = null;
    $("qrMask").textContent = "加载中…";
    $("qrMask").classList.remove("hide");
    $("qrMsg").textContent = "请使用百度 App 扫码登录";
    $("btnQrRefresh").textContent = "刷新二维码";
    api("/api/qr/new").then(function (r) {
      qrId = r.qr_id;
      $("qrImg").src = r.image;
      $("qrMask").classList.add("hide");
      pollQr();
    }).catch(function (e) {
      $("qrMask").textContent = "二维码加载失败";
      toast("二维码获取失败: " + e.message);
      setTimeout(startQr, 3000);
    });
  }

  function pollQr() {
    stopQr();
    if (!qrId) return;
    qrTimer = setTimeout(function () {
      api("/api/qr/poll?qr_id=" + encodeURIComponent(qrId)).then(function (r) {
        if (r.status === 0) {
          $("qrMsg").textContent = r.msg || "等待扫码";
          pollQr();
        } else if (r.status === 1) {
          $("qrMsg").textContent = "已扫码，请在手机上确认";
          $("qrMask").classList.add("hide");
          pollQr();
        } else if (r.status === 2) {
          // 登录成功
          $("qrMsg").textContent = "登录成功，正在进入…";
          stopQr();
          toast("登录成功");
          setTimeout(refreshStatus, 400);
        } else {
          // -1 过期 / -2 失败
          stopQr();
          $("qrMask").textContent = "二维码已失效";
          $("qrMask").classList.remove("hide");
          $("qrMsg").textContent = r.msg || "二维码已失效，请刷新";
          $("btnQrRefresh").textContent = "重新获取二维码";
        }
      }).catch(function (e) {
        // 网络抖动, 继续轮询
        pollQr();
      });
    }, 1500);
  }

  function stopQr() {
    if (qrTimer) { clearTimeout(qrTimer); qrTimer = null; }
  }

  /* ---------------- 网盘文件 ---------------- */
  var sortState = { order: "time", desc: true };
  var selected = {};          // fs_id -> item

  function selectedCount() {
    return Object.keys(selected).length;
  }

  function updateBatchBar() {
    var n = selectedCount();
    $("batchBar").style.display = n > 0 ? "" : "none";
    $("selInfo").textContent = "已选 " + n + " 项";
    var all = $("chkAllHead");
    if (all) all.checked = false;
  }

  function loadCloud(dir) {
    $("cloudPath").textContent = dir;
    selected = {};
    updateBatchBar();
    var tb = $("cloudBody");
    tb.innerHTML = '<tr><td colspan="5" class="empty">加载中…</td></tr>';

    var url = "/api/list?dir=" + encodeURIComponent(dir)
      + "&order=" + sortState.order
      + "&desc=" + (sortState.desc ? "1" : "0");

    api(url).then(function (r) {
      var list = r.list || [];
      if (!list.length) {
        tb.innerHTML = '<tr><td colspan="5" class="empty">空目录</td></tr>';
        return;
      }
      var h = "";
      list.forEach(function (it, i) {
        h += '<tr data-fsid="' + esc(it.fs_id) + '">';
        h += '<td class="col-chk"><input type="checkbox" class="row-chk" data-idx="' + i + '"></td>';
        h += '<td><div class="name-cell" data-idx="' + i + '">'
          + '<span class="ico">' + fileIcon(it) + '</span>'
          + '<span>' + esc(it.name) + '</span>'
          + (it.isdir ? '<span class="dir-tag">文件夹</span>' : '')
          + '</div></td>';
        h += '<td class="col-size">' + (it.isdir ? "-" : esc(it.size_h)) + '</td>';
        h += '<td class="col-time">' + esc(it.mtime_h) + '</td>';
        h += '<td class="col-act">'
          + '<button class="btn primary sm dl-btn" data-idx="' + i + '">下载</button>'
          + '</td>';
        h += '</tr>';
      });
      tb.innerHTML = h;

      // 点击名称: 文件夹进入, 文件不响应
      tb.querySelectorAll(".name-cell").forEach(function (el) {
        el.onclick = function () {
          var it = list[+el.dataset.idx];
          if (it.isdir) {
            cloudPath = it.path;
            loadCloud(it.path);
          }
        };
      });

      // 单行下载: 先弹窗选目录, 确认后再下载
      tb.querySelectorAll(".dl-btn").forEach(function (el) {
        el.onclick = function () {
          var it = list[+el.dataset.idx];
          var kind = it.isdir ? "文件夹" : "文件";
          askDownloadDir("下载 " + kind + ": " + it.name, function (dir) {
            el.disabled = true;
            el.textContent = "…";
            if (it.isdir) {
              // 文件夹: 走批量接口 (会自动展开)
              var body = {
                items: [{ fs_id: it.fs_id, name: it.name,
                          path: it.path, isdir: true }],
                out_dir: dir
              };
              api("/api/download/batch", { method: "POST", body: body })
                .then(function (res) {
                  toast("已加入 " + res.created + " 个文件 (" + it.name + ")", 3000);
                  $("taskDot").classList.add("show");
                  el.textContent = "已加入";
                  setTimeout(function () {
                    el.disabled = false; el.textContent = "下载";
                  }, 2500);
                }).catch(function (e) {
                  toast("失败: " + e.message, 3000);
                  el.disabled = false; el.textContent = "下载";
                });
            } else {
              api("/api/download", {
                method: "POST",
                body: { fs_id: it.fs_id, name: it.name, path: it.path,
                        size: it.size, out_dir: dir }
              })
                .then(function () {
                  toast("已加入下载: " + it.name);
                  el.textContent = "已加入";
                  $("taskDot").classList.add("show");
                  setTimeout(function () {
                    el.disabled = false; el.textContent = "下载";
                  }, 2000);
                }).catch(function (e) {
                  toast("失败: " + e.message, 3000);
                  el.disabled = false; el.textContent = "下载";
                });
            }
          });
        };
      });

      // 勾选
      tb.querySelectorAll(".row-chk").forEach(function (cb) {
        cb.onchange = function () {
          var it = list[+cb.dataset.idx];
          var tr = cb.closest("tr");
          if (cb.checked) {
            selected[it.fs_id] = it;
            tr.classList.add("selected");
          } else {
            delete selected[it.fs_id];
            tr.classList.remove("selected");
          }
          updateBatchBar();
        };
      });

    }).catch(function (e) {
      tb.innerHTML = '<tr><td colspan="5" class="empty">加载失败: '
        + esc(e.message) + '</td></tr>';
    });
  }

  /* ---------------- 下载任务 ---------------- */
  var lastTasks = [];

  function loadTasks() {
    api("/api/tasks").then(function (r) {
      lastTasks = r.tasks || [];
      renderTasks();
      renderSpeedPanel();
      var running = lastTasks.filter(function (t) {
        return t.state === "running";
      }).length;
      $("taskDot").classList.toggle("show", running > 0);
    }).catch(function (e) {
      toast("任务加载失败: " + e.message);
    });
  }

  /* ---------------- 总速度面板 ---------------- */
  var speedHistory = [];       // 最近 30 次采样

  function fmtBytes(n) {
    n = n || 0;
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? Math.round(n) : n.toFixed(2)) + " " + u[i];
  }

  /** 把秒数格式化成「1 小时 30 分」这类文本 */
  function fmtDuration(sec) {
    var s = Math.max(0, Math.floor(sec || 0));
    if (s < 60) return s + " 秒";
    if (s < 3600) {
      var m = Math.floor(s / 60), r = s % 60;
      return m < 10 ? (m + " 分 " + r + " 秒") : (m + " 分");
    }
    if (s < 86400) {
      var h = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60);
      return h < 10 ? (h + " 小时 " + mm + " 分") : (h + " 小时");
    }
    var d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600);
    return d < 10 ? (d + " 天 " + hh + " 小时") : (d + " 天");
  }

  function renderSpeedPanel() {
    var run = [], queued = 0, done = 0, paused = 0, errs = 0;
    var totalSpeed = 0;
    var totalBytes = 0;
    var runRemain = 0;      // 运行中任务的剩余字节合计

    lastTasks.forEach(function (t) {
      var sp = t.speed || 0;
      if (t.state === "running") { run.push(t); totalSpeed += sp; }
      else if (t.state === "queued" || t.state === "pending") queued++;
      else if (t.state === "done") done++;
      else if (t.state === "paused") paused++;
      else if (t.state === "error") errs++;

      // 已下载字节 = 完成片数 × 分片大小
      if (t.state === "done") {
        totalBytes += t.size || 0;
      } else if (t.done_chunks && t.nchunks && t.size) {
        var got = Math.round(t.size * t.done_chunks / t.nchunks);
        totalBytes += got;
        if (t.state === "running") runRemain += Math.max(0, t.size - got);
      }
    });

    // 总速度数字
    var el = $("totalSpeed");
    if (el) {
      el.textContent = totalSpeed.toFixed(2);
      el.classList.toggle("idle", totalSpeed < 0.01);
    }

    // 统计项
    if ($("speedRunning")) {
      $("speedRunning").innerHTML = "运行中 <b>" + run.length + "</b>";
      $("speedQueued").innerHTML = "排队 <b>" + queued + "</b>";
      $("speedDone").innerHTML = "已完成 <b>" + done + "</b>";
      var pEl = $("speedPaused");
      if (errs > 0) {
        pEl.innerHTML = "已暂停 <b>" + paused + "</b> · 出错 <b>" + errs + "</b>";
      } else {
        pEl.innerHTML = "已暂停 <b>" + paused + "</b>";
      }
      $("speedTotalBytes").innerHTML =
        "已下载 <b>" + fmtBytes(totalBytes) + "</b>";

      // 总剩余时间 (仅当有任务在跑且速度已知)
      var etaEl = $("speedEta");
      if (etaEl) {
        if (run.length && totalSpeed > 0.01 && runRemain > 0) {
          var etaSec = Math.round(runRemain / (totalSpeed * 1048576));
          etaEl.innerHTML = "剩余 <b>" + fmtDuration(etaSec) + "</b>";
        } else if (run.length) {
          etaEl.innerHTML = "剩余 <b>计算中…</b>";
        } else {
          etaEl.innerHTML = "";
        }
      }
    }

    // 速度曲线
    speedHistory.push(totalSpeed);
    if (speedHistory.length > 30) speedHistory.shift();
    var spark = $("speedSpark");
    if (spark) {
      var max = Math.max.apply(null, speedHistory.concat([1]));
      var html = "";
      for (var i = 0; i < 30; i++) {
        var v = i < speedHistory.length
          ? speedHistory[i]
          : 0;
        var hgt = Math.max(2, Math.round(v / max * 30));
        html += '<i style="height:' + hgt + 'px"></i>';
      }
      spark.innerHTML = html;
    }

    // 摘要
    var sum = "共 " + lastTasks.length + " 个任务";
    if (run.length) sum += "，运行中 " + run.length;
    if (queued) sum += "，排队 " + queued;
    if ($("taskSummary")) $("taskSummary").textContent = lastTasks.length ? sum : "";
  }

  function renderTasks() {
    var box = $("taskList");
    if (!lastTasks.length) {
      box.innerHTML = '<div class="empty">暂无任务</div>';
      taskSel = {};
      updateTaskSelBar();
      return;
    }
    var h = "";
    lastTasks.forEach(function (t) {
      var cls = t.state;
      var label = {
        pending: "等待中", queued: "排队中", running: "下载中",
        paused: "已暂停", done: "已完成", error: "出错"
      }[t.state] || t.state;
      var barCls = t.state === "done" ? "done" : (t.state === "error" ? "error" : "");
      var checked = taskSel[t.id] ? " checked" : "";
      var selCls = taskSel[t.id] ? " task-item-sel" : "";
      h += '<div class="task-item' + selCls + '">';
      h += '<div class="task-head">';
      h += '<input type="checkbox" class="task-chk" data-id="' + t.id + '"' + checked + '>';
      h += '<div class="task-name">' + esc(t.name) + '</div>';
      h += '<span class="task-tag ' + cls + '">' + label + '</span>';
      h += '</div>';
      h += '<div class="bar"><div class="bar-inner ' + barCls
        + '" style="width:' + Math.min(100, t.progress) + '%"></div></div>';
      h += '<div class="task-meta">';
      h += '<span>' + t.progress + '%</span>';
      h += '<span>' + esc(t.bytes_done_h) + ' / ' + esc(t.size_h) + '</span>';
      if (t.state === "running") {
        h += '<span>' + esc(t.speed_h) + '</span>';
        // 剩余时间
        if (t.eta_h) {
          h += '<span class="eta">剩余 ' + esc(t.eta_h) + '</span>';
        }
      }
      h += '<span>' + t.done_chunks + '/' + t.nchunks + ' 片</span>';
      if (t.elapsed) h += '<span>已用 ' + fmtDuration(t.elapsed) + '</span>';
      if (t.msg) h += '<span>' + esc(t.msg) + '</span>';
      h += '</div>';
      h += '<div class="task-acts">';
      if (t.state === "running" || t.state === "queued") {
        h += '<button class="btn ghost sm" data-act="pause" data-id="' + t.id + '">暂停</button>';
      } else if (t.state === "paused" || t.state === "error") {
        h += '<button class="btn primary sm" data-act="resume" data-id="' + t.id + '">继续</button>';
      }
      h += '<button class="btn ghost sm" data-act="delete" data-id="' + t.id + '">删除</button>';
      h += '</div>';
      h += '</div>';
    });
    box.innerHTML = h;

    // 单条勾选
    box.querySelectorAll(".task-chk").forEach(function (cb) {
      cb.onchange = function () {
        if (cb.checked) taskSel[cb.dataset.id] = true;
        else delete taskSel[cb.dataset.id];
        var item = cb.closest(".task-item");
        if (item) item.classList.toggle("task-item-sel", cb.checked);
        updateTaskSelBar();
      };
    });

    box.querySelectorAll("[data-act]").forEach(function (el) {
      el.onclick = function () {
        var act = el.dataset.act, id = el.dataset.id;
        var p;
        if (act === "pause") p = api("/api/task/pause", { method: "POST", body: { id: id } });
        else if (act === "resume") p = api("/api/task/resume", { method: "POST", body: { id: id } });
        else {
          if (!confirm("删除任务？\n（不会删除已下载文件）")) return;
          p = api("/api/task/delete", { method: "POST", body: { id: id } });
        }
        p.then(function () { loadTasks(); })
         .catch(function (e) { toast("失败: " + e.message); });
      };
    });

    updateTaskSelBar();
  }

  /* ---------------- 任务批量操作 ---------------- */
  var taskSel = {};

  function taskSelIds() {
    return Object.keys(taskSel);
  }

  function updateTaskSelBar() {
    var n = taskSelIds().length;
    $("taskSelInfo").textContent = "已选 " + n + " 项";
    $("btnTaskBatchPause").disabled = n === 0;
    $("btnTaskBatchResume").disabled = n === 0;
    $("btnTaskBatchDelete").disabled = n === 0;
    var all = $("taskChkAll");
    if (all) all.checked = false;
  }

  function batchTaskAction(action, ids, removeFile) {
    var body = { action: action };
    if (ids && ids.length) body.ids = ids;
    if (removeFile) body.remove_file = true;
    return api("/api/tasks/batch", { method: "POST", body: body })
      .then(function (r) {
        var msg = {
          pause: "已暂停", resume: "已开始", delete: "已删除"
        }[action] + " " + r.affected + " 个任务";
        toast(msg, 3000);
        if (action === "delete") taskSel = {};
        loadTasks();
      }).catch(function (e) {
        toast("操作失败: " + e.message, 4000);
      });
  }

  /* ---------------- 本地目录 ---------------- */
  function loadLocal(dir) {
    api("/api/browse?dir=" + encodeURIComponent(dir || "")).then(function (r) {
      localPath = r.path;
      $("localPath").textContent = r.path;
      var items = r.items || [];
      var tb = $("localBody");
      if (!items.length) {
        tb.innerHTML = '<tr><td colspan="3" class="empty">空目录</td></tr>';
        return;
      }
      var h = "";
      items.forEach(function (it, i) {
        h += '<tr>';
        h += '<td><div class="name-cell" data-idx="' + i + '">'
          + '<span class="ico">' + fileIcon(it) + '</span>'
          + '<span>' + esc(it.name) + '</span></div></td>';
        h += '<td class="col-size">' + (it.isdir ? "-" : esc(it.size_h)) + '</td>';
        h += '<td class="col-act"></td>';
        h += '</tr>';
      });
      tb.innerHTML = h;
      tb.querySelectorAll(".name-cell").forEach(function (el) {
        el.onclick = function () {
          var it = items[+el.dataset.idx];
          if (it.isdir) loadLocal(it.path);
        };
      });
    }).catch(function (e) {
      toast("本地目录加载失败: " + e.message);
    });
  }

  /* ---------------- 设置 ---------------- */
  function loadSettings() {
    api("/api/config").then(function (c) {
      $("setDir").value = c.download_dir || "";
      $("setConns").value = c.conns || 64;
      $("setTotalConns").value = c.total_conns || 64;
      $("setMaxActive").value = c.max_active_tasks || 1;
      $("setSpeedLimit").value = (c.speed_limit_mb === undefined)
        ? 0 : c.speed_limit_mb;
    });
  }

  /* ---------------- 下载目标目录 ---------------- */
  var defaultDir = "";        // 设置里的默认下载目录
  var lastOutDir = "";        // 上次下载实际使用的目录

  // 本次下载要用的目录: 上次用过的 > 设置里的默认
  function currentTarget() {
    return lastOutDir || defaultDir;
  }

  function refreshDefaultDir() {
    return api("/api/config").then(function (c) {
      defaultDir = c.download_dir || "";
      lastOutDir = c.last_out_dir || "";
    }).catch(function () {});
  }

  // 记住本次下载目录, 作为下次弹窗默认值
  function rememberOutDir(dir) {
    lastOutDir = dir;
    api("/api/out_dir", { method: "POST", body: { dir: dir } })
      .catch(function () {});
  }

  /**
   * 每次下载前弹出目录选择, 让用户确认保存到哪
   * cb(dir) 在用户确认后调用; 取消则不执行
   */
  function askDownloadDir(title, onOk) {
    var start = currentTarget() || "/";
    openDirPicker(start, function (dir) {
      rememberOutDir(dir);
      onOk(dir);
    }, title || "选择下载目录");
  }

  /* ---- 目录选择弹窗 ---- */
  var dpState = { cur: "/", sel: "", onOk: null };

  function openDirPicker(startDir, onOk, title) {
    dpState.onOk = onOk;
    dpState.sel = startDir || defaultDir || "/";
    $("dirPicker").style.display = "flex";
    $("dirPickerTitle").textContent = title || "选择下载目录";
    $("dpManual").value = dpState.sel;
    dpLoad(startDir || dpState.sel);
  }

  function closeDirPicker() {
    $("dirPicker").style.display = "none";
    dpState.onOk = null;
  }

  function dpLoad(dir) {
    var d = dir || "/";
    $("dpPath").textContent = d;
    $("dpManual").value = d;
    dpState.cur = d;
    dpState.sel = d;
    var box = $("dpList");
    box.innerHTML = '<div class="empty">加载中…</div>';

    api("/api/browse?dir=" + encodeURIComponent(d)).then(function (r) {
      // 后端已按修改时间倒序排好, 这里只取文件夹
      var items = (r.items || []).filter(function (x) { return x.isdir; });
      var h = "";
      // 上级
      if (r.parent && r.parent !== r.path) {
        h += '<div class="dp-item" data-go="' + esc(r.parent) + '">'
          + '<span class="ico">↩</span><span class="dp-name">.. 上级目录</span></div>';
      }
      items.forEach(function (it) {
        h += '<div class="dp-item" data-go="' + esc(it.path) + '">'
          + '<span class="ico">📁</span>'
          + '<span class="dp-name">' + esc(it.name) + '</span>'
          + '<span class="dp-time">' + esc(it.mtime_h || "") + '</span>'
          + '<span class="dp-go">进入 ›</span></div>';
      });
      if (!h) h = '<div class="empty">此目录下没有子文件夹</div>';
      box.innerHTML = h;

      box.querySelectorAll(".dp-item").forEach(function (el) {
        // 单击选中当前目录, 双击进入子目录
        el.onclick = function () {
          var go = el.dataset.go;
          box.querySelectorAll(".dp-item").forEach(function (x) {
            x.classList.remove("selected");
          });
          el.classList.add("selected");
          // 若是"上级", 直接把选中设为上级? 保持当前目录更直观
          dpState.sel = dpState.cur;
          $("dpManual").value = dpState.cur;
        };
        el.ondblclick = function () {
          dpLoad(el.dataset.go);
        };
      });
    }).catch(function (e) {
      box.innerHTML = '<div class="empty">加载失败: ' + esc(e.message) + '</div>';
    });
  }

  /* ---------------- 事件绑定 ---------------- */
  function bind() {
    // 登录 tab
    document.querySelectorAll(".login-card .tab").forEach(function (tab) {
      tab.onclick = function () {
        document.querySelectorAll(".login-card .tab").forEach(function (t) {
          t.classList.remove("active");
        });
        tab.classList.add("active");
        var isQr = tab.dataset.tab === "qr";
        $("tabQr").style.display = isQr ? "" : "none";
        $("tabCookie").style.display = isQr ? "none" : "";
        if (isQr) startQr(); else stopQr();
      };
    });

    $("btnQrRefresh").onclick = startQr;

    $("btnCookieLogin").onclick = function () {
      var v = $("cookieInput").value.trim();
      if (!v) { toast("请填写 cookie"); return; }
      $("cookieMsg").textContent = "登录中…";
      api("/api/login/cookie", { method: "POST", body: { cookie: v } })
        .then(function (r) {
          $("cookieMsg").textContent = "";
          toast("登录成功");
          refreshStatus();
        }).catch(function (e) {
          $("cookieMsg").textContent = "登录失败: " + e.message;
        });
    };

    $("btnLogout").onclick = function () {
      if (!confirm("确定退出登录？")) return;
      api("/api/logout", { method: "POST" }).then(function () {
        location.reload();
      });
    };

    // 主视图 tab
    document.querySelectorAll(".main-tabs .tab").forEach(function (tab) {
      tab.onclick = function () { switchView(tab.dataset.view); };
    });

    $("btnUp").onclick = function () {
      if (cloudPath === "/" || !cloudPath) return;
      var p = cloudPath.replace(/\/+$/, "");
      var idx = p.lastIndexOf("/");
      var parent = idx <= 0 ? "/" : p.slice(0, idx);
      cloudPath = parent;
      loadCloud(parent);
    };
    $("btnRefreshCloud").onclick = function () { loadCloud(cloudPath); };

    // 排序切换
    document.querySelectorAll(".sort-btn").forEach(function (btn) {
      btn.onclick = function () {
        document.querySelectorAll(".sort-btn").forEach(function (b) {
          b.classList.remove("active");
        });
        btn.classList.add("active");
        sortState.order = btn.dataset.order;
        sortState.desc = btn.dataset.desc === "1";
        loadCloud(cloudPath);
      };
    });

    // 全选 (表头)
    function toggleAll(checked) {
      var tb = $("cloudBody");
      tb.querySelectorAll(".row-chk").forEach(function (cb) {
        if (cb.checked !== checked) {
          cb.checked = checked;
          cb.onchange();
        }
      });
    }
    $("chkAllHead").onchange = function () { toggleAll(this.checked); };
    $("chkAll").onchange = function () { toggleAll(this.checked); };

    // 清空选择
    $("btnClearSel").onclick = function () {
      toggleAll(false);
      selected = {};
      updateBatchBar();
    };

    // 批量下载
    $("btnBatchDownload").onclick = function () {
      var items = Object.keys(selected).map(function (k) {
        var it = selected[k];
        return {
          fs_id: it.fs_id, name: it.name, path: it.path,
          size: it.size, isdir: it.isdir
        };
      });
      if (!items.length) { toast("请先勾选文件"); return; }

      var dirs = items.filter(function (x) { return x.isdir; }).length;
      var files = items.length - dirs;
      var desc = "下载 " + files + " 个文件";
      if (dirs) desc += " 和 " + dirs + " 个文件夹";

      // 先弹窗选目录, 确认后再提交
      askDownloadDir(desc + " — 选择保存目录", function (dir) {
        var btn = $("btnBatchDownload");
        btn.disabled = true;
        btn.textContent = "处理中…";

        api("/api/download/batch", {
          method: "POST",
          body: { items: items, out_dir: dir }
        })
          .then(function (r) {
            var t = "已创建 " + r.created + " 个任务";
            if (r.expanded > r.selected) t += "（展开 " + r.expanded + " 个文件）";
            if (r.skipped && r.skipped.length) t += "，跳过 " + r.skipped.length;
            toast(t, 4000);
            $("taskDot").classList.add("show");
            toggleAll(false);
            selected = {};
            updateBatchBar();
            switchView("tasks");
          }).catch(function (e) {
            toast("批量下载失败: " + e.message, 4000);
          }).then(function () {
            btn.disabled = false;
            btn.textContent = "下载选中";
          });
      });
    };

    // ---- 弹窗内部 ----
    $("dirPickerClose").onclick = closeDirPicker;
    $("dirPickerCancel").onclick = closeDirPicker;
    $("dpUp").onclick = function () {
      var p = dpState.cur.replace(/\/+$/, "");
      var i = p.lastIndexOf("/");
      dpLoad(i <= 0 ? "/" : p.slice(0, i));
    };
    $("dpRefresh").onclick = function () { dpLoad(dpState.cur); };
    $("dpManual").oninput = function () {
      dpState.sel = $("dpManual").value.trim();
    };
    $("dirPickerOk").onclick = function () {
      var dir = ($("dpManual").value || "").trim() || dpState.sel || dpState.cur;
      if (!dir) { toast("请选择或输入目录"); return; }
      var cb = dpState.onOk;
      closeDirPicker();
      if (cb) cb(dir);
    };
    // 点遮罩关闭
    $("dirPicker").onclick = function (e) {
      if (e.target === $("dirPicker")) closeDirPicker();
    };

    // 设置页的"浏览…"
    $("btnPickDefaultDir").onclick = function () {
      openDirPicker($("setDir").value || defaultDir, function (dir) {
        $("setDir").value = dir;
      });
    };

    $("btnUpLocal").onclick = function () {
      if (!localPath || localPath === "/") return;
      var idx = localPath.replace(/\/+$/, "").lastIndexOf("/");
      loadLocal(idx <= 0 ? "/" : localPath.slice(0, idx));
    };
    $("btnRefreshLocal").onclick = function () { loadLocal(localPath); };

    $("btnRefreshTasks").onclick = loadTasks;

    // ---- 任务批量操作 ----
    $("btnTaskPauseAll").onclick = function () {
      batchTaskAction("pause", null);
    };
    $("btnTaskResumeAll").onclick = function () {
      batchTaskAction("resume", null);
    };
    $("btnTaskBatchPause").onclick = function () {
      var ids = taskSelIds();
      if (!ids.length) return;
      batchTaskAction("pause", ids);
    };
    $("btnTaskBatchResume").onclick = function () {
      var ids = taskSelIds();
      if (!ids.length) return;
      batchTaskAction("resume", ids);
    };
    $("btnTaskBatchDelete").onclick = function () {
      var ids = taskSelIds();
      if (!ids.length) return;
      if (!confirm("删除选中的 " + ids.length + " 个任务？\n（不会删除已下载文件）")) return;
      batchTaskAction("delete", ids);
    };
    $("btnTaskDeleteDone").onclick = function () {
      var doneIds = lastTasks.filter(function (t) {
        return t.state === "done" || t.state === "error";
      }).map(function (t) { return t.id; });
      if (!doneIds.length) { toast("没有已完成的任务"); return; }
      if (!confirm("清除 " + doneIds.length + " 个已完成/出错的任务？\n（不会删除已下载文件）")) return;
      batchTaskAction("delete", doneIds);
    };
    $("taskChkAll").onchange = function () {
      var on = this.checked;
      $("taskList").querySelectorAll(".task-chk").forEach(function (cb) {
        if (cb.checked !== on) {
          cb.checked = on;
          if (on) taskSel[cb.dataset.id] = true;
          else delete taskSel[cb.dataset.id];
          var item = cb.closest(".task-item");
          if (item) item.classList.toggle("task-item-sel", on);
        }
      });
      updateTaskSelBar();
      $("taskChkAll").checked = on;
    };

    $("btnSaveSettings").onclick = function () {
      var dir = $("setDir").value.trim();
      var conns = parseInt($("setConns").value, 10) || 64;
      var totalConns = parseInt($("setTotalConns").value, 10) || 64;
      var maxActive = parseInt($("setMaxActive").value, 10) || 1;
      var speedLimit = parseFloat($("setSpeedLimit").value);
      if (isNaN(speedLimit) || speedLimit < 0) speedLimit = 0;
      api("/api/config", {
        method: "POST",
        body: {
          download_dir: dir,
          conns: conns,
          total_conns: totalConns,
          max_active_tasks: maxActive,
          speed_limit_mb: speedLimit
        }
      })
        .then(function () {
          $("setMsg").textContent = "已保存";
          setTimeout(function () { $("setMsg").textContent = ""; }, 2000);
          refreshDefaultDir();
        }).catch(function (e) {
          $("setMsg").textContent = "保存失败: " + e.message;
        });
    };
  }

  /* ---------------- 定时刷新任务 ---------------- */
  function startTaskPoll() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(function () {
      if ($("mainView").style.display === "none") return;
      if ($("viewTasks").style.display !== "none") loadTasks();
      else {
        // 后台也刷新一下红点
        api("/api/tasks").then(function (r) {
          var run = (r.tasks || []).filter(function (t) {
            return t.state === "running";
          }).length;
          $("taskDot").classList.toggle("show", run > 0);
        }).catch(function () {});
      }
    }, 3000);
  }

  /* ---------------- 启动 ---------------- */
  document.addEventListener("DOMContentLoaded", function () {
    bind();
    refreshDefaultDir();
    refreshStatus();
    startTaskPoll();
  });
})();
