#!/usr/bin/env python3
"""
sprob / vprob 分位数校准工具
============================
用全市场经验分布为三因子模型的手工阈值赋予分位数语义，产出对比报告。
本工具只做统计与报告——不修改任何模型常数。候选映射的采纳是独立决策，
需通过 tests/test_events.py 8 锚点回归 + tests/sensitivity.py 门禁。

子命令:
  fetch    拉取全市场份额数据集 (SSE 逐日 2020起 + SZSE 半年分块仅2024+, 接口限制)
           断点续传: 中断后重跑自动跳过已有日期
  vr       监控池 7 只 ETF 的 sina 全历史 → 滚动20日倍量分布
  report   计算分位数并生成 references/calibration.md

缓存位于 {ETF_WORKSPACE}/calib/ (gzip CSV, 不入库); 报告入库。
"""

import argparse
import csv
import gzip
import json
import os
import sys
from datetime import date, datetime, timedelta

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)
for p in (_SCRIPT_DIR, _REPO_ROOT, os.path.join(_REPO_ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from etf_data_store import ETFS

WORKSPACE = os.path.expanduser(os.environ.get("ETF_WORKSPACE", "~/.etf-skill/workspace"))
# 校准数据集随仓库保存 (raw csv.gz), 便于复现与离线重分析
CALIB_DIR = os.environ.get("ETF_CALIB_DIR", os.path.join(_REPO_ROOT, "tests", "data", "calib"))
SSE_CSV = os.path.join(CALIB_DIR, "shares_sse.csv.gz")
SZSE_CSV = os.path.join(CALIB_DIR, "shares_szse.csv.gz")
IDX_JSON = os.path.join(CALIB_DIR, "idx.json")
VR_CSV = os.path.join(CALIB_DIR, "vr.csv.gz")
REPORT_MD = os.path.join(_REPO_ROOT, "references", "calibration.md")

QUANTILES = [0.005, 0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.975, 0.99, 0.995, 0.999]

# 当前模型的手工阈值 (对照对象)
SPROB_BANDS = [1.0, 3.0, 5.0, 10.0]      # ±% 双侧对称使用绝对值
VPROB_BREAKS = [0.5, 1.0, 1.3, 1.5, 2.0, 3.0, 5.0]
# 候选"分位数锚定"骨架: 分数档位沿用现有语义, 锚点换成经验分位
CANDIDATE_POS_P = [0.50, 0.75, 0.90, 0.975, 0.995]   # 申购侧分数锚点对应的分位
CANDIDATE_NEG_Q = [0.25, 0.10, 0.05, 0.01]            # 赎回侧 (下尾分位)
MIN_BUCKET_N = 200


# ============================================================
# 基础 IO
# ============================================================

def _ensure_dir():
    os.makedirs(CALIB_DIR, exist_ok=True)


def _read_csv_gz(path):
    if not os.path.exists(path):
        return []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _append_csv_gz(path, rows, header):
    new_file = not os.path.exists(path)
    with gzip.open(path, "at", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if new_file:
            w.writeheader()
        if rows:
            w.writerows(rows)


def _trading_days(start, end):
    import akshare as ak
    if os.path.exists(IDX_JSON):
        with open(IDX_JSON, encoding="utf-8") as f:
            days = json.load(f)
    else:
        df = ak.stock_zh_index_daily(symbol="sh000300")
        days = sorted(str(d)[:10] for d in df["date"])
        with open(IDX_JSON, "w", encoding="utf-8") as f:
            json.dump(days, f)
    return [d for d in days if start <= d <= end]


# ============================================================
# fetch: 全市场份额数据集 (断点续传)
# ============================================================

def cmd_fetch(args):
    import akshare as ak
    _ensure_dir()
    end = datetime.now().strftime("%Y-%m-%d")
    days = _trading_days(args.start, end)

    # --- SSE 全网格 ---
    done = {r["date"] for r in _read_csv_gz(SSE_CSV)}
    todo = [d for d in days if d not in done]
    print(f"SSE: 目标 {len(days)} 日 [{args.start}~{end}], 已缓存 {len(done)}, 待拉取 {len(todo)}")
    import time as _time
    last_rows = []
    n_err = 0
    for i, d in enumerate(todo):
        t0 = datetime.now()
        rows = []
        # 重试退避: 批量连续调用易触发限流/代理拒绝
        for attempt, wait in enumerate((0, 1.5, 4.0)):
            try:
                if wait:
                    _time.sleep(wait)
                df = ak.fund_etf_scale_sse(date=d.replace("-", ""))
                if df is not None and len(df) and "基金代码" in df.columns:
                    for _, r in df.iterrows():
                        try:
                            sh = float(r["基金份额"])
                            if sh > 0:
                                rows.append({"date": d, "code": str(r["基金代码"]), "shares": sh})
                        except (ValueError, TypeError):
                            continue
                break
            except KeyboardInterrupt:
                raise
            except Exception as e:
                if attempt == 2:
                    n_err += 1
                    print(f"\n  ⚠️ {d} {type(e).__name__} ×3, 跳过", end="")
        _append_csv_gz(SSE_CSV, rows, ["date", "code", "shares"])
        _time.sleep(0.12)
        if (i + 1) % 100 == 0 or i == len(todo) - 1:
            print(f"\n  [{i+1}/{len(todo)}] 最新 {d} ({len(rows)} 行, "
                  f"{(datetime.now()-t0).total_seconds():.2f}s, 累计失败 {n_err})", end="")
    print()

    # --- SZSE 半年分块 (数据源仅覆盖 2024+) ---
    sz_start = max(args.start, "2024-01-01")
    done = {r["date"] for r in _read_csv_gz(SZSE_CSV)}
    chunks, cur = [], date.fromisoformat(sz_start)
    stop = date.fromisoformat(end)
    while cur < stop:
        nxt = min(cur + timedelta(days=170), stop)
        chunks.append((cur.isoformat(), nxt.isoformat()))
        cur = nxt + timedelta(days=1)
    todo_chunks = [(s, e) for s, e in chunks
                   if any(d not in done for d in days if s <= d <= e)]
    print(f"SZSE: 待拉取 {len(todo_chunks)}/{len(chunks)} 块 (≥2024-01, 上半年约120日/次)")
    for s, e in todo_chunks:
        try:
            df = ak.fund_scale_daily_szse(start_date=s.replace("-", ""),
                                          end_date=e.replace("-", ""), symbol="ETF")
            rows = []
            if df is not None and len(df):
                for _, r in df.iterrows():
                    try:
                        sh = float(r["基金份额"])
                        if sh > 0:
                            rows.append({"date": str(r["日期"])[:10].replace("-", ""),
                                         "code": str(r["基金代码"]), "shares": sh})
                    except (ValueError, TypeError):
                        continue
            _append_csv_gz(SZSE_CSV, rows, ["date", "code", "shares"])
            n_days = len({r["date"] for r in rows})
            print(f"  ✅ {s}~{e}: {n_days} 日 / {len(rows)} 行")
        except Exception as e:
            print(f"  ⚠️ {s}~{e} {type(e).__name__}")

    print()
    for name, path in [("SSE", SSE_CSV), ("SZSE", SZSE_CSV)]:
        rs = _read_csv_gz(path)
        ds = sorted({r["date"] for r in rs})
        rng = f"{ds[0]}~{ds[-1]}" if ds else "-"
        print(f"{name}: {len(rs):,} 行 / {len(ds)} 日 [{rng}]" if ds else f"{name}: 空")


# ============================================================
# Δ% 观测集与统计
# ============================================================

def load_observations(min_listing_bars=20):
    """合并双源缓存 → 相邻交易日 Δ% 观测列表
    返回 [dict(src,date,delta_pct,prev_shares)]"""
    all_days = set()
    maps = {}
    for src, path in (("sse", SSE_CSV), ("szse", SZSE_CSV)):
        m = {}
        bad_tail = False
        for r in _read_csv_gz(path):
            try:
                sh = float(r["shares"])
            except (ValueError, TypeError, KeyError):
                bad_tail = True   # 断点续传中断可能留下半行尾
                continue
            if sh > 0:
                m[(r["date"], r["code"])] = sh
                all_days.add(r["date"])
        if bad_tail:
            print(f"⚠️ {path} 含坏行(已忽略; 由上次中断残留), 建议重跑 fetch 续传该日期")
        maps[src] = m
    ordered = sorted(all_days)
    idx_of = {d: i for i, d in enumerate(ordered)}
    obs = []
    for src, m in maps.items():
        first_seen = {}
        keys = sorted(m.keys())
        for d, c in keys:
            i = idx_of[d]
            if c not in first_seen:
                first_seen[c] = i
        for d, c in keys:
            i = idx_of.get(d)
            if i is None or i == 0 or i - first_seen[c] < min_listing_bars:
                continue
            prev = m.get((ordered[i - 1], c))
            cur = m[(d, c)]
            if prev and prev > 0 and cur > 0:
                obs.append({"src": src, "date": d,
                            "delta_pct": round((cur - prev) / prev * 100.0, 4),
                            "prev_shares": prev})
    return obs


def qvalues(values, ps=QUANTILES):
    vs = sorted(values)
    sps = sorted(set(ps))                      # 计算按升序; 返回保留调用方键序
    tmp = {}
    for p in sps:
        k = (len(vs) - 1) * p
        lo = int(k)
        hi = min(lo + 1, len(vs) - 1)
        tmp[p] = round(vs[lo] + (vs[hi] - vs[lo]) * (k - lo), 4)
    seq = [tmp[p] for p in sps]
    assert all(seq[i] <= seq[i + 1] for i in range(len(seq) - 1)), "分位数非单调"
    return {p: tmp[p] for p in ps}


def qlabel(p):
    return f"P{p*100:g}"


def top_exceedance(values, x):
    """x 的右侧超越比例 (%)"""
    return sum(1 for v in values if v > x) / len(values) * 100.0


def bottom_exceedance(values, x):
    """|x| 的左侧下尾比例 (%)"""
    return sum(1 for v in values if v < x) / len(values) * 100.0


def size_decile_stats(obs, n_buckets=10):
    srt = sorted(obs, key=lambda o: o["prev_shares"])
    step = len(srt) // n_buckets
    rows = []
    for bi in range(n_buckets):
        chunk = srt[bi*step:(bi+1)*step if bi < n_buckets-1 else None]
        ds = [o["delta_pct"] for o in chunk]
        hi = qvalues(ds, [0.95, 0.99])
        lo = qvalues(ds, [0.05])
        rows.append({
            "bucket": bi + 1,
            "prev_lo": chunk[0]["prev_shares"], "prev_hi": chunk[-1]["prev_shares"],
            "n": len(chunk),
            "p95_up": hi[0.95], "p99_up": hi[0.99], "p05_dn": lo[0.05],
        })
    return rows


# ============================================================
# vr 子命令
# ============================================================

def cmd_vr(args):
    # _sina_kline 依赖仓库内 tests/test_events.py — 部署形态下 fetch/report/gate 不需要它
    try:
        sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))
        from test_events import _sina_kline
    except ImportError:
        sys.exit("vr 需要 git 检出仓库中的 tests/（部署目录无此模块）; fetch/report/gate 不受影响")
    _ensure_dir()
    per_code = {}
    for code, info in ETFS.items():
        bars = _sina_kline(code)
        items = []
        for i in range(len(bars)):
            if i < 20:
                continue
            ma = sum(bars[j]["v"] for j in range(i-20, i)) / 20.0
            if ma > 0:
                items.append((bars[i]["date"], round(bars[i]["v"] / ma, 6)))
        per_code[code] = items
        rng = f"{items[0][0]}~{items[-1][0]}" if items else "-"
        print(f"  {code} {info['n'][:10]}: {len(items)} 样本 [{rng}]")

    with gzip.open(VR_CSV, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "date", "vr"])
        for code, items in per_code.items():
            w.writerows((code, d, v) for d, v in items)

    pooled = [v for items in per_code.values() for _, v in items]
    result = {
        "n": len(pooled),
        "range": [round(min(pooled), 4), round(max(pooled), 4)],
        "quantiles": {qlabel(p): v for p, v in qvalues(pooled).items()},
    }
    with open(os.path.join(CALIB_DIR, "vr_quantiles.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\npooled n={result['n']}  quantiles={json.dumps(result['quantiles'])}")


# ============================================================
# report 子命令
# ============================================================

def cmd_report(args):
    obs = load_observations()
    deltas = [o["delta_pct"] for o in obs]
    assert len(deltas) > MIN_BUCKET_N * 10, f"样本不足 ({len(deltas)})"

    overall = qvalues(deltas, QUANTILES)
    half_a = [o["delta_pct"] for o in obs if o["date"] < "2023-01-01"]
    half_b = [o["delta_pct"] for o in obs if o["date"] >= "2023-01-01"]
    qa, qb = qvalues(half_a, QUANTILES), qvalues(half_b, QUANTILES)
    drift = max(abs(qa[p] - qb[p]) for p in QUANTILES)

    pos = [d for d in deltas if d > 0]
    neg_abs = [abs(d) for d in deltas if d < 0]
    qpos_right = qvalues(pos, CANDIDATE_POS_P)
    qneg_tail = qvalues(neg_abs, CANDIDATE_POS_P)   # 赎回侧与申购侧镜像: 取幅度高分位
    deciles = size_decile_stats(obs)

    # sprob 阈值 ↔ 经验位置
    sp_rows = []
    for b in SPROB_BANDS:
        up_all = top_exceedance(deltas, b)
        dn_all = bottom_exceedance(deltas, -b)
        ups = [top_exceedance([o["delta_pct"] for o in dec_obs], b)
               for dec_obs in ([o for o in obs if lo <= o["prev_shares"] < hi]
                               for lo, hi in _decile_bounds(obs))]
        dns = [bottom_exceedance([o["delta_pct"] for o in dec_obs], -b)
               for dec_obs in ([o for o in obs if lo <= o["prev_shares"] < hi]
                               for lo, hi in _decile_bounds(obs))]
        sp_rows.append((b, up_all, dn_all,
                        min(u for u in ups if True), max(ups),
                        min(dns), max(dns)))

    # vr 分布
    vr_path = VR_CSV
    vrs = []
    if os.path.exists(vr_path):
        with gzip.open(vr_path, "rt", encoding="utf-8") as f:
            vrs = [float(r["vr"]) for r in csv.DictReader(f)]
    vr_q = qvalues(vrs, QUANTILES) if vrs else {}

    lines = []
    A = lines.append
    A("# sprob / vprob 分位数校准报告\n")
    A(f"> 生成时间：{datetime.now().isoformat(timespec='seconds')} · "
      f"窗口：{args.start} 至今 · 工具：`scripts/calibrate.py`\n")
    A("> **性质**：参考报告，未修改任何模型常数。候选映射的采纳需另跑\n"
      "> `tests/test_events.py`（8 锚点）与 `tests/sensitivity.py` 门禁。\n")

    A("## 1 数据集\n")
    A("| 项 | 值 |")
    A("|----|----|")
    A(f"| Δ% 观测数 | {len(obs):,} |")
    A(f"| 正向（申购）| {len(pos):,} ({len(pos)/len(obs)*100:.1f}%) |")
    A(f"| 负向（赎回）| {len(neg_abs):,} ({len(neg_abs)/len(obs)*100:.1f}%) |")
    A(f"| 对半分割漂移（13 个分位的最大差）| {drift:.2f} pp（2020-22 vs 2023+）|")
    A("| SZSE 覆盖 | 仅 2024-01 起（接口限制）|\n")

    A("## 2 全体 Δ% 经验分位数（%）\n")
    A("| 统计 | 值 | 2020-22 | 2023+ |")
    A("|------|-----|---------|-------|")
    for p in QUANTILES:
        A(f"| {qlabel(p)} | {overall[p]:+.3f} | {qa[p]:+.3f} | {qb[p]:+.3f} |")
    A("")

    A("## 3 当前 sprob 阈值在经验分布中的位置\n")
    A("超越比例 = 全体样本中变动超过该阈值的占比；分层范围 = 规模十分位中的最小~最大（局限6 的量化证据）。\n")
    A("| 阈值(±%) | 申购侧超越% | 赎回侧超越% | 申购超越·分层范围 | 赎回超越·分层范围 | 现行分数 |")
    A("|----------|-------------|-------------|--------------------|--------------------|----------|")
    score_map = {1.0: "45~65", 3.0: "65~80", 5.0: "80~95", 10.0: "=95"}
    for b, ua, da, ulo, uhi, dlo, dhi in sp_rows:
        A(f"| ±{b:g}% | {ua:.2f}% | {da:.2f}% | {ulo:.1f}%~{uhi:.1f}% | {dlo:.1f}%~{dhi:.1f}% | {score_map[b]} |")
    A("")

    A("## 4 候选分位数锚定映射（仅建议，未应用）\n")
    A("保持现行分段形状与单调性，仅把锚点数值换成分位数位置；以下由本次分布直接算出：\n")
    A("**申购侧**（正变动右尾，分位在正样本内取）：\n")
    pos_ladder = ["≈45（小幅申购）", "≈65（中等）", "≈80（较大）", "≈90（大规模）", "≥95（极端）"]
    A("| 建议分数档 | 锚定分位 | 实测边界 |")
    A("|------------|----------|----------|")
    for i, p in enumerate(CANDIDATE_POS_P):
        A(f"| {pos_ladder[i]} | {qlabel(p)} | {qpos_right[p]:+.2f}% |")
    A("")
    A("**赎回侧**（负变动幅度右尾，与申购侧镜像；分位在赎回样本内取）：\n")
    A("| 建议分数档 | 锚定分位 | 实测边界（%） |")
    A("|------------|----------|----------------|")
    neg_ladder = ["≈10–15（轻微）", "≈8（一般）", "≈6（明显）", "≈4（严重）", "<=3（大幅疑似减持）"]
    cur_sym5 = dict(zip(CANDIDATE_POS_P, SPROB_BANDS + [None]))
    for i, p in enumerate(CANDIDATE_POS_P):
        ref = f"现行对称 ±{SPROB_BANDS[i]:g}%" if i < len(SPROB_BANDS) else "现行 =95 档外"
        A(f"| {neg_ladder[i]} | {qlabel(p)} | {qneg_tail[p]:.2f}%（{ref}）|")
    A("")

    A("## 5 规模十分位分层（回应局限 6）\n")
    A("| 十分位 | 前日份额区间(亿份→份原值) | n | P95 申购% | P99 申购% | P5 赎回% |")
    A("|--------|---------------------------|---|-----------|-----------|----------|")
    for r in deciles:
        A(f"| D{r['bucket']} | {r['prev_lo']:.0f} ~ {r['prev_hi']:.0f} | {r['n']:,} "
          f"| {r['p95_up']:+.2f} | {r['p99_up']:+.2f} | {r['p05_dn']:+.2f} |")
    A("")

    if vrs:
        A("## 6 vprob 倍量经验分位数（监控池 7 只 sina 全历史）\n")
        A("| 统计 | 值 |")
        A("|------|-----|")
        for p in QUANTILES:
            A(f"| {qlabel(p)} | {vr_q[p]:.3f}x |")
        A("")
        A("当前断点的超越比例：\n")
        A("| 断点 | ≥x 占比 | 现行量能分 |")
        A("|------|---------|------------|")
        vmap = {0.5: "≤5", 1.0: "5~17", 1.3: "17→35", 1.5: "35~55", 2.0: "55~80", 3.0: "80~95", 5.0: "95+"}
        for vb in VPROB_BREAKS:
            A(f"| {vb:g}x | {sum(1 for v in vrs if v >= vb)/len(vrs)*100:.2f}% | {vmap[vb]} |")
        A("")

    A("## 7 结论速记（填写于人工复核后）\n")
    A("- [ ] sprob ±1% 对应的全体超越比例：____ （判断该档是否过松/过紧）")
    A("- [ ] 规模分层内同一阈值的超越比例极差：____ （量化局限 6）")
    A("- [ ] vprob 1.3x/2x 的稀有度是否支撑现行分数跳变")
    A("- [ ] 是否采纳候选映射 → 若采纳，跑 `test_events` + `sensitivity` 门禁\n")

    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"✅ {REPORT_MD} ({os.path.getsize(REPORT_MD)} bytes)")


def _decile_bounds(obs, n_buckets=10):
    srt = sorted(o["prev_shares"] for o in obs)
    step = len(srt) // n_buckets
    bounds = []
    for bi in range(n_buckets):
        lo = srt[bi*step]
        hi = srt[(bi+1)*step] if bi < n_buckets-1 else srt[-1]*10**9
        bounds.append((lo if bi == 0 else srt[bi*step],
                       hi if bi < n_buckets-1 else float("inf")))
    return bounds


# ============================================================
# gate 子命令: 在 fixtures 上离线试跑候选 sprob 映射
# ============================================================

def _mk_piecewise(points, neg_points=None):
    """构造 sprob 变体函数。points 正向锚点升序[(Δ%,score)]; neg_points 同格式作用于 |Δ|;
    自动在 0% 处锚定中性 15 分并向首点线性过渡。"""
    pos = [(0.0, 15)] + list(points)
    neg = [(0.0, 15)] + list(neg_points or [])

    def fn(d):
        if d is None:
            return None
        table = pos if d >= 0 else neg
        ad = abs(d)
        if ad <= table[0][0]:
            return table[0][1]
        for (x1, s1), (x2, s2) in zip(table, table[1:]):
            if ad <= x2:
                return round(s1 + (ad - x1) / (x2 - x1) * (s2 - s1), 1)
        return table[-1][1]
    return fn


SPROB_CURRENT = None  # 运行时从主模块导入
# 候选B: 报告§4 全量分位锚定 (申购P50/75/90/97.5/99.5→45/65/80/90/95; 赎回镜像高分位→12/8/6/4/3)
SPROB_CAND_B_POS = [(0.82, 45), (2.05, 65), (4.74, 80), (13.02, 90), (41.91, 95)]
SPROB_CAND_B_NEG = [(0.87, 12), (1.95, 8), (3.90, 6), (8.42, 4), (16.98, 3)]
# 温和变体C: 仅收紧噪声区 (0~1% 低坡化), 中高段保持现行骨架
SPROB_VAR_C_POS = [(1.0, 40), (3.0, 60), (5.0, 80), (10.0, 93)]
SPROB_VAR_C_NEG = [(1.0, 11), (5.0, 7), (10.0, 4)]


def _mk_current_sprob():
    from etf_threefactor import sprob
    return sprob


def _evaluate(prepared, sprob_fn, thresh=70):
    """按基准权重重组全部锚点; 返回 {aid: 判定详情} 与总体可行性"""
    per = {}
    ok_all = True
    for item in prepared:
        a = item["anchor"]
        w0, w1 = a["window"]
        day_stats = {}
        for code, hist in item["recs"].items():
            for h in hist:
                d = h["d"]
                if not (w0 <= d <= w1):
                    continue
                sp_v = sprob_fn(h["share_delta_pct"]) if h["share_delta_pct"] is not None else None
                if sp_v is None:
                    cp = round(h["vp"] * 0.7 + h["dp"] * 0.3, 1)
                else:
                    cp = round(h["vp"] * 0.5 + h["dp"] * 0.2 + sp_v * 0.3, 1)
                st = day_stats.setdefault(d, {"high": 0})
                if cp >= thresh:
                    st["high"] += 1
        exp = a["expect"]
        max_hi = max((s["high"] for s in day_stats.values()), default=0)
        if "max_high_per_day" in exp:
            feas = all(s["high"] <= exp["max_high_per_day"] for s in day_stats.values())
            per[a["id"]] = {"feasible": feas}
        else:
            n_strong = sum(1 for s in day_stats.values() if s["high"] >= exp.get("min_high", 2))
            cond = True
            if "min_high" in exp:
                cond &= n_strong >= exp.get("min_days", 1)
            if "best_day_high" in exp:
                cond &= max_hi >= exp["best_day_high"]
            feas = bool(cond)
            per[a["id"]] = {"feasible": feas, "n_strong_days": n_strong, "peak_high": max_hi}
        ok_all &= feas
    return ok_all, per


def cmd_gate(args):
    sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))
    from sensitivity import prepare
    from etf_threefactor import sprob as sprob_cur

    variants = {
        "CURRENT": ("现行映射", sprob_cur),
        "CAND-B": ("报告§4 分位全量锚定",
                   _mk_piecewise(SPROB_CAND_B_POS, SPROB_CAND_B_NEG)),
        "VAR-C": ("温和收紧(仅0~1%噪声区)", _mk_piecewise(SPROB_VAR_C_POS, SPROB_VAR_C_NEG)),
    }
    print("加载 fixtures ...")
    prepared = prepare()
    print(f"{'变体':<9} {'总体':<4} | 各锚点 feasible / 关键指标")
    for name, (_desc, fn) in variants.items():
        ok_all, per = _evaluate(prepared, fn)
        det = []
        for aid, r in per.items():
            f = "✓" if r.get("feasible") else "✗"
            extra = ""
            if "n_strong_days" in r:
                extra = f"(强日{r['n_strong_days']},峰{r['peak_high']})"
            det.append(f"{aid.replace('ev-','').replace('neg-','N')}:{f}{extra}")
        print(f"{name:<9} {'✅' if ok_all else '❌'}   | " + "  ".join(det))


def main():
    ap = argparse.ArgumentParser(description="sprob/vprob 分位数校准工具（只出报告不改模型）")
    ap.add_argument("--start", default="2020-01-01", help="数据起点 (默认 2020-01-01)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch", help="拉取份额数据集（断点续传）")
    sub.add_parser("vr", help="监控池倍量分布")
    sub.add_parser("report", help="生成分位数校准报告")
    sub.add_parser("gate", help="fixtures 上离线试跑候选 sprob 变体（不改主代码）")
    args = ap.parse_args()
    {"fetch": cmd_fetch, "vr": cmd_vr, "report": cmd_report,
     "gate": cmd_gate}[args.cmd](args)


if __name__ == "__main__":
    main()
