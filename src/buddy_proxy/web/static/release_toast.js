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
