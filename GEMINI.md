# Campus Player — Workspace Operational & Architectural Rules

See [.agents/rules/agent_rule.md](.agents/rules/agent_rule.md) and [agent_rule.md](agent_rule.md) for the full detailed reference.

## ⚡ 1. Mandatory Session Protocol
- **Git Pull First**: Every session or task begins with pulling the latest changes:
  ```bash
  git pull origin main
  ```
- If local changes exist, stash and rebase: `git stash && git pull --rebase origin main && git stash pop`

## 🏛️ 2. Core Architecture Rules
- **Multi-Tenancy**: All models, uploads, queries, and records MUST be scoped with `institution_id` unless strictly within the `system_admin` portal.
- **Entry Points**: Primary production application is `app.py` (`python app.py` / `gunicorn app:app`). Modular blueprints exist under `routes/` and `factory.py`.
- **HLS Video Pipeline**: Chunked uploads up to 20 GB handled by `services/upload_engine.py`. Resumable, crash-safe multi-bitrate HLS conversion (144p to 16K) handled by `services/conversion_engine.py` and `services/ultra_parallel_processor.py`.
- **Security & CSRF**: All non-GET requests require valid `csrf_token`. Passwords and sensitive keys are Fernet-encrypted via `crypto_helper.py`.
- **Gamification**: XP points awarded for video watching (1 XP/tick) and quizzes (100 XP). Level formula is `(xp // 500) + 1`.
- **PDF Generation**: Native ReportLab generation in `attendance_utils.py` and `app.py`.

## 🎨 3. Branding & UI
- **Branding**: The platform name must always be displayed as **`Campus Player`** (with a space) across all dashboards, templates, emails, and PDFs.
- **Design System**: Modern Cyber-Glass styling with dark/light theme support and complete mobile-to-desktop responsive layouts.
- **Device-Differentiated UI/UX (PC/Laptops vs Mobile Android/iOS)**:
  - **PC / Laptop / Desktop (min-width: 992px)**: High-productivity wide Cyber-Glass layouts, two-column interactive hero, rich interactive video player preview mockups, multi-column feature matrices, live platform telemetry, and keyboard-friendly (`Tab`/`Enter`) split-screen login cards.
  - **Mobile Android & iOS (< 992px)**: Native mobile app ergonomic design, touch-first large targets (min 48px), iOS notch & gesture bar safe-area insets (`env(safe-area-inset-bottom)`), segmented quick-touch role chips (`🎓 Student`, `👨‍🏫 Teacher`, `🏛️ Admin`, `⚙️ System`), swipeable cards, and sticky bottom action CTA buttons.

## 👥 4. Git & Server Standards
- **Conventional Commits**: Format commit messages as `feat:`, `fix:`, `refactor:`, `docs:`, `chore:`.
- **Clean VCS**: No `.env`, database files, log files, or video `.ts`/`.m3u8` chunks committed to git.
- **Ubuntu Server**: Production service managed via `deploy.sh` and systemd (`sudo systemctl restart campusplayer`).
