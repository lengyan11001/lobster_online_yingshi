/**
 * Twilio WhatsApp：会话列表 + 聊天记录（参考 wecom-detail.js，走本机 LOCAL_API_BASE）。
 */
(function() {
  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /** 无正文时按 msg_type 占位（媒体入站 Body 常为空） */
  function twilioMessageBodyForDisplay(m) {
    var raw = (m.content || '').trim();
    if (raw) return raw;
    var mt = String(m.msg_type || 'text').toLowerCase();
    if (mt === 'media') return '[媒体，无文字说明]';
    return '[无正文]';
  }

  function apiBase() {
    return (typeof LOCAL_API_BASE !== 'undefined' ? LOCAL_API_BASE : '') || '';
  }

  function api(method, path, body) {
    var opts = { method: method, headers: typeof authHeaders === 'function' ? authHeaders() : {} };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(apiBase().replace(/\/$/, '') + path, opts);
  }

  function showPollMsg(text, isErr) {
    var el = document.getElementById('twilioDetailPollMsg');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'msg' + (isErr ? ' err' : '');
    el.style.display = text ? 'inline-block' : 'none';
  }

  function syncTwilioPollAutoButton() {
    var btn = document.getElementById('twilioPollAutoToggleBtn');
    if (!btn) return;
    api('GET', '/api/twilio-whatsapp/poll-auto').then(function(r) {
      return r.ok ? r.json() : null;
    }).then(function(d) {
      if (!btn) return;
      var en = !!(d && d.enabled);
      btn.dataset.pollAutoEnabled = en ? '1' : '0';
      btn.textContent = en ? '停止自动回复' : '恢复自动回复';
    }).catch(function() {
      btn.dataset.pollAutoEnabled = '0';
      btn.textContent = '恢复自动回复';
    });
  }

  var selectedPeerId = null;
  /** 消息页每 2s 自动拉取会话 + 当前会话消息 */
  var twilioMessagesRefreshTimer = null;

  function stopTwilioMessagesAutoRefresh() {
    if (twilioMessagesRefreshTimer) {
      clearInterval(twilioMessagesRefreshTimer);
      twilioMessagesRefreshTimer = null;
    }
  }

  function startTwilioMessagesAutoRefresh() {
    stopTwilioMessagesAutoRefresh();
    twilioMessagesRefreshTimer = setInterval(function() {
      var root = document.getElementById('content-twilio-whatsapp-detail');
      if (!root || !root.classList.contains('visible')) {
        stopTwilioMessagesAutoRefresh();
        return;
      }
      var tabMsg = document.getElementById('twilioTabMessages');
      if (!tabMsg || tabMsg.style.display === 'none') return;
      loadTwilioSessionList(true);
      if (selectedPeerId) loadTwilioMessageList(true);
    }, 2000);
  }

  function switchTwilioTab(key) {
    var tabMsg = document.getElementById('twilioTabMessages');
    var tabKb = document.getElementById('twilioTabKnowledge');
    document.querySelectorAll('.twilio-detail-tab').forEach(function(t) {
      var k = t.getAttribute('data-twilio-tab');
      var on = k === key;
      t.classList.toggle('active', on);
      t.classList.toggle('btn-primary', on);
      t.classList.toggle('btn-ghost', !on);
    });
    if (tabMsg) tabMsg.style.display = key === 'messages' ? 'block' : 'none';
    if (tabKb) tabKb.style.display = key === 'knowledge' ? 'block' : 'none';
    if (key === 'knowledge' && typeof loadTwilioKnowledgeTab === 'function') loadTwilioKnowledgeTab();
    if (key === 'messages') {
      syncTwilioPollAutoButton();
      startTwilioMessagesAutoRefresh();
      loadTwilioSessionList();
      if (selectedPeerId) loadTwilioMessageList();
    } else {
      stopTwilioMessagesAutoRefresh();
    }
  }

  document.querySelectorAll('.twilio-detail-tab').forEach(function(tab) {
    tab.addEventListener('click', function() {
      var key = tab.getAttribute('data-twilio-tab');
      if (key) switchTwilioTab(key);
    });
  });

  function showKbMsg(el, text, isErr) {
    if (!el) return;
    el.textContent = text || '';
    el.className = 'msg' + (isErr ? ' err' : '');
    el.style.display = text ? 'inline-block' : 'none';
  }

  function renderTwilioKbUploadResultBoard(d) {
    var board = document.getElementById('twilioKbUploadResultBoard');
    if (!board) return;
    if (!d) {
      board.style.display = 'none';
      board.innerHTML = '';
      return;
    }
    var rows = [
      ['公司新增', d.created_enterprises || 0],
      ['公司更新', d.updated_enterprises || 0],
      ['产品新增', d.created_products || 0],
      ['产品更新', d.updated_products || 0]
    ];
    var html = '<div style="font-weight:600;margin-bottom:0.35rem;">本次导入摘要</div>';
    html += '<table style="width:100%;max-width:24rem;border-collapse:collapse;font-size:0.8rem;"><tbody>';
    rows.forEach(function(r) {
      html += '<tr><td style="padding:0.2rem 0.65rem 0.2rem 0;color:var(--text-muted);">' + escapeHtml(r[0]) + '</td><td style="padding:0.2rem 0;">' + r[1] + '</td></tr>';
    });
    html += '</tbody></table>';
    if (d.errors && d.errors.length) {
      html += '<div style="margin-top:0.5rem;color:#f87171;font-size:0.78rem;">提示</div><ul style="margin:0.25rem 0 0 1rem;padding:0;font-size:0.78rem;">';
      d.errors.forEach(function(err) {
        html += '<li style="margin-bottom:0.2rem;">' + escapeHtml(String(err)) + '</li>';
      });
      html += '</ul>';
    }
    board.innerHTML = html;
    board.style.display = 'block';
  }

  var twilioKbTreeItems = [];

  function fillProductOptions(prodSel, pitems, selectedPid) {
    prodSel.innerHTML = '<option value="">不绑定</option>' + pitems.map(function(p) {
      return '<option value="' + p.id + '">' + escapeHtml(p.name || ('产品 #' + p.id)) + '</option>';
    }).join('');
    prodSel.value = selectedPid ? String(selectedPid) : '';
  }

  function renderKbManageList(items) {
    var el = document.getElementById('twilioKbManageList');
    if (!el) return;
    if (!items || items.length === 0) {
      el.innerHTML = '<p class="meta">暂无公司。请使用上方「下载资料模板」填表后<strong>上传资料 CSV</strong>导入。</p>';
      return;
    }
    el.innerHTML = items.map(function(e) {
      var prods = e.products || [];
      var pl = prods.map(function(p) {
        return '<div style="padding-left:0.75rem;margin:0.15rem 0;">· ' + escapeHtml(p.name || '') + ' <span style="color:var(--text-muted);font-size:0.72rem;">#' + p.id + '</span> <button type="button" class="btn btn-ghost btn-sm twilio-kb-edit-prod" data-pid="' + p.id + '" style="font-size:0.7rem;padding:0.05rem 0.3rem;">编辑</button> <button type="button" class="btn btn-ghost btn-sm twilio-kb-del-prod" data-pid="' + p.id + '" style="font-size:0.7rem;padding:0.05rem 0.3rem;">删</button></div>';
      }).join('');
      return '<div style="margin-bottom:0.45rem;padding-bottom:0.35rem;border-bottom:1px solid rgba(255,255,255,0.06);"><strong>' + escapeHtml(e.name || '') + '</strong> <span style="color:var(--text-muted);font-size:0.75rem;">ID ' + e.id + '</span> <button type="button" class="btn btn-ghost btn-sm twilio-kb-edit-ent" data-eid="' + e.id + '" style="font-size:0.7rem;padding:0.05rem 0.3rem;">编辑公司</button> <button type="button" class="btn btn-ghost btn-sm twilio-kb-del-ent" data-eid="' + e.id + '" style="font-size:0.7rem;padding:0.05rem 0.3rem;">删公司</button></div>' + pl;
    }).join('');
  }

  function loadTwilioKnowledgeTab() {
    var entSel = document.getElementById('twilioKbEnterpriseSelect');
    var prodSel = document.getElementById('twilioKbProductSelect');
    var preview = document.getElementById('twilioKbPreview');
    if (!entSel || !prodSel) return;
    api('GET', '/api/twilio-whatsapp/knowledge/tree').then(function(r) { return r.ok ? r.json() : null; }).then(function(tree) {
      var items = (tree && tree.items) ? tree.items : [];
      twilioKbTreeItems = items;
      entSel.innerHTML = '<option value="">不绑定</option>' + items.map(function(e) {
        return '<option value="' + e.id + '">' + escapeHtml(e.name || ('公司 #' + e.id)) + '</option>';
      }).join('');
      renderKbManageList(items);
      return api('GET', '/api/twilio-whatsapp/config').then(function(r) { return r.ok ? r.json() : null; }).then(function(cfg) {
        cfg = cfg || {};
        var eid = cfg.twilio_kb_enterprise_id != null ? String(cfg.twilio_kb_enterprise_id) : '';
        var pid = cfg.twilio_kb_product_id != null ? String(cfg.twilio_kb_product_id) : '';
        entSel.value = eid;
        var pitems = [];
        for (var i = 0; i < items.length; i++) {
          if (String(items[i].id) === eid) {
            pitems = items[i].products || [];
            break;
          }
        }
        fillProductOptions(prodSel, pitems, pid);
        updateKbPreview(cfg, entSel, prodSel, preview);
        entSel.onchange = function() {
          var pitems2 = [];
          for (var j = 0; j < twilioKbTreeItems.length; j++) {
            if (String(twilioKbTreeItems[j].id) === entSel.value) {
              pitems2 = twilioKbTreeItems[j].products || [];
              break;
            }
          }
          fillProductOptions(prodSel, pitems2, '');
        };
      });
    }).catch(function() {
      showKbMsg(document.getElementById('twilioKbSaveMsg'), '加载失败（需登录）', true);
    });
  }

  function updateKbPreview(cfg, entSel, prodSel, preview) {
    if (!preview) return;
    var lines = [];
    if (cfg && cfg.twilio_kb_enterprise_id != null) lines.push('已绑定 WhatsApp 公司 ID：' + cfg.twilio_kb_enterprise_id);
    if (cfg && cfg.twilio_kb_product_id != null) lines.push('已绑定 WhatsApp 产品 ID：' + cfg.twilio_kb_product_id);
    lines.push('自动回复使用本页 WhatsApp 专用资料与最近 10 条对话。');
    preview.innerHTML = lines.map(function(l) { return '<div style="margin-bottom:0.35rem;">' + escapeHtml(l) + '</div>'; }).join('');
    preview.style.display = 'block';
  }

  var twilioKbSaveBtn = document.getElementById('twilioKbSaveBtn');
  if (twilioKbSaveBtn) {
    twilioKbSaveBtn.addEventListener('click', function() {
      var entSel = document.getElementById('twilioKbEnterpriseSelect');
      var prodSel = document.getElementById('twilioKbProductSelect');
      var msgEl = document.getElementById('twilioKbSaveMsg');
      if (!entSel || !prodSel) return;
      var body = {};
      body.twilio_kb_enterprise_id = entSel.value ? parseInt(entSel.value, 10) : null;
      body.twilio_kb_product_id = prodSel.value ? parseInt(prodSel.value, 10) : null;
      twilioKbSaveBtn.disabled = true;
      showKbMsg(msgEl, '保存中…', false);
      api('POST', '/api/twilio-whatsapp/config', body).then(function(r) {
        return r.json().then(function(j) { return { ok: r.ok, j: j }; });
      }).then(function(x) {
        if (!x.ok) {
          showKbMsg(msgEl, (x.j && x.j.detail) ? String(x.j.detail) : '保存失败', true);
          return;
        }
        showKbMsg(msgEl, '已保存', false);
        api('GET', '/api/twilio-whatsapp/config').then(function(r) { return r.ok ? r.json() : null; }).then(function(cfg) {
          updateKbPreview(cfg, entSel, prodSel, document.getElementById('twilioKbPreview'));
        });
      }).catch(function(e) {
        showKbMsg(msgEl, (e && e.message) ? e.message : '请求失败', true);
      }).finally(function() {
        twilioKbSaveBtn.disabled = false;
      });
    });
  }

  var twilioKbManageList = document.getElementById('twilioKbManageList');
  if (twilioKbManageList) {
    twilioKbManageList.addEventListener('click', function(ev) {
      var btn = ev.target && ev.target.closest ? ev.target.closest('button') : null;
      if (!btn) return;
      if (btn.classList.contains('twilio-kb-edit-ent')) {
        var eid = parseInt(btn.getAttribute('data-eid'), 10);
        if (isNaN(eid)) return;
        var ent = twilioKbTreeItems.filter(function(e) { return e.id === eid; })[0];
        var n = prompt('公司名称：', ent ? ent.name : '');
        if (n == null) return;
        var ci = prompt('公司介绍：', '');
        if (ci === null) return;
        api('PUT', '/api/twilio-whatsapp/knowledge/enterprises/' + eid, { name: n.trim() || '未命名', company_info: ci ? ci.trim() : '' }).then(function(r) {
          return r.json().then(function(j) { return { ok: r.ok, j: j }; });
        }).then(function(x) {
          if (!x.ok) { alert((x.j && x.j.detail) ? String(x.j.detail) : '保存失败'); return; }
          loadTwilioKnowledgeTab();
        }).catch(function() { alert('请求失败'); });
        return;
      }
      if (btn.classList.contains('twilio-kb-del-ent')) {
        var eid2 = parseInt(btn.getAttribute('data-eid'), 10);
        if (isNaN(eid2)) return;
        if (!confirm('确定删除该公司及其下所有产品？')) return;
        api('DELETE', '/api/twilio-whatsapp/knowledge/enterprises/' + eid2).then(function(r) {
          return r.json().then(function(j) { return { ok: r.ok, j: j }; });
        }).then(function(x) {
          if (!x.ok) { alert((x.j && x.j.detail) ? String(x.j.detail) : '删除失败'); return; }
          loadTwilioKnowledgeTab();
        }).catch(function() { alert('请求失败'); });
        return;
      }
      if (btn.classList.contains('twilio-kb-edit-prod')) {
        var pid = parseInt(btn.getAttribute('data-pid'), 10);
        if (isNaN(pid)) return;
        var n2 = prompt('产品名称：', '');
        if (n2 == null) return;
        var intro3 = prompt('产品介绍（可留空）：', '');
        if (intro3 === null) return;
        var ph3 = prompt('常用话术（可留空）：', '');
        if (ph3 === null) return;
        api('PUT', '/api/twilio-whatsapp/knowledge/products/' + pid, {
          name: n2.trim() || '未命名',
          product_intro: intro3 ? intro3.trim() : null,
          common_phrases: ph3 ? ph3.trim() : null
        }).then(function(r) {
          return r.json().then(function(j) { return { ok: r.ok, j: j }; });
        }).then(function(x) {
          if (!x.ok) { alert((x.j && x.j.detail) ? String(x.j.detail) : '保存失败'); return; }
          loadTwilioKnowledgeTab();
        }).catch(function() { alert('请求失败'); });
        return;
      }
      if (btn.classList.contains('twilio-kb-del-prod')) {
        var pid2 = parseInt(btn.getAttribute('data-pid'), 10);
        if (isNaN(pid2)) return;
        if (!confirm('确定删除该产品？')) return;
        api('DELETE', '/api/twilio-whatsapp/knowledge/products/' + pid2).then(function(r) {
          return r.json().then(function(j) { return { ok: r.ok, j: j }; });
        }).then(function(x) {
          if (!x.ok) { alert((x.j && x.j.detail) ? String(x.j.detail) : '删除失败'); return; }
          loadTwilioKnowledgeTab();
        }).catch(function() { alert('请求失败'); });
      }
    });
  }

  var twilioKbDownloadTemplateBtn = document.getElementById('twilioKbDownloadTemplateBtn');
  if (twilioKbDownloadTemplateBtn) {
    twilioKbDownloadTemplateBtn.addEventListener('click', function(e) {
      e.preventDefault();
      var url = apiBase().replace(/\/$/, '') + '/api/twilio-whatsapp/knowledge/material-template';
      var headers = typeof authHeaders === 'function' ? authHeaders() : {};
      fetch(url, { headers: headers }).then(function(r) {
        if (!r.ok) {
          return r.json().then(function(j) {
            throw new Error((j && j.detail) ? String(j.detail) : ('HTTP ' + r.status));
          });
        }
        return r.blob();
      }).then(function(blob) {
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'twilio_whatsapp_materials_template.csv';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(a.href);
      }).catch(function(err) {
        alert((err && err.message) ? err.message : '下载失败');
      });
    });
  }

  var twilioKbUploadInput = document.getElementById('twilioKbUploadInput');
  if (twilioKbUploadInput) {
    twilioKbUploadInput.addEventListener('change', function() {
      if (!twilioKbUploadInput.files || twilioKbUploadInput.files.length === 0) return;
      var fd = new FormData();
      fd.append('file', twilioKbUploadInput.files[0]);
      var opts = { method: 'POST', body: fd, headers: typeof authHeaders === 'function' ? authHeaders() : {} };
      delete opts.headers['Content-Type'];
      var msgEl = document.getElementById('twilioKbUploadMsg');
      showKbMsg(msgEl, '上传中…', false);
      fetch(apiBase().replace(/\/$/, '') + '/api/twilio-whatsapp/knowledge/upload-materials', opts).then(function(r) {
        return r.json().then(function(j) { return { ok: r.ok, j: j }; });
      }).then(function(x) {
        var d = x.j || {};
        if (!x.ok) {
          renderTwilioKbUploadResultBoard(null);
          showKbMsg(msgEl, (d.detail && String(d.detail)) || '上传失败', true);
          twilioKbUploadInput.value = '';
          return;
        }
        var msg = '导入完成：公司 ' + (d.created_enterprises || 0) + ' 个新增、' + (d.updated_enterprises || 0) + ' 个更新；产品 ' + (d.created_products || 0) + ' 个新增、' + (d.updated_products || 0) + ' 个更新。';
        if (d.errors && d.errors.length) msg += ' 提示: ' + d.errors.join('; ');
        showKbMsg(msgEl, msg, !!(d.errors && d.errors.length));
        renderTwilioKbUploadResultBoard(d);
        twilioKbUploadInput.value = '';
        loadTwilioKnowledgeTab();
      }).catch(function() {
        renderTwilioKbUploadResultBoard(null);
        showKbMsg(msgEl, '上传失败', true);
        twilioKbUploadInput.value = '';
      });
    });
  }

  function loadTwilioSessionList(silent) {
    var listEl = document.getElementById('twilioSessionList');
    if (!listEl) return;
    if (!silent) listEl.innerHTML = '<p class="meta" style="padding:0.5rem;">加载中…</p>';
    api('GET', '/api/twilio-whatsapp/sessions').then(function(r) { return r.ok ? r.json() : null; }).then(function(d) {
      if (!listEl) return;
      var items = (d && d.items) ? d.items : [];
      if (items.length === 0) {
        listEl.innerHTML = '<p class="meta" style="padding:0.5rem;">暂无会话</p>';
        return;
      }
      listEl.innerHTML = items.map(function(s) {
        var name = s.peer_id || '未知';
        var previewRaw = (s.last_preview || '').trim();
        var preview = (previewRaw || '[无正文]').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        var time = (s.last_at || '').substring(0, 16).replace('T', ' ');
        var pid = encodeURIComponent(s.peer_id || '');
        var active = selectedPeerId === s.peer_id ? ' background:rgba(37,211,102,0.18);' : '';
        return '<div class="twilio-session-item" data-peer-id="' + escapeHtml(s.peer_id) + '" style="padding:0.5rem 0.75rem;border-bottom:1px solid var(--border);cursor:pointer;font-size:0.85rem;' + active + '"><div style="font-weight:500;word-break:break-all;">' + escapeHtml(name) + '</div><div style="font-size:0.78rem;color:var(--text-muted);margin-top:0.2rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + preview + '</div><div style="font-size:0.72rem;color:var(--text-muted);margin-top:0.15rem;">' + escapeHtml(time) + '</div></div>';
      }).join('');
      listEl.querySelectorAll('.twilio-session-item').forEach(function(el) {
        el.addEventListener('click', function() {
          selectedPeerId = el.getAttribute('data-peer-id');
          listEl.querySelectorAll('.twilio-session-item').forEach(function(e) { e.style.background = ''; });
          el.style.background = 'rgba(37,211,102,0.18)';
          var titleEl = document.getElementById('twilioMessageListTitle');
          if (titleEl) titleEl.textContent = selectedPeerId || '会话';
          loadTwilioMessageList();
        });
      });
    }).catch(function() { if (listEl) listEl.innerHTML = '<p class="msg err" style="padding:0.5rem;">加载失败</p>'; });
  }

  function loadTwilioMessageList(silent) {
    var listEl = document.getElementById('twilioMessageList');
    var titleEl = document.getElementById('twilioMessageListTitle');
    if (!listEl) return;
    if (!selectedPeerId) {
      if (titleEl) titleEl.textContent = '请从左侧选择会话';
      listEl.innerHTML = '<p class="meta">选择会话后可查看消息</p>';
      return;
    }
    var q = '/api/twilio-whatsapp/messages?peer_id=' + encodeURIComponent(selectedPeerId) + '&limit=200';
    if (!silent) listEl.innerHTML = '<p class="meta">加载中…</p>';
    api('GET', q).then(function(r) { return r.ok ? r.json() : null; }).then(function(d) {
      if (!listEl) return;
      var items = (d && d.items) ? d.items : [];
      if (items.length === 0) {
        listEl.innerHTML = '<p class="meta">该会话暂无消息</p>';
        return;
      }
      listEl.innerHTML = items.slice().reverse().map(function(m) {
        var dir = m.direction === 'in' ? '收' : '发';
        var content = twilioMessageBodyForDisplay(m).replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>');
        var time = (m.created_at || '').substring(0, 19).replace('T', ' ');
        var align = m.direction === 'in' ? 'left' : 'right';
        var bg = m.direction === 'in' ? 'rgba(255,255,255,0.06)' : 'rgba(37,211,102,0.18)';
        return '<div style="margin-bottom:0.55rem;text-align:' + align + ';"><span style="font-size:0.72rem;color:var(--text-muted);">' + dir + ' · ' + escapeHtml(time) + '</span><div style="display:inline-block;max-width:88%;padding:0.45rem 0.65rem;border-radius:var(--radius-sm);background:' + bg + ';font-size:0.85rem;text-align:left;word-break:break-word;">' + content + '</div></div>';
      }).join('');
    }).catch(function() { if (listEl) listEl.innerHTML = '<p class="msg err">加载失败</p>'; });
  }

  var refreshBtn = document.getElementById('twilioRefreshSessionsBtn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', function() {
      refreshBtn.disabled = true;
      loadTwilioSessionList();
      if (selectedPeerId) loadTwilioMessageList();
      setTimeout(function() { refreshBtn.disabled = false; }, 400);
    });
  }

  var pollBtn = document.getElementById('twilioPollReplyBtn');
  if (pollBtn) {
    pollBtn.addEventListener('click', function() {
      pollBtn.disabled = true;
      showPollMsg('处理中…', false);
      api('POST', '/api/twilio-whatsapp/poll-and-reply').then(function(r) {
        return r.json().then(function(j) { return { ok: r.ok, j: j, status: r.status }; });
      }).then(function(x) {
        if (!x.ok) {
          showPollMsg((x.j && x.j.detail) ? String(x.j.detail) : ('HTTP ' + x.status), true);
          return;
        }
        var p = (x.j && x.j.processed) != null ? x.j.processed : 0;
        var errs = (x.j && x.j.errors && x.j.errors.length) ? ('；' + x.j.errors.join(' ')) : '';
        showPollMsg('已处理 ' + p + ' 条' + errs, !!errs);
        loadTwilioSessionList();
        if (selectedPeerId) loadTwilioMessageList();
      }).catch(function(e) {
        showPollMsg((e && e.message) ? e.message : '请求失败', true);
      }).finally(function() {
        pollBtn.disabled = false;
      });
    });
  }

  var pollAutoToggleBtn = document.getElementById('twilioPollAutoToggleBtn');
  if (pollAutoToggleBtn) {
    pollAutoToggleBtn.addEventListener('click', function() {
      var currentlyOn = pollAutoToggleBtn.dataset.pollAutoEnabled === '1';
      var nextEnabled = !currentlyOn;
      pollAutoToggleBtn.disabled = true;
      api('POST', '/api/twilio-whatsapp/poll-auto', { enabled: nextEnabled }).then(function(r) {
        return r.json().then(function(j) { return { ok: r.ok, j: j }; });
      }).then(function(x) {
        if (!x.ok) {
          showPollMsg((x.j && x.j.detail) ? String(x.j.detail) : '设置失败', true);
          return;
        }
        showPollMsg(nextEnabled ? '已恢复每 2 秒自动拉取并回复' : '已停止后台自动回复（仍可手动「拉取并回复」）', false);
        syncTwilioPollAutoButton();
      }).catch(function(e) {
        showPollMsg((e && e.message) ? e.message : '请求失败', true);
      }).finally(function() {
        pollAutoToggleBtn.disabled = false;
      });
    });
  }

  window.showTwilioWhatsappDetailView = function() {
    if (typeof saveCurrentSessionToStore === 'function' && typeof currentView !== 'undefined' && currentView === 'chat') {
      saveCurrentSessionToStore();
    }
    location.hash = 'twilio-whatsapp-detail';
    document.querySelectorAll('.nav-left-item').forEach(function(b) { b.classList.remove('active'); });
    var navEl = document.querySelector('.nav-left-item[data-view="skill-store"]');
    if (navEl) navEl.classList.add('active');
    document.querySelectorAll('.content-block').forEach(function(p) { p.classList.remove('visible'); });
    var contentEl = document.getElementById('content-twilio-whatsapp-detail');
    if (contentEl) contentEl.classList.add('visible');
    if (typeof currentView !== 'undefined') currentView = 'twilio-whatsapp-detail';
    switchTwilioTab('messages');
  };

  var twilioDetailBackBtn = document.getElementById('twilioDetailBackBtn');
  if (twilioDetailBackBtn) {
    twilioDetailBackBtn.addEventListener('click', function() {
      stopTwilioMessagesAutoRefresh();
      var nav = document.querySelector('.nav-left-item[data-view="skill-store"]');
      if (nav) {
        location.hash = '';
        nav.click();
      }
    });
  }
})();
