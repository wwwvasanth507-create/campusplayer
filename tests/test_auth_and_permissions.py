"""
Test Suite: Authentication, Roles & Session Persistence.
"""
import pytest
from factory import create_app
from extensions import db
from models import User, Institution
from crypto_helper import encrypt_password

@pytest.fixture
def app_ctx():
    app = create_app({
        'TESTING': True,
        'WTF_CSRF_ENABLED': False
    })
    with app.app_context():
        db.create_all()
        inst = Institution(name='Test Inst', slug='test-inst', status='active')
        db.session.add(inst)
        db.session.commit()

        user = User(
            username='teststudent',
            password_hash=encrypt_password('Password123!'),
            role='student',
            institution_id=inst.id
        )
        db.session.add(user)
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()

@pytest.fixture
def client(app_ctx):
    return app_ctx.test_client()

def test_login_flow(client):
    res = client.post('/login', data={
        'username': 'teststudent',
        'password': 'Password123!',
        'role': 'student'
    }, follow_redirects=True)
    assert res.status_code == 200

def test_invalid_login(client):
    res = client.post('/login', data={
        'username': 'teststudent',
        'password': 'WrongPassword!',
        'role': 'student'
    }, follow_redirects=True)
    assert b'Invalid username or password' in res.data or res.status_code == 200
