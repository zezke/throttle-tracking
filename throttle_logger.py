#!/usr/bin/env python3
"""Log CPU thermal throttling on Apple Silicon Macs during work hours.

Runs `powermetrics` (needs root) and stores, per sample:
  - thermal pressure level (Nominal / Moderate / Heavy / Trapping / Sleeping),
    the same signal MacThrottle shows
  - per-cluster CPU frequency (average active + highest state reached) and
    the maximum frequency the cluster supports -> "by how much"
  - CPU / GPU / package power, AC power state
  - the top N processes by CPU time -> "why"
  - average and maximum chip (die) temperature, see temps.py

Only samples inside the configured work hours are recorded. After the
tracking period (default 14 days from first start) the logger exits cleanly.

Usage:
  sudo python3 throttle_logger.py --probe           # one sample, print what was parsed
  sudo python3 throttle_logger.py --db data/throttle.db   # run (normally via launchd)
"""
import argparse
import datetime as dt
import os
import plistlib
import signal
import sqlite3
import subprocess
import sys
import time

import temps

PRESSURE_LEVELS = {"Nominal": 0, "Moderate": 1, "Heavy": 2, "Trapping": 3, "Sleeping": 4}
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,               -- local ISO timestamp
    interval_s REAL,
    pressure TEXT,
    pressure_level INTEGER,
    cpu_power_mw REAL,
    gpu_power_mw REAL,
    package_power_mw REAL,
    on_ac INTEGER,
    temp_avg_c REAL,                -- average of the on-die sensors
    temp_max_c REAL,                -- hottest on-die sensor
    screen_active INTEGER           -- 0 = locked or all displays asleep, NULL = unknown
);
CREATE TABLE IF NOT EXISTS clusters (
    sample_id INTEGER NOT NULL REFERENCES samples(id),
    name TEXT,
    busy_ratio REAL,                -- 1 - idle_ratio
    avg_active_mhz REAL,            -- residency-weighted average while active
    peak_mhz REAL,                  -- highest DVFS state with meaningful residency
    max_mhz REAL                    -- highest DVFS state the cluster supports
);
CREATE TABLE IF NOT EXISTS processes (
    sample_id INTEGER NOT NULL REFERENCES samples(id),
    rank INTEGER,
    pid INTEGER,
    name TEXT,
    cpu_ms_per_s REAL,              -- 1000 = one full core
    energy_impact REAL
);
CREATE TABLE IF NOT EXISTS tasks (   -- written by timed.py
    id INTEGER PRIMARY KEY,
    start TEXT NOT NULL,            -- local ISO timestamp
    duration_s REAL,
    name TEXT,
    exit_code INTEGER
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE INDEX IF NOT EXISTS idx_clusters_sample ON clusters(sample_id);
CREATE INDEX IF NOT EXISTS idx_processes_sample ON processes(sample_id);
"""


def first(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def parse_cluster(c):
    states = c.get("dvfm_states") or []
    freqs = [s.get("freq") for s in states if s.get("freq")]
    max_mhz = max(freqs) if freqs else None
    active = sum(s.get("used_ratio", 0) for s in states)
    avg = sum(s.get("freq", 0) * s.get("used_ratio", 0) for s in states) / active if active else None
    # "reached" = at least 2% of the active time spent at that state
    reached = [s["freq"] for s in states if s.get("freq") and active and s.get("used_ratio", 0) / active >= 0.02]
    peak = max(reached) if reached else None
    if avg is None and c.get("freq_hz"):
        avg = c["freq_hz"] / 1e6
    idle = c.get("idle_ratio")
    return {
        "name": c.get("name", "?"),
        "busy_ratio": None if idle is None else max(0.0, 1.0 - idle),
        "avg_active_mhz": avg,
        "peak_mhz": peak,
        "max_mhz": max_mhz,
    }


def parse_sample(p, top_n):
    proc = p.get("processor") or {}
    gpu = p.get("gpu") or {}
    pressure = first(p, "thermal_pressure", default="Unknown")
    tasks = []
    for t in p.get("tasks") or []:
        cpu = first(t, "cputime_ms_per_s", "cpu_ms_per_s", default=0.0)
        tasks.append({
            "pid": t.get("pid"),
            "name": t.get("name", "?"),
            "cpu_ms_per_s": float(cpu or 0.0),
            "energy_impact": first(t, "energy_impact_per_s", "energy_impact"),
        })
    tasks.sort(key=lambda t: t["cpu_ms_per_s"], reverse=True)
    elapsed_ns = p.get("elapsed_ns")
    ts = p.get("timestamp")
    if isinstance(ts, dt.datetime):
        # plist dates are UTC
        ts = ts.replace(tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None)
    else:
        ts = dt.datetime.now()
    return {
        "ts": ts.isoformat(timespec="seconds"),
        "interval_s": elapsed_ns / 1e9 if elapsed_ns else None,
        "pressure": pressure,
        "pressure_level": PRESSURE_LEVELS.get(pressure),
        "cpu_power_mw": first(proc, "cpu_power"),
        "gpu_power_mw": first(proc, "gpu_power", default=first(gpu, "gpu_power")),
        "package_power_mw": first(proc, "combined_power", "package_power"),
        "clusters": [parse_cluster(c) for c in proc.get("clusters") or []],
        "processes": tasks[:top_n],
    }


def on_ac_power():
    try:
        out = subprocess.run(["pmset", "-g", "ps"], capture_output=True, text=True, timeout=5).stdout
        return 1 if "AC Power" in out else 0
    except Exception:
        return None


def _ioreg(*args):
    return plistlib.loads(subprocess.run(["ioreg", "-a", *args], capture_output=True, timeout=5).stdout)


def screen_state():
    """(locked, displays_asleep) read from IOKit; works from a root daemon outside the GUI session."""
    users = _ioreg("-n", "Root", "-d1").get("IOConsoleUsers") or []
    locked = any(u.get("CGSSessionScreenIsLocked") for u in users)
    fbs = _ioreg("-r", "-c", "IOMobileFramebuffer", "-d1") or []
    states = [(fb.get("IOPowerManagement") or {}).get("CurrentPowerState") for fb in fbs]
    asleep = bool(states) and all(st == 0 for st in states)
    return locked, asleep


def screen_active():
    try:
        locked, asleep = screen_state()
        return 0 if locked or asleep else 1
    except Exception as e:
        log("screen check failed: %r" % e)
        return None


def powermetrics_stream(interval_ms):
    """Yield parsed plist dicts from a running powermetrics process."""
    cmd = [
        "/usr/bin/powermetrics",
        "-f", "plist",
        "-i", str(interval_ms),
        "-s", "thermal,cpu_power,gpu_power,tasks",
        "--show-process-energy",
        "--handle-invalid-values",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    buf = b""
    try:
        while True:
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            buf += chunk
            # powermetrics separates plist documents with a NUL byte
            while b"\0" in buf:
                doc, buf = buf.split(b"\0", 1)
                doc = doc.strip()
                if doc:
                    try:
                        yield plistlib.loads(doc)
                    except Exception as e:
                        log("could not parse sample: %s" % e)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def log(msg):
    print("%s %s" % (dt.datetime.now().isoformat(timespec="seconds"), msg), flush=True)


def parse_hhmm(s):
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def parse_days(s):
    s = s.lower()
    if "-" in s and "," not in s:
        a, b = s.split("-")
        return set(range(DAY_NAMES.index(a), DAY_NAMES.index(b) + 1))
    return {DAY_NAMES.index(d.strip()) for d in s.split(",")}


def in_work_hours(now, days, start, end):
    return now.weekday() in days and start <= now.time() < end


def seconds_until_work(now, days, start, end):
    """Seconds until the next work period starts (capped so we re-check regularly)."""
    for add in range(0, 8):
        day = (now + dt.timedelta(days=add)).date()
        if day.weekday() not in days:
            continue
        begin = dt.datetime.combine(day, start)
        if begin > now:
            return min((begin - now).total_seconds(), 3600)
    return 3600


def open_db(path):
    data_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(data_dir, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    if os.geteuid() == 0:
        # hand the db to the owner of data/ (set by install.sh) so timed.py can write to it without sudo
        st = os.stat(data_dir)
        os.chown(path, st.st_uid, st.st_gid)
    # databases created before temperatures were logged
    cols = {r[1] for r in db.execute("PRAGMA table_info(samples)")}
    for col, typ in (("temp_avg_c", "REAL"), ("temp_max_c", "REAL"), ("screen_active", "INTEGER")):
        if col not in cols:
            db.execute("ALTER TABLE samples ADD COLUMN %s %s" % (col, typ))
    db.commit()
    return db


def tracking_end(db, days):
    row = db.execute("SELECT value FROM meta WHERE key='end'").fetchone()
    if row:
        return dt.datetime.fromisoformat(row[0])
    start = dt.datetime.now()
    end = dt.datetime.combine(start.date() + dt.timedelta(days=days), dt.time(0, 0))
    db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                   [("start", start.isoformat(timespec="seconds")), ("end", end.isoformat())])
    db.commit()
    return end


def store(db, s, on_ac, active, temp_avg, temp_max):
    cur = db.execute(
        "INSERT INTO samples (ts, interval_s, pressure, pressure_level, cpu_power_mw, gpu_power_mw,"
        " package_power_mw, on_ac, screen_active, temp_avg_c, temp_max_c) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (s["ts"], s["interval_s"], s["pressure"], s["pressure_level"], s["cpu_power_mw"],
         s["gpu_power_mw"], s["package_power_mw"], on_ac, active, temp_avg, temp_max))
    sid = cur.lastrowid
    db.executemany(
        "INSERT INTO clusters VALUES (?,?,?,?,?,?)",
        [(sid, c["name"], c["busy_ratio"], c["avg_active_mhz"], c["peak_mhz"], c["max_mhz"])
         for c in s["clusters"]])
    db.executemany(
        "INSERT INTO processes VALUES (?,?,?,?,?,?)",
        [(sid, i + 1, t["pid"], t["name"], t["cpu_ms_per_s"], t["energy_impact"])
         for i, t in enumerate(s["processes"])])
    db.commit()


def probe(args):
    for raw in powermetrics_stream(1000):
        s = parse_sample(raw, args.top)
        print("Top-level keys:", sorted(raw.keys()))
        print("Processor keys:", sorted((raw.get("processor") or {}).keys()))
        if raw.get("tasks"):
            print("Task keys:", sorted(raw["tasks"][0].keys()))
        print()
        print("Thermal pressure:", s["pressure"])
        print("CPU / GPU / package power (mW):", s["cpu_power_mw"], s["gpu_power_mw"], s["package_power_mw"])
        print("Screen locked / displays asleep:", *screen_state())
        print("Die temperature avg / max (°C):", *[_r(t, 1) for t in temps.die_temps()])
        for c in s["clusters"]:
            print("  cluster %-12s busy=%s avg=%s peak=%s max=%s MHz" % (
                c["name"], _r(c["busy_ratio"], 2), _r(c["avg_active_mhz"]), _r(c["peak_mhz"]), _r(c["max_mhz"])))
        print("Top processes:")
        for t in s["processes"]:
            print("  %8.1f ms/s  %s (%s)" % (t["cpu_ms_per_s"], t["name"], t["pid"]))
        return


def _r(v, n=0):
    return None if v is None else round(v, n)


def run(args):
    days = parse_days(args.days_of_week)
    start, end = parse_hhmm(args.start), parse_hhmm(args.end)
    db = open_db(args.db)
    stop_at = tracking_end(db, args.period_days)
    log("tracking until %s, work hours %s %s-%s, db=%s" % (stop_at, args.days_of_week, args.start, args.end, args.db))

    stopping = {"flag": False}

    def handle_term(*_):
        stopping["flag"] = True
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handle_term)
    last_active = 1

    try:
        while not stopping["flag"]:
            now = dt.datetime.now()
            if now >= stop_at:
                log("tracking period finished")
                return 0
            if not in_work_hours(now, days, start, end):
                time.sleep(seconds_until_work(now, days, start, end))
                continue
            log("work hours: sampling every %ss" % args.interval)
            for raw in powermetrics_stream(args.interval * 1000):
                now = dt.datetime.now()
                if now >= stop_at or not in_work_hours(now, days, start, end):
                    break
                try:
                    active = screen_active()
                    if active != last_active:
                        log("screen %s" % {0: "locked/asleep: flagging samples", 1: "active", None: "state unknown"}[active])
                        last_active = active
                    store(db, parse_sample(raw, args.top), on_ac_power(), active, *temps.die_temps())
                except Exception as e:
                    log("failed to store sample: %s" % e)
            else:
                # powermetrics exited on its own (e.g. after sleep/wake); retry shortly
                time.sleep(5)
    except KeyboardInterrupt:
        log("stopped")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "throttle.db"))
    ap.add_argument("--interval", type=int, default=10, help="seconds between samples (default 10)")
    ap.add_argument("--top", type=int, default=5, help="processes stored per sample (default 5)")
    ap.add_argument("--start", default="09:00", help="work day start HH:MM (default 09:00)")
    ap.add_argument("--end", default="18:00", help="work day end HH:MM (default 18:00)")
    ap.add_argument("--days-of-week", default="mon-fri", help="e.g. mon-fri or mon,tue,thu")
    ap.add_argument("--period-days", type=int, default=14, help="stop after N days from first start (default 14)")
    ap.add_argument("--probe", action="store_true", help="take one sample, print it, and exit")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("powermetrics requires root: run with sudo (or install the LaunchDaemon)")
    return probe(args) if args.probe else run(args)


if __name__ == "__main__":
    sys.exit(main())
