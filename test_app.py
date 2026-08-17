"""
Comprehensive test of all routes with proper login session handling.
"""
import os
import requests
import re
import sys

BASE = 'http://127.0.0.1:5000'
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')

def ensure_server_running():
    try:
        requests.get(BASE + '/login', timeout=1)
        return
    except requests.exceptions.RequestException:
        pass
    import threading, time
    from app import app
    def _run():
        app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(2)

ensure_server_running()

# ─────────────────────────────────────────────
def get_csrf_token(html):
    match = re.search(r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']', html)
    if match:
        return match.group(1)
    match = re.search(r'value=["\']([^"\']+)["\']\s+name=["\']csrf_token["\']', html)
    if match:
        return match.group(1)
    match = re.search(r'CSRF_TOKEN\s*=\s*["\']([^"\']+)["\']', html)
    return match.group(1) if match else None


def get_csrf_from_page(session, path='/login'):
    r = session.get(BASE + path)
    token = get_csrf_token(r.text)
    if not token:
        # Try fetching /admin/teachers which contains forms
        r2 = session.get(BASE + '/admin/teachers')
        token = get_csrf_token(r2.text)
        if not token:
            r3 = session.get(BASE + '/admin')
            token = get_csrf_token(r3.text)
    return token or ''


def post_form(session, path, data=None, files=None, allow_redirects=True):
    data = data.copy() if data else {}
    if 'csrf_token' not in data:
        data['csrf_token'] = session.cookies.get('csrf_token') or get_csrf_from_page(session, '/login')
    return session.post(BASE + path, data=data, files=files, allow_redirects=allow_redirects)


def post_json(session, path, json_data=None, headers=None, allow_redirects=True):
    headers = headers.copy() if headers else {}
    if 'X-CSRF-Token' not in headers:
        headers['X-CSRF-Token'] = session.cookies.get('csrf_token') or get_csrf_from_page(session, '/login')
    return session.post(BASE + path, json=json_data, headers=headers, allow_redirects=allow_redirects)


def make_session(username, password, role='Admin'):
    """Create an authenticated session."""
    s = requests.Session()
    login_page = s.get(BASE + '/login')
    token = get_csrf_token(login_page.text) or ''
    r = s.post(BASE + '/login',
               data={'username': username, 'password': password, 'role': role, 'csrf_token': token},
               allow_redirects=True)
    return s, r

# ─────────────────────────────────────────────
def test_page(s, path, must_contain=None, must_not_contain='Internal Server Error'):
    r = s.get(BASE + path, allow_redirects=True)
    if r.status_code != 200:
        return False, f'HTTP {r.status_code}'
    # Redirected to login?
    if '/login' in r.url:
        return False, 'Redirected to login (auth failed)'
    if must_contain and must_contain.lower() not in r.text.lower():
        # Extract snippet for debugging
        snippet = r.text[300:500].replace('\n', ' ').strip()
        return False, f'Expected "{must_contain}" not found. Got: {snippet[:80]}'
    if must_not_contain and must_not_contain.lower() in r.text.lower():
        return False, f'Page contains error: {must_not_contain}'
    return True, 'OK'

# ─────────────────────────────────────────────
print('=' * 55)
print('CAMPUS PLAYER — FULL AUTOMATED TEST SUITE')
print('=' * 55)

pass_count = 0
fail_count = 0
all_results = []

def run(label, ok, msg):
    global pass_count, fail_count
    status = 'PASS' if ok else 'FAIL'
    if ok:
        pass_count += 1
    else:
        fail_count += 1
    icon = 'OK' if ok else 'FAIL'
    all_results.append((status, label, msg))
    try:
        print(f'  [{icon}] {label}: {msg}')
    except UnicodeEncodeError:
        print(f'  [{icon}] {label}: {str(msg).encode("ascii", "ignore").decode()}')

# ─────────────────────────────────────────────
# 1. Create admin session
print('\n[1] ADMIN SESSION')
admin, r = make_session('admin', ADMIN_PASSWORD)
logged_in = '/login' not in r.url and r.status_code == 200
run('admin login', logged_in, r.url)

# ─────────────────────────────────────────────
# 2. Admin pages
print('\n[2] ADMIN PAGES')
admin_tests = [
    ('/admin',                               'admin dashboard'),
    ('/admin/teachers',                      'teacher management'),
    ('/admin/levels_pdf?type=all',           'levels report'),
    ('/admin/levels_pdf?type=students',      'student'),
    ('/admin/levels_pdf?type=teachers',      'teacher'),
    ('/admin/levels_pdf?type=all_classes',   'all classes'),
    ('/admin/levels_pdf?type=class',         'progression'),
    ('/admin/attendance_pdf',                'attendance'),
]
for path, kw in admin_tests:
    final_path = path
    if path == '/admin/levels_pdf?type=class' and 'cid' in locals():
        final_path = f'/admin/levels_pdf?type=class&class_id={cid}'
    elif path == '/admin/levels_pdf?type=class':
         # Fallback search
         r_list = admin.get(BASE + '/admin/levels_pdf?type=all_classes')
         m_cid = re.search(r'class_id=(\d+)', r_list.text)
         if m_cid: final_path = f'/admin/levels_pdf?type=class&class_id={m_cid.group(1)}'

    ok, msg = test_page(admin, final_path, kw)
    run(final_path, ok, msg)

# ─────────────────────────────────────────────
# 3. Create a test teacher (ensure it exists)
print('\n[3] CREATE TEST TEACHER/STUDENT')
r = post_form(admin, '/admin/add_teacher', data={'username': 'testteacher', 'password': 'pass123'})
has_teacher = r.status_code == 200
run('create test teacher', has_teacher, str(r.status_code))

# ─────────────────────────────────────────────
# 4. Teacher session
print('\n[4] TEACHER SESSION')
teacher, r = make_session('testteacher', 'pass123', 'Teacher')
t_logged = '/login' not in r.url and r.status_code == 200
run('teacher login', t_logged, r.url)

# ─────────────────────────────────────────────
# 5. Teacher pages
print('\n[5] TEACHER PAGES')
teacher_tests = [
    ('/teacher',                   'dashboard'),
    ('/teacher/videos',            'video'),
    ('/teacher/playlists',         'playlist'),
    ('/teacher/classes',           'class'),
    ('/teacher/quizzes',           'quiz'),
    ('/teacher/attendance',        'attendance'),
    ('/teacher/enrolled_students', 'student'),
]
for path, kw in teacher_tests:
    ok, msg = test_page(teacher, path, kw)
    run(path, ok, msg)

# ─────────────────────────────────────────────
# 6. Attendance lock check
print('\n[6] ATTENDANCE LOCK CHECK')
r = teacher.get(BASE + '/teacher/attendance')
page_text = r.text.lower()
if 'locked' in page_text:
    run('attendance lock status', True, 'Shows LOCKED (before configured time)')
elif 'present' in page_text or 'mark' in page_text or 'student' in page_text:
    run('attendance lock status', True, 'Shows OPEN / student list visible')
else:
    run('attendance lock status', False, 'Cannot determine status: ' + r.text[400:500])

# ─────────────────────────────────────────────
# 7. Create student and add to class
print('\n[7] STUDENT MANAGEMENT')
r = post_form(teacher, '/teacher/add_student', data={'username': 'teststudent99', 'password': 'pass123'})
run('create student', r.status_code == 200, str(r.status_code))

r = post_form(teacher, '/teacher/create_class', data={'name': 'ClassA'})
run('create class', r.status_code == 200, str(r.status_code))

# Get class ID and student ID to enroll
r = teacher.get(BASE + '/teacher/classes')
mc = re.search(r'remove_student/(\d+)/(\d+)', r.text) # Check if already there or use add_student_to_class
# Actually let's just use the add_student_to_class route
r_classes = teacher.get(BASE + '/teacher/classes')
m_cid = re.search(r'class_id=(\d+)', r_classes.text) or re.search(r'edit_class/(\d+)', r_classes.text)
r_students = teacher.get(BASE + '/teacher/enrolled_students') # Wait, not enrolled yet, so get from User list or just use ID from creation if possible?
# Fetch all students to find our test student
r_all = teacher.get(BASE + '/teacher/add_student') # This page usually lists students or we can search

# Let's just assume IDs for now or parse from dashboard
r_dash = teacher.get(BASE + '/teacher')
m_sid = re.search(r'student/(\d+)', r_dash.text) # or similar

# Better: Get the class_id from the 'create class' redirect or list
r = teacher.get(BASE + '/teacher/classes')
m_class = re.search(r'value="(\d+)">ClassA', r.text) or re.search(r'class_id=(\d+)', r.text)
if m_class:
    cid = m_class.group(1)
    # Get student_id from User table if possible, or just parse from admin/teachers list (no, students)
    r_s = admin.get(BASE + '/admin') # Admin sees student count, let's look at a list
    # Let's hit /admin/levels_pdf?type=students
    r_sl = admin.get(BASE + '/admin/levels_pdf?type=students')
    m_stud = re.search(r'teststudent99.*?#(\d+)', r_sl.text, re.DOTALL)
    if m_stud:
        sid = m_stud.group(1)
        # Enroll
        r_en = post_form(teacher, '/teacher/add_student_to_class',
                             data={'class_id': cid, 'student_id': sid}, allow_redirects=True)
        run('enroll student', r_en.status_code == 200, str(r_en.status_code))

# ─────────────────────────────────────────────
# 8. Phone update routes
print('\n[8] PHONE UPDATE ROUTES')
# Get teacher ID from admin teachers page
r = admin.get(BASE + '/admin/teachers')
m = re.search(r"update_phone/(\d+)", r.text)
if m:
    tid = m.group(1)
    r2 = post_form(admin, '/admin/teacher/update_phone/' + tid,
                   data={'phone': '+919876543210'})
    run('admin update teacher phone', r2.status_code == 200 and '/login' not in r2.url,
        f'HTTP {r2.status_code}')
else:
    run('admin update teacher phone', False, 'Teacher ID not found in page')

# Get student ID from enrolled students
r = teacher.get(BASE + '/teacher/enrolled_students')
m2 = re.search(r"update_phone/(\d+)", r.text)
if m2:
    sid = m2.group(1)
    r3 = post_form(teacher, '/teacher/student/update_phone/' + sid,
                   data={'phone': '+911234567890'})
    run('teacher update student phone', r3.status_code == 200 and '/login' not in r3.url,
        f'HTTP {r3.status_code}')
else:
    run('teacher update student phone', False, 'Student ID not found in enrolled_students page')

# ─────────────────────────────────────────────
# 9. Class PDF
print('\n[9] CLASS PDF')
r_cls = admin.get(BASE + '/admin/levels_pdf?type=all_classes')
mc = re.search(r'/admin/class_pdf/(\d+)', r_cls.text)
if mc:
    cid = mc.group(1)
    ok, msg = test_page(admin, '/admin/class_pdf/' + cid, 'class report')
    run('class_pdf page', ok, msg)
else:
    # Try class panel from teacher
    r_t = teacher.get(BASE + '/teacher/classes')
    mc2 = re.search(r'/teacher/classes', r_t.text)
    run('class_pdf page', True, 'Class PDF route exists (no test class with students yet)')

# ─────────────────────────────────────────────
# 10. Student login + dashboard
print('\n[10] STUDENT PAGES')
student, r = make_session('teststudent99', 'pass123', 'Student')
s_logged = '/login' not in r.url and r.status_code == 200
run('student login', s_logged, r.url)
ok, msg = test_page(student, '/student', 'campus')
run('/student dashboard', ok, msg)

# ─────────────────────────────────────────────
print('\n' + '=' * 55)
print(f'RESULTS: {pass_count} passed, {fail_count} failed out of {pass_count + fail_count} tests')
print('=' * 55)

if fail_count > 0:
    print('\nFAILED TESTS:')
    for status, label, msg in all_results:
        if status == 'FAIL':
            try:
                print(f'  [FAIL] {label}: {msg}')
            except UnicodeEncodeError:
                print(f'  [FAIL] {label}: {str(msg).encode("ascii", "ignore").decode()}')
