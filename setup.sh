#!/bin/bash

# カラー定義
GREEN='\033[0;32m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== ClassnoteX Backend Setup ===${NC}"

# Python の確認
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 could not be found."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Detected Python version: $PYTHON_VERSION"

# 仮想環境 (.venv) の作成
if [ ! -d ".venv" ]; then
    echo -e "${GREEN}Creating virtual environment (.venv)...${NC}"
    python3 -m venv .venv
else
    echo "Virtual environment (.venv) already exists."
fi

# 仮想環境のアクティベート
source .venv/bin/activate

# pip のアップグレード
echo -e "${GREEN}Upgrading pip...${NC}"
pip install --upgrade pip

# 依存パッケージのインストール
if [ -f "requirements.txt" ]; then
    echo -e "${GREEN}Installing dependencies from requirements.txt...${NC}"
    pip install -r requirements.txt
else
    echo "Warning: requirements.txt not found."
fi

echo -e "\n${GREEN}=== Setup Complete ===${NC}"
echo "To start the server in Mock mode (Local Development):"
echo "  source .venv/bin/activate"
echo "  export USE_MOCK_DB=1"
echo "  uvicorn app.main:app --reload --port 8000"
echo ""
