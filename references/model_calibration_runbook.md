# 模型迭代 Runbook（可复现流程）

本文档定义三因子模型的**受控迭代流程**。任何触及模型常数（因子映射、权重、阈值）的改动都必须走完本章步骤并留下落地记录；跳过门禁的改动视为回归缺陷。

---

## 0 前置条件

```bash
bash setup.sh          # 或确认 ~/.etf-skill/{scripts,venv} 就绪
python3 --version      # ≥3.7；PEP668 环境用 ~/.etf-skill/venv/bin/python
```

工具链（全部随仓库分发）：

| 工具 | 作用 |
|------|------|
| `tests/test_events.py` | 8 个历史事件锚点回归（4 正 + 4 负，静态 fixtures 离线运行） |
| `tests/sensitivity.py` | 权重单纯形扫描（可行域边界与分离边际） |
| `scripts/calibrate.py` | 全市场份额数据集 → 分位数报告 → 候选映射 gate 模拟 |

## 1 建立事实基线（每次迭代的起点）

```bash
python3 scripts/calibrate.py fetch      # 断点续传, 全市场日频份额(2020起+SSE/SZSE≥2024)
python3 scripts/calibrate.py vr         # 监控池倍量分布
python3 scripts/calibrate.py report     # 生成/刷新 references/calibration.md
python3 tests/test_events.py            # 确认当前代码 8/8 PASS
python3 tests/sensitivity.py            # 记录当前 margin 与邻域可行性
```

产出判读要点见 `calibration.md` §3/§5/§6（阈值↔经验百分位对照、规模分层、倍量稀有度）。

## 2 提出候选改动（不改主代码）

在 `calibrate.py` 中以 `_mk_piecewise` 定义变体，加入 `cmd_gate` 的 `variants` 字典后运行：

```bash
python3 scripts/calibrate.py gate       # 变体在 8 锚点上的离线判定对比
```

筛选标准：
- 正锚点判定不得弱于现行（强信号日数、峰高）
- 负锚点特别是 neg-2021-04 的每日峰值应下降或持平
- 若多个变体同过 gate，选噪声底更低者

## 3 落地前的正式门禁（顺序执行，任一失败即停）

```bash
# 1. 写入新常数后
python3 tests/test_events.py            # 必须 8/8 PASS
python3 tests/sensitivity.py            # margin 相对上一落地版本的降幅 ≤1 分为绿灯;
                                        # >3 分或有邻域结构恶化需回退重议
~/.etf-skill/venv/bin/python scripts/etf_threefactor.py --date <最近事件日>
                                        # 实跑抽查: 信号档位不降级
```

## 4 落地记录（强制）

在 `references/calibration.md` 追加 `## N 落地记录：<名称>（日期）` 一节，固定包含：

```markdown
**动机**：对应 §7 结论第 N 条。
**新映射/常数**：（逐段列出新旧对照）
**落地前门禁**：gate 对比表 + test_events 结果 + sensitivity margin 前后值 + 实跑抽查
**效果预估**：对平静期噪声与事件检测的量化预期
```

同时在 `etf_model.md` 同步被修改的章节表格（保持文档=代码），并在本文件末尾"版本历史"登记一行。

## 5 铁律

- 锚点期望来自公开事实，失败先调查（数据源差异/模型盲区），**永不放宽期望**
- 二因子/三因子的分数不可直接比较，跨模式对比必须看标识
- 样本量级仍是数个事件窗：单一指标改善不足以落地，需要 §3 全链路一致
- 数据集缓存在仓库内（`tests/data/calib/`）保证可复现；刷新数据后应重算报告并注明窗口

## 版本历史

| 日期 | 变更 | 门禁 |
|------|------|------|
| 2026-08-27 | sprob 落地 VAR-C 映射（§8）| gate 三变体一致 / 8 PASS / margin 11.9→11.4 |
