#!/usr/bin/env python3
"""Self-check for the lost-time and timed-task maths: python3 test_report.py"""
import datetime as dt
import sqlite3

import throttle_logger as L
import throttle_report as R

T0 = dt.datetime(2026, 9, 24, 10, 0)


def sample(i, level, peak, procs):
    return {"id": i, "ts": T0 + dt.timedelta(seconds=10 * (i + 1)), "interval": 10.0, "level": level,
            "perf": {"peak_pct": peak, "avg_pct": peak} if peak else None, "procs": procs}


# 10 Nominal samples at 100%, then 10 Heavy at 60% (4 s lost each), split 3:1 between two processes
samples = [sample(i, 0, 100, [("a", 500)]) for i in range(10)]
samples += [sample(i, 2, 60, [("hot", 750), ("bg", 250)]) for i in range(10, 20)]
samples += [sample(20, 2, None, [("idle", 100)])]  # P-cores not busy: no loss
ref, lost, by_proc = R.lost_time(samples)
assert ref == 100 and abs(lost - 40) < 1e-9, (ref, lost)
assert abs(by_proc["hot"] - 30) < 1e-9 and abs(by_proc["bg"] - 10) < 1e-9 and "idle" not in by_proc

db = sqlite3.connect(":memory:")
db.executescript(L.SCHEMA)
db.executemany("INSERT INTO tasks (start, duration_s, name, exit_code) VALUES (?,?,?,0)", [
    ((T0 + dt.timedelta(seconds=5)).isoformat(), 30, "cool"),   # Nominal samples only
    ((T0 + dt.timedelta(seconds=120)).isoformat(), 50, "hot"),  # Heavy samples only
    ((T0 - dt.timedelta(days=1)).isoformat(), 5, "old"),        # before any sample
])
runs = {r["name"]: r for r in R.load_tasks(db, samples, None, None)}
assert runs["cool"]["throttled"] == 0, runs["cool"]
assert runs["hot"]["throttled"] == 1, runs["hot"]
assert runs["old"]["throttled"] is None, runs["old"]
print("ok")
