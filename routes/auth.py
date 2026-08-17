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
        if not validate_csrf_token(request.form.get('csrf_token')):
            abort(400, description='Invalid CSRF token')

        username = sanitize_input(request.form.get('username'), 150)
        password = request.form.get('password') or ''
        role = sanitize_input(request.form.get('role'), 20)

        if not username or not password or not role:
            flash('Please provide username, password, and role.', 'error')
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
            log_activity('login', f'User {user.username} logged in')

            if user.role == 'admin':
                return redirect(url_for('admin.admin_dashboard'))
            elif user.role == 'teacher':
                return redirect(url_for('teacher.teacher_dashboard'))
            elif user.role == 'student':
                return redirect(url_for('student.student_dashboard'))
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
