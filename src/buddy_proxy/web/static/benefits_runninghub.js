// RunningHub 生图/生视频账号与额度管理面板（复用原生 chart-card / pat-head / pat-quota-grid 样式）

(function () {
  if (!document.getElementById('rh-panel-style')) {
    const st = document.createElement('style');
    st.id = 'rh-panel-style';
    st.textContent = [
      '.rh-wallets { display:flex; flex-direction:column; gap:2px; }',
      '.rh-wallet-row { display:grid; grid-template-columns:64px 1fr auto; align-items:center; gap:10px; font-size:12px; padding:4px 0; }',
      '.rh-wallet-row + .rh-wallet-row { border-top:1px dashed var(--line); }',
      '.rh-wallet-name { color:var(--muted, #9ca3af); }',
      '.rh-wallet-val { font-weight:600; font-size:13px; }',
      '.rh-exp { font-size:11px; padding:1px 7px; border-radius:999px; white-space:nowrap; }',
      '.rh-exp-ok { background:rgba(16,185,129,.12); color:#10b981; }',
      '.rh-exp-day { background:rgba(245,158,11,.10); color:#f59e0b; }',
      '.rh-exp-soon { background:rgba(239,68,68,.12); color:#ef4444; }',
    ].join('\n');
    document.head.appendChild(st);
  }
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

  function setHtmlIfChanged(el, html) {
    if (el.innerHTML === html) return false;
    el.innerHTML = html;
    return true;
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

  // ---- 每日积分自动领取 ----
  // 后端 dailygift 循环每 10 分钟巡一轮，站点 00:00（UTC 16:00）清零后给每个号领一次。
  // 这里只负责把状态画出来 + 手动「立即领取」。
  let _giftState = { running: false, day: '', accounts: {} };
  async function loadGiftState() {
    try { _giftState = (await rhFetch('/ui/api/runninghub/daily-gift')).gift || _giftState; }
    catch (e) { /* 拿不到状态不影响别的 */ }
    return _giftState;
  }
  function giftBadge(acc) {
    if (!acc.email) return '';
    const g = _giftState.accounts || {};
    const mine = g[acc.account_id];
    const today = _giftState.day;
    const claimed = mine && mine.day && mine.day === today;
    if (claimed) {
      const t = mine.claimed_at ? new Date(mine.claimed_at * 1000) : null;
      return `<span class="rh-exp rh-exp-ok" data-tip="${escapeHtml(mine.already ? '今天已领过（网页上领的 / 刷余额时顺手领的）' : '自动领取成功')}${t ? ' · ' + escapeHtml(t.toLocaleTimeString()) : ''}" style="cursor:help">今日积分 ✓</span>`;
    }
    if (mine && mine.error) {
      const mins = mine.next_try_in ? Math.ceil(mine.next_try_in / 60) : null;
      return `<button class="rh-exp rh-exp-soon" data-tip="${escapeHtml(mine.error)}" data-rh-action="claim" data-id="${escapeHtml(acc.account_id)}" style="border:0;cursor:pointer">领取失败${mins ? ' · ' + mins + 'm 后重试' : ''}</button>`;
    }
    return `<button class="rh-exp rh-exp-day" data-tip="${_giftState.running ? '等自动领取（每 10 分钟巡一轮）；点这里立即领' : '自动领取循环没在跑；点这里手动领'}" data-rh-action="claim" data-id="${escapeHtml(acc.account_id)}" style="border:0;cursor:pointer">待领取</button>`;
  }

  // ---- 过期时间 / 钱包行 ----
  // 站内时间 → 本地 Date。支持 "2026-11-27T16:00:00.000+00:00" 和无时区的 "2026-11-08 19:23:17"（站点 UTC+8）
  function parseSiteTime(s) {
    if (!s) return null;
    const str = String(s).trim();
    if (str.includes('T')) { const d = new Date(str.replace(' ', '+')); return isNaN(d) ? null : d; }
    const m = str.match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/);
    if (!m) return null;
    // 站点本地时间按 UTC+8 解释——和后端 websession.SITE_TZ_OFFSET_HOURS 是同一个假设，
    // 来源见那边的注释（point/summary 的 nearestExpireTime 是 16:00Z = 站点 00:00），要改一起改
    return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4] - 8, +m[5], +m[6]));
  }
  function fmtExpiry(s) {
    const d = parseSiteTime(s);
    if (!d) return null;
    const ms = d.getTime() - Date.now();
    if (ms <= 0) return { label: '已过期', cls: 'rh-exp-ok', title: d.toLocaleString() };
    const hours = ms / 3.6e6;
    const days = ms / 8.64e7;
    const cls = hours < 6 ? 'rh-exp-soon' : (days < 2 ? 'rh-exp-day' : 'rh-exp-ok');
    let label;
    if (hours < 1) label = `${Math.max(1, Math.round(ms / 6e4))} 分钟后过期`;
    else if (hours < 24) label = `${Math.round(hours)} 小时后过期`;
    else if (days < 60) label = `${Math.round(days)} 天后过期`;
    else label = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    return { label, cls, title: d.toLocaleString() };
  }
  function walletRow(name, value, expire, tip, expireNote) {
    const ex = fmtExpiry(expire);
    const exHtml = ex
      ? `<span class="rh-exp ${ex.cls}" title="${escapeHtml(ex.title || '')}">${escapeHtml(ex.label)}</span>`
      : `<span class="muted" style="font-size:11px">${escapeHtml(expireNote || '—')}</span>`;
    return `
      <div class="rh-wallet-row" data-tip="${escapeHtml(tip || '')}">
        <span class="rh-wallet-name">${escapeHtml(name)}</span>
        <span class="rh-wallet-val mono">${escapeHtml(value == null ? '—' : String(value))}</span>
        <span class="rh-wallet-exp">${exHtml}</span>
      </div>`;
  }

  function renderAccounts(panel, accounts) {
    const bodyEl = panel.querySelector('#rh-accounts-body');
    if (!bodyEl) return;
    if (!accounts || !accounts.length) {
      setHtmlIfChanged(bodyEl, '<div class="empty" style="padding:18px 0">暂无 RunningHub 账号，点击右上角「导入账号」添加 API Token</div>');
      return;
    }

    const cards = accounts.map((acc, idx) => {
      const info = acc.account_info || {};
      // 邮箱账号（站内模式）account_info 来自 /uc/point/summary + getUserInfo：
      //   transitionCoins 过渡币（只有网页端登录态能花）/ activityPoints 活动积分 / walletBalance 钱包 $
      // 纯 key 账号（OpenAPI 模式）还是老的 remainCoins / remainMoney / remainPoint。
      const isWeb = !!(acc.email && acc.web_session_status && acc.web_session_status.logged_in);
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
          <div class="muted mono" style="font-size:11px;margin:0 0 8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <span>${escapeHtml(acc.masked_email || acc.masked_key || '')}</span>
            ${ss.logged_in
              ? `<span class="rh-exp ${ss.last_error ? 'rh-exp-soon' : 'rh-exp-ok'}" data-tip="${escapeHtml(ss.last_error || '登录态正常，到期前自动续')}" style="cursor:help">登录态 ${ss.expires_in_days != null ? ss.expires_in_days + 'd' : 'ok'}</span>`
              : (acc.email ? '<span class="rh-exp rh-exp-soon">未登录</span>' : '')}
            <span class="muted">运行中 <strong class="mono">${escapeHtml(String(tasks))}</strong></span>
            ${giftBadge(acc)}
          </div>
          <div class="rh-wallets">
            ${isWeb
              ? walletRow('过渡币', info.transitionCoins, info.transitionCoinExpireAt, '旧币体系的过渡币：只有网页端登录态能花，API 不认；有效期见右', '有效期未知')
              : walletRow('站内币', info.remainCoins, null, '站内 RH 币（纯 key 账号拿不到有效期）', '有效期未知')}
            ${isWeb
              ? walletRow('积分', info.activityPoints, info.pointsExpireAt, '每日赠送 30 分，每天 00:00（站点时间）清零；应用里的「模型调用」扣这个', '有效期未知')
              : walletRow('API 额度', info.remainPoint, null, 'API 调用可用额度（remainPoint）', '')}
            ${walletRow('钱包 $', info.walletBalance, null, '充值余额：闭源模型节点 / OpenAPI 只认这个，不过期', '不过期')}
            ${isWeb && (info.freeImageLeft != null || info.freeVideoLeft != null)
              ? walletRow('免费次数', `图 ${info.freeImageLeft ?? 0} · 视频 ${info.freeVideoLeft ?? 0}`, null, '站内福利送的免费生成次数（另算，不扣币不扣分；每天重置）', '每日重置')
              : ''}
          </div>
        </div>`;
    }).join('');

    if (!setHtmlIfChanged(bodyEl, `<div class="pat-quota-grid">${cards}</div>`)) return;

    bodyEl.querySelectorAll('button[data-rh-action]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const action = btn.getAttribute('data-rh-action');
        const id = btn.getAttribute('data-id');
        try {
          if (action === 'refresh') {
            const res = await rhFetch('/ui/api/runninghub/accounts/refresh', { method: 'POST', body: JSON.stringify({ account_id: id }) });
            renderAccounts(panel, res.accounts);
          } else if (action === 'claim') {
            btn.disabled = true;
            const res = await rhFetch('/ui/api/runninghub/daily-gift/claim', { method: 'POST', body: JSON.stringify({ account_id: id }) });
            _giftState = res.gift || _giftState;
            renderAccounts(panel, res.accounts);
            const failed = res.failed && res.failed[id];
            if (failed) window.alert('领取失败：' + failed);
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
    // 全局 30s 轮询也会进这里。外层骨架和事件只建一次，否则每次轮询都会
    // 把三个卡片整块删掉，表现为页面闪烁、滚动位置跳动。
    if (panel.dataset.rhInitialized !== '1') {
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
            <button class="ghost" id="rh-queue-archived">归档</button>
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
          <span class="tag" id="rh-gift-loop-tag" data-tip="每日赠送 30 积分，站点 00:00（UTC 16:00）清零后自动给每个号领一次；每 10 分钟巡一轮">积分自动领取</span>
          <span class="pat-head-mid"></span>
          <span class="pat-head-actions">
            <button class="ghost" id="rh-claim-all-btn" data-tip="立即给所有号领今天的积分（已领过的不影响）">全部领取</button>
            <button class="ghost" id="rh-refresh-all-btn">全部刷新</button>
            <button class="ghost" id="rh-import-btn">导入账号</button>
          </span>
        </div>
        <div id="rh-accounts-body"><div class="empty" style="padding:18px 0">加载中…</div></div>
      </div>
    `;

    panel.querySelector('#rh-wf-import-btn').addEventListener('click', () => openImportWorkflowModal(panel));
    panel.querySelector('#rh-queue-refresh').addEventListener('click', () => renderTaskQueue(panel));
    panel.querySelector('#rh-queue-archived').addEventListener('click', () => openArchivedTasks(panel));
    panel.querySelector('#rh-queue-clear').addEventListener('click', async () => {
      if (!window.confirm('清掉所有已完成 / 失败 / 取消的任务记录？（不影响站内产出文件）')) return;
      try {
        const res = await rhFetch('/ui/api/runninghub/tasks', {});
        const done = (res.tasks || []).filter((t) => !isTaskActive(t.status));
        for (const t of done) await rhFetch('/ui/api/runninghub/tasks/delete', { method: 'POST', body: JSON.stringify({ task_id: t.taskId }) });
        renderTaskQueue(panel);
      } catch (err) { window.alert(err.message || String(err)); }
    });
    panel.querySelector('#rh-claim-all-btn').addEventListener('click', async () => {
      const btn = panel.querySelector('#rh-claim-all-btn');
      btn.disabled = true; btn.textContent = '领取中…';
      try {
        const res = await rhFetch('/ui/api/runninghub/daily-gift/claim', { method: 'POST', body: '{}' });
        _giftState = res.gift || _giftState;
        renderAccounts(panel, res.accounts);
        const nOk = (res.claimed || []).length, nAlready = (res.already || []).length, nFail = Object.keys(res.failed || {}).length;
        window.alert(`领取完成：新领到 ${nOk} 个，之前已领 ${nAlready} 个${nFail ? '，失败 ' + nFail + ' 个（看卡片上的红标）' : ''}`);
      } catch (err) { window.alert(err.message || String(err)); }
      finally { btn.disabled = false; btn.textContent = '全部领取'; }
    });
    panel.querySelector('#rh-import-btn').addEventListener('click', () => openImportModal(panel));
    panel.querySelector('#rh-refresh-all-btn').addEventListener('click', async () => {
      try {
        const res = await rhFetch('/ui/api/runninghub/accounts/refresh', { method: 'POST', body: '{}' });
        await loadGiftState();
        renderAccounts(panel, res.accounts);
      } catch (err) {
        window.alert(err.message || String(err));
      }
    });
      panel.dataset.rhInitialized = '1';
    }

    try {
      const [accRes, wfRes] = await Promise.all([
        rhFetch('/ui/api/runninghub/accounts'),
        rhFetch('/ui/api/runninghub/workflows'),
        loadGiftState(),
      ]);
      const loopTag = panel.querySelector('#rh-gift-loop-tag');
      if (loopTag) {
        loopTag.textContent = _giftState.running ? '积分自动领取 · 运行中' : '积分自动领取 · 未运行';
        loopTag.className = 'tag ' + (_giftState.running ? 'tag-coins' : 'tag-money');
      }
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
  async function renderTaskQueue(panel, archived = false, targetBody = null) {
    const bodyEl = targetBody || (panel && panel.querySelector('#rh-queue-body'));
    if (!bodyEl) return;
    if (!archived && _queueTimer) { clearTimeout(_queueTimer); _queueTimer = null; }
    let tasks = [];
    try {
      const res = await rhFetch(`/ui/api/runninghub/tasks?limit=100${archived ? '&archived=1' : ''}`);
      tasks = res.tasks || [];
    } catch (err) {
      bodyEl.innerHTML = `<div class="empty" style="padding:18px 0;color:#ef4444">加载失败: ${escapeHtml(err.message || err)}</div>`;
      return;
    }
    const cnt = panel.querySelector('#rh-queue-count');
    if (!archived && cnt) cnt.textContent = String(tasks.filter((t) => isTaskActive(t.status)).length);
    if (!tasks.length) {
      setHtmlIfChanged(bodyEl, `<div class="empty" style="padding:18px 0">${archived ? '暂无归档任务' : '暂无任务。在工作流卡片点「运行」提交一个。'}</div>`);
      return;
    }
    const queueChanged = setHtmlIfChanged(bodyEl, `<div class="rh-queue">${tasks.map((t) => {
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
          ${active ? `<button class="ghost rh-queue-cancel" data-id="${escapeHtml(t.taskId)}" title="不再等待（站内任务照跑，已扣币不退）">取消</button>`
            : `<button class="ghost rh-queue-archive" data-id="${escapeHtml(t.taskId)}">${archived ? '恢复' : '归档'}</button>`}
        </div>`;
    }).join('')}</div>`);

    if (queueChanged) {
      bodyEl.querySelectorAll('.rh-queue-row').forEach((row) => {
        row.addEventListener('click', () => openTaskDetail(panel, row.getAttribute('data-task'), tasks, archived));
        row.addEventListener('keydown', (e) => { if (e.key === 'Enter') openTaskDetail(panel, row.getAttribute('data-task'), tasks, archived); });
      });
      bodyEl.querySelectorAll('.rh-queue-archive').forEach((btn) => {
        btn.addEventListener('click', async (e) => {
          e.stopPropagation();
          btn.disabled = true;
          try {
            await rhFetch('/ui/api/runninghub/tasks/archive', {
              method: 'POST',
              body: JSON.stringify({ task_id: btn.getAttribute('data-id'), archived: !archived }),
            });
            renderTaskQueue(panel, archived, targetBody);
            if (archived) renderTaskQueue(panel);
          } catch (err) { window.alert(err.message || String(err)); btn.disabled = false; }
        });
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
    }
    // 有进行中的任务就 4s 一刷；没有就停掉定时器
    if (!archived && tasks.some((t) => isTaskActive(t.status))) {
      _queueTimer = setTimeout(() => { if (panel.isConnected) renderTaskQueue(panel); }, 4000);
    }
  }
  function openArchivedTasks(panel) {
    const titleEl = document.getElementById('modal-title');
    const bodyEl = document.getElementById('modal-body');
    const footEl = document.getElementById('modal-foot');
    const overlayEl = document.getElementById('overlay');
    if (!titleEl || !bodyEl || !footEl || !overlayEl) return;
    titleEl.textContent = '归档任务';
    bodyEl.innerHTML = '<div class="empty" style="padding:18px 0">加载中…</div>';
    footEl.innerHTML = '<button class="ghost" onclick="closeModal()">关闭</button>';
    overlayEl.classList.add('show');
    renderTaskQueue(panel, true, bodyEl);
  }
  function openTaskDetail(panel, taskId, tasks, archived = false) {
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
      ${isTaskActive(t.status) ? `<button class="ghost" id="rh-task-detail-cancel">不再等待</button>`
        : `<button class="ghost" id="rh-task-detail-archive">${archived ? '恢复到队列' : '归档'}</button>`}
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
    const archiveBtn = document.getElementById('rh-task-detail-archive');
    if (archiveBtn) archiveBtn.addEventListener('click', async () => {
      try {
        await rhFetch('/ui/api/runninghub/tasks/archive', {
          method: 'POST', body: JSON.stringify({ task_id: t.taskId, archived: !archived }),
        });
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
      setHtmlIfChanged(bodyEl, '<div class="empty" style="padding:18px 0">暂无工作流，点击右上角「导入工作流」添加 webappId</div>');
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

    if (!setHtmlIfChanged(bodyEl, `<div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;">${cards}</div>`)) return;

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

  function parseListOptions(fieldData, currentValue) {
    if (!fieldData) return null;
    let parsed;
    try { parsed = typeof fieldData === 'string' ? JSON.parse(fieldData) : fieldData; }
    catch (_) { return null; }
    if (!Array.isArray(parsed) || !parsed.length) return null;

    // ComfyUI 的 LIST 是 ["COMBO", {options:[...]}]；COMBO 是类型名，不是选项。
    // 否则会只剩一个 COMBO 下拉项，提交后上游报 value_not_in_list。
    let raw = parsed;
    if (Array.isArray(parsed[0])) raw = parsed[0];
    else if (parsed[1] && typeof parsed[1] === 'object' && Array.isArray(parsed[1].options)) raw = parsed[1].options;

    const options = raw.map((opt) => {
      if (opt && typeof opt === 'object') {
        const value = opt.index !== undefined ? String(opt.index)
          : (opt.value !== undefined ? String(opt.value) : String(opt.name || ''));
        return { value, label: opt.name !== undefined ? String(opt.name) : value };
      }
      return { value: String(opt), label: String(opt) };
    }).filter((o) => o.value);
    if (!options.length) return null;

    const current = currentValue !== undefined && currentValue !== null ? String(currentValue) : '';
    if (current && !options.some((o) => o.value === current)) options.unshift({ value: current, label: current });
    return options;
  }
  window.parseRunningHubListOptions = parseListOptions;

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

          const listOptions = ftype === 'LIST' ? parseListOptions(n.fieldData, val) : null;

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
