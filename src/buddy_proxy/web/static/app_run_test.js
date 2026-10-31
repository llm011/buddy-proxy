
async function runTest(provider, model) {
  const overlay = document.getElementById('overlay');
  document.getElementById('modal-title').textContent = `测试 ${provider ? provider + '/' : ''}${model}`;
  document.getElementById('modal-body').innerHTML = '<span class="spin"></span>发送 "hi" 到上游，等待回复…（最长 120s）';
  setModalFoot('');  // 这个弹窗只有固定的「关闭」；不清空会残留上一个弹窗的按钮
  overlay.classList.add('show');
  try {
    const r = await api('/ui/api/test', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider, model })});
    const u = r.usage || {};
    const replyProvider = String(r.provider || provider || '');
    const replyModel = String(r.model || model || '');
    const replyTarget = replyProvider && !replyModel.startsWith(replyProvider + '/')
      ? `${replyProvider}/${replyModel}` : replyModel;
    document.getElementById('modal-body').innerHTML = r.ok ? `
      <div class="kv">
        <span class="k">结果</span><span style="color:var(--ok)">✓ 成功</span>
        <span class="k">耗时</span><span class="mono">${fmtMs(r.latency_ms)}</span>
        <span class="k">回复模型</span><span class="mono">${esc(replyTarget)}</span>
        <span class="k">Tokens</span><span class="mono">${u.prompt_tokens ?? '—'} + ${u.completion_tokens ?? '—'}</span>
        <span class="k">finish</span><span class="mono">${esc(r.finish_reason || '—')}</span>
      </div>
      <pre class="view">${esc(r.content)}</pre>` : `
      <div class="kv">
        <span class="k">结果</span><span style="color:var(--err)">✗ 失败</span>
        <span class="k">耗时</span><span class="mono">${fmtMs(r.latency_ms)}</span>
        <span class="k">状态</span><span class="mono">${r.status || '—'}</span>
      </div>
      <pre class="view" style="color:#ffb3b3">${esc(r.error || '未知错误')}</pre>`;
    refreshAll();
  } catch (e) {
    document.getElementById('modal-body').innerHTML =
      `<pre class="view" style="color:#ffb3b3">${esc(e.message)}</pre>`;
  }
}

// 弹窗底部按钮槽。各弹窗自己带「取消/保存」，写进这里而不是 body 末尾，
// 否则会和骨架里固定的「关闭」并排出现两个含义重叠的按钮。
function setModalFoot(html) {
  const foot = document.getElementById('modal-foot');
  if (foot) foot.innerHTML = html || '<button onclick="closeModal()">关闭</button>';
}

function closeModal() {
  document.getElementById('overlay').classList.remove('show');
  setModalFoot('');  // 复位：下次直接打开「测试」弹窗时不会看到上个弹窗的按钮
}
document.getElementById('overlay').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeModal();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

let initTab = location.hash.replace('#', '');
try { initTab = initTab || localStorage.getItem('bp-tab') || ''; } catch (e) {}
switchTab(initTab);
loadAll();
setInterval(() => loadAll(false, true), 30000);
