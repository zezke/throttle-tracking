# Throttle tracking

Is your Mac getting slow during the workday because it's running hot, and if so,
which app is to blame?

This tool logs CPU thermal throttling on your Mac (Apple Silicon) during work hours
over a fixed period, then reports **how often**, **by how much** and **which processes**
were responsible.

## Requirements

- An Apple Silicon Mac (M1 or later) running a recent macOS
- `/usr/bin/python3` (install the Xcode Command Line Tools with `xcode-select --install`).
  No pip packages are needed.
- Admin rights: `powermetrics` only runs as root

## Privacy

Everything stays on your Mac in a local SQLite file (`data/`); nothing is sent anywhere.
The database does contain the names of the processes you ran and when, so think twice
before sharing it.

## How it works

`throttle_logger.py` runs Apple's `powermetrics` (root only) every 10 s and records to
SQLite (`data/throttle.db`):

| What | Source | Answers |
|---|---|---|
| Thermal pressure: Nominal / Moderate / Heavy / Trapping | `thermal` sampler (same signal [MacThrottle](https://github.com/angristan/MacThrottle) shows) | *how often* |
| Per-cluster CPU frequency: average, peak reached, max supported | `cpu_power` sampler, DVFS residency | *by how much* |
| CPU / GPU power, AC or battery | `cpu_power`, `gpu_power`, `pmset` | context |
| Top 5 processes by CPU time | `tasks` sampler | *why* |
| Chip temperature: average and hottest on-die sensor | IOKit HID sensors (`temps.py`) | context |

Samples taken while the screen is locked or every display is asleep are flagged
(`screen_active = 0`) and left out of all reports, so idle time doesn't dilute the results.

It only records inside work hours (default Mon–Fri 09:00–18:00). The period ends
14 days after the first start, even if the Mac reboots in between; after that the
logger exits and stops.

`pmset -g therm` is not used: on Apple Silicon it doesn't report throttling.

## Setup

```bash
# 1. Sanity check: take one sample and show what was parsed
sudo python3 throttle_logger.py --probe

# 2. Install as a LaunchDaemon (runs in background, survives reboots)
sudo ./install.sh                                   # defaults
sudo ./install.sh --start 08:30 --end 17:30         # custom hours
#   other options: --days-of-week mon-fri|mon,tue,thu  --period-days 14  --interval 10  --top 5

# 3. Check it's running
tail -f data/logger.log
```

Keep [MacThrottle](https://github.com/angristan/MacThrottle) running if you like; the two
don't interfere.

## Live view

```bash
python3 live.py     # one line per sample as it's logged, Ctrl-C to stop
```

## Report (any time, no sudo needed)

```bash
python3 throttle_report.py
python3 throttle_report.py --since 2026-09-28 --until 2026-10-03
python3 throttle_report.py --csv episodes.csv
```

The report contains:

- **Totals**: tracked time, time throttled, number of throttle *episodes* (runs of
  consecutive non-Nominal samples), and their median and longest duration.
- **HOW MUCH**: the highest clock the performance cores reached while busy, as a % of their
  maximum, split by pressure level. For example, Nominal 100% → Heavy 65% means a
  35% clock-speed cut.
- **LOST TIME**: an upper bound on the time throttling cost: busy performance-core time
  while throttled × the clock cut vs Nominal, split over the processes by CPU share. It's an
  upper bound because memory-bound work slows down less than the clock, and background work
  you weren't waiting for counts too.
- **TIMED TASKS** (if you use `timed.py`, see below): median duration of each task when cool
  vs when throttled, and the time lost across the throttled runs.
- **WHY**: CPU-seconds per process during throttling, plus the 60 s before each
  episode, when the heat builds up. The *vs norm* column compares a process's share
  during throttling with its share the rest of the time. A high value or `new` points
  to the cause; steady background processes score around 1x or lower.
- **Per day / by hour of day**, and a list of the longest episodes with their top 3 processes.

## Timing real tasks

The most honest number is how much longer the things you wait for take. Run them through
`timed.py` (no sudo needed); it logs start time and duration to `data/tasks.csv`, and the
report compares runs that were cool (<10% throttled) with hot ones (≥50%):

```bash
python3 timed.py --name build -- make -j8
alias tbuild='python3 ~/throttle-tracking/timed.py --name build --'   # e.g. in ~/.zshrc
```

Only successful runs while the logger was sampling count. Keep one kind of work per name
(clean and incremental builds under different names), or the comparison is noise.

## Daily summary

```bash
python3 daily_summary.py                     # today
python3 daily_summary.py --date yesterday
python3 daily_summary.py --date 2026-09-28
```

Shows the % of the day spent in each thermal state, the top processes by CPU time (and how
much of that fell while throttled), the average and maximum chip temperature, and the
average and maximum core frequency per CPU cluster (the last two each overall, while
Nominal and while throttled).

`DEAD_TASKS` in the process list is CPU time of processes that exited during a sample,
usually many short-lived ones (compilers, shells, `xcrun`) spawned by builds or scripts.

`python3 temps.py` prints all temperature sensors right now.

## Stop / remove

```bash
sudo ./uninstall.sh      # data/ is kept
```

## Notes

- The overhead is small: one `powermetrics` sample every 10 s, and only during work hours.
- Storage is roughly 15 MB for two weeks of work hours.
- `temps.py` uses undocumented IOKit HID calls. A future macOS update may break them; the
  temperature columns are then empty, or `temps.py` needs fixing.
- If GPU power is high while throttling but the CPU culprits look harmless, the heat
  is probably coming from the GPU (video calls, external displays, games).
- `sqlite3 data/throttle.db` gives you the raw data (`samples`, `clusters`, `processes`)
  for your own queries.

## License

MIT, see [LICENSE](LICENSE): use it for anything, as long as you keep the copyright notice.
