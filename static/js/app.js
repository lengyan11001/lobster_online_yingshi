/** 定死：公网 lobster_server（登录/验证码/auth/me；与 pack_bundle AUTH_SERVER_BASE 一致；走 HTTPS 与 Nginx 443） */
var LOBSTER_SERVER_PUBLIC = 'http://42.194.209.150';

(function setApiBaseFromUrl() {
  // 本机回环：优先用当前页 origin（含真实端口）。port 为空时（如默认 80）不能误写成 :8000，否则会连错端口 → Failed to fetch
  var loc = window.location;
  var loopbackOrigin = '';
  try {
    if (loc && loc.hostname && /^(localhost|127\.0\.0\.1)$/i.test(loc.hostname) && /^https?:/i.test(loc.protocol || '')) {
      loopbackOrigin = (loc.origin || '').replace(/\/$/, '');
    }
  } catch (e) {}
  var lp = (loc && loc.port) ? loc.port : '';
  var LOBSTER_LOCAL_LOOPBACK = loopbackOrigin || ('http://127.0.0.1:' + (lp || '8000'));

  // 本机打开页面（localhost/127）→ API 仍走公网服务器；?api= / localStorage 可覆盖
  var isLoopbackHost = /^(localhost|127\.0\.0\.1)(:\d+)?$/i.test(window.location.host || '');
  var serverDefault = isLoopbackHost ? LOBSTER_SERVER_PUBLIC : (window.location.origin || LOBSTER_SERVER_PUBLIC);
  var p = new URLSearchParams(window.location.search);
  var api = (p.get('api') || '').trim() || (localStorage.getItem('lobster_api_base') || '').trim() || serverDefault;
  if (api) localStorage.setItem('lobster_api_base', api);
  window.__API_BASE = api;

  window.__LOCAL_API_BASE = (typeof window.__LOCAL_API_BASE !== 'undefined' ? window.__LOCAL_API_BASE : '');
  var exLocal = String(window.__LOCAL_API_BASE || '').trim();
  if (!exLocal && window.location && /^https?:/i.test(window.location.protocol || '')) {
    var h = (window.location.hostname || '').toLowerCase();
    // 公网静态页也可显式指向本机 lobster_online（内网 IP / 穿透 URL）：?local_api= 或 localStorage.lobster_local_api_base
    var localApiOverride = (p.get('local_api') || '').trim() || (localStorage.getItem('lobster_local_api_base') || '').trim();
    if (p.get('local_api')) {
      try { localStorage.setItem('lobster_local_api_base', localApiOverride.replace(/\/$/, '')); } catch (eLoc) {}
    }
    if (h === 'localhost' || h === '127.0.0.1') {
      // 架构：LOCAL_API_BASE = 本机 lobster_online 后端（对话/素材/发布/OpenClaw 扫码）；推荐与页面同源（backend/run.py 与静态同端口）。
      window.__LOCAL_API_BASE = localApiOverride ? localApiOverride.replace(/\/$/, '') : LOBSTER_LOCAL_LOOPBACK;
    } else if (
      /^192\.168\.\d{1,3}\.\d{1,3}$/.test(h) ||
      /^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(h) ||
      /^172\.(1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}$/.test(h)
    ) {
      window.__LOCAL_API_BASE = localApiOverride ? localApiOverride.replace(/\/$/, '') : window.location.origin;
    } else if (localApiOverride) {
      window.__LOCAL_API_BASE = localApiOverride.replace(/\/$/, '');
    }
  }
})();
// 本机打开静态页时 API_BASE 仍指向远程；LOCAL_API_BASE 默认同源（见 docs/架构说明_server与本地职责.md）
var API_BASE = window.__API_BASE || (function() {
  if (/^(localhost|127\.0\.0\.1)(:\d+)?$/i.test(window.location.host)) {
    return LOBSTER_SERVER_PUBLIC;
  }
  var o = window.location.origin;
  return o || LOBSTER_SERVER_PUBLIC;
})();
/** 发布与素材接口走本地（同源）；需本机运行 lobster_online 并配置 AUTH_SERVER_BASE */
var LOCAL_API_BASE = (typeof window.__LOCAL_API_BASE !== 'undefined' ? window.__LOCAL_API_BASE : '');
/** Messenger：默认海外 lobster_server（与 Meta Webhook 同机）；?messenger_api= / localStorage 可覆盖 */
(function setMessengerApiBase() {
  /** 使用 https:// 与 443，避免 https 前端页对 http:8000 的混合内容拦截；与 Nginx 反代一致 */
  var def = 'http://43.162.111.36';
  var p = new URLSearchParams(window.location.search);
  var m = (p.get('messenger_api') || '').trim() || (localStorage.getItem('lobster_messenger_api_base') || '').trim() || def;
  if (m === 'http://43.162.111.36:8000') {
    m = def;
  }
  if (m) localStorage.setItem('lobster_messenger_api_base', m);
  window.__MESSENGER_API_BASE = m;
})();
var MESSENGER_API_BASE = (typeof window.__MESSENGER_API_BASE !== 'undefined' ? window.__MESSENGER_API_BASE : '');
/** Twilio：与企微一致默认走本机同源 LOCAL_API_BASE；仅 ?twilio_api= / localStorage 显式指定时才打其它根地址（调试） */
(function setTwilioApiBase() {
  var p = new URLSearchParams(window.location.search);
  var q = (p.get('twilio_api') || '').trim();
  if (q) {
    if (q === 'http://43.162.111.36:8000') q = 'http://43.162.111.36';
    try { localStorage.setItem('lobster_twilio_api_base', q); } catch (e) {}
    window.__TWILIO_API_BASE = q;
  } else {
    var v = (localStorage.getItem('lobster_twilio_api_base') || '').trim();
    // 旧版默认写死海外根地址会导致浏览器直连跨域 Failed to fetch；与企微一致改走后，清除该默认值
    if (v === 'http://43.162.111.36' || v === 'http://43.162.111.36:8000' || v === 'https://lobster-server.icu' || v === 'http://lobster-server.icu:8000') {
      try { localStorage.removeItem('lobster_twilio_api_base'); } catch (e2) {}
      window.__TWILIO_API_BASE = '';
    } else {
      window.__TWILIO_API_BASE = v;
    }
  }
})();
var TWILIO_API_BASE = (typeof window.__TWILIO_API_BASE !== 'undefined' ? window.__TWILIO_API_BASE : '');
var token = localStorage.getItem('token');
var currentView = 'chat';
/** 在线版前端，默认连 lobster_server（注册/登录在 server 上） */
var EDITION = 'online';
/** 在线版是否允许自配模型（由 /api/edition 返回） */
var ALLOW_SELF_CONFIG_MODEL = true;
/** 在线版充值页 URL（由 /api/edition 返回） */
var RECHARGE_URL = null;

(function applyTokenFromUrl() {
  var params = new URLSearchParams(window.location.search);
  var t = params.get('token');
  if (t && t.length > 10) {
    token = t;
    localStorage.setItem('token', t);
    window.history.replaceState({}, document.title, window.location.pathname + window.location.hash);
  }
})();

function showMsg(el, text, isErr) {
  if (!el) return;
  el.textContent = text;
  el.className = 'msg ' + (isErr ? 'err' : 'ok');
  el.style.display = 'block';
}

function copyToClipboard(text, doneCb) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function() { if (doneCb) doneCb(); }).catch(function() {
      fallbackCopy(text, doneCb);
    });
  } else {
    fallbackCopy(text, doneCb);
  }
}
function fallbackCopy(text, doneCb) {
  var ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed'; ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); if (doneCb) doneCb(); } catch (e) {}
  document.body.removeChild(ta);
}

/** 在线版：与认证中心「每账号最多 3 安装槽」对应的设备身份（持久化于 localStorage） */
function getOrCreateInstallationId() {
  var k = 'lobster_installation_id';
  var v = localStorage.getItem(k);
  if (v && v.length >= 8) return v;
  var u = (typeof crypto !== 'undefined' && crypto.randomUUID)
    ? crypto.randomUUID().replace(/-/g, '')
    : (Date.now().toString(36) + Math.random().toString(36).slice(2, 18));
  localStorage.setItem(k, u);
  return u;
}

function authHeaders() {
  var h = { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (token || '') };
  h['X-Installation-Id'] = getOrCreateInstallationId();
  return h;
}

/** 从 JWT payload 解析 sub（与认证中心签发一致），供本地会话等按用户隔离；无 token 或解析失败返回空串 */
function getCurrentUserIdFromToken() {
  try {
    var t = (typeof token !== 'undefined' && token) ? token : (localStorage.getItem('token') || '');
    if (!t || t.indexOf('.') < 0) return '';
    var parts = t.split('.');
    if (parts.length < 2) return '';
    var b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    while (b64.length % 4) b64 += '=';
    var payload = JSON.parse(atob(b64));
    var sub = payload.sub;
    if (sub == null || sub === '') return '';
    return String(sub);
  } catch (e) {
    return '';
  }
}
window.getCurrentUserIdFromToken = getCurrentUserIdFromToken;

function escapeHtml(s) { return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
function escapeAttr(s) { return (s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function truncate(s, len) { s = (s || '').trim(); return s.length <= len ? s : s.slice(0, len) + '…'; }
