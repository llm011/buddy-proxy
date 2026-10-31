// ---- 额度页侧边导航 ----
// 条目 = 当前实际渲染出来的各通道卡（含打卡日历与各通道签到）：通道开关 /
// 登录状态一变，导航得跟着变，所以在 renderBenefits 尾部（所有面板重建后）重建。
// 只把「有内容」的面板列进去——空面板（未注册/停用通道）滚过去只有空白，没
// 意义；PAT 卡有自己完整的空态清空（renderTraepatPanel），逻辑上无特权。
// 高亮用 IntersectionObserver：视口里最靠上的已渲染卡片对应的条目亮着，
// 比纯 scroll 监听省事且滚动中实时跟随。观察器随条目一起重建（面板 DOM 换了
// 旧观察全失效，挂着只会白跑回调）。
function renderBenefitsNav() {
  const nav = document.getElementById('benefits-sidenav');
  if (!nav) return;
  const page = document.getElementById('page-benefits');
  const labels = [];
  const cards = [];
  // 打卡日历：页首第一张卡（grid2.top 里左栏那张）。选择器只匹配得到它
  // ——后面的面板卡都是动态 innerHTML 填充、不带 .chart-card 属性节点。
  const cal = page && page.querySelector('.grid2.top > .chart-card');
  if (cal) { labels.push(['打卡日历', 'cal']); cards.push(cal); }
  // 「各通道签到」全宽卡（DOM 位置在日历 grid 与通道面板之间）：也是页面
  // 主区块之一，滚过去要能一键回来看签到状态
  const ck = page && page.querySelector('.rows-card');
  if (ck && ck.querySelector('#checkin-rows')) {
    labels.push(['各通道签到', 'checkin']); cards.push(ck);
  }
  page.querySelectorAll('[id$="-panel"]').forEach(p => {
    if (!p.innerHTML.trim()) return;      // 未注册/停用的通道面板：整块空
    const pid = p.id.replace(/-panel$/, '');
    const shown = (BENEFITS.providers || []).find(x => x.id === pid && !x.disabled);
    if (!shown) return;                    // 停用通道：面板有历史 DOM 也不列
    labels.push([shown.name || pid, pid]);
    cards.push(p);
  });
  if (!labels.length) { nav.innerHTML = ''; _navDisconnect(); return; }
  // rail 标签剥掉通道名里的括注（「Antigravity (Gemini/Claude/GPT free tier)」→
  // 「Antigravity」）：导航宽度钉死 132px（style.css .sidenav），长名只会被
  // 省略号截断；完整名挂 title，悬停可见。循环剥到不动为止——单趟正则
  // 穿不透嵌套括号（「A (B (C))」会剩个悬空括号，review 指出）。
  const shortLabel = (l) => {
    let s = String(l), prev;
    do { prev = s; s = s.replace(/[（(][^()（）]*[)）]/g, ""); } while (s !== prev);
    return s.trim() || l;
  };
  nav.innerHTML = labels.map(([label, pid], i) =>
    `<a data-i="${i}" title="${esc(label)}"><span class="sn-dot" style="background:${pcolor(pid)}"></span><span class="sn-label">${esc(shortLabel(label))}</span></a>`).join('');
  // 点击不用 hash 跳转（会改地址栏且默认硬跳）。不用 scrollIntoView：
  // block:'start' 会把卡片顶到视口 0，被 sticky 的 .topbar（tab 头）盖住
  // 一截（2026-10-12 用户实报）——手动滚，目标位置扣掉 topbar 实际高度。
  // 滚动动画期间（~800ms）把高亮锁到目标卡——中途经过的卡片会轮流进
  // IntersectionObserver 的视口，不锁的话高亮会从上到下逐个闪过去。
  nav.querySelectorAll('a').forEach(a => {
    a.addEventListener('click', e => {
      e.preventDefault();
      const card = cards[+a.dataset.i];
      nav.querySelectorAll('a').forEach(x =>
        x.classList.toggle('active', x === a));
      const bar = document.querySelector('.topbar');
      const top = bar ? bar.getBoundingClientRect().bottom : 0;
      const y = card.getBoundingClientRect().top + window.scrollY - top - 6;
      _navLockUntil = Date.now() + 800;
      window.scrollTo({ top: Math.max(0, y), behavior: 'smooth' });
    });
  });
  // 滚动高亮：每张卡进入/离开视口时回调，取「离视口顶最近且已进入」的那张
  const active = new Map();  // card -> isIntersecting
  const io = new IntersectionObserver(items => {
    items.forEach(it => active.set(it.target, it.isIntersecting));
    if (Date.now() < _navLockUntil) return;  // 点击后的平滑滚动中：高亮已锁
    let best = null, bestTop = Infinity;
    active.forEach((on, card) => {
      if (!on) return;
      const top = card.getBoundingClientRect().top;
      if (top < bestTop) { bestTop = top; best = card; }
    });
    nav.querySelectorAll('a').forEach(a => {
      a.classList.toggle('active', cards[+a.dataset.i] === best);
    });
  }, { root: null, threshold: 0 });
  cards.forEach(c => io.observe(c));
  _navDisconnect();
  renderBenefitsNav._io = io;
}

let _navLockUntil = 0;

function _navDisconnect() {
  renderBenefitsNav._io && renderBenefitsNav._io.disconnect();
  renderBenefitsNav._io = null;
}
