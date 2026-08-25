#!/usr/bin/env python3
"""
ETF三因子模型 — 事件锚点回归测试
=================================
用历史公开事件（国家队增持）作为锚点，验证模型在事件窗口内能触发预期信号。
fixtures 为静态数据快照，测试离线确定性运行，不依赖实时 API。

用法:
  python3 tests/test_events.py            # 运行全部锚点测试
  python3 tests/test_events.py --list     # 列出锚点
  python3 tests/test_events.py --build    # 从 akshare(sina源) 重建 fixtures
  python3 tests/test_events.py --anchor ev-2026-07   # 只跑指定锚点

锚点期望值来自公开事实（见 events.json 的 evidence/source），而非模型调参。
若某个正锚点失败，不要放松期望，应先调查（数据源差异/模型盲区）。
"""

import argparse
import json
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
sys.path.insert(0, os.path.join(_SCRIPT_DIR, "..", "scripts"))

from etf_v7_threefactor import ETFS, analyze_all, get_historical_share, fetch_history_shares_bulk

TESTS_DIR = _SCRIPT_DIR
EVENTS_FILE = os.path.join(TESTS_DIR, "events.json")
FIXTURES_DIR = os.path.join(TESTS_DIR, "fixtures")

# sina K线列名 → 统一格式
BAR_KEYS = ("date", "o", "c", "h", "l", "v")


# ============================================================
# fixtures 构建 (--build)
# ============================================================

def _to_bar(row, volume_key="volume"):
    """sina DataFrame 行 → {date,o,c,h,l,v}"""
    d = row["date"]
    if hasattr(d, "strftime"):
        d = d.strftime("%Y-%m-%d")
    return {
        "date": str(d),
        "o": float(row["open"]),
        "c": float(row["close"]),
        "h": float(row["high"]),
        "l": float(row["low"]),
        "v": float(row.get(volume_key) or 0),
    }


def _sina_kline(code):
    """sina ETF 全历史K线 (unadjusted) → [{date,o,c,h,l,v}]"""
    import akshare as ak
    pfx = "sh" if code.startswith(("51", "56", "58")) else "sz"
    df = ak.fund_etf_hist_sina(symbol=f"{pfx}{code}")
    return [_to_bar(r) for _, r in df.iterrows()]


def _sina_index():
    """sina 沪深300 全历史 → [{date,o,c,h,l,v}]"""
    import akshare as ak
    df = ak.stock_zh_index_daily(symbol="sh000300")
    return [_to_bar(r) for _, r in df.iterrows()]


def build_fixture(anchor):
    aid = anchor["id"]
    w0, w1 = anchor["window"]
    out_dir = os.path.join(FIXTURES_DIR, aid)
    os.makedirs(out_dir, exist_ok=True)
    print(f"\n🔨 {aid} {anchor['name']} ({w0} ~ {w1})")

    # 1. 指数K线 → 交易日历 + 切片(窗口前60交易日至窗口末)
    idx = _sina_index()
    dates = [b["date"] for b in idx]
    if w0 not in dates or w1 not in dates:
        print(f"  ❌ 窗口 {w0}~{w1} 不在指数数据中")
        return False
    i0, i1 = dates.index(w0), dates.index(w1)
    slice_idx = idx[max(0, i0 - 60):i1 + 1]
    with open(os.path.join(out_dir, "idx.json"), "w", encoding="utf-8") as f:
        json.dump(slice_idx, f, ensure_ascii=False)
    print(f"  ✅ 指数 {len(slice_idx)}条 ({slice_idx[0]['date']} ~ {slice_idx[-1]['date']})")

    # 2. 7只ETF K线 (按指数交易日过滤, 保证对齐)
    dates_set = {b["date"] for b in slice_idx}
    klines = {}
    for code in ETFS:
        bars = [b for b in _sina_kline(code) if b["date"] in dates_set]
        klines[code] = bars
        print(f"  ✅ {code} {len(bars)}条")
    with open(os.path.join(out_dir, "klines.json"), "w", encoding="utf-8") as f:
        json.dump(klines, f, ensure_ascii=False)

    # 3. 份额历史 (复用主脚本的 akshare SSE/SZSE 批量回溯)
    trading_dates = [b["date"] for b in slice_idx]
    hist = fetch_history_shares_bulk(trading_dates)
    with open(os.path.join(out_dir, "shares.json"), "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False)
    print(f"  ✅ 份额 {len(hist)}日")
    return True


# ============================================================
# 测试运行
# ============================================================

def _load_fixture(aid):
    fdir = os.path.join(FIXTURES_DIR, aid)
    if not os.path.isdir(fdir):
        return None, f"fixture 缺失, 先运行 --build"
    with open(os.path.join(fdir, "idx.json"), encoding="utf-8") as f:
        idx_d = json.load(f)
    with open(os.path.join(fdir, "klines.json"), encoding="utf-8") as f:
        klines = json.load(f)
    with open(os.path.join(fdir, "shares.json"), encoding="utf-8") as f:
        shares_hist = json.load(f)
    return {"idx": idx_d, "klines": klines, "shares": shares_hist}, None


def run_anchor(anchor):
    """返回 (ok, 输出行列表)"""
    aid = anchor["id"]
    w0, w1 = anchor["window"]
    lines = [f"{aid} {anchor['name']}  [{w0} ~ {w1}]"]

    fixture, err = _load_fixture(aid)
    if err:
        return False, lines + [f"  ❌ {err}"]

    # shares_map: 与 etf_v7_threefactor.main() 完全相同的构建路径
    shares_hist = fixture["shares"]
    shares_map = {}
    for code in ETFS:
        shares_map[code] = {}
        for date, entries in shares_hist.items():
            if isinstance(entries, dict) and code in entries:
                _t, _p, delta_yi, delta_pct = get_historical_share(code, date, shares_hist)
                shares_map[code][date] = {
                    "shares_yi": entries[code].get("shares_yi"),
                    "delta_yi": delta_yi,
                    "delta_pct": delta_pct,
                }

    # 模型计算 (fixture 切片末尾即窗口, analyze_all 分析末35日)
    all_hist = {}
    for code in ETFS:
        data = fixture["klines"].get(code) or []
        if len(data) < 22:
            lines.append(f"  ⚠️ {code} K线不足22条, 跳过")
            continue
        hist = analyze_all(data, fixture["idx"], shares_map, w0, code, 35)
        all_hist[code] = hist

    # 窗口内逐日统计
    day_stats = {}
    for code, hist in all_hist.items():
        for h in hist:
            d = h["d"]
            if not (w0 <= d <= w1):
                continue
            s = day_stats.setdefault(d, {"high": 0, "mid": 0, "codes": []})
            if h["cp"] >= 70:
                s["high"] += 1
                s["codes"].append(f"{code}({h['cp']:.0f}%)")
            elif h["cp"] >= 50:
                s["mid"] += 1

    for d in sorted(day_stats):
        s = day_stats[d]
        codes = ", ".join(s["codes"][:6])
        lines.append(f"  📅 {d}: {s['high']}🔴+{s['mid']}🟡  {codes}")

    # 期望断言
    exp = anchor["expect"]
    ok = True
    if "max_high_per_day" in exp:  # 负锚点: 每天高确信数上限
        bad = [d for d, s in sorted(day_stats.items()) if s["high"] > exp["max_high_per_day"]]
        cond = len(bad) == 0
        ok &= cond
        lines.append(f"  每天高确信≤{exp['max_high_per_day']}: {'✅' if cond else '❌ 违规日: ' + ', '.join(bad)}")
    if "min_high" in exp:  # 正锚点: 窗口内 ≥min_days 天出现 ≥min_high 只🔴
        strong = [d for d, s in sorted(day_stats.items()) if s["high"] >= exp["min_high"]]
        need = exp.get("min_days", 1)
        cond = len(strong) >= need
        ok &= cond
        lines.append(f"  ≥{exp['min_high']}只🔴的天数 ≥{need}: {'✅' if cond else '❌'} ({len(strong)}天: {', '.join(strong)})")
    if "best_day_high" in exp:  # 窗口内至少一天达到该峰值
        best = max((s["high"] for s in day_stats.values()), default=0)
        cond = best >= exp["best_day_high"]
        ok &= cond
        lines.append(f"  单日峰值 ≥{exp['best_day_high']}只🔴: {'✅' if cond else '❌'} (峰值 {best})")

    return ok, lines


# ============================================================
# 入口
# ============================================================

def main():
    ap = argparse.ArgumentParser(description="ETF三因子模型 事件锚点回归测试")
    ap.add_argument("--build", action="store_true", help="从 akshare(sina源) 重建 fixtures")
    ap.add_argument("--list", action="store_true", help="列出锚点")
    ap.add_argument("--anchor", type=str, default=None, help="只处理指定锚点id")
    args = ap.parse_args()

    with open(EVENTS_FILE, encoding="utf-8") as f:
        anchors = json.load(f)["anchors"]

    if args.list:
        for a in anchors:
            print(f"{a['id']:<14} {a['name']}  [{a['window'][0]} ~ {a['window'][1]}]")
        return

    if args.build:
        ok_all = True
        for a in anchors:
            if args.anchor and a["id"] != args.anchor:
                continue
            ok_all &= build_fixture(a)
        print("\n" + ("✅ fixtures 构建完成" if ok_all else "❌ 部分 fixture 构建失败"))
        sys.exit(0 if ok_all else 1)

    # 运行测试
    print("=" * 70)
    print("🎯 ETF三因子模型 事件锚点回归测试")
    print("=" * 70)
    n_pass = n_fail = 0
    for a in anchors:
        if args.anchor and a["id"] != args.anchor:
            continue
        ok, lines = run_anchor(a)
        tag = "✅ PASS" if ok else "❌ FAIL"
        if ok:
            n_pass += 1
        else:
            n_fail += 1
        print(f"\n[{tag}] " + lines[0])
        print("\n".join(lines[1:]))
        if a.get("evidence"):
            print(f"  依据: {a['evidence']}")

    print("\n" + "=" * 70)
    print(f"结果: {n_pass} PASS / {n_fail} FAIL")
    if n_fail:
        print("❌ 存在失败的锚点 — 期望值来自公开事实, 请调查而不是放宽")
    print("=" * 70)
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
