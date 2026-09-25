#!/usr/bin/env python3
"""Summarise the data collected by throttle_logger.py.

  python3 throttle_report.py                     # full report
  python3 throttle_report.py --since 2026-09-28  # only part of the period
  python3 throttle_report.py --csv episodes.csv  # also export the episode list
"""
import argparse
import collections
import csv
import datetime as dt
import os
import sqlite3
import statistics
import sys

LEVEL_NAMES = {0: "Nominal", 1: "Moderate", 2: "Heavy", 3: "Trapping", 4: "Sleeping"}
LEAD_IN_S = 60  # CPU time in the minute before an episode also counts towards its cause


def load(db, since, until):
    q = "SELECT id, ts, interval_s, pressure_level, cpu_power_mw, gpu_power_mw, on_ac FROM samples WHERE 1=1"
    params = []
    if since:
        q += " AND ts >= ?"
        params.append(since)
    if until:
        q += " AND ts < ?"
        params.append(until)
    samples = []
    for sid, ts, interval, level, cpu_mw, gpu_mw, on_ac in db.execute(q + " ORDER BY ts", params):
        samples.append({
            "id": sid, "ts": dt.datetime.fromisoformat(ts), "interval": interval or 10.0,
            "level": level or 0, "cpu_mw": cpu_mw, "gpu_mw": gpu_mw, "on_ac": on_ac,
            "procs": [], "perf": None,
        })
    by_id = {s["id"]: s for s in samples}

    # The performance cluster(s) are the ones with the highest supported frequency.
    clusters = collections.defaultdict(list)
    for sid, name, busy, avg, peak, mx in db.execute("SELECT sample_id, name, busy_ratio, avg_active_mhz, peak_mhz, max_mhz FROM clusters"):
        if sid in by_id:
            clusters[sid].append((name, busy, avg, peak, mx))
    top_max = max((c[4] or 0 for cs in clusters.values() for c in cs), default=0)
    for sid, cs in clusters.items():
        perf = [c for c in cs if c[4] and c[4] >= top_max * 0.9]
        busy = [c for c in perf if c[1] is not None and c[1] >= 0.5 and c[3]]
        if busy:
            by_id[sid]["perf"] = {
                "peak_pct": max(c[3] / c[4] for c in busy) * 100,
                "avg_pct": statistics.mean(c[2] / c[4] for c in busy if c[2]) * 100,
            }

    for sid, name, cpu in db.execute("SELECT sample_id, name, cpu_ms_per_s FROM processes"):
        if sid in by_id:
            by_id[sid]["procs"].append((name, cpu or 0.0))
    return samples, top_max


def find_episodes(samples):
    """Group consecutive throttled samples (pressure > Nominal) into episodes."""
    episodes, cur = [], None
    for i, s in enumerate(samples):
        throttled = 0 < s["level"] < 4
        gap = i and (s["ts"] - samples[i - 1]["ts"]).total_seconds() > 3 * s["interval"]
        if cur and (not throttled or gap):
            episodes.append(cur)
            cur = None
        if throttled:
            if cur is None:
                cur = {"start_idx": i, "samples": []}
            cur["samples"].append(s)
    if cur:
        episodes.append(cur)

    for ep in episodes:
        ss = ep["samples"]
        ep["start"] = ss[0]["ts"]
        ep["duration_s"] = sum(s["interval"] for s in ss)
        ep["max_level"] = max(s["level"] for s in ss)
        perf = [s["perf"] for s in ss if s["perf"]]
        ep["peak_pct"] = statistics.median(p["peak_pct"] for p in perf) if perf else None
        ep["cpu_w"] = _mean(s["cpu_mw"] for s in ss) / 1000 if _mean(s["cpu_mw"] for s in ss) else None
        ep["gpu_w"] = _mean(s["gpu_mw"] for s in ss) / 1000 if _mean(s["gpu_mw"] for s in ss) else None
        # attribution window: the episode plus the lead-in before it
        lead = [p for p in samples[max(0, ep["start_idx"] - 20):ep["start_idx"]]
                if 0 <= (ep["start"] - p["ts"]).total_seconds() <= LEAD_IN_S]
        ep["window"] = lead + ss
        ep["culprits"] = top_procs(ep["window"], 3)
    return episodes


def _mean(values):
    vals = [v for v in values if v is not None]
    return statistics.mean(vals) if vals else None


def proc_totals(samples):
    tot = collections.Counter()
    for s in samples:
        for name, cpu in s["procs"]:
            tot[name] += cpu * s["interval"] / 1000.0  # CPU-seconds
    return tot


def top_procs(samples, n):
    tot = proc_totals(samples)
    total = sum(tot.values()) or 1
    return [(name, secs, secs / total * 100) for name, secs in tot.most_common(n)]


def fmt_dur(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%dh%02dm" % (h, m) if h else ("%dm%02ds" % (m, s) if m else "%ds" % s)


def pct(v):
    return "  n/a" if v is None else "%4.0f%%" % v


def report(samples, top_max, episodes, args):
    if not samples:
        print("No samples recorded yet.")
        return
    total_s = sum(s["interval"] for s in samples)
    level_s = collections.Counter()
    for s in samples:
        level_s[s["level"]] += s["interval"]
    throttled_s = sum(v for k, v in level_s.items() if 0 < k < 4)

    print("=" * 72)
    print("CPU THROTTLE REPORT   %s  ->  %s" % (samples[0]["ts"].strftime("%a %Y-%m-%d %H:%M"),
                                              samples[-1]["ts"].strftime("%a %Y-%m-%d %H:%M")))
    print("=" * 72)
    print("Tracked work time : %s (%d samples, %d days)" % (
        fmt_dur(total_s), len(samples), len({s["ts"].date() for s in samples})))
    print("Throttled         : %s  (%.1f%% of tracked time)" % (fmt_dur(throttled_s), throttled_s / total_s * 100))
    print("Throttle episodes : %d" % len(episodes))
    if episodes:
        durs = [e["duration_s"] for e in episodes]
        print("  duration        : median %s, longest %s" % (fmt_dur(statistics.median(durs)), fmt_dur(max(durs))))
    print()
    print("Time per thermal pressure level:")
    for lvl in sorted(level_s):
        print("  %-9s %9s  %5.1f%%" % (LEVEL_NAMES.get(lvl, lvl), fmt_dur(level_s[lvl]), level_s[lvl] / total_s * 100))

    # --- by how much -------------------------------------------------------
    print()
    print("HOW MUCH: performance-core speed while busy (%% of max %.0f MHz)" % top_max)
    by_level = collections.defaultdict(list)
    for s in samples:
        if s["perf"]:
            by_level[s["level"]].append(s["perf"])
    print("  %-9s %8s %14s %14s %8s" % ("level", "samples", "peak reached", "avg active", "CPU W"))
    for lvl in sorted(by_level):
        ps = by_level[lvl]
        cpu = _mean(s["cpu_mw"] for s in samples if s["level"] == lvl and s["perf"])
        print("  %-9s %8d %13s %14s %8s" % (
            LEVEL_NAMES.get(lvl, lvl), len(ps),
            pct(statistics.median(p["peak_pct"] for p in ps)),
            pct(statistics.median(p["avg_pct"] for p in ps)),
            "%.1f" % (cpu / 1000) if cpu else "n/a"))
    print("  (peak reached = highest clock the cluster hit; a drop vs Nominal is the throttle)")

    # --- why ---------------------------------------------------------------
    print()
    print("WHY: processes using the most CPU during throttling (incl. %ds lead-in)" % LEAD_IN_S)
    window_ids, window = set(), []
    for ep in episodes:
        for s in ep["window"]:
            if s["id"] not in window_ids:
                window_ids.add(s["id"])
                window.append(s)
    baseline = [s for s in samples if s["id"] not in window_ids]
    thr_tot, base_tot = proc_totals(window), proc_totals(baseline)
    thr_sum, base_sum = sum(thr_tot.values()) or 1, sum(base_tot.values()) or 1
    thr_time = sum(s["interval"] for s in window) or 1
    print("  %-32s %10s %8s %10s %8s" % ("process", "CPU-sec", "share", "avg cores", "vs norm"))
    for name, secs in thr_tot.most_common(args.top):
        share = secs / thr_sum * 100
        base_share = base_tot.get(name, 0) / base_sum * 100
        lift = ("%.1fx" if share >= base_share else "%.2fx") % (share / base_share) if base_share > 0.1 else "new"
        print("  %-32s %10.0f %7.1f%% %10.2f %8s" % (name[:32], secs, share, secs / thr_time, lift))
    print("  (vs norm = share during throttling / share the rest of the time; high = likely cause)")

    ac = [s for s in window if s["on_ac"] is not None]
    if ac:
        print()
        print("During throttling the Mac was on AC power %.0f%% of the time." % (
            sum(1 for s in ac if s["on_ac"]) / len(ac) * 100))
    gpu_thr = _mean(s["gpu_mw"] for s in window)
    gpu_base = _mean(s["gpu_mw"] for s in baseline)
    if gpu_thr and gpu_base:
        print("Avg GPU power: %.1f W while throttling vs %.1f W otherwise." % (gpu_thr / 1000, gpu_base / 1000))

    # --- per day & hour ----------------------------------------------------
    print()
    print("PER DAY")
    days = collections.OrderedDict()
    for s in samples:
        d = days.setdefault(s["ts"].date(), {"total": 0, "thr": 0, "eps": 0})
        d["total"] += s["interval"]
        if 0 < s["level"] < 4:
            d["thr"] += s["interval"]
    for ep in episodes:
        days[ep["start"].date()]["eps"] += 1
    for day, d in days.items():
        bar = "#" * int(round(d["thr"] / d["total"] * 50)) if d["total"] else ""
        print("  %s  %3d episodes  %8s throttled  %s" % (day.strftime("%a %m-%d"), d["eps"], fmt_dur(d["thr"]), bar))

    print()
    print("BY HOUR OF DAY (% of tracked time throttled)")
    hours = collections.defaultdict(lambda: [0, 0])
    for s in samples:
        hours[s["ts"].hour][0] += s["interval"]
        if 0 < s["level"] < 4:
            hours[s["ts"].hour][1] += s["interval"]
    for h in sorted(hours):
        tot, thr = hours[h]
        share = thr / tot * 100 if tot else 0
        print("  %02d:00  %5.1f%%  %s" % (h, share, "#" * int(round(share / 2))))

    # --- episode list ------------------------------------------------------
    print()
    print("LONGEST EPISODES")
    print("  %-17s %8s %-9s %6s %6s  %s" % ("start", "duration", "max level", "P-peak", "CPU W", "top processes"))
    for ep in sorted(episodes, key=lambda e: e["duration_s"], reverse=True)[:args.episodes]:
        culprits = ", ".join("%s %.0f%%" % (n[:20], p) for n, _, p in ep["culprits"])
        print("  %-17s %8s %-9s %6s %6s  %s" % (
            ep["start"].strftime("%a %m-%d %H:%M"), fmt_dur(ep["duration_s"]),
            LEVEL_NAMES.get(ep["max_level"]), pct(ep["peak_pct"]),
            "%.1f" % ep["cpu_w"] if ep["cpu_w"] else "n/a", culprits))


def write_csv(path, episodes):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["start", "duration_s", "max_level", "perf_peak_pct_of_max", "cpu_w", "gpu_w",
                    "process_1", "share_1", "process_2", "share_2", "process_3", "share_3"])
        for ep in episodes:
            row = [ep["start"].isoformat(), round(ep["duration_s"]), LEVEL_NAMES.get(ep["max_level"]),
                   _round(ep["peak_pct"]), _round(ep["cpu_w"], 2), _round(ep["gpu_w"], 2)]
            for n, _, p in ep["culprits"]:
                row += [n, round(p, 1)]
            w.writerow(row)
    print()
    print("Episodes written to %s" % path)


def _round(v, n=0):
    return None if v is None else round(v, n)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "throttle.db"))
    ap.add_argument("--since", help="YYYY-MM-DD[THH:MM]")
    ap.add_argument("--until", help="YYYY-MM-DD[THH:MM]")
    ap.add_argument("--top", type=int, default=12, help="processes to list (default 12)")
    ap.add_argument("--episodes", type=int, default=15, help="episodes to list (default 15)")
    ap.add_argument("--csv", help="write all episodes to this CSV file")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit("No database at %s yet - is the logger installed and running?" % args.db)
    db = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    samples, top_max = load(db, args.since, args.until)
    episodes = find_episodes(samples)
    report(samples, top_max, episodes, args)
    if args.csv:
        write_csv(args.csv, episodes)


if __name__ == "__main__":
    main()
