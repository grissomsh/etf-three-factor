#!/usr/bin/env python3
"""
三因子权重敏感性分析
====================
在事件锚点 fixtures 上扫描权重单纯形 (w_量能, w_方向, w_份额)，回答两个问题：

1. 现有权重 (0.50, 0.20, 0.30) 离"检测失效"边界还有多少余量？
2. 权重可行域在哪里——哪些组合能让全部锚点保持 PASS？

方法：
- 因子分 (量能/方向/份额) 与权重无关，只算一次；cp 按扫描权重线性重组
- 分离边际 margin = min(正锚点窗口峰值信号分) − max(负锚点窗口峰值信号分)
  （窗口峰值 = 该窗口内任一天、任一ETF 的最高分；margin 越大事件与平静期越可分）
- 可行性判定完全复用 events.json 的期望规则（与 test_events.run_anchor 同一逻辑）

注意（解读红线）：
- 锚点窗口之间高度相关，有效样本 ≈ 事件数。本工具用于划定安全区，不用于"找最优"。
- 最优 margin 组合不应直接采用：它是把 5 个窗口背下来的产物。离基线不远且 comfortably
  feasible 的区域才有参考价值。

用法:
  python3 tests/sensitivity.py                 # 全单纯形扫描 (步长0.05, 231组)
  python3 tests/sensitivity.py --step 0.10     # 粗扫
  python3 tests/sensitivity.py --top 15        # 显示前N名
"""

import argparse
import itertools
import json
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
sys.path.insert(0, os.path.join(_SCRIPT_DIR, "..", "scripts"))

from etf_threefactor import ETFS, analyze_all, get_historical_share
from test_events import FIXTURES_DIR, EVENTS_FILE, _load_fixture

BASELINE = (0.50, 0.20, 0.30)
THRESH_HIGH = 70   # L3 高确信阈值（本工具固定，只扫 L1 权重）


# ============================================================
# 数据准备：每个 fixture 只做一次模型分析，缓存逐日因子分
# ============================================================

def prepare():
    """加载全部锚点并跑一次 analyze_all → {aid: {..., "recs": {code: hist}}}"""
    with open(EVENTS_FILE, encoding="utf-8") as f:
        anchors = json.load(f)["anchors"]
    prepared = []
    for a in anchors:
        aid = a["id"]
        fixture, err = _load_fixture(aid)
        if err:
            print(f"⚠️ {aid}: {err}，跳过该锚点")
            continue
        shares_hist = fixture["shares"]
        shares_map = {}
        for code in ETFS:
            shares_map[code] = {}
            for date, entries in shares_hist.items():
                if isinstance(entries, dict) and code in entries:
                    _, _, dy, dpct = get_historical_share(code, date, shares_hist)
                    shares_map[code][date] = {"shares_yi": entries[code].get("shares_yi"),
                                              "delta_yi": dy, "delta_pct": dpct}
        recs = {}
        for code in ETFS:
            data = fixture["klines"].get(code) or []
            if len(data) >= 22:
                recs[code] = analyze_all(data, fixture["idx"], shares_map, a["window"][0], code, 35)
        prepared.append({"anchor": a, "recs": recs})
    return prepared


def scan_combo(prepared, wv, wd, ws):
    """按给定权重重组 cp，返回 (可行性bool, 正锚点峰值list, 负锚点峰值list)"""
    pos_peaks, neg_peaks = [], []
    ok_all = True
    for item in prepared:
        anchor = item["anchor"]
        w0, w1 = anchor["window"]

        # 逐日统计（重组后的分数）
        day_stats = {}
        for code, hist in item["recs"].items():
            for h in hist:
                d = h["d"]
                if not (w0 <= d <= w1):
                    continue
                if h["sp"] is None:          # 二因子日: 用 70/30 退化口径
                    cp = round(h["vp"] * 0.7 + h["dp"] * 0.3, 1)
                else:
                    cp = round(h["vp"] * wv + h["dp"] * wd + h["sp"] * ws, 1)
                s = day_stats.setdefault(d, {"high": 0, "mid": 0, "peak": 0.0})
                s["peak"] = max(s["peak"], cp)
                if cp >= THRESH_HIGH:
                    s["high"] += 1
                elif cp >= 50:
                    s["mid"] += 1

        peaks = [s["peak"] for s in day_stats.values()]
        strong_days = [d for d, s in sorted(day_stats.items()) if s["high"] >= 2]

        exp = anchor["expect"]
        if "max_high_per_day" in exp:                      # 负锚点
            bad = [d for d, s in sorted(day_stats.items()) if s["high"] > exp["max_high_per_day"]]
            feas = len(bad) == 0
            neg_peaks.extend(peaks)
        else:                                              # 正锚点
            cond = True
            if "min_high" in exp:
                cond &= len(strong_days) >= exp.get("min_days", 1)
            if "best_day_high" in exp:
                cond &= max((s["high"] for s in day_stats.values()), default=0) >= exp["best_day_high"]
            feas = bool(cond)
            pos_peaks.append(max(peaks) if peaks else 0.0)
        ok_all &= feas
    margin = (min(pos_peaks) - max(neg_peaks)) if pos_peaks and neg_peaks else float("nan")
    return ok_all, pos_peaks, neg_peaks, margin


def main():
    ap = argparse.ArgumentParser(description="三因子权重敏感性分析")
    ap.add_argument("--step", type=float, default=0.05, help="单纯形步长 (默认0.05)")
    ap.add_argument("--top", type=int, default=12, help="显示前N名")
    args = ap.parse_args()

    print("加载 fixtures 并计算因子分 ...")
    prepared = prepare()
    n_rec = sum(len(r) for item in prepared for r in item["recs"].values())
    print(f"  {len(prepared)} 个锚点 / {n_rec} 条逐ETF记录\n")

    combos = []
    k = int(round(1 / args.step))
    for i in range(k + 1):
        for j in range(k + 1 - i):
            wv, wd = i * args.step, j * args.step
            combos.append((round(wv, 4), round(wd, 4), round(1 - wv - wd, 4)))

    results = []
    for w in combos:
        ok, pos_p, neg_p, margin = scan_combo(prepared, *w)
        results.append({"w": w, "ok": ok, "margin": margin,
                        "pos_min": min(pos_p) if pos_p else None,
                        "neg_max": max(neg_p) if neg_p else None})

    feas = [r for r in results if r["ok"]]
    infeas = len(results) - len(feas)

    print("=" * 78)
    print(f"🎯 权重单纯形扫描: {len(results)} 组合 (步长{args.step}) | "
          f"可行 {len(feas)} | 违反锚点 {infeas}")
    print("=" * 78)

    base_row = next(r for r in results if r["w"] == BASELINE)
    base_ok, base_margin = base_row["ok"], base_row["margin"]
    b = "✅" if base_ok else "❌"
    print(f"\n当前权重 {BASELINE} → {'✅ 全部锚点PASS' if base_ok else '❌'}  "
          f"margin={base_margin:+.1f}")
    print(f"含义: 最弱事件窗的峰值信号分比平静窗峰值{'高' if base_margin>0 else '低'} {abs(base_margin):.1f} 分")

    feas_sorted = sorted(feas, key=lambda r: r["margin"], reverse=True)
    print(f"\n──── 可行域内 margin 最高前 {args.top} 名 ────")
    print(f"{'w量能':>6} {'w方向':>6} {'w份额':>6} | {'margin':>7} | {'事件窗最低峰':>10} | {'平静窗峰值':>8}")
    for r in feas_sorted[:args.top]:
        wv, wd, ws = r["w"]
        star = " ← 当前" if r["w"] == BASELINE else ""
        print(f"{wv:>6.2f} {wd:>6.2f} {ws:>6.2f} | {r['margin']:>+7.1f} | "
              f"{r['pos_min']:>14.1f} | {r['neg_max']:>8.1f}{star}")

    # 基线邻域稳健性: ±step 直接重扫（不做缓存查表），失败者给出具体违反的锚点
    bv, bd, bs = BASELINE
    st = args.step
    neigh = []
    for da in (-st, 0, st):
        for db in (-st, 0, st):
            wv = round(bv + da, 4)
            wd = round(bd + db, 4)
            ws = round(1 - wv - wd, 4)
            if abs(wv + wd + ws - 1) < 1e-9 and (wv, wd, ws) != BASELINE:
                neigh.append((wv, wd, ws))
    rows = [(w, scan_combo(prepared, *w)) for w in neigh]
    n_ok = sum(1 for _, r in rows if r[0])
    print(f"\n──── 基线邻域 (±{st:g}) ────")
    verdict = ('🟢 不敏感' if n_ok == len(rows)
               else '🟡 部分敏感' if n_ok >= len(rows) / 2 else '🔴 边缘')
    print(f"邻域 {len(rows)} 组中可行 {n_ok} 组 → {verdict}")
    for w, (ok, _, _, mg) in rows:
        marks = "".join(
            f"{item['anchor']['id']}:{'✓' if scan_combo([item], *w)[0] else '✗'} ".strip() + "  "
            for item in prepared)
        flag = "✅" if ok else "❌"
        print(f"  {str(w):>20} {flag} margin={mg:+6.1f}   {marks}")

    if feas_sorted:
        top_w = feas_sorted[0]["w"]
        print(f"\n⚠️ 解读提醒: 最高 margin 组合 {top_w} 是对现有 {len(prepared)} 个窗口的过拟合产物，仅供观察可行域形状，不建议直接采用。")


if __name__ == "__main__":
    main()
