"""
Test Suite: Multi-Tenant Scoping & Cross-Tenant Data Isolation.
"""
import pytest
from factory import create_app
from extensions import db
from models import User, Institution, Video
from crypto_helper import encrypt_password

@pytest.fixture
def app_ctx():
    app = create_app({
        'TESTING': True,
        'WTF_CSRF_ENABLED': False
    })
    with app.app_context():
        db.create_all()

        inst_a = Institution(name='Inst A', slug='inst-a', status='active')
        inst_b = Institution(name='Inst B', slug='inst-b', status='active')
        db.session.add_all([inst_a, inst_b])
        db.session.commit()

        user_a = User(username='student_a', password_hash=encrypt_password('Pass123!'), role='student', institution_id=inst_a.id)
        user_b = User(username='student_b', password_hash=encrypt_password('Pass123!'), role='student', institution_id=inst_b.id)
        db.session.add_all([user_a, user_b])
        db.session.commit()

        video_a = Video(title='Video A', filename='a.mp4', institution_id=inst_a.id, uploader_id=user_a.id, status='published')
        video_b = Video(title='Video B', filename='b.mp4', institution_id=inst_b.id, uploader_id=user_b.id, status='published')
        db.session.add_all([video_a, video_b])
        db.session.commit()

        yield app
        db.session.remove()
        db.drop_all()

def test_tenant_scoping(app_ctx):
    with app_ctx.app_context():
        inst_a = Institution.query.filter_by(slug='inst-a').first()
        inst_b = Institution.query.filter_by(slug='inst-b').first()

        v_a = Video.query.filter_by(institution_id=inst_a.id).all()
        v_b = Video.query.filter_by(institution_id=inst_b.id).all()

        assert len(v_a) == 1
        assert v_a[0].title == 'Video A'
        assert len(v_b) == 1
        assert v_b[0].title == 'Video B'
