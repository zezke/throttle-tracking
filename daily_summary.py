#!/usr/bin/env python3
"""One-day summary of the data collected by throttle_logger.py.

  python3 daily_summary.py                   # today
  python3 daily_summary.py --date 2026-09-28
  python3 daily_summary.py --date yesterday
"""
import argparse
import collections
import datetime as dt
import os
import sqlite3
import sys

from throttle_report import LEVEL_NAMES, fmt_dur


def parse_date(s):
    if s == "today":
        return dt.date.today()
    if s == "yesterday":
        return dt.date.today() - dt.timedelta(days=1)
    return dt.date.fromisoformat(s)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "throttle.db"))
    ap.add_argument("--date", default="today", help="YYYY-MM-DD, 'today' (default) or 'yesterday'")
    ap.add_argument("--top", type=int, default=10, help="processes to list (default 10)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit("No database at %s yet - is the logger installed and running?" % args.db)
    day = parse_date(args.date)
    db = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    lo, hi = day.isoformat(), (day + dt.timedelta(days=1)).isoformat()

    # temperature columns only exist once the updated logger has started
    has_temps = "temp_avg_c" in {r[1] for r in db.execute("PRAGMA table_info(samples)")}
    temp_cols = "temp_avg_c, temp_max_c" if has_temps else "NULL, NULL"
    # skip samples taken while the screen was locked or asleep (NULL = unknown, keep)
    has_screen = "screen_active" in {r[1] for r in db.execute("PRAGMA table_info(samples)")}
    active = " AND coalesce(s.screen_active, 1) = 1" if has_screen else ""
    samples = db.execute(
        "SELECT id, ts, coalesce(interval_s, 10), coalesce(pressure_level, 0), " + temp_cols +
        " FROM samples s WHERE ts >= ? AND ts < ?" + active + " ORDER BY ts", (lo, hi)).fetchall()
    print("DAILY SUMMARY  %s" % day.strftime("%A %Y-%m-%d"))
    print("=" * 60)
    if not samples:
        print("No samples for this day (outside work hours, or the logger wasn't running).")
        return

    total = sum(s[2] for s in samples)
    print("Tracked : %s (%s - %s, %d samples)" % (fmt_dur(total), samples[0][1][11:16], samples[-1][1][11:16], len(samples)))

    # --- states ------------------------------------------------------------
    print()
    print("THERMAL STATE")
    per_level = collections.Counter()
    for _, _, interval, level, _, _ in samples:
        per_level[level] += interval
    for level in sorted(set(per_level) | {0, 1, 2}):
        secs = per_level.get(level, 0)
        share = secs / total * 100
        print("  %-9s %6.1f%%  %8s  %s" % (LEVEL_NAMES.get(level, level), share, fmt_dur(secs), "#" * int(round(share / 2))))

    # --- processes ---------------------------------------------------------
    print()
    print("TOP PROCESSES (by CPU time)")
    rows = db.execute(
        "SELECT p.name, sum(p.cpu_ms_per_s * coalesce(s.interval_s, 10)) / 1000.0,"
        "       sum(CASE WHEN s.pressure_level BETWEEN 1 AND 3"
        "                THEN p.cpu_ms_per_s * coalesce(s.interval_s, 10) ELSE 0 END) / 1000.0"
        " FROM processes p JOIN samples s ON s.id = p.sample_id"
        " WHERE s.ts >= ? AND s.ts < ?" + active + " GROUP BY p.name ORDER BY 2 DESC LIMIT ?", (lo, hi, args.top)).fetchall()
    all_cpu = db.execute(
        "SELECT sum(p.cpu_ms_per_s * coalesce(s.interval_s, 10)) / 1000.0"
        " FROM processes p JOIN samples s ON s.id = p.sample_id WHERE s.ts >= ? AND s.ts < ?" + active, (lo, hi)).fetchone()[0] or 1
    print("  %-30s %9s %7s %10s %16s" % ("process", "CPU time", "share", "avg cores", "while throttled"))
    for name, cpu_s, thr_s in rows:
        print("  %-30s %9s %6.1f%% %10.2f %16s" % (
            name[:30], fmt_dur(cpu_s), cpu_s / all_cpu * 100, cpu_s / total, fmt_dur(thr_s) if thr_s else "-"))
    print("  (share = of the CPU time used by the top processes recorded each sample)")

    # --- temperature -------------------------------------------------------
    print()
    print("CHIP TEMPERATURE (on-die sensors)")
    temps = [s for s in samples if s[4] is not None]
    if not temps:
        print("  No temperature data for this day (recorded before temperature logging was added).")
    else:
        avg = sum(s[4] * s[2] for s in temps) / sum(s[2] for s in temps)
        hottest = max(temps, key=lambda s: s[5])
        print("  Average : %5.1f °C" % avg)
        print("  Maximum : %5.1f °C  at %s" % (hottest[5], hottest[1][11:16]))
        for label, subset in (("Nominal", [s for s in temps if s[3] == 0]),
                              ("Throttled", [s for s in temps if 1 <= s[3] <= 3])):
            if subset:
                print("  Avg while %-9s: %5.1f °C  (max %.1f °C)" % (
                    label, sum(s[4] for s in subset) / len(subset), max(s[5] for s in subset)))
        if len(temps) < len(samples):
            print("  (%d of %d samples have temperature data)" % (len(temps), len(samples)))

    # --- frequencies -------------------------------------------------------
    print()
    print("CORE FREQUENCIES (average while active, per cluster)")
    rows = db.execute(
        "SELECT c.name, sum(c.avg_active_mhz * coalesce(s.interval_s, 10)) / sum(coalesce(s.interval_s, 10)),"
        "       max(c.peak_mhz), max(c.max_mhz),"
        "       avg(CASE WHEN s.pressure_level = 0 THEN c.avg_active_mhz END),"
        "       avg(CASE WHEN s.pressure_level BETWEEN 1 AND 3 THEN c.avg_active_mhz END)"
        " FROM clusters c JOIN samples s ON s.id = c.sample_id"
        " WHERE s.ts >= ? AND s.ts < ? AND c.avg_active_mhz IS NOT NULL" + active +
        " GROUP BY c.name ORDER BY c.name", (lo, hi)).fetchall()
    if not rows:
        print("  No frequency data for this day.")
    mhz = lambda v: "%5.0f MHz" % v if v is not None else "        -"
    for name, avg, peak, cap, nominal, throttled in rows:
        print("  %s  (max supported %.0f MHz)" % (name, cap))
        print("    Average            : %s" % mhz(avg))
        print("    Maximum            : %s" % mhz(peak))
        print("    Avg while Nominal  : %s" % mhz(nominal))
        print("    Avg while Throttled: %s" % mhz(throttled))


if __name__ == "__main__":
    main()
