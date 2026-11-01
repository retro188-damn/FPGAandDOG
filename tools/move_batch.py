"""Repeat the forward/backward motion cycle and collect per-leg statistics.

Purpose: get repeatability numbers for the report (distance error, duration,
abort count) without needing a spoken command each time.  The dog is assumed to
be standing, in autonomous mode, with clear floor ahead and behind.

    python tools/move_batch.py --cycles 3 --forward 0.30 --back 0.30
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.app.dog_state import StateFeed  # noqa: E402
from host.app.motion_exec import MotionExecutor, VelocitySender  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--forward", type=float, default=0.30)
    ap.add_argument("--back", type=float, default=0.30)
    ap.add_argument("--speed", type=float, default=0.10)
    ap.add_argument("--reverse-speed", type=float, default=0.08)
    ap.add_argument("--board", default="192.168.1.100")
    ap.add_argument("--cmd-port", type=int, default=43902)
    ap.add_argument("--state-port", type=int, default=43900)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    feed = StateFeed(port=args.state_port).start()
    sender = VelocitySender(board=args.board, port=args.cmd_port)
    ex = MotionExecutor(sender, feed, allow_motion=True, period_s=0.05)
    rows = []
    stamp = time.strftime("%Y%m%dT%H%M%S")
    out = Path(args.out or "metrics/evidence/move_batch_%s.json" % stamp)
    try:
        for cycle in range(1, args.cycles + 1):
            for tag, dist, speed, reverse in (
                ("fwd", args.forward, args.speed, False),
                ("bwd", args.back, args.reverse_speed, True),
            ):
                r = ex.move(dist, speed=speed, reverse=reverse)
                err = r.travelled_m - dist
                rows.append({
                    "cycle": cycle, "leg": tag, "target_m": dist,
                    "travelled_m": r.travelled_m, "error_m": round(err, 3),
                    "elapsed_s": r.elapsed_s, "ok": r.ok, "reason": r.reason,
                })
                print("cycle %d %s: target %.2f travelled %.3f err %+.3f "
                      "%.2fs ok=%s (%s)"
                      % (cycle, tag, dist, r.travelled_m, err, r.elapsed_s,
                         r.ok, r.reason))
    finally:
        sender.close()
        feed.close()

    done = [r for r in rows if r["ok"]]
    summary = {
        "legs": len(rows),
        "ok": len(done),
        "distance_mae_m": (round(sum(abs(r["error_m"]) for r in done) / len(done), 4)
                           if done else None),
        "max_abs_error_m": (max(abs(r["error_m"]) for r in done) if done else None),
        "mean_elapsed_s": (round(sum(r["elapsed_s"] for r in done) / len(done), 3)
                           if done else None),
        "rows": rows,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== summary ===")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"},
                     ensure_ascii=False))
    print("saved:", out)
    return 0 if len(done) == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
