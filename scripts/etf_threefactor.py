#!/usr/bin/env python3
"""
ETF国家队资金监测流水线 v6.1 — 三因子模型 + 本地SQLite数据存储
量能概率 50% + 方向概率 20% + 份额概率 30%

v5 → v6 升级：
  - 新增份额因子（sprob），权重30%，捕捉一级市场申购
  - 综合概率 cp = vp*0.5 + dp*0.2 + sp*0.3 （原 vp*0.7 + dp*0.3）
  - 份额因子基于日份额变化/20日均份额
  - 支持指定分析日期（--date YYYY-MM-DD）
  - 份额因子历史回溯：load历史份额数据，用当日vs前日的变化计算

v6 → v6.1 升级（本地数据存储）：
  - 集成 etf_data_store.py 的 SQLite 数据库 (etf_history.db)
  - 实时份额数据自动写入 DB，解决 push2 API 不可回溯问题
  - 历史分析优先查本地 DB，其次查 JSON 历史文件
  - 分析结果自动存入 DB（包含概率、信号级别等）
  - 新增 --record 参数：只采集当日数据入库，不做完整分析
  - 新增 --stats 参数：查看数据库状态

v6.1 → v7 升级（数据源替换）：
  - push2.eastmoney.com 长期中断（Empty reply from server）
  - 替换为 akshare 上交所/深交所 ETF 份额接口
  - 上交所：fund_etf_scale_sse(date) - 按日期查询全市场SSE ETF份额
  - 深交所：fund_scale_daily_szse(start,end) - 按日期范围查询全市场SZSE ETF份额
  - 新增 fetch_history_shares_bulk() 批量回溯函数
  - 自动回补历史数据（JSON历史不足60天时触发）

使用方式：
  python3 etf_threefactor.py                # 默认：最近交易日
  python3 etf_threefactor.py --date 2026-04-30  # 指定日期
  python3 etf_threefactor.py --record         # 仅采集份额数据入库
  python3 etf_threefactor.py --stats          # 查看DB状态
  python3 etf_threefactor.py --healthcheck    # 环境健康检查
  python3 etf_threefactor.py --backfill       # 一次性回补份额历史
  python3 etf_threefactor.py --query --days 7 # 查询DB历史信号
"""

import json, urllib.request, ssl, os, sys, math, argparse
from datetime import datetime, timedelta

# ---------- 本地数据存储模块 ----------
# 确保脚本所在目录在 sys.path 中（无论从哪个目录运行）
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
try:
    from etf_data_store import ETFDataStore
    DATA_STORE_AVAILABLE = True
except ImportError:
    DATA_STORE_AVAILABLE = False
    print("⚠️ etf_data_store.py 未找到，本地数据存储功能不可用")

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

WORKSPACE = os.path.expanduser(os.environ.get("ETF_WORKSPACE", "~/.etf-skill/workspace"))
HTML_OUT = os.path.join(WORKSPACE, "ETF国家队监测-终版.html")
JSON_OUT = os.path.join(WORKSPACE, "ETF国家队监测-终版.json")
SHARES_OUT = os.path.join(WORKSPACE, "etf_shares_history.json")
THREE_FACTOR_OUT = os.path.join(WORKSPACE, "ETF三因子分析.json")
THREE_FACTOR_HTML = os.path.join(WORKSPACE, "ETF三因子分析.html")

ETFS = {
    "510300": {"n": "华泰柏瑞沪深300ETF", "idx": "沪深300", "p": 5},
    "510310": {"n": "易方达沪深300ETF",   "idx": "沪深300", "p": 5},
    "510330": {"n": "华夏沪深300ETF",     "idx": "沪深300", "p": 5},
    "159919": {"n": "嘉实沪深300ETF",     "idx": "沪深300", "p": 4},
    "510050": {"n": "华夏上证50ETF",      "idx": "上证50",  "p": 4},
    "510500": {"n": "华泰柏瑞中证500ETF",  "idx": "中证500",  "p": 3},
    "512100": {"n": "南方中证1000ETF",    "idx": "中证1000", "p": 3},
}

PUSH2_MKT = {
    "510300": "1", "510310": "1", "510330": "1", "159919": "0",
    "510050": "1", "510500": "1", "512100": "1",
}

SPECIAL = {
    "2026-04-30": "五一前", "2026-05-06": "五一后",
}

# ============================================================
# 数据获取
# ============================================================

def fetch(code, limit=60):
    if code.startswith("sh") or code.startswith("sz"):
        pfx = code[:2]; numcode = code[2:]
    else:
        pfx = "sh" if code.startswith(("51", "56", "0")) else "sz"; numcode = code
    u = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={pfx}{numcode},day,,,{limit},qfq"
    try:
        r = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(r, timeout=15, context=ssl_ctx) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        k = d.get("data", {}).get(f"{pfx}{numcode}", {}).get("day", []) or \
            d.get("data", {}).get(f"{pfx}{numcode}", {}).get("qfqday", [])
        return [{"date": r[0], "o": float(r[1]), "c": float(r[2]),
                 "h": float(r[3]), "l": float(r[4]), "v": float(r[5])} for r in k if len(r) >= 6 and r[0]]
    except:
        return []


# ============================================================
# 份额数据获取 (akshare替代push2.eastmoney.com)
# ============================================================
# push2.eastmoney.com已长期中断(返回Empty reply from server)
# 替代方案:
#   上交所ETF: ak.fund_etf_scale_sse(date) — 返回指定日期全市场上交所ETF份额
#   深交所ETF: ak.fund_scale_daily_szse(start, end) — 返回日期范围内的深交所ETF份额
# 注意: 份额数据盘后更新(约19:00), 当日盘中无数据

# 缓存最近的SSE/SZSE结果, 避免重复API调用
_SSE_CACHE = {}     # {date_str: DataFrame}
_SZSE_CACHE = {}    # {date_str: {code: shares_yi}}

def _get_shares_sse(date_str):
    """获取指定日期的上交所ETF份额数据 (带缓存)"""
    if date_str in _SSE_CACHE:
        return _SSE_CACHE[date_str]
    try:
        import akshare as ak
        import pandas as pd
        df = ak.fund_etf_scale_sse(date=date_str)
        if df is not None and len(df) > 0 and '基金代码' in df.columns:
            _SSE_CACHE[date_str] = df
            return df
    except Exception as e:
        # 当日数据未发布时会抛异常(empty DataFrame无预期列)
        pass
    _SSE_CACHE[date_str] = None
    return None

def _get_shares_szse_range(start_date, end_date):
    """获取日期范围内的深交所ETF份额数据 (带缓存)"""
    cache_key = f"{start_date}_{end_date}"
    if cache_key in _SZSE_CACHE:
        return _SZSE_CACHE[cache_key]
    result = {}
    try:
        import akshare as ak
        df = ak.fund_scale_daily_szse(start_date=start_date, end_date=end_date, symbol='ETF')
        if df is not None and len(df) > 0:
            for _, row in df.iterrows():
                code = str(row['基金代码'])
                try:
                    d = str(row['日期'])[:10].replace('-','')
                except:
                    d = start_date
                shares = float(row['基金份额'])
                if d not in result:
                    result[d] = {}
                result[d][code] = shares / 1e8  # 份 → 亿份
    except Exception as e:
        pass
    _SZSE_CACHE[cache_key] = result
    return result

def _get_price_from_kline(code, target_date):
    """从腾讯K线数据获取指定日期的收盘价"""
    # 复用已有的fetch()函数
    data = fetch(code, 60)
    if not data:
        return None
    for row in reversed(data):
        if row['date'] == target_date:
            return row['c']
    # 未匹配到精确日期, 返回最新收盘价
    return data[-1]['c'] if data else None

def fetch_fund_shares(code, target_date=None):
    """
    获取ETF份额数据 (使用 akshare SSE/SZSE API)
    
    参数:
        code: ETF代码 (如 '510300')
        target_date: 目标日期 YYYY-MM-DD 格式, None=最近交易日
    返回:
        {"shares_yi": 份额(亿份), "price": 收盘价, "mktval_yi": 市值(亿元)}
        或 None (数据不可用)
    """
    try:
        import akshare as ak
    except ImportError:
        print(f"  ⚠️ akshare 未安装")
        return None

    if target_date:
        date_str = target_date.replace('-', '')
    else:
        # 使用最近交易日 (今天或昨天, 取决于份额是否发布)
        today = datetime.now()
        date_str = today.strftime('%Y%m%d')

    # 判断交易所
    if code.startswith('159') or code.startswith('16'):
        # 深交所ETF
        return _fetch_szse_shares(code, date_str)
    else:
        # 上交所ETF (51xxxx / 56xxxx / 58xxxx / 588xxx)
        return _fetch_sse_shares(code, date_str)

def _fetch_sse_shares(code, date_str):
    """从SSE获取上交所ETF份额"""
    # 尝试3天: 今天→昨天→前天 (份额数据可能延迟)
    from datetime import datetime, timedelta
    current_date = datetime.strptime(date_str, '%Y%m%d')

    for offset in [0, -1, -2]:
        try_date = (current_date + timedelta(days=offset)).strftime('%Y%m%d')
        df = _get_shares_sse(try_date)
        if df is None:
            continue
        try:
            row = df[df['基金代码'] == code]
            if len(row) > 0:
                shares_fen = float(row['基金份额'].values[0])
                shares_yi = round(shares_fen / 1e8, 4)
                # 获取价格
                target_display = try_date[:4] + '-' + try_date[4:6] + '-' + try_date[6:8]
                price = _get_price_from_kline(code, target_display)
                if price is None:
                    price = 0
                mktval_yi = round(shares_yi * price, 1)
                return {"shares_yi": shares_yi, "price": round(price, 3),
                        "mktval_yi": mktval_yi, "data_date": target_display}
        except (KeyError, IndexError, ValueError):
            continue

    return None

def _fetch_szse_shares(code, date_str):
    """从SZSE获取深交所ETF份额"""
    from datetime import datetime, timedelta
    current_date = datetime.strptime(date_str, '%Y%m%d')

    # SZSE批量查询最近7天
    start_date = (current_date - timedelta(days=7)).strftime('%Y%m%d')
    end_date = current_date.strftime('%Y%m%d')

    data_map = _get_shares_szse_range(start_date, end_date)

    # 从最新日期开始查找
    for offset in [0, -1, -2, -3, -4, -5, -6]:
        try_date = (current_date + timedelta(days=offset)).strftime('%Y%m%d')
        if try_date in data_map and code in data_map[try_date]:
            shares_yi = round(data_map[try_date][code], 4)
            target_display = try_date[:4] + '-' + try_date[4:6] + '-' + try_date[6:8]
            price = _get_price_from_kline(code, target_display)
            if price is None:
                price = 0
            mktval_yi = round(shares_yi * price, 1)
            return {"shares_yi": shares_yi, "price": round(price, 3),
                    "mktval_yi": mktval_yi, "data_date": target_display}

    return None

def fetch_history_shares_bulk(dates_list):
    """
    批量获取历史份额数据 (用于回溯初始化)
    
    参数:
        dates_list: 日期列表 ["2026-05-06", "2026-05-07", ...]
    返回:
        history_dict: {date: {code: {shares_yi: ...}}}  (与 load_shares_history 格式兼容)
    """
    history = {}
    if not dates_list:
        return history

    # 上交所: 逐日查询
    print(f"  📡 SSE份额: {len(dates_list)}日...")
    for d in sorted(dates_list):
        d8 = d.replace('-', '')
        df = _get_shares_sse(d8)
        if df is None:
            continue
        for code in ETFS:
            if code.startswith('159'):
                continue  # 深交所在后面处理
            try:
                row = df[df['基金代码'] == code]
                if len(row) > 0:
                    shares_yi = round(float(row['基金份额'].values[0]) / 1e8, 2)
                    if d not in history:
                        history[d] = {}
                    history[d][code] = {"shares_yi": shares_yi, "ts": d + "T19:00:00"}
            except (KeyError, IndexError, ValueError):
                pass

    # 深交所: 批量查询日期范围
    if dates_list:
        min_d = min(dates_list).replace('-', '')
        max_d = max(dates_list).replace('-', '')
        print(f"  📡 SZSE份额: {min_d}~{max_d}...")
        data_map = _get_shares_szse_range(min_d, max_d)
        for d_str, codes in data_map.items():
            d = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:8]}"
            for code, shares_yi in codes.items():
                if code in ETFS:
                    if d not in history:
                        history[d] = {}
                    history[d][code] = {"shares_yi": round(shares_yi, 2), "ts": d + "T19:00:00"}

    return history


def load_shares_history():
    if not os.path.exists(SHARES_OUT): return {}
    try:
        with open(SHARES_OUT, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


def save_shares_history(history):
    dates = sorted(history.keys())
    if len(dates) > 60:
        for old in dates[:-60]:
            del history[old]
    with open(SHARES_OUT, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _persist_shares(store, shares_history):
    """份额双写持久化: JSON(兼容备份, 60天裁剪) + SQLite shares_raw(全量原始)"""
    save_shares_history(shares_history)
    if store:
        store.upsert_shares_bulk(shares_history)


def get_historical_share(code, target_date, history):
    """从历史记录中查找目标日期的份额数据，返回 (share_yi, prev_share_yi, delta_yi, delta_pct)"""
    if target_date in history and isinstance(history[target_date], dict) and code in history[target_date]:
        target_share = history[target_date][code].get("shares_yi")
        # 找前一日
        prev_share = None
        all_dates = sorted(history.keys())
        idx = all_dates.index(target_date) if target_date in all_dates else -1
        if idx > 0:
            for prev_d in all_dates[idx-1::-1]:
                if isinstance(history.get(prev_d, {}), dict) and code in history[prev_d]:
                    prev_share = history[prev_d][code].get("shares_yi")
                    break
        if target_share and prev_share:
            delta_yi = round(target_share - prev_share, 2)
            delta_pct = round(delta_yi / prev_share * 100, 2)
            return target_share, prev_share, delta_yi, delta_pct
        elif target_share:
            return target_share, None, None, None
    return None, None, None, None


# ============================================================
# 三因子模型核心函数
# ============================================================

def vprob(r):
    """量能概率（原权重70%→现50%）"""
    if r < 0.5: return max(0, r / 0.5 * 5)
    if r < 1.0: return 5 + (r - 0.5) / 0.5 * 12
    if r < 1.3: return 17 + (r - 1) / 0.3 * 18
    if r < 1.5: return 35 + (r - 1.3) / 0.2 * 20
    if r < 2.0: return 55 + (r - 1.5) / 0.5 * 25
    if r < 3.0: return 80 + (r - 2) / 1 * 15
    if r < 5.0: return 95 + (r - 3) / 2 * 3
    return min(100, 98 + (r - 5) / 5 * 2)


def _dprob_parts(chg, t5_etf, t5_idx, vr, idx_chg):
    """方向概率四维分值分解 (f1~f4 + 普涨折扣), 供报告展示支撑数据"""
    rally_discount = 1.0
    if idx_chg > 2.0: rally_discount = 0.60
    elif idx_chg > 1.5: rally_discount = 0.70
    elif idx_chg > 1.0: rally_discount = 0.80
    elif idx_chg > 0.5: rally_discount = 0.90

    # 注: 普涨环境的降级由函数末尾的 rally_discount 统一处理,
    #     f1 内部不再单独扣减, 避免双重计扣
    if chg > 0.3 and t5_idx < -1:      f1 = 95
    elif chg > 0 and t5_idx < -0.5:     f1 = 85
    elif chg > 0 and t5_idx < 0:        f1 = 70
    elif abs(chg) < 0.15 and t5_idx < -1: f1 = 80
    elif abs(chg) < 0.3 and t5_idx < -0.5: f1 = 65
    elif chg > 1 and vr > 1.5:          f1 = 45
    elif chg > 0.5 and vr > 1.3:        f1 = 50
    elif chg > 0:                       f1 = 40
    elif chg < -1.5 and vr > 2:         f1 = 8
    elif chg < -0.5 and vr > 1.5:       f1 = 15
    else:                               f1 = 25

    gap = t5_etf - t5_idx
    if gap > 3:      f2 = 95
    elif gap > 2:    f2 = 85
    elif gap > 1.2:  f2 = 75
    elif gap > 0.6:  f2 = 60
    elif gap > 0.2:  f2 = 50
    elif gap > -0.2: f2 = 40
    elif gap > -0.6: f2 = 30
    else:            f2 = 15

    if t5_idx < -4:     f3 = 95
    elif t5_idx < -3:   f3 = 90
    elif t5_idx < -2:   f3 = 80
    elif t5_idx < -1:   f3 = 70
    elif t5_idx < -0.5: f3 = 55
    elif t5_idx < 0:    f3 = 45
    elif t5_idx < 1:    f3 = 35
    elif t5_idx < 3:    f3 = 20
    else:               f3 = 10
    f4 = 35

    return f1, f2, f3, f4, rally_discount


def dprob(chg, t5_etf, t5_idx, vr, idx_chg):
    """方向概率（原权重30%→现20%）"""
    f1, f2, f3, f4, rally_discount = _dprob_parts(chg, t5_etf, t5_idx, vr, idx_chg)
    raw = f1 * 0.4 + f2 * 0.3 + f3 * 0.2 + f4 * 0.1
    return round(raw * rally_discount, 1)


def sprob(share_delta_pct):
    """
    份额概率（权重30%）【v6新增】
    基于日份额变化 / 前日份额

    份额变动比 → 份额概率（连续分段，0% 处为近中性 15 分）：
      >10% → 95      |  5~10% → 80~95  |  3~5% → 65~80  |  1~3% → 45~65
      0~1% → 15~45   |  -1~0% → 10~15  |  -5~-1% → 5~10 |  <-5% → 0~5
    映射整体偏向申购（模型只识别增持，减持信号见 etf_model.md 局限性）
    """
    if share_delta_pct is None:
        return None  # 数据不可用
    if share_delta_pct > 10:    return 95
    elif share_delta_pct > 5:   return 80 + (share_delta_pct - 5) / 5 * 15
    elif share_delta_pct > 3:   return 65 + (share_delta_pct - 3) / 2 * 15
    elif share_delta_pct > 1:   return 45 + (share_delta_pct - 1) / 2 * 20
    elif share_delta_pct > 0:   return 15 + share_delta_pct * 30
    elif share_delta_pct > -1:  return 10 + (share_delta_pct + 1) * 5
    elif share_delta_pct > -5:  return 5 + (share_delta_pct + 5) / 4 * 5
    else:                       return max(0, 5 + (share_delta_pct + 5) / 5 * 5)


def analyze_all(data, idx_d, shares_map, target_date, code, days=35):
    """
    三因子模型分析
    shares_map: {code: {date: {shares_yi, prev_shares_yi, delta_yi, delta_pct}}}
    code: 当前分析的ETF代码 (必填) — 份额因子用该ETF自身的份额变化,
          不传会静默退化为二因子(历史bug: 所有ETF都用了510300的份额)
    """
    if len(data) < 22: return []
    res = []
    aligned = align_idx(data, idx_d)
    for i in range(max(21, len(data) - days), len(data)):
        d = data[i]
        v = d["v"] / 10000
        pv = [data[j]["v"] / 10000 for j in range(i - 20, i)]
        ma = sum(pv) / 20
        if ma == 0: continue
        vr = v / ma
        pc = data[i - 1]["c"]
        chg = (d["c"] - pc) / pc * 100 if pc > 0 else 0
        t5 = i >= 6 and data[i - 5]["c"] > 0 and (d["c"] - data[i - 5]["c"]) / data[i - 5]["c"] * 100 or 0
        t5i = t5
        idchg = 0
        if i < len(aligned) and aligned[i] is not None:
            ii = aligned[i]
            vp_idx = ii > 0 and idx_d[ii - 1]["c"] > 0 and (idx_d[ii]["c"] - idx_d[ii - 1]["c"]) / idx_d[ii - 1]["c"] * 100 or 0
            idchg = round(vp_idx, 1)
            if i >= 6 and aligned[i - 5] is not None:
                j5 = aligned[i - 5]
                t5i = idx_d[j5]["c"] > 0 and (idx_d[ii]["c"] - idx_d[j5]["c"]) / idx_d[j5]["c"] * 100 or 0
        vp = vprob(vr)
        dp = dprob(chg, t5, round(t5i, 2), vr, idchg)

        # 三因子：份额概率 (每只ETF用其自身的份额变化)
        sp = None
        share_delta_pct = None
        share_delta_yi = None
        info = shares_map.get(code, {}).get(d["date"])
        if info:
            share_delta_pct = info.get("delta_pct")
            share_delta_yi = info.get("delta_yi")
            sp = sprob(share_delta_pct)

        # 三因子综合概率
        if sp is not None:
            cp = round(vp * 0.5 + dp * 0.2 + sp * 0.3, 1)
        else:
            # 份额数据不可用，退化为二因子（保持70/30用于对比）
            cp = round(vp * 0.7 + dp * 0.3, 1)

        tag = SPECIAL.get(d["date"], "")
        res.append({
            "d": d["date"], "c": d["c"], "chg": round(chg, 2),
            "t5": round(t5, 2), "t5i": round(t5i, 2), "idx_chg": idchg,
            "v": round(v, 2), "vma": round(ma, 2), "vr": round(vr, 2),
            "vp": round(vp, 1), "dp": dp, "sp": sp, "cp": cp,
            "share_delta_pct": share_delta_pct, "share_delta_yi": share_delta_yi,
            "tag": tag, "has_shares": sp is not None,
        })
    return res


def align_idx(data, idx_d):
    idx_map = {}
    for j, d in enumerate(idx_d):
        idx_map[d["date"]] = j
    return [idx_map.get(d["date"]) for d in data]


# ============================================================
# HTML 报告生成（每ETF独立卡片 + 交互明细）
# ============================================================

def _sprob_band(pct):
    """份额变动比 → 所属区间名 (报告支撑数据展示用)"""
    if pct > 10:  return ">10% 大规模申购段"
    if pct > 5:   return "5~10% 较大申购段"
    if pct > 3:   return "3~5% 中等申购段"
    if pct > 1:   return "1~3% 小幅申购段"
    if pct > 0:   return "0~1% 常规申购段"
    if pct > -1:  return "-1~0% 轻微赎回段"
    if pct > -5:  return "-5~-1% 赎回段"
    return "<-5% 大幅赎回段"


def _factor_bar(label, val, color):
    """三因子概率条 (卡片内)"""
    if val is None:
        # 因子不可用(如二因子退化日): 显示 — 而非误导性的 0%
        return (f'<div class="fbar"><span>{label}</span>'
                f'<div class="ftrack"><div class="ffill" style="width:2%;background:{color}"></div></div>'
                f'<b>–</b></div>')
    return (f'<div class="fbar"><span>{label}</span>'
            f'<div class="ftrack"><div class="ffill" style="width:{max(2, min(100, val)):.0f}%;background:{color}"></div></div>'
            f'<b>{val:.0f}%</b></div>')


def _sparkline(hist, width=560, height=56):
    """cp 历史趋势 sparkline (纯内联SVG, 信号日用刻度线标记)"""
    if not hist:
        return ""
    pts = [h["cp"] for h in hist]
    n = len(pts)
    def x(i): return 4 + i * (width - 8) / max(1, n - 1)
    def y(v): return height - 6 - v / 100 * (height - 12)
    l70 = (f'<line x1="4" y1="{y(70):.1f}" x2="{width-4}" y2="{y(70):.1f}" '
           f'stroke="#ef4444" stroke-width="1" stroke-dasharray="4 4" opacity="0.4"/>')
    l50 = (f'<line x1="4" y1="{y(50):.1f}" x2="{width-4}" y2="{y(50):.1f}" '
           f'stroke="#f59e0b" stroke-width="1" stroke-dasharray="4 4" opacity="0.3"/>')
    poly = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(pts))
    ticks = ""
    for i, h in enumerate(hist):
        c = h["cp"]
        if c >= 50:
            color = "#ef4444" if c >= 70 else "#f59e0b"
            ticks += (f'<line x1="{x(i):.1f}" y1="{y(c)-3:.1f}" x2="{x(i):.1f}" y2="{y(c)+3:.1f}" '
                      f'stroke="{color}" stroke-width="2"><title>{h["d"]} CP{c:.0f}%</title></line>')
    last = pts[-1]
    txt = (f'<text x="{x(n-1):.1f}" y="{max(8, y(last)-4):.1f}" font-size="9" '
           f'fill="#7dd3fc" text-anchor="end">{last:.0f}%</text>')
    return (f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none">'
            f'{l70}{l50}<polyline points="{poly}" fill="none" stroke="#38bdf8" stroke-width="1.5"/>'
            f'{ticks}{txt}</svg>')


def gen_html(all_hist, shares_data, target_date):
    """生成每ETF独立卡片的交互式HTML报告 (点击卡片展开支撑数据)"""
    dates = set()
    for hh in all_hist.values():
        for h in hh:
            dates.add(h["d"])
    dates = sorted(dates)
    primary_date = target_date if target_date in dates else (dates[-1] if dates else (target_date or ""))

    # 数据状态徽章（盘中 vs 收盘后）
    now_ymd = datetime.now().strftime("%Y-%m-%d")
    now_hm = datetime.now().strftime("%H:%M")
    shares_available = sum(1 for sd in shares_data.values() if sd.get("shares_yi") is not None)
    if primary_date == now_ymd and now_hm < "15:30":
        status_txt = "⚠️ 盘中运行：K线为实时累计值，当日信号不可靠"
        status_cls = "warn"
    elif shares_available == 0:
        status_txt = "⚠️ 份额未发布（盘后约19:00更新）：份额因子不可用，信号为二因子"
        status_cls = "warn"
    else:
        status_txt = "✅ 收盘后完整数据：量价+份额均已发布，信号可信"
        status_cls = "ok"

    cards = ""
    for code, info in ETFS.items():
        hist = [h for h in all_hist.get(code, []) if h["d"] <= primary_date]
        if not hist:
            cards += (f'<div class="card"><div class="c-title"><b>{info["n"]}</b>'
                      f'<span class="code">{code}</span></div>'
                      f'<div class="c-action lo">❌ 数据获取失败，无分析结果</div></div>')
            continue
        rec = hist[-1]
        sd = shares_data.get(code, {})
        cp, chg, vr = rec["cp"], rec["chg"], rec["vr"]
        vp, dp, sp = rec["vp"], rec["dp"], rec["sp"]

        # 行动标签 (买/观望/无操作) + 卖出提示位
        if cp >= 70:
            sig_emoji, action_txt, action_cls = "🔴", "买入信号", "hi"
        elif cp >= 50:
            sig_emoji, action_txt, action_cls = "🟡", "观望（等确认）", "md"
        else:
            sig_emoji, action_txt, action_cls = "⚪", "无操作", "lo"
        sell_hints = ""
        if chg < -0.5 and vr > 1.5:
            sell_hints += '<span class="hint">⚠️ 放量下跌</span>'
        if (sd.get("delta_pct") or 0) < -1:
            sell_hints += '<span class="hint">🔻 赎回迹象</span>'

        # 当日数据行
        chg_cls = "#ef4444" if chg > 0 else ("#22c55e" if chg < 0 else "#94a3b8")
        shares_txt = "份额 -"
        if sd.get("shares_yi") is not None:
            shares_txt = f"份额 {sd['shares_yi']:.1f}亿"
            if sd.get("delta_yi") is not None:
                dc = "#ef4444" if sd["delta_yi"] > 0 else ("#22c55e" if sd["delta_yi"] < 0 else "#94a3b8")
                shares_txt += f' <span style="color:{dc}">({sd["delta_yi"]:+.1f}亿 {sd["delta_pct"]:+.2f}%)</span>'
        tag_html = f'<span class="tag">{rec["tag"]}</span>' if rec.get("tag") else ""

        # 因子概率条 + 历史趋势 sparkline
        factors = (_factor_bar("量能P", vp, "#38bdf8") +
                   _factor_bar("方向P", dp, "#818cf8") +
                   _factor_bar("份额P", sp, "#f59e0b"))
        spark = _sparkline(hist[-40:])

        # 因子支撑（展开明细）
        v_txt = f"量能：倍量 {vr:.2f}x = 当日 {rec['v']:.0f}万 ÷ 20日均 {rec['vma']:.0f}万 → 量能P {vp:.0f}%"
        f1, f2, f3, f4, disc = _dprob_parts(chg, rec["t5"], rec["t5i"], vr, rec["idx_chg"])
        raw = f1 * 0.4 + f2 * 0.3 + f3 * 0.2 + f4 * 0.1
        d_txt = f"方向：{dp:.0f}% = (f1[{f1:.0f}]×0.4 + f2[{f2:.0f}]×0.3 + f3[{f3:.0f}]×0.2 + f4[{f4:.0f}]×0.1)"
        if disc < 1:
            d_txt += f" ×{disc:.2f} 普涨折扣 = {raw * disc:.1f}"
        else:
            d_txt += f" = {raw:.1f}"
        if sp is not None:
            sp_txt = f"份额：{sp:.0f}% ← 份额日变 {rec['share_delta_pct']:+.2f}%（{_sprob_band(rec['share_delta_pct'])}）"
        else:
            sp_txt = "份额：不可用（二因子退化: cp = 量能×0.7 + 方向×0.3）"

        # 近40日逐日明细表 (倒序, 最新在上)
        rows = ""
        for h in reversed(hist[-40:]):
            ch = "#ef4444" if h["chg"] > 0 else ("#22c55e" if h["chg"] < 0 else "#94a3b8")
            if h["cp"] >= 70:
                s = "🔴"
            elif h["cp"] >= 50:
                s = "🟡"
            else:
                s = "⚪"
            sh = f'{h["share_delta_pct"]:+.2f}%' if h["share_delta_pct"] is not None else "-"
            spv = f'{h["sp"]:.0f}%' if h["sp"] is not None else "-"
            cp_col = "#ef4444" if h["cp"] >= 70 else ("#f59e0b" if h["cp"] >= 50 else "#cbd5e1")
            t = f' <span class="tag">{h["tag"]}</span>' if h.get("tag") else ""
            rows += (f'<tr><td>{h["d"][5:]}{t}</td><td>{h["c"]:.3f}</td>'
                     f'<td style="color:{ch}">{h["chg"]:+.2f}%</td><td>{h["vr"]:.2f}x</td>'
                     f'<td>{sh}</td><td>{h["vp"]:.0f}%</td><td>{h["dp"]:.0f}%</td>'
                     f'<td>{spv}</td><td style="color:{cp_col};font-weight:700">{h["cp"]:.0f}%</td><td>{s}</td></tr>')

        card_id = f"card-{code}"
        cards += f'''<div class="card" id="{card_id}">
  <div class="c-head" onclick="toggle('{card_id}')">
    <div class="c-title"><span class="sig">{sig_emoji}</span> <b>{info["n"]}</b> <span class="code">{code}</span> <span class="idx">{info["idx"]}</span>{tag_html}</div>
    <div class="c-meta">今日 <span style="color:{chg_cls}">{chg:+.2f}%</span> · 倍量 {vr:.2f}x · {shares_txt}</div>
  </div>
  <div class="c-factors">{factors}</div>
  <div class="c-spark">{spark}</div>
  <div class="c-action {action_cls}">综合概率 {cp:.0f}% → {action_txt}{sell_hints}</div>
  <div class="c-btn" onclick="toggle('{card_id}')">▶ 查看支撑数据（点击卡片任意处亦可）</div>
  <div class="c-detail" onclick="event.stopPropagation()">
    <div class="d-support">
      <div>🔍 {v_txt}</div>
      <div>🔍 {d_txt}</div>
      <div>🔍 {sp_txt}</div>
    </div>
    <table class="d-table"><thead><tr><th>日期</th><th>收盘</th><th>涨跌</th><th>倍量</th><th>份额日变</th><th>量P</th><th>方P</th><th>份P</th><th>CP</th><th>信号</th></tr></thead><tbody>{rows}</tbody></table>
  </div>
</div>'''

    return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ETF三因子监测报告</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0a0f1a;color:#dfe6ef;font-family:-apple-system,"SF Pro Display","PingFang SC","Microsoft YaHei",sans-serif;padding:18px 0 30px}}
.wrap{{max-width:1200px;margin:0 auto;padding:0 16px}}
.topbar{{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:10px 16px;background:rgba(17,28,46,0.85);border:1px solid rgba(56,189,248,0.12);border-radius:12px;margin-bottom:14px}}
.topbar h1{{font-size:17px;font-weight:700}}
.topbar .date{{font-size:12px;color:#8896ab}}
.badge{{font-size:12px;padding:3px 10px;border-radius:20px;border:1px solid}}
.badge.warn{{color:#fdba74;border-color:rgba(249,115,22,0.3);background:rgba(249,115,22,0.06)}}
.badge.mid{{color:#fcd34d;border-color:rgba(245,158,11,0.3);background:rgba(245,158,11,0.06)}}
.badge.ok{{color:#86efac;border-color:rgba(34,197,94,0.3);background:rgba(34,197,94,0.06)}}
#btnAll{{margin-left:auto;background:rgba(56,189,248,0.1);border:1px solid rgba(56,189,248,0.25);color:#7dd3fc;padding:4px 12px;border-radius:8px;font-size:12px;cursor:pointer}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(560px,1fr));gap:14px}}
.card{{background:rgba(17,28,46,0.7);border:1px solid rgba(56,189,248,0.1);border-radius:12px;padding:14px 16px;display:flex;flex-direction:column;gap:8px;cursor:pointer;transition:border-color .15s}}
.card:hover{{border-color:rgba(56,189,248,0.3)}}
.c-head{{display:flex;flex-direction:column;gap:4px}}
.c-title{{display:flex;align-items:center;gap:8px}}
.c-title .sig{{font-size:16px}}
.c-title b{{font-size:14px}}
.c-title .code{{color:#64748b;font-size:12px}}
.c-title .idx{{color:#7a8ba0;font-size:11px;background:rgba(148,163,184,0.1);padding:1px 6px;border-radius:4px}}
.c-meta{{font-size:12px;color:#8896ab}}
.tag{{font-size:10px;background:rgba(239,68,68,0.15);color:#fca5a5;padding:1px 5px;border-radius:3px}}
.c-factors{{display:flex;flex-direction:column;gap:4px}}
.fbar{{display:flex;align-items:center;gap:8px;font-size:11px;color:#7a8ba0}}
.fbar span{{width:44px;flex-shrink:0}}
.ftrack{{flex:1;height:8px;background:rgba(30,41,59,0.8);border-radius:4px;overflow:hidden}}
.ffill{{height:100%;border-radius:4px}}
.fbar b{{width:34px;text-align:right;color:#dfe6ef}}
.c-spark svg{{display:block;width:100%;height:56px;background:rgba(15,23,42,0.6);border-radius:8px;border:1px solid rgba(56,189,248,0.08)}}
.c-action{{font-size:13px;font-weight:700;padding:8px 12px;border-radius:8px}}
.c-action.hi{{background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);color:#fca5a5}}
.c-action.md{{background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.25);color:#fcd34d}}
.c-action.lo{{background:rgba(148,163,184,0.06);border:1px solid rgba(148,163,184,0.15);color:#94a3b8}}
.hint{{font-size:11px;font-weight:600;color:#fdba74;border:1px solid rgba(249,115,22,0.3);background:rgba(249,115,22,0.06);padding:1px 6px;border-radius:4px;margin-left:6px}}
.c-btn{{font-size:12px;color:#7dd3fc;text-align:center;padding:4px;border:1px dashed rgba(56,189,248,0.25);border-radius:8px}}
.c-detail{{display:none;flex-direction:column;gap:10px;border-top:1px solid rgba(56,189,248,0.1);padding-top:10px}}
.card.open .c-detail{{display:flex}}
.d-support{{font-size:11.5px;color:#94a3b8;background:rgba(15,23,42,0.6);border-radius:8px;padding:8px 12px;display:flex;flex-direction:column;gap:3px;line-height:1.5}}
.d-table{{width:100%;border-collapse:collapse;font-size:11px}}
.d-table th{{text-align:left;padding:4px 6px;color:#7a8ba0;font-weight:600;border-bottom:1px solid rgba(56,189,248,0.1);white-space:nowrap}}
.d-table td{{padding:4px 6px;border-bottom:1px solid rgba(20,30,50,0.5);color:#b0bdd0;white-space:nowrap}}
.d-table tbody tr:hover td{{background:rgba(56,189,248,0.03)}}
.ftr{{margin-top:18px;text-align:center;font-size:11px;color:#64748b}}
@media (max-width:1180px){{.cards{{grid-template-columns:1fr}}}}
</style></head><body>
<div class="wrap">
  <div class="topbar">
    <h1>🛡️ ETF三因子监测</h1>
    <span class="date">分析日 {primary_date}</span>
    <span class="badge {status_cls}">{status_txt}</span>
    <button id="btnAll" onclick="toggleAll()">📂 全部展开</button>
  </div>
  <div class="cards">{cards}</div>
  <div class="ftr">ETF国家队资金监测 · 三因子模型（量能50%+方向20%+份额30%）· 份额数据盘后约19:00发布 · 腾讯财经API + 上交所/深交所akshare · 点击卡片查看支撑数据</div>
</div>
<script>
function toggle(id){{var c=document.getElementById(id);if(c)c.classList.toggle('open')}}
function toggleAll(){{
  var cards=document.querySelectorAll('.card');
  var open=cards.length>0&&cards[0].classList.contains('open');
  for(var i=0;i<cards.length;i++){{cards[i].classList.toggle('open',!open)}}
  document.getElementById('btnAll').textContent=open?'📂 全部展开':'📁 全部收起';
}}
</script>
</body></html>'''


# ============================================================
# 运维工具 (--healthcheck / --backfill / --query)
# ============================================================

def _disp_width(s):
    """终端显示宽度 (CJK字符按2列计算, 用于表格对齐)"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)

def _pad(s, width):
    return s + " " * max(0, width - _disp_width(s))

def _trunc(s, width):
    """按显示宽度截断 (避免从CJK字符中间切开)"""
    out = ""
    for ch in s:
        if _disp_width(out + ch) > width:
            break
        out += ch
    return out

def fmt_pct(v):
    return f"{v:.0f}%" if isinstance(v, (int, float)) else "-"

def fmt_chg(v):
    return f"{v:+.2f}%" if isinstance(v, (int, float)) else "-"

def fmt_vr(v):
    return f"{v:.2f}x" if isinstance(v, (int, float)) else "-"

def healthcheck():
    """环境健康检查: akshare / 数据源 / DB, 任一关键项失败退出码1"""
    print("=" * 60)
    print("🩺 ETF三因子系统健康检查")
    print("=" * 60)
    fails = 0

    def check(name, ok, detail=""):
        nonlocal fails
        mark = "✅" if ok else "❌"
        print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            fails += 1

    # 1. akshare 可导入
    try:
        import akshare as ak
        check("akshare 可导入", True, f"v{getattr(ak, '__version__', '?')}")
    except ImportError:
        check("akshare 可导入", False, "未安装, 运行 pip3 install akshare")

    # 2. 腾讯K线API
    k = fetch("sh000300", 5)
    check("腾讯K线API", len(k) > 0, f"{len(k)}条K线" if k else "无数据(可能网络不通)")

    # 3. 上交所份额API (内置 今天→昨天→前天 重试)
    sse = fetch_fund_shares("510300")
    if sse:
        check("上交所份额API", True, f"510300 {sse['shares_yi']:.1f}亿份 ({sse['data_date']})")
    else:
        check("上交所份额API", False, "获取失败(份额盘后约19:00更新, 或网络问题)")

    # 4. 深交所份额API (内置 7天回溯)
    szse = fetch_fund_shares("159919")
    if szse:
        check("深交所份额API", True, f"159919 {szse['shares_yi']:.1f}亿份 ({szse['data_date']})")
    else:
        check("深交所份额API", False, "获取失败(份额盘后约19:00更新, 或网络问题)")

    # 5. SQLite数据库
    db_ok, db_detail = True, ""
    try:
        store = ETFDataStore()
        st = store.get_stats()
        db_detail = f"{st['total_records']}条 / {st['total_dates']}日 @ {store.db_path}"
    except Exception as e:
        db_ok, db_detail = False, str(e)
    check("SQLite数据库", db_ok, db_detail)

    print("=" * 60)
    if fails == 0:
        print("🎉 全部检查通过, 系统可正常运行")
        return True
    print(f"❌ {fails} 项关键检查失败, 请按上方提示修复 (定时任务可能失败, 退出码1)")
    return False


def backfill_shares():
    """一次性回补全部份额历史 (绕过主流水线每次最多20天的限制)"""
    print("=" * 60)
    print("📡 份额历史完整回溯")
    print("=" * 60)
    store = ETFDataStore() if DATA_STORE_AVAILABLE else None
    idx = fetch("sh000300", 60)
    if not idx:
        print("❌ 获取沪深300交易日历失败(腾讯K线API不可达)")
        return False

    all_dates = [d["date"] for d in idx]
    shares_history = store.get_shares_history() if store else {}
    if not shares_history:
        shares_history = load_shares_history()  # JSON 兼容回退
    # 清理空日期残留 (push2失败遗留)
    empty = [d for d, v in shares_history.items() if isinstance(v, dict) and len(v) == 0]
    for d in empty:
        del shares_history[d]
    if empty:
        print(f"  🧹 清理{len(empty)}个空日期")

    print(f"  📦 现有历史: {len(shares_history)}日")
    dates_to_fetch = sorted(set(all_dates) - set(shares_history.keys()))
    if not dates_to_fetch:
        print(f"  ✅ 历史已完整 ({len(all_dates)}日全部覆盖), 无需回溯")
        return True

    print(f"  🎯 待回溯: {len(dates_to_fetch)}日 (交易日历共{len(all_dates)}日)")
    bulk = fetch_history_shares_bulk(dates_to_fetch)
    if not bulk:
        print("  ⚠️ 未获取到任何数据 (akshare未安装或接口异常)")
        return False

    new_days = 0
    for d, entries in bulk.items():
        if d not in shares_history:
            shares_history[d] = {}
        shares_history[d].update(entries)
        new_days += 1
    _persist_shares(store, shares_history)
    print(f"  ✅ 新增 {new_days}日份额数据, 累计 {len(shares_history)}日")
    print(f"  💡 每次完整分析会自动增量采集, 此后无需再回溯")
    return True


def query_signals(days=7, code=None):
    """从本地DB查询历史信号 (不跑完整分析)"""
    if not DATA_STORE_AVAILABLE:
        print("❌ etf_data_store.py 不可用")
        return False
    store = ETFDataStore()
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = store.get_range(code=code, start_date=start, end_date=end)
    if not rows:
        print(f"  ℹ️ DB中 {start} ~ {end} 无记录")
        print("  💡 提示: 先运行 python3 etf_threefactor.py --record 或完整分析生成数据")
        return True

    print("=" * 60)
    print(f"📋 信号查询: {start} ~ {end}" + (f" (过滤: {code})" if code else ""))
    print("=" * 60)
    # 控制台配色与报告统一: 红=买入方向(涨), 绿=卖出方向(跌); 仅TTY时启用, 避免污染管道输出
    use_color = sys.stdout.isatty()
    RED, GREEN, RESET = "\033[91m", "\033[92m", "\033[0m"
    header = " ".join([
        _pad("日期", 10), _pad("代码", 6), _pad("名称", 18),
        f"{'涨跌':>8}", f"{'倍量':>7}", f"{'量能P':>6}", f"{'方向P':>6}", f"{'份额P':>6}", f"{'CP':>5}", "信号",
    ])
    print("  " + header)
    print("  " + "-" * (_disp_width(header) + 4))

    for r in rows:
        cp = r.get("composite_prob")
        level = r.get("signal_level")
        if level == "HIGH":
            sig = "🔴"
        elif level == "MID":
            sig = "🟡"
        else:
            sig = "🔴" if (cp or 0) >= 70 else ("🟡" if (cp or 0) >= 50 else "⚪")
        name = _trunc(r.get("name") or "", 18)
        chg_v = r.get("change_pct")
        chg_s = f"{fmt_chg(chg_v):>8}"
        if use_color and isinstance(chg_v, (int, float)) and chg_v != 0:
            chg_s = (RED if chg_v > 0 else GREEN) + chg_s + RESET
        line = " ".join([
            _pad(r.get("date") or "", 10), _pad(r.get("code") or "", 6), _pad(name, 18),
            chg_s, f"{fmt_vr(r.get('volume_ratio')):>7}",
            f"{fmt_pct(r.get('vol_prob')):>6}", f"{fmt_pct(r.get('dir_prob')):>6}",
            f"{fmt_pct(r.get('share_prob')):>6}", f"{fmt_pct(cp):>5}", sig,
        ])
        print("  " + line)
    return True


# ============================================================
# 主程序
# ============================================================

def record_shares_only():
    """仅采集当日份额数据到本地DB（不跑完整分析）"""
    if not DATA_STORE_AVAILABLE:
        print("❌ etf_data_store 不可用")
        return
    store = ETFDataStore()
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"📊 采集 {today} 的ETF份额数据...")
    for code, info in ETFS.items():
        print(f"  📊 {code} {info['n']}...", end=" ")
        sh_data = fetch_fund_shares(code)
        if sh_data:
            d_date = sh_data["data_date"]
            store.upsert_record(d_date, code, {
                "date": d_date, "code": code,
                "name": info["n"], "idx_name": info["idx"],
                "shares_yi": sh_data.get("shares_yi"),
            })
            store.upsert_shares_bulk({d_date: {code: {
                "shares_yi": sh_data["shares_yi"], "ts": datetime.now().isoformat()}}})
            print(f"✅ {sh_data['shares_yi']:.1f}亿份 (数据日 {d_date})")
        else:
            print("❌ 获取失败")
    stats = store.get_stats()
    print(f"\n📊 数据库状态: {stats['total_records']}条记录, {stats['total_dates']}个交易日")
    print(f"   日期范围: {stats['date_range'][0]} ~ {stats['date_range'][1]}")
    print(f"   含份额: {stats['records_with_shares']}条")


def main(target_date=None, record_only=False):
    # 初始化 DB
    store = ETFDataStore() if DATA_STORE_AVAILABLE else None

    if record_only:
        record_shares_only()
        return

    print("=" * 70)
    print("🛡️ ETF国家队资金监测 — 三因子模型 + 本地DB")
    print(f"   量能50% + 方向20% + 份额30%")
    if store:
        db_stats = store.get_stats()
        print(f"   📦 本地DB: {db_stats['total_records']}条 / {db_stats['total_dates']}日")
    print(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)

    # 1. 获取沪深300指数数据
    print("\n📊 Step 1: 获取沪深300指数数据...")
    idx_300 = fetch("sh000300", 60)
    if idx_300:
        print(f"  ✅ {len(idx_300)}条  {idx_300[-1]['date']}~{idx_300[0]['date']}")

    # 2. 加载历史份额数据（优先DB shares_raw 全量, 其次JSON, 再akshare回溯）
    print("\n📊 Step 2: 加载历史份额数据...")
    shares_history = store.get_shares_history() if store else {}
    if shares_history:
        print(f"  📦 DB原始份额: {len(shares_history)}日")
    else:
        shares_history = load_shares_history()  # JSON 兼容回退
        print(f"  📦 JSON历史: {len(shares_history)}日")
    
    # 清理空日期 (push2失败残留)
    empty_dates = [d for d, v in shares_history.items() if isinstance(v, dict) and len(v) == 0]
    for d in empty_dates:
        del shares_history[d]
    if empty_dates:
        print(f"  🧹 清理{len(empty_dates)}个空日期: {empty_dates}")

    # 如果历史数据不足60天, 用akshare回溯补充
    need_dates = 60 - len(shares_history)
    if need_dates > 10:
        print(f"  📡 JSON历史不足({len(shares_history)}日), 需补充{need_dates}日...")
        # 获取过去N个交易日的日期列表
        all_dates = set()
        if idx_300:
            for d in idx_300:
                all_dates.add(d['date'])
        # 也从每只ETF的K线获取日期
        for code in ETFS:
            try:
                kdata = fetch(code, 60)
                for d in kdata:
                    all_dates.add(d['date'])
            except:
                pass
        # 筛选需要补充的日期
        dates_to_fetch = sorted([d for d in all_dates if d not in shares_history])
        # 限制回溯数量，避免超长等待(每次最多回溯20天)
        MAX_BACKFILL = 20
        if len(dates_to_fetch) > MAX_BACKFILL:
            print(f"  📡 仅回溯最近{MAX_BACKFILL}日(共需{len(dates_to_fetch)}日, 后续增量采集)")
            dates_to_fetch = dates_to_fetch[-MAX_BACKFILL:]
        if dates_to_fetch:
            print(f"  📡 从akshare回溯{len(dates_to_fetch)}日份额数据...")
            bulk_history = fetch_history_shares_bulk(dates_to_fetch)
            if bulk_history:
                new_count = 0
                for d, entries in bulk_history.items():
                    if d not in shares_history:
                        shares_history[d] = {}
                    shares_history[d].update(entries)
                    new_count += 1
                _persist_shares(store, shares_history)
                print(f"  ✅ 补充了{new_count}日份额数据")
    
    # 获取实时份额并写入本地DB (按实际数据日存储, 盘中取到的是最近发布日)
    if store and not target_date:
        today_str = datetime.now().strftime("%Y-%m-%d")
        print(f"  📡 采集 {today_str} 实时份额数据...")
        shares_collected = 0
        stale_dates = set()
        for code, info in ETFS.items():
            sh_data = fetch_fund_shares(code)
            if sh_data:
                d_date = sh_data["data_date"]
                store.upsert_record(d_date, code, {
                    "date": d_date, "code": code,
                    "name": info["n"], "idx_name": info["idx"],
                    "shares_yi": sh_data.get("shares_yi"),
                })
                # 也记录到 JSON（保持兼容）
                if d_date not in shares_history:
                    shares_history[d_date] = {}
                shares_history[d_date][code] = {"shares_yi": sh_data["shares_yi"], "ts": datetime.now().isoformat()}
                print(f"    ✅ {code} {info['n'][:12]}: {sh_data['shares_yi']:.1f}亿份 (数据日 {d_date})")
                shares_collected += 1
                if d_date != today_str:
                    stale_dates.add(d_date)
            else:
                print(f"    ⚠️ {code} {info['n'][:12]}: 份额数据暂未发布")
        if shares_collected == 0:
            print("  ⚠️ 盘中运行：当日份额未发布(盘后约19:00)，今日信号仅基于盘中量价+昨日份额，建议19:30后重跑")
        elif stale_dates:
            print(f"  ⚠️ 份额数据为最近发布日(盘中为{max(stale_dates)})，今日无当日份额，今日信号退化为二因子")
        _persist_shares(store, shares_history)
    elif store and target_date:
        # 指定日期：尝试从akshare获取该日的份额数据
        d8 = target_date.replace('-', '')
        bulk = fetch_history_shares_bulk([target_date])
        if bulk and target_date in bulk:
            for code, entry in bulk[target_date].items():
                if target_date not in shares_history:
                    shares_history[target_date] = {}
                shares_history[target_date][code] = entry
            _persist_shares(store, shares_history)
            print(f"  ✅ 已从akshare获取{target_date}的份额数据")
        else:
            db_shares = store.get_range(start_date=target_date, end_date=target_date)
            if db_shares:
                print(f"  📦 本地DB有{target_date}的份额记录 ({len(db_shares)}条)")
            else:
                print(f"  ⚠️ 无{target_date}份额数据，退化为二因子")
    print(f"  📊 累计历史: {len(shares_history)}日")

    # 3. 构建份额映射
    shares_map = {}
    for code in ETFS:
        shares_map[code] = {}
        for date, entries in shares_history.items():
            if isinstance(entries, dict) and code in entries:
                target_sh, prev_sh, delta_yi, delta_pct = get_historical_share(code, date, shares_history)
                shares_map[code][date] = {
                    "shares_yi": entries[code].get("shares_yi"),
                    "delta_yi": delta_yi,
                    "delta_pct": delta_pct,
                }

    # 4. 获取ETF行情 + 三因子分析
    print("\n📊 Step 3: 获取ETF行情 + 三因子分析...")
    if target_date:
        print(f"  🎯 目标分析日期: {target_date}")

    all_hist = {}
    latest_map = {}
    target_shares_data = {}

    for code, info in ETFS.items():
        print(f"\n  📊 {code} {info['n']} ({info['idx']})")
        data = fetch(code, 60)
        if not data:
            print("    ❌ 数据获取失败")
            continue
        if store:
            store.upsert_klines(code, data)  # 原始K线入库, 逐日累积突破60天API窗口
        if len(data) < 22:
            print(f"    ⚠️ 仅{len(data)}条，不足22条")
            continue

        hist = analyze_all(data, idx_300, shares_map, target_date or "", code, 60)
        if not hist:
            print("    ⚠️ 分析失败")
            continue

        all_hist[code] = hist

        # 找到目标日期的分析结果
        target_hist = None
        if target_date:
            for h in hist:
                if h["d"] == target_date:
                    target_hist = h
                    break
        if not target_hist:
            target_hist = hist[-1]

        l = target_hist

        # 获取份额数据（目标日期）
        sh_on_target = shares_map.get(code, {}).get(target_date or l["d"], {})
        target_shares_data[code] = sh_on_target

        latest_map[code] = {
            "d": l["d"], "c": l["c"], "chg": l["chg"], "cp": l["cp"],
            "vr": l["vr"], "vp": l["vp"], "dp": l["dp"], "sp": l["sp"],
            "v": l["v"], "vma": l["vma"],
            "shares_yi": sh_on_target.get("shares_yi"),
            "delta_yi": sh_on_target.get("delta_yi"),
            "delta_pct": sh_on_target.get("delta_pct"),
        }

        sp_str = f"份额P:{l['sp']:.0f}%" if l.get("has_shares") else "份额P:N/A"
        s = "🔥" if l["cp"] >= 70 else ("⚠️" if l["cp"] >= 50 else "○")
        model_flag = "三因子" if l.get("has_shares") else "二因子"
        t = f"[{l['tag']}]" if l.get("tag") else ""
        print(f"    {s} {l['d']} {t} | {l['chg']:+.2f}% | {l['v']:.0f}万({l['vr']:.2f}x) | 量能P:{l['vp']:.0f}% 方向P:{l['dp']:.0f}% {sp_str} → CP:{l['cp']:.0f}% [{model_flag}]")

    # 5. 重要信号回溯 (生成 actual_date)
    print("\n" + "=" * 70)
    print("📋 历史重要信号回溯（三因子模型）")
    print("=" * 70)
    date_sig = {}
    for code, hist in all_hist.items():
        for h in hist:
            d = h["d"]
            if d not in date_sig:
                date_sig[d] = {"total": 0, "high": 0, "mid": 0, "codes": []}
            date_sig[d]["total"] += 1
            if h["cp"] >= 70: date_sig[d]["high"] += 1; date_sig[d]["codes"].append(f"{code}({h['cp']:.0f}%)")
            elif h["cp"] >= 50: date_sig[d]["mid"] += 1
    sigs = [(d, v) for d, v in date_sig.items() if v["high"] >= 2 or v["high"] + v["mid"] >= 4]
    sigs.sort(key=lambda x: x[0], reverse=True)
    if sigs:
        for d, v in sigs[:10]:
            t = SPECIAL.get(d, "")
            ts = f" [{t}]" if t else ""
            print(f"  📅 {d}{ts}: {v['high']}🔴+{v['mid']}🟡 → {', '.join(v['codes'][:5])}")
    else:
        print("  ℹ️ 无多ETF同步信号")

    actual_date = target_date if target_date else list(date_sig.keys())[-1]

    # 计算当日沪深300涨跌（用于DB记录）
    idx_gain = 0
    if idx_300 and len(idx_300) >= 2:
        idx_today = None
        for d in idx_300:
            if d["date"] == actual_date:
                idx_today = d
                break
        if idx_today:
            prev_idx = None
            for d in idx_300:
                if d["date"] < actual_date:
                    prev_idx = d
                    break
            if prev_idx and prev_idx.get("c"):
                idx_gain = round((idx_today["c"] - prev_idx["c"]) / prev_idx["c"] * 100, 2)

    # 6. 记录分析结果到本地DB
    if store:
        print(f"\n💾 Step 6: 保存分析结果到本地DB...")
        etf_results_for_db = {}
        for code, hist in all_hist.items():
            for h in hist:
                if h["d"] == actual_date:
                    sh = target_shares_data.get(code, {})
                    etf_results_for_db[code] = {
                        "name": ETFS[code]["n"], "idx_name": ETFS[code]["idx"],
                        "c": h["c"], "chg": h["chg"],
                        "v": h["v"], "vma": h["vma"], "vr": h["vr"],
                        "vp": h["vp"], "dp": h["dp"], "sp": h["sp"], "cp": h["cp"],
                        "shares_yi": sh.get("shares_yi"),
                        "delta_yi": sh.get("delta_yi"),
                        "delta_pct": sh.get("delta_pct"),
                    }
                    break
        cnt = store.record_from_v6_result(actual_date, etf_results_for_db, idx_gain)
        print(f"  ✅ 已记录 {cnt}条数据到本地DB")
        db_stats = store.get_stats()
        print(f"  📦 DB总量: {db_stats['total_records']}条 / {db_stats['total_dates']}日 / 含份额{db_stats['records_with_shares']}条")
    else:
        print(f"\n💾 本地DB不可用，跳过数据持久化")

    # 7. 生成HTML (原step 6)
    print(f"\n🎨 Step 7: 生成三因子HTML报告 (分析日: {actual_date})...")
    html = gen_html(all_hist, target_shares_data, target_date or "")
    with open(THREE_FACTOR_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✅ {THREE_FACTOR_HTML} ({len(html)} bytes)")

    # 8. 保存JSON (原step 7)
    with open(THREE_FACTOR_OUT, "w", encoding="utf-8") as f:
        json.dump({
            "run_time": datetime.now().isoformat(),
            "model": "三因子: 量能50%+方向20%+份额30%",
            "target_date": actual_date,
            "signal_dates": [(d, v["high"], v["mid"], v["codes"][:4]) for d, v in sigs[:10]],
            "latest": latest_map,
            "shares_data": target_shares_data,
        }, f, ensure_ascii=False, indent=2)
    print(f"  ✅ {THREE_FACTOR_OUT}")

    return html


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ETF三因子监测 v6.1")
    parser.add_argument("--date", type=str, default=None,
                        help="分析日期 (YYYY-MM-DD)，默认最近交易日")
    parser.add_argument("--record", action="store_true",
                        help="仅采集当日份额数据入库，不做完整分析")
    parser.add_argument("--stats", action="store_true",
                        help="查看本地DB状态，不做分析")
    parser.add_argument("--healthcheck", action="store_true",
                        help="环境健康检查(akshare/数据源/DB)，不做分析")
    parser.add_argument("--backfill", action="store_true",
                        help="一次性回补全部份额历史，不做分析")
    parser.add_argument("--query", action="store_true",
                        help="从本地DB查询历史信号，不做分析")
    parser.add_argument("--days", type=int, default=7,
                        help="--query 回溯天数 (默认7)")
    parser.add_argument("--code", type=str, default=None,
                        help="--query 过滤ETF代码")
    args = parser.parse_args()

    if args.stats:
        if not DATA_STORE_AVAILABLE:
            print("❌ etf_data_store.py 不可用")
            sys.exit(1)
        store = ETFDataStore()
        stats = store.get_stats()
        print("=" * 60)
        print("📊 ETF本地数据库状态")
        print("=" * 60)
        print(f"  数据库路径: {store.db_path}")
        print(f"  总记录数:   {stats['total_records']}")
        print(f"  覆盖交易日: {stats['total_dates']}")
        print(f"  日期范围:   {stats['date_range'][0]} ~ {stats['date_range'][1]}")
        print(f"  含份额记录: {stats['records_with_shares']}/{stats['total_records']}")
        print(f"  原始数据:   K线 {stats.get('klines_records', 0)}条 / 份额 {stats.get('shares_records', 0)}条")
        print(f"\n  最近5个交易日:")
        for d, cnt in stats["recent_dates"]:
            print(f"    {d}: {cnt}只ETF")
        if stats["total_records"] == 0:
            print("\n  💡 提示: 运行 --record 采集今日数据入库")
        sys.exit(0)

    if args.healthcheck:
        sys.exit(0 if healthcheck() else 1)

    if args.backfill:
        sys.exit(0 if backfill_shares() else 1)

    if args.query:
        sys.exit(0 if query_signals(args.days, args.code) else 1)

    main(args.date, args.record)