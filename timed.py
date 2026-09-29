#!/usr/bin/env python3
"""Run a command and log how long it took, so the report can compare cool vs throttled runs.

  python3 timed.py --name build -- make -j8
  alias tbuild='python3 ~/throttle-tracking/timed.py --name build --'

Appends start time, duration, name and exit code to data/tasks.csv (no sudo needed).
The thermal pressure during the run is looked up later from the logger's samples.
"""
import argparse
import csv
import datetime as dt
import os
import subprocess
import sys
import time

TASKS_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tasks.csv")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", help="label to group runs by (default: the command's first word)")
    ap.add_argument("--csv", default=TASKS_CSV)
    ap.add_argument("cmd", nargs=argparse.REMAINDER, help="command to run (after --)")
    args = ap.parse_args()
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        ap.error("no command given")

    start = dt.datetime.now()
    t0 = time.monotonic()
    try:
        code = subprocess.call(cmd)
    except KeyboardInterrupt:
        code = 130
    duration = time.monotonic() - t0

    new = not os.path.exists(args.csv)
    os.makedirs(os.path.dirname(os.path.abspath(args.csv)), exist_ok=True)
    with open(args.csv, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["start", "duration_s", "name", "exit_code"])
        w.writerow([start.isoformat(timespec="seconds"), round(duration, 2),
                    args.name or os.path.basename(cmd[0]), code])
    return code


if __name__ == "__main__":
    sys.exit(main())
