# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

ETF 三因子监测系统 (three-factor ETF monitoring system) — detects potential "national team" (中央汇金) buying signals in Chinese broad-based ETFs via a three-factor probability model. This repo is the source for a Claude Code skill: `SKILL.md` is the skill entry point, and the scripts are normally deployed to `~/.etf-skill/scripts/` in production (see README), so all workspace paths resolve to `~/.etf-skill/workspace` at runtime.

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
bash setup.sh                                          # one-shot deploy: dirs, copy scripts, install akshare
python3 tests/test_events.py                           # event-anchor regression tests (offline fixtures; --list/--build)
```

- Only external dependency: akshare (everything else is stdlib).
- `scripts/etf_data_store.py` also runs standalone to print DB stats.
- There are no tests; verify a change by running the pipeline and reading the per-ETF summary lines (CP value, 三因子/二因子 model flag).
- **On this machine**: Homebrew Python 3.14 is PEP 668-externally-managed, so `pip3 install akshare` fails. setup.sh falls back to a venv at `~/.etf-skill/venv` (installed via the Aliyun PyPI mirror) — run scripts with `~/.etf-skill/venv/bin/python`.

## Architecture

### Pipeline (`scripts/etf_threefactor.py`, `main()`)

1. Fetch 沪深300 index K-line (`fetch("sh000300", 60)`) — the market baseline for the direction factor.
2. Load share history from `etf_shares_history.json`; if < 60 days, backfill from akshare (capped at 20 trading days per run, so a fresh install converges over several runs).
3. Record today's shares into SQLite (live runs) or fetch the target date's shares (`--date` runs).
4. For each ETF in the global `ETFS` dict (7 monitored ETFs): fetch K-line, run `analyze_all()` over the last ~35 days → per-day records with vp/dp/sp/cp.
5. Save results to SQLite, render the interactive per-ETF HTML report, write JSON.

### Three-factor model

`cp = vp×0.5 + dp×0.2 + sp×0.3`

- **量能 vp (50%)** — `vprob()`: piecewise-linear map of volume ratio (day volume ÷ 20-day MA). Steeper mapping for volume expansion than contraction.
- **方向 dp (20%)** — `dprob()`: weighted blend of 4 sub-dimensions (f1 当日行情 40%, f2 ETF vs 沪深300 超额 30%, f3 前5日大盘走势 20%, f4 尾盘固定35% 10%), then a rally discount (×0.6–0.9) when the index rose sharply.
- **份额 sp (30%)** — `sprob()`: map of day-over-day share change % (一级市场申购/赎回) to probability. Per-ETF: `analyze_all(..., code)` uses each ETF's own share change (a 2026-08 fix — it previously used 510300's shares for every ETF).
- **Fallback**: when share data is unavailable for a day, `analyze_all()` degrades to two-factor `cp = vp×0.7 + dp×0.3` and sets `has_shares: false` — reports label that day "二因子". This happens on old history dates and when share data hasn't been published yet.
- Signal thresholds: 🔴 ≥70, 🟡 50–70, ⚪ <50. The `SPECIAL` dict maps specific dates to manual tags shown in reports (e.g. "五一前") — dates are hardcoded and go stale; update for new holidays.

### Data sources

| Data | Source | Notes |
|------|--------|-------|
| ETF/index K-line | Tencent `web.ifzq.gtimg.cn` qfq daily | 60-day lookback max; `fetch()` infers sh/sz prefix from the code |
| SSE ETF shares | `akshare.fund_etf_scale_sse(date)` | per-date full-market query; results cached in `_SSE_CACHE` |
| SZSE ETF shares | `akshare.fund_scale_daily_szse(start,end)` | date-range query; cached in `_SZSE_CACHE` |

- Share data publishes ~19:00 on trading days; the fetchers search today → yesterday → earlier (up to 7 days back for SZSE) for the latest published value.
- The old v6 script (dead push2.eastmoney.com source) was removed — git history has it.

### Storage (`scripts/etf_data_store.py`)

SQLite at `~/.etf-skill/workspace/etf_history.db` with three table groups:
- `etf_daily` — analysis results (PK date+code; columns mirror model outputs plus `signal_level`).
- `klines_raw` — raw Tencent K-line (PK code+date), upserted every run; accumulates beyond the 60-day API window.
- `shares_raw` — raw exchange shares (PK date+code), full history, no 60-day cap. Shares are stored under their **actual data date** (`data_date`, published ~19:00) — an intraday run stores yesterday's published values under yesterday, so today correctly has no share factor (two-factor fallback). `etf_shares_history.json` remains as a capped 60-day backup.

The main script imports `ETFDataStore` via a sys.path shim at the top; share loading is DB-first with JSON fallback.

### Configuration (env vars, defaults in code)

- `ETF_WORKSPACE` — output dir (default `~/.etf-skill/workspace`).
- To monitor different ETFs, edit the `ETFS` dict — it's duplicated in `etf_data_store.py`, keep both in sync. Exchange is inferred from the code prefix (159/16 → SZSE, everything else → SSE).

## Notes

- All HTTP uses a no-verify SSL context; network failures are swallowed and degrade data silently (`fetch()` returns `[]` on error), which cascades into the two-factor fallback rather than failing the run.
- Output files land in the workspace: `ETF三因子分析.html`, `ETF三因子分析.json`, `etf_history.db`, `etf_shares_history.json`.
