from flask import Blueprint, render_template, redirect, url_for
from flask_login import current_user, login_required

core_bp = Blueprint('core', __name__)


@core_bp.route('/')
def index():
    if current_user.is_authenticated:
        from flask import current_app
        if current_user.role == 'admin':
            target = url_for('admin_dashboard') if 'admin_dashboard' in current_app.view_functions else (url_for('admin.admin_dashboard') if 'admin.admin_dashboard' in current_app.view_functions else '/admin')
            return redirect(target)
        elif current_user.role == 'teacher':
            target = url_for('teacher_dashboard') if 'teacher_dashboard' in current_app.view_functions else (url_for('teacher.teacher_dashboard') if 'teacher.teacher_dashboard' in current_app.view_functions else '/teacher')
            return redirect(target)
        elif current_user.role == 'student':
            target = url_for('student_dashboard') if 'student_dashboard' in current_app.view_functions else (url_for('student.student_dashboard') if 'student.student_dashboard' in current_app.view_functions else '/student')
            return redirect(target)
    return render_template('ads.html')
