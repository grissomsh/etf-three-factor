# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

ETF 三因子监测系统 (three-factor ETF monitoring system) — detects potential "national team" (中央汇金) buying signals in Chinese broad-based ETFs via a heuristic three-factor scoring model (信号分, not a calibrated probability). This repo is the source for a Claude Code skill: `SKILL.md` is the skill entry point, and the scripts are normally deployed to `~/.etf-skill/scripts/` in production (see README), so all workspace paths resolve to `~/.etf-skill/workspace` at runtime.

## Commands

No build, test, or lint tooling — scripts are pure Python stdlib + akshare. Run directly:

```bash
python3 scripts/etf_threefactor.py                 # full pipeline: fetch → store → analyze → HTML → JSON
python3 scripts/etf_threefactor.py --date 2026-04-30  # analyze a specific historical date
python3 scripts/etf_threefactor.py --record        # collect today's shares into SQLite only
python3 scripts/etf_threefactor.py --stats         # print DB status
python3 scripts/etf_threefactor.py --healthcheck   # env check (akshare/data sources/DB), exit 1 on failure
python3 scripts/etf_threefactor.py --backfill      # one-shot full 60-day share history backfill (no 20-day cap)
python3 scripts/etf_threefactor.py --query --days 7 [--code 510300]  # query signal history from SQLite
python3 scripts/etf_threefactor.py --etf "CODE[:name[:index]]"      # ad-hoc attach extra ETF (data view only; direction factor assumes broad-based rescue)
bash setup.sh                                          # one-shot deploy: dirs, copy scripts, install akshare
python3 tests/test_events.py                           # event-anchor regression tests (offline fixtures; --list/--build)
python3 tests/sensitivity.py                           # factor-weight simplex scan over those fixtures (--step/--top)
python3 scripts/calibrate.py fetch|vr|report|gate     # quantile calibration (dataset cached in tests/data/calib/) + report + candidate-mapping gate
```

Any change to model constants (factor mappings / weights / thresholds) MUST follow `references/model_calibration_runbook.md`: calibrate report → gate variant offline → land only after test_events 8/8 + sensitivity margin drop ≤1 → append a dated landing record to references/calibration.md. Anchor expectations come from public facts and are never loosened to make a variant pass.

- Only external dependency: akshare (everything else is stdlib).
- `scripts/etf_data_store.py` also runs standalone to print DB stats.
- Verify model changes with `tests/test_events.py` — offline fixtures built from real national-team buying events: 4 positive anchors (2023-10 first announcement, 2024-02 scope expansion, 2025-04 intraday announcement, 2026-07 record week) + 4 data-driven calm-week negative anchors (2019-01, 2021-04, 2023-02, 2026-08). Expectations come from public facts: investigate failures (source drift / model blind spot) instead of loosening them. Rebuild fixtures with `--build [--anchor id]` (needs network; each anchor sweeps ~65 SSE share queries).
- `tests/sensitivity.py` scans the factor-weight simplex over those fixtures (feasibility boundary + separation margin). Known finding at 8 windows: baseline (0.5/0.2/0.3) is feasible and sits near the top of the margin ranking (+11.4 after the VAR-C sprob landing), squeezed between two opposite constraints — lowering w_vol below 0.5 (or shrinking w_dir) trips neg-2021-04 (late-stage bubble-era broad share drift scores one ETF ≥70), while raising w_dir to 0.25 trips ev-2026-07. Don't touch weights without new event anchors.
- `scripts/calibrate.py` builds the full-market share-Δ% dataset (SSE daily 2020+, SZSE ≥2024 in ~half-year chunks; resume-safe, cached as gzip CSV **inside the repo** at `tests/data/calib/`), generates `references/calibration.md`, and has a `gate` subcommand that offline-tests candidate sprob mappings against the fixtures without touching main code. The VAR-C mapping (2026-08-27) was adopted through that gate — see calibration.md §8 for definition and gate results.
- Smoke-test report/pipeline changes by running the pipeline and reading the per-ETF summary lines (信号分 value, 三因子/二因子 model flag).
- **On this machine**: Homebrew Python 3.14 is PEP 668-externally-managed, so `pip3 install akshare` fails. setup.sh falls back to a venv at `~/.etf-skill/venv` (installed via the Aliyun PyPI mirror) — run scripts with `~/.etf-skill/venv/bin/python`.

## Architecture

### Pipeline (`scripts/etf_threefactor.py`, `main()`)

1. Fetch 沪深300 index K-line (`fetch("sh000300", 60)`) — the market baseline for the direction factor.
2. Load share history — SQLite `shares_raw` first (full accumulation), falling back to the capped 60-day JSON; if < 60 days, backfill from akshare (capped at 20 trading days per run, so a fresh install converges over several runs).
3. Record today's shares into SQLite under their published `data_date` (live runs) or fetch the target date's shares (`--date` runs).
4. For each ETF in `ETFS`: fetch K-line, upsert it into `klines_raw`, run `analyze_all()` over the last 60 bars (~39 trading days) → per-day records with vp/dp/sp/cp.
5. Save results to SQLite, render the interactive per-ETF HTML report, write JSON.

### Three-factor model

`cp = vp×0.5 + dp×0.2 + sp×0.3`

- **量能 vp (50%)** — `vprob()`: piecewise-linear map of volume ratio (day volume ÷ 20-day MA). Steeper mapping for volume expansion than contraction.
- **方向 dp (20%)** — `dprob()`: weighted blend of 4 sub-dimensions (f1 当日行情 40%, f2 ETF vs 沪深300 超额 30%, f3 前5日大盘走势 20%, f4 尾盘固定35% 10%), then a rally discount (×0.6–0.9) when the index rose sharply.
- **份额 sp (30%)** — `sprob()`: map of day-over-day share change % (一级市场申购/赎回) to probability. Per-ETF: `analyze_all(..., code)` uses each ETF's own share change (a 2026-08 fix — it previously used 510300's shares for every ETF).
- **Fallback**: when share data is unavailable for a day, `analyze_all()` degrades to two-factor `cp = vp×0.7 + dp×0.3` and sets `has_shares: false` — reports label that day "二因子". This happens on old history dates and when share data hasn't been published yet.
- Signal thresholds: 🔴 ≥70, 🟡 50–70, ⚪ <50. Displayed as **信号分** (with 量能分/方向分/份额分) in the report and CLI — a heuristic strength score in 0–100, *not* a calibrated probability. Internal names keep the legacy `_prob` vocabulary (`vprob/dprob/sprob`, `cp`, DB columns `composite_prob` etc.); the mismatch with display wording is intentional — don't rename storage schema. The `SPECIAL` dict maps specific dates to manual tags shown in reports (e.g. "五一前") — dates are hardcoded and go stale; update for new holidays.

### Data sources

| Data | Source | Notes |
|------|--------|-------|
| ETF/index K-line | Tencent `web.ifzq.gtimg.cn` qfq daily | 60-day lookback max; `fetch()` infers sh/sz prefix from the code |
| SSE ETF shares | `akshare.fund_etf_scale_sse(date)` | per-date full-market query; results cached in `_SSE_CACHE` |
| SZSE ETF shares | `akshare.fund_scale_daily_szse(start,end)` | date-range query; cached in `_SZSE_CACHE` |

- Share data publishes ~19:00 on trading days; the fetchers search for the latest published value (SSE retries today → -2 days, SZSE up to 7 days back). The report badge treats <15:30 as intraday (K-line not final), 15:30–19:00 as shares-not-published (two-factor), ≥19:00 as complete.
- The old v6 script (dead push2.eastmoney.com source) was removed — git history has it.

### Storage (`scripts/etf_data_store.py`)

SQLite at `~/.etf-skill/workspace/etf_history.db` with three table groups:
- `etf_daily` — analysis results (PK date+code; columns mirror model outputs plus `signal_level`).
- `klines_raw` — raw Tencent K-line (PK code+date), upserted every run; accumulates beyond the 60-day API window.
- `shares_raw` — raw exchange shares (PK date+code), full history, no 60-day cap. Shares are stored under their **actual data date** (`data_date`, published ~19:00) — an intraday run stores yesterday's published values under yesterday, so today correctly has no share factor (two-factor fallback). `etf_shares_history.json` remains as a capped 60-day backup.

The main script imports `ETFDataStore` via a sys.path shim at the top; share loading is DB-first with JSON fallback.

### Configuration (env vars, defaults in code)

- `ETF_WORKSPACE` — output dir (default `~/.etf-skill/workspace`).
- To monitor different ETFs, edit the `ETFS` dict in `scripts/etf_data_store.py` — the main script imports it from there (single source of truth). Exchange is inferred from the code prefix (159/16 → SZSE, everything else → SSE).

## Notes

- All HTTP uses a no-verify SSL context; network failures are swallowed and degrade data silently (`fetch()` returns `[]` on error), which cascades into the two-factor fallback rather than failing the run.
- Output files land in the workspace: `ETF三因子分析.html`, `ETF三因子分析.json`, `etf_history.db`, `etf_shares_history.json`.
