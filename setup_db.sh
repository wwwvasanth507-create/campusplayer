#!/bin/bash
# Run as root: bash /opt/campusplayer/cp1/setup_db.sh
# Creates PostgreSQL user and database for campusplayer cp1 instance

DB_USER="cp1user"
DB_NAME="campusplayer_cp1"
DB_PASS="Cp1Secure@2026"

# Check if user exists, create if not
if runuser -l postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'\"" | grep -q 1; then
    echo "Role ${DB_USER} already exists."
else
    runuser -l postgres -c "psql -c \"CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';\""
    echo "Created role ${DB_USER}."
fi

# Check if database exists, create if not
if runuser -l postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'\"" | grep -q 1; then
    echo "Database ${DB_NAME} already exists."
else
    runuser -l postgres -c "psql -c \"CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};\""
    echo "Created database ${DB_NAME}."
fi

# Grant privileges
runuser -l postgres -c "psql -c \"GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};\""
runuser -l postgres -c "psql -d ${DB_NAME} -c \"GRANT ALL ON SCHEMA public TO ${DB_USER};\""

echo "Database setup complete!"
echo "DB_USER: ${DB_USER}"
echo "DB_NAME: ${DB_NAME}"
echo "DATABASE_URL: postgresql://${DB_USER}:${DB_PASS}@localhost/${DB_NAME}"
