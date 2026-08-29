"""
Flask Test Client verification for CampusPlayer routes and conversion integration.
"""
import os
import sys
import json
import re

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

from app import app
from extensions import db
from models import User, Video, ConversionJob

def extract_csrf(html_text):
    match = re.search(r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']', html_text)
    if match:
        return match.group(1)
    match = re.search(r'value=["\']([^"\']+)["\']\s+name=["\']csrf_token["\']', html_text)
    if match:
        return match.group(1)
    return ""

def test_routes_and_apis():
    print("Testing Flask Web Routes & Conversion APIs via test client...")
    app.config['TESTING'] = True

    with app.test_client() as client:
        with app.app_context():
            db.create_all()
            # Ensure admin
            admin = User.query.filter_by(username='test_admin').first()
            if not admin:
                admin = User(username='test_admin', role='admin')
                admin.set_password('admin123')
                db.session.add(admin)
                db.session.commit()
            else:
                admin.set_password('admin123')
                db.session.commit()

            # Ensure teacher
            teacher = User.query.filter_by(username='test_teacher_web').first()
            if not teacher:
                teacher = User(username='test_teacher_web', role='teacher')
                teacher.set_password('teacher123')
                db.session.add(teacher)
                db.session.commit()
            else:
                teacher.set_password('teacher123')
                db.session.commit()

        # 1. Fetch login page to get CSRF token
        r_get = client.get('/login')
        csrf_token = extract_csrf(r_get.get_data(as_text=True))

        # 2. Test Admin Login
        res = client.post('/login', data={
            'username': 'test_admin',
            'password': 'admin123',
            'role': 'Admin',
            'csrf_token': csrf_token
        }, follow_redirects=True)
        assert res.status_code == 200, f"Admin login failed: {res.status_code}"
        assert b"Admin Dashboard" in res.data or b"Logout" in res.data or b"admin" in res.data.lower()
        print("  [PASS] Admin login successful.")

        # 3. Test Admin Dashboard
        res = client.get('/admin')
        assert res.status_code == 200
        assert b"Video Conversion Queue" in res.data or b"conversionJobsCard" in res.data
        print("  [PASS] Admin dashboard contains conversion queue card.")

        # 4. Test Admin Conversion Jobs API
        res = client.get('/api/admin/conversion_jobs')
        assert res.status_code == 200
        data = json.loads(res.data)
        assert data['success'] is True
        assert 'jobs' in data
        expected_workers = int(os.getenv('MAX_CONCURRENT_CONVERSIONS', 3))
        assert data['max_workers'] == expected_workers, f"Expected {expected_workers} max_workers, got {data['max_workers']}"
        print(f"  [PASS] /api/admin/conversion_jobs responded successfully: {data['max_workers']} workers.")

        # 5. Test Bulk Retry API as Admin
        res_retry = client.post('/api/admin/conversion_jobs/retry_all', headers={'X-CSRF-Token': csrf_token})
        assert res_retry.status_code == 200, f"Retry all failed: {res_retry.status_code} - {res_retry.data}"
        retry_data = json.loads(res_retry.data)
        assert retry_data['success'] is True
        print(f"  [PASS] /api/admin/conversion_jobs/retry_all responded successfully: {retry_data['message']}")

        # 6. Switch to System Admin session
        client.get('/logout', follow_redirects=True)
        with app.app_context():
            sysadmin = User.query.filter_by(username='test_sysadmin').first()
            if not sysadmin:
                sysadmin = User(username='test_sysadmin', email='sysadmin@test.com', role='system_admin')
                sysadmin.set_password('sysadmin123')
                db.session.add(sysadmin)
                db.session.commit()
            else:
                sysadmin.set_password('sysadmin123')
                db.session.commit()

        r_get = client.get('/login')
        csrf_token = extract_csrf(r_get.get_data(as_text=True))
        res = client.post('/login', data={
            'username': 'test_sysadmin',
            'password': 'sysadmin123',
            'role': 'system_admin',
            'csrf_token': csrf_token
        }, follow_redirects=True)
        assert res.status_code == 200
        print("  [PASS] System Admin login successful.")

        # 7. Test System Admin Dashboard & Conversion Queue Card
        res = client.get('/sysadmin')
        assert res.status_code == 200, f"System admin dashboard failed: {res.status_code}"
        assert b"System-Wide Video Conversion" in res.data or b"sysAdminConversionCard" in res.data
        print("  [PASS] System Admin dashboard rendered with system-wide conversion queue.")

        # 8. Test System Admin API with Filters
        res = client.get('/api/admin/conversion_jobs')
        assert res.status_code == 200
        sys_data = json.loads(res.data)
        assert sys_data['success'] is True
        assert sys_data['is_system_admin'] is True
        assert 'institutions' in sys_data
        print(f"  [PASS] System Admin API returned all-institution data ({len(sys_data.get('institutions', []))} institutions).")

        # 9. Switch to teacher session
        client.get('/logout', follow_redirects=True)
        r_get = client.get('/login')
        csrf_token = extract_csrf(r_get.get_data(as_text=True))
        res = client.post('/login', data={
            'username': 'test_teacher_web',
            'password': 'teacher123',
            'role': 'Teacher',
            'csrf_token': csrf_token
        }, follow_redirects=True)
        assert res.status_code == 200
        print("  [PASS] Teacher login successful.")

        # 10. Test Teacher Processing Videos API
        res = client.get('/api/teacher/processing_videos')
        assert res.status_code == 200
        teacher_jobs = json.loads(res.data)
        assert isinstance(teacher_jobs, list)
        print(f"  [PASS] /api/teacher/processing_videos responded with {len(teacher_jobs)} items.")

    print("\nALL Flask Web Route and API tests PASSED!")

if __name__ == '__main__':
    test_routes_and_apis()
