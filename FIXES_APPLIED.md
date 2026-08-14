# What was actually wrong, and what was fixed

## 1. Chunk upload — root cause found: two competing apps in the same repo

This project contains **two separate Flask application definitions**:

- `app.py` — the real, complete, actively-used application. This is what
  `Dockerfile` and `docker-compose.yml` already run (`gunicorn ... app:app`).
  Its `/teacher/upload_chunk` route (and the JS in `templates/teacher_videos.html`
  that calls it) is correct: it writes each chunk atomically, checks which
  chunks have actually landed on disk (order-independent), uses a per-upload
  lock so only one request assembles the final file, and only then creates the
  `Video` row and kicks off HLS processing.
- `factory.py` + `routes/` + `services/upload_engine.py` /
  `services/ultra_parallel_processor.py` — an **abandoned, half-finished
  rewrite**. Its copy of `/teacher/upload_chunk` (in `routes/video.py`) expects
  different form field names (`file`, `total_chunks`) than the frontend sends
  (`chunk`, `chunkIndex`, `totalChunks`), never validates the chunk index, and
  is missing the completion/assembly logic entirely.
- `wsgi.py` was wired to the **broken** one (`from factory import create_app`).

So depending on which entrypoint actually gets used to start the server
(`app:app` vs `wsgi:app`) — which varies by platform (Heroku/Render/PaaS
defaults, `flask run` with `FLASK_APP=wsgi.py`, a differently-configured
Procfile, etc.) — chunk upload either worked correctly or silently failed.
That mismatch, not a bug in the upload logic itself, is why it looked
"fixed" in the code but "not working" when actually deployed.

**Fix:** `wsgi.py` now simply re-exports the real `app`/`socketio` from
`app.py`, so no matter which entrypoint a host uses, you always get the
same, fully-working application. The legacy `factory.py`/`routes/`/
`services/upload_engine.py`/`services/ultra_parallel_processor.py` files
are left in place (harmless, unused, not imported by `app.py`), but are now
clearly commented as dead code so nobody wires them up by accident again.

**Always start the app as `app:app`** (that's what Docker already does):
```
gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:5000 app:app
# or for local dev:
python app.py
```

## 2. Permanent delete for videos / quizzes / assignments

**Real bug found (this was the actual cause of "deleted video files stay on
disk"):** this app is multi-tenant — a teacher belonging to an institution
gets their videos processed into
`static/uploads/institutions/<slug>/hls/<video_id>/` instead of the plain
`static/hls/<video_id>/` folder. `delete_video` was hard-coded to only ever
look in `static/hls/<video_id>/`, so for any institution-backed video that
path simply didn't exist, the `os.path.exists()` check silently failed, and
`shutil.rmtree(...)` never ran — the real HLS folder (video segments,
thumbnail, sprite sheet, seek-preview VTT) was left behind forever, exactly
as reported (e.g. `static/uploads/institutions/vasanth-university/hls/1`).

Fixed by adding one shared helper, `resolve_video_hls_paths(video)`, that
both `process_video_background` (which creates the folder) and
`delete_video` (which must remove the same folder) now call — so they can
never drift out of sync again. `delete_video` also now double-checks the
directory implied by whatever path is actually stored in the DB
(`hls_playlist_path` / `master_playlist_path`) and the legacy global path,
and removes all of them if present, so videos are cleaned up correctly
whether they're institution-scoped, legacy/global, or even stuck
mid-processing with only partial files written.

Beyond that fix, the rest of the delete behavior was already correct:

- **Video delete** (`/teacher/delete_video/<id>`): removes the original
  upload file, the entire per-video HLS folder (which also contains the
  thumbnail, sprite sheet, and seek-preview VTT — all stored under the same
  folder), the subtitle file, and the DB row (which cascades to comments,
  analytics, notifications, progress, notes, bookmarks).
- **Quiz delete** (`/teacher/delete_quiz/<id>`): hard-deletes the quiz row;
  cascades to its questions and results. Quizzes have no associated files
  on disk, so there's nothing else to clean up.
- **Assignment delete** (`/teacher/delete_assignment/<id>`): removes the
  teacher's question-paper file and every student's submitted file from
  disk, then hard-deletes the assignment row (cascades to submissions).

**Gap found and fixed:** deleting an entire **classroom**
(`/teacher/delete_class/<id>`) cascades-deleted its assignments and
submissions at the database level, but never cleaned up their files on
disk — those files would have been silently orphaned. `delete_class` now
calls the same file-cleanup helper used by `delete_assignment` for every
assignment in the classroom before the cascading delete runs, so nothing
is left behind on the server regardless of which route the deletion comes
from.

## 3. `.env`

A real `.env` (not `.env.example`) already ships in this project with
working generated secrets and sane local defaults (SQLite database, debug
off, etc.), so the app runs immediately without any manual setup. It also
documents which variables `app.py` actually reads via `os.getenv` (some
variables in the old `.env.example`, like `SESSION_TIMEOUT_MINUTES`, were
never read by the code — the real name is `SESSION_TIMEOUT_HOURS`).

Change `SECRET_KEY`, `ENCRYPTION_KEY`, and `ADMIN_PASSWORD` again before any
real/production deployment.

## What I verified in this environment

This sandbox has no internet access, so `pip install -r requirements.txt`
could not be run here (Flask-SQLAlchemy, Flask-Login, Celery, etc. aren't
installable offline), and there's no `ffmpeg`/Redis available either — so I
could not literally boot the server end-to-end inside this tool. What I did
verify directly:

- Every `.py` file in the project compiles cleanly (`python -m py_compile`),
  including `app.py`, `models.py`, `wsgi.py`, `factory.py`, `routes/*.py`,
  `services/*.py`.
- All 48 Jinja templates parse without syntax errors.
- All standalone JS files in `static/js/` pass `node --check`.
- No duplicate Flask route paths or duplicate view-function names in
  `app.py` (which is what causes Flask's "view function mapping is
  overwriting an existing endpoint" crash at startup).
- Traced the full chunk-upload flow (frontend JS → `/teacher/upload_chunk`
  → chunk assembly → `Video` row creation → background HLS thread) and the
  full delete flow for videos/quizzes/assignments/classes line by line
  against the actual models and cascade rules in `models.py`.

**Please still smoke-test after `pip install -r requirements.txt` on a
machine with internet + ffmpeg** — install, `python app.py`, log in as
`admin`/(the password in `.env`), upload a large-enough video to trigger
chunking (>20MB), then delete a video/quiz/assignment and confirm the
files are gone from `static/uploads` / `static/hls` / `static/uploads/assignments`.
