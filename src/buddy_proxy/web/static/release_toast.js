// 右下角「有新版本」提示。
//
// 数据来自本机 /ui/api/release（服务端后台 git fetch），**浏览器不出网**——
// 国内直连 GitHub 又慢又不可靠，检测全交给本地进程。
//
// 轮询节奏（用户要求 5 分钟一次）：
//   - 页面可见时每 5 分钟问一次；切到后台标签页时浏览器会节流定时器，
//     这里再叠一层 visibilitychange：切回前台且距上次检查超过 5 分钟就补一次，
//     避免「挂着一下午回来还是旧状态」。
//   - 点过「不再提示」后写 localStorage，并且**停掉轮询**——用户明确说了不用
//     提醒，继续每 5 分钟打一次接口纯属浪费。
const RELEASE_POLL_MS = 5 * 60 * 1000;
// 本地记「已忽略」的键。值存的是被忽略的版本号而不是 true：这样忽略 2.0.5
// 之后，等 2.0.6 出来还能正常提示——用布尔值会让「不再提示」变成永久静音。
const RELEASE_MUTE_KEY = 'buddy_release_muted_version';
const RELEASE_SEEN_KEY = 'buddy_release_last_seen';

let RELEASE_TIMER = null;
let RELEASE_LAST_AT = 0;
// 当前弹窗对应的版本号。点「不再提示」时用它落 localStorage——从 DOM 里
// 反解析版本号不可靠（标题文案一改就取不到）。
let RELEASE_CURRENT_LATEST = '';
// 点「更新」那一刻服务端的版本号。更新后它应该变（或 has_update 变 false），
// 用它判断「重启完了没有」比等一个固定秒数可靠。
let RELEASE_CURRENT_VERSION = '';

// 提交标题来自 git，含 `<`/`&` 时直接塞进 innerHTML 会被当成标签。
// 仓库里没有全局 escapeHtml（benefits_runninghub.js 那份是函数内局部定义），
// 所以这里自带一个。
function releaseEsc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function releaseMuted() {
  try { return localStorage.getItem(RELEASE_MUTE_KEY) || ''; } catch (e) { return ''; }
}

function releaseMute(version) {
  try { localStorage.setItem(RELEASE_MUTE_KEY, version || ''); } catch (e) { /* 隐私模式 */ }
}

function releaseHide() {
  const el = document.getElementById('release-toast');
  if (el) el.style.display = 'none';
}

function releaseShow(data) {
  const el = document.getElementById('release-toast');
  if (!el) return;
  const latest = data.latest || '';
  // 已经点过「不再提示」的版本就别再弹（刷新页面也不该回来）
  if (latest && releaseMuted() === latest) { releaseHide(); return; }
  RELEASE_CURRENT_LATEST = latest;
  if (data.current) RELEASE_CURRENT_VERSION = data.current;

  const title = el.querySelector('.rel-title');
  const body = el.querySelector('.rel-body');
  const cur = el.querySelector('.rel-cur');
  const notes = Array.isArray(data.notes) ? data.notes : [];

  if (title) title.textContent = '🎉 有新版本 ' + (latest || '');
  if (cur) {
    cur.textContent = data.current ? ('当前 ' + data.current) : '';
  }
  if (body) {
    // 只列几点：commit 标题已经足够说明改了什么，不展开 diff
    const items = notes.slice(0, 8).map(n => '<li>' + releaseEsc(n) + '</li>').join('');
    const more = notes.length > 8 ? '<li class="rel-more">…</li>' : '';
    const behind = data.behind > notes.length && notes.length
      ? '<div class="rel-more">共 ' + data.behind + ' 项改动</div>' : '';
    body.innerHTML = items
      ? '<ul class="rel-notes">' + items + more + '</ul>' + behind
      : '<div class="rel-more">查看提交记录了解详情</div>';
  }
  el.style.display = 'block';
}

function releaseRender(data) {
  if (!data || !data.has_update) { releaseHide(); return; }
  releaseShow(data);
}

async function releaseCheck(force) {
  // 静默失败：版本检测只是锦上添花，断网/后端异常不该在页面上报错
  try {
    const data = await api('/ui/api/release' + (force ? '?refresh=1' : ''));
    RELEASE_LAST_AT = Date.now();
    if (data && data.current) {
      try { localStorage.setItem(RELEASE_SEEN_KEY, data.current); } catch (e) { /* ignore */ }
    }
    releaseRender(data);
    return data;
  } catch (e) {
    return null;
  }
}

// ---- 「更新」：真的拉代码 + 同步依赖 + 重启，然后自动刷新 --------------
//
// 以前的按钮只做 `location.reload()`——服务端代码根本没变，刷新后
// `/ui/api/release` 依然报「有新版」，弹窗又出现，看起来像按钮没生效。
//
// 现在的流程：POST /ui/api/release/update 让**服务端**脱离进程去跑
// `buddy update`（它会 kill 掉当前进程，所以接口只回 202 不等结果），
// 页面随后轮询 `/ui/api/release`，等到版本追平（或服务重启后恢复）就 reload。

//: 轮询间隔。更新包含 git pull + uv sync + 重启，本机实测十几秒量级；
//: 1.5 秒足够跟上，也不会把接口打穿（它只读缓存，本来就很轻）。
const RELEASE_UPDATE_POLL_MS = 1500;

//: 最多等多久。留足 `uv sync` 慢（装依赖）与重启健康的余量；超时不报失败，
//: 只提示「可能仍在进行」——真正的结果以版本号为准。
const RELEASE_UPDATE_TIMEOUT_MS = 180000;

let RELEASE_UPDATING = false;
// 点「更新」那一刻服务的 uptime（秒）。新进程从 0 起算，用它判断重启是否落地。
let RELEASE_UPTIME_BEFORE = Infinity;

function releaseSetFoot(text, note) {
  const foot = document.querySelector('#release-toast .rel-foot');
  if (!foot) return;
  if (!foot.dataset.orig) foot.dataset.orig = foot.innerHTML;
  if (!text) { foot.innerHTML = foot.dataset.orig; return; }
  foot.innerHTML = '<div class="rel-busy">' + releaseEsc(text) +
    (note ? '<div class="rel-more">' + releaseEsc(note) + '</div>' : '') + '</div>';
}

async function releaseRunUpdate(btn) {
  if (RELEASE_UPDATING) return;
  RELEASE_UPDATING = true;
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span> 更新中'; }
  // 先记基线 uptime：重启后新进程的 uptime 会小于它。
  // 拿不到就保持 Infinity（永不触发），退回超时提示，至少不会误刷新。
  try {
    const ov = await api('/ui/api/overview');
    if (ov && typeof ov.uptime_seconds === 'number') RELEASE_UPTIME_BEFORE = ov.uptime_seconds;
  } catch (e) { /* 记不到就不刷新，靠超时提示 */ }

  let started = false;
  try {
    const r = await api('/ui/api/release/update', { method: 'POST' });
    started = !!(r && r.started);
    releaseSetFoot('正在更新，完成后页面会自动刷新…', 'git pull → uv sync → 重启');
  } catch (e) {
    // 409 = 工作树脏。后端把 `git status --short` 原文带回来了，原样展示，
    // 用户才知道该处理哪几个文件（这条信息比「更新失败」有用得多）。
    releaseSetFoot('', null);
    if (btn && btn.isConnected) { btn.disabled = false; btn.textContent = old; }
    RELEASE_UPDATING = false;
    const box = document.querySelector('#release-toast .rel-body');
    if (box) box.innerHTML = '<div class="rel-error">更新未开始：<br>' +
      releaseEsc(e.message).replace(/\n/g, '<br>') + '</div>';
    return;
  }
  if (!started) { RELEASE_UPDATING = false; return; }
  releaseWaitForRestart();
}

async function releaseWaitForRestart() {
  const deadline = Date.now() + RELEASE_UPDATE_TIMEOUT_MS;
  // 基线没拿到（overview 当时不可用）就退回「版本号变了」判定。
  // **不能**让 Infinity 参与比较：那样 `uptime < Infinity` 恒真，第一次
  // 轮询就会刷新——更新还没开始就被当成完成了。
  const useUptime = Number.isFinite(RELEASE_UPTIME_BEFORE);
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, RELEASE_UPDATE_POLL_MS));
    let ov = null;
    try {
      ov = await api('/ui/api/overview');
    } catch (e) {
      // 服务正在重启 → 连不上是**预期**，继续等
      continue;
    }
    if (!ov) continue;
    if (useUptime) {
      // 用 uptime 判断「服务真的重启了」。
      //
      // **不能只看 has_update**：`installed_version()` 读的是磁盘上的
      // pyproject.toml，`git pull` 一完成它立刻就是新版本号——此时
      // `uv sync` 和重启都还没做，刷新页面拿到的仍是旧进程的旧代码，
      // 表现为「刷新了但行为没变」。uptime 只在**新进程**里归零，是唯一
      // 可靠的落地信号。
      if (typeof ov.uptime_seconds === 'number' &&
          ov.uptime_seconds < RELEASE_UPTIME_BEFORE) {
        location.reload();
        return;
      }
    } else {
      let data = null;
      try { data = await api('/ui/api/release?refresh=1'); } catch (e) { continue; }
      if (data && !data.has_update) { location.reload(); return; }
    }
  }
  releaseSetFoot('更新可能仍在进行', '刷新页面查看，或到终端跑 buddy update');
}

function releaseMuteCurrent() {
  releaseMute(RELEASE_CURRENT_LATEST);
  releaseHide();
  // 用户说了不用提醒：停掉轮询，别再每 5 分钟打一次接口
  if (RELEASE_TIMER) { clearInterval(RELEASE_TIMER); RELEASE_TIMER = null; }
}

function releaseStartPolling() {
  if (RELEASE_TIMER) return;
  // 已静音则连第一次都不查——省一次 git，也符合「别再提示」的语义
  if (releaseMuted()) return;
  releaseCheck(false);
  RELEASE_TIMER = setInterval(() => {
    if (document.hidden) return;          // 后台标签页不查，省一次请求
    releaseCheck(false);
  }, RELEASE_POLL_MS);
  // 切回前台且距上次超过一个周期就补查（浏览器会节流后台定时器）
  document.addEventListener('visibilitychange', () => {
    if (document.hidden || !RELEASE_TIMER) return;
    if (Date.now() - RELEASE_LAST_AT >= RELEASE_POLL_MS) releaseCheck(false);
  });
}

// 本脚本在 index.html 里最后加载，DOM 与 api() 都已就绪；仍用 readyState
// 兜底，避免将来被挪到 <head> 后静默失效。
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', releaseStartPolling);
} else {
  releaseStartPolling();
}
