/**
 * 企业微信详细界面：消息记录、客户配置、系统配置。
 * 企微 API 走本地 lobster_online 后端（LOCAL_API_BASE 同源）。
 */
(function() {
  function escapeHtml(s) {
    if (s == null) return '';
    var t = String(s);
    return t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /** 与后端 _wecom_body_display 一致：无正文时按 msg_type 显示占位（兼容历史空 content） */
  function wecomMessageBodyForDisplay(m) {
    var raw = (m.content || '').trim();
    if (raw) return raw;
    var mt = String(m.msg_type || 'text').toLowerCase();
    var map = {
      text: '[无正文]',
      image: '[图片]',
      voice: '[语音]',
      video: '[视频]',
      file: '[文件]',
      location: '[位置]',
      link: '[链接]',
      event: '[事件]',
      shortvideo: '[短视频]',
      emoji: '[表情]',
      mixed: '[混合消息]'
    };
    return map[mt] || ('[' + mt + ']');
  }

  function wecomApiBase() {
    return (typeof LOCAL_API_BASE !== 'undefined' ? LOCAL_API_BASE : '') || '';
  }

  function api(method, path, body) {
    var opts = { method: method, headers: typeof authHeaders === 'function' ? authHeaders() : {} };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(wecomApiBase() + path, opts);
  }

  function showMsg(el, text, isErr) {
    if (!el) return;
    el.textContent = text || '';
    el.className = 'msg' + (isErr ? ' err' : '');
    el.style.display = text ? 'block' : 'none';
  }

  var configFilterCache = [];

  function loadConfigOptions(selectId, preselect) {
    var sel = document.getElementById(selectId);
    if (!sel) return;
    api('GET', '/api/wecom/configs').then(function(r) { return r.ok ? r.json() : null; }).then(function(d) {
      var configs = (d && d.configs) ? d.configs : [];
      configFilterCache = configs;
      sel.innerHTML = '<option value="">全部应用</option>' + configs.map(function(c) {
        return '<option value="' + c.id + '">' + (c.name || c.callback_path || c.id) + '</option>';
      }).join('');
      if (preselect) {
        sel.value = String(preselect);
        sel.dispatchEvent(new Event('change'));
      }
    });
  }

  var selectedWecomCustomerId = null;

  function showWecomDetailView(configId) {
    if (configId != null) window._wecomDetailConfigId = configId;
    if (!window._wecomDetailConfigId) window._wecomDetailConfigId = null;
    location.hash = window._wecomDetailConfigId ? 'wecom-detail:' + window._wecomDetailConfigId : 'wecom-detail';
    document.querySelectorAll('.content-block').forEach(function(p) { p.classList.remove('visible'); });
    var contentEl = document.getElementById('content-wecom-detail');
    if (contentEl) contentEl.classList.add('visible');
    document.querySelectorAll('.nav-left-item').forEach(function(b) { b.classList.remove('active'); });
    var navEl = document.querySelector('.nav-left-item[data-view="skill-store"]');
    if (navEl) navEl.classList.add('active');
    if (typeof currentView !== 'undefined') currentView = 'wecom-detail';
    loadConfigOptions('wecomMsgConfigFilter', configId);
    loadConfigOptions('wecomCustConfigFilter', configId);
    document.querySelectorAll('.wecom-detail-tab').forEach(function(t) { t.classList.remove('active'); });
    var first = document.querySelector('.wecom-detail-tab[data-wecom-tab="messages"]');
    if (first) first.classList.add('active');
    var tabMsg = document.getElementById('wecomTabMessages');
    var tabContacts = document.getElementById('wecomTabContacts');
    var tabSend = document.getElementById('wecomTabSend');
    var tabCust = document.getElementById('wecomTabCustomers');
    var tabSys = document.getElementById('wecomTabSystem');
    if (tabMsg) tabMsg.style.display = 'block';
    if (tabContacts) tabContacts.style.display = 'none';
    if (tabSend) tabSend.style.display = 'none';
    if (tabCust) tabCust.style.display = 'none';
    if (tabSys) tabSys.style.display = 'none';
    var appNameEl = document.getElementById('wecomDetailAppName');
    if (appNameEl) appNameEl.textContent = '';
    if (configId) {
      api('GET', '/api/wecom/configs').then(function(r) { return r.ok ? r.json() : null; }).then(function(d) {
        if (!d || !d.configs) return;
        var match = d.configs.find(function(c) { return c.id === configId; });
        if (match && appNameEl) appNameEl.textContent = '— ' + (match.name || match.callback_path || '');
      });
    }
    loadSessionList();
    loadEnterpriseList();
  }

  document.querySelectorAll('.wecom-detail-tab').forEach(function(tab) {
    tab.addEventListener('click', function() {
      var key = tab.getAttribute('data-wecom-tab');
      document.querySelectorAll('.wecom-detail-tab').forEach(function(t) { t.classList.remove('active'); });
      tab.classList.add('active');
      var tabMsg = document.getElementById('wecomTabMessages');
      var tabContacts = document.getElementById('wecomTabContacts');
      var tabSend = document.getElementById('wecomTabSend');
      var tabCust = document.getElementById('wecomTabCustomers');
      var tabSys = document.getElementById('wecomTabSystem');
      if (tabMsg) tabMsg.style.display = key === 'messages' ? 'block' : 'none';
      if (tabContacts) tabContacts.style.display = key === 'contacts' ? 'block' : 'none';
      if (tabSend) tabSend.style.display = key === 'send' ? 'block' : 'none';
      if (tabCust) tabCust.style.display = key === 'customers' ? 'block' : 'none';
      if (tabSys) tabSys.style.display = key === 'system' ? 'block' : 'none';
      if (key === 'messages') { loadSessionList(); if (selectedWecomCustomerId) loadMessageList(); }
      if (key === 'contacts' && typeof initWecomContacts === 'function') initWecomContacts();
      if (key === 'send' && typeof initWecomSend === 'function') initWecomSend();
      if (key === 'customers') loadCustomerList();
      if (key === 'system') loadEnterpriseList();
    });
  });

  var wecomDetailBackBtn = document.getElementById('wecomDetailBackBtn');
  if (wecomDetailBackBtn) {
    wecomDetailBackBtn.addEventListener('click', function() {
      location.hash = 'wecom-config';
      if (typeof showWecomConfigView === 'function') showWecomConfigView();
    });
  }

  var wecomDetailToConfigLink = document.getElementById('wecomDetailToConfigLink');
  if (wecomDetailToConfigLink) {
    wecomDetailToConfigLink.addEventListener('click', function(e) { e.preventDefault(); location.hash = 'wecom-config'; if (typeof showWecomConfigView === 'function') showWecomConfigView(); });
  }

  function loadSessionList() {
    var listEl = document.getElementById('wecomSessionList');
    if (!listEl) return;
    var configId = window._wecomDetailConfigId || null;
    if (!configId) {
      var sel = document.getElementById('wecomMsgConfigFilter');
      if (sel && sel.value) configId = parseInt(sel.value, 10);
    }
    var q = '/api/wecom/sessions';
    if (configId) q += '?wecom_config_id=' + configId;
    listEl.innerHTML = '<p class="meta">加载中…</p>';
    api('GET', q).then(function(r) { return r.ok ? r.json() : null; }).then(function(d) {
      if (!listEl) return;
      var items = (d && d.items) ? d.items : [];
      if (items.length === 0) {
        listEl.innerHTML = '<p class="meta" style="padding:0.5rem;">暂无会话</p>';
        return;
      }
      listEl.innerHTML = items.map(function(s) {
        var name = s.customer_name || s.customer_phone || s.external_user_id || '未知';
        var previewRaw = (s.last_preview || '').trim();
        var preview = (previewRaw || '[无正文]').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        var time = (s.last_at || '').substring(11, 16);
        var active = selectedWecomCustomerId === s.customer_id ? 'background:rgba(6,182,212,0.15);' : '';
        return '<div class="wecom-session-item" data-customer-id="' + s.customer_id + '" style="padding:0.55rem 0.75rem;border-bottom:1px solid rgba(255,255,255,0.06);cursor:pointer;' + active + '">' +
          '<div style="display:flex;justify-content:space-between;align-items:baseline;">' +
            '<span style="font-size:0.85rem;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;">' + escapeHtml(name) + '</span>' +
            '<span style="font-size:0.68rem;color:var(--text-muted);margin-left:0.5rem;flex-shrink:0;">' + escapeHtml(time) + '</span>' +
          '</div>' +
          '<div style="font-size:0.78rem;color:var(--text-muted);margin-top:0.2rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + preview + '</div>' +
        '</div>';
      }).join('');
      listEl.querySelectorAll('.wecom-session-item').forEach(function(el) {
        el.addEventListener('click', function() {
          selectedWecomCustomerId = parseInt(el.getAttribute('data-customer-id'), 10);
          listEl.querySelectorAll('.wecom-session-item').forEach(function(e) { e.style.background = ''; });
          el.style.background = 'rgba(6,182,212,0.15)';
          var titleEl = document.getElementById('wecomMessageListTitle');
          if (titleEl) {
            var spanEl = titleEl.querySelector('span');
            if (spanEl) spanEl.textContent = el.querySelector('div') ? el.querySelector('div').textContent : '会话';
          }
          loadMessageList();
          var inp = document.getElementById('wecomSendInput');
          if (inp) inp.focus();
        });
      });
    }).catch(function() { if (listEl) listEl.innerHTML = '<p class="msg err">加载失败</p>'; });
  }

  function loadMessageList() {
    var listEl = document.getElementById('wecomMessageList');
    var titleEl = document.getElementById('wecomMessageListTitle');
    var sendBar = document.getElementById('wecomSendBar');
    var editBtn = document.getElementById('wecomEditCustomerTopBtn');
    if (!listEl) return;
    if (!selectedWecomCustomerId) {
      if (titleEl) titleEl.querySelector('span').textContent = '请选择会话';
      listEl.innerHTML = '<p class="meta" style="text-align:center;padding:3rem 1rem;color:var(--text-muted);">选择左侧会话查看聊天记录</p>';
      if (sendBar) sendBar.style.display = 'none';
      if (editBtn) editBtn.style.display = 'none';
      return;
    }
    if (sendBar) sendBar.style.display = 'block';
    if (editBtn) {
      editBtn.style.display = 'inline-block';
      editBtn.onclick = function(e) { e.preventDefault(); openCustomerModal(selectedWecomCustomerId); };
    }
    var q = '/api/wecom/messages?limit=100&customer_id=' + selectedWecomCustomerId;
    listEl.innerHTML = '<p class="meta" style="text-align:center;padding:2rem;">加载中…</p>';
    api('GET', q).then(function(r) { return r.ok ? r.json() : null; }).then(function(d) {
      if (!listEl) return;
      var items = (d && d.items) ? d.items : [];
      if (items.length === 0) {
        listEl.innerHTML = '<p class="meta" style="text-align:center;padding:3rem 1rem;">暂无消息</p>';
        return;
      }
      listEl.innerHTML = items.slice().reverse().map(function(m) {
        var mt = String(m.msg_type || 'text').toLowerCase();
        var raw = (m.content || '').trim();
        var hasUrl = raw && /^(https?:\/\/|\/static\/)/.test(raw);
        var contentHtml;
        if (mt === 'image' && hasUrl) {
          contentHtml = '<img src="' + escapeHtml(raw) + '" style="max-width:100%;max-height:240px;border-radius:6px;cursor:pointer;" onclick="window.open(this.src,\'_blank\')" alt="[图片]">';
        } else if (mt === 'video' && hasUrl) {
          contentHtml = '<video src="' + escapeHtml(raw) + '" controls style="max-width:100%;max-height:240px;border-radius:6px;"></video>';
        } else {
          contentHtml = wecomMessageBodyForDisplay(m).replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>');
        }
        var time = (m.created_at || '').substring(11, 16);
        var isIn = m.direction === 'in';
        return '<div style="display:flex;margin-bottom:0.6rem;' + (isIn ? '' : 'flex-direction:row-reverse;') + '">' +
          '<div style="max-width:70%;"><div style="padding:0.5rem 0.7rem;border-radius:' + (isIn ? '2px 10px 10px 10px' : '10px 2px 10px 10px') + ';background:' + (isIn ? 'rgba(255,255,255,0.08)' : 'rgba(6,182,212,0.2)') + ';font-size:0.85rem;line-height:1.5;word-break:break-word;">' + contentHtml + '</div>' +
          '<div style="font-size:0.68rem;color:var(--text-muted);margin-top:0.15rem;' + (isIn ? '' : 'text-align:right;') + '">' + escapeHtml(time) + '</div></div></div>';
      }).join('');
      listEl.scrollTop = listEl.scrollHeight;
    }).catch(function() { if (listEl) listEl.innerHTML = '<p class="msg err">加载失败</p>'; });
  }

  // 附件和发送消息
  var _pendingAttach = null; // {file, type, previewUrl}
  var wecomChatSendBtn = document.getElementById('wecomChatSendBtn');
  var wecomSendInput = document.getElementById('wecomSendInput');
  var attachBtn = document.getElementById('wecomAttachBtn');
  var attachInput = document.getElementById('wecomAttachInput');
  var attachPreview = document.getElementById('wecomAttachPreview');
  var attachImg = document.getElementById('wecomAttachImg');
  var attachFileName = document.getElementById('wecomAttachFileName');
  var attachRemove = document.getElementById('wecomAttachRemove');

  if (attachBtn && attachInput) {
    attachBtn.addEventListener('click', function() { attachInput.click(); });
    attachInput.addEventListener('change', function() {
      var f = attachInput.files && attachInput.files[0];
      if (!f) return;
      if (f.size > 20 * 1024 * 1024) { showMsg(document.getElementById('wecomSendMsg'), '文件不能超过 20MB', true); attachInput.value = ''; return; }
      var isImg = /^image\//.test(f.type);
      var isVideo = /^video\//.test(f.type);
      _pendingAttach = { file: f, type: isImg ? 'image' : (isVideo ? 'video' : 'file') };
      if (attachPreview) attachPreview.style.display = '';
      if (isImg && attachImg) {
        var url = URL.createObjectURL(f);
        attachImg.src = url; attachImg.style.display = ''; if (attachFileName) attachFileName.style.display = 'none';
      } else {
        if (attachImg) attachImg.style.display = 'none';
        if (attachFileName) { attachFileName.textContent = (_pendingAttach.type === 'video' ? '🎬 ' : '📄 ') + f.name; attachFileName.style.display = ''; }
      }
    });
  }
  if (attachRemove) {
    attachRemove.addEventListener('click', function() {
      _pendingAttach = null;
      if (attachInput) attachInput.value = '';
      if (attachPreview) attachPreview.style.display = 'none';
      if (attachImg) { attachImg.style.display = 'none'; attachImg.src = ''; }
      if (attachFileName) attachFileName.style.display = 'none';
    });
  }

  if (wecomChatSendBtn && wecomSendInput) {
    wecomChatSendBtn.addEventListener('click', sendWecomMessage);
    wecomSendInput.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendWecomMessage(); }
    });
  }

  function _getConfigId() {
    return window._wecomDetailConfigId || (function() { var s = document.getElementById('wecomMsgConfigFilter'); return s && s.value ? parseInt(s.value, 10) : null; })();
  }

  function sendWecomMessage() {
    var input = document.getElementById('wecomSendInput');
    var msgEl = document.getElementById('wecomSendMsg');
    if (!input || !selectedWecomCustomerId) return;
    var text = (input.value || '').trim();
    var hasAttach = !!_pendingAttach;
    if (!text && !hasAttach) return;
    var configId = _getConfigId();
    if (!configId) { showMsg(msgEl, '请先选择应用', true); return; }
    input.disabled = true;
    var btn = document.getElementById('wecomChatSendBtn');
    if (btn) btn.disabled = true;
    if (attachBtn) attachBtn.disabled = true;
    showMsg(msgEl, '');

    if (hasAttach) {
      showMsg(msgEl, '正在上传…', false);
      var fd = new FormData();
      fd.append('file', _pendingAttach.file);
      var mediaType = _pendingAttach.type;
      fetch('/api/wecom/media/upload?config_id=' + configId + '&media_type=' + mediaType, {
        method: 'POST', body: fd, headers: { 'Authorization': 'Bearer ' + (localStorage.getItem('token') || '') }
      })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
      .then(function(x) {
        if (!x.ok) { showMsg(msgEl, (x.data && x.data.detail) || '上传失败', true); return Promise.reject('upload_fail'); }
        var mediaId = x.data.media_id;
        var localUrl = x.data.local_url || '';
        var promises = [];
        promises.push(
          api('POST', '/api/wecom/send-message-to-customer', {
            wecom_config_id: configId, customer_id: selectedWecomCustomerId,
            msg_type: mediaType, media_id: mediaId, content: localUrl
          }).then(function(r2) { return r2.json().then(function(d2) { return { ok: r2.ok, data: d2 }; }); })
        );
        if (text) {
          promises.push(
            api('POST', '/api/wecom/send-message-to-customer', {
              wecom_config_id: configId, customer_id: selectedWecomCustomerId,
              msg_type: 'text', content: text
            }).then(function(r3) { return r3.json().then(function(d3) { return { ok: r3.ok, data: d3 }; }); })
          );
        }
        return Promise.all(promises);
      })
      .then(function(results) {
        if (!results) return;
        var allOk = results.every(function(r) { return r.ok; });
        if (allOk) { input.value = ''; _clearAttach(); loadMessageList(); loadSessionList(); }
        else { var fail = results.find(function(r) { return !r.ok; }); showMsg(msgEl, (fail.data && fail.data.detail) || '发送失败', true); }
      })
      .catch(function(e) { if (e !== 'upload_fail') showMsg(msgEl, '网络异常', true); })
      .finally(function() { input.disabled = false; if (btn) btn.disabled = false; if (attachBtn) attachBtn.disabled = false; input.focus(); });
    } else {
      api('POST', '/api/wecom/send-message-to-customer', { wecom_config_id: configId, customer_id: selectedWecomCustomerId, content: text })
        .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
        .then(function(x) {
          if (x.ok) { input.value = ''; loadMessageList(); loadSessionList(); }
          else showMsg(msgEl, (x.data && x.data.detail) || '发送失败', true);
        })
        .catch(function() { showMsg(msgEl, '网络异常', true); })
        .finally(function() { input.disabled = false; if (btn) btn.disabled = false; if (attachBtn) attachBtn.disabled = false; input.focus(); });
    }
  }

  function _clearAttach() {
    _pendingAttach = null;
    if (attachInput) attachInput.value = '';
    if (attachPreview) attachPreview.style.display = 'none';
    if (attachImg) { attachImg.style.display = 'none'; attachImg.src = ''; }
    if (attachFileName) attachFileName.style.display = 'none';
  }

  var refreshMessagesBtn = document.getElementById('wecomRefreshMessagesBtn');
  if (refreshMessagesBtn) {
    refreshMessagesBtn.addEventListener('click', function() {
      refreshMessagesBtn.disabled = true;
      loadSessionList();
      if (selectedWecomCustomerId) loadMessageList();
      setTimeout(function() { refreshMessagesBtn.disabled = false; }, 500);
    });
  }
  var msgConfigFilter = document.getElementById('wecomMsgConfigFilter');
  if (msgConfigFilter) msgConfigFilter.addEventListener('change', function() { loadSessionList(); });

  function loadCustomerList() {
    var listEl = document.getElementById('wecomCustomerList');
    if (!listEl) return;
    var configId = window._wecomDetailConfigId || null;
    if (!configId) {
      var sel = document.getElementById('wecomCustConfigFilter');
      if (sel && sel.value) configId = parseInt(sel.value, 10);
    }
    var name = document.getElementById('wecomCustNameFilter') && document.getElementById('wecomCustNameFilter').value ? document.getElementById('wecomCustNameFilter').value.trim() : null;
    var phone = document.getElementById('wecomCustPhoneFilter') && document.getElementById('wecomCustPhoneFilter').value ? document.getElementById('wecomCustPhoneFilter').value.trim() : null;
    var q = '/api/wecom/customers?';
    if (configId) q += 'wecom_config_id=' + configId + '&';
    if (name) q += 'name=' + encodeURIComponent(name) + '&';
    if (phone) q += 'phone=' + encodeURIComponent(phone) + '&';
    listEl.innerHTML = '<p class="meta">加载中…</p>';
    api('GET', q).then(function(r) { return r.ok ? r.json() : null; }).then(function(d) {
      if (!listEl) return;
      var items = (d && d.items) ? d.items : [];
      if (items.length === 0) {
        listEl.innerHTML = '<p class="meta">暂无客户</p>';
        return;
      }
      listEl.innerHTML = '<table style="width:100%;font-size:0.85rem;border-collapse:collapse;"><thead><tr><th style="text-align:left;padding:0.4rem;">姓名</th><th style="text-align:left;padding:0.4rem;">手机</th><th style="text-align:left;padding:0.4rem;">备注</th><th></th></tr></thead><tbody>' +
        items.map(function(c) {
          return '<tr><td style="padding:0.4rem;">' + escapeHtml(c.name || '-') + '</td><td style="padding:0.4rem;">' + escapeHtml(c.phone || '-') + '</td><td style="padding:0.4rem;">' + escapeHtml((c.remark || '').substring(0, 30)) + '</td><td style="padding:0.4rem;"><button type="button" class="btn btn-ghost btn-sm wecom-edit-customer" data-customer-id="' + c.id + '">编辑</button></td></tr>';
        }).join('') + '</tbody></table>';
      listEl.querySelectorAll('.wecom-edit-customer').forEach(function(btn) {
        btn.addEventListener('click', function() { openCustomerModal(parseInt(btn.getAttribute('data-customer-id'), 10)); });
      });
    }).catch(function() { if (listEl) listEl.innerHTML = '<p class="msg err">加载失败</p>'; });
  }

  document.getElementById('wecomCustSearchBtn') && document.getElementById('wecomCustSearchBtn').addEventListener('click', loadCustomerList);

  var customerModal = document.getElementById('wecomCustomerModal');
  var customerModalId = document.getElementById('wecomCustomerModalId');
  var customerName = document.getElementById('wecomCustomerName');
  var customerBirthday = document.getElementById('wecomCustomerBirthday');
  var customerCompany = document.getElementById('wecomCustomerCompany');
  var customerJob = document.getElementById('wecomCustomerJob');
  var customerPhone = document.getElementById('wecomCustomerPhone');
  var customerWechatId = document.getElementById('wecomCustomerWechatId');
  var customerRemark = document.getElementById('wecomCustomerRemark');
  var customerModalMsg = document.getElementById('wecomCustomerModalMsg');

  function openCustomerModal(id) {
    if (!id) return;
    if (customerModalId) customerModalId.value = String(id);
    api('GET', '/api/wecom/customers/' + id).then(function(r) { return r.ok ? r.json() : null; }).then(function(c) {
      if (!c) return;
      if (customerName) customerName.value = c.name || '';
      if (customerBirthday) customerBirthday.value = c.birthday || '';
      if (customerCompany) customerCompany.value = c.company || '';
      if (customerJob) customerJob.value = c.job || '';
      if (customerPhone) customerPhone.value = c.phone || '';
      if (customerWechatId) customerWechatId.value = c.wechat_id || '';
      if (customerRemark) customerRemark.value = c.remark || '';
      showMsg(customerModalMsg, '');
      if (customerModal) customerModal.classList.add('visible');
    });
  }

  document.getElementById('wecomCustomerModalCancel') && document.getElementById('wecomCustomerModalCancel').addEventListener('click', function() { if (customerModal) customerModal.classList.remove('visible'); });
  document.getElementById('wecomCustomerModalSave') && document.getElementById('wecomCustomerModalSave').addEventListener('click', function() {
    var id = customerModalId && customerModalId.value ? parseInt(customerModalId.value, 10) : 0;
    if (!id) return;
    var body = {
      name: (customerName && customerName.value) ? customerName.value.trim() : null,
      birthday: (customerBirthday && customerBirthday.value) ? customerBirthday.value.trim() : null,
      company: (customerCompany && customerCompany.value) ? customerCompany.value.trim() : null,
      job: (customerJob && customerJob.value) ? customerJob.value.trim() : null,
      phone: (customerPhone && customerPhone.value) ? customerPhone.value.trim() : null,
      wechat_id: (customerWechatId && customerWechatId.value) ? customerWechatId.value.trim() : null,
      remark: (customerRemark && customerRemark.value) ? customerRemark.value.trim() : null,
    };
    showMsg(customerModalMsg, '保存中…');
    api('PUT', '/api/wecom/customers/' + id, body).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); }).then(function(x) {
      if (x.ok) {
        if (customerModal) customerModal.classList.remove('visible');
        loadMessageList();
        loadCustomerList();
      } else {
        showMsg(customerModalMsg, (x.data && x.data.detail) || '保存失败', true);
      }
    }).catch(function() { showMsg(customerModalMsg, '请求失败', true); });
  });

  document.getElementById('wecomCustAddBtn') && document.getElementById('wecomCustAddBtn').addEventListener('click', function() {
    var configId = window._wecomDetailConfigId || null;
    if (!configId) {
      var sel = document.getElementById('wecomCustConfigFilter');
      if (sel && sel.value) configId = parseInt(sel.value, 10);
    }
    if (!configId) { alert('请先选择应用'); return; }
    var extId = prompt('请输入客户 external_user_id（企微侧用户标识）或微信号：');
    if (!extId || !extId.trim()) return;
    api('POST', '/api/wecom/customers', { wecom_config_id: parseInt(configId, 10), external_user_id: extId.trim() }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); }).then(function(x) {
      if (x.ok) { loadCustomerList(); openCustomerModal(x.data.id); }
      else alert((x.data && x.data.detail) || '添加失败');
    });
  });

  function loadEnterpriseList() {
    var listEl = document.getElementById('wecomEnterpriseList');
    if (!listEl) return;
    api('GET', '/api/wecom/enterprises').then(function(r) { return r.ok ? r.json() : null; }).then(function(d) {
      var ents = (d && d.items) ? d.items : [];
      if (ents.length === 0) {
        listEl.innerHTML = '<p class="meta">暂无企业。请在「上传资料」中上传 CSV 或于系统配置中创建。</p>';
        return;
      }
      listEl.innerHTML = ents.map(function(e) { return '<div style="margin-bottom:0.5rem;">' + escapeHtml(e.name) + '（ID: ' + e.id + '）</div>'; }).join('');
    });
  }

  document.getElementById('wecomDownloadTemplateBtn') && document.getElementById('wecomDownloadTemplateBtn').addEventListener('click', function(e) {
    e.preventDefault();
    var url = wecomApiBase() + '/api/wecom/material-template';
    if (url.indexOf('/') === 0) url = window.location.origin + url;
    window.open(url, '_blank');
  });

  var uploadInput = document.getElementById('wecomUploadMaterialsInput');
  var uploadResult = document.getElementById('wecomUploadResult');
  if (uploadInput) {
    uploadInput.addEventListener('change', function() {
      if (!uploadInput.files || uploadInput.files.length === 0) return;
      var fd = new FormData();
      fd.append('file', uploadInput.files[0]);
      var opts = { method: 'POST', body: fd, headers: typeof authHeaders === 'function' ? authHeaders() : {} };
      delete opts.headers['Content-Type'];
      showMsg(uploadResult, '上传中…');
      fetch(wecomApiBase() + '/api/wecom/upload-materials', opts).then(function(r) { return r.json(); }).then(function(d) {
        var msg = '导入完成：企业 ' + (d.created_enterprises || 0) + ' 个新增、' + (d.updated_enterprises || 0) + ' 个更新；产品 ' + (d.created_products || 0) + ' 个新增、' + (d.updated_products || 0) + ' 个更新。';
        if (d.errors && d.errors.length) msg += ' 错误: ' + d.errors.join('; ');
        showMsg(uploadResult, msg, d.errors && d.errors.length > 0);
        loadEnterpriseList();
      }).catch(function() { showMsg(uploadResult, '上传失败', true); });
      uploadInput.value = '';
    });
  }

  window.showWecomDetailView = showWecomDetailView;
})();
