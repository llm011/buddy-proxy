// ---- TRAE PAT 面板（额度页末尾整宽卡片）----
// 结构：每个账号的 token 状态直接回填到对应 PAT #N 额度卡标题；模型负载放在
// 账号卡之后，内部两栏排列。默认读缓存（GET，不触网），点「更新」再强制查询。
function renderTraepatPanel() {
  const panel = document.getElementById('traepat-panel');
  if (!panel) return;
  const pat = (BENEFITS.providers || []).find(p => p.id === 'traepat' && !p.disabled && p.quota && p.quota.supported);
  if (!pat) { panel.innerHTML = ''; return; }

  // 日包/周包按「PAT #N」分组（label 形如「PAT #1 · Orange 包」）。
  // 查询失败说明条（unreachable/query_failed 标记、label「PAT 额度网关不可达」
  // 等）不进卡片：它没有「 · 」后缀会被拆成独立一组，网关一抖就整页冒出
  // 「Trae PAT (服务账号直连) / 账号状态加载中… / 剩 10/10 个账号本轮未取到新
  // 数据…」的奇怪首卡（用户 2026-10-12）。失败状态由账号行的 token/冷却小字
  // 与 keeper 行传达；后端条目保留（benefits.py 的短缓存判定要用）。
  const groups = new Map();
  for (const it of pat.quota.items || []) {
    if (it.unreachable === true || it.query_failed === true) continue;
    const idx = it.label.indexOf(' · ');
    const grp = idx >= 0 ? it.label.slice(0, idx) : (pat.name || 'PAT');
    const sub = idx >= 0 ? it.label.slice(idx + 3) : it.label;
    if (!groups.has(grp)) groups.set(grp, []);
    groups.get(grp).push(Object.assign({}, it, {label: sub}));
  }
  const quotaHtml = [...groups.entries()].map(([grp, its]) => {
    const m = /^PAT #(\d+)$/.exec(grp);
    const idx = m ? Number(m[1]) : null;
    const attrs = idx == null ? '' : ` data-pat-idx="${idx}"`;
    return `<div class="pat-pkg"${attrs}>
      <div class="pat-pkg-head">
        <div class="pat-pkg-name">${esc(grp)}</div>
        <!-- 副标题先留空位：账号数据回填前的「账号状态加载中…」会被误读成
             出错（用户 2026-10-12），改静默占位，loadTraepatAccounts 回填或
             出错时才显示文字 -->
        <div class="pat-pkg-status muted"></div>
      </div>
      ${quotaItemsHtml(its, 'pat:' + grp)}
    </div>`;
  }).join('') || '<div class="empty" style="padding:12px 0">无额度数据</div>';

  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head">
        <span class="name">Trae PAT</span>
        <span class="tag">多账号 · 自愈</span>
        <span class="grow"></span>
        <span class="pat-keeper" id="pat-keeper"></span>
        ${providerRefreshButton('traepat')}
        <button class="primary" onclick="refreshTraepatTokens(this)">补签 Token</button>
      </div>
      <div class="pat-quota-grid">${quotaHtml}</div>
      <div id="traepat-account-error"></div>
      <div class="pat-load-section">
        <div class="pat-col-head">
          <span>模型负载</span><span class="grow"></span>
          <button class="primary" onclick="loadTraepatStatus(this, true)">更新</button>
        </div>
        <div id="traepat-status"><div class="empty" style="padding:8px 0">加载中…</div></div>
      </div>
    </div>`;
  loadTraepatAccounts();
  loadTraepatStatus(null, false);
  syncPatStatusAuto();  // 面板每次重建（含 30s loadAll）后按当前页是否可见起停自动刷新
}

async function loadTraepatAccounts() {
  const panel = document.getElementById('traepat-panel');
  const keeperBox = document.getElementById('pat-keeper');
  const errorBox = document.getElementById('traepat-account-error');
  if (!panel) return;
  try {
    // 同一通道在飞时复用（acctFetch）：30s 轮询 + 整页重绘叠加不再并排多发
    const r = await acctFetch('traepat', () => api('/ui/api/traepat/accounts'));
    if (!r.enabled) { panel.innerHTML = ''; return; }
    const tokenText = t => t === 'ok' ? 'token 正常' : t === 'expiring' ? 'token 临期' : '无 token';
    for (const [i, a] of (r.accounts || []).entries()) {
      // accounts 按 priority 排序，但额度 label 的 #N 是配置原始下标；以后者为准。
      const idx = Number(a.display_index) || (i + 1);  // 兼容滚动升级时的旧响应
      const card = panel.querySelector(`[data-pat-idx="${idx}"]`);
      if (!card) continue;  // 该账号本轮无额度数据时不凭空造一张空卡
      const name = card.querySelector('.pat-pkg-name');
      const status = card.querySelector('.pat-pkg-status');
      if (name) name.textContent = `PAT #${idx}（${a.id}）`;
      const bits = [tokenText(a.token)];
      if (a.hours_left != null) bits.push(`剩 ${a.hours_left}h`);
      for (const c of a.cooling || []) bits.push(`${c.kind} 冷却 ${c.minutes_left}min`);
      if (status) {
        status.textContent = bits.join(' · ');
        status.classList.toggle('warn', a.token !== 'ok' || (a.cooling || []).length > 0);
      }
    }
    if (errorBox) errorBox.innerHTML = '';
    // keeper 自愈状态 → 总标题；账号 token 详情已经散到各自卡片标题。
    const k = r.keeper || {};
    const kAt = k.at ? new Date(k.at * 1000).toLocaleTimeString('zh-CN') : '—';
    const kState = k.env_ready === false ? '<span style="color:var(--warn)">换 token 端点不可达，等网络恢复</span>'
      : k.env_ready === true ? `上轮补签 ${((k.refreshed || []).length)} 个${(k.waiting || []).length ? `，待补 ${(k.waiting).length} 个` : ''}`
      : '尚未运行';
    if (keeperBox) keeperBox.innerHTML =
      `每 ${Math.round((r.keepalive_s || 0) / 60)} 分钟自愈 · ${kState}（${kAt}）`;
  } catch (e) {
    if (errorBox) errorBox.innerHTML =
      `<div class="empty" style="padding:8px 0">账号状态加载失败：${esc(e.message)}</div>`;
  }
}

async function refreshTraepatTokens(btn) {
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '补签中…'; }
  try {
    const r = await api('/ui/api/traepat/refresh-tokens', {method: 'POST'});
    const n = (r.refreshed || []).length;
    const waiting = r.waiting || [];
    if (r.env_ready === false) {
      toast(`补签端点暂不可达，${waiting.length} 个账号等待中`, true);
    } else if (waiting.length) {
      toast(`已补签 ${n} 个，仍有 ${waiting.length} 个待补`, true);
    } else {
      toast(`✓ Token 补签完成${n ? `（更新 ${n} 个）` : '（无需更新）'}`);
    }
    await loadTraepatAccounts();
  } catch (e) { toast('Token 补签失败：' + e.message, true); }
  if (btn) { btn.disabled = false; btn.textContent = old || '补签 Token'; }
}
