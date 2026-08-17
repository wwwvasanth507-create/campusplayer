# CampusPlayer - Developer Rules & Operational Guide (rule.md)

> **Mandatory Guidelines for Multi-Developer Collaboration, Git Hygiene, Ubuntu Server Deployment, and Agent Workflows.**

---

## ⚡ 1. Golden Rule: "Git Pull First" on Every Chat / Session

To ensure multiple developers (e.g. Vasanth, Sanjay, and AI agents) work simultaneously without code drift or overwriting each other's work:

1. **Every chat, task, or coding session MUST begin with pulling the latest code from GitHub:**
   ```bash
   git pull origin main
   ```
2. **If you have local uncommitted changes before pulling:**
   ```bash
   git stash
   git pull --rebase origin main
   git stash pop
   ```
3. **Never start editing files on an outdated branch or commit.** Always verify upstream status with:
   ```bash
   git status
   ```

---

## 👥 2. Multi-Developer Git Workflow & Commands

### A. Initial One-Time Repository Setup
Ensure the local branch is properly tracking remote `origin/main`:
```bash
git branch --set-upstream-to=origin/main main
```

### B. Standard Feature Branch Workflow (Recommended for Teams)
When adding new features or fixing bugs:

1. **Pull the latest `main` branch:**
   ```bash
   git checkout main
   git pull origin main
   ```
2. **Create and switch to your feature branch:**
   ```bash
   git checkout -b feature/your-feature-name
   # e.g., git checkout -b feature/video-player-enhancement
   # e.g., git checkout -b fix/attendance-sms-alert
   ```
3. **Check status of modified files:**
   ```bash
   git status
   ```
4. **Stage modified files (never add unwanted files):**
   ```bash
   git add path/to/file.py templates/view.html
   # Or stage all tracked updates:
   git add -u
   ```
5. **Commit with a descriptive message following Conventional Commits:**
   ```bash
   git commit -m "feat(video): add adaptive bitrate selection"
   # Types: feat, fix, docs, style, refactor, perf, test, chore
   ```
6. **Sync with latest `main` before pushing (avoids merge conflicts):**
   ```bash
   git fetch origin main
   git merge origin/main
   # (Resolve any conflicts if indicated)
   ```
7. **Push feature branch to GitHub:**
   ```bash
   git push -u origin feature/your-feature-name
   ```

### C. Direct Main Branch Workflow (Fast Solo / Small Team Updates)
When working directly on `main`:
```bash
# 1. Pull latest upstream changes first
git pull --rebase origin main

# 2. Check changes
git status

# 3. Stage changes
git add <files>

# 4. Commit
git commit -m "feat: description of work"

# 5. Push to GitHub
git push origin main
```

### D. Resolving Merge Conflicts
If Git reports a merge conflict:
```bash
# 1. Identify conflicted files:
git status

# 2. Open conflicted files, find markers (<<<<<<<, =======, >>>>>>>) and keep the desired code.
# 3. Mark conflict as resolved by staging:
git add <resolved_file>

# 4. Complete merge or rebase:
git commit -m "merge: resolve conflict with origin/main"
# (or 'git rebase --continue' if rebasing)

# 5. Push resolved changes:
git push origin main
```

---

## 🛡️ 3. Git Ignore & Repository Cleanliness

### Rules for Repository Hygiene:
- **Zero Unwanted Files:** Never commit databases, logs, local virtual environments, video uploads, or temporary files.
- **Sensitive Credentials:** Never commit `.env` or files containing secrets. Use `.env.example` as a template with placeholder values.
- **Dynamic Folders:** Runtime directories must only contain `.gitkeep` in git.

### Files & Patterns Automatically Ignored by `.gitignore`:
| Category | Ignored Patterns |
| :--- | :--- |
| **Credentials** | `.env`, `.env.local`, `.env.*.local`, `.env.production` |
| **Databases** | `*.db`, `*.db-shm`, `*.db-wal`, `*.sqlite`, `*.sqlite3`, `instance/*` (except `instance/.gitkeep`) |
| **Logs** | `*.log`, `campusplayer.log`, `*.pid`, `celerybeat-schedule*` |
| **Python Cache** | `__pycache__/`, `*.py[cod]`, `*$py.class`, `.Python`, `build/`, `dist/` |
| **Virtual Envs** | `.venv/`, `venv/`, `env/`, `ENV/` |
| **Uploads & Media** | `static/uploads/*` (except `.gitkeep`), `static/hls/*` (except `.gitkeep`), `static/subtitles/*`, `generated_pdfs/*` (except `.gitkeep`), `*.m3u8`, `*.ts` |
| **OS / IDE Cache** | `.vscode/`, `.idea/`, `.DS_Store`, `Thumbs.db`, `*.swp`, `*.bak`, `*.tmp`, `*.temp`, `*.orig` |

### Untracking Accidentally Tracked Files:
If an unwanted file was previously committed to git, remove it from git tracking without deleting it from local disk:
```bash
git rm --cached path/to/unwanted_file
git commit -m "chore: remove unwanted file from git tracking"
git push origin main
```

---

## 🖥️ 4. Ubuntu Server Commands & Operations

### A. Quick One-Line Automated Deployment
On your Ubuntu production server:
```bash
cd /opt/campusplayer
chmod +x deploy.sh
./deploy.sh
```

### B. Manual Step-by-Step Server Deployment
```bash
# 1. Navigate to application folder
cd /opt/campusplayer

# 2. Fetch and align to latest GitHub main
git fetch origin main
git reset --hard origin/main

# 3. Ensure required directories exist with permissions
mkdir -p instance static/uploads/chunks static/hls static/subtitles generated_pdfs

# 4. Update dependencies inside virtual environment
./venv/bin/pip install -r requirements.txt --quiet

# 5. Run database migrations if any
./venv/bin/python3 migrate_db.py

# 6. Restart application service
sudo systemctl daemon-reload
sudo systemctl restart campusplayer
sudo systemctl restart campusplayer-worker   # Celery worker (if running)
sudo systemctl restart campusplayer-beat     # Celery beat (if running)
```

### C. Systemd Service Management Commands
| Action | Command |
| :--- | :--- |
| **Check Web Status** | `sudo systemctl status campusplayer` |
| **Restart Web Service** | `sudo systemctl restart campusplayer` |
| **Start Web Service** | `sudo systemctl start campusplayer` |
| **Stop Web Service** | `sudo systemctl stop campusplayer` |
| **View Live Web Logs** | `sudo journalctl -u campusplayer -f -n 50` |
| **Restart Celery Worker** | `sudo systemctl restart campusplayer-worker` |
| **View Worker Logs** | `sudo journalctl -u campusplayer-worker -f -n 50` |
| **Restart Redis** | `sudo systemctl restart redis` |
| **Check Redis Status** | `sudo systemctl status redis` |

### D. Nginx Web Server Commands
```bash
# Test Nginx configuration syntax
sudo nginx -t

# Reload Nginx without downtime
sudo systemctl reload nginx

# Restart Nginx
sudo systemctl restart nginx

# View Nginx error logs
sudo tail -f /var/log/nginx/error.log

# View Nginx access logs
sudo tail -f /var/log/nginx/access.log
```

### E. Server Maintenance & Admin Commands
```bash
# Reset Admin credentials on server:
./venv/bin/python3 reset_admin.py

# Reset System Admin credentials:
./venv/bin/python3 reset_systemadmin.py

# Verify Admin user existence:
./venv/bin/python3 check_admin.py

# List all Admins in DB:
./venv/bin/python3 list_admins.py
```

---

## 🔧 5. Fixing GitHub "Cannot retrieve latest commit at this time."

When GitHub displays `"Cannot retrieve latest commit at this time."` on the web interface, it is caused by GitHub's tree-cache RPC lag following a commit touching numerous files or branch ref desync.

### Resolution Steps:
1. **Ensure branch upstream is set:**
   ```bash
   git branch --set-upstream-to=origin/main main
   ```
2. **Verify repository object integrity:**
   ```bash
   git fsck --full
   ```
3. **Trigger GitHub cache invalidation by pushing a clean commit:**
   ```bash
   git add rule.md .gitignore
   git commit -m "docs: add rule.md and update gitignore for team collaboration"
   git push origin main
   ```
   *Pushing this fresh commit clears GitHub's stale cache and forces GitHub to re-render HEAD immediately.*
4. **Ensure Default Branch on GitHub Settings** is set to `main` (`Settings > Branches > Default branch`).

---

## 🏗️ 6. CampusPlayer Architecture & Code Standards (Preserved)

1. **Multi-Tenancy**: All queries, uploads, and data models MUST filter by `institution_id` unless operating strictly within the System Admin dashboard.
2. **Video Processing**: Chunked upload supports up to 20 GB (`MAX_VIDEO_SIZE_MB=20480`). Transcoding uses FFmpeg HLS segmented streams (`144p`, `240p`, `360p`, `480p`, `720p`, `1080p`).
3. **Security & CSRF**: All POST, PUT, DELETE endpoints MUST include CSRF validation (`csrf_token`). Session cookies must have `HttpOnly`, `SameSite=Lax`, and `Secure` (in HTTPS).
4. **Responsive UI & Device Differentiation (PC/Laptops vs Mobile Android/iOS)**:
   - **PC / Laptop / Desktop (min-width: 992px)**: Wide-screen Cyber-Glass design, two-column interactive hero, animated 3D video player preview mockups, 4-column feature matrix, telemetry counters, and keyboard-optimized split-pane login cards.
   - **Mobile Android & iOS (< 992px)**: Native mobile app ergonomics, large touch targets (min 48px), iOS safe-area insets (`env(safe-area-inset-bottom)`), segmented quick-touch role selector chips (`🎓 Student`, `👨‍🏫 Teacher`, `🏛️ Admin`, `⚙️ System`), swipeable feature cards, and sticky bottom action CTAs.

---

© 2026 Vasanth V. — CampusPlayer. All Rights Reserved.
