// GET 去重：同一 URL 在飞时复用 Promise。页面里多处 render 会重复触发同一接口
// （切页签 + 轮询 + 各区块渲染），重复请求会白读磁盘（/ui/api/logs 最重）。
// 仅对 GET 生效；带 opts（POST 等写操作）一律直通，避免误合并。
const GET_INFLIGHT = new Map();

// 从错误响应体里提取可读消息。
// FastAPI 的 `HTTPException(detail={...})` 会把 detail 原样包成 `{"detail": {...}}`，
// 而后端统一约定 detail 是 Anthropic 风格的嵌套对象 `{"error": {"message": ...}}`
// （全项目 60+ 处）。若只取 `body.detail` 会拿到**对象**，`new Error(对象)` 转成
// 字符串就是 `[object Object]`——用户看到「保存失败: [object Object]」看不出原因。
// 这里逐层剥：顶层 error.message → detail 里的 error.message → detail 是字符串 →
// detail.message → 兜底状态码。
function errMessage(body, status) {
  const pick = o => (o && typeof o === 'object' && typeof o.message === 'string') ? o.message : '';
  return pick(body)
    || (body && pick(body.error))
    || (body && body.detail && (pick(body.detail) || pick(body.detail.error)))
    || (typeof (body && body.detail) === 'string' ? body.detail : '')
    || (status + ' error');
}

async function api(path, opts) {
  const isGet = !opts || (!opts.method || opts.method.toUpperCase() === 'GET');
  if (isGet && GET_INFLIGHT.has(path)) return GET_INFLIGHT.get(path);
  const p = (async () => {
    const r = await fetch(path, opts);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(errMessage(body, r.status));
    return body;
  })();
  if (isGet) p.finally(() => GET_INFLIGHT.delete(path)).catch(() => {});
  return p;
}
