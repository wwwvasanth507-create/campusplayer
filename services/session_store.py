"""
CampusPlayer - Persistent Server-Side Database Session Store.

Implements a custom Flask SessionInterface backed by the UserSession SQLAlchemy model.
"""

import json
import secrets
from datetime import datetime, timedelta
from flask import request
from flask.sessions import SessionInterface, SessionMixin
from werkzeug.datastructures import CallbackDict

from extensions import db


class DbServerSession(CallbackDict, SessionMixin):
    """Session dict implementation that tracks modifications."""
    def __init__(self, initial=None, sid=None, new=False):
        def on_update(self):
            self.modified = True

        super().__init__(initial, on_update)
        self.sid = sid
        self.new = new
        self.modified = False


class SqlAlchemySessionInterface(SessionInterface):
    """
    Production-grade persistent server-side session interface.
    Stores session dictionary payload in the database (`user_session` table).
    Cookies hold only an opaque 64-character session ID string.
    """

    def __init__(self, key_prefix='session:'):
        self.key_prefix = key_prefix

    def generate_sid(self):
        return secrets.token_urlsafe(48)

    def _get_user_session_model(self):
        from models import UserSession
        return UserSession

    def open_session(self, app, request):
        cookie_name = app.config.get('SESSION_COOKIE_NAME', 'session')
        sid = request.cookies.get(cookie_name)

        if not sid:
            sid = self.generate_sid()
            return DbServerSession(sid=sid, new=True)

        try:
            UserSession = self._get_user_session_model()
            sess_rec = UserSession.query.filter_by(sid=sid, is_active=True).first()

            if not sess_rec:
                return DbServerSession(sid=sid, new=True)

            if sess_rec.expiry and sess_rec.expiry < datetime.utcnow():
                try:
                    db.session.delete(sess_rec)
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                return DbServerSession(sid=self.generate_sid(), new=True)

            data = json.loads(sess_rec.data or '{}')
            session_obj = DbServerSession(data, sid=sid, new=False)
            return session_obj

        except Exception as e:
            # Fall back safely without breaking request execution
            app.logger.warning(f"Error loading server-side session {sid}: {e}")
            return DbServerSession(sid=sid, new=True)

    def save_session(self, app, session, response):
        cookie_name = app.config.get('SESSION_COOKIE_NAME', 'session')
        domain = self.get_cookie_domain(app)
        path = self.get_cookie_path(app)

        # Handle deleted or emptied session
        if not session:
            if session.modified:
                try:
                    UserSession = self._get_user_session_model()
                    sess_rec = UserSession.query.get(session.sid)
                    if sess_rec:
                        db.session.delete(sess_rec)
                        db.session.commit()
                except Exception:
                    db.session.rollback()
                response.delete_cookie(cookie_name, domain=domain, path=path)
            return

        if not self.should_set_cookie(app, session):
            return

        httponly = self.get_cookie_httponly(app)
        secure = self.get_cookie_secure(app)
        samesite = self.get_cookie_samesite(app) or 'Lax'
        expiration = self.get_expiration_time(app, session)
        if not expiration:
            expiration = datetime.utcnow() + app.config.get('PERMANENT_SESSION_LIFETIME', timedelta(days=30))

        # Extract current user / institution metadata from session dict if available
        user_id = session.get('_user_id')
        try:
            user_id = int(user_id) if user_id else None
        except Exception:
            user_id = None

        institution_id = session.get('institution_id')
        try:
            institution_id = int(institution_id) if institution_id else None
        except Exception:
            institution_id = None

        payload_json = json.dumps(dict(session))
        user_agent = (request.user_agent.string[:490] if request and request.user_agent else None)
        ip_addr = (request.remote_addr[:90] if request and request.remote_addr else None)

        try:
            UserSession = self._get_user_session_model()
            sess_rec = UserSession.query.get(session.sid)
            now = datetime.utcnow()

            if not sess_rec:
                sess_rec = UserSession(
                    sid=session.sid,
                    user_id=user_id,
                    institution_id=institution_id,
                    data=payload_json,
                    expiry=expiration,
                    created_at=now,
                    last_accessed=now,
                    user_agent=user_agent,
                    ip_address=ip_addr,
                    is_active=True
                )
                db.session.add(sess_rec)
            else:
                sess_rec.data = payload_json
                sess_rec.user_id = user_id or sess_rec.user_id
                sess_rec.institution_id = institution_id or sess_rec.institution_id
                sess_rec.expiry = expiration
                sess_rec.last_accessed = now
                sess_rec.is_active = True
                if user_agent:
                    sess_rec.user_agent = user_agent
                if ip_addr:
                    sess_rec.ip_address = ip_addr

            db.session.commit()

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Failed to persist server-side session {session.sid}: {e}")

        response.set_cookie(
            cookie_name,
            session.sid,
            expires=expiration,
            httponly=httponly,
            domain=domain,
            path=path,
            secure=secure,
            samesite=samesite
        )


def revoke_user_sessions(user_id):
    """Deactivate all active sessions for a specific user ID."""
    from models import UserSession
    try:
        UserSession.query.filter_by(user_id=user_id).update({'is_active': False})
        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        return False


def invalidate_institution_sessions(institution_id):
    """Deactivate and purge all active sessions for users belonging to a specific institution."""
    from models import UserSession, User
    try:
        user_ids = [u.id for u in User.query.filter_by(institution_id=institution_id).all()]
        if user_ids:
            UserSession.query.filter(UserSession.user_id.in_(user_ids)).update({'is_active': False}, synchronize_session=False)
        UserSession.query.filter_by(institution_id=institution_id).update({'is_active': False}, synchronize_session=False)
        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        return False


def cleanup_expired_sessions():
    """Delete expired and inactive session records from the database."""
    from models import UserSession
    try:
        now = datetime.utcnow()
        deleted = UserSession.query.filter(
            (UserSession.expiry < now) | (UserSession.is_active == False)
        ).delete()
        db.session.commit()
        return deleted
    except Exception as e:
        db.session.rollback()
        return 0
