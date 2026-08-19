# CampusPlayer — Production Emergency Rollback Strategy & Protocol

This document provides explicit, step-by-step instructions for safely rolling back application code and database state in the event of a deployment failure or unexpected issue.

---

## 1. Safety Directives
- **NEVER** drop production databases or delete uploaded media files (`static/uploads/`, `static/hls/`, `generated_pdfs/`).
- **NEVER** perform an unverified database overwrite without inspecting the target backup file first.
- Always stop application workers/service before restoring database files.

---

## 2. Emergency Rollback Workflow

### Step 1: Stop Application Service
Stop all Gunicorn workers or systemd service to ensure no active database writes occur:
```bash
sudo systemctl stop campusplayer.service
```

### Step 2: Identify Most Recent Valid Pre-Deployment Backup
Backups are located in the `backups/` directory:
```bash
ls -la backups/campusplayer_*.db
```

Run an integrity check on the target backup file using Python or SQLite:
```bash
python -c "from services.backup_engine import verify_sqlite_file; ok, err = verify_sqlite_file('backups/campusplayer_YYYY-MM-DD_HHMMSS.db'); print('Valid:', ok, err)"
```

### Step 3: Restore Database File
Safety copy current database state before restoring:
```bash
cp app.db app.db.broken_$(date +%Y%m%d_%H%M%S)
cp backups/campusplayer_YYYY-MM-DD_HHMMSS.db app.db
```

### Step 4: Revert Code to Previous Known Good Commit
Find previous commit hash using Git log:
```bash
git log -n 5 --oneline
```

Checkout target commit hash:
```bash
git checkout <previous_commit_hash>
```

### Step 5: Verify Data Integrity
Run platform audit to confirm database records and relationships are intact:
```bash
python audit_platform.py
```

### Step 6: Restart Application Service
Restart service and verify health endpoint:
```bash
sudo systemctl start campusplayer.service
curl http://127.0.0.1:5000/health
```

---

## 3. Rollback Verification Checklists
- [ ] Database opened cleanly with `audit_platform.py`.
- [ ] Institution count and user count match pre-deployment baseline.
- [ ] Admin login and student login verified.
- [ ] Video playback and HLS files verified.
- [ ] Session persistence functioning without forced logouts.
