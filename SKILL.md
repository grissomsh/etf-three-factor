---
name: etf-three-factor
description: 🛡️ 三因子ETF国家队资金监测系统 — 追踪国家队（中央汇金）ETF操作信号。数据源（腾讯财经K线 + akshare上交所/深交所份额）、完整历史回溯、一键分析。支持：① 数据获取（腾讯财经K线 + akshare上交所/深交所份额）② 本地SQLite存档（分析结果+原始K线/份额）③ 三因子分析（量能P50% + 方向P20% + 份额P30%）④ 每ETF独立卡片的交互式HTML报告（点击查看支撑数据）。当用户需要运行ETF三因子分析、查询国家队信号、查看每日监测报告、或设置定时任务时使用。
---

# 🛡️ etf-three-factor — 三因子ETF监测系统

## 核心文件

- **`scripts/etf_threefactor.py`** — 主分析脚本，一键流水线
- **`scripts/etf_data_store.py`** — SQLite本地数据存储模块
- **`setup.sh`** — 一键部署脚本（建目录/复制脚本/装akshare/配置邮箱）
- **`references/etf_model.md`** — 三因子模型详细说明
- **`references/config.md`** — 配置与部署指南

---

## 🚀 快速开始

### 一键完整运行（推荐）
```bash
cd ~/.etf-skill/scripts
python3 etf_threefactor.py
```
自动执行完整流水线：获取数据 → 存档 → 分析 → 生成HTML → 保存JSON

### 分功能调用

| 功能 | 命令 | 说明 |
|------|------|------|
| 📊 完整分析 | `python3 etf_threefactor.py` | 一键全流程 |
| 📅 分析特定日期 | `python3 etf_threefactor.py --date 2026-04-30` | 历史回溯分析 |
| 📡 仅采集份额入库 | `python3 etf_threefactor.py --record` | 只记录不含分析 |
| 📦 查看数据库状态 | `python3 etf_threefactor.py --stats` | 统计信息 |
| 🩺 健康检查 | `python3 etf_threefactor.py --healthcheck` | 环境自检(数据源/DB) |
| 📡 完整回溯 | `python3 etf_threefactor.py --backfill` | 一次性补满份额历史 |
| 📋 信号查询 | `python3 etf_threefactor.py --query --days 7` | 从DB查历史信号 |

---

## 📊 输出文件

| 文件 | 位置 | 说明 |
|------|------|------|
| HTML报告 | `~/.etf-skill/workspace/ETF三因子分析.html` | 16:9可视化报告 |
| JSON数据 | `~/.etf-skill/workspace/ETF三因子分析.json` | 纯数据 |
| SQLite DB | `~/.etf-skill/workspace/etf_history.db` | 历史数据本地存储 |
| 份额历史 | `~/.etf-skill/workspace/etf_shares_history.json` | 份额JSON备份 |

---

## 🧠 三因子模型

```
综合概率 = 量能概率 × 50% + 方向概率 × 20% + 份额概率 × 30%
```

| 因子 | 权重 | 来源 | 说明 |
|------|------|------|------|
| 量能概率 | 50% | 腾讯财经K线 | 日成交量 ÷ 20日均量 |
| 方向概率 | 20% | 腾讯财经K线 | 护盘特征：逆市+超额+前几日跌 |
| 份额概率 | 30% | akshare交易所 | 日份额变化检测一级市场申赎 |

**信号分级**: 🔴 ≥70% 高确信 | 🟡 50~70% 中等 | ⚪ <50% 正常

---

## 🔗 数据源

| 数据 | API | 回溯能力 |
|------|-----|----------|
| K线行情 | `web.ifzq.gtimg.cn` | 60天历史 |
| 上交所份额 | `akshare.fund_etf_scale_sse(date)` | ✅ 完整历史 |
| 深交所份额 | `akshare.fund_scale_daily_szse(start,end)` | ✅ 完整历史 |

> ⚠️ 份额数据**盘后更新**（约19:00后），盘中运行将自动使用最新可用日。

---

## ⏰ 定时任务

建议工作日 16:30 运行（收盘后份额已发布）：

```bash
openclaw cron add
# name: "ETF三因子日报"
# schedule: 30 16 * * 1-5 (Asia/Shanghai)
# command: python3 ~/.etf-skill/scripts/etf_threefactor.py
```

---

## 📈 监控ETF

| 代码 | 名称 | 跟踪指数 | 交易所 |
|------|------|----------|--------|
| 510300 | 华泰柏瑞沪深300ETF | 沪深300 | 上交所 |
| 510310 | 易方达沪深300ETF | 沪深300 | 上交所 |
| 510330 | 华夏沪深300ETF | 沪深300 | 上交所 |
| 159919 | 嘉实沪深300ETF | 沪深300 | 深交所 |
| 510050 | 华夏上证50ETF | 上证50 | 上交所 |
| 510500 | 华泰柏瑞中证500ETF | 中证500 | 上交所 |
| 512100 | 南方中证1000ETF | 中证1000 | 上交所 |

---

## 🔧 环境依赖

- Python 3.7+
- **akshare** — `pip3 install akshare`
- 其余为 Python 标准库（json, urllib, sqlite3, argparse）

---

更多详情:
- 📐 模型数学原理 → `references/etf_model.md`
- ⚙️ 环境配置/部署 → `references/config.md`
