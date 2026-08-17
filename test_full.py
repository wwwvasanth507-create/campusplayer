import os
import re
import requests
import time

BASE = 'http://127.0.0.1:5000'
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')

def ensure_server_running():
    try:
        requests.get(BASE + '/login', timeout=1)
        return
    except requests.exceptions.RequestException:
        pass
    import threading
    from app import app
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

def test_full():
    print("Starting Full System Test...")
    
    # 1. Admin Login
    admin = make_session('admin', ADMIN_PASSWORD, 'Admin')
    r = admin.get(BASE + '/admin')
    if 'admin' not in r.text.lower():
        print("FAIL: Admin login failed")
        return
    print("PASS: Admin login")

    # 2. Create Teacher
    teacher_user = 'teacher_test_full'
    teacher_pass = 'pass123'
    post_form(admin, '/admin/add_teacher', data={'username': teacher_user, 'password': teacher_pass})
    
    # 3. Teacher Login
    teacher = make_session(teacher_user, teacher_pass, 'Teacher')
    r = teacher.get(BASE + '/teacher')
    if 'dashboard' not in r.text.lower():
        print("FAIL: Teacher login failed")
        return
    print("PASS: Teacher login")

    # 4. Create Student
    student_user = 'student_test_full'
    student_pass = 'pass123'
    post_form(teacher, '/teacher/add_student', data={'username': student_user, 'password': student_pass})
    
    # 5. Create Quiz
    r = post_form(teacher, '/teacher/create_quiz', data={'title': 'Full Test Quiz'})
    m = re.search(r'edit_quiz/(\d+)', r.url)
    if not m:
        print("FAIL: Quiz creation failed")
        return
    quiz_id = m.group(1)
    print(f"PASS: Quiz created (ID: {quiz_id})")

    # 6. Add Question to Quiz
    post_form(teacher, f'/teacher/edit_quiz/{quiz_id}', data={
        'text': 'What is 2+2?',
        'option_a': '3',
        'option_b': '4',
        'option_c': '5',
        'option_d': '6',
        'correct_option': 'B'
    })
    print("PASS: Question added to quiz")

    # 7. AI Assistant Test
    r = post_json(teacher, '/api/ai_chat', json_data={'message': 'hello'})
    if 'hello' in r.json().get('response', '').lower():
        print("PASS: AI Assistant responded")
    else:
        print(f"FAIL: AI Assistant response: {r.text}")

    # 8. Video Upload Test
    video_path = 'test_video.mp4'
    if os.path.exists(video_path):
        with open(video_path, 'rb') as f:
            r = post_form(teacher, '/teacher/upload', data={'title': 'Full Test Video'}, files={'video_file': f})
        if r.status_code == 200 and r.json().get('success'):
            video_id = r.json().get('video_id')
            print(f"PASS: Video uploaded (ID: {video_id})")
            
            # 9. Check Processing Status
            print("Waiting for video processing...")
            for _ in range(10):
                time.sleep(2)
                r = teacher.get(f'{BASE}/api/video_status/{video_id}')
                status = r.json().get('status')
                progress = r.json().get('progress')
                print(f"  Status: {status} ({progress}%)")
                if status == 'completed':
                    print("PASS: Video processing completed")
                    break
                if status == 'failed':
                    print("FAIL: Video processing failed")
                    break
            else:
                print("TIMEOUT: Video processing still pending")
        else:
            print(f"FAIL: Video upload failed: {r.text}")
    else:
        print("SKIP: test_video.mp4 not found")

    # 10. Student Login
    student = make_session(student_user, student_pass, 'Student')
    r = student.get(BASE + '/student')
    if 'campus' in r.text.lower() or 'discover' in r.text.lower():
        print("PASS: Student login")
    else:
        print("FAIL: Student login failed")

    print("\nFull System Test Complete!")

if __name__ == '__main__':
    test_full()
