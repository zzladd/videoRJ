"""命令行：自检、跑一次影子盘、重写报告、重放、本机站点、补本地 K 线。"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from paper_spot.config_loader import ConfigError
from paper_spot.config_loader import repo_root
from paper_spot.guard import ShadowGuardError
from paper_spot.session import check_session
from paper_spot.session import replay_session
from paper_spot.session import run_session
from paper_spot.session import write_outputs
from paper_spot.site import serve_site
from paper_spot.view import serve_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第一期加密现货影子盘。订单只进入本机沙箱。")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="探测官方行情并建节点，不发单")
    check.add_argument("--run-dir", type=Path, default=None)
    check.add_argument("--strategy", default=None, help="connectivity、user，或含 decide 的 .py 文件")

    run = sub.add_parser("run", help="跑一次通路测试")
    run.add_argument("--seconds", type=int, default=None)
    run.add_argument("--run-dir", type=Path, default=None)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--imbalance", default=None)
    run.add_argument("--strategy", default=None, help="connectivity、user，或含 decide 的 .py 文件")

    report = sub.add_parser("report", help="根据已落盘的行情和账本重写报告")
    report.add_argument("run_dir", type=Path)

    replay = sub.add_parser("replay", help="用同一套撮合把行情重放两遍")
    replay.add_argument("run_dir", type=Path)

    serve = sub.add_parser("serve", help="在 127.0.0.1 打开本机量化站点")
    serve.add_argument("--port", type=int, default=8765)

    view = sub.add_parser("view", help="在本机站点打开一次运行目录")
    view.add_argument("run_dir", type=Path)
    view.add_argument("--port", type=int, default=8765)

    bars = sub.add_parser("bars", help="维护本地已收盘 K 线")
    bars_sub = bars.add_subparsers(dest="bars_command", required=True)
    sync = bars_sub.add_parser("sync", help="从官方公开行情补已收盘 K 线")
    sync.add_argument("--venue", default="BINANCE")
    sync.add_argument("--symbol", default="BTCUSDT")
    sync.add_argument("--interval", default="1d")
    sync.add_argument("--start", required=True, help="UTC 日期，YYYY-MM-DD")
    sync.add_argument("--end", default=None, help="UTC 日期，含当天。省略则到今天")

    args = parser.parse_args(argv)
    try:
        root = repo_root()
        if args.command == "check":
            if args.run_dir is None:
                with tempfile.TemporaryDirectory(prefix="paper-spot-check-") as temporary:
                    check_session(root, Path(temporary), strategy=args.strategy)
            else:
                check_session(root, args.run_dir, strategy=args.strategy)
        elif args.command == "run":
            run_dir = args.run_dir or _new_run_dir(root)
            run_session(
                root,
                run_dir,
                resume=args.resume,
                seconds=args.seconds,
                imbalance=args.imbalance,
                strategy=args.strategy,
            )
        elif args.command == "report":
            write_outputs(args.run_dir, replay=False)
        elif args.command == "replay":
            replay_session(args.run_dir)
        elif args.command == "serve":
            serve_site(port=args.port, root=root)
        elif args.command == "view":
            serve_run(args.run_dir, args.port)
        elif args.command == "bars":
            _sync_bars(root, args)
    except (ConfigError, ShadowGuardError, ValueError) as exc:
        print(f"拒绝启动：{exc}")
        return 2
    return 0


def _sync_bars(root: Path, args: argparse.Namespace) -> None:
    from paper_spot.research.service import end_of_utc_date
    from paper_spot.research.service import parse_utc_date
    from paper_spot.research.service import sync_range

    start_ms = parse_utc_date(args.start)
    if args.end:
        end_ms = end_of_utc_date(args.end)
    else:
        moment = datetime.now(timezone.utc)
        end_ms = int(datetime(moment.year, moment.month, moment.day, tzinfo=timezone.utc).timestamp() * 1000) + 86_400_000 - 1
    result = sync_range(root, args.venue.upper(), args.symbol.upper(), args.interval, start_ms, end_ms)
    print(json.dumps(result["coverage"], ensure_ascii=False, indent=2))


def _new_run_dir(root: Path) -> Path:
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return root / "runs" / stamp


if __name__ == "__main__":
    raise SystemExit(main())
