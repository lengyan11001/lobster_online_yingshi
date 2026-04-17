/* 微信客服管理 — 客服账号 / 客户列表 / 消息 / 手动发送 */
(function () {
  'use strict';

  var selectedKfAccountId = null;
  var selectedKfCustomer = null; // { external_userid, nickname }

  function getConfigId() {
    return typeof wecomDetailConfigId !== 'undefined' ? wecomDetailConfigId : null;
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
    api('GET', '/api/wecom/kf/customers?kf_account_id=' + selectedKfAccountId)
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
      html += '<div class="kf-customer-item" data-ext="' + escHtml(c.external_userid) + '" data-nick="' + escHtml(c.nickname || '') + '" style="padding:0.5rem 0.75rem;cursor:pointer;border-bottom:1px solid rgba(255,255,255,0.05);transition:background 0.1s;' + bg + '">';
      html += '<div style="display:flex;justify-content:space-between;align-items:center;">';
      html += '<span style="font-size:0.85rem;font-weight:500;">' + escHtml(name) + '</span>';
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

  // ── 初始化（由 wecom-detail.js tab 切换调用）─────────────────────

  window.initWecomKf = function () {
    loadKfAccounts();
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
})();
