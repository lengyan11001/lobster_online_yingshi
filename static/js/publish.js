// ── Publish Management (发布管理) ─────────────────────────────────

var _currentPubTab = 'accounts';

document.querySelectorAll('.pub-tab').forEach(function(tab) {
  tab.addEventListener('click', function() {
    if (typeof window.closeAllPublishModals === 'function') window.closeAllPublishModals();
    var target = tab.getAttribute('data-pub-tab');
    if (!target || target === _currentPubTab) return;
    _currentPubTab = target;
    document.querySelectorAll('.pub-tab').forEach(function(t) { t.classList.remove('active'); });
    tab.classList.add('active');
    document.getElementById('pubTabAccounts').style.display = (target === 'accounts') ? '' : 'none';
    document.getElementById('pubTabAssets').style.display = (target === 'assets') ? '' : 'none';
    document.getElementById('pubTabTasks').style.display = (target === 'tasks') ? '' : 'none';
    if (target === 'accounts') {
      hideAccountDetailPanel();
      loadAccounts();
    }
    if (target === 'assets') loadAssets();
    if (target === 'tasks') loadTasks();
  });
});

var PLATFORM_NAMES = { douyin: '抖音', bilibili: 'B站', xiaohongshu: '小红书', kuaishou: '快手', toutiao: '今日头条', douyin_shop: '抖店', xiaohongshu_shop: '小红书店铺', alibaba1688: '1688', taobao: '淘宝', pinduoduo: '拼多多' };
var STATUS_LABELS = { active: '已登录', pending: '待登录', error: '异常' };
var STATUS_COLORS = { active: '#34d399', pending: '#fb923c', error: '#f87171' };

/** 发布/素材/创作者同步须走本机 lobster_online（LOCAL_API_BASE），勿用公网 API_BASE */
function publishLocalBase() {
  var b = (typeof LOCAL_API_BASE !== 'undefined' && LOCAL_API_BASE) ? String(LOCAL_API_BASE).replace(/\/$/, '') : '';
  return b;
}

/** 解析 fetch 响应：静态服返回 HTML 时给出可操作的报错 */
function _publishParseResponse(r) {
  return r.text().then(function(text) {
    var d = {};
    try {
      d = text ? JSON.parse(text) : {};
    } catch (e1) {
      var hint = 'HTTP ' + r.status;
      if (text && (/<!DOCTYPE/i.test(text) || /<html/i.test(text))) {
        hint += '：未打到本机 lobster_online 后端（架构见 docs/架构说明_server与本地职责.md）。请在本机执行 LOBSTER_EDITION=online python3 backend/run.py；若后端与静态不同端口，用 ?local_api= 或 localStorage.lobster_local_api_base 指定后端根 URL';
      } else if (text) {
        hint += '（非 JSON）：' + text.slice(0, 200);
      } else {
        hint += '（空响应）';
      }
      return Promise.reject(new Error(hint));
    }
    return { ok: r.ok, status: r.status, d: d };
  });
}

// ── Accounts ─────────────────────────────────────────────────────

var _allAccounts = [];
var _detailAccountId = null;
var _schModalAccountId = null;
var _schTasksAccountId = null;
var _schTasksPollTimer = null;
var _creatorDefaultTtlSec = 3600;
var _detailScheduleCache = null;
/** 审核发布子 Tab：current | history */
var _detailReviewSubTab = 'current';

function _formatScheduleIntervalMinutes(m) {
  m = parseInt(m, 10);
  if (!m || m < 1) m = 60;
  if (m % 1440 === 0 && m >= 1440) return '每' + (m / 1440) + '天';
  if (m % 60 === 0 && m >= 60) return '每' + (m / 60) + '小时';
  return '每' + m + '分钟';
}

function _scheduleKindLabel(kind) {
  return kind === 'video' ? '视频' : '图文';
}

function _scheduleVideoBranchHint(sch) {
  if (!sch || sch.schedule_kind !== 'video') return '';
  var aid = (sch.video_source_asset_id || '').trim();
  return aid ? '图生视频' : '文生视频';
}

function _schUpdateScheduleKindUI() {
  var sel = document.getElementById('schScheduleKind');
  var wrap = document.getElementById('schVideoAssetWrap');
  var lbl = document.getElementById('schRequirementsLabel');
  var hint = document.getElementById('schRequirementsHint');
  if (!sel || !wrap || !lbl || !hint) return;
  var isVideo = sel.value === 'video';
  wrap.style.display = isVideo ? '' : 'none';
  if (isVideo) {
    lbl.textContent = '生产要求';
    hint.textContent = '参考图请填「素材 ID」；正文按提纲写：模型、画面方向、生成素材、发布文案、是否发布（见上方说明）。';
  } else {
    lbl.textContent = '描述需求';
    hint.textContent = '可用上方提纲：模型、画面方向、生成素材、发布文案、是否发布；不需要的栏写「无」。';
  }
}

function _schUpdatePublishModeUI() {
  var pm = document.getElementById('schPublishMode');
  var wrap = document.getElementById('schReviewVariantWrap');
  var act = document.getElementById('schModalReviewActions');
  if (!pm) return;
  var isReview = pm.value === 'review';
  if (wrap) wrap.style.display = isReview ? '' : 'none';
  if (act) act.style.display = isReview ? '' : 'none';
}

/** 与「保存」弹窗相同的校验，供生成审核稿前 PUT 使用 */
function _buildSchedulePutBodyFromModal(msgEl) {
  var enabled = document.getElementById('schEnabled').checked;
  var intervalMinutes = _intervalMinutesFromModal();
  var req = document.getElementById('schRequirements').value || '';
  var skEl = document.getElementById('schScheduleKind');
  var scheduleKind = skEl && skEl.value === 'video' ? 'video' : 'image';
  var videoAssetId = '';
  if (scheduleKind === 'video') {
    var aEl = document.getElementById('schVideoAssetId');
    videoAssetId = ((aEl && aEl.value) || '').trim();
    if (videoAssetId.length > 64) {
      if (msgEl) {
        msgEl.textContent = '素材 ID 最长 64 字符。';
        msgEl.style.display = 'block';
        msgEl.className = 'msg err';
      }
      return { ok: false };
    }
  }
  if (intervalMinutes == null) {
    if (msgEl) {
      msgEl.textContent = '请填写有效间隔：数字 ≥1，合计不超过 10080 分钟（7 天）。';
      msgEl.style.display = 'block';
      msgEl.className = 'msg err';
    }
    return { ok: false };
  }
  var putBody = {
    enabled: enabled,
    interval_minutes: intervalMinutes,
    schedule_kind: scheduleKind,
    requirements_text: req || null
  };
  if (scheduleKind === 'video') {
    putBody.video_source_asset_id = videoAssetId || null;
  }
  var pmEl = document.getElementById('schPublishMode');
  var rvcEl = document.getElementById('schReviewVariantCount');
  if (pmEl) {
    putBody.schedule_publish_mode = pmEl.value === 'review' ? 'review' : 'immediate';
  }
  if (rvcEl && pmEl && pmEl.value === 'review') {
    putBody.review_variant_count = Math.max(1, Math.min(10, parseInt(rvcEl.value, 10) || 3));
  }
  return { ok: true, body: putBody };
}

function _parsePublishJsonResponse(r) {
  return r.text().then(function(text) {
    var d = {};
    try {
      d = text ? JSON.parse(text) : {};
    } catch (e1) {
      d = { detail: text ? text.slice(0, 600) : ('HTTP ' + r.status) };
    }
    return { ok: r.ok, status: r.status, data: d };
  });
}

function _setReviewGenBusy(busy) {
  document.querySelectorAll('[data-action="review-generate"]').forEach(function(b) {
    b.disabled = !!busy;
  });
  document.querySelectorAll('[data-action="review-generate-assets"]').forEach(function(b) {
    b.disabled = !!busy;
  });
}

/** 智能生成提示词开始前：清空当前草稿列表并显示等待（仅当前详情账号与列表一致时更新 DOM） */
function _reviewGenerateClearDraftsUi(accountId) {
  if (_detailAccountId !== accountId) return;
  if (_detailScheduleCache) {
    _detailScheduleCache.review_drafts_json = [];
  }
  var ac = _allAccounts.filter(function(a) { return a.id === accountId; })[0];
  if (ac && ac.creator_schedule) {
    ac.creator_schedule = Object.assign({}, ac.creator_schedule, { review_drafts_json: [] });
  }
  var host = document.getElementById('accountDetailReviewDraftsList');
  if (host) {
    host.innerHTML = '<p class="meta" style="margin:0;font-size:0.82rem;color:var(--text-muted);">正在重新生成提示词，请稍候…</p>';
  }
}

function _postReviewGenerate(accountId, variantCount) {
  _reviewGenerateClearDraftsUi(accountId);
  var msgEl = document.getElementById('accountDetailReviewMsg');
  var modalMsg = document.getElementById('schModalMsg');
  if (msgEl) {
    msgEl.textContent = '正在智能生成提示词，请稍候（可能需数分钟）…';
    msgEl.style.display = 'block';
    msgEl.style.color = 'var(--text-muted)';
  }
  return fetch(publishLocalBase() + '/api/accounts/' + accountId + '/creator-schedule/review-generate', {
    method: 'POST',
    headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
    body: JSON.stringify({ variant_count: variantCount })
  })
    .then(function(r) { return _parsePublishJsonResponse(r); })
    .then(function(x) {
      if (!x.ok) {
        var det = x.data && x.data.detail;
        var t = typeof det === 'string' ? det : JSON.stringify(det || x.data);
        if (msgEl) {
          msgEl.textContent = t;
          msgEl.style.display = 'block';
          msgEl.style.color = '#f87171';
        }
        if (modalMsg) {
          modalMsg.textContent = t;
          modalMsg.style.display = 'block';
          modalMsg.className = 'msg err';
        }
        return;
      }
      _detailScheduleCache = x.data;
      var ac = _allAccounts.filter(function(a) { return a.id === accountId; })[0];
      if (ac) {
        ac.creator_schedule = Object.assign({}, ac.creator_schedule || {}, x.data);
        if (_detailAccountId === accountId) _refreshDetailScheduleSummary(ac);
      }
      if (_detailAccountId === accountId) _detailApplyScheduleTabFields(x.data);
      var n = (x.data && x.data.review_drafts_json && x.data.review_drafts_json.length) ? x.data.review_drafts_json.length : 0;
      var okT = '已生成 ' + n + ' 条提示词草稿，可在下方编辑后再点「生成发布内容」。';
      if (msgEl) {
        msgEl.textContent = okT;
        msgEl.style.display = 'block';
        msgEl.style.color = '#86efac';
      }
      if (modalMsg) {
        modalMsg.textContent = okT;
        modalMsg.style.display = 'block';
        modalMsg.className = 'msg ok';
      }
      loadAccounts();
      _refreshReviewSnapshotsIfNeeded();
    })
    .catch(function() {
      if (msgEl) {
        msgEl.textContent = '请求失败（网络或服务异常）';
        msgEl.style.display = 'block';
        msgEl.style.color = '#f87171';
      }
      if (modalMsg) {
        modalMsg.textContent = '请求失败（网络或服务异常）';
        modalMsg.style.display = 'block';
        modalMsg.className = 'msg err';
      }
    });
}

function _handleReviewGenerateClick() {
  var base = publishLocalBase();
  var msgEl = document.getElementById('accountDetailReviewMsg');
  var modalMsg = document.getElementById('schModalMsg');
  if (!base) {
    var t0 = '未配置本机 API。请用本机运行 lobster_online 后端后从该地址打开页面（需 LOCAL_API_BASE / 同源）。';
    if (msgEl) {
      msgEl.textContent = t0;
      msgEl.style.display = 'block';
      msgEl.style.color = '#f87171';
    }
    if (modalMsg) {
      modalMsg.textContent = t0;
      modalMsg.style.display = 'block';
      modalMsg.className = 'msg err';
    }
    return;
  }
  var accountId = _detailAccountId || _schModalAccountId;
  if (!accountId) {
    var t1 = '请先进入账号详情或打开「完整配置」弹窗后再生成。';
    if (msgEl) {
      msgEl.textContent = t1;
      msgEl.style.display = 'block';
      msgEl.style.color = '#f87171';
    }
    alert(t1);
    return;
  }
  var modalMask = document.getElementById('creatorScheduleModal');
  var modalOpen = modalMask && modalMask.style.display === 'flex';
  var nEl = document.getElementById('accountDetailReviewVariantCount');
  if (modalOpen) {
    var rvcM = document.getElementById('schReviewVariantCount');
    if (rvcM) nEl = rvcM;
  }
  var n = Math.max(1, Math.min(10, parseInt(nEl && nEl.value, 10) || 3));
  _setReviewGenBusy(true);
  function afterPost() {
    _setReviewGenBusy(false);
  }
  if (modalOpen && _schModalAccountId === accountId) {
    var built = _buildSchedulePutBodyFromModal(modalMsg);
    if (!built.ok) {
      _setReviewGenBusy(false);
      return;
    }
    fetch(publishLocalBase() + '/api/accounts/' + accountId + '/creator-schedule', {
      method: 'PUT',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      body: JSON.stringify(built.body)
    })
      .then(function(r) { return _parsePublishJsonResponse(r); })
      .then(function(x) {
        if (!x.ok) {
          var det = x.data && x.data.detail;
          var te = typeof det === 'string' ? det : JSON.stringify(det || x.data);
          if (modalMsg) {
            modalMsg.textContent = te;
            modalMsg.style.display = 'block';
            modalMsg.className = 'msg err';
          }
          return;
        }
        if (_detailAccountId === accountId && x.data) {
          _detailScheduleCache = Object.assign({}, x.data, { review_drafts_json: [] });
          var ac = _allAccounts.filter(function(a) { return a.id === accountId; })[0];
          if (ac) {
            ac.creator_schedule = Object.assign({}, ac.creator_schedule || {}, x.data, {
              review_drafts_json: []
            });
            _refreshDetailScheduleSummary(ac);
          }
          _detailApplyScheduleTabFields(_detailScheduleCache, { skipDrafts: true });
        }
        return _postReviewGenerate(accountId, n);
      })
      .catch(function() {
        if (modalMsg) {
          modalMsg.textContent = '保存失败，无法继续生成';
          modalMsg.style.display = 'block';
          modalMsg.className = 'msg err';
        }
      })
      .finally(afterPost);
    return;
  }
  _postReviewGenerate(accountId, n).finally(afterPost);
}

function _handleReviewConfirmClick() {
  var base = publishLocalBase();
  if (!base) {
    alert('未配置本机 API，无法提交。');
    return;
  }
  var accountId = _detailAccountId || _schModalAccountId;
  if (!accountId) {
    alert('请先进入账号详情。');
    return;
  }
  var acf = document.getElementById('accountDetailReviewConfirmBtn');
  if (acf) acf.disabled = true;
  fetch(publishLocalBase() + '/api/accounts/' + accountId + '/creator-schedule/review-confirm', {
    method: 'POST',
    headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
    body: JSON.stringify({})
  })
    .then(function(r) { return _parsePublishJsonResponse(r); })
    .then(function(x) {
      if (!x.ok) {
        var det = x.data && x.data.detail;
        alert(typeof det === 'string' ? det : JSON.stringify(det || x.data));
        return;
      }
      _detailScheduleCache = x.data;
      var ac = _allAccounts.filter(function(a) { return a.id === accountId; })[0];
      if (ac) {
        ac.creator_schedule = Object.assign({}, ac.creator_schedule || {}, x.data);
        if (_detailAccountId === accountId) _refreshDetailScheduleSummary(ac);
      }
      if (_detailAccountId === accountId) _detailApplyScheduleTabFields(x.data);
      alert('已提交确认，后台将按所选草稿执行编排（可在「任务列表」查看进度）。');
      loadAccounts();
    })
    .catch(function() { alert('请求失败'); })
    .finally(function() { if (acf) acf.disabled = false; });
}

function _showReviewGenProgressHtml(html) {
  var box = document.getElementById('accountDetailReviewGenProgress');
  if (!box) return;
  box.innerHTML = html;
  box.style.display = 'block';
}

function _hideReviewGenProgress() {
  var box = document.getElementById('accountDetailReviewGenProgress');
  if (!box) return;
  box.style.display = 'none';
  box.innerHTML = '';
}

/** 生成发布内容：分步展示 saveDone / 当前第几条 / 共几条 */
function _reviewGenProgressMarkup(state) {
  var n = state.n;
  var saveLine = state.saveDone
    ? '<span style="color:#86efac;">✓ 已保存提示词到服务器</span>'
    : '<span style="color:var(--accent);">① 正在保存提示词…</span>';
  var genLine = '';
  if (!state.saveDone) {
    genLine = '<div style="margin-top:0.4rem;color:var(--text-muted);">② 生成发布内容：等待保存完成（共 ' + n + ' 条）</div>';
  } else if (state.phase === 'done') {
    genLine = '<div style="margin-top:0.4rem;color:#86efac;">② 生成发布内容：已全部完成（' + n + ' 条）</div>';
  } else if (state.currentIdx >= 1 && state.currentIdx <= n) {
    genLine = '<div style="margin-top:0.4rem;color:var(--accent);">② 正在生成第 <strong>' + state.currentIdx + '</strong>/' + n + ' 条</div>' +
      '<div class="meta" style="margin-top:0.25rem;font-size:0.74rem;">本步会调用本机 POST /chat 与能力（单条可能数分钟），请勿关闭页面。</div>';
  } else {
    genLine = '<div style="margin-top:0.4rem;color:var(--text-muted);">② 生成发布内容：准备中…（共 ' + n + ' 条）</div>';
  }
  return '<div style="font-weight:600;margin-bottom:0.35rem;">生成发布内容 · 进度</div><div>' + saveLine + '</div>' + genLine;
}

function _reviewGenProgressShortText(state) {
  if (!state.saveDone) return '① 保存提示词…';
  if (state.phase === 'done') return '② 已完成 ' + state.n + ' 条';
  return '② 第 ' + state.currentIdx + '/' + state.n + ' 条生成中…';
}

function _syncReviewGenModalProgress(state) {
  var modalMsg = document.getElementById('schModalMsg');
  var modalMask = document.getElementById('creatorScheduleModal');
  if (!modalMsg || !modalMask || modalMask.style.display !== 'flex') return;
  modalMsg.textContent = _reviewGenProgressShortText(state);
  modalMsg.style.display = 'block';
  modalMsg.className = 'msg';
}

function _postReviewGenerateAssetsOne(accountId, slotIndex) {
  return fetch(publishLocalBase() + '/api/accounts/' + accountId + '/creator-schedule/review-generate-assets', {
    method: 'POST',
    headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
    body: JSON.stringify({ slot_indices: [slotIndex] })
  }).then(function(r) { return _parsePublishJsonResponse(r); });
}

function _handleReviewGenerateAssets() {
  var base = publishLocalBase();
  var msgEl = document.getElementById('accountDetailReviewMsg');
  var modalMsg = document.getElementById('schModalMsg');
  if (!base) {
    var t0 = '未配置本机 API。请用本机运行 lobster_online 后端后从该地址打开页面（需 LOCAL_API_BASE / 同源）。';
    if (msgEl) {
      msgEl.textContent = t0;
      msgEl.style.display = 'block';
      msgEl.style.color = '#f87171';
    }
    return;
  }
  var accountId = _detailAccountId || _schModalAccountId;
  if (!accountId) {
    alert('请先进入账号详情。');
    return;
  }
  var drafts = _collectReviewDraftsFromDom();
  if (!drafts || !drafts.length) {
    var tn = '没有可保存的提示词条目。请先「智能生成提示词」或保存定时任务后再试。';
    if (msgEl) {
      msgEl.textContent = tn;
      msgEl.style.display = 'block';
      msgEl.style.color = '#f87171';
    }
    alert(tn);
    return;
  }
  var nSlots = drafts.length;
  var modalMask = document.getElementById('creatorScheduleModal');
  var modalOpen = modalMask && modalMask.style.display === 'flex';
  _setReviewGenBusy(true);
  function afterPost() {
    _setReviewGenBusy(false);
  }
  if (msgEl) {
    msgEl.style.display = 'none';
  }
  var progState = { saveDone: false, currentIdx: 0, n: nSlots, phase: '' };
  _showReviewGenProgressHtml(_reviewGenProgressMarkup(progState));
  _syncReviewGenModalProgress(progState);

  var putPromise;
  if (modalOpen && _schModalAccountId === accountId) {
    var built = _buildSchedulePutBodyFromModal(modalMsg);
    if (!built.ok) {
      _hideReviewGenProgress();
      afterPost();
      return;
    }
    built.body.review_drafts_json = drafts;
    putPromise = fetch(publishLocalBase() + '/api/accounts/' + accountId + '/creator-schedule', {
      method: 'PUT',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      body: JSON.stringify(built.body)
    })
      .then(function(r) { return _parsePublishJsonResponse(r); })
      .then(function(x) {
        if (!x.ok) {
          var det = x.data && x.data.detail;
          var te = typeof det === 'string' ? det : JSON.stringify(det || x.data);
          if (modalMsg) {
            modalMsg.textContent = te;
            modalMsg.style.display = 'block';
            modalMsg.className = 'msg err';
          }
          throw new Error(te);
        }
        if (_detailAccountId === accountId && x.data) {
          _detailScheduleCache = x.data;
          var ac = _allAccounts.filter(function(a) { return a.id === accountId; })[0];
          if (ac) {
            ac.creator_schedule = Object.assign({}, ac.creator_schedule || {}, x.data);
            _refreshDetailScheduleSummary(ac);
          }
          _detailApplyScheduleTabFields(x.data);
        }
        return x.data;
      });
  } else {
    if (!_detailAccountId || _detailAccountId !== accountId) {
      _hideReviewGenProgress();
      afterPost();
      alert('请在账号详情页的定时任务中操作。');
      return;
    }
    putPromise = _detailPutScheduleMerge({ review_drafts_json: drafts });
  }

  function runSlotsSequential(idx) {
    if (idx >= nSlots) {
      return Promise.resolve(null);
    }
    progState.saveDone = true;
    progState.currentIdx = idx + 1;
    progState.phase = 'slot';
    _showReviewGenProgressHtml(_reviewGenProgressMarkup(progState));
    _syncReviewGenModalProgress(progState);
    return _postReviewGenerateAssetsOne(accountId, idx)
      .then(function(x) {
        if (!x.ok) {
          var det = x.data && x.data.detail;
          var t = typeof det === 'string' ? det : JSON.stringify(det || x.data);
          throw new Error('第 ' + (idx + 1) + ' 条失败：' + t);
        }
        _detailScheduleCache = x.data;
        var ac = _allAccounts.filter(function(a) { return a.id === accountId; })[0];
        if (ac) {
          ac.creator_schedule = Object.assign({}, ac.creator_schedule || {}, x.data);
          if (_detailAccountId === accountId) _refreshDetailScheduleSummary(ac);
        }
        if (_detailAccountId === accountId) _detailApplyScheduleTabFields(x.data);
        return runSlotsSequential(idx + 1);
      });
  }

  putPromise
    .then(function() {
      progState.saveDone = true;
      return runSlotsSequential(0);
    })
    .then(function() {
      progState.phase = 'done';
      progState.currentIdx = nSlots;
      _showReviewGenProgressHtml(_reviewGenProgressMarkup(progState));
      _syncReviewGenModalProgress(progState);
      _hideReviewGenProgress();
      var last = _detailScheduleCache;
      var n = (last && last.review_drafts_json && last.review_drafts_json.length) ? last.review_drafts_json.length : nSlots;
      var okT = '已为 ' + n + ' 条生成发布内容（拟发布说明与素材预览），可在下方查看。';
      if (msgEl) {
        msgEl.textContent = okT;
        msgEl.style.display = 'block';
        msgEl.style.color = '#86efac';
      }
      if (modalMsg && modalOpen) {
        modalMsg.textContent = okT;
        modalMsg.style.display = 'block';
        modalMsg.className = 'msg ok';
      }
      loadAccounts();
      _refreshReviewSnapshotsIfNeeded();
    })
    .catch(function(err) {
      _hideReviewGenProgress();
      var em = (err && err.message) ? err.message : '请求失败（网络或服务异常）';
      if (msgEl) {
        msgEl.textContent = em;
        msgEl.style.display = 'block';
        msgEl.style.color = '#f87171';
      }
      if (modalMsg && modalOpen) {
        modalMsg.textContent = em;
        modalMsg.style.display = 'block';
        modalMsg.className = 'msg err';
      }
    })
    .finally(afterPost);
}

function _parseUtcMs(iso) {
  if (!iso) return NaN;
  try {
    var s = String(iso).trim();
    if (s.indexOf(' ') > 0 && s.indexOf('T') < 0) s = s.replace(' ', 'T');
    if (!/[zZ]$/.test(s) && !/[+-]\d{2}:?\d{2}$/.test(s)) s += 'Z';
    var d = new Date(s);
    return d.getTime();
  } catch (e) {
    return NaN;
  }
}

/** 将 UTC ISO 转为 datetime-local 用的 YYYY-MM-DDTHH:mm（按 Asia/Shanghai 墙钟，与输入语义一致） */
function _utcIsoToDatetimeLocalValueShanghai(iso) {
  if (!iso) return '';
  try {
    var s = String(iso).trim();
    if (s.indexOf(' ') > 0 && s.indexOf('T') < 0) s = s.replace(' ', 'T');
    if (!/[zZ]$/.test(s) && !/[+-]\d{2}:?\d{2}$/.test(s)) s += 'Z';
    var d = new Date(s);
    if (isNaN(d.getTime())) return '';
    var f = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Shanghai',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false
    });
    var parts = {};
    f.formatToParts(d).forEach(function(p) {
      if (p.type !== 'literal') parts[p.type] = p.value;
    });
    return parts.year + '-' + parts.month + '-' + parts.day + 'T' + parts.hour + ':' + parts.minute;
  } catch (e2) {
    return '';
  }
}

/** datetime-local 值视为北京时间，转为 UTC ISO（带 Z）供 PUT */
function _datetimeLocalValueToUtcIsoShanghai(val) {
  if (!val || !String(val).trim()) return null;
  var m = String(val).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!m) return null;
  var y = m[1];
  var mo = m[2];
  var da = m[3];
  var h = m[4];
  var mi = m[5];
  var d = new Date(y + '-' + mo + '-' + da + 'T' + h + ':' + mi + ':00+08:00');
  if (isNaN(d.getTime())) return null;
  return d.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/** 从当前时刻起延后 minutes 分钟 → UTC ISO；0 表示马上发，返回 null 清空服务端首条时间 */
function _minutesFromNowToUtcIso(minutes) {
  var m = Math.max(0, Math.min(10080, parseInt(minutes, 10) || 0));
  if (m === 0) return null;
  var ms = Date.now() + m * 60 * 1000;
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/** 已保存的首条 UTC ISO → 与当前时刻的分钟差（展示用，最小 0） */
function _delayMinutesFromReviewFirstEta(iso) {
  if (!iso) return 0;
  var ms = _parseUtcMs(iso);
  if (isNaN(ms)) return 0;
  return Math.max(0, Math.round((ms - Date.now()) / 60000));
}

/** 审核稿列表：按间隔推算预计发布时间（北京时间） */
function _draftPromptText(d) {
  if (!d || typeof d !== 'object') return '';
  var p = (d.prompt || '').trim();
  if (p) return p;
  var t = (d.title || '').trim();
  var desc = (d.description || '').trim();
  var parts = [];
  if (t) parts.push('【标题意图】' + t);
  if (desc) parts.push('【正文/描述】' + desc);
  return parts.join('\n');
}

function _collectReviewDraftsFromDom() {
  var host = document.getElementById('accountDetailReviewDraftsList');
  if (!host || !_detailScheduleCache) return null;
  var base = _detailScheduleCache.review_drafts_json;
  if (!Array.isArray(base)) return null;
  var out = [];
  for (var i = 0; i < base.length; i++) {
    var ta = host.querySelector('textarea[data-review-prompt-idx="' + i + '"]');
    var prompt = ta ? String(ta.value || '').trim() : _draftPromptText(base[i]);
    var prev = base[i] && typeof base[i] === 'object' ? base[i] : {};
    out.push({
      prompt: prompt,
      attachment_asset_ids: Array.isArray(prev.attachment_asset_ids) ? prev.attachment_asset_ids : [],
      params: prev.params && typeof prev.params === 'object' ? prev.params : {},
      generated: prev.generated && typeof prev.generated === 'object' ? prev.generated : {}
    });
  }
  return out;
}

function _detailReviewEtaList(sch) {
  if (!sch) return [];
  var drafts = sch.review_drafts_json;
  var n = Array.isArray(drafts) ? drafts.length : 0;
  if (n < 1) return [];
  var iv = Math.max(1, parseInt(sch.interval_minutes, 10) || 60);
  var baseMs;
  if (sch.review_first_eta_at) {
    var ms0 = _parseUtcMs(sch.review_first_eta_at);
    if (!isNaN(ms0)) baseMs = ms0;
  }
  if (baseMs == null && sch.enabled && sch.next_run_at) {
    var ms = _parseUtcMs(sch.next_run_at);
    if (!isNaN(ms)) baseMs = ms;
  }
  if (baseMs == null) baseMs = Date.now() + iv * 60 * 1000;
  var out = [];
  for (var i = 0; i < n; i++) {
    out.push(new Date(baseMs + i * iv * 60 * 1000));
  }
  return out;
}

function _detailRenderReviewDrafts(sch) {
  var host = document.getElementById('accountDetailReviewDraftsList');
  if (!host || !sch) return;
  var drafts = sch.review_drafts_json;
  var etas = _detailReviewEtaList(sch);
  if (!Array.isArray(drafts) || !drafts.length) {
    host.innerHTML = '<p class="meta" style="margin:0;font-size:0.8rem;">暂无条目。请在「完整配置」中写好说明后，设置「出几次」并点「智能生成提示词」，或手动保存后再生成。</p>';
    return;
  }
  host.innerHTML = drafts.map(function(d, idx) {
    var eta = etas[idx] ? _formatDateTimeBeijing(etas[idx].toISOString()) : '—';
    var promptVal = _draftPromptText(d);
    var gen = (d && d.generated) ? d.generated : {};
    var excerpt = (gen.reply_excerpt || '').trim();
    var exShort = excerpt.length > 1200 ? excerpt.slice(0, 1200) + '…' : excerpt;
    var urls = Array.isArray(gen.preview_urls) ? gen.preview_urls : [];
    var aids = Array.isArray(gen.asset_ids) ? gen.asset_ids : [];
    var urlBlocks = urls.slice(0, 6).map(function(u) {
      var isImg = /\.(png|jpg|jpeg|webp|gif)(\?|$)/i.test(u);
      if (isImg) {
        return '<div style="margin-top:0.35rem;"><a href="' + escapeAttr(u) + '" target="_blank" rel="noopener"><img src="' + escapeAttr(u) + '" alt="" style="max-width:100%;max-height:160px;border-radius:6px;" referrerpolicy="no-referrer"></a></div>';
      }
      return '<div class="sch-task-mono" style="margin-top:0.25rem;font-size:0.75rem;"><a href="' + escapeAttr(u) + '" target="_blank" rel="noopener">' + escapeHtml(u.length > 80 ? u.slice(0, 80) + '…' : u) + '</a></div>';
    }).join('');
    var aidLine = aids.length ? ('<div class="meta" style="margin-top:0.25rem;font-size:0.75rem;">asset_id：' + escapeHtml(aids.join('、')) + '</div>') : '';
    var genBlock = (excerpt || urlBlocks || aidLine)
      ? ('<div style="margin-top:0.45rem;padding:0.45rem;border-radius:6px;background:rgba(6,182,212,0.08);border:1px solid rgba(6,182,212,0.2);">' +
        '<div style="font-size:0.78rem;font-weight:600;margin-bottom:0.25rem;">生成结果（拟发布说明与素材线索）</div>' +
        (excerpt ? ('<div style="font-size:0.78rem;white-space:pre-wrap;word-break:break-word;">' + escapeHtml(exShort) + '</div>') : '') +
        aidLine + urlBlocks + '</div>')
      : '';
    return '<div class="card" style="margin-bottom:0.5rem;padding:0.65rem;font-size:0.82rem;">' +
      '<div style="font-weight:600;margin-bottom:0.35rem;">第 ' + (idx + 1) + ' 条 · 预计发布时间（北京）：' + escapeHtml(eta) + '</div>' +
      '<label style="font-size:0.76rem;color:var(--text-muted);">发给 AI 的提示词（可编辑；改后请先保存或点「生成发布内容」前会自动保存）</label>' +
      '<textarea data-review-prompt-idx="' + idx + '" rows="5" style="width:100%;box-sizing:border-box;margin-top:0.3rem;padding:0.45rem;font-size:0.82rem;border-radius:var(--radius-sm);background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.12);color:var(--text);resize:vertical;">' +
      escapeHtml(promptVal) + '</textarea>' +
      genBlock +
      '<div style="margin-top:0.4rem;"><button type="button" class="btn btn-ghost btn-sm" data-review-regen="' + idx + '">重新生成此条提示词</button></div>' +
      '</div>';
  }).join('');
  host.querySelectorAll('button[data-review-regen]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var si = parseInt(btn.getAttribute('data-review-regen'), 10);
      if (!_detailAccountId || isNaN(si)) return;
      btn.disabled = true;
      fetch(publishLocalBase() + '/api/accounts/' + _detailAccountId + '/creator-schedule/review-regenerate-slot', {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
        body: JSON.stringify({ slot_index: si })
      })
        .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
        .then(function(x) {
          if (!x.ok) {
            alert((x.data && x.data.detail) ? (typeof x.data.detail === 'string' ? x.data.detail : JSON.stringify(x.data.detail)) : '重新生成失败');
            return;
          }
          _detailScheduleCache = x.data;
          var ac = _allAccounts.filter(function(a) { return a.id === _detailAccountId; })[0];
          if (ac) {
            ac.creator_schedule = Object.assign({}, ac.creator_schedule || {}, x.data);
            _refreshDetailScheduleSummary(ac);
          }
          _detailApplyScheduleTabFields(x.data);
          _refreshReviewSnapshotsIfNeeded();
        })
        .catch(function() { alert('请求失败'); })
        .finally(function() { btn.disabled = false; });
    });
  });
}

function _reviewSnapshotKindLabel(k) {
  if (k === 'prompts') return '智能生成提示词';
  if (k === 'assets') return '生成发布内容';
  if (k === 'slot_regen') return '单条重生成提示词';
  return k || '';
}

function _syncReviewSubtabButtons() {
  document.querySelectorAll('#accountDetailReviewBlock .review-subtab').forEach(function(b) {
    var on = b.getAttribute('data-review-subtab') === _detailReviewSubTab;
    b.className = 'btn btn-sm review-subtab ' + (on ? 'btn-primary' : 'btn-ghost');
  });
}

function _resetReviewSubtabDom() {
  _detailReviewSubTab = 'current';
  var pc = document.getElementById('accountDetailReviewPanelCurrent');
  var ph = document.getElementById('accountDetailReviewPanelHistory');
  if (pc) pc.style.display = '';
  if (ph) ph.style.display = 'none';
  var det = document.getElementById('accountDetailReviewSnapshotDetail');
  if (det) {
    det.style.display = 'none';
    det.innerHTML = '';
  }
  _syncReviewSubtabButtons();
}

function _switchReviewSubTab(which) {
  _detailReviewSubTab = (which === 'history') ? 'history' : 'current';
  var pc = document.getElementById('accountDetailReviewPanelCurrent');
  var ph = document.getElementById('accountDetailReviewPanelHistory');
  if (pc) pc.style.display = (_detailReviewSubTab === 'current') ? '' : 'none';
  if (ph) ph.style.display = (_detailReviewSubTab === 'history') ? '' : 'none';
  _syncReviewSubtabButtons();
  if (_detailReviewSubTab === 'history' && _detailAccountId) _loadReviewSnapshots();
}

function _refreshReviewSnapshotsIfNeeded() {
  if (_detailReviewSubTab !== 'history' || !_detailAccountId) return;
  _loadReviewSnapshots();
}

function _loadReviewSnapshots() {
  var host = document.getElementById('accountDetailReviewSnapshotList');
  if (!host || !_detailAccountId) return;
  host.innerHTML = '<p class="meta" style="margin:0;">加载中…</p>';
  fetch(publishLocalBase() + '/api/accounts/' + _detailAccountId + '/creator-schedule/review-snapshots?limit=50', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      _renderReviewSnapshots(d.snapshots || []);
    })
    .catch(function() {
      host.innerHTML = '<p class="msg err" style="margin:0;">加载失败</p>';
    });
}

function _renderReviewSnapshots(snapshots) {
  var host = document.getElementById('accountDetailReviewSnapshotList');
  if (!host) return;
  if (!snapshots.length) {
    host.innerHTML = '<p class="meta" style="margin:0;">暂无历史记录。完成一次生成后会出现在此。</p>';
    return;
  }
  host.innerHTML = snapshots.map(function(s) {
    var kind = _reviewSnapshotKindLabel(s.kind);
    var st = s.status === 'ok' ? '成功' : '失败';
    var stColor = s.status === 'ok' ? '#86efac' : '#f87171';
    var time = escapeHtml(_formatDateTimeBeijing(s.created_at));
    var sum = escapeHtml((s.summary || '').slice(0, 220));
    var err = s.error_detail ? ('<div class="meta" style="margin-top:0.25rem;color:#f87171;font-size:0.78rem;">' + escapeHtml(String(s.error_detail).slice(0, 400)) + '</div>') : '';
    return '<div class="card" style="margin-bottom:0.5rem;padding:0.55rem 0.65rem;font-size:0.82rem;">' +
      '<div style="display:flex;flex-wrap:wrap;gap:0.5rem;align-items:flex-start;justify-content:space-between;">' +
      '<div><span style="font-weight:600;">' + escapeHtml(kind) + '</span> ' +
      '<span style="color:' + stColor + ';">' + escapeHtml(st) + '</span></div>' +
      '<div class="meta" style="font-size:0.76rem;">' + time + '</div></div>' +
      '<div style="margin-top:0.25rem;">' + sum + '</div>' + err +
      '<div style="margin-top:0.4rem;display:flex;gap:0.4rem;flex-wrap:wrap;">' +
      '<button type="button" class="btn btn-primary btn-sm" data-review-restore-snapshot="' + s.id + '">恢复为当前草稿</button>' +
      '<button type="button" class="btn btn-ghost btn-sm" data-review-detail-snapshot="' + s.id + '">查看详情</button>' +
      '</div></div>';
  }).join('');
}

function _restoreReviewSnapshot(sid) {
  if (!_detailAccountId || !sid) return;
  if (!confirm('确定用此快照覆盖当前草稿？当前编辑区内容将被替换。')) return;
  fetch(publishLocalBase() + '/api/accounts/' + _detailAccountId + '/creator-schedule/review-snapshots/' + sid + '/restore', {
    method: 'POST',
    headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
    body: JSON.stringify({})
  })
    .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
    .then(function(x) {
      if (!x.ok) {
        alert((x.data && x.data.detail) ? String(x.data.detail) : '恢复失败');
        return;
      }
      _detailScheduleCache = x.data;
      var ac = _allAccounts.filter(function(a) { return a.id === _detailAccountId; })[0];
      if (ac) {
        ac.creator_schedule = Object.assign({}, ac.creator_schedule || {}, x.data);
        _refreshDetailScheduleSummary(ac);
      }
      _detailApplyScheduleTabFields(x.data);
      _switchReviewSubTab('current');
      loadAccounts();
      _refreshReviewSnapshotsIfNeeded();
    })
    .catch(function() { alert('请求失败'); });
}

function _showReviewSnapshotDetail(sid) {
  if (!_detailAccountId || !sid) return;
  var box = document.getElementById('accountDetailReviewSnapshotDetail');
  if (!box) return;
  box.style.display = 'block';
  box.textContent = '加载中…';
  fetch(publishLocalBase() + '/api/accounts/' + _detailAccountId + '/creator-schedule/review-snapshots/' + sid, { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var s = d.snapshot || {};
      var j = s.drafts_json;
      var txt = (typeof j === 'undefined') ? '(无)' : JSON.stringify(j, null, 2);
      if (txt.length > 12000) txt = txt.slice(0, 12000) + '\n…（已截断）';
      box.innerHTML = '<div style="font-size:0.76rem;color:var(--text-muted);margin-bottom:0.35rem;">#' + escapeHtml(String(s.id)) + ' · ' + escapeHtml(_reviewSnapshotKindLabel(s.kind)) + ' · ' + escapeHtml(s.status === 'ok' ? '成功' : '失败') + '</div><pre style="margin:0;font-size:0.72rem;white-space:pre-wrap;word-break:break-all;">' + escapeHtml(txt) + '</pre>';
    })
    .catch(function() {
      box.textContent = '加载失败';
    });
}

function _detailApplyScheduleTabFields(d, opts) {
  opts = opts || {};
  if (!d) return;
  var modeEl = document.getElementById('accountDetailScheduleMode');
  if (modeEl) modeEl.value = d.schedule_publish_mode === 'review' ? 'review' : 'immediate';
  var rvc = document.getElementById('accountDetailReviewVariantCount');
  if (rvc) rvc.value = d.review_variant_count != null ? String(d.review_variant_count) : '3';
  var fd = document.getElementById('accountDetailReviewFirstDelayMinutes');
  if (fd) fd.value = String(_delayMinutesFromReviewFirstEta(d.review_first_eta_at));
  var blk = document.getElementById('accountDetailReviewBlock');
  if (blk) blk.style.display = (d.schedule_publish_mode === 'review') ? '' : 'none';
  if (opts.skipDrafts) {
    var host = document.getElementById('accountDetailReviewDraftsList');
    if (host) {
      host.innerHTML = '<p class="meta" style="margin:0;font-size:0.82rem;color:var(--text-muted);">正在重新生成提示词，请稍候…</p>';
    }
  } else {
    _detailRenderReviewDrafts(d);
  }
}

function _detailPutScheduleMerge(extra) {
  var c = _detailScheduleCache;
  if (!c || !_detailAccountId) return Promise.resolve();
  extra = extra || {};
  var body = {
    enabled: !!c.enabled,
    interval_minutes: parseInt(c.interval_minutes, 10) || 60,
    schedule_kind: c.schedule_kind === 'video' ? 'video' : 'image',
    video_source_asset_id: c.video_source_asset_id || null,
    requirements_text: c.requirements_text || null,
    schedule_publish_mode: extra.schedule_publish_mode != null ? extra.schedule_publish_mode : (c.schedule_publish_mode || 'immediate'),
    review_variant_count: extra.review_variant_count != null ? extra.review_variant_count : (c.review_variant_count != null ? c.review_variant_count : 3),
    review_drafts_json: extra.review_drafts_json !== undefined ? extra.review_drafts_json : c.review_drafts_json,
    review_confirmed: extra.review_confirmed !== undefined ? extra.review_confirmed : !!c.review_confirmed
  };
  if (extra.review_first_eta_at !== undefined) {
    body.review_first_eta_at = extra.review_first_eta_at;
  } else if (Object.prototype.hasOwnProperty.call(c, 'review_first_eta_at')) {
    body.review_first_eta_at = c.review_first_eta_at;
  }
  return fetch(publishLocalBase() + '/api/accounts/' + _detailAccountId + '/creator-schedule', {
    method: 'PUT',
    headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
    body: JSON.stringify(body)
  })
    .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
    .then(function(x) {
      if (!x.ok) throw new Error((x.data && x.data.detail) ? String(x.data.detail) : '保存失败');
      _detailScheduleCache = x.data;
      var ac = _allAccounts.filter(function(a) { return a.id === _detailAccountId; })[0];
      if (ac) {
        ac.creator_schedule = Object.assign({}, ac.creator_schedule || {}, x.data);
        _refreshDetailScheduleSummary(ac);
      }
      _detailApplyScheduleTabFields(x.data);
      return x.data;
    });
}

/** 服务端时间为 UTC（带 Z 或与旧数据无后缀均按 UTC 解析），展示为北京时间 */
function _formatDateTimeBeijing(iso) {
  if (!iso) return '';
  try {
    var s = String(iso).trim();
    if (s.indexOf(' ') > 0 && s.indexOf('T') < 0) s = s.replace(' ', 'T');
    if (!/[zZ]$/.test(s) && !/[+-]\d{2}:?\d{2}$/.test(s)) s += 'Z';
    var d = new Date(s);
    if (isNaN(d.getTime())) return String(iso).substring(0, 19).replace('T', ' ');
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
    return String(iso);
  }
}

function _applyIntervalMinutesToModal(m) {
  m = parseInt(m, 10) || 60;
  if (m > 10080) m = 10080;
  if (m < 1) m = 1;
  var valEl = document.getElementById('schIntervalValue');
  var unitEl = document.getElementById('schIntervalUnit');
  if (!valEl || !unitEl) return;
  if (m % 1440 === 0 && m >= 1440) {
    unitEl.value = 'day';
    valEl.value = String(m / 1440);
  } else if (m % 60 === 0 && m >= 60) {
    unitEl.value = 'hour';
    valEl.value = String(m / 60);
  } else {
    unitEl.value = 'min';
    valEl.value = String(m);
  }
}

function _intervalMinutesFromModal() {
  var valEl = document.getElementById('schIntervalValue');
  var unitEl = document.getElementById('schIntervalUnit');
  if (!valEl || !unitEl) return null;
  var v = parseInt(valEl.value, 10);
  if (!v || v < 1) return null;
  var u = unitEl.value;
  var m = u === 'hour' ? v * 60 : u === 'day' ? v * 1440 : v;
  if (m < 1 || m > 10080) return null;
  return m;
}

function _renderAccountList(accounts) {
  var el = document.getElementById('accountList');
  if (!el) return;
  if (!accounts.length) {
    el.innerHTML = '<p class="meta" style="padding:1rem;">该平台暂无发布账号。请在上方添加账号后扫码登录。</p>';
    return;
  }
  el.innerHTML = accounts.map(function(a) {
    var statusColor = STATUS_COLORS[a.status] || '#888';
    var statusLabel = STATUS_LABELS[a.status] || a.status;
    var detailBtn = '<button type="button" class="btn btn-primary btn-sm" data-open-account-detail="' + a.id + '" title="进入账号详情（数据与定时任务）">进入详情</button>';
    var openBtn = '<button type="button" class="btn btn-primary btn-sm" data-open-browser="' + a.id + '">打开浏览器</button>';
    var runsBtn = '<button type="button" class="btn btn-ghost btn-sm" data-schedule-runs-acct="' + a.id + '" title="间隔定时任务的执行记录">执行记录</button>';
    var publishBtn = '<button type="button" class="btn btn-primary btn-sm" data-publish-acct="' + a.id + '" data-publish-nick="' + escapeAttr(a.nickname) + '">发布素材</button>';
    var deleteBtn = '<button type="button" class="btn btn-ghost btn-sm" data-delete-id="' + a.id + '">删除</button>';
    var lastLogin = a.last_login ? '上次登录: ' + _formatDateTimeBeijing(a.last_login) : '';
    var lc = a.last_creator_sync;
    var syncLine = '';
    if (lc && lc.fetched_at) {
      syncLine = '作品数据: ' + _formatDateTimeBeijing(lc.fetched_at) +
        (lc.sync_error ? ' (上次同步失败)' : ' · ' + (lc.item_count != null ? lc.item_count : 0) + ' 条');
    }
    var sch = a.creator_schedule;
    var schHint = '';
    if (sch && sch.enabled) {
      var im = sch.interval_minutes != null ? sch.interval_minutes : 60;
      var nextL = sch.next_run_at ? (' · 下次≈' + escapeHtml(_formatDateTimeBeijing(sch.next_run_at))) : '';
      var kindL = _scheduleKindLabel(sch.schedule_kind);
      var vHint = sch.schedule_kind === 'video' ? (' · ' + escapeHtml(_scheduleVideoBranchHint(sch))) : '';
      var modeShort = sch.schedule_publish_mode === 'review' ? '审核' : '立即';
      schHint = '<div class="card-desc" style="font-size:0.7rem;color:#a5b4fc;">定时已开 · ' + escapeHtml(modeShort) +
        ' · ' + escapeHtml(kindL) + ' · ' + escapeHtml(_formatScheduleIntervalMinutes(im)) + vHint + nextL + '</div>';
    }
    return '<div class="skill-store-card account-card" data-account-card="' + a.id + '" data-platform="' + escapeAttr(a.platform) + '" style="cursor:pointer;" title="点击查看详情">' +
      '<div class="card-label">' + escapeHtml(PLATFORM_NAMES[a.platform] || a.platform) +
      ' <span style="color:' + statusColor + ';font-weight:600;">' + escapeHtml(statusLabel) + '</span></div>' +
      '<div class="card-value">' + escapeHtml(a.nickname) + '</div>' +
      '<div class="card-desc" style="font-size:0.78rem;color:var(--text-muted);">' + escapeHtml(lastLogin) + '</div>' +
      (syncLine ? '<div class="card-desc" style="font-size:0.72rem;color:var(--text-muted);">' + escapeHtml(syncLine) + '</div>' : '') +
      schHint +
      '<div class="card-actions" onclick="event.stopPropagation();">' + detailBtn + ' ' + openBtn + ' ' + runsBtn + ' ' + publishBtn + ' ' + deleteBtn + '</div></div>';
  }).join('');
  _bindAccountButtons(el);
  _bindAccountCardClicks(el);
}

function _applyAccountPlatformFilter() {
  var platform = (document.getElementById('accountPlatformFilter') || {}).value || '';
  var list = platform ? _allAccounts.filter(function(a) { return a.platform === platform; }) : _allAccounts;
  _renderAccountList(list);
}

function loadAccounts() {
  var el = document.getElementById('accountList');
  if (!el) return;
  if (!publishLocalBase()) {
    el.innerHTML = '<p class="msg err" style="padding:1rem;">未配置本机 API（LOCAL_API_BASE）。请用本机运行 backend/run.py 后从 <code>http://127.0.0.1:端口</code> 打开页面。</p>';
    return;
  }
  el.innerHTML = '<p class="meta">加载中…</p>';
  fetch(publishLocalBase() + '/api/accounts', { headers: authHeaders() })
    .then(_publishParseResponse)
    .then(function(x) {
      if (!x.ok) {
        var msg = (x.d && (x.d.detail || x.d.message)) ? String(x.d.detail || x.d.message) : ('HTTP ' + x.status);
        el.innerHTML = '<p class="msg err" style="padding:1rem;">加载失败：' + escapeHtml(msg) + '</p>';
        return;
      }
      var d = x.d;
      var accounts = (d && Array.isArray(d.accounts)) ? d.accounts : [];
      _allAccounts = accounts;
      if (!accounts.length) {
        el.innerHTML = '<p class="meta" style="padding:1rem;">暂无发布账号。请在上方添加账号后扫码登录。</p>';
        return;
      }
      _applyAccountPlatformFilter();
    })
    .catch(function(err) {
      var m = (err && err.message) ? err.message : String(err);
      el.innerHTML = '<p class="msg err" style="padding:1rem;">加载失败：' + escapeHtml(m) + '</p>';
    });
}

function _bindAccountButtons(el) {
  el.querySelectorAll('button[data-open-account-detail]').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      var id = parseInt(btn.getAttribute('data-open-account-detail'), 10);
      if (id) openAccountDetailPanel(id);
    });
  });
  el.querySelectorAll('button[data-open-browser]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var id = btn.getAttribute('data-open-browser');
      btn.disabled = true; btn.textContent = '启动中…';
      fetch(publishLocalBase() + '/api/accounts/' + id + '/open-browser', {
        method: 'POST', headers: authHeaders()
      })
        .then(function(r) { return r.json(); })
        .then(function(d) {
          var status = d.logged_in ? '已登录' : '未登录，请在浏览器中扫码';
          btn.textContent = status;
          setTimeout(function() { loadAccounts(); }, 2000);
        })
        .catch(function() { alert('请求失败'); })
        .finally(function() { btn.disabled = false; });
    });
  });
  el.querySelectorAll('button[data-publish-acct]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var card = btn.closest('.skill-store-card');
      var platform = (card && card.getAttribute('data-platform')) || '';
      var id = btn.getAttribute('data-publish-acct');
      var nick = btn.getAttribute('data-publish-nick') || '';
      var assetId = prompt('请输入要发布的素材 ID（可在「素材库」tab 查看）：');
      if (!assetId || !assetId.trim()) return;
      var title = prompt('发布标题（可留空）：', '') || '';
      var options = {};
      var coverAssetId = null;
      if (platform === 'xiaohongshu') {
        var typeChoice = prompt('发布类型（仅图片素材时有效）：1=图文 2=长文，直接回车=图文', '1') || '1';
        if ((typeChoice || '1').trim() === '2') options.xiaohongshu_publish_type = 'article';
      }
      if (platform === 'douyin') {
        var cm = prompt(
          '抖音视频封面策略（必填）：\n' +
          '1 = smart  智能识别后按需自动点横/竖封面（默认）\n' +
          '2 = upload 必须再指定一张「封面图」素材 ID\n' +
          '3 = manual 仅在浏览器里手动选封面，脚本不自动点',
          '1'
        ) || '1';
        var m = (cm || '1').trim();
        if (m === '2') options.douyin_cover_mode = 'upload';
        else if (m === '3') options.douyin_cover_mode = 'manual';
        else options.douyin_cover_mode = 'smart';
        if (options.douyin_cover_mode === 'upload') {
          coverAssetId = prompt('封面图素材 ID（必填）：');
          if (!coverAssetId || !coverAssetId.trim()) {
            alert('upload 模式必须填写封面图素材 ID');
            return;
          }
        }
      }
      btn.disabled = true; btn.textContent = '发布中…';
      var payload = {
        asset_id: assetId.trim(),
        account_nickname: nick,
        title: title,
        options: Object.keys(options).length ? options : undefined
      };
      if (coverAssetId && coverAssetId.trim()) payload.cover_asset_id = coverAssetId.trim();
      fetch(publishLocalBase() + '/api/publish', {
        method: 'POST', headers: authHeaders(),
        body: JSON.stringify(payload)
      })
        .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
        .then(function(x) {
          if (x.data && x.data.need_login) {
            alert('未登录，已打开浏览器，请扫码登录后重试');
          } else if (x.data && x.data.status === 'success') {
            alert('发布成功！' + (x.data.result_url ? '\n' + x.data.result_url : ''));
          } else {
            alert(x.data.error || x.data.detail || '发布失败');
          }
          loadAccounts();
        })
        .catch(function() { alert('请求失败'); })
        .finally(function() { btn.disabled = false; btn.textContent = '发布素材'; });
    });
  });
  el.querySelectorAll('button[data-delete-id]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var id = btn.getAttribute('data-delete-id');
      if (!confirm('确定删除此账号？')) return;
      fetch(publishLocalBase() + '/api/accounts/' + id, {
        method: 'DELETE', headers: authHeaders()
      })
        .then(function() { loadAccounts(); })
        .catch(function() { alert('删除失败'); });
    });
  });
  el.querySelectorAll('button[data-schedule-runs-acct]').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      var id = parseInt(btn.getAttribute('data-schedule-runs-acct'), 10);
      if (id) openCreatorScheduleTasksModal(id);
    });
  });
}

function _bindAccountCardClicks(listEl) {
  if (!listEl || listEl._accountCardBound) return;
  listEl._accountCardBound = true;
  listEl.addEventListener('click', function(e) {
    if (e.target.closest('button')) return;
    var card = e.target.closest('[data-account-card]');
    if (!card) return;
    var id = parseInt(card.getAttribute('data-account-card'), 10);
    if (id) openAccountDetailPanel(id);
  });
}

function hideAccountDetailPanel() {
  _detailAccountId = null;
  _detailScheduleCache = null;
  _resetReviewSubtabDom();
  var lp = document.getElementById('accountListPanel');
  var dp = document.getElementById('accountDetailPanel');
  if (lp) lp.style.display = '';
  if (dp) dp.style.display = 'none';
}

function openAccountDetailPanel(accountId) {
  var acct = _allAccounts.filter(function(a) { return a.id === accountId; })[0];
  if (!acct) return;
  _detailAccountId = accountId;
  var lp = document.getElementById('accountListPanel');
  var dp = document.getElementById('accountDetailPanel');
  if (lp) lp.style.display = 'none';
  if (dp) dp.style.display = '';
  var tabData = document.getElementById('accountDetailTabData');
  var tabSch = document.getElementById('accountDetailTabSchedule');
  document.querySelectorAll('#accountDetailTabs .sys-tab').forEach(function(t) { t.classList.remove('active'); });
  var firstTab = document.querySelector('#accountDetailTabs [data-ad-tab="data"]');
  if (firstTab) firstTab.classList.add('active');
  if (tabData) tabData.style.display = '';
  if (tabSch) tabSch.style.display = 'none';
  var titleEl = document.getElementById('accountDetailTitle');
  if (titleEl) {
    titleEl.textContent = (PLATFORM_NAMES[acct.platform] || acct.platform) + ' · ' + acct.nickname + ' — 详情';
  }
  _detailScheduleCache = acct.creator_schedule ? Object.assign({}, acct.creator_schedule) : null;
  _resetReviewSubtabDom();
  if (_detailScheduleCache) {
    _detailApplyScheduleTabFields(_detailScheduleCache);
  }
  _refreshDetailScheduleSummary(acct);
  _detailLoadCreatorSettings();
  _detailCreatorSetStatus('', false);
  _renderToutiaoInsightsPanel(acct.platform, null);
  _creatorRenderItems([], 'detailCreatorItemGrid');
  _detailLoadCreatorCache();
  fetch(publishLocalBase() + '/api/accounts/' + accountId + '/creator-schedule', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      _detailScheduleCache = d;
      var ac = _allAccounts.filter(function(a) { return a.id === accountId; })[0];
      if (ac) {
        ac.creator_schedule = Object.assign({}, ac.creator_schedule || {}, d);
        _refreshDetailScheduleSummary(ac);
      }
      _detailApplyScheduleTabFields(d);
    })
    .catch(function() { _detailScheduleCache = null; });
}

function _refreshDetailScheduleSummary(acct) {
  var sum = document.getElementById('accountDetailScheduleSummary');
  if (!sum) return;
  var sch = acct.creator_schedule;
  if (!sch) {
    sum.innerHTML = '尚未配置定时任务。点击「配置定时任务」设置间隔（每隔多久一次），并填写目标与要求（可后续提供给 AI）。';
    return;
  }
  var on = sch.enabled ? '已启用' : '未启用';
  var im = sch.interval_minutes != null ? sch.interval_minutes : 60;
  var nextLine = sch.next_run_at
    ? (' · 下次执行（北京时间）：' + escapeHtml(_formatDateTimeBeijing(sch.next_run_at)))
    : '';
  var modeLabel = sch.schedule_publish_mode === 'review' ? '审核后发布' : '立即发布';
  var kindLine = '类型：<strong>' + escapeHtml(_scheduleKindLabel(sch.schedule_kind)) + '</strong>';
  if (sch.schedule_kind === 'video') {
    kindLine += '（' + escapeHtml(_scheduleVideoBranchHint(sch)) + '）';
    var aid = (sch.video_source_asset_id || '').trim();
    if (aid) kindLine += ' · 素材 ID：<code style="font-size:0.85em;">' + escapeHtml(aid) + '</code>';
  }
  sum.innerHTML = '状态：<strong>' + on + '</strong> · 模式：<strong>' + escapeHtml(modeLabel) + '</strong> · 间隔：<strong>' + escapeHtml(_formatScheduleIntervalMinutes(im)) + '</strong> · ' + kindLine + nextLine +
    ' <button type="button" class="help-q" data-help-key="account_schedule_summary" aria-label="说明">?</button>';
}

function _detailLoadCreatorSettings() {
  fetch(publishLocalBase() + '/api/creator-content/settings', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(s) {
      if (s && typeof s.creator_content_ttl_seconds === 'number') _creatorDefaultTtlSec = s.creator_content_ttl_seconds;
      if (s && typeof s.creator_sync_headless_default === 'boolean') {
        var chk = document.getElementById('detailCreatorHeadlessChk');
        if (chk && !chk.dataset.inited) {
          chk.checked = s.creator_sync_headless_default;
          chk.dataset.inited = '1';
        }
      }
    })
    .catch(function() {});
}

function _detailCreatorSetStatus(t, isErr) {
  var el = document.getElementById('detailCreatorStatusMsg');
  if (!el) return;
  el.textContent = t || '';
  el.style.color = isErr ? '#f87171' : 'var(--text-muted)';
}

function _detailLoadCreatorCache() {
  if (!_detailAccountId) return;
  var id = _detailAccountId;
  var q = _creatorDefaultTtlSec > 0 ? ('?ttl_seconds=' + encodeURIComponent(String(_creatorDefaultTtlSec))) : '';
  _detailCreatorSetStatus('正在加载缓存…', false);
  fetch(publishLocalBase() + '/api/accounts/' + id + '/creator-content' + q, { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.platform !== 'douyin' && d.platform !== 'xiaohongshu' && d.platform !== 'toutiao') {
        _detailCreatorSetStatus('该账号不是抖音/小红书/今日头条，无此类作品列表同步。', false);
        _creatorRenderItems([], 'detailCreatorItemGrid');
        return;
      }
      var insCount = 0;
      if (d.meta && d.meta.toutiao_insights && typeof d.meta.toutiao_insights === 'object') {
        insCount = Object.keys(d.meta.toutiao_insights).length;
      }
      var toutiaoExtra = (d.platform === 'toutiao' && insCount) ? (' · 已汇总 ' + insCount + ' 项数据/收益字段') : '';
      if (d.sync_error) _detailCreatorSetStatus('上次同步错误: ' + d.sync_error, true);
      else if (!d.has_snapshot) _detailCreatorSetStatus('尚无快照，请点击「从平台同步」。', false);
      else _detailCreatorSetStatus('共 ' + ((d.items && d.items.length) || 0) + ' 条作品' + toutiaoExtra + ' · 更新于（北京时间）' + _formatDateTimeBeijing(d.fetched_at), false);
      _renderToutiaoInsightsPanel(d.platform, d.meta || null);
      _creatorRenderItems(d.items || [], 'detailCreatorItemGrid');
    })
    .catch(function() { _detailCreatorSetStatus('加载失败', true); });
}

function openCreatorScheduleModal(accountId) {
  _schModalAccountId = accountId;
  var mask = document.getElementById('creatorScheduleModal');
  var msg = document.getElementById('schModalMsg');
  if (msg) { msg.style.display = 'none'; msg.textContent = ''; }
  if (!mask) return;
  fetch(publishLocalBase() + '/api/accounts/' + accountId + '/creator-schedule', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      document.getElementById('schEnabled').checked = !!d.enabled;
      _applyIntervalMinutesToModal(d.interval_minutes != null ? d.interval_minutes : 60);
      var kindSel = document.getElementById('schScheduleKind');
      if (kindSel) kindSel.value = d.schedule_kind === 'video' ? 'video' : 'image';
      var assetInp = document.getElementById('schVideoAssetId');
      if (assetInp) assetInp.value = d.video_source_asset_id || '';
      document.getElementById('schRequirements').value = d.requirements_text || '';
      var pmEl = document.getElementById('schPublishMode');
      if (pmEl) pmEl.value = d.schedule_publish_mode === 'review' ? 'review' : 'immediate';
      var rvcEl = document.getElementById('schReviewVariantCount');
      if (rvcEl) rvcEl.value = d.review_variant_count != null ? String(d.review_variant_count) : '3';
      _schUpdateScheduleKindUI();
      _schUpdatePublishModeUI();
      mask.style.display = 'flex';
    })
    .catch(function() { alert('加载定时配置失败'); });
}

function closeCreatorScheduleModal() {
  var mask = document.getElementById('creatorScheduleModal');
  if (mask) mask.style.display = 'none';
  _schModalAccountId = null;
}

function _schTriggerLabel(t) {
  if (t === 'bootstrap') return '保存首轮';
  if (t === 'tick') return '定时到点';
  return t || '—';
}

function _schStatusLabel(s) {
  var m = { running: '进行中', success: '成功', failed: '失败', partial: '部分成功', cancelled: '已取消' };
  return m[s] || s || '—';
}

function _schStatusClass(s) {
  if (s === 'running') return 'sch-task-st-running';
  if (s === 'success') return 'sch-task-st-success';
  if (s === 'failed') return 'sch-task-st-failed';
  if (s === 'partial') return 'sch-task-st-partial';
  if (s === 'cancelled') return 'sch-task-st-failed';
  return '';
}

function _schTri(v) {
  if (v === true) return '<span style="color:#4ade80">是</span>';
  if (v === false) return '<span style="color:#f87171">否</span>';
  return '<span class="meta">—</span>';
}

/** 作品同步：接口报错但已拉到条数（如小红书 406 + 导航兜底）时标为「部分」避免误解为完全失败 */
function _schSyncCell(r) {
  var se = (r.sync_error || '').trim();
  var n = r.item_count;
  var hasItems = n != null && n !== '' && Number(n) > 0;
  if (r.sync_ok === true) {
    var okCell = _schTri(true);
    if (se) {
      okCell += '<div class="sch-task-mono meta" style="margin-top:0.12rem;">提示：' + escapeHtml(se.length > 56 ? se.slice(0, 56) + '…' : se) + '</div>';
    }
    return okCell;
  }
  if (r.sync_ok === false && hasItems) {
    var cell = '<span style="color:#fbbf24">部分</span>';
    cell += '<div class="meta" style="margin-top:0.12rem;">已拉取 ' + escapeHtml(String(n)) + ' 条（接口或分页未完全成功）</div>';
    if (se) {
      cell += '<div class="sch-task-mono" style="margin-top:0.15rem;">' + escapeHtml(se.length > 48 ? se.slice(0, 48) + '…' : se) + '</div>';
    }
    return cell;
  }
  var syncCell = _schTri(r.sync_ok);
  if (se) {
    var sshort = se.length > 48 ? se.slice(0, 48) + '…' : se;
    syncCell += '<div class="sch-task-mono" style="margin-top:0.15rem;">' + escapeHtml(sshort) + '</div>';
  }
  if (hasItems) {
    syncCell += '<div class="meta" style="margin-top:0.12rem;">' + escapeHtml(String(n)) + ' 条</div>';
  }
  return syncCell;
}

function _stopSchTasksPoll() {
  if (_schTasksPollTimer) {
    clearInterval(_schTasksPollTimer);
    _schTasksPollTimer = null;
  }
}

function _renderSchTasks(runs) {
  var el = document.getElementById('schTasksBody');
  if (!el) return;
  if (!runs || !runs.length) {
    el.innerHTML = '<p class="meta" style="margin:0;">暂无执行记录。保存定时配置触发首轮或等到点后会在此显示。</p>';
    return;
  }
  var html = '<table class="sch-tasks-table"><thead><tr>';
  html += '<th>开始时间（北京）</th><th>触发</th><th>状态</th><th>进度</th><th>作品同步</th><th>智能编排</th><th>结束时间</th>';
  html += '</tr></thead><tbody>';
  runs.forEach(function(r) {
    var phase = escapeHtml(r.phase || '');
    var det = (r.detail || '').trim();
    if (det) {
      var dshort = det.length > 140 ? det.slice(0, 140) + '…' : det;
      phase += '<div class="sch-task-mono" style="margin-top:0.2rem;">' + escapeHtml(dshort) + '</div>';
    }
    var syncCell = _schSyncCell(r);
    var oe = (r.orchestration_error || '').trim();
    var orchCell = _schTri(r.orchestration_ok);
    if (oe) {
      var oshort = oe.length > 48 ? oe.slice(0, 48) + '…' : oe;
      orchCell += '<div class="sch-task-mono" style="margin-top:0.15rem;">' + escapeHtml(oshort) + '</div>';
    }
    html += '<tr>';
    html += '<td class="sch-task-mono">' + escapeHtml(_formatDateTimeBeijing(r.started_at)) + '</td>';
    html += '<td>' + escapeHtml(_schTriggerLabel(r.trigger)) + '</td>';
    html += '<td class="' + _schStatusClass(r.status) + '">' + escapeHtml(_schStatusLabel(r.status)) + '</td>';
    html += '<td>' + phase + '</td>';
    html += '<td>' + syncCell + '</td>';
    html += '<td>' + orchCell + '</td>';
    html += '<td class="sch-task-mono">' + (r.finished_at ? escapeHtml(_formatDateTimeBeijing(r.finished_at)) : '—') + '</td>';
    html += '</tr>';
  });
  html += '</tbody></table>';
  el.innerHTML = html;
}

function loadCreatorScheduleTasks() {
  var id = _schTasksAccountId;
  var el = document.getElementById('schTasksBody');
  if (!id || !el) return;
  fetch(publishLocalBase() + '/api/accounts/' + id + '/creator-schedule/runs?limit=80', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var runs = (d && d.runs) ? d.runs : [];
      _renderSchTasks(runs);
      var anyRunning = runs.some(function(x) { return x.status === 'running'; });
      _stopSchTasksPoll();
      var mask = document.getElementById('creatorScheduleTasksModal');
      if (mask && mask.style.display === 'flex' && anyRunning) {
        _schTasksPollTimer = setInterval(loadCreatorScheduleTasks, 4000);
      }
    })
    .catch(function() {
      el.innerHTML = '<p class="msg err" style="margin:0;">加载失败</p>';
    });
}

function openCreatorScheduleTasksModal(accountId) {
  _schTasksAccountId = accountId;
  var mask = document.getElementById('creatorScheduleTasksModal');
  if (!mask) return;
  mask.style.display = 'flex';
  var el = document.getElementById('schTasksBody');
  if (el) el.innerHTML = '<p class="meta" style="margin:0;">加载中…</p>';
  loadCreatorScheduleTasks();
}

function closeCreatorScheduleTasksModal() {
  _stopSchTasksPoll();
  _schTasksAccountId = null;
  var mask = document.getElementById('creatorScheduleTasksModal');
  if (mask) mask.style.display = 'none';
}

// 选平台筛选：切换时只显示该平台账号，并确保下方列表立即刷新
var accountPlatformFilter = document.getElementById('accountPlatformFilter');
if (accountPlatformFilter) {
  accountPlatformFilter.addEventListener('change', function() {
    if (_allAccounts.length === 0) {
      loadAccounts();
    } else {
      _applyAccountPlatformFilter();
    }
  });
}

// Add publish account（弹窗）
function openAddPublishAccountModal() {
  var mask = document.getElementById('addPublishAccountModal');
  if (!mask) return;
  var msg = document.getElementById('addPublishAccountModalMsg');
  if (msg) { msg.style.display = 'none'; msg.textContent = ''; }
  mask.style.display = 'flex';
}

function closeAddPublishAccountModal() {
  var mask = document.getElementById('addPublishAccountModal');
  if (mask) mask.style.display = 'none';
}

/** 关闭发布管理下所有全屏遮罩；未关时 fixed 层会盖住主区导致「整页点不动」 */
function closeAllPublishModals() {
  closeAddPublishAccountModal();
  closeCreatorScheduleModal();
  closeCreatorScheduleTasksModal();
}
window.closeAllPublishModals = closeAllPublishModals;

var openAddPubAcctBtn = document.getElementById('openAddPublishAccountModalBtn');
if (openAddPubAcctBtn) {
  openAddPubAcctBtn.addEventListener('click', openAddPublishAccountModal);
}

var addPubAcctCancel = document.getElementById('addPublishAccountModalCancel');
if (addPubAcctCancel) {
  addPubAcctCancel.addEventListener('click', closeAddPublishAccountModal);
}

var addPubAcctMask = document.getElementById('addPublishAccountModal');
if (addPubAcctMask) {
  addPubAcctMask.addEventListener('click', function(e) {
    if (e.target === addPubAcctMask) closeAddPublishAccountModal();
  });
}

var addPubAcctSubmit = document.getElementById('addPublishAccountModalSubmit');
if (addPubAcctSubmit) {
  addPubAcctSubmit.addEventListener('click', function() {
    var platform = document.getElementById('modalAddAcctPlatform').value;
    var nickname = (document.getElementById('modalAddAcctNickname').value || '').trim();
    var msgEl = document.getElementById('addPublishAccountModalMsg');
    if (!nickname) {
      if (msgEl) {
        msgEl.textContent = '请输入账号昵称';
        msgEl.className = 'msg err';
        msgEl.style.display = 'block';
      }
      return;
    }
    var body = { platform: platform, nickname: nickname };
    var ps = (document.getElementById('modalAddAcctProxyServer').value || '').trim();
    var pu = (document.getElementById('modalAddAcctProxyUser').value || '').trim();
    var ppEl = document.getElementById('modalAddAcctProxyPass');
    var pp = ppEl ? (ppEl.value || '') : '';
    var ua = (document.getElementById('modalAddAcctUa').value || '').trim();
    if (ps) body.proxy_server = ps;
    if (pu) body.proxy_username = pu;
    if (pp) body.proxy_password = pp;
    if (ua) body.user_agent = ua;
    addPubAcctSubmit.disabled = true;
    fetch(publishLocalBase() + '/api/accounts', {
      method: 'POST', headers: authHeaders(),
      body: JSON.stringify(body)
    })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
      .then(function(x) {
        if (x.ok) {
          if (msgEl) {
            msgEl.textContent = (x.data && x.data.message) ? x.data.message : '添加成功';
            msgEl.className = 'msg ok';
            msgEl.style.display = 'block';
          }
          document.getElementById('modalAddAcctNickname').value = '';
          document.getElementById('modalAddAcctProxyServer').value = '';
          document.getElementById('modalAddAcctProxyUser').value = '';
          if (ppEl) ppEl.value = '';
          document.getElementById('modalAddAcctUa').value = '';
          loadAccounts();
          setTimeout(closeAddPublishAccountModal, 500);
        } else {
          var det = x.data && x.data.detail;
          var errText = typeof det === 'string' ? det : (det ? JSON.stringify(det) : '添加失败');
          if (msgEl) {
            msgEl.textContent = errText;
            msgEl.className = 'msg err';
            msgEl.style.display = 'block';
          }
        }
      })
      .catch(function() {
        if (msgEl) {
          msgEl.textContent = '网络错误';
          msgEl.className = 'msg err';
          msgEl.style.display = 'block';
        }
      })
      .finally(function() { addPubAcctSubmit.disabled = false; });
  });
}

// ── Assets ───────────────────────────────────────────────────────

var _MEDIA_TYPE_LABELS = { image: '图片', video: '视频', audio: '音频' };

function _assetMsgShow(text, isErr) {
  var m = document.getElementById('assetUploadMsg');
  if (!m) return;
  m.textContent = text;
  m.className = 'msg' + (isErr ? ' err' : ' ok');
  m.style.display = 'inline';
  setTimeout(function() { m.style.display = 'none'; }, 4000);
}

/** 素材列表缩略图：识别 http(s)（大小写不敏感）；支持后端返回的以 / 开头的相对路径 */
function _isAbsoluteHttpUrl(s) {
  return /^https?:\/\//i.test((s || '').trim());
}

function _resolvePossiblyRelativeMediaUrl(s) {
  var t = (s || '').trim();
  if (!t) return '';
  if (_isAbsoluteHttpUrl(t)) return t;
  if (t.length >= 2 && t.charAt(0) === '/' && t.charAt(1) === '/') {
    return (window.location && window.location.protocol ? window.location.protocol : 'https:') + t;
  }
  if (t.charAt(0) === '/' && publishLocalBase()) {
    return publishLocalBase().replace(/\/$/, '') + t;
  }
  return '';
}

/** 当前页是否在回环主机上打开（与签名 URL 里的 127.0.0.1 一致时才适合直链缩略图） */
function _pageHostIsLoopback() {
  var h = (window.location && window.location.hostname) ? String(window.location.hostname).toLowerCase() : '';
  return h === 'localhost' || h === '127.0.0.1';
}

/**
 * 直链放在 img/video 上很可能加载失败：局域网访问时签名根常为 127.0.0.1；HTTPS 页拉 HTTP 会被混合内容拦截。
 */
function _thumbDirectLoadLikelyBroken(url) {
  var u = (url || '').trim();
  if (!u) return true;
  var locProto = (window.location && window.location.protocol) ? String(window.location.protocol).toLowerCase() : '';
  if (locProto === 'https:' && /^http:\/\//i.test(u)) return true;
  if (!_pageHostIsLoopback()) {
    var low = u.toLowerCase();
    if (low.indexOf('127.0.0.1') >= 0 || low.indexOf('localhost') >= 0) return true;
  }
  return false;
}

/** GET 二进制不要用 Content-Type: application/json，减少网关/中间件异常 */
function _authHeadersForMediaFetch() {
  var h = {};
  if (typeof authHeaders === 'function') {
    var ah = authHeaders();
    if (ah && ah.Authorization) h.Authorization = ah.Authorization;
    if (ah && ah['X-Installation-Id']) h['X-Installation-Id'] = ah['X-Installation-Id'];
  }
  return h;
}

function _bindVideoListThumbSeek(vid) {
  if (!vid || !vid.addEventListener) return;
  function bump() {
    try {
      var d = vid.duration;
      if (d && !isNaN(d) && d > 0) vid.currentTime = Math.min(0.15, Math.max(0.05, d * 0.02));
      else vid.currentTime = 0.1;
    } catch (e) {}
  }
  vid.addEventListener(
    'loadeddata',
    function onData() {
      vid.removeEventListener('loadeddata', onData);
      bump();
    },
    { once: true }
  );
  vid.addEventListener(
    'loadedmetadata',
    function onMeta() {
      vid.removeEventListener('loadedmetadata', onMeta);
      bump();
    },
    { once: true }
  );
}

function _pickAssetListThumbUrl(a) {
  var parts = [
    _resolvePossiblyRelativeMediaUrl(a.preview_url),
    _resolvePossiblyRelativeMediaUrl(a.open_url),
    _resolvePossiblyRelativeMediaUrl(a.source_url)
  ];
  for (var i = 0; i < parts.length; i++) {
    if (parts[i]) return parts[i];
  }
  return '';
}

/**
 * 缩略图：已配置本机 API 时优先走带登录头的 /content（与本机素材文件一致，避免签名 URL 误用 127.0.0.1 导致局域网打不开）；
 * 无本机 base 或 /content 失败时再尝试安全直链（公网 CDN 等）；视频 seek 一小段以显示首帧。
 */
function _wireAssetListThumbs(container) {
  var base = publishLocalBase();
  if (!base || typeof fetch !== 'function') return;

  function loadBlobIntoMedia(el, isVideo, directFallback) {
    var aid = el.getAttribute('data-asset-id');
    if (!aid) return;
    var fb = (directFallback || el.getAttribute('data-direct-fallback') || '').trim();
    fetch(base + '/api/assets/' + encodeURIComponent(aid) + '/content', {
      headers: _authHeadersForMediaFetch()
    })
      .then(function(r) {
        if (!r.ok) throw new Error('content ' + r.status);
        return r.blob();
      })
      .then(function(blob) {
        el.src = URL.createObjectURL(blob);
        if (isVideo) _bindVideoListThumbSeek(el);
      })
      .catch(function() {
        if (fb && !_thumbDirectLoadLikelyBroken(fb)) {
          el.src = fb;
          if (isVideo) _bindVideoListThumbSeek(el);
        }
      });
  }

  function wireImg(img) {
    var initial = (img.getAttribute('data-initial-src') || '').trim();
    var preferBlobFirst = img.getAttribute('data-prefer-content') === '1';
    if (preferBlobFirst) {
      loadBlobIntoMedia(img, false, '');
      return;
    }
    if (initial) {
      img.src = initial;
      img.addEventListener(
        'error',
        function onErr() {
          img.removeEventListener('error', onErr);
          loadBlobIntoMedia(img, false, '');
        },
        { once: true }
      );
    } else {
      loadBlobIntoMedia(img, false, '');
    }
  }

  container.querySelectorAll('img.asset-list-thumb').forEach(function(img) {
    wireImg(img);
  });

  container.querySelectorAll('video.asset-list-thumb-video').forEach(function(vid) {
    var initial = (vid.getAttribute('data-initial-src') || '').trim();
    var preferBlobFirst = vid.getAttribute('data-prefer-content') === '1';
    if (preferBlobFirst) {
      loadBlobIntoMedia(vid, true, '');
      return;
    }
    if (initial) {
      vid.src = initial;
      _bindVideoListThumbSeek(vid);
      vid.addEventListener(
        'error',
        function onErr() {
          vid.removeEventListener('error', onErr);
          loadBlobIntoMedia(vid, true, '');
        },
        { once: true }
      );
    } else {
      loadBlobIntoMedia(vid, true, '');
    }
  });
}

function loadAssets(query) {
  var el = document.getElementById('assetList');
  if (!el) return;
  el.innerHTML = '<p class="meta">加载中…</p>';
  var mediaType = (document.getElementById('assetTypeFilter') || {}).value || '';
  var url = publishLocalBase() + '/api/assets?limit=50';
  if (mediaType) url += '&media_type=' + encodeURIComponent(mediaType);
  if (query) url += '&q=' + encodeURIComponent(query);
  fetch(url, { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var assets = (d && Array.isArray(d.assets)) ? d.assets : [];
      if (!assets.length) {
        el.innerHTML = '<p class="meta" style="padding:1rem;">暂无素材。可上传本地文件或保存网络URL，也可在对话中让龙虾生成。</p>';
        return;
      }
      el.innerHTML = assets.map(function(a) {
        var isImage = a.media_type === 'image';
        var isVideo = a.media_type === 'video';
        var hasUrl = _isAbsoluteHttpUrl(a.source_url);
        var thumbUrl = _pickAssetListThumbUrl(a);
        var openUrl = _resolvePossiblyRelativeMediaUrl(a.open_url);
        if (!openUrl && hasUrl) openUrl = _resolvePossiblyRelativeMediaUrl(a.source_url);
        if (!openUrl) openUrl = thumbUrl || '';
        var localBase = publishLocalBase();
        var blobOk = !!(localBase && a.asset_id);
        var canDirectThumb = _isAbsoluteHttpUrl(thumbUrl) || (!!thumbUrl && thumbUrl.charAt(0) === '/');
        var showThumb = (isImage || isVideo) && (canDirectThumb || blobOk);
        var safeDirectFallback = (canDirectThumb && !_thumbDirectLoadLikelyBroken(thumbUrl)) ? thumbUrl : '';
        var preview = '';
        var titleHint = openUrl && _isAbsoluteHttpUrl(openUrl)
          ? '点击在新窗口打开（优先公网可分享链接）'
          : blobOk
            ? '预览由本机素材接口加载；点击尝试打开公网链'
            : '无可用缩略图';
        var wrapAttrs = 'data-asset-id="' + escapeAttr(a.asset_id) + '" data-media-type="' + escapeAttr(a.media_type || '') + '" data-open-url="' + escapeAttr(openUrl || '') + '" style="margin:0.5rem 0;cursor:pointer;" title="' + titleHint + '"';
        if (isImage) {
          if (showThumb) {
            var imgPreferContent = blobOk ? '1' : '0';
            var imgInitialSrc = blobOk ? '' : safeDirectFallback;
            preview =
              '<div class="asset-preview-wrap" ' +
              wrapAttrs +
              '><img class="asset-list-thumb" data-asset-id="' +
              escapeAttr(a.asset_id) +
              '" data-prefer-content="' +
              imgPreferContent +
              '" data-direct-fallback="' +
              escapeAttr(safeDirectFallback) +
              '" data-initial-src="' +
              escapeAttr(imgInitialSrc) +
              '" alt="" style="max-width:160px;max-height:120px;border-radius:6px;object-fit:cover;pointer-events:none;"></div>';
          } else {
            preview = '<div class="asset-preview-wrap" ' + wrapAttrs + '><div style="max-width:160px;max-height:120px;border-radius:6px;background:rgba(255,255,255,0.08);display:flex;align-items:center;justify-content:center;font-size:0.72rem;color:var(--text-muted);padding:0.5rem;">无缩略图<br>（未配置本机 API 或素材无文件）</div></div>';
          }
        } else if (isVideo) {
          if (showThumb) {
            var vidPreferContent = blobOk ? '1' : '0';
            var vidInitialSrc = blobOk ? '' : safeDirectFallback;
            preview =
              '<div class="asset-preview-wrap" ' +
              wrapAttrs +
              '><video class="asset-list-thumb-video" data-asset-id="' +
              escapeAttr(a.asset_id) +
              '" data-prefer-content="' +
              vidPreferContent +
              '" data-direct-fallback="' +
              escapeAttr(safeDirectFallback) +
              '" data-initial-src="' +
              escapeAttr(vidInitialSrc) +
              '" style="max-width:160px;max-height:120px;border-radius:6px;pointer-events:none;" muted preload="auto" playsinline></video></div>';
          } else {
            preview = '<div class="asset-preview-wrap" ' + wrapAttrs + '><div style="max-width:160px;max-height:120px;border-radius:6px;background:rgba(255,255,255,0.08);display:flex;align-items:center;justify-content:center;font-size:0.72rem;color:var(--text-muted);padding:0.5rem;">无缩略图<br>（未配置本机 API 或素材无文件）</div></div>';
          }
        } else {
          preview = '<div style="margin:0.5rem 0;font-size:0.8rem;color:var(--text-muted);">[' + escapeHtml(a.media_type) + '] ' + escapeHtml(a.filename) + '</div>';
        }
        var typeLabel = _MEDIA_TYPE_LABELS[a.media_type] || a.media_type;
        var tags = a.tags ? '<div class="card-tags">' + a.tags.split(',').map(function(t) { return '<span class="tag">' + escapeHtml(t.trim()) + '</span>'; }).join('') + '</div>' : '';
        var size = a.file_size ? (a.file_size > 1048576 ? (a.file_size / 1048576).toFixed(1) + ' MB' : (a.file_size / 1024).toFixed(1) + ' KB') : '';
        var useAsAttachBtn = (isImage || isVideo) ? '<button type="button" class="btn btn-primary btn-sm" data-use-as-attach="' + escapeAttr(a.asset_id) + '" data-attach-media-type="' + escapeAttr(a.media_type || '') + '" data-attach-has-url="' + (hasUrl ? '1' : '0') + '">用作附图</button>' : '';
        var deleteBtn = '<button type="button" class="btn btn-ghost btn-sm" data-delete-asset="' + escapeAttr(a.asset_id) + '">删除</button>';
        return '<div class="skill-store-card">' +
          '<div class="card-label"><span style="background:' + (isImage ? '#6366f1' : isVideo ? '#f59e0b' : '#888') + ';color:#fff;padding:1px 6px;border-radius:3px;font-size:0.72rem;margin-right:4px;">' + escapeHtml(typeLabel) + '</span> ' + escapeHtml(size) + '</div>' +
          preview +
          '<div class="card-desc" style="font-size:0.78rem;">' + escapeHtml(a.prompt || a.filename) + '</div>' +
          tags +
          '<div class="card-desc" style="font-size:0.72rem;color:var(--text-muted);">ID: ' + escapeHtml(a.asset_id) + ' · ' + escapeHtml(_formatDateTimeBeijing(a.created_at)) + '</div>' +
          '<div class="card-actions">' + useAsAttachBtn + ' ' + deleteBtn + '</div></div>';
      }).join('');
      el.querySelectorAll('button[data-use-as-attach]').forEach(function(btn) {
        btn.addEventListener('click', function() {
          var aid = btn.getAttribute('data-use-as-attach');
          var mtype = btn.getAttribute('data-attach-media-type') || 'image';
          var hasUrl = btn.getAttribute('data-attach-has-url') === '1';
          if (typeof addChatAttachment === 'function') {
            addChatAttachment(aid, mtype, hasUrl);
            var chatNav = document.querySelector('.nav-left-item[data-view="chat"]');
            if (chatNav) chatNav.click();
            if (typeof _assetMsgShow === 'function') _assetMsgShow('已添加为附图，输入内容后发送即可带图生成', false);
            else alert('已添加为附图，请在输入框输入内容后发送');
          }
        });
      });
      el.querySelectorAll('button[data-delete-asset]').forEach(function(btn) {
        btn.addEventListener('click', function() {
          var aid = btn.getAttribute('data-delete-asset');
          if (!confirm('确定删除此素材？')) return;
          fetch(publishLocalBase() + '/api/assets/' + aid, { method: 'DELETE', headers: authHeaders() })
            .then(function() { loadAssets(); })
            .catch(function() { alert('删除失败'); });
        });
      });
      _wireAssetListThumbs(el);
      el.querySelectorAll('.asset-preview-wrap').forEach(function(wrap) {
        wrap.addEventListener('click', function() {
          var url = wrap.getAttribute('data-open-url');
          if (!url || !_isAbsoluteHttpUrl(url)) {
            alert('无法在新窗口打开：当前无公网 http(s) 链接。缩略图已从本机加载时，可在素材目录或对话附图中使用。');
            return;
          }
          window.open(url, '_blank');
        });
      });
    })
    .catch(function() { el.innerHTML = '<p class="msg err">加载失败</p>'; });
}

// Search
var assetSearchBtn = document.getElementById('assetSearchBtn');
if (assetSearchBtn) {
  assetSearchBtn.addEventListener('click', function() {
    var q = (document.getElementById('assetSearchInput') || {}).value || '';
    loadAssets(q.trim());
  });
}

// Filter by media type
var assetTypeFilter = document.getElementById('assetTypeFilter');
if (assetTypeFilter) {
  assetTypeFilter.addEventListener('change', function() {
    var q = (document.getElementById('assetSearchInput') || {}).value || '';
    loadAssets(q.trim());
  });
}

// Upload local files（仅火山链接算成功，上传中灰色）
var assetUploadFile = document.getElementById('assetUploadFile');
var assetUploadLabel = assetUploadFile ? assetUploadFile.closest('label') : null;
function setAssetUploadState(loading, text) {
  if (assetUploadLabel) {
    assetUploadLabel.style.opacity = loading ? '0.5' : '1';
    assetUploadLabel.style.pointerEvents = loading ? 'none' : '';
  }
  if (text) _assetMsgShow(text, false);
}
if (assetUploadFile) {
  assetUploadFile.addEventListener('change', function() {
    var files = assetUploadFile.files;
    if (!files || !files.length) return;
    var total = files.length;
    var done = 0, failed = 0, noTos = 0;
    setAssetUploadState(true, '正在上传到火山 ' + total + ' 个文件…');
    Array.from(files).forEach(function(f, idx) {
      var fd = new FormData();
      fd.append('file', f);
      fetch(publishLocalBase() + '/api/assets/upload', { method: 'POST', headers: authHeaders(), body: fd })
        .then(function(r) {
          return r.json().then(function(d) {
            if (!r.ok) {
              var msg = 'HTTP ' + r.status;
              if (d && d.detail) {
                msg = typeof d.detail === 'string' ? d.detail : (Array.isArray(d.detail) ? d.detail.map(function(x) { return x.msg || JSON.stringify(x); }).join('; ') : JSON.stringify(d.detail));
              }
              throw new Error(msg);
            }
            return d;
          });
        })
        .then(function(d) {
          if (d && d.source_url && (d.source_url.indexOf('http') === 0)) {
            done++;
          } else if (d && d.asset_id && d.media_type === 'audio') {
            done++;
          } else {
            noTos++;
          }
        })
        .catch(function() { failed++; })
        .finally(function() {
          var finished = done + noTos + failed;
          if (finished === total) {
            assetUploadFile.value = '';
            setAssetUploadState(false, '');
            var msg = '上传完成: ' + done + ' 已同步火山';
            if (noTos) msg += ', ' + noTos + ' 未同步火山（失败）';
            if (failed) msg += ', ' + failed + ' 请求失败';
            _assetMsgShow(msg, noTos > 0 || failed > 0);
            loadAssets();
          } else {
            setAssetUploadState(true, '正在上传到火山 ' + finished + '/' + total + '…');
          }
        });
    });
  });
}

// Save URL asset
var assetSaveUrlBtn = document.getElementById('assetSaveUrlBtn');
if (assetSaveUrlBtn) {
  assetSaveUrlBtn.addEventListener('click', function() {
    var urlInput = document.getElementById('assetUrlInput');
    var rawUrl = (urlInput ? urlInput.value : '').trim();
    if (!rawUrl) { _assetMsgShow('请输入素材URL', true); return; }
    assetSaveUrlBtn.disabled = true;
    _assetMsgShow('正在保存…', false);
    var ext = rawUrl.split('?')[0].split('#')[0].split('.').pop().toLowerCase();
    var mtype = 'image';
    if (['mp4', 'mov', 'avi', 'mkv', 'webm', 'flv'].indexOf(ext) >= 0) mtype = 'video';
    fetch(publishLocalBase() + '/api/assets/save-url', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      body: JSON.stringify({ url: rawUrl, media_type: mtype })
    })
      .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function(d) {
        if (urlInput) urlInput.value = '';
        _assetMsgShow('保存成功 (ID: ' + (d.asset_id || '') + ')', false);
        loadAssets();
      })
      .catch(function(e) { _assetMsgShow('保存失败: ' + e.message, true); })
      .finally(function() { assetSaveUrlBtn.disabled = false; });
  });
}

// ── Tasks ────────────────────────────────────────────────────────

var TASK_STATUS = { pending: '排队中', publishing: '发布中', success: '成功', failed: '失败', need_login: '需登录' };
var TASK_COLORS = { pending: '#fbbf24', publishing: '#60a5fa', success: '#34d399', failed: '#f87171', need_login: '#fb923c' };

function _renderSteps(steps) {
  if (!steps || !steps.length) return '';
  var html = '<div style="margin-top:0.5rem;padding:0.5rem;background:rgba(255,255,255,0.03);border-radius:6px;font-size:0.75rem;">';
  html += '<div style="color:var(--text-muted);margin-bottom:0.25rem;font-weight:600;">执行步骤：</div>';
  for (var i = 0; i < steps.length; i++) {
    var s = steps[i];
    var icon = s.ok ? '✓' : '✗';
    var color = s.ok ? '#34d399' : '#f87171';
    var action = s.action || s.note || '';
    var detail = '';
    if (s.error) detail = ' — ' + s.error;
    else if (s.selector) detail = '';
    else if (s.url) detail = '';
    else if (s.tried && !s.ok) detail = ' (未匹配)';
    html += '<div style="color:' + color + ';padding:1px 0;">' +
      '<span style="display:inline-block;width:1.2em;text-align:center;">' + icon + '</span> ' +
      escapeHtml(action) + escapeHtml(detail) + '</div>';
  }
  html += '</div>';
  return html;
}

function loadTasks() {
  var el = document.getElementById('taskList');
  if (!el) return;
  el.innerHTML = '<p class="meta">加载中…</p>';
  fetch(publishLocalBase() + '/api/publish/tasks?limit=50', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var tasks = (d && Array.isArray(d.tasks)) ? d.tasks : [];
      if (!tasks.length) {
        el.innerHTML = '<p class="meta" style="padding:1rem;line-height:1.55;">暂无<strong>单次发布</strong>记录（对话触发的 publish 任务）。<br>' +
          '若你配置的是账号上的<strong>间隔定时任务</strong>：请到<strong>发布账号</strong> → 点击该账号 → <strong>执行记录</strong> 或进入详情后点 <strong>任务列表</strong>（今日头条与抖音、小红书相同）。</p>';
        return;
      }
      el.innerHTML = '<div class="card">' + tasks.map(function(t) {
        var statusColor = TASK_COLORS[t.status] || '#888';
        var statusLabel = TASK_STATUS[t.status] || t.status;
        var resultLink = t.result_url ? ' <a href="' + escapeAttr(t.result_url) + '" target="_blank" style="color:var(--primary);">查看</a>' : '';
        var errorText = t.error ? '<div style="color:#f87171;font-size:0.78rem;margin-top:0.25rem;">' + escapeHtml(t.error) + '</div>' : '';
        var acctInfo = (t.platform ? (PLATFORM_NAMES[t.platform] || t.platform) : '') +
          (t.account_nickname ? ' · ' + t.account_nickname : '');
        var stepsHtml = _renderSteps(t.steps || []);
        return '<div style="padding:0.75rem 0;border-bottom:1px solid rgba(255,255,255,0.06);">' +
          '<div style="display:flex;justify-content:space-between;align-items:center;">' +
            '<div><span style="font-weight:600;">' + escapeHtml(t.title || '无标题') + '</span>' +
            ' <span style="font-size:0.78rem;color:var(--text-muted);">素材:' + escapeHtml(t.asset_id) +
            (acctInfo ? ' · ' + escapeHtml(acctInfo) : '') + '</span></div>' +
            '<span style="color:' + statusColor + ';font-weight:600;font-size:0.85rem;">' + statusLabel + resultLink + '</span>' +
          '</div>' +
          errorText +
          stepsHtml +
          '<div style="font-size:0.72rem;color:var(--text-muted);margin-top:0.25rem;">' +
            escapeHtml(_formatDateTimeBeijing(t.created_at)) +
            (t.finished_at ? ' → ' + escapeHtml(_formatDateTimeBeijing(t.finished_at)) : '') +
          '</div>' +
        '</div>';
      }).join('') + '</div>';
    })
    .catch(function() { el.innerHTML = '<p class="msg err">加载失败</p>'; });
}

// ── Refresh button ───────────────────────────────────────────────

var refreshPubBtn = document.getElementById('refreshPublishBtn');
if (refreshPubBtn) {
  refreshPubBtn.addEventListener('click', function() {
    if (_currentPubTab === 'accounts') {
      if (_detailAccountId) {
        var ac = _allAccounts.filter(function(a) { return a.id === _detailAccountId; })[0];
        if (ac) openAccountDetailPanel(_detailAccountId);
      }
      loadAccounts();
    }
    if (_currentPubTab === 'assets') loadAssets();
    if (_currentPubTab === 'tasks') loadTasks();
  });
}

function initPublishView() {
  hideAccountDetailPanel();
  loadAccounts();
}

// ── 详情页作品网格 + 定时弹窗 ─────────────────────────────────────

function _renderToutiaoInsightsPanel(platform, meta) {
  var el = document.getElementById('detailCreatorToutiaoInsights');
  if (!el) return;
  if (platform !== 'toutiao') {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  var ins = meta && meta.toutiao_insights;
  if (!ins || typeof ins !== 'object') {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  var keys = Object.keys(ins);
  if (!keys.length) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  keys.sort(function(a, b) { return a.toLowerCase().localeCompare(b.toLowerCase()); });
  var rows = keys.map(function(k) {
    var v = ins[k];
    if (v === null || v === undefined) v = '';
    if (typeof v === 'object') v = JSON.stringify(v);
    return '<tr><td class="sch-task-mono" style="padding:0.25rem 0.5rem 0.25rem 0;vertical-align:top;color:var(--text-muted);max-width:42%;word-break:break-all;">' + escapeHtml(k) + '</td><td style="padding:0.25rem 0;word-break:break-word;">' + escapeHtml(String(v)) + '</td></tr>';
  }).join('');
  el.style.display = 'block';
  el.innerHTML = '<div style="font-weight:600;margin-bottom:0.35rem;font-size:0.9rem;">账号 / 收益 / 数据（XHR 摘要）</div>' +
    '<p class="meta" style="font-size:0.75rem;margin-bottom:0.5rem;line-height:1.45;">同步时依次打开首页、内容管理、收益与数据等页并抓取接口中的标量字段；字段名随头条后台可能变化，仅供参考。</p>' +
    '<div style="overflow-x:auto;max-height:240px;overflow-y:auto;border:1px solid rgba(255,255,255,0.1);border-radius:8px;"><table style="width:100%;font-size:0.78rem;border-collapse:collapse;">' + rows + '</table></div>';
}

function _creatorFormatMetrics(m) {
  if (!m || typeof m !== 'object') return '';
  var parts = [];
  if (m.view_count != null) parts.push('播/阅 ' + m.view_count);
  if (m.play_count != null && m.play_count > 0) parts.push('播放 ' + m.play_count);
  if (m.like_count != null) parts.push('赞 ' + m.like_count);
  if (m.comment_count != null) parts.push('评 ' + m.comment_count);
  if (m.collect_count != null) parts.push('藏 ' + m.collect_count);
  if (m.share_count != null) parts.push('享 ' + m.share_count);
  return parts.join(' · ');
}

function _creatorRenderItems(items, gridId) {
  var grid = document.getElementById(gridId || 'detailCreatorItemGrid');
  if (!grid) return;
  if (!items || !items.length) {
    grid.innerHTML = '<p class="meta" style="padding:1rem;">暂无作品数据。抖音/小红书请先「从平台同步」并确保已登录。</p>';
    return;
  }
  grid.innerHTML = items.map(function(it) {
    var title = it.title || '(无标题)';
    var cover = it.cover_url && it.cover_url.indexOf('http') === 0
      ? '<img src="' + escapeAttr(it.cover_url) + '" alt="" referrerpolicy="no-referrer" style="width:100%;max-height:140px;object-fit:cover;border-radius:6px;">'
      : '<div style="height:100px;border-radius:6px;background:rgba(255,255,255,0.06);display:flex;align-items:center;justify-content:center;font-size:0.75rem;color:var(--text-muted);">无封面</div>';
    var metrics = _creatorFormatMetrics(it.metrics);
    return '<div class="skill-store-card">' +
      '<div class="card-label" style="font-size:0.72rem;color:var(--text-muted);">' + escapeHtml(it.content_type || '') + '</div>' +
      cover +
      '<div class="card-value" style="font-size:0.85rem;margin-top:0.35rem;max-height:3.2em;overflow:hidden;">' + escapeHtml(title) + '</div>' +
      '<div class="card-desc" style="font-size:0.75rem;color:var(--text-muted);">' + escapeHtml(metrics) + '</div>' +
      '<div class="card-desc" style="font-size:0.7rem;color:var(--text-muted);">ID: ' + escapeHtml(String(it.id || '')) + '</div></div>';
  }).join('');
}

(function bindPublishAccountDetailAndSchedule() {
  var back = document.getElementById('accountDetailBack');
  if (back && !back._bound) {
    back._bound = true;
    back.addEventListener('click', function() {
      hideAccountDetailPanel();
      loadAccounts();
    });
  }
  var schBtn = document.getElementById('accountDetailScheduleBtn');
  if (schBtn && !schBtn._bound) {
    schBtn._bound = true;
    schBtn.addEventListener('click', function() {
      if (_detailAccountId) openCreatorScheduleModal(_detailAccountId);
    });
  }
  var schTasksBtn = document.getElementById('accountDetailScheduleTasksBtn');
  if (schTasksBtn && !schTasksBtn._bound) {
    schTasksBtn._bound = true;
    schTasksBtn.addEventListener('click', function() {
      if (!_detailAccountId) return;
      openCreatorScheduleTasksModal(_detailAccountId);
    });
  }
  var loadB = document.getElementById('detailCreatorLoadBtn');
  if (loadB && !loadB._bound) {
    loadB._bound = true;
    loadB.addEventListener('click', function() { _detailLoadCreatorCache(); });
  }
  var syncB = document.getElementById('detailCreatorSyncBtn');
  if (syncB && !syncB._bound) {
    syncB._bound = true;
    syncB.addEventListener('click', function() {
      if (!_detailAccountId) return;
      var chk = document.getElementById('detailCreatorHeadlessChk');
      _detailCreatorSetStatus('正在从平台同步…', false);
      syncB.disabled = true;
      fetch(publishLocalBase() + '/api/accounts/' + _detailAccountId + '/sync-creator-content', {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
        body: JSON.stringify({ headless: !!(chk && chk.checked) })
      })
        .then(function(r) { return r.json(); })
        .then(function(d) {
          var ac = _allAccounts.filter(function(a) { return a.id === _detailAccountId; })[0];
          var plat = ac ? ac.platform : '';
          if (!d.ok) _detailCreatorSetStatus('同步失败: ' + (d.error || d.detail || JSON.stringify(d)), true);
          else if (plat === 'toutiao') {
            var ic = (d.meta && d.meta.toutiao_insights) ? Object.keys(d.meta.toutiao_insights).length : 0;
            var tx = (d.item_count || 0) + ' 条作品' + (ic ? ' · ' + ic + ' 项数据/收益字段' : '');
            _detailCreatorSetStatus('同步成功，共 ' + tx, false);
          } else {
            _detailCreatorSetStatus('同步成功，共 ' + (d.item_count || 0) + ' 条', false);
          }
          _renderToutiaoInsightsPanel(plat, d.meta || null);
          _creatorRenderItems(d.items || [], 'detailCreatorItemGrid');
          loadAccounts();
        })
        .catch(function() { _detailCreatorSetStatus('请求失败', true); })
        .finally(function() { syncB.disabled = false; });
    });
  }
  var kindSel = document.getElementById('schScheduleKind');
  if (kindSel && !kindSel._schBound) {
    kindSel._schBound = true;
    kindSel.addEventListener('change', _schUpdateScheduleKindUI);
  }
  var schPm = document.getElementById('schPublishMode');
  if (schPm && !schPm._schBound) {
    schPm._schBound = true;
    schPm.addEventListener('change', _schUpdatePublishModeUI);
  }
  document.querySelectorAll('#accountDetailTabs [data-ad-tab]').forEach(function(btn) {
    if (btn._adTabBound) return;
    btn._adTabBound = true;
    btn.addEventListener('click', function() {
      var tab = btn.getAttribute('data-ad-tab');
      document.querySelectorAll('#accountDetailTabs .sys-tab').forEach(function(t) { t.classList.remove('active'); });
      btn.classList.add('active');
      var d = document.getElementById('accountDetailTabData');
      var s = document.getElementById('accountDetailTabSchedule');
      if (tab === 'schedule') {
        if (d) d.style.display = 'none';
        if (s) s.style.display = '';
      } else {
        if (d) d.style.display = '';
        if (s) s.style.display = 'none';
      }
    });
  });
  var adm = document.getElementById('accountDetailScheduleMode');
  if (adm && !adm._bound) {
    adm._bound = true;
    adm.addEventListener('change', function() {
      var v = adm.value === 'review' ? 'review' : 'immediate';
      _detailPutScheduleMerge({ schedule_publish_mode: v }).catch(function(e) {
        alert(e && e.message ? e.message : String(e));
      });
    });
  }
  var adv = document.getElementById('accountDetailReviewVariantCount');
  if (adv && !adv._bound) {
    adv._bound = true;
    adv.addEventListener('change', function() {
      var n = Math.max(1, Math.min(10, parseInt(adv.value, 10) || 3));
      adv.value = String(n);
      _detailPutScheduleMerge({ review_variant_count: n }).catch(function(e) {
        alert(e && e.message ? e.message : String(e));
      });
    });
  }
  var firstDelay = document.getElementById('accountDetailReviewFirstDelayMinutes');
  if (firstDelay && !firstDelay._bound) {
    firstDelay._bound = true;
    firstDelay.addEventListener('change', function() {
      var m = Math.max(0, Math.min(10080, parseInt(firstDelay.value, 10) || 0));
      firstDelay.value = String(m);
      var iso = _minutesFromNowToUtcIso(m);
      _detailPutScheduleMerge({ review_first_eta_at: iso }).catch(function(e) {
        alert(e && e.message ? e.message : String(e));
      });
    });
  }
  var cbtn = document.getElementById('schCancelBtn');
  if (cbtn && !cbtn._bound) {
    cbtn._bound = true;
    cbtn.addEventListener('click', closeCreatorScheduleModal);
  }
  var sbtn = document.getElementById('schSaveBtn');
  if (sbtn && !sbtn._bound) {
    sbtn._bound = true;
    sbtn.addEventListener('click', function() {
      if (!_schModalAccountId) return;
      var msg = document.getElementById('schModalMsg');
      var built = _buildSchedulePutBodyFromModal(msg);
      if (!built.ok) return;
      sbtn.disabled = true;
      var putBody = built.body;
      fetch(publishLocalBase() + '/api/accounts/' + _schModalAccountId + '/creator-schedule', {
        method: 'PUT',
        headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
        body: JSON.stringify(putBody)
      })
        .then(function(r) { return _parsePublishJsonResponse(r); })
        .then(function(x) {
          if (!x.ok) {
            var det = x.data && x.data.detail;
            msg.textContent = typeof det === 'string' ? det : JSON.stringify(det || x.data);
            msg.style.display = 'block';
            msg.className = 'msg err';
            return;
          }
          var savedAcct = _schModalAccountId;
          closeCreatorScheduleModal();
          loadAccounts();
          if (savedAcct && _detailAccountId === savedAcct) {
            fetch(publishLocalBase() + '/api/accounts', { headers: authHeaders() })
              .then(function(r) { return r.json(); })
              .then(function(d) {
                _allAccounts = (d && d.accounts) || [];
                var ac = _allAccounts.filter(function(a) { return a.id === _detailAccountId; })[0];
                if (ac) _refreshDetailScheduleSummary(ac);
              });
            fetch(publishLocalBase() + '/api/accounts/' + savedAcct + '/creator-schedule', { headers: authHeaders() })
              .then(function(r) { return r.json(); })
              .then(function(d) {
                _detailScheduleCache = d;
                var ac = _allAccounts.filter(function(a) { return a.id === _detailAccountId; })[0];
                if (ac) {
                  ac.creator_schedule = Object.assign({}, ac.creator_schedule || {}, d);
                  _refreshDetailScheduleSummary(ac);
                }
                _detailApplyScheduleTabFields(d);
              })
              .catch(function() {});
          }
        })
        .catch(function() {
          msg.textContent = '保存失败';
          msg.style.display = 'block';
          msg.className = 'msg err';
        })
        .finally(function() { sbtn.disabled = false; });
    });
  }
  var mask = document.getElementById('creatorScheduleModal');
  if (mask && !mask._bound) {
    mask._bound = true;
    mask.addEventListener('click', function(e) {
      if (e.target === mask) closeCreatorScheduleModal();
    });
  }

  var schTasksMask = document.getElementById('creatorScheduleTasksModal');
  if (schTasksMask && !schTasksMask._bound) {
    schTasksMask._bound = true;
    schTasksMask.addEventListener('click', function(e) {
      if (e.target === schTasksMask) closeCreatorScheduleTasksModal();
    });
  }
  var schTasksCloseBtn = document.getElementById('schTasksCloseBtn');
  if (schTasksCloseBtn && !schTasksCloseBtn._bound) {
    schTasksCloseBtn._bound = true;
    schTasksCloseBtn.addEventListener('click', closeCreatorScheduleTasksModal);
  }
  var schTasksRefreshBtn = document.getElementById('schTasksRefreshBtn');
  if (schTasksRefreshBtn && !schTasksRefreshBtn._bound) {
    schTasksRefreshBtn._bound = true;
    schTasksRefreshBtn.addEventListener('click', loadCreatorScheduleTasks);
  }
})();

(function bindReviewSnapshotUi() {
  if (document.body._reviewSnapshotUi) return;
  document.body._reviewSnapshotUi = true;
  document.body.addEventListener('click', function(e) {
    var sub = e.target.closest('[data-review-subtab]');
    if (sub && sub.closest('#accountDetailReviewBlock')) {
      e.preventDefault();
      e.stopPropagation();
      _switchReviewSubTab(sub.getAttribute('data-review-subtab'));
      return;
    }
    if (e.target.closest('#accountDetailReviewSnapshotRefreshBtn')) {
      e.preventDefault();
      _loadReviewSnapshots();
      return;
    }
    var rst = e.target.closest('[data-review-restore-snapshot]');
    if (rst) {
      e.preventDefault();
      e.stopPropagation();
      _restoreReviewSnapshot(parseInt(rst.getAttribute('data-review-restore-snapshot'), 10));
      return;
    }
    var dtl = e.target.closest('[data-review-detail-snapshot]');
    if (dtl) {
      e.preventDefault();
      e.stopPropagation();
      _showReviewSnapshotDetail(parseInt(dtl.getAttribute('data-review-detail-snapshot'), 10));
    }
  });
})();

(function bindReviewDraftDelegation() {
  if (document.body._reviewDraftDeleg) return;
  document.body._reviewDraftDeleg = true;
  document.body.addEventListener('click', function(e) {
    var g = e.target.closest('[data-action="review-generate"]');
    if (g) {
      e.preventDefault();
      e.stopPropagation();
      _handleReviewGenerateClick();
      return;
    }
    var a = e.target.closest('[data-action="review-generate-assets"]');
    if (a) {
      e.preventDefault();
      e.stopPropagation();
      _handleReviewGenerateAssets();
      return;
    }
    var c = e.target.closest('[data-action="review-confirm"]');
    if (c) {
      e.preventDefault();
      e.stopPropagation();
      _handleReviewConfirmClick();
    }
  });
})();
