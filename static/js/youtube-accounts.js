/**
 * YouTube 多账号：列表页 + 添加弹窗（Client ID/Secret、代理）。
 * 授权默认由后端用与「发布-打开浏览器」相同的 Playwright 持久化 Chromium 打开；失败时回退 window.open。
 */
(function() {
  function localApiBase() {
    return (typeof LOCAL_API_BASE !== 'undefined' ? LOCAL_API_BASE : '') || '';
  }

  function apiUrl(path) {
    var base = localApiBase().replace(/\/$/, '');
    return (base ? base : '') + path;
  }

  function hdrs() {
    return Object.assign({ 'Content-Type': 'application/json' }, typeof authHeaders === 'function' ? authHeaders() : {});
  }

  function showMsg(el, text, isErr) {
    if (!el) return;
    el.textContent = text || '';
    el.className = 'msg' + (isErr ? ' err' : '');
    el.style.display = text ? 'block' : 'none';
  }

  var _lastAccounts = [];
  var _youtubeModalMode = 'add';
  var _youtubeEditAid = '';
  var _youtubeScheduleAid = '';

  function statusLabel(st) {
    if (st === 'ready') return '<span class="badge-installed">可用</span>';
    if (st === 'error') return '<span class="badge-coming" style="background:rgba(239,68,68,0.15);color:#fb7185;">异常</span>';
    return '<span class="badge-coming" style="background:rgba(251,146,60,0.15);color:#fb923c;">待授权</span>';
  }

  function renderList() {
    var listEl = document.getElementById('youtubeAccountsList');
    var testSel = document.getElementById('youtubeAccountsTestSelect');
    if (!listEl) return;
    listEl.innerHTML = '<p class="meta">加载中…</p>';
    if (testSel) testSel.innerHTML = '<option value="">—</option>';
    fetch(apiUrl('/api/youtube-publish/accounts'), { headers: typeof authHeaders === 'function' ? authHeaders() : {} })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d, status: r.status }; }); })
      .then(function(x) {
        if (!listEl) return;
        if (!x.ok) {
          if (x.status === 403) {
            listEl.innerHTML = '<p class="msg err">当前账号无权限：YouTube 上传仅技能商店管理员可使用。</p>';
            return;
          }
          listEl.innerHTML = '<p class="msg err">加载失败 ' + (x.status || '') + '</p>';
          return;
        }
        var rows = Array.isArray(x.d) ? x.d : [];
        _lastAccounts = rows;
        if (rows.length === 0) {
          listEl.innerHTML = '<p class="meta">暂无账号，点击「添加账号」填写 Google OAuth 客户端与代理（可选）。</p>';
          return;
        }
        if (testSel) {
          testSel.innerHTML = '<option value="">选择账号</option>' +
            rows.map(function(a) {
              return '<option value="' + escapeAttr(a.account_id || '') + '">' + escapeHtml(a.account_id || '') +
                (a.label ? (' · ' + escapeHtml(a.label)) : '') + '</option>';
            }).join('');
        }
        listEl.innerHTML = rows.map(function(a) {
          var id = escapeHtml(a.account_id || '');
          var lab = (a.label || '').trim() ? (' · ' + escapeHtml(a.label)) : '';
          var err = (a.last_error || '').trim() ? ('<div class="err" style="font-size:0.78rem;margin-top:0.35rem;">' + escapeHtml(a.last_error) + '</div>') : '';
          return '<div class="config-block-item" style="margin-bottom:0.65rem;">' +
            '<div class="block-header" style="align-items:flex-start;">' +
            '<div><span class="block-name">' + id + lab + '</span> ' + statusLabel(a.status) +
            '<div style="font-size:0.78rem;color:var(--text-muted);margin-top:0.35rem;">Client: ' + escapeHtml(a.oauth_client_id_masked || '-') +
            ' · 代理: ' + escapeHtml(a.proxy_server_masked || '无') + '</div>' + err + '</div>' +
            '<div style="display:flex;flex-wrap:wrap;gap:0.35rem;">' +
            '<button type="button" class="btn btn-ghost btn-sm yt-edit-btn" data-aid="' + escapeAttr(a.account_id || '') + '">编辑</button>' +
            '<button type="button" class="btn btn-ghost btn-sm yt-analytics-btn" data-aid="' + escapeAttr(a.account_id || '') + '">频道数据</button>' +
            '<button type="button" class="btn btn-ghost btn-sm yt-sched-btn" data-aid="' + escapeAttr(a.account_id || '') + '">定时发布</button>' +
            '<button type="button" class="btn btn-primary btn-sm yt-oauth-btn" data-aid="' + escapeAttr(a.account_id || '') + '">浏览器授权</button>' +
            '<button type="button" class="btn btn-ghost btn-sm yt-del-btn" data-aid="' + escapeAttr(a.account_id || '') + '">删除</button>' +
            '</div></div></div>';
        }).join('');
        listEl.querySelectorAll('.yt-edit-btn').forEach(function(btn) {
          btn.addEventListener('click', function() { openEditModal(btn.getAttribute('data-aid')); });
        });
        listEl.querySelectorAll('.yt-oauth-btn').forEach(function(btn) {
          btn.addEventListener('click', function() { startOauth(btn.getAttribute('data-aid')); });
        });
        listEl.querySelectorAll('.yt-del-btn').forEach(function(btn) {
          btn.addEventListener('click', function() { delAccount(btn.getAttribute('data-aid')); });
        });
        listEl.querySelectorAll('.yt-analytics-btn').forEach(function(btn) {
          btn.addEventListener('click', function() { showAnalytics(btn.getAttribute('data-aid'), btn); });
        });
        listEl.querySelectorAll('.yt-sched-btn').forEach(function(btn) {
          btn.addEventListener('click', function() { openScheduleModal(btn.getAttribute('data-aid')); });
        });
      })
      .catch(function(err) {
        if (listEl) listEl.innerHTML = '<p class="msg err">' + escapeHtml((err && err.message) ? err.message : '加载失败') + '</p>';
      });
  }

  function showAnalytics(accountId, triggerBtn) {
    var aid = (accountId || '').trim();
    if (!aid) return;
    var existing = document.getElementById('yt-analytics-' + aid);
    if (existing) { existing.remove(); return; }
    var card = triggerBtn ? triggerBtn.closest('.config-block-item') : null;
    if (!card) return;
    var panel = document.createElement('div');
    panel.id = 'yt-analytics-' + aid;
    panel.style.cssText = 'margin:0.5rem 0 0.75rem;padding:0.75rem;border:1px solid var(--border);border-radius:var(--radius-sm);background:rgba(255,255,255,0.03);font-size:0.82rem;';
    panel.innerHTML = '<p class="meta">加载频道数据…</p>';
    card.appendChild(panel);
    fetch(apiUrl('/api/youtube-publish/accounts/' + encodeURIComponent(aid) + '/analytics'), { headers: hdrs() })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d, status: r.status }; }); })
      .then(function(x) {
        if (!x.ok) {
          panel.innerHTML = '<p class="msg err">加载失败: ' + escapeHtml((x.d && x.d.detail) || ('HTTP ' + x.status)) + '</p>';
          return;
        }
        var d = x.d;
        var ch = d.channel_analytics || {};
        var videos = d.videos || [];
        var html = '<div style="margin-bottom:0.5rem;font-weight:600;">频道分析（近 28 天）</div>';
        if (ch.error) {
          html += '<p class="msg err" style="font-size:0.78rem;">' + escapeHtml(ch.error) + '</p>';
        } else if (Object.keys(ch).length > 0) {
          html += '<div style="display:flex;flex-wrap:wrap;gap:0.75rem;margin-bottom:0.5rem;">';
          if (ch.views !== undefined) html += '<div><div style="font-size:0.75rem;color:var(--text-muted);">播放</div><div style="font-size:1.1rem;font-weight:600;">' + ch.views + '</div></div>';
          if (ch.likes !== undefined) html += '<div><div style="font-size:0.75rem;color:var(--text-muted);">赞</div><div style="font-size:1.1rem;font-weight:600;">' + ch.likes + '</div></div>';
          if (ch.subscribersGained !== undefined) html += '<div><div style="font-size:0.75rem;color:var(--text-muted);">新增订阅</div><div style="font-size:1.1rem;font-weight:600;">' + ch.subscribersGained + '</div></div>';
          if (ch.estimatedMinutesWatched !== undefined) html += '<div><div style="font-size:0.75rem;color:var(--text-muted);">观看时长(分)</div><div style="font-size:1.1rem;font-weight:600;">' + ch.estimatedMinutesWatched + '</div></div>';
          html += '</div>';
        }
        if (videos.length > 0) {
          html += '<div style="margin-top:0.5rem;font-weight:600;">近期视频（' + videos.length + '）</div>';
          html += '<div style="max-height:16rem;overflow-y:auto;margin-top:0.35rem;"><table style="width:100%;border-collapse:collapse;font-size:0.78rem;"><thead><tr style="border-bottom:1px solid var(--border);text-align:left;"><th style="padding:0.25rem 0.5rem;">标题</th><th style="padding:0.25rem 0.5rem;">发布时间</th><th style="padding:0.25rem 0.5rem;">播放</th><th style="padding:0.25rem 0.5rem;">赞</th><th style="padding:0.25rem 0.5rem;">评论</th></tr></thead><tbody>';
          videos.slice(0, 30).forEach(function(v) {
            html += '<tr style="border-bottom:1px solid rgba(255,255,255,0.05);"><td style="padding:0.2rem 0.5rem;max-width:16rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + escapeHtml(v.title || '') + '</td><td style="padding:0.2rem 0.5rem;">' + escapeHtml((v.published_at || '').slice(0, 10)) + '</td><td style="padding:0.2rem 0.5rem;">' + (v.views || 0) + '</td><td style="padding:0.2rem 0.5rem;">' + (v.likes || 0) + '</td><td style="padding:0.2rem 0.5rem;">' + (v.comments || 0) + '</td></tr>';
          });
          html += '</tbody></table></div>';
        }
        html += '<div style="margin-top:0.5rem;"><button type="button" class="btn btn-ghost btn-sm yt-analytics-close" style="font-size:0.75rem;">收起</button></div>';
        panel.innerHTML = html;
        panel.querySelector('.yt-analytics-close').addEventListener('click', function() { panel.remove(); });
      })
      .catch(function(e) {
        panel.innerHTML = '<p class="msg err">' + escapeHtml(e.message || '请求失败') + '</p>';
      });
  }

  function startOauth(accountId) {
    var aid = (accountId || '').trim();
    if (!aid) return;
    fetch(apiUrl('/api/youtube-publish/accounts/' + encodeURIComponent(aid) + '/oauth/start'), {
      method: 'POST',
      headers: hdrs(),
      body: JSON.stringify({ open_chromium: true })
    })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d, status: r.status }; }); })
      .then(function(x) {
        if (!x.ok) {
          alert((x.d && x.d.detail) ? x.d.detail : ('请求失败 HTTP ' + (x.status || '')));
          return;
        }
        var u = (x.d && x.d.url) ? String(x.d.url) : '';
        var opened = !!(x.d && x.d.chromium_opened);
        var cmsg = (x.d && x.d.chromium_message) ? String(x.d.chromium_message) : '';
        if (opened) {
          return;
        }
        if (u) {
          window.open(u, '_blank', 'noopener,noreferrer');
          if (cmsg) alert('内置 Chromium 未启动，已改用系统浏览器。' + cmsg);
        } else if (cmsg) {
          alert(cmsg);
        }
      })
      .catch(function(e) { alert(e && e.message ? e.message : '请求失败'); });
  }

  function closeScheduleModal() {
    var m = document.getElementById('youtubeScheduleModal');
    if (m) m.classList.remove('visible');
    _youtubeScheduleAid = '';
  }

  function parseScheduleAssetIds(text) {
    var lines = String(text || '').split(/[\n\r,]+/);
    var out = [];
    for (var i = 0; i < lines.length; i++) {
      var s = (lines[i] || '').trim();
      if (s && out.indexOf(s) === -1) out.push(s);
    }
    return out;
  }

  function openScheduleModal(accountId) {
    var aid = (accountId || '').trim();
    if (!aid) return;
    _youtubeScheduleAid = aid;
    var m = document.getElementById('youtubeScheduleModal');
    var title = document.getElementById('youtubeScheduleModalTitle');
    var smsg = document.getElementById('youtubeSchedMsg');
    var meta = document.getElementById('youtubeSchedMeta');
    if (title) title.textContent = '定时发布 · ' + aid;
    if (smsg) { smsg.style.display = 'none'; smsg.textContent = ''; }
    if (meta) meta.textContent = '加载中…';
    if (m) m.classList.add('visible');
    fetch(apiUrl('/api/youtube-publish/accounts/' + encodeURIComponent(aid) + '/publish-schedule'), { headers: hdrs() })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d, status: r.status }; }); })
      .then(function(x) {
        if (!x.ok) {
          if (meta) meta.textContent = '';
          showMsg(smsg, (x.d && x.d.detail) ? x.d.detail : '加载失败', true);
          return;
        }
        var d = x.d || {};
        var en = document.getElementById('youtubeSchedEnabled');
        var iv = document.getElementById('youtubeSchedInterval');
        var ta = document.getElementById('youtubeSchedAssetIds');
        var mo = document.getElementById('youtubeSchedMaterialOrigin');
        var pr = document.getElementById('youtubeSchedPrivacy');
        var cat = document.getElementById('youtubeSchedCategory');
        var ti = document.getElementById('youtubeSchedTitle');
        var ds = document.getElementById('youtubeSchedDesc');
        if (en) en.checked = !!d.enabled;
        if (iv) iv.value = d.interval_minutes != null ? d.interval_minutes : 60;
        if (ta) ta.value = Array.isArray(d.asset_ids) ? d.asset_ids.join('\n') : '';
        if (mo) mo.value = (d.material_origin === 'ai_generated') ? 'ai_generated' : 'script_batch';
        if (pr) pr.value = d.privacy_status || 'public';
        if (cat) cat.value = d.category_id || '22';
        if (ti) ti.value = d.title || '';
        if (ds) ds.value = d.description || '';
        var metaStr = '';
        if (d.next_run_at) metaStr += '下次（UTC）：' + d.next_run_at;
        if (d.last_run_at) metaStr += (metaStr ? ' · ' : '') + '上次：' + d.last_run_at;
        if (d.last_video_id) metaStr += ' · video_id：' + d.last_video_id;
        if (d.last_run_error) metaStr += ' · 错误：' + d.last_run_error;
        if (meta) meta.textContent = metaStr || '—';
      })
      .catch(function(e) {
        if (meta) meta.textContent = '';
        showMsg(smsg, e && e.message ? e.message : '加载失败', true);
      });
  }

  function saveYoutubeSchedule() {
    var aid = (_youtubeScheduleAid || '').trim();
    var smsg = document.getElementById('youtubeSchedMsg');
    var btn = document.getElementById('youtubeSchedSaveBtn');
    if (!aid) return;
    var en = document.getElementById('youtubeSchedEnabled');
    var iv = document.getElementById('youtubeSchedInterval');
    var ta = document.getElementById('youtubeSchedAssetIds');
    var mo = document.getElementById('youtubeSchedMaterialOrigin');
    var pr = document.getElementById('youtubeSchedPrivacy');
    var cat = document.getElementById('youtubeSchedCategory');
    var ti = document.getElementById('youtubeSchedTitle');
    var ds = document.getElementById('youtubeSchedDesc');
    var body = {
      enabled: en && en.checked,
      interval_minutes: iv ? parseInt(iv.value, 10) || 60 : 60,
      asset_ids: parseScheduleAssetIds(ta ? ta.value : ''),
      material_origin: (mo && mo.value === 'ai_generated') ? 'ai_generated' : 'script_batch',
      privacy_status: pr ? pr.value : 'public',
      category_id: cat ? (cat.value || '22').trim() : '22',
      title: ti ? ti.value.trim() : '',
      description: ds ? ds.value.trim() : ''
    };
    if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
    showMsg(smsg, '', false);
    fetch(apiUrl('/api/youtube-publish/accounts/' + encodeURIComponent(aid) + '/publish-schedule'), {
      method: 'PUT',
      headers: hdrs(),
      body: JSON.stringify(body)
    })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d, status: r.status }; }); })
      .then(function(x) {
        if (!x.ok) {
          showMsg(smsg, (x.d && x.d.detail) ? x.d.detail : ('保存失败 HTTP ' + (x.status || '')), true);
          return;
        }
        var d = x.d || {};
        var meta = document.getElementById('youtubeSchedMeta');
        var metaStr = '';
        if (d.next_run_at) metaStr += '下次（UTC）：' + d.next_run_at;
        if (d.last_run_at) metaStr += (metaStr ? ' · ' : '') + '上次：' + d.last_run_at;
        if (d.last_video_id) metaStr += ' · video_id：' + d.last_video_id;
        if (d.last_run_error) metaStr += ' · 错误：' + d.last_run_error;
        if (meta) meta.textContent = metaStr || '—';
        showMsg(smsg, '已保存', false);
      })
      .catch(function(e) { showMsg(smsg, e && e.message ? e.message : '保存失败', true); })
      .finally(function() {
        if (btn) { btn.disabled = false; btn.textContent = '保存'; }
      });
  }

  function delAccount(accountId) {
    var aid = (accountId || '').trim();
    if (!aid) return;
    if (!confirm('确定删除账号 ' + aid + '？')) return;
    fetch(apiUrl('/api/youtube-publish/accounts/' + encodeURIComponent(aid)), { method: 'DELETE', headers: hdrs() })
      .then(function(r) {
        if (!r.ok) return r.json().then(function(d) { throw new Error((d && d.detail) || ('HTTP ' + r.status)); });
        renderList();
        if (typeof loadSkillStore === 'function') loadSkillStore();
      })
      .catch(function(e) { alert(e.message || '删除失败'); });
  }

  function closeAddModal() {
    var m = document.getElementById('youtubeAccountAddModal');
    if (m) m.classList.remove('visible');
  }

  function _setModalUi(mode) {
    var titleEl = document.getElementById('youtubeAccountModalTitle');
    var hintEl = document.getElementById('youtubeAccountModalHint');
    var saveBtn = document.getElementById('youtubeAccountAddSaveBtn');
    if (titleEl) titleEl.textContent = mode === 'edit' ? '编辑 YouTube 账号' : '添加 YouTube 账号';
    if (hintEl) {
      hintEl.textContent = mode === 'edit'
        ? '可补填代理、修改 Client ID/Secret（Secret 留空则不修改）。若更换 OAuth 客户端，请保存后再点「浏览器授权」。'
        : '保存后将生成账号 ID（yt_ 开头），并自动打开浏览器向 Google 授权。';
    }
    if (saveBtn) saveBtn.textContent = mode === 'edit' ? '保存' : '保存并授权';
  }

  function openAddModal() {
    _youtubeModalMode = 'add';
    _youtubeEditAid = '';
    _setModalUi('add');
    var m = document.getElementById('youtubeAccountAddModal');
    var msg = document.getElementById('youtubeAccountAddMsg');
    if (msg) { msg.style.display = 'none'; msg.textContent = ''; }
    var ids = ['youtubeAddLabel', 'youtubeAddClientId', 'youtubeAddClientSecret', 'youtubeAddProxy', 'youtubeAddProxyUser', 'youtubeAddProxyPass'];
    ids.forEach(function(id) {
      var el = document.getElementById(id);
      if (el) el.value = '';
    });
    if (m) m.classList.add('visible');
  }

  function openEditModal(accountId) {
    var aid = (accountId || '').trim();
    if (!aid) return;
    var row = _lastAccounts.filter(function(a) { return (a.account_id || '') === aid; })[0];
    if (!row) {
      alert('找不到该账号，请刷新列表后重试');
      return;
    }
    _youtubeModalMode = 'edit';
    _youtubeEditAid = aid;
    _setModalUi('edit');
    var msg = document.getElementById('youtubeAccountAddMsg');
    if (msg) { msg.style.display = 'none'; msg.textContent = ''; }
    var labelEl = document.getElementById('youtubeAddLabel');
    var cidEl = document.getElementById('youtubeAddClientId');
    var csecEl = document.getElementById('youtubeAddClientSecret');
    var psEl = document.getElementById('youtubeAddProxy');
    var puEl = document.getElementById('youtubeAddProxyUser');
    var ppEl = document.getElementById('youtubeAddProxyPass');
    if (labelEl) labelEl.value = (row.label || '').trim();
    if (cidEl) cidEl.value = (row.oauth_client_id || '').trim();
    if (csecEl) csecEl.value = '';
    if (psEl) psEl.value = (row.proxy_server || '').trim();
    if (puEl) puEl.value = (row.proxy_username || '').trim();
    if (ppEl) ppEl.value = '';
    var m = document.getElementById('youtubeAccountAddModal');
    if (m) m.classList.add('visible');
  }

  function saveNewAccount() {
    var msg = document.getElementById('youtubeAccountAddMsg');
    var label = (document.getElementById('youtubeAddLabel') || {}).value || '';
    var cid = (document.getElementById('youtubeAddClientId') || {}).value || '';
    var csec = (document.getElementById('youtubeAddClientSecret') || {}).value || '';
    var ps = (document.getElementById('youtubeAddProxy') || {}).value || '';
    var pu = (document.getElementById('youtubeAddProxyUser') || {}).value || '';
    var pp = (document.getElementById('youtubeAddProxyPass') || {}).value || '';
    if (!cid.trim()) {
      showMsg(msg, '请填写 OAuth Client ID', true);
      return;
    }
    if (_youtubeModalMode === 'edit') {
      if (!_youtubeEditAid) {
        showMsg(msg, '内部错误：缺少 account_id', true);
        return;
      }
      var body = {
        label: label.trim(),
        oauth_client_id: cid.trim(),
        proxy_server: ps.trim(),
        proxy_username: pu.trim()
      };
      if (csec.trim()) body.oauth_client_secret = csec.trim();
      if (pp.trim()) body.proxy_password = pp.trim();
      var btn = document.getElementById('youtubeAccountAddSaveBtn');
      if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
      fetch(apiUrl('/api/youtube-publish/accounts/' + encodeURIComponent(_youtubeEditAid)), {
        method: 'PATCH',
        headers: hdrs(),
        body: JSON.stringify(body)
      })
        .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d, status: r.status }; }); })
        .then(function(x) {
          if (!x.ok) {
            showMsg(msg, (x.d && x.d.detail) ? x.d.detail : ('保存失败 HTTP ' + (x.status || '')), true);
            return;
          }
          closeAddModal();
          renderList();
          if (typeof loadSkillStore === 'function') loadSkillStore();
        })
        .catch(function(e) { showMsg(msg, e && e.message ? e.message : '保存失败', true); })
        .finally(function() {
          if (btn) { btn.disabled = false; btn.textContent = '保存'; }
        });
      return;
    }
    if (!csec.trim()) {
      showMsg(msg, '请填写 OAuth Client ID 与 Client Secret', true);
      return;
    }
    var btn = document.getElementById('youtubeAccountAddSaveBtn');
    if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
    fetch(apiUrl('/api/youtube-publish/accounts'), {
      method: 'POST',
      headers: hdrs(),
      body: JSON.stringify({
        label: label.trim(),
        oauth_client_id: cid.trim(),
        oauth_client_secret: csec.trim(),
        proxy_server: ps.trim(),
        proxy_username: pu.trim(),
        proxy_password: pp.trim()
      })
    })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d, status: r.status }; }); })
      .then(function(x) {
        if (!x.ok) {
          showMsg(msg, (x.d && x.d.detail) ? x.d.detail : ('保存失败 HTTP ' + (x.status || '')), true);
          return;
        }
        var newId = (x.d && x.d.account_id) ? String(x.d.account_id) : '';
        closeAddModal();
        renderList();
        if (typeof loadSkillStore === 'function') loadSkillStore();
        if (newId) startOauth(newId);
      })
      .catch(function(e) { showMsg(msg, e && e.message ? e.message : '保存失败', true); })
      .finally(function() {
        if (btn) { btn.disabled = false; btn.textContent = '保存并授权'; }
      });
  }

  window.loadYoutubeAccountsPage = function() {
    var pre = document.getElementById('youtubeAccountsRedirectPre');
    fetch(apiUrl('/api/youtube-publish/accounts'), { headers: typeof authHeaders === 'function' ? authHeaders() : {} })
      .then(function(r) { return r.json(); })
      .then(function(rows) {
        var redir = '';
        if (Array.isArray(rows) && rows.length && rows[0].oauth_redirect_uri) redir = rows[0].oauth_redirect_uri;
        if (pre && redir) pre.textContent = redir;
      })
      .catch(function() {});
    renderList();
  };

  var backBtn = document.getElementById('youtubeAccountsBackBtn');
  if (backBtn) {
    backBtn.addEventListener('click', function() {
      var nav = document.querySelector('.nav-left-item[data-view="skill-store"]');
      if (nav) nav.click();
      try { history.replaceState(null, '', location.pathname + location.search); } catch (e2) {}
    });
  }
  var addBtn = document.getElementById('youtubeAccountsAddBtn');
  if (addBtn) addBtn.addEventListener('click', openAddModal);
  var addClose = document.getElementById('youtubeAccountAddModalClose');
  if (addClose) addClose.addEventListener('click', closeAddModal);
  var addModal = document.getElementById('youtubeAccountAddModal');
  if (addModal) {
    addModal.addEventListener('click', function(e) { if (e.target === addModal) closeAddModal(); });
  }
  var addSave = document.getElementById('youtubeAccountAddSaveBtn');
  if (addSave) addSave.addEventListener('click', saveNewAccount);

  var schedClose = document.getElementById('youtubeScheduleModalClose');
  if (schedClose) schedClose.addEventListener('click', closeScheduleModal);
  var schedModal = document.getElementById('youtubeScheduleModal');
  if (schedModal) {
    schedModal.addEventListener('click', function(e) { if (e.target === schedModal) closeScheduleModal(); });
  }
  var schedSave = document.getElementById('youtubeSchedSaveBtn');
  if (schedSave) schedSave.addEventListener('click', saveYoutubeSchedule);

  var testBtn = document.getElementById('youtubeAccountsTestBtn');
  if (testBtn) {
    testBtn.addEventListener('click', function() {
      var testMsg = document.getElementById('youtubeAccountsTestMsg');
      var aidIn = document.getElementById('youtubeAccountsTestAssetId');
      var sel = document.getElementById('youtubeAccountsTestSelect');
      var titleIn = document.getElementById('youtubeAccountsTestTitle');
      var moSel = document.getElementById('youtubeAccountsTestMaterialOrigin');
      var mo = (moSel && moSel.value) ? moSel.value : 'script_batch';
      var vid = aidIn ? aidIn.value.trim() : '';
      var acc = sel ? sel.value.trim() : '';
      if (!vid) { showMsg(testMsg, '请填写素材 asset_id', true); return; }
      if (!acc) { showMsg(testMsg, '请选择 YouTube 账号', true); return; }
      testBtn.disabled = true;
      testBtn.textContent = '上传中…';
      showMsg(testMsg, '', false);
      fetch(apiUrl('/api/youtube-publish/upload'), {
        method: 'POST',
        headers: hdrs(),
        body: JSON.stringify({
          account_id: acc,
          asset_id: vid,
          title: (titleIn && titleIn.value.trim()) ? titleIn.value.trim() : 'Test upload',
          description: 'Uploaded from Lobster',
          material_origin: mo
        })
      })
        .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d, status: r.status }; }); })
        .then(function(x) {
          if (!x.ok) {
            showMsg(testMsg, (x.d && x.d.detail) ? x.d.detail : ('失败 HTTP ' + (x.status || '')), true);
            return;
          }
          var d = x.d || {};
          var url = d.watch_url || '';
          showMsg(testMsg, '成功：video_id=' + (d.video_id || '') + (url ? ' ' + url : ''), false);
        })
        .catch(function(err) { showMsg(testMsg, (err && err.message) ? err.message : '请求失败', true); })
        .finally(function() {
          testBtn.disabled = false;
          testBtn.textContent = '测试上传';
        });
    });
  }
})();
