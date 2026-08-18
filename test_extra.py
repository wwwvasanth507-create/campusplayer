import os
import re
import requests

BASE = 'http://127.0.0.1:5000'
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')

def ensure_server_running():
    from app import app
    from extensions import db
    from models import User, Institution, Classroom
    with app.app_context():
        default_inst = Institution.query.filter_by(slug='default').first()
        inst_id = default_inst.id if default_inst else None
        
        # Ensure admin
        admin_u = User.query.filter_by(username='admin').first()
        if not admin_u:
            admin_u = User(username='admin', role='admin', institution_id=inst_id)
            admin_u.set_password(ADMIN_PASSWORD)
            db.session.add(admin_u)
        else:
            admin_u.set_password(ADMIN_PASSWORD)
            if inst_id and not admin_u.institution_id:
                admin_u.institution_id = inst_id
                
        # Ensure teacher_test_full
        teacher_u = User.query.filter_by(username='teacher_test_full').first()
        if not teacher_u:
            teacher_u = User(username='teacher_test_full', role='teacher', institution_id=inst_id)
            teacher_u.set_password('pass123')
            db.session.add(teacher_u)
            db.session.commit()
        else:
            teacher_u.set_password('pass123')
            if inst_id and not teacher_u.institution_id:
                teacher_u.institution_id = inst_id
            db.session.commit()

        # Ensure class exists for chatroom test
        cls = Classroom.query.filter_by(teacher_id=teacher_u.id).first()
        if not cls:
            cls = Classroom(name='Test Class Extra', teacher_id=teacher_u.id, institution_id=inst_id)
            db.session.add(cls)
            db.session.commit()

    try:
        requests.get(BASE + '/login', timeout=1)
        return
    except requests.exceptions.RequestException:
        pass
    import threading, time
    def _run():
        app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(2)

ensure_server_running()


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
    s = requests.Session()
    login_page = s.get(BASE + '/login')
    token = get_csrf_token(login_page.text) or ''
    s.post(BASE + '/login', data={'username': username, 'password': password, 'role': role, 'csrf_token': token}, allow_redirects=True)
    return s

def test_extra():
    print("Starting Extra Features Test...")
    
    # 1. Login as teacher
    teacher = make_session('teacher_test_full', 'pass123', 'Teacher')
    
    # 2. Check Analytics
    r = teacher.get(BASE + '/teacher/analytics')
    if 'analytics' in r.text.lower():
        print("PASS: Analytics page accessible")
    else:
        print("FAIL: Analytics page inaccessible")

    # 3. Check Chatroom
    # Need a class ID first. Let's find one.
    r = teacher.get(BASE + '/teacher/classes')
    import re
    m = re.search(r'/chatroom/(\d+)', r.text)
    if m:
        class_id = m.group(1)
        # Check chatroom page
        r = teacher.get(f'{BASE}/chatroom/{class_id}')
        if 'chatroom' in r.text.lower():
            print(f"PASS: Chatroom page accessible (ID: {class_id})")
            
            # Send a message
            r = post_json(teacher, f'/api/chatroom/{class_id}/send', json_data={'content': 'Hello from test!'})
            if r.json().get('success'):
                print("PASS: Chatroom message sent")
            else:
                print("FAIL: Chatroom message send failed")
        else:
            print(f"FAIL: Chatroom page inaccessible (ID: {class_id})")
    else:
        print("SKIP: No class found to test chatroom")

    # 4. Check Reports (Admin)
    admin = make_session('admin', ADMIN_PASSWORD, 'Admin')
    
    # Levels PDF
    r = admin.get(BASE + '/admin/levels_pdf?type=all')
    if r.status_code == 200:
        print("PASS: Levels PDF generated")
    else:
        print(f"FAIL: Levels PDF failed (Status: {r.status_code})")

    # Attendance PDF
    r = admin.get(BASE + '/admin/attendance_pdf')
    if 'attendance report' in r.text.lower():
        print("PASS: Attendance Report page accessible")
    else:
        print("FAIL: Attendance Report page failed")

    # Struggling Topics (Teacher)
    r = teacher.get(BASE + '/teacher/report/struggling_topics')
    if 'summarized' in r.text.lower() or 'struggling' in r.text.lower():
        print("PASS: Struggling Topics report accessible")
    else:
        print(f"FAIL: Struggling Topics report failed: {r.text[:100]}")

    print("\nExtra Features Test Complete!")

if __name__ == '__main__':
    test_extra()
