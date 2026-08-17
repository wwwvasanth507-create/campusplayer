from flask import Blueprint, render_template, redirect, url_for
from flask_login import current_user, login_required

core_bp = Blueprint('core', __name__)


@core_bp.route('/')
def index():
    if current_user.is_authenticated:
        if current_user.role == 'admin':
            return redirect(url_for('admin.admin_dashboard'))
        elif current_user.role == 'teacher':
            return redirect(url_for('teacher.teacher_dashboard'))
        elif current_user.role == 'student':
            return redirect(url_for('student.student_dashboard'))
    return render_template('ads.html')
