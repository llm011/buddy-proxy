#!/usr/bin/env python
"""把豆包签到的「现场」一次性抓全并存档，避免每次排查都从零开始。

为什么需要这个脚本（2026-11-16 的真实教训）
-------------------------------------------
签到（``/alice/ug/campaign/landing/checkin``）从 2026-11-04 起恒回
``710010702 系统错误``，前后排查了四次，每次都重新手工拼 CDP 求值、重新
让用户在 App 里点一次按钮。更糟的是**每次结论都没落盘**：

- 11-08 的结论是「签到请求发自 drive-iframe 子 frame」；
- 11-12 的结论是「改回主聊天页 + 绝对 URL」（与 11-08 相反）；
- 11-13 的结论是「aid 用错」，验证依据写的是 `code: 0, idempotent: true`
  ——而 ``idempotent: true`` 恰恰表示「今天已经签过了」，用它验证真实领取
  是**假阳性**。

于是同一个坑反复踩：没有一份可复查的现场，后一次只能相信前一次的结论，
而前一次的结论本身可能是错的。

这个脚本解决的就是这件事：一条命令，把**能决定结论的所有事实**落到一个
JSON + 一份人读摘要里，下次直接从存档继续。

抓什么
------
1. 各 target 的清单（页面 / iframe / webview）与 CDP 端口；
2. 每个上下文里的登录态探针（``login_state_from_probe`` 三态）；
3. ``v3/detail/`` 的原始响应（签到状态真相）；
4. 页面 ``performance`` 里 App **自己**发出的 campaign 请求（含 query 全貌），
   用来确定「哪些参数是 App 真的在带、哪些是我们自己脑补的」；
5. 我们复刻的领取请求的**最终 URL**（关键：判断签名 ``msToken`` / ``a_bogus``
   有没有被页面的 mssdk 自动加上）、请求头、响应原文；
6. 若 ``--click``：真实鼠标事件点一次页面上的「立即签到」，并记录它自己
   发出的请求与响应——**这是唯一能区分「我们写错了」和「上游坏了」的证据**。

用法
----
    # 只读探查（不点按钮，安全）
    .venv/bin/python tools/capture_doubao_checkin.py

    # 额外点一次页面上的签到按钮（会在上游产生一次真实领取尝试）
    .venv/bin/python tools/capture_doubao_checkin.py --click

    # 指定输出与端口
    .venv/bin/python tools/capture_doubao_checkin.py --port 9223 --out /tmp/doubao.json

产物
----
``--out`` 指定的 JSON（默认 ``/tmp/doubao_checkin_capture_<时间戳>.json``），
含 ``conclusion`` 字段：脚本会自行判定当前属于哪一类故障，省得人肉比对。

⚠ 存档里**不含** cookie / token / msToken / a_bogus 明文：query 一律脱敏成
参数名集合 + 长度，只保留判定所需的事实（有没有签名、aid 是多少）。日志与
存档里不许出现凭据，这条在 AGENTS.md 里。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.request
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from buddy_proxy.doubao import checkin  # noqa: E402
from buddy_proxy.doubao.ws import _WS  # noqa: E402

#: 凭据类参数，存档里一律不落明文。
_SECRET_PARAMS = {"msToken", "a_bogus", "X-Bogus", "fp", "web_id", "tea_uuid",
                  "device_id", "web_tab_id", "sessionid", "sid_tt", "uid_tt"}

#: 在页面文档创建**之前**注入。必须在 reload 前装，否则页面自己的请求
#: （尤其是它自动补的签名）已经发完，抓不到。
_HOOK_SOURCE = """
(() => {
  if (window.__buddy_rec) return;
  window.__buddy_rec = [];
  const push = o => { try { window.__buddy_rec.push(o); } catch (e) {} };
  const interesting = u => typeof u === 'string' && u.includes('/alice/ug/campaign');

  const of = window.fetch;
  window.fetch = function (input, init) {
    let u = typeof input === 'string' ? input : (input && input.url);
    const body = init && init.body ? String(init.body).slice(0, 2000) : '';
    const p = of.apply(this, arguments);
    if (interesting(u)) {
      p.then(r => r.clone().text().then(t => push({
        kind: 'fetch', url: u, reqBody: body, status: r.status,
        resp: t.slice(0, 1200),
      })).catch(() => {})).catch(() => {});
    }
    return p;
  };

  const oo = XMLHttpRequest.prototype.open;
  const os = XMLHttpRequest.prototype.send;
  const sr = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function (m, u) {
    this.__m = m; this.__u = u; this.__hdrs = {};
    return oo.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    try { this.__hdrs[k] = String(v).slice(0, 300); } catch (e) {}
    return sr.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (b) {
    if (interesting(this.__u)) {
      this.addEventListener('load', () => push({
        kind: 'xhr', url: this.__u, reqBody: b ? String(b).slice(0, 2000) : '',
        status: this.status, resp: String(this.responseText).slice(0, 1200),
        hdrs: this.__hdrs,
      }));
    }
    return os.apply(this, arguments);
  };
})()
"""

#: 从页面 performance 里读 App 自己发出的 campaign 请求。**带签名**（不剥），
#: 因为「App 到底有没有签名」本身就是待验证的事实。
_PERF_JS = """
(() => JSON.stringify(
  performance.getEntriesByType('resource')
    .map(e => e.name)
    .filter(n => n.includes('/alice/ug/campaign'))
))()
"""


def _sanitize_query(url: str) -> dict[str, Any]:
    """query → 可安全落盘的形状（保留判定事实，抹掉凭据明文）。"""
    if not isinstance(url, str) or "?" not in url:
        return {"params": [], "has_msToken": False, "has_a_bogus": False}
    query = url.split("?", 1)[1]
    pairs = parse_qsl(query, keep_blank_values=True)
    names = sorted({k for k, _ in pairs})
    return {
        "params": names,
        "param_count": len(names),
        "has_msToken": any(k == "msToken" for k in names),
        "has_a_bogus": any(k == "a_bogus" for k in names),
        "aid": next((v for k, v in pairs if k == "aid"), None),
        "doubao_device_platform": next(
            (v for k, v in pairs if k == "doubao_device_platform"), None),
        "web_platform": next((v for k, v in pairs if k == "web_platform"), None),
        "pc_version": next((v for k, v in pairs if k == "pc_version"), None),
        "origin_path": urlsplit(url).path,
    }


def _json_url(port: int, path: str) -> Any:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return json.loads(resp.read())


class Page:
    """一个 target 的 CDP 求值上下文（与 ``doubao.checkin.CheckinPage`` 同构）。"""

    def __init__(self, port: int, target: dict[str, Any]):
        self.target = target
        self._ws = _WS("127.0.0.1", port,
                       target["webSocketDebuggerUrl"].split(f":{port}", 1)[1])

    def evaluate(self, expr: str, timeout: float = 30.0) -> Any:
        result = self._ws.cmd("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
        }, timeout=timeout).get("result", {})
        if "exceptionDetails" in result:
            return {"ERROR": str(result["exceptionDetails"])[:400]}
        return result.get("result", {}).get("value")

    def cmd(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._ws.cmd(method, params or {})

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响已采集的证据
            pass


def _probe_target(port: int, target: dict[str, Any], *, reload: bool) -> dict[str, Any]:
    """采一个上下文的全部事实。任何一步失败都只记录、不中断整体采集。"""
    out: dict[str, Any] = {
        "type": target.get("type"),
        "url": target.get("url"),
        "id": target.get("id"),
    }
    try:
        page = Page(port, target)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"connect failed: {type(exc).__name__}"
        return out

    try:
        try:
            page.cmd("Page.enable")
            page.cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _HOOK_SOURCE})
        except Exception as exc:  # noqa: BLE001
            out["hook_error"] = type(exc).__name__

        if reload:
            out["reloaded"] = True
            try:
                page.evaluate("location.reload()", timeout=10)
            except Exception:  # noqa: BLE001 - reload 本身会让求值中断
                pass
            # 导航会销毁执行上下文，旧 CDP 连接随之中断（Broken pipe）。
            # 必须重连，否则后面每一步都撞在死 socket 上。
            page.close()
            time.sleep(10)
            try:
                fresh = next((t for t in _json_url(port, "/json/list")
                              if t.get("id") == target.get("id")), None)
            except Exception:  # noqa: BLE001
                fresh = None
            if fresh is None or not fresh.get("webSocketDebuggerUrl"):
                out["error"] = "target disappeared after reload"
                return out
            page = Page(port, fresh)

        out["href"] = page.evaluate("location.href")
        out["has_bdms"] = page.evaluate("typeof window.bdms")
        out["has_acrawler"] = page.evaluate("typeof window.byted_acrawler")

        # App 自己发过的 campaign 请求（含签名与否）
        perf = page.evaluate(_PERF_JS)
        try:
            urls = json.loads(perf) if isinstance(perf, str) else []
        except ValueError:
            urls = []
        out["app_requests"] = [_sanitize_query(u) for u in urls[-12:]]

        # 登录探针三态
        try:
            status, data, body = checkin.CheckinPage.request(
                page, checkin._LOGIN_PROBE, {}, 20)  # type: ignore[arg-type]
            out["login"] = {
                "http": status,
                "state": checkin.login_state_from_probe(status, data, body),
                "body_head": (body or "")[:200],
            }
        except Exception as exc:  # noqa: BLE001
            out["login"] = {"error": type(exc).__name__}

        # 签到状态真相
        try:
            page.query = checkin._PAGE_QUERY
            status, data, body = checkin.CheckinPage.request(
                page, checkin._DETAIL_PATH,  # type: ignore[arg-type]
                {"campaign_group_code": checkin._CAMPAIGN_GROUP}, 25,
                daily_task_only=True)
            task = None
            if isinstance(data, dict):
                tasks = (data.get("data") or {}).get("tasks") or []
                task = tasks[0] if tasks else None
            out["detail"] = {
                "http": status,
                "code": data.get("code") if isinstance(data, dict) else None,
                "daily_checkin": (task or {}).get("daily_checkin"),
                "task_status": (task or {}).get("task_status"),
            }
        except Exception as exc:  # noqa: BLE001
            out["detail"] = {"error": type(exc).__name__}

    finally:
        try:
            recs = page.evaluate("JSON.stringify(window.__buddy_rec || [])")
        except Exception:  # noqa: BLE001
            recs = None
        try:
            out["page_requests"] = [_sanitize_record(r)
                                    for r in (json.loads(recs) if isinstance(recs, str) else [])]
        except ValueError:
            out["page_requests"] = []
        page.close()

    return out


def _sanitize_record(rec: Any) -> dict[str, Any]:
    """把一条抓到的请求脱敏后再存档。

    为什么必须逐条洗（2026-11-16 的疏漏）：``_probe_target`` 那条路径存的是
    ``_sanitize_query`` 的结果（已脱敏），但 ``--click`` 抓到的是**页面原文**，
    直接 ``json.dump`` 就把 ``web_tab_id`` / ``device_id`` / ``tea_uuid`` 等
    标识符明文带进了仓库。AGENTS.md 要求凭据/标识一律不落明文，所以两条路径
    必须共用同一套规则。

    保留的判定事实：路径、方法、状态码、请求体（body 本来就不含凭据）、
    响应原文（上游错误码是我们最需要的信息）、query 里**有哪些参数**。
    """
    if not isinstance(rec, dict):
        return {}
    url = rec.get("url") or ""
    body = rec.get("resp") or rec.get("r") or ""
    return {
        "kind": rec.get("kind") or rec.get("k"),
        "path": urlsplit(url).path if url else "",
        "query": _sanitize_query(url),
        "req_body": (rec.get("reqBody") or rec.get("b") or "")[:800],
        "status": rec.get("status") if "status" in rec else rec.get("s"),
        "resp": (body or "")[:900],
    }


def _replay_claim(port: int, target: dict[str, Any]) -> dict[str, Any]:
    """复刻一次领取，并**记录最终 URL**，用来判断签名有没有被自动补上。

    这是整个排查里最关键的一步：只看响应体分不清「参数不对」和「没签名」，
    而最终 URL 里 ``msToken`` / ``a_bogus`` 的有无是可直接观测的事实。
    """
    page = Page(port, target)
    try:
        q = checkin._PAGE_QUERY
        expr = """(async () => {
          let finalUrl = '';
          const oo = XMLHttpRequest.prototype.open;
          XMLHttpRequest.prototype.open = function (m, u) {
            finalUrl = u;
            return oo.apply(this, arguments);
          };
          const res = await new Promise(resolve => {
            const x = new XMLHttpRequest();
            x.open('POST', 'https://www.doubao.com%s' + %s, true);
            x.setRequestHeader('Content-Type', 'application/json');
            x.setRequestHeader('Agw-Js-Conv', 'str');
            x.onload = () => resolve(x.status + '\\n' + String(x.responseText));
            x.onerror = () => resolve('0\\nerror');
            x.send(JSON.stringify({scene: '%s'}));
          });
          XMLHttpRequest.prototype.open = oo;
          return JSON.stringify({finalUrl, res});
        })()""" % (checkin._CLAIM_PATH, json.dumps(q), checkin._CAMPAIGN_GROUP)
        raw = page.evaluate(expr, timeout=40)
        payload = json.loads(raw) if isinstance(raw, str) else {}
        final_url = payload.get("finalUrl") or ""
        res = payload.get("res") or ""
        return {
            "final_url": _sanitize_query(final_url),
            "signature_auto_added": _sanitize_query(final_url).get("has_msToken"),
            "status": res.split("\n", 1)[0],
            "body": res.split("\n", 1)[1][:600] if "\n" in res else res[:600],
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__, "detail": str(exc)[:200]}
    finally:
        page.close()


def _click_checkin(port: int, target: dict[str, Any]) -> dict[str, Any]:
    """用真实鼠标事件点一次「立即签到」。

    为什么必须是真实鼠标事件：``element.click()`` 与 CDP 派发的
    ``Input.dispatchMouseEvent`` 走的是不同路径，某些埋点/风控只看后者。
    要证明「上游对真人操作也失败」，只能用前者之外的方式点。

    ⚠ 钩子必须**在当前连接上直接注入，不要 reload**。两条教训都踩过
    （2026-11-16）：

    - ``Page.addScriptToEvaluateOnNewDocument`` 的脚本绑定在 CDP **session**
      上。reload 导致旧 ws 断开、session 销毁，脚本随之失效 —— 重连后
      ``typeof window.__buddy_rec === 'undefined'``，requests 恒为空，
      看起来像「页面根本没发请求」，实际是没装钩子。
    - 页面已经加载完也没关系：钩子装好**之后**发生的点击请求照样会被记录，
      而我们要抓的正是点击那一刻发出的请求。

    所以顺序是：注入钩子 → 校验钩子存在 → 点按钮 → 读记录。校验那一步不能
    省，否则「没有记录」会被误读成「没有请求」。
    """
    page = Page(port, target)
    try:
        hooked = page.evaluate(
            "(() => { %s; return typeof window.__buddy_rec; })()" % _HOOK_SOURCE)
        if hooked != "object":
            return {"clicked": False,
                    "reason": f"request hook not installed (got {hooked!r})"}

        box = page.evaluate("""(() => {
          const b = [...document.querySelectorAll('button')]
            .find(x => /立即签到/.test(x.textContent || ''));
          if (!b) return '';
          b.scrollIntoView({block: 'center'});
          const r = b.getBoundingClientRect();
          return JSON.stringify({x: r.x + r.width / 2, y: r.y + r.height / 2});
        })()""")
        if not box or not isinstance(box, str):
            return {"clicked": False, "reason": "no 立即签到 button"}
        pos = json.loads(box)
        page.cmd("Page.bringToFront")
        for method, params in [
            ("Input.dispatchMouseEvent",
             {"type": "mouseMoved", "x": pos["x"], "y": pos["y"], "button": "none"}),
            ("Input.dispatchMouseEvent",
             {"type": "mousePressed", "x": pos["x"], "y": pos["y"],
              "button": "left", "clickCount": 1}),
            ("Input.dispatchMouseEvent",
             {"type": "mouseReleased", "x": pos["x"], "y": pos["y"],
              "button": "left", "clickCount": 1}),
        ]:
            page.cmd(method, params)
        time.sleep(8)
        recs = page.evaluate("JSON.stringify(window.__buddy_rec || [])")
        toast = page.evaluate(
            "(document.body.innerText.match(/签到[^\\n]{0,26}/g) || []).slice(-3).join(' | ')")
        return {
            "clicked": True,
            "toast": toast,
            "requests": [_sanitize_record(r)
                         for r in (json.loads(recs) if isinstance(recs, str) else [])],
        }
    except Exception as exc:  # noqa: BLE001
        return {"clicked": False, "error": type(exc).__name__, "detail": str(exc)[:200]}
    finally:
        page.close()


def _conclude(capture: dict[str, Any]) -> str:
    """根据采集到的事实给出结论，省得下次再人肉比对。

    判定顺序刻意固定：先看「是不是我们自己的问题」，再谈上游。没有这一层
    区分，每次都会得出「上游坏了」这种无法行动的结论。
    """
    contexts = capture.get("contexts") or []
    replay = capture.get("replay") or {}
    click = capture.get("click") or {}

    claim_bodies = " ".join(
        (r.get("resp") or "") for c in contexts for r in (c.get("page_requests") or [])
        if "checkin" in (r.get("path") or ""))
    app_claim_bad = "710010702" in claim_bodies

    # 先看「我们自己的请求」有没有到位的证据，再谈上游。顺序不能反：一上来
    # 就说「上游坏了」是这次事故里最误导人的结论形式，它无法行动。
    if replay.get("signature_auto_added") is False and not click.get("clicked"):
        return ("我们复刻的领取请求**没有带上签名**（最终 URL 无 msToken/a_bogus）。"
                "这是 ``Runtime.evaluate`` 里构造请求的固有差异，不一定是根因"
                "（带上签名后实测同样失败，见 docs/doubao-checkin-investigation.md）。"
                "要判定上游是否也坏，请加 --click 拿页面官方代码的对照。")

    if click.get("clicked"):
        click_recs = click.get("requests") or []
        click_bad = "710010702" in json.dumps(click_recs)
        click_ok = any(
            "checked_in" in (r.get("resp") or "") and "710010702" not in (r.get("resp") or "")
            for r in click_recs if "checkin" in (r.get("path") or ""))
        if click_ok:
            return ("App 自己的签到按钮**成功**，而我们的领取失败 → 差异在我们这边。"
                    "对比 click.requests 与 replay 的请求，定位参数/上下文差异。")
        if click_bad:
            return ("**App 自己的签到按钮也失败**（同样的 710010702）。"
                    "说明问题在上游服务端或账号风控，不在代理的参数/上下文。"
                    "继续调参数没有意义：要么换干净账号验证风控，要么等上游恢复。")
        return ("点击了但没抓到签到请求，可能是按钮状态不允许点（今天已签）。"
                "换一个今天未签的账号重试。")


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取豆包签到现场并落盘")
    parser.add_argument("--port", type=int, default=9223, help="豆包工作 CDP 端口")
    parser.add_argument("--out", default="", help="存档 JSON 路径")
    parser.add_argument("--click", action="store_true",
                        help="额外真实点击一次「立即签到」（会在上游产生领取尝试）")
    parser.add_argument("--no-reload", action="store_true",
                        help="不重载页面（重载才能装上请求钩子）")
    args = parser.parse_args()

    try:
        targets = _json_url(args.port, "/json/list")
    except Exception as exc:  # noqa: BLE001
        print(f"连不上 CDP 端口 {args.port}：{exc}", file=sys.stderr)
        print("提示：豆包工作 App 需以 --remote-debugging-port 启动"
              "（代理启动时会自动加）", file=sys.stderr)
        return 2

    capture: dict[str, Any] = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "port": args.port,
        "targets": [{k: t.get(k) for k in ("id", "type", "url")} for t in targets],
        "contexts": [],
        "replay": {},
        "click": {},
    }

    # 只有豆包域名的上下文能发同源请求；主聊天页（chrome://）用于看签名机制。
    candidates = [t for t in targets
                  if t.get("type") in ("page", "iframe", "webview")
                  and t.get("webSocketDebuggerUrl")]
    for target in candidates:
        ctx = _probe_target(args.port, target, reload=not args.no_reload)
        capture["contexts"].append(ctx)
        print(f"[{ctx.get('type')}] {(ctx.get('url') or '')[:70]}"
              f" login={ctx.get('login', {}).get('state')}"
              f" detail_code={ctx.get('detail', {}).get('code')}")

    # 复刻领取：优先同源 iframe，其次落地页
    replay_target = next(
        (t for t in candidates if t.get("type") == "iframe"), None) or next(
        (t for t in candidates if "www.doubao.com" in (t.get("url") or "")), None)
    if replay_target is not None:
        capture["replay"] = _replay_claim(args.port, replay_target)
        print(f"[replay] signature_auto_added="
              f"{capture['replay'].get('signature_auto_added')}")

    if args.click:
        click_target = next(
            (t for t in candidates
             if (t.get("url") or "").startswith("https://www.doubao.com/pc/agent-explore")),
            None)
        if click_target is None:
            capture["click"] = {"clicked": False, "reason": "no agent-explore page"}
        else:
            capture["click"] = _click_checkin(args.port, click_target)
            print(f"[click] clicked={capture['click'].get('clicked')} "
                  f"toast={capture['click'].get('toast')}")

    capture["conclusion"] = _conclude(capture)

    out = args.out or (
        "/tmp/doubao_checkin_capture_"
        + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json")
    pathlib.Path(out).write_text(
        json.dumps(capture, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n" + "=" * 60)
    print("结论：", capture["conclusion"])
    print("存档：", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
