#!/usr/bin/env python3
"""Follow the logger live: one line per new sample (no sudo needed, Ctrl-C to stop).

  python3 live.py
"""
import argparse
import os
import sqlite3
import time

COLORS = {"Nominal": "32", "Moderate": "33", "Heavy": "31", "Trapping": "1;31"}


def line(db, sid, ts, pressure, cpu_mw, gpu_mw, temp_max, on_ac, screen):
    # performance cores = the cluster with the highest supported frequency
    perf = db.execute("SELECT avg_active_mhz, max_mhz, busy_ratio FROM clusters WHERE sample_id = ?"
                      " ORDER BY max_mhz DESC LIMIT 1", (sid,)).fetchone()
    top = db.execute("SELECT name, cpu_ms_per_s FROM processes WHERE sample_id = ? ORDER BY rank LIMIT 1",
                     (sid,)).fetchone()
    thr, tot = db.execute(
        "SELECT sum(CASE WHEN pressure_level BETWEEN 1 AND 3 THEN interval_s END), sum(interval_s)"
        " FROM samples WHERE ts >= ? AND coalesce(screen_active, 1) = 1", (ts[:10],)).fetchone()
    parts = [ts[11:19], "\033[%sm%-8s\033[0m" % (COLORS.get(pressure, "0"), pressure)]
    if perf and perf[0] and perf[1]:
        parts.append("P %4.0f/%4.0f MHz %3.0f%% busy %3.0f%%" % (perf[0], perf[1], perf[0] / perf[1] * 100, (perf[2] or 0) * 100))
    parts.append("CPU %4.1fW GPU %4.1fW" % ((cpu_mw or 0) / 1000, (gpu_mw or 0) / 1000))
    if temp_max is not None:
        parts.append("%3.0f°C" % temp_max)
    parts.append("AC " if on_ac else "bat")
    if screen == 0:
        parts.append("\033[2mlocked (not counted)\033[0m")
    if tot:
        parts.append("today %2.0f%% throttled" % ((thr or 0) / tot * 100))
    if top:
        parts.append("top: %s %.1f cores" % (top[0][:24], top[1] / 1000))
    return "  ".join(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "throttle.db"))
    ap.add_argument("--history", type=int, default=5, help="recent samples to show first (default 5)")
    args = ap.parse_args()

    db = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    q = ("SELECT id, ts, pressure, cpu_power_mw, gpu_power_mw, temp_max_c, on_ac, screen_active FROM samples"
         " WHERE id > ? ORDER BY id")
    last = (db.execute("SELECT max(id) FROM samples").fetchone()[0] or 0) - args.history
    try:
        while True:
            for row in db.execute(q, (last,)).fetchall():
                print(line(db, *row), flush=True)
                last = row[0]
            time.sleep(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
