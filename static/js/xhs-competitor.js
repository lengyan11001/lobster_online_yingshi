/**
 * 多平台同行：技能商店「同行跟踪」二级工作台
 *
 * 支持平台：小红书 / 抖音 / 快手 / 微博 / 微信视频号
 *
 * - 复用云端 /api/tikhub-proxy/catalog 与 /api/tikhub-proxy/call
 * - 启动时从 catalog 中按平台 id/名称匹配定位平台，然后按端点 id 模式自动发现四个核心接口：
 *     · 搜索用户       searchUsers
 *     · 获取用户作品   userPosts
 *     · 获取作品评论   postComments
 *     · 搜索作品       searchPosts
 * - 同行跟踪记录、作品缓存、评论缓存均落到 localStorage（按平台隔离）
 * - 默认每次只查 1 页；用户点「下一页」才追加请求
 */
(function() {
  'use strict';

  function _base() { return (typeof API_BASE !== 'undefined' ? API_BASE : ''); }
  function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function(c) {
      return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c];
    });
  }
  function _fetchJson(path, opts) {
    return fetch(_base() + path, Object.assign({ headers: (typeof authHeaders === 'function' ? authHeaders() : {}) }, opts || {}))
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, status: r.status, data: d }; }, function() { return { ok: r.ok, status: r.status, data: null }; }); });
  }

  // -------------------------------------------------------------
  // 多平台配置
  // -------------------------------------------------------------
  var PLATFORM_CONFIGS = {
    xiaohongshu: {
      label: '小红书',
      platformMatch: /^(xiaohongshu|xhs)$/i,
      nameMatch: /小红书|xiaohongshu|xhs/i,
      userIdLabel: '小红书号',
      userIdPlaceholder: 'red_id，可留空',
      postLabel: '笔记',
      searchPlaceholder: '如：李子柒',
      keywordPlaceholder: '如：露营装备',
    },
    douyin: {
      label: '抖音',
      platformMatch: /^(douyin|dy)$/i,
      nameMatch: /抖音|douyin/i,
      userIdLabel: '抖音号',
      userIdPlaceholder: 'sec_uid / unique_id，可留空',
      postLabel: '视频',
      searchPlaceholder: '如：张同学',
      keywordPlaceholder: '如：美食探店',
    },
    kuaishou: {
      label: '快手',
      platformMatch: /^(kuaishou|ks)$/i,
      nameMatch: /快手|kuaishou/i,
      userIdLabel: '快手号',
      userIdPlaceholder: '快手ID，可留空',
      postLabel: '视频',
      searchPlaceholder: '如：辛巴',
      keywordPlaceholder: '如：户外直播',
    },
    weibo: {
      label: '微博',
      platformMatch: /^(weibo|sina_weibo)$/i,
      nameMatch: /微博|weibo/i,
      userIdLabel: '微博ID',
      userIdPlaceholder: '微博uid，可留空',
      postLabel: '微博',
      searchPlaceholder: '如：人民日报',
      keywordPlaceholder: '如：热搜话题',
    },
    weixin: {
      label: '微信视频号',
      platformMatch: /^(weixin|wechat|wechat_video|weixin_video)$/i,
      nameMatch: /微信|视频号|weixin|wechat/i,
      userIdLabel: '视频号ID',
      userIdPlaceholder: '视频号ID，可留空',
      postLabel: '视频',
      searchPlaceholder: '如：央视新闻',
      keywordPlaceholder: '如：直播带货',
    },
  };
  var PLATFORM_ORDER = ['xiaohongshu', 'douyin', 'kuaishou', 'weibo', 'weixin'];

  var _currentPlatform = 'xiaohongshu';

  // -------------------------------------------------------------
  // localStorage helpers（按平台隔离）
  // -------------------------------------------------------------
  function _lsKey(suffix) { return 'compet_' + _currentPlatform + '_' + suffix; }
  function _lsRecordsKey() { return _lsKey('records_v1'); }
  function _lsNotesPrefix() { return _lsKey('notes_v1::'); }
  function _lsCommentsPrefix() { return _lsKey('comments_v1::'); }
  function _lsKeywordKey() { return _lsKey('keyword_state_v1'); }

  var LS_VERSION_KEY = 'compet_lsver';
  var CURRENT_LS_VERSION = 'v4-2026-04-22-multiplatform';
  (function migrateLs() {
    try {
      if (localStorage.getItem(LS_VERSION_KEY) === CURRENT_LS_VERSION) return;
      var keys = Object.keys(localStorage);
      keys.forEach(function(k) {
        if (/^(xhs_compet_|compet_).*(keyword_state|notes_v1::|comments_v1::)/.test(k)) {
          try { localStorage.removeItem(k); } catch (e) {}
        }
      });
      var oldRecords = null;
      try { oldRecords = localStorage.getItem('xhs_compet_records_v1'); } catch (e) {}
      if (oldRecords) {
        try {
          localStorage.setItem('compet_xiaohongshu_records_v1', oldRecords);
          localStorage.removeItem('xhs_compet_records_v1');
        } catch (e) {}
      }
      localStorage.setItem(LS_VERSION_KEY, CURRENT_LS_VERSION);
    } catch (e) {}
  })();

  function _loadJson(k, fallback) {
    try { var raw = localStorage.getItem(k); if (!raw) return fallback; return JSON.parse(raw); }
    catch (e) { return fallback; }
  }
  function _saveJson(k, v) {
    try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {}
  }

  // -------------------------------------------------------------
  // 端点配置：小红书钦定 ID + 通用自动发现模式
  // -------------------------------------------------------------
  // 小红书保留原来的精确 ID 列表（按稳定性排序），其他平台走自动发现。
  var XHS_ENDPOINT_TARGETS = {
    searchUsers: ['xhs_search_users_2', 'xhs_fetch_search_users'],
    userPosts: ['xhs_get_user_notes_2', 'xhs_fetch_home_notes', 'xhs_fetch_user_notes', 'xhs_get_user_posted_notes'],
    postComments: ['xhs_fetch_note_comments', 'xhs_fetch_note_comments_2', 'xhs_get_note_comments_3', 'xhs_get_note_comments', 'xhs_get_note_comments_2'],
    searchPosts: ['xhs_search_notes_v3', 'xhs_search_notes_3', 'xhs_fetch_search_notes', 'xhs_fetch_search_notes_2', 'xhs_search_notes', 'xhs_search_notes_2'],
  };

  /** 自动发现端点的角色模式：在目录中按 endpoint.id 和 endpoint.title 匹配 */
  var ROLE_PATTERNS = {
    searchUsers:  { idRe: /search.*user|user.*search/i, titleRe: /搜索.*用户|用户.*搜索|search.*user/i },
    userPosts:    { idRe: /user.*(post|video|note|work|aweme|photo|feed)|get.*(post|video|note|work|aweme)|fetch.*(post|video|note|work|aweme)/i, titleRe: /用户.*(作品|视频|笔记|动态|帖子)|获取.*(作品|视频|笔记)/i },
    postComments: { idRe: /comment/i, titleRe: /评论|comment/i },
    searchPosts:  { idRe: /search.*(post|video|note|work|aweme|content)|search(?!.*user)/i, titleRe: /搜索.*(视频|笔记|作品|内容|帖子)|search.*(video|note|post)/i },
  };

  /**
   * 每个 endpoint 的精确翻页规则（catalog 自动识别经常漏，所以写死已知的）。
   * 未收录的端点会根据其参数定义自动推断翻页方式。
   */
  var ENDPOINT_PAGING = {
    // 小红书 - 搜索用户
    'xhs_search_users_2': { mode: 'page', inParam: 'page' },
    'xhs_fetch_search_users': { mode: 'page', inParam: 'page' },
    // 小红书 - 用户笔记
    'xhs_get_user_notes_2': { mode: 'cursor', inParam: 'lastCursor', outFields: ['cursor', 'last_cursor', 'lastCursor', 'next_cursor'] },
    'xhs_fetch_home_notes': { mode: 'cursor', inParam: 'cursor', outFields: ['cursor', 'next_cursor', 'last_cursor'] },
    'xhs_fetch_user_notes': { mode: 'cursor', inParam: 'cursor', outFields: ['cursor', 'next_cursor', 'last_cursor'] },
    'xhs_get_user_posted_notes': { mode: 'cursor', inParam: 'cursor', outFields: ['cursor', 'next_cursor', 'last_cursor'] },
    // 小红书 - 笔记评论
    'xhs_fetch_note_comments_2': { mode: 'cursor', inParam: 'cursor', outFields: ['cursor', 'next_cursor', 'last_cursor'] },
    'xhs_fetch_note_comments': { mode: 'cursor', inParam: 'cursor', outFields: ['cursor', 'next_cursor', 'last_cursor'] },
    'xhs_get_note_comments_3': { mode: 'cursor', inParam: 'lastCursor', outFields: ['cursor', 'last_cursor', 'lastCursor', 'next_cursor'] },
    'xhs_get_note_comments': { mode: 'cursor', inParam: 'cursor', outFields: ['cursor', 'next_cursor', 'last_cursor'] },
    'xhs_get_note_comments_2': { mode: 'cursor', inParam: 'start', outFields: ['cursor', 'start', 'next_cursor', 'next_start'] },
    // 小红书 - 搜索笔记
    'xhs_search_notes_v3': { mode: 'page', inParam: 'page' },
    'xhs_search_notes_3': { mode: 'page', inParam: 'page' },
    'xhs_fetch_search_notes': { mode: 'page', inParam: 'page' },
    'xhs_fetch_search_notes_2': { mode: 'page', inParam: 'page' },
    'xhs_search_notes': { mode: 'page', inParam: 'page' },
    'xhs_search_notes_2': { mode: 'page', inParam: 'page' },
  };

  /** 当 ENDPOINT_PAGING 没有收录某端点时，根据其参数自动推断翻页方式 */
  function _inferPaging(ep) {
    if (ENDPOINT_PAGING[ep.id]) return ENDPOINT_PAGING[ep.id];
    var pdef = ep.params || [];
    var names = pdef.map(function(p) { return p.name; });
    var CURSOR_CANDIDATES = ['cursor', 'next_cursor', 'max_cursor', 'pcursor', 'lastCursor', 'last_cursor', 'offset'];
    var PAGE_CANDIDATES = ['page', 'page_num', 'pageNum', 'page_no'];
    for (var i = 0; i < CURSOR_CANDIDATES.length; i++) {
      if (names.indexOf(CURSOR_CANDIDATES[i]) >= 0) {
        return { mode: 'cursor', inParam: CURSOR_CANDIDATES[i], outFields: ['cursor', 'next_cursor', 'last_cursor', 'lastCursor', 'max_cursor', 'pcursor'] };
      }
    }
    for (var j = 0; j < PAGE_CANDIDATES.length; j++) {
      if (names.indexOf(PAGE_CANDIDATES[j]) >= 0) {
        return { mode: 'page', inParam: PAGE_CANDIDATES[j] };
      }
    }
    return { mode: 'page', inParam: 'page' };
  }

  /** 从响应里按 endpoint 已知的 outFields 顺序取下一页 cursor。返回 null 表示找不到。 */
  function _extractCursor(payload, epIdOrObj) {
    var rule = (typeof epIdOrObj === 'object' && epIdOrObj) ? _inferPaging(epIdOrObj) : (ENDPOINT_PAGING[epIdOrObj] || null);
    if (!rule || rule.mode !== 'cursor') return null;
    var data = (payload && payload.data) || {};
    var listInfo = _findFirstList(data) || { parent: data };
    var inners = [listInfo.parent || data, data, data && data.data, data && data.data && data.data.data];
    for (var i = 0; i < inners.length; i++) {
      var o = inners[i];
      if (!o || typeof o !== 'object') continue;
      for (var j = 0; j < (rule.outFields || []).length; j++) {
        var v = o[rule.outFields[j]];
        if (v != null && v !== '' && v !== '0' && v !== 0) return v;
      }
    }
    return null;
  }

  /** 诊断用：把响应里所有 cursor 嫌疑字段、has_more 字段都收集出来打印 */
  function _diagPagingFields(payload) {
    var out = { cursors: {}, more: {} };
    var data = (payload && payload.data) || {};
    var listInfo = _findFirstList(data) || { parent: data };
    var seen = new Set();
    function scan(o, path, depth) {
      if (!o || typeof o !== 'object' || depth > 4) return;
      Object.keys(o).forEach(function(k) {
        var p = path ? path + '.' + k : k;
        if (seen.has(p)) return; seen.add(p);
        var v = o[k];
        if (/cursor|start|next|last/i.test(k) && (typeof v === 'string' || typeof v === 'number')) {
          out.cursors[p] = v;
        }
        if (/has_more|more_available|hasMore|next_page/i.test(k)) {
          out.more[p] = v;
        }
        if (v && typeof v === 'object' && !Array.isArray(v)) scan(v, p, depth + 1);
      });
    }
    scan(data, '', 0);
    return out;
  }

  function _readHasMore(payload) {
    var data = (payload && payload.data) || {};
    var listInfo = _findFirstList(data) || { parent: data };
    var inners = [listInfo.parent || data, data, data && data.data];
    for (var i = 0; i < inners.length; i++) {
      var o = inners[i];
      if (!o || typeof o !== 'object') continue;
      if (o.has_more === true || o.has_more === 'true' || o.has_more === 1) return true;
      if (o.hasMore === true || o.hasMore === 'true') return true;
      if (o.more_available === true || o.more_available === 'true') return true;
    }
    return false;
  }

  // -------------------------------------------------------------
  // State
  // -------------------------------------------------------------
  function _initState() {
    return {
      catalog: null,
      endpoints: {
        searchUsers: null,
        userPosts: null,
        postComments: null,
        searchPosts: null,
      },
      endpointAlts: { searchUsers: [], userPosts: [], postComments: [], searchPosts: [] },
      records: _loadJson(_lsRecordsKey(), []),
      keyword: _loadJson(_lsKeywordKey(), { keyword: '', items: [], page: 0, hasMore: false, cursor: null, endpointId: null }),
    };
  }
  var _state = _initState();

  // -------------------------------------------------------------
  // 视图打开
  // -------------------------------------------------------------
  function _switchHidden(view) {
    if (typeof currentView !== 'undefined' && currentView === 'chat' && typeof saveCurrentSessionToStore === 'function') {
      try { saveCurrentSessionToStore(); } catch(e) {}
    }
    location.hash = view;
    document.querySelectorAll('.nav-left-item').forEach(function(b) { b.classList.remove('active'); });
    document.querySelectorAll('.content-block').forEach(function(p) { p.classList.remove('visible'); });
    var el = document.getElementById('content-' + view);
    if (el) el.classList.add('visible');
    if (typeof currentView !== 'undefined') currentView = view;
  }

  window._openXhsCompetitorView = function() {
    _switchHidden('xhs-competitor');
    _ensureCatalog();
    _refreshBalance();
    _updatePlatformUI();
    _renderTrackedList();
    _renderKeywordResult();
  };

  function _switchPlatform(platId) {
    if (!PLATFORM_CONFIGS[platId]) return;
    _currentPlatform = platId;
    _state.records = _loadJson(_lsRecordsKey(), []);
    _state.keyword = _loadJson(_lsKeywordKey(), { keyword: '', items: [], page: 0, hasMore: false, cursor: null, endpointId: null });
    _state.endpoints = { searchUsers: null, userPosts: null, postComments: null, searchPosts: null };
    _state.endpointAlts = { searchUsers: [], userPosts: [], postComments: [], searchPosts: [] };
    if (_state.catalog) _resolveEndpoints();
    _updatePlatformUI();
    _renderTrackedList();
    _renderKeywordResult();
    var searchResult = document.getElementById('xhsCompetUserSearchResult');
    if (searchResult) searchResult.innerHTML = '';
  }

  function _updatePlatformUI() {
    var cfg = PLATFORM_CONFIGS[_currentPlatform] || {};
    var titleEl = document.getElementById('xhsCompetTitle');
    if (titleEl) titleEl.textContent = (cfg.label || '') + '同行';
    var uidLabel = document.getElementById('xhsCompetUidLabel');
    if (uidLabel) uidLabel.textContent = (cfg.userIdLabel || '平台ID') + '（可选）';
    var uidInput = document.getElementById('xhsCompetRedIdInput');
    if (uidInput) uidInput.placeholder = cfg.userIdPlaceholder || '可留空';
    var nameInput = document.getElementById('xhsCompetNameInput');
    if (nameInput) nameInput.placeholder = cfg.searchPlaceholder || '搜索昵称';
    var kwInput = document.getElementById('xhsCompetKeywordInput');
    if (kwInput) kwInput.placeholder = cfg.keywordPlaceholder || '关键词';
    var platSel = document.getElementById('xhsCompetPlatformSel');
    if (platSel) platSel.value = _currentPlatform;
  }

  function _backToStore() {
    var nav = document.querySelector('.nav-left-item[data-view="skill-store"]');
    if (nav) nav.click();
  }

  // -------------------------------------------------------------
  // 目录加载 + 接口定位
  // -------------------------------------------------------------
  function _setCatalogStatus(text, isErr) {
    var el = document.getElementById('xhsCompetCatalogStatus');
    if (!el) return;
    el.textContent = text || '';
    el.style.color = isErr ? '#ef4444' : 'var(--text-muted)';
  }

  function _refreshBalance() {
    _fetchJson('/api/tikhub-proxy/balance').then(function(res) {
      if (!res.ok) return;
      var tip = document.getElementById('xhsCompetBalanceTip');
      if (tip) tip.textContent = '当前余额：' + (res.data && res.data.credits != null ? res.data.credits : '?') + ' 积分';
    });
  }

  function _ensureCatalog(force) {
    if (_state.catalog && !force) { _resolveEndpoints(); return Promise.resolve(_state.catalog); }
    _setCatalogStatus('加载接口目录…');
    return _fetchJson('/api/tikhub-proxy/catalog').then(function(res) {
      if (!res.ok) {
        var msg = (res.data && (res.data.detail || res.data.message)) || ('HTTP ' + res.status);
        _setCatalogStatus('目录加载失败：' + msg, true);
        return null;
      }
      _state.catalog = res.data || { platforms: [] };
      _resolveEndpoints();
      return _state.catalog;
    });
  }

  function _resolveEndpoints() {
    var plats = (_state.catalog && _state.catalog.platforms) || [];
    var cfg = PLATFORM_CONFIGS[_currentPlatform];
    if (!cfg) { _setCatalogStatus('未知平台', true); return; }

    var matched = plats.filter(function(p) {
      return cfg.platformMatch.test(p.id || '') || cfg.nameMatch.test(p.name || '');
    })[0];
    if (!matched) {
      _state.endpoints = { searchUsers: null, userPosts: null, postComments: null, searchPosts: null };
      _state.endpointAlts = { searchUsers: [], userPosts: [], postComments: [], searchPosts: [] };
      _setCatalogStatus('未在云端目录找到「' + cfg.label + '」平台。请联系管理员补齐 TikHub 分组。', true);
      return;
    }

    var allEps = [];
    (matched.groups || []).forEach(function(g) {
      (g.endpoints || []).forEach(function(e) { allEps.push(Object.assign({ _group: g.name || g.id || '' }, e)); });
    });
    var byId = {};
    allEps.forEach(function(e) { byId[e.id] = e; });

    if (_currentPlatform === 'xiaohongshu') {
      Object.keys(XHS_ENDPOINT_TARGETS).forEach(function(role) {
        var ids = XHS_ENDPOINT_TARGETS[role];
        var alts = ids.map(function(id) { return byId[id]; }).filter(Boolean);
        var extra = _discoverByPattern(allEps, role, alts);
        _state.endpointAlts[role] = alts.concat(extra);
        _state.endpoints[role] = _state.endpointAlts[role][0] || null;
      });
    } else {
      var roles = ['searchUsers', 'userPosts', 'postComments', 'searchPosts'];
      roles.forEach(function(role) {
        var alts = _discoverByPattern(allEps, role, []);
        _state.endpointAlts[role] = alts;
        _state.endpoints[role] = alts[0] || null;
      });
    }

    var roleLabels = { searchUsers: '搜索用户', userPosts: '用户作品', postComments: '作品评论', searchPosts: '搜索作品' };
    var miss = [];
    Object.keys(roleLabels).forEach(function(role) {
      if (!_state.endpoints[role]) miss.push(roleLabels[role]);
    });
    if (miss.length) {
      _setCatalogStatus(cfg.label + '：未找到接口 ' + miss.join(' / ') + '。请联系管理员刷新 TikHub 目录。', true);
    } else {
      _setCatalogStatus(cfg.label + ' 已就绪 · ' +
        Object.keys(roleLabels).map(function(r) { return roleLabels[r] + '=' + _state.endpoints[r].id; }).join(' · ') +
        '（失败时自动切下一备选）', false);
    }
  }

  function _discoverByPattern(allEps, role, exclude) {
    var pat = ROLE_PATTERNS[role];
    if (!pat) return [];
    var excludeIds = {};
    (exclude || []).forEach(function(e) { excludeIds[e.id] = true; });
    return allEps.filter(function(e) {
      if (excludeIds[e.id]) return false;
      return pat.idRe.test(e.id || '') || pat.titleRe.test(e.title || e.name || '');
    });
  }

  // -------------------------------------------------------------
  // 通用：调 tikhub-proxy/call
  // -------------------------------------------------------------
  function _callEndpoint(endpoint, params) {
    if (!endpoint) return Promise.reject(new Error('接口未就绪，请先刷新目录'));
    return _fetchJson('/api/tikhub-proxy/call', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, (typeof authHeaders === 'function' ? authHeaders() : {})),
      body: JSON.stringify({ endpoint_id: endpoint.id, params: params || {} }),
    }).then(function(res) {
      if (!res.ok) {
        var detail = (res.data && (res.data.detail || res.data.message)) || ('HTTP ' + res.status);
        if (res.status === 402 && window.confirm(detail + '\n\n是否前往充值？')) {
          var billNav = document.querySelector('.nav-left-item[data-view="billing"]');
          if (billNav) billNav.click();
        }
        var err = new Error(detail);
        err.status = res.status;
        throw err;
      }
      _refreshBalance();
      return res.data;
    });
  }

  // -------------------------------------------------------------
  // 数据规整：从原始响应里挑出列表 + 翻页游标 + has_more
  // -------------------------------------------------------------
  var SKIP_KEYS = { nav:1, navs:1, pages:1, page_info:1, pagination:1, tabs:1, filters:1, sorts:1, banners:1, ads:1, extra:1, log_pb:1 };

  function _findFirstList(obj, depth) {
    if (!obj || typeof obj !== 'object') return null;
    depth = depth || 0;
    if (depth > 6) return null;
    var keys = Object.keys(obj);
    var best = null;
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i]; if (SKIP_KEYS[k]) continue;
      var v = obj[k];
      if (Array.isArray(v) && v.length && typeof v[0] === 'object') {
        var score = Object.keys(v[0] || {}).length * 10 + Math.min(v.length, 30);
        if (!best || score > best.score) best = { key: k, list: v, score: score, parent: obj };
      }
    }
    if (best) return best;
    for (var j = 0; j < keys.length; j++) {
      var kk = keys[j]; if (SKIP_KEYS[kk]) continue;
      var vv = obj[kk];
      if (vv && typeof vv === 'object' && !Array.isArray(vv)) {
        var sub = _findFirstList(vv, depth + 1);
        if (sub) return sub;
      }
    }
    return null;
  }

  function _readPaging(payload, endpoint) {
    var data = (payload && payload.data) || {};
    var listInfo = _findFirstList(data) || { list: [], parent: data };
    var parent = listInfo.parent || data;
    var ep = endpoint && endpoint.pagination;
    var cursor = null, hasMore = false;
    var inners = [parent, data, data && data.data, data && data.data && data.data.data];
    var CURSOR_KEYS = ['next_cursor', 'nextCursor', 'cursor', 'last_cursor', 'lastCursor', 'max_cursor', 'next_max_id', 'next_token', 'pcursor'];
    inners.forEach(function(o) {
      if (!o || typeof o !== 'object') return;
      if (cursor == null) {
        for (var i = 0; i < CURSOR_KEYS.length; i++) {
          var v = o[CURSOR_KEYS[i]];
          if (v != null && v !== '' && v !== '0' && v !== 0) { cursor = v; break; }
        }
      }
      if (!hasMore) {
        if (o.has_more === true || o.has_more === 'true' || o.has_more === 1) hasMore = true;
        if (o.hasMore === true || o.hasMore === 'true') hasMore = true;
        if (o.more_available === true || o.more_available === 'true') hasMore = true;
      }
    });
    if (ep && ep.out_field) {
      var v2 = parent[ep.out_field] || (data && data[ep.out_field]);
      if (v2 != null && v2 !== '' && v2 !== '0') cursor = v2;
    }
    if (ep && ep.has_more_field) {
      var hv = parent[ep.has_more_field];
      if (hv === true || hv === 'true' || hv === 1) hasMore = true;
    }
    return { list: listInfo.list || [], cursor: cursor, hasMore: !!hasMore || cursor != null };
  }

  function _findImageUrl(obj, depth) {
    depth = depth || 0;
    if (!obj || typeof obj !== 'object' || depth > 4) return null;
    var imgKeys = ['avatar', 'image', 'image_url', 'images', 'cover', 'cover_url', 'thumb', 'pic_url', 'picture', 'img', 'imgUrl', 'avatar_url'];
    for (var i = 0; i < imgKeys.length; i++) {
      var v = obj[imgKeys[i]];
      if (typeof v === 'string' && /^https?:/.test(v)) return v;
      if (v && typeof v === 'object') {
        if (Array.isArray(v.url_list) && v.url_list.length) return v.url_list[0];
        if (typeof v.url === 'string') return v.url;
        if (Array.isArray(v) && v.length) {
          if (typeof v[0] === 'string' && /^https?:/.test(v[0])) return v[0];
          if (v[0] && typeof v[0] === 'object') {
            var f = _findImageUrl(v[0], depth + 1); if (f) return f;
          }
        }
      }
    }
    var keys = Object.keys(obj);
    for (var j = 0; j < keys.length; j++) {
      var sub = obj[keys[j]];
      if (sub && typeof sub === 'object') {
        var ff = _findImageUrl(sub, depth + 1); if (ff) return ff;
      }
    }
    return null;
  }

  function _firstNonEmpty(obj, keys) {
    for (var i = 0; i < keys.length; i++) {
      var v = obj[keys[i]];
      if (v != null && v !== '') return v;
    }
    return '';
  }

  function _normalizeUser(item) {
    if (!item || typeof item !== 'object') return null;
    var u = item.user_info || item.user || item.author || item;
    return {
      user_id: String(_firstNonEmpty(u, ['user_id', 'userId', 'uid', 'id']) || _firstNonEmpty(item, ['user_id', 'userId', 'uid', 'id'])),
      sec_user_id: String(_firstNonEmpty(u, ['sec_user_id', 'sec_uid']) || _firstNonEmpty(item, ['sec_user_id', 'sec_uid'])),
      red_id: String(_firstNonEmpty(u, ['red_id', 'redId', 'redbook_id', 'unique_id', 'short_id', 'custom_verify']) || _firstNonEmpty(item, ['red_id', 'redId', 'unique_id', 'short_id'])),
      nickname: String(_firstNonEmpty(u, ['nickname', 'nick_name', 'name', 'screen_name', 'user_name']) || _firstNonEmpty(item, ['nickname', 'name', 'nick_name', 'screen_name'])),
      avatar: _findImageUrl(item) || '',
      signature: String(_firstNonEmpty(u, ['signature', 'desc', 'description', 'sub_title', 'bio']) || ''),
      fans: _firstNonEmpty(u, ['fans', 'follower_count', 'followers', 'follower', 'follow_count', 'fans_count', 'mfollower_count']),
      raw: item,
    };
  }

  function _normalizeNote(item) {
    if (!item || typeof item !== 'object') return null;
    var n = item.note_info || item.note || item.note_card || item.aweme || item.video || item;
    var noteId = String(_firstNonEmpty(n, ['note_id', 'noteId', 'aweme_id', 'video_id', 'item_id', 'id', 'mid', 'photo_id']) || _firstNonEmpty(item, ['note_id', 'noteId', 'aweme_id', 'video_id', 'item_id', 'id']));
    var xsec = String(_firstNonEmpty(n, ['xsec_token']) || _firstNonEmpty(item, ['xsec_token']) || '');
    var u = n.user || n.user_info || n.author || item.user || item.user_info || item.author || {};
    return {
      note_id: noteId,
      xsec_token: xsec,
      title: String(_firstNonEmpty(n, ['title', 'display_title', 'caption', 'desc', 'description', 'text']) || ''),
      desc: String(_firstNonEmpty(n, ['desc', 'description', 'content', 'caption', 'text']) || ''),
      cover: _findImageUrl(n) || _findImageUrl(item) || '',
      type: String(_firstNonEmpty(n, ['type', 'note_type', 'aweme_type', 'media_type']) || ''),
      like_count: _firstNonEmpty(n, ['liked_count', 'like_count', 'likes', 'digg_count', 'attitudes_count']),
      comment_count: _firstNonEmpty(n, ['comments_count', 'comment_count', 'comments', 'comment_cnt']),
      collect_count: _firstNonEmpty(n, ['collected_count', 'collect_count', 'collects', 'favorite_count']),
      share_count: _firstNonEmpty(n, ['shared_count', 'share_count', 'shares', 'forward_count', 'reposts_count']),
      time: _firstNonEmpty(n, ['time', 'create_time', 'created_at', 'publish_time', 'last_update_time', 'timestamp']),
      author: {
        user_id: String(_firstNonEmpty(u, ['user_id', 'userId', 'uid', 'id'])),
        nickname: String(_firstNonEmpty(u, ['nickname', 'nick_name', 'name', 'screen_name'])),
      },
      raw: item,
    };
  }

  function _normalizeComment(item) {
    if (!item || typeof item !== 'object') return null;
    var u = item.user_info || item.user || item.author || {};
    return {
      comment_id: String(_firstNonEmpty(item, ['id', 'comment_id', 'commentId']) || ''),
      content: String(_firstNonEmpty(item, ['content', 'text', 'desc']) || ''),
      like_count: _firstNonEmpty(item, ['like_count', 'likes', 'liked_count']),
      sub_count: _firstNonEmpty(item, ['sub_comment_count', 'reply_count', 'sub_count']),
      time: _firstNonEmpty(item, ['create_time', 'time', 'created_at']),
      ip: String(_firstNonEmpty(item, ['ip_location', 'ip']) || ''),
      author: {
        user_id: String(_firstNonEmpty(u, ['user_id', 'userId', 'id'])),
        nickname: String(_firstNonEmpty(u, ['nickname', 'nick_name', 'name'])),
        avatar: _findImageUrl(u),
      },
      raw: item,
    };
  }

  function _formatNumber(n) {
    if (n == null || n === '') return '-';
    var x = typeof n === 'number' ? n : parseFloat(n);
    if (isNaN(x)) return String(n);
    if (x >= 1e8) return (x / 1e8).toFixed(1) + '亿';
    if (x >= 1e4) return (x / 1e4).toFixed(1) + '万';
    return String(Math.floor(x));
  }

  function _postLabel() { return (PLATFORM_CONFIGS[_currentPlatform] || {}).postLabel || '作品'; }

  function _formatTime(t) {
    if (!t) return '';
    var v = t;
    if (typeof v === 'string' && /^\d+$/.test(v)) v = parseInt(v, 10);
    if (typeof v === 'number') {
      if (v < 1e12) v = v * 1000;
      try { return new Date(v).toLocaleString('zh-CN', { hour12: false }); } catch (e) {}
    }
    return String(t);
  }

  // -------------------------------------------------------------
  // Tab1：搜索用户 → 候选列表 → 用户确认入库
  // -------------------------------------------------------------
  function _onSearchUsers() {
    var nameInput = document.getElementById('xhsCompetNameInput');
    var redInput = document.getElementById('xhsCompetRedIdInput');
    var name = (nameInput && nameInput.value || '').trim();
    var redId = (redInput && redInput.value || '').trim();
    var cfg = PLATFORM_CONFIGS[_currentPlatform] || {};
    if (!name && !redId) {
      _setSearchUserMsg('昵称和' + (cfg.userIdLabel || '平台ID') + '至少填一个', true);
      return;
    }
    if (!_state.endpoints.searchUsers) {
      _setSearchUserMsg('搜索用户接口未就绪，请等待目录加载', true);
      return;
    }
    _setSearchUserMsg('搜索中…', false);
    var params = {};
    var pdef = _state.endpoints.searchUsers.params || [];
    var kwName = _firstParamName(pdef, KW_NAMES);
    if (kwName) params[kwName] = name || redId;
    var pageName = _firstParamName(pdef, PAGE_NAMES);
    if (pageName) params[pageName] = 1;
    /* 搜索用户也用自动 fallback：sequential 尝试 alts 直到拿到结果 */
    var allAlts = _state.endpointAlts.searchUsers || [_state.endpoints.searchUsers].filter(Boolean);

    function tryUser(idx) {
      if (idx >= allAlts.length) { _setSearchUserMsg('所有备选接口都失败或返回 0 条', true); return; }
      var ep = allAlts[idx];
      var pdef2 = ep.params || [];
      var ps = {};
      var kn = _firstParamName(pdef2, KW_NAMES);
      if (!kn) { tryUser(idx + 1); return; }
      ps[kn] = name || redId;
      var pn = _firstParamName(pdef2, PAGE_NAMES);
      if (pn) ps[pn] = 1;
      if (idx > 0) _setSearchUserMsg('搜索中（自动尝试 ' + ep.id + '）…', false);
      _callEndpoint(ep, ps).then(function(payload) {
        var info = _readPaging(payload, ep);
        var users = (info.list || []).map(_normalizeUser).filter(function(u) { return u && (u.user_id || u.sec_user_id || u.nickname); });
        if (redId) {
          var hit = users.filter(function(u) { return u.red_id === redId; });
          if (hit.length) users = hit;
        }
        if (!users.length) { tryUser(idx + 1); return; }
        _renderUserSearchResult(users, name, redId);
        _setSearchUserMsg('共 ' + users.length + ' 条候选', false);
      }).catch(function(e) {
        try { console.warn('[xhs_compet] users failed on', ep.id, e.status, e.message); } catch (e2) {}
        tryUser(idx + 1);
      });
    }
    tryUser(0);
  }

  function _setSearchUserMsg(text, isErr) {
    var el = document.getElementById('xhsCompetSearchUserMsg');
    if (!el) return;
    el.textContent = text || '';
    el.style.color = isErr ? '#ef4444' : 'var(--text-muted)';
  }

  function _firstParamName(params, candidates) {
    var names = (params || []).map(function(p) { return p.name; });
    for (var i = 0; i < candidates.length; i++) if (names.indexOf(candidates[i]) >= 0) return candidates[i];
    for (var j = 0; j < names.length; j++) {
      var ln = names[j].toLowerCase();
      for (var k = 0; k < candidates.length; k++) if (ln.indexOf(candidates[k].toLowerCase()) >= 0) return names[j];
    }
    return null;
  }

  /** 搜索类接口里关键词参数名常见 alias */
  var KW_NAMES = ['keyword', 'keywords', 'query', 'q', 'word', 'kw', 'search'];
  /** 翻页游标参数 alias（含小红书 web 接口的 lastCursor） */
  var CURSOR_NAMES = ['cursor', 'next_cursor', 'max_cursor', 'pcursor', 'lastCursor', 'last_cursor'];
  var PAGE_NAMES = ['page', 'page_num', 'pageNum', 'page_no'];
  var SIZE_NAMES = ['count', 'page_size', 'pageSize', 'size', 'num', 'limit'];

  function _renderUserSearchResult(users, queryName, queryRedId) {
    var box = document.getElementById('xhsCompetUserSearchResult');
    if (!box) return;
    if (!users.length) {
      box.innerHTML = '<div style="padding:0.75rem;background:rgba(0,0,0,0.15);border-radius:var(--radius-sm);color:var(--text-muted);font-size:0.82rem;">未搜到匹配的用户</div>';
      return;
    }
    var html = '<div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:0.4rem;">点击「保存」将该用户加入跟踪列表：</div>';
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:0.5rem;">';
    users.forEach(function(u, idx) {
      var existed = _state.records.filter(function(r) {
        return (u.user_id && r.user_id === u.user_id) || (u.sec_user_id && r.sec_user_id === u.sec_user_id);
      })[0];
      var btn = existed
        ? '<button type="button" class="btn btn-ghost btn-sm" disabled style="opacity:0.5;">已跟踪</button>'
        : '<button type="button" class="btn btn-primary btn-sm xhs-compet-save-user-btn" data-idx="' + idx + '">保存</button>';
      html += '<div style="display:flex;gap:0.5rem;padding:0.55rem;border:1px solid rgba(255,255,255,0.08);border-radius:var(--radius-sm);background:rgba(0,0,0,0.18);">'
        + (u.avatar ? '<img src="' + _esc(u.avatar) + '" referrerpolicy="no-referrer" style="width:48px;height:48px;border-radius:50%;object-fit:cover;flex-shrink:0;" onerror="this.style.display=\'none\'">' : '<div style="width:48px;height:48px;border-radius:50%;background:rgba(255,255,255,0.1);flex-shrink:0;"></div>')
        + '<div style="flex:1;min-width:0;font-size:0.8rem;line-height:1.5;">'
          + '<div style="font-weight:600;">' + _esc(u.nickname || '(无昵称)') + '</div>'
          + '<div style="color:var(--text-muted);font-size:0.72rem;">'
            + (u.red_id ? ((PLATFORM_CONFIGS[_currentPlatform] || {}).userIdLabel || '平台ID') + '：' + _esc(u.red_id) + ' · ' : '')
            + (u.fans != null && u.fans !== '' ? '粉丝 ' + _formatNumber(u.fans) : '')
          + '</div>'
          + '<div style="color:var(--text-muted);font-size:0.7rem;word-break:break-all;">user_id: ' + _esc(u.user_id || '-') + '</div>'
          + (u.signature ? '<div style="color:var(--text-muted);font-size:0.72rem;margin-top:2px;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;">' + _esc(u.signature) + '</div>' : '')
        + '</div>'
        + '<div style="align-self:flex-start;">' + btn + '</div>'
      + '</div>';
    });
    html += '</div>';
    box.innerHTML = html;

    box.querySelectorAll('.xhs-compet-save-user-btn').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var idx = parseInt(btn.dataset.idx, 10);
        var u = users[idx];
        if (!u) return;
        _saveCompetitorRecord(u, queryName, queryRedId);
        btn.disabled = true; btn.textContent = '已保存'; btn.style.opacity = '0.5';
        _renderTrackedList();
      });
    });
  }

  function _saveCompetitorRecord(user, queryName, queryRedId) {
    var key = _currentPlatform + '::' + (user.user_id || user.sec_user_id || user.nickname || Date.now());
    var existed = _state.records.filter(function(r) { return r.key === key; })[0];
    if (existed) return existed;
    var rec = {
      key: key,
      user_id: user.user_id || '',
      sec_user_id: user.sec_user_id || '',
      red_id: user.red_id || queryRedId || '',
      nickname: user.nickname || queryName || '',
      avatar: user.avatar || '',
      signature: user.signature || '',
      fans: user.fans || '',
      save_time: Date.now(),
    };
    _state.records.unshift(rec);
    _saveJson(_lsRecordsKey(), _state.records);
    return rec;
  }

  // -------------------------------------------------------------
  // Tab1：跟踪列表 + 同步笔记 + 查询评论
  // -------------------------------------------------------------
  function _renderTrackedList() {
    var box = document.getElementById('xhsCompetTrackedList');
    var cnt = document.getElementById('xhsCompetTrackedCount');
    if (cnt) cnt.textContent = _state.records.length;
    if (!box) return;
    if (!_state.records.length) {
      box.innerHTML = '<div style="padding:1rem;background:rgba(0,0,0,0.15);border-radius:var(--radius-sm);color:var(--text-muted);font-size:0.82rem;text-align:center;">尚未跟踪同行。在上方搜索后保存即可。</div>';
      return;
    }
    var html = '';
    _state.records.forEach(function(r) {
      var notesState = _loadJson(_lsNotesPrefix() + r.key, null);
      var noteCount = notesState && notesState.items ? notesState.items.length : 0;
      var fetchTime = notesState && notesState.last_time ? new Date(notesState.last_time).toLocaleString('zh-CN', { hour12: false }) : '';
      html += '<div class="xhs-compet-record" data-key="' + _esc(r.key) + '" style="border:1px solid rgba(255,255,255,0.08);border-radius:var(--radius-sm);padding:0.6rem;margin-bottom:0.6rem;background:rgba(0,0,0,0.15);">'
        + '<div style="display:flex;gap:0.5rem;align-items:flex-start;">'
        + (r.avatar ? '<img src="' + _esc(r.avatar) + '" referrerpolicy="no-referrer" style="width:42px;height:42px;border-radius:50%;object-fit:cover;flex-shrink:0;" onerror="this.style.display=\'none\'">' : '<div style="width:42px;height:42px;border-radius:50%;background:rgba(255,255,255,0.1);flex-shrink:0;"></div>')
        + '<div style="flex:1;min-width:0;font-size:0.82rem;line-height:1.55;">'
          + '<div style="font-weight:600;">' + _esc(r.nickname || '(无昵称)') + '</div>'
          + '<div style="color:var(--text-muted);font-size:0.72rem;">'
            + (r.red_id ? ((PLATFORM_CONFIGS[_currentPlatform] || {}).userIdLabel || '平台ID') + ' ' + _esc(r.red_id) + ' · ' : '')
            + 'user_id ' + _esc(r.user_id || '-')
          + '</div>'
          + '<div style="color:var(--text-muted);font-size:0.72rem;">已入库 ' + noteCount + ' 篇' + _postLabel() + (fetchTime ? ' · 上次同步 ' + fetchTime : '') + '</div>'
        + '</div>'
        + '<div style="display:flex;flex-direction:column;gap:0.3rem;flex-shrink:0;">'
          + '<button type="button" class="btn btn-primary btn-sm xhs-compet-sync-notes-btn" data-key="' + _esc(r.key) + '">同步作品入库</button>'
          + '<button type="button" class="btn btn-ghost btn-sm xhs-compet-toggle-notes-btn" data-key="' + _esc(r.key) + '">查看' + _postLabel() + '</button>'
          + '<button type="button" class="btn btn-ghost btn-sm xhs-compet-remove-btn" data-key="' + _esc(r.key) + '" style="color:#ef4444;">删除</button>'
        + '</div>'
      + '</div>'
      + '<div class="xhs-compet-record-msg" style="margin-top:0.4rem;font-size:0.74rem;color:var(--text-muted);"></div>'
      + '<div class="xhs-compet-notes-pane" style="margin-top:0.5rem;display:none;"></div>'
    + '</div>';
    });
    box.innerHTML = html;

    box.querySelectorAll('.xhs-compet-sync-notes-btn').forEach(function(btn) {
      btn.addEventListener('click', function() { _syncUserNotes(btn.dataset.key, false); });
    });
    box.querySelectorAll('.xhs-compet-toggle-notes-btn').forEach(function(btn) {
      btn.addEventListener('click', function() { _toggleNotesPane(btn.dataset.key); });
    });
    box.querySelectorAll('.xhs-compet-remove-btn').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var key = btn.dataset.key;
        var r = _state.records.filter(function(x) { return x.key === key; })[0];
        if (!r) return;
        if (!window.confirm('删除「' + (r.nickname || key) + '」？已同步的' + _postLabel() + '/评论缓存也会一并删除。')) return;
        _state.records = _state.records.filter(function(x) { return x.key !== key; });
        _saveJson(_lsRecordsKey(), _state.records);
        try { localStorage.removeItem(_lsNotesPrefix() + key); } catch (e) {}
        _renderTrackedList();
      });
    });
  }

  function _toggleNotesPane(key) {
    var card = document.querySelector('.xhs-compet-record[data-key="' + CSS.escape(key) + '"]');
    if (!card) return;
    var pane = card.querySelector('.xhs-compet-notes-pane');
    if (!pane) return;
    if (pane.style.display === 'none') {
      pane.style.display = '';
      _renderNotesPane(key);
    } else {
      pane.style.display = 'none';
    }
  }

  function _setRecordMsg(key, text, isErr) {
    var card = document.querySelector('.xhs-compet-record[data-key="' + CSS.escape(key) + '"]');
    if (!card) return;
    var msg = card.querySelector('.xhs-compet-record-msg');
    if (!msg) return;
    msg.textContent = text || '';
    msg.style.color = isErr ? '#ef4444' : 'var(--text-muted)';
  }

  function _syncUserNotes(key, append) {
    var rec = _state.records.filter(function(r) { return r.key === key; })[0];
    if (!rec) return;
    var allAlts = _state.endpointAlts.userPosts || [];
    if (!allAlts.length) { _setRecordMsg(key, '用户作品接口未就绪', true); return; }

    var notesState = _loadJson(_lsNotesPrefix() + key, { items: [], cursor: null, hasMore: false, last_time: 0, endpointId: null });
    if (!append) notesState = { items: [], cursor: null, hasMore: false, last_time: 0, endpointId: null };

    var lastEpId = append ? notesState.endpointId : null;
    var ordered = (lastEpId ? allAlts.filter(function(e) { return e.id === lastEpId; }) : [])
      .concat(allAlts.filter(function(e) { return e.id !== lastEpId; }));

    function buildParams(ep) {
      var rule = _inferPaging(ep);
      var pdef = ep.params || [];
      var params = {};
      var uidName = _firstParamName(pdef, ['user_id', 'userId', 'sec_user_id', 'sec_uid']);
      if (!uidName) return null;
      if (/sec/i.test(uidName)) params[uidName] = rec.sec_user_id || rec.user_id;
      else params[uidName] = rec.user_id || rec.sec_user_id;
      var secName = _firstParamName(pdef, ['sec_user_id', 'sec_uid']);
      if (secName && secName !== uidName && rec.sec_user_id) params[secName] = rec.sec_user_id;
      if (rule.mode === 'cursor' && append && ep.id === notesState.endpointId && notesState.cursor) {
        params[rule.inParam] = String(notesState.cursor);
      } else if (rule.mode === 'page') {
        var nextPage = (append && ep.id === notesState.endpointId) ? ((notesState.page || 0) + 1) : 1;
        params[rule.inParam] = nextPage;
        params.__nextPage = nextPage;
      }
      return params;
    }

    _setRecordMsg(key, append ? '加载下一页…' : '同步中…', false);

    function tryAt(idx) {
      if (idx >= ordered.length) {
        _setRecordMsg(key, append
          ? '所有备选接口都翻不到下一页（已入库 ' + notesState.items.length + ' 篇）'
          : '所有备选接口都失败或无作品', true);
        return;
      }
      var ep = ordered[idx];
      var params = buildParams(ep);
      if (!params) { tryAt(idx + 1); return; }
      var nextPage = params.__nextPage; delete params.__nextPage;
      if (idx > 0) _setRecordMsg(key, (append ? '加载下一页…' : '同步中…') + '（自动尝试 ' + ep.id + '）', false);
      try { console.debug('[xhs_compet] notes call', { ep: ep.id, append: !!append, params: params }); } catch (e) {}
      _callEndpoint(ep, params).then(function(payload) {
        var info = _readPaging(payload, ep);
        var rule = _inferPaging(ep);
        var notes = (info.list || []).map(_normalizeNote).filter(function(n) { return n && n.note_id; });
        try { console.debug('[xhs_compet] notes resp', { ep: ep.id, listLen: notes.length, cursor: _extractCursor(payload, ep.id) }); } catch (e) {}
        if (append && notesState.endpointId && ep.id !== notesState.endpointId) {
          notesState.items = []; notesState.cursor = null; notesState.page = 0;
        }
        var seen = {}; notesState.items.forEach(function(x) { seen[x.note_id] = true; });
        var added = 0;
        notes.forEach(function(n) { if (!seen[n.note_id]) { notesState.items.push(n); seen[n.note_id] = true; added++; } });
        if (added === 0) { tryAt(idx + 1); return; }
        notesState.endpointId = ep.id;
        if (rule.mode === 'page') {
          notesState.page = nextPage || 1;
          notesState.cursor = null;
          notesState.hasMore = notes.length >= 10;
        } else {
          var nc = _extractCursor(payload, ep.id);
          notesState.cursor = nc;
          notesState.hasMore = !!nc || _readHasMore(payload);
        }
        notesState.last_time = Date.now();
        _saveJson(_lsNotesPrefix() + key, notesState);
        _renderTrackedList();
        _setRecordMsg(key, '本次 +' + added + ' · 累计 ' + notesState.items.length + ' 篇' + (notesState.hasMore ? ' · 还有更多' : ' · 已到末尾'), false);
        var card2 = document.querySelector('.xhs-compet-record[data-key="' + CSS.escape(key) + '"]');
        if (card2) {
          var pane2 = card2.querySelector('.xhs-compet-notes-pane');
          if (pane2) { pane2.style.display = ''; _renderNotesPane(key); }
        }
      }).catch(function(e) {
        try { console.warn('[xhs_compet] notes failed on', ep.id, e.status, e.message); } catch (e2) {}
        tryAt(idx + 1);
      });
    }
    tryAt(0);
  }

  function _renderNotesPane(key) {
    var card = document.querySelector('.xhs-compet-record[data-key="' + CSS.escape(key) + '"]');
    if (!card) return;
    var pane = card.querySelector('.xhs-compet-notes-pane');
    if (!pane) return;
    var notesState = _loadJson(_lsNotesPrefix() + key, { items: [], cursor: null, hasMore: false });
    if (!notesState.items.length) {
      pane.innerHTML = '<div style="padding:0.6rem;background:rgba(0,0,0,0.2);border-radius:var(--radius-sm);color:var(--text-muted);font-size:0.78rem;">尚未同步' + _postLabel() + '，点「同步作品入库」拉取一页</div>';
      return;
    }
    var hasMoreBtn = notesState.hasMore
      ? '<button type="button" class="btn btn-ghost btn-sm xhs-compet-notes-next-btn" data-key="' + _esc(key) + '">下一页 →</button>'
      : '<span style="font-size:0.74rem;color:var(--text-muted);">已加载完</span>';
    var html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.4rem;">'
      + '<span style="font-size:0.76rem;color:var(--text-muted);">共 ' + notesState.items.length + ' 篇' + _postLabel() + '</span>'
      + hasMoreBtn
    + '</div>';
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:0.5rem;">';
    notesState.items.forEach(function(n) {
      html += _renderNoteCard(n, { showCommentBtn: true, ownerKey: key });
    });
    html += '</div>';
    pane.innerHTML = html;

    pane.querySelectorAll('.xhs-compet-notes-next-btn').forEach(function(btn) {
      btn.addEventListener('click', function() { _syncUserNotes(btn.dataset.key, true); });
    });
    _bindNoteCommentButtons(pane);
  }

  // -------------------------------------------------------------
  // 笔记卡片 + 评论交互
  // -------------------------------------------------------------
  function _renderNoteCard(n, opts) {
    opts = opts || {};
    var commentBtn = opts.showCommentBtn
      ? '<button type="button" class="btn btn-ghost btn-sm xhs-compet-load-comment-btn" data-note-id="' + _esc(n.note_id) + '" data-xsec="' + _esc(n.xsec_token || '') + '" style="font-size:0.74rem;">查询评论</button>'
      : '';
    var stats = '<span title="点赞">❤ ' + _formatNumber(n.like_count) + '</span>'
      + ' · <span title="评论">💬 ' + _formatNumber(n.comment_count) + '</span>'
      + ' · <span title="收藏">★ ' + _formatNumber(n.collect_count) + '</span>';
    var displayTitle = (n.title || n.desc || '(无标题)').slice(0, 100);
    return '<div class="xhs-compet-note-card" data-note-id="' + _esc(n.note_id) + '" style="display:flex;flex-direction:column;border:1px solid rgba(255,255,255,0.08);border-radius:var(--radius-sm);background:rgba(0,0,0,0.18);overflow:hidden;">'
      + (n.cover ? '<img src="' + _esc(n.cover) + '" referrerpolicy="no-referrer" loading="lazy" style="width:100%;aspect-ratio:1/1;object-fit:cover;background:#222;" onerror="this.style.display=\'none\'">' : '')
      + '<div style="padding:0.5rem 0.6rem;font-size:0.78rem;line-height:1.45;">'
        + '<div style="font-weight:600;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;">' + _esc(displayTitle) + '</div>'
        + '<div style="color:var(--text-muted);font-size:0.7rem;margin-top:3px;">' + _esc(n.author && n.author.nickname || '') + (n.time ? ' · ' + _esc(_formatTime(n.time)) : '') + '</div>'
        + '<div style="color:var(--text-muted);font-size:0.72rem;margin-top:3px;">' + stats + '</div>'
        + '<div style="margin-top:6px;display:flex;justify-content:space-between;align-items:center;">'
          + '<code style="font-size:0.65rem;background:rgba(99,102,241,0.15);padding:1px 4px;border-radius:3px;color:var(--text-muted);" title="' + _esc(n.note_id) + '">' + _esc(n.note_id.slice(0, 14)) + '…</code>'
          + commentBtn
        + '</div>'
      + '</div>'
      + '<div class="xhs-compet-comment-pane" style="display:none;border-top:1px solid rgba(255,255,255,0.08);padding:0.4rem 0.6rem;background:rgba(0,0,0,0.25);"></div>'
    + '</div>';
  }

  function _bindNoteCommentButtons(scope) {
    scope.querySelectorAll('.xhs-compet-load-comment-btn').forEach(function(btn) {
      btn.addEventListener('click', function() {
        _loadNoteComments(btn.dataset.noteId, btn.dataset.xsec || '', false);
      });
    });
  }

  function _loadNoteComments(noteId, xsecToken, append) {
    if (!noteId) return;
    var allAlts = _state.endpointAlts.postComments || [];
    if (!allAlts.length) { window.alert('作品评论接口未就绪'); return; }
    var card = document.querySelector('.xhs-compet-note-card[data-note-id="' + CSS.escape(noteId) + '"]');
    if (!card) return;
    var pane = card.querySelector('.xhs-compet-comment-pane');
    if (!pane) return;
    pane.style.display = '';

    var ck = _lsCommentsPrefix() + noteId;
    var st = _loadJson(ck, { items: [], cursor: null, hasMore: false, endpointId: null });
    if (!append) st = { items: [], cursor: null, hasMore: false, endpointId: null };

    /* 翻页时优先沿用上次成功的 ep；首次/失败时按 allAlts 顺序自动找一个能拉到的 */
    var lastEpId = append ? st.endpointId : null;
    var ordered = (lastEpId ? allAlts.filter(function(e) { return e.id === lastEpId; }) : [])
      .concat(allAlts.filter(function(e) { return e.id !== lastEpId; }));

    function buildParams(ep) {
      var rule = _inferPaging(ep);
      var pdef = ep.params || [];
      var params = {};
      var nidName = _firstParamName(pdef, ['note_id', 'noteId', 'aweme_id', 'video_id', 'item_id', 'post_id', 'id']);
      if (!nidName) return null;
      params[nidName] = noteId;
      var xsecName = _firstParamName(pdef, ['xsec_token']);
      if (xsecName && xsecToken) params[xsecName] = xsecToken;
      if (rule.mode === 'cursor' && append && ep.id === st.endpointId && st.cursor) {
        params[rule.inParam] = String(st.cursor);
      } else if (rule.mode === 'page') {
        var nextPage = (append && ep.id === st.endpointId) ? ((st.page || 0) + 1) : 1;
        params[rule.inParam] = nextPage;
        params.__nextPage = nextPage;
      }
      return params;
    }

    pane.innerHTML = '<div style="font-size:0.74rem;color:var(--text-muted);padding:6px 0;">' + (append ? '加载下一页…' : '加载评论中…') + '</div>';

    function tryAt(idx) {
      if (idx >= ordered.length) {
        var msg = append
          ? '所有备选接口都翻不到下一页（已加载 ' + st.items.length + ' 条评论）'
          : '所有备选接口都失败或返回 0 条评论';
        if (st.items.length) {
          /* 续页失败但已有数据 → 仍展示，只是不能再翻 */
          st.hasMore = false; _saveJson(ck, st);
          _renderCommentsPane(pane, noteId, xsecToken, st, _findEpById(allAlts, st.endpointId));
          var hint = pane.querySelector('.xhs-compet-comment-final-hint');
          if (!hint) {
            var d = document.createElement('div');
            d.className = 'xhs-compet-comment-final-hint';
            d.style.cssText = 'font-size:0.7rem;color:#ef4444;padding:4px 0;';
            d.textContent = msg;
            pane.appendChild(d);
          }
        } else {
          pane.innerHTML = '<div style="font-size:0.74rem;color:#ef4444;padding:6px 0;">' + _esc(msg) + '</div>';
        }
        return;
      }
      var ep = ordered[idx];
      var params = buildParams(ep);
      if (!params) { tryAt(idx + 1); return; }
      var nextPage = params.__nextPage; delete params.__nextPage;
      if (idx > 0 && pane) {
        pane.innerHTML = '<div style="font-size:0.74rem;color:var(--text-muted);padding:6px 0;">' + (append ? '加载下一页…' : '加载评论中…') + '（自动尝试 ' + _esc(ep.id) + '）</div>';
      }
      var _cpRule = _inferPaging(ep);
      var sentCursor = _cpRule.inParam ? params[_cpRule.inParam] : null;
      try { console.debug('[xhs_compet] comments call', { ep: ep.id, append: !!append, params: params, sentCursor: sentCursor }); } catch (e) {}
      _callEndpoint(ep, params).then(function(payload) {
        var info = _readPaging(payload, ep);
        var rule = _inferPaging(ep);
        var comments = (info.list || []).map(_normalizeComment).filter(function(c) { return c && (c.content || c.comment_id); });
        var nc = _extractCursor(payload, ep.id);
        var diag = null;
        try { diag = _diagPagingFields(payload); } catch (e) {}
        try {
          console.debug('[xhs_compet] comments resp', {
            ep: ep.id,
            listLen: comments.length,
            extractedCursor: nc,
            sentCursor: sentCursor,
            allCursorFields: diag && diag.cursors,
            allMoreFields: diag && diag.more,
          });
        } catch (e) {}

        /* 切换 ep 时（append 状态下 ep 不同）丢弃旧累积 */
        if (append && st.endpointId && ep.id !== st.endpointId) {
          st.items = []; st.cursor = null; st.page = 0;
        }
        var seen = {}; st.items.forEach(function(x) { seen[x.comment_id] = true; });
        var addedItems = [];
        comments.forEach(function(c) {
          if (!seen[c.comment_id]) { st.items.push(c); seen[c.comment_id] = true; addedItems.push(c); }
        });
        var added = addedItems.length;

        /* 翻页时识别「伪新增」：返回的 comment 跟已有项内容（content+author）几乎相同 → 视为同一页重传 */
        if (append && added > 0 && st.items.length - added > 0) {
          var prevSet = {};
          st.items.slice(0, st.items.length - added).forEach(function(x) {
            var sig = (x.content || '') + '|' + (x.author && x.author.nickname || '') + '|' + (x.time || '');
            prevSet[sig] = true;
          });
          var dupCount = 0;
          addedItems.forEach(function(c) {
            var sig = (c.content || '') + '|' + (c.author && c.author.nickname || '') + '|' + (c.time || '');
            if (prevSet[sig]) dupCount++;
          });
          if (dupCount / added >= 0.6) {
            try { console.warn('[xhs_compet] comments duplicate content detected, rollback + fallback', { ep: ep.id, dup: dupCount, added: added }); } catch (e3) {}
            st.items = st.items.slice(0, st.items.length - added);
            tryAt(idx + 1); return;
          }
        }

        /* 0 条新增 → 该接口翻页失败，自动切下一个 */
        if (added === 0) {
          try { console.warn('[xhs_compet] comments 0 added, fallback', { ep: ep.id, sentCursor: sentCursor, extractedCursor: nc }); } catch (e2) {}
          tryAt(idx + 1); return;
        }

        /* cursor 模式：取不到非空 cursor 或新 cursor 与本次发出的 cursor 相同 → hasMore=false（防止下次又拉同一页） */
        var pagingDead = false;
        if (rule.mode === 'cursor') {
          if (!nc) pagingDead = true;
          else if (sentCursor && String(nc) === String(sentCursor)) pagingDead = true;
        }

        st.endpointId = ep.id;
        if (rule.mode === 'page') {
          st.page = nextPage || 1;
          st.cursor = null;
          st.hasMore = comments.length >= 10;
        } else {
          st.cursor = nc;
          st.hasMore = !pagingDead && (!!nc || _readHasMore(payload));
        }
        _saveJson(ck, st);
        _renderCommentsPane(pane, noteId, xsecToken, st, ep);
      }).catch(function(e) {
        try { console.warn('[xhs_compet] comments failed on', ep.id, e.status, e.message); } catch (e2) {}
        tryAt(idx + 1);
      });
    }
    tryAt(0);
  }

  function _findEpById(alts, id) {
    if (!id) return null;
    for (var i = 0; i < (alts || []).length; i++) if (alts[i].id === id) return alts[i];
    return null;
  }

  function _renderCommentsPane(pane, noteId, xsecToken, st, ep) {
    var epLabel = ep ? ep.id : '-';
    if (!st.items.length) {
      pane.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">'
        + '<span style="font-size:0.72rem;color:var(--text-muted);">没有评论</span>'
      + '</div>';
      return;
    }
    var html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;flex-wrap:wrap;gap:4px;">'
      + '<span style="font-size:0.72rem;color:var(--text-muted);">共 ' + st.items.length + ' 条 · <code style="font-size:0.66rem;opacity:0.7;">' + _esc(epLabel) + '</code></span>'
      + (st.hasMore
        ? '<button type="button" class="btn btn-ghost btn-sm xhs-compet-comment-next-btn" data-note-id="' + _esc(noteId) + '" data-xsec="' + _esc(xsecToken || '') + '" style="font-size:0.72rem;">下一页 →</button>'
        : '<span style="font-size:0.72rem;color:var(--text-muted);">已加载完</span>')
    + '</div>';
    html += '<div style="display:flex;flex-direction:column;gap:6px;max-height:280px;overflow:auto;">';
    st.items.forEach(function(c) {
      html += '<div style="display:flex;gap:6px;font-size:0.74rem;line-height:1.4;">'
        + (c.author && c.author.avatar ? '<img src="' + _esc(c.author.avatar) + '" referrerpolicy="no-referrer" style="width:24px;height:24px;border-radius:50%;object-fit:cover;flex-shrink:0;" onerror="this.style.display=\'none\'">' : '')
        + '<div style="flex:1;min-width:0;">'
          + '<div><span style="color:var(--accent);">' + _esc(c.author && c.author.nickname || '匿名') + '</span> '
            + '<span style="color:var(--text-muted);font-size:0.7rem;">' + _esc(_formatTime(c.time)) + (c.ip ? ' · ' + _esc(c.ip) : '') + '</span></div>'
          + '<div style="word-break:break-all;">' + _esc(c.content) + '</div>'
          + '<div style="font-size:0.68rem;color:var(--text-muted);margin-top:2px;">❤ ' + _formatNumber(c.like_count) + (c.sub_count ? ' · 子评论 ' + _formatNumber(c.sub_count) : '') + '</div>'
        + '</div>'
      + '</div>';
    });
    html += '</div>';
    pane.innerHTML = html;
    pane.querySelectorAll('.xhs-compet-comment-next-btn').forEach(function(btn) {
      btn.addEventListener('click', function() { _loadNoteComments(btn.dataset.noteId, btn.dataset.xsec || '', true); });
    });
  }

  // -------------------------------------------------------------
  // Tab2：关键词搜索笔记
  // -------------------------------------------------------------
  function _onSearchKeyword(append) {
    var kwInput = document.getElementById('xhsCompetKeywordInput');
    var kw = (kwInput && kwInput.value || '').trim();
    if (!kw) { _setKeywordMsg('请输入关键词', true); return; }
    var allAlts = _state.endpointAlts.searchPosts || [];
    if (!allAlts.length) { _setKeywordMsg('搜索' + _postLabel() + '接口未就绪', true); return; }

    if (!append || _state.keyword.keyword !== kw) {
      _state.keyword = { keyword: kw, items: [], page: 0, hasMore: false, cursor: null, endpointId: null };
    }
    /* 翻页时优先沿用上次成功的 ep；首次/失败时按 allAlts 顺序自动找一个能拉到结果的 */
    var lastEpId = append ? _state.keyword.endpointId : null;
    var ordered = (lastEpId ? allAlts.filter(function(e) { return e.id === lastEpId; }) : [])
      .concat(allAlts.filter(function(e) { return e.id !== lastEpId; }));

    function buildParams(ep) {
      var rule = _inferPaging(ep);
      var pdef = ep.params || [];
      var params = {};
      var kwName = _firstParamName(pdef, KW_NAMES);
      if (!kwName) return null;
      params[kwName] = kw;
      if (rule.mode === 'page') {
        var nextPage = (append && ep.id === _state.keyword.endpointId) ? ((_state.keyword.page || 0) + 1) : 1;
        params[rule.inParam] = nextPage;
        params.__nextPage = nextPage;
      } else if (rule.mode === 'cursor') {
        if (append && ep.id === _state.keyword.endpointId && _state.keyword.cursor) {
          params[rule.inParam] = String(_state.keyword.cursor);
        }
      }
      return params;
    }

    _setKeywordMsg(append ? '加载下一页…' : '搜索中…', false);

    function tryAt(idx) {
      if (idx >= ordered.length) {
        _setKeywordMsg(append
          ? '所有备选接口都翻不到下一页（已加载 ' + _state.keyword.items.length + ' 条，可能就这么多）'
          : '所有备选接口都失败或返回 0 条，请稍后重试或换关键词', true);
        _renderKeywordResult();
        return;
      }
      var ep = ordered[idx];
      var params = buildParams(ep);
      if (!params) { tryAt(idx + 1); return; }
      var nextPage = params.__nextPage; delete params.__nextPage;
      if (idx > 0) _setKeywordMsg((append ? '加载下一页…' : '搜索中…') + '（自动尝试 ' + ep.id + '）', false);
      try { console.debug('[xhs_compet] kw call', { ep: ep.id, append: !!append, params: params }); } catch (e) {}
      _callEndpoint(ep, params).then(function(payload) {
        var info = _readPaging(payload, ep);
        var notes = (info.list || []).map(_normalizeNote).filter(function(n) { return n && n.note_id; });
        var rule = _inferPaging(ep);
        try { console.debug('[xhs_compet] kw resp', { ep: ep.id, listLen: notes.length, cursor: _extractCursor(payload, ep.id) }); } catch (e) {}
        /* 自动切了接口后，旧 items 与新接口 ordering 不兼容 → 续页时丢弃 */
        if (append && _state.keyword.endpointId && ep.id !== _state.keyword.endpointId) {
          _state.keyword.items = [];
          _state.keyword.page = 0;
          _state.keyword.cursor = null;
        }
        var seen = {};
        _state.keyword.items.forEach(function(x) { seen[x.note_id] = true; });
        var added = 0;
        notes.forEach(function(n) { if (!seen[n.note_id]) { _state.keyword.items.push(n); seen[n.note_id] = true; added++; } });
        if (added === 0) { tryAt(idx + 1); return; }
        _state.keyword.endpointId = ep.id;
        if (rule.mode === 'page') {
          _state.keyword.page = nextPage || 1;
          _state.keyword.cursor = null;
          _state.keyword.hasMore = notes.length >= 10;
        } else {
          var nc = _extractCursor(payload, ep.id);
          _state.keyword.cursor = nc;
          _state.keyword.hasMore = !!nc || _readHasMore(payload);
        }
        _saveJson(_lsKeywordKey(), _state.keyword);
        _setKeywordMsg('本次 +' + added + ' · 累计 ' + _state.keyword.items.length + ' 条' + (_state.keyword.hasMore ? ' · 还有更多' : ' · 已到末尾'), false);
        _renderKeywordResult();
      }).catch(function(e) {
        try { console.warn('[xhs_compet] kw failed on', ep.id, e.status, e.message); } catch (e2) {}
        tryAt(idx + 1);
      });
    }
    tryAt(0);
  }

  function _setKeywordMsg(text, isErr) {
    var el = document.getElementById('xhsCompetSearchNoteMsg');
    if (!el) return;
    el.textContent = text || '';
    el.style.color = isErr ? '#ef4444' : 'var(--text-muted)';
  }

  function _renderKeywordResult() {
    var box = document.getElementById('xhsCompetKeywordNoteList');
    var sumEl = document.getElementById('xhsCompetKeywordSummary');
    var nextBtn = document.getElementById('xhsCompetKeywordNextPageBtn');
    if (!box) return;
    var st = _state.keyword || { items: [] };
    if (sumEl) sumEl.textContent = st.items.length ? ('「' + st.keyword + '」 · 第 ' + st.page + ' 页 · 累计 ' + st.items.length + ' 条' + (st.hasMore ? ' · 还有更多' : '')) : '';
    if (nextBtn) nextBtn.disabled = !(st.hasMore && st.items.length);
    if (!st.items.length) {
      box.innerHTML = '<div style="padding:0.75rem;background:rgba(0,0,0,0.15);border-radius:var(--radius-sm);color:var(--text-muted);font-size:0.82rem;">输入关键词后点「搜索' + _postLabel() + '」</div>';
      return;
    }
    var html = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:0.5rem;">';
    st.items.forEach(function(n) { html += _renderNoteCard(n, { showCommentBtn: true }); });
    html += '</div>';
    box.innerHTML = html;
    _bindNoteCommentButtons(box);
  }

  // -------------------------------------------------------------
  // Tab 切换 + 事件绑定
  // -------------------------------------------------------------
  function _switchTab(name) {
    document.querySelectorAll('.xhs-compet-tab').forEach(function(b) {
      var on = b.dataset.xcTab === name;
      b.classList.toggle('active', on);
      b.style.background = on ? 'rgba(99,102,241,0.18)' : 'none';
      b.style.borderColor = on ? 'rgba(99,102,241,0.55)' : 'rgba(255,255,255,0.15)';
    });
    document.getElementById('xhsCompetTabTrack').style.display = (name === 'track') ? '' : 'none';
    document.getElementById('xhsCompetTabKeyword').style.display = (name === 'keyword') ? '' : 'none';
  }

  function _bindOnce() {
    var back = document.getElementById('xhsCompetBackBtn');
    if (!back) return;
    back.addEventListener('click', _backToStore);
    document.getElementById('xhsCompetReloadCatalogBtn').addEventListener('click', function() { _ensureCatalog(true); });

    var platSel = document.getElementById('xhsCompetPlatformSel');
    if (platSel) {
      platSel.addEventListener('change', function() { _switchPlatform(platSel.value); });
    }
    document.querySelectorAll('.xhs-compet-tab').forEach(function(b) {
      b.addEventListener('click', function() { _switchTab(b.dataset.xcTab); });
    });
    _switchTab('track');

    document.getElementById('xhsCompetSearchUserBtn').addEventListener('click', _onSearchUsers);
    document.getElementById('xhsCompetNameInput').addEventListener('keydown', function(e) { if (e.key === 'Enter') _onSearchUsers(); });
    document.getElementById('xhsCompetRedIdInput').addEventListener('keydown', function(e) { if (e.key === 'Enter') _onSearchUsers(); });

    document.getElementById('xhsCompetSearchNoteBtn').addEventListener('click', function() { _onSearchKeyword(false); });
    document.getElementById('xhsCompetKeywordInput').addEventListener('keydown', function(e) { if (e.key === 'Enter') _onSearchKeyword(false); });
    document.getElementById('xhsCompetKeywordNextPageBtn').addEventListener('click', function() { _onSearchKeyword(true); });

    document.getElementById('xhsCompetClearAllBtn').addEventListener('click', function() {
      if (!_state.records.length) return;
      if (!window.confirm('确认清空所有跟踪同行？同步过的' + _postLabel() + '/评论缓存也会全部删除。')) return;
      _state.records.forEach(function(r) { try { localStorage.removeItem(_lsNotesPrefix() + r.key); } catch (e) {} });
      _state.records = [];
      _saveJson(_lsRecordsKey(), _state.records);
      _renderTrackedList();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _bindOnce);
  } else {
    _bindOnce();
  }
})();
