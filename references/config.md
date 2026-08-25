# ⚙️ ETF三因子系统 — 配置与部署指南

---

## 🔧 环境依赖

```bash
# 系统要求
Python 3.7+
macOS / Linux

# Python 包
pip3 install -i https://mirrors.aliyun.com/pypi/simple/ akshare   # 交易所ETF份额数据（必需，国内用阿里云镜像加速）
# 其余为标准库：json, urllib, sqlite3, argparse
# 注意: PEP 668 受限环境(如 Homebrew Python) pip3 会失败,
#       run bash setup.sh 会自动创建 ~/.etf-skill/venv 虚拟环境并装入 akshare
#       (定时任务中需用 ~/.etf-skill/venv/bin/python 运行脚本)
```

---

## 🗄️ 数据库说明

### 基本信息

- 文件位置：`~/.etf-skill/workspace/etf_history.db`
- 引擎：SQLite3（无需安装数据库）
- 表名：`etf_daily` — 每只ETF每天一条记录

### 表字段

| 字段             | 类型 | 说明                   |
| ---------------- | ---- | ---------------------- |
| date             | TEXT | 日期 YYYY-MM-DD        |
| code             | TEXT | ETF代码                |
| name             | TEXT | ETF名称                |
| idx_name         | TEXT | 跟踪指数               |
| close_price      | REAL | 收盘价                 |
| change_pct       | REAL | 涨跌幅(%)              |
| volume           | REAL | 成交量(万手)           |
| volume_ma20      | REAL | 20日均量(万手)         |
| volume_ratio     | REAL | 倍量(vr)               |
| shares_yi        | REAL | 份额(亿份)             |
| shares_delta_yi  | REAL | 份额日变(亿份)         |
| shares_delta_pct | REAL | 份额日变(%)            |
| vol_prob         | REAL | 量能概率(%)            |
| dir_prob         | REAL | 方向概率(%)            |
| share_prob       | REAL | 份额概率(%)            |
| composite_prob   | REAL | 综合概率(%)            |
| idx_chg          | REAL | 沪深300涨跌幅(%)       |
| signal_level     | TEXT | 信号级别(HIGH/MID/LOW) |

### 原始数据表（klines_raw / shares_raw）

按需拉取后入库，逐日累积（无 60 天裁剪），供历史回溯与多样化分析：

| 表 | 字段 | 说明 |
|----|------|------|
| klines_raw | code, date, open, close, high, low, volume | 腾讯K线原始数据，主键(code,date)；每次运行增量写入，可突破60天API窗口 |
| shares_raw | date, code, shares_yi, ts | 交易所份额原始数据，主键(date,code)；date 为份额实际数据日（盘后19:00发布，盘中为最近发布日） |

> 份额读取优先 DB（全量），JSON `etf_shares_history.json` 仅作兼容备份（60天裁剪）。

---

## 🔗 API数据源

### 1. K线行情 — 腾讯财经

```
http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh510300,day,,,60,qfq
```

| 属性 | 值                |
| ---- | ----------------- |
| 数据 | 日线OHLC + 成交量 |
| 历史 | 可回溯60天        |
| 频率 | 每日收盘后更新    |
| 费用 | 免费，无需Key     |

### 2. 上交所ETF份额 — akshare

```python
import akshare as ak

df = ak.fund_etf_scale_sse(date='20260512')
# 返回全部上交所ETF当日份额
# df.columns: [序号, 基金代码, 基金简称, ETF类型, 统计日期, 基金份额]
```

| 属性 | 值              |
| ---- | --------------- |
| 数据 | ETF总份额（份） |
| 历史 | ✅ 完整历史     |
| 更新 | 盘后约19:00     |
| 费用 | 免费            |

### 3. 深交所ETF份额 — akshare

```python
import akshare as ak

df = ak.fund_scale_daily_szse(start_date='20260506', end_date='20260512', symbol='ETF')
# 返回指定日期范围内全部深交所ETF份额
# df.columns: [日期, 基金代码, 基金简称, 基金份额]
```

| 属性 | 值                          |
| ---- | --------------------------- |
| 数据 | ETF总份额（份）             |
| 历史 | ✅ 完整历史（支持日期范围） |
| 更新 | 盘后约19:00                 |
| 费用 | 免费                        |

---

## ⏰ 定时任务部署

### 建议方案

| 场景           | 时间         | 命令                                     |
| -------------- | ------------ | ---------------------------------------- |
| 日报（收盘后） | 工作日 16:30 | `python3 etf_threefactor.py`          |
| 仅记录份额     | 工作日 16:00 | `python3 etf_threefactor.py --record` |

### 创建定时任务（推荐：收盘后完整分析）

```bash
openclaw cron add
```

配置：

```yaml
name: "ETF三因子日报·16:30"
schedule: "30 16 * * 1-5" # 周一至周五 16:30（Asia/Shanghai）
sessionTarget: isolated
payload:
  kind: agentTurn
  message: |
    运行 ETF 三因子分析（完整流程）:
    cd ~/.etf-skill/scripts
    python3 etf_threefactor.py
  timeoutSeconds: 180
```

---

## 📁 文件清单

```
~/.etf-skill/
├── scripts/                         # 主脚本目录
│   ├── etf_threefactor.py        # 主分析脚本（一键流水线）
│   └── etf_data_store.py            # SQLite数据存储模块
└── workspace/
    ├── etf_history.db                # SQLite数据库
    ├── etf_shares_history.json       # 份额JSON历史（自动维护）
    ├── ETF三因子分析.html         # HTML报告
    └── ETF三因子分析.json         # JSON数据

# Skill说明文件（与本文件同级目录）
SKILL.md              # 技能说明
references/
  ├── etf_model.md    # 三因子模型详解
  └── config.md        # 本文件（配置指南）
```

---

## 🔄 自定义监控ETF

修改 `scripts/etf_threefactor.py` 中的 `ETFS` 字典：

```python
ETFS = {
    "510300": {"n": "华泰柏瑞沪深300ETF", "idx": "沪深300", "p": 5},
    # ↓ 新增ETF示例
    "588000": {"n": "华夏科创50ETF",     "idx": "科创50",  "p": 3},
}
```

新增的沪深ETF（51/56开头）由 akshare `fund_etf_scale_sse` 覆盖，深圳ETF（159开头）由 `fund_scale_daily_szse` 覆盖。

---

## 🛠️ 故障排查

### 健康检查失败怎么办

```bash
python3 etf_threefactor.py --healthcheck
```

逐项显示 ✅/❌：

- **akshare ❌** → `pip3 install akshare`
- **腾讯K线/份额API ❌** → 多为网络问题，或份额数据盘后约19:00未发布（可稍后重试）
- **SQLite ❌** → 确认 `~/.etf-skill/workspace` 目录存在且可写（sqlite 不会自动建目录）
- 任一关键项 ❌ 时退出码为 1，可在定时任务中捕获告警

### akshare 导入报错

```bash
pip3 install akshare --upgrade
```

### 份额数据为空（周末/假日）

正常现象。非交易日无份额数据，脚本自动回退到最近交易日。

### HTML文件过大

每次运行覆盖同一HTML文件。如需保留历史报告，复制到带日期的文件名。

### 首次运行

- 自动从akshare回溯最近20个交易日份额数据
- 后续每次运行递增1天（增量采集）
- 约40~50秒完成首次回溯

