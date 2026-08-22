#!/bin/bash
# =============================================================
#  CampusPlayer CP1 — Full Deploy Script
#  Run with: sudo bash /opt/campusplayer/cp1/deploy_cp1.sh
# =============================================================
set -e

APP_DIR="/opt/campusplayer/cp1"
VENV="$APP_DIR/venv"
APP_USER="vasanth-v"
DB_NAME="campusplayer_cp1"
DB_USER="cp1user"
DB_PASS="Cp1Secure@2026"

echo "============================================"
echo " CampusPlayer CP1 — Deploy Script"
echo "============================================"

# ---- Step 1: Create PostgreSQL user & database ----
echo ""
echo "[1/6] Setting up PostgreSQL..."

# Create user if not exists
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
    echo "  -> User '$DB_USER' already exists. Updating password..."
    sudo -u postgres psql -c "ALTER USER $DB_USER WITH PASSWORD '$DB_PASS';"
else
    echo "  -> Creating user '$DB_USER'..."
    sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';"
fi

# Create database if not exists
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    echo "  -> Database '$DB_NAME' already exists."
else
    echo "  -> Creating database '$DB_NAME'..."
    sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;"
fi

# Grant privileges
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"
sudo -u postgres psql -d "$DB_NAME" -c "GRANT ALL ON SCHEMA public TO $DB_USER;"
echo "  -> PostgreSQL setup complete."

# ---- Step 2: Install Python dependencies ----
echo ""
echo "[2/6] Installing Python dependencies..."
$VENV/bin/pip install --upgrade pip -q
$VENV/bin/pip install -r $APP_DIR/requirements.txt -q
echo "  -> Dependencies installed."

# ---- Step 3: Set ownership ----
echo ""
echo "[3/6] Setting file ownership..."
chown -R $APP_USER:$APP_USER $APP_DIR
chmod 600 $APP_DIR/.env
echo "  -> Ownership set to $APP_USER."

# ---- Step 4: Initialize / migrate database ----
echo ""
echo "[4/6] Initialising database schema..."
cd $APP_DIR
sudo -u $APP_USER $VENV/bin/python migrate_db.py || {
    echo "  -> migrate_db.py failed or not found, trying db.create_all()..."
    sudo -u $APP_USER $VENV/bin/python -c "
from app import app, db
with app.app_context():
    db.create_all()
    print('  -> db.create_all() completed.')
"
}
echo "  -> Database ready."

# ---- Step 5: Stop & disable old services ----
echo ""
echo "[5/6] Stopping existing services..."
systemctl stop campusplayer.service campusplayer-worker.service campusplayer-beat.service 2>/dev/null || true
systemctl disable campusplayer.service campusplayer-worker.service campusplayer-beat.service 2>/dev/null || true
echo "  -> Old services stopped and disabled."

# ---- Step 6: Write new systemd service files ----
echo ""
echo "[6/6] Writing systemd service files..."

# Main web service
cat > /etc/systemd/system/campusplayer.service << 'EOF'
[Unit]
Description=Campus Player Web Application (cp1)
After=network.target postgresql.service redis.service
Wants=postgresql.service redis.service

[Service]
User=vasanth-v
Group=vasanth-v
WorkingDirectory=/opt/campusplayer/cp1
EnvironmentFile=/opt/campusplayer/cp1/.env
ExecStart=/opt/campusplayer/cp1/venv/bin/gunicorn --workers 4 --threads 4 --timeout 3600 --bind 0.0.0.0:5000 app:app
Restart=always
RestartSec=5
Environment=PATH=/opt/campusplayer/cp1/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

[Install]
WantedBy=multi-user.target
EOF

# Celery worker service
cat > /etc/systemd/system/campusplayer-worker.service << 'EOF'
[Unit]
Description=CampusPlayer Celery Background Worker Service (cp1)
After=network.target postgresql.service redis.service
Wants=postgresql.service redis.service

[Service]
Type=simple
User=vasanth-v
Group=vasanth-v
WorkingDirectory=/opt/campusplayer/cp1
EnvironmentFile=/opt/campusplayer/cp1/.env
ExecStart=/opt/campusplayer/cp1/venv/bin/celery -A celery_tasks.celery worker --loglevel=info --concurrency=4
KillMode=mixed
TimeoutStopSec=60
Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF

# Celery beat service
cat > /etc/systemd/system/campusplayer-beat.service << 'EOF'
[Unit]
Description=CampusPlayer Celery Beat Scheduler Service (cp1)
After=network.target postgresql.service redis.service
Wants=postgresql.service redis.service

[Service]
Type=simple
User=vasanth-v
Group=vasanth-v
WorkingDirectory=/opt/campusplayer/cp1
EnvironmentFile=/opt/campusplayer/cp1/.env
ExecStart=/opt/campusplayer/cp1/venv/bin/celery -A celery_config.celery beat --loglevel=info --schedule=/tmp/celerybeat-schedule-cp1
KillMode=mixed
TimeoutStopSec=30
Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF

echo "  -> Service files written."

# ---- Reload & start ----
echo ""
echo "Reloading systemd and starting services..."
systemctl daemon-reload
systemctl enable campusplayer.service campusplayer-worker.service campusplayer-beat.service
systemctl start campusplayer.service campusplayer-worker.service campusplayer-beat.service

echo ""
echo "============================================"
echo " Waiting 5s for services to stabilise..."
sleep 5

echo ""
echo " Service Status:"
systemctl is-active campusplayer.service       && echo "  ✅ campusplayer.service        → RUNNING" || echo "  ❌ campusplayer.service        → FAILED"
systemctl is-active campusplayer-worker.service && echo "  ✅ campusplayer-worker.service → RUNNING" || echo "  ❌ campusplayer-worker.service → FAILED"
systemctl is-active campusplayer-beat.service   && echo "  ✅ campusplayer-beat.service   → RUNNING" || echo "  ❌ campusplayer-beat.service   → FAILED"

echo ""
echo " Port check:"
ss -tlnp | grep 5000 && echo "  ✅ Port 5000 is listening" || echo "  ❌ Port 5000 NOT listening"

echo ""
echo " Cloudflare tunnel (unchanged):"
echo "  🌐 campusplayer.dpdns.org  →  http://127.0.0.1:5000  (via cloudflared.service)"

echo ""
echo "============================================"
echo " DEPLOY COMPLETE!"
echo " Check admin password in logs if not set:"
echo "   journalctl -u campusplayer.service -n 50"
echo "============================================"
