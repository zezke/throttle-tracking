WITH baseline AS (          -- normal speed per cluster: busy + not throttled
  SELECT c.name, avg(c.avg_active_mhz) AS base_mhz
  FROM clusters c JOIN samples s ON s.id = c.sample_id
  WHERE s.pressure_level = 0 AND c.busy_ratio >= 0.5 AND coalesce(s.screen_active, 1) = 1
  GROUP BY c.name
),
busy AS (                   -- every busy sample, with its speed loss vs baseline
  SELECT c.name, s.pressure, s.pressure_level, s.interval_s, c.busy_ratio,
         max(0, 1 - c.avg_active_mhz / b.base_mhz) AS loss
  FROM clusters c JOIN samples s ON s.id = c.sample_id JOIN baseline b ON b.name = c.name
  WHERE c.busy_ratio >= 0.5 AND coalesce(s.screen_active, 1) = 1  -- skip locked / display asleep
)
SELECT name, pressure,
       round(sum(interval_s) / 60.0, 1)                       AS busy_minutes,
       round(100 * avg(loss), 1)                              AS avg_loss_pct,
       round(sum(interval_s * loss) / 60.0, 1)                AS lost_minutes
FROM busy
GROUP BY name, pressure_level
ORDER BY name, pressure_level;
