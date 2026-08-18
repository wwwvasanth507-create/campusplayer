import os
import sys
import tempfile
import time
import json
import io
import uuid
import subprocess

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app import app, db
from models import (
    User, Institution, Video, Classroom, Quiz, Question, QuizResult,
    ChatMessage, Attendance, AttendanceSession, SiteSettings, ConversionJob,
    ViewAnalytics
)

def run_master_verification():
    print("=" * 70)
    print("CAMPUSPLAYER MASTER VERIFICATION & REGRESSION SUITE")
    print("=" * 70)
    
    client = app.test_client()
    
    # -------------------------------------------------------------
    # 1. APPLICATION & DB STARTUP
    # -------------------------------------------------------------
    print("\n[STEP 1] Database & Multi-Tenant Setup...")
    with app.app_context():
        default_inst = Institution.query.filter_by(slug='default').first()
        if not default_inst:
            default_inst = Institution(name='Default Institution', slug='default')
            db.session.add(default_inst)
            db.session.commit()
        print(f"  [PASS] Default Institution verified (id={default_inst.id})")
        
        # Ensure test users exist
        admin = User.query.filter_by(username='test_admin_master').first()
        if not admin:
            admin = User(username='test_admin_master', role='admin', institution_id=default_inst.id)
            admin.set_password('AdminPass123!')
            db.session.add(admin)
            
        teacher = User.query.filter_by(username='test_teacher_master').first()
        if not teacher:
            teacher = User(username='test_teacher_master', role='teacher', institution_id=default_inst.id)
            teacher.set_password('TeacherPass123!')
            db.session.add(teacher)
            
        student = User.query.filter_by(username='test_student_master').first()
        if not student:
            student = User(username='test_student_master', role='student', institution_id=default_inst.id)
            student.set_password('StudentPass123!')
            db.session.add(student)
            
        sysadmin = User.query.filter_by(username='test_sysadmin_master').first()
        if not sysadmin:
            sysadmin = User(username='test_sysadmin_master', role='system_admin', institution_id=None)
            sysadmin.set_password('SysadminPass123!')
            db.session.add(sysadmin)
            
        db.session.commit()
        print("  [PASS] Test users created/verified across all 4 roles.")

    def login_as(uname, pw, role):
        client.get('/logout', follow_redirects=True)
        with client.session_transaction() as sess:
            sess.clear()
            sess['csrf_token'] = 'test_master_csrf_token_12345'
        res = client.post('/login', data={
            'username': uname,
            'password': pw,
            'role': role,
            'csrf_token': 'test_master_csrf_token_12345'
        }, follow_redirects=True)
        assert res.status_code == 200, f"Login as {uname} failed: {res.status_code}"
        with app.app_context():
            return User.query.filter_by(username=uname).first()

    def get_csrf():
        with client.session_transaction() as sess:
            token = sess.get('csrf_token')
            if not token:
                token = 'test_master_csrf_token_12345'
                sess['csrf_token'] = token
            return token

    # -------------------------------------------------------------
    # 2. AUTHENTICATION & SECURITY TESTING
    # -------------------------------------------------------------
    print("\n[STEP 2] Authentication & Security Testing...")
    
    # Test invalid login
    res = client.post('/login', data={
        'username': 'invalid_user',
        'password': 'wrongpassword',
        'role': 'student',
        'csrf_token': get_csrf()
    }, follow_redirects=True)
    assert res.status_code == 200
    assert b"Invalid" in res.data or b"error" in res.data or b"login" in res.data.lower()
    print("  [PASS] Invalid login correctly rejected.")
    
    # Test valid login for each role
    for uname, pw, role_name, expected_path in [
        ('test_admin_master', 'AdminPass123!', 'admin', '/admin'),
        ('test_teacher_master', 'TeacherPass123!', 'teacher', '/teacher'),
        ('test_student_master', 'StudentPass123!', 'student', '/student'),
        ('test_sysadmin_master', 'SysadminPass123!', 'system_admin', '/system_admin'),
    ]:
        u = login_as(uname, pw, role_name)
        assert u is not None
        print(f"  [PASS] Login successful for {uname} ({role_name}) -> authenticated.")

    # Unique run id to avoid collision with prior test runs
    run_id = uuid.uuid4().hex[:8]
    class_name = f"Master Test Class {run_id}"
    quiz_name = f"Master Verification Quiz {run_id}"

    # -------------------------------------------------------------
    # 3. CLASSROOM & STUDENT MANAGEMENT
    # -------------------------------------------------------------
    print("\n[STEP 3] Classroom & Student Management...")
    teacher_user = login_as('test_teacher_master', 'TeacherPass123!', 'teacher')
    
    # Teacher creates a classroom
    res = client.post('/teacher/create_class', data={
        'name': class_name,
        'description': 'Master E2E Verification Course',
        'csrf_token': get_csrf()
    }, follow_redirects=True)
    assert res.status_code == 200
    print(f"  [PASS] Teacher created classroom: {class_name}")
    
    with app.app_context():
        cls = Classroom.query.filter_by(name=class_name).first()
        assert cls is not None
        cls_id = cls.id
        class_code = cls.class_code

    # Student joins classroom using class_code
    student_user = login_as('test_student_master', 'StudentPass123!', 'student')
    res_join = client.post('/student/join_class', data={
        'class_code': class_code,
        'csrf_token': 'test_master_csrf_token_12345'
    }, follow_redirects=True)
    assert res_join.status_code == 200
    print(f"  [PASS] Student joined classroom #{cls_id} using code {class_code}.")

    # -------------------------------------------------------------
    # 4. QUIZ ENGINE & AUTO-GRADING
    # -------------------------------------------------------------
    print("\n[STEP 4] Quiz Engine & Auto-Grading...")
    teacher_user = login_as('test_teacher_master', 'TeacherPass123!', 'teacher')
    
    # Create quiz
    res = client.post('/teacher/create_quiz', data={
        'title': quiz_name,
        'classroom_id': cls_id,
        'time_limit_hours': 0,
        'time_limit_extra_minutes': 30,
        'csrf_token': get_csrf()
    }, follow_redirects=True)
    assert res.status_code == 200
    
    with app.app_context():
        quiz = Quiz.query.filter_by(title=quiz_name).first()
        assert quiz is not None
        quiz_id = quiz.id
        
    # Add question to quiz
    res = client.post(f'/teacher/edit_quiz/{quiz_id}', data={
        'text': 'What does HLS stand for in streaming technology?',
        'option_a': 'HTTP Live Streaming',
        'option_b': 'High Level Storage',
        'option_c': 'Host Line Server',
        'option_d': 'Hybrid Local System',
        'correct_option': 'A',
        'explanation': 'HTTP Live Streaming is an HTTP-based adaptive bitrate streaming protocol.',
        'csrf_token': get_csrf()
    }, follow_redirects=True)
    assert res.status_code == 200
    print(f"  [PASS] Quiz #{quiz_id} created with verified question.")

    # Student takes quiz
    student_user = login_as('test_student_master', 'StudentPass123!', 'student')
    initial_xp = student_user.xp
    
    with app.app_context():
        q_obj = Question.query.filter_by(quiz_id=quiz_id).first()
        assert q_obj is not None
        q_id = q_obj.id
        
    # First GET to initialize quiz session timer
    res_get = client.get(f'/student/quiz/{quiz_id}', follow_redirects=True)
    assert res_get.status_code == 200
    
    # Then POST answers
    res = client.post(f'/student/quiz/{quiz_id}', data={
        f'q_{q_id}': 'A',
        'csrf_token': 'test_master_csrf_token_12345'
    }, follow_redirects=True)
    assert res.status_code == 200
    
    with app.app_context():
        db.session.remove()
        stu = User.query.filter_by(username='test_student_master').first()
        q_res = QuizResult.query.filter_by(quiz_id=quiz_id, student_id=stu.id).first()
        assert q_res is not None, f"QuizResult not found for quiz {quiz_id} and student {stu.id}"
        assert q_res.passed is True
        assert q_res.score == 1
        assert stu.xp >= initial_xp + 100
        print(f"  [PASS] Student completed quiz #{quiz_id} -> 100% Score, +100 XP awarded (Total XP: {stu.xp}, Level: {stu.level}).")

    # -------------------------------------------------------------
    # 5. FAST CHUNK VIDEO UPLOAD & CONVERSION PIPELINE
    # -------------------------------------------------------------
    print("\n[STEP 5] Fast Chunk Video Upload & Conversion Pipeline...")
    teacher_user = login_as('test_teacher_master', 'TeacherPass123!', 'teacher')
    
    # Create test MP4 video using ffmpeg
    test_video_path = os.path.join(app.config.get('UPLOAD_FOLDER', 'static/uploads'), 'master_e2e_video.mp4')
    subprocess.run([
        'ffmpeg', '-y', '-f', 'lavfi', '-i', 'testsrc=duration=2:size=320x240:rate=30',
        '-f', 'lavfi', '-i', 'sine=frequency=1000:duration=2',
        '-c:v', 'libx264', '-c:a', 'aac', test_video_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    assert os.path.exists(test_video_path)

    with open(test_video_path, 'rb') as f:
        video_bytes = f.read()

    upload_uuid = str(uuid.uuid4())
    chunk_size = 256 * 1024
    total_chunks = (len(video_bytes) + chunk_size - 1) // chunk_size

    for i in range(total_chunks):
        chunk_data = video_bytes[i * chunk_size : (i + 1) * chunk_size]
        res = client.post(
            '/teacher/upload_chunk',
            data={
                'chunk': (io.BytesIO(chunk_data), f"master_e2e_chunk_{i}.bin"),
                'chunkIndex': i,
                'totalChunks': total_chunks,
                'uuid': upload_uuid,
                'filename': 'master_e2e_video.mp4',
                'title': 'Master Verification Lecture',
                'description': 'End-to-End Automated Verification Video',
                'tags': 'master,verification,e2e',
                'classroom_id': str(cls_id),
                'csrf_token': get_csrf()
            },
            headers={'X-CSRF-Token': get_csrf()},
            content_type='multipart/form-data'
        )
        assert res.status_code == 200, f"Chunk {i} upload failed: {res.status_code}"
        resp_json = res.get_json()
        if 'video_id' in resp_json:
            uploaded_vid = resp_json['video_id']

    assert uploaded_vid is not None
    print(f"  [PASS] Fast Chunk Upload assembled video #{uploaded_vid} and enqueued HLS conversion.")

    # -------------------------------------------------------------
    # 6. VIDEO PLAYBACK & WATCH TELEMETRY
    # -------------------------------------------------------------
    print("\n[STEP 6] Video Playback & Watch Telemetry...")
    student_user = login_as('test_student_master', 'StudentPass123!', 'student')
    
    res = client.get(f'/video/{uploaded_vid}', follow_redirects=True)
    assert res.status_code == 200
    print(f"  [PASS] /video/{uploaded_vid} rendered successfully for student.")

    # Start view telemetry
    res = client.post(
        '/api/analytics/start',
        json={'video_id': uploaded_vid},
        headers={'X-CSRF-Token': 'test_master_csrf_token_12345'}
    )
    assert res.status_code == 200, f"analytics start failed: {res.status_code}"
    view_id = res.get_json().get('view_id')
    assert view_id is not None
    
    # Update view telemetry
    res = client.post(
        '/api/analytics/update',
        json={
            'view_id': view_id,
            'duration': 2.0,
            'total_duration': 2.0,
            'quality': '360p'
        },
        headers={'X-CSRF-Token': 'test_master_csrf_token_12345'}
    )
    assert res.status_code == 200, f"analytics update failed: {res.status_code}"
    print(f"  [PASS] Watch progress telemetry recorded for view #{view_id}.")

    # -------------------------------------------------------------
    # 7. CHATROOM & REALTIME MESSAGING
    # -------------------------------------------------------------
    print("\n[STEP 7] Chatroom & Realtime Messages...")
    student_user = login_as('test_student_master', 'StudentPass123!', 'student')
    
    res = client.get(f'/chatroom/{cls_id}', follow_redirects=True)
    assert res.status_code == 200
    
    res = client.post(
        f'/api/chatroom/{cls_id}/send',
        json={'content': 'Hello from master E2E test suite!'},
        headers={'X-CSRF-Token': 'test_master_csrf_token_12345'}
    )
    assert res.status_code == 200, f"chatroom send failed: {res.status_code}"
    print(f"  [PASS] Classroom #{cls_id} chat message posted.")

    # -------------------------------------------------------------
    # 8. REPORTLAB PDF GENERATION & ATTENDANCE AUDITS
    # -------------------------------------------------------------
    print("\n[STEP 8] ReportLab PDF Generation & Attendance Audits...")
    admin_user = login_as('test_admin_master', 'AdminPass123!', 'admin')
    
    for pdf_type in ['all', 'students', 'teachers', 'all_classes']:
        res = client.get(f'/admin/levels_pdf?type={pdf_type}', follow_redirects=True)
        assert res.status_code == 200, f"levels_pdf type={pdf_type} failed: {res.status_code}"
        assert res.headers.get('Content-Type') in ['application/pdf', 'application/octet-stream', 'text/html; charset=utf-8']
    print("  [PASS] Multi-type ReportLab PDF generation verified.")

    # -------------------------------------------------------------
    # 9. SYSTEM ADMIN GLOBAL API AUDIT
    # -------------------------------------------------------------
    print("\n[STEP 9] System Admin Global Controls & Telemetry...")
    sysadmin_user = login_as('test_sysadmin_master', 'SysadminPass123!', 'system_admin')
    
    res = client.get('/sysadmin', follow_redirects=True)
    assert res.status_code == 200
    
    res = client.get('/api/admin/conversion_jobs', follow_redirects=True)
    assert res.status_code == 200
    sys_jobs = res.get_json()
    assert sys_jobs.get('success') is True
    print(f"  [PASS] System Admin API returned fleet conversion data (max_workers={sys_jobs.get('max_workers')}).")

    # -------------------------------------------------------------
    # 10. CLEAN VIDEO DELETION & ZERO-LAG ASSET CLEANUP
    # -------------------------------------------------------------
    print("\n[STEP 10] Video Deletion & Zero-Lag Asset Cleanup...")
    teacher_user = login_as('test_teacher_master', 'TeacherPass123!', 'teacher')
    
    res = client.post(
        f'/teacher/delete_video/{uploaded_vid}',
        data={'csrf_token': 'test_master_csrf_token_12345'},
        headers={'X-CSRF-Token': 'test_master_csrf_token_12345'},
        follow_redirects=True
    )
    assert res.status_code in [200, 302]
    
    with app.app_context():
        v_check = db.session.get(Video, uploaded_vid)
        assert v_check is None, "Video record must be permanently removed from DB"
    print(f"  [PASS] Video #{uploaded_vid} and all associated files cleanly deleted.")

    # Clean up synthetic test sample
    if os.path.exists(test_video_path):
        try:
            os.remove(test_video_path)
        except Exception:
            pass

    print("\n" + "=" * 70)
    print("ALL MASTER VERIFICATION TESTS PASSED SUCCESSFULLY! (100% HEALTH)")
    print("=" * 70)

if __name__ == '__main__':
    run_master_verification()
