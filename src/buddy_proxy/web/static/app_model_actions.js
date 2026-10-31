async function setDefault(provider, model) {
  try {
    await api('/ui/api/settings', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ default_model: provider + '/' + model })});
    toast(`默认模型已设为 ${provider}/${model}`);
    refreshAll();
  } catch (e) { toast('设置失败: ' + e.message, true); }
}

async function toggleModel(provider, model, disabled) {
  try {
    await api('/ui/api/model-toggle', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider, model, disabled })});
    toast(`${provider}/${model} 已${disabled ? '停用（调用将直接失败）' : '启用'}`);
    refreshAll();
  } catch (e) { toast('操作失败: ' + e.message, true); }
}

async function toggleProvider(provider, enabled) {
  try {
    await api('/ui/api/provider-toggle', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider, disabled: !enabled })});
    toast(`${provider} 已${enabled ? '启用' : '停用（调用直接失败，额度页不再展示）'}`);
    refreshAll();
  } catch (e) {
    toast('操作失败: ' + e.message, true);
    if (MODELS) renderGroups();  // 失败回滚 checkbox 视觉：按服务端状态重画
  }
}

async function refreshProviderModels(provider, button) {
  if (button) button.disabled = true;
  const oldText = button ? button.textContent : '';
  if (button) button.innerHTML = '<span class="spin"></span> 拉取中';
  try {
    const r = await api('/ui/api/models/refresh', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider })});
    const changes = [];
    if ((r.added || []).length) changes.push(`新增 ${r.added.length}`);
    if ((r.removed || []).length) changes.push(`下架 ${r.removed.length}`);
    toast(`${provider} 模型目录已更新：${r.count} 个${changes.length ? '（' + changes.join('，') + '）' : ''}`);
    // 强制重取：刷新本身只改 provider 内存目录，当前 MODELS 是旧快照。
    await loadData(['models'], true);
  } catch (e) {
    toast('模型目录刷新失败: ' + e.message, true);
  } finally {
    if (button && button.isConnected) { button.disabled = false; button.textContent = oldText; }
  }
}

async function hideModel(provider, model) {
  if (!confirm(`隐藏 ${provider}/${model}？\n\n它会从模型列表和选择器消失，但直接点名仍可调用；这不是“停用”。`)) return;
  try {
    await api('/ui/api/model-hidden', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider, model, hidden: true })});
    toast(`${provider}/${model} 已隐藏（直接点名仍可调用）`);
    await loadData(['models'], true);
  } catch (e) { toast('隐藏失败: ' + e.message, true); }
}

async function restoreHiddenModel(provider, model) {
  try {
    await api('/ui/api/model-hidden', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider, model, hidden: false })});
    toast(`${provider}/${model} 已恢复显示`);
    closeModal();
    await loadData(['models'], true);
  } catch (e) { toast('恢复失败: ' + e.message, true); }
}

function openHiddenModelsModal() {
  const hidden = (MODELS && MODELS.hidden_models) || [];
  document.getElementById('modal-title').textContent = `已隐藏模型 · ${hidden.length}`;
  document.getElementById('modal-body').innerHTML = hidden.length ? `
    <div class="muted" style="font-size:13px;margin-bottom:10px">隐藏只影响列表曝光，直接点名仍可调用；要禁止调用请使用模型行里的“停用”。</div>
    <div class="kv">${hidden.map(m => `
      <span class="k"><span class="tag">${esc(m.provider)}</span></span>
      <span><span class="mono">${esc(m.model)}</span>${m.name && m.name !== m.model ? `<small class="muted" style="margin-left:8px">${esc(m.name)}</small>` : ''}${m.missing ? '<span class="tag bad" style="margin-left:8px">上游已下架</span>' : ''}</span>
      <span></span><span><button class="primary" onclick='restoreHiddenModel(${jsq(m.provider)},${jsq(m.model)})'>恢复显示</button></span>`).join('')}</div>`
    : '<div class="empty">没有隐藏的模型</div>';
  setModalFoot('');
  document.getElementById('overlay').classList.add('show');
}

// ── 限时可用时段编辑 ──
