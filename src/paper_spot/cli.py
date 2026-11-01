"""命令行：自检、跑一次影子盘、重写报告、重放。"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from paper_spot.config_loader import ConfigError
from paper_spot.config_loader import repo_root
from paper_spot.guard import ShadowGuardError
from paper_spot.session import check_session
from paper_spot.session import replay_session
from paper_spot.session import run_session
from paper_spot.session import write_outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第一期加密现货影子盘。订单只进入本机沙箱。")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="探测官方行情并建节点，不发单")
    check.add_argument("--run-dir", type=Path, default=None)

    run = sub.add_parser("run", help="跑一次通路测试")
    run.add_argument("--seconds", type=int, default=None)
    run.add_argument("--run-dir", type=Path, default=None)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--imbalance", default=None)

    report = sub.add_parser("report", help="根据已落盘的行情和账本重写报告")
    report.add_argument("run_dir", type=Path)

    replay = sub.add_parser("replay", help="用同一套撮合把行情重放两遍")
    replay.add_argument("run_dir", type=Path)

    args = parser.parse_args(argv)
    try:
        root = repo_root()
        if args.command == "check":
            if args.run_dir is None:
                with tempfile.TemporaryDirectory(prefix="paper-spot-check-") as temporary:
                    check_session(root, Path(temporary))
            else:
                check_session(root, args.run_dir)
        elif args.command == "run":
            run_dir = args.run_dir or _new_run_dir(root)
            run_session(
                root,
                run_dir,
                resume=args.resume,
                seconds=args.seconds,
                imbalance=args.imbalance,
            )
        elif args.command == "report":
            write_outputs(args.run_dir, replay=False)
        elif args.command == "replay":
            replay_session(args.run_dir)
    except (ConfigError, ShadowGuardError, ValueError) as exc:
        print(f"拒绝启动：{exc}")
        return 2
    return 0


def _new_run_dir(root: Path) -> Path:
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return root / "runs" / stamp


if __name__ == "__main__":
    raise SystemExit(main())
