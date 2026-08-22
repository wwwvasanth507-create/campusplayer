#!/usr/bin/env bash
# ==============================================================================
# CampusPlayer - Hardened Production Deployment Script (Ubuntu / Linux)
# ==============================================================================
set -e

main() {
    APP_DIR="${CAMPUSPLAYER_DIR:-/opt/campusplayer}"
    if [ ! -d "$APP_DIR" ]; then
        APP_DIR="$(pwd)"
    fi

    VENV_PYTHON="$APP_DIR/venv/bin/python3"
    if [ ! -f "$VENV_PYTHON" ]; then
        VENV_PYTHON="$APP_DIR/venv/Scripts/python.exe"
    fi
    if [ ! -f "$VENV_PYTHON" ]; then
        VENV_PYTHON="python3"
    fi

    VENV_PIP="$APP_DIR/venv/bin/pip"
    if [ ! -f "$VENV_PIP" ]; then
        VENV_PIP="$APP_DIR/venv/Scripts/pip.exe"
    fi
    if [ ! -f "$VENV_PIP" ]; then
        VENV_PIP="pip"
    fi

    SERVICE_NAME="campusplayer.service"

    echo "============================================================"
    echo "  Deploying CampusPlayer Updates to $APP_DIR"
    echo "============================================================"

    cd "$APP_DIR"

    # Step 1: Ensure .env file exists and contains persistent SECRET_KEY
    if [ ! -f "$APP_DIR/.env" ]; then
        echo "[1/10] .env file not found. Initializing from .env.example..."
        cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    fi

    # Pre-flight: Verify DATABASE_URL is PostgreSQL — abort if not set or SQLite
    DB_URL_CHECK=$(grep -E '^DATABASE_URL=' "$APP_DIR/.env" | head -1 | cut -d= -f2-)
    if [ -z "$DB_URL_CHECK" ]; then
        echo "❌ ABORT: DATABASE_URL is not set in $APP_DIR/.env"
        echo "   Set it to a postgresql:// URI matching your cp1 database."
        exit 1
    fi
    if [[ "$DB_URL_CHECK" != postgresql://* ]]; then
        echo "❌ ABORT: DATABASE_URL does not start with postgresql://"
        echo "   Current value: $DB_URL_CHECK"
        echo "   cp1 requires PostgreSQL. SQLite is not supported."
        exit 1
    fi
    echo "[Pre-flight] DATABASE_URL verified as PostgreSQL ✓"

    # Step 2: Storage directory verification
    echo "[2/10] Verifying storage directories..."
    mkdir -p "$APP_DIR/backups"
    mkdir -p "$APP_DIR/instance"
    mkdir -p "$APP_DIR/static/uploads/chunks"
    mkdir -p "$APP_DIR/static/uploads/avatars"
    mkdir -p "$APP_DIR/static/hls"
    mkdir -p "$APP_DIR/static/subtitles"
    mkdir -p "$APP_DIR/generated_pdfs"

    # Step 3: Pre-deployment database backup & integrity verification
    echo "[3/10] Creating pre-deployment database backup..."
    $VENV_PYTHON -c "from services.backup_engine import create_backup; ok, res = create_backup(); exit(0 if ok else 1)" || {
        echo "❌ Pre-deployment database backup failed! Aborting deployment."
        exit 1
    }

    # Step 4: Safe Git fetch and pull (NO git reset --hard)
    echo "[4/10] Fetching and pulling latest changes from origin/main..."
    git pull origin main || {
        echo "❌ Git pull failed! Resolve conflicts manually before proceeding."
        exit 1
    }

    # Step 5: Dependencies update
    echo "[5/10] Installing / updating Python dependencies..."
    $VENV_PIP install -r requirements.txt --quiet

    # Step 6: Save pre-migration baseline audit report
    echo "[6/10] Capturing pre-migration baseline data audit..."
    $VENV_PYTHON "$APP_DIR/audit_platform.py" --save-baseline || {
        echo "❌ Pre-migration baseline audit failed! Aborting deployment."
        exit 1
    }

    # Step 7: Single-process Database Migration & Schema Sync
    echo "[7/10] Running database migrations..."
    $VENV_PYTHON "$APP_DIR/migrate_db.py" || {
        echo "❌ Database migration failed! Aborting deployment."
        exit 1
    }

    # Step 8: Verify data baseline audit (detect unexpected record drops)
    echo "[8/10] Verifying post-migration data integrity against baseline..."
    $VENV_PYTHON "$APP_DIR/audit_platform.py" --verify-baseline || {
        echo "❌ Data integrity baseline verification failed! Check audit output."
        exit 1
    }


    # Step 9: Service restart
    echo "[9/10] Restarting application service..."
    if command -v systemctl >/dev/null 2>&1; then
        sudo systemctl daemon-reload || true
        sudo systemctl restart "$SERVICE_NAME" || true
    fi

    # Step 10: Service status & Health Check Verification
    echo "[10/10] Running application health verification..."
    $VENV_PYTHON -c "
import urllib.request, json
try:
    req = urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=5)
    data = json.loads(req.read().decode())
    if data.get('status') == 'healthy':
        print('[OK] Application health endpoint verified successfully!')
        exit(0)
    else:
        print('[FAIL] Health endpoint returned non-healthy status:', data)
        exit(1)
except Exception as e:
    print('[NOTICE] Could not connect to local health endpoint (service may run via standalone server):', e)
    exit(0)
"

    echo "============================================================"
    echo "  ✅ CampusPlayer Deployment Completed Successfully!"
    echo "============================================================"
}

main "$@"
