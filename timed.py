#!/usr/bin/env python3
"""Run a command and log how long it took, so the report can compare cool vs throttled runs.

  python3 timed.py --name build -- make -j8
  alias tbuild='python3 ~/throttle-tracking/timed.py --name build --'

Stores start time, duration, name and exit code in the tasks table of data/throttle.db
(no sudo needed). The thermal pressure during the run is looked up later from the logger's samples.
"""
import argparse
import datetime as dt
import os
import subprocess
import sys
import time

import throttle_logger


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", help="label to group runs by (default: the command's first word)")
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "throttle.db"))
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

    try:
        db = throttle_logger.open_db(args.db)
        db.execute("INSERT INTO tasks (start, duration_s, name, exit_code) VALUES (?,?,?,?)",
                   (start.isoformat(timespec="seconds"), round(duration, 2), args.name or os.path.basename(cmd[0]), code))
        db.commit()
    except Exception as e:
        # never fail the wrapped command over bookkeeping
        print("timed.py: could not record the run in %s: %s" % (args.db, e), file=sys.stderr)
        if not os.access(args.db, os.W_OK):
            print("timed.py: the db isn't writable by you; restart the logger so it hands the db over:\n"
                  "  sudo launchctl kickstart -k system/local.throttle-logger", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
