/* 微信客服管理 — 客服账号 / 客户列表 / 消息 / 手动发送 */
(function () {
  'use strict';

  var selectedKfAccountId = null;
  var selectedKfCustomer = null; // { external_userid, nickname }
  var _kfGroups = []; // [{id, name, count}]
  var _kfSelectedGroupFilter = '';

  function getConfigId() {
    return window._wecomDetailConfigId || null;
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
    return fetch(apiBase() + path, opts);
  }

  function showMsg(id, text, ok) {
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className = 'msg msg-' + (ok ? 'ok' : 'err');
    el.style.display = 'inline';
    setTimeout(function () { el.style.display = 'none'; }, 4000);
  }

  // ── 客服账号列表 ──────────────────────────────────────────────────

  function loadKfAccounts() {
    var configId = getConfigId();
    if (!configId) return;
    api('GET', '/api/wecom/kf/accounts?config_id=' + configId)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        renderKfAccounts(d.accounts || []);
      });
  }

  function renderKfAccounts(accounts) {
    var wrap = document.getElementById('kfAccountList');
    if (!wrap) return;
    if (!accounts.length) {
      wrap.innerHTML = '<p style="color:var(--text-muted);font-size:0.85rem;">暂无客服账号，请创建或从企微同步。</p>';
      return;
    }
    var html = '<div style="display:flex;flex-wrap:wrap;gap:0.75rem;">';
    accounts.forEach(function (a) {
      var sel = a.id === selectedKfAccountId;
      var border = sel ? 'border-color:var(--accent);' : '';
      var bg = sel ? 'background:rgba(6,182,212,0.08);' : '';
      html += '<div class="kf-account-card" data-kf-id="' + a.id + '" style="border:1px solid var(--border);border-radius:var(--radius);padding:0.75rem 1rem;min-width:200px;cursor:pointer;transition:all 0.15s;' + border + bg + '">';
      html += '<div style="font-weight:600;font-size:0.9rem;margin-bottom:0.3rem;">' + escHtml(a.name) + '</div>';
      html += '<div style="font-size:0.75rem;color:var(--text-muted);word-break:break-all;">' + escHtml(a.open_kfid) + '</div>';
      html += '<div style="display:flex;gap:0.5rem;align-items:center;margin-top:0.5rem;flex-wrap:wrap;">';
      html += '<span style="font-size:0.72rem;padding:0.15rem 0.4rem;border-radius:8px;' + (a.auto_reply_enabled ? 'background:rgba(34,197,94,0.15);color:#22c55e;' : 'background:rgba(239,68,68,0.12);color:#ef4444;') + '">' + (a.auto_reply_enabled ? 'AI自动回复' : '手动回复') + '</span>';
      html += '<button type="button" class="btn btn-ghost btn-sm kf-toggle-ar" data-kf-id="' + a.id + '" data-enabled="' + (a.auto_reply_enabled ? '1' : '0') + '" style="font-size:0.7rem;padding:0.1rem 0.35rem;">' + (a.auto_reply_enabled ? '关闭AI' : '开启AI') + '</button>';
      if (a.url) {
        html += '<button type="button" class="btn btn-ghost btn-sm kf-show-qr" data-url="' + escHtml(a.url) + '" style="font-size:0.7rem;padding:0.1rem 0.35rem;">二维码</button>';
      }
      html += '<button type="button" class="btn btn-ghost btn-sm kf-del" data-kf-id="' + a.id + '" style="font-size:0.7rem;padding:0.1rem 0.35rem;color:#ef4444;">删除</button>';
      html += '</div>';
      html += '</div>';
    });
    html += '</div>';
    wrap.innerHTML = html;

    wrap.querySelectorAll('.kf-account-card').forEach(function (card) {
      card.addEventListener('click', function (e) {
        if (e.target.closest('button')) return;
        var id = parseInt(card.getAttribute('data-kf-id'));
        selectKfAccount(id);
      });
    });

    wrap.querySelectorAll('.kf-toggle-ar').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        var id = parseInt(btn.getAttribute('data-kf-id'));
        var cur = btn.getAttribute('data-enabled') === '1';
        api('POST', '/api/wecom/kf/account/auto-reply', { kf_account_id: id, enabled: !cur })
          .then(function (r) { return r.json(); })
          .then(function () { loadKfAccounts(); });
      });
    });

    wrap.querySelectorAll('.kf-show-qr').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        var url = btn.getAttribute('data-url');
        showKfQrCode(url);
      });
    });

    wrap.querySelectorAll('.kf-del').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        if (!confirm('确定删除此客服账号（本地记录）？')) return;
        var id = parseInt(btn.getAttribute('data-kf-id'));
        api('POST', '/api/wecom/kf/account/delete', { kf_account_id: id, delete_remote: false })
          .then(function () {
            if (selectedKfAccountId === id) {
              selectedKfAccountId = null;
              document.getElementById('kfChatArea').style.display = 'none';
            }
            loadKfAccounts();
          });
      });
    });
  }

  function selectKfAccount(id) {
    selectedKfAccountId = id;
    selectedKfCustomer = null;
    loadKfAccounts();
    document.getElementById('kfChatArea').style.display = 'block';
    document.getElementById('kfSendBar').style.display = 'none';
    document.getElementById('kfMsgTitle').textContent = '请选择客户';
    document.getElementById('kfMessageList').innerHTML = '';
    loadKfCustomers();

    api('POST', '/api/wecom/kf/pull', { kf_account_id: id }).then(function () {
      loadKfCustomers();
    });
  }

  // ── 客户列表 ──────────────────────────────────────────────────────

  function loadKfCustomers() {
    if (!selectedKfAccountId) return;
    var url = '/api/wecom/kf/customers?kf_account_id=' + selectedKfAccountId;
    if (_kfSelectedGroupFilter) url += '&group_id=' + _kfSelectedGroupFilter;
    api('GET', url)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        renderKfCustomers(d.customers || []);
      });
  }

  function renderKfCustomers(customers) {
    var wrap = document.getElementById('kfCustomerList');
    if (!wrap) return;
    if (!customers.length) {
      wrap.innerHTML = '<div style="padding:1rem;text-align:center;color:var(--text-muted);font-size:0.82rem;">暂无客户<br><span style="font-size:0.75rem;">等待外部用户扫码咨询</span></div>';
      return;
    }
    var html = '';
    customers.forEach(function (c) {
      var sel = selectedKfCustomer && selectedKfCustomer.external_userid === c.external_userid;
      var bg = sel ? 'background:rgba(6,182,212,0.1);' : '';
      var name = c.nickname || c.external_userid;
      var timeStr = c.last_msg_time ? new Date(c.last_msg_time).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '';
      var groupTag = c.group_name ? '<span style="font-size:0.65rem;background:rgba(6,182,212,0.15);color:var(--accent);padding:0.05rem 0.3rem;border-radius:3px;margin-left:0.3rem;">' + escHtml(c.group_name) + '</span>' : '';
      html += '<div class="kf-customer-item" data-ext="' + escHtml(c.external_userid) + '" data-nick="' + escHtml(c.nickname || '') + '" data-cid="' + c.id + '" data-gid="' + (c.group_id || '') + '" style="padding:0.5rem 0.75rem;cursor:pointer;border-bottom:1px solid rgba(255,255,255,0.05);transition:background 0.1s;' + bg + '">';
      html += '<div style="display:flex;justify-content:space-between;align-items:center;">';
      html += '<span style="font-size:0.85rem;font-weight:500;">' + escHtml(name) + groupTag + '</span>';
      html += '<span style="font-size:0.7rem;color:var(--text-muted);">' + timeStr + '</span>';
      html += '</div>';
      html += '</div>';
    });
    wrap.innerHTML = html;

    wrap.querySelectorAll('.kf-customer-item').forEach(function (item) {
      item.addEventListener('click', function () {
        selectedKfCustomer = {
          external_userid: item.getAttribute('data-ext'),
          nickname: item.getAttribute('data-nick') || item.getAttribute('data-ext')
        };
        loadKfCustomers();
        loadKfMessages();
        document.getElementById('kfSendBar').style.display = 'block';
        document.getElementById('kfMsgTitle').textContent = selectedKfCustomer.nickname;
      });
    });
  }

  // ── 消息列表 ──────────────────────────────────────────────────────

  function loadKfMessages() {
    if (!selectedKfAccountId || !selectedKfCustomer) return;
    api('GET', '/api/wecom/kf/messages?kf_account_id=' + selectedKfAccountId + '&external_userid=' + encodeURIComponent(selectedKfCustomer.external_userid) + '&limit=50')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        renderKfMessages(d.messages || []);
      });
  }

  function renderKfMessages(messages) {
    var wrap = document.getElementById('kfMessageList');
    if (!wrap) return;
    if (!messages.length) {
      wrap.innerHTML = '<div style="padding:2rem;text-align:center;color:var(--text-muted);font-size:0.82rem;">暂无消息</div>';
      return;
    }
    var html = '';
    messages.forEach(function (m) {
      var isIn = m.direction === 'in';
      var align = isIn ? 'flex-start' : 'flex-end';
      var bg = isIn ? 'rgba(255,255,255,0.06)' : 'rgba(6,182,212,0.12)';
      var timeStr = m.send_time ? new Date(m.send_time).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
      html += '<div style="display:flex;justify-content:' + align + ';margin-bottom:0.5rem;">';
      html += '<div style="max-width:70%;padding:0.5rem 0.75rem;border-radius:8px;background:' + bg + ';word-break:break-word;">';
      html += '<div style="font-size:0.85rem;line-height:1.5;white-space:pre-wrap;">' + escHtml(m.content) + '</div>';
      html += '<div style="font-size:0.68rem;color:var(--text-muted);margin-top:0.2rem;text-align:right;">' + timeStr + '</div>';
      html += '</div></div>';
    });
    wrap.innerHTML = html;
    wrap.scrollTop = wrap.scrollHeight;
  }

  // ── 发送消息 ──────────────────────────────────────────────────────

  function sendKfMessage() {
    if (!selectedKfAccountId || !selectedKfCustomer) return;
    var input = document.getElementById('kfSendInput');
    var text = (input.value || '').trim();
    if (!text) return;
    input.value = '';
    api('POST', '/api/wecom/kf/send', {
      kf_account_id: selectedKfAccountId,
      external_userid: selectedKfCustomer.external_userid,
      content: text
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok) {
        loadKfMessages();
      } else {
        showMsg('kfSendMsg', d.detail || '发送失败', false);
      }
    }).catch(function () {
      showMsg('kfSendMsg', '发送失败', false);
    });
  }

  // ── 创建客服账号 ──────────────────────────────────────────────────

  function createKfAccount() {
    var configId = getConfigId();
    if (!configId) return showMsg('kfStatusMsg', '请先选择企微应用', false);
    var name = prompt('客服账号名称：', 'AI客服');
    if (!name) return;
    showMsg('kfStatusMsg', '创建中…', true);
    api('POST', '/api/wecom/kf/account/create', { config_id: configId, name: name })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) {
          showMsg('kfStatusMsg', '创建成功', true);
          loadKfAccounts();
        } else {
          showMsg('kfStatusMsg', d.detail || '创建失败', false);
        }
      })
      .catch(function () { showMsg('kfStatusMsg', '创建失败', false); });
  }

  function syncKfAccounts() {
    var configId = getConfigId();
    if (!configId) return showMsg('kfStatusMsg', '请先选择企微应用', false);
    showMsg('kfStatusMsg', '同步中…', true);
    api('POST', '/api/wecom/kf/account/sync', { config_id: configId })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) {
          showMsg('kfStatusMsg', '同步完成，新增 ' + (d.synced || 0) + ' 个账号', true);
          renderKfAccounts(d.accounts || []);
        } else {
          showMsg('kfStatusMsg', d.detail || '同步失败', false);
        }
      })
      .catch(function () { showMsg('kfStatusMsg', '同步失败', false); });
  }

  // ── 二维码弹窗 ────────────────────────────────────────────────────

  function showKfQrCode(url) {
    var overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;z-index:9999;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;';
    var box = document.createElement('div');
    box.style.cssText = 'background:var(--card-bg,#1e293b);border-radius:12px;padding:1.5rem;max-width:360px;text-align:center;';
    box.innerHTML = '<h3 style="margin:0 0 0.75rem;">客服二维码</h3>' +
      '<p style="font-size:0.82rem;color:var(--text-muted);margin-bottom:1rem;word-break:break-all;">' + escHtml(url) + '</p>' +
      '<img src="https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=' + encodeURIComponent(url) + '" style="max-width:200px;border-radius:8px;background:#fff;padding:8px;" />' +
      '<div style="margin-top:1rem;display:flex;gap:0.5rem;justify-content:center;">' +
      '<button type="button" class="btn btn-ghost btn-sm" id="_kfCopyUrl">复制链接</button>' +
      '<button type="button" class="btn btn-primary btn-sm" id="_kfCloseQr">关闭</button>' +
      '</div>';
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    overlay.querySelector('#_kfCloseQr').addEventListener('click', function () { overlay.remove(); });
    overlay.addEventListener('click', function (e) { if (e.target === overlay) overlay.remove(); });
    overlay.querySelector('#_kfCopyUrl').addEventListener('click', function () {
      navigator.clipboard.writeText(url).then(function () { alert('已复制'); });
    });
  }

  // ── 工具 ──────────────────────────────────────────────────────────

  function escHtml(s) {
    var d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
  }

  // ── 客户分组 ──────────────────────────────────────────────────────

  function loadKfGroups() {
    api('GET', '/api/wecom/kf/groups')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        _kfGroups = (d && d.groups) || [];
        renderGroupFilter();
        renderGroupModal();
      });
  }

  function renderGroupFilter() {
    var sel = document.getElementById('kfGroupFilter');
    if (!sel) return;
    var html = '<option value="">全部客户</option>';
    _kfGroups.forEach(function (g) {
      html += '<option value="' + g.id + '"' + (_kfSelectedGroupFilter == g.id ? ' selected' : '') + '>' + escHtml(g.name) + ' (' + (g.count || 0) + ')</option>';
    });
    html += '<option value="0">未分组</option>';
    sel.innerHTML = html;
  }

  function renderGroupModal() {
    var list = document.getElementById('kfGroupList');
    if (!list) return;
    if (!_kfGroups.length) {
      list.innerHTML = '<div style="padding:0.5rem;text-align:center;color:var(--text-muted);font-size:0.82rem;">暂无分组</div>';
      return;
    }
    var html = '';
    _kfGroups.forEach(function (g) {
      html += '<div style="display:flex;justify-content:space-between;align-items:center;padding:0.35rem 0.5rem;border-bottom:1px solid rgba(255,255,255,0.05);">';
      html += '<span style="font-size:0.85rem;">' + escHtml(g.name) + ' <span style="color:var(--text-muted);font-size:0.75rem;">(' + (g.count || 0) + '人)</span></span>';
      html += '<div style="display:flex;gap:0.2rem;">';
      html += '<button type="button" class="btn btn-ghost btn-sm kf-grp-rename" data-gid="' + g.id + '" data-name="' + escHtml(g.name) + '" style="font-size:0.72rem;">改名</button>';
      html += '<button type="button" class="btn btn-ghost btn-sm kf-grp-del" data-gid="' + g.id + '" style="font-size:0.72rem;color:#f87171;">删除</button>';
      html += '</div></div>';
    });
    list.innerHTML = html;
    list.querySelectorAll('.kf-grp-rename').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var n = prompt('新名称', btn.getAttribute('data-name'));
        if (n && n.trim()) {
          api('PUT', '/api/wecom/kf/groups', { group_id: parseInt(btn.getAttribute('data-gid')), name: n.trim() })
            .then(function () { loadKfGroups(); loadKfCustomers(); });
        }
      });
    });
    list.querySelectorAll('.kf-grp-del').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (confirm('删除此分组？客户将变为未分组。')) {
          api('DELETE', '/api/wecom/kf/groups/' + btn.getAttribute('data-gid'))
            .then(function () { loadKfGroups(); loadKfCustomers(); });
        }
      });
    });
  }

  function showGroupAssignMenu(customerId, currentGroupName) {
    var name = prompt('输入分组名称（留空则取消分组）', currentGroupName || '');
    if (name === null) return;
    api('POST', '/api/wecom/kf/customers/assign-group', {
      customer_ids: [customerId],
      group_name: name
    }).then(function () {
      loadKfGroups();
      loadKfCustomers();
      if (_custTabInited) _loadCustTabData();
    });
  }

  // ── 客户列表 Tab（原"客户配置"）──────────────────────────────────

  var _custTabInited = false;
  var _custTabKfId = '';
  var _custTabGroupId = '';
  var _custTabSearch = '';

  function _loadCustTabData() {
    var listEl = document.getElementById('kfCustTabList');
    if (!listEl) return;
    listEl.innerHTML = '<div style="padding:1rem;text-align:center;color:var(--text-muted);font-size:0.82rem;">加载中…</div>';
    var configId = window._wecomDetailConfigId || 0;
    api('GET', '/api/wecom/kf/accounts?config_id=' + configId)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || !d.accounts || !d.accounts.length) {
          listEl.innerHTML = '<div style="padding:1rem;text-align:center;color:var(--text-muted);font-size:0.82rem;">暂无客服账号，请先在"微信客服"tab中创建</div>';
          return;
        }
        var kfFilter = document.getElementById('kfCustTabKfFilter');
        if (kfFilter && kfFilter.options.length <= 1) {
          var html = '<option value="">全部客服账号</option>';
          d.accounts.forEach(function (a) { html += '<option value="' + a.id + '">' + escHtml(a.name) + '</option>'; });
          kfFilter.innerHTML = html;
        }
        var kfIds = _custTabKfId ? [parseInt(_custTabKfId)] : d.accounts.map(function (a) { return a.id; });
        var allCustomers = [];
        var fetches = kfIds.map(function (id) {
          var url = '/api/wecom/kf/customers?kf_account_id=' + id;
          if (_custTabGroupId) url += '&group_id=' + _custTabGroupId;
          return api('GET', url)
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (cd) {
              if (cd && cd.customers) {
                var kfName = (d.accounts.find(function (a) { return a.id === id; }) || {}).name || '';
                cd.customers.forEach(function (c) { c._kf_name = kfName; c._kf_account_id = id; });
                allCustomers = allCustomers.concat(cd.customers);
              }
            });
        });
        return Promise.all(fetches).then(function () {
          if (_custTabSearch) {
            var q = _custTabSearch.toLowerCase();
            allCustomers = allCustomers.filter(function (c) {
              return (c.nickname || '').toLowerCase().indexOf(q) >= 0 || (c.external_userid || '').toLowerCase().indexOf(q) >= 0;
            });
          }
          _renderCustTab(allCustomers);
        });
      })
      .catch(function () { listEl.innerHTML = '<div style="padding:1rem;text-align:center;color:var(--text-muted);">加载失败</div>'; });
    api('GET', '/api/wecom/kf/groups')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var groups = (d && d.groups) || [];
        var gf = document.getElementById('kfCustTabGroupFilter');
        if (gf) {
          var html = '<option value="">全部分组</option>';
          groups.forEach(function (g) { html += '<option value="' + g.id + '"' + (_custTabGroupId == g.id ? ' selected' : '') + '>' + escHtml(g.name) + ' (' + (g.count || 0) + ')</option>'; });
          html += '<option value="0"' + (_custTabGroupId === '0' ? ' selected' : '') + '>未分组</option>';
          gf.innerHTML = html;
        }
      });
  }

  function _renderCustTab(customers) {
    var listEl = document.getElementById('kfCustTabList');
    if (!listEl) return;
    if (!customers.length) {
      listEl.innerHTML = '<div style="padding:1rem;text-align:center;color:var(--text-muted);font-size:0.82rem;">暂无客户</div>';
      return;
    }
    var html = '<table style="width:100%;border-collapse:collapse;font-size:0.82rem;">';
    html += '<tr style="text-align:left;border-bottom:1px solid var(--border);">';
    html += '<th style="padding:0.4rem 0.5rem;">昵称</th>';
    html += '<th style="padding:0.4rem 0.5rem;">来源客服</th>';
    html += '<th style="padding:0.4rem 0.5rem;">分组</th>';
    html += '<th style="padding:0.4rem 0.5rem;">最后消息</th>';
    html += '<th style="padding:0.4rem 0.5rem;">操作</th>';
    html += '</tr>';
    customers.forEach(function (c) {
      var name = c.nickname || c.external_userid;
      var avatarHtml = c.avatar ? '<img src="' + escHtml(c.avatar) + '" style="width:24px;height:24px;border-radius:50%;margin-right:0.4rem;vertical-align:middle;">' : '';
      var groupName = c.group_name || '<span style="color:var(--text-muted);">未分组</span>';
      var timeStr = c.last_msg_time ? new Date(c.last_msg_time).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '-';
      html += '<tr data-cid="' + c.id + '" style="border-bottom:1px solid rgba(255,255,255,0.04);">';
      html += '<td style="padding:0.4rem 0.5rem;">' + avatarHtml + escHtml(name) + '</td>';
      html += '<td style="padding:0.4rem 0.5rem;color:var(--text-muted);">' + escHtml(c._kf_name || '-') + '</td>';
      html += '<td style="padding:0.4rem 0.5rem;">' + groupName + '</td>';
      html += '<td style="padding:0.4rem 0.5rem;color:var(--text-muted);">' + timeStr + '</td>';
      html += '<td style="padding:0.4rem 0.5rem;"><button type="button" class="btn btn-ghost btn-sm kf-cust-setgroup" data-cid="' + c.id + '" data-gname="' + escHtml(c.group_name || '') + '" style="font-size:0.72rem;">分组</button></td>';
      html += '</tr>';
    });
    html += '</table>';
    listEl.innerHTML = html;
    listEl.querySelectorAll('.kf-cust-setgroup').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var cid = parseInt(btn.getAttribute('data-cid'));
        var gname = btn.getAttribute('data-gname') || '';
        showGroupAssignMenu(cid, gname);
      });
    });
  }

  window.initKfCustomerTab = function () {
    if (!_custTabInited) {
      var kfFilter = document.getElementById('kfCustTabKfFilter');
      if (kfFilter) kfFilter.addEventListener('change', function () { _custTabKfId = kfFilter.value; _loadCustTabData(); });
      var gFilter = document.getElementById('kfCustTabGroupFilter');
      if (gFilter) gFilter.addEventListener('change', function () { _custTabGroupId = gFilter.value; _loadCustTabData(); });
      var searchInput = document.getElementById('kfCustTabSearch');
      if (searchInput) searchInput.addEventListener('input', function () { _custTabSearch = searchInput.value.trim(); _loadCustTabData(); });
      var refreshBtn = document.getElementById('kfCustTabRefreshBtn');
      if (refreshBtn) refreshBtn.addEventListener('click', function () { _loadCustTabData(); });
      _custTabInited = true;
    }
    _loadCustTabData();
  };

  // ── 初始化（由 wecom-detail.js tab 切换调用）─────────────────────

  window.initWecomKf = function () {
    loadKfAccounts();
    loadKfGroups();
  };

  // 事件绑定
  var createBtn = document.getElementById('kfCreateBtn');
  if (createBtn) createBtn.addEventListener('click', createKfAccount);
  var syncBtn = document.getElementById('kfSyncBtn');
  if (syncBtn) syncBtn.addEventListener('click', syncKfAccounts);
  var refreshCustBtn = document.getElementById('kfRefreshCustomersBtn');
  if (refreshCustBtn) refreshCustBtn.addEventListener('click', function () {
    if (selectedKfAccountId) {
      api('POST', '/api/wecom/kf/pull', { kf_account_id: selectedKfAccountId }).then(function () {
        return api('POST', '/api/wecom/kf/customers/refresh', { kf_account_id: selectedKfAccountId });
      }).then(function () {
        loadKfCustomers();
        if (selectedKfCustomer) loadKfMessages();
      });
    }
  });
  var sendBtn = document.getElementById('kfSendBtn');
  if (sendBtn) sendBtn.addEventListener('click', sendKfMessage);
  var sendInput = document.getElementById('kfSendInput');
  if (sendInput) sendInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendKfMessage(); }
  });

  // 分组筛选
  var groupFilter = document.getElementById('kfGroupFilter');
  if (groupFilter) groupFilter.addEventListener('change', function () {
    _kfSelectedGroupFilter = groupFilter.value;
    loadKfCustomers();
  });

  // 分组管理弹窗
  var manageGroupsBtn = document.getElementById('kfManageGroupsBtn');
  var groupModal = document.getElementById('kfGroupModal');
  if (manageGroupsBtn && groupModal) {
    manageGroupsBtn.addEventListener('click', function () {
      loadKfGroups();
      groupModal.style.display = 'flex';
    });
  }
  var groupModalClose = document.getElementById('kfGroupModalClose');
  if (groupModalClose && groupModal) {
    groupModalClose.addEventListener('click', function () { groupModal.style.display = 'none'; });
    groupModal.addEventListener('click', function (e) { if (e.target === groupModal) groupModal.style.display = 'none'; });
  }
  var createGroupBtn = document.getElementById('kfCreateGroupBtn');
  if (createGroupBtn) createGroupBtn.addEventListener('click', function () {
    var inp = document.getElementById('kfNewGroupName');
    var name = (inp && inp.value || '').trim();
    if (!name) return;
    api('POST', '/api/wecom/kf/groups', { name: name })
      .then(function (r) { return r.json(); })
      .then(function () { inp.value = ''; loadKfGroups(); });
  });

  // 右键菜单分配分组
  document.addEventListener('contextmenu', function (e) {
    var item = e.target.closest && e.target.closest('.kf-customer-item');
    if (item) {
      e.preventDefault();
      var cid = parseInt(item.getAttribute('data-cid'));
      var gname = '';
      var gTag = item.querySelector('[style*="background:rgba(6,182,212"]');
      if (gTag) gname = gTag.textContent || '';
      if (cid) showGroupAssignMenu(cid, gname);
    }
  });
})();
