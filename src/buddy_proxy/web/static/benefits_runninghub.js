// RunningHub 生图/生视频账号与额度管理面板（复用原生 chart-card / pat-head / pat-quota-grid 样式）

(function () {
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function renderBillingTag(billingType, closedSource) {
    const bType = billingType || '未知';
    const isClosed = !!closedSource || bType.indexOf('闭源') >= 0 || bType === '钱包余额';
    const isCoin = bType.indexOf('过渡币') >= 0 || bType === 'RH 金币';
    const bCls = isClosed ? 'tag-money' : (isCoin ? 'tag-coins' : 'tag-mixed');
    const tip = isClosed ? '含闭源模型节点（Seedance/Kling/MAGI…），站内也只扣钱包 $，过渡币跑不了'
      : (isCoin ? '开源节点跑在 RH 自家 GPU，站内过渡币可付（网页端登录态）' : '计费方式未知，运行一次后自动学');
    return `<span class="tag ${bCls}" data-tip="${escapeHtml(tip)}">${escapeHtml(bType)}</span>`;
  }

  async function rhFetch(path, opts) {
    const resp = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {}));
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const detail = data && data.detail;
      const msg = (detail && detail.error && detail.error.message) || data.message || ('HTTP ' + resp.status);
      const err = new Error(msg);
      // 把整个 detail 挂上：有些接口（workflows/run）出错时会顺带返回最新列表，
      // 调用方据此刷新 UI，不用再多发一次请求。
      err.detail = detail;
      throw err;
    }
    return data;
  }

  function openRenameModal(title, currentName, onConfirm, extraHtml) {
    const titleEl = document.getElementById('modal-title');
    const bodyEl = document.getElementById('modal-body');
    const footEl = document.getElementById('modal-foot');
    const overlayEl = document.getElementById('overlay');
    if (!titleEl || !bodyEl || !footEl || !overlayEl) return;

    titleEl.textContent = title;
    bodyEl.innerHTML = `
      <div style="display:flex;flex-direction:column;gap:8px;">
        <input type="text" id="rh-rename-input" value="${escapeHtml(currentName || '')}" placeholder="输入新名称" />
        ${extraHtml || ''}
        <div id="rh-rename-err" class="muted" style="font-size:12px;color:#ef4444;"></div>
      </div>
    `;
    footEl.innerHTML = `
      <button class="ghost" onclick="closeModal()">取消</button>
      <button id="rh-rename-save" class="primary">保存</button>
    `;
    overlayEl.classList.add('show');

    const inputEl = document.getElementById('rh-rename-input');
    const saveBtn = document.getElementById('rh-rename-save');
    if (inputEl) {
      inputEl.focus();
      inputEl.select();
      inputEl.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && saveBtn) saveBtn.click();
      });
    }
    if (saveBtn) {
      saveBtn.addEventListener('click', async () => {
        const val = (inputEl?.value || '').trim();
        const errEl = document.getElementById('rh-rename-err');
        if (!val) {
          if (errEl) errEl.textContent = '名称不能为空';
          return;
        }
        saveBtn.disabled = true;
        saveBtn.textContent = '保存中…';
        try {
          await onConfirm(val);
          if (typeof closeModal === 'function') closeModal();
          else overlayEl.classList.remove('show');
        } catch (err) {
          if (errEl) errEl.textContent = err.message || String(err);
        } finally {
          saveBtn.disabled = false;
          saveBtn.textContent = '保存';
        }
      });
    }
  }

  function renderAccounts(panel, accounts) {
    const bodyEl = panel.querySelector('#rh-accounts-body');
    if (!bodyEl) return;
    if (!accounts || !accounts.length) {
      bodyEl.innerHTML = '<div class="empty" style="padding:18px 0">暂无 RunningHub 账号，点击右上角「导入账号」添加 API Token</div>';
      return;
    }

    const cards = accounts.map((acc, idx) => {
      const info = acc.account_info || {};
      // 邮箱账号（站内模式）account_info 来自 /uc/point/summary + getUserInfo：
      //   transitionCoins 过渡币（只有网页端登录态能花）/ activityPoints 活动积分 / walletBalance 钱包 $
      // 纯 key 账号（OpenAPI 模式）还是老的 remainCoins / remainMoney / remainPoint。
      const isWeb = !!(acc.email && acc.web_session_status && acc.web_session_status.logged_in);
      const coins = isWeb ? (info.transitionCoins ?? '—') : (info.remainCoins ?? '—');
      const point = isWeb ? (info.activityPoints ?? info.paidPoints ?? '—') : (info.remainPoint ?? '—');
      const money = isWeb ? (info.walletBalance ?? '—') : (info.remainMoney ?? '—');
      const ss = acc.web_session_status || {};
      const tasks = info.currentTaskCounts ?? '0';
      const apiTypeRaw = String(info.apiType || (isWeb ? 'WEB' : 'NORMAL')).toUpperCase();
      const apiTypeLabel = isWeb ? '站内登录态' : (apiTypeRaw === 'SHARED' ? '共享 API' : (apiTypeRaw === 'EXCLUSIVE' ? '独享 API' : apiTypeRaw));

      const upBtn = idx > 0
        ? `<button class="ghost" data-tip="上移" data-rh-action="up" data-id="${escapeHtml(acc.account_id)}">↑</button>`
        : '';

      return `
        <div class="pat-pkg" style="padding:14px 16px;">
          <div style="display:flex;align-items:center;gap:8px;min-width:0;border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:10px;">
            <span class="pat-pkg-name" style="font-size:14px;font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(acc.masked_email || '')}">${escapeHtml(acc.label || acc.masked_email || acc.account_id)}</span>
            <span class="tag ${isWeb ? 'tag-coins' : ''}" style="flex-shrink:0;" data-tip="${isWeb ? '网页端登录态（Bearer JWT）：能花站内过渡币' : '只有 api_key，走 OpenAPI 要钱包余额 ($)'}">${escapeHtml(apiTypeLabel)}</span>
            <span style="flex-shrink:0;display:inline-flex;gap:4px;">
              <button class="ghost" title="刷新额度" data-rh-action="refresh" data-id="${escapeHtml(acc.account_id)}">↻</button>
              ${acc.email ? `<button class="ghost" title="重新登录（网页改过密码 / 登录态坏了）" data-rh-action="relogin" data-id="${escapeHtml(acc.account_id)}">🔑</button>` : ''}
              <button class="ghost" title="重命名" data-rh-action="rename" data-id="${escapeHtml(acc.account_id)}">✎</button>
              ${upBtn}
              <button class="ghost" title="删除" data-rh-action="delete" data-id="${escapeHtml(acc.account_id)}">✕</button>
            </span>
          </div>
          <div class="muted mono" style="font-size:11px;margin:0 0 8px">${escapeHtml(acc.masked_email || acc.masked_key || '')}</div>
          <div style="display:flex;gap:14px;font-size:12px;flex-wrap:wrap;align-items:center">
            <span data-tip="${isWeb ? '过渡币：只有网页端登录态能花，API 通道不认' : '站内 RH 币：网页端可用，API 通道不认'}">${isWeb ? '过渡币' : '站内币'} <strong class="mono">${escapeHtml(String(coins))}</strong></span>
            <span data-tip="${isWeb ? '活动 / 付费积分（站内）' : 'API 调用可用额度（remainPoint）'}">${isWeb ? '积分' : 'API 额度'} <strong class="mono">${escapeHtml(String(point))}</strong></span>
            <span data-tip="钱包余额（USD）：闭源模型节点 / OpenAPI 只认这个">钱包 <strong class="mono">${escapeHtml(String(money))}</strong></span>
            ${ss.logged_in
              ? `<span data-tip="${escapeHtml(ss.last_error || '登录态正常')}" style="${ss.last_error ? 'color:#ef4444' : ''}">登录态 <strong class="mono">${ss.expires_in_days != null ? ss.expires_in_days + 'd' : 'ok'}</strong></span>`
              : (acc.email ? '<span style="color:#f59e0b">未登录</span>' : '')}
            <span>运行中 <strong class="mono">${escapeHtml(String(tasks))}</strong></span>
          </div>
        </div>`;
    }).join('');

    bodyEl.innerHTML = `<div class="pat-quota-grid">${cards}</div>`;

    bodyEl.querySelectorAll('button[data-rh-action]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const action = btn.getAttribute('data-rh-action');
        const id = btn.getAttribute('data-id');
        try {
          if (action === 'refresh') {
            const res = await rhFetch('/ui/api/runninghub/accounts/refresh', { method: 'POST', body: JSON.stringify({ account_id: id }) });
            renderAccounts(panel, res.accounts);
          } else if (action === 'relogin') {
            const res = await rhFetch('/ui/api/runninghub/accounts/relogin', { method: 'POST', body: JSON.stringify({ account_id: id }) });
            renderAccounts(panel, res.accounts);
          } else if (action === 'rename') {
            const acc = accounts.find((a) => a.account_id === id);
            openRenameModal('重命名账号', acc ? (acc.label || acc.account_id) : '', async (label) => {
              const res = await rhFetch('/ui/api/runninghub/accounts/rename', {
                method: 'POST',
                body: JSON.stringify({ account_id: id, label }),
              });
              renderAccounts(panel, res.accounts);
            });
          } else if (action === 'delete') {
            if (!window.confirm('确认删除该 RunningHub 账号？')) return;
            const res = await rhFetch('/ui/api/runninghub/accounts/delete', { method: 'POST', body: JSON.stringify({ account_id: id }) });
            renderAccounts(panel, res.accounts);
          } else if (action === 'up') {
            const ids = accounts.map((a) => a.account_id);
            const i = ids.indexOf(id);
            if (i > 0) {
              [ids[i - 1], ids[i]] = [ids[i], ids[i - 1]];
              const res = await rhFetch('/ui/api/runninghub/accounts/order', { method: 'POST', body: JSON.stringify({ order: ids }) });
              renderAccounts(panel, res.accounts);
            }
          }
        } catch (err) {
          window.alert(err.message || String(err));
        }
      });
    });
  }

  function openImportModal(panel) {
    const html = `
      <p style="margin:0 0 8px;font-size:12px;line-height:1.5">填 <strong>runninghub.ai 的登录邮箱 + 密码</strong>。系统会登录拿登录态（Bearer JWT，30 天，自动续期），
      运行走网页端同一条路，<strong>花的是站内过渡币 / 积分</strong>。密码 0600 落盘，不进日志。<br/>
      <span class="muted">只填 API Key 也行，但 OpenAPI 只认钱包余额（$），过渡币跑不了。</span></p>
      <div style="display:flex;flex-direction:column;gap:8px;margin-bottom:8px">
        <input type="text" id="rh-modal-label" placeholder="备注名（可选，留空用邮箱前缀）" />
        <input type="email" id="rh-modal-email" class="mono" placeholder="登录邮箱（runninghub.ai）" />
        <input type="password" id="rh-modal-password" class="mono" placeholder="密码" />
        <input type="password" id="rh-modal-token" class="mono" placeholder="API Key（可选，仅上传文件用）" />
      </div>
      <div id="rh-modal-err" class="muted" style="font-size:12px;color:#ef4444"></div>
    `;
    const foot = `
      <button class="ghost" onclick="closeModal()">取消</button>
      <button id="rh-modal-submit">验证并导入</button>
    `;
    const titleEl = document.getElementById('modal-title');
    const bodyEl = document.getElementById('modal-body');
    const footEl = document.getElementById('modal-foot');
    const overlayEl = document.getElementById('overlay');
    if (!titleEl || !bodyEl || !footEl || !overlayEl) return;

    titleEl.textContent = '导入 RunningHub 账号';
    bodyEl.innerHTML = html;
    footEl.innerHTML = foot;
    overlayEl.classList.add('show');

    const submitBtn = document.getElementById('rh-modal-submit');
    if (submitBtn) {
      submitBtn.addEventListener('click', async () => {
        const token = (document.getElementById('rh-modal-token')?.value || '').trim();
        const email = (document.getElementById('rh-modal-email')?.value || '').trim();
        const password = document.getElementById('rh-modal-password')?.value || '';
        const label = (document.getElementById('rh-modal-label')?.value || '').trim();
        const errEl = document.getElementById('rh-modal-err');
        if (!email && !token) {
          if (errEl) errEl.textContent = '请输入登录邮箱 + 密码（或至少 API Key）';
          return;
        }
        if (email && !password) {
          if (errEl) errEl.textContent = '邮箱账号必须填密码';
          return;
        }
        submitBtn.disabled = true;
        submitBtn.textContent = '登录中…';
        try {
          const res = await rhFetch('/ui/api/runninghub/accounts/import', {
            method: 'POST',
            body: JSON.stringify({ email: email || undefined, password: password || undefined, api_key: token || undefined, label: label || undefined }),
          });
          if (typeof closeModal === 'function') closeModal();
          else overlayEl.classList.remove('show');
          renderAccounts(panel, res.accounts);
        } catch (err) {
          if (errEl) errEl.textContent = err.message || String(err);
        } finally {
          submitBtn.disabled = false;
          submitBtn.textContent = '验证并导入';
        }
      });
    }
  }

  window.renderRunningHubPanel = async function renderRunningHubPanel() {
    const panel = document.getElementById('runninghub-page-box');
    if (!panel) return;
    panel.innerHTML = `
      <div class="chart-card" style="margin-top:14px">
        <div class="pat-head">
          <span class="name">任务队列</span>
          <span class="tag" id="rh-queue-count">0</span>
          <span class="pat-head-mid">
            <span class="muted" style="font-size:11px">提交后在后台跑，关掉页面也继续；点击查看参数 / 结果 / 成本</span>
          </span>
          <span class="pat-head-actions">
            <button class="ghost" id="rh-queue-refresh">刷新</button>
            <button class="ghost" id="rh-queue-clear">清空已完成</button>
          </span>
        </div>
        <div id="rh-queue-body"><div class="empty" style="padding:18px 0">暂无任务</div></div>
      </div>

      <div class="chart-card" style="margin-top:14px">
        <div class="pat-head">
          <span class="name">工作流 / AI 应用</span>
          <span class="tag">ai-app</span>
          <span class="pat-head-mid">
            <span class="muted" style="font-size:11px">输入 webappId 自动拉取参数格式，点击「运行」可直接填参调用</span>
          </span>
          <span class="pat-head-actions">
            <button class="ghost" id="rh-wf-import-btn">导入工作流</button>
          </span>
        </div>
        <div id="rh-workflows-body"><div class="empty" style="padding:18px 0">加载中…</div></div>
      </div>

      <div class="chart-card" style="margin-top:14px">
        <div class="pat-head">
          <span class="name">RunningHub 账号</span>
          <span class="pat-head-mid"></span>
          <span class="pat-head-actions">
            <button class="ghost" id="rh-refresh-all-btn">全部刷新</button>
            <button class="ghost" id="rh-import-btn">导入账号</button>
          </span>
        </div>
        <div id="rh-accounts-body"><div class="empty" style="padding:18px 0">加载中…</div></div>
      </div>
    `;

    panel.querySelector('#rh-wf-import-btn').addEventListener('click', () => openImportWorkflowModal(panel));
    panel.querySelector('#rh-queue-refresh').addEventListener('click', () => renderTaskQueue(panel));
    panel.querySelector('#rh-queue-clear').addEventListener('click', async () => {
      if (!window.confirm('清掉所有已完成 / 失败 / 取消的任务记录？（不影响站内产出文件）')) return;
      try {
        const res = await rhFetch('/ui/api/runninghub/tasks', {});
        const done = (res.tasks || []).filter((t) => !isTaskActive(t.status));
        for (const t of done) await rhFetch('/ui/api/runninghub/tasks/delete', { method: 'POST', body: JSON.stringify({ task_id: t.taskId }) });
        renderTaskQueue(panel);
      } catch (err) { window.alert(err.message || String(err)); }
    });
    panel.querySelector('#rh-import-btn').addEventListener('click', () => openImportModal(panel));
    panel.querySelector('#rh-refresh-all-btn').addEventListener('click', async () => {
      try {
        const res = await rhFetch('/ui/api/runninghub/accounts/refresh', { method: 'POST', body: '{}' });
        renderAccounts(panel, res.accounts);
      } catch (err) {
        window.alert(err.message || String(err));
      }
    });

    try {
      const [accRes, wfRes] = await Promise.all([
        rhFetch('/ui/api/runninghub/accounts'),
        rhFetch('/ui/api/runninghub/workflows'),
      ]);
      renderAccounts(panel, accRes.accounts || []);
      renderWorkflows(panel, wfRes.workflows || []);
      renderTaskQueue(panel);
    } catch (err) {
      panel.querySelector('#rh-accounts-body').innerHTML = `<div class="empty" style="padding:18px 0;color:#ef4444">加载失败: ${escapeHtml(err.message)}</div>`;
    }
  };

  function sanitizeHtml(rawHtml) {
    if (!rawHtml) return '';
    const doc = new DOMParser().parseFromString(String(rawHtml), 'text/html');
    const allowedTags = new Set(['P', 'BR', 'A', 'STRONG', 'B', 'EM', 'I', 'UL', 'OL', 'LI', 'CODE']);

    function cleanNode(node) {
      if (node.nodeType === Node.TEXT_NODE) {
        return document.createTextNode(node.textContent || '');
      }
      if (node.nodeType !== Node.ELEMENT_NODE) {
        return document.createDocumentFragment();
      }
      if (!allowedTags.has(node.tagName)) {
        const frag = document.createDocumentFragment();
        node.childNodes.forEach((child) => frag.appendChild(cleanNode(child)));
        return frag;
      }
      const el = document.createElement(node.tagName.toLowerCase());
      if (node.tagName === 'A') {
        const href = (node.getAttribute('href') || '').trim();
        if (/^https?:\/\//i.test(href)) {
          el.setAttribute('href', href);
          el.setAttribute('target', '_blank');
          el.setAttribute('rel', 'noopener noreferrer');
        }
      }
      node.childNodes.forEach((child) => el.appendChild(cleanNode(child)));
      return el;
    }

    const container = document.createElement('div');
    doc.body.childNodes.forEach((child) => container.appendChild(cleanNode(child)));
    return container.innerHTML;
  }

  // ---- 任务队列 ----
  const TASK_ACTIVE = new Set(['SUBMITTING', 'RUNNING', 'QUEUED']);
  function isTaskActive(s) { return TASK_ACTIVE.has(String(s || '').toUpperCase()); }
  function statusLabel(s) {
    return ({ SUBMITTING: '提交中', RUNNING: '生成中', QUEUED: '排队中', SUCCESS: '已完成',
      FAILED: '失败', TIMEOUT: '超时', CANCELLED: '已取消', INTERRUPTED: '已中断' })[String(s || '').toUpperCase()] || String(s || '?');
  }
  function statusColor(s) {
    s = String(s || '').toUpperCase();
    if (s === 'SUCCESS') return '#10b981';
    if (s === 'FAILED' || s === 'TIMEOUT') return '#ef4444';
    if (s === 'CANCELLED') return '#9ca3af';
    if (s === 'INTERRUPTED') return '#f59e0b';
    return '#f59e0b';
  }
  function renderTaskOutputs(el, t) {
    if (!el) return;
    const outs = Array.isArray(t.outputs) ? t.outputs : [];
    el.innerHTML = outs.length
      ? outs.map((o) => o.type === 'video'
          ? `<div style="margin-top:6px;"><video src="${escapeHtml(o.url)}" controls style="max-width:100%;border-radius:8px;"></video><div><a href="${escapeHtml(o.url)}" target="_blank">打开视频链接</a></div></div>`
          : o.type === 'audio'
            ? `<div style="margin-top:6px;"><audio src="${escapeHtml(o.url)}" controls style="max-width:100%;"></audio><div><a href="${escapeHtml(o.url)}" target="_blank">打开音频链接</a></div></div>`
            : `<div style="margin-top:6px;"><img src="${escapeHtml(o.url)}" loading="lazy" style="max-width:100%;border-radius:8px;" /><div><a href="${escapeHtml(o.url)}" target="_blank">打开图片链接</a></div></div>`).join('')
      : '<div class="muted" style="margin-top:6px;">任务完成，但没有产出文件。</div>';
  }
  function fmtWhen(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
  }
  function fmtDuration(t) {
    if (t.status === 'SUCCESS' && t.costTime != null) return `${Math.round(t.costTime)}s`;
    const start = t.created_at || 0;
    const end = t.finishedAt || (isTaskActive(t.status) ? Date.now() / 1000 : start);
    const s = Math.max(0, Math.round(end - start));
    return s >= 60 ? `${Math.floor(s / 60)}m${s % 60}s` : `${s}s`;
  }
  let _queueTimer = null;
  async function refreshWorkflows(panel) {
    try {
      const wfRes = await rhFetch('/ui/api/runninghub/workflows');
      renderWorkflows(panel, wfRes.workflows || []);
      const accRes = await rhFetch('/ui/api/runninghub/accounts');
      renderAccounts(panel, accRes.accounts || []);
    } catch (e) { /* 面板可能已经切走 */ }
  }
  async function renderTaskQueue(panel) {
    const bodyEl = panel && panel.querySelector('#rh-queue-body');
    if (!bodyEl) return;
    let tasks = [];
    try {
      const res = await rhFetch('/ui/api/runninghub/tasks?limit=100');
      tasks = res.tasks || [];
    } catch (err) {
      bodyEl.innerHTML = `<div class="empty" style="padding:18px 0;color:#ef4444">加载失败: ${escapeHtml(err.message || err)}</div>`;
      return;
    }
    const cnt = panel.querySelector('#rh-queue-count');
    if (cnt) cnt.textContent = String(tasks.filter((t) => isTaskActive(t.status)).length);
    if (!tasks.length) {
      bodyEl.innerHTML = '<div class="empty" style="padding:18px 0">暂无任务。在工作流卡片点「运行」提交一个。</div>';
      return;
    }
    bodyEl.innerHTML = `<div class="rh-queue">${tasks.map((t) => {
      const active = isTaskActive(t.status);
      const thumb = (t.outputs && t.outputs[0]) || null;
      const thumbHtml = thumb
        ? (thumb.type === 'video'
            ? `<video src="${escapeHtml(thumb.url)}" muted preload="metadata" style="width:56px;height:56px;object-fit:cover;border-radius:6px;background:#000"></video>`
            : `<img src="${escapeHtml(thumb.url)}" loading="lazy" style="width:56px;height:56px;object-fit:cover;border-radius:6px;background:#111" />`)
        : `<div style="width:56px;height:56px;border-radius:6px;background:var(--bg2,#1a1a1a);display:flex;align-items:center;justify-content:center;font-size:20px;color:${statusColor(t.status)}">${t.status === 'SUCCESS' ? '✓' : (active ? '⏳' : '✕')}</div>`;
      const cost = t.coinsSpent != null ? `<span class="muted">实扣 <strong class="mono">${escapeHtml(String(t.coinsSpent))}</strong> 币</span>` : '';
      return `
        <div class="rh-queue-row" data-task="${escapeHtml(t.taskId)}" role="button" tabindex="0"
             style="display:flex;align-items:center;gap:12px;padding:10px 12px;border-bottom:1px solid var(--line);cursor:pointer;">
          ${thumbHtml}
          <div style="flex:1;min-width:0;">
            <div style="display:flex;align-items:center;gap:8px;min-width:0;">
              <span class="mono" style="font-size:11px;color:${statusColor(t.status)};flex-shrink:0;">● ${escapeHtml(statusLabel(t.status))}</span>
              <span style="font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(t.webappName || t.webappId)}</span>
            </div>
            <div class="muted" style="font-size:11px;margin-top:3px;display:flex;gap:10px;flex-wrap:wrap;">
              <span class="mono">${escapeHtml(fmtWhen(t.created_at))}</span>
              <span>${escapeHtml(fmtDuration(t))}</span>
              ${cost}
              ${active && t.cancelRequested ? '<span style="color:#f59e0b">取消中…</span>' : ''}
              ${t.status === 'CANCELLED' ? `<span class="muted">${t.submitted ? '已提交到站内，只是不再等待' : '提交前取消，未扣币'}</span>` : ''}
              ${t.status === 'INTERRUPTED' ? '<span style="color:#f59e0b">服务重启丢了跟踪，去站内「任务历史」看结果</span>' : ''}
              ${t.error ? `<span style="color:#ef4444;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:420px;" title="${escapeHtml(t.error)}">${escapeHtml(t.error)}</span>` : ''}
            </div>
          </div>
          ${active ? `<button class="ghost rh-queue-cancel" data-id="${escapeHtml(t.taskId)}" title="不再等待（站内任务照跑，已扣币不退）">取消</button>` : ''}
        </div>`;
    }).join('')}</div>`;

    bodyEl.querySelectorAll('.rh-queue-row').forEach((row) => {
      row.addEventListener('click', () => openTaskDetail(panel, row.getAttribute('data-task'), tasks));
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter') openTaskDetail(panel, row.getAttribute('data-task'), tasks); });
    });
    bodyEl.querySelectorAll('.rh-queue-cancel').forEach((btn) => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!window.confirm('不再等待这个任务？（站内会继续跑完，已扣的过渡币不会退）')) return;
        try {
          await rhFetch('/ui/api/runninghub/tasks/cancel', { method: 'POST', body: JSON.stringify({ task_id: btn.getAttribute('data-id') }) });
          renderTaskQueue(panel);
        } catch (err) { window.alert(err.message || String(err)); }
      });
    });
    // 有进行中的任务就 4s 一刷；没有就停掉定时器
    if (_queueTimer) { clearTimeout(_queueTimer); _queueTimer = null; }
    if (tasks.some((t) => isTaskActive(t.status))) {
      _queueTimer = setTimeout(() => { if (panel.isConnected) renderTaskQueue(panel); }, 4000);
    }
  }
  function openTaskDetail(panel, taskId, tasks) {
    const t = tasks.find((x) => String(x.taskId) === String(taskId));
    if (!t) return;
    const titleEl = document.getElementById('modal-title');
    const bodyEl = document.getElementById('modal-body');
    const footEl = document.getElementById('modal-foot');
    const overlayEl = document.getElementById('overlay');
    if (!titleEl || !bodyEl || !footEl || !overlayEl) return;
    const inputs = Array.isArray(t.inputs) ? t.inputs : [];
    const est = t.estimate || {};
    titleEl.textContent = `${t.webappName || t.webappId} · ${statusLabel(t.status)}`;
    bodyEl.innerHTML = `
      <div style="display:flex;flex-direction:column;gap:12px;">
        <div class="muted" style="font-size:12px;display:flex;gap:14px;flex-wrap:wrap;">
          <span>本地 ID <span class="mono">${escapeHtml(t.taskId)}</span></span>
          ${t.upstreamTaskId ? `<span>站内 ID <span class="mono">${escapeHtml(t.upstreamTaskId)}</span></span>` : ''}
          <span>提交 <span class="mono">${escapeHtml(fmtWhen(t.created_at))}</span></span>
          <span>用时 <span class="mono">${escapeHtml(fmtDuration(t))}</span></span>
          ${t.coinsSpent != null ? `<span>实扣 <strong class="mono">${escapeHtml(String(t.coinsSpent))}</strong> 过渡币</span>` : ''}
          ${est.priceText || est.estimatedPoint != null ? `<span>预估 ${escapeHtml(est.priceText || '—')} / ${escapeHtml(String(est.estimatedPoint ?? '—'))} 积分</span>` : ''}
          ${t.closedSource ? '<span class="tag tag-money">闭源 · 扣钱包</span>' : ''}
        </div>
        ${t.status === 'CANCELLED' ? `<div class="muted" style="font-size:12px">${t.submitted ? '站内任务已提交，只是我们不再等待；已扣的过渡币不会退。' : '提交前取消，没有扣币。'}</div>` : ''}
        ${t.status === 'INTERRUPTED' ? '<div style="color:#f59e0b;font-size:12px">服务重启导致丢失跟踪。站内任务可能已经跑完，去站内「任务历史」查看。</div>' : ''}
        ${t.error ? `<div style="color:#ef4444;font-size:12px;white-space:pre-wrap;word-break:break-word;">${escapeHtml(t.error)}</div>` : ''}
        <details ${inputs.length && !(t.outputs && t.outputs.length) ? 'open' : ''}>
          <summary class="muted" style="cursor:pointer;font-size:12px;">输入参数（${inputs.length}）</summary>
          <div style="margin-top:8px;display:flex;flex-direction:column;gap:6px;">
            ${inputs.map((i) => `<div style="display:grid;grid-template-columns:110px 1fr;gap:8px;font-size:12px;"><span class="muted mono">${escapeHtml(i.fieldName || i.nodeId)}</span><span class="mono" style="word-break:break-all;white-space:pre-wrap;">${escapeHtml(String(i.fieldValue ?? ''))}</span></div>`).join('') || '<div class="muted">无</div>'}
          </div>
        </details>
        <div id="rh-task-detail-outputs">${t.status === 'SUCCESS' ? '' : '<div class="muted">尚无产出。</div>'}</div>
      </div>`;
    footEl.innerHTML = `
      <button class="ghost" onclick="closeModal()">关闭</button>
      ${isTaskActive(t.status) ? `<button class="ghost" id="rh-task-detail-cancel">不再等待</button>` : ''}
      <button class="ghost" id="rh-task-detail-del">删除记录</button>`;
    overlayEl.classList.add('show');
    renderTaskOutputs(document.getElementById('rh-task-detail-outputs'), t);
    const delBtn = document.getElementById('rh-task-detail-del');
    if (delBtn) delBtn.addEventListener('click', async () => {
      if (!window.confirm('删除这条记录？（不影响站内产出文件）')) return;
      try {
        await rhFetch('/ui/api/runninghub/tasks/delete', { method: 'POST', body: JSON.stringify({ task_id: t.taskId }) });
        if (typeof closeModal === 'function') closeModal(); else overlayEl.classList.remove('show');
        renderTaskQueue(panel);
      } catch (err) { window.alert(err.message || String(err)); }
    });
    const cBtn = document.getElementById('rh-task-detail-cancel');
    if (cBtn) cBtn.addEventListener('click', async () => {
      try {
        await rhFetch('/ui/api/runninghub/tasks/cancel', { method: 'POST', body: JSON.stringify({ task_id: t.taskId }) });
        if (typeof closeModal === 'function') closeModal(); else overlayEl.classList.remove('show');
        renderTaskQueue(panel);
      } catch (err) { window.alert(err.message || String(err)); }
    });
  }

  function renderWorkflows(panel, workflows) {
    const bodyEl = panel.querySelector('#rh-workflows-body');
    if (!bodyEl) return;
    if (!workflows || !workflows.length) {
      bodyEl.innerHTML = '<div class="empty" style="padding:18px 0">暂无工作流，点击右上角「导入工作流」添加 webappId</div>';
      return;
    }

    const cards = workflows.map((wf) => {
      const nodes = Array.isArray(wf.nodeInfoList) ? wf.nodeInfoList : [];
      const safeDesc = sanitizeHtml(wf.description);
      const billingTag = `<span style="float:right;margin:0 0 6px 10px;display:inline-flex;gap:4px;">${renderBillingTag(wf.billingType, wf.closedSource)}${wf.verifiedCost ? `<span class="tag tag-coins" data-tip="${escapeHtml(wf.verifiedNote || '实测成本')}">实测 ${escapeHtml(wf.verifiedCost)}</span>` : ''}</span>`;
      const desc = safeDesc
        ? `<div class="muted rh-wf-desc" style="font-size:12px;line-height:1.55;margin:0;word-break:break-word;">${billingTag}${safeDesc}</div>`
        : `<div class="muted" style="font-size:12px;">${billingTag}暂无描述</div>`;
      return `
        <div class="pat-pkg" style="padding:14px 16px;">
          <div style="display:flex;align-items:center;gap:8px;min-width:0;border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:10px;">
            <span class="pat-pkg-name" style="font-size:14px;font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" data-tip="ai-app/${escapeHtml(wf.webappId)}">${escapeHtml(wf.name || wf.webappId)}</span>
            <span class="tag" style="flex-shrink:0;" data-tip="ai-app/${escapeHtml(wf.webappId)}">${nodes.length} 参数</span>
            <span style="flex-shrink:0;display:inline-flex;gap:4px;">
              <button class="ghost" data-tip="复制 ID: ${escapeHtml(wf.webappId)}" data-rh-wf-copy="${escapeHtml(wf.webappId)}">📋</button>
              <button class="ghost" data-tip="填写参数并运行" data-rh-wf-run="${escapeHtml(wf.webappId)}">▶ 运行</button>
              <button class="ghost" data-tip="重命名" data-rh-wf-rename="${escapeHtml(wf.webappId)}">✎</button>
              <button class="ghost" data-tip="删除" data-rh-wf-del="${escapeHtml(wf.webappId)}">✕</button>
            </span>
          </div>
          ${desc}
        </div>`;
    }).join('');

    bodyEl.innerHTML = `<div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;">${cards}</div>`;

    bodyEl.querySelectorAll('button[data-rh-wf-copy]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const wid = btn.getAttribute('data-rh-wf-copy');
        const text = String(wid || '');
        try {
          await navigator.clipboard.writeText(text);
          const old = btn.textContent;
          btn.textContent = '✓';
          setTimeout(() => { btn.textContent = old; }, 1200);
        } catch (_err) {
          window.prompt('请手动复制：', text);
        }
      });
    });

    bodyEl.querySelectorAll('button[data-rh-wf-run]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const wid = btn.getAttribute('data-rh-wf-run');
        const wf = workflows.find((w) => String(w.webappId) === String(wid));
        if (wf) openRunWorkflowModal(wf, panel);
      });
    });

    bodyEl.querySelectorAll('button[data-rh-wf-rename]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const wid = btn.getAttribute('data-rh-wf-rename');
        const wf = workflows.find((w) => String(w.webappId) === String(wid));
        const curBilling = (wf && wf.billingType) || 'RH 金币 / 余额';
        const billingSelect = `
          <select id="rh-wf-billing-select" style="font-size:12px;">
            <option value="RH 金币" ${curBilling === 'RH 金币' ? 'selected' : ''}>RH 金币</option>
            <option value="钱包余额" ${curBilling === '钱包余额' ? 'selected' : ''}>钱包余额</option>
            <option value="RH 金币 / 余额" ${curBilling === 'RH 金币 / 余额' ? 'selected' : ''}>RH 金币 / 余额</option>
          </select>
        `;
        openRenameModal('编辑工作流', wf ? (wf.name || wf.webappId) : '', async (name) => {
          const billing = document.getElementById('rh-wf-billing-select')?.value;
          const res = await rhFetch('/ui/api/runninghub/workflows/rename', {
            method: 'POST',
            body: JSON.stringify({ webapp_id: wid, name, billing_type: billing }),
          });
          renderWorkflows(panel, res.workflows || []);
        }, billingSelect);
      });
    });

    bodyEl.querySelectorAll('button[data-rh-wf-del]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const wid = btn.getAttribute('data-rh-wf-del');
        if (!window.confirm('确认删除该工作流？')) return;
        try {
          const res = await rhFetch('/ui/api/runninghub/workflows/delete', {
            method: 'POST',
            body: JSON.stringify({ webapp_id: wid }),
          });
          renderWorkflows(panel, res.workflows || []);
        } catch (err) {
          window.alert(err.message || String(err));
        }
      });
    });
  }

  function openImportWorkflowModal(panel) {
    const titleEl = document.getElementById('modal-title');
    const bodyEl = document.getElementById('modal-body');
    const footEl = document.getElementById('modal-foot');
    const overlayEl = document.getElementById('overlay');
    if (!titleEl || !bodyEl || !footEl || !overlayEl) return;

    titleEl.textContent = '导入 RunningHub 工作流 (ai-app)';
    bodyEl.innerHTML = `
      <p style="margin:0 0 8px">输入 RunningHub AI 应用的 <span class="mono">webappId</span>（URL 末尾数字），可先「拉取」测试可用性与查看详情，再决定是否「保存」。</p>
      <div style="display:flex;flex-direction:column;gap:8px;margin-bottom:8px">
        <input type="text" id="rh-wf-id" class="mono" placeholder="webappId，例如：2086711729156894722" />
        <input type="text" id="rh-wf-name" placeholder="自定义名称（可选，留空使用上游名称）" />
      </div>
      <div id="rh-wf-preview" style="display:none;margin:8px 0;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);font-size:12px;"></div>
      <div id="rh-wf-err" class="muted" style="font-size:12px;color:#ef4444"></div>
    `;
    footEl.innerHTML = `
      <div style="flex:1;display:flex;gap:8px;">
        <button class="ghost" id="rh-wf-fetch">🔍 拉取</button>
      </div>
      <button class="ghost" onclick="closeModal()">取消</button>
      <button id="rh-wf-submit" class="primary">保存</button>
    `;
    overlayEl.classList.add('show');

    // 打开弹窗后聚焦到 webappId 输入框，方便直接粘贴。
    requestAnimationFrame(() => {
      const idInput = document.getElementById('rh-wf-id');
      if (idInput) { idInput.focus(); idInput.select(); }
    });

    const fetchBtn = document.getElementById('rh-wf-fetch');
    const submitBtn = document.getElementById('rh-wf-submit');
    const errEl = document.getElementById('rh-wf-err');
    const previewEl = document.getElementById('rh-wf-preview');

    if (fetchBtn) {
      fetchBtn.addEventListener('click', async () => {
        const wid = (document.getElementById('rh-wf-id')?.value || '').trim();
        const name = (document.getElementById('rh-wf-name')?.value || '').trim();
        if (!wid) {
          if (errEl) errEl.textContent = '请输入 webappId';
          return;
        }
        if (errEl) errEl.textContent = '';
        fetchBtn.disabled = true;
        fetchBtn.textContent = '拉取中…';
        try {
          const res = await rhFetch('/ui/api/runninghub/workflows/import', {
            method: 'POST',
            body: JSON.stringify({ webapp_id: wid, name: name || undefined, preview: true }),
          });
          const wf = res.workflow || {};
          const nodes = Array.isArray(wf.nodeInfoList) ? wf.nodeInfoList : [];
          const nodeCount = nodes.length;
          const paramsHtml = nodes.length
            ? `<div style="margin-top:8px;">
                 <div class="muted" style="font-size:11px;margin-bottom:4px;">参数（${nodeCount}）：</div>
                 <div style="display:flex;flex-wrap:wrap;gap:4px;">
                   ${nodes.map(n => {
                     const label = n.description || n.fieldName || n.nodeName || 'param';
                     const ftype = String(n.fieldType || 'TEXT').toUpperCase();
                     const val = n.fieldValue !== undefined && n.fieldValue !== null
                       ? String(n.fieldValue)
                       : '';
                     const tip = val ? `data-tip="${escapeHtml(val)}"` : '';
                     return `<span class="tag" ${tip}>${escapeHtml(label)} <span class="muted" style="font-size:10px;">(${escapeHtml(ftype)})</span></span>`;
                   }).join('')}
                 </div>
               </div>`
            : '';
          if (previewEl) {
            previewEl.style.display = 'block';
            previewEl.innerHTML = `
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                <strong style="font-size:13px;">${escapeHtml(wf.name || wid)}</strong>
                ${renderBillingTag(wf.billingType, wf.closedSource)}
              </div>
              ${wf.description ? `<div class="rh-wf-desc" style="margin-top:4px;">${sanitizeHtml(wf.description)}</div>` : '<div class="muted" style="margin-top:4px;">暂无应用描述</div>'}
              ${paramsHtml}
            `;
          }
        } catch (err) {
          if (previewEl) previewEl.style.display = 'none';
          if (errEl) errEl.textContent = err.message || String(err);
        } finally {
          fetchBtn.disabled = false;
          fetchBtn.textContent = '🔍 拉取';
        }
      });
    }

    if (submitBtn) {
      submitBtn.addEventListener('click', async () => {
        const wid = (document.getElementById('rh-wf-id')?.value || '').trim();
        const name = (document.getElementById('rh-wf-name')?.value || '').trim();
        if (!wid) {
          if (errEl) errEl.textContent = '请输入 webappId';
          return;
        }
        if (errEl) errEl.textContent = '';
        submitBtn.disabled = true;
        submitBtn.textContent = '保存中…';
        try {
          const res = await rhFetch('/ui/api/runninghub/workflows/import', {
            method: 'POST',
            body: JSON.stringify({ webapp_id: wid, name: name || undefined }),
          });
          if (typeof closeModal === 'function') closeModal();
          else overlayEl.classList.remove('show');
          renderWorkflows(panel, res.workflows || []);
        } catch (err) {
          if (errEl) errEl.textContent = err.message || String(err);
        } finally {
          submitBtn.disabled = false;
          submitBtn.textContent = '保存';
        }
      });
    }
  }

  function openRunWorkflowModal(wf, panel) {
    const titleEl = document.getElementById('modal-title');
    const bodyEl = document.getElementById('modal-body');
    const footEl = document.getElementById('modal-foot');
    const overlayEl = document.getElementById('overlay');
    if (!titleEl || !bodyEl || !footEl || !overlayEl) return;

    const nodes = Array.isArray(wf.nodeInfoList) ? wf.nodeInfoList : [];
    const fieldsHtml = nodes.length
      ? nodes.map((n, idx) => {
          const label = n.description || `${n.nodeName || 'Node'} · ${n.fieldName || ''}`;
          const val = n.fieldValue !== undefined && n.fieldValue !== null ? String(n.fieldValue) : '';
          const ftype = String(n.fieldType || 'TEXT').toUpperCase();
          const isMedia = ftype === 'IMAGE' || ftype === 'AUDIO' || ftype === 'VIDEO';
          const accept = ftype === 'IMAGE' ? 'image/*' : ftype === 'AUDIO' ? 'audio/*' : ftype === 'VIDEO' ? 'video/*' : '*/*';
          const uploadBtn = isMedia
            ? `<button type="button" class="ghost rh-upload-btn" data-idx="${idx}" data-ftype="${ftype.toLowerCase()}" style="flex:none;white-space:nowrap;">上传${ftype === 'IMAGE' ? '图片' : ftype === 'AUDIO' ? '音频' : '视频'}</button><input type="file" class="rh-upload-input" data-idx="${idx}" accept="${accept}" style="display:none;" />`
            : '';

          let listOptions = null;
          if (ftype === 'LIST' && n.fieldData) {
            try {
              const parsed = typeof n.fieldData === 'string' ? JSON.parse(n.fieldData) : n.fieldData;
              // 上游 fieldData 实际有三种形状，都要兼容：
              //   1. [["a","b","c"], {default:"a"}]   ← 最常见，首元素是选项值数组
              //   2. ["a","b","c"]                     ← 纯字符串数组
              //   3. [{index:1,value:"a",name:"A"},…]  ← 对象数组
              let raw = parsed;
              if (Array.isArray(parsed) && parsed.length && Array.isArray(parsed[0])) {
                raw = parsed[0];
              }
              if (Array.isArray(raw) && raw.length) {
                listOptions = raw.map(opt => {
                  if (opt && typeof opt === 'object') {
                    const ov = opt.index !== undefined ? String(opt.index)
                      : (opt.value !== undefined ? String(opt.value) : String(opt.name || ''));
                    const ol = opt.name !== undefined ? String(opt.name) : ov;
                    return { value: ov, label: ol };
                  }
                  return { value: String(opt), label: String(opt) };
                }).filter(o => o.value);
              }
            } catch (_) {}
          }

          const controlHtml = listOptions
            ? `<select class="rh-wf-field" data-idx="${idx}" style="flex:1;min-width:0;">${listOptions.map(o => `<option value="${escapeHtml(o.value)}"${o.value === val ? ' selected' : ''}>${escapeHtml(o.label)}</option>`).join('')}</select>`
            : `<input type="text" class="rh-wf-field" data-idx="${idx}" value="${escapeHtml(val)}" style="flex:1;min-width:0;" />`;

          return `
            <div style="display:flex;flex-direction:column;gap:4px;">
              <label style="font-size:12px;">
                <strong>${escapeHtml(label)}</strong>
                <span class="muted mono" style="font-size:11px;margin-left:6px;">#${escapeHtml(String(n.nodeId || ''))}.${escapeHtml(String(n.fieldName || ''))} (${escapeHtml(ftype)})</span>
              </label>
              <div style="display:flex;gap:6px;align-items:center;">
                ${controlHtml}
                ${uploadBtn}
              </div>
            </div>`;
        }).join('')
      : '<div class="muted" style="font-size:12px;">该工作流无预设可编辑参数，可直接发起调用。</div>';

    titleEl.textContent = `运行工作流：${wf.name || wf.webappId}`;
    bodyEl.innerHTML = `
      <div style="display:flex;flex-direction:column;gap:10px;max-height:55vh;overflow-y:auto;padding-right:4px;">
        ${fieldsHtml}
      </div>
      <div id="rh-run-status" class="muted" style="font-size:12px;margin-top:10px;"></div>
      <div id="rh-run-result" style="margin-top:10px;"></div>
    `;
    footEl.innerHTML = `
      <button class="ghost" onclick="closeModal()">关闭</button>
      <button id="rh-wf-run-btn">发起调用</button>
    `;
    overlayEl.classList.add('show');

    bodyEl.querySelectorAll('.rh-upload-btn').forEach(btn => {
      const idx = btn.dataset.idx;
      const ftype = btn.dataset.ftype || 'image';
      const fileInput = bodyEl.querySelector(`.rh-upload-input[data-idx="${idx}"]`);
      const textInput = bodyEl.querySelector(`.rh-wf-field[data-idx="${idx}"]`);
      if (!fileInput || !textInput) return;
      btn.addEventListener('click', () => fileInput.click());
      fileInput.addEventListener('change', async () => {
        const file = fileInput.files && fileInput.files[0];
        if (!file) return;
        const origText = btn.textContent;
        btn.disabled = true;
        btn.textContent = '上传中…';
        const statusEl = document.getElementById('rh-run-status');
        if (statusEl) statusEl.textContent = '';
        try {
          const fd = new FormData();
          fd.append('file', file);
          fd.append('file_type', ftype);
          const resp = await fetch('/ui/api/runninghub/upload', { method: 'POST', body: fd });
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) {
            throw new Error(data?.error?.message || data?.detail?.error?.message || `HTTP ${resp.status}`);
          }
          if (data.fileName) {
            textInput.value = data.fileName;
          }
        } catch (err) {
          if (statusEl) statusEl.textContent = `上传失败: ${err.message || err}`;
        } finally {
          btn.disabled = false;
          btn.textContent = origText;
          fileInput.value = '';
        }
      });
    });

    const runBtn = document.getElementById('rh-wf-run-btn');
    if (runBtn) {
      // 提交后立刻返回，任务进队列；弹窗只做「轻跟踪」：每 4s 刷一次这条任务，
      // 完成就把结果渲染出来。关掉弹窗任务也照跑，去队列页看。
      const doRun = async (force) => {
        const updatedNodes = nodes.map((n, idx) => {
          const input = bodyEl.querySelector(`.rh-wf-field[data-idx="${idx}"]`);
          return Object.assign({}, n, { fieldValue: input ? input.value : n.fieldValue });
        });
        const statusEl = document.getElementById('rh-run-status');
        const resultEl = document.getElementById('rh-run-result');
        runBtn.disabled = true;
        runBtn.textContent = '提交中…';
        if (statusEl) statusEl.textContent = '正在提交到站内队列…';
        if (resultEl) resultEl.innerHTML = '';
        try {
          const res = await rhFetch('/ui/api/runninghub/tasks/submit', {
            method: 'POST',
            body: JSON.stringify({ webapp_id: wf.webappId, nodeInfoList: updatedNodes }),
          });
          const taskId = res.task && res.task.taskId;
          if (statusEl) statusEl.innerHTML = `已提交 <span class="mono">${escapeHtml(taskId || '')}</span>，正在生成（可关闭弹窗，任务在队列里继续跑）`
            + (res.balance && res.balance.transitionCoins != null ? `<span class="muted" style="margin-left:8px">剩余过渡币 <strong class="mono">${escapeHtml(String(res.balance.transitionCoins))}</strong></span>` : '');
          if (typeof renderTaskQueue === 'function' && panel) renderTaskQueue(panel);
          if (!taskId) return;
          // 轻跟踪：终态就渲染结果，否则每 4s 刷一次
          const t0 = Date.now();
          const tick = async () => {
            let t = null;
            try { t = (await rhFetch(`/ui/api/runninghub/tasks/${encodeURIComponent(taskId)}`)).task; } catch (e) { return; }
            if (!t || !document.getElementById('rh-run-status')) return; // 弹窗关了
            const el = Math.round((Date.now() - t0) / 1000);
            if (t.status === 'SUCCESS') {
              if (statusEl) statusEl.innerHTML = `任务完成 <span class="mono">${escapeHtml(t.upstreamTaskId || taskId)}</span> · ${el}s`
                + (t.coinsSpent != null ? `<span class="muted" style="margin-left:8px">实扣 <strong class="mono">${escapeHtml(String(t.coinsSpent))}</strong> 过渡币</span>` : '');
              renderTaskOutputs(resultEl, t);
            } else if (['FAILED', 'TIMEOUT', 'CANCELLED'].includes(t.status)) {
              if (statusEl) statusEl.innerHTML = `<span style="color:#ef4444">${escapeHtml(statusLabel(t.status))}${t.error ? '：' + escapeHtml(t.error) : ''}</span>`;
              if (t.closedSource && panel) refreshWorkflows(panel);
            } else {
              if (statusEl) statusEl.textContent = `${statusLabel(t.status)} · ${el}s`;
              setTimeout(tick, 4000);
            }
          };
          setTimeout(tick, 1500);
        } catch (err) {
          if (statusEl) statusEl.innerHTML = `<span style="color:#ef4444">${escapeHtml(err.message || String(err))}</span>`;
          if (err && err.detail && Array.isArray(err.detail.workflows) && panel) renderWorkflows(panel, err.detail.workflows);
        } finally {
          runBtn.disabled = false;
          runBtn.textContent = '发起调用';
        }
      };
      runBtn.addEventListener('click', () => doRun(false));
    }
  }
})();
