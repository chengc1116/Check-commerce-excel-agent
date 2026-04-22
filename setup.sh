#!/bin/bash
# ============================================
# 产品立项审核系统 — Linux 一键部署脚本
# ============================================
set -e

echo "========================================"
echo "  产品立项审核系统 — Linux 部署"
echo "========================================"

# ---- 颜色 ----
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# ---- 检测系统包管理器 ----
if command -v apt-get &>/dev/null; then
    PKG_MGR="apt"
    echo -e "${GREEN}检测到 apt 包管理器 (Debian/Ubuntu)${NC}"
elif command -v yum &>/dev/null; then
    PKG_MGR="yum"
    echo -e "${GREEN}检测到 yum 包管理器 (CentOS/RHEL)${NC}"
elif command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
    echo -e "${GREEN}检测到 dnf 包管理器 (Fedora)${NC}"
elif command -v opkg &>/dev/null; then
    PKG_MGR="opkg"
    echo -e "${YELLOW}检测到 opkg (Entware/OpenWrt)，需手动安装 Python${NC}"
else
    PKG_MGR="unknown"
    echo -e "${RED}未检测到已知包管理器，请手动安装 Python 3.10+${NC}"
fi

# ---- 1. 安装 Python ----
PYTHON_CMD=""

# 先检查系统已有的 Python
if command -v python3 &>/dev/null; then
    PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
        PYTHON_CMD="python3"
        echo -e "${GREEN}已安装 Python $PY_VERSION，满足要求${NC}"
    else
        echo -e "${YELLOW}已安装 Python $PY_VERSION，但需要 3.10+${NC}"
    fi
fi

# 如果没有合适的 Python，尝试安装
if [ -z "$PYTHON_CMD" ]; then
    echo ""
    echo -e "${YELLOW}正在安装 Python 3...${NC}"

    if [ "$PKG_MGR" = "apt" ]; then
        sudo apt-get update
        sudo apt-get install -y python3 python3-venv python3-pip
        PYTHON_CMD="python3"
    elif [ "$PKG_MGR" = "yum" ]; then
        sudo yum install -y python3 python3-pip
        PYTHON_CMD="python3"
    elif [ "$PKG_MGR" = "dnf" ]; then
        sudo dnf install -y python3 python3-pip
        PYTHON_CMD="python3"
    else
        echo -e "${RED}无法自动安装 Python，请手动安装 Python 3.10+ 后重新运行此脚本${NC}"
        echo ""
        echo "常见安装方式："
        echo "  Ubuntu/Debian: sudo apt install python3 python3-venv python3-pip"
        echo "  CentOS/RHEL:   sudo yum install python3 python3-pip"
        echo "  群晖 NAS:      套件中心安装 Python 3"
        echo "  通用:          从 https://www.python.org/downloads/ 下载源码编译"
        exit 1
    fi

    # 验证安装
    PY_VERSION=$($PYTHON_CMD -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    echo -e "${GREEN}Python $PY_VERSION 安装完成${NC}"
fi

# 确保 venv 模块可用（Debian/Ubuntu 上 venv 可导入但 ensurepip 缺失）
# 先尝试实际创建临时 venv 来验证
echo -e "${YELLOW}检查 venv + ensurepip 是否可用...${NC}"
VENV_OK=true
$PYTHON_CMD -m venv /tmp/_check_venv &>/dev/null || VENV_OK=false
rm -rf /tmp/_check_venv

if [ "$VENV_OK" = false ]; then
    echo -e "${YELLOW}venv/ensurepip 不可用，正在安装...${NC}"
    PY_VERSION=$($PYTHON_CMD -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    if [ "$PKG_MGR" = "apt" ]; then
        sudo apt-get update
        sudo apt-get install -y "python${PY_VERSION}-venv" python3-venv
    elif [ "$PKG_MGR" = "yum" ]; then
        sudo yum install -y python3-venv 2>/dev/null || echo "venv 可能已包含在 python3 中"
    elif [ "$PKG_MGR" = "dnf" ]; then
        sudo dnf install -y python3-venv 2>/dev/null || echo "venv 可能已包含在 python3 中"
    fi
    # 再次验证
    $PYTHON_CMD -m venv /tmp/_check_venv2 &>/dev/null || {
        echo -e "${RED}安装 venv 后仍然失败，请手动执行: sudo apt install python${PY_VERSION}-venv${NC}"
        exit 1
    }
    rm -rf /tmp/_check_venv2
    echo -e "${GREEN}venv 安装完成${NC}"
else
    echo -e "${GREEN}venv + ensurepip 可用${NC}"
fi

# ---- 2. 创建虚拟环境 ----
echo ""
echo -e "${YELLOW}[2/5] 创建虚拟环境...${NC}"

if [ -d ".venv" ]; then
    echo -e "${YELLOW}.venv 已存在，跳过创建（如需重建请先 rm -rf .venv）${NC}"
else
    $PYTHON_CMD -m venv .venv
    echo -e "${GREEN}虚拟环境创建完成${NC}"
fi

# 激活虚拟环境
source .venv/bin/activate
echo -e "${GREEN}虚拟环境已激活: $(which python)${NC}"

# ---- 3. 安装依赖 ----
echo ""
echo -e "${YELLOW}[3/5] 安装 Python 依赖...${NC}"
pip install --upgrade pip -i https://mirrors.aliyun.com/pypi/simple/
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
echo -e "${GREEN}依赖安装完成${NC}"

# ---- 4. 配置环境变量 ----
echo ""
echo -e "${YELLOW}[4/5] 检查配置文件...${NC}"

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo -e "${RED}已从 .env.example 创建 .env，请编辑填写 API Key！${NC}"
    echo -e "${RED}  vi .env${NC}"
else
    echo -e "${GREEN}.env 已存在${NC}"
fi

# ---- 5. 创建必要目录 ----
echo ""
echo -e "${YELLOW}[5/5] 创建数据目录...${NC}"
mkdir -p data uploads output
echo -e "${GREEN}目录创建完成${NC}"

# ---- 完成 ----
echo ""
echo "========================================"
echo -e "${GREEN}  ✅ 部署完成！${NC}"
echo "========================================"
echo ""
echo "启动 Web 服务："
echo "  source .venv/bin/activate"
echo "  python web_app.py"
echo ""
echo "启动飞书 Bot："
echo "  source .venv/bin/activate"
echo "  python run_feishu_bot.py"
echo ""
echo "后台运行（推荐生产环境）："
echo "  nohup .venv/bin/python web_app.py > web.log 2>&1 &"
echo ""
echo "访问地址: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo '服务器IP'):8080"
