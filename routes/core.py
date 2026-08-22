from flask import Blueprint, render_template, redirect, url_for
from flask_login import current_user, login_required

core_bp = Blueprint('core', __name__)


@core_bp.route('/', endpoint='index')
def index():
    if current_user.is_authenticated:
        if current_user.role == 'system_admin':
            return redirect(url_for('system_admin_dashboard'))
        elif current_user.role == 'admin':
            return redirect(url_for('admin_dashboard'))
        elif current_user.role == 'teacher':
            return redirect(url_for('teacher_dashboard'))
        elif current_user.role == 'student':
            return redirect(url_for('student_dashboard'))
    return render_template('ads.html')


@core_bp.route('/student', endpoint='student_dashboard')
@login_required
def student_dashboard():
    return render_template('student_dashboard.html')


@core_bp.route('/teacher', endpoint='teacher_dashboard')
@login_required
def teacher_dashboard():
    return render_template('teacher_dashboard.html')


@core_bp.route('/admin', endpoint='admin_dashboard')
@login_required
def admin_dashboard():
    return render_template('admin_dashboard.html')

