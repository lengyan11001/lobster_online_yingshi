(function() {
  var ANALYSIS_MODEL = 'gpt-4.1-mini';
  var IMAGE_MODEL = 'nano-banana-2';
  var RESULT_LABELS = {
    main_images: '主图',
    sku_images: 'SKU 图',
    transparent_white_bg: '透明 / 白底',
    detail_images: '详情图',
    material_images: '素材图',
    showcase_images: '橱窗图'
  };
  var RESULT_FOLDERS = {
    main_images: '1 】主图',
    sku_images: '2 】SKU图',
    transparent_white_bg: '3 】透明白底',
    detail_images: '4 】详情图',
    material_images: '5 】素材图',
    showcase_images: '6 】橱窗图'
  };
  var RESULT_ORDER = Object.keys(RESULT_LABELS);
  var JOB_HISTORY_STORAGE_KEY = 'comfly_ecommerce_detail_recent_jobs_v2';
  var JOB_HISTORY_LIMIT = 18;
  var OUTPUT_TARGETS = [
    { id: 'ecomTargetMain', key: 'main_images', label: '主图' },
    { id: 'ecomTargetSku', key: 'sku_images', label: 'SKU 图' },
    { id: 'ecomTargetTransparent', key: 'transparent_image', label: '透明图' },
    { id: 'ecomTargetWhite', key: 'white_bg_image', label: '白底图' },
    { id: 'ecomTargetDetail', key: 'detail_pages', label: '详情图' },
    { id: 'ecomTargetMaterial', key: 'material_images', label: '素材图' },
    { id: 'ecomTargetShowcase', key: 'showcase_images', label: '橱窗图' }
  ];
  var STAGE_META = [
    { key: '01_upload_inputs', label: '上传素材' },
    { key: '02_analyze_product', label: '商品分析' },
    { key: '03_plan_pages', label: '页面规划' },
    { key: '04_generate_main_images', label: '主图生成' },
    { key: '05_generate_sku_images', label: 'SKU 图生成' },
    { key: '06_generate_isolated_assets', label: '透明/白底' },
    { key: '07_generate_detail_pages', label: '详情图排版' },
    { key: '08_generate_material_images', label: '素材图导出' },
    { key: '09_generate_showcase_images', label: '橱窗图导出' },
    { key: '10_export_suite_bundle', label: '导出整包' }
  ];

  var state = {
    initialized: false,
    pollTimer: null,
    mainAsset: null,
    productRefs: [],
    styleRefs: [],
    recentJobs: [],
    currentJobId: '',
    activeResultTab: 'main_images',
    activeWorkspaceTab: 'workspace',
    latestResponse: null,
    lastGalleryByTab: {},
    focusedResultIndexByTab: {},
    taskDrawerOpen: false
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function _localBase() {
    return (typeof LOCAL_API_BASE !== 'undefined' ? (LOCAL_API_BASE || '') : '').replace(/\/$/, '');
  }

  function _authHeaderOnly() {
    return { Authorization: 'Bearer ' + (typeof token !== 'undefined' ? (token || '') : '') };
  }

  function _setMsg(text, isErr) {
    var el = byId('ecomStudioMsg');
    if (!el) return;
    if (!text) {
      el.style.display = 'none';
      el.textContent = '';
      el.className = 'msg';
      return;
    }
    el.textContent = text;
    el.className = 'msg ' + (isErr ? 'err' : 'ok');
    el.style.display = 'block';
  }

  function _stopPolling() {
    if (state.pollTimer) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function _schedulePoll(delayMs) {
    _stopPolling();
    state.pollTimer = setTimeout(function() {
      _refreshJobStatus(false);
    }, delayMs || 4000);
  }

  function _resolveAssetPreview(asset) {
    if (!asset) return '';
    var src = (asset.preview_url || asset.open_url || asset.source_url || '').trim();
    return src;
  }

  function _pickResponseMessage(resp, fallback) {
    if (!resp || typeof resp !== 'object') return fallback || '';
    if (typeof resp.error === 'string' && resp.error.trim()) return resp.error.trim();
    if (resp.progress && Array.isArray(resp.progress.errors) && resp.progress.errors.length) {
      var latest = resp.progress.errors[resp.progress.errors.length - 1];
      if (typeof latest === 'string' && latest.trim()) return latest.trim();
    }
    return fallback || '';
  }

  function _statusLabel(status) {
    if (status === 'completed') return '已完成';
    if (status === 'failed') return '失败';
    if (status === 'running') return '生成中';
    return '待提交';
  }

  function _resultFolderLabel(key) {
    return RESULT_FOLDERS[key] || RESULT_LABELS[key] || key;
  }

  function _formatTimeLabel(value) {
    if (!value) return '';
    var date = new Date(value);
    if (isNaN(date.getTime())) return String(value);
    return date.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit'
    });
  }

  function _safeLocalStorageGet(key) {
    try {
      return window.localStorage ? window.localStorage.getItem(key) : null;
    } catch (err) {
      return null;
    }
  }

  function _safeLocalStorageSet(key, value) {
    try {
      if (window.localStorage) window.localStorage.setItem(key, value);
    } catch (err) {
      // ignore storage errors so the UI still works in memory
    }
  }

  function _readRecentJobs() {
    var raw = _safeLocalStorageGet(JOB_HISTORY_STORAGE_KEY);
    if (!raw) return [];
    try {
      var parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed
        .filter(function(item) { return item && item.jobId; })
        .sort(function(a, b) {
          return String(b.updatedAt || b.createdAt || '').localeCompare(String(a.updatedAt || a.createdAt || ''));
        });
    } catch (err) {
      return [];
    }
  }

  function _writeRecentJobs() {
    _safeLocalStorageSet(JOB_HISTORY_STORAGE_KEY, JSON.stringify(state.recentJobs.slice(0, JOB_HISTORY_LIMIT)));
  }

  function _jobRequestedOutputsFromForm() {
    var targets = _outputTargetsFromForm();
    return OUTPUT_TARGETS
      .filter(function(item) { return targets[item.key]; })
      .map(function(item) { return item.label; });
  }

  function _getResultCounts(galleryByTab) {
    var rows = galleryByTab || {};
    return {
      main_images: (rows.main_images || []).length,
      sku_images: (rows.sku_images || []).length,
      transparent_white_bg: (rows.transparent_white_bg || []).length,
      detail_images: (rows.detail_images || []).length,
      material_images: (rows.material_images || []).length,
      showcase_images: (rows.showcase_images || []).length
    };
  }

  function _resultTotalCount(galleryByTab) {
    return RESULT_ORDER.reduce(function(sum, key) {
      return sum + ((galleryByTab && galleryByTab[key]) ? galleryByTab[key].length : 0);
    }, 0);
  }

  function _buildJobDraft(jobId) {
    return {
      jobId: jobId,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      status: 'running',
      productName: (byId('ecomProductNameInput') && byId('ecomProductNameInput').value || '').trim() || (state.mainAsset && state.mainAsset.filename) || ('任务 ' + String(jobId || '').slice(0, 8)),
      productDirectionHint: (byId('ecomProductDirectionInput') && byId('ecomProductDirectionInput').value || '').trim(),
      mainPreviewUrl: state.mainAsset ? _resolveAssetPreview(state.mainAsset) : '',
      requestedOutputs: _jobRequestedOutputsFromForm(),
      galleryByTab: {},
      latestResponse: { status: 'running', progress: { last_steps: [] } }
    };
  }

  function _findRecentJob(jobId) {
    var id = String(jobId || '');
    if (!id) return null;
    for (var i = 0; i < state.recentJobs.length; i += 1) {
      if (state.recentJobs[i] && state.recentJobs[i].jobId === id) return state.recentJobs[i];
    }
    return null;
  }

  function _upsertRecentJob(patch) {
    if (!patch || !patch.jobId) return null;
    var next = state.recentJobs.slice();
    var existingIndex = -1;
    for (var i = 0; i < next.length; i += 1) {
      if (next[i] && next[i].jobId === patch.jobId) {
        existingIndex = i;
        break;
      }
    }
    var merged = existingIndex >= 0 ? Object.assign({}, next[existingIndex], patch) : Object.assign({}, patch);
    merged.updatedAt = patch.updatedAt || new Date().toISOString();
    if (existingIndex >= 0) next.splice(existingIndex, 1);
    next.unshift(merged);
    state.recentJobs = next
      .sort(function(a, b) {
        return String(b.updatedAt || b.createdAt || '').localeCompare(String(a.updatedAt || a.createdAt || ''));
      })
      .slice(0, JOB_HISTORY_LIMIT);
    _writeRecentJobs();
    _renderRecentTasks();
    _renderTaskDrawer();
    return merged;
  }

  function _collectLastStep(progress) {
    var steps = progress && Array.isArray(progress.last_steps) ? progress.last_steps : [];
    if (!steps.length) return '';
    var last = steps[steps.length - 1] || {};
    return (last.name || '').trim();
  }

  function _syncCurrentJobHistory(resp, galleryByTab) {
    if (!state.currentJobId) return;
    var existing = _findRecentJob(state.currentJobId) || {};
    var result = resp && resp.result ? resp.result : {};
    var config = result && result.config ? result.config : {};
    var bundle = result && result.suite_bundle ? result.suite_bundle : {};
    _upsertRecentJob({
      jobId: state.currentJobId,
      createdAt: existing.createdAt || new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      status: resp && resp.status ? resp.status : existing.status || 'idle',
      productName: existing.productName || config.product_name_hint || ('任务 ' + String(state.currentJobId).slice(0, 8)),
      productDirectionHint: existing.productDirectionHint || config.product_direction_hint || '',
      mainPreviewUrl: existing.mainPreviewUrl || (state.mainAsset ? _resolveAssetPreview(state.mainAsset) : ''),
      requestedOutputs: existing.requestedOutputs && existing.requestedOutputs.length ? existing.requestedOutputs : _jobRequestedOutputsFromForm(),
      galleryByTab: galleryByTab || existing.galleryByTab || {},
      latestResponse: resp || existing.latestResponse || null,
      summary: _pickResponseMessage(resp, ''),
      lastStep: _collectLastStep(resp && resp.progress ? resp.progress : {}),
      suiteRootRelativePath: bundle.root_relative_path || existing.suiteRootRelativePath || ''
    });
  }

  function _setTaskDrawerOpen(forceOpen) {
    state.taskDrawerOpen = typeof forceOpen === 'boolean' ? forceOpen : !state.taskDrawerOpen;
    var panel = byId('ecomTaskDrawer');
    var btn = byId('ecomToggleTaskDrawerBtn');
    if (panel) panel.classList.toggle('open', !!state.taskDrawerOpen);
    if (btn) btn.textContent = state.taskDrawerOpen ? '收起任务' : '全部任务';
  }

  function _activateRecentJob(jobId, options) {
    var record = _findRecentJob(jobId);
    if (!record) return;
    state.currentJobId = record.jobId;
    if (!RESULT_ORDER.some(function(key) { return key === state.activeResultTab; })) {
      state.activeResultTab = 'main_images';
    }
    _renderRecentTasks();
    _renderTaskDrawer();
    _renderStatus(record.latestResponse || { status: record.status || 'idle', progress: { last_steps: [] } });
    if ((record.status === 'running' || !record.latestResponse) && !(options && options.skipRefresh)) {
      _refreshJobStatus(false);
    }
  }

  function _restoreRecentJobs() {
    state.recentJobs = _readRecentJobs();
    _renderRecentTasks();
    _renderTaskDrawer();
    if (!state.currentJobId && state.recentJobs.length) {
      _activateRecentJob(state.recentJobs[0].jobId, { skipRefresh: false });
    }
    _mergeDbJobs();
  }

  function _mergeDbJobs() {
    var base = _localBase();
    if (!base) return;
    fetch(base + '/api/comfly-ecommerce-detail/pipeline/jobs?limit=20', {
      headers: authHeaders()
    })
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(data) {
        if (!data || !Array.isArray(data.jobs)) return;
        var changed = false;
        data.jobs.forEach(function(dbJob) {
          if (!dbJob || !dbJob.job_id) return;
          if (_findRecentJob(dbJob.job_id)) return;
          _upsertRecentJob({
            jobId: dbJob.job_id,
            createdAt: dbJob.created_at || new Date().toISOString(),
            updatedAt: dbJob.created_at || new Date().toISOString(),
            status: dbJob.status || 'completed',
            productName: dbJob.product_name || ('\u4efb\u52a1 ' + String(dbJob.job_id).slice(0, 8)),
            galleryByTab: {},
            latestResponse: null,
            fromDb: true
          });
          changed = true;
        });
        if (changed) {
          _renderRecentTasks();
          _renderTaskDrawer();
          if (!state.currentJobId && state.recentJobs.length) {
            _activateRecentJob(state.recentJobs[0].jobId, { skipRefresh: false });
          }
        }
      })
      .catch(function() {});
  }

  function _collectProgressFacts(resp) {
    var progress = resp && resp.progress ? resp.progress : {};
    var facts = [];
    if (progress && progress.manifest_status) facts.push('manifest: ' + progress.manifest_status);
    if (progress && progress.step_count != null) facts.push('steps: ' + progress.step_count);
    if (progress && Array.isArray(progress.page_indexes) && progress.page_indexes.length) {
      facts.push('pages: ' + progress.page_indexes.join(', '));
    }
    if (progress && Array.isArray(progress.errors) && progress.errors.length) {
      progress.errors.slice(-3).forEach(function(item) {
        if (typeof item === 'string' && item.trim()) facts.push('error: ' + item.trim());
      });
    }
    return facts;
  }

  function _fileToAssetCard(fileRow) {
    var preview = _resolveAssetPreview(fileRow);
    var filename = (fileRow.filename || fileRow.name || '').trim() || '未命名素材';
    return (
      '<div class="ecom-upload-item">' +
        '<button type="button" class="ecom-upload-remove" data-remove-kind="' + escapeAttr(fileRow.kind || '') + '" data-remove-id="' + escapeAttr(fileRow.uid || '') + '">×</button>' +
        '<div class="ecom-upload-thumb">' +
          (preview ? '<img src="' + escapeAttr(preview) + '" alt="">' : '<div class="ecom-empty" style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;padding:0.4rem;">无预览</div>') +
        '</div>' +
        '<div class="ecom-upload-name">' + escapeHtml(filename) + '</div>' +
      '</div>'
    );
  }

  function _renderUploadList(containerId, items) {
    var el = byId(containerId);
    if (!el) return;
    if (!items || !items.length) {
      el.innerHTML = '<div class="ecom-empty" style="width:100%;margin:0;">尚未上传</div>';
      return;
    }
    el.innerHTML = items.map(_fileToAssetCard).join('');
    el.querySelectorAll('.ecom-upload-remove').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var kind = btn.getAttribute('data-remove-kind') || '';
        var uid = btn.getAttribute('data-remove-id') || '';
        if (kind === 'main') {
          state.mainAsset = null;
          byId('ecomMainAssetIdInput').value = '';
          _renderMainAsset();
          return;
        }
        if (kind === 'product_ref') {
          state.productRefs = state.productRefs.filter(function(item) { return item.uid !== uid; });
          _renderReferenceAssets();
          return;
        }
        if (kind === 'style_ref') {
          state.styleRefs = state.styleRefs.filter(function(item) { return item.uid !== uid; });
          _renderReferenceAssets();
        }
      });
    });
  }

  function _renderMainAsset() {
    _renderUploadList('ecomMainUploadList', state.mainAsset ? [state.mainAsset] : []);
    var previewEl = byId('ecomPrimaryPreview');
    if (!previewEl) return;
    var preview = state.mainAsset ? _resolveAssetPreview(state.mainAsset) : '';
    if (preview) {
      previewEl.innerHTML = '<img src="' + escapeAttr(preview) + '" alt="">';
    } else {
      previewEl.innerHTML = '<div class="ecom-empty" style="width:100%;margin:0;">上传主图后，这里会展示当前商品图。</div>';
    }
  }

  function _renderReferenceAssets() {
    _renderUploadList('ecomProductRefsList', state.productRefs);
    _renderUploadList('ecomStyleRefsList', state.styleRefs);
  }

  function _makeUid(prefix) {
    return prefix + '_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 8);
  }

  function _uploadFile(file, kind, done) {
    var base = _localBase();
    if (!base) {
      _setMsg('当前未检测到本机 LOCAL_API_BASE，无法上传图片。', true);
      if (done) done(new Error('missing_local_api'));
      return;
    }
    var fd = new FormData();
    fd.append('file', file);
    fetch(base + '/api/assets/upload', {
      method: 'POST',
      headers: _authHeaderOnly(),
      body: fd
    })
      .then(function(r) {
        return r.json().then(function(d) { return { ok: r.ok, data: d || {} }; });
      })
      .then(function(res) {
        if (!res.ok || !res.data || !res.data.asset_id) {
          throw new Error((res.data && (res.data.detail || res.data.message)) || '上传失败');
        }
        var row = {
          uid: _makeUid(kind),
          kind: kind,
          asset_id: res.data.asset_id,
          filename: res.data.filename || file.name || '',
          source_url: res.data.source_url || '',
          preview_url: res.data.source_url || ''
        };
        if (done) done(null, row);
      })
      .catch(function(err) {
        _setMsg('上传失败：' + (err && err.message ? err.message : '未知错误'), true);
        if (done) done(err || new Error('upload_failed'));
      });
  }

  function _bindUploader(buttonId, inputId, kind, appendMode) {
    var button = byId(buttonId);
    var input = byId(inputId);
    if (!button || !input) return;
    button.addEventListener('click', function() { input.click(); });
    input.addEventListener('change', function() {
      var files = input.files;
      if (!files || !files.length) return;
      Array.prototype.forEach.call(files, function(file) {
        _uploadFile(file, kind, function(err, row) {
          if (err || !row) return;
          if (appendMode) {
            if (kind === 'product_ref') state.productRefs.push(row);
            if (kind === 'style_ref') state.styleRefs.push(row);
            _renderReferenceAssets();
          } else {
            state.mainAsset = row;
            byId('ecomMainAssetIdInput').value = row.asset_id || '';
            _renderMainAsset();
          }
          _renderRequestedOutputs();
          _setMsg('素材上传成功，可继续填写参数后开始生成。', false);
        });
      });
      input.value = '';
    });
  }

  function _fetchAssetById(assetId, done) {
    var aid = (assetId || '').trim();
    var base = _localBase();
    if (!aid || !base) {
      if (done) done(new Error('missing_asset_or_base'));
      return;
    }
    fetch(base + '/api/assets/' + encodeURIComponent(aid), { headers: authHeaders() })
      .then(function(r) {
        return r.json().then(function(d) { return { ok: r.ok, data: d || {} }; });
      })
      .then(function(res) {
        if (!res.ok || !res.data || !res.data.asset_id) {
          throw new Error((res.data && (res.data.detail || res.data.message)) || '素材不存在');
        }
        var row = {
          uid: _makeUid('main'),
          kind: 'main',
          asset_id: res.data.asset_id,
          filename: res.data.filename || res.data.asset_id,
          source_url: res.data.source_url || '',
          preview_url: res.data.source_url || ''
        };
        if (done) done(null, row);
      })
      .catch(function(err) {
        if (done) done(err || new Error('asset_lookup_failed'));
      });
  }

  function _parseSellingPoints() {
    var raw = (byId('ecomSellingPointsInput').value || '').split(/\r?\n/);
    return raw.map(function(line) {
      var text = String(line || '').trim();
      if (!text) return null;
      var parts = text.split('|');
      return {
        title: (parts[0] || '').trim(),
        description: (parts.slice(1).join('|') || '').trim()
      };
    }).filter(function(item) { return item && item.title; });
  }

  function _parseSpecs() {
    var out = {};
    var raw = (byId('ecomSpecsInput').value || '').split(/\r?\n/);
    raw.forEach(function(line) {
      var text = String(line || '').trim();
      if (!text) return;
      var idx = text.indexOf(':') >= 0 ? text.indexOf(':') : text.indexOf('：');
      if (idx <= 0) return;
      var key = text.slice(0, idx).trim();
      var value = text.slice(idx + 1).trim();
      if (key) out[key] = value;
    });
    return out;
  }

  function _parseLines(id, separator) {
    var raw = (byId(id).value || '').split(/\r?\n/);
    return raw.map(function(line) { return String(line || '').trim(); }).filter(Boolean);
  }

  function _outputTargetsFromForm() {
    var out = {};
    OUTPUT_TARGETS.forEach(function(item) {
      var el = byId(item.id);
      out[item.key] = !!(el && el.checked);
    });
    return out;
  }

  function _scenePreferencesFromForm() {
    var decorTags = (byId('ecomDecorTagsInput').value || '')
      .split(/[,，]/)
      .map(function(item) { return String(item || '').trim(); })
      .filter(Boolean);
    return {
      include_pet: !!(byId('ecomIncludePetCheck') && byId('ecomIncludePetCheck').checked),
      pet_type: (byId('ecomPetTypeInput').value || '').trim(),
      include_human: !!(byId('ecomIncludeHumanCheck') && byId('ecomIncludeHumanCheck').checked),
      human_type: (byId('ecomHumanTypeInput').value || '').trim(),
      decor_tags: decorTags
    };
  }

  function _buildPayload() {
    var mainAssetId = (byId('ecomMainAssetIdInput').value || '').trim() || (state.mainAsset && state.mainAsset.asset_id) || '';
    if (!mainAssetId) return { error: '请先上传主商品图，或填写可用的 asset_id。' };
    var payload = {
      product_name_hint: (byId('ecomProductNameInput').value || '').trim(),
      product_direction_hint: (byId('ecomProductDirectionInput').value || '').trim(),
      sku: (byId('ecomSkuInput').value || '').trim(),
      brand: (byId('ecomBrandInput').value || '').trim(),
      selling_points: _parseSellingPoints(),
      specs: _parseSpecs(),
      style: (byId('ecomStyleSelect').value || '').trim() || 'creamy_wood',
      detail_template_id: (byId('ecomDetailTemplateSelect').value || '').trim() || 'detail_template_02',
      showcase_template_id: (byId('ecomShowcaseTemplateSelect').value || '').trim() || 'showcase_template_02',
      page_count: Number(byId('ecomPageCountInput').value || 12) || 12,
      auto_save: true,
      analysis_model: ANALYSIS_MODEL,
      image_model: IMAGE_MODEL,
      output_targets: _outputTargetsFromForm(),
      scene_preferences: _scenePreferencesFromForm(),
      style_reference_asset_ids: state.styleRefs.map(function(item) { return item.asset_id; }).filter(Boolean),
      compliance_notes: _parseLines('ecomComplianceNotesInput'),
      platform: 'ecommerce',
      country: 'China',
      language: 'zh-CN'
    };
    var productImages = [{ role: 'front', asset_id: mainAssetId }];
    state.productRefs.forEach(function(item, idx) {
      productImages.push({
        role: idx === 0 ? 'side' : 'detail',
        asset_id: item.asset_id
      });
    });
    if (productImages.length > 1) {
      payload.product_images = productImages;
    } else {
      payload.asset_id = mainAssetId;
    }
    return { payload: payload };
  }

  function _setWorkspaceTab(tab) {
    state.activeWorkspaceTab = tab || 'workspace';
    document.querySelectorAll('.ecom-workspace-tab').forEach(function(btn) {
      btn.classList.toggle('active', btn.getAttribute('data-ecom-tab') === state.activeWorkspaceTab);
    });
    if (byId('ecomWorkspacePanel')) byId('ecomWorkspacePanel').classList.toggle('visible', state.activeWorkspaceTab === 'workspace');
    if (byId('ecomExamplesPanel')) byId('ecomExamplesPanel').classList.toggle('visible', state.activeWorkspaceTab === 'examples');
  }

  function _normalizeSavedSuite(savedSuite) {
    var out = {};
    if (!savedSuite || typeof savedSuite !== 'object') return out;
    Object.keys(savedSuite).forEach(function(key) {
      var rows = Array.isArray(savedSuite[key]) ? savedSuite[key] : [];
      out[key] = rows.map(function(item, index) {
        var asset = item && item.asset ? item.asset : {};
        var previewUrl = (asset.preview_url || asset.open_url || asset.source_url || '').trim();
        return {
          title: (item && item.filename) || ('结果 ' + (index + 1)),
          meta: [asset.asset_id ? ('asset_id: ' + asset.asset_id) : '', item && item.relative_path ? item.relative_path : ''].filter(Boolean).join(' · '),
          preview_url: previewUrl,
          open_url: previewUrl,
          filename: (item && item.filename) || '',
          asset_id: asset.asset_id || '',
          width: item && item.width ? item.width : '',
          height: item && item.height ? item.height : ''
        };
      });
    });
    return out;
  }

  function _normalizeResultSuite__legacy_unused(result) {
    var bundle = result && result.suite_bundle && result.suite_bundle.categories ? result.suite_bundle.categories : {};
    var out = {};
    Object.keys(bundle || {}).forEach(function(key) {
      var payload = bundle[key] || {};
      var items = Array.isArray(payload.items) ? payload.items : [];
      out[key] = items.map(function(item, index) {
        var previewUrl = (item.generated_image_url || item.preview_url || item.open_url || item.source_url || '').trim();
        return {
          title: item.filename || ('结果 ' + (index + 1)),
          meta: [item.kind || '', item.width && item.height ? (item.width + '×' + item.height) : '', item.relative_path || ''].filter(Boolean).join(' · '),
          preview_url: previewUrl,
          open_url: previewUrl,
          filename: item.filename || '',
          asset_id: '',
          width: item.width || '',
          height: item.height || ''
        };
      });
    });
    return out;
  }

  function _collectGalleryData(resp) {
    var saved = resp && resp.saved_assets && resp.saved_assets.suite_bundle ? _normalizeSavedSuite(resp.saved_assets.suite_bundle) : {};
    var result = resp && resp.result ? _normalizeResultSuite(resp.result) : {};
    var out = {};
    Object.keys(RESULT_LABELS).forEach(function(key) {
      var rows = [];
      if (saved[key] && saved[key].length) rows = saved[key];
      else if (result[key] && result[key].length) rows = result[key];
      out[key] = rows;
    });
    return out;
  }

  function _renderRecentTasks() {
    var wrap = byId('ecomRecentTasks');
    if (!wrap) return;
    if (!state.recentJobs.length) {
      wrap.innerHTML = '<div class="ecom-empty" style="width:100%;margin:0;">提交任务后，这里会保留最近任务，方便切换查看不同商品的套图结果。</div>';
      return;
    }
    var visible = state.recentJobs.slice(0, 5);
    var extraCount = Math.max(0, state.recentJobs.length - visible.length);
    wrap.innerHTML = visible.map(function(item) {
      var title = item.productName || ('任务 ' + String(item.jobId || '').slice(0, 8));
      var meta = [_statusLabel(item.status), _formatTimeLabel(item.updatedAt || item.createdAt)].filter(Boolean).join(' · ');
      return (
        '<button type="button" class="ecom-task-pill' + (state.currentJobId === item.jobId ? ' active' : '') + '" data-task-pill="' + escapeAttr(item.jobId) + '">' +
          '<span class="name">' + escapeHtml(title) + '</span>' +
          '<span class="meta">' + escapeHtml(meta || '最近任务') + '</span>' +
        '</button>'
      );
    }).join('') + (extraCount > 0 ? '<button type="button" class="ecom-task-pill" data-task-drawer-more="1"><span class="name">+' + extraCount + ' 个任务</span><span class="meta">在全部任务中查看</span></button>' : '');
    wrap.querySelectorAll('[data-task-pill]').forEach(function(btn) {
      btn.addEventListener('click', function() {
        _activateRecentJob(btn.getAttribute('data-task-pill') || '');
      });
    });
    wrap.querySelectorAll('[data-task-drawer-more]').forEach(function(btn) {
      btn.addEventListener('click', function() {
        _setTaskDrawerOpen(true);
      });
    });
  }

  function _renderTaskDrawer() {
    var wrap = byId('ecomTaskDrawerList');
    if (!wrap) return;
    if (!state.recentJobs.length) {
      wrap.innerHTML = '<div class="ecom-empty">当前还没有任务记录。</div>';
      return;
    }
    wrap.innerHTML = state.recentJobs.map(function(item) {
      var counts = _getResultCounts(item.galleryByTab || {});
      var countChips = RESULT_ORDER.map(function(key) {
        var count = counts[key] || 0;
        if (!count) return '';
        return '<span class="ecom-task-count">' + escapeHtml(RESULT_LABELS[key]) + ' ' + count + '</span>';
      }).filter(Boolean).join('');
      var thumb = item.mainPreviewUrl
        ? '<img src="' + escapeAttr(item.mainPreviewUrl) + '" alt="">'
        : '<span>预览</span>';
      var meta = [
        _statusLabel(item.status),
        _formatTimeLabel(item.updatedAt || item.createdAt),
        item.suiteRootRelativePath || ''
      ].filter(Boolean).join(' · ');
      var hint = item.lastStep || item.productDirectionHint || '点击切换查看该任务';
      return (
        '<button type="button" class="ecom-task-card' + (state.currentJobId === item.jobId ? ' active' : '') + '" data-task-card="' + escapeAttr(item.jobId) + '">' +
          '<div class="ecom-task-card-shell">' +
            '<div class="ecom-task-card-thumb">' + thumb + '</div>' +
            '<div>' +
              '<div class="ecom-task-card-head">' +
                '<div class="ecom-task-card-title">' + escapeHtml(item.productName || ('任务 ' + String(item.jobId || '').slice(0, 8))) + '</div>' +
                '<span class="ecom-preview-stage">' + escapeHtml(_statusLabel(item.status)) + '</span>' +
              '</div>' +
              '<div class="ecom-task-card-meta">' + escapeHtml(meta || ('任务 ID ' + String(item.jobId || '').slice(0, 8))) + '</div>' +
              '<div class="ecom-task-card-meta">' + escapeHtml(hint) + '</div>' +
              '<div class="ecom-task-card-counts">' + (countChips || '<span class="ecom-task-count">暂无结果</span>') + '</div>' +
            '</div>' +
          '</div>' +
        '</button>'
      );
    }).join('');
    wrap.querySelectorAll('[data-task-card]').forEach(function(btn) {
      btn.addEventListener('click', function() {
        _activateRecentJob(btn.getAttribute('data-task-card') || '');
      });
    });
  }

  function _renderResultTabs(galleryByTab) {
    var wrap = byId('ecomResultTabs');
    if (!wrap) return;
    var keys = Object.keys(RESULT_LABELS).filter(function(key) {
      return galleryByTab[key] && galleryByTab[key].length;
    });
    if (!keys.length) keys = ['main_images'];
    if (!galleryByTab[state.activeResultTab] || !galleryByTab[state.activeResultTab].length) {
      state.activeResultTab = keys[0];
    }
    wrap.innerHTML = keys.map(function(key) {
      var count = galleryByTab[key] && galleryByTab[key].length ? galleryByTab[key].length : 0;
      return '<button type="button" class="ecom-result-tab' + (state.activeResultTab === key ? ' active' : '') + '" data-result-tab="' + escapeAttr(key) + '">' + escapeHtml(RESULT_LABELS[key] || key) + ' · ' + count + '</button>';
    }).join('');
    wrap.querySelectorAll('.ecom-result-tab').forEach(function(btn) {
      btn.addEventListener('click', function() {
        state.activeResultTab = btn.getAttribute('data-result-tab') || 'main_images';
        _renderGallery(state.lastGalleryByTab);
      });
    });
  }

  function _renderGallery(galleryByTab) {
    state.lastGalleryByTab = galleryByTab || {};
    _renderResultTabs(state.lastGalleryByTab);
    var el = byId('ecomGallery');
    if (!el) return;
    var rows = state.lastGalleryByTab[state.activeResultTab] || [];
    if (!rows.length) {
      el.innerHTML = '<div class="ecom-empty" style="grid-column:1 / -1;">当前分类还没有结果。</div>';
      return;
    }
    el.innerHTML = rows.map(function(item) {
      var openUrl = (item.open_url || item.preview_url || '').trim();
      return (
        '<div class="ecom-gallery-item">' +
          '<div class="ecom-gallery-thumb">' +
            (item.preview_url ? '<img src="' + escapeAttr(item.preview_url) + '" alt="">' : '<div class="ecom-empty" style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;padding:0.5rem;">暂无预览</div>') +
          '</div>' +
          '<div class="ecom-gallery-body">' +
            '<div class="title">' + escapeHtml(item.title || '未命名结果') + '</div>' +
            '<div class="meta">' + escapeHtml(item.meta || '无附加信息') + '</div>' +
            '<div class="ecom-gallery-actions">' +
              (openUrl ? '<a class="btn btn-primary btn-sm" href="' + escapeAttr(openUrl) + '" target="_blank" rel="noopener">打开</a>' : '') +
              ((item.asset_id && typeof copyToClipboard === 'function')
                ? '<button type="button" class="btn btn-ghost btn-sm" data-copy-asset-id="' + escapeAttr(item.asset_id) + '">复制 asset_id</button>'
                : '') +
            '</div>' +
          '</div>' +
        '</div>'
      );
    }).join('');
    el.querySelectorAll('[data-copy-asset-id]').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var aid = btn.getAttribute('data-copy-asset-id') || '';
        if (!aid || typeof copyToClipboard !== 'function') return;
        copyToClipboard(aid, function() {
          _setMsg('已复制 asset_id：' + aid, false);
        });
      });
    });
  }

  function _renderOverviewFromGallery(galleryByTab) {
    var wrap = byId('ecomOverviewStats');
    if (!wrap) return;
    var items = [
      { label: '主图', value: (galleryByTab.main_images || []).length },
      { label: 'SKU 图', value: (galleryByTab.sku_images || []).length },
      { label: '详情图', value: (galleryByTab.detail_images || []).length },
      { label: '橱窗图', value: (galleryByTab.showcase_images || []).length }
    ];
    wrap.innerHTML = items.map(function(item) {
      return '<div class="ecom-stat-card"><div class="label">' + escapeHtml(item.label) + '</div><div class="value">' + item.value + '</div></div>';
    }).join('');
  }

  function _renderRequestedOutputs() {
    var wrap = byId('ecomRequestedOutputs');
    if (!wrap) return;
    var targets = _outputTargetsFromForm();
    var active = [];
    OUTPUT_TARGETS.forEach(function(item) {
      if (targets[item.key]) active.push(item.label);
    });
    if (!active.length) {
      wrap.innerHTML = '<div class="ecom-empty" style="grid-column:1 / -1;">至少勾选一个输出内容。</div>';
      return;
    }
    wrap.innerHTML = active.map(function(label, index) {
      return '<div class="ecom-stage-chip"><div class="kicker">output ' + (index + 1) + '</div><div class="name">' + escapeHtml(label) + '</div></div>';
    }).join('');
  }

  function _renderStageChips(resp) {
    var wrap = byId('ecomStageChips');
    if (!wrap) return;
    var progress = resp && resp.progress ? resp.progress : {};
    var lastSteps = Array.isArray(progress.last_steps) ? progress.last_steps : [];
    var doneMap = {};
    lastSteps.forEach(function(step) {
      if (step && step.name) doneMap[step.name] = String(step.status || '').toLowerCase();
    });
    var highestDone = -1;
    STAGE_META.forEach(function(stage, idx) {
      if (doneMap[stage.key] === 'success') highestDone = idx;
    });
    var runningIdx = resp && resp.status === 'running' ? Math.min(highestDone + 1, STAGE_META.length - 1) : -1;
    if (resp && resp.status === 'completed') runningIdx = -1;
    wrap.innerHTML = STAGE_META.map(function(stage, idx) {
      var cls = '';
      if (doneMap[stage.key] === 'success' || (resp && resp.status === 'completed')) cls = ' done';
      else if (idx === runningIdx) cls = ' running';
      return '<div class="ecom-stage-chip' + cls + '"><div class="kicker">' + String(idx + 1).padStart(2, '0') + '</div><div class="name">' + escapeHtml(stage.label) + '</div></div>';
    }).join('');
    var progressFill = byId('ecomProgressBarFill');
    if (progressFill) {
      var ratio = resp && resp.status === 'completed' ? 100 : Math.max(0, Math.round(((highestDone + 1) / STAGE_META.length) * 100));
      if (resp && resp.status === 'failed') ratio = Math.max(ratio, 12);
      progressFill.style.width = ratio + '%';
    }
  }

  function _renderActivity(resp) {
    var wrap = byId('ecomActivityList');
    if (!wrap) return;
    var progress = resp && resp.progress ? resp.progress : {};
    var steps = Array.isArray(progress.last_steps) ? progress.last_steps.slice().reverse() : [];
    if (!steps.length) {
      wrap.innerHTML = '<div class="ecom-empty">当前还没有可展示的阶段记录。</div>';
      return;
    }
    wrap.innerHTML = steps.map(function(step) {
      var title = step.name || '未知步骤';
      var status = step.status || 'unknown';
      var meta = [];
      if (step.updated_at) meta.push(step.updated_at);
      if (step.attempts) meta.push('尝试 ' + step.attempts + ' 次');
      if (step.error) meta.push(step.error);
      return '<div class="ecom-activity-item"><div><strong>' + escapeHtml(title) + '</strong> · ' + escapeHtml(status) + '</div><div class="meta">' + escapeHtml(meta.join(' · ') || '无额外信息') + '</div></div>';
    }).join('');
  }

  function _renderFacts__legacy_unused(resp) {
    var wrap = byId('ecomRunFacts');
    if (!wrap) return;
    var result = resp && resp.result ? resp.result : {};
    var config = result && result.config ? result.config : {};
    var progress = resp && resp.progress ? resp.progress : {};
    var usage = progress && progress.usage_summary ? progress.usage_summary : {};
    var facts = [];
    if (state.currentJobId) facts.push('任务 ID：' + state.currentJobId);
    if (config.analysis_model) facts.push('分析模型：' + config.analysis_model);
    if (config.image_model) facts.push('生图模型：' + config.image_model);
    if (config.page_count) facts.push('详情页数：' + config.page_count);
    if (usage.image_count != null) facts.push('生图次数：' + usage.image_count);
    if (usage.analysis_count != null) facts.push('分析次数：' + usage.analysis_count);
    if (usage.total_points != null) facts.push('累计点数：' + usage.total_points);
    if (result && result.suite_bundle && result.suite_bundle.root_relative_path) facts.push('输出目录：' + result.suite_bundle.root_relative_path);
    if (!facts.length) {
      wrap.innerHTML = '<div class="ecom-empty">生成完成后，这里会显示模型、页数、算力消耗和输出目录摘要。</div>';
      return;
    }
    wrap.innerHTML = facts.map(function(item) {
      return '<div class="ecom-activity-item"><div>' + escapeHtml(item) + '</div></div>';
    }).join('');
  }

  function _renderStatus__legacy_unused(resp) {
    state.latestResponse = resp || null;
    var status = resp && resp.status ? resp.status : 'idle';
    var summaryEl = byId('ecomStatusSummary');
    var previewStatus = byId('ecomPreviewStatus');
    var currentJob = byId('ecomCurrentJobText');
    if (previewStatus) {
      previewStatus.textContent = status === 'completed' ? '已完成' : status === 'failed' ? '失败' : status === 'running' ? '生成中' : '待提交';
    }
    if (currentJob) {
      currentJob.textContent = state.currentJobId ? ('任务 ' + state.currentJobId.slice(0, 8)) : '尚未开始';
    }
    if (summaryEl) {
      if (status === 'completed') summaryEl.textContent = '生成完成，可以在下方按分类查看本次套图产物。';
      else if (status === 'failed') summaryEl.textContent = '任务执行失败，请根据最近步骤与错误信息调整输入后重试。';
      else if (status === 'running') summaryEl.textContent = '任务运行中，页面会自动轮询最新进度。';
      else summaryEl.textContent = '提交后会显示流水线阶段、最近步骤和结果摘要。';
    }
    _renderStageChips(resp);
    _renderActivity(resp);
    _renderFacts(resp);
    var galleryByTab = _collectGalleryData(resp || {});
    _renderOverviewFromGallery(galleryByTab);
    _renderGallery(galleryByTab);
  }

  function _refreshJobStatus__legacy_unused(showToast) {
    var base = _localBase();
    if (!base || !state.currentJobId) return;
    fetch(base + '/api/comfly-ecommerce-detail/pipeline/jobs/' + encodeURIComponent(state.currentJobId), {
      headers: authHeaders()
    })
      .then(function(r) {
        return r.json().then(function(d) { return { ok: r.ok, data: d || {} }; });
      })
      .then(function(res) {
        if (!res.ok) {
          throw new Error((res.data && (res.data.detail || res.data.message)) || '状态查询失败');
        }
        _renderStatus(res.data);
        if (res.data.status === 'running') {
          _schedulePoll(4000);
        } else {
          _stopPolling();
          if (showToast) _setMsg(res.data.status === 'completed' ? '任务已完成。' : '任务状态已刷新。', false);
        }
      })
      .catch(function(err) {
        _stopPolling();
        _setMsg('刷新状态失败：' + (err && err.message ? err.message : '未知错误'), true);
      });
  }

  function _startRun() {
    var base = _localBase();
    if (!base) {
      _setMsg('当前未检测到本机 LOCAL_API_BASE，无法提交套图任务。', true);
      return;
    }
    var mainAssetId = (byId('ecomMainAssetIdInput').value || '').trim();
    if (!state.mainAsset && mainAssetId) {
      _fetchAssetById(mainAssetId, function(err, row) {
        if (!err && row) {
          row.kind = 'main';
          state.mainAsset = row;
          _renderMainAsset();
        }
      });
    }
    var built = _buildPayload();
    if (built.error) {
      _setMsg(built.error, true);
      return;
    }
    var btn = byId('ecomStartBtn');
    if (btn) {
      btn.disabled = true;
      btn.textContent = '提交中...';
    }
    _setMsg('正在提交套图任务，请稍候...', false);
    fetch(base + '/api/comfly-ecommerce-detail/pipeline/start', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ payload: built.payload })
    })
      .then(function(r) {
        return r.json().then(function(d) { return { ok: r.ok, data: d || {} }; });
      })
      .then(function(res) {
        if (!res.ok || !res.data || !res.data.job_id) {
          throw new Error((res.data && (res.data.detail || res.data.message)) || '任务提交失败');
        }
        state.currentJobId = res.data.job_id;
        _setWorkspaceTab('workspace');
        _setMsg('任务已提交，开始自动轮询进度。', false);
        _renderStatus({ status: 'running', progress: { last_steps: [] } });
        _refreshJobStatus(false);
      })
      .catch(function(err) {
        _setMsg('提交失败：' + (err && err.message ? err.message : '未知错误'), true);
      })
      .finally(function() {
        if (btn) {
          btn.disabled = false;
          btn.textContent = '开始生成套图';
        }
      });
  }

  function _resetForm() {
    state.mainAsset = null;
    state.productRefs = [];
    state.styleRefs = [];
    state.currentJobId = '';
    state.latestResponse = null;
    state.lastGalleryByTab = {};
    state.activeResultTab = 'main_images';
    _stopPolling();
    [
      'ecomMainAssetIdInput',
      'ecomProductNameInput',
      'ecomProductDirectionInput',
      'ecomSkuInput',
      'ecomBrandInput',
      'ecomSellingPointsInput',
      'ecomSpecsInput',
      'ecomComplianceNotesInput',
      'ecomPetTypeInput',
      'ecomHumanTypeInput',
      'ecomDecorTagsInput'
    ].forEach(function(id) {
      if (byId(id)) byId(id).value = '';
    });
    if (byId('ecomStyleSelect')) byId('ecomStyleSelect').value = 'creamy_wood';
    if (byId('ecomDetailTemplateSelect')) byId('ecomDetailTemplateSelect').value = 'detail_template_02';
    if (byId('ecomShowcaseTemplateSelect')) byId('ecomShowcaseTemplateSelect').value = 'showcase_template_02';
    if (byId('ecomPageCountInput')) byId('ecomPageCountInput').value = 12;
    OUTPUT_TARGETS.forEach(function(item) {
      if (byId(item.id)) byId(item.id).checked = true;
    });
    if (byId('ecomIncludePetCheck')) byId('ecomIncludePetCheck').checked = false;
    if (byId('ecomIncludeHumanCheck')) byId('ecomIncludeHumanCheck').checked = false;
    _renderMainAsset();
    _renderReferenceAssets();
    _renderRequestedOutputs();
    _renderStatus(null);
    _setMsg('', false);
  }

  function _bindWorkspaceTabs() {
    document.querySelectorAll('.ecom-workspace-tab').forEach(function(btn) {
      btn.addEventListener('click', function() {
        _setWorkspaceTab(btn.getAttribute('data-ecom-tab') || 'workspace');
      });
    });
  }

  function _bindFormWatchers() {
    OUTPUT_TARGETS.forEach(function(item) {
      var el = byId(item.id);
      if (el) el.addEventListener('change', _renderRequestedOutputs);
    });
  }

  function _bindActions() {
    var backBtn = byId('ecomStudioBackBtn');
    if (backBtn) backBtn.addEventListener('click', function() {
      if (typeof window._ensureSkillStoreVisible === 'function') window._ensureSkillStoreVisible();
    });
    var startBtn = byId('ecomStartBtn');
    if (startBtn) startBtn.addEventListener('click', _startRun);
    var resetBtn = byId('ecomResetBtn');
    if (resetBtn) resetBtn.addEventListener('click', _resetForm);
    var refreshBtn = byId('ecomRefreshBtn');
    if (refreshBtn) refreshBtn.addEventListener('click', function() { _refreshJobStatus(true); });
    var mainAssetInput = byId('ecomMainAssetIdInput');
    if (mainAssetInput) {
      mainAssetInput.addEventListener('change', function() {
        var aid = (mainAssetInput.value || '').trim();
        if (!aid) {
          state.mainAsset = null;
          _renderMainAsset();
          return;
        }
        _fetchAssetById(aid, function(err, row) {
          if (err || !row) {
            _setMsg('未找到该 asset_id 对应的素材，请确认后重试。', true);
            return;
          }
          row.kind = 'main';
          state.mainAsset = row;
          _renderMainAsset();
          _setMsg('已载入现有素材，可直接开始生成。', false);
        });
      });
    }
  }

  function _normalizeResultSuite(result) {
    var bundle = result && result.suite_bundle && result.suite_bundle.categories ? result.suite_bundle.categories : {};
    var out = {};
    Object.keys(bundle || {}).forEach(function(key) {
      var payload = bundle[key] || {};
      var items = Array.isArray(payload.items) ? payload.items : [];
      out[key] = items.map(function(item, index) {
        var previewUrl = (item.generated_image_url || item.preview_url || item.open_url || item.source_url || '').trim();
        var sizeLabel = item.width && item.height ? (String(item.width) + 'x' + String(item.height)) : '';
        return {
          title: item.filename || ('结果 ' + (index + 1)),
          meta: [item.shot_label || '', item.kind || '', sizeLabel, item.relative_path || ''].filter(Boolean).join(' · '),
          preview_url: previewUrl,
          open_url: previewUrl,
          filename: item.filename || '',
          asset_id: '',
          width: item.width || '',
          height: item.height || ''
        };
      });
    });
    return out;
  }

  function _renderFacts(resp) {
    var wrap = byId('ecomRunFacts');
    if (!wrap) return;
    var result = resp && resp.result ? resp.result : {};
    var config = result && result.config ? result.config : {};
    var progress = resp && resp.progress ? resp.progress : {};
    var usage = progress && progress.usage_summary ? progress.usage_summary : {};
    var facts = [];
    if (state.currentJobId) facts.push('任务 ID：' + state.currentJobId);
    if (config.analysis_model) facts.push('分析模型：' + config.analysis_model);
    if (config.image_model) facts.push('生图模型：' + config.image_model);
    if (config.page_count) facts.push('详情页数：' + config.page_count);
    if (usage.image_count != null) facts.push('生图次数：' + usage.image_count);
    if (usage.analysis_count != null) facts.push('分析次数：' + usage.analysis_count);
    if (usage.total_points != null) facts.push('累计点数：' + usage.total_points);
    if (result && result.suite_bundle && result.suite_bundle.root_relative_path) facts.push('输出目录：' + result.suite_bundle.root_relative_path);
    facts = facts.concat(_collectProgressFacts(resp));
    if (!facts.length) {
      wrap.innerHTML = '<div class="ecom-empty">生成完成后，这里会显示模型、页数、算力消耗和输出目录摘要。</div>';
      return;
    }
    wrap.innerHTML = facts.map(function(item) {
      return '<div class="ecom-activity-item"><div>' + escapeHtml(item) + '</div></div>';
    }).join('');
  }

  function _renderStatus(resp) {
    state.latestResponse = resp || null;
    var status = resp && resp.status ? resp.status : 'idle';
    var summaryEl = byId('ecomStatusSummary');
    var previewStatus = byId('ecomPreviewStatus');
    var currentJob = byId('ecomCurrentJobText');
    if (previewStatus) {
      previewStatus.textContent = status === 'completed' ? '已完成' : status === 'failed' ? '失败' : status === 'running' ? '生成中' : '待提交';
    }
    if (currentJob) {
      currentJob.textContent = state.currentJobId ? ('任务 ' + state.currentJobId.slice(0, 8)) : '尚未开始';
    }
    if (summaryEl) {
      if (status === 'completed') summaryEl.textContent = '生成完成，可以在下方按分类查看本次套图产物。';
      else if (status === 'failed') summaryEl.textContent = _pickResponseMessage(resp, '任务执行失败，请根据最近步骤与错误信息调整输入后重试。');
      else if (status === 'running') summaryEl.textContent = '任务运行中，页面会自动轮询最新进度。';
      else summaryEl.textContent = '提交后会显示流水线阶段、最近步骤和结果摘要。';
    }
    _renderStageChips(resp);
    _renderActivity(resp);
    _renderFacts(resp);
    var galleryByTab = _collectGalleryData(resp || {});
    _renderOverviewFromGallery(galleryByTab);
    _renderGallery(galleryByTab);
  }

  function _refreshJobStatus(showToast) {
    var base = _localBase();
    if (!base || !state.currentJobId) return;
    fetch(base + '/api/comfly-ecommerce-detail/pipeline/jobs/' + encodeURIComponent(state.currentJobId), {
      headers: authHeaders()
    })
      .then(function(r) {
        return r.json().then(function(d) { return { ok: r.ok, data: d || {} }; });
      })
      .then(function(res) {
        if (!res.ok) {
          throw new Error((res.data && (res.data.detail || res.data.message)) || '状态查询失败');
        }
        _renderStatus(res.data);
        if (res.data.status === 'running') {
          _schedulePoll(4000);
        } else {
          _stopPolling();
          if (showToast) {
            _setMsg(
              res.data.status === 'completed'
                ? '任务已完成。'
                : _pickResponseMessage(res.data, '任务状态已刷新。'),
              res.data.status === 'failed'
            );
          }
        }
      })
      .catch(function(err) {
        _stopPolling();
        _setMsg('刷新状态失败：' + (err && err.message ? err.message : '未知错误'), true);
      });
  }

  function _renderResultTabs(galleryByTab) {
    var wrap = byId('ecomResultTabs');
    if (!wrap) return;
    if (!RESULT_ORDER.some(function(key) { return key === state.activeResultTab; })) {
      state.activeResultTab = 'main_images';
    }
    wrap.innerHTML = RESULT_ORDER.map(function(key) {
      var count = galleryByTab[key] && galleryByTab[key].length ? galleryByTab[key].length : 0;
      var cls = 'ecom-result-tab' + (state.activeResultTab === key ? ' active' : '') + (count ? '' : ' is-empty');
      return '<button type="button" class="' + cls + '" data-result-tab="' + escapeAttr(key) + '">' + escapeHtml(_resultFolderLabel(key)) + ' · ' + count + '</button>';
    }).join('');
    wrap.querySelectorAll('.ecom-result-tab').forEach(function(btn) {
      btn.addEventListener('click', function() {
        state.activeResultTab = btn.getAttribute('data-result-tab') || 'main_images';
        _renderGallery(state.lastGalleryByTab);
      });
    });
  }

  function _renderFocusedPreview(rows) {
    var previewEl = byId('ecomFocusedPreview');
    var metaEl = byId('ecomFocusedMeta');
    var titleEl = byId('ecomFocusedFolderTitle');
    var hintEl = byId('ecomFocusedFolderHint');
    var counterEl = byId('ecomFocusedCounter');
    if (titleEl) titleEl.textContent = _resultFolderLabel(state.activeResultTab);
    if (counterEl) counterEl.textContent = String((rows || []).length) + ' 张';
    if (!previewEl || !metaEl) return;
    if (!rows || !rows.length) {
      previewEl.innerHTML = '<div class="ecom-empty" style="width:100%;margin:0;">当前分类暂无结果，任务完成后会在这里显示大图预览。</div>';
      metaEl.innerHTML = '<div class="ecom-empty" style="width:100%;margin:0;">可以先切换到其他目录查看，或者等待当前任务继续生成。</div>';
      if (hintEl) hintEl.textContent = '当前分类暂无结果，任务完成后会在这里显示大图预览。';
      return;
    }
    var index = state.focusedResultIndexByTab[state.activeResultTab] || 0;
    if (index < 0 || index >= rows.length) index = 0;
    state.focusedResultIndexByTab[state.activeResultTab] = index;
    var item = rows[index] || {};
    var openUrl = (item.open_url || item.preview_url || '').trim();
    previewEl.innerHTML = item.preview_url
      ? '<img src="' + escapeAttr(item.preview_url) + '" alt="">'
      : '<div class="ecom-empty" style="width:100%;margin:0;">当前结果暂无可展示预览。</div>';
    metaEl.innerHTML =
      '<div class="ecom-focused-meta-copy">' +
        '<div class="ecom-focused-meta-title">' + escapeHtml(item.title || '未命名结果') + '</div>' +
        '<div class="ecom-focused-meta-sub">' + escapeHtml(item.meta || '当前结果没有额外说明信息。') + '</div>' +
      '</div>' +
      '<div class="ecom-focused-meta-actions">' +
        (openUrl ? '<a class="btn btn-primary btn-sm" href="' + escapeAttr(openUrl) + '" target="_blank" rel="noopener">打开原图</a>' : '') +
        (item.asset_id && typeof copyToClipboard === 'function'
          ? '<button type="button" class="btn btn-ghost btn-sm" data-copy-focused-asset="' + escapeAttr(item.asset_id) + '">复制 asset_id</button>'
          : '') +
      '</div>';
    if (hintEl) hintEl.textContent = '当前目录共 ' + String(rows.length) + ' 张，点击下方缩略图可切换大图。';
    metaEl.querySelectorAll('[data-copy-focused-asset]').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var aid = btn.getAttribute('data-copy-focused-asset') || '';
        if (!aid || typeof copyToClipboard !== 'function') return;
        copyToClipboard(aid, function() {
          _setMsg('已复制 asset_id：' + aid, false);
        });
      });
    });
  }

  function _renderGallery(galleryByTab) {
    state.lastGalleryByTab = galleryByTab || {};
    _renderResultTabs(state.lastGalleryByTab);
    var el = byId('ecomGallery');
    if (!el) return;
    var rows = state.lastGalleryByTab[state.activeResultTab] || [];
    _renderFocusedPreview(rows);
    if (!rows.length) {
      el.innerHTML = '<div class="ecom-empty" style="grid-column:1 / -1;">当前分类还没有结果。</div>';
      return;
    }
    el.innerHTML = rows.map(function(item, index) {
      var active = (state.focusedResultIndexByTab[state.activeResultTab] || 0) === index;
      return (
        '<button type="button" class="ecom-gallery-item' + (active ? ' active' : '') + '" data-gallery-index="' + index + '">' +
          '<div class="ecom-gallery-thumb">' +
            (item.preview_url ? '<img src="' + escapeAttr(item.preview_url) + '" alt="">' : '<div class="ecom-empty" style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;padding:0.5rem;">暂无预览</div>') +
          '</div>' +
          '<div class="ecom-gallery-body">' +
            '<div class="title">' + escapeHtml(item.title || '未命名结果') + '</div>' +
            '<div class="meta">' + escapeHtml(item.meta || '无附加信息') + '</div>' +
          '</div>' +
        '</button>'
      );
    }).join('');
    el.querySelectorAll('[data-gallery-index]').forEach(function(btn) {
      btn.addEventListener('click', function() {
        state.focusedResultIndexByTab[state.activeResultTab] = Number(btn.getAttribute('data-gallery-index') || 0) || 0;
        _renderGallery(state.lastGalleryByTab);
      });
    });
  }

  function _renderOverviewFromGallery(galleryByTab) {
    var wrap = byId('ecomOverviewStats');
    if (!wrap) return;
    var items = [
      { label: '主图', value: (galleryByTab.main_images || []).length },
      { label: 'SKU 图', value: (galleryByTab.sku_images || []).length },
      { label: '详情图', value: (galleryByTab.detail_images || []).length },
      { label: '橱窗图', value: (galleryByTab.showcase_images || []).length }
    ];
    wrap.innerHTML = items.map(function(item) {
      return '<div class="ecom-stat-card"><div class="label">' + escapeHtml(item.label) + '</div><div class="value">' + item.value + '</div></div>';
    }).join('');
  }

  function _renderRequestedOutputs(labelsOverride) {
    var wrap = byId('ecomRequestedOutputs');
    if (!wrap) return;
    var active = Array.isArray(labelsOverride) && labelsOverride.length ? labelsOverride.slice() : _jobRequestedOutputsFromForm();
    if (!active.length) {
      wrap.innerHTML = '<div class="ecom-empty" style="grid-column:1 / -1;">至少勾选一个输出内容。</div>';
      return;
    }
    wrap.innerHTML = active.map(function(label, index) {
      return '<div class="ecom-stage-chip"><div class="kicker">output ' + (index + 1) + '</div><div class="name">' + escapeHtml(label) + '</div></div>';
    }).join('');
  }

  function _renderCurrentTaskHero(resp, galleryByTab) {
    var status = resp && resp.status ? resp.status : 'idle';
    var record = state.currentJobId ? _findRecentJob(state.currentJobId) : null;
    var totalCount = _resultTotalCount(galleryByTab || {});
    var titleEl = byId('ecomCurrentTaskTitle');
    var metaEl = byId('ecomCurrentTaskMeta');
    var previewStatus = byId('ecomPreviewStatus');
    var currentJob = byId('ecomCurrentJobText');
    var summaryEl = byId('ecomStatusSummary');
    if (previewStatus) previewStatus.textContent = _statusLabel(status);
    if (currentJob) currentJob.textContent = state.currentJobId ? ('任务 ' + state.currentJobId.slice(0, 8)) : '尚未开始';
    if (titleEl) {
      titleEl.textContent = record && record.productName
        ? record.productName
        : ((state.mainAsset && state.mainAsset.filename) || '等待提交商品套图任务');
    }
    if (metaEl) {
      var metaParts = [];
      if (record && record.productDirectionHint) metaParts.push(record.productDirectionHint);
      if (record && (record.updatedAt || record.createdAt)) metaParts.push('最近更新 ' + _formatTimeLabel(record.updatedAt || record.createdAt));
      if (record && record.suiteRootRelativePath) metaParts.push(record.suiteRootRelativePath);
      if (totalCount) metaParts.push('共 ' + totalCount + ' 张结果');
      metaEl.textContent = metaParts.length
        ? metaParts.join(' · ')
        : '上传主图并提交后，这里会保留最近任务，方便在不同商品任务之间来回切换查看结果。';
    }
    if (summaryEl) {
      if (status === 'completed') summaryEl.textContent = '任务已完成，按真实导出目录切换查看主图、SKU 图、透明白底、详情图、素材图和橱窗图。';
      else if (status === 'failed') summaryEl.textContent = _pickResponseMessage(resp, '当前任务执行失败，可以切换到其他历史任务继续查看结果。');
      else if (status === 'running') summaryEl.textContent = '任务生成中，右侧仅保留轻量进度，主区域优先展示最终图片结果。';
      else summaryEl.textContent = '按真实导出目录浏览结果，右侧只保留轻量状态和任务摘要。';
    }
    var publishBtn = byId('ecomPublishToShopBtn');
    if (publishBtn) {
      publishBtn.style.display = (status === 'completed' && state.currentJobId) ? '' : 'none';
    }
  }

  function _renderStatus(resp) {
    var activeRecord = state.currentJobId ? _findRecentJob(state.currentJobId) : null;
    var effectiveResp = resp || (activeRecord && activeRecord.latestResponse) || null;
    state.latestResponse = effectiveResp || null;
    var galleryByTab = effectiveResp ? _collectGalleryData(effectiveResp) : ((activeRecord && activeRecord.galleryByTab) || {});
    if (effectiveResp && state.currentJobId) {
      _syncCurrentJobHistory(effectiveResp, galleryByTab);
      activeRecord = _findRecentJob(state.currentJobId) || activeRecord;
    }
    _renderCurrentTaskHero(effectiveResp, galleryByTab);
    _renderStageChips(effectiveResp);
    _renderActivity(effectiveResp);
    _renderFacts(effectiveResp);
    _renderOverviewFromGallery(galleryByTab);
    _renderRequestedOutputs(activeRecord && activeRecord.requestedOutputs ? activeRecord.requestedOutputs : null);
    _renderGallery(galleryByTab);
    _renderRecentTasks();
    _renderTaskDrawer();
  }

  function _refreshJobStatus(showToast) {
    var base = _localBase();
    if (!base || !state.currentJobId) return;
    fetch(base + '/api/comfly-ecommerce-detail/pipeline/jobs/' + encodeURIComponent(state.currentJobId), {
      headers: authHeaders()
    })
      .then(function(r) {
        return r.json().then(function(d) { return { ok: r.ok, data: d || {} }; });
      })
      .then(function(res) {
        if (!res.ok) {
          throw new Error((res.data && (res.data.detail || res.data.message)) || '状态查询失败');
        }
        _renderStatus(res.data);
        if (res.data.status === 'running') {
          _schedulePoll(4000);
        } else {
          _stopPolling();
          if (showToast) {
            _setMsg(
              res.data.status === 'completed'
                ? '任务已完成。'
                : _pickResponseMessage(res.data, '任务状态已刷新。'),
              res.data.status === 'failed'
            );
          }
        }
      })
      .catch(function(err) {
        _stopPolling();
        _setMsg('刷新状态失败：' + (err && err.message ? err.message : '未知错误'), true);
      });
  }

  function _startRun() {
    var base = _localBase();
    if (!base) {
      _setMsg('当前未检测到本机 LOCAL_API_BASE，无法提交套图任务。', true);
      return;
    }
    var mainAssetId = (byId('ecomMainAssetIdInput').value || '').trim();
    if (!state.mainAsset && mainAssetId) {
      _fetchAssetById(mainAssetId, function(err, row) {
        if (!err && row) {
          row.kind = 'main';
          state.mainAsset = row;
          _renderMainAsset();
        }
      });
    }
    var built = _buildPayload();
    if (built.error) {
      _setMsg(built.error, true);
      return;
    }
    var btn = byId('ecomStartBtn');
    if (btn) {
      btn.disabled = true;
      btn.textContent = '提交中...';
    }
    _setMsg('正在提交套图任务，请稍候...', false);
    fetch(base + '/api/comfly-ecommerce-detail/pipeline/start', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ payload: built.payload })
    })
      .then(function(r) {
        return r.json().then(function(d) { return { ok: r.ok, data: d || {} }; });
      })
      .then(function(res) {
        if (!res.ok || !res.data || !res.data.job_id) {
          throw new Error((res.data && (res.data.detail || res.data.message)) || '任务提交失败');
        }
        state.currentJobId = res.data.job_id;
        state.focusedResultIndexByTab = {};
        _upsertRecentJob(_buildJobDraft(res.data.job_id));
        _setWorkspaceTab('workspace');
        _setTaskDrawerOpen(false);
        _setMsg('任务已提交，开始自动轮询进度。', false);
        _renderStatus({ status: 'running', progress: { last_steps: [] } });
        _refreshJobStatus(false);
      })
      .catch(function(err) {
        _setMsg('提交失败：' + (err && err.message ? err.message : '未知错误'), true);
      })
      .finally(function() {
        if (btn) {
          btn.disabled = false;
          btn.textContent = '开始生成套图';
        }
      });
  }

  function _resetForm() {
    state.mainAsset = null;
    state.productRefs = [];
    state.styleRefs = [];
    [
      'ecomMainAssetIdInput',
      'ecomProductNameInput',
      'ecomProductDirectionInput',
      'ecomSkuInput',
      'ecomBrandInput',
      'ecomSellingPointsInput',
      'ecomSpecsInput',
      'ecomComplianceNotesInput',
      'ecomPetTypeInput',
      'ecomHumanTypeInput',
      'ecomDecorTagsInput'
    ].forEach(function(id) {
      if (byId(id)) byId(id).value = '';
    });
    if (byId('ecomStyleSelect')) byId('ecomStyleSelect').value = 'creamy_wood';
    if (byId('ecomDetailTemplateSelect')) byId('ecomDetailTemplateSelect').value = 'detail_template_02';
    if (byId('ecomShowcaseTemplateSelect')) byId('ecomShowcaseTemplateSelect').value = 'showcase_template_02';
    if (byId('ecomPageCountInput')) byId('ecomPageCountInput').value = 12;
    OUTPUT_TARGETS.forEach(function(item) {
      if (byId(item.id)) byId(item.id).checked = true;
    });
    if (byId('ecomIncludePetCheck')) byId('ecomIncludePetCheck').checked = false;
    if (byId('ecomIncludeHumanCheck')) byId('ecomIncludeHumanCheck').checked = false;
    _renderMainAsset();
    _renderReferenceAssets();
    var activeRecord = state.currentJobId ? _findRecentJob(state.currentJobId) : null;
    _renderRequestedOutputs(activeRecord && activeRecord.requestedOutputs ? activeRecord.requestedOutputs : null);
    _renderStatus(state.latestResponse);
    _setMsg('', false);
  }

  function _publishToShop() {
    var base = _localBase();
    if (!base || !state.currentJobId) {
      _setMsg('无有效任务或未连接后端。', true);
      return;
    }
    var btn = byId('ecomPublishToShopBtn');
    if (btn) { btn.disabled = true; btn.textContent = '正在打开抖店…'; }
    var record = _findRecentJob(state.currentJobId);
    var payload = {
      job_id: state.currentJobId,
      platform: 'douyin_shop',
      title: record && record.productName ? record.productName : undefined
    };
    fetch(base + '/api/ecommerce-publish/from-job', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      body: JSON.stringify(payload)
    })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
      .then(function(res) {
        if (btn) { btn.disabled = false; btn.textContent = '发布到抖店'; }
        if (res.ok && res.data && res.data.ok) {
          var parts = ['已打开抖店商品发布页面'];
          if (res.data.auto_filled && res.data.auto_filled.length) {
            parts.push('自动填充: ' + res.data.auto_filled.join(', '));
          }
          _setMsg(parts.join('，') + '。请在浏览器中检查并手动发布。', false);
        } else if (res.data && res.data.need_login) {
          _setMsg(res.data.message || '请先登录抖店', true);
        } else {
          _setMsg(res.data.detail || res.data.message || '发布失败', true);
        }
      })
      .catch(function(err) {
        if (btn) { btn.disabled = false; btn.textContent = '发布到抖店'; }
        _setMsg('发布请求失败: ' + (err.message || err), true);
      });
  }

  function _bindActions() {
    var backBtn = byId('ecomStudioBackBtn');
    if (backBtn) backBtn.addEventListener('click', function() {
      if (typeof window._ensureSkillStoreVisible === 'function') window._ensureSkillStoreVisible();
    });
    var startBtn = byId('ecomStartBtn');
    if (startBtn) startBtn.addEventListener('click', _startRun);
    var resetBtn = byId('ecomResetBtn');
    if (resetBtn) resetBtn.addEventListener('click', _resetForm);
    var refreshBtn = byId('ecomRefreshBtn');
    if (refreshBtn) refreshBtn.addEventListener('click', function() { _refreshJobStatus(true); });
    var drawerBtn = byId('ecomToggleTaskDrawerBtn');
    if (drawerBtn) drawerBtn.addEventListener('click', function() { _setTaskDrawerOpen(); });
    var publishBtn = byId('ecomPublishToShopBtn');
    if (publishBtn) publishBtn.addEventListener('click', _publishToShop);
    var mainAssetInput = byId('ecomMainAssetIdInput');
    if (mainAssetInput) {
      mainAssetInput.addEventListener('change', function() {
        var aid = (mainAssetInput.value || '').trim();
        if (!aid) {
          state.mainAsset = null;
          _renderMainAsset();
          return;
        }
        _fetchAssetById(aid, function(err, row) {
          if (err || !row) {
            _setMsg('未找到该 asset_id 对应的素材，请确认后重试。', true);
            return;
          }
          row.kind = 'main';
          state.mainAsset = row;
          _renderMainAsset();
          _setMsg('已载入现有素材，可直接开始生成。', false);
        });
      });
    }
  }

  function _renderResultTabs(galleryByTab) {
    var wrap = byId('ecomResultTabs');
    if (!wrap) return;
    var availableKeys = RESULT_ORDER.filter(function(key) {
      return galleryByTab[key] && galleryByTab[key].length;
    });
    if (!RESULT_ORDER.some(function(key) { return key === state.activeResultTab; })) {
      state.activeResultTab = availableKeys[0] || 'main_images';
    } else if ((!galleryByTab[state.activeResultTab] || !galleryByTab[state.activeResultTab].length) && availableKeys.length) {
      state.activeResultTab = availableKeys[0];
    }
    wrap.innerHTML = RESULT_ORDER.map(function(key) {
      var count = galleryByTab[key] && galleryByTab[key].length ? galleryByTab[key].length : 0;
      var cls = 'ecom-result-tab' + (state.activeResultTab === key ? ' active' : '') + (count ? '' : ' is-empty');
      return '<button type="button" class="' + cls + '" data-result-tab="' + escapeAttr(key) + '">' + escapeHtml(_resultFolderLabel(key)) + ' · ' + count + '</button>';
    }).join('');
    wrap.querySelectorAll('.ecom-result-tab').forEach(function(btn) {
      btn.addEventListener('click', function() {
        state.activeResultTab = btn.getAttribute('data-result-tab') || 'main_images';
        _renderGallery(state.lastGalleryByTab);
      });
    });
  }

  function _renderFocusedPreview(rows) {
    return;
  }

  function _renderGallery(galleryByTab) {
    state.lastGalleryByTab = galleryByTab || {};
    _renderResultTabs(state.lastGalleryByTab);
    var el = byId('ecomGallery');
    if (!el) return;
    el.classList.add('ecom-gallery-results');
    var rows = state.lastGalleryByTab[state.activeResultTab] || [];
    if (!rows.length) {
      el.innerHTML = '<div class="ecom-empty" style="grid-column:1 / -1;">当前目录暂时还没有图片结果。</div>';
      return;
    }
    el.innerHTML = rows.map(function(item) {
      var openUrl = (item.open_url || item.preview_url || '').trim();
      var metaParts = [];
      var openTag = openUrl
        ? '<a class="ecom-gallery-item link-card" href="' + escapeAttr(openUrl) + '" target="_blank" rel="noopener">'
        : '<div class="ecom-gallery-item link-card is-static">';
      var closeTag = openUrl ? '</a>' : '</div>';
      if (item.meta) metaParts.push(item.meta);
      if (item.width && item.height) metaParts.push(String(item.width) + ' x ' + String(item.height));
      return (
        openTag +
          '<div class="ecom-gallery-thumb">' +
            (item.preview_url
              ? '<img src="' + escapeAttr(item.preview_url) + '" alt="' + escapeAttr(item.title || item.filename || '') + '">'
              : '<div class="ecom-empty" style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;padding:0.5rem;">暂无预览</div>') +
          '</div>' +
          '<div class="ecom-gallery-body">' +
            '<div class="title">' + escapeHtml(item.title || item.filename || '未命名图片') + '</div>' +
            '<div class="meta">' + escapeHtml(metaParts.join(' · ') || '点击查看原图') + '</div>' +
          '</div>' +
        closeTag
      );
    }).join('');
  }

  function _renderFacts(resp) {
    var wrap = byId('ecomRunFacts');
    if (!wrap) return;
    var result = resp && resp.result ? resp.result : {};
    var config = result && result.config ? result.config : {};
    var progress = resp && resp.progress ? resp.progress : {};
    var usage = progress && progress.usage_summary ? progress.usage_summary : {};
    var billing = result && result.billing_summary ? result.billing_summary : {};
    var facts = [];
    if (state.currentJobId) facts.push('任务 ID：' + state.currentJobId);
    if (config.analysis_model) facts.push('分析模型：' + config.analysis_model);
    if (config.image_model) facts.push('生图模型：' + config.image_model);
    if (config.page_count) facts.push('详情页数：' + config.page_count);
    if (usage.image_count != null) facts.push('生图成功次数：' + usage.image_count);
    if (usage.analysis_count != null) facts.push('分析调用次数：' + usage.analysis_count);
    if (billing.total_points != null) {
      var amount = billing.total_cost_cny != null ? ('（约 ¥' + Number(billing.total_cost_cny).toFixed(2) + '）') : '';
      facts.push('积分消耗：' + billing.total_points + ' 积分' + amount);
    } else if (usage.total_points != null) {
      facts.push('积分消耗：' + usage.total_points + ' 积分');
    }
    if (billing.image_points_per_success != null || billing.analysis_points_per_call != null) {
      facts.push(
        '计费规则：生图成功 ' + String(billing.image_points_per_success != null ? billing.image_points_per_success : 40) +
        ' 积分/次，分析 ' + String(billing.analysis_points_per_call != null ? billing.analysis_points_per_call : 10) + ' 积分/次'
      );
    }
    if (result && result.suite_bundle && result.suite_bundle.root_relative_path) {
      facts.push('输出目录：' + result.suite_bundle.root_relative_path);
    }
    facts = facts.concat(_collectProgressFacts(resp));
    if (!facts.length) {
      wrap.innerHTML = '<div class="ecom-empty">生成完成后，这里会显示模型、页数、积分消耗和输出目录摘要。</div>';
      return;
    }
    wrap.innerHTML = facts.map(function(item) {
      return '<div class="ecom-activity-item"><div>' + escapeHtml(item) + '</div></div>';
    }).join('');
  }

  function initEcommerceDetailStudioView() {
    if (!state.initialized) {
      _bindUploader('ecomUploadMainBtn', 'ecomMainFileInput', 'main', false);
      _bindUploader('ecomUploadProductRefsBtn', 'ecomProductRefsFileInput', 'product_ref', true);
      _bindUploader('ecomUploadStyleRefsBtn', 'ecomStyleRefsFileInput', 'style_ref', true);
      _bindWorkspaceTabs();
      _bindFormWatchers();
      _bindActions();
      _restoreRecentJobs();
      state.initialized = true;
    }
    _renderMainAsset();
    _renderReferenceAssets();
    var activeRecord = state.currentJobId ? _findRecentJob(state.currentJobId) : null;
    _renderRequestedOutputs(activeRecord && activeRecord.requestedOutputs ? activeRecord.requestedOutputs : null);
    _renderStatus(state.latestResponse || (activeRecord && activeRecord.latestResponse) || null);
  }

  window.initEcommerceDetailStudioView = initEcommerceDetailStudioView;
})();
