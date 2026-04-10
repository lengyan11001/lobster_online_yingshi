/**
 * Meta Social (Instagram / Facebook) 账号管理页。
 * 支持 per-user Facebook App 凭据：用户填写自己的 App ID / Secret，服务器中转 OAuth。
 */
(function () {
  function serverBase() {
    return (typeof AUTH_SERVER_BASE !== 'undefined' && AUTH_SERVER_BASE)
      ? String(AUTH_SERVER_BASE).replace(/\/$/, '')
      : (typeof API_BASE !== 'undefined' ? String(API_BASE).replace(/\/$/, '') : '');
  }

  function hdrs() {
    return Object.assign({ 'Content-Type': 'application/json' }, typeof authHeaders === 'function' ? authHeaders() : {});
  }

  function jwtToken() {
    try { return typeof authHeaders === 'function' ? (authHeaders()['Authorization'] || '').replace('Bearer ', '') : ''; } catch (e) { return ''; }
  }

  function escapeH(s) { return typeof escapeHtml === 'function' ? escapeHtml(s) : String(s || ''); }

  function showMsg(el, text, isErr) {
    if (!el) return;
    el.textContent = text || '';
    el.className = 'msg' + (isErr ? ' err' : '');
    el.style.display = text ? 'block' : 'none';
  }

  var _accounts = [];

  function platformBadge(plat) {
    if (plat === 'instagram') return '<span style="color:#e1306c;font-weight:600;">IG</span>';
    if (plat === 'facebook') return '<span style="color:#1877f2;font-weight:600;">FB</span>';
    return escapeH(plat || '');
  }

  // ── OAuth redirect URI (display to user) ──

  function loadRedirectUri() {
    var el = document.getElementById('metaSocialRedirectUri');
    if (!el) return;
    var base = serverBase();
    if (!base) return;
    fetch(base + '/api/meta-social/oauth/redirect-uri', { headers: hdrs() })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.redirect_uri) el.textContent = d.redirect_uri;
      })
      .catch(function () {});
  }

  // ── Account list ──

  function renderAccountList() {
    var listEl = document.getElementById('metaSocialAccountsList');
    if (!listEl) return;
    var base = serverBase();
    if (!base) {
      listEl.innerHTML = '<p class="msg err">未配置 AUTH_SERVER_BASE / API_BASE，无法连接服务器。</p>';
      return;
    }
    listEl.innerHTML = '<p class="meta">加载中…</p>';

    fetch(base + '/api/meta-social/accounts', { headers: hdrs() })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d, status: r.status }; }); })
      .then(function (x) {
        if (!x.ok) {
          listEl.innerHTML = '<p class="msg err">加载失败 (' + x.status + ')</p>';
          return;
        }
        var rows = Array.isArray(x.d) ? x.d : (x.d && Array.isArray(x.d.accounts) ? x.d.accounts : []);
        _accounts = rows;
        if (rows.length === 0) {
          listEl.innerHTML = '<p class="meta">暂无已连接账号。请先填写 Facebook App 凭据，然后点击「授权连接」。</p>';
          return;
        }
        var html = '<div class="skill-store-grid" style="gap:0.75rem;">';
        rows.forEach(function (a) {
          var label = a.label || a.facebook_page_name || ('账号 #' + a.id);
          var igUser = a.instagram_username ? ('@' + a.instagram_username) : '';
          var pageName = a.facebook_page_name || '';
          var detail = igUser ? (igUser + (pageName ? ' · ' + pageName : '')) : pageName;
          var hasIG = !!a.instagram_business_account_id;
          var borderColor = hasIG ? 'rgba(225,48,108,0.35)' : 'rgba(24,119,242,0.35)';
          var bgGrad = hasIG ? 'rgba(225,48,108,0.06)' : 'rgba(24,119,242,0.06)';

          html += '<div class="skill-store-card" style="border-color:' + borderColor + ';background:linear-gradient(135deg,' + bgGrad + ',transparent);">';
          html += '<div class="card-label">';
          if (hasIG) html += platformBadge('instagram') + ' + ';
          html += platformBadge('facebook');
          html += ' <span class="badge-installed">已连接</span></div>';
          html += '<div class="card-value">' + escapeH(label) + '</div>';
          if (detail) html += '<div class="card-desc">' + escapeH(detail) + '</div>';
          if (a.meta_app_id) html += '<div style="font-size:0.75rem;color:var(--text-muted);margin-top:0.15rem;">App ID: ' + escapeH(a.meta_app_id) + '</div>';
          if (a.proxy_server_masked) html += '<div style="font-size:0.72rem;color:var(--text-muted);">代理: ' + escapeH(a.proxy_server_masked) + '</div>';
          if (a.token_expires_at) html += '<div style="font-size:0.72rem;color:var(--text-muted);">Token 过期: ' + escapeH(a.token_expires_at).slice(0, 10) + '</div>';
          html += '<div class="card-actions" style="display:flex;gap:0.35rem;flex-wrap:wrap;margin-top:0.5rem;">';
          html += '<button type="button" class="btn btn-ghost btn-sm meta-social-reauth-btn" data-id="' + a.id + '">重新授权</button>';
          html += '<button type="button" class="btn btn-ghost btn-sm meta-social-sync-btn" data-id="' + a.id + '">同步数据</button>';
          html += '<button type="button" class="btn btn-ghost btn-sm meta-social-edit-btn" data-id="' + a.id + '">编辑</button>';
          html += '<button type="button" class="btn btn-ghost btn-sm meta-social-del-btn" data-id="' + a.id + '" style="color:#fb7185;">删除</button>';
          html += '</div></div>';
        });
        html += '</div>';
        listEl.innerHTML = html;
        bindAccountActions();
      })
      .catch(function (e) {
        listEl.innerHTML = '<p class="msg err">网络错误: ' + escapeH(e.message) + '</p>';
      });
  }

  function bindAccountActions() {
    document.querySelectorAll('.meta-social-reauth-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var aid = btn.getAttribute('data-id');
        var local = localBase();
        var base = serverBase();
        btn.disabled = true;
        showMsg(document.getElementById('metaSocialPageMsg'), '正在启动 Chromium 重新授权…', false);

        var token = jwtToken();
        var reauthUrl = base + '/api/meta-social/accounts/' + aid + '/reauth';
        if (token) reauthUrl += '?token=' + encodeURIComponent(token);

        fetch(reauthUrl, { headers: hdrs() })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (!d || !d.login_url) {
              btn.disabled = false;
              showMsg(document.getElementById('metaSocialPageMsg'), '重新授权失败: ' + (d.detail || JSON.stringify(d)), true);
              return;
            }
            if (!local) {
              btn.disabled = false;
              window.open(d.login_url, '_blank');
              showMsg(document.getElementById('metaSocialPageMsg'), '已打开 Facebook 授权页面。完成后请刷新列表。', false);
              return;
            }
            var acct = _accounts.filter(function (a) { return a.id === parseInt(aid, 10); })[0];
            var proxyServer = _buildProxyServer();
            fetch(local + '/api/meta-social-local/oauth/open-chromium-url', {
              method: 'POST',
              headers: hdrs(),
              body: JSON.stringify({ login_url: d.login_url, proxy_server: proxyServer.trim() })
            })
              .then(function (r2) { return r2.json(); })
              .then(function (d2) {
                btn.disabled = false;
                if (d2.chromium_opened) {
                  showMsg(document.getElementById('metaSocialPageMsg'), '已在 Chromium 中打开重新授权页。完成后请刷新列表。', false);
                } else {
                  window.open(d.login_url, '_blank');
                  showMsg(document.getElementById('metaSocialPageMsg'), 'Chromium 未能启动，已用浏览器打开。完成后请刷新列表。', false);
                }
              })
              .catch(function () {
                btn.disabled = false;
                window.open(d.login_url, '_blank');
                showMsg(document.getElementById('metaSocialPageMsg'), '已打开 Facebook 授权页面。完成后请刷新列表。', false);
              });
          })
          .catch(function (e) {
            btn.disabled = false;
            showMsg(document.getElementById('metaSocialPageMsg'), '重新授权失败: ' + e.message, true);
          });
      });
    });

    document.querySelectorAll('.meta-social-sync-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var aid = btn.getAttribute('data-id');
        var msgEl = document.getElementById('metaSocialPageMsg');
        showMsg(msgEl, '同步中…', false);
        btn.disabled = true;
        fetch(serverBase() + '/api/meta-social/sync?account_id=' + aid, { method: 'POST', headers: hdrs() })
          .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d, status: r.status }; }); })
          .then(function (x) {
            if (!x.ok) {
              showMsg(msgEl, '同步失败 (' + x.status + '): ' + (x.d.detail || JSON.stringify(x.d)), true);
            } else {
              showMsg(msgEl, '同步完成', false);
              loadMetaSocialDataView();
            }
            btn.disabled = false;
          })
          .catch(function (e) {
            showMsg(msgEl, '同步失败: ' + e.message, true);
            btn.disabled = false;
          });
      });
    });

    document.querySelectorAll('.meta-social-edit-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var aid = parseInt(btn.getAttribute('data-id'), 10);
        var acct = _accounts.filter(function (a) { return a.id === aid; })[0];
        if (!acct) return;
        var newLabel = prompt('修改标签（当前: ' + (acct.label || '') + '）', acct.label || '');
        if (newLabel === null) return;
        fetch(serverBase() + '/api/meta-social/accounts/' + aid, {
          method: 'PATCH',
          headers: hdrs(),
          body: JSON.stringify({ label: newLabel })
        })
          .then(function (r) { return r.json(); })
          .then(function () { renderAccountList(); })
          .catch(function (e) { alert('修改失败: ' + e.message); });
      });
    });

    document.querySelectorAll('.meta-social-del-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var aid = btn.getAttribute('data-id');
        if (!confirm('确定删除此账号？删除后需重新授权。')) return;
        fetch(serverBase() + '/api/meta-social/accounts/' + aid, { method: 'DELETE', headers: hdrs() })
          .then(function () { renderAccountList(); })
          .catch(function (e) { alert('删除失败: ' + e.message); });
      });
    });
  }

  // ── New OAuth with user-provided App credentials ──

  function localBase() {
    return (typeof API_BASE !== 'undefined' && API_BASE)
      ? String(API_BASE).replace(/\/$/, '')
      : '';
  }

  var _DRAFT_KEY = '_meta_social_draft';

  function _saveDraft() {
    try {
      var d = {
        app_id: (document.getElementById('metaAppIdInput') || {}).value || '',
        app_secret: (document.getElementById('metaAppSecretInput') || {}).value || '',
        proxy_ip: (document.getElementById('metaProxyIpInput') || {}).value || '',
        proxy_port: (document.getElementById('metaProxyPortInput') || {}).value || '',
        proxy_user: (document.getElementById('metaProxyUserInput') || {}).value || '',
        proxy_pass: (document.getElementById('metaProxyPassInput') || {}).value || ''
      };
      localStorage.setItem(_DRAFT_KEY, JSON.stringify(d));
    } catch (e) {}
  }

  function _loadDraft() {
    try {
      var raw = localStorage.getItem(_DRAFT_KEY);
      if (!raw) return;
      var d = JSON.parse(raw);
      var fields = [
        ['metaAppIdInput', 'app_id'],
        ['metaAppSecretInput', 'app_secret'],
        ['metaProxyIpInput', 'proxy_ip'],
        ['metaProxyPortInput', 'proxy_port'],
        ['metaProxyUserInput', 'proxy_user'],
        ['metaProxyPassInput', 'proxy_pass']
      ];
      fields.forEach(function (f) {
        var el = document.getElementById(f[0]);
        if (el && d[f[1]]) el.value = d[f[1]];
      });
    } catch (e) {}
  }

  function _bindDraftAutoSave() {
    ['metaAppIdInput', 'metaAppSecretInput', 'metaProxyIpInput', 'metaProxyPortInput', 'metaProxyUserInput', 'metaProxyPassInput'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el && !el._draftBound) {
        el._draftBound = true;
        el.addEventListener('input', _saveDraft);
      }
    });
  }

  function _buildProxyServer() {
    var ip = (document.getElementById('metaProxyIpInput') || {}).value || '';
    var port = (document.getElementById('metaProxyPortInput') || {}).value || '';
    ip = ip.trim();
    port = port.trim();
    if (!ip) return '';
    if (!port) return 'http://' + ip;
    return 'http://' + ip + ':' + port;
  }

  function startOAuthWithCredentials() {
    var local = localBase();
    if (!local) { alert('未配置本地服务器地址'); return; }
    var appId = (document.getElementById('metaAppIdInput') || {}).value || '';
    var appSecret = (document.getElementById('metaAppSecretInput') || {}).value || '';
    if (!appId.trim() || !appSecret.trim()) {
      showMsg(document.getElementById('metaSocialPageMsg'), '请填写 Facebook App ID 和 App Secret。', true);
      return;
    }
    var proxyServer = _buildProxyServer();
    var proxyUser = (document.getElementById('metaProxyUserInput') || {}).value || '';
    var proxyPass = (document.getElementById('metaProxyPassInput') || {}).value || '';
    _saveDraft();

    var body = {
      app_id: appId.trim(),
      app_secret: appSecret.trim(),
      proxy_server: proxyServer,
      proxy_username: proxyUser.trim(),
      proxy_password: proxyPass.trim()
    };

    showMsg(document.getElementById('metaSocialPageMsg'), '正在启动 Chromium 打开授权页…', false);
    fetch(local + '/api/meta-social-local/oauth/open-chromium', {
      method: 'POST',
      headers: hdrs(),
      body: JSON.stringify(body)
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (x) {
        if (!x.ok) {
          showMsg(document.getElementById('metaSocialPageMsg'), '启动失败: ' + (x.d.detail || JSON.stringify(x.d)), true);
          return;
        }
        if (x.d.chromium_opened) {
          showMsg(document.getElementById('metaSocialPageMsg'), '已在 Chromium 中打开 Facebook 授权页（带代理）。授权完成后请点击「刷新列表」。', false);
        } else {
          var msg = 'Chromium 启动失败';
          if (x.d.chromium_message) msg += ': ' + x.d.chromium_message;
          if (x.d.login_url) {
            msg += '。已回退为浏览器打开。';
            window.open(x.d.login_url, '_blank');
          }
          showMsg(document.getElementById('metaSocialPageMsg'), msg, !x.d.login_url);
        }
      })
      .catch(function (e) {
        showMsg(document.getElementById('metaSocialPageMsg'), '启动失败: ' + e.message, true);
      });
  }

  // ── Page init ──

  function loadMetaSocialPage() {
    _loadDraft();
    _bindDraftAutoSave();
    renderAccountList();
    loadRedirectUri();

    var connectBtn = document.getElementById('metaSocialConnectBtn');
    if (connectBtn && !connectBtn._bound) {
      connectBtn._bound = true;
      connectBtn.addEventListener('click', startOAuthWithCredentials);
    }
    var refreshBtn = document.getElementById('metaSocialRefreshBtn');
    if (refreshBtn && !refreshBtn._bound) {
      refreshBtn._bound = true;
      refreshBtn.addEventListener('click', renderAccountList);
    }
    var backBtn = document.getElementById('metaSocialBackBtn');
    if (backBtn && !backBtn._bound) {
      backBtn._bound = true;
      backBtn.addEventListener('click', function () {
        if (typeof _switchToHiddenView === 'function') {
          location.hash = '';
          document.querySelectorAll('.nav-left-item').forEach(function (b) { b.classList.remove('active'); });
          document.querySelectorAll('.content-block').forEach(function (p) { p.classList.remove('visible'); });
          var skillEl = document.getElementById('content-skill-store');
          if (skillEl) skillEl.classList.add('visible');
          var navItem = document.querySelector('.nav-left-item[data-view="skill-store"]');
          if (navItem) navItem.classList.add('active');
        }
      });
    }

    var proxyBrowserBtn = document.getElementById('metaOpenProxyBrowserBtn');
    if (proxyBrowserBtn && !proxyBrowserBtn._bound) {
      proxyBrowserBtn._bound = true;
      proxyBrowserBtn.addEventListener('click', function () {
        var local = localBase();
        if (!local) { alert('未配置本地服务器地址'); return; }
        var proxyServer = _buildProxyServer();
        var proxyUser = (document.getElementById('metaProxyUserInput') || {}).value || '';
        var proxyPass = (document.getElementById('metaProxyPassInput') || {}).value || '';
        _saveDraft();
        showMsg(document.getElementById('metaSocialPageMsg'), '正在启动代理浏览器…', false);
        proxyBrowserBtn.disabled = true;
        fetch(local + '/api/meta-social-local/open-proxy-browser', {
          method: 'POST',
          headers: hdrs(),
          body: JSON.stringify({
            proxy_server: proxyServer.trim(),
            proxy_username: proxyUser.trim(),
            proxy_password: proxyPass.trim()
          })
        })
          .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
          .then(function (x) {
            proxyBrowserBtn.disabled = false;
            if (!x.ok) {
              showMsg(document.getElementById('metaSocialPageMsg'), '启动失败: ' + (x.d.detail || JSON.stringify(x.d)), true);
              return;
            }
            if (x.d.chromium_opened) {
              showMsg(document.getElementById('metaSocialPageMsg'), '已打开代理浏览器（Facebook 开发者页面）。你可以在里面创建 App、配置权限等，所有操作都走同一 IP。', false);
            } else {
              showMsg(document.getElementById('metaSocialPageMsg'), '浏览器启动失败: ' + (x.d.chromium_message || '未知错误'), true);
            }
          })
          .catch(function (e) {
            proxyBrowserBtn.disabled = false;
            showMsg(document.getElementById('metaSocialPageMsg'), '启动失败: ' + e.message, true);
          });
      });
    }

    // toggle password visibility
    var toggleBtn = document.getElementById('metaAppSecretToggle');
    if (toggleBtn && !toggleBtn._bound) {
      toggleBtn._bound = true;
      toggleBtn.addEventListener('click', function () {
        var inp = document.getElementById('metaAppSecretInput');
        if (!inp) return;
        inp.type = inp.type === 'password' ? 'text' : 'password';
        toggleBtn.textContent = inp.type === 'password' ? '显示' : '隐藏';
      });
    }

    loadMetaSocialDataView();
    loadMetaSocialSchedules();
  }

  function loadMetaSocialDataView() {
    var dataEl = document.getElementById('metaSocialDataView');
    if (!dataEl) return;
    var base = serverBase();
    if (!base) return;
    dataEl.innerHTML = '<p class="meta">加载数据概览…</p>';
    fetch(base + '/api/meta-social/data', { headers: hdrs() })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var entries = (d && d.data) ? d.data : [];
        if (entries.length === 0) {
          dataEl.innerHTML = '<p class="meta">暂无数据。连接账号并同步后即可查看。</p>';
          return;
        }
        var html = '';
        entries.forEach(function (entry) {
          var acct = entry.account || {};
          var posts = entry.posts || [];
          var metrics = entry.account_metrics || {};
          var label = acct.label || acct.username || acct.page_name || '';
          var plat = acct.platform || '';
          html += '<div style="margin-bottom:1rem;padding:0.75rem;border:1px solid var(--border);border-radius:var(--radius);background:rgba(255,255,255,0.03);">';
          html += '<div style="font-weight:600;margin-bottom:0.35rem;">' + platformBadge(plat) + ' ' + escapeH(label) + '</div>';
          if (metrics.followers_count !== undefined) {
            html += '<div style="font-size:0.82rem;color:var(--text-muted);">粉丝: ' + (metrics.followers_count || 0) + ' · 帖子: ' + posts.length + '</div>';
          }
          if (posts.length > 0) {
            html += '<div style="margin-top:0.5rem;font-size:0.8rem;max-height:12rem;overflow-y:auto;">';
            html += '<table style="width:100%;border-collapse:collapse;font-size:0.78rem;"><thead><tr style="border-bottom:1px solid var(--border);text-align:left;"><th style="padding:0.25rem 0.5rem;">时间</th><th style="padding:0.25rem 0.5rem;">类型</th><th style="padding:0.25rem 0.5rem;">赞</th><th style="padding:0.25rem 0.5rem;">评论</th></tr></thead><tbody>';
            posts.slice(0, 20).forEach(function (p) {
              var ts = p.timestamp || p.created_time || '';
              var typ = p.media_type || p.type || '';
              var likes = p.like_count || p.likes || 0;
              var comments = p.comments_count || p.comments || 0;
              html += '<tr style="border-bottom:1px solid rgba(255,255,255,0.05);"><td style="padding:0.2rem 0.5rem;">' + escapeH(ts).slice(0, 16) + '</td><td style="padding:0.2rem 0.5rem;">' + escapeH(typ) + '</td><td style="padding:0.2rem 0.5rem;">' + likes + '</td><td style="padding:0.2rem 0.5rem;">' + comments + '</td></tr>';
            });
            html += '</tbody></table></div>';
          }
          html += '</div>';
        });
        dataEl.innerHTML = html;
      })
      .catch(function () {
        dataEl.innerHTML = '<p class="msg err">加载数据失败</p>';
      });
  }

  // ── 定时发布 ──

  function loadMetaSocialSchedules() {
    var el = document.getElementById('metaSocialSchedulesView');
    if (!el) return;
    var base = serverBase();
    if (!base) return;
    el.innerHTML = '<p class="meta">加载定时发布…</p>';
    fetch(base + '/api/meta-social/schedules', { headers: hdrs() })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (x) {
        if (!x.ok) { el.innerHTML = '<p class="msg err">加载失败</p>'; return; }
        var rows = Array.isArray(x.d) ? x.d : [];
        if (rows.length === 0 && _accounts.length === 0) {
          el.innerHTML = '<p class="meta">请先连接账号，然后可以设置定时发布计划。</p>';
          return;
        }
        var html = '<div style="display:flex;flex-wrap:wrap;gap:0.75rem;">';
        rows.forEach(function (s) {
          var acct = _accounts.filter(function (a) { return a.id === s.meta_account_id; })[0];
          var acctLabel = acct ? (acct.label || acct.facebook_page_name || '账号 #' + acct.id) : '账号 #' + s.meta_account_id;
          html += '<div style="flex:1;min-width:280px;padding:0.75rem;border:1px solid var(--border);border-radius:var(--radius);background:rgba(255,255,255,0.02);">';
          html += '<div style="font-weight:600;margin-bottom:0.35rem;">' + platformBadge(s.platform) + ' ' + escapeH(acctLabel) + '</div>';
          html += '<div style="font-size:0.82rem;color:var(--text-muted);">';
          html += '类型: ' + escapeH(s.content_type) + ' · 间隔: ' + s.interval_minutes + '分钟';
          html += ' · 状态: ' + (s.enabled ? '<span style="color:#22c55e;">启用</span>' : '<span style="color:#94a3b8;">暂停</span>');
          html += '</div>';
          html += '<div style="font-size:0.78rem;color:var(--text-muted);margin-top:0.25rem;">';
          html += '队列: ' + ((s.asset_ids || []).length) + ' 个素材';
          if (s.next_run_at) html += ' · 下次: ' + escapeH(s.next_run_at).slice(0, 16);
          if (s.last_run_error) html += '<br><span style="color:#fb7185;">上次错误: ' + escapeH(s.last_run_error).slice(0, 80) + '</span>';
          html += '</div>';
          html += '<div style="margin-top:0.5rem;display:flex;gap:0.35rem;">';
          html += '<button type="button" class="btn btn-ghost btn-sm meta-sch-toggle" data-id="' + s.id + '" data-acct="' + s.meta_account_id + '" data-platform="' + s.platform + '" data-enabled="' + (s.enabled ? '1' : '0') + '" data-interval="' + s.interval_minutes + '" data-ct="' + s.content_type + '">' + (s.enabled ? '暂停' : '启用') + '</button>';
          html += '</div></div>';
        });
        html += '</div>';
        if (_accounts.length > 0) {
          html += '<div style="margin-top:0.75rem;">';
          html += '<button type="button" class="btn btn-ghost btn-sm" id="metaSchAddBtn">+ 新建定时计划</button>';
          html += '</div>';
        }
        el.innerHTML = html;
        bindScheduleActions();
      })
      .catch(function () { el.innerHTML = '<p class="msg err">加载定时发布失败</p>'; });
  }

  function bindScheduleActions() {
    document.querySelectorAll('.meta-sch-toggle').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var enabled = btn.getAttribute('data-enabled') === '1';
        var body = {
          meta_account_id: parseInt(btn.getAttribute('data-acct'), 10),
          platform: btn.getAttribute('data-platform'),
          content_type: btn.getAttribute('data-ct'),
          interval_minutes: parseInt(btn.getAttribute('data-interval'), 10) || 60,
          enabled: !enabled,
          asset_ids: [],
          caption: ''
        };
        btn.disabled = true;
        fetch(serverBase() + '/api/meta-social/schedules', {
          method: 'PUT', headers: hdrs(), body: JSON.stringify(body)
        })
          .then(function () { loadMetaSocialSchedules(); })
          .catch(function (e) { alert('操作失败: ' + e.message); btn.disabled = false; });
      });
    });

    var addBtn = document.getElementById('metaSchAddBtn');
    if (addBtn) {
      addBtn.addEventListener('click', function () {
        if (_accounts.length === 0) { alert('请先连接账号'); return; }
        var acctId = _accounts[0].id;
        if (_accounts.length > 1) {
          var names = _accounts.map(function (a, i) { return (i + 1) + '. ' + (a.label || a.facebook_page_name || '账号 #' + a.id); }).join('\n');
          var choice = prompt('选择账号序号:\n' + names, '1');
          if (!choice) return;
          var idx = parseInt(choice, 10) - 1;
          if (idx >= 0 && idx < _accounts.length) acctId = _accounts[idx].id;
        }
        var platform = prompt('平台 (instagram / facebook):', 'instagram');
        if (!platform) return;
        var ct = prompt('内容类型 (photo / video / reel / story / carousel / link):', 'photo');
        if (!ct) return;
        var interval = parseInt(prompt('发布间隔（分钟）:', '60'), 10) || 60;
        fetch(serverBase() + '/api/meta-social/schedules', {
          method: 'PUT', headers: hdrs(),
          body: JSON.stringify({ meta_account_id: acctId, platform: platform, content_type: ct, interval_minutes: interval, enabled: false, asset_ids: [], caption: '' })
        })
          .then(function () { loadMetaSocialSchedules(); })
          .catch(function (e) { alert('创建失败: ' + e.message); });
      });
    }
  }

  window.loadMetaSocialPage = loadMetaSocialPage;

  window._metaSocialStatus = { accounts_count: 0 };
  window._loadMetaSocialStatus = function (cb) {
    var base = serverBase();
    if (!base) { if (cb) cb(); return; }
    fetch(base + '/api/meta-social/accounts', { headers: typeof authHeaders === 'function' ? authHeaders() : {} })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var rows = Array.isArray(d) ? d : (d && Array.isArray(d.accounts) ? d.accounts : []);
        window._metaSocialStatus = { accounts_count: rows.length };
        if (cb) cb();
      })
      .catch(function () {
        window._metaSocialStatus = { accounts_count: 0 };
        if (cb) cb();
      });
  };

  window._openMetaSocialView = function () {
    if (typeof _switchToHiddenView === 'function') {
      _switchToHiddenView('meta-social');
    } else {
      location.hash = 'meta-social';
      document.querySelectorAll('.nav-left-item').forEach(function (b) { b.classList.remove('active'); });
      document.querySelectorAll('.content-block').forEach(function (p) { p.classList.remove('visible'); });
      var el = document.getElementById('content-meta-social');
      if (el) el.classList.add('visible');
    }
    if (typeof loadMetaSocialPage === 'function') loadMetaSocialPage();
  };
})();
