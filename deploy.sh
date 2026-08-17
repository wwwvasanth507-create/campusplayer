#!/usr/bin/env bash
# ==============================================================================
# CampusPlayer - Safe Server Deployment Script (Ubuntu / Linux)
# ==============================================================================
set -e

main() {
    APP_DIR="/opt/campusplayer"
    VENV_PYTHON="$APP_DIR/venv/bin/python3"
    VENV_PIP="$APP_DIR/venv/bin/pip"
    SERVICE_NAME="campusplayer.service"

    echo "============================================================"
    echo "  Deploying CampusPlayer Updates to $APP_DIR"
    echo "============================================================"

    # Step 1: Navigate to app directory
    cd "$APP_DIR"

    # Step 2: Ensure .env exists before pulling / starting
    if [ ! -f "$APP_DIR/.env" ]; then
        echo "⚠️  .env file not found. Initializing from .env.example..."
        cp "$APP_DIR/.env.example" "$APP_DIR/.env"
        echo "✅ .env initialized. Remember to review configuration in $APP_DIR/.env"
    fi

    # Step 3: Fetch and align repository to origin/main cleanly
    echo "[1/5] Syncing latest code from origin/main..."
    git fetch origin main
    git reset --hard origin/main

    # Step 4: Ensure necessary storage directories exist with proper write permissions
    echo "[2/5] Verifying storage directories..."
    mkdir -p "$APP_DIR/instance"
    mkdir -p "$APP_DIR/static/uploads/chunks"
    mkdir -p "$APP_DIR/static/hls"
    mkdir -p "$APP_DIR/static/subtitles"
    mkdir -p "$APP_DIR/generated_pdfs"

    # Step 5: Install and update python packages in virtual environment
    echo "[3/5] Installing / updating Python dependencies..."
    $VENV_PIP install -r requirements.txt --quiet

    # Step 6: Run DB migration if available
    echo "[4/5] Checking database status..."
    if [ -f "$APP_DIR/migrate_db.py" ]; then
        $VENV_PYTHON "$APP_DIR/migrate_db.py" || true
    fi

    # Step 7: Restart systemd service and check status
    echo "[5/5] Restarting $SERVICE_NAME..."
    sudo systemctl daemon-reload
    sudo systemctl restart "$SERVICE_NAME"

    echo ""
    echo "Verifying service status..."
    if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
        echo "============================================================"
        echo "  ✅ CampusPlayer is ACTIVE and running successfully!"
        echo "============================================================"
    else
        echo "============================================================"
        echo "  ❌ Failed to start $SERVICE_NAME. Checking logs:"
        echo "============================================================"
        sudo journalctl -xeu "$SERVICE_NAME" -n 20 --no-pager
        exit 1
    fi
}

main "$@"

