#!/usr/bin/env bash
# ============================================================
# ETF三因子 skill 一键部署脚本
# 用法: bash setup.sh
# 功能: 建目录 → 复制脚本 → 装akshare → 打印使用说明
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_HOME="${ETF_SKILL_HOME:-$HOME/.etf-skill}"
SCRIPTS_DIR="$SKILL_HOME/scripts"
WORKSPACE_DIR="$SKILL_HOME/workspace"
# PyPI镜像 (国内加速, 可通过 ETF_PYPI_MIRROR 覆盖)
PYPI_MIRROR="${ETF_PYPI_MIRROR:-https://mirrors.aliyun.com/pypi/simple/}"

echo "🛡️ ETF三因子 skill 部署"
echo "  目标目录: $SKILL_HOME"
echo ""

# 1. 创建目录结构
mkdir -p "$SCRIPTS_DIR" "$WORKSPACE_DIR"
echo "✅ 脚本目录: $SCRIPTS_DIR"
echo "✅ 工作区:   $WORKSPACE_DIR"

# 2. 复制脚本 (跳过 v6 遗留版本)
cp "$SCRIPT_DIR/scripts/etf_threefactor.py" "$SCRIPTS_DIR/"
cp "$SCRIPT_DIR/scripts/etf_data_store.py" "$SCRIPTS_DIR/"
echo "✅ 脚本已复制: etf_threefactor.py + etf_data_store.py"

# 3. Python 环境 + akshare
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ 未找到 python3, 请先安装 Python 3.7+"
    exit 1
fi
echo "✅ Python: $(python3 --version)"

VENV_DIR="$SKILL_HOME/venv"
PY_BIN="python3"
if [ -x "$VENV_DIR/bin/python" ]; then
    PY_BIN="$VENV_DIR/bin/python"
    echo "✅ 使用虚拟环境: $VENV_DIR"
elif python3 -c "import akshare" 2>/dev/null; then
    AK_VER=$(python3 -c "import akshare; print(getattr(akshare, '__version__', '?'))" 2>/dev/null || echo "?")
    echo "✅ akshare 已安装 (v$AK_VER)"
elif pip3 install -i "$PYPI_MIRROR" akshare 2>/dev/null; then
    echo "✅ akshare 安装完成"
else
    echo "📦 pip3 受限 (PEP 668 外部管理环境等), 改用独立虚拟环境 ..."
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip -q -i "$PYPI_MIRROR"
    "$VENV_DIR/bin/pip" install -q -i "$PYPI_MIRROR" akshare
    PY_BIN="$VENV_DIR/bin/python"
    echo "✅ akshare 已装入虚拟环境: $VENV_DIR"
fi

# 4. 完成
echo ""
echo "=================================================="
echo "🎉 部署完成! 常用命令:"
echo "  cd $SCRIPTS_DIR"
echo "  $PY_BIN etf_threefactor.py                   # 完整分析"
echo "  $PY_BIN etf_threefactor.py --healthcheck     # 环境自检 (推荐先跑)"
echo "  $PY_BIN etf_threefactor.py --backfill        # 一次性回补份额历史"
echo "  $PY_BIN etf_threefactor.py --query --days 7  # 查询历史信号"
echo "  定时任务建议: 工作日 16:30 运行完整分析 (见 references/config.md)"
