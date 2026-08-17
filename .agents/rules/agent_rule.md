# Campus Player — Permanent Agent Rules & Project Memory (agent_rule.md)

> **Mandatory Guidelines for AI Agent Workflows, Multi-Developer Collaboration, Code Quality, Multi-Tenancy Architecture, Ubuntu Server Operations, and Brand Consistency.**

---

## ⚡ 1. Golden Rule: "Git Pull First" on Every Session
To ensure seamless multi-developer collaboration without code drift or overwriting team commits:
1. **Always pull latest changes before starting any task:**
   ```bash
   git pull origin main
   ```
2. **If uncommitted local changes exist:**
   ```bash
   git stash
   git pull --rebase origin main
   git stash pop
   ```
3. **Always verify clean git status before editing files:**
   ```bash
   git status
   ```

---

## 🏛️ 2. Core Project Architecture & Conventions

### A. Dual Architecture Pattern
- **Production Monolith**: `app.py` is the primary entry point executed via `python app.py` and `gunicorn app:app` in Docker/Systemd.
- **Factory / Blueprint Pattern**: `factory.py`, `wsgi.py`, and `routes/` provide modular blueprint support for scalable development.

### B. Multi-Tenancy & Data Isolation
- **Scope by Institution**: Every `User`, `Classroom`, `Video`, `Assignment`, `Quiz`, `Attendance` record, and `SiteSettings` belongs to an `institution_id`.
- **System Admin vs. Institution Admin**:
  - `system_admin`: Global superuser managing institutions, server storage, and retention permissions across all tenants.
  - `admin`: Tenant-specific administrator managing teachers, students, classrooms, and school settings for their own institution.
- **Tenant Storage Roots**: Stored under isolated directories `static/uploads/institutions/<slug>/` or standard multi-tenant upload paths.

### C. Video Processing & Streaming Engine
- **Chunked Uploads**: High-throughput upload engine in `services/upload_engine.py` supports chunked resumable uploads up to 20 GB.
- **HLS Transcoding Ladder**: Adaptive video streaming ladder (`144p`, `240p`, `360p`, `480p`, `720p`, `1080p`, `2K`, `4K`, `8K`, `16K`) managed by `services/conversion_engine.py` and `services/ultra_parallel_processor.py`.
- **Crash Safety & Resumption**: All conversion progress is tracked segment-by-segment in `ConversionJob` database models to safely recover from restarts.
- **Auto-Cleanup & Retention**: Background video retention policies automatically clean expired video assets using `services/video_cleanup.py`.

### D. Gamification & Progression
- **XP Engine**: Students earn XP through video watch ticks (1 XP/tick) and passing quizzes (100 XP). Teachers earn XP for publishing video lectures (50 XP).
- **Levels Calculation**: Level formula is `(xp // 500) + 1`. Levels reports exportable as printable PDFs via ReportLab.

### E. Attendance & Academic Reporting
- **Attendance Model**: Multi-session / sub-session attendance with Present, Late, and Absent statuses.
- **Alert Dispatch**: Automatic SMS and parent email alerts for consecutive absences or high late counts.
- **PDF Generation**: Direct ReportLab programmatic generation (A4, custom styling, tables, charts) in `attendance_utils.py` and `app.py`.

### F. Security, Encryption & RBAC
- **Credential Encryption**: Sensitive credentials (e.g. teacher Gmail App Passwords) must be encrypted/decrypted via `crypto_helper.py` using Fernet AES-128-CBC.
- **CSRF & Session Security**: All POST/PUT/DELETE forms and AJAX endpoints must pass `csrf_token`. Session cookies must be `HttpOnly` and `SameSite=Lax`.
- **RBAC Decorators**: Protect endpoints using `@login_required`, `@admin_required`, `@teacher_required`, and `@system_admin_required`.

### G. Database & Zero-Loss Schema Migration Standards
- **Strictly Non-Destructive**: Never drop tables or columns in production. Always design schema modifications additively (`ALTER TABLE ... ADD COLUMN ...`).
- **Synchronized `migrate_db.py` Registration**: Every new column or index added in `models.py` MUST be mirrored in `migrate_db.py` with safe defaults or nullable types.
- **Dynamic Schema Inspection**: Migrations dynamically query table columns using SQLAlchemy inspector before issuing `ALTER TABLE` to guarantee complete idempotency.
- **`CREATE INDEX IF NOT EXISTS`**: All performance indexes across foreign keys, status flags, timestamps, and tenant IDs must be created safely without collisions.
- **Idempotent Multi-Tenant Backfill**: Transparently assigns legacy data to the default institution (`slug='default'`) so existing servers upgrade seamlessly without 500 errors.

---

## 🎨 3. UI/UX & Brand Aesthetics Rules

1. **Brand Name**: Always format the platform brand as **`Campus Player`** (with a space) in all user-facing dashboards, page titles, navigation bars, email templates, PDF headers, and marketing materials.
2. **Design Language**: Follow the **Cyber-Glass Modern Design System** with dark/light mode toggle, vibrant gradients (`#6366f1` Indigo, `#00e5ff` Cyan, `#f59e0b` Amber), glassmorphism borders, and smooth micro-animations.
3. **Responsiveness & Device Differentiation**: All templates must render flawlessly across 320px mobile screens, tablets, laptops, and ultra-wide desktop monitors using `responsive.css`.
4. **Device-Differentiated UI Architecture**:
   - **PC / Laptop / Desktop (min-width: 992px)**:
     - Wide two-column interactive hero layouts with live interactive Cyber-Glass player preview simulations.
     - 4-column feature matrix, telemetry counters, and horizontal navigation.
     - Split-screen / expansive login card with keyboard accessibility (`Tab`, `Enter`) and password reveal.
   - **Mobile Android & iOS (< 992px)**:
     - Native mobile app ergonomic design with thumb-reach action zones.
     - Big touch targets (min 48px), swipeable feature cards, and safe-area insets (`env(safe-area-inset-bottom)`).
     - Segmented quick-touch role selector chips (`🎓 Student`, `👨‍🏫 Teacher`, `🏛️ Admin`, `⚙️ System`).
     - Sticky bottom-bar action CTAs and touch feedback animations.

---

## 👥 4. Git Hygiene & Commit Standards

1. **Clean Commits**: Follow Conventional Commits format:
   - `feat(...)`: New features
   - `fix(...)`: Bug fixes
   - `refactor(...)`: Code refactoring without behavioral change
   - `docs(...)`: Documentation and rule updates
   - `chore(...)`: Maintenance, dependency updates
2. **No Junk in Version Control**:
   - Never commit `.env`, `.log` files, `__pycache__`, `*.sqlite`, `*.db`, `*.ts`, `*.m3u8`, or raw video chunks in `static/uploads/`.
   - Keep dynamic runtime directories populated only with `.gitkeep`.

---

## 🖥️ 5. Ubuntu Server Production Operations

### Automated Deployment
```bash
cd /opt/campusplayer
chmod +x deploy.sh
./deploy.sh
```

### Systemd Service Management
- **Web App**: `sudo systemctl restart campusplayer` | `sudo journalctl -u campusplayer -f -n 50`
- **Celery Worker**: `sudo systemctl restart campusplayer-worker` | `sudo journalctl -u campusplayer-worker -f -n 50`
- **Celery Beat**: `sudo systemctl restart campusplayer-beat` | `sudo journalctl -u campusplayer-beat -f -n 50`
- **Nginx**: `sudo nginx -t` && `sudo systemctl reload nginx`
- **Redis**: `sudo systemctl status redis`

---

## ✅ 6. Automated Testing & Verification Protocols

Before finalizing any significant changes or pull requests, always execute the automated test suites:
```bash
# 1. Flask Web Routes & Conversion Integration
python test_flask_routes.py

# 2. Parallel HLS Conversion, Segment Resumption & Recovery
python test_conversion_system.py

# 3. Video Deletion & Retention Tests
python test_video_deletion.py
```
