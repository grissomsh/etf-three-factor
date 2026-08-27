# etf-three-factor

三因子 ETF 国家队资金监测系统，用来追踪国家队（中央汇金）在宽基 ETF 上的潜在操作信号。数据源：腾讯财经 K 线 + akshare 上交所/深交所官方份额接口（支持完整历史回溯）。

## Skill 描述

这个 skill 支持以下能力：

- 数据获取：腾讯财经 K 线 + akshare 上交所/深交所份额数据
- 本地存储：SQLite 持久化历史数据（分析结果 + 原始 K 线/份额）
- 三因子分析：量能分 50% + 方向分 20% + 份额分 30%
- 报告输出：每 ETF 独立卡片的交互式 HTML 报告（点击查看支撑数据）

适用场景：

- 运行 ETF 三因子分析
- 查询国家队信号
- 查看每日监测报告
- 配置定时任务自动执行

## 项目结构

- `SKILL.md`：skill 入口说明
- `setup.sh`：一键部署脚本
- `scripts/etf_threefactor.py`：主分析脚本
- `scripts/etf_data_store.py`：SQLite 数据存储模块
- `references/etf_model.md`：三因子模型详解

## 快速开始

```bash
# 1. 一键部署（建目录/复制脚本/装akshare）
bash setup.sh

# 2. 运行分析
cd scripts
python3 etf_threefactor.py
```

常用命令：

- `python3 etf_threefactor.py --record`：仅采集份额数据
- `python3 etf_threefactor.py --stats`：查看数据库状态
- `python3 etf_threefactor.py --date 2026-04-30`：分析指定日期

运维命令：

- `python3 etf_threefactor.py --healthcheck`：环境健康检查（数据源/DB）
- `python3 etf_threefactor.py --backfill`：一次性回补全部份额历史
- `python3 etf_threefactor.py --query --days 7`：从本地 DB 查询历史信号

## 自定义监控 ETF

修改 `scripts/etf_data_store.py` 中的 `ETFS` 字典（监控池单点定义，主脚本从此处 import，只需改这一处）：

```python
ETFS = {
    "510300": {"n": "华泰柏瑞沪深300ETF", "idx": "沪深300"},
    # ↓ 新增ETF示例
    "588000": {"n": "华夏科创50ETF",     "idx": "科创50"},
}
```

新增的沪深 ETF（51/56 开头）由 akshare `fund_etf_scale_sse` 覆盖，深圳 ETF（159 开头）由 `fund_scale_daily_szse` 覆盖。

## 测试（事件锚点回归）

用历史公开事件（国家队增持）作为锚点验证模型信号能力。fixtures 为静态数据快照，测试离线确定性运行：

```bash
python3 tests/test_events.py --list     # 查看锚点清单
python3 tests/test_events.py            # 运行全部锚点测试
python3 tests/test_events.py --build    # 重建 fixtures（需 akshare + 网络，约5-10分钟）
python3 tests/sensitivity.py           # 权重敏感性扫描（可行域与分离边际）
```

锚点集（4 正 + 4 负）：正锚点为 2023-10（汇金首次公告买入 ETF）、2024-02（扩大增持范围至中小盘）、2025-04（盘中公告增持）、2026-07（单周净申购 2036 亿创纪录）；负锚点为 2019-01 / 2021-04 / 2023-02 / 2026-08 四个数据驱动挑选的平静周。**期望值来自公开事实而非模型调参**——正锚点失败时应先调查（数据源差异/模型盲区），而不是放宽期望。

## 三因子模型

信号分计算方式：

```text
信号分 = 量能分 × 50% + 方向分 × 20% + 份额分 × 30%
```

其中：

- 量能因子：观察 ETF 成交量相对 20 日均量的异常放大程度
- 方向因子：结合 ETF 相对大盘表现、近期市场走势与护盘特征
- 份额因子：观察 ETF 份额变化，识别一级市场申购/赎回信号

**权重的依据**：50/20/30 并非任意设定——它在 8 个事件锚点上的敏感性扫描（`python3 tests/sensitivity.py`）中位于可行域内、贴近分离边际最优，且被两股反向约束夹持：量能+份额权重下移会让抱团期平静周产生误报（neg-2021-04），方向权重升到 0.25 则会漏掉 ev-2026-07 的多日连续信号。量能占 50% 对应二级市场买入这一最常见的国家队路径；份额 30% 是独立的一级市场通道；方向 20% 作环境过滤防止普涨误报。**不建议自行调整权重**——若必须改动，先跑锚点回归和敏感性扫描确认不破坏 8 个窗口的检测。

信号分级：

- `≥70 分`：高确信
- `50~70 分`：中等关注
- `< 50 分`：正常

更多细节见 [etf_model.md](references/etf_model.md)。

## 数据源

- 腾讯财经：ETF 日线 K 线与成交量数据
- akshare `fund_etf_scale_sse(date)`：上交所 ETF 份额历史
- akshare `fund_scale_daily_szse(start, end)`：深交所 ETF 份额历史

份额数据支持完整历史回溯（上交所/深交所官方接口），不再依赖只能读实时值的旧接口。

## 版权与来源

- 本项目整理自 B 站 `小贺FIRE了` 相关内容
- README 中的 skill 描述基于当前 [SKILL.md](SKILL.md) 整理
- 如原作者对署名或转载方式有进一步要求，建议按原作者说明补充或调整
