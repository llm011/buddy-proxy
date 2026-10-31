// ---- 模型分组表 ----
// 分组折叠状态（localStorage 持久化；30s 自动刷新重渲染后仍保持）
let COLLAPSED = new Set();
try { COLLAPSED = new Set(JSON.parse(localStorage.getItem('bp-collapsed') || '[]')); } catch (e) {}
function persistCollapsed() {
  try { localStorage.setItem('bp-collapsed', JSON.stringify([...COLLAPSED])); } catch (e) {}
}
// 搜索只改变视图，不改 COLLAPSED（含 localStorage 中的偏好）。
let MODEL_QUERY = '';
function filteredModelGroups(groups, query) {
  const q = query.trim().toLowerCase();
  if (!q) return groups;
  const matches = value => String(value || '').toLowerCase().includes(q);
  return groups.flatMap(g => {
    if (matches(g.id) || matches(g.name)) return [g];
    const models = g.models.filter(m => matches(m.id) || matches(m.name));
    return models.length ? [{...g, models}] : [];
  });
}
function clearModelSearch() {
  const input = document.getElementById('model-search');
  input.value = '';
  MODEL_QUERY = '';
  const clearBtn = document.getElementById('model-search-clear');
  if (clearBtn) clearBtn.hidden = true;
  renderGroups();
  input.focus();
}
document.getElementById('model-search').addEventListener('input', e => {
  MODEL_QUERY = e.target.value.trim();
  const clearBtn = document.getElementById('model-search-clear');
  if (clearBtn) clearBtn.hidden = !e.target.value;
  renderGroups();
});
document.getElementById('model-search').addEventListener('keydown', e => {
  if (e.key === 'Escape') { e.preventDefault(); clearModelSearch(); }
});
document.getElementById('model-search-clear').addEventListener('click', clearModelSearch);
document.getElementById('groups').addEventListener('click', e => {
  if (e.target.closest('[data-clear-model-search]')) clearModelSearch();
});
function toggleGroup(gid) {
  if (MODEL_QUERY) return;
  if (COLLAPSED.has(gid)) COLLAPSED.delete(gid); else COLLAPSED.add(gid);
  persistCollapsed();
  const g = document.querySelector(`.group[data-gid="${CSS.escape(gid)}"]`);
  if (g) g.classList.toggle('collapsed', COLLAPSED.has(gid));
}
function setAllGroups(collapse) {
  if (MODEL_QUERY) return;
  COLLAPSED = collapse ? new Set((MODELS.groups || []).map(g => g.id)) : new Set();
  persistCollapsed();
  document.querySelectorAll('#groups .group').forEach(g =>
    g.classList.toggle('collapsed', COLLAPSED.has(g.dataset.gid)));
}
// 点击通道条或列头（表头）任意处 → 折叠/展开该组
document.getElementById('groups').addEventListener('click', e => {
  const g = e.target.closest('.group');
  if (!g) return;
  if (e.target.closest('.group-head') || e.target.closest('thead th')) {
    if (!e.target.closest('button')) toggleGroup(g.dataset.gid);
  }
});
