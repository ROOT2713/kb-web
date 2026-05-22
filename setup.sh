#!/usr/bin/env bash
# ============================================================
# kb-web 一键部署脚本
# 适用: Ubuntu 24.04 / Debian 12
# 用法: bash setup.sh
# ============================================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; exit 1; }

echo "============================================"
echo "  kb-web — 知识库 Web 服务 部署脚本"
echo "============================================"
echo ""

# ── 1. 系统依赖 ──────────────────────────────────────
log "检查系统依赖..."

NEED_APT=""
for pkg in poppler-utils tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng python3-venv python3-pip; do
    if ! dpkg -l "$pkg" &>/dev/null; then
        NEED_APT="$NEED_APT $pkg"
    fi
done

if [ -n "$NEED_APT" ]; then
    warn "需要安装: $NEED_APT"
    sudo apt-get update -qq
    sudo apt-get install -y $NEED_APT
else
    log "系统依赖已就绪"
fi

# ── 2. Python 虚拟环境 ────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    log "创建 Python 虚拟环境: $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

log "安装 Python 依赖..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"

# ── 3. 目录 ──────────────────────────────────────────
mkdir -p "$SCRIPT_DIR/uploads"
mkdir -p "$SCRIPT_DIR/data"
log "目录结构已创建"

# ── 4. 前置服务检查 ────────────────────────────────────
echo ""
echo "── 前置服务检查 ──"
echo ""

if systemctl is-active --quiet hindsight.service 2>/dev/null; then
    if curl -s --noproxy '*' http://localhost:8888/health >/dev/null 2>&1; then
        log "Hindsight 服务运行中 (localhost:8888) ✓"
    else
        warn "Hindsight 服务已启动但 /health 无响应"
    fi
else
    warn "Hindsight 服务未启动。请先部署 Hindsight:"
    echo "   参见 https://github.com/vectorize-io/hindsight"
    echo ""
fi

# ── 5. 环境变量检查 ────────────────────────────────────
echo ""
echo "── 环境变量检查 ──"
echo ""

ENV_FILE="$HOME/.hermes/.env"
MISSING_VARS=""

for var in DEEPSEEK_API_KEY MINERU_API_TOKEN; do
    if grep -q "^${var}=" "$ENV_FILE" 2>/dev/null; then
        log "$var 已配置 ✓"
    else
        warn "$var 未配置 — 对应功能将不可用"
        MISSING_VARS="$MISSING_VARS $var"
    fi
done

if [ -n "$MISSING_VARS" ]; then
    echo ""
    warn "缺失环境变量，请在 $ENV_FILE 中补充:"
    for v in $MISSING_VARS; do
        echo "   ${v}=<your-value>"
    done
fi

# ── 6. 安装 systemd 服务 ──────────────────────────────
echo ""
echo "── systemd 服务 ──"
echo ""

if [ -f "$SCRIPT_DIR/kb-web.service" ]; then
    read -p "是否安装 kb-web systemd 服务？[Y/n] " -r
    REPLY=${REPLY:-Y}
    if [[ "$REPLY" =~ ^[Yy] ]]; then
        sudo cp "$SCRIPT_DIR/kb-web.service" /etc/systemd/system/
        sudo systemctl daemon-reload
        sudo systemctl enable kb-web.service
        sudo systemctl restart kb-web.service
        sleep 2
        if systemctl is-active --quiet kb-web.service; then
            log "kb-web 服务已启动 ✓"
            echo ""
            log "访问地址: http://localhost:3002"
        else
            err "kb-web 服务启动失败，查看日志: sudo journalctl -u kb-web --no-pager -n 30"
        fi
    else
        warn "跳过 systemd 安装，手动启动:"
        echo "   cd $SCRIPT_DIR && .venv/bin/python3 server.py"
    fi
else
    warn "未找到 kb-web.service 文件，跳过 systemd 安装"
    echo "   手动启动: cd $SCRIPT_DIR && .venv/bin/python3 server.py"
fi

echo ""
echo "============================================"
log "部署完成！"
echo "============================================"
