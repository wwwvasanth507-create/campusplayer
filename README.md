# Campus Player — Advanced Educational Video Streaming & Campus Management Platform

**Campus Player** is an enterprise-grade, multi-tenant Flask application for educational video streaming, real-time classroom interaction, gamified learning, and campus management. It features role-based access for Students, Teachers, Institution Admins, and System Administrators, adaptive HLS video streaming, automated PDF reporting, real-time Socket.IO chat, and an integrated AI assistant.

© 2026 Vasanth V. — All Rights Reserved. Proprietary software; see [`LICENSE.md`](LICENSE.md), [`COPYRIGHT.md`](COPYRIGHT.md), [`TERMS.md`](TERMS.md), [`PRIVACY.md`](PRIVACY.md), and [`NOTICE.md`](NOTICE.md).

---

## 🌟 Key Features

- **Profile Identity & Photo Security System**:
  - Universal privacy protection preventing raw handle/username leaks across public UI touchpoints.
  - Automatically displays user profile photos (`avatar_url`) and updated profile display names (`user.name`).
  - Fallback renders avatar badges with updated profile display initials (`name[0].upper()`).

- **Multi-Tenant Institution Isolation**:
  - Full data segregation via `institution_id` across models, queries, uploads, and reports.
  - System Admin master portal for global institution provisioning, domain routing, and feature configuration.

- **Adaptive HLS Video Pipeline**:
  - Resumable chunked video uploads (up to 20 GB).
  - Crash-safe, multi-bitrate HLS conversion (144p to 16K) powered by FFmpeg.
  - Sprite sheet hover seek previews, auto-generated thumbnails, and WebVTT subtitle support.

- **Classrooms, Quizzes & Homework Assignments**:
  - Interactive classroom management with assignment submissions, grading, and automated PDF export.
  - Quiz creation, attempt limits, passing threshold verification, and XP integration.

- **Gamification Engine**:
  - XP rewards for video watching (1 XP/tick) and quiz completion (100 XP).
  - Dynamic level progression formula `(xp // 500) + 1`, login streaks, and daily quests.

- **Real-Time Interactive Chat Hub**:
  - Classroom live chat powered by Flask-SocketIO with profile photo and display name rendering.

- **Attendance & PDF Reporting**:
  - Daily attendance tracking, sub-session time bounds, attendance lock enforcement, and ReportLab PDF exports.

- **AI Assistant & Study Kit**:
  - Integrated Gemini AI helper for automated video summarization, doubt clearing, and study kit generation.

---

## 📁 Project Architecture

- **`app.py`**: Primary Flask application entry point (`python app.py` / `gunicorn app:app`). Contains core routes, auth, sockets, analytics, and admin handlers.
- **`models.py`**: SQLAlchemy ORM models (`User`, `Institution`, `Video`, `Classroom`, `Quiz`, `ChatMessage`, `AttendanceRecord`, `DailyQuest`).
- **`services/`**: Modular engine services:
  - `upload_engine.py`: Resumable chunked upload processor.
  - `conversion_engine.py` & `ultra_parallel_processor.py`: Multi-bitrate HLS transcoding pipeline.
  - `report_engine.py`: PDF and weekly report compilation engine.
  - `certificate_engine.py`: ReportLab PDF certificate generation.
  - `email.py`: Email delivery and notification service.
  - `crypto_helper.py`: Fernet AES encryption for sensitive data.
- **`attendance_utils.py`**: Attendance calculations and native ReportLab PDF export.
- **`migrate_db.py`**: Zero-loss database schema migration and multi-tenant backfill.
- **`templates/` & `static/`**: Cyber-Glass UI Jinja2 templates and assets.
- **`test_*.py`**: Comprehensive 14-module automated test suite.

---

## 🚀 Quick Start (Local Development)

1. **Clone & Setup Virtual Environment**:
   ```bash
   git clone https://github.com/wwwvasanth507-create/campusplayer.git
   cd campusplayer
   python -m venv venv
   # On Windows PowerShell:
   .\venv\Scripts\Activate.ps1
   # On Linux/macOS:
   source venv/bin/activate
   ```

2. **Install Dependencies & FFmpeg**:
   ```bash
   pip install -r requirements.txt
   ```
   *(Ensure system binary `ffmpeg` is installed and added to PATH for video conversion).*

3. **Configure Environment Variables**:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` to set `SECRET_KEY`, `ENCRYPTION_KEY`, and optional `GEMINI_API_KEY`.

4. **Sync Database & Migration**:
   ```bash
   python migrate_db.py
   ```

5. **Run Application**:
   ```bash
   python app.py
   ```
   Open `http://127.0.0.1:5000` in your browser. Default Admin credentials: `admin` / `admin123`.

---

## 🛡️ Production Deployment (Docker)

```bash
cp .env.example .env
docker compose up -d --build
```
This provisions Flask (`gunicorn` with `eventlet`), Celery workers, Redis, PostgreSQL, and Nginx.

---

## 🧪 Running Automated Test Suite

Run the full project automated test suite (51/51 tests passing):

```bash
# Discover and run all unittest files:
python -m unittest discover -s . -p "test_*.py"

# Or run individual test modules:
python -m unittest test_profile_display_name_security.py test_full.py test_master_e2e.py
```

---

## 📄 License & Terms

Campus Player is proprietary software. See [`LICENSE.md`](LICENSE.md), [`COPYRIGHT.md`](COPYRIGHT.md), and [`TERMS.md`](TERMS.md) for licensing terms.
