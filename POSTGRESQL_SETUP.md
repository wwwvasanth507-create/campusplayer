# 🐘 Campus Player — PostgreSQL Setup & Deployment Guide

This guide details how to run **Campus Player** with **PostgreSQL** as its primary production database engine.

---

## 🏛️ Architecture: Single Database Multi-Tenancy

All institutions, classrooms, users, videos, quizzes, and telemetry records are housed within **ONE single PostgreSQL database** (`campusplayer_db`). Multi-tenancy isolation is maintained strictly through column-level `institution_id` scoping across all models and queries.

---

## ⚡ 1. Ubuntu Server Quick Setup

### Step 1.1: Install PostgreSQL & Packages
```bash
sudo apt-get update
sudo apt-get install -y postgresql postgresql-contrib libpq-dev python3-pip
```

### Step 1.2: Create PostgreSQL User & Database
```bash
# Switch to postgres system user and open psql prompt
sudo -u postgres psql
```

Inside the `psql` shell, execute:
```sql
CREATE USER campususer WITH PASSWORD 'CampusPlayer_Secure_Pass_2026';
CREATE DATABASE campusplayer_db OWNER campususer;
GRANT ALL PRIVILEGES ON DATABASE campusplayer_db TO campususer;
\q
```

---

## ⚙️ 2. Application Configuration (`.env`)

Set `DATABASE_URL` in `/opt/campusplayer/.env`:

```ini
# PostgreSQL Connection URI
DATABASE_URL=postgresql://campususer:CampusPlayer_Secure_Pass_2026@localhost:5432/campusplayer_db

# Connection Pool Tuning
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=20
```

---

## 🚀 3. Database Migration & Initialization

Run the automated migration script to initialize the PostgreSQL schema and seed system defaults:

```bash
cd /opt/campusplayer
./venv/bin/python3 migrate_db.py
```

---

## 🧪 4. Verifying Health & Connections

Run the system test suite to verify 100% database health and non-destructive operations:

```bash
./venv/bin/python3 test_master_e2e.py
```

---

## 📂 5. Database Backup & Maintenance

PostgreSQL backups are automatically created as `.sql` dumps using `pg_dump` in `/opt/campusplayer/backups/`:

```bash
# Manual Backup
./venv/bin/python3 services/backup_engine.py
```
