# systemd units

The agent cannot install systemd units (the tool classifier blocks it), so these
ship as files for you to install by hand.

## `pmbot-mos-rebuild` — weekly MOS skill-table refresh

**Why.** `data/logs/historical_skill.json` was built once on 2026-07-08 and never
rebuilt, so its per-month bias cells are a year stale. The shadow harness caught the
consequence: the `mos_off` track scores identically to `prod_mirror` to four decimal
places (model Brier 0.2220 vs 0.2218, Sep 2026), i.e. the MOS layer is currently
contributing nothing. A weekly rebuild keeps the month cells current, which makes the
correction seasonal for free.

**Install:**

```sh
sudo cp deploy/systemd/pmbot-mos-rebuild.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pmbot-mos-rebuild.timer
systemctl list-timers pmbot-mos-rebuild.timer      # confirm it is scheduled
```

**First run — do it by hand and read the output before trusting the timer:**

```sh
sudo systemctl start pmbot-mos-rebuild.service
journalctl -u pmbot-mos-rebuild -n 50 --no-pager
```

The unit runs `--validate-only` as an `ExecStartPre`, so a rebuild that would make
the table worse fails before writing anything. It also snapshots the live table to
`historical_skill.json.pre_rebuild_<ts>.bak` first — to undo a bad refresh, copy that
back over `historical_skill.json` and restart `polymarket-bot.service`.

**Note.** This rebuilds the CITY table against ERA5 grid actuals — the same target
the current table uses. It fixes staleness, not the grid-vs-station blindness that
`docs/PHASE3_STATION_FORECAST_PLAN.md` is about. When a station-trained table
exists, point `ExecStart` at `--stations` and this timer keeps that fresh instead.

## `pmbot-mosmix-snapshot` — daily Phase 3 T1 archiver

**Why.** DWD's MOSMIX open-data endpoint keeps no historical archive (~2-3 days
of past runs only, verified 2026-09-28) and Open-Meteo's ensemble API is the
same story (~3-4 days) — neither can be backtested retroactively. T1's "does
MOSMIX beat our pipeline at the station" question can only be answered by
recording all three forecasts (MOSMIX, raw Open-Meteo, skill-corrected) for the
same station/day/moment, going forward, and joining against IEM truth once each
day resolves. This timer is that recording. See `scripts/mosmix_snapshot.py`'s
module docstring for the full reasoning.

**Install:**

```sh
sudo cp deploy/systemd/pmbot-mosmix-snapshot.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pmbot-mosmix-snapshot.timer
systemctl list-timers pmbot-mosmix-snapshot.timer      # confirm it is scheduled
```

**First run — do it by hand and read the output before trusting the timer:**

```sh
sudo systemctl start pmbot-mosmix-snapshot.service
journalctl -u pmbot-mosmix-snapshot -n 50 --no-pager
```

**Reading the results.** Run `venv/bin/python scripts/mosmix_backcheck.py` any
time — it reports whatever n the archive has accumulated so far and explicitly
refuses to print a go/no-go verdict below its own n threshold. Expect "not
answerable yet" for the first 1-3 weeks; that is the correct output, not a bug.
The archive lives at `data/logs/mosmix_archive.csv` — safe to inspect directly,
one row per (station, target day, kind, snapshot day).
