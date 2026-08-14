"""
WSGI entrypoint.

IMPORTANT: `app.py` is the single, canonical Flask application for this
project (it's what Dockerfile / docker-compose already run via `app:app`).

`factory.py` + `routes/` + `services/upload_engine.py` /
`services/ultra_parallel_processor.py` were an abandoned, incomplete rewrite
that was never finished (its chunk-upload endpoint used different field
names than the frontend sends and skipped several validation/completion
steps that the real implementation in app.py has). Importing that half
-finished app here caused features that work fine under `app:app` (chunk
uploads in particular) to appear broken whenever something started the
server via `wsgi:app` instead (e.g. `gunicorn wsgi:app`, some PaaS default
Procfiles, or `flask run` with FLASK_APP=wsgi.py).

To guarantee identical behavior no matter which entrypoint a host/platform
picks, this module simply re-exports the real app from app.py.
"""
from app import app, socketio  # noqa: F401

if __name__ == '__main__':
    import os
    debug = os.getenv('FLASK_DEBUG', 'False').lower() in ('1', 'true', 'yes')
    socketio.run(app, host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=debug)
