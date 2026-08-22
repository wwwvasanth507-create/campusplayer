#!/bin/bash
# Run as root: bash /opt/campusplayer/cp1/setup_db.sh
# Creates PostgreSQL user and database for campusplayer cp1 instance.
# Credentials are read from .env — never hardcoded in this script.

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$APP_DIR/.env"

# ---- Ensure .env exists ----
if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$APP_DIR/.env.example" ]; then
        echo "  -> .env not found. Creating from .env.example..."
        cp "$APP_DIR/.env.example" "$ENV_FILE"
        # Generate a secure random password on first run
        GENERATED_PASS="$(openssl rand -hex 16)"
        sed -i "s|DATABASE_URL=postgresql://cp1user:CHANGE_ME@localhost:5432/campusplayer_cp1|DATABASE_URL=postgresql://cp1user:${GENERATED_PASS}@localhost:5432/campusplayer_cp1|" "$ENV_FILE"
        echo "  -> Generated secure DB password. Saved to .env."
    else
        echo "❌ Neither .env nor .env.example found in $APP_DIR"
        exit 1
    fi
fi

# ---- Parse credentials from DATABASE_URL ----
DB_URL=$(grep -E '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)

if [ -z "$DB_URL" ]; then
    echo "❌ DATABASE_URL not found in $ENV_FILE"
    exit 1
fi

if [[ "$DB_URL" != postgresql://* ]]; then
    echo "❌ DATABASE_URL is not a postgresql:// URI: $DB_URL"
    echo "   cp1 requires PostgreSQL. Update .env and retry."
    exit 1
fi

DB_USER=$(echo "$DB_URL" | sed -E 's|postgresql://([^:]+):.*|\1|')
DB_PASS=$(echo "$DB_URL" | sed -E 's|postgresql://[^:]+:([^@]+)@.*|\1|')
DB_NAME=$(echo "$DB_URL" | sed -E 's|.*/([^?]+).*|\1|')

echo "Setting up PostgreSQL for cp1..."
echo "DB_USER: $DB_USER"
echo "DB_NAME: $DB_NAME"

# ---- Create user if not exists ----
if runuser -l postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'\"" | grep -q 1; then
    echo "Role ${DB_USER} already exists. Updating password..."
    runuser -l postgres -c "psql -c \"ALTER USER ${DB_USER} WITH PASSWORD '${DB_PASS}';\""
else
    runuser -l postgres -c "psql -c \"CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';\""
    echo "Created role ${DB_USER}."
fi

# ---- Create database if not exists ----
if runuser -l postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'\"" | grep -q 1; then
    echo "Database ${DB_NAME} already exists."
else
    runuser -l postgres -c "psql -c \"CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};\""
    echo "Created database ${DB_NAME}."
fi

# ---- Grant privileges ----
runuser -l postgres -c "psql -c \"GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};\""
runuser -l postgres -c "psql -d ${DB_NAME} -c \"GRANT ALL ON SCHEMA public TO ${DB_USER};\""

echo ""
echo "Database setup complete!"
echo "DB_USER: ${DB_USER}"
echo "DB_NAME: ${DB_NAME}"
echo "DATABASE_URL: ${DB_URL}"
