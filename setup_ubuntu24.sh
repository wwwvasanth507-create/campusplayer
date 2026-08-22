#!/usr/bin/env bash
# ==============================================================================
# CampusPlayer - Automated Fresh Project Setup for Ubuntu 24.04 LTS
# ==============================================================================
# Usage:
#   git clone https://github.com/wwwvasanth507-create/campusplayer.git
#   cd campusplayer
#   chmod +x setup_ubuntu24.sh deploy.sh
#   ./setup_ubuntu24.sh
# ==============================================================================

set -e

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${CYAN}============================================================${NC}"
echo -e "${CYAN}  Campus Player - Fresh Project Setup (Ubuntu 24.04 LTS)   ${NC}"
echo -e "${CYAN}============================================================${NC}"

# Step 1: System Dependencies
echo -e "\n${YELLOW}[1/7] Installing System Dependencies (apt)...${NC}"
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq || true
    sudo apt-get install -y -qq python3-full python3-pip python3-venv ffmpeg git curl build-essential libssl-dev libffi-dev
else
    echo -e "${RED}⚠️  Warning: apt-get not found. Ensure Python 3, venv, and ffmpeg are installed.${NC}"
fi

# Step 2: Virtual Environment Setup
echo -e "\n${YELLOW}[2/7] Setting up Python Virtual Environment...${NC}"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

VENV_PYTHON="./venv/bin/python3"
VENV_PIP="./venv/bin/pip"

if [ ! -f "$VENV_PYTHON" ]; then
    VENV_PYTHON="python3"
    VENV_PIP="pip"
fi

$VENV_PIP install --upgrade pip --quiet
echo -e "${GREEN}✓ Virtual environment initialized.${NC}"

# Step 3: Python Package Installation
echo -e "\n${YELLOW}[3/7] Installing Python Dependencies from requirements.txt...${NC}"
$VENV_PIP install -r requirements.txt --quiet
echo -e "${GREEN}✓ All Python dependencies installed successfully.${NC}"

# Step 4: Environment & Cryptographic Key Setup
echo -e "\n${YELLOW}[4/7] Configuring Environment Variables (.env)...${NC}"
if [ ! -f ".env" ]; then
    cp .env.example .env
    
    # Generate cryptographic keys
    SECRET_KEY=$($VENV_PYTHON -c "import secrets; print(secrets.token_urlsafe(32))")
    ENCRYPTION_KEY=$($VENV_PYTHON -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    
    # Inject keys into .env
    sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$SECRET_KEY/" .env
    sed -i "s/^ENCRYPTION_KEY=.*/ENCRYPTION_KEY=$ENCRYPTION_KEY/" .env
    echo -e "${GREEN}✓ Created new .env file with generated SECRET_KEY and ENCRYPTION_KEY.${NC}"
else
    echo -e "${GREEN}✓ Existing .env file found.${NC}"
fi

# Step 5: Directory Structure Verification
echo -e "\n${YELLOW}[5/7] Verifying Storage & Asset Directories...${NC}"
mkdir -p instance backups generated_pdfs static/hls static/subtitles static/uploads/chunks static/uploads/avatars static/uploads/institutions
echo -e "${GREEN}✓ All storage directories created.${NC}"

# Step 6: Fresh Database Migration & Initialization
echo -e "\n${YELLOW}[6/7] Running Database Migrations & Multi-Tenant Backfill...${NC}"
$VENV_PYTHON migrate_db.py
echo -e "${GREEN}✓ Database initialized and synced with zero errors.${NC}"

# Step 7: Systemd Production Service Setup (Optional)
echo -e "\n${YELLOW}[7/7] Setting up Systemd Service Configuration...${NC}"
SERVICE_FILE="/etc/systemd/system/campusplayer.service"
WORK_DIR="$(pwd)"

if [ -w "/etc/systemd/system" ] || sudo -n true 2>/dev/null; then
    cat <<EOF | sudo tee "$SERVICE_FILE" >/dev/null
[Unit]
Description=Campus Player Web Application
After=network.target

[Service]
User=$USER
WorkingDirectory=$WORK_DIR
ExecStart=$WORK_DIR/venv/bin/gunicorn --workers 4 --bind 0.0.0.0:5000 app:app
Restart=always
RestartSec=5
Environment=PATH=$WORK_DIR/venv/bin:\$PATH

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload || true
    echo -e "${GREEN}✓ Service unit installed at $SERVICE_FILE${NC}"
else
    echo -e "${YELLOW}ℹ️  Skipped systemd setup (sudo non-interactive access not available). You can manually enable it later.${NC}"
fi

echo -e "\n${CYAN}============================================================${NC}"
echo -e "${GREEN}  🎉 Campus Player Fresh Setup Completed Successfully!      ${NC}"
echo -e "${CYAN}============================================================${NC}"
echo -e "To start the application locally:"
echo -e "  ${YELLOW}./venv/bin/python3 app.py${NC}"
echo -e "Or via gunicorn:"
echo -e "  ${YELLOW}./venv/bin/gunicorn --workers 4 --bind 0.0.0.0:5000 app:app${NC}"
echo -e "Or via systemd service:"
echo -e "  ${YELLOW}sudo systemctl start campusplayer${NC}"
echo -e "${CYAN}============================================================${NC}"
