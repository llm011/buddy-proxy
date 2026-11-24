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

  function renderBillingTag(billingType) {
    const bType = billingType || 'RH 金币 / 余额';
    const bCls = bType === '钱包余额' ? 'tag-money' : (bType === 'RH 金币' ? 'tag-coins' : 'tag-mixed');
    return `<span class="tag ${bCls}" data-tip="计费方式">${escapeHtml(bType)}</span>`;
  }

  async function rhFetch(path, opts) {
    const resp = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {}));
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const msg = (data && data.detail && data.detail.error && data.detail.error.message) || data.message || ('HTTP ' + resp.status);
      throw new Error(msg);
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
      const coins = info.remainCoins !== undefined && info.remainCoins !== null ? info.remainCoins : '—';
      const money = info.remainMoney !== undefined && info.remainMoney !== null
        ? `${info.remainMoney}${info.currency ? ' ' + info.currency : ''}`
        : '—';
      const tasks = info.currentTaskCounts !== undefined && info.currentTaskCounts !== null ? info.currentTaskCounts : '0';
      const apiTypeRaw = String(info.apiType || 'NORMAL').toUpperCase();
      const apiTypeLabel = apiTypeRaw === 'SHARED' ? '共享 API' : (apiTypeRaw === 'EXCLUSIVE' ? '独享 API' : apiTypeRaw);

      const upBtn = idx > 0
        ? `<button class="ghost" data-tip="上移" data-rh-action="up" data-id="${escapeHtml(acc.account_id)}">↑</button>`
        : '';

      return `
        <div class="pat-pkg" style="padding:14px 16px;">
          <div style="display:flex;align-items:center;gap:8px;min-width:0;border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:10px;">
            <span class="pat-pkg-name" style="font-size:14px;font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(acc.label || acc.account_id)}</span>
            <span class="tag" style="flex-shrink:0;" data-tip="API 类型: ${escapeHtml(apiTypeRaw)}">${escapeHtml(apiTypeLabel)}</span>
            <span style="flex-shrink:0;display:inline-flex;gap:4px;">
              <button class="ghost" title="刷新额度" data-rh-action="refresh" data-id="${escapeHtml(acc.account_id)}">↻</button>
              <button class="ghost" title="重命名" data-rh-action="rename" data-id="${escapeHtml(acc.account_id)}">✎</button>
              ${upBtn}
              <button class="ghost" title="删除" data-rh-action="delete" data-id="${escapeHtml(acc.account_id)}">✕</button>
            </span>
          </div>
          <div class="muted mono" style="font-size:11px;margin:0 0 8px">${escapeHtml(acc.masked_key || '')}</div>
          <div style="display:flex;gap:14px;font-size:12px;flex-wrap:wrap">
            <span>金币 <strong class="mono">${escapeHtml(String(coins))}</strong></span>
            <span>余额 <strong class="mono">${escapeHtml(String(money))}</strong></span>
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
      <p style="margin:0 0 8px">粘贴 RunningHub API Token，系统会自动查询账户金币与余额并安全落盘（0600）。</p>
      <div style="display:flex;flex-direction:column;gap:8px;margin-bottom:8px">
        <input type="text" id="rh-modal-label" placeholder="备注名（可选，留空自动填充）" />
        <input type="password" id="rh-modal-token" class="mono" placeholder="输入 RunningHub API Token" />
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
        const label = (document.getElementById('rh-modal-label')?.value || '').trim();
        const errEl = document.getElementById('rh-modal-err');
        if (!token) {
          if (errEl) errEl.textContent = '请输入 API Token';
          return;
        }
        submitBtn.disabled = true;
        submitBtn.textContent = '验证中…';
        try {
          const res = await rhFetch('/ui/api/runninghub/accounts/import', {
            method: 'POST',
            body: JSON.stringify({ api_key: token, label: label || undefined }),
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
      const bType = wf.billingType || 'RH 金币 / 余额';
      const bCls = bType === '钱包余额' ? 'tag-money' : (bType === 'RH 金币' ? 'tag-coins' : 'tag-mixed');
      const billingTag = `<span class="tag ${bCls}" style="float:right;margin:0 0 6px 10px;" data-tip="计费方式">${escapeHtml(bType)}</span>`;
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
        if (wf) openRunWorkflowModal(wf);
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
                ${renderBillingTag(wf.billingType)}
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

  function openRunWorkflowModal(wf) {
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
      runBtn.addEventListener('click', async () => {
        const updatedNodes = nodes.map((n, idx) => {
          const input = bodyEl.querySelector(`.rh-wf-field[data-idx="${idx}"]`);
          return Object.assign({}, n, { fieldValue: input ? input.value : n.fieldValue });
        });
        const statusEl = document.getElementById('rh-run-status');
        const resultEl = document.getElementById('rh-run-result');
        runBtn.disabled = true;
        runBtn.textContent = '执行中…';
        if (statusEl) statusEl.textContent = '已提交任务，正在轮询生成结果（可能需要数十秒）…';
        if (resultEl) resultEl.innerHTML = '';

        try {
          const res = await rhFetch('/ui/api/runninghub/workflows/run', {
            method: 'POST',
            body: JSON.stringify({ webapp_id: wf.webappId, nodeInfoList: updatedNodes }),
          });
          if (statusEl) statusEl.textContent = `任务完成 (taskId=${res.taskId})`;
          const outs = Array.isArray(res.outputs) ? res.outputs : [];
          if (resultEl) {
            resultEl.innerHTML = outs.length
              ? outs.map((o) => o.type === 'video'
                  ? `<div style="margin-top:6px;"><video src="${escapeHtml(o.url)}" controls style="max-width:100%;border-radius:8px;"></video><div><a href="${escapeHtml(o.url)}" target="_blank">打开视频链接</a></div></div>`
                  : `<div style="margin-top:6px;"><img src="${escapeHtml(o.url)}" style="max-width:100%;border-radius:8px;" /><div><a href="${escapeHtml(o.url)}" target="_blank">打开图片链接</a></div></div>`).join('')
              : '<div class="muted">任务完成，但未返回媒体文件。</div>';
          }
        } catch (err) {
          if (statusEl) statusEl.innerHTML = `<span style="color:#ef4444">${escapeHtml(err.message || String(err))}</span>`;
        } finally {
          runBtn.disabled = false;
          runBtn.textContent = '发起调用';
        }
      });
    }
  }
})();
