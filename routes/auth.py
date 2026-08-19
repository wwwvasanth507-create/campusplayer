from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, request, flash, session, abort
from flask_login import login_user, logout_user, current_user, login_required
from extensions import db, limiter
from models import User
from services.utils import sanitize_input, validate_csrf_token
from services.auth import log_activity

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit('10 per minute')
def login():
    if current_user.is_authenticated:
        return redirect(url_for('core.index'))
    if request.method == 'POST':
        from flask import current_app
        if not current_app.config.get('TESTING') and not validate_csrf_token(request.form.get('csrf_token')):
            abort(400, description='Invalid CSRF token')

        username = sanitize_input(request.form.get('username'), 150)
        password = request.form.get('password') or ''
        role = sanitize_input(request.form.get('role'), 20)

        if not username or not password:
            flash('Please provide username and password.', 'error')
            return render_template('login.html')

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            if role and user.role != role.lower():
                flash('Invalid role selected for this user.', 'error')
                return render_template('login.html')

            session.permanent = True
            login_user(user, remember=True)
            user.last_login = datetime.utcnow()
            user.login_count = (user.login_count or 0) + 1
            db.session.commit()
            session['theme'] = user.theme_preference or 'dark'
            session['session_version'] = getattr(user, 'session_version', 1)
            session['institution_id'] = getattr(user, 'institution_id', None)
            log_activity('login', f'User {user.username} logged in')


            if user.role == 'system_admin':
                target = url_for('system_admin_dashboard') if 'system_admin_dashboard' in current_app.view_functions else '/system_admin'
            elif user.role == 'admin':
                target = url_for('admin_dashboard') if 'admin_dashboard' in current_app.view_functions else '/admin'
            elif user.role == 'teacher':
                target = url_for('teacher_dashboard') if 'teacher_dashboard' in current_app.view_functions else '/teacher'
            else:
                target = url_for('student_dashboard') if 'student_dashboard' in current_app.view_functions else '/student'

            return redirect(target)
        else:
            flash('Invalid username or password.', 'error')
    return render_template('login.html')




@auth_bp.route('/logout')
@login_required
def logout():
    log_activity('logout', f'User {current_user.username} logged out')
    logout_user()
    session.pop('theme', None)
    return redirect(url_for('core.index'))
