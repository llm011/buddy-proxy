const PCOLORS = { codebuddy: '#4f8cff', trae: '#a78bfa', zcode: '#3ecf8e', doubao: '#f0b429', kimi: '#4dd0e7', codex: '#10a37f' };
function pcolor(p) {
  if (PCOLORS[p]) return PCOLORS[p];
  let h = 0; for (const c of p) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h}, 62%, 62%)`;
}
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
// 安全嵌进单引号 HTML 属性里的 JS 字符串字面量。模型 id 来自上游目录，
// 不能假定不含引号；先 JSON 编码，再做 HTML entity 编码。
function jsq(s) { return esc(JSON.stringify(String(s ?? ''))); }
function fmtMs(ms) { return ms >= 1000 ? (ms/1000).toFixed(ms >= 10000 ? 0 : 1) + ' s' : ms + ' ms'; }
function fmtUptime(s) {
  if (s < 90) return s + ' 秒';
  if (s < 5400) return Math.round(s/60) + ' 分钟';
  if (s < 172800) return (s/3600).toFixed(1) + ' 小时';
  return Math.round(s/86400) + ' 天';
}
function fmtNum(n) {
  if (n == null) return '—';
  const x = Number(n);
  if (!isFinite(x)) return String(n);
  return x >= 100 ? Math.round(x).toLocaleString() : String(Math.round(x * 10) / 10);
}
// 积分只留 2 位小数展示：上游给的是完整浮点（0.008510259999999999 这种），
// 直接铺在表格里又长又晃眼。Qoder 官网自己也是 2 位（0.12 / 0.07），对齐它。
// 只截展示，JSONL 里存的仍是原值——精度丢了就没法再算总账。
function fmtCredit(v) {
  if (v == null) return null;
  const x = Number(v);
  return isFinite(x) ? x.toFixed(2) : String(v);
}
function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const pad = n => String(n).padStart(2, '0');
  return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function toast(msg, isErr) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.className = isErr ? 'err' : ''; el.style.display = 'block';
  clearTimeout(el._t); el._t = setTimeout(() => el.style.display = 'none', 2600);
}
