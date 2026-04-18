/**
 * 企业微信：通讯录浏览 + 主动发消息 + 群聊管理。
 * 依赖 wecom-detail.js 先加载、DOM 中已有对应容器。
 */
(function () {
  function localApiBase() {
    return (typeof LOCAL_API_BASE !== 'undefined' ? LOCAL_API_BASE : '') || '';
  }
  function api(method, path, body) {
    var opts = { method: method, headers: typeof authHeaders === 'function' ? authHeaders() : {} };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(localApiBase() + path, opts);
  }
  function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
  function showMsg(el, text, isErr) {
    if (!el) return;
    el.textContent = text || '';
    el.className = 'msg' + (isErr ? ' err' : '');
    el.style.display = text ? 'inline' : 'none';
  }

  // ─── 通讯录 ───────────────────────────────────────────────

  var _contactsInited = false;
  var _selectedDeptId = null;

  function loadConfigOptions(selectId) {
    var sel = document.getElementById(selectId);
    if (!sel) return;
    var preselect = window._wecomDetailConfigId || null;
    api('GET', '/api/wecom/configs').then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      var configs = (d && d.configs) ? d.configs : [];
      sel.innerHTML = '<option value="">请选择应用</option>' + configs.map(function (c) {
        return '<option value="' + c.id + '">' + esc(c.name || c.callback_path || c.id) + '</option>';
      }).join('');
      if (preselect) {
        sel.value = String(preselect);
        sel.dispatchEvent(new Event('change'));
      }
    });
  }

  function _getContactsConfigId() {
    if (window._wecomDetailConfigId) return window._wecomDetailConfigId;
    var sel = document.getElementById('wecomContactsConfigFilter');
    return (sel && sel.value) ? parseInt(sel.value, 10) : null;
  }

  function loadDepartments() {
    var cfgId = _getContactsConfigId();
    var treeEl = document.getElementById('wecomDeptTree');
    if (!cfgId) {
      if (treeEl) treeEl.innerHTML = '<p class="meta" style="padding:0.5rem;">请先选择应用</p>';
      return;
    }
    if (treeEl) treeEl.innerHTML = '<p class="meta" style="padding:0.5rem;">加载中…</p>';
    api('GET', '/api/wecom/contacts/departments?config_id=' + cfgId)
      .then(function (r) { return r.ok ? r.json() : r.json().then(function (d) { throw d; }); })
      .then(function (d) {
        var depts = (d && d.departments) ? d.departments : [];
        if (depts.length === 0) {
          treeEl.innerHTML = '<p class="meta" style="padding:0.5rem;">无部门数据</p>';
          return;
        }
        depts.sort(function (a, b) { return (a.order || 0) - (b.order || 0); });
        treeEl.innerHTML = depts.map(function (dept) {
          return '<div class="wecom-dept-item" data-dept-id="' + dept.id + '" style="padding:0.4rem 0.6rem;cursor:pointer;font-size:0.84rem;border-bottom:1px solid rgba(255,255,255,0.05);">' +
            esc(dept.name) + ' <span style="color:var(--text-muted);font-size:0.75rem;">(ID:' + dept.id + ')</span></div>';
        }).join('');
        treeEl.querySelectorAll('.wecom-dept-item').forEach(function (el) {
          el.addEventListener('click', function () {
            _selectedDeptId = parseInt(el.getAttribute('data-dept-id'), 10);
            treeEl.querySelectorAll('.wecom-dept-item').forEach(function (e) { e.style.background = ''; });
            el.style.background = 'rgba(6,182,212,0.15)';
            var titleEl = document.getElementById('wecomUserListTitle');
            if (titleEl) titleEl.textContent = el.textContent.trim();
            loadUsers();
          });
        });
      })
      .catch(function (e) {
        if (treeEl) treeEl.innerHTML = '<p class="msg err" style="padding:0.5rem;">' + esc(e.detail || '加载失败') + '</p>';
      });
  }

  function loadUsers() {
    var cfgId = _getContactsConfigId();
    var listEl = document.getElementById('wecomUserList');
    if (!cfgId || !_selectedDeptId) return;
    if (listEl) listEl.innerHTML = '<p class="meta">加载中…</p>';
    api('GET', '/api/wecom/contacts/users?config_id=' + cfgId + '&department_id=' + _selectedDeptId)
      .then(function (r) { return r.ok ? r.json() : r.json().then(function (d) { throw d; }); })
      .then(function (d) {
        var users = (d && d.users) ? d.users : [];
        if (users.length === 0) {
          listEl.innerHTML = '<p class="meta">该部门无成员</p>';
          return;
        }
        listEl.innerHTML = '<table style="width:100%;font-size:0.84rem;border-collapse:collapse;"><thead><tr>' +
          '<th style="text-align:left;padding:0.35rem 0.5rem;">UserID</th>' +
          '<th style="text-align:left;padding:0.35rem 0.5rem;">姓名</th>' +
          '<th style="text-align:left;padding:0.35rem 0.5rem;">职位</th>' +
          '<th style="text-align:left;padding:0.35rem 0.5rem;">手机</th>' +
          '<th style="text-align:left;padding:0.35rem 0.5rem;">状态</th>' +
          '<th style="padding:0.35rem 0.5rem;">操作</th>' +
          '</tr></thead><tbody>' +
          users.map(function (u) {
            var statusText = u.status === 1 ? '已激活' : u.status === 2 ? '已禁用' : u.status === 4 ? '未激活' : String(u.status);
            var statusColor = u.status === 1 ? 'color:#4ade80;' : 'color:var(--text-muted);';
            return '<tr>' +
              '<td style="padding:0.35rem 0.5rem;font-family:monospace;">' + esc(u.userid) + '</td>' +
              '<td style="padding:0.35rem 0.5rem;">' + esc(u.name) + '</td>' +
              '<td style="padding:0.35rem 0.5rem;">' + esc(u.position || '-') + '</td>' +
              '<td style="padding:0.35rem 0.5rem;">' + esc(u.mobile || '-') + '</td>' +
              '<td style="padding:0.35rem 0.5rem;' + statusColor + '">' + statusText + '</td>' +
              '<td style="padding:0.35rem 0.5rem;text-align:center;"><button type="button" class="btn btn-ghost btn-sm wecom-quick-send" data-userid="' + esc(u.userid) + '" data-name="' + esc(u.name) + '">发消息</button></td>' +
              '</tr>';
          }).join('') + '</tbody></table>';
        listEl.querySelectorAll('.wecom-quick-send').forEach(function (btn) {
          btn.addEventListener('click', function () {
            var uid = btn.getAttribute('data-userid');
            var name = btn.getAttribute('data-name');
            switchToSendTab(uid, name);
          });
        });
      })
      .catch(function (e) {
        if (listEl) listEl.innerHTML = '<p class="msg err">' + esc(e.detail || '加载失败') + '</p>';
      });
  }

  function switchToSendTab(userid, name) {
    var tab = document.querySelector('.wecom-detail-tab[data-wecom-tab="send"]');
    if (tab) tab.click();
    var sendType = document.getElementById('wecomSendType');
    if (sendType) { sendType.value = 'user'; sendType.dispatchEvent(new Event('change')); }
    var toUser = document.getElementById('wecomSendToUser');
    if (toUser) {
      var trySelect = function () {
        for (var i = 0; i < toUser.options.length; i++) {
          toUser.options[i].selected = (toUser.options[i].value === userid);
        }
        _updateRecipientHint(toUser);
      };
      if (_sendUsersLoaded) { trySelect(); }
      else { setTimeout(trySelect, 1500); }
    }
  }

  window.initWecomContacts = function () {
    if (!_contactsInited) {
      var loadBtn = document.getElementById('wecomLoadContactsBtn');
      if (loadBtn) loadBtn.addEventListener('click', loadDepartments);
      _contactsInited = true;
    }
    if (window._wecomDetailConfigId) loadDepartments();
  };

  // ─── 发消息 ───────────────────────────────────────────────

  var _sendInited = false;
  var _sendUsersLoaded = false;
  var _sendDeptsLoaded = false;

  function _getSelectedValues(el) {
    if (!el) return '';
    var vals = [];
    if (el.tagName === 'SELECT') {
      for (var i = 0; i < el.options.length; i++) {
        if (el.options[i].selected) vals.push(el.options[i].value);
      }
    } else {
      var cbs = el.querySelectorAll('input[type="checkbox"]:checked');
      cbs.forEach(function (cb) { vals.push(cb.value); });
    }
    return vals.join('|');
  }

  function _updateRecipientHint(el) {
    var hint = document.getElementById('wecomRecipientHint');
    if (!hint || !el) return;
    var count = 0;
    if (el.tagName === 'SELECT') {
      for (var i = 0; i < el.options.length; i++) { if (el.options[i].selected) count++; }
    } else {
      count = el.querySelectorAll('input[type="checkbox"]:checked').length;
    }
    hint.textContent = count > 0 ? '（已选 ' + count + ' 个）' : '';
  }

  function loadSendUsers() {
    var cfgId = _getSendConfigId();
    var sel = document.getElementById('wecomSendToUser');
    if (!sel || !cfgId) return;
    sel.innerHTML = '<option disabled>加载中…</option>';
    _sendUsersLoaded = false;
    api('GET', '/api/wecom/contacts/departments?config_id=' + cfgId)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var depts = (d && d.departments) ? d.departments : [];
        if (depts.length === 0) return Promise.resolve([]);
        var fetches = depts.map(function (dept) {
          return api('GET', '/api/wecom/contacts/users?config_id=' + cfgId + '&department_id=' + dept.id)
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (ud) {
              return ((ud && ud.users) || []).map(function (u) { u._dept_name = dept.name; return u; });
            })
            .catch(function () { return []; });
        });
        return Promise.all(fetches).then(function (results) {
          var all = [];
          var seen = {};
          results.forEach(function (arr) {
            arr.forEach(function (u) {
              if (!seen[u.userid]) { seen[u.userid] = true; all.push(u); }
            });
          });
          return all;
        });
      })
      .then(function (users) {
        if (!users || users.length === 0) {
          sel.innerHTML = '<span style="color:var(--text-muted);font-size:0.82rem;">无可用成员（请先在通讯录 tab 加载）</span>';
          return;
        }
        sel.innerHTML = users.map(function (u) {
          var label = u.name + ' (' + u.userid + ')';
          if (u._dept_name) label += ' - ' + u._dept_name;
          return '<label style="display:flex;align-items:center;gap:0.4rem;padding:0.2rem 0;cursor:pointer;">' +
            '<input type="checkbox" value="' + esc(u.userid) + '" class="_send-user-cb" style="accent-color:var(--accent);">' +
            '<span>' + esc(label) + '</span></label>';
        }).join('');
        _sendUsersLoaded = true;
      })
      .catch(function () {
        sel.innerHTML = '<span style="color:var(--text-muted);font-size:0.82rem;">加载失败</span>';
      });
  }

  function loadSendDepts() {
    var cfgId = _getSendConfigId();
    var sel = document.getElementById('wecomSendToParty');
    if (!sel || !cfgId) return;
    sel.innerHTML = '<option disabled>加载中…</option>';
    _sendDeptsLoaded = false;
    api('GET', '/api/wecom/contacts/departments?config_id=' + cfgId)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var depts = (d && d.departments) ? d.departments : [];
        if (depts.length === 0) {
          sel.innerHTML = '<option disabled>无部门数据</option>';
          return;
        }
        depts.sort(function (a, b) { return (a.order || 0) - (b.order || 0); });
        sel.innerHTML = depts.map(function (dept) {
          return '<option value="' + dept.id + '">' + esc(dept.name) + ' (ID:' + dept.id + ')</option>';
        }).join('');
        _sendDeptsLoaded = true;
      })
      .catch(function () {
        sel.innerHTML = '<option disabled>加载失败</option>';
      });
  }

  var _sendKfCustomersLoaded = false;

  function updateRecipientFields() {
    var type = (document.getElementById('wecomSendType') || {}).value || 'user';
    var toUser = document.getElementById('wecomSendToUser');
    var toParty = document.getElementById('wecomSendToParty');
    var chatId = document.getElementById('wecomSendChatId');
    var toKfCustomer = document.getElementById('wecomSendKfWrap');
    if (toUser) toUser.style.display = type === 'user' ? '' : 'none';
    if (toParty) toParty.style.display = type === 'party' ? '' : 'none';
    if (chatId) chatId.style.display = type === 'group' ? '' : 'none';
    if (toKfCustomer) toKfCustomer.style.display = type === 'kf_customer' ? '' : 'none';
    var createGroupBtn = document.getElementById('wecomCreateGroupBtn');
    if (createGroupBtn) createGroupBtn.style.display = type === 'group' ? '' : 'none';
    var hint = document.getElementById('wecomRecipientHint');
    if (hint) hint.textContent = '';
    var hintText = document.getElementById('wecomRecipientHintText');
    if (hintText) {
      var hints = {
        user: '勾选要发送的成员',
        party: '按住 Ctrl 或 Shift 可多选部门',
        group: '发给群聊需先创建群聊获得 chatid',
        kf_customer: '勾选客户（48小时内可主动发消息）'
      };
      hintText.textContent = hints[type] || '';
    }
    if (type === 'user' && !_sendUsersLoaded) loadSendUsers();
    if (type === 'party' && !_sendDeptsLoaded) loadSendDepts();
    if (type === 'kf_customer' && !_sendKfCustomersLoaded) loadSendKfCustomers();
  }

  var _sendKfAllCustomers = [];
  var _sendKfGroups = [];

  function loadSendKfCustomers() {
    var sel = document.getElementById('wecomSendToKfCustomer');
    if (!sel) return;
    sel.innerHTML = '<option disabled>加载中…</option>';
    var groupSel = document.getElementById('wecomSendKfGroupFilter');
    var groupsP = api('GET', '/api/wecom/kf/groups')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { _sendKfGroups = (d && d.groups) || []; });
    var customersP = api('GET', '/api/wecom/kf/accounts?config_id=' + (_getSendConfigId() || 0))
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || !d.accounts || !d.accounts.length) return;
        var kfIds = d.accounts.map(function (a) { return a.id; });
        var allCustomers = [];
        var fetches = kfIds.map(function (id) {
          return api('GET', '/api/wecom/kf/customers?kf_account_id=' + id)
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (cd) {
              if (cd && cd.customers) {
                cd.customers.forEach(function (c) { c._kf_account_id = id; });
                allCustomers = allCustomers.concat(cd.customers);
              }
            });
        });
        return Promise.all(fetches).then(function () { _sendKfAllCustomers = allCustomers; });
      });
    Promise.all([groupsP, customersP]).then(function () {
      _renderSendKfGroupFilter();
      _renderSendKfCustomerList('');
      _sendKfCustomersLoaded = true;
    }).catch(function () { sel.innerHTML = '<option disabled>加载失败</option>'; });
  }

  function _renderSendKfGroupFilter() {
    var groupSel = document.getElementById('wecomSendKfGroupFilter');
    if (!groupSel) return;
    var html = '<option value="">全部客户 (' + _sendKfAllCustomers.length + ')</option>';
    _sendKfGroups.forEach(function (g) {
      html += '<option value="' + g.id + '">' + g.name + ' (' + (g.count || 0) + ')</option>';
    });
    html += '<option value="0">未分组</option>';
    groupSel.innerHTML = html;
  }

  function _renderSendKfCustomerList(groupId) {
    var wrap = document.getElementById('wecomSendToKfCustomer');
    if (!wrap) return;
    var filtered = _sendKfAllCustomers;
    if (groupId === '0') {
      filtered = filtered.filter(function (c) { return !c.group_id; });
    } else if (groupId) {
      var gid = parseInt(groupId);
      filtered = filtered.filter(function (c) { return c.group_id === gid; });
    }
    if (!filtered.length) {
      wrap.innerHTML = '<span style="color:var(--text-muted);font-size:0.82rem;">暂无客户</span>';
      return;
    }
    var html = '';
    filtered.forEach(function (c) {
      var groupTag = c.group_name ? ' [' + c.group_name + ']' : '';
      var label = (c.nickname || c.external_userid) + groupTag + (c.last_msg_time ? ' (' + new Date(c.last_msg_time).toLocaleDateString('zh-CN') + ')' : '');
      html += '<label style="display:flex;align-items:center;gap:0.4rem;padding:0.2rem 0;cursor:pointer;">' +
        '<input type="checkbox" value="' + c._kf_account_id + ':' + c.external_userid + '" class="_send-kf-cb" style="accent-color:var(--accent);">' +
        '<span>' + esc(label) + '</span></label>';
    });
    wrap.innerHTML = html;
  }

  function _getSendConfigId() {
    if (window._wecomDetailConfigId) return window._wecomDetailConfigId;
    var sel = document.getElementById('wecomSendConfigFilter');
    return (sel && sel.value) ? parseInt(sel.value, 10) : null;
  }

  var _sendPendingAttach = null;

  function _initSendAttach() {
    var attachBtn = document.getElementById('wecomSendAttachBtn');
    var attachInput = document.getElementById('wecomSendAttachInput');
    var preview = document.getElementById('wecomSendAttachPreview');
    var imgEl = document.getElementById('wecomSendAttachImg');
    var nameEl = document.getElementById('wecomSendAttachFileName');
    var nameSpan = document.getElementById('wecomSendAttachName');
    var removeBtn = document.getElementById('wecomSendAttachRemove');
    if (!attachBtn || !attachInput) return;
    attachBtn.addEventListener('click', function () { attachInput.click(); });
    attachInput.addEventListener('change', function () {
      var f = attachInput.files && attachInput.files[0];
      if (!f) return;
      if (f.size > 20 * 1024 * 1024) { alert('文件不能超过 20MB'); attachInput.value = ''; return; }
      var isImg = /^image\//.test(f.type);
      var isVideo = /^video\//.test(f.type);
      _sendPendingAttach = { file: f, type: isImg ? 'image' : (isVideo ? 'video' : 'file') };
      if (nameSpan) nameSpan.textContent = f.name;
      if (preview) preview.style.display = '';
      if (isImg && imgEl) {
        imgEl.src = URL.createObjectURL(f); imgEl.style.display = '';
        if (nameEl) nameEl.style.display = 'none';
      } else {
        if (imgEl) imgEl.style.display = 'none';
        if (nameEl) { nameEl.textContent = (isVideo ? '🎬 ' : '📄 ') + f.name; nameEl.style.display = ''; }
      }
    });
    if (removeBtn) removeBtn.addEventListener('click', function () {
      _sendPendingAttach = null;
      attachInput.value = '';
      if (preview) preview.style.display = 'none';
      if (imgEl) { imgEl.style.display = 'none'; imgEl.src = ''; }
      if (nameEl) nameEl.style.display = 'none';
      if (nameSpan) nameSpan.textContent = '';
    });
  }

  function _clearSendAttach() {
    _sendPendingAttach = null;
    var inp = document.getElementById('wecomSendAttachInput');
    if (inp) inp.value = '';
    var p = document.getElementById('wecomSendAttachPreview');
    if (p) p.style.display = 'none';
    var im = document.getElementById('wecomSendAttachImg');
    if (im) { im.style.display = 'none'; im.src = ''; }
    var fn = document.getElementById('wecomSendAttachFileName');
    if (fn) fn.style.display = 'none';
    var ns = document.getElementById('wecomSendAttachName');
    if (ns) ns.textContent = '';
  }

  function _uploadMediaThenSend(configId, cbPath, mediaType, file, sendPayload, msgEl) {
    showMsg(msgEl, '正在上传附件…');
    var fd = new FormData();
    fd.append('file', file);
    return fetch('/api/wecom/media/upload?config_id=' + configId + '&media_type=' + mediaType, {
      method: 'POST', body: fd, headers: { 'Authorization': 'Bearer ' + (localStorage.getItem('token') || '') }
    })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
    .then(function (x) {
      if (!x.ok) { showMsg(msgEl, (x.data && x.data.detail) || '上传失败', true); return Promise.reject('upload_fail'); }
      sendPayload.media_id = x.data.media_id;
      sendPayload.msg_type = mediaType;
      sendPayload.content = x.data.local_url || '';
      return sendPayload;
    });
  }

  function sendMessage() {
    var msgEl = document.getElementById('wecomSendMsg');
    var configId = _getSendConfigId();
    if (!configId) { showMsg(msgEl, '请选择应用', true); return; }
    var type = (document.getElementById('wecomSendType') || {}).value || 'user';
    var content = (document.getElementById('wecomSendContent') || {}).value || '';
    var hasAttach = !!_sendPendingAttach;
    console.log('[wecom-contacts v20260416d] sendMessage type=' + type + ' content.length=' + content.length + ' hasAttach=' + hasAttach);
    if (!content.trim() && !hasAttach) { showMsg(msgEl, '请输入消息内容或选择附件', true); return; }

    if (type === 'kf_customer') {
      console.log('[wecom-contacts] entering kf_customer branch');
      var toKfWrap = document.getElementById('wecomSendToKfCustomer');
      var kfTargets = [];
      if (toKfWrap) {
        toKfWrap.querySelectorAll('input[type="checkbox"]:checked').forEach(function (cb) {
          kfTargets.push(cb.value);
        });
      }
      console.log('[wecom-contacts] kfTargets:', kfTargets);
      if (!kfTargets.length) { showMsg(msgEl, '请选择客户', true); return; }
      if (!content.trim() && !hasAttach) { showMsg(msgEl, '请输入消息内容或选择附件', true); return; }

      var kfMediaId = null;
      var kfMediaType = null;
      var uploadPromise = Promise.resolve();
      if (hasAttach) {
        kfMediaType = _sendPendingAttach.type;
        console.log('[wecom-contacts] uploading media, type=' + kfMediaType + ' file=' + _sendPendingAttach.file.name);
        uploadPromise = new Promise(function (resolve, reject) {
          showMsg(msgEl, '正在上传附件…');
          var fd = new FormData();
          fd.append('file', _sendPendingAttach.file);
          var uploadUrl = localApiBase() + '/api/wecom/media/upload?config_id=' + configId + '&media_type=' + kfMediaType;
          console.log('[wecom-contacts] upload URL:', uploadUrl);
          fetch(uploadUrl, {
            method: 'POST', body: fd, headers: { 'Authorization': 'Bearer ' + (localStorage.getItem('token') || '') }
          }).then(function (r) {
            console.log('[wecom-contacts] upload response status:', r.status);
            return r.json();
          })
            .then(function (d) {
              console.log('[wecom-contacts] upload result:', d);
              if (d && d.media_id) { kfMediaId = d.media_id; resolve(); }
              else {
                var errText = '上传失败';
                if (d && d.detail) {
                  try { var inner = JSON.parse(d.detail); errText = inner.detail || d.detail; } catch (e2) { errText = d.detail; }
                }
                if (errText.length > 80) errText = errText.substring(0, 80) + '…';
                showMsg(msgEl, errText, true);
                alert('上传失败: ' + errText);
                reject('upload_fail');
              }
            })
            .catch(function (e) {
              console.error('[wecom-contacts] upload error:', e);
              showMsg(msgEl, '上传失败', true);
              reject('upload_fail');
            });
        });
      }

      console.log('[wecom-contacts] starting send chain, hasAttach=' + hasAttach);

      uploadPromise.then(function () {
        showMsg(msgEl, '发送中… (0/' + kfTargets.length + ')');
        var kfOk = 0; var kfFail = 0;
        var kfChain = Promise.resolve();
        kfTargets.forEach(function (val) {
          kfChain = kfChain.then(function () {
            var colonIdx = val.indexOf(':');
            var kfAccountId = parseInt(val.substring(0, colonIdx), 10);
            var externalUserid = val.substring(colonIdx + 1);
            var sendItems = [];
            if (kfMediaId) {
              sendItems.push({ kf_account_id: kfAccountId, external_userid: externalUserid, msgtype: kfMediaType, media_id: kfMediaId });
            }
            if (content.trim()) {
              sendItems.push({ kf_account_id: kfAccountId, external_userid: externalUserid, msgtype: 'text', content: content.trim() });
            }
            var itemChain = Promise.resolve();
            sendItems.forEach(function (item) {
              itemChain = itemChain.then(function () {
                return api('POST', '/api/wecom/kf/send', item)
                  .then(function (r) { return r.json(); })
                  .then(function (d) { if (!d || !d.ok) kfFail++; else kfOk++; })
                  .catch(function () { kfFail++; });
              });
            });
            return itemChain.then(function () {
              showMsg(msgEl, '发送中… (' + (kfOk + kfFail) + '/' + (kfTargets.length * sendItems.length) + ')');
            });
          });
        });
        return kfChain.then(function () {
          if (kfFail === 0) {
            showMsg(msgEl, '发送成功，共 ' + kfOk + ' 条');
            document.getElementById('wecomSendContent').value = '';
            _clearSendAttach();
          } else {
            showMsg(msgEl, '成功 ' + kfOk + ' 条，失败 ' + kfFail + ' 条', true);
          }
        });
      }).catch(function (e) { if (e !== 'upload_fail') showMsg(msgEl, '网络错误', true); });
      return;
    }

    if (type === 'group') {
      var chatid = (document.getElementById('wecomSendChatId') || {}).value || '';
      if (!chatid.trim()) { showMsg(msgEl, '请输入群聊 chatid', true); return; }
      showMsg(msgEl, '发送中…');
      var groupSend = function (payload) {
        return api('POST', '/api/wecom/group-chat/send', payload)
          .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); });
      };
      var promises = [];
      if (hasAttach) {
        var gPayload = { config_id: configId, chatid: chatid.trim(), msg_type: _sendPendingAttach.type, content: '' };
        promises.push(
          _uploadMediaThenSend(configId, null, _sendPendingAttach.type, _sendPendingAttach.file, gPayload, msgEl)
            .then(function (p) { return groupSend(p); })
        );
      }
      if (content.trim()) {
        promises.push(groupSend({ config_id: configId, chatid: chatid.trim(), msg_type: 'text', content: content.trim() }));
      }
      Promise.all(promises).then(function (results) {
        var allOk = results.every(function (r) { return r.ok; });
        if (allOk) { showMsg(msgEl, '发送成功'); document.getElementById('wecomSendContent').value = ''; _clearSendAttach(); }
        else { var fail = results.find(function (r) { return !r.ok; }); showMsg(msgEl, (fail && fail.data && fail.data.detail) || '发送失败', true); }
      }).catch(function (e) { if (e !== 'upload_fail') showMsg(msgEl, '网络错误', true); });
    } else {
      var toUser = type === 'user' ? _getSelectedValues(document.getElementById('wecomSendToUser')) : '';
      var toParty = type === 'party' ? _getSelectedValues(document.getElementById('wecomSendToParty')) : '';
      if (!toUser && !toParty) { showMsg(msgEl, '请选择接收者', true); return; }
      showMsg(msgEl, '发送中…');
      var doSend = function (payload) {
        return api('POST', '/api/wecom/send-message', payload)
          .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); });
      };
      var promises2 = [];
      if (hasAttach) {
        var aPayload = { config_id: configId, to_user: toUser || undefined, to_party: toParty || undefined, msg_type: _sendPendingAttach.type, content: '' };
        promises2.push(
          _uploadMediaThenSend(configId, null, _sendPendingAttach.type, _sendPendingAttach.file, aPayload, msgEl)
            .then(function (p) { return doSend(p); })
        );
      }
      if (content.trim()) {
        promises2.push(doSend({ config_id: configId, to_user: toUser || undefined, to_party: toParty || undefined, msg_type: 'text', content: content.trim() }));
      }
      Promise.all(promises2).then(function (results) {
        var allOk = results.every(function (r) { return r.ok; });
        if (allOk) {
          var warn = '';
          results.forEach(function (r) {
            if (r.data && r.data.invaliduser) warn += ' 无效用户: ' + r.data.invaliduser;
            if (r.data && r.data.invalidparty) warn += ' 无效部门: ' + r.data.invalidparty;
          });
          showMsg(msgEl, '发送成功' + warn, !!warn);
          document.getElementById('wecomSendContent').value = '';
          _clearSendAttach();
        } else {
          var fail = results.find(function (r) { return !r.ok; });
          showMsg(msgEl, (fail && fail.data && fail.data.detail) || '发送失败', true);
        }
      }).catch(function (e) { if (e !== 'upload_fail') showMsg(msgEl, '网络错误', true); });
    }
  }

  function createGroupChat() {
    var msgEl = document.getElementById('wecomCreateGroupMsg');
    var cfgId = _getSendConfigId();
    if (!cfgId) { showMsg(msgEl, '请选择应用', true); return; }
    var name = (document.getElementById('wecomGroupName') || {}).value || '';
    var members = (document.getElementById('wecomGroupMembers') || {}).value || '';
    var owner = (document.getElementById('wecomGroupOwner') || {}).value || '';
    var memberList = members.split(/[,，]/).map(function (s) { return s.trim(); }).filter(Boolean);
    if (memberList.length < 2) { showMsg(msgEl, '至少需要 2 个成员', true); return; }
    showMsg(msgEl, '创建中…');
    api('POST', '/api/wecom/group-chat/create', {
      config_id: cfgId,
      name: name.trim(),
      userlist: memberList,
      owner: owner.trim() || undefined
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (x) {
        if (x.ok) {
          showMsg(msgEl, '创建成功！chatid: ' + x.data.chatid);
          var chatIdInput = document.getElementById('wecomSendChatId');
          if (chatIdInput) chatIdInput.value = x.data.chatid;
        } else {
          showMsg(msgEl, (x.data && x.data.detail) || '创建失败', true);
        }
      })
      .catch(function () { showMsg(msgEl, '网络错误', true); });
  }

  // ─── 定时发布 ───────────────────────────────────────────────

  function _getScheduleWeekdays() {
    var checks = document.querySelectorAll('#wecomScheduleWeekdays input[type="checkbox"]');
    var days = [];
    for (var i = 0; i < checks.length; i++) {
      if (checks[i].checked) days.push(checks[i].value);
    }
    return days.join(',');
  }

  function createScheduledMessage() {
    var msgEl = document.getElementById('wecomScheduleMsg');
    var configId = _getSendConfigId();
    if (!configId) { showMsg(msgEl, '请选择应用', true); return; }
    var type = (document.getElementById('wecomSendType') || {}).value || 'user';
    var content = (document.getElementById('wecomSendContent') || {}).value || '';
    if (!content.trim()) { showMsg(msgEl, '请输入消息内容', true); return; }
    var weekdays = _getScheduleWeekdays();
    if (!weekdays) { showMsg(msgEl, '请选择至少一个星期', true); return; }
    var sendTime = (document.getElementById('wecomScheduleTime') || {}).value || '';
    if (!sendTime) { showMsg(msgEl, '请设置发送时间', true); return; }

    var toUser = type === 'user' ? _getSelectedValues(document.getElementById('wecomSendToUser')) : null;
    var toParty = type === 'party' ? _getSelectedValues(document.getElementById('wecomSendToParty')) : null;
    var chatid = type === 'group' ? ((document.getElementById('wecomSendChatId') || {}).value || '').trim() : null;
    if (type === 'user' && !toUser) { showMsg(msgEl, '请选择接收者', true); return; }
    if (type === 'party' && !toParty) { showMsg(msgEl, '请选择部门', true); return; }
    if (type === 'group' && !chatid) { showMsg(msgEl, '请输入群聊 chatid', true); return; }

    showMsg(msgEl, '创建中…');
    api('POST', '/api/wecom/scheduled-messages', {
      wecom_config_id: configId,
      send_type: type,
      to_user: toUser || undefined,
      to_party: toParty || undefined,
      chatid: chatid || undefined,
      msg_type: 'text',
      content: content.trim(),
      weekdays: weekdays,
      send_time: sendTime,
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (x) {
        if (x.ok) {
          showMsg(msgEl, '定时任务已创建');
          var panel = document.getElementById('wecomSchedulePanel');
          if (panel) setTimeout(function () { panel.style.display = 'none'; }, 1500);
          loadScheduledMessages();
        } else {
          showMsg(msgEl, (x.data && x.data.detail) || '创建失败', true);
        }
      })
      .catch(function () { showMsg(msgEl, '网络错误', true); });
  }

  var _dayNames = { '1': '周一', '2': '周二', '3': '周三', '4': '周四', '5': '周五', '6': '周六', '7': '周日' };

  function loadScheduledMessages() {
    var listEl = document.getElementById('wecomScheduledList');
    if (!listEl) return;
    api('GET', '/api/wecom/scheduled-messages')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var items = (d && d.items) ? d.items : [];
        if (items.length === 0) {
          listEl.innerHTML = '';
          return;
        }
        listEl.innerHTML = '<h4 style="font-size:0.88rem;margin:0 0 0.5rem 0;color:var(--text);">定时任务</h4>' +
          '<div style="display:flex;flex-direction:column;gap:0.4rem;">' +
          items.map(function (m) {
            var days = (m.weekdays || '').split(',').map(function (d) { return _dayNames[d] || d; }).join(' ');
            var statusColor = m.enabled ? '#4ade80' : 'var(--text-muted)';
            var statusText = m.enabled ? '启用' : '禁用';
            var typeLabel = m.send_type === 'user' ? '个人' : m.send_type === 'party' ? '部门' : '群聊';
            var recipient = m.to_user || m.to_party || m.chatid || '-';
            return '<div style="padding:0.5rem 0.6rem;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:var(--radius-sm);font-size:0.82rem;">' +
              '<div style="display:flex;justify-content:space-between;align-items:center;">' +
                '<div><span style="color:' + statusColor + ';font-weight:600;">[' + statusText + ']</span> ' +
                  '<span style="color:var(--accent);">' + esc(days) + ' ' + esc(m.send_time) + '</span> ' +
                  '<span style="color:var(--text-muted);">' + esc(typeLabel) + ' → ' + esc(recipient.length > 30 ? recipient.substring(0, 30) + '…' : recipient) + '</span></div>' +
                '<div style="display:flex;gap:0.3rem;">' +
                  '<button type="button" class="btn btn-ghost btn-sm wecom-sched-toggle" data-id="' + m.id + '" style="font-size:0.75rem;">' + (m.enabled ? '禁用' : '启用') + '</button>' +
                  '<button type="button" class="btn btn-ghost btn-sm wecom-sched-delete" data-id="' + m.id + '" style="font-size:0.75rem;color:#f87171;">删除</button>' +
                '</div>' +
              '</div>' +
              '<div style="margin-top:0.25rem;color:var(--text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + esc(m.content) + '</div>' +
              (m.last_sent_at ? '<div style="margin-top:0.15rem;font-size:0.75rem;color:var(--text-muted);">上次发送: ' + esc(m.last_sent_at) + '</div>' : '') +
            '</div>';
          }).join('') + '</div>';
        listEl.querySelectorAll('.wecom-sched-delete').forEach(function (btn) {
          btn.addEventListener('click', function () {
            if (!confirm('确定删除此定时任务？')) return;
            api('DELETE', '/api/wecom/scheduled-messages/' + btn.getAttribute('data-id'))
              .then(function () { loadScheduledMessages(); });
          });
        });
        listEl.querySelectorAll('.wecom-sched-toggle').forEach(function (btn) {
          btn.addEventListener('click', function () {
            api('PUT', '/api/wecom/scheduled-messages/' + btn.getAttribute('data-id') + '/toggle')
              .then(function () { loadScheduledMessages(); });
          });
        });
      });
  }

  window.initWecomSend = function () {
    if (!_sendInited) {
      var sendType = document.getElementById('wecomSendType');
      if (sendType) sendType.addEventListener('change', function () {
        _sendUsersLoaded = false;
        _sendDeptsLoaded = false;
        _sendKfCustomersLoaded = false;
        updateRecipientFields();
      });
      var kfGroupFilter = document.getElementById('wecomSendKfGroupFilter');
      if (kfGroupFilter) kfGroupFilter.addEventListener('change', function () {
        _renderSendKfCustomerList(kfGroupFilter.value);
      });
      var sendBtn = document.getElementById('wecomSendBtn');
      if (sendBtn) sendBtn.addEventListener('click', sendMessage);
      var scheduleBtn = document.getElementById('wecomScheduleBtn');
      if (scheduleBtn) scheduleBtn.addEventListener('click', function () {
        var panel = document.getElementById('wecomSchedulePanel');
        if (panel) panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
      });
      var scheduleSubmit = document.getElementById('wecomScheduleSubmitBtn');
      if (scheduleSubmit) scheduleSubmit.addEventListener('click', createScheduledMessage);
      var scheduleCancel = document.getElementById('wecomScheduleCancelBtn');
      if (scheduleCancel) scheduleCancel.addEventListener('click', function () {
        var panel = document.getElementById('wecomSchedulePanel');
        if (panel) panel.style.display = 'none';
      });
      var createGroupBtn = document.getElementById('wecomCreateGroupBtn');
      if (createGroupBtn) {
        createGroupBtn.style.display = 'none';
        createGroupBtn.addEventListener('click', function () {
          var panel = document.getElementById('wecomCreateGroupPanel');
          if (panel) panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
        });
      }
      var createGroupSubmit = document.getElementById('wecomCreateGroupSubmitBtn');
      if (createGroupSubmit) createGroupSubmit.addEventListener('click', createGroupChat);
      var createGroupCancel = document.getElementById('wecomCreateGroupCancelBtn');
      if (createGroupCancel) createGroupCancel.addEventListener('click', function () {
        var panel = document.getElementById('wecomCreateGroupPanel');
        if (panel) panel.style.display = 'none';
      });
      var toUserWrap = document.getElementById('wecomSendToUser');
      if (toUserWrap) toUserWrap.addEventListener('change', function () { _updateRecipientHint(toUserWrap); });
      var toPartySel = document.getElementById('wecomSendToParty');
      if (toPartySel) toPartySel.addEventListener('change', function () { _updateRecipientHint(toPartySel); });
      _initSendAttach();
      _sendInited = true;
    }
    _sendUsersLoaded = false;
    _sendDeptsLoaded = false;
    updateRecipientFields();
    loadScheduledMessages();
  };
})();
