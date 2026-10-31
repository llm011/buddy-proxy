// 每行模型 id 旁的复制按钮：点一下把**带通道前缀的完整模型名**（就是客户端
// 请求 model 字段该填的值）写进剪贴板。裸名常常在多通道撞车（ark 与 codebuddy
// 都有 deepseek-v4.1-flash），只复制裸名等于没告诉用户走哪条通道。
function _copyBtnHtml(provider, model) {
  const full = provider + '/' + model;
  // 不放文案进 title 之外的属性：整串要能原样塞进 onclick 的单引号字面量
  return `<button class="ghost copy-model" title="复制 ${esc(full)}" data-copy="${esc(full)}" data-label="复制" onclick='copyModelName(this,${jsq(full)})'>复制</button>`;
}

async function copyModelName(btn, full) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(full);
    } else {
      // 非安全上下文（http://局域网IP:8787 访问是常见情况）没有 clipboard API，
      // 退回 textarea + execCommand；失败才报错，不能静默什么都不发生。
      const ta = document.createElement('textarea');
      ta.value = full;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      if (!ok) throw new Error('execCommand 失败');
    }
    toast(`已复制 ${full}`);
    if (btn) {
      // 复原文案必须是**模板里的初始常量**，不能读当前 textContent：连点两次时
      // 第二次读到的已经是「已复制」，两个 timer 一前一后会把它写回去再等 1.2s，
      // 看起来像卡住。各按钮初始文案不同（「复制」/「复制键名」），由模板用
      // data-label 声明，缺省回退「复制」。
      const label = btn.dataset.label || '复制';
      btn.textContent = '已复制';
      clearTimeout(btn._copyT);
      btn._copyT = setTimeout(() => { btn.textContent = label; }, 1200);
    }
  } catch (e) {
    toast('复制失败，请手动选择：' + full, true);
  }
}

function renderGroups() {
  // 「近 3 次耗时」单元格的 tooltip：逐条列出样本（新→旧），并对照 14 天均值
  function _recentMsTitle(st) {
    const ring = (st && st.recent_ms) || [];
    // 无样本（新模型 / 只有 0 耗时的失败请求）：单元格显示 —，这里也给一句
    // 说明，别让悬停什么反应都没有
    if (!ring.length) {
      return st && st.count ? ' title="还没有带耗时的实测；重启后的前几次请求会陆续补上"' : '';
    }
    const list = ring.map((ms, i) => `第 ${i + 1} 近：${fmtMs(ms)}`).join('；');
    const avg = st.avg_ms ? `；14 天均值 ${fmtMs(st.avg_ms)}` : '';
    return ` title="${esc(`近 ${ring.length} 次实测：${list}${avg}`)}"`;
  }
  const el = document.getElementById('groups');
  // 搜索时基于原始 MODELS.groups 生成过滤视图（浅拷贝组、过滤模型行），
  // 不改数据源；COLLAPSED 完全不参与搜索态的折叠判断。
  const searching = MODEL_QUERY.trim() !== '';
  const all = MODELS.groups || [];
  const groups = searching ? filteredModelGroups(all, MODEL_QUERY) : all;
  const shownModels = groups.reduce((n, g) => n + g.models.length, 0);
  const dm = MODELS.default_model || '';
  document.getElementById('model-summary').textContent = searching
    ? `命中 ${groups.length} 个通道 · ${shownModels} 个模型 · 搜索「${MODEL_QUERY.trim()}」`
    : `${all.length} 个通道 · ${shownModels} 个模型` + (dm ? ` · 默认 ${dm}` : '');
  const hidden = (MODELS.hidden_models || []).length;
  const hiddenBtn = document.getElementById('hidden-models-btn');
  if (hiddenBtn) hiddenBtn.textContent = `已隐藏${hidden ? ' ' + hidden : ''}`;
  // 搜索态下视图强制全展开、且不允许写折叠偏好，全部展开/折叠按钮禁用
  ['models-expand', 'models-collapse'].forEach(id => {
    const b = document.getElementById(id);
    if (b) b.disabled = searching;
  });
  if (!groups.length) {
    el.innerHTML = searching
      ? `<div class="empty">没有匹配「${esc(MODEL_QUERY.trim())}」的通道或模型 <button class="ghost" data-clear-model-search>清空搜索</button></div>`
      : '<div class="empty">没有可用通道</div>';
    return;
  }
  el.innerHTML = groups.map(g => {
    const h = g.health || {};
    const okFlag = (g.id === 'codebuddy') ? (h.authenticated && h.token_valid !== false)
                 : ('configured' in h ? h.configured !== false : (h.ready !== false && h.started !== false));
    const healthTxt = g.id === 'codebuddy' ? (okFlag ? '已登录' : '未登录')
                    : h.base_url ? esc(h.base_url.replace(/^https?:\/\//, '')) : (okFlag ? '正常' : '未就绪');
    const rows = g.models.map(m => {
      const st = m.stats || {};
      const dis = !!m.disabled;
      const sch = m.schedule || null;
      const schWins = sch ? sch.windows.map(w => w[0] + '~' + w[1]).join(', ') : '';
      const schTag = sch
        ? `<span class="tag ${sch.open ? 'ok' : 'bad'}" title="限时可用：${esc(schWins)}${sch.open ? '（当前开放）' : '（当前关闭，窗口外调用被拒）'}">⏰ ${sch.open ? '开放中' : '限时'}</span>`
        : '';
      const schWinsAttr = sch ? esc(JSON.stringify(sch.windows)) : '[]';
      const dim = dis ? ' style="opacity:.5"' : '';
      const ord = m.order || null;
      const ordMarks = ord ? ord.marks : [];
      const ordTitle = ord
        ? '候选上游顺序：' + ord.targets.join(' → ')
          + (ordMarks.length ? '；冷却中：' + ordMarks.map(k => k.target).join(', ') : '')
        : '';
      const ordTag = ord
        ? `<span class="tag ${ordMarks.length ? 'bad' : 'ok'}" title="${esc(ordTitle)}">⇄ ${ord.targets.length}${ordMarks.length ? ` <span style="cursor:pointer" title="冷却中，点击清除" onclick='clearOrderMarks(${jsq(g.id)},${jsq(m.id)})'>⏸${ordMarks.length}</span>` : ''}</span>`
        : '';
      return `<tr>
        <td${dim}><span class="mono">${esc(bareModelId(m.id, g.id))}</span>
            ${_copyBtnHtml(g.id, bareModelId(m.id, g.id))}
            ${m.is_default ? '<span class="badge-default">✦ 默认</span>' : ''}
            ${dis ? '<span class="tag bad">已停用</span>' : ''}
            ${schTag}
            ${ordTag}</td>
        <td class="muted"${dim}>${esc(m.name || '')}</td>
        <td${dim}>${m.credits != null ? `<span class="tag">${esc(m.credits)}</span>` : ''}${m.support ? `<span class="tag" title="受账号限制，仅支持：${esc((m.support.accounts || []).join('、'))}（转发时自动跳过不支持的账号）">部分账号</span>` : ''}${m.tier ? `<span class="tag">${esc(m.tier)}</span>` : ''}${m.reasoning ? '<span class="tag">reasoning</span>' : ''}</td>
        <td class="num mono muted"${dim}>${st.count || '—'}</td>
        <td class="num mono muted"${dim}${_recentMsTitle(st)}>${st.recent_count && st.recent_avg_ms != null ? fmtMs(st.recent_avg_ms) : '—'}</td>
        <td class="num" style="white-space:nowrap">
            ${(m.is_default || dis) ? '' : `<button title="客户端请求未带 model 字段时，自动改用此模型（不影响已指定 model 的请求）" onclick='setDefault(${jsq(g.id)},${jsq(m.id)})'>设为默认</button>`}
            <button class="primary" onclick='runTest(${jsq(g.id)},${jsq(bareModelId(m.id, g.id))})'>测试</button>
            <button class="ghost" title="设置限时可用时段：仅在窗口内可调用，窗口外直接失败（支持跨天）" onclick='openScheduleModal(${jsq(g.id)},${jsq(m.id)},${schWinsAttr})'>${sch ? '时段·' + sch.windows.length : '时段'}</button>
            <!-- 候选顺序的编辑入口只在「模型顺序」页签：这张表行多列窄，塞进去
                 既挤又难找（用户当初就是在这一列里找不到它）。行内只留一个
                 徽标，说明「这个模型已配顺序、有几档、几档在冷却」。 -->
            ${ordMarks.length ? `<button class="ghost" title="清除冷却标记，立刻重新尝试这些目标" onclick='clearOrderMarks(${jsq(g.id)},${jsq(m.id)})'>清冷却</button>` : ''}
            <button class="${dis ? 'primary' : 'ghost'}" title="${dis ? '重新启用后可正常调用' : '停用后调用此模型的请求直接失败'}" onclick='toggleModel(${jsq(g.id)},${jsq(m.id)},${dis ? 'false' : 'true'})'>${dis ? '启用' : '停用'}</button>
            <button class="ghost" title="只从模型列表和选择器隐藏；直接点名仍可调用" onclick='hideModel(${jsq(g.id)},${jsq(m.id)})'>隐藏</button>
        </td></tr>`;
    }).join('');
    const gdis = !!g.disabled;
    const isDefaultProv = g.id === 'codebuddy';
    const swTitle = isDefaultProv ? '默认兜底通道，不能停用'
      : (gdis ? '重新启用该通道' : '停用该通道：调用直接失败，额度页不再展示，顺序页模型置灰');
    return `<div class="group${!searching && COLLAPSED.has(g.id) ? ' collapsed' : ''}" data-gid="${esc(g.id)}"${gdis ? ' style="opacity:.55"' : ''}>
      <div class="group-head" title="点击折叠/展开">
        <span class="dot" style="background:${gdis ? 'var(--muted)' : (okFlag ? 'var(--ok)' : 'var(--err)')}"></span>
        <span class="name">${esc(g.name)}</span>
        <span class="tag">${g.id}</span>
        ${gdis ? '<span class="tag bad">已停用</span>' : ''}
        <span class="tag ${okFlag ? 'ok' : 'bad'}">${healthTxt}</span>
        <label style="display:flex;align-items:center;gap:4px;font-size:12px;margin-left:6px;cursor:${isDefaultProv ? 'not-allowed' : 'pointer'}" title="${swTitle}" onclick="event.stopPropagation()">
          <input type="checkbox" ${gdis ? '' : 'checked'} ${isDefaultProv ? 'disabled' : ''} onchange='toggleProvider(${jsq(g.id)}, this.checked)'> 启用
        </label>
        <span class="spacer"></span>
        ${g.refreshable ? `<button class="ghost" title="从上游官方/逆向目录强制重新拉取，发现新增模型" onclick='event.stopPropagation();refreshProviderModels(${jsq(g.id)},this)'>↻ 重新拉取</button>` : ''}
        <span class="muted" style="font-size:12px">${g.models.length} 个模型</span>
        <span class="chev">▾</span>
      </div>
      <table>
        <thead><tr><th>模型 ID</th><th>名称</th><th>标签</th>
          <th class="num" title="本代理转发的、近 14 天的成功+失败请求数。按实际转发的通道和模型名统计（不是按客户端原始请求里的名字），最久保留 30 天。">近 14 天请求</th>
          <th class="num" title="取最近 3 次有耗时的实测（悬停看逐次明细）。14 天均值在模型换代、上游调参后和当下体感差得太远，所以这里只看近几次">近 3 次耗时</th><th class="num">操作</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>`;
  }).join('');
  refreshStickyVars();
}

let RECENT_PAGE = 1, RECENT_SIZE = 20;
let RECENT_SEQ = 0;  // 并发防抖：仅最后一次请求的结果生效（范围/翻页快速切换时）
let RECENT_QS = null;  // 已渲染结果对应的查询串；相同则跳过（多次 render 不重复打请求）

function recentGo(p) { RECENT_PAGE = p; renderRecent(); }
function recentSize(v) { RECENT_SIZE = +v; RECENT_PAGE = 1; renderRecent(); }

// 页码序列：首尾常驻，当前页 ±1，间隔以 '…' 占位
function recentPages(cur, total) {
  const wanted = new Set([1, total, cur - 1, cur, cur + 1]);
  const pages = [...wanted].filter(p => p >= 1 && p <= total).sort((a, b) => a - b);
  const out = [];
  let prev = 0;
  for (const p of pages) {
    if (p - prev > 1) out.push('…');
    out.push(p);
    prev = p;
  }
  return out;
}

// 服务端分页：直接读 logs/metrics.jsonl + 30 天归档，突破进程内 200 条上限。
// 该接口最慢（要全量扫归档），故对「相同查询」直接跳过（RECENT_QS），
// 并在飞请求由 api() 的 GET 去重兜住。
async function renderRecent() {
  // 日志页不可见时直接跳过：翻页/筛选/范围变化都可能调到这里，别在其它页签白跑。
  if (currentTab() !== 'log') return;
  // 头部饼图与本表同源同范围，一起刷新（它读内存计数表，不扫磁盘，开销可忽略）
  loadModelMix();
  const el = document.getElementById('recent');
  const pager = document.getElementById('recent-pager');
  document.getElementById('recent-sub').textContent =
    `${rangeLabel()} · 读磁盘日志（最多 30 天归档），服务端分页；TTFT 为首 token 延迟（流式）`;
  let qs = `start=${encodeURIComponent(RANGE.start)}&end=${encodeURIComponent(RANGE.end)}` +
           `&page=${RECENT_PAGE}&page_size=${RECENT_SIZE}`;
  if (LOG_FILTERS.provider.size)
    qs += `&provider=${encodeURIComponent([...LOG_FILTERS.provider].join(','))}`;
  if (LOG_FILTERS.model.size)
    qs += `&model=${encodeURIComponent([...LOG_FILTERS.model].join(','))}`;
  if (LOG_FILTERS.client.size)
    qs += `&client=${encodeURIComponent([...LOG_FILTERS.client].join(','))}`;

  // 同一查询已渲染过 → 直接跳过。首屏/切页签/轮询会多次 render(),
  // 该接口要全量扫归档，重复请求代价最高。
  if (qs === RECENT_QS) return;

  const seq = ++RECENT_SEQ;
  let resp;
  try {
    resp = await api('/ui/api/logs?' + qs);
  } catch (e) {
    if (seq !== RECENT_SEQ) return;  // 已被更晚的请求取代
    // 与饼图同款：404/Not Found 基本就是「进程没重启，路由还不存在」，
    // 把这句话说出来，比把后端原话直接铺在表里有用得多。
    const gone = /not found|404/i.test(e.message || '');
    el.innerHTML = `<tr><td colspan="9" class="empty">加载失败：${esc(e.message)}` +
      (gone ? '<br><span class="muted" style="font-size:12px">后端没有这个接口——改完代码后需<b>重启 proxy 进程</b>才会加载新路由。</span>' : '') +
      '</td></tr>';
    pager.innerHTML = '';
    return;
    // 失败路径不记 RECENT_QS：下次渲染（轮询/切回页签）应当重试，而不是拿着旧视图空转
  }
  if (seq !== RECENT_SEQ) return;  // 已被更晚的请求取代
  RECENT_QS = qs;  // 记录已渲染的查询。所有 return 分支都在此行之后，
                   // 故「空结果 / 无行」也照样记，不会切走再回来白重跑一次全量扫描
  const rows = resp.rows || [];
  const total = resp.total || 0;
  const pages = resp.pages || 1;
  RECENT_PAGE = resp.page || 1;  // 后端已收敛到合法区间
  // 客户端筛选候选随日志数据刷新（按出现次数降序，不随已选筛选收缩）
  RANGE_CLIENTS = resp.clients || [];
  renderLogFilters();
  if (!rows.length) {
    const filtered = LOG_FILTERS.provider.size || LOG_FILTERS.model.size || LOG_FILTERS.client.size;
    el.innerHTML = `<tr><td colspan="9" class="empty">${
      filtered ? '当前筛选条件下没有请求记录' : (total ? '所选范围内没有请求记录' : '还没有请求记录')}</td></tr>`;
    pager.innerHTML = '';
    return;
  }
  const cmap = (STATS || {}).credits_map || {};
  el.innerHTML = rows.map(r => {
    const bad = (r.status >= 400 || r.error);
    const ttft = r.ttft_ms != null ? fmtMs(r.ttft_ms) : '—';
    const hasTok = (r.prompt_tokens || r.completion_tokens || r.cached_tokens);
    let tok = '—', tokTitle = '';
    if (hasTok) {
      const cacheTxt = r.cached_tokens ? ` · 其中缓存命中 ${fmtNum(r.cached_tokens)}` : '';
      tok = `↑${fmtNum(r.prompt_tokens)} ↓${fmtNum(r.completion_tokens)}`;
      tokTitle = `输入 ${r.prompt_tokens}（↑ 含缓存命中）· 输出 ${r.completion_tokens}${cacheTxt}`;
    }
    // 实扣积分留 2 位展示；credits_map 那档本来就是 "×1.62" 这种字符串，原样带过
    const creditTxt = r.credit != null ? fmtCredit(r.credit)
                    : cmap[r.provider + '/' + r.model] || null;
    // 多账号通道：括号内展示账号名。日志落库的是稳定 id，后端在 /ui/api/logs
    // 按各通道当前 alias 链解析成 account_name（改名后历史行跟着变）。解析不到
    // （trae 旧行存的当时 alias、或已删号）原样显示：UUID 形态截前 6 位、其余
    // 截 12 位，hover 看完整原始值。
    const shown = r.account_name || r.account;
    const acct = r.account
      ? ` <span class="muted acct-name" title="${r.account_name ? '账号 id：' : '账号：'}${esc(r.account)}">(${esc(shown)})</span>`
      : '';
    return `<tr>
      <td class="mono muted">${fmtTime(r.ts)}</td>
      <td><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${pcolor(r.provider)};margin-right:6px"></i><span class="mono muted">${esc(r.provider)}</span>${acct}</td>
      <td class="mono">${esc(r.model)}</td>
      <td class="mono muted" title="客户端来源：X-Client-Name 自声明 > UA 推断（旧记录无此字段）">${r.client ? esc(r.client) : '<span class="muted">—</span>'}</td>
      <td class="num mono">${r.ttft_ms != null ? ttft : '—'}</td>
      <td class="num mono">${r.duration_ms ? fmtMs(r.duration_ms) : '—'}</td>
      <td class="num mono" title="${esc(tokTitle)}">${tok}${r.cached_tokens ? ` <span class="tag">缓 ${fmtNum(r.cached_tokens)}</span>` : ''}</td>
      <td class="num mono">${r.credit != null ? (r.credit_estimated
          ? `<b style="color:var(--ok)" title="本地估算，非上游实扣：trae 已实测模型按官方账单校准的输入/输出单价×token（随倍率面板调价自动缩放；会话首请求实测偏高约 2~3 倍），未实测模型按倍率粗估；zcode 按 GLM Coding Plan 官方抵扣系数（含时段折扣）">≈${creditTxt}</b>`
          : `<b style="color:var(--ok)" title="上游实扣积分（原始值 ${esc(r.credit)}）">${creditTxt}</b>`)
        : (creditTxt ? `<span class="tag" title="仅倍率参考（×基准单价），非本次实际消耗；${esc(r.provider)} 不提供单次消耗">${esc(creditTxt)}</span>` : '<span class="muted" title="该上游未提供单次积分消耗">—</span>')}</td>
      <td>${bad ? `<span class="tag bad" title="${esc(r.error || '')}">${r.status || 'ERR'}</span>` : '<span class="tag ok">OK</span>'}</td>
    </tr>`;
  }).join('');
  const nav = (label, page, disabled) =>
    `<button class="ghost" ${disabled ? 'disabled' : ''} onclick="recentGo(${page})">${label}</button>`;
  pager.innerHTML =
    `<span class="muted">共 ${total} 条</span>` +
    `<select title="每页条数" onchange="recentSize(this.value)">` +
      [20, 50, 100].map(n => `<option value="${n}" ${n === RECENT_SIZE ? 'selected' : ''}>${n} 条/页</option>`).join('') +
    `</select>` +
    `<span class="spacer"></span>` +
    nav('«', 1, RECENT_PAGE === 1) +
    nav('‹', RECENT_PAGE - 1, RECENT_PAGE === 1) +
    recentPages(RECENT_PAGE, pages).map(p => p === '…'
      ? '<span class="muted">…</span>'
      : `<button class="${p === RECENT_PAGE ? 'primary' : 'ghost'}" onclick="recentGo(${p})">${p}</button>`).join('') +
    nav('›', RECENT_PAGE + 1, RECENT_PAGE === pages) +
    nav('»', pages, RECENT_PAGE === pages);
}
