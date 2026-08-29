"""
Test Suite: Resumable Multipart Video Upload Engine.
"""
import io
import pytest
from factory import create_app
from extensions import db
from models import Institution, User, UploadSession, UploadPart
from crypto_helper import encrypt_password
from services.upload_engine import (
    init_upload_session, save_upload_part, get_upload_status, complete_upload_session
)

@pytest.fixture
def app_ctx():
    app = create_app({
        'TESTING': True,
        'WTF_CSRF_ENABLED': False
    })
    with app.app_context():
        db.create_all()
        inst = Institution(name='Upload Inst', slug='upload-inst', status='active')
        db.session.add(inst)
        db.session.commit()

        user = User(username='teacher1', password_hash=encrypt_password('Pass123!'), role='teacher', institution_id=inst.id)
        db.session.add(user)
        db.session.commit()

        yield app
        db.session.remove()
        db.drop_all()

def test_resumable_upload_lifecycle(app_ctx):
    with app_ctx.app_context():
        inst = Institution.query.filter_by(slug='upload-inst').first()
        user = User.query.filter_by(username='teacher1').first()

        # 1. Init Session
        session_obj = init_upload_session(
            institution_id=inst.id,
            uploader_id=user.id,
            original_filename='lecture.mp4',
            title='Physics Lecture',
            total_bytes=100,
            part_size=50
        )
        assert session_obj.upload_id.startswith('upl_')
        assert session_obj.total_parts == 2

        # 2. Upload Part 1
        stream1 = io.BytesIO(b'A' * 50)
        part1 = save_upload_part(session_obj.upload_id, 1, stream1)
        assert part1.part_number == 1
        assert part1.part_size == 50

        # 3. Check status
        status = get_upload_status(session_obj.upload_id)
        assert status['received_parts'] == 1
        assert status['uploaded_part_numbers'] == [1]
        assert status['missing_part_numbers'] == [2]

        # 4. Upload Part 2 (Out of order test: re-upload part 1 is idempotent)
        part1_dup = save_upload_part(session_obj.upload_id, 1, io.BytesIO(b'A' * 50))
        assert part1_dup.part_number == 1

        stream2 = io.BytesIO(b'B' * 50)
        part2 = save_upload_part(session_obj.upload_id, 2, stream2)
        assert part2.part_number == 2

        # 5. Complete Session
        res = complete_upload_session(session_obj.upload_id)
        assert res['status'] == 'completed'
        assert res['video_id'] is not None
        assert res['job_id'] is not None
