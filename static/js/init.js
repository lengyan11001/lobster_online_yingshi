/** 在线版：独立认证时优先显示账号+验证码登录/注册，扫码为备选；/api/edition 可覆盖 */
var USE_INDEPENDENT_AUTH = true;

/**
 * 认证中心注册接口在「在线版 + 安装槽位」下强制要求合法 X-Installation-Id。
 * 若 app.js 未加载、脚本顺序异常或 localStorage 曾异常，避免发空请求头导致「请使用最新客户端」。
 * 与 static/js/app.js 中 getOrCreateInstallationId 逻辑一致。
 */
(function ensureInstallationIdFn() {
  if (typeof window.getOrCreateInstallationId === 'function') return;
  window.getOrCreateInstallationId = function() {
    var k = 'lobster_installation_id';
    var v = '';
    try { v = localStorage.getItem(k) || ''; } catch (e) {}
    if (v && v.length >= 8) return v;
    var u = (typeof crypto !== 'undefined' && crypto.randomUUID)
      ? crypto.randomUUID().replace(/-/g, '')
      : (Date.now().toString(36) + Math.random().toString(36).slice(2, 18));
    try { localStorage.setItem(k, u); } catch (e2) {}
    return u;
  };
})();

/** 本机后端 /api/branding 与 .env LOBSTER_BRAND_MARK、static/branding/brands.json 一致 */
function applyBrandingFromApi() {
  var base = (typeof LOCAL_API_BASE !== 'undefined' && LOCAL_API_BASE) ? String(LOCAL_API_BASE).replace(/\/$/, '') : '';
  if (!base) return;
  fetch(base + '/api/branding', { credentials: 'same-origin' })
    .then(function(r) {
      if (!r.ok) return Promise.reject(new Error('branding ' + r.status));
      return r.json();
    })
    .then(function(b) {
      if (!b || typeof b !== 'object') return;
      if (b.mark) window.__LOBSTER_BRAND_MARK = b.mark;
      if (b.document_title) document.title = b.document_title;
      var icons = b.icons || {};
      var fav = document.getElementById('brandFavicon');
      if (fav && icons.favicon_32) fav.setAttribute('href', icons.favicon_32);
      var apt = document.getElementById('brandAppleTouch');
      if (apt && icons.apple_touch) apt.setAttribute('href', icons.apple_touch);
      var markImg = document.getElementById('brandLogoMark');
      if (markImg && icons.logo_mark) {
        markImg.src = icons.logo_mark;
        if (icons.logo_mark_width) markImg.width = Number(icons.logo_mark_width);
        if (icons.logo_mark_height) markImg.height = Number(icons.logo_mark_height);
      }
      var primary = document.getElementById('brandLogoPrimary');
      var accent = document.getElementById('brandLogoAccent');
      if (primary && b.logo_primary != null) primary.textContent = b.logo_primary;
      if (accent && b.logo_accent != null) accent.textContent = b.logo_accent;
      var heroH = document.getElementById('brandHeroTitle');
      var heroP = document.getElementById('brandHeroSubtitle');
      if (heroH && b.hero_title != null) heroH.textContent = b.hero_title;
      if (heroP && b.hero_subtitle != null) heroP.textContent = b.hero_subtitle;
    })
    .catch(function(e) {
      if (typeof console !== 'undefined') console.warn('[branding]', e);
    });
}
var USE_FUBEI_PAY = false;
var _fubeiPollTimer = null;
function _startFubeiPoll(outTradeNo) {
  if (_fubeiPollTimer) clearInterval(_fubeiPollTimer);
  var attempts = 0, maxAttempts = 90;
  _fubeiPollTimer = setInterval(function() {
    attempts++;
    if (attempts > maxAttempts) { clearInterval(_fubeiPollTimer); _fubeiPollTimer = null; var el = document.getElementById('fubeiPollStatus'); if (el) el.textContent = '查询超时，请刷新页面查看是否到账'; return; }
    fetch(API_BASE + '/api/recharge/fubei-query?out_trade_no=' + encodeURIComponent(outTradeNo), { headers: authHeaders() })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (d && d.status === 'paid') {
          clearInterval(_fubeiPollTimer); _fubeiPollTimer = null;
          var el = document.getElementById('fubeiPollStatus');
          if (el) { el.style.color = '#27ae60'; el.textContent = '支付成功！已到账 ' + (d.credits || '') + ' 积分'; }
          if (typeof loadSutuiBalance === 'function') loadSutuiBalance();
          var balanceEl = document.getElementById('billingBalance');
          if (balanceEl) fetch(API_BASE + '/auth/me', { headers: authHeaders() }).then(function(r) { return r.json(); }).then(function(me) { balanceEl.textContent = '我的积分：' + (me && me.credits != null ? me.credits : '--'); });
        }
      }).catch(function() {});
  }, 3000);
}

/** 充值/套餐：单价（元），支持 price_yuan 或 price_fen */
function billingPackageYuan(p) {
  if (!p) return 0;
  if (p.price_yuan != null && p.price_yuan !== '') return Number(p.price_yuan);
  if (p.price_fen != null && p.price_fen !== '') return Number(p.price_fen) / 100;
  return 0;
}
/** 每元人民币可得积分（展示） */
function billingCreditsPerYuan(p) {
  var yuan = billingPackageYuan(p);
  var c = Number(p.credits || 0);
  if (!yuan || !c) return null;
  return Math.round((c / yuan) * 100) / 100;
}
function billingRatioHintLinesHtml(packages) {
  if (!packages || !packages.length) return '';
  var parts = [];
  packages.forEach(function(p) {
    var per = billingCreditsPerYuan(p);
    if (per == null) return;
    parts.push(billingPackageYuan(p) + ' 元档约 ' + per + ' 积分/元');
  });
  if (!parts.length) return '';
  return '<p style="margin:0.45rem 0 0 0;font-size:0.82rem;color:var(--text-muted);">折算参考：' + parts.join('；') + '。支付成功后以实际到账积分为准。</p>';
}
function billingRatioHintPlainText(packages) {
  if (!packages || !packages.length) return '';
  var parts = [];
  packages.forEach(function(p) {
    var per = billingCreditsPerYuan(p);
    if (per == null) return;
    parts.push(billingPackageYuan(p) + ' 元档约 ' + per + ' 积分/元');
  });
  if (!parts.length) return '';
  return '折算参考：' + parts.join('；') + '。支付成功后以实际到账积分为准。';
}

function loadLoginCaptcha() {
  var img = document.getElementById('loginCaptchaImg');
  var msgEl = document.getElementById('loginMsg');
  fetch(API_BASE + '/auth/captcha').then(function(r) {
    return r.json().then(function(d) { return { ok: r.ok, data: d }; });
  }).then(function(x) {
    var d = x.data || {};
    if (!x.ok || !d.captcha_id || !d.image) {
      if (img) { img.alt = '验证码加载失败'; img.style.background = 'rgba(244,67,54,0.2)'; img.removeAttribute('src'); }
      if (msgEl) showMsg(msgEl, '验证码加载失败：无法从认证服务获取（' + (API_BASE || '') + '）。请确认云端 API 已启动并检查网络后刷新。', true);
      return;
    }
    if (img) { img.style.background = '#f5f5f5'; img.alt = '验证码'; img.src = d.image; }
    var hid = document.getElementById('loginCaptchaId');
    var ans = document.getElementById('loginCaptchaAnswer');
    if (hid) hid.value = d.captcha_id;
    if (ans) ans.value = '';
  }).catch(function(e) {
    if (img) { img.alt = '验证码加载失败'; img.style.background = 'rgba(244,67,54,0.2)'; img.removeAttribute('src'); }
    if (msgEl) showMsg(msgEl, '验证码请求失败（网络或认证服务不可用）。认证服务：' + (API_BASE || '') + '。请打开开发者工具 Network 查看。', true);
    console.warn('[login captcha]', e);
  });
}
function loadRegisterCaptcha() {
  var img = document.getElementById('registerCaptchaImg');
  var msgEl = document.getElementById('registerMsg');
  fetch(API_BASE + '/auth/captcha').then(function(r) {
    return r.json().then(function(d) { return { ok: r.ok, data: d }; });
  }).then(function(x) {
    var d = x.data || {};
    if (!x.ok || !d.captcha_id || !d.image) {
      if (img) { img.alt = '验证码加载失败'; img.style.background = 'rgba(244,67,54,0.2)'; img.removeAttribute('src'); }
      if (msgEl) showMsg(msgEl, '验证码加载失败：无法从认证服务获取（' + (API_BASE || '') + '）。请检查网络或 API 地址。', true);
      return;
    }
    if (img) { img.style.background = '#f5f5f5'; img.alt = '验证码'; img.src = d.image; }
    var hid = document.getElementById('registerCaptchaId');
    var ans = document.getElementById('registerCaptchaAnswer');
    if (hid) hid.value = d.captcha_id;
    if (ans) ans.value = '';
  }).catch(function(e) {
    if (img) { img.alt = '验证码加载失败'; img.style.background = 'rgba(244,67,54,0.2)'; img.removeAttribute('src'); }
    if (msgEl) showMsg(msgEl, '验证码请求失败（网络或认证服务不可用）。认证服务：' + (API_BASE || '') + '。', true);
    console.warn('[register captcha]', e);
  });
}

function applyEditionLoginUI() {
  var loginView = document.getElementById('authLoginView');
  var registerView = document.getElementById('authRegisterView');
  var tabLogin = document.getElementById('authTabLogin');
  var tabRegister = document.getElementById('authTabRegister');
  var ownWechatBlock = document.getElementById('ownWechatLoginBlock');
  /** 仅 online 且关闭独立认证时走服务号扫码；其余为账号密码登录，且 login 打 API_BASE 时须验证码 */
  if (EDITION === 'online' && !USE_INDEPENDENT_AUTH) {
    if (loginView) loginView.style.display = 'none';
    if (registerView) registerView.style.display = 'none';
    if (tabLogin) tabLogin.style.display = 'none';
    if (tabRegister) tabRegister.style.display = 'none';
    if (ownWechatBlock) {
      ownWechatBlock.style.display = 'block';
      startOwnWechatLogin();
    }
  } else {
    if (loginView) loginView.style.display = ''; if (registerView) registerView.style.display = 'none'; loadLoginCaptcha();
    if (ownWechatBlock) ownWechatBlock.style.display = 'none';
    if (tabLogin) tabLogin.style.display = ''; if (tabRegister) tabRegister.style.display = '';
    var loginImg = document.getElementById('loginCaptchaImg');
    var loginRefresh = document.getElementById('loginCaptchaRefresh');
    if (loginImg) loginImg.onclick = function() { loadLoginCaptcha(); };
    if (loginRefresh) loginRefresh.onclick = function(e) { e.preventDefault(); loadLoginCaptcha(); };
    var regImg = document.getElementById('registerCaptchaImg');
    var regRefresh = document.getElementById('registerCaptchaRefresh');
    if (regImg) regImg.onclick = function() { loadRegisterCaptcha(); };
    if (regRefresh) regRefresh.onclick = function(e) { e.preventDefault(); loadRegisterCaptcha(); };
    if (tabLogin) tabLogin.onclick = function() { if (loginView) loginView.style.display = ''; if (registerView) registerView.style.display = 'none'; loadLoginCaptcha(); if (tabLogin) { tabLogin.style.borderBottomColor = 'var(--accent)'; tabLogin.style.color = 'var(--accent)'; } if (tabRegister) { tabRegister.style.borderBottomColor = 'transparent'; tabRegister.style.color = 'var(--text-muted)'; } };
    if (tabRegister) tabRegister.onclick = function() { if (loginView) loginView.style.display = 'none'; if (registerView) registerView.style.display = ''; loadRegisterCaptcha(); if (tabRegister) { tabRegister.style.borderBottomColor = 'var(--accent)'; tabRegister.style.color = 'var(--accent)'; } if (tabLogin) { tabLogin.style.borderBottomColor = 'transparent'; tabLogin.style.color = 'var(--text-muted)'; } };
  }
}
(function fetchEdition() {
  function setClientVersionLabel(semver, build, appliedAt) {
    var el = document.getElementById('clientVersionLabel');
    if (!el) return;
    var v = (semver && String(semver).trim()) ? String(semver).trim().replace(/^v/i, '') : '';
    var b = (build === null || build === undefined) ? NaN : Number(build);
    var parts = [];
    if (v) parts.push('v' + v);
    if (!isNaN(b)) parts.push('build ' + b);
    if (appliedAt) parts.push(String(appliedAt));
    el.textContent = parts.length ? parts.join(' · ') : '';
  }
  function tryStaticClientVersionIfEmpty() {
    var el = document.getElementById('clientVersionLabel');
    if (!el || (el.textContent && String(el.textContent).trim())) return Promise.resolve();
    return fetch('/static/client_version.json').then(function(r) {
      return r.ok ? r.json() : null;
    }).then(function(j) {
      if (!j) return;
      if (j.build == null && !j.version) return;
      setClientVersionLabel(j.version || '1.0.0', j.build, j.applied_at || null);
    }).catch(function() {});
  }
  function applyEditionPayload(d) {
    d = d || {};
    EDITION = (d.edition) || 'online';
    USE_INDEPENDENT_AUTH = !!d.use_independent_auth;
    USE_FUBEI_PAY = !!(d.use_fubei_pay);
    ALLOW_SELF_CONFIG_MODEL = d.allow_self_config_model !== false;
    RECHARGE_URL = (d.recharge_url && d.recharge_url.trim()) ? d.recharge_url.trim() : null;
    if (typeof d.client_code_build === 'number' || (d.client_code_version && String(d.client_code_version).trim())) {
      setClientVersionLabel(
        d.client_code_version || '1.0.0',
        typeof d.client_code_build === 'number' ? d.client_code_build : null,
        d.client_code_applied_at || null
      );
    }
    applyEditionLoginUI();
    if (typeof updateSutuiSubSelectVisibility === 'function') updateSutuiSubSelectVisibility();
  }
  function fetchFrom(base) {
    var b = (base || '').replace(/\/$/, '');
    if (!b) return Promise.reject(new Error('no base'));
    return fetch(b + '/api/edition').then(function(r) {
      return r.ok ? r.json() : Promise.reject(new Error('bad status'));
    });
  }
  var localBase = (typeof LOCAL_API_BASE !== 'undefined' && LOCAL_API_BASE) ? String(LOCAL_API_BASE).trim() : '';
  function supplementFubeiPayFromApiBase() {
    if (typeof EDITION === 'undefined' || EDITION !== 'online' || !USE_INDEPENDENT_AUTH || USE_FUBEI_PAY) return Promise.resolve();
    var apiB = (typeof API_BASE !== 'undefined' && API_BASE) ? String(API_BASE).replace(/\/$/, '') : '';
    if (!apiB) return Promise.resolve();
    return fetchFrom(apiB).then(function(remote) {
      if (remote && remote.use_fubei_pay) USE_FUBEI_PAY = true;
    }).catch(function() {});
  }
  function chainAfterEdition(p) {
    return p.then(supplementFubeiPayFromApiBase).then(tryStaticClientVersionIfEmpty);
  }
  if (localBase) {
    chainAfterEdition(fetchFrom(localBase).then(applyEditionPayload)).catch(function() {
      chainAfterEdition(fetchFrom(API_BASE).then(applyEditionPayload)).catch(function() {
        applyEditionLoginUI();
        return tryStaticClientVersionIfEmpty();
      });
    });
  } else {
    chainAfterEdition(fetchFrom(API_BASE).then(applyEditionPayload)).catch(function() {
      applyEditionLoginUI();
      return tryStaticClientVersionIfEmpty();
    });
  }
})();

function startOwnWechatLogin() {
  var img = document.getElementById('ownWechatQrImg');
  var status = document.getElementById('ownWechatQrStatus');
  var link = document.getElementById('ownWechatLink');
  var qrWrap = document.getElementById('ownWechatQrWrap');
  var mpWrap = document.getElementById('ownWechatMiniprogramWrap');
  if (!status) return;
  status.textContent = '正在获取…';
  if (img) img.style.display = 'none';
  if (link) link.style.display = 'none';
  if (mpWrap) mpWrap.style.display = 'none';
  if (qrWrap) qrWrap.style.display = 'block';
  fetch(API_BASE + '/auth/wechat-login-url').then(function(r) {
    return r.json().then(function(d) {
      if (!r.ok) {
        var msg = (d && (d.detail || d.msg)) || ('请求失败 ' + r.status);
        status.textContent = (typeof msg === 'string' ? msg : (Array.isArray(msg) ? msg[0] : '获取失败'));
        return;
      }
      var url = (d && (d.login_url || (d.data && d.data.login_url))) || '';
      if (!url) {
        console.warn('[wechat-login-url] 200 但无 login_url，响应:', d);
        status.textContent = '服务器未返回登录链接';
        return;
      }
      var titleEl = document.getElementById('ownWechatLoginTitle');
      if (titleEl) titleEl.textContent = '请使用微信扫描下方二维码登录（服务号）';
      status.textContent = '请使用微信扫码登录';
      if (img) {
        img.src = 'https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=' + encodeURIComponent(url);
        img.style.display = 'inline';
      }
      if (link) {
        link.href = url;
        link.style.display = 'inline-block';
        link.textContent = '打开微信扫码登录';
      }
    });
  }).catch(function(e) {
    status.textContent = '网络错误或接口异常，请检查 API 地址（' + (API_BASE || '') + '）或稍后重试';
    console.warn('[wechat-login-url]', e);
  });
}

/** OAuth / 登录后同步到本机后端：写入 openclaw/.channel_fallback.json，供本机 OpenClaw（127.0.0.1:8000）读。
 * 须打 LOCAL_API_BASE，不能打 API_BASE：本机页默认 API_BASE 为公网认证域名，写文件会落在远端，本机永远 401。 */
function persistOpenclawChannelFallback(tok) {
  var t = (tok != null && tok !== '') ? String(tok) : (typeof token !== 'undefined' ? token : '');
  var localBase = (typeof LOCAL_API_BASE !== 'undefined' && LOCAL_API_BASE) ? String(LOCAL_API_BASE).replace(/\/$/, '') : '';
  if (!t || !localBase) {
    if (typeof console !== 'undefined' && console.debug) {
      console.debug('[openclaw-channel-fallback] 跳过：无 token 或未配置 LOCAL_API_BASE（本机须运行 backend 且 localhost 打开页面或设 lobster_local_api_base）');
    }
    return;
  }
  fetch(localBase + '/auth/persist-openclaw-channel-fallback', {
    method: 'POST',
    headers: {
      'Authorization': 'Bearer ' + t,
      'X-Installation-Id': typeof getOrCreateInstallationId === 'function' ? getOrCreateInstallationId() : ''
    }
  }).catch(function() {});
}

(function applyTokenFromUrl() {
  var m = /[?&]token=([^&]+)/.exec(window.location.search || '');
  if (!m || !m[1]) return;
  var t = decodeURIComponent(m[1]);
  token = t;
  localStorage.setItem('token', t);
  persistOpenclawChannelFallback(t);
  if (window.opener) {
    try { window.opener.postMessage({ type: 'auth_login_ok', token: t }, '*'); } catch (e) {}
    window.close();
  } else {
    setTimeout(function() { loadDashboard(); }, 0);
  }
})();

window.addEventListener('message', function(e) {
  if (e.data && e.data.type === 'auth_login_ok' && e.data.token) {
    token = e.data.token;
    localStorage.setItem('token', token);
    persistOpenclawChannelFallback(token);
    loadDashboard();
  }
});

document.getElementById('loginForm').addEventListener('submit', function(e) {
  e.preventDefault();
  var fd = new FormData(this);
  var body = new URLSearchParams({
    username: fd.get('username'),
    password: fd.get('password'),
    captcha_id: fd.get('captcha_id') || '',
    captcha_answer: fd.get('captcha_answer') || ''
  });
  var msgEl = document.getElementById('loginMsg');
  fetch(API_BASE + '/auth/login', {
    method: 'POST',
    body: body,
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      'X-Installation-Id': typeof getOrCreateInstallationId === 'function' ? getOrCreateInstallationId() : ''
    }
  })
    .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
    .then(function(x) {
      if (x.ok) {
        token = x.data.access_token;
        localStorage.setItem('token', token);
        if (typeof persistOpenclawChannelFallback === 'function') persistOpenclawChannelFallback(token);
        showMsg(msgEl, '登录成功', false);
        loadDashboard();
      } else {
        showMsg(msgEl, normalizeAuthErrorDetail(x.data.detail) || '登录失败', true);
        if (typeof loadLoginCaptcha === 'function') loadLoginCaptcha();
      }
    })
    .catch(function() { showMsg(msgEl, '网络错误', true); if (typeof loadLoginCaptcha === 'function') loadLoginCaptcha(); });
});
function validateCnPhone(raw) {
  var d = String(raw || '').replace(/\D/g, '');
  if (!d) return null;
  return /^1[3-9]\d{9}$/.test(d) ? d : null;
}

var _registerSmsCooldownTimer = null;
function setRegisterSmsButtonCooldown(sec) {
  var btn = document.getElementById('registerSendSmsBtn');
  if (!btn) return;
  if (_registerSmsCooldownTimer) {
    clearInterval(_registerSmsCooldownTimer);
    _registerSmsCooldownTimer = null;
  }
  if (sec <= 0) {
    btn.disabled = false;
    btn.textContent = '获取短信验证码';
    return;
  }
  var left = sec;
  btn.disabled = true;
  function tick() {
    btn.textContent = left > 0 ? ('已发送（' + left + 's）') : '获取短信验证码';
    if (left <= 0) {
      btn.disabled = false;
      if (_registerSmsCooldownTimer) clearInterval(_registerSmsCooldownTimer);
      _registerSmsCooldownTimer = null;
      return;
    }
    left -= 1;
  }
  tick();
  _registerSmsCooldownTimer = setInterval(tick, 1000);
}
function normalizeAuthErrorDetail(detail) {
  return detail;
}
(function bindRegisterSmsButton() {
  var btn = document.getElementById('registerSendSmsBtn');
  if (!btn || btn._smsBound) return;
  btn._smsBound = true;
  btn.addEventListener('click', function() {
    var msgEl = document.getElementById('registerMsg');
    var phone = validateCnPhone((document.getElementById('registerPhone') || {}).value);
    var captchaId = (document.getElementById('registerCaptchaId') || {}).value || '';
    var captchaAnswer = (document.getElementById('registerCaptchaAnswer') || {}).value || '';
    if (!phone) { showMsg(msgEl, '请输入有效的 11 位手机号', true); return; }
    if (!captchaAnswer) { showMsg(msgEl, '请先填写图形验证码', true); return; }
    btn.disabled = true;
    fetch(API_BASE + '/auth/sms/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: phone, captcha_id: captchaId, captcha_answer: captchaAnswer })
    })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d, status: r.status }; }); })
      .then(function(x) {
        if (x.ok) {
          showMsg(msgEl, '短信已发送，请查收', false);
          setRegisterSmsButtonCooldown(60);
          if (typeof loadRegisterCaptcha === 'function') loadRegisterCaptcha();
        } else {
          var det = normalizeAuthErrorDetail(x.data.detail);
          showMsg(msgEl, det || ('发送失败 (' + x.status + ')'), true);
          btn.disabled = false;
          if (typeof loadRegisterCaptcha === 'function') loadRegisterCaptcha();
        }
      })
      .catch(function() {
        showMsg(msgEl, '网络错误', true);
        btn.disabled = false;
      });
  });
})();

var registerForm = document.getElementById('registerForm');
if (registerForm) {
  registerForm.addEventListener('submit', function(e) {
    e.preventDefault();
    var phone = validateCnPhone((document.getElementById('registerPhone') || {}).value);
    var smsCode = ((document.getElementById('registerSmsCode') || {}).value || '').trim();
    var password = (document.getElementById('registerPassword') || {}).value || '';
    var msgEl = document.getElementById('registerMsg');
    if (!phone) { showMsg(msgEl, '请输入有效的 11 位手机号', true); return; }
    if (!smsCode) { showMsg(msgEl, '请填写短信验证码', true); return; }
    if (password.length < 6) { showMsg(msgEl, '密码至少 6 位', true); return; }

    function postRegisterPhone(brandMark) {
      var payload = { phone: phone, code: smsCode, password: password };
      if (brandMark) payload.brand_mark = brandMark;
      fetch(API_BASE + '/auth/register-phone', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Installation-Id': typeof getOrCreateInstallationId === 'function' ? getOrCreateInstallationId() : ''
        },
        body: JSON.stringify(payload)
      })
        .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
        .then(function(x) {
          if (x.ok) {
            token = x.data.access_token;
            localStorage.setItem('token', token);
            if (typeof persistOpenclawChannelFallback === 'function') persistOpenclawChannelFallback(token);
            showMsg(msgEl, '注册成功', false);
            setRegisterSmsButtonCooldown(0);
            loadDashboard();
          } else {
            var detail = normalizeAuthErrorDetail(x.data.detail);
            showMsg(msgEl, detail || '注册失败', true);
          }
        })
        .catch(function() { showMsg(msgEl, '网络错误', true); });
    }

    var bm = (typeof window.__LOBSTER_BRAND_MARK !== 'undefined' && window.__LOBSTER_BRAND_MARK) ? String(window.__LOBSTER_BRAND_MARK) : '';
    if (bm) {
      postRegisterPhone(bm);
      return;
    }
    var localBase = (typeof LOCAL_API_BASE !== 'undefined' && LOCAL_API_BASE) ? String(LOCAL_API_BASE).replace(/\/$/, '') : '';
    if (!localBase) {
      postRegisterPhone('');
      return;
    }
    fetch(localBase + '/api/branding', { credentials: 'same-origin' })
      .then(function(r) { return r.ok ? r.json() : {}; })
      .then(function(b) { postRegisterPhone((b && b.mark) ? String(b.mark) : ''); })
      .catch(function() { postRegisterPhone(''); });
  });
}

/** 在线版：本机未配 TOS 时从认证中心拉取服务器 TOS_CONFIG 写入 custom_configs.json */
function syncTosFromServerIfOnline() {
  if (typeof EDITION === 'undefined' || EDITION !== 'online' || !token) return;
  var localBase = (typeof LOCAL_API_BASE !== 'undefined' && LOCAL_API_BASE) ? String(LOCAL_API_BASE).replace(/\/$/, '') : '';
  if (!localBase) return;
  fetch(localBase + '/api/settings/sync-tos-from-server', { method: 'POST', headers: typeof authHeaders === 'function' ? authHeaders() : { 'Authorization': 'Bearer ' + token } })
    .catch(function() {});
}

function loadDashboard() {
  if (!token) {
    if (typeof window.resetChatSessionsForLogout === 'function') window.resetChatSessionsForLogout();
    document.getElementById('authPanel').style.display = 'block';
    document.getElementById('dashboard').classList.remove('visible');
    document.getElementById('headerActions').style.display = 'none';
    var heroEl = document.getElementById('pageHero');
    if (heroEl) heroEl.style.display = '';
    return;
  }
  fetch(API_BASE + '/auth/me', { headers: typeof authHeaders === 'function' ? authHeaders() : { 'Authorization': 'Bearer ' + token } })
    .then(function(r) {
      if (r.status === 401) { token = null; localStorage.removeItem('token'); loadDashboard(); return null; }
      return r.json();
    })
    .then(function(d) {
      if (!d) return;
      if (d.id == null) {
        token = null;
        localStorage.removeItem('token');
        loadDashboard();
        return;
      }
      if (typeof persistOpenclawChannelFallback === 'function') persistOpenclawChannelFallback(token);
      window.__currentUserId = d.id;
      if (typeof window.resetChatSessionsMemory === 'function') window.resetChatSessionsMemory();
      document.getElementById('userEmail').textContent = d.email;
      document.getElementById('headerUserEmail').textContent = (d.email || '').split('@')[0];
      document.getElementById('headerActions').style.display = 'flex';
      document.getElementById('authPanel').style.display = 'none';
      document.getElementById('dashboard').classList.add('visible');
      var heroEl = document.getElementById('pageHero');
      if (heroEl) heroEl.style.display = 'none';
      loadModelSelector(d.preferred_model);
      initChatSessions();
      syncTosFromServerIfOnline();
      if (EDITION === 'online') {
        loadSutuiBalance();
        var rBtn = document.getElementById('sutuiRechargeBtn');
        if (USE_INDEPENDENT_AUTH && rBtn) {
          rBtn.onclick = function(e) { e.preventDefault(); document.querySelector('.nav-left-item[data-view="billing"]') && document.querySelector('.nav-left-item[data-view="billing"]').click(); };
        }
      } else {
        var w = document.getElementById('sutuiBalanceWrap');
        if (w) w.style.display = 'none';
      }
      if (typeof window._applyWecomConfigHash === 'function' && location.hash && location.hash.indexOf('wecom') !== -1) window._applyWecomConfigHash();
    });
}

var _modelSelectorBound = false;

function updateSutuiSubSelectVisibility() {
  var sub = document.getElementById('sutuiModelSelect');
  var lab = document.getElementById('sutuiModelLabel');
  if (typeof EDITION !== 'undefined' && EDITION === 'online') {
    if (sub) sub.style.display = 'none';
    if (lab) lab.style.display = 'none';
    return;
  }
  var main = document.getElementById('modelSelect');
  if (!main || !sub) return;
  var on = (main.value === 'sutui_aggregate' && typeof EDITION !== 'undefined' && EDITION === 'online');
  sub.style.display = on ? '' : 'none';
  if (lab) lab.style.display = on ? '' : 'none';
  if (on) loadSutuiSubModels();
}

function loadSutuiSubModels() {
  var sub = document.getElementById('sutuiModelSelect');
  if (!sub) return;
  var b = (typeof API_BASE !== 'undefined' && API_BASE) ? String(API_BASE).replace(/\/$/, '') : '';
  if (!b || !token) return;
  fetch(b + '/api/sutui-llm/models', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var list = (d && Array.isArray(d.models)) ? d.models : [];
      var cur = sub.value;
      sub.innerHTML = '';
      function appendOptions(showUnavailable) {
        list.forEach(function(m) {
          if (!m || !m.id) return;
          if (!showUnavailable && m.available === false) return;
          var opt = document.createElement('option');
          opt.value = m.id;
          opt.textContent = m.name || m.id;
          sub.appendChild(opt);
        });
      }
      appendOptions(false);
      if (!sub.options.length && list.length) appendOptions(true);
      var rec = d && d.recommended;
      if (rec && sub.options.length && Array.prototype.some.call(sub.options, function(o) { return o.value === rec; })) {
        sub.value = rec;
      } else if (cur && Array.prototype.some.call(sub.options, function(o) { return o.value === cur; })) {
        sub.value = cur;
      } else {
        var last = '';
        try {
          last = (localStorage.getItem('lobster_last_sutui_submodel') || '').trim();
        } catch (e0) {}
        if (last && Array.prototype.some.call(sub.options, function(o) { return o.value === last; })) {
          sub.value = last;
        }
      }
      if (sub.value) {
        try {
          localStorage.setItem('lobster_last_sutui_submodel', sub.value);
        } catch (e1) {}
      }
      sub.onchange = function() {
        try {
          if (sub.value) localStorage.setItem('lobster_last_sutui_submodel', sub.value);
        } catch (e2) {}
      };
      // 勿在此调用 maybeAutoResume：每次切回「对话」会 refreshModelSelector→loadSutuiSubModels，
      // 会误把会话拉回「恢复进度」或擅自切换到带 poll_resume 的其它会话；续查仅在 initChatSessions 触发。
    })
    .catch(function() {});
}

function loadModelSelector(preferredModel) {
  var sel = document.getElementById('modelSelect');
  if (!sel) return;
  var row = sel.closest('.model-selector');
  if (typeof EDITION !== 'undefined' && EDITION === 'online') {
    if (row) row.style.display = 'none';
    sel.innerHTML = '<option value="sutui_aggregate">速推聚合</option>';
    sel.value = 'sutui_aggregate';
    try {
      localStorage.setItem('lobster_last_sutui_submodel', 'deepseek-chat');
    } catch (eOnlineModel) {}
    return;
  }
  if (row) row.style.display = '';
  if (preferredModel) sel.setAttribute('data-preferred', preferredModel);
  var pref = preferredModel || sel.getAttribute('data-preferred') || '';
  if (pref === 'sutui') pref = 'sutui_aggregate';
  if (typeof EDITION !== 'undefined' && EDITION === 'online' && !pref) {
    pref = 'sutui_aggregate';
  }
  var cloudBase = (typeof API_BASE !== 'undefined' && API_BASE) ? String(API_BASE).replace(/\/$/, '') : '';
  var localBase = (typeof LOCAL_API_BASE !== 'undefined' && LOCAL_API_BASE) ? String(LOCAL_API_BASE).replace(/\/$/, '') : '';
  var cloudP = (cloudBase && token)
    ? fetch(cloudBase + '/api/settings/models', { headers: authHeaders() }).then(function(r) { return r.json(); }).catch(function() { return { models: [] }; })
    : Promise.resolve({ models: [] });
  var localP = localBase
    ? fetch(localBase + '/api/settings/models', { headers: token ? authHeaders() : {} }).then(function(r) { return r.json(); }).catch(function() { return { models: [] }; })
    : Promise.resolve({ models: [] });
  Promise.all([cloudP, localP]).then(function(arr) {
    var cloud = arr[0] || {};
    var local = arr[1] || {};
    var merged = [];
    var seen = {};
    (cloud.models || []).forEach(function(m) {
      if (!m || !m.id || seen[m.id]) return;
      seen[m.id] = true;
      merged.push(m);
    });
    (local.models || []).forEach(function(m) {
      if (!m || !m.id || seen[m.id]) return;
      if (m.id === 'sutui' || m.id === 'sutui_aggregate') return;
      seen[m.id] = true;
      merged.push(m);
    });
    merged.sort(function(a, b) {
      if (a.id === 'sutui_aggregate') return -1;
      if (b.id === 'sutui_aggregate') return 1;
      return 0;
    });
    if (!merged.length) {
      updateSutuiSubSelectVisibility();
      return;
    }
    var curVal = sel.value;
    sel.innerHTML = merged.map(function(m) {
      var selected = (m.id === pref || m.id === curVal) ? ' selected' : '';
      var label = m.custom ? m.name + ' (自定义)' : m.name;
      return '<option value="' + escapeAttr(m.id) + '"' + selected + '>' + escapeHtml(label) + '</option>';
    }).join('');
    updateSutuiSubSelectVisibility();
  });
  if (!_modelSelectorBound) {
    _modelSelectorBound = true;
    sel.addEventListener('change', function() {
      if (typeof API_BASE !== 'undefined' && API_BASE) {
        fetch(API_BASE + '/api/settings', {
          method: 'POST', headers: authHeaders(),
          body: JSON.stringify({ preferred_model: sel.value })
        }).catch(function() {});
      }
      updateSutuiSubSelectVisibility();
    });
  }
}

function refreshModelSelector() {
  loadModelSelector();
}

(function initChatTipsModal() {
  var modal = document.getElementById('chatTipsModal');
  var openBtn = document.getElementById('chatTipsBtn');
  var closeBtn = document.getElementById('chatTipsModalClose');
  function closeModal() {
    if (modal) modal.classList.remove('visible');
  }
  function openModal() {
    if (modal) modal.classList.add('visible');
  }
  if (openBtn) openBtn.addEventListener('click', openModal);
  if (closeBtn) closeBtn.addEventListener('click', closeModal);
  if (modal) {
    modal.addEventListener('click', function(e) {
      if (e.target === modal) closeModal();
    });
  }
})();

document.getElementById('logout').addEventListener('click', function() {
  token = null;
  localStorage.removeItem('token');
  if (typeof window.resetChatSessionsForLogout === 'function') window.resetChatSessionsForLogout();
  document.getElementById('dashboard').classList.remove('visible');
  document.getElementById('authPanel').style.display = 'block';
  document.getElementById('headerActions').style.display = 'none';
  var heroEl = document.getElementById('pageHero');
  if (heroEl) heroEl.style.display = '';
});

(function initDropdown() {
  var dropdown = document.getElementById('headerUserDropdown');
  var btn = document.getElementById('headerDropdownBtn');
  if (dropdown && btn) {
    btn.addEventListener('click', function(e) { e.stopPropagation(); dropdown.classList.toggle('open'); });
    document.addEventListener('click', function() { dropdown.classList.remove('open'); });
  }
})();

document.querySelectorAll('.nav-left-item').forEach(function(el) {
  el.addEventListener('click', function() {
    if (typeof window.closeAllPublishModals === 'function') window.closeAllPublishModals();
    var chatTips = document.getElementById('chatTipsModal');
    if (chatTips) chatTips.classList.remove('visible');
    var view = el.dataset.view;
    if (!view) return;
    if (currentView === 'chat' && view !== 'chat' && typeof saveCurrentSessionToStore === 'function') saveCurrentSessionToStore();
    document.querySelectorAll('.nav-left-item').forEach(function(b) { b.classList.remove('active'); });
    var navEl = document.querySelector('.nav-left-item[data-view="' + view + '"]');
    if (navEl) navEl.classList.add('active'); else el.classList.add('active');
    document.querySelectorAll('.content-block').forEach(function(p) { p.classList.remove('visible'); });
    var contentId = 'content-' + view;
    var contentEl = document.getElementById(contentId);
    if (contentEl) contentEl.classList.add('visible');
    currentView = view;
    if (view === 'chat') refreshModelSelector();
    if (view === 'skill-store') { loadSkillStore(); if (typeof initOnlineSkillStore === 'function') initOnlineSkillStore(); }
    if (view === 'publish') { if (typeof initPublishView === 'function') initPublishView(); }
    if (view === 'production') { if (typeof initProductionView === 'function') initProductionView(); }
    if (view === 'billing') { if (typeof loadBillingView === 'function') loadBillingView(); }
    if (view === 'sys-config') { loadOpenClawConfig(); }
    if (view === 'logs') { if (typeof ensureLogsBindings === 'function') ensureLogsBindings(); }
    if (view === 'messenger-config' && typeof loadMessengerConfigPage === 'function') loadMessengerConfigPage();
    if (view === 'youtube-accounts' && typeof loadYoutubeAccountsPage === 'function') loadYoutubeAccountsPage();
    if (view === 'meta-social' && typeof loadMetaSocialPage === 'function') loadMetaSocialPage();
  });
});

window.addEventListener('beforeunload', function() {
  if (typeof saveCurrentSessionToStore === 'function') saveCurrentSessionToStore();
});

function loadSutuiBalance() {
  var wrap = document.getElementById('sutuiBalanceWrap');
  var textEl = document.getElementById('sutuiBalanceText');
  var rechargeBtn = document.getElementById('sutuiRechargeBtn');
  if (!wrap || !textEl) return;
  wrap.style.display = 'flex';
  if (USE_INDEPENDENT_AUTH) {
    textEl.textContent = '积分：加载中…';
    if (rechargeBtn) { rechargeBtn.style.display = ''; rechargeBtn.href = '#'; rechargeBtn.textContent = '充值'; }
    fetch(API_BASE + '/auth/me', { headers: authHeaders() })
      .then(function(r) { return r.json(); })
      .then(function(d) { textEl.textContent = '积分：' + (d && d.credits != null ? d.credits : '--'); })
      .catch(function() { textEl.textContent = '积分：--'; });
    return;
  }
  textEl.textContent = '余额：加载中…';
  if (rechargeBtn) {
    rechargeBtn.style.display = RECHARGE_URL ? '' : 'none';
    rechargeBtn.href = RECHARGE_URL || '#';
    rechargeBtn.textContent = '充值';
  }
  fetch(API_BASE + '/api/sutui/balance', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.error) {
        textEl.textContent = '余额：--';
        return;
      }
      var yuan = (d.balance_yuan != null) ? String(d.balance_yuan) : (d.balance != null ? (d.balance / 1000).toFixed(2) : '--');
      textEl.textContent = '余额：' + yuan + ' 元';
    })
    .catch(function() { textEl.textContent = '余额：--'; });
}

function loadBillingView() {
  var balanceEl = document.getElementById('billingBalance');
  var rechargeListEl = document.getElementById('billingRechargeList');
  var creditHistoryEl = document.getElementById('billingCreditHistory');
  var rechargePagerEl = document.getElementById('billingRechargePager');
  var creditPagerEl = document.getElementById('billingCreditPager');
  var refreshBtn = document.getElementById('billingRefreshBtn');
  var pricingBlock = document.getElementById('billingPricingBlock');
  var pricingContent = document.getElementById('billingPricingContent');
  if (!rechargeListEl || !creditHistoryEl) return;
  var billingRechargePage = 1;
  var billingConsumptionPage = 1;
  /** 将服务端 UTC/无时区 ISO 格式化为北京时间展示（与 API 返回的 *_beijing 一致） */
  function formatIsoToBeijingDisplay(isoStr) {
    if (!isoStr) return '';
    try {
      var s = String(isoStr).trim();
      if (s.indexOf(' ') > 0 && s.indexOf('T') < 0) s = s.replace(' ', 'T');
      if (!/[zZ]$/.test(s) && !/[+-]\d{2}:?\d{2}$/.test(s)) s += 'Z';
      var d = new Date(s);
      if (isNaN(d.getTime())) return s.slice(0, 19).replace('T', ' ');
      return d.toLocaleString('zh-CN', {
        timeZone: 'Asia/Shanghai',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false
      });
    } catch (e) {
      return String(isoStr).slice(0, 19).replace('T', ' ');
    }
  }
  var PAGE_SIZE = 10;
  var base = (typeof API_BASE !== 'undefined' ? API_BASE : '').replace(/\/$/, '');
  if (!base) base = (typeof LOCAL_API_BASE !== 'undefined' ? LOCAL_API_BASE : '') || '';
  function parseListResp(d) {
    if (Array.isArray(d)) return { items: d, total: d.length };
    return { items: (d && d.items) ? d.items : [], total: (d && d.total != null) ? d.total : 0 };
  }
  function loadRechargePage(page) {
    billingRechargePage = Math.max(1, page);
    rechargeListEl.innerHTML = '<p class="meta" style="padding:1rem;">加载中…</p>';
    rechargePagerEl.innerHTML = '';
    var offset = (billingRechargePage - 1) * PAGE_SIZE;
    fetch(base + '/api/recharge/my-orders?limit=' + PAGE_SIZE + '&offset=' + offset, { headers: authHeaders() })
      .then(function(r) { return r.ok ? r.json() : { items: [], total: 0 }; })
      .then(function(d) {
        var data = parseListResp(d);
        var orders = data.items || [];
        var total = data.total || 0;
        if (orders.length === 0) {
          rechargeListEl.innerHTML = '<p class="meta" style="padding:1rem;">暂无充值记录。</p>';
        } else {
          var rh = '<table style="width:100%;border-collapse:collapse;font-size:0.82rem;"><thead><tr style="border-bottom:1px solid var(--border);"><th style="text-align:left;padding:0.5rem;">时间</th><th style="text-align:left;padding:0.5rem;">订单号</th><th style="text-align:right;padding:0.5rem;">金额</th><th style="text-align:right;padding:0.5rem;">积分</th><th style="text-align:left;padding:0.5rem;">状态</th></tr></thead><tbody>';
          orders.forEach(function(o) {
            var amt = (o.amount_fen && o.amount_fen > 0) ? (o.amount_fen / 100).toFixed(2) + ' 元' : (o.amount_yuan != null ? o.amount_yuan + ' 元' : '-');
            var time = (o.paid_at_beijing || o.created_at_beijing || '').trim() ||
              formatIsoToBeijingDisplay(o.paid_at || o.created_at || '');
            var st = o.status === 'paid' ? '已支付' : (o.status === 'cancelled' ? '已取消' : '待支付');
            rh += '<tr style="border-bottom:1px solid rgba(255,255,255,0.06);"><td style="padding:0.5rem;">' + escapeHtml(time) + '</td><td style="padding:0.5rem;">' + escapeHtml(o.out_trade_no || '-') + '</td><td style="padding:0.5rem;text-align:right;">' + amt + '</td><td style="padding:0.5rem;text-align:right;">' + (o.credits != null ? o.credits : '-') + '</td><td style="padding:0.5rem;">' + escapeHtml(st) + '</td></tr>';
          });
          rh += '</tbody></table>';
          rechargeListEl.innerHTML = rh;
        }
        var totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
        rechargePagerEl.innerHTML = '<span class="meta">第 ' + billingRechargePage + ' / ' + totalPages + ' 页</span>' +
          '<button type="button" class="btn btn-ghost btn-sm" id="billingRechargePrev"' + (billingRechargePage <= 1 ? ' disabled' : '') + '>上一页</button>' +
          '<button type="button" class="btn btn-ghost btn-sm" id="billingRechargeNext"' + (billingRechargePage >= totalPages ? ' disabled' : '') + '>下一页</button>';
        var prevBtn = document.getElementById('billingRechargePrev');
        var nextBtn = document.getElementById('billingRechargeNext');
        if (prevBtn && billingRechargePage > 1) prevBtn.onclick = function() { loadRechargePage(billingRechargePage - 1); };
        if (nextBtn && billingRechargePage < totalPages) nextBtn.onclick = function() { loadRechargePage(billingRechargePage + 1); };
      })
      .catch(function() { rechargeListEl.innerHTML = '<p class="meta" style="padding:1rem;">加载失败。</p>'; });
  }
  function loadConsumptionPage(page) {
    billingConsumptionPage = Math.max(1, page);
    creditHistoryEl.innerHTML = '<p class="meta" style="padding:1rem;">加载中…</p>';
    creditPagerEl.innerHTML = '';
    var offset = (billingConsumptionPage - 1) * PAGE_SIZE;
    fetch(base + '/api/billing/credit-history?limit=' + PAGE_SIZE + '&offset=' + offset, { headers: authHeaders() })
      .then(function(r) {
        return r.json().then(function(d) { return { ok: r.ok, status: r.status, d: d }; }).catch(function() {
          return { ok: r.ok, status: r.status, d: {} };
        });
      })
      .then(function(pack) {
        if (!pack.ok) {
          var msg = '加载积分流水失败（HTTP ' + pack.status + '）';
          var det = (pack.d && (pack.d.detail || pack.d.message)) ? String(pack.d.detail || pack.d.message) : '';
          if (det) msg += '：' + det.slice(0, 400);
          else if (pack.status === 404) {
            msg += '：本地址无积分流水接口。若用本机 IP 打开页面，请升级 lobster_online 后端（含 credit-history 转发），或用 ?api= 指向认证中心域名后重新登录。';
          }
          creditHistoryEl.innerHTML = '<p class="meta err" style="padding:1rem;">' + escapeHtml(msg) + '</p>';
          creditPagerEl.innerHTML = '';
          return;
        }
        var d = pack.d;
        var data = parseListResp(d);
        var history = data.items || [];
        var total = data.total || 0;
        if (history.length === 0) {
          creditHistoryEl.innerHTML = '<p class="meta" style="padding:1rem;">暂无积分变动。</p>';
        } else {
          var html = '<table style="width:100%;border-collapse:collapse;font-size:0.82rem;"><thead><tr style="border-bottom:1px solid var(--border);"><th style="text-align:left;padding:0.5rem;">时间</th><th style="text-align:left;padding:0.5rem;">类型</th><th style="text-align:right;padding:0.5rem;">变动</th><th style="text-align:left;padding:0.5rem;">说明</th></tr></thead><tbody>';
          function billingConsumptionTypeLabel(et, hType) {
            if (hType === 'recharge') return '充值增加';
            var e = (et || '').trim().toLowerCase();
            if (e === 'sutui_chat') return 'LLM对话扣费';
            if (e === 'pre_deduct') return '能力预扣';
            if (e === 'settle') return '能力结算';
            if (e === 'refund') return '退款';
            if (e === 'unit_charge' || e === 'direct_charge') return '能力扣费';
            if (e === 'skill_unlock') return '技能解锁';
            return et || '扣减';
          }
          history.forEach(function(h) {
            var time = (h.time_beijing || '').trim() || formatIsoToBeijingDisplay(h.time || '');
            var et = (h.entry_type || '').trim();
            var typeText = billingConsumptionTypeLabel(et, h.type);
            var amount = h.amount != null ? Number(h.amount) : 0;
            var amountStr;
            if (amount >= 0) {
              amountStr = '+' + (Math.abs(amount) > 0 && Math.abs(amount) < 1 ? amount.toFixed(4) : String(amount));
            } else {
              amountStr = (Math.abs(amount) > 0 && Math.abs(amount) < 1 ? amount.toFixed(4) : String(amount));
            }
            var desc = (h.description || '-').trim() || '-';
            if (h.balance_after != null && h.balance_after !== undefined) {
              desc = desc + '（余额 ' + h.balance_after + '）';
            }
            html += '<tr style="border-bottom:1px solid rgba(255,255,255,0.06);"><td style="padding:0.5rem;">' + escapeHtml(time) + '</td><td style="padding:0.5rem;">' + escapeHtml(typeText) + '</td><td style="padding:0.5rem;text-align:right;">' + amountStr + '</td><td style="padding:0.5rem;">' + escapeHtml(desc) + '</td></tr>';
          });
          html += '</tbody></table>';
          creditHistoryEl.innerHTML = html;
        }
        var totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
        creditPagerEl.innerHTML = '<span class="meta">第 ' + billingConsumptionPage + ' / ' + totalPages + ' 页</span>' +
          '<button type="button" class="btn btn-ghost btn-sm" id="billingCreditPrev"' + (billingConsumptionPage <= 1 ? ' disabled' : '') + '>上一页</button>' +
          '<button type="button" class="btn btn-ghost btn-sm" id="billingCreditNext"' + (billingConsumptionPage >= totalPages ? ' disabled' : '') + '>下一页</button>';
        var prevBtn = document.getElementById('billingCreditPrev');
        var nextBtn = document.getElementById('billingCreditNext');
        if (prevBtn && billingConsumptionPage > 1) prevBtn.onclick = function() { loadConsumptionPage(billingConsumptionPage - 1); };
        if (nextBtn && billingConsumptionPage < totalPages) nextBtn.onclick = function() { loadConsumptionPage(billingConsumptionPage + 1); };
      })
      .catch(function() { creditHistoryEl.innerHTML = '<p class="meta err" style="padding:1rem;">网络错误，无法加载积分流水。</p>'; creditPagerEl.innerHTML = ''; });
  }
  loadRechargePage(1);
  loadConsumptionPage(1);
  var tabRecharge = document.querySelector('.store-tab[data-billing-tab="recharge"]');
  var tabConsumption = document.querySelector('.store-tab[data-billing-tab="consumption"]');
  var panelRecharge = document.getElementById('billingTabRecharge');
  var panelConsumption = document.getElementById('billingTabConsumption');
  function showBillingTab(tab) {
    if (tab === 'recharge') {
      if (panelRecharge) panelRecharge.style.display = '';
      if (panelConsumption) panelConsumption.style.display = 'none';
      if (tabRecharge) { tabRecharge.classList.add('active'); }
      if (tabConsumption) tabConsumption.classList.remove('active');
    } else {
      if (panelRecharge) panelRecharge.style.display = 'none';
      if (panelConsumption) panelConsumption.style.display = '';
      if (tabRecharge) tabRecharge.classList.remove('active');
      if (tabConsumption) tabConsumption.classList.add('active');
    }
  }
  if (tabRecharge) tabRecharge.onclick = function() { showBillingTab('recharge'); };
  if (tabConsumption) tabConsumption.onclick = function() { showBillingTab('consumption'); };
  if (pricingContent) {
    fetch(API_BASE + '/api/billing/pricing', { headers: authHeaders() })
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(d) {
        if (!pricingContent) return;
        if (!d) { pricingContent.innerHTML = '<span class="meta">收费说明加载失败</span>'; return; }
        var packages = d.credit_packages || [];
        var html = '';
        if (packages.length) {
          html += '<p style="margin:0 0 0.35rem 0;"><strong>算力套餐（积分）</strong>：</p><ul style="margin:0;padding-left:1.25rem;">';
          packages.forEach(function(p) {
            html += '<li>' + escapeHtml(p.label || (p.price_yuan + '元 - ' + p.credits + '积分')) + '</li>';
          });
          html += '</ul>';
        } else {
          html = '<p style="margin:0;"><strong>算力套餐</strong>：198元/20000积分、498元/50000积分、998元/120000积分。</p>';
        }
        pricingContent.innerHTML = html;
      })
      .catch(function() { if (pricingContent) pricingContent.innerHTML = '<span class="meta">收费说明加载失败</span>'; });
  }
  if (balanceEl) {
    if (typeof EDITION !== 'undefined' && EDITION !== 'online') {
      balanceEl.textContent = '单机版无速推余额，仅显示本机能力调用记录。';
    } else if (USE_INDEPENDENT_AUTH) {
      balanceEl.textContent = '我的积分：加载中…';
    } else {
      balanceEl.textContent = '速推余额：加载中…';
    }
  }
  function renderBalance(d) {
    if (!balanceEl || (typeof EDITION !== 'undefined' && EDITION !== 'online')) return;
    if (d && d.error) {
      balanceEl.textContent = '速推余额：' + (d.error || '--');
      return;
    }
    var yuan = (d && d.balance_yuan != null) ? String(d.balance_yuan) : (d && d.balance != null ? (d.balance / 1000).toFixed(2) : '--');
    balanceEl.textContent = '速推余额：' + yuan + ' 元' + (d && d.vip_level ? '（VIP' + d.vip_level + '）' : '');
  }
  if (USE_INDEPENDENT_AUTH && EDITION === 'online') {
    fetch(API_BASE + '/auth/me', { headers: authHeaders() })
      .then(function(r) { return r.json(); })
      .then(function(d) { if (balanceEl) balanceEl.textContent = '我的积分：' + (d && d.credits != null ? d.credits : '--'); })
      .catch(function() { if (balanceEl) balanceEl.textContent = '我的积分：--'; });
    var rechargeBlock = document.getElementById('rechargeBlock');
    if (rechargeBlock) {
      rechargeBlock.style.display = '';
      var rechargeTitle = rechargeBlock.querySelector('h4');
      if (rechargeTitle) rechargeTitle.textContent = '积分充值';
      fetch(API_BASE + '/api/recharge/packages', { headers: authHeaders() })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(opts) {
          var amountSel = document.getElementById('rechargeAmount');
          var hintEl = document.getElementById('rechargeRatioHint');
          if (amountSel && opts && Array.isArray(opts.packages) && opts.packages.length) {
            amountSel.innerHTML = opts.packages.map(function(p, i) {
              var py = billingPackageYuan(p);
              var lab = p.label || (py + '元 - ' + p.credits + '积分');
              return '<option value="' + i + '" data-credits="' + (p.credits || 0) + '">' + escapeHtml(lab) + '</option>';
            }).join('');
          }
          if (hintEl) {
            if (opts && Array.isArray(opts.packages) && opts.packages.length) {
              hintEl.textContent = billingRatioHintPlainText(opts.packages);
              hintEl.style.display = '';
            } else {
              hintEl.textContent = '';
              hintEl.style.display = 'none';
            }
          }
        })
        .catch(function() {
          var hintEl = document.getElementById('rechargeRatioHint');
          if (hintEl) {
            hintEl.textContent = '';
            hintEl.style.display = 'none';
          }
        });
    }
    var rechargeSubmitBtn = document.getElementById('rechargeSubmitBtn');
    var rechargeMsg = document.getElementById('rechargeMsg');
    var rechargeResult = document.getElementById('rechargeResult');
    if (rechargeSubmitBtn && !rechargeSubmitBtn._ownRechargeBound) {
      rechargeSubmitBtn._ownRechargeBound = true;
      rechargeSubmitBtn.addEventListener('click', function() {
        var amountEl = document.getElementById('rechargeAmount');
        var idx = amountEl ? parseInt(amountEl.value, 10) : -1;
        if (!amountEl || idx < 0) { showMsg(rechargeMsg, '请选择套餐', true); return; }
        if (rechargeResult) { rechargeResult.style.display = 'none'; rechargeResult.innerHTML = ''; }
        rechargeSubmitBtn.disabled = true;
        showMsg(rechargeMsg, '正在创建订单…', false);
        var apiUrl = USE_FUBEI_PAY ? (API_BASE + '/api/recharge/fubei-create') : (API_BASE + '/api/recharge/create');
        fetch(apiUrl, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ package_index: idx }) })
          .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
          .then(function(x) {
            if (!x.ok && x.data && x.data.detail) { showMsg(rechargeMsg, x.data.detail, true); return; }
            var d = x.data || {};
            showMsg(rechargeMsg, '', false);
            if (rechargeResult) {
              if (USE_FUBEI_PAY && d.qr_code) {
                var apiRoot = (typeof API_BASE !== 'undefined' && API_BASE) ? String(API_BASE).replace(/\/$/, '') : '';
                var qrSrc = apiRoot + '/api/recharge/qr-png?data=' + encodeURIComponent(d.qr_code);
                rechargeResult.innerHTML = '<p><strong>订单号：' + escapeHtml(d.out_trade_no || '') + '</strong></p>'
                  + '<p>请使用微信/支付宝扫描下方二维码完成支付：</p>'
                  + '<img src="' + escapeAttr(qrSrc) + '" alt="支付二维码" style="max-width:220px;height:auto;margin-top:0.5rem;">'
                  + '<p id="fubeiPollStatus" style="margin-top:0.5rem;color:#888;">等待支付…</p>';
                rechargeResult.style.display = 'block';
                _startFubeiPoll(d.out_trade_no);
              } else {
                rechargeResult.innerHTML = '<p><strong>订单号：' + escapeHtml(d.out_trade_no || '') + '</strong></p><p>' + escapeHtml(d.payment_info || '') + '</p>';
                rechargeResult.style.display = 'block';
              }
            }
          })
          .catch(function() { showMsg(rechargeMsg, '网络错误', true); })
          .finally(function() { rechargeSubmitBtn.disabled = false; });
      });
    }
  } else if (typeof EDITION !== 'undefined' && EDITION === 'online') {
    fetch(API_BASE + '/api/sutui/balance', { headers: authHeaders() })
      .then(function(r) { return r.json(); })
      .then(renderBalance)
      .catch(function() { if (balanceEl) balanceEl.textContent = '速推余额：--'; });
    var rechargeBlock = document.getElementById('rechargeBlock');
    if (rechargeBlock) {
      rechargeBlock.style.display = '';
      fetch(API_BASE + '/api/sutui/recharge-options', { headers: authHeaders() })
        .then(function(r) { return r.json(); })
        .then(function(opts) {
          var amountSel = document.getElementById('rechargeAmount');
          var typeSel = document.getElementById('rechargePaymentType');
          if (amountSel && Array.isArray(opts.shops) && opts.shops.length) {
            amountSel.innerHTML = opts.shops.map(function(s) {
              return '<option value="' + Number(s.shop_id) + '" data-yuan="' + Number(s.money_yuan) + '">' + escapeHtml(s.title) + (s.tag ? ' ' + escapeHtml(s.tag) : '') + '</option>';
            }).join('');
          } else if (amountSel && Array.isArray(opts.amounts)) {
            amountSel.innerHTML = opts.amounts.map(function(a) { return '<option value="0" data-yuan="' + Number(a) + '">' + Number(a) + ' 元</option>'; }).join('');
          }
          if (typeSel) typeSel.style.display = 'none';
        })
        .catch(function() {});
    }
    var rechargeSubmitBtn = document.getElementById('rechargeSubmitBtn');
    var rechargeMsg = document.getElementById('rechargeMsg');
    var rechargeResult = document.getElementById('rechargeResult');
    if (rechargeSubmitBtn && !rechargeSubmitBtn._rechargeBound) {
      rechargeSubmitBtn._rechargeBound = true;
      rechargeSubmitBtn.addEventListener('click', function() {
        var amountEl = document.getElementById('rechargeAmount');
        var shopId = amountEl ? parseInt(amountEl.value, 10) : 0;
        if (!amountEl || (shopId === 0 && !amountEl.options[amountEl.selectedIndex].getAttribute('data-yuan'))) {
          showMsg(rechargeMsg, '请选择充值档位', true); return;
        }
        if (rechargeResult) { rechargeResult.style.display = 'none'; rechargeResult.innerHTML = ''; }
        rechargeSubmitBtn.disabled = true;
        showMsg(rechargeMsg, '正在创建订单…', false);
        fetch(API_BASE + '/api/sutui/recharge-create', {
          method: 'POST',
          headers: authHeaders(),
          body: JSON.stringify({ shop_id: shopId })
        })
          .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, status: r.status, data: d }; }); })
          .then(function(x) {
            if (!x.ok && x.data && x.data.detail) {
              showMsg(rechargeMsg, x.data.detail, true);
              return;
            }
            var d = x.data || {};
            showMsg(rechargeMsg, '', false);
            if (d.need_oauth && d.recharge_url) {
              window.open(d.recharge_url, '_blank', 'noopener');
              if (rechargeResult) {
                rechargeResult.innerHTML = '<p>' + (d.message || '请前往速推官网完成登录后充值') + '。已为您打开充值页，若未打开<a href="' + escapeAttr(d.recharge_url) + '" target="_blank" rel="noopener" style="color:var(--primary);">点击此处</a>。</p>';
                rechargeResult.style.display = 'block';
              }
            } else if (d.pay_url) {
              window.open(d.pay_url, '_blank', 'noopener');
              if (rechargeResult) {
                rechargeResult.innerHTML = '<p>已打开支付页面，完成支付后余额将自动到账。若未打开，<a href="' + escapeAttr(d.pay_url) + '" target="_blank" rel="noopener" style="color:var(--primary);">点击此处</a>。</p>';
                rechargeResult.style.display = 'block';
              }
            } else if (d.qr_code) {
              if (rechargeResult) {
                var qr = d.qr_code;
                if (qr.indexOf('http') === 0 || qr.indexOf('data:') === 0) {
                  rechargeResult.innerHTML = '<p>请使用支付 App 扫描下方二维码：</p><img src="' + escapeAttr(qr) + '" alt="支付二维码" style="max-width:220px;height:auto;margin-top:0.5rem;">';
                } else {
                  rechargeResult.innerHTML = '<p>支付链接：<a href="' + escapeAttr(qr) + '" target="_blank" rel="noopener" style="color:var(--primary);">' + escapeHtml(qr.slice(0, 60)) + '…</a></p>';
                }
                rechargeResult.style.display = 'block';
              }
            }
            if (typeof loadSutuiBalance === 'function') loadSutuiBalance();
          })
          .catch(function() { showMsg(rechargeMsg, '网络错误', true); })
          .finally(function() { rechargeSubmitBtn.disabled = false; });
      });
    }
  } else {
    var rechargeBlock = document.getElementById('rechargeBlock');
    if (rechargeBlock) rechargeBlock.style.display = 'none';
  }
  if (refreshBtn) refreshBtn.onclick = loadBillingView;
}

function logsApiBase() {
  var lb = (typeof LOCAL_API_BASE !== 'undefined' && LOCAL_API_BASE) ? String(LOCAL_API_BASE).replace(/\/$/, '') : '';
  if (lb) return lb;
  return (typeof API_BASE !== 'undefined' ? String(API_BASE) : '').replace(/\/$/, '');
}

function ensureLogsBindings() {
  var refreshBtn = document.getElementById('logsRefreshBtn');
  var loadBtn = document.getElementById('logsLoadBtn');
  var exportBtn = document.getElementById('logsExportBtn');
  var tailEl = document.getElementById('logsTail');
  if (refreshBtn && !refreshBtn._logsBound) {
    refreshBtn._logsBound = true;
    refreshBtn.onclick = loadLogsView;
  }
  if (loadBtn && !loadBtn._logsBound) {
    loadBtn._logsBound = true;
    loadBtn.onclick = loadLogsView;
  }
  if (exportBtn && !exportBtn._logsBound) {
    exportBtn._logsBound = true;
    exportBtn.onclick = exportLogsView;
  }
  if (tailEl && !tailEl._logsBound) {
    tailEl._logsBound = true;
    tailEl.addEventListener('change', loadLogsView);
  }
}

function exportLogsView() {
  var btn = document.getElementById('logsExportBtn');
  var tailEl = document.getElementById('logsTail');
  var tail = (tailEl && tailEl.value) ? parseInt(tailEl.value, 10) : 2000;
  var base = logsApiBase();
  var url = (base ? base : '') + '/api/logs?tail=' + tail;
  if (btn) btn.disabled = true;
  var opts = {
    method: 'GET',
    credentials: 'same-origin',
    headers: typeof authHeaders === 'function' ? authHeaders() : { 'Authorization': 'Bearer ' + (typeof token !== 'undefined' ? token : '') }
  };
  fetch(url, opts)
    .then(function(r) {
      if (!r.ok) return r.text().then(function(txt) { throw new Error((txt || '').slice(0, 400) || String(r.status)); });
      return r.text();
    })
    .then(function(text) {
      var d = new Date();
      var pad = function(n) { return n < 10 ? '0' + n : String(n); };
      var fname = 'lobster-app-log-' + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + '-' + pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds()) + '.txt';
      var blob = new Blob([text != null ? text : ''], { type: 'text/plain;charset=utf-8' });
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(a.href);
    })
    .catch(function(e) {
      var msg = (e && e.message) ? e.message : String(e);
      if (typeof alert !== 'undefined') alert('导出失败：' + msg);
    })
    .finally(function() { if (btn) btn.disabled = false; });
}

function loadLogsView() {
  var pre = document.getElementById('logsContent');
  var tailEl = document.getElementById('logsTail');
  if (!pre) {
    if (typeof console !== 'undefined') console.warn('[日志] #logsContent 未找到');
    return;
  }
  var tail = (tailEl && tailEl.value) ? parseInt(tailEl.value, 10) : 2000;
  pre.textContent = '加载中…';
  var base = logsApiBase();
  var url = (base ? base : '') + '/api/logs?tail=' + tail;
  var timeout = 20000;
  var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  var t = ctrl ? setTimeout(function() { if (ctrl) ctrl.abort(); }, timeout) : null;
  var opts = {
    method: 'GET',
    credentials: 'same-origin',
    headers: typeof authHeaders === 'function' ? authHeaders() : { 'Authorization': 'Bearer ' + (typeof token !== 'undefined' ? token : '') }
  };
  if (ctrl) opts.signal = ctrl.signal;
  fetch(url, opts)
    .then(function(r) {
      if (t) clearTimeout(t);
      if (!r.ok) return r.text().then(function(txt) { throw new Error(txt || r.status); });
      return r.text();
    })
    .then(function(text) {
      pre.textContent = text || '(空)';
      pre.scrollTop = pre.scrollHeight;
    })
    .catch(function(e) {
      if (t) clearTimeout(t);
      var msg = (e && e.name === 'AbortError') ? '加载超时，请重试' : (e && e.message ? e.message : String(e));
      pre.textContent = '加载失败: ' + msg;
    });
  ensureLogsBindings();
}

(function initWecomConfigHash() {
  function applyHash() {
    var hash = (location.hash || '').replace(/^#/, '');
    if (hash === 'wecom-config' && typeof showWecomConfigView === 'function') showWecomConfigView();
    if (hash.indexOf('wecom-detail') === 0 && typeof showWecomDetailView === 'function') {
      var parts = hash.split(':');
      showWecomDetailView(parts[1] ? parseInt(parts[1], 10) : undefined);
    }
    if (hash === 'messenger-config' && typeof _openMessengerConfigView === 'function') {
      _openMessengerConfigView();
    }
    if (hash === 'twilio-whatsapp-config' && typeof _openTwilioWhatsappConfigView === 'function') {
      _openTwilioWhatsappConfigView();
    }
    if (hash === 'twilio-whatsapp-detail' && typeof showTwilioWhatsappDetailView === 'function') {
      showTwilioWhatsappDetailView();
    }
    if (hash === 'youtube-accounts' && typeof window._openYoutubeAccountsView === 'function') {
      window._openYoutubeAccountsView();
    }
    if (hash === 'meta-social' && typeof window._openMetaSocialView === 'function') {
      window._openMetaSocialView();
    }
    if (hash === 'ecommerce-detail-studio' && typeof window._openEcommerceDetailStudioView === 'function') {
      window._openEcommerceDetailStudioView();
    }
  }
  window.addEventListener('hashchange', applyHash);
  window._applyWecomConfigHash = applyHash;
  if (location.hash && (
    location.hash.indexOf('wecom') !== -1 ||
    location.hash.indexOf('messenger') !== -1 ||
    location.hash.indexOf('twilio-whatsapp') !== -1 ||
    location.hash.indexOf('youtube-accounts') !== -1 ||
    location.hash.indexOf('meta-social') !== -1 ||
    location.hash.indexOf('ecommerce-detail-studio') !== -1
  )) applyHash();
})();

applyBrandingFromApi();
if (token) loadDashboard();
