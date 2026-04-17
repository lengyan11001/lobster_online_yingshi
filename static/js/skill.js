// ── Tab switching ───────────────────────────────────────────────────
/** 为 false 时隐藏「官方在线」Tab，不加载 MCP 官方注册表 */
var STORE_OFFICIAL_TAB_ENABLED = false;
var _currentStoreTab = 'popular';

document.querySelectorAll('.store-tab').forEach(function(tab) {
  tab.addEventListener('click', function() {
    var target = tab.getAttribute('data-store-tab');
    if (target === 'official' && !STORE_OFFICIAL_TAB_ENABLED) return;
    if (!target || target === _currentStoreTab) return;
    _currentStoreTab = target;
    document.querySelectorAll('.store-tab').forEach(function(t) { t.classList.remove('active'); });
    tab.classList.add('active');
    document.getElementById('storeTabPopular').style.display = (target === 'popular') ? '' : 'none';
    document.getElementById('storeTabOfficial').style.display = (target === 'official') ? '' : 'none';
    if (target === 'official' && !_officialLoaded) {
      browseOfficialPage(1);
    }
  });
});

// ── 热门 Tab: local skills ──────────────────────────────────────────

var _xskillStatus = { has_token: false, token: '', url: '' };
var _comflyStatus = {
  effective_ready: false,
  has_user_key: false,
  masked_user_key: '',
  user_api_base: '',
  default_api_base_hint: 'https://ai.comfly.chat/v1',
};
var _youtubePublishStatus = { has_ready: false, accounts_count: 0 };

/** OpenClaw 微信插件本机扫码授权（/api/openclaw/weixin-login/*） */
var _openclawWeixinLast = { last_ok: false, at: null, detail: '' };
var _openclawWeixinPollTimer = null;

function _switchToHiddenView(view) {
  if (!view) return;
  location.hash = view;
  if (typeof currentView !== 'undefined' && currentView === 'chat' && typeof saveCurrentSessionToStore === 'function') {
    saveCurrentSessionToStore();
  }
  document.querySelectorAll('.nav-left-item').forEach(function(b) { b.classList.remove('active'); });
  document.querySelectorAll('.content-block').forEach(function(p) { p.classList.remove('visible'); });
  var contentEl = document.getElementById('content-' + view);
  if (contentEl) contentEl.classList.add('visible');
  if (typeof currentView !== 'undefined') currentView = view;
}

function _openMessengerConfigView() {
  _switchToHiddenView('messenger-config');
  if (typeof loadMessengerConfigPage === 'function') loadMessengerConfigPage();
}

function _ensureSkillStoreVisible() {
  var nav = document.querySelector('.nav-left-item[data-view="skill-store"]');
  if (nav) nav.click();
}
window._ensureSkillStoreVisible = _ensureSkillStoreVisible;

window._openYoutubeAccountsView = function() {
  _switchToHiddenView('youtube-accounts');
  if (typeof loadYoutubeAccountsPage === 'function') loadYoutubeAccountsPage();
  try { location.hash = 'youtube-accounts'; } catch (e1) {}
};

window._openEcommerceDetailStudioView = function() {
  _switchToHiddenView('ecommerce-detail-studio');
  if (typeof window.initEcommerceDetailStudioView === 'function') window.initEcommerceDetailStudioView();
  try { location.hash = 'ecommerce-detail-studio'; } catch (e1) {}
};

function _openTwilioWhatsappConfigView() {
  _ensureSkillStoreVisible();
  var modal = document.getElementById('twilioWhatsappConfigModal');
  if (modal) modal.classList.add('visible');
  if (typeof loadTwilioWhatsappConfigPage === 'function') loadTwilioWhatsappConfigPage();
  try { location.hash = 'twilio-whatsapp-config'; } catch (e1) {}
}

function _openTwilioWhatsappDetailView() {
  if (typeof showTwilioWhatsappDetailView === 'function') showTwilioWhatsappDetailView();
}

function _renderYoutubePublishCard(opts) {
  opts = opts || {};
  var pkg = opts.pkg || {};
  var showDebug = !!opts.showDebug;
  var debugBadge = showDebug
    ? '<span class="badge-coming" style="background:rgba(139,92,246,0.12);color:#a78bfa;border-color:rgba(139,92,246,0.25);margin-right:0.35rem;">调试</span> '
    : '';
  var title = (pkg.name && String(pkg.name).trim()) || 'YouTube 上传';
  var desc = (pkg.description && String(pkg.description).trim()) ||
    '多账号管理：每个账号独立 OAuth 与代理；授权成功后即可在对话中指定素材与 YouTube 账号 ID。';
  var configured = _youtubePublishStatus.has_ready;
  var cnt = _youtubePublishStatus.accounts_count || 0;
  var statusBadge = configured
    ? '<span class="badge-installed">已有可用账号</span>'
    : (cnt > 0
      ? '<span class="badge-coming" style="background:rgba(251,146,60,0.15);color:#fb923c;border-color:rgba(251,146,60,0.3);">待完成授权</span>'
      : '<span class="badge-coming" style="background:rgba(251,146,60,0.15);color:#fb923c;border-color:rgba(251,146,60,0.3);">未添加</span>');
  var hint = configured ? '' :
    '<div style="margin-top:0.45rem;font-size:0.78rem;color:var(--text-muted);">点进列表添加账号；对话里用「账号 ID」（yt_ 开头）指定发到哪个 YouTube。</div>';
  return '<div class="skill-store-card youtube-publish-card" style="cursor:pointer;border-color:rgba(255,0,0,0.22);background:linear-gradient(135deg,rgba(255,0,0,0.06),transparent);">' +
    '<div class="card-label">' + debugBadge + '发布 <span class="badge-installed">可配置</span> ' + statusBadge + '</div>' +
    '<div class="card-value">' + escapeHtml(title) + '</div>' +
    '<div class="card-desc">' + escapeHtml(desc) + '</div>' +
    hint +
    '<div class="card-tags"><span class="tag">YouTube</span><span class="tag">OAuth</span></div>' +
    '<div class="card-actions" style="display:flex;flex-wrap:wrap;gap:0.35rem;">' +
    '<button type="button" class="btn btn-primary btn-sm youtube-publish-entry-btn">管理账号</button></div></div>';
}

function _renderMetaSocialCard(opts) {
  opts = opts || {};
  if (typeof EDITION === 'undefined' || EDITION !== 'online') return '';
  var cnt = (typeof _metaSocialStatus !== 'undefined') ? (_metaSocialStatus.accounts_count || 0) : 0;
  var statusBadge = cnt > 0
    ? '<span class="badge-installed">已连接 ' + cnt + ' 个</span>'
    : '<span class="badge-coming" style="background:rgba(251,146,60,0.15);color:#fb923c;border-color:rgba(251,146,60,0.3);">未连接</span>';
  return '<div class="skill-store-card meta-social-card" style="cursor:pointer;border-color:rgba(225,48,108,0.35);background:linear-gradient(135deg,rgba(24,119,242,0.06),rgba(225,48,108,0.06));">' +
    '<div class="card-label">发布 <span class="badge-installed">可配置</span> ' + statusBadge + '</div>' +
    '<div class="card-value">Instagram / Facebook</div>' +
    '<div class="card-desc">通过 Facebook OAuth 授权连接 IG Business 或 FB 主页；对话中可直接发布 photo / video / reel / story / carousel，也可拉取粉丝数据与互动指标。</div>' +
    '<div class="card-tags"><span class="tag">Instagram</span><span class="tag">Facebook</span><span class="tag">OAuth</span></div>' +
    '<div class="card-actions" style="display:flex;flex-wrap:wrap;gap:0.35rem;">' +
    '<button type="button" class="btn btn-primary btn-sm meta-social-entry-btn">管理账号</button></div></div>';
}

function _bindMetaSocialCardEntry() {
  document.querySelectorAll('.meta-social-card').forEach(function(card) {
    card.addEventListener('click', function(e) {
      if (e.target.closest('.card-actions')) return;
      if (typeof window._openMetaSocialView === 'function') window._openMetaSocialView();
    });
  });
  document.querySelectorAll('.meta-social-entry-btn').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      if (typeof window._openMetaSocialView === 'function') window._openMetaSocialView();
    });
  });
}

function _renderTwilioWhatsappCard(opts) {
  opts = opts || {};
  if (typeof EDITION === 'undefined' || EDITION !== 'online') return '';
  var pkg = opts.pkg || {};
  var showDebug = !!opts.showDebug;
  var debugBadge = showDebug
    ? '<span class="badge-coming" style="background:rgba(139,92,246,0.12);color:#a78bfa;border-color:rgba(139,92,246,0.25);margin-right:0.35rem;">调试</span> '
    : '';
  var title = (pkg.name && String(pkg.name).trim()) || 'Twilio WhatsApp';
  var desc = (pkg.description && String(pkg.description).trim()) ||
    '云端入站 + 本机轮询 AI 回复；点卡片查看消息与公司列表，点「配置」填写 Twilio';
  return '<div class="skill-store-card twilio-whatsapp-card" style="cursor:pointer;border-color:rgba(37,211,102,0.45);background:linear-gradient(135deg,rgba(37,211,102,0.08),transparent);">' +
    '<div class="card-label">' + debugBadge + '通道 <span class="badge-installed">可配置</span></div>' +
    '<div class="card-value">' + escapeHtml(title) + '</div>' +
    '<div class="card-desc">' + escapeHtml(desc) + '</div>' +
    '<div class="card-tags"><span class="tag">WhatsApp</span><span class="tag">Twilio</span></div>' +
    '<div class="card-actions" style="display:flex;flex-wrap:wrap;gap:0.35rem;">' +
    '<button type="button" class="btn btn-primary btn-sm twilio-whatsapp-entry-btn">配置</button></div></div>';
}

function _renderXSkillCard() {
  var configured = _xskillStatus.has_token;
  var statusBadge = configured
    ? '<span class="badge-installed">已配置</span>'
    : '<span class="badge-coming" style="background:rgba(251,146,60,0.15);color:#fb923c;border-color:rgba(251,146,60,0.3);">未配置</span>';
  var guide = configured ? '' :
    '<div style="margin-top:0.6rem;padding:0.6rem 0.75rem;background:rgba(251,146,60,0.06);border:1px solid rgba(251,146,60,0.18);border-radius:8px;font-size:0.8rem;color:var(--text-muted);line-height:1.6;">' +
      '<div style="font-weight:600;color:#fb923c;margin-bottom:0.3rem;">获取 Token 步骤：</div>' +
      '<div>1. 打开 <a href="https://www.51aigc.cc" target="_blank" style="color:var(--primary);">51aigc.cc</a> ，微信扫码 或 手机号登录</div>' +
      '<div>2. 登录后点击 <a href="https://www.51aigc.cc/#/userInfo" target="_blank" style="color:var(--primary);">个人中心</a> 复制 API Token</div>' +
      '<div>3. 回到这里点击「配置 Token」粘贴即可</div>' +
    '</div>';
  var configBtn = (EDITION === 'online')
    ? '<span class="btn btn-ghost btn-sm" style="cursor:default;color:var(--text-muted);">已安装</span>'
    : '<button type="button" class="btn btn-primary btn-sm" id="xskillConfigBtn">' + (configured ? '修改 Token' : '配置 Token') + '</button>';
  if (EDITION === 'online') guide = '';
  return '<div class="skill-store-card" style="border-color:rgba(6,182,212,0.25);background:linear-gradient(135deg,rgba(6,182,212,0.06),transparent);">' +
    '<div class="card-label">MCP · 内置 ' + statusBadge + '</div>' +
    '<div class="card-value">xSkill AI (速推)</div>' +
    '<div class="card-desc">图片生成、视频生成、视频解析、语音合成、音色克隆等 50+ AI 模型能力</div>' +
    '<div class="card-tags"><span class="tag">图片</span><span class="tag">视频</span><span class="tag">音频</span><span class="tag">AI创作</span></div>' +
    guide +
    '<div class="card-actions">' +
      configBtn +
      '<a href="https://xskill.ai" target="_blank" rel="noopener" class="btn btn-ghost btn-sm">官网</a>' +
    '</div></div>';
}

function _loadXSkillStatus(cb) {
  fetch((LOCAL_API_BASE || '') + '/api/sutui/config', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      _xskillStatus = { has_token: !!d.has_token, token: d.token || '', url: d.url || '' };
      if (cb) cb();
    })
    .catch(function() { if (cb) cb(); });
}

function _loadComflyStatus(cb) {
  fetch((LOCAL_API_BASE || '') + '/api/comfly/config', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      _comflyStatus = {
        effective_ready: !!d.effective_ready,
        has_user_key: !!d.has_user_key,
        masked_user_key: d.masked_user_key || '',
        user_api_base: d.user_api_base || '',
        default_api_base_hint: d.default_api_base_hint || 'https://ai.comfly.chat/v1',
      };
      if (cb) cb();
    })
    .catch(function() { if (cb) cb(); });
}

function _renderComflyCard() {
  var ok = _comflyStatus.effective_ready;
  var statusBadge = ok
    ? '<span class="badge-installed">已就绪</span>'
    : '<span class="badge-coming" style="background:rgba(251,146,60,0.15);color:#fb923c;border-color:rgba(251,146,60,0.3);">待配置</span>';
  var sub = ok
    ? ''
    : '<div style="margin-top:0.55rem;padding:0.55rem 0.7rem;background:rgba(245,158,11,0.06);border:1px solid rgba(245,158,11,0.2);border-radius:8px;font-size:0.78rem;color:var(--text-muted);line-height:1.55;">'
      + '只需在 <strong>Comfly</strong> 控制台复制 <strong>API Key</strong>，并填写常用根地址 <code>https://ai.comfly.chat/v1</code>。'
      + '用户对话可说「用<strong>爆款TVC</strong>和这个素材做视频」；技能会自动跑分镜、多段成片与入库，无需在卡片里配分镜参数。'
      + ' 点击下方「配置」填写凭据，不会写入聊天记录。</div>';
  return '<div class="skill-store-card comfly-veo-card" style="border-color:rgba(245,158,11,0.38);background:linear-gradient(135deg,rgba(245,158,11,0.07),transparent);">' +
    '<div class="card-label">生成 · 内置 ' + statusBadge + '</div>' +
    '<div class="card-value">爆款TVC</div>' +
    '<div class="card-desc">整包成片走 <code>comfly.veo.daihuo_pipeline</code>（start_pipeline + 素材）；单段调试可走 <code>comfly.veo</code>。均与速推 <code>video.generate</code> 无关。</div>' +
    sub +
    '<div class="card-tags"><span class="tag">爆款TVC</span><span class="tag">TVC</span><span class="tag">Veo</span><span class="tag">Comfly</span></div>' +
    '<div class="card-actions"><button type="button" class="btn btn-primary btn-sm" id="comflyConfigBtn">配置</button></div></div>';
}

function _renderEcommerceDetailCard(opts) {
  opts = opts || {};
  var pkg = opts.pkg || {};
  var ok = _comflyStatus.effective_ready;
  var rawTitle = (pkg.name && String(pkg.name).trim()) || '';
  var rawDesc = (pkg.description && String(pkg.description).trim()) || '';
  var title = rawTitle && !/^\?+$/.test(rawTitle) ? rawTitle : '电商上架套图';
  var desc = rawDesc && !/^\?+$/.test(rawDesc) ? rawDesc :
    '把商品图、卖点、风格和模板组织成一次完整的上架视觉资产生产流程，覆盖主图、SKU 图、透明/白底、详情图、素材图与橱窗图。';
  var statusBadge = ok
    ? '<span class="badge-installed">已就绪</span>'
    : '<span class="badge-coming" style="background:rgba(251,146,60,0.15);color:#fb923c;border-color:rgba(251,146,60,0.3);">待配置</span>';
  var sub = ok
    ? '<div style="margin-top:0.45rem;font-size:0.78rem;color:var(--text-muted);">直接进入工作台，按结构化参数控制本次套图生成内容。</div>'
    : '<div style="margin-top:0.55rem;padding:0.55rem 0.7rem;background:rgba(245,158,11,0.06);border:1px solid rgba(245,158,11,0.2);border-radius:8px;font-size:0.78rem;color:var(--text-muted);line-height:1.55;">先在本机保存 <strong>Comfly API Key</strong> 与 API Base，然后再进入产品套图工作台。这个界面是专门用于批量生成电商上架素材的，不是聊天入口。</div>';
  return '<div class="skill-store-card ecommerce-detail-card" style="cursor:pointer;border-color:rgba(236,72,153,0.34);background:linear-gradient(135deg,rgba(236,72,153,0.08),rgba(245,158,11,0.05));">' +
    '<div class="card-label">生成 · 内置 ' + statusBadge + '</div>' +
    '<div class="card-value">' + escapeHtml(title) + '</div>' +
    '<div class="card-desc">' + escapeHtml(desc) + '</div>' +
    sub +
    '<div class="card-tags"><span class="tag">上架套图</span><span class="tag">SKU</span><span class="tag">详情图</span><span class="tag">Comfly</span></div>' +
    '<div class="card-actions" style="display:flex;flex-wrap:wrap;gap:0.35rem;">' +
      '<button type="button" class="btn btn-primary btn-sm ecommerce-detail-entry-btn">进入工作台</button>' +
      '<button type="button" class="btn btn-ghost btn-sm js-comfly-config-btn">配置 Comfly</button>' +
    '</div></div>';
}

function _openclawWeixinResolveBase() {
  if (typeof LOCAL_API_BASE === 'undefined' || !LOCAL_API_BASE) return '';
  return String(LOCAL_API_BASE).replace(/\/$/, '');
}

function _fetchSkillStoreFrom(base) {
  var b = String(base || '').replace(/\/$/, '');
  if (!b) return Promise.resolve({ packages: [] });
  return fetch(b + '/skills/store', { headers: authHeaders() })
    .then(function(r) {
      return r.json().then(function(d) {
        return { ok: r.ok, data: d || {} };
      });
    })
    .then(function(res) {
      if (!res.ok) throw new Error((res.data && res.data.detail) || 'load_skill_store_failed');
      return res.data || {};
    });
}

function _mergeSkillStorePackages(primary, secondary) {
  var out = [];
  var seen = {};
  var first = (primary && Array.isArray(primary.packages)) ? primary.packages : [];
  var second = (secondary && Array.isArray(secondary.packages)) ? secondary.packages : [];
  first.forEach(function(pkg) {
    if (!pkg || !pkg.id || seen[pkg.id]) return;
    seen[pkg.id] = true;
    out.push(pkg);
  });
  second.forEach(function(pkg) {
    if (!pkg || !pkg.id || seen[pkg.id]) return;
    seen[pkg.id] = true;
    out.push(pkg);
  });
  return {
    packages: out,
    is_skill_store_admin: !!(primary && primary.is_skill_store_admin)
  };
}

function _renderOpenclawWeixinCard(opts) {
  opts = opts || {};
  var showDebug = !!opts.showDebug;
  var noLocalBackend = !!opts.noLocalBackend;
  var debugBadge = showDebug
    ? '<span class="badge-coming" style="background:rgba(139,92,246,0.12);color:#a78bfa;border-color:rgba(139,92,246,0.25);margin-right:0.35rem;">调试</span> '
    : '';
  var ok = _openclawWeixinLast.last_ok;
  var badge = ok
    ? '<span class="badge-installed">近期已登录</span>'
    : '<span class="badge-coming" style="background:rgba(7,193,96,0.12);color:#059669;border-color:rgba(7,193,96,0.25);">需授权</span>';
  var atHint = _openclawWeixinLast.at
    ? '<div style="font-size:0.78rem;color:var(--text-muted);margin-top:0.35rem;">上次记录：' + escapeHtml(String(_openclawWeixinLast.at)) + '</div>'
    : '';
  var localHint = noLocalBackend
    ? '<div style="font-size:0.78rem;color:var(--text-muted);margin-top:0.35rem;">商店入口由服务器控制；发起扫码须本机运行 lobster_online 并配置 LOCAL_API_BASE。</div>'
    : '';
  return '<div class="skill-store-card openclaw-weixin-card" style="border-color:rgba(7,193,96,0.35);background:linear-gradient(135deg,rgba(7,193,96,0.07),transparent);">' +
    '<div class="card-label">' + debugBadge + '通道 <span class="badge-installed">OpenClaw</span> ' + badge + '</div>' +
    '<div class="card-value">微信助手 (OpenClaw)</div>' +
    '<div class="card-desc">点击后将自动依次执行：① <code>plugins install</code> ② <code>config set … enabled true</code> ③ <code>channels login</code>（弹出层内展示二维码）④ 成功后 <code>gateway restart</code>。与腾讯官方微信插件文档步骤一致。</div>' +
    localHint +
    atHint +
    '<div class="card-tags"><span class="tag">微信</span><span class="tag">OpenClaw</span></div>' +
    '<div class="card-actions"><button type="button" class="btn btn-primary btn-sm js-openclaw-weixin-auth">扫码授权</button></div></div>';
}

function _loadYoutubePublishStatus(cb) {
  fetch((LOCAL_API_BASE || '') + '/api/youtube-publish/summary', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      _youtubePublishStatus = {
        has_ready: !!d.has_ready,
        accounts_count: typeof d.accounts_count === 'number' ? d.accounts_count : 0
      };
      if (cb) cb();
    })
    .catch(function() {
      _youtubePublishStatus = { has_ready: false, accounts_count: 0 };
      if (cb) cb();
    });
}

function loadSkillStore() {
  var el = document.getElementById('skillStoreList');
  if (!el) return;
  el.innerHTML = '<p class="meta">加载中…</p>';

  var remoteBase = (typeof API_BASE !== 'undefined' ? API_BASE : '') || '';
  var localBase = (typeof LOCAL_API_BASE !== 'undefined' ? LOCAL_API_BASE : '') || '';
  var remoteReq = _fetchSkillStoreFrom(remoteBase).catch(function() { return { packages: [] }; });
  var localReq = (!localBase || String(localBase).replace(/\/$/, '') === String(remoteBase).replace(/\/$/, ''))
    ? Promise.resolve({ packages: [] })
    : _fetchSkillStoreFrom(localBase).catch(function() { return { packages: [] }; });

  Promise.all([remoteReq, localReq])
    .then(function(results) {
      var d = _mergeSkillStorePackages(results[0], results[1]);
      var packages = (d && Array.isArray(d.packages)) ? d.packages : [];
      var isSkillAdmin = !!(d && d.is_skill_store_admin);
      var needYoutube = packages.some(function(p) { return p.id === 'youtube_publish'; });
      var ecommercePkg = packages.filter(function(p) { return p.id === 'comfly_ecommerce_detail_skill'; })[0] || null;

      function paintSkillStoreList() {
        var html = _renderXSkillCard() + _renderComflyCard();
        if (ecommercePkg) html += _renderEcommerceDetailCard({ pkg: ecommercePkg });
        html += _renderMetaSocialCard();
        var hasWxPkg = packages.some(function(p) { return p.id === 'openclaw_weixin_channel'; });
        if (hasWxPkg) {
          var wxPkg = packages.filter(function(p) { return p.id === 'openclaw_weixin_channel'; })[0];
          var hasLocal = !!(typeof LOCAL_API_BASE !== 'undefined' && LOCAL_API_BASE);
          html += _renderOpenclawWeixinCard({
            showDebug: !!(isSkillAdmin && wxPkg && wxPkg.store_visibility === 'debug'),
            noLocalBackend: !hasLocal,
          });
        }
        html += packages.map(function(pkg) {
          var debugBadge = (isSkillAdmin && pkg.store_visibility === 'debug')
            ? '<span class="badge-coming" style="background:rgba(139,92,246,0.12);color:#a78bfa;border-color:rgba(139,92,246,0.25);margin-right:0.35rem;">调试</span> '
            : '';
          if (pkg.id === 'sutui_mcp') return '';
          /* 爆款TVC 仅由上方 _renderComflyCard() 展示，避免与 skill_registry 的 comfly_veo_skill 重复成两张卡 */
          if (pkg.id === 'comfly_veo_skill') return '';
          if (pkg.id === 'comfly_ecommerce_detail_skill') return '';
          if (pkg.id === 'openclaw_weixin_channel') return '';
          if (pkg.id === 'youtube_publish') {
            if (typeof EDITION === 'undefined' || EDITION !== 'online') return '';
            if (!isSkillAdmin) return '';
            return _renderYoutubePublishCard({
              pkg: pkg,
              showDebug: !!(isSkillAdmin && pkg.store_visibility === 'debug'),
            });
          }
          if (pkg.id === 'twilio_whatsapp') {
            if (typeof EDITION === 'undefined' || EDITION !== 'online') return '';
            return _renderTwilioWhatsappCard({
              pkg: pkg,
              showDebug: !!(isSkillAdmin && pkg.store_visibility === 'debug'),
            });
          }
        if (pkg.id === 'messenger_reply') {
          if (typeof EDITION === 'undefined' || EDITION !== 'online') return '';
          var tagsM = (pkg.tags || []).map(function(t) { return '<span class="tag">' + escapeHtml(t) + '</span>'; }).join('');
          var capM = pkg.capabilities_count ? ' · ' + pkg.capabilities_count + ' 个能力' : '';
          return '<div class="skill-store-card messenger-reply-card" style="cursor:pointer;border-color:rgba(99,102,241,0.35);background:linear-gradient(135deg,rgba(99,102,241,0.08),transparent);">' +
            '<div class="card-label">' + debugBadge + escapeHtml(pkg.type || 'skill') + ' <span class="badge-installed">可配置</span></div>' +
            '<div class="card-value">' + escapeHtml(pkg.name || pkg.id) + '</div>' +
            '<div class="card-desc">' + escapeHtml(pkg.description || '') + capM + '</div>' +
            '<div class="card-tags">' + tagsM + '</div>' +
            '<div class="card-actions"><button type="button" class="btn btn-primary btn-sm messenger-config-entry-btn">进入配置</button></div></div>';
        }
        if (pkg.id === 'ecommerce_publish_skill') {
          var tagsE = (pkg.tags || []).map(function(t) { return '<span class="tag">' + escapeHtml(t) + '</span>'; }).join('');
          var capE = pkg.capabilities_count ? ' · ' + pkg.capabilities_count + ' 个能力' : '';
          return '<div class="skill-store-card ecommerce-publish-card" style="cursor:pointer;border-color:rgba(251,146,60,0.35);background:linear-gradient(135deg,rgba(251,146,60,0.08),transparent);">' +
            '<div class="card-label">' + debugBadge + escapeHtml(pkg.type || 'skill') + ' <span class="badge-installed">可配置</span></div>' +
            '<div class="card-value">' + escapeHtml(pkg.name || pkg.id) + '</div>' +
            '<div class="card-desc">' + escapeHtml(pkg.description || '') + capE + '</div>' +
            '<div class="card-tags">' + tagsE + '</div>' +
            '<div class="card-actions"><button type="button" class="btn btn-primary btn-sm ecommerce-publish-entry-btn">管理店铺账号</button></div></div>';
        }
        if (pkg.id === 'wecom_reply') {
          var tags = (pkg.tags || []).map(function(t) { return '<span class="tag">' + escapeHtml(t) + '</span>'; }).join('');
          var capCount = pkg.capabilities_count ? ' · ' + pkg.capabilities_count + ' 个能力' : '';
          // 与 lobster 商店展示一致：仅「可配置」+ 配置按钮；积分解锁在点击时由服务器 wecom-config-eligible 判定
          return '<div class="skill-store-card wecom-reply-card" style="cursor:pointer;">' +
            '<div class="card-label">' + debugBadge + escapeHtml(pkg.type || 'skill') + ' <span class="badge-installed">可配置</span></div>' +
            '<div class="card-value">' + escapeHtml(pkg.name || pkg.id) + '</div>' +
            '<div class="card-desc">' + escapeHtml(pkg.description || '') + capCount + '</div>' +
            '<div class="card-tags">' + tags + '</div>' +
            '<div class="card-actions"><button type="button" class="btn btn-primary btn-sm wecom-config-entry-btn">配置</button></div></div>';
        }
        var statusBadge = '';
        var actionBtn = '';
        if (pkg.status === 'installed') {
          statusBadge = '<span class="badge-installed">已安装</span>';
          actionBtn = pkg.default_installed ? '' : '<button type="button" class="btn btn-ghost btn-sm" data-uninstall="' + escapeAttr(pkg.id) + '">卸载</button>';
        } else if (pkg.status === 'coming_soon') {
          statusBadge = '<span class="badge-coming">即将推出</span>';
        } else {
          actionBtn = '<button type="button" class="btn btn-primary btn-sm" data-install="' + escapeAttr(pkg.id) + '">安装</button>';
          if (pkg.unlock_price_credits && !pkg.unlocked) {
            actionBtn = '<button type="button" class="btn btn-primary btn-sm" data-unlock-credits="' + escapeAttr(pkg.id) + '">积分解锁（' + (pkg.unlock_price_credits || 0) + '）</button> ' + actionBtn;
          }
        }
        var tags = (pkg.tags || []).map(function(t) { return '<span class="tag">' + escapeHtml(t) + '</span>'; }).join('');
          var capCount = pkg.capabilities_count ? ' · ' + pkg.capabilities_count + ' 个能力' : '';
        return '<div class="skill-store-card">' +
          '<div class="card-label">' + debugBadge + escapeHtml(pkg.type || 'skill') + ' ' + statusBadge + '</div>' +
          '<div class="card-value">' + escapeHtml(pkg.name || pkg.id) + '</div>' +
            '<div class="card-desc">' + escapeHtml(pkg.description || '') + capCount + '</div>' +
          '<div class="card-tags">' + tags + '</div>' +
          '<div class="card-actions">' + actionBtn + '</div></div>';
      }).join('');
        el.innerHTML = html;
        _bindWecomConfigEntry();
        _bindMessengerCardEntry();
        _bindTwilioWhatsappCardEntry();
        _bindYoutubePublishCardEntry();
        _bindMetaSocialCardEntry();
        _bindEcommerceDetailCardEntry();
        _bindEcommercePublishCardEntry();
        _bindInstallUninstall(el);
        _bindXSkillConfigBtn();
        _bindComflyConfigBtn();
      }

      function finishRender() {
        var hasWxPkg = packages.some(function(p) { return p.id === 'openclaw_weixin_channel'; });
        if (!hasWxPkg) {
          paintSkillStoreList();
          return;
        }
        var wxBase = _openclawWeixinResolveBase();
        if (!wxBase) {
          paintSkillStoreList();
          return;
        }
        fetch(wxBase + '/api/openclaw/weixin-login/last', { headers: authHeaders() })
          .then(function(r) { return r.json(); })
          .then(function(d) {
            _openclawWeixinLast = { last_ok: !!d.last_ok, at: d.at || null, detail: d.detail || '' };
            paintSkillStoreList();
          })
          .catch(function() { paintSkillStoreList(); });
      }

      _loadXSkillStatus(function() {
        _loadComflyStatus(function() {
          var afterYoutube = function() {
            if (typeof _loadMetaSocialStatus === 'function') {
              _loadMetaSocialStatus(finishRender);
            } else {
              finishRender();
            }
          };
          if (needYoutube && isSkillAdmin) {
            _loadYoutubePublishStatus(afterYoutube);
          } else {
            afterYoutube();
          }
        });
      });
    })
    .catch(function() { el.innerHTML = '<p class="msg err">加载失败</p>'; });
}

// ── 企业微信：与 lobster 相同入口；online 仅在点击卡片/配置时多一步服务器 wecom-config-eligible ──

function _fetchWecomConfigEligible(serverBase) {
  return fetch(serverBase + '/skills/wecom-config-eligible', { headers: authHeaders() })
    .then(function(r) {
      if (r.status === 401) { throw new Error('401'); }
      if (!r.ok) throw new Error('eligible');
      return r.json();
    });
}

function _openWecomConfigIfUnlocked() {
  var base = (typeof API_BASE !== 'undefined' ? API_BASE : '');
  if (!base || typeof authHeaders !== 'function') {
    if (typeof showWecomConfigView === 'function') { location.hash = 'wecom-config'; showWecomConfigView(); }
    return;
  }
  function goConfig() {
    if (typeof showWecomConfigView === 'function') {
      location.hash = 'wecom-config';
      showWecomConfigView();
    }
  }
  _fetchWecomConfigEligible(base)
    .then(function(info) {
      if (info && info.allowed) {
        goConfig();
        return;
      }
      var amount = (info && info.amount_credits) || 1000;
      var pkgId = (info && info.package_id) || 'wecom_reply';
      var msg = (info && info.detail) || ('「企业微信自动回复」需 ' + amount + ' 积分解锁后才能管理本地配置。');
      if (!confirm(msg + '\n\n是否现在解锁？')) return;
      return fetch(base + '/skills/unlock-by-credits', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ package_id: pkgId })
      })
        .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
        .then(function(x) {
          if (!x.ok) {
            alert((x.data && x.data.detail) || '解锁失败');
            return;
          }
          alert(x.data.message || '解锁成功');
          if (typeof loadSkillStore === 'function') loadSkillStore();
          return _fetchWecomConfigEligible(base);
        })
        .then(function(info2) {
          if (!info2) return;
          if (!info2.allowed) {
            alert('解锁后仍无权限，请刷新后重试');
            return;
          }
          goConfig();
        });
    })
    .catch(function(e) {
      if (e && e.message === '401') { alert('请先登录'); return; }
      alert('无法连接服务器校验企微权限，请稍后重试');
    });
}

function _bindYoutubePublishCardEntry() {
  document.querySelectorAll('.youtube-publish-card').forEach(function(card) {
    card.addEventListener('click', function(e) {
      if (e.target.closest('.card-actions')) return;
      window._openYoutubeAccountsView();
    });
  });
  document.querySelectorAll('.youtube-publish-entry-btn').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      window._openYoutubeAccountsView();
    });
  });
}

function _bindTwilioWhatsappCardEntry() {
  document.querySelectorAll('.twilio-whatsapp-card').forEach(function(card) {
    card.addEventListener('click', function(e) {
      if (e.target.closest('.card-actions')) return;
      _openTwilioWhatsappDetailView();
    });
  });
  document.querySelectorAll('.twilio-whatsapp-entry-btn').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      _openTwilioWhatsappConfigView();
    });
  });
}

function _bindMessengerCardEntry() {
  document.querySelectorAll('.messenger-reply-card').forEach(function(card) {
    card.addEventListener('click', function(e) {
      if (e.target.closest('.card-actions')) return;
      _openMessengerConfigView();
    });
  });
  document.querySelectorAll('.messenger-config-entry-btn').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      _openMessengerConfigView();
    });
  });
}

function _bindWecomConfigEntry() {
  document.querySelectorAll('.wecom-reply-card').forEach(function(card) {
    card.addEventListener('click', function(e) {
      if (e.target.closest('.card-actions')) return;
      _openWecomConfigIfUnlocked();
    });
  });
  document.querySelectorAll('.wecom-config-entry-btn').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      _openWecomConfigIfUnlocked();
    });
  });
}

function _bindEcommercePublishCardEntry() {
  document.querySelectorAll('.ecommerce-publish-card').forEach(function(card) {
    card.addEventListener('click', function(e) {
      if (e.target.closest('.card-actions')) return;
      _navigateToEcommerceAccounts();
    });
  });
  document.querySelectorAll('.ecommerce-publish-entry-btn').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      _navigateToEcommerceAccounts();
    });
  });
}

function _navigateToEcommerceAccounts() {
  var publishTab = document.querySelector('[data-view="publish"]');
  if (publishTab) publishTab.click();
  setTimeout(function() {
    var filter = document.getElementById('accountPlatformFilter');
    if (filter) {
      filter.value = 'douyin_shop';
      filter.dispatchEvent(new Event('change'));
    }
  }, 300);
}

// ── xSkill Token Modal ──────────────────────────────────────────────

function _bindEcommerceDetailCardEntry() {
  document.querySelectorAll('.ecommerce-detail-card').forEach(function(card) {
    card.addEventListener('click', function(e) {
      if (e.target.closest('.card-actions')) return;
      if (typeof window._openEcommerceDetailStudioView === 'function') window._openEcommerceDetailStudioView();
    });
  });
  document.querySelectorAll('.ecommerce-detail-entry-btn').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      if (typeof window._openEcommerceDetailStudioView === 'function') window._openEcommerceDetailStudioView();
    });
  });
}

function _bindComflyConfigBtn__legacy_unused() {
  document.querySelectorAll('#comflyConfigBtn, .js-comfly-config-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var modal = document.getElementById('comflyModal');
      var keyInput = document.getElementById('comflyApiKeyInput');
      var baseInput = document.getElementById('comflyApiBaseInput');
      if (!modal) return;
      if (keyInput) {
        keyInput.value = '';
        keyInput.placeholder = _comflyStatus.has_user_key
          ? '已保存(' + (_comflyStatus.masked_user_key || '……') + ')，输入新 Key 可覆盖'
          : '粘贴 Comfly API Key';
      }
      if (baseInput) {
        baseInput.value = _comflyStatus.user_api_base || '';
        baseInput.placeholder = _comflyStatus.default_api_base_hint || 'https://ai.comfly.chat/v1';
      }
      var msgEl = document.getElementById('comflyModalMsg');
      if (msgEl) { msgEl.style.display = 'none'; msgEl.textContent = ''; }
      modal.classList.add('visible');
    });
  });
  return;
  document.querySelectorAll('#comflyConfigBtn, .js-comfly-config-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var modal = document.getElementById('comflyModal');
      var keyInput = document.getElementById('comflyApiKeyInput');
      var baseInput = document.getElementById('comflyApiBaseInput');
      if (!modal) return;
    if (keyInput) {
      keyInput.value = '';
      keyInput.placeholder = _comflyStatus.has_user_key
        ? '已保存 (' + (_comflyStatus.masked_user_key || '••••') + ')，输入新 Key 可覆盖'
        : '粘贴 Comfly API Key';
    }
    if (baseInput) {
      baseInput.value = _comflyStatus.user_api_base || '';
      baseInput.placeholder = _comflyStatus.default_api_base_hint || 'https://ai.comfly.chat/v1';
    }
    var msgEl = document.getElementById('comflyModalMsg');
    if (msgEl) { msgEl.style.display = 'none'; msgEl.textContent = ''; }
      modal.classList.add('visible');
    });
  });
}

function _bindXSkillConfigBtn() {
  if (EDITION === 'online') return;
  var btn = document.getElementById('xskillConfigBtn');
  if (!btn) return;
  btn.addEventListener('click', function() {
    var modal = document.getElementById('xskillModal');
    var tokenInput = document.getElementById('xskillTokenInput');
    var urlInput = document.getElementById('xskillUrlInput');
    if (!modal) return;
    if (tokenInput) { tokenInput.value = ''; tokenInput.placeholder = _xskillStatus.has_token ? '已配置 (' + _xskillStatus.token + ')' : 'sk-...'; }
    if (urlInput) urlInput.value = _xskillStatus.url || '';
    modal.classList.add('visible');
  });
}

function _openComflyConfigModal() {
  var modal = document.getElementById('comflyModal');
  var keyInput = document.getElementById('comflyApiKeyInput');
  var baseInput = document.getElementById('comflyApiBaseInput');
  if (!modal) return;
  if (keyInput) {
    keyInput.value = '';
    keyInput.placeholder = _comflyStatus.has_user_key
      ? '已保存 Key（' + (_comflyStatus.masked_user_key || '已脱敏') + '），输入新 Key 可覆盖'
      : '粘贴 Comfly API Key';
  }
  if (baseInput) {
    baseInput.value = _comflyStatus.user_api_base || '';
    baseInput.placeholder = _comflyStatus.default_api_base_hint || 'https://ai.comfly.chat/v1';
  }
  var msgEl = document.getElementById('comflyModalMsg');
  if (msgEl) {
    msgEl.style.display = 'none';
    msgEl.textContent = '';
  }
  modal.classList.add('visible');
}

function _bindComflyConfigBtn() {
  document.querySelectorAll('#comflyConfigBtn, .js-comfly-config-btn').forEach(function(btn) {
    if (btn.dataset.comflyBound === '1') return;
    btn.dataset.comflyBound = '1';
    btn.addEventListener('click', function() {
      _openComflyConfigModal();
    });
  });
}

(function _initXSkillModal() {
  var modal = document.getElementById('xskillModal');
  if (!modal) return;
  var cancelBtn = document.getElementById('xskillModalCancel');
  var saveBtn = document.getElementById('xskillModalSave');

  function closeModal() { modal.classList.remove('visible'); }

  if (cancelBtn) cancelBtn.addEventListener('click', closeModal);
  modal.addEventListener('click', function(e) { if (e.target === modal) closeModal(); });

  if (saveBtn) saveBtn.addEventListener('click', function() {
    var tokenInput = document.getElementById('xskillTokenInput');
    var urlInput = document.getElementById('xskillUrlInput');
    var msgEl = document.getElementById('xskillModalMsg');
    var body = {};
    if (tokenInput && tokenInput.value.trim()) body.token = tokenInput.value.trim();
    if (urlInput && urlInput.value.trim()) body.url = urlInput.value.trim();
    if (!body.token && !_xskillStatus.has_token) {
      if (msgEl) { msgEl.textContent = '请输入 Token'; msgEl.className = 'msg err'; msgEl.style.display = ''; }
      return;
    }
    saveBtn.disabled = true; saveBtn.textContent = '保存中…';
    fetch((LOCAL_API_BASE || '') + '/api/sutui/config', {
      method: 'POST', headers: authHeaders(),
      body: JSON.stringify(body)
    })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
      .then(function(x) {
        if (x.ok) {
          if (msgEl) { msgEl.textContent = '保存成功'; msgEl.className = 'msg'; msgEl.style.display = ''; }
          setTimeout(function() { closeModal(); loadSkillStore(); }, 600);
        } else {
          if (msgEl) { msgEl.textContent = x.data.detail || '保存失败'; msgEl.className = 'msg err'; msgEl.style.display = ''; }
        }
      })
      .catch(function() { if (msgEl) { msgEl.textContent = '网络错误'; msgEl.className = 'msg err'; msgEl.style.display = ''; } })
      .finally(function() { saveBtn.disabled = false; saveBtn.textContent = '保存'; });
  });
})();

(function _initComflyModal() {
  var modal = document.getElementById('comflyModal');
  if (!modal) return;
  var cancelBtn = document.getElementById('comflyModalCancel');
  var saveBtn = document.getElementById('comflyModalSave');

  function closeModal() { modal.classList.remove('visible'); }

  if (cancelBtn) cancelBtn.addEventListener('click', closeModal);
  modal.addEventListener('click', function(e) { if (e.target === modal) closeModal(); });

  if (saveBtn) saveBtn.addEventListener('click', function() {
    var keyInput = document.getElementById('comflyApiKeyInput');
    var baseInput = document.getElementById('comflyApiBaseInput');
    var msgEl = document.getElementById('comflyModalMsg');
    var k = keyInput ? keyInput.value.trim() : '';
    var b = baseInput ? baseInput.value.trim().replace(/\/+$/, '') : '';
    var body = {};
    if (k) body.api_key = k;
    else if (!_comflyStatus.has_user_key) {
      if (msgEl) { msgEl.textContent = '请填写 Comfly API Key'; msgEl.className = 'msg err'; msgEl.style.display = ''; }
      return;
    }
    if (!b && !_comflyStatus.user_api_base) {
      if (msgEl) { msgEl.textContent = '请填写 API 根地址（常用 https://ai.comfly.chat/v1）'; msgEl.className = 'msg err'; msgEl.style.display = ''; }
      return;
    }
    body.api_base = b;
    saveBtn.disabled = true; saveBtn.textContent = '保存中…';
    fetch((LOCAL_API_BASE || '') + '/api/comfly/config', {
      method: 'POST', headers: authHeaders(),
      body: JSON.stringify(body)
    })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
      .then(function(x) {
        if (x.ok) {
          if (msgEl) { msgEl.textContent = '已保存'; msgEl.className = 'msg'; msgEl.style.display = ''; }
          setTimeout(function() { closeModal(); loadSkillStore(); }, 500);
        } else {
          var det = x.data && (x.data.detail || x.data.message);
          if (msgEl) { msgEl.textContent = det || '保存失败'; msgEl.className = 'msg err'; msgEl.style.display = ''; }
        }
      })
      .catch(function() { if (msgEl) { msgEl.textContent = '网络错误'; msgEl.className = 'msg err'; msgEl.style.display = ''; } })
      .finally(function() { saveBtn.disabled = false; saveBtn.textContent = '保存'; });
  });
})();

/** 在线版：认证在公网 API_BASE，MCP/剪辑走本机 LOCAL_API_BASE；安装技能须在两端写入（服务端记账 + 本机 CapabilityConfig/catalog）。 */
function _localSkillApiBase() {
  var l = (typeof LOCAL_API_BASE !== 'undefined' && LOCAL_API_BASE) ? String(LOCAL_API_BASE).replace(/\/$/, '') : '';
  var a = (typeof API_BASE !== 'undefined' && API_BASE) ? String(API_BASE).replace(/\/$/, '') : '';
  if (!l || l === a) return '';
  return l;
}

function _syncLocalSkillInstall(pkgId) {
  var base = _localSkillApiBase();
  if (!base) return Promise.resolve({ ok: true, skipped: true });
  return fetch(base + '/skills/install', {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({ package_id: pkgId })
  }).then(function(r) {
    return r.json().then(function(d) { return { ok: r.ok, data: d, status: r.status }; });
  });
}

function _syncLocalSkillUninstall(pkgId) {
  var base = _localSkillApiBase();
  if (!base) return Promise.resolve({ ok: true, skipped: true });
  return fetch(base + '/skills/uninstall', {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({ package_id: pkgId })
  }).then(function(r) {
    return r.json().then(function(d) { return { ok: r.ok, data: d, status: r.status }; });
  });
}

function _bindInstallUninstall(el) {
      el.querySelectorAll('button[data-unlock-credits]').forEach(function(btn) {
        btn.addEventListener('click', function() {
          var pkgId = btn.getAttribute('data-unlock-credits');
          btn.disabled = true; btn.textContent = '解锁中…';
          fetch(API_BASE + '/skills/unlock-by-credits', { method: 'POST', headers: authHeaders(), body: JSON.stringify({ package_id: pkgId }) })
            .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
            .then(function(x) {
              if (x.ok) {
                alert((x.data.message || '解锁成功') + '\n\n请再点击「安装」，以在本机注册能力（含素材剪辑 media.edit，供 MCP 使用）。');
                loadSkillStore();
              } else { alert(x.data.detail || '解锁失败'); btn.disabled = false; btn.textContent = '积分解锁'; }
            }).catch(function() { alert('网络错误'); btn.disabled = false; btn.textContent = '积分解锁'; });
        });
      });
      el.querySelectorAll('button[data-install]').forEach(function(btn) {
        btn.addEventListener('click', function() {
          var pkgId = btn.getAttribute('data-install');
          btn.disabled = true; btn.textContent = '安装中…';
          fetch(API_BASE + '/skills/install', { method: 'POST', headers: authHeaders(), body: JSON.stringify({ package_id: pkgId }) })
            .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
            .then(function(x) {
              if (!x.ok) {
                alert(x.data.detail || '安装失败');
                btn.disabled = false; btn.textContent = '安装';
                return;
              }
              return _syncLocalSkillInstall(pkgId).then(function(loc) {
                var msg = x.data.message || '安装成功';
                if (loc.skipped) {
                  alert(msg);
                } else if (!loc.ok) {
                  alert(msg + '\n\n【本机未同步】' + ((loc.data && loc.data.detail) || ('HTTP ' + loc.status)) + '\n请确认本机 lobster_online 后端已运行，再对同一技能点一次「安装」。');
                } else {
                  alert(msg + (loc.data && loc.data.already_installed ? '\n（本机能力已就绪）' : '\n（本机已注册能力）'));
                }
                loadSkillStore();
              }).catch(function() {
                alert((x.data.message || '服务端已安装') + '\n\n本机同步请求失败：请确认本机后端已启动，再点一次「安装」。');
                loadSkillStore();
              });
            }).catch(function() { alert('网络错误'); btn.disabled = false; btn.textContent = '安装'; });
        });
      });
      el.querySelectorAll('button[data-uninstall]').forEach(function(btn) {
        btn.addEventListener('click', function() {
          var pkgId = btn.getAttribute('data-uninstall');
          if (!confirm('确定卸载 ' + pkgId + '？')) return;
          btn.disabled = true; btn.textContent = '卸载中…';
          fetch(API_BASE + '/skills/uninstall', { method: 'POST', headers: authHeaders(), body: JSON.stringify({ package_id: pkgId }) })
            .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
            .then(function(x) {
              if (!x.ok) {
                alert(x.data.detail || '卸载失败');
                btn.disabled = false; btn.textContent = '卸载';
                return;
              }
              return _syncLocalSkillUninstall(pkgId).then(function(loc) {
                var baseMsg = x.data.message || '卸载成功';
                alert(baseMsg + ((loc.ok || loc.skipped) ? '' : '\n（本机卸载未完全同步，可再点卸载或重启本机后端）'));
                loadSkillStore();
              }).catch(function() {
                alert('服务端已卸载；本机同步失败，请稍后重试卸载或重启本机服务。');
                loadSkillStore();
              });
            }).catch(function() { alert('网络错误'); btn.disabled = false; btn.textContent = '卸载'; });
        });
      });
}

// Add MCP Modal
(function() {
  var modal = document.getElementById('addMcpModal');
  var openBtn = document.getElementById('openAddMcpModal');
  var cancelBtn = document.getElementById('addMcpModalCancel');
  var addBtn = document.getElementById('addMcpBtn');
  if (!modal) return;

  function closeModal() { modal.classList.remove('visible'); }

  if (openBtn) openBtn.addEventListener('click', function() {
    var nameInput = document.getElementById('addMcpName');
    var urlInput = document.getElementById('addMcpUrl');
    var msgEl = document.getElementById('addMcpMsg');
    if (nameInput) nameInput.value = '';
    if (urlInput) urlInput.value = '';
    if (msgEl) msgEl.style.display = 'none';
    modal.classList.add('visible');
  });
  if (cancelBtn) cancelBtn.addEventListener('click', closeModal);
  modal.addEventListener('click', function(e) { if (e.target === modal) closeModal(); });

  if (addBtn) addBtn.addEventListener('click', function() {
    var nameInput = document.getElementById('addMcpName');
    var urlInput = document.getElementById('addMcpUrl');
    var msgEl = document.getElementById('addMcpMsg');
    var name = (nameInput.value || '').trim();
    var url = (urlInput.value || '').trim();
    if (!name || !url) { showMsg(msgEl, '请填写名称和 URL', true); return; }
    addBtn.disabled = true;
    fetch((LOCAL_API_BASE || '') + '/skills/add-mcp', {
      method: 'POST', headers: authHeaders(),
      body: JSON.stringify({ name: name, url: url })
    })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
      .then(function(x) {
        if (x.ok) {
          showMsg(msgEl, x.data.message || 'MCP 已添加', false);
          setTimeout(function() { closeModal(); loadSkillStore(); }, 600);
        } else { showMsg(msgEl, x.data.detail || '添加失败', true); }
      })
      .catch(function() { showMsg(msgEl, '网络错误', true); })
      .finally(function() { addBtn.disabled = false; });
  });
})();

var refreshStoreBtn = document.getElementById('refreshStoreBtn');
if (refreshStoreBtn) {
  refreshStoreBtn.addEventListener('click', function() {
    loadSkillStore();
    if (STORE_OFFICIAL_TAB_ENABLED && _currentStoreTab === 'official') browseOfficialPage(_officialPage);
  });
}

// ── 官方在线 Tab: paginated browsing + cached search ───────────────

var _officialPage = 1;
var _officialHasNext = false;
var _officialLoaded = false;
var _activeCategory = null;
var _searchMode = false;

var CATEGORY_LABELS = {
  image: '图片', video: '视频', audio: '音频', database: '数据库',
  search: '搜索/爬虫', code: '代码/Git', file: '文件', ai: 'AI/LLM',
  communication: '通讯', devops: 'DevOps'
};

function renderCategoryBar(categories) {
  var bar = document.getElementById('mcpCategoryBar');
  if (!bar || !categories) return;
  var keys = Object.keys(categories);
  if (!keys.length) { bar.innerHTML = ''; return; }

  var html = '<span class="category-chip' + (!_activeCategory ? ' active' : '') + '" data-cat="">全部</span>';
  keys.forEach(function(cat) {
    var label = CATEGORY_LABELS[cat] || cat;
    var active = (_activeCategory === cat) ? ' active' : '';
    html += '<span class="category-chip' + active + '" data-cat="' + escapeAttr(cat) + '">' +
      escapeHtml(label) + '<span class="chip-count">(' + categories[cat] + ')</span></span>';
  });
  bar.innerHTML = html;
  bar.querySelectorAll('.category-chip').forEach(function(chip) {
    chip.addEventListener('click', function() {
      var cat = chip.getAttribute('data-cat') || '';
      _activeCategory = cat || null;
      searchCachedSkills(
        (document.getElementById('mcpRegistrySearch') || {}).value || '',
        cat || null, 1
      );
    });
  });
}

function browseOfficialPage(page) {
  var el = document.getElementById('mcpRegistryResults');
  var pagingEl = document.getElementById('mcpRegistryPaging');
  var totalEl = document.getElementById('mcpRegistryTotal');
  if (!el) return;
  _searchMode = false;
  _activeCategory = null;
  var searchInput = document.getElementById('mcpRegistrySearch');
  if (searchInput) searchInput.value = '';

  el.innerHTML = '<p class="meta">加载第 ' + page + ' 页…</p>';
  if (pagingEl) pagingEl.innerHTML = '';

  fetch((LOCAL_API_BASE || '') + '/api/mcp-registry/browse?page=' + page, { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      _officialLoaded = true;
      _officialPage = d.page || page;
      _officialHasNext = !!d.has_next;
      var servers = (d && Array.isArray(d.servers)) ? d.servers : [];
      if (d.categories) renderCategoryBar(d.categories);
      if (totalEl) totalEl.textContent = '本地已缓存 ' + (d.cached_total || 0) + ' 个技能';

      if (!servers.length) {
        el.innerHTML = '<p class="meta">该页没有更多技能了</p>';
      } else {
        _renderServerCards(el, servers);
      }
      _renderBrowsePaging(pagingEl);
    })
    .catch(function() { el.innerHTML = '<p class="msg err">网络错误，请确认可访问外网</p>'; });
}

function searchCachedSkills(query, category, page) {
  var el = document.getElementById('mcpRegistryResults');
  var pagingEl = document.getElementById('mcpRegistryPaging');
  var totalEl = document.getElementById('mcpRegistryTotal');
  if (!el) return;
  _searchMode = true;

  el.innerHTML = '<p class="meta">搜索中…</p>';
  if (pagingEl) pagingEl.innerHTML = '';

  var params = ['page=' + (page || 1), 'page_size=30'];
  if (query) params.push('q=' + encodeURIComponent(query));
  if (category) params.push('category=' + encodeURIComponent(category));
  var url = (LOCAL_API_BASE || '') + '/api/mcp-registry/search?' + params.join('&');

  fetch(url, { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var servers = (d && Array.isArray(d.servers)) ? d.servers : [];
      if (d.categories) renderCategoryBar(d.categories);
      var total = d.total || 0;
      var curPage = d.page || 1;
      var hasNext = !!d.has_next;
      if (totalEl) totalEl.textContent = '搜索到 ' + total + ' 个';

      if (!servers.length) {
        el.innerHTML = '<p class="meta">本地缓存中未找到匹配技能。请换个关键词，或稍后重试。</p>';
      } else {
        _renderServerCards(el, servers);
      }
      _renderSearchPaging(pagingEl, query, category, curPage, hasNext, total);
    })
    .catch(function() { el.innerHTML = '<p class="msg err">搜索失败</p>'; });
}

function _renderServerCards(el, servers) {
  el.innerHTML = servers.map(function(srv) {
    var hasRemote = srv.remote_url && srv.remote_url.indexOf('{') < 0;
    var addBtn = hasRemote
      ? '<button type="button" class="btn btn-primary btn-sm" data-add-registry-name="' + escapeAttr(srv.name) + '" data-add-registry-url="' + escapeAttr(srv.remote_url) + '">添加</button>'
      : '';
    var linkBtn = srv.website
      ? '<a href="' + escapeAttr(srv.website) + '" target="_blank" rel="noopener" class="btn btn-ghost btn-sm">官网</a>'
      : (srv.repo ? '<a href="' + escapeAttr(srv.repo) + '" target="_blank" rel="noopener" class="btn btn-ghost btn-sm">源码</a>' : '');
    var version = srv.version ? '<span class="tag">v' + escapeHtml(srv.version) + '</span>' : '';
    var tagHtml = (srv.tags || []).map(function(t) {
      var label = CATEGORY_LABELS[t] || t;
      return '<span class="tag">' + escapeHtml(label) + '</span>';
    }).join('');
    return '<div class="skill-store-card">' +
      '<div class="card-label">MCP ' + version + '</div>' +
      '<div class="card-value">' + escapeHtml(srv.title || srv.name) + '</div>' +
      '<div class="card-desc">' + escapeHtml(srv.description || '') + '</div>' +
      '<div class="card-tags">' + tagHtml + '</div>' +
      '<div style="font-size:0.75rem;color:var(--text-muted);margin-top:0.25rem;word-break:break-all;">' + escapeHtml(srv.name) + '</div>' +
      '<div class="card-actions">' + addBtn + linkBtn + '</div></div>';
  }).join('');
  _bindAddButtons(el);
}

function _renderBrowsePaging(pagingEl) {
  if (!pagingEl) return;
  var html = '';
  if (_officialPage > 1) {
    html += '<button type="button" class="btn btn-ghost btn-sm" id="pagePrev">上一页</button>';
  }
  html += '<span class="paging-info">第 ' + _officialPage + ' 页</span>';
  if (_officialHasNext) {
    html += '<button type="button" class="btn btn-primary btn-sm" id="pageNext">下一页</button>';
  }
  pagingEl.innerHTML = html;
  var prevBtn = document.getElementById('pagePrev');
  var nextBtn = document.getElementById('pageNext');
  if (prevBtn) prevBtn.addEventListener('click', function() { browseOfficialPage(_officialPage - 1); });
  if (nextBtn) nextBtn.addEventListener('click', function() { browseOfficialPage(_officialPage + 1); });
}

function _renderSearchPaging(pagingEl, query, category, curPage, hasNext, total) {
  if (!pagingEl) return;
  var html = '';
  if (curPage > 1) {
    html += '<button type="button" class="btn btn-ghost btn-sm" id="searchPrev">上一页</button>';
  }
  html += '<span class="paging-info">第 ' + curPage + ' 页 · 共 ' + total + ' 个</span>';
  if (hasNext) {
    html += '<button type="button" class="btn btn-primary btn-sm" id="searchNext">下一页</button>';
  }
  html += '<button type="button" class="btn btn-ghost btn-sm" id="backToBrowse" style="margin-left:0.5rem;">返回浏览</button>';
  pagingEl.innerHTML = html;
  var prev = document.getElementById('searchPrev');
  var next = document.getElementById('searchNext');
  var back = document.getElementById('backToBrowse');
  if (prev) prev.addEventListener('click', function() { searchCachedSkills(query, category, curPage - 1); });
  if (next) next.addEventListener('click', function() { searchCachedSkills(query, category, curPage + 1); });
  if (back) back.addEventListener('click', function() { browseOfficialPage(1); });
}

function _bindAddButtons(container) {
  container.querySelectorAll('button[data-add-registry-name]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var name = btn.getAttribute('data-add-registry-name') || '';
      var url = btn.getAttribute('data-add-registry-url') || '';
      var shortName = name.replace(/[^a-zA-Z0-9_-]/g, '_').replace(/_+/g, '_');
      btn.disabled = true; btn.textContent = '添加中…';
      fetch((LOCAL_API_BASE || '') + '/skills/add-mcp', {
        method: 'POST', headers: authHeaders(),
        body: JSON.stringify({ name: shortName, url: url })
      })
        .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
        .then(function(x) {
          if (x.ok) {
            btn.textContent = '已添加'; btn.className = 'btn btn-ghost btn-sm';
            loadSkillStore();
          } else { alert(x.data.detail || '添加失败'); btn.disabled = false; btn.textContent = '添加'; }
        })
        .catch(function() { alert('网络错误'); btn.disabled = false; btn.textContent = '添加'; });
    });
  });
}

// search bar + enter key
var mcpSearchBtn = document.getElementById('mcpRegistrySearchBtn');
var mcpSearchInput = document.getElementById('mcpRegistrySearch');
if (mcpSearchBtn) {
  mcpSearchBtn.addEventListener('click', function() {
    var q = mcpSearchInput ? mcpSearchInput.value.trim() : '';
    if (!q && !_activeCategory) { browseOfficialPage(1); return; }
    searchCachedSkills(q, _activeCategory, 1);
  });
}
if (mcpSearchInput) {
  mcpSearchInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      var q = mcpSearchInput.value.trim();
      if (!q && !_activeCategory) { browseOfficialPage(1); return; }
      searchCachedSkills(q, _activeCategory, 1);
    }
  });
}

function _clearOpenclawWeixinQrInModal() {
  var wrap = document.getElementById('openclawWeixinQrWrap');
  var img = document.getElementById('openclawWeixinQrImg');
  if (img) {
    img.removeAttribute('src');
    img.alt = '';
    img.onerror = null;
  }
  if (wrap) wrap.style.display = 'none';
}

function _showOpenclawWeixinQrInModal(url) {
  var wrap = document.getElementById('openclawWeixinQrWrap');
  var img = document.getElementById('openclawWeixinQrImg');
  if (!wrap || !img || !url) return;
  var s = String(url);
  if (s.length > 2000) {
    wrap.style.display = 'none';
    return;
  }
  img.alt = '微信扫码登录';
  img.onerror = function() { wrap.style.display = 'none'; };
  img.src = 'https://api.qrserver.com/v1/create-qr-code/?size=220x220&margin=2&data=' + encodeURIComponent(s);
  wrap.style.display = 'block';
}

(function _initOpenclawWeixinSkillStore() {
  var list = document.getElementById('skillStoreList');
  if (list) {
    list.addEventListener('click', function OpenclawWeixinAuthDelegate(ev) {
      var t = ev.target && ev.target.closest && ev.target.closest('.js-openclaw-weixin-auth');
      if (!t) return;
      ev.preventDefault();
      ev.stopPropagation();
      var modal = document.getElementById('openclawWeixinLoginModal');
      if (!modal) return;
      modal.classList.add('visible');
      _startOpenclawWeixinLoginFlow();
    });
  }
  var closeBtn = document.getElementById('openclawWeixinLoginModalClose');
  if (closeBtn) {
    closeBtn.addEventListener('click', function() {
      var m = document.getElementById('openclawWeixinLoginModal');
      if (m) m.classList.remove('visible');
      _clearOpenclawWeixinQrInModal();
      if (_openclawWeixinPollTimer) {
        clearInterval(_openclawWeixinPollTimer);
        _openclawWeixinPollTimer = null;
      }
    });
  }
})();

function _startOpenclawWeixinLoginFlow() {
  var lb = _openclawWeixinResolveBase();
  var statusEl = document.getElementById('openclawWeixinLoginStatus');
  var linkEl = document.getElementById('openclawWeixinLoginQrLink');
  var logEl = document.getElementById('openclawWeixinLoginLog');
  if (!lb) {
    if (statusEl) statusEl.textContent = 'OpenClaw 扫码仅走本机 lobster_online：请在设置中配置 LOCAL_API_BASE。';
    return;
  }
  _clearOpenclawWeixinQrInModal();
  if (linkEl) {
    linkEl.href = '#';
    linkEl.textContent = '';
    linkEl.style.display = 'none';
  }
  if (logEl) logEl.textContent = '';
  if (statusEl) statusEl.textContent = '正在启动扫码任务…';
  if (_openclawWeixinPollTimer) {
    clearInterval(_openclawWeixinPollTimer);
    _openclawWeixinPollTimer = null;
  }
  fetch(lb + '/api/openclaw/weixin-login/start', { method: 'POST', headers: authHeaders() })
    .then(function(r) {
      return r.json().then(function(d) { return { ok: r.ok, status: r.status, data: d }; });
    })
    .then(function(x) {
      if (!x.ok) {
        if (statusEl) statusEl.textContent = (x.data && x.data.detail) ? x.data.detail : ('请求失败 HTTP ' + x.status);
        return;
      }
      var jobId = x.data.job_id;
      if (!jobId) {
        if (statusEl) statusEl.textContent = '未返回 job_id';
        return;
      }
      function poll() {
        fetch(lb + '/api/openclaw/weixin-login/status?job_id=' + encodeURIComponent(jobId), { headers: authHeaders() })
          .then(function(r) { return r.json(); })
          .then(function(d) {
            if (statusEl) statusEl.textContent = d.message || d.status || '';
            if (d.qrcode_url) {
              _showOpenclawWeixinQrInModal(d.qrcode_url);
              if (linkEl) {
                linkEl.href = d.qrcode_url;
                linkEl.textContent = d.qrcode_url;
                linkEl.style.display = 'inline';
              }
            }
            if (logEl && d.log_tail) logEl.textContent = d.log_tail;
            var st = d.status || '';
            if (st === 'success' || st === 'failed' || st === 'timeout') {
              if (_openclawWeixinPollTimer) {
                clearInterval(_openclawWeixinPollTimer);
                _openclawWeixinPollTimer = null;
              }
              if (st === 'success' && typeof loadSkillStore === 'function') loadSkillStore();
            }
          })
          .catch(function() { /* 单次轮询失败可忽略 */ });
      }
      poll();
      _openclawWeixinPollTimer = setInterval(poll, 1500);
    })
    .catch(function() {
      if (statusEl) statusEl.textContent = '网络错误';
    });
}

function initOnlineSkillStore() {
  if (STORE_OFFICIAL_TAB_ENABLED && _currentStoreTab === 'official' && !_officialLoaded) {
    browseOfficialPage(1);
  }
}
