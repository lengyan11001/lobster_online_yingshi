/**
 * 通用「?」说明：点击带 .help-q[data-help-key] 的元素展示文案，避免在页面上堆砌长说明。
 * 依赖 #helpHintModal（modal-mask）、#helpHintTitle、#helpHintBody、#helpHintClose
 */
(function() {
  var HELP_HINTS = {
    skill_store: {
      title: '技能商店',
      html: '<p>浏览并启用 MCP 能力；添加 MCP 后本机会发现并注册工具。</p><p>热门/分类用于筛选；刷新可重新拉取列表。</p>'
    },
    billing: {
      title: '消费记录',
      html: '<p>展示能力调用与速推扣费流水（以本系统记录为准）。</p><p>「充值」页签可查看充值订单；「消费」为扣费明细。时间一般为北京时间。</p>'
    },
    billing_recharge: {
      title: '速推充值',
      html: '<p>选择档位后跳转支付；完成后余额会更新。若未显示充值区，可能当前版本或环境未开放在线充值。</p>'
    },
    publish_overview: {
      title: '发布管理',
      html: '<p>点击<strong>账号卡片</strong>进入详情，查看抖音/小红书/今日头条作品数据。</p><p><strong>定时任务</strong>与<strong>执行记录</strong>在卡片或详情内；本页「发布记录」tab 是<strong>单次发布</strong>（对话里触发的 publish），与按间隔的定时任务<strong>不是同一处</strong>。</p>'
    },
    publish_add_account: {
      title: '添加发布账号',
      html: '<p>选择平台并填写昵称；添加后按提示在浏览器中登录。代理、UA 为可选项。</p><p>登录成功后即可在对话或定时任务中指定该账号发布。</p>'
    },
    publish_tasks_tab: {
      title: '发布记录（单次）',
      html: '<p>此处为<strong>单次发布</strong>任务记录（对话里说「发到抖音/头条」等产生的任务）。</p><p>账号上的<strong>间隔定时</strong>（同步作品 + 可选智能编排）请到<strong>发布账号</strong> → 该账号 → <strong>执行记录</strong>或详情里<strong>任务列表</strong>查看。</p>'
    },
    production: {
      title: '生产记录',
      html: '<p>展示速推能力调用与模型对话相关记录，便于查看生成进度与结果。</p><p>点击<strong>刷新</strong>拉取最新一页。</p>'
    },
    sys_config: {
      title: '系统配置',
      html: '<p><strong>模型配置</strong>：默认对话模型与各厂商 API Key；在线版能力可能由服务端统一提供，部分项只读或无需填写。</p><p><strong>自定义配置</strong>：导入键值/JSON 供本机后端使用。</p><p>下方「清除本机个人配置」仅影响当前账号在本机的缓存数据，不会删云端算力。</p>'
    },
    sys_save_oc: {
      title: '保存 / Gateway',
      html: '<p><strong>保存</strong>：各厂商 API Key 只写入本机 <code>openclaw/.env</code> 与 <code>openclaw.json</code>，<strong>不会</strong>上传到云端 lobster_server。</p><p><strong>重启 Gateway</strong>：重启本机对话网关进程，改 Key 后若未生效可试。</p><p><strong>刷新状态</strong>：探测本机网关是否就绪。</p>'
    },
    sys_custom_import: {
      title: '导入配置',
      html: '<p>将命名配置写入本机后端（支持 JSON、Python 常量或 KEY=VALUE）。</p><p>导入后可在下方列表查看与删除。</p>'
    },
    sys_clear_local: {
      title: '清除本机个人配置',
      html: '<p>清除<strong>本机</strong>：OpenClaw 目录下各厂商 API Key（<code>openclaw/.env</code>，与云端无关）；若本机库有用户行则再清速推 Token、首选模型、算力账号；并清<strong>浏览器</strong>对话与 <code>lobster_*_base</code> 调试项。</p><p>不删云端算力与素材，不退出登录；自定义配置块请用上方 Tab 单独删。</p><p>需已登录（仅用于确认操作者，不在远端存你的 Key）。</p>'
    },
    logs: {
      title: '系统日志',
      html: '<p>读取本机 <code>logs/app.log</code> 末尾若干行，用于排查对话、MCP、定时任务等问题。</p><p>可选择「末尾」行数；<strong>导出 txt</strong> 便于发给他人分析。若一直空白请先点<strong>刷新</strong>。</p>'
    },
    schedule_modal_full: {
      title: '定时任务（完整说明）',
      html: '<p>按<strong>固定间隔</strong>重复执行。勾选<strong>启用</strong>并<strong>保存</strong>后会<strong>立即同步一次</strong>作品列表（抖音 / 小红书 / 今日头条，默认无头不打扰桌面）。</p><p>每次<strong>到点</strong>若「描述/生产要求」<strong>有内容</strong>，后台会按提纲调用<strong>本机 POST /chat + MCP</strong>自动拆解并执行生成；需要<strong>发布</strong>时会唤起该账号<strong>有头浏览器</strong>（需已配置对话模型、速推 Token、MCP 已启动）。</p><p>保存首轮与每次到点可在账号卡片<strong>执行记录</strong>或详情<strong>任务列表</strong>查看（与顶部「发布记录」tab 的单次任务不同）。</p><p><strong>视频模式</strong>填写<strong>素材 ID</strong>可作图生视频参考图。</p>'
    },
    schedule_save: {
      title: '保存定时任务',
      html: '<p>将启用状态、间隔、类型、描述需求、发布模式等写入服务器。</p><p>启用后会触发首轮同步；审核模式下可先「智能生成提示词」再「生成发布内容」，最后「确认并发布」。</p>'
    },
    schedule_review_actions: {
      title: '审核区按钮',
      html: '<p>请先<strong>保存</strong>表单。</p><p><strong>智能生成提示词</strong>：只生成可编辑的提示词与参数。</p><p><strong>生成发布内容</strong>：先保存当前提示词，再调用本机对话与能力生成素材与拟发布文案（耗时较长）。</p><p>列表与预计发布时间见账号详情 · 定时任务页。</p>'
    },
    account_detail_works: {
      title: '作品数据',
      html: '<p>展示抖音/小红书/今日头条已发布内容的标题、封面、阅读/播放、互动等（来自平台同步快照；头条为 mp 后台数据聚合）。</p><p><strong>加载缓存</strong>：读上次同步结果；<strong>从平台同步</strong>：重新拉取（可选无头）。</p>'
    },
    account_detail_schedule_btns: {
      title: '完整配置 / 任务列表',
      html: '<p><strong>完整配置</strong>：打开弹窗设置间隔、内容类型、描述需求、发布模式（立即 / 审核）等。</p><p><strong>任务列表</strong>：查看每次保存首轮与到点执行日志（作品同步 + 智能编排进度）。</p>'
    },
    account_detail_review: {
      title: '审核后发布 · 生成区',
      html: '<p>以下为将发给 AI 的<strong>提示词与参数</strong>；可先「智能生成提示词」再手改。</p><p>点<strong>生成发布内容</strong>后才会调用能力生成素材与拟发布文案。预计发布时间见每条上方。</p><p><strong>确认并发布</strong>：在「完整配置」中已启用并填写要求后，按首条时间与间隔逐条编排发布。</p>'
    },
    account_review_timing: {
      title: '首条时间与间隔',
      html: '<p><strong>分钟</strong>为从<strong>保存时</strong>起算的首条发布时间；<strong>0</strong> 表示尽快开始。</p><p>第 2 条及以后按「完整配置」中的<strong>间隔分钟</strong>顺延。</p>'
    },
    account_review_snapshots: {
      title: '历史记录',
      html: '<p>每次「智能生成提示词」「生成发布内容」或单条重生成后会自动存档（最多保留约 60 条），可恢复为当前草稿继续编辑或再次生成。</p>'
    },
    account_review_confirm_publish: {
      title: '确认并发布',
      html: '<p>按首条时间与「完整配置」中的间隔逐条定时编排发布；进度见<strong>任务列表</strong>。</p>'
    },
    billing_pricing_mode: {
      title: '软件收费模式',
      html: '<p>下方蓝色区域为服务端下发的收费与套餐说明；具体档位以页面展示为准。</p>'
    },
    account_schedule_summary: {
      title: '定时任务摘要（详情页）',
      html: '<p>启用并保存后会<strong>立即同步一次</strong>作品列表（含今日头条）。每次保存首轮与到点执行请在<strong>任务列表</strong>或卡片<strong>执行记录</strong>查看（与顶部「发布记录」tab 不同）。</p><p>选视频且填写生产要求时，到点还会提交视频生成（本机 MCP + 速推）。</p>'
    },
    chat_send: {
      title: '发送',
      html: '<p>将当前输入（及附图）发往本机对话接口；模型与速推配置来自系统配置。</p><p>长任务可能持续数分钟，可用<strong>取消</strong>终止当前请求（已提交上游生成的任务可能无法撤回）。</p>'
    },
    chat_attach: {
      title: '图片 / 视频',
      html: '<p>选择本地图片或视频作为附件，会随消息一并提交（用于多模态或参考）。</p>'
    },
    chat_cancel: {
      title: '取消',
      html: '<p>终止当前进行中的对话请求；若已向速推等平台提交异步生成任务，可能无法取消。</p>'
    },
    chat_new_session: {
      title: '新会话',
      html: '<p>新建一条对话会话，与历史上下文隔离。左侧列表可切换、搜索历史会话。</p>'
    },
    add_mcp: {
      title: '添加 MCP',
      html: '<p>输入 MCP 服务名称与 URL，本机会连接并注册可用能力到技能商店。</p>'
    },
    sch_tasks_modal: {
      title: '定时任务 · 执行记录',
      html: '<p>展示该账号每次定时触发的执行记录：作品同步结果、智能编排是否成功等。</p><p>有<strong>进行中</strong>任务时列表会周期性自动刷新。</p>'
    }
  };

  function openHelpHint(key) {
    var h = HELP_HINTS[key];
    if (!h) return;
    var title = document.getElementById('helpHintTitle');
    var body = document.getElementById('helpHintBody');
    var mask = document.getElementById('helpHintModal');
    if (title) title.textContent = h.title || '说明';
    if (body) body.innerHTML = h.html || '';
    if (mask) mask.classList.add('visible');
  }

  function closeHelpHint() {
    var mask = document.getElementById('helpHintModal');
    if (mask) mask.classList.remove('visible');
  }

  document.addEventListener('click', function(e) {
    var q = e.target.closest('.help-q[data-help-key]');
    if (q) {
      e.preventDefault();
      e.stopPropagation();
      openHelpHint(q.getAttribute('data-help-key'));
      return;
    }
    var closeBtn = e.target.closest('#helpHintClose');
    if (closeBtn) {
      closeHelpHint();
      return;
    }
  });

  var hm = document.getElementById('helpHintModal');
  if (hm) {
    hm.addEventListener('click', function(e) {
      if (e.target === this) closeHelpHint();
    });
  }

  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      var mask = document.getElementById('helpHintModal');
      if (mask && mask.classList.contains('visible')) closeHelpHint();
    }
  });

  window.openHelpHintKey = openHelpHint;
  window.closeHelpHintModal = closeHelpHint;
})();
