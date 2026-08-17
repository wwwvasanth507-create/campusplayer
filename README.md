# CampusPlayer

CampusPlayer is a Flask-based educational video streaming and campus
management platform: role-based dashboards for administrators, teachers,
and students, HLS video streaming with adaptive quality, classroom
workflows, quizzes, assignments, attendance tracking, analytics, and an
optional AI assistant.

© 2026 Vasanth V. — All Rights Reserved. This is proprietary software;
see [`LICENSE.md`](LICENSE.md), [`COPYRIGHT.md`](COPYRIGHT.md),
[`TERMS.md`](TERMS.md), [`PRIVACY.md`](PRIVACY.md), and
[`NOTICE.md`](NOTICE.md) before deploying or distributing it.

## Key Features

- **Role-based access** — Admin, Teacher, and Student dashboards with
  distinct permissions
- **Adaptive HLS video streaming** — chunked uploads, multi-quality
  transcoding (144p–1080p+), thumbnails, and subtitles
- **Classrooms, quizzes & assignments** — full teacher/student workflow
- **Attendance tracking** — session reports, PDF exports, SMS/email
  alerts to parents
- **Analytics & leaderboards** — student progress and engagement
  reporting
- **Chatrooms** — real-time messaging via Flask-SocketIO
- **Optional AI assistant** — powered by the Gemini API

## Project Structure

- `app.py` — main Flask application (routes, auth, uploads, chat,
  analytics, admin) — the entry point used by `python app.py` and by
  the Dockerfile (`gunicorn app:app`)
- `factory.py` / `wsgi.py` — an alternative application-factory
  structure (`create_app()`) with blueprints under `routes/`; kept for
  modular development, not the default production entry point
- `models.py` — SQLAlchemy ORM models
- `routes/` — blueprint route modules (auth, core, search, video) used
  by the factory pattern
- `services/` — shared utilities: security helpers, CSRF/session
  helpers, upload engine, email
- `extensions.py` — Flask extension initialization
- `celery_config.py` / `celery_tasks.py` — background task queue
  configuration
- `crypto_helper.py` — Fernet-based encryption helpers for sensitive
  stored values
- `templates/`, `static/` — Jinja2 templates and frontend assets
- `marketing/` — standalone presentation/advertisement HTML pages
- `requirements.txt` — Python dependencies
- `Dockerfile`, `docker-compose.yml`, `nginx.conf` — containerized
  deployment
- `test_app.py`, `test_full.py`, `test_extra.py`, `test_selenium.py` —
  automated tests

## Getting Started (Local Development)

1. **Install Python dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
2. **Install FFmpeg** (required as a system binary for video
   transcoding) and make sure it's on your `PATH`.
3. **Configure environment variables:**
   ```bash
   cp .env.example .env
   ```
   Then edit `.env` and set at minimum `SECRET_KEY` (generate one with
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
   Keep `.env` private — don't share it or upload it anywhere public.
4. **Run the app:**
   ```bash
   python app.py
   ```
5. **Open** `http://127.0.0.1:5000` in your browser.

On first run, if no admin user exists, an admin account is created
automatically. If `ADMIN_PASSWORD` is not set in `.env`, a random
password is generated and printed once to the server log — save it, or
set `ADMIN_PASSWORD` explicitly for a predictable credential.

## Production Deployment (Docker)

```bash
cp .env.example .env   # fill in real values
docker compose up -d --build
```

This starts the Flask app (via `gunicorn` with the `eventlet` worker
class for SocketIO support), a Celery worker and beat scheduler, Redis,
PostgreSQL, and an nginx reverse proxy. Review `docker-compose.yml` and
`nginx.conf` and adjust ports/volumes for your environment before
exposing it publicly.

Before going to production:
- Set a strong, unique `SECRET_KEY` and `ENCRYPTION_KEY`
- Set `FORCE_HTTPS=True` and `SESSION_COOKIE_SECURE=True` once served
  over HTTPS
- Set `ADMIN_PASSWORD` explicitly rather than relying on the
  auto-generated one
- Switch `DATABASE_URL` to PostgreSQL (already configured in
  `docker-compose.yml`)
- Leave `FLASK_DEBUG` unset/`False`

## Security Notes

A security review was performed on this codebase before packaging. See
[`SECURITY.md`](SECURITY.md) for a summary of what was checked, what was
fixed, and recommendations for anyone deploying this publicly.

## Optional Extensions

- `Flask-Mail` — email support for notifications and report delivery
- `Flasgger` — optional Swagger/OpenAPI documentation
- `Flask-Assets` — asset bundling
- `Selenium` + headless Chrome — optional browser automation for bulk
  SMS workflows (see `SMS_ENABLED`/`SMS_USE_SELENIUM` in `.env`)
- `google-generativeai` — optional AI assistant (`GEMINI_API_KEY`)
- `eventlet` — async worker support for `Flask-SocketIO`

## Running Tests

```bash
pip install pytest
pytest test_app.py test_extra.py test_full.py
```

`test_selenium.py` requires a running server and a local Chrome/Chromium
install and is not part of the default test run.

## License

CampusPlayer is proprietary software. See [`LICENSE.md`](LICENSE.md) for
full terms. Unauthorized copying, modification, or redistribution is
prohibited.
