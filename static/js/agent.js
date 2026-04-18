/* global API_BASE, authHeaders, escapeHtml */

(function () {
  'use strict';

  var _loaded = false;

  window.loadAgentSubUsers = function loadAgentSubUsers() {
    var listEl = document.getElementById('agentSubUserList');
    var countEl = document.getElementById('agentSubUserCount');
    if (!listEl) return;

    var base = (typeof API_BASE !== 'undefined' && API_BASE) ? String(API_BASE).replace(/\/$/, '') : '';
    if (!base) return;

    if (!_loaded) countEl.textContent = '下级用户：加载中…';

    fetch(base + '/auth/agent/sub-users', { headers: authHeaders() })
      .then(function (r) {
        if (r.status === 403) return { sub_users: [], count: 0, _forbidden: true };
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (d) {
        _loaded = true;
        if (d._forbidden) {
          countEl.textContent = '无权访问（非代理商）';
          listEl.innerHTML = '';
          return;
        }
        var list = d.sub_users || [];
        countEl.textContent = '下级用户：' + (d.count || list.length) + ' 人';
        if (!list.length) {
          listEl.innerHTML = '<p style="color:var(--text-muted);font-size:0.88rem;">暂无下级用户</p>';
          return;
        }
        var h = '<table style="width:100%;border-collapse:collapse;font-size:0.85rem;">'
          + '<thead><tr style="border-bottom:2px solid rgba(255,255,255,0.12);text-align:left;">'
          + '<th style="padding:0.55rem;">ID</th>'
          + '<th style="padding:0.55rem;">账号</th>'
          + '<th style="padding:0.55rem;text-align:right;">当前算力</th>'
          + '<th style="padding:0.55rem;text-align:right;">累计充值</th>'
          + '<th style="padding:0.55rem;">注册时间</th>'
          + '</tr></thead><tbody>';
        list.forEach(function (u) {
          var email = u.email || '-';
          var display = email.replace(/@sms\.lobster\.local$/, '');
          var created = (u.created_at || '').replace('T', ' ').substring(0, 19);
          h += '<tr style="border-bottom:1px solid rgba(255,255,255,0.06);">'
            + '<td style="padding:0.5rem;">' + u.id + '</td>'
            + '<td style="padding:0.5rem;">' + escapeHtml(display) + '</td>'
            + '<td style="padding:0.5rem;text-align:right;">' + (u.credits != null ? u.credits : '-') + '</td>'
            + '<td style="padding:0.5rem;text-align:right;">' + (u.total_recharged || 0) + '</td>'
            + '<td style="padding:0.5rem;">' + escapeHtml(created) + '</td>'
            + '</tr>';
        });
        h += '</tbody></table>';
        listEl.innerHTML = h;
      })
      .catch(function (e) {
        countEl.textContent = '加载失败';
        listEl.innerHTML = '<p style="color:#e74c3c;font-size:0.85rem;">' + escapeHtml(String(e)) + '</p>';
      });
  };

  var btn = document.getElementById('agentRefreshBtn');
  if (btn) btn.addEventListener('click', function () { _loaded = false; loadAgentSubUsers(); });
})();
