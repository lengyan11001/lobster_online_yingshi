/** 旧版全局键；升级后首次登录会迁移到 lobster_chat_sessions_u{userId} 并删除 */
var LEGACY_CHAT_SESSIONS_KEY = 'lobster_chat_sessions';
/** 刷新后可续查 task_poll 的最大存活时间（须早于本文件内任意使用它的函数）；过长会导致「每次打开页面都续查旧任务」 */
var _POLL_RESUME_MAX_AGE_MS = 3600000;
/** 刷新续查专用 /chat/stream：超过此时长主动断开，避免「约每 15 秒更新」永远结束不了（后端轮询最长约 40min） */
var _RESUME_CHAT_STREAM_MAX_MS = 50 * 60 * 1000;

/**
 * 是否在刷新/切换会话时自动请求「恢复轮询」(resume_task_poll_task_id)。
 * 默认关闭：否则 localStorage 里遗留的 poll_resume 会让每次打开页面都看到「正在恢复并查询…」并打 /chat/stream。
 * 需要自动续查：localStorage.setItem('lobster_chat_auto_resume_poll','1') 后刷新；或 window.__LOBSTER_CHAT_AUTO_RESUME_POLL = true
 */
function chatAutoResumePollEnabled() {
  try {
    if (typeof window !== 'undefined' && window.__LOBSTER_CHAT_AUTO_RESUME_POLL === true) return true;
    return localStorage.getItem('lobster_chat_auto_resume_poll') === '1';
  } catch (e) {
    return false;
  }
}

function getChatSessionsStorageKey() {
  var uid = '';
  if (typeof window.__currentUserId !== 'undefined' && window.__currentUserId != null) {
    uid = String(window.__currentUserId);
  }
  if (!uid && typeof window.getCurrentUserIdFromToken === 'function') {
    uid = window.getCurrentUserIdFromToken();
  }
  // 无用户 ID 时仍落盘，否则刷新后无法恢复 poll_resume（本地/未注入 __currentUserId 场景）
  return uid ? ('lobster_chat_sessions_u' + uid) : 'lobster_chat_sessions_anon';
}

/** 最后打开的会话 id，刷新后优先恢复（与 poll_resume 会话可能不同） */
function getChatLastSessionStorageKey() {
  var sk = getChatSessionsStorageKey();
  if (!sk) return '';
  return sk.replace(/^lobster_chat_sessions/, 'lobster_chat_last_session');
}

function saveLastActiveChatSessionToStorage(sid) {
  try {
    var k = getChatLastSessionStorageKey();
    if (!k || !sid) return;
    localStorage.setItem(k, String(sid));
  } catch (e) {}
}

function getLastActiveChatSessionIdFromStorage() {
  try {
    var k = getChatLastSessionStorageKey();
    if (!k) return '';
    return String(localStorage.getItem(k) || '').trim();
  } catch (e) {
    return '';
  }
}

/** 流式 /chat/stream 根地址：与发送消息一致，缺省时用当前页 origin（同源部署） */
function _chatStreamApiBase() {
  var b = (typeof LOCAL_API_BASE !== 'undefined' && LOCAL_API_BASE) ? String(LOCAL_API_BASE).replace(/\/$/, '') : '';
  if (b) return b;
  if (typeof window !== 'undefined' && window.location && window.location.origin) {
    return String(window.location.origin).replace(/\/$/, '');
  }
  return '';
}

function _rawChatStreamError(e) {
  if (!e) return '';
  if (e.detail != null && e.detail !== '') return String(e.detail);
  if (e.message) return String(e.message);
  return '';
}
function _normalizeChatStreamErrorMessage(raw) {
  var s = String(raw || '').trim();
  if (s.indexOf('错误：') === 0) s = s.slice(3).trim();
  var low = s.toLowerCase();
  if (!s) return '请稍后重试';
  if (low === 'failed to fetch' || low.indexOf('failed to fetch') >= 0)
    return '网络连接失败，请检查后端是否已启动或网络是否正常';
  if (low === 'network error' || low.indexOf('network error') >= 0)
    return '网络连接失败，请检查后端是否已启动或网络是否正常';
  if (low.indexOf('networkerror') >= 0)
    return '网络连接失败，请检查后端是否已启动或网络是否正常';
  if (low.indexOf('load failed') >= 0)
    return '网络连接失败，请检查后端是否已启动或网络是否正常';
  /* httpx.RemoteProtocolError 常见英文：对端在未发完 HTTP 时断开（OpenClaw/MCP/速推/网关） */
  if (low.indexOf('server disconnected without sending a response') >= 0) {
    return (
      '上游连接被异常关闭（未返回完整 HTTP 响应），常见于网关或速推、OpenClaw、MCP 重启、代理超时或网络抖动。' +
      '若进度里已出现「✓ 素材已生成」，请到素材库用对应素材 ID 查看；也可刷新后续查或重新发送消息重试。'
    );
  }
  return s;
}
/** 将 done 事件里「错误：…」中的已知英文技术句替换为中文（兼容未重启的旧后端） */
function _normalizeAssistantStreamReply(reply) {
  var r = String(reply || '');
  var t = r.trim();
  if (!t) return r;
  if (t.indexOf('错误：') === 0) {
    var inner = t.slice(3).trim();
    var norm = _normalizeChatStreamErrorMessage(inner);
    if (norm !== inner) return '错误：' + norm;
    return r;
  }
  var norm2 = _normalizeChatStreamErrorMessage(t);
  if (norm2 !== t) return norm2;
  return r;
}
/** 刷新续查 /chat/stream 失败时：不写入历史、保留 poll_resume，避免每次 F5 多一条重复错误 */
function _isTransientResumeStreamFailure(e, rawStr, normalizedMsg) {
  if (e && e.name === 'AbortError') return false;
  if (e && e.name === 'TypeError') return true;
  var st = e && typeof e.status === 'number' ? e.status : NaN;
  if (st === 502 || st === 503 || st === 504) return true;
  var nm = String(normalizedMsg || '');
  if (nm.indexOf('网络连接失败') === 0) return true;
  var raw = String(rawStr || '').toLowerCase();
  if (raw.indexOf('network error') >= 0 || raw.indexOf('failed to fetch') >= 0) return true;
  return false;
}
function _pushAssistantErrorIfNotDuplicate(targetSession, msg) {
  if (!targetSession) return false;
  var line = '错误：' + msg;
  targetSession.messages = Array.isArray(targetSession.messages) ? targetSession.messages : [];
  var last = targetSession.messages.length ? targetSession.messages[targetSession.messages.length - 1] : null;
  if (last && last.role === 'assistant' && String(last.content || '') === line) return false;
  targetSession.messages.push({ role: 'assistant', content: line });
  targetSession.updatedAt = Date.now();
  return true;
}
/** 去掉末尾连续重复的助手错误行（修复历史里已堆积的「错误：network error」） */
function _pruneTrailingDuplicateAssistantErrors(session) {
  var msgs = session && session.messages;
  if (!msgs || msgs.length < 2) return false;
  var changed = false;
  while (msgs.length >= 2) {
    var a = msgs[msgs.length - 1];
    var b = msgs[msgs.length - 2];
    if (!a || !b || a.role !== 'assistant' || b.role !== 'assistant') break;
    var ca = String(a.content || '').trim();
    var cb = String(b.content || '').trim();
    if (ca.indexOf('错误：') !== 0 || cb.indexOf('错误：') !== 0) break;
    if (ca.toLowerCase() !== cb.toLowerCase()) break;
    msgs.pop();
    changed = true;
  }
  return changed;
}

/**
 * 选一个需要续查轮询的会话：优先 preferSid（若其 poll_resume 仍有效）；
 * pickAny 为 true 时（仅页面初始化）：当前会话无续查任务则取 poll_resume_at 最新的会话（用于 F5 恢复）。
 * pickAny 为 false 时：禁止因其它会话有 poll_resume 而自动切换会话（避免切换左侧导航反复触发恢复）。
 */
function _pickSessionIdNeedingPollResume(preferSid, pickAny) {
  var now = Date.now();
  var pref = String(preferSid || '').trim();
  if (pref) {
    var ps = getSessionById(pref);
    if (ps) {
      var ptid = (ps.poll_resume_task_id || '').trim();
      if (ptid && (now - (ps.poll_resume_at || 0)) <= _POLL_RESUME_MAX_AGE_MS) {
        return pref;
      }
    }
  }
  if (!pickAny) {
    return null;
  }
  var bestSid = '';
  var bestAt = -1;
  for (var i = 0; i < chatSessions.length; i++) {
    var s = chatSessions[i];
    var tid = (s.poll_resume_task_id || '').trim();
    if (!tid) continue;
    var age = now - (s.poll_resume_at || 0);
    if (age > _POLL_RESUME_MAX_AGE_MS) continue;
    var at = Number(s.poll_resume_at) || 0;
    if (at > bestAt) {
      bestAt = at;
      bestSid = String(s.id);
    }
  }
  return bestSid || null;
}

function migrateLegacyChatSessionsIfNeeded() {
  var key = getChatSessionsStorageKey();
  if (!key) return;
  try {
    if (localStorage.getItem(key)) return;
    var leg = localStorage.getItem(LEGACY_CHAT_SESSIONS_KEY);
    if (!leg) return;
    localStorage.setItem(key, leg);
    localStorage.removeItem(LEGACY_CHAT_SESSIONS_KEY);
  } catch (e) {}
}

/** 切换用户或登出时清空内存与对话区 DOM（须在设置 __currentUserId 之后立刻 load/init） */
function resetChatSessionsMemory() {
  chatSessions = [];
  currentSessionId = null;
  chatHistory = [];
  chatPendingBySession = {};
  chatAttachmentIds = [];
  chatAttachmentInfos = [];
  var c = document.getElementById('chatMessages');
  if (c) c.innerHTML = '';
  var att = document.getElementById('chatAttachments');
  if (att) {
    att.style.display = 'none';
    att.innerHTML = '';
  }
  var listEl = document.getElementById('chatSessionList');
  if (listEl) listEl.innerHTML = '';
}

window.resetChatSessionsForLogout = function() {
  try {
    var k = getChatLastSessionStorageKey();
    if (k) localStorage.removeItem(k);
  } catch (e) {}
  window.__currentUserId = undefined;
  resetChatSessionsMemory();
};

var chatSessions = [];
var currentSessionId = null;
var chatHistory = [];
var chatPendingBySession = {};
/** 当前 /chat/stream 请求的 AbortController，非空时「取消」可点 */
var chatStreamAbortController = null;
/** 当前流式对话是否已向速推提交生成任务（image/video 的 tasks/create 已成功）；为 true 时取消仅提示不可中止 */
var chatStreamSutuiSubmitted = false;
/** 中止进行中的 /chat/stream，防止新请求覆盖 controller 后旧流的 finally 把全局置空、导致无法取消或 pending 错乱 */
function abortActiveChatStream() {
  var c = chatStreamAbortController;
  if (!c) return;
  try {
    c.abort();
  } catch (e) {}
  chatStreamAbortController = null;
}
var chatAttachmentIds = [];
var chatAttachmentInfos = [];

function getSessionById(id) {
  var sid = id != null ? String(id) : '';
  return chatSessions.find(function(s) { return String(s.id) === sid; }) || null;
}
function isSessionPending(id) {
  return !!chatPendingBySession[String(id)];
}
function setSessionPending(id, pending) {
  var sid = String(id || '');
  if (!sid) return;
  var next = !!pending;
  var s = getSessionById(sid);
  if (s && !!s.pending === next) {
    if (next) chatPendingBySession[sid] = true;
    else delete chatPendingBySession[sid];
    refreshChatInputState();
    return;
  }
  if (next) chatPendingBySession[sid] = true;
  else delete chatPendingBySession[sid];
  if (s) {
    s.pending = next;
    if (!next) { delete s._typingState; _stopTypingStateSyncTimer(); }
  }
  try {
    saveChatSessionsToStorage();
  } catch (e1) {}
  refreshChatInputState();
  renderChatSessionList();
}

function _saveSessionTypingState(sid, mainText, step, stepMode) {
  var s = getSessionById(String(sid || ''));
  if (!s) return;
  if (!s._typingState) s._typingState = { mainText: '正在处理…', steps: [], _ver: 0 };
  if (mainText != null) { s._typingState.mainText = mainText; s._typingState._ver = (s._typingState._ver || 0) + 1; }
  if (step != null) {
    s._typingState._ver = (s._typingState._ver || 0) + 1;
    if (stepMode === 'replace_last' && s._typingState.steps.length) {
      s._typingState.steps[s._typingState.steps.length - 1] = step;
    } else if (stepMode === 'append') {
      s._typingState.steps.push(step);
    }
  }
}
function _clearSessionTypingState(sid) {
  var s = getSessionById(String(sid || ''));
  if (s) delete s._typingState;
  _stopTypingStateSyncTimer();
}

var _typingStateSyncTimer = null;
var _typingStateSyncLastVer = -1;
var _typingStateSyncLastStepCount = -1;

function _startTypingStateSyncTimer(sid) {
  _stopTypingStateSyncTimer();
  _typingStateSyncLastVer = -1;
  _typingStateSyncLastStepCount = -1;
  _typingStateSyncTimer = setInterval(function() {
    if (String(currentSessionId) !== String(sid)) { _stopTypingStateSyncTimer(); return; }
    var s = getSessionById(String(sid));
    if (!s || !s._typingState || !isSessionPending(sid)) { _stopTypingStateSyncTimer(); return; }
    var ts = s._typingState;
    var ver = ts._ver || 0;
    var stepCount = (ts.steps || []).length;
    if (ver === _typingStateSyncLastVer && stepCount === _typingStateSyncLastStepCount) return;
    setChatTypingMainText(ts.mainText || '正在处理…');
    if (stepCount > _typingStateSyncLastStepCount && _typingStateSyncLastStepCount >= 0) {
      for (var i = _typingStateSyncLastStepCount; i < stepCount; i++) {
        appendChatTypingStep(ts.steps[i]);
      }
    }
    _typingStateSyncLastVer = ver;
    _typingStateSyncLastStepCount = stepCount;
  }, 2000);
}
function _stopTypingStateSyncTimer() {
  if (_typingStateSyncTimer != null) { clearInterval(_typingStateSyncTimer); _typingStateSyncTimer = null; }
}

/** 与 chatSessions JSON 并列：按会话 id 单独存 poll_resume，避免会话对象未找到时整段不落盘 */
function _pollResumeBackupStorageKey(sid) {
  var u = getChatSessionsStorageKey();
  if (!u || sid == null || sid === '') return '';
  return u + '_poll_' + String(sid);
}

function mergePollResumeFromBackupIntoSession(s) {
  if (!s || s.id == null) return;
  if ((s.poll_resume_task_id || '').trim()) return;
  try {
    var bk = _pollResumeBackupStorageKey(s.id);
    if (!bk) return;
    var raw = localStorage.getItem(bk);
    if (!raw) return;
    var o = JSON.parse(raw);
    if (!o || !(o.task_id || '').toString().trim()) return;
    var at = Number(o.at) || Date.now();
    if (Date.now() - at > _POLL_RESUME_MAX_AGE_MS) {
      localStorage.removeItem(bk);
      return;
    }
    s.poll_resume_task_id = String(o.task_id).trim();
    s.poll_resume_at = at;
  } catch (e) {}
}

/** 刷新后可据 task_id 恢复轮询；由 task_poll 事件更新 */
function persistSessionPollResumeTaskId(sid, taskId) {
  var tid = (taskId || '').trim();
  var sid0 = String(sid != null ? sid : '').trim();
  if (!tid || !sid0) return;
  var s = getSessionById(sid0);
  if (s) {
    s.poll_resume_task_id = tid;
    s.poll_resume_at = Date.now();
  }
  try {
    var bk = _pollResumeBackupStorageKey(sid0);
    if (bk) {
      localStorage.setItem(bk, JSON.stringify({ task_id: tid, at: Date.now() }));
    }
  } catch (e0) {}
  scheduleSaveChatSessionsToStorage();
}

function clearSessionPollResume(sid) {
  var sid0 = String(sid != null ? sid : '').trim();
  var s = sid0 ? getSessionById(sid0) : null;
  if (s) {
    delete s.poll_resume_task_id;
    delete s.poll_resume_at;
  }
  try {
    var bk = _pollResumeBackupStorageKey(sid0);
    if (bk) localStorage.removeItem(bk);
  } catch (eRm) {}
  try {
    saveChatSessionsToStorage();
  } catch (e3) {}
}
function refreshChatInputState() {
  var input = document.getElementById('chatInput');
  var btn = document.getElementById('chatSendBtn');
  var cancelBtn = document.getElementById('chatCancelBtn');
  if (!btn) return;
  btn.disabled = !!(currentSessionId && isSessionPending(currentSessionId));
  if (input) input.disabled = false;
  if (cancelBtn) {
    var hasActiveStream = !!chatStreamAbortController;
    cancelBtn.disabled = !hasActiveStream;
    cancelBtn.title = hasActiveStream && chatStreamSutuiSubmitted
      ? '任务已在速推生成中：点击可查看说明'
      : '终止当前进行中的请求；尚未提交到速推时会直接中止';
  }
}

function escapeHtmlChat(str) {
  if (str == null || str === '') return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** 在线版：后端固定 sutui/deepseek-chat，与 backend lobster_default_sutui_chat_model 一致 */
function _isOnlineFixedSutuiChat() {
  return typeof EDITION !== 'undefined' && EDITION === 'online';
}
function _onlineFixedChatModelPayload() {
  return 'sutui/deepseek-chat';
}

/** 发送按钮旁：费用与模型确认 */
function openChatCapabilityCostConfirm(opts) {
  opts = opts || {};
  var capId = opts.capability_id || '';
  var invokeModel = (opts.invoke_model || '').trim();
  var creditDisplay = opts.credit_display || '未知';
  var note = (opts.note || '').trim();
  var secLeft = opts.timeout_seconds != null ? opts.timeout_seconds : 300;

  return new Promise(function(resolve) {
    var sendBtn = document.getElementById('chatSendBtn');
    if (!sendBtn) {
      resolve(false);
      return;
    }

    var backdrop = document.createElement('div');
    backdrop.className = 'chat-cost-confirm-backdrop';
    backdrop.style.cssText =
      'position:fixed;inset:0;z-index:10040;background:rgba(0,0,0,0.45);' +
      '-webkit-backdrop-filter:blur(3px);backdrop-filter:blur(3px);';

    var pop = document.createElement('div');
    pop.className = 'chat-cost-confirm-popover chat-cost-confirm-popover--floating';
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-modal', 'true');
    pop.setAttribute('aria-labelledby', 'chatCostConfirmTitle');
    pop.style.cssText =
      'position:fixed;z-index:10050;box-sizing:border-box;margin:0;' +
      'width:min(92vw,300px);max-width:300px;padding:0.9rem 1rem;' +
      'background:rgba(18,18,24,0.97);border:1px solid rgba(255,255,255,0.1);' +
      'border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,0.55),0 0 0 1px rgba(6,182,212,0.15);' +
      'color:#e4e4e7;font-size:0.82rem;line-height:1.45;';

    pop.innerHTML =
      '<div id="chatCostConfirmTitle" class="chat-cost-confirm-title" role="heading" aria-level="2" ' +
      'style="font-weight:600;font-size:0.9rem;margin:0 0 0.5rem;color:#e4e4e7;line-height:1.3;">确认调用能力</div>' +
      '<div class="chat-cost-confirm-cap-label" style="font-size:0.72rem;color:#a1a1aa;text-transform:uppercase;letter-spacing:0.04em;">能力</div>' +
      '<div class="chat-cost-confirm-cap-value" style="font-family:ui-monospace,Consolas,monospace;font-size:0.8rem;word-break:break-all;margin:0.15rem 0 0.35rem;color:#e4e4e7;">' +
      escapeHtmlChat(capId || '（未指定）') +
      '</div>' +
      (invokeModel
        ? '<div class="chat-cost-confirm-cap-label" style="font-size:0.72rem;color:#a1a1aa;margin-top:0.35rem;">模型（与本次调用一致）</div>' +
          '<div class="chat-cost-confirm-cap-value" style="font-family:ui-monospace,Consolas,monospace;font-size:0.8rem;word-break:break-all;margin:0.15rem 0 0.5rem;color:#e4e4e7;">' +
          escapeHtmlChat(invokeModel) +
          '</div>'
        : '') +
      '<div class="chat-cost-confirm-credits" style="font-weight:600;font-size:0.95rem;color:#06b6d4;margin:0 0 0.35rem;">' +
      escapeHtmlChat('参考算力：' + creditDisplay) +
      '</div>' +
      (note
        ? '<div class="chat-cost-confirm-note" style="color:#a1a1aa;font-size:0.78rem;max-height:6.5rem;overflow-y:auto;margin:0 0 0.5rem;white-space:pre-wrap;word-break:break-word;padding:0.45rem 0.5rem;background:rgba(0,0,0,0.25);border-radius:8px;border:1px solid rgba(255,255,255,0.06);">' +
          escapeHtmlChat(note) +
          '</div>'
        : '') +
      '<div class="chat-cost-confirm-timeout" style="font-size:0.72rem;color:#a1a1aa;margin-bottom:0.55rem;">' +
      escapeHtmlChat('约 ' + secLeft + ' 秒内有效，超时将自动取消') +
      '</div>' +
      '<div class="chat-cost-confirm-actions" style="display:flex;gap:0.45rem;justify-content:flex-end;flex-wrap:wrap;">' +
      '<button type="button" class="btn btn-ghost btn-sm" data-cc-cancel>取消</button>' +
      '<button type="button" class="btn btn-primary btn-sm" data-cc-ok>确认调用</button>' +
      '</div>';

    function positionPopover() {
      var rect = sendBtn.getBoundingClientRect();
      var gap = 10;
      var vw = window.innerWidth || document.documentElement.clientWidth || 0;
      var popW = Math.min(300, Math.max(260, vw - 16));
      pop.style.width = popW + 'px';
      var left = rect.right - popW;
      if (left < 8) left = 8;
      if (left + popW > vw - 8) left = Math.max(8, vw - popW - 8);
      pop.style.left = left + 'px';
      pop.style.right = 'auto';
      var th = pop.offsetHeight || 200;
      var topEdge = rect.top - gap - th;
      if (topEdge < 8) topEdge = 8;
      pop.style.top = topEdge + 'px';
    }

    document.body.appendChild(backdrop);
    document.body.appendChild(pop);
    requestAnimationFrame(function() {
      positionPopover();
      requestAnimationFrame(positionPopover);
    });

    var onResize = function() {
      positionPopover();
    };
    window.addEventListener('resize', onResize);

    var settled = false;
    function cleanup() {
      window.removeEventListener('resize', onResize);
      document.removeEventListener('keydown', onKey);
      if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
      if (pop.parentNode) pop.parentNode.removeChild(pop);
    }

    function finish(accept) {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(accept);
    }

    function onKey(e) {
      if (e.key === 'Escape') finish(false);
    }
    document.addEventListener('keydown', onKey);

    backdrop.addEventListener('click', function() {
      finish(false);
    });

    var ok = pop.querySelector('[data-cc-ok]');
    var cancel = pop.querySelector('[data-cc-cancel]');
    if (ok) ok.addEventListener('click', function() { finish(true); });
    if (cancel) cancel.addEventListener('click', function() { finish(false); });

    if (ok) ok.focus();
  });
}

function renderCurrentSessionMessages() {
  var container = document.getElementById('chatMessages');
  if (!container) return;
  container.innerHTML = '';
  var sid = currentSessionId ? String(currentSessionId) : '';
  var session = getSessionById(sid);
  var messages = session && Array.isArray(session.messages) ? session.messages : [];
  chatHistory = messages.slice();
  messages.forEach(function(m) {
    if (m.role === 'assistant' && m.saved_assets && m.saved_assets.length) {
      appendAssistantMessageReveal(m.content || '', m.saved_assets);
    } else if (m.role === 'user') {
      appendUserMessageDisplay(m.content, m.attachment_asset_ids);
    } else {
      appendChatMessage(m.role, m.content);
    }
  });
  _stopTypingStateSyncTimer();
  if (sid && isSessionPending(sid)) {
    showChatTypingIndicator();
    var s0 = getSessionById(sid);
    if (s0 && s0._typingState) {
      setChatTypingMainText(s0._typingState.mainText || '正在处理…');
      var savedSteps = s0._typingState.steps || [];
      for (var si = 0; si < savedSteps.length; si++) {
        appendChatTypingStep(savedSteps[si]);
      }
      _typingStateSyncLastVer = s0._typingState._ver || 0;
      _typingStateSyncLastStepCount = savedSteps.length;
      _startTypingStateSyncTimer(sid);
    } else if (s0 && (s0.poll_resume_task_id || '').trim()) {
      setChatTypingMainText('正在查询生成结果…（恢复连接）');
    }
  }
  container.scrollTop = container.scrollHeight;
  refreshChatInputState();
}

/** 刷新后仅「有可续查的 task_poll task_id」才保留 pending；否则流已断无法恢复，不应再显示正在思考 */

function _normalizeSessionPendingAfterLoad(s) {
  if (!s) return false;
  delete s._typingState;
  var changed = false;
  var tid = (s.poll_resume_task_id || '').trim();
  if (tid) {
    var age = Date.now() - (s.poll_resume_at || 0);
    if (age > _POLL_RESUME_MAX_AGE_MS) {
      if (s.pending) s.pending = false;
      delete s.poll_resume_task_id;
      delete s.poll_resume_at;
      return true;
    }
    if (!chatAutoResumePollEnabled()) {
      if (s.pending) {
        s.pending = false;
        changed = true;
      }
      return changed;
    }
    // 有可续查 task_id 时：保留 poll_resume，不因「末条是 assistant」清空（多轮对话里上一轮常以助手结尾）
    if (!s.pending) {
      s.pending = true;
      changed = true;
    }
    return changed;
  }
  var msgs = s.messages || [];
  var last = msgs.length ? msgs[msgs.length - 1] : null;
  if (last && last.role === 'assistant') {
    if (s.pending) {
      s.pending = false;
      changed = true;
    }
    return changed;
  }
  if (s.pending) {
    s.pending = false;
    changed = true;
  }
  return changed;
}

function loadChatSessionsFromStorage() {
  /** 必须先清空，避免「新用户 localStorage 无数据」时仍沿用上一用户内存中的 chatSessions */
  chatSessions = [];
  try {
    migrateLegacyChatSessionsIfNeeded();
    var key = getChatSessionsStorageKey();
    if (!key) return;
    var raw = localStorage.getItem(key);
    if (!raw) return;
    var parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      chatSessions = parsed;
      chatPendingBySession = {};
      var storageDirty = false;
      chatSessions.forEach(function(s) {
        if (s.id != null) s.id = String(s.id);
        var m = s.messages || s.history;
        s.messages = Array.isArray(m) ? m : [];
        mergePollResumeFromBackupIntoSession(s);
        if (_normalizeSessionPendingAfterLoad(s)) storageDirty = true;
        if (_pruneTrailingDuplicateAssistantErrors(s)) storageDirty = true;
        if (s.pending) chatPendingBySession[s.id] = true;
      });
      if (storageDirty) {
        try {
          saveChatSessionsToStorage();
        } catch (e0) {}
      }
    }
  } catch (e) {
    chatSessions = [];
  }
}
function saveChatSessionsToStorage() {
  try {
    var key = getChatSessionsStorageKey();
    if (!key) return;
    localStorage.setItem(key, JSON.stringify(chatSessions));
  } catch (e) {}
}
/** task_poll 高频时避免每次全量 stringify 写盘卡死主线程；备份键仍即时写入 */
var _saveChatSessionsScheduledTimer = null;
function scheduleSaveChatSessionsToStorage() {
  if (_saveChatSessionsScheduledTimer != null) clearTimeout(_saveChatSessionsScheduledTimer);
  _saveChatSessionsScheduledTimer = setTimeout(function() {
    _saveChatSessionsScheduledTimer = null;
    try {
      saveChatSessionsToStorage();
    } catch (e) {}
  }, 500);
}
function flushPendingChatSessionsSave() {
  if (_saveChatSessionsScheduledTimer != null) {
    clearTimeout(_saveChatSessionsScheduledTimer);
    _saveChatSessionsScheduledTimer = null;
    try {
      saveChatSessionsToStorage();
    } catch (e) {}
  }
}
function getSessionTitle(session) {
  var msg = (session.messages || []).find(function(m) { return m.role === 'user' && (m.content || '').trim(); });
  if (msg) {
    var t = (msg.content || '').trim();
    return t.length > 24 ? t.slice(0, 24) + '…' : t;
  }
  return session.title || '新对话';
}
function getSessionPreview(session) {
  var messages = session.messages || [];
  for (var i = messages.length - 1; i >= 0; i--) {
    var m = messages[i];
    if (m && (m.content || '').trim()) {
      var t = (m.content || '').trim();
      return t.length > 32 ? t.slice(0, 32) + '…' : t;
    }
  }
  return '暂无消息';
}
function formatSessionTime(ts) {
  if (!ts) return '';
  var d = new Date(ts);
  var now = new Date();
  var diff = (now - d) / 60000;
  if (diff < 1) return '刚刚';
  if (diff < 60) return Math.floor(diff) + ' 分钟前';
  if (diff < 1440) return Math.floor(diff / 60) + ' 小时前';
  if (diff < 43200) return Math.floor(diff / 1440) + ' 天前';
  return d.toLocaleDateString();
}
function createNewSession() {
  var id = 's' + Date.now();
  var session = { id: id, title: '新对话', messages: [], updatedAt: Date.now(), pending: false };
  chatSessions.unshift(session);
  saveChatSessionsToStorage();
  switchChatSession(id);
  renderChatSessionList();
}
function switchChatSession(id) {
  var sid = id != null ? String(id) : '';
  if (currentSessionId === sid) return;
  saveCurrentSessionToStore();
  currentSessionId = sid;
  saveLastActiveChatSessionToStorage(sid);
  renderCurrentSessionMessages();
  renderChatSessionList();
  /** 默认不自动续查，见 chatAutoResumePollEnabled */
  if (typeof maybeAutoResumeChatTaskPoll === 'function' && chatAutoResumePollEnabled())
    maybeAutoResumeChatTaskPoll({ pickAnySession: false });
}
function saveCurrentSessionToStore() {
  if (!currentSessionId) return;
  var session = chatSessions.find(function(s) { return String(s.id) === String(currentSessionId); });
  if (session) {
    session.messages = Array.isArray(chatHistory) ? chatHistory.slice() : [];
    session.updatedAt = Date.now();
    if (session.messages.length) {
      var firstUser = session.messages.find(function(m) { return m && m.role === 'user'; });
      if (firstUser && (firstUser.content || '').trim()) session.title = getSessionTitle(session);
    }
    saveChatSessionsToStorage();
  }
}
window.addEventListener('beforeunload', function() {
  flushPendingChatSessionsSave();
  abortActiveChatStream();
  if (typeof saveCurrentSessionToStore === 'function') saveCurrentSessionToStore();
});
window.addEventListener('pagehide', function() {
  abortActiveChatStream();
});
function renderChatSessionList() {
  var listEl = document.getElementById('chatSessionList');
  var searchVal = (document.getElementById('chatSessionSearch') && document.getElementById('chatSessionSearch').value || '').trim().toLowerCase();
  if (!listEl) return;
  var filtered = searchVal
    ? chatSessions.filter(function(s) {
        var title = getSessionTitle(s); var preview = getSessionPreview(s);
        return title.toLowerCase().indexOf(searchVal) >= 0 || preview.toLowerCase().indexOf(searchVal) >= 0;
      })
    : chatSessions.slice();
  if (filtered.length === 0) {
    listEl.innerHTML = '<p class="meta" style="padding:0.5rem;font-size:0.8rem;color:var(--text-muted);">暂无对话</p>';
    return;
  }
  listEl.innerHTML = filtered.map(function(s) {
    var title = getSessionTitle(s);
    var preview = getSessionPreview(s);
    var time = formatSessionTime(s.updatedAt);
    var active = s.id === currentSessionId ? ' active' : '';
    var pendingDot = isSessionPending(s.id) ? '<span class="session-pending-dot" title="任务进行中"></span>' : '';
    return '<div class="chat-session-item' + active + '" data-session-id="' + escapeAttr(s.id) + '">' +
      '<div class="session-title">' + pendingDot + escapeHtml(title) + '</div>' +
      '<div class="session-preview">' + escapeHtml(preview) + '</div>' +
      '<div class="session-time">' + escapeHtml(time) + '</div></div>';
  }).join('');
  listEl.querySelectorAll('.chat-session-item').forEach(function(el) {
    el.addEventListener('click', function() { switchChatSession(el.getAttribute('data-session-id')); });
  });
}
function initChatSessions() {
  loadChatSessionsFromStorage();
  if (chatSessions.length === 0) {
    createNewSession();
    return;
  }
  // 登录后 currentSessionId 已被清空：优先恢复「上次正在看的会话」，再考虑带 poll_resume 的会话。
  // 若用全局最新 poll_resume_at 选会话，会话1 轮询会不断刷新时间戳，刷新页面后会盖住正在会话2 里做的任务。
  var lastSid = getLastActiveChatSessionIdFromStorage();
  var targetId = '';
  if (lastSid && chatSessions.some(function(s) { return String(s.id) === lastSid; })) {
    targetId = lastSid;
  } else {
    var resumeSid = _pickSessionIdNeedingPollResume(null, true);
    if (resumeSid && chatSessions.some(function(s) { return String(s.id) === resumeSid; })) {
      targetId = resumeSid;
    } else if (chatSessions[0]) {
      targetId = String(chatSessions[0].id);
    }
  }
  if (!targetId) {
    createNewSession();
    return;
  }
  currentSessionId = null;
  setTimeout(function() {
    if (document.getElementById('chatMessages')) switchChatSession(targetId);
    renderChatSessionList();
  }, 0);
}

/** 模型常用 Markdown 行内代码包裹 URL，导致无法点击；去掉 URL 两侧反引号 */
function stripBackticksAroundUrls(text) {
  if (!text) return text;
  var t = text;
  t = t.replace(/`+\s*(https?:\/\/[^\s`<>]+)\s*`+/gi, '$1');
  t = t.replace(/`+\s*(https?:\/\/[^\s`<>]+)/gi, '$1');
  t = t.replace(/(https?:\/\/[^\s`<>]+)\s*`+/g, '$1');
  return t;
}

/** 正文中显式写的素材 ID（与上传附图合并展示） */
function extractAssetIdsFromUserMessageText(text) {
  var found = [];
  var seen = {};
  if (!text) return found;
  var patterns = [
    /asset_id[：:\s]+([a-f0-9]{12})\b/gi,
    /素材\s*ID[：:\s]*([a-f0-9]{12})\b/gi,
    // 「素材3108855349d9」「素材 3108855349d9」等（与 mergeUserMessageAssetIds 展示一致，并随请求带上附图 ID）
    /素材\D*([a-f0-9]{12})\b/gi
  ];
  patterns.forEach(function(re) {
    var m;
    var r = new RegExp(re.source, re.flags);
    while ((m = r.exec(text)) !== null) {
      var id = (m[1] || '').toLowerCase();
      if (id && !seen[id]) {
        seen[id] = true;
        found.push(id);
      }
    }
  });
  return found;
}

function mergeUserMessageAssetIds(attachmentIds, content) {
  var seen = {};
  var out = [];
  (attachmentIds || []).forEach(function(id) {
    id = String(id || '').trim().toLowerCase();
    if (!id || seen[id]) return;
    seen[id] = true;
    out.push(id);
  });
  extractAssetIdsFromUserMessageText(content || '').forEach(function(id) {
    if (!seen[id]) {
      seen[id] = true;
      out.push(id);
    }
  });
  return out;
}

function linkifyText(text) {
  var raw = stripBackticksAroundUrls(text || '');
  var escaped = raw.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    var result = escaped.replace(/https?:\/\/[^\s<>"'`]+/g, function(raw) {
    var url = raw;
    var suffix = '';
    while (/[)\]}\u3002\uff0c\uff01\uff1f,.]$/.test(url)) {
      if (url.endsWith(')')) {
        var opens = (url.match(/\(/g) || []).length;
        var closes = (url.match(/\)/g) || []).length;
        if (closes <= opens) break;
      }
      suffix = url.slice(-1) + suffix;
      url = url.slice(0, -1);
    }
    var rewritten = url.replace(/^https?:\/\/(?:localhost|127\.0\.0\.1):8000\/media\//, window.location.origin + '/media/');
    return '<a href="' + rewritten + '" target="_blank" rel="noopener noreferrer">' + rewritten + '</a>' + suffix;
  });
  result = result.replace(/(^|[^a-zA-Z0-9/">=])\/media\/[^\s<>"'`]+/g, function(match, prefix) {
    var path = match.slice(prefix.length);
    var full = window.location.origin + path;
    return prefix + '<a href="' + full + '" target="_blank" rel="noopener noreferrer">' + full + '</a>';
  });
  return result;
}

function appendUserMessageDisplay(content, attachmentAssetIds) {
  var container = document.getElementById('chatMessages');
  if (!container) return;
  var div = document.createElement('div');
  div.className = 'chat-msg user';
  var roleDiv = document.createElement('div');
  roleDiv.className = 'role';
  roleDiv.textContent = '我';
  var bodyDiv = document.createElement('div');
  bodyDiv.className = 'chat-msg-body';
  div.appendChild(roleDiv);
  div.appendChild(bodyDiv);
  var ids = mergeUserMessageAssetIds(attachmentAssetIds, content);
  if (ids.length) {
    var refsWrap = document.createElement('div');
    refsWrap.className = 'chat-user-refs';
    var base = (typeof LOCAL_API_BASE !== 'undefined' && LOCAL_API_BASE) ? String(LOCAL_API_BASE).replace(/\/$/, '') : '';
    ids.forEach(function(aid) {
      var box = document.createElement('div');
      box.className = 'chat-user-ref-box';
      var cap = document.createElement('div');
      cap.className = 'chat-user-ref-id';
      cap.textContent = aid;
      box.appendChild(cap);
      if (base && typeof authHeaders === 'function') {
        fetch(base + '/api/assets/' + encodeURIComponent(aid) + '/content', { headers: authHeaders() })
          .then(function(r) {
            if (!r.ok) throw new Error('no');
            return r.blob();
          })
          .then(function(blob) {
            var u = URL.createObjectURL(blob);
            box._blobUrl = u;
            if ((blob.type || '').indexOf('video') >= 0) {
              var v = document.createElement('video');
              v.src = u;
              v.muted = true;
              v.playsInline = true;
              v.controls = true;
              v.preload = 'metadata';
              box.insertBefore(v, cap);
            } else {
              var img = document.createElement('img');
              img.src = u;
              img.alt = aid;
              box.insertBefore(img, cap);
            }
          })
          .catch(function() {
            cap.textContent = '素材 ' + aid + '（预览失败）';
          });
      } else {
        cap.textContent = '素材 ' + aid;
      }
      refsWrap.appendChild(box);
    });
    bodyDiv.appendChild(refsWrap);
  }
  var textDiv = document.createElement('div');
  textDiv.className = 'chat-msg-text';
  var text = (content || '').trim() || (ids.length ? '' : '（无内容）');
  if (text) textDiv.innerHTML = linkifyText(text);
  bodyDiv.appendChild(textDiv);
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

function appendChatMessage(role, content) {
  if (role === 'user') {
    appendUserMessageDisplay(content, null);
    return;
  }
  var container = document.getElementById('chatMessages');
  if (!container) return;
  var div = document.createElement('div');
  div.className = 'chat-msg ' + role;
  var text = role === 'assistant' ? _compactAssistantReplyForDisplay(content, null) : (content || '');
  text = (text || '').trim() || '（无内容）';
  var html = linkifyText(text);
  div.innerHTML = '<div class="role">' + (role === 'user' ? '我' : '龙虾') + '</div>' + html;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}
var _toolNameLabels = {
  invoke_capability: '调用能力',
  save_asset: '保存素材',
  publish_content: '发布内容',
  publish_youtube_video: '上传到 YouTube',
  list_youtube_accounts: 'YouTube 账号列表',
  get_youtube_analytics: 'YouTube 数据',
  sync_youtube_analytics: '同步 YouTube 数据',
  list_meta_social_accounts: 'IG/FB 账号列表',
  publish_meta_social: '发布到 IG/FB',
  get_meta_social_data: '读取 IG/FB 数据',
  sync_meta_social_data: '同步 IG/FB 数据',
  get_social_report: '社交媒体报告',
  list_assets: '查看素材',
  list_publish_accounts: '查看账号',
  check_account_login: '检查登录',
  open_account_browser: '打开浏览器'
};
var _capabilityLabels = {
  'image.generate': '生成图片',
  'image.understand': '理解图片',
  'video.generate': '生成视频',
  'video.understand': '理解视频',
  'task.get_result': '查询结果',
  'media.edit': '素材编辑',
  'sutui.search_models': '搜索模型',
  'sutui.guide': '查询指南',
  'sutui.transfer_url': '转存链接',
  // 新名（首选）
  'comfly.daihuo': '爆款TVC 单段',
  'comfly.daihuo.pipeline': '爆款TVC 带货视频',
  // 老名兼容（历史 task 仍用老 capability_id 显示）
  'comfly.veo': '爆款TVC 单段',
  'comfly.veo.daihuo_pipeline': '爆款TVC 带货视频',
  'comfly.ecommerce.detail_pipeline': '电商详情页'
};
function _toolLabel(name, capId) {
  if (name === 'invoke_capability' && capId && _capabilityLabels[capId]) return _capabilityLabels[capId];
  return _toolNameLabels[name] || name;
}

function showChatTypingIndicator() {
  var container = document.getElementById('chatMessages');
  if (!container) return;
  removeChatTypingIndicator();
  var div = document.createElement('div');
  div.id = 'chatTypingIndicator';
  div.className = 'chat-msg assistant typing chat-typing-indicator';
  div.innerHTML = '<div class="role">龙虾</div><div class="typing-dots"><span></span><span></span><span></span></div> <span class="typing-text">正在处理…</span><div class="typing-steps" id="chatTypingSteps"></div>';
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}
function appendChatTypingStep(text) {
  var steps = document.getElementById('chatTypingSteps');
  if (!steps) return;
  var line = document.createElement('div');
  line.className = 'typing-step';
  line.style.cssText = 'font-size:0.82rem;color:var(--text-muted);margin-top:0.35rem;';
  line.textContent = text;
  steps.appendChild(line);
  var container = document.getElementById('chatMessages');
  if (container) container.scrollTop = container.scrollHeight;
}
function updateLastChatTypingStep(text) {
  var steps = document.getElementById('chatTypingSteps');
  if (!steps || !steps.lastElementChild) return;
  if (steps.lastElementChild.classList.contains('chat-generated-assets-preview')) {
    appendChatTypingStep(text);
    return;
  }
  steps.lastElementChild.textContent = text;
  var container = document.getElementById('chatMessages');
  if (container) container.scrollTop = container.scrollHeight;
}
function setChatTypingMainText(text) {
  var el = document.querySelector('#chatTypingIndicator .typing-text');
  if (el) el.textContent = text || '正在处理…';
}
/** task_poll：完整展示后端 message；附加 result_hint（与正文重复则不加）。 */
function _formatTaskPollTypingLine(ev) {
  var msg = String(ev.message || '').trim();
  var line = msg || '正在查询生成结果…';
  if (ev.result_hint) {
    var h = String(ev.result_hint);
    if (line.indexOf(h) === -1) line += ' · ' + h;
  }
  return line;
}

function getLocalApiBaseForAssets() {
  var b = (typeof LOCAL_API_BASE !== 'undefined' && LOCAL_API_BASE) ? String(LOCAL_API_BASE).replace(/\/$/, '') : '';
  return b;
}

/** 会话素材「显示内容」：优先 prompt/说明，否则缩短后的链接 */
function savedAssetDisplayText(a) {
  if (!a) return '';
  var p = (a.prompt || a.caption || a.description || a.title || a.label || '').trim();
  if (p) return p;
  var u = (a.source_url || a.url || '').trim();
  if (!u) return '';
  return u.length > 140 ? u.slice(0, 140) + '…' : u;
}

function savedAssetPrimaryHttpUrl(a) {
  return ((a && (a.source_url || a.url)) || '').trim();
}

function scrollChatMessagesToBottom() {
  var container = document.getElementById('chatMessages');
  if (container) container.scrollTop = container.scrollHeight;
}

/**
 * 流式/终态：展示素材 ID、显示内容，并优先用本机 GET /api/assets/:id/content 加载预览（图/视频 blob）。
 */
function appendSavedAssetDom(parent, a, opts) {
  if (!parent || !a) return;
  opts = opts || {};
  var compact = !!opts.compact;
  var assetId = (a.asset_id || '').trim();
  var mediaType = (a.media_type || 'image').toLowerCase();
  var box = document.createElement('div');
  box.className = 'chat-generated-asset-item';

  var idEl = document.createElement('div');
  idEl.className = 'chat-generated-asset-id';
  var httpUrlEarly = savedAssetPrimaryHttpUrl(a);
  var pendingDedup = (!assetId && httpUrlEarly) ? _pendingAssetDedupAttrKey(httpUrlEarly) : '';
  if (pendingDedup) box.setAttribute('data-pending-asset-dedup', pendingDedup);
  if (assetId) {
    idEl.textContent = '素材 ID · ' + assetId;
  } else if (pendingDedup) {
    idEl.textContent = '素材（正在写入素材库…）';
  } else {
    idEl.textContent = '素材（未入库 ID）';
  }
  box.appendChild(idEl);

  var disp = savedAssetDisplayText(a);
  if (disp) {
    var metaEl = document.createElement('div');
    metaEl.className = 'chat-generated-asset-meta';
    metaEl.textContent = '内容 · ' + disp;
    box.appendChild(metaEl);
  }

  var mediaWrap = document.createElement('div');
  mediaWrap.className = 'chat-generated-asset-media';

  var loadingEl = document.createElement('div');
  loadingEl.className = 'chat-generated-asset-loading';
  loadingEl.style.cssText = 'font-size:0.8rem;color:var(--text-muted);';
  loadingEl.textContent = assetId ? '正在加载本地预览…' : '正在加载预览…';
  mediaWrap.appendChild(loadingEl);

  var maxH = compact ? '140px' : '200px';
  var maxHVid = compact ? '160px' : '220px';

  function removeLoading() {
    if (loadingEl.parentNode) loadingEl.parentNode.removeChild(loadingEl);
  }

  function appendBlobPreview(blob) {
    removeLoading();
    var t = (blob.type || '').toLowerCase();
    var asVideo = (mediaType === 'video') || /^video\//.test(t) || /\.(mp4|webm|mov|m4v)(\?|$)/i.test(savedAssetPrimaryHttpUrl(a) || '');
    var u = URL.createObjectURL(blob);
    box._blobUrl = u;
    if (asVideo) {
      var v = document.createElement('video');
      v.src = u;
      v.controls = true;
      v.playsInline = true;
      v.preload = 'metadata';
      v.style.cssText = 'width:100%;max-height:' + maxHVid + ';border-radius:6px;background:#000;cursor:pointer;';
      v.addEventListener('dblclick', function() { if (v.requestFullscreen) v.requestFullscreen(); });
      mediaWrap.appendChild(v);
    } else {
      var link = document.createElement('a');
      var openUrl = savedAssetPrimaryHttpUrl(a) || u;
      link.href = openUrl;
      link.target = '_blank';
      link.rel = 'noopener';
      link.title = '点击在新标签页查看大图';
      var img = document.createElement('img');
      img.src = u;
      img.alt = assetId || '素材';
      img.style.cssText = 'width:100%;max-height:' + maxH + ';object-fit:contain;border-radius:6px;display:block;cursor:pointer;';
      link.appendChild(img);
      mediaWrap.appendChild(link);
    }
    scrollChatMessagesToBottom();
  }

  function appendHttpPreview(url) {
    removeLoading();
    var u = url;
    var looksVideo = (mediaType === 'video') || /\.(mp4|webm|mov|m4v)(\?|$)/i.test(u);
    if (looksVideo) {
      var v = document.createElement('video');
      v.src = u;
      v.controls = true;
      v.playsInline = true;
      v.preload = 'metadata';
      v.style.cssText = 'width:100%;max-height:' + maxHVid + ';border-radius:6px;background:#000;cursor:pointer;';
      v.addEventListener('dblclick', function() { if (v.requestFullscreen) v.requestFullscreen(); });
      mediaWrap.appendChild(v);
    } else {
      var link = document.createElement('a');
      link.href = u;
      link.target = '_blank';
      link.rel = 'noopener';
      link.title = '点击在新标签页查看大图';
      var img = document.createElement('img');
      img.src = u;
      img.alt = assetId || '素材';
      img.style.cssText = 'width:100%;max-height:' + maxH + ';object-fit:contain;border-radius:6px;display:block;cursor:pointer;';
      link.appendChild(img);
      mediaWrap.appendChild(link);
    }
    scrollChatMessagesToBottom();
  }

  function showLoadError(fallbackUrl) {
    removeLoading();
    var err = document.createElement('div');
    err.style.cssText = 'font-size:0.78rem;color:var(--text-muted);margin-bottom:0.25rem;';
    err.textContent = '本地预览不可用';
    mediaWrap.appendChild(err);
    if (fallbackUrl) appendHttpPreview(fallbackUrl);
    else if (savedAssetPrimaryHttpUrl(a)) appendHttpPreview(savedAssetPrimaryHttpUrl(a));
    else scrollChatMessagesToBottom();
  }

  box.appendChild(mediaWrap);
  parent.appendChild(box);

  var base = getLocalApiBaseForAssets();
  if (assetId && base && typeof authHeaders === 'function') {
    fetch(base + '/api/assets/' + encodeURIComponent(assetId) + '/content', { headers: authHeaders() })
      .then(function(r) {
        if (!r.ok) throw new Error('bad');
        return r.blob();
      })
      .then(appendBlobPreview)
      .catch(function() {
        showLoadError(savedAssetPrimaryHttpUrl(a) || '');
      });
  } else {
    var httpUrl = savedAssetPrimaryHttpUrl(a);
    if (httpUrl) {
      appendHttpPreview(httpUrl);
    } else {
      removeLoading();
      var hint = document.createElement('div');
      hint.style.cssText = 'font-size:0.78rem;color:var(--text-muted);';
      hint.textContent = assetId ? '无可用预览（请确认已登录且素材在本机）' : '无预览地址';
      mediaWrap.appendChild(hint);
      scrollChatMessagesToBottom();
    }
  }

  scrollChatMessagesToBottom();
}

function appendChatGeneratedAssetsToTyping(assets) {
  var steps = document.getElementById('chatTypingSteps');
  if (!steps || !assets || !assets.length) return;
  var wrap = document.createElement('div');
  wrap.className = 'chat-generated-assets-preview';
  wrap.style.cssText = 'margin-top:0.5rem;display:flex;flex-wrap:wrap;gap:0.5rem;align-items:flex-start;';
  assets.forEach(function(a) {
    appendSavedAssetDom(wrap, a, { compact: true });
  });
  steps.appendChild(wrap);
  var container = document.getElementById('chatMessages');
  if (container) container.scrollTop = container.scrollHeight;
}
function removeChatTypingIndicator() {
  var container = document.getElementById('chatMessages');
  if (container) {
    var list = container.querySelectorAll('.chat-typing-indicator');
    for (var i = 0; i < list.length; i++) {
      var n = list[i];
      if (n.parentNode) n.parentNode.removeChild(n);
    }
  }
  var el;
  while ((el = document.getElementById('chatTypingIndicator'))) {
    if (el.parentNode) el.parentNode.removeChild(el);
  }
}
/** 将 task.get_result 等大段 JSON 压成简短说明（素材仍以卡片展示） */
function _compactAssistantReplyForDisplay(fullText, savedAssets) {
  var t = String(fullText || '').trim();
  if (!t) return t;
  var toParse = t;
  var prefix = '';
  var code = t.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (code) toParse = (code[1] || '').trim();
  else {
    var br0 = t.indexOf('{');
    var br1 = t.lastIndexOf('}');
    if (br0 >= 0 && br1 > br0) {
      if (br0 > 0) prefix = t.slice(0, br0).trim();
      toParse = t.slice(br0, br1 + 1);
    }
  }
  var obj;
  try {
    obj = JSON.parse(toParse);
  } catch (e) {
    return t;
  }
  if (!obj || typeof obj !== 'object' || obj.capability_id !== 'task.get_result' || typeof obj.result !== 'object')
    return t;
  var r = obj.result;
  var st = String(r.status || '').toLowerCase();
  var prompt = '';
  if (r.params && r.params.prompt) prompt = String(r.params.prompt).trim();
  else if (r.output && r.output.prompt) prompt = String(r.output.prompt).trim();
  var hasAssets = savedAssets && savedAssets.length;
  if (st === 'completed') {
    var short = hasAssets
      ? (prompt ? '已生成：' + (prompt.length > 120 ? prompt.slice(0, 120) + '…' : prompt) : '图片已生成，见上方素材卡片。')
      : (prompt ? '已完成：' + (prompt.length > 100 ? prompt.slice(0, 100) + '…' : prompt) : '任务已完成。');
    if (prefix) return prefix + '\n\n' + short;
    return short;
  }
  if (st === 'failed' || st === 'error' || st === 'cancelled') {
    return prefix ? prefix + '\n\n生成未成功。' : '生成未成功。';
  }
  return t;
}
function appendAssistantMessageReveal(fullText, savedAssets) {
  var container = document.getElementById('chatMessages');
  if (!container) return;
  var text = _compactAssistantReplyForDisplay(fullText, savedAssets);
  text = (text || '').trim() || '（无内容）';
  var lines = text.split('\n');
  var div = document.createElement('div');
  div.className = 'chat-msg assistant';
  var roleDiv = document.createElement('div');
  roleDiv.className = 'role';
  roleDiv.textContent = '龙虾';
  var bodyDiv = document.createElement('div');
  bodyDiv.className = 'chat-msg-body';
  div.appendChild(roleDiv);
  div.appendChild(bodyDiv);
  if (savedAssets && savedAssets.length) {
    var assetsWrap = document.createElement('div');
    assetsWrap.className = 'chat-generated-assets';
    assetsWrap.style.cssText = 'display:flex;flex-wrap:wrap;gap:0.5rem;margin-bottom:0.75rem;align-items:flex-start;';
    savedAssets.forEach(function(a) {
      appendSavedAssetDom(assetsWrap, a, { compact: false });
    });
    bodyDiv.appendChild(assetsWrap);
  }
  container.appendChild(div);
  var lineDelay = 150;
  var i = 0;
  function showNext() {
    if (i >= lines.length) {
      container.scrollTop = container.scrollHeight;
      return;
    }
    var line = lines[i];
    var lineEl = document.createElement('div');
    lineEl.className = 'chat-msg-line';
    lineEl.innerHTML = linkifyText(line);
    bodyDiv.appendChild(lineEl);
    i++;
    container.scrollTop = container.scrollHeight;
    if (i < lines.length) setTimeout(showNext, lineDelay);
  }
  if (lines.length) setTimeout(showNext, lineDelay); else container.scrollTop = container.scrollHeight;
}
function renderChatAttachments() {
  var container = document.getElementById('chatAttachments');
  if (!container) return;
  if (chatAttachmentIds.length === 0) {
    container.style.display = 'none';
    container.innerHTML = '';
    return;
  }
  container.style.display = 'flex';
  container.innerHTML = '';
  chatAttachmentInfos.forEach(function(info, idx) {
    var wrap = document.createElement('div');
    wrap.className = 'chat-attach-item';
    if (info.media_type === 'video') {
      var v = document.createElement('video');
      v.src = info.previewUrl || '';
      v.muted = true;
      v.playsInline = true;
      wrap.appendChild(v);
    } else {
      var img = document.createElement('img');
      img.src = info.previewUrl || '';
      img.alt = '附件';
      wrap.appendChild(img);
    }
    var rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'attach-remove';
    rm.textContent = '×';
    rm.setAttribute('data-idx', String(idx));
    rm.addEventListener('click', function() {
      var i = parseInt(rm.getAttribute('data-idx'), 10);
      if (chatAttachmentInfos[i] && chatAttachmentInfos[i].previewUrl) {
        try { URL.revokeObjectURL(chatAttachmentInfos[i].previewUrl); } catch (e) {}
      }
      chatAttachmentIds.splice(i, 1);
      chatAttachmentInfos.splice(i, 1);
      renderChatAttachments();
    });
    wrap.appendChild(rm);
    container.appendChild(wrap);
  });
}
function addChatAttachment(assetId, mediaType) {
  chatAttachmentIds.push(assetId);
  var info = { asset_id: assetId, media_type: mediaType || 'image', previewUrl: '' };
  chatAttachmentInfos.push(info);
  fetch((typeof LOCAL_API_BASE !== 'undefined' ? LOCAL_API_BASE : '') + '/api/assets/' + assetId + '/content', { headers: authHeaders() })
    .then(function(r) { return r.blob(); })
    .then(function(blob) {
      info.previewUrl = URL.createObjectURL(blob);
      renderChatAttachments();
    })
    .catch(function() { renderChatAttachments(); });
}
function _canonicalAssetSaveKey(url) {
  if (!url) return '';
  var s = String(url).split('?')[0].split('#')[0];
  var sl = s.toLowerCase();
  if (sl.indexOf('/v3-tasks/') >= 0) return s;
  if (s.indexOf('/assets/') >= 0) return s;
  return s;
}
/** 与 appendSavedAssetDom 的 data-pending-asset-dedup 一致，便于 save-url 完成后回写 UI */
function _pendingAssetDedupAttrKey(rawUrl) {
  var k = _canonicalAssetSaveKey(rawUrl);
  return k ? encodeURIComponent(k) : '';
}
function _updateChatAssetDomAfterSaveUrl(attrKey, newAssetId) {
  if (!attrKey || !newAssetId) return;
  try {
    var boxes = document.querySelectorAll('[data-pending-asset-dedup="' + attrKey + '"]');
    for (var i = 0; i < boxes.length; i++) {
      var box = boxes[i];
      var idEl = box.querySelector('.chat-generated-asset-id');
      if (idEl) idEl.textContent = '素材 ID · ' + newAssetId;
      box.removeAttribute('data-pending-asset-dedup');
    }
  } catch (e) {}
}
function saveGeneratedAssetsToLocal(assets, dedupSet) {
  if (!assets || !assets.length) return;
  var base = ((typeof LOCAL_API_BASE !== 'undefined' ? LOCAL_API_BASE : '') || '');
  var headers = Object.assign({ 'Content-Type': 'application/json' }, typeof authHeaders === 'function' ? authHeaders() : { 'Authorization': 'Bearer ' + (typeof token !== 'undefined' ? token : '') });
  var seen = dedupSet || {};
  assets.forEach(function(a) {
    if (!a) return;
    if (a.asset_id) return;
    var rawUrl = (a.url || a.source_url || '').trim();
    if (!rawUrl) return;
    var dedupKey = _canonicalAssetSaveKey(rawUrl);
    if (seen[dedupKey]) return;
    seen[dedupKey] = true;
    var attrKey = _pendingAssetDedupAttrKey(rawUrl);
    var tagStr = (a.tags && String(a.tags).trim()) ? String(a.tags).trim() : 'auto,task.get_result';
    var saveBody = { url: rawUrl, media_type: (a.media_type || 'image'), tags: tagStr };
    if (a.prompt && String(a.prompt).trim()) saveBody.prompt = String(a.prompt).trim().slice(0, 500);
    if (a.model && String(a.model).trim()) saveBody.model = String(a.model).trim().slice(0, 128);
    if (a.generation_task_id && String(a.generation_task_id).trim()) saveBody.generation_task_id = String(a.generation_task_id).trim().slice(0, 128);
    fetch(base + '/api/assets/save-url', {
      method: 'POST',
      headers: headers,
      body: JSON.stringify(saveBody)
    })
      .then(function(r) {
        if (!r.ok) return null;
        return r.json();
      })
      .then(function(d) {
        if (!d || !d.asset_id) return;
        a.asset_id = d.asset_id;
        if (d.source_url) a.source_url = d.source_url;
        if (attrKey) _updateChatAssetDomAfterSaveUrl(attrKey, d.asset_id);
      })
      .catch(function() {});
  });
}
function clearChatAttachments() {
  chatAttachmentInfos.forEach(function(info) {
    if (info.previewUrl) try { URL.revokeObjectURL(info.previewUrl); } catch (e) {}
  });
  chatAttachmentIds = [];
  chatAttachmentInfos = [];
  renderChatAttachments();
}

/** 刷新后根据 localStorage 中的 poll_resume_task_id 自动续连 /chat/stream（仅轮询，不重复插入 user） */
var _maybeAutoResumeDebounceTimer = null;
/** 下一帧 maybeAutoResumeChatTaskPollRun 是否允许扫描全部会话（默认不再全局扫，避免刷新后误续查其它会话的旧 task） */
var _resumePollPickAnyOnce = false;
function maybeAutoResumeChatTaskPoll(opts) {
  if (opts && opts.pickAnySession) _resumePollPickAnyOnce = true;
  if (_maybeAutoResumeDebounceTimer != null) clearTimeout(_maybeAutoResumeDebounceTimer);
  _maybeAutoResumeDebounceTimer = setTimeout(function() {
    _maybeAutoResumeDebounceTimer = null;
    maybeAutoResumeChatTaskPollRun();
  }, 200);
}
function maybeAutoResumeChatTaskPollRun() {
  if (window._chatResumePollInFlight && !chatStreamAbortController) window._chatResumePollInFlight = false;
  if (window._chatResumePollInFlight) return;
  var chatBase = _chatStreamApiBase();
  if (!chatBase) return;
  var pickAny = _resumePollPickAnyOnce;
  _resumePollPickAnyOnce = false;
  var needSid = _pickSessionIdNeedingPollResume(String(currentSessionId || ''), pickAny);
  if (!needSid) return;
  if (String(currentSessionId || '') !== needSid) {
    switchChatSession(needSid);
  }
  var sid = String(currentSessionId || '');
  var s = getSessionById(sid);
  if (!s || !s.poll_resume_task_id) return;
  var age = Date.now() - (s.poll_resume_at || 0);
  if (age > _POLL_RESUME_MAX_AGE_MS) {
    clearSessionPollResume(sid);
    setSessionPending(sid, false);
    return;
  }
  resumeChatStreamForTaskPoll(sid, String(s.poll_resume_task_id).trim());
}

/** 刷新后续查时「速推 LLM」子下拉可能尚未异步加载，不能因无 value 放弃 /chat/stream */
function _resolveModelForResumePoll() {
  if (_isOnlineFixedSutuiChat()) return _onlineFixedChatModelPayload();
  var modelSel = document.getElementById('modelSelect');
  var model = modelSel ? (modelSel.value || '') : '';
  if (model !== 'sutui_aggregate') return model;
  var subSel = document.getElementById('sutuiModelSelect');
  var subId = '';
  if (subSel && subSel.value) subId = String(subSel.value).trim();
  else if (subSel && subSel.options && subSel.options.length)
    subId = String(subSel.options[0].value || '').trim();
  if (!subId) {
    try {
      subId = (localStorage.getItem('lobster_last_sutui_submodel') || '').trim();
    } catch (e) {}
  }
  if (!subId) subId = 'deepseek-chat';
  return 'sutui/' + subId;
}

function resumeChatStreamForTaskPoll(sid, taskId) {
  var tid = (taskId || '').trim();
  if (!tid) return;
  if (window._chatResumePollInFlight && !chatStreamAbortController) window._chatResumePollInFlight = false;
  if (window._chatResumePollInFlight) return;
  var chatBase = _chatStreamApiBase();
  if (!chatBase) return;
  abortActiveChatStream();
  window._chatResumePollInFlight = true;
  var session = getSessionById(sid);
  if (!session) {
    window._chatResumePollInFlight = false;
    return;
  }
  var model = _resolveModelForResumePoll();
  var body = {
    message: '（页面恢复后继续查询生成进度）',
    history: Array.isArray(session.messages) ? session.messages.slice() : [],
    session_id: sid,
    context_id: null,
    model: model || undefined,
    resume_task_poll_task_id: tid
  };
  var bodyStr = JSON.stringify(body);
  var headers = authHeaders();
  headers['Content-Type'] = 'application/json';
  var taskPollingCompleted = false;
  var videoGeneratedShown = false;
  var streamGeneratedAssets = [];
  var savedAssetUrls = {};
  var taskPollLocalSaveDone = false;
  var assetsPreviewAppended = false;
  var streamAbortedByUser = false;
  var resumeAbortReason = null;
  chatStreamSutuiSubmitted = false;
  var abortController = new AbortController();
  chatStreamAbortController = abortController;
  var resumeDeadlineTimer = window.setTimeout(function() {
    resumeAbortReason = 'deadline';
    try {
      abortController.abort();
    } catch (eDeadline) {}
  }, _RESUME_CHAT_STREAM_MAX_MS);
  refreshChatInputState();
  setSessionPending(sid, true);
  if (String(currentSessionId) === sid) {
    var cm = document.getElementById('chatMessages');
    if (!cm || !cm.querySelector('.chat-typing-indicator')) showChatTypingIndicator();
  }
  var streamKindResume = true;
  fetch(chatBase + '/chat/stream', { method: 'POST', headers: headers, body: bodyStr, signal: abortController.signal })
    .then(function(r) {
      if (!r.ok) {
        return r.json().then(function(d) { throw { status: r.status, detail: (d && d.detail) || r.statusText }; });
      }
      if (!r.body) throw new Error('No body');
      var decoder = new TextDecoder();
      var buf = '';
      var reader = r.body.getReader();
      function processChunk(result) {
        if (result.done) return Promise.resolve(null);
        buf += decoder.decode(result.value, { stream: true });
        var parts = buf.split('\n\n');
        buf = parts.pop() || '';
        for (var i = 0; i < parts.length; i++) {
          var block = parts[i];
          var dataLine = block.split('\n').filter(function(l) { return l.indexOf('data:') === 0; })[0];
          if (!dataLine) continue;
          try {
            var ev = JSON.parse(dataLine.slice(5).trim());
            if (ev.type === 'capability_cost_confirm' && String(currentSessionId) === sid) {
              var capId = ev.capability_id || '';
              var cr = ev.estimated_credits;
              var note = (ev.estimate_note || '').trim();
              var secLeft = ev.timeout_seconds != null ? ev.timeout_seconds : 300;
              var cn = (cr != null && cr !== '') ? Number(cr) : NaN;
              var creditLine = !isNaN(cn) && cn > 0 ? String(cn) : (!isNaN(cn) && cn === 0 ? '0' : '未知');
              appendChatTypingStep('等待确认…');
              return openChatCapabilityCostConfirm({
                capability_id: capId,
                invoke_model: ev.invoke_model || '',
                credit_display: creditLine,
                note: note,
                timeout_seconds: secLeft
              }).then(function(userAccept) {
                var tokenBody = JSON.stringify({
                  confirm_token: String(ev.confirm_token || '').trim(),
                  accept: !!userAccept
                });
                var confirmHdr = Object.assign({}, headers, { 'Content-Type': 'application/json' });
                return fetch(chatBase + '/capabilities/confirm-invoke', {
                  method: 'POST',
                  headers: confirmHdr,
                  body: tokenBody
                }).catch(function() {
                  return fetch(chatBase + '/capabilities/confirm-invoke', {
                    method: 'POST',
                    headers: confirmHdr,
                    body: JSON.stringify({
                      confirm_token: String(ev.confirm_token || '').trim(),
                      accept: false
                    })
                  });
                }).then(function() {
                  if (String(currentSessionId) === sid) {
                    updateLastChatTypingStep(userAccept ? '已确认，继续执行…' : '已取消本次能力调用');
                  }
                  return reader.read().then(processChunk);
                });
              });
            } else if (ev.type === 'tool_start') {
              var _isAct = String(currentSessionId) === sid;
              if (ev.task_id) persistSessionPollResumeTaskId(sid, ev.task_id);
              if (ev.name === 'list_capabilities') {
                _saveSessionTypingState(sid, null, '正在查询可用能力…', 'append');
                if (_isAct) appendChatTypingStep('正在查询可用能力…');
              } else if (ev.phase === 'video_submit') {
                _saveSessionTypingState(sid, null, '正在提交视频生成任务…', 'append');
                if (_isAct) appendChatTypingStep('正在提交视频生成任务…');
              } else if (ev.phase === 'image_submit') {
                _saveSessionTypingState(sid, null, '正在提交图片生成任务…', 'append');
                if (_isAct) appendChatTypingStep('正在提交图片生成任务…');
              } else if (ev.phase === 'task_polling') {
                chatStreamSutuiSubmitted = true;
                if (_isAct) refreshChatInputState();
                var _pollMain = streamKindResume
                  ? '正在恢复并查询生成结果（约每 15 秒更新；超过 ' +
                      Math.round(_RESUME_CHAT_STREAM_MAX_MS / 60000) +
                      ' 分钟仍未结束将自动停止恢复，任务可能在后台继续）…'
                  : '正在查询生成结果（约每 15 秒自动更新）…';
                _saveSessionTypingState(sid, _pollMain, null, null);
                if (_isAct) setChatTypingMainText(_pollMain);
              } else {
                var _sStep = '正在 ' + _toolLabel(ev.name, ev.capability_id) + '…';
                _saveSessionTypingState(sid, null, _sStep, 'append');
                if (_isAct) appendChatTypingStep(_sStep);
              }
            } else if (ev.type === 'tool_end') {
              var _isAct2 = String(currentSessionId) === sid;
              if (ev.task_id) persistSessionPollResumeTaskId(sid, ev.task_id);
              if ((ev.phase === 'image_submit' || ev.phase === 'video_submit') && ev.success !== false) {
                chatStreamSutuiSubmitted = true;
                if (_isAct2) refreshChatInputState();
              }
              if (ev.phase === 'video_submit') {
                if (ev.success === false) {
                  var failPrev = (ev.preview || '').trim();
                  var _ft = failPrev ? ('✗ 提交未成功：' + (failPrev.length > 140 ? failPrev.slice(0, 140) + '…' : failPrev))
                    : '✗ 任务提交失败，请查看下方回复';
                  _saveSessionTypingState(sid, null, _ft, 'replace_last');
                  if (_isAct2) updateLastChatTypingStep(_ft);
                } else {
                  _saveSessionTypingState(sid, null, '✓ 任务已提交成功，正在查询生成结果…', 'replace_last');
                  if (_isAct2) updateLastChatTypingStep('✓ 任务已提交成功，正在查询生成结果…');
                }
              } else if (ev.phase === 'image_submit') {
                if (ev.success === false) {
                  var failPrevImg = (ev.preview || '').trim();
                  var _fti = failPrevImg ? ('✗ 提交未成功：' + (failPrevImg.length > 140 ? failPrevImg.slice(0, 140) + '…' : failPrevImg))
                    : '✗ 任务提交失败，请查看下方回复';
                  _saveSessionTypingState(sid, null, _fti, 'replace_last');
                  if (_isAct2) updateLastChatTypingStep(_fti);
                } else {
                  if (ev.saved_assets && ev.saved_assets.length) {
                    streamGeneratedAssets = ev.saved_assets.slice();
                    saveGeneratedAssetsToLocal(streamGeneratedAssets, savedAssetUrls);
                    _saveSessionTypingState(sid, null, '✓ 素材已生成', 'replace_last');
                    if (_isAct2) {
                      updateLastChatTypingStep('✓ 素材已生成');
                      if (!assetsPreviewAppended) {
                        assetsPreviewAppended = true;
                        appendChatGeneratedAssetsToTyping(streamGeneratedAssets);
                      }
                    }
                  } else {
                    _saveSessionTypingState(sid, null, '✓ 任务已提交成功，正在查询生成结果…', 'replace_last');
                    if (_isAct2) updateLastChatTypingStep('✓ 任务已提交成功，正在查询生成结果…');
                  }
                }
              } else if (ev.phase === 'task_polling') {
                var stillInProgress = ev.in_progress === true;
                if (!stillInProgress && ev.understand_text) {
                  taskPollingCompleted = true;
                  _saveSessionTypingState(sid, null, '✓ 理解完成', 'replace_last');
                  if (_isAct2) updateLastChatTypingStep('✓ 理解完成');
                  _saveSessionTypingState(sid, null, null, null);
                } else if (!stillInProgress) {
                  taskPollingCompleted = true;
                  if (ev.saved_assets && ev.saved_assets.length) streamGeneratedAssets = ev.saved_assets;
                  if (streamGeneratedAssets.length && !taskPollLocalSaveDone) {
                    taskPollLocalSaveDone = true;
                    saveGeneratedAssetsToLocal(streamGeneratedAssets, savedAssetUrls);
                  }
                  if (!videoGeneratedShown) {
                    _saveSessionTypingState(sid, null, '✓ 素材已生成', 'replace_last');
                    if (_isAct2) updateLastChatTypingStep('✓ 素材已生成');
                    videoGeneratedShown = true;
                  }
                  if (streamGeneratedAssets.length && _isAct2 && !assetsPreviewAppended) {
                    assetsPreviewAppended = true;
                    appendChatGeneratedAssetsToTyping(streamGeneratedAssets);
                  }
                  _saveSessionTypingState(sid, null, null, null);
                  /* 不强制改主行：避免「撰写」与后续 save/list 步骤语义冲突；done 时会清指示器 */
                } else if (!taskPollingCompleted) {
                  _saveSessionTypingState(sid, null, '正在查询生成结果…', 'replace_last');
                  if (_isAct2) updateLastChatTypingStep('正在查询生成结果…');
                }
              } else if (ev.phase === 'understand_submit') {
                _saveSessionTypingState(sid, null, '✓ 已提交，正在获取理解结果…', 'replace_last');
                if (_isAct2) updateLastChatTypingStep('✓ 已提交，正在获取理解结果…');
              } else if (ev.name === 'list_capabilities') {
                _saveSessionTypingState(sid, null, '✓ 能力列表已获取', 'replace_last');
                if (_isAct2) updateLastChatTypingStep('✓ 能力列表已获取');
              } else {
                if (ev.success !== false && ev.saved_assets && ev.saved_assets.length) {
                  streamGeneratedAssets = ev.saved_assets.slice();
                  saveGeneratedAssetsToLocal(streamGeneratedAssets, savedAssetUrls);
                  _saveSessionTypingState(sid, null, '✓ 素材已生成', 'replace_last');
                  if (_isAct2) {
                    if (!assetsPreviewAppended) {
                      assetsPreviewAppended = true;
                      appendChatGeneratedAssetsToTyping(streamGeneratedAssets);
                    }
                    updateLastChatTypingStep('✓ 素材已生成');
                  }
                } else {
                  var _endT = '✓ ' + _toolLabel(ev.name, ev.capability_id) + ' 完成';
                  _saveSessionTypingState(sid, null, _endT, 'replace_last');
                  if (_isAct2) updateLastChatTypingStep(_endT);
                }
              }
            } else if (ev.type === 'task_poll') {
              if (ev.task_id) persistSessionPollResumeTaskId(sid, ev.task_id);
              if (!ev.message || taskPollingCompleted) continue;
              var _pollLine = _formatTaskPollTypingLine(ev);
              _saveSessionTypingState(sid, _pollLine, null, null);
              if (String(currentSessionId) === sid) setChatTypingMainText(_pollLine);
            } else if (ev.type === 'status' && ev.message) {
              if (ev.message === '正在请模型撰写回复…' || ev.message === '正在生成回复…') continue;
              _saveSessionTypingState(sid, null, ev.message, 'append');
              if (String(currentSessionId) === sid) appendChatTypingStep(ev.message);
            } else if (ev.type === 'done') {
              _clearSessionTypingState(sid);
              return Promise.resolve(ev);
            }
          } catch (e) {}
        }
        return reader.read().then(processChunk);
      }
      return reader.read().then(processChunk);
    })
    .then(function(doneEv) {
      _clearSessionTypingState(sid);
      clearSessionPollResume(sid);
      var targetSession = getSessionById(sid);
      if (!targetSession) return;
      if (String(currentSessionId) === sid) removeChatTypingIndicator();
      var reply = _normalizeAssistantStreamReply(
        (doneEv && doneEv.reply) ? doneEv.reply : (doneEv ? '' : '请求异常结束')
      );
      targetSession.messages = Array.isArray(targetSession.messages) ? targetSession.messages : [];
      targetSession.messages.push({
        role: 'assistant',
        content: reply,
        saved_assets: streamGeneratedAssets && streamGeneratedAssets.length ? streamGeneratedAssets : undefined
      });
      targetSession.updatedAt = Date.now();
      if (String(currentSessionId) === sid) {
        appendAssistantMessageReveal(reply, streamGeneratedAssets);
        chatHistory = targetSession.messages.slice();
      }
      saveChatSessionsToStorage();
    })
    .catch(function(e) {
      if (e && e.name === 'AbortError') {
        if (resumeAbortReason === 'deadline') {
          _clearSessionTypingState(sid);
          clearSessionPollResume(sid);
          var targetDead = getSessionById(sid);
          var tdetail =
            '恢复查询已超时自动停止（任务可能仍在后台）。请重新发消息续查，或到素材库查看是否已生成。';
          _pushAssistantErrorIfNotDuplicate(targetDead, tdetail);
          if (String(currentSessionId) === sid) {
            removeChatTypingIndicator();
            appendChatMessage('assistant', '错误：' + tdetail);
          }
          saveChatSessionsToStorage();
          return;
        }
        streamAbortedByUser = true;
        setSessionPending(sid, false);
        if (String(currentSessionId) === sid) {
          setChatTypingMainText('已取消');
          appendChatTypingStep('已终止恢复查询；刷新页面可再次续查');
          setTimeout(removeChatTypingIndicator, 1500);
        }
        saveChatSessionsToStorage();
        return;
      }
      var targetSession = getSessionById(sid);
      var raw0 = _rawChatStreamError(e);
      var msg = _normalizeChatStreamErrorMessage(raw0 || '请稍后重试');
      var transient = _isTransientResumeStreamFailure(e, raw0, msg);
      var httpErr = e && e.status != null;
      if (httpErr && !transient) clearSessionPollResume(sid);
      var addedErr = false;
      if (!transient) addedErr = _pushAssistantErrorIfNotDuplicate(targetSession, msg);
      if (String(currentSessionId) === sid) {
        removeChatTypingIndicator();
        if (!transient && addedErr) appendChatMessage('assistant', '错误：' + msg);
        if (targetSession) chatHistory = targetSession.messages.slice();
      }
      saveChatSessionsToStorage();
    })
    .finally(function() {
      if (resumeDeadlineTimer != null) {
        try {
          clearTimeout(resumeDeadlineTimer);
        } catch (eClr) {}
        resumeDeadlineTimer = null;
      }
      window._chatResumePollInFlight = false;
      if (chatStreamAbortController === abortController) chatStreamAbortController = null;
      chatStreamSutuiSubmitted = false;
      refreshChatInputState();
      setSessionPending(sid, false);
      if (!streamAbortedByUser && resumeAbortReason !== 'deadline' && String(currentSessionId) === sid)
        removeChatTypingIndicator();
    });
}

function sendChatMessage() {
  var input = document.getElementById('chatInput');
  var btn = document.getElementById('chatSendBtn');
  if (!input || !btn) return;
  var message = (input.value || '').trim();
  if (!message && chatAttachmentIds.length === 0) return;
  if (!currentSessionId) {
    if (chatSessions.length) switchChatSession(chatSessions[0].id);
    else createNewSession();
  }
  var sid = String(currentSessionId);
  var session = getSessionById(sid);
  if (!session) return;
  if (isSessionPending(sid)) return;

  var chatBase = (typeof LOCAL_API_BASE !== 'undefined' && LOCAL_API_BASE) ? String(LOCAL_API_BASE).replace(/\/$/, '') : '';
  if (!chatBase) {
    alert('智能对话须连接本机 lobster_online 后端（含 OpenClaw/MCP）。请运行 backend/run.py 后用该后端地址打开页面，或在页面中设置 window.__LOCAL_API_BASE。');
    return;
  }

  input.value = '';
  var attachIds = mergeUserMessageAssetIds(chatAttachmentIds.slice(), message);
  clearChatAttachments();
  session.messages = Array.isArray(session.messages) ? session.messages : [];
  session.messages.push({
    role: 'user',
    content: message,
    attachment_asset_ids: attachIds.length ? attachIds.slice() : undefined
  });
  session.updatedAt = Date.now();
  if (String(currentSessionId) === sid) {
    appendUserMessageDisplay(message, attachIds);
    chatHistory = session.messages.slice();
  }
  saveCurrentSessionToStore();
  renderChatSessionList();
  abortActiveChatStream();
  setSessionPending(sid, true);
  showChatTypingIndicator();
  var historyForRequest = session.messages.slice(0, -1);
  var model = '';
  if (_isOnlineFixedSutuiChat()) {
    model = _onlineFixedChatModelPayload();
  } else {
    var modelSel = document.getElementById('modelSelect');
    model = modelSel ? (modelSel.value || '') : '';
    var subSel = document.getElementById('sutuiModelSelect');
    if (model === 'sutui_aggregate') {
      if (!subSel || !subSel.value) {
        alert('请先在「速推 LLM」子下拉中选择对话模型（仅 LLM/text 类，列表由服务器提供）。若列表为空，请稍后再试或联系管理员检查速推 Token。');
        setSessionPending(sid, false);
        if (String(currentSessionId) === sid) removeChatTypingIndicator();
        return;
      }
      model = 'sutui/' + subSel.value;
    }
  }
  var body = {
    message: message,
    history: historyForRequest,
    session_id: sid,
    context_id: null,
    model: model || undefined
  };
  var directChk = document.getElementById('chatDirectLlmCheck');
  if (directChk && directChk.checked) body.direct_llm = true;
  if (attachIds.length) body.attachment_asset_ids = attachIds;
  var bodyStr = JSON.stringify(body);
  var headers = authHeaders();
  headers['Content-Type'] = 'application/json';
  var taskPollingCompleted = false;
  var videoGeneratedShown = false;
  var streamGeneratedAssets = [];
  var savedAssetUrls = {};
  var taskPollLocalSaveDone = false;
  var assetsPreviewAppended = false;
  var streamAbortedByUser = false;
  chatStreamSutuiSubmitted = false;
  var abortController = new AbortController();
  chatStreamAbortController = abortController;
  refreshChatInputState();
  var streamKindResume = false;
  fetch(chatBase + '/chat/stream', { method: 'POST', headers: headers, body: bodyStr, signal: abortController.signal })
    .then(function(r) {
      if (!r.ok) {
        return r.json().then(function(d) { throw { status: r.status, detail: (d && d.detail) || r.statusText }; });
      }
      if (!r.body) throw new Error('No body');
      var decoder = new TextDecoder();
      var buf = '';
      var reader = r.body.getReader();
      function processChunk(result) {
        if (result.done) return Promise.resolve(null);
        buf += decoder.decode(result.value, { stream: true });
        var parts = buf.split('\n\n');
        buf = parts.pop() || '';
        for (var i = 0; i < parts.length; i++) {
          var block = parts[i];
          var dataLine = block.split('\n').filter(function(l) { return l.indexOf('data:') === 0; })[0];
          if (!dataLine) continue;
          try {
            var ev = JSON.parse(dataLine.slice(5).trim());
            if (ev.type === 'capability_cost_confirm' && String(currentSessionId) === sid) {
              var capId = ev.capability_id || '';
              var cr = ev.estimated_credits;
              var note = (ev.estimate_note || '').trim();
              var secLeft = ev.timeout_seconds != null ? ev.timeout_seconds : 300;
              var cn = (cr != null && cr !== '') ? Number(cr) : NaN;
              var creditLine = !isNaN(cn) && cn > 0 ? String(cn) : (!isNaN(cn) && cn === 0 ? '0' : '未知');
              appendChatTypingStep('等待确认…');
              return openChatCapabilityCostConfirm({
                capability_id: capId,
                invoke_model: ev.invoke_model || '',
                credit_display: creditLine,
                note: note,
                timeout_seconds: secLeft
              }).then(function(userAccept) {
                var tokenBody = JSON.stringify({
                  confirm_token: String(ev.confirm_token || '').trim(),
                  accept: !!userAccept
                });
                var confirmHdr = Object.assign({}, headers, { 'Content-Type': 'application/json' });
                return fetch(chatBase + '/capabilities/confirm-invoke', {
                  method: 'POST',
                  headers: confirmHdr,
                  body: tokenBody
                }).catch(function() {
                  return fetch(chatBase + '/capabilities/confirm-invoke', {
                    method: 'POST',
                    headers: confirmHdr,
                    body: JSON.stringify({
                      confirm_token: String(ev.confirm_token || '').trim(),
                      accept: false
                    })
                  });
                }).then(function() {
                  if (String(currentSessionId) === sid) {
                    updateLastChatTypingStep(userAccept ? '已确认，继续执行…' : '已取消本次能力调用');
                  }
                  return reader.read().then(processChunk);
                });
              });
            } else if (ev.type === 'tool_start') {
              var _isAct = String(currentSessionId) === sid;
              if (ev.task_id) persistSessionPollResumeTaskId(sid, ev.task_id);
              if (ev.name === 'list_capabilities') {
                _saveSessionTypingState(sid, null, '正在查询可用能力…', 'append');
                if (_isAct) appendChatTypingStep('正在查询可用能力…');
              } else if (ev.phase === 'video_submit') {
                _saveSessionTypingState(sid, null, '正在提交视频生成任务…', 'append');
                if (_isAct) appendChatTypingStep('正在提交视频生成任务…');
              } else if (ev.phase === 'image_submit') {
                _saveSessionTypingState(sid, null, '正在提交图片生成任务…', 'append');
                if (_isAct) appendChatTypingStep('正在提交图片生成任务…');
              } else if (ev.phase === 'task_polling') {
                chatStreamSutuiSubmitted = true;
                if (_isAct) refreshChatInputState();
                var _pollMain = streamKindResume
                  ? '正在恢复并查询生成结果（约每 15 秒更新；超过 ' +
                      Math.round(_RESUME_CHAT_STREAM_MAX_MS / 60000) +
                      ' 分钟仍未结束将自动停止恢复，任务可能在后台继续）…'
                  : '正在查询生成结果（约每 15 秒自动更新）…';
                _saveSessionTypingState(sid, _pollMain, null, null);
                if (_isAct) setChatTypingMainText(_pollMain);
              } else {
                var _sStep = '正在 ' + _toolLabel(ev.name, ev.capability_id) + '…';
                _saveSessionTypingState(sid, null, _sStep, 'append');
                if (_isAct) appendChatTypingStep(_sStep);
              }
            } else if (ev.type === 'tool_end') {
              var _isAct2 = String(currentSessionId) === sid;
              if (ev.task_id) persistSessionPollResumeTaskId(sid, ev.task_id);
              if ((ev.phase === 'image_submit' || ev.phase === 'video_submit') && ev.success !== false) {
                chatStreamSutuiSubmitted = true;
                if (_isAct2) refreshChatInputState();
              }
              if (ev.phase === 'video_submit') {
                if (ev.success === false) {
                  var failPrev = (ev.preview || '').trim();
                  var _ft = failPrev ? ('✗ 提交未成功：' + (failPrev.length > 140 ? failPrev.slice(0, 140) + '…' : failPrev))
                    : '✗ 任务提交失败，请查看下方回复';
                  _saveSessionTypingState(sid, null, _ft, 'replace_last');
                  if (_isAct2) updateLastChatTypingStep(_ft);
                } else {
                  _saveSessionTypingState(sid, null, '✓ 任务已提交成功，正在查询生成结果…', 'replace_last');
                  if (_isAct2) updateLastChatTypingStep('✓ 任务已提交成功，正在查询生成结果…');
                }
              } else if (ev.phase === 'image_submit') {
                if (ev.success === false) {
                  var failPrevImg = (ev.preview || '').trim();
                  var _fti = failPrevImg ? ('✗ 提交未成功：' + (failPrevImg.length > 140 ? failPrevImg.slice(0, 140) + '…' : failPrevImg))
                    : '✗ 任务提交失败，请查看下方回复';
                  _saveSessionTypingState(sid, null, _fti, 'replace_last');
                  if (_isAct2) updateLastChatTypingStep(_fti);
                } else {
                  if (ev.saved_assets && ev.saved_assets.length) {
                    streamGeneratedAssets = ev.saved_assets.slice();
                    saveGeneratedAssetsToLocal(streamGeneratedAssets, savedAssetUrls);
                    _saveSessionTypingState(sid, null, '✓ 素材已生成', 'replace_last');
                    if (_isAct2) {
                      updateLastChatTypingStep('✓ 素材已生成');
                      if (!assetsPreviewAppended) {
                        assetsPreviewAppended = true;
                        appendChatGeneratedAssetsToTyping(streamGeneratedAssets);
                      }
                    }
                  } else {
                    _saveSessionTypingState(sid, null, '✓ 任务已提交成功，正在查询生成结果…', 'replace_last');
                    if (_isAct2) updateLastChatTypingStep('✓ 任务已提交成功，正在查询生成结果…');
                  }
                }
              } else if (ev.phase === 'task_polling') {
                var stillInProgress = ev.in_progress === true;
                if (!stillInProgress && ev.understand_text) {
                  taskPollingCompleted = true;
                  _saveSessionTypingState(sid, null, '✓ 理解完成', 'replace_last');
                  if (_isAct2) updateLastChatTypingStep('✓ 理解完成');
                  _saveSessionTypingState(sid, null, null, null);
                } else if (!stillInProgress) {
                  taskPollingCompleted = true;
                  if (ev.saved_assets && ev.saved_assets.length) streamGeneratedAssets = ev.saved_assets;
                  if (streamGeneratedAssets.length && !taskPollLocalSaveDone) {
                    taskPollLocalSaveDone = true;
                    saveGeneratedAssetsToLocal(streamGeneratedAssets, savedAssetUrls);
                  }
                  if (!videoGeneratedShown) {
                    _saveSessionTypingState(sid, null, '✓ 素材已生成', 'replace_last');
                    if (_isAct2) updateLastChatTypingStep('✓ 素材已生成');
                    videoGeneratedShown = true;
                  }
                  if (streamGeneratedAssets.length && _isAct2 && !assetsPreviewAppended) {
                    assetsPreviewAppended = true;
                    appendChatGeneratedAssetsToTyping(streamGeneratedAssets);
                  }
                  _saveSessionTypingState(sid, null, null, null);
                  /* 不强制改主行：避免「撰写」与后续 save/list 步骤语义冲突；done 时会清指示器 */
                } else if (!taskPollingCompleted) {
                  _saveSessionTypingState(sid, null, '正在查询生成结果…', 'replace_last');
                  if (_isAct2) updateLastChatTypingStep('正在查询生成结果…');
                }
              } else if (ev.phase === 'understand_submit') {
                _saveSessionTypingState(sid, null, '✓ 已提交，正在获取理解结果…', 'replace_last');
                if (_isAct2) updateLastChatTypingStep('✓ 已提交，正在获取理解结果…');
              } else if (ev.name === 'list_capabilities') {
                _saveSessionTypingState(sid, null, '✓ 能力列表已获取', 'replace_last');
                if (_isAct2) updateLastChatTypingStep('✓ 能力列表已获取');
              } else {
                if (ev.success !== false && ev.saved_assets && ev.saved_assets.length) {
                  streamGeneratedAssets = ev.saved_assets.slice();
                  saveGeneratedAssetsToLocal(streamGeneratedAssets, savedAssetUrls);
                  _saveSessionTypingState(sid, null, '✓ 素材已生成', 'replace_last');
                  if (_isAct2) {
                    if (!assetsPreviewAppended) {
                      assetsPreviewAppended = true;
                      appendChatGeneratedAssetsToTyping(streamGeneratedAssets);
                    }
                    updateLastChatTypingStep('✓ 素材已生成');
                  }
                } else {
                  var _endT = '✓ ' + _toolLabel(ev.name, ev.capability_id) + ' 完成';
                  _saveSessionTypingState(sid, null, _endT, 'replace_last');
                  if (_isAct2) updateLastChatTypingStep(_endT);
                }
              }
            } else if (ev.type === 'task_poll') {
              if (ev.task_id) persistSessionPollResumeTaskId(sid, ev.task_id);
              if (!ev.message || taskPollingCompleted) continue;
              var _pollLine = _formatTaskPollTypingLine(ev);
              _saveSessionTypingState(sid, _pollLine, null, null);
              if (String(currentSessionId) === sid) setChatTypingMainText(_pollLine);
            } else if (ev.type === 'status' && ev.message) {
              if (ev.message === '正在请模型撰写回复…' || ev.message === '正在生成回复…') continue;
              _saveSessionTypingState(sid, null, ev.message, 'append');
              if (String(currentSessionId) === sid) appendChatTypingStep(ev.message);
            } else if (ev.type === 'done') {
              _clearSessionTypingState(sid);
              return Promise.resolve(ev);
            }
          } catch (e) {}
        }
        return reader.read().then(processChunk);
      }
      return reader.read().then(processChunk);
    })
    .then(function(doneEv) {
      _clearSessionTypingState(sid);
      clearSessionPollResume(sid);
      var targetSession = getSessionById(sid);
      if (!targetSession) return;
      if (String(currentSessionId) === sid) removeChatTypingIndicator();
      var reply = _normalizeAssistantStreamReply(
        (doneEv && doneEv.reply) ? doneEv.reply : (doneEv ? '' : '请求异常结束')
      );
      targetSession.messages = Array.isArray(targetSession.messages) ? targetSession.messages : [];
      targetSession.messages.push({
        role: 'assistant',
        content: reply,
        saved_assets: streamGeneratedAssets && streamGeneratedAssets.length ? streamGeneratedAssets : undefined
      });
      targetSession.updatedAt = Date.now();
      if (String(currentSessionId) === sid) {
        appendAssistantMessageReveal(reply, streamGeneratedAssets);
        chatHistory = targetSession.messages.slice();
      }
      saveChatSessionsToStorage();
    })
    .catch(function(e) {
      if (e && e.name === 'AbortError') {
        streamAbortedByUser = true;
        setSessionPending(sid, false);
        if (!chatStreamSutuiSubmitted) clearSessionPollResume(sid);
        if (String(currentSessionId) === sid) {
          setChatTypingMainText('已取消');
          appendChatTypingStep('已终止当前任务，可重新发送消息继续');
          setTimeout(removeChatTypingIndicator, 1500);
        }
        saveChatSessionsToStorage();
        return;
      }
      var targetSession = getSessionById(sid);
      var raw0 = _rawChatStreamError(e);
      var msg = _normalizeChatStreamErrorMessage(raw0 || '请稍后重试');
      var addedErr = _pushAssistantErrorIfNotDuplicate(targetSession, msg);
      if (String(currentSessionId) === sid) {
        removeChatTypingIndicator();
        if (addedErr) appendChatMessage('assistant', '错误：' + msg);
        if (targetSession) chatHistory = targetSession.messages.slice();
      }
      saveChatSessionsToStorage();
    })
    .finally(function() {
      if (chatStreamAbortController === abortController) chatStreamAbortController = null;
      chatStreamSutuiSubmitted = false;
      refreshChatInputState();
      setSessionPending(sid, false);
      if (!streamAbortedByUser && String(currentSessionId) === sid) removeChatTypingIndicator();
    });
}
var chatSendBtn = document.getElementById('chatSendBtn');
var chatInput = document.getElementById('chatInput');
var chatAttachBtn = document.getElementById('chatAttachBtn');
var chatFileInput = document.getElementById('chatFileInput');
if (chatSendBtn) chatSendBtn.addEventListener('click', sendChatMessage);
var chatCancelBtn = document.getElementById('chatCancelBtn');
if (chatCancelBtn) {
  chatCancelBtn.addEventListener('click', function() {
    if (!chatStreamAbortController) return;
    if (chatStreamSutuiSubmitted) {
      alert('任务已在速推生成中，无法取消。请等待生成完成。');
      return;
    }
    try { chatStreamAbortController.abort(); } catch (err) {}
  });
}
if (chatAttachBtn && chatFileInput) {
  chatAttachBtn.addEventListener('click', function() { chatFileInput.click(); });
  chatFileInput.addEventListener('change', function() {
    var files = chatFileInput.files;
    if (!files || !files.length) return;
    for (var i = 0; i < files.length; i++) {
      (function(file) {
        var fd = new FormData();
        fd.append('file', file);
        fetch((typeof LOCAL_API_BASE !== 'undefined' ? LOCAL_API_BASE : '') + '/api/assets/upload', { method: 'POST', headers: { 'Authorization': 'Bearer ' + (typeof token !== 'undefined' ? token : '') }, body: fd })
          .then(function(r) { return r.json(); })
          .then(function(d) {
            if (d && d.asset_id) addChatAttachment(d.asset_id, d.media_type || 'image');
          })
          .catch(function() {});
      })(files[i]);
    }
    chatFileInput.value = '';
  });
}
if (chatInput) {
  var chatInputComposing = false;
  chatInput.addEventListener('compositionstart', function() { chatInputComposing = true; });
  chatInput.addEventListener('compositionend', function() { chatInputComposing = false; });
  chatInput.addEventListener('keydown', function(e) {
    if (chatInputComposing || e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChatMessage(); }
  });
}
var chatNewSessionBtn = document.getElementById('chatNewSessionBtn');
if (chatNewSessionBtn) chatNewSessionBtn.addEventListener('click', createNewSession);
var chatSessionSearch = document.getElementById('chatSessionSearch');
if (chatSessionSearch) chatSessionSearch.addEventListener('input', renderChatSessionList);
