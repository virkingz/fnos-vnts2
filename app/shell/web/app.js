/* vnts-nas 管理页前端 */
(function () {
  'use strict';

  var TOKEN = (window.__PANEL__ && window.__PANEL__.token) || '';
  var $ = function (s) { return document.querySelector(s); };
  var $$ = function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); };

  // 挂载前缀：直连时为 ''，经飞牛网关内嵌时为 '/app/vnts-nas'。
  // 绝对路径请求必须带上它，否则会打到飞牛根目录而不是本应用。
  var PREFIX = (function () {
    var p = location.pathname || '/';
    var m = p.match(/^(\/app\/[^\/]+)\/?/);
    return m ? m[1] : '';
  })();

  // ---------------- 全局忙碌锁 ----------------
  // 核心操作（启动/停止/重启/下载/上传/导入/导出/保存）执行期间，
  // 禁掉所有交互控件，避免并发点击造成重复启动、端口冲突等竞态问题。
  var busyDepth = 0;

  function setBusy(on) {
    busyDepth = Math.max(0, busyDepth + (on ? 1 : -1));
    var locked = busyDepth > 0;
    // 所有按钮与输入控件统一禁用（导航标签不禁，允许查看）
    $$('.btn, .inp, input, select, textarea').forEach(function (el) {
      if (el.id === 'consoleFrame') return;
      if (el.tagName === 'A') return;            // 链接不禁（如"新窗口打开"）
      if (locked) {
        if (!el.dataset._wasDisabled) {
          el.dataset._wasDisabled = el.disabled ? '1' : '0';
        }
        el.disabled = true;
        el.classList.add('is-busy');
      } else {
        el.disabled = el.dataset._wasDisabled === '1';
        delete el.dataset._wasDisabled;
        el.classList.remove('is-busy');
      }
    });
    document.body.classList.toggle('op-busy', locked);
  }

  // 包裹一次「占用锁 → 执行 → 释放锁」的操作。
  // fn 返回 Promise 时，等其结束（含 finally）再释放。
  function withBusy(fn) {
    setBusy(true);
    var done = function () { setBusy(false); };
    try {
      var r = fn();
      if (r && typeof r.then === 'function') {
        return r.then(function (v) { done(); return v; },
                      function (e) { done(); throw e; });
      }
      done();
      return r;
    } catch (e) {
      done();
      throw e;
    }
  }

  // ---------------- API ----------------
  function api(path, opts) {
    opts = opts || {};
    var headers = opts.headers || {};
    if (TOKEN) headers['X-Panel-Token'] = TOKEN;
    if (opts.json !== undefined) {
      headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.json);
    }
    return fetch(PREFIX + '/panel-api' + path, {
      method: opts.method || (opts.body ? 'POST' : 'GET'),
      headers: headers,
      body: opts.body
    }).then(function (r) {
      return r.json().catch(function () { return { code: r.status, msg: 'HTTP ' + r.status }; });
    }).then(function (j) {
      if (j && j.code === 0) return j.data;
      throw new Error((j && j.msg) || '请求失败');
    });
  }

  // ---------------- toast ----------------
  function toast(msg, kind) {
    var el = document.createElement('div');
    el.className = 'toast ' + (kind || '');
    el.textContent = msg;
    $('#toasts').appendChild(el);
    setTimeout(function () { el.remove(); }, kind === 'err' ? 6000 : 3200);
  }

  // ---------------- tabs ----------------
  function switchTab(name) {
    $$('.nav-item').forEach(function (a) {
      a.classList.toggle('active', a.getAttribute('data-tab') === name);
    });
    $$('.tab').forEach(function (s) {
      s.classList.toggle('hidden', s.id !== 'tab-' + name);
    });
    location.hash = name;
    if (name === 'logs') loadLog();
    if (name === 'backup') { refreshBackup(); loadLogConf(); }
    if (name === 'console') consoleEnter();
  }
  $$('.nav-item').forEach(function (a) {
    a.addEventListener('click', function () { switchTab(a.getAttribute('data-tab')); });
  });

  // ---------------- 总览 ----------------
  function card(k, v, cls, small) {
    return '<div class="card"><div class="k">' + k + '</div><div class="v ' + (cls || '') +
      (small ? ' small' : '') + '">' + v + '</div></div>';
  }

  function renderOverview(st) {
    var run = st.core_running
      ? '<span class="v ok">运行中</span>'
      : (st.core_present ? '<span class="v warn">已停止</span>' : '<span class="v err">未安装</span>');
    var html = '';
    html += '<div class="card"><div class="k">核心状态</div><div class="v">' + run + '</div></div>';
    html += card('核心版本', st.core_version ? 'v' + st.core_version : '未下载', '', true);
    html += card('组网服务端口', st.ports.SERVICE_PORT, '', true);
    html += card('管理页端口', st.ports.PANEL_PORT, '', true);
    html += card('默认网段', (st.settings && st.settings.network) || '-', '', true);
    html += card('白名单', (st.settings && st.settings.white_list && st.settings.white_list.length)
      ? st.settings.white_list.join(', ') : '全部允许', '', true);
    html += card('日志目录', st.log_dir, '', true);
    $('#ovCards').innerHTML = html;

    var host = location.hostname || 'NAS-IP';
    $('#ovAddr').textContent = host + ':' + st.ports.SERVICE_PORT;
    $('#sideStatus').innerHTML = '<span class="dot ' + (st.core_running ? 'on' : 'off') + '"></span>' +
      (st.core_running ? '核心运行中' : (st.core_present ? '核心已停止' : '核心未安装'));
  }

  function loadState() {
    return api('/state').then(function (st) {
      renderOverview(st);
      $('#coreCur').textContent = st.core_version ? 'v' + st.core_version : '未下载';
      $('#coreRun').textContent = st.core_running ? ('运行中 (pid ' + st.core_pid + ')') : '未运行';
      $('#coreArch').textContent = st.arch + (st.arch_supported ? '' : '（无官方包）');
      return st;
    });
  }

  // ---------------- 核心 ----------------
  function coreProg(on, text) {
    var p = $('#coreProg');
    p.classList.toggle('hidden', !on);
    if (on) {
      p.querySelector('span').textContent = text || '下载中…';
      p.querySelector('i').className = 'indet';
    }
  }

  $('#btnCheck').addEventListener('click', function () {
    withBusy(function () {
      $('#coreLatest').textContent = '查询中…';
      return api('/core/check').then(function (d) {
        $('#coreLatest').textContent = 'v' + d.latest;
        var act = d.state === 'latest' ? '已是最新' : (d.state === 'update' ? '可更新' : '尚未安装');
        $('#btnDownload').textContent = d.state === 'update' ? ('更新到 v' + d.latest) : '下载核心';
        $('#btnDownload').dataset.version = d.latest;
        toast('最新版本 v' + d.latest + '（' + act + '）', 'ok');
      }).catch(function (e) {
        $('#coreLatest').textContent = '查询失败';
        toast(e.message, 'err');
      });
    });
  });

  $('#btnDownload').addEventListener('click', function () {
    withBusy(function () {
      var ver = $('#btnDownload').dataset.version || '';
      coreProg(true);
      return api('/core/download', { json: { version: ver } }).then(function (d) {
        toast('核心已安装 v' + (d.version || ver), 'ok');
        return loadState();
      }).catch(function (e) { toast(e.message, 'err'); })
        .finally(function () { coreProg(false); });
    });
  });

  $('#btnStart').addEventListener('click', function () {
    withBusy(function () {
      return api('/core/start', { method: 'POST' }).then(function (d) {
        toast('已启动：' + d.msg, 'ok'); return loadState();
      }).catch(function (e) { toast(e.message, 'err'); });
    });
  });
  $('#btnStop').addEventListener('click', function () {
    withBusy(function () {
      return api('/core/stop', { method: 'POST' }).then(function () {
        toast('核心已停止', 'ok'); return loadState();
      }).catch(function (e) { toast(e.message, 'err'); });
    });
  });

  $('#btnUpload').addEventListener('click', function () {
    var f = $('#coreFile').files[0];
    if (!f) return toast('请选择文件', 'warn');
    var ver = $('#coreVer').value.trim();
    withBusy(function () {
      coreProg(true, '上传中…');
      var headers = {};
      if (TOKEN) headers['X-Panel-Token'] = TOKEN;
      if (ver) headers['X-Core-Version'] = ver;
      return fetch(PREFIX + '/panel-api/core/upload', { method: 'POST', headers: headers, body: f })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (j.code !== 0) throw new Error(j.msg);
          toast('上传安装成功 v' + (j.data.version || ver || '?'), 'ok');
          return loadState();
        })
        .catch(function (e) { toast('上传失败：' + e.message, 'err'); })
        .finally(function () { coreProg(false); });
    });
  });

  // 无端口的 http(s) 地址是「加速前缀」不是代理：代理必须有端口，
  // 把前缀填进代理栏会让 urllib 向它发 CONNECT 而超时，之后所有下载都失败。
  function looksLikeMirror(v) {
    return /^https?:\/\/[^\/?#:]+\/?$/i.test(v || '');
  }

  $('#btnSaveNet').addEventListener('click', function () {
    var mirror = $('#mirror').value.trim();
    var proxy = $('#proxy').value.trim();
    if (proxy && looksLikeMirror(proxy)) {
      if (!confirm('「' + proxy + '」不带端口，是加速镜像前缀，不是代理。\n\n' +
        '要把它改填到「镜像前缀」吗？\n（点“取消”则不保存，可自行修改后再存）')) {
        toast('未保存：请把它填到「镜像前缀」，或填写带端口的代理地址', 'warn');
        return;
      }
      mirror = proxy;
      proxy = '';
      $('#mirror').value = mirror;
      $('#proxy').value = '';
    }
    withBusy(function () {
      // 顺序保存：代理要实测，失败就不落盘，错误信息也更好读
      return api('/mirror', { method: 'PUT', json: { mirror: mirror } })
        .then(function (d) {
          if (d && d.warning) toast(d.warning, 'warn');
          return api('/proxy', { method: 'PUT', json: { proxy: proxy } });
        })
        .then(function () { toast('已保存下载源与代理', 'ok'); })
        .catch(function (e) { toast(e.message, 'err'); });
    });
  });
  $('#btnTest').addEventListener('click', function () {
    withBusy(function () {
      $('#testResult').textContent = '测试中…';
      var headers = {};
      if (TOKEN) headers['X-Panel-Token'] = TOKEN;
      return fetch(PREFIX + '/panel-api/test-connection', { headers: headers })
        .then(function (r) {
          return r.json().catch(function () { return { code: r.status, msg: 'HTTP ' + r.status }; });
        })
        .then(function (j) {
          var d = (j && j.data) || {};
          var checks = d.checks || [];
          $('#testResult').innerHTML = checks.map(function (c) {
            return '<span style="color:' + (c.ok ? '#16a34a' : '#dc2626') + '">' +
              (c.ok ? '✓' : '✗') + '</span> ' + c.name + '：' + (c.detail || '');
          }).join('<br>');
          if (j.code === 0) toast('连通性正常', 'ok');
          else toast(j.msg || '连通性异常', 'err');
        })
        .catch(function (e) {
          $('#testResult').textContent = '测试失败';
          toast(e.message, 'err');
        });
    });
  });

  // ---------------- 设置 ----------------
  function fillSettings(s) {
    $('#sNetwork').value = s.network || '';
    $('#sWhite').value = (s.white_list || []).join(',');
    $('#sLease').value = s.lease_duration || 86400;
    $('#sCustom').value = Object.keys(s.custom_nets || {}).map(function (k) {
      return k + '=' + s.custom_nets[k];
    }).join('\n');
    $('#sPersist').checked = !!s.persistence;
    $('#sUser').value = s.username || '';
    $('#sPass').value = s.password || '';
    $('#sTcp').checked = !!s.enable_tcp;
    $('#sQuic').checked = !!s.enable_quic;
    $('#sWss').checked = !!s.enable_wss;
    // 留空表示复用「组网服务端口」
    $('#sTcpPort').value = s.tcp_port || '';
    $('#sQuicPort').value = s.quic_port || '';
    $('#sWsPort').value = s.ws_port || '';
    $('#sPeerOn').checked = !!s.enable_peer;
    $('#sPeerPort').value = s.peer_bind_port || 29873;
    $('#sPeerToken').value = s.server_token || '';
  }

  function collectSettings() {
    var custom = {};
    $('#sCustom').value.split('\n').forEach(function (ln) {
      ln = ln.trim();
      if (!ln) return;
      var i = ln.indexOf('=');
      if (i > 0) custom[ln.slice(0, i).trim()] = ln.slice(i + 1).trim();
    });
    return {
      network: $('#sNetwork').value.trim(),
      white_list: $('#sWhite').value.split(/[,\s]+/).filter(Boolean),
      lease_duration: parseInt($('#sLease').value, 10) || 86400,
      custom_nets: custom,
      persistence: $('#sPersist').checked,
      username: $('#sUser').value.trim(),
      password: $('#sPass').value,
      enable_tcp: $('#sTcp').checked,
      enable_quic: $('#sQuic').checked,
      enable_wss: $('#sWss').checked,
      // 空串表示复用组网服务端口（后端的默认行为）
      tcp_port: $('#sTcpPort').value.trim(),
      quic_port: $('#sQuicPort').value.trim(),
      ws_port: $('#sWsPort').value.trim(),
      enable_peer: $('#sPeerOn').checked,
      peer_bind_port: parseInt($('#sPeerPort').value, 10) || 29873,
      server_token: $('#sPeerToken').value.trim()
    };
  }

  $('#btnSaveSettings').addEventListener('click', function () {
    var msg = $('#settingsMsg');
    withBusy(function () {
      msg.textContent = '保存中…';
      return api('/settings', { method: 'PUT', json: collectSettings() }).then(function () {
        msg.textContent = '已保存';
        toast('设置已保存', 'ok'); return loadState();
      }).catch(function (e) {
        msg.textContent = '';
        toast(e.message, 'err');
      });
    });
  });

  // ---------------- 出站互联域名（dns-ip.json） ----------------
  function esc(s) {
    return String(s === undefined || s === null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function fmtTime(sec) {
    if (!sec) return '—';
    var d = new Date(sec * 1000);
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function renderPeers(st) {
    var box = $('#dnsPeersStatus');
    if (!st || !st.records || !st.records.length) {
      // 清单已清空，但可能还有我们加过的地址待撤下
      if (st && st.pending_cleanup) {
        box.innerHTML = '<p class="muted">清单已清空，正在撤下托管地址：' +
          (st.managed || []).map(function (a) {
            return '<span class="mono">' + esc(a) + '</span>';
          }).join('、') + '</p>';
      } else {
        box.innerHTML = '<p>未配置出站互联域名（不轮询）。</p>';
      }
      return;
    }
    var rows = st.records.map(function (r) {
      var state;
      if (r.last_error) state = '<span class="bad">异常：' + esc(r.last_error) + '</span>';
      else if (r.current) state = '<span class="ok">当前 ' + esc(r.current) + '</span>';
      else state = '<span class="muted">等待同步</span>';
      return '<tr><td class="mono">' + esc(r.addr) + '</td><td>' + state + '</td>' +
        '<td class="muted">' + (r.last_sync ? fmtTime(r.last_sync) : '—') + '</td></tr>';
    }).join('');
    box.innerHTML = '<p class="muted">轮询间隔 ' + st.interval + ' 秒 · 共 ' +
      st.count + ' 条</p><table class="tbl"><thead><tr>' +
      '<th>配置的域名</th><th>状态</th><th>上次同步</th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table>';
  }

  function loadPeers() {
    return api('/dns-peers').then(function (st) {
      $('#sPeers').value = (st.records || []).map(function (r) {
        return r.addr;
      }).join('\n');
      $('#dnsInterval').value = st.interval || 60;
      renderPeers(st);
    }).catch(function (e) { toast(e.message, 'err'); });
  }

  $('#btnSavePeers').addEventListener('click', function () {
    var msg = $('#peersMsg');
    withBusy(function () {
      msg.textContent = '保存中…';
      return api('/dns-peers', { method: 'PUT', json: {
        peers: $('#sPeers').value,
        interval: $('#dnsInterval').value
      } }).then(function (d) {
        msg.textContent = '';
        renderPeers(d.status);
        toast('已保存并同步', 'ok');
        return loadState();
      }).catch(function (e) {
        msg.textContent = '';
        toast(e.message, 'err');
      });
    });
  });

  $('#btnSyncPeers').addEventListener('click', function () {
    var msg = $('#peersMsg');
    withBusy(function () {
      msg.textContent = '同步中…';
      return api('/dns-peers/sync', { method: 'POST', json: {} }).then(function (d) {
        msg.textContent = '';
        renderPeers(d.status);
        toast(d.msg || '已同步', d.ok ? 'ok' : 'err');
      }).catch(function (e) {
        msg.textContent = '';
        toast(e.message, 'err');
      });
    });
  });

  function loadPorts() {
    api('/ports').then(function (p) {
      $('#pPanel').value = p.PANEL_PORT;
      $('#pService').value = p.SERVICE_PORT;
      $('#pConsole').value = p.CONSOLE_PORT;
    });
  }
  $('#btnSavePorts').addEventListener('click', function () {
    withBusy(function () {
      return api('/ports', { method: 'PUT', json: {
        PANEL_PORT: $('#pPanel').value,
        SERVICE_PORT: $('#pService').value,
        CONSOLE_PORT: $('#pConsole').value
      } }).then(function (d) {
        toast(d.restart_needed ? '端口已保存，请改用新端口访问管理页' : '端口已保存', 'ok');
        return loadState();
      }).catch(function (e) { toast(e.message, 'err'); });
    });
  });

  // ---------------- 导入导出 ----------------
  function refreshBackup() {
    api('/logdir').then(function (d) {
      $('#logDir').value = d.from_env ? d.log_dir : (d.log_dir === d.default ? '' : d.log_dir);
      $('#logDir').placeholder = d.default + '（默认）';
      $('#logDirHint').textContent = d.from_env
        ? '当前由环境变量 LOG_DIR 指定：' + d.log_dir + '（界面不可改）'
        : '留空则用默认：' + d.default;
    });
    api('/state').then(function (st) {
      var items = ['server/config.toml（唯一配置文件）',
        st.has_db ? 'server/network_control.db' : null, 'server/cert.pem / key.pem（若存在）']
        .filter(Boolean);
      $('#exportList').innerHTML = items.map(function (x) { return '✓ ' + x; }).join('<br>') +
        '<br><span style="color:#dc2626">✗ 不含日志目录（' + st.log_dir + '）</span>';
    });
  }

  $('#btnExport').addEventListener('click', function () {
    withBusy(function () {
      // 不能用 <a href> 直接下载：浏览器导航带不上 X-Panel-Token 头，
      // 后端会回 401 JSON，Chrome 按 MIME 存成 export.json（下载失败）。
      // 必须走 fetch 带令牌取 blob，再用 object URL 下载。
      var headers = {};
      if (TOKEN) headers['X-Panel-Token'] = TOKEN;
      return fetch(PREFIX + '/panel-api/config/export', { headers: headers })
        .then(function (r) {
          var ct = r.headers.get('Content-Type') || '';
          if (!r.ok || ct.indexOf('application/zip') < 0) {
            return r.json().catch(function () { return { msg: 'HTTP ' + r.status }; })
              .then(function (j) { throw new Error(j.msg || ('HTTP ' + r.status)); });
          }
          var cd = r.headers.get('Content-Disposition') || '';
          var m = /filename="?([^";]+)"?/.exec(cd);
          var name = m ? m[1] : 'fnos-vnts2-config.zip';
          return r.blob().then(function (b) { return { blob: b, name: name }; });
        })
        .then(function (res) {
          var url = URL.createObjectURL(res.blob);
          var a = document.createElement('a');
          a.href = url;
          a.download = res.name;
          document.body.appendChild(a); a.click(); a.remove();
          setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
          toast('已导出 ' + res.name, 'ok');
        })
        .catch(function (e) { toast('导出失败：' + e.message, 'err'); });
    });
  });

  $('#btnImport').addEventListener('click', function () {
    var f = $('#importFile').files[0];
    if (!f) return toast('请选择 zip 文件', 'warn');
    if (!confirm('导入将覆盖当前服务端配置与数据库（会自动备份）。继续？')) return;
    withBusy(function () {
      $('#importResult').textContent = '导入中…';
      var headers = {};
      if (TOKEN) headers['X-Panel-Token'] = TOKEN;
      return fetch(PREFIX + '/panel-api/config/import', { method: 'POST', headers: headers, body: f })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (j.code !== 0) throw new Error(j.msg);
          $('#importResult').textContent = '已导入：' + (j.data.restored || []).join('、');
          // 核心没跑时，出站互联要等核心起来才会同步 —— 如实说明，
          // 免得用户以为「导入了却没生效」
          if (j.data.notice) {
            toast('导入成功，但核心未运行', 'ok');
            $('#importResult').textContent += '　' + j.data.notice;
          } else {
            toast('导入成功', 'ok');
          }
          loadState(); loadPorts();
          api('/settings').then(fillSettings);
        })
        .catch(function (e) {
          $('#importResult').textContent = '';
          toast('导入失败：' + e.message, 'err');
        });
    });
  });

  $('#btnSaveLogDir').addEventListener('click', function () {
    withBusy(function () {
      return api('/logdir', { method: 'PUT', json: { log_dir: $('#logDir').value.trim() } })
        .then(function (d) { toast('日志目录已设为 ' + d.log_dir, 'ok'); refreshBackup(); loadState(); })
        .catch(function (e) { toast(e.message, 'err'); });
    });
  });

  // ---------------- vnts2 日志级别与轮转 ----------------
  // 上限统一按 KB 提交；界面上用户可用 KB 或 MB 输入。
  function logSetMax(kb) {
    if (kb % 1024 === 0) {
      $('#logMax').value = kb / 1024;
      $('#logMaxUnit').value = 'mb';
    } else {
      $('#logMax').value = kb;
      $('#logMaxUnit').value = 'kb';
    }
  }

  function logMaxKb() {
    var n = parseFloat($('#logMax').value);
    if (isNaN(n)) return '';
    return Math.round(n * ($('#logMaxUnit').value === 'mb' ? 1024 : 1));
  }

  function loadLogConf() {
    api('/logconf').then(function (c) {
      $('#logLevel').value = c.level || 'info';
      logSetMax(c.max_kb || 10240);
      $('#logKeep').value = c.keep || 5;
      $('#logConfHint').textContent =
        '修改后约 30 秒自动生效（核心每隔 30 秒重读配置），无需重启。' +
        (c.core_running ? '' : ' 当前核心未运行，启动时即按此配置输出。');
    }).catch(function (e) { toast(e.message, 'err'); });
  }

  $('#logMax512').addEventListener('click', function (e) {
    e.preventDefault(); logSetMax(512);
  });
  $('#logMax10').addEventListener('click', function (e) {
    e.preventDefault(); logSetMax(10 * 1024);
  });

  $('#btnSaveLogConf').addEventListener('click', function () {
    var msg = $('#logConfMsg');
    var kb = logMaxKb();
    if (kb === '') { toast('请填写日志大小上限', 'err'); return; }
    withBusy(function () {
      msg.textContent = '保存中…';
      return api('/logconf', { method: 'PUT', json: {
        level: $('#logLevel').value,
        max_kb: kb,
        keep: $('#logKeep').value
      } }).then(function (d) {
        msg.textContent = '';
        var c = d.conf;
        $('#logLevel').value = c.level;
        logSetMax(c.max_kb);
        $('#logKeep').value = c.keep;
        toast('日志设置已保存，约 30 秒内生效', 'ok');
      }).catch(function (e) {
        msg.textContent = '';
        toast(e.message, 'err');
      });
    });
  });

  // ---------------- 日志 ----------------
  function loadLog() {
    var name = $('#logSelect').value;
    api('/logs?name=' + encodeURIComponent(name)).then(function (d) {
      $('#logView').textContent = d.text || '（空）';
      var parts = Object.keys(d.sizes || {}).map(function (k) {
        return k + ':' + (d.sizes[k] / 1024).toFixed(1) + 'K';
      });
      $('#logDirShown').textContent = parts.join('  ');
      $('#logView').scrollTop = $('#logView').scrollHeight;
    }).catch(function (e) { $('#logView').textContent = '读取失败：' + e.message; });
  }
  $('#btnReloadLog').addEventListener('click', loadLog);
  $('#logSelect').addEventListener('change', loadLog);
  $('#btnClearLog').addEventListener('click', function () {
    if (!confirm('清空所有日志文件？')) return;
    withBusy(function () {
      return api('/logs/clear', { method: 'POST' }).then(function () {
        toast('日志已清空', 'ok'); loadLog();
      }).catch(function (e) { toast(e.message, 'err'); });
    });
  });

  // ---------------- 控制台 ----------------
  // 关键：iframe 的 src 一旦固定就不会因核心后来启动而刷新。
  // 因此这里先探测控制台是否就绪，就绪才加载 iframe；
  // 未就绪时展示诊断（含核心日志尾部）并轮询等待，就绪后自动加载。
  var consoleState = { ready: false, timer: null, loaded: false, username: '' };

  function consoleSetBar(kind, text) {
    $('#consoleDot').className = 'dot ' + kind;
    $('#consoleBar').textContent = text;
  }

  function consoleLoadFrame() {
    if (consoleState.loaded) return;
    consoleState.loaded = true;
    // hash 显式指向网络管理：控制台此前若停在其他视图（如系统设置），
    // 会因 hash 保留而直接进那一页。这里强制回到首页。
    // 注意：控制台路由表本身已有 `{path:'/', redirect:'/networks'}`，
    // 但若登录前停在 #/login，登录后由守卫跳转，个别时序下 URL 可能停在 #/。
    // 故加载后再做一次同源兜底纠正（见 nudgeConsoleHash）。
    $('#consoleFrame').src = PREFIX + '/console/?' + Date.now() + '#/networks';
    $('#consoleFrame').onload = function () {
      nudgeConsoleHash(6);
    };
    $('#consoleFrame').classList.remove('hidden');
    $('#consoleDiag').classList.add('hidden');
    $('#btnConsoleStart').classList.add('hidden');
    var u = consoleState.username || 'admin';
    consoleSetBar('on', '控制台已加载（登录账号：' + u + '）');
  }

  // 同源兜底：管理页与控制台都在同一源下，可直接读 iframe 的 location。
  // 若 hash 为空、'#/' 或 '#/login'（已登录时），纠正到 '#/networks'。
  // 只在必要时改，且限定重试次数，避免与前端路由互相打架。
  function nudgeConsoleHash(tries) {
    var fr = $('#consoleFrame');
    var want = '#/networks';
    try {
      var loc = fr.contentWindow && fr.contentWindow.location;
      if (!loc) return;
      var h = loc.hash || '';
      // 非登录页却停在根路径 → 纠正
      if (h === '' || h === '#' || h === '#/') {
        loc.replace(loc.pathname + loc.search + want);
        return;
      }
    } catch (e) {
      return;  // 非同源（理论上不会），静默放弃
    }
    if (tries > 1) {
      setTimeout(function () { nudgeConsoleHash(tries - 1); }, 1200);
    }
  }

  function consoleShowDiag(d) {
    $('#consoleFrame').classList.add('hidden');
    $('#consoleDiag').classList.remove('hidden');
    $('#btnConsoleStart').classList.toggle('hidden', !d.core_present);

    if (!d.core_present) {
      consoleSetBar('off', '核心尚未下载');
      $('#consoleDiagMsg').innerHTML =
        '还未下载 vnts2 核心，控制台无法使用。<br>请到「<b>核心更新</b>」标签点「检查更新」→「下载核心」。';
      return;
    }
    if (!d.core_running) {
      consoleSetBar('wait', '核心未运行');
      $('#consoleDiagMsg').innerHTML =
        '核心已下载但未运行，控制台无法连接。<br>点右上「<b>启动核心</b>」，或到「核心更新」标签启动。';
      return;
    }
    if (d.ready) return;
    consoleSetBar('wait', '核心运行中，控制台正在就绪…');
    $('#consoleDiagMsg').innerHTML =
      '核心进程在运行，但控制台端口（' + d.bind + '）尚未响应。<br>' +
      '刚启动时属正常现象，通常几秒内可用；若持续如此请看下方核心日志。';
  }

  function consolePoll() {
    clearTimeout(consoleState.timer);
    api('/console-ready').then(function (d) {
      if (d.username) consoleState.username = d.username;
      if (d.ready) {
        consoleLoadFrame();
        return;
      }
      consoleShowDiag(d);
      $('#consoleDiagLog').textContent = d.log_tail || '（核心日志为空）';
      // 未就绪则继续等待，就绪后自动加载
      consoleState.timer = setTimeout(consolePoll, 2500);
    }).catch(function (e) {
      consoleSetBar('off', '检测失败：' + e.message);
      consoleState.timer = setTimeout(consolePoll, 4000);
    });
  }

  function consoleEnter() {
    if (consoleState.ready) return;
    consolePoll();
  }

  $('#btnReloadFrame').addEventListener('click', function () {
    withBusy(function () {
      consoleState.loaded = false;
      $('#consoleFrame').src = 'about:blank';
      consoleState.ready = false;
      consolePoll();
    });
  });

  $('#btnConsoleStart').addEventListener('click', function () {
    withBusy(function () {
      consoleSetBar('wait', '正在启动核心…');
      return api('/core/start', { method: 'POST' }).then(function () {
        toast('核心已启动', 'ok');
        consoleState.ready = false;
        consolePoll();
        loadState();
      }).catch(function (e) {
        toast('启动失败：' + e.message, 'err');
        consolePoll();
      });
    });
  });

  // ---------------- 复制地址 ----------------
  $('#copyAddr').addEventListener('click', function () {
    var t = $('#ovAddr').textContent;
    navigator.clipboard.writeText(t).then(function () { toast('已复制 ' + t, 'ok'); },
      function () { toast('复制失败，请手动选择', 'warn'); });
  });

  // ---------------- 初始化 ----------------
  function boot() {
    var hash = (location.hash || '#overview').slice(1) || 'overview';
    switchTab(hash);
    loadState().catch(function (e) { toast('加载状态失败：' + e.message, 'err'); });
    loadPorts();
    api('/settings').then(fillSettings).catch(function () {});
    loadPeers();
    // 镜像/代理现值统一从 /state 取（见下），无需再单独拉 /mirror、/proxy
    // 拉取镜像/代理现值
    api('/state').then(function (st) {
      $('#mirror').value = st.mirror || '';
      $('#proxy').value = st.proxy || '';
    });
    setInterval(function () { loadState().catch(function () {}); }, 8000);
  }
  boot();
})();
