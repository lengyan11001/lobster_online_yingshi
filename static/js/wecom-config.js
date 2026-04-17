/**
 * 企业微信配置页：列表、添加/编辑弹窗、返回技能商店。
 * 与 lobster 一致：列表与 CRUD 走本地（LOCAL_API_BASE）。解锁鉴权仅在技能商店点击卡片时由 skill.js 调 wecom-config-eligible。
 */
(function() {
  var listEl = document.getElementById('wecomConfigList');
  var backBtn = document.getElementById('wecomConfigBackBtn');
  var addBtn = document.getElementById('wecomConfigAddBtn');
  var modal = document.getElementById('wecomConfigModal');
  var modalTitle = document.getElementById('wecomConfigModalTitle');
  var nameInput = document.getElementById('wecomConfigName');
  var tokenInput = document.getElementById('wecomConfigToken');
  var aesKeyInput = document.getElementById('wecomConfigAesKey');
  var corpIdInput = document.getElementById('wecomConfigCorpId');
  var productInput = document.getElementById('wecomConfigProductKnowledge');
  var modalMsg = document.getElementById('wecomConfigModalMsg');
  var modalCancel = document.getElementById('wecomConfigModalCancel');
  var modalSave = document.getElementById('wecomConfigModalSave');

  var secretInput = document.getElementById('wecomConfigSecret');
  var contactsSecretInput = document.getElementById('wecomConfigContactsSecret');
  var agentIdInput = document.getElementById('wecomConfigAgentId');

  var _editingId = null;
  var enterpriseSelect = document.getElementById('wecomConfigEnterpriseId');
  var productSelect = document.getElementById('wecomConfigProductId');

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

  function showMsg(el, text, isErr) {
    if (!el) return;
    el.textContent = text || '';
    el.className = 'msg' + (isErr ? ' err' : '');
    el.style.display = text ? 'block' : 'none';
  }

  function loadWecomConfigList() {
    if (!listEl) return;
    listEl.innerHTML = '<p class="meta">加载中…</p>';
    api('GET', '/api/wecom/configs')
      .then(function(r) {
        if (r.status === 401) {
          if (listEl) listEl.innerHTML = '<p class="meta">未登录或已过期，请重新登录。</p>';
          return null;
        }
        return r.json();
      })
      .then(function(d) {
        if (!listEl) return;
        if (d === null) return;
        if (!d || !Array.isArray(d.configs)) {
          listEl.innerHTML = '<p class="meta">加载失败或暂无配置</p>';
          return;
        }
        var configs = d.configs;
        if (configs.length === 0) {
          listEl.innerHTML = '<p class="meta">暂无配置，点击「添加配置」创建。</p>';
          return;
        }
        var html = configs.map(function(c) {
          var name = (c.name || '未命名').trim() || '未命名';
          var corp = c.corp_id || '-';
          var displayUrl = c.callback_url || ('/api/wecom/callback/' + (c.callback_path || ''));
          var hasKnowledge = c.has_product_knowledge ? '有' : '无';
          var hasSecret = c.has_secret ? '已配置' : '未配置';
          var secretColor = c.has_secret ? 'color:#4ade80;' : 'color:#f87171;';
          return '<div class="skill-store-card wecom-config-card" data-config-id="' + escapeAttr(String(c.id)) + '">' +
            '<div class="card-label">应用</div>' +
            '<div class="card-value">' + escapeHtml(name) + '</div>' +
            '<div class="card-desc">CorpID: ' + escapeHtml(corp) + ' · Secret: <span style="' + secretColor + '">' + hasSecret + '</span> · 知识库: ' + hasKnowledge + '</div>' +
            '<div style="font-size:0.72rem;color:var(--text-muted);margin-top:0.3rem;">回调 URL（填入企微后台）</div>' +
            '<div style="display:flex;align-items:center;gap:0.5rem;margin:0.2rem 0 0.5rem 0;">' +
              '<pre class="config-block-item" style="font-size:0.75rem;margin:0;padding:0.4rem;background:rgba(0,0,0,0.2);border-radius:4px;overflow-x:auto;flex:1;">' + escapeHtml(displayUrl) + '</pre>' +
              '<button type="button" class="btn btn-primary btn-sm wecom-copy-url" data-url="' + escapeAttr(displayUrl) + '" style="white-space:nowrap;">复制</button>' +
            '</div>' +
            '<div class="card-actions">' +
              '<button type="button" class="btn btn-primary btn-sm wecom-detail" data-id="' + escapeAttr(String(c.id)) + '">详情</button>' +
              '<button type="button" class="btn btn-ghost btn-sm wecom-edit" data-id="' + escapeAttr(String(c.id)) + '">编辑</button>' +
              '<button type="button" class="btn btn-ghost btn-sm wecom-delete" data-id="' + escapeAttr(String(c.id)) + '">删除</button>' +
            '</div></div>';
        }).join('');
        listEl.innerHTML = html;
        listEl.querySelectorAll('.wecom-config-card').forEach(function(card) {
          var id = card.getAttribute('data-config-id');
          card.addEventListener('click', function(e) {
            if (e.target.closest('.card-actions')) return;
            openEdit(parseInt(id, 10));
          });
        });
        listEl.querySelectorAll('.wecom-copy-url').forEach(function(btn) {
          btn.addEventListener('click', function(e) { e.stopPropagation(); copyUrl(btn.getAttribute('data-url'), btn); });
        });
        listEl.querySelectorAll('.wecom-edit').forEach(function(btn) {
          btn.addEventListener('click', function(e) { e.stopPropagation(); openEdit(parseInt(btn.getAttribute('data-id'), 10)); });
        });
        listEl.querySelectorAll('.wecom-delete').forEach(function(btn) {
          btn.addEventListener('click', function(e) {
            e.stopPropagation();
            if (!confirm('确定删除该配置？')) return;
            deleteConfig(parseInt(btn.getAttribute('data-id'), 10));
          });
        });
        listEl.querySelectorAll('.wecom-detail').forEach(function(btn) {
          btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var configId = parseInt(btn.getAttribute('data-id'), 10);
            if (typeof showWecomDetailView === 'function') showWecomDetailView(configId);
          });
        });
      })
      .catch(function() {
        if (listEl) listEl.innerHTML = '<p class="msg err">加载失败</p>';
      });
  }

  function copyUrl(url, btn) {
    if (!url) return;
    if (url.indexOf('/') === 0) url = window.location.origin + url;
    function onCopied() {
      if (btn) {
        var orig = btn.textContent;
        btn.textContent = '已复制 ✓';
        btn.style.color = '#4ade80';
        setTimeout(function() { btn.textContent = orig; btn.style.color = ''; }, 1500);
      }
    }
    if (typeof navigator.clipboard !== 'undefined' && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(onCopied).catch(function() { fallbackCopy(url); onCopied(); });
    } else { fallbackCopy(url); onCopied(); }
  }
  function fallbackCopy(str) {
    var ta = document.createElement('textarea');
    ta.value = str;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
  }

  function loadEnterpriseProductOptions(cb) {
    api('GET', '/api/wecom/enterprises').then(function(r) { return r.ok ? r.json() : null; }).then(function(entData) {
      var ents = (entData && entData.items) ? entData.items : [];
      if (enterpriseSelect) {
        enterpriseSelect.innerHTML = '<option value="">不绑定</option>' + ents.map(function(e) { return '<option value="' + e.id + '">' + escapeHtml(e.name || '') + '</option>'; }).join('');
      }
      api('GET', '/api/wecom/products').then(function(r) { return r.ok ? r.json() : null; }).then(function(prodData) {
        var prods = (prodData && prodData.items) ? prodData.items : [];
        if (productSelect) {
          productSelect.innerHTML = '<option value="">不绑定</option>' + prods.map(function(p) { return '<option value="' + p.id + '" data-enterprise-id="' + (p.enterprise_id || '') + '">' + escapeHtml(p.name || '') + '</option>'; }).join('');
        }
        if (cb) cb();
      });
    });
  }

  function openAdd() {
    _editingId = null;
    if (modalTitle) modalTitle.textContent = '添加企业微信配置';
    if (nameInput) nameInput.value = '默认应用';
    if (tokenInput) tokenInput.value = '';
    if (aesKeyInput) aesKeyInput.value = '';
    if (corpIdInput) corpIdInput.value = '';
    if (secretInput) secretInput.value = '';
    if (contactsSecretInput) contactsSecretInput.value = '';
    if (agentIdInput) agentIdInput.value = '';
    if (productInput) productInput.value = '';
    if (enterpriseSelect) enterpriseSelect.value = '';
    if (productSelect) productSelect.value = '';
    showMsg(modalMsg, '');
    loadEnterpriseProductOptions(function() { if (modal) modal.classList.add('visible'); });
  }

  function openEdit(id) {
    _editingId = id;
    if (modalTitle) modalTitle.textContent = '编辑企业微信配置';
    loadEnterpriseProductOptions(function() {
      api('GET', '/api/wecom/configs/' + id)
        .then(function(r) { return r.json(); })
        .then(function(c) {
          if (!c) return;
          if (nameInput) nameInput.value = c.name || '';
          if (tokenInput) { tokenInput.value = ''; tokenInput.placeholder = '不修改请留空'; }
          if (aesKeyInput) { aesKeyInput.value = ''; aesKeyInput.placeholder = '不修改请留空'; }
          if (corpIdInput) corpIdInput.value = c.corp_id || '';
          if (secretInput) { secretInput.value = c.secret || ''; secretInput.placeholder = c.secret ? '已配置，输入新值可覆盖' : '企微后台 → 应用管理 → 自建应用 → Secret'; }
          if (contactsSecretInput) { contactsSecretInput.value = c.contacts_secret || ''; contactsSecretInput.placeholder = c.contacts_secret ? '已配置，输入新值可覆盖' : '企微后台 → 管理工具 → 通讯录同步 → Secret'; }
          if (agentIdInput) agentIdInput.value = c.agent_id || '';
          if (productInput) productInput.value = c.product_knowledge || '';
          if (enterpriseSelect && c.enterprise_id) enterpriseSelect.value = String(c.enterprise_id);
          if (productSelect && c.product_id) productSelect.value = String(c.product_id);
          showMsg(modalMsg, '');
          if (modal) modal.classList.add('visible');
        })
        .catch(function() { showMsg(modalMsg, '加载配置失败', true); });
    });
  }

  function closeModal() {
    _editingId = null;
    if (modal) modal.classList.remove('visible');
    showMsg(modalMsg, '');
  }

  function saveConfig() {
    var name = (nameInput && nameInput.value) ? nameInput.value.trim() : '默认应用';
    var token = (tokenInput && tokenInput.value) ? tokenInput.value.trim() : '';
    var aesKey = (aesKeyInput && aesKeyInput.value) ? aesKeyInput.value.trim() : '';
    var corpId = (corpIdInput && corpIdInput.value) ? corpIdInput.value.trim() : '';
    var secret = (secretInput && secretInput.value) ? secretInput.value.trim() : '';
    var contactsSecret = (contactsSecretInput && contactsSecretInput.value) ? contactsSecretInput.value.trim() : '';
    var agentId = (agentIdInput && agentIdInput.value) ? parseInt(agentIdInput.value, 10) : null;
    var product = (productInput && productInput.value) ? productInput.value.trim() : '';
    if (!token && !_editingId) { showMsg(modalMsg, '请填写 Token', true); return; }
    if (!aesKey && !_editingId) { showMsg(modalMsg, '请填写 EncodingAESKey', true); return; }
    showMsg(modalMsg, '保存中…', false);
    var entId = (enterpriseSelect && enterpriseSelect.value) ? parseInt(enterpriseSelect.value, 10) : null;
    var prodId = (productSelect && productSelect.value) ? parseInt(productSelect.value, 10) : null;
    var body = { name: name || '默认应用', token: token || undefined, encoding_aes_key: aesKey || undefined, corp_id: corpId || undefined, secret: secret || undefined, contacts_secret: contactsSecret || undefined, agent_id: agentId || undefined, product_knowledge: product || undefined, enterprise_id: entId || undefined, product_id: prodId || undefined };
    var method = _editingId ? 'PUT' : 'POST';
    var path = _editingId ? '/api/wecom/configs/' + _editingId : '/api/wecom/configs';
    if (_editingId) {
      var up = {};
      if (name) up.name = name;
      if (token) up.token = token;
      if (aesKey) up.encoding_aes_key = aesKey;
      if (corpId !== undefined) up.corp_id = corpId;
      if (secret) up.secret = secret;
      if (contactsSecret) up.contacts_secret = contactsSecret;
      if (agentId) up.agent_id = agentId;
      if (product !== undefined) up.product_knowledge = product;
      if (entId !== undefined) up.enterprise_id = entId;
      if (prodId !== undefined) up.product_id = prodId;
      body = up;
    }
    api(method, path, body)
      .then(function(r) {
        return r.json().then(function(d) { return { ok: r.ok, data: d }; });
      })
      .then(function(x) {
        if (x.ok) {
          closeModal();
          loadWecomConfigList();
          showMsg(modalMsg, '');
        } else {
          showMsg(modalMsg, (x.data && (x.data.detail || x.data.message)) || '保存失败', true);
        }
      })
      .catch(function() {
        showMsg(modalMsg, '请求失败', true);
      });
  }

  function deleteConfig(id) {
    api('DELETE', '/api/wecom/configs/' + id)
      .then(function(r) {
        if (r.ok) loadWecomConfigList();
        else r.json().then(function(d) { alert(d.detail || '删除失败'); });
      })
      .catch(function() { alert('请求失败'); });
  }

  if (backBtn) {
    backBtn.addEventListener('click', function() {
      location.hash = '';
      document.querySelectorAll('.content-block').forEach(function(p) { p.classList.remove('visible'); });
      var contentEl = document.getElementById('content-skill-store');
      if (contentEl) contentEl.classList.add('visible');
      document.querySelectorAll('.nav-left-item').forEach(function(b) { b.classList.remove('active'); });
      var navEl = document.querySelector('.nav-left-item[data-view="skill-store"]');
      if (navEl) navEl.classList.add('active');
      if (typeof currentView !== 'undefined') currentView = 'skill-store';
      if (typeof loadSkillStore === 'function') loadSkillStore();
    });
  }
  if (addBtn) addBtn.addEventListener('click', openAdd);
  if (modalCancel) modalCancel.addEventListener('click', closeModal);
  if (modalSave) modalSave.addEventListener('click', saveConfig);
  if (modal) {
    modal.addEventListener('click', function(e) {
      if (e.target === modal) closeModal();
    });
  }

  // 企微云端配置 UI 已移除（云端地址与 WECOM_FORWARD_SECRET 不再需要手动配置）

  window.showWecomConfigView = function() {
    location.hash = 'wecom-config';
    document.querySelectorAll('.content-block').forEach(function(p) { p.classList.remove('visible'); });
    var contentEl = document.getElementById('content-wecom-config');
    if (contentEl) contentEl.classList.add('visible');
    document.querySelectorAll('.nav-left-item').forEach(function(b) { b.classList.remove('active'); });
    var navEl = document.querySelector('.nav-left-item[data-view="skill-store"]');
    if (navEl) navEl.classList.add('active');
    if (typeof currentView !== 'undefined') currentView = 'wecom-config';
    loadWecomConfigList();
  };

  window.loadWecomConfigList = loadWecomConfigList;
})();
