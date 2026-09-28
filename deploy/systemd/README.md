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
