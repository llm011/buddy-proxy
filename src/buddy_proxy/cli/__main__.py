"""``python -m buddy_proxy.cli``：buddy 的本地只读子命令入口。

参数全部在这里集中定义，而不是各模块自己 argparse：`buddy` 是 bash 脚本，
它把 ``"$@"`` 原样转给这里，所以「哪些参数叫什么」只有这一处真相；以前
``buddy login`` 的 python 侧和 shell 侧各有一份 provider 白名单，加通道时漏改
一边就被拦「未知参数」，正是这种双份定义的老毛病。
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-color", action="store_true", help="不输出颜色")
    p.add_argument("--json", action="store_true", help="输出 JSON（脚本用）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="buddy",
        description="buddy-proxy 的本地查询命令（额度 / 日志统计 / 体检）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    usage = sub.add_parser("usage", help="按通道/账号查看套餐余量与到期时间")
    usage.add_argument("-p", "--provider", action="append", default=[],
                       metavar="ID", help="只看指定通道（可重复/逗号分隔）")
    usage.add_argument("-a", "--account", action="append", default=[],
                       metavar="#N", help="只看指定账号序号（可重复/逗号分隔）")
    usage.add_argument("--refresh", nargs="?", const=True, default=None,
                       metavar="ID", help="强刷上游（只对指定通道；否则本机首个）")
    usage.add_argument("--all", action="store_true", help="连没有额度的通道也列出来")
    usage.add_argument("--list-providers", action="store_true", help="列出可用的通道 id")
    usage.add_argument("--base-url", default="", help="管理接口地址（默认 127.0.0.1:<port>）")
    _add_common(usage)

    logs = sub.add_parser("logs", help="请求日志统计（默认）+ 明细")
    logs.add_argument("-n", "--limit", type=int, default=20,
                      help="末尾展示的明细条数，0 = 不展示（默认 20）")
    logs.add_argument("--days", type=int, default=None, help="最近 N 天（默认全部保留窗口）")
    logs.add_argument("--today", action="store_true", help="只看今天")
    logs.add_argument("--start", default=None, metavar="YYYY-MM-DD")
    logs.add_argument("--end", default=None, metavar="YYYY-MM-DD")
    logs.add_argument("-p", "--provider", action="append", default=[],
                      metavar="ID", help="按通道过滤")
    logs.add_argument("-m", "--model", action="append", default=[],
                      metavar="NAME", help="按模型过滤（精确匹配，可重复）")
    logs.add_argument("-c", "--client", action="append", default=[],
                      metavar="NAME", help="按客户端过滤")
    logs.add_argument("-a", "--account", action="append", default=[],
                      metavar="#N", help="按账号过滤")
    logs.add_argument("-f", "--follow", action="store_true",
                      help="实时跟踪新请求（Ctrl-C 退出；筛选参数照常生效）")
    logs.add_argument("--csv", action="store_true", help="导出筛选后明细 CSV")
    logs.add_argument("--state-dir", default=None, help="状态目录（默认按 BUDDY_PROXY_STATE_DIR）")
    _add_common(logs)

    doctor = sub.add_parser("doctor", help="体检：服务/配置/通道/指标/磁盘")
    doctor.add_argument("--port", type=int, default=None, help="代理端口（默认 8787）")
    _add_common(doctor)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # CLI 只输出结构化结果，库里的 warning 不该混进 stdout/stderr 干扰解析
    logging.basicConfig(level=logging.ERROR)

    if args.command == "usage":
        from . import usage as mod
    elif args.command == "logs":
        from . import logs as mod
    else:
        from . import doctor as mod
    try:
        return mod.run(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # `buddy logs | head` 是正常用法，别抛堆栈
        try:
            sys.stdout.close()
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
