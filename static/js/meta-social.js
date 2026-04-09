/**
 * Meta Social (Instagram / Facebook) 账号管理页。
 * OAuth 授权在服务器端完成（需要公网回调），本页通过 AUTH_SERVER_BASE 代理所有 API 调用。
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
          listEl.innerHTML = '<p class="meta">暂无已连接账号。点击「连接 Instagram / Facebook」通过 Facebook OAuth 授权。</p>';
          return;
        }
        var html = '<div class="skill-store-grid" style="gap:0.75rem;">';
        rows.forEach(function (a) {
          var label = a.label || a.username || a.page_name || ('账号 #' + a.id);
          var plat = a.platform || 'unknown';
          var igUser = a.ig_username ? ('@' + a.ig_username) : '';
          var pageName = a.page_name || '';
          var detail = plat === 'instagram'
            ? (igUser || pageName)
            : pageName;
          html += '<div class="skill-store-card" style="border-color:' + (plat === 'instagram' ? 'rgba(225,48,108,0.35)' : 'rgba(24,119,242,0.35)') + ';background:linear-gradient(135deg,' + (plat === 'instagram' ? 'rgba(225,48,108,0.06)' : 'rgba(24,119,242,0.06)') + ',transparent);">';
          html += '<div class="card-label">' + platformBadge(plat) + ' <span class="badge-installed">已连接</span></div>';
          html += '<div class="card-value">' + escapeH(label) + '</div>';
          if (detail) html += '<div class="card-desc">' + escapeH(detail) + '</div>';
          if (a.proxy_url) html += '<div style="font-size:0.75rem;color:var(--text-muted);margin-top:0.25rem;">代理: ' + escapeH(a.proxy_url) + '</div>';
          html += '<div class="card-actions" style="display:flex;gap:0.35rem;flex-wrap:wrap;">';
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

  function startOAuth() {
    var base = serverBase();
    if (!base) { alert('未配置服务器地址'); return; }
    var token = '';
    try { token = typeof authHeaders === 'function' ? (authHeaders()['Authorization'] || '').replace('Bearer ', '') : ''; } catch (e) {}
    var url = base + '/api/meta-social/oauth/start';
    if (token) url += '?token=' + encodeURIComponent(token);
    window.open(url, '_blank');
    var msgEl = document.getElementById('metaSocialPageMsg');
    showMsg(msgEl, '已打开 Facebook 授权页面。授权完成后请点击「刷新列表」。', false);
  }

  function loadMetaSocialPage() {
    renderAccountList();
    var addBtn = document.getElementById('metaSocialAddBtn');
    if (addBtn && !addBtn._bound) {
      addBtn._bound = true;
      addBtn.addEventListener('click', startOAuth);
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
