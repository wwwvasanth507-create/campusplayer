# Security Review Summary

This document summarizes the security pass performed on CampusPlayer
before this production package was built. It covers what was checked,
what was fixed, and what remains as a recommendation for whoever deploys
this publicly. It is a best-effort static/automated review, **not** a
substitute for a professional penetration test — especially before
handling real student data at scale.

## What Was Checked

- All `.py` files compiled cleanly (`py_compile`), no syntax errors
- The app boots successfully and core routes (`/`, `/login`) respond
  correctly via Flask's test client
- Grep-based sweep for common issues: `eval`/`exec`/`pickle`, `shell=True`
  subprocess calls, raw SQL string interpolation, hardcoded secrets,
  debug flags, weak hashing (MD5/SHA1 for passwords), CORS
  configuration, cookie flags, CSRF handling, file-upload validation,
  and rate limiting on auth endpoints
- CSRF token generation/validation logic, cross-checked between the
  `app.py` entry point and the `factory.py`/`routes/` blueprint path

## Issues Found & Fixed

1. **Broken CSRF validation in the blueprint code path**
   (`services/security.py`). `csrf_protect_request()` only checked that
   a token was *present* in the request — it never compared it against
   the value stored in the session. Any non-empty token (including a
   made-up one) would pass. This path is used by `wsgi.py` /
   `factory.py`, not by `python app.py` directly (which already had
   correct validation), but it's shipped as the "proper" factory
   pattern and would be live if deployed via `gunicorn wsgi:app`.
   **Fixed**: it now reuses the same constant-time comparison
   (`services/utils.validate_csrf_token`) used elsewhere in the app.

2. **Insecure CORS: wildcard origin + credentials on video/subtitle
   responses** (`app.py`, `routes/video.py`). Both set
   `Access-Control-Allow-Origin: *` together with
   `Access-Control-Allow-Credentials: true`. This combination is
   invalid per the Fetch spec and, more importantly, is a well-known
   anti-pattern for cookie-authenticated resources. **Fixed**: replaced
   with an origin allow-list (`MEDIA_ALLOWED_ORIGINS` env var); with
   nothing configured, no cross-origin headers are sent at all
   (same-origin only).

3. **Weak hardcoded fallback encryption key** (`crypto_helper.py`). If
   neither `ENCRYPTION_KEY` nor `SECRET_KEY` was set, the code fell back
   to the literal string `'default-fallback-campusplayer-key'` to derive
   the Fernet key — meaning anyone with the source code could decrypt
   any data encrypted under that fallback. **Fixed**: it now raises an
   error instead of silently using a predictable key, forcing a real key
   to be configured.

4. **Real-looking secrets committed in `.env`**. The uploaded project's
   `.env` contained a hardcoded `SECRET_KEY` and what appears to be a
   live `GEMINI_API_KEY`. **Fixed**: `.env` was removed from this
   package and replaced with a scrubbed `.env.example`; `.gitignore` now
   excludes `.env`. **Action needed from you**: if that Gemini API key
   was ever used outside your own machine or pushed anywhere, rotate it
   at https://aistudio.google.com/apikey — treat it as compromised.

5. **Test/dev artifacts removed from the package**: debug scripts using
   hardcoded credentials (`debug_500.py`), raw dev output dumps
   (`debug*.txt`, `wa_poll*.txt`, `pytest_output.txt`, etc.), an
   unrelated video-segmenting script (`ghost_generator.py` and its
   zip), the committed dev SQLite database (`instance/app.db`, which
   contained a real password hash and test data), and test video/HLS
   output under `static/uploads/`. None of these belong in a
   production or distributable package.

## What Was Already Solid

- Passwords are hashed with Werkzeug's `generate_password_hash` /
  `check_password_hash` (salted, not reversible) — never stored or
  compared in plain text
- File uploads go through `secure_filename()` plus extension
  allow-lists, and path traversal characters (`..`, leading `/`/`\`) are
  explicitly rejected in `services/utils.allowed_file` and friends
- The main `app.py` entry point (the one actually used by `python app.py`
  and the Dockerfile) already had correct, constant-time CSRF
  validation
- Session cookies are `HttpOnly` and `SameSite=Lax` by default, with a
  `SESSION_COOKIE_SECURE` env flag to enable `Secure` once behind HTTPS
- Security headers are set on every response: `X-Frame-Options`,
  `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, and
  a `Content-Security-Policy`
- Login is rate-limited (`10 per minute`) via Flask-Limiter
- `SECRET_KEY` already falls back to a random generated value (with a
  warning) rather than a hardcoded default, when unset

## Recommendations Before a Public Launch

- **Rotate the Gemini API key** noted above if it was ever exposed.
- Set `SECRET_KEY`, `ENCRYPTION_KEY`, and `ADMIN_PASSWORD` explicitly in
  your production `.env` — don't rely on generated defaults.
- Put Flask-Limiter behind Redis (`storage_uri`) instead of the default
  in-memory store once you run more than one worker process, or rate
  limits won't be shared across workers.
- Review the very high rate limits (`10000 per minute`) on some
  non-auth endpoints in `app.py` and tune them to your expected traffic
  and abuse tolerance.
- Consider a dedicated CSRF library (Flask-WTF's `CSRFProtect`) instead
  of the hand-rolled implementation for defense-in-depth, especially if
  this project continues to grow.
- This review did not include: a live penetration test, dependency CVE
  scanning (run `pip-audit` or similar periodically), or manual QA of
  every UI workflow (video upload/transcode, email/SMS delivery, Celery
  jobs, the AI assistant) — those depend on external services
  (SMTP, Redis, FFmpeg, Gemini) that aren't available in an automated
  review and should be tested in a real environment before launch.
