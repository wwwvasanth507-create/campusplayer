"""
Test Suite: Multi-Teacher Classrooms & Subject Assignments.
"""
import pytest
from app import app as flask_app
from extensions import db
from models import User, Institution, Classroom, ClassroomTeacher, ChatMessage

@pytest.fixture
def multi_teacher_app():
    flask_app.config.update({
        'TESTING': True,
        'WTF_CSRF_ENABLED': False
    })
    with flask_app.app_context():
        db.create_all()

        inst1 = Institution.query.filter_by(slug='main-inst-mt').first()
        if not inst1:
            inst1 = Institution(name='Main Inst MT', slug='main-inst-mt', status='active')
            db.session.add(inst1)

        inst2 = Institution.query.filter_by(slug='other-inst-mt').first()
        if not inst2:
            inst2 = Institution(name='Other Inst MT', slug='other-inst-mt', status='active')
            db.session.add(inst2)

        db.session.commit()

        creator = User.query.filter_by(username='class_creator_mt').first()
        if not creator:
            creator = User(username='class_creator_mt', display_name='Dr. Padma', role='teacher', institution_id=inst1.id)
            creator.set_password('Pass123!')
            db.session.add(creator)

        subject_t = User.query.filter_by(username='physics_doc_mt').first()
        if not subject_t:
            subject_t = User(username='physics_doc_mt', display_name='Dr. Daniel', role='teacher', institution_id=inst1.id)
            subject_t.set_password('Pass123!')
            db.session.add(subject_t)

        external_t = User.query.filter_by(username='external_doc_mt').first()
        if not external_t:
            external_t = User(username='external_doc_mt', display_name='Dr. External', role='teacher', institution_id=inst2.id)
            external_t.set_password('Pass123!')
            db.session.add(external_t)

        db.session.commit()

        cls = Classroom.query.filter_by(class_code='SCI101MT').first()
        if not cls:
            cls = Classroom(name='Grade 10 Science MT', teacher_id=creator.id, institution_id=inst1.id, class_code='SCI101MT')
            db.session.add(cls)
            db.session.commit()

        yield flask_app

def login_test_user_by_id(client, user_id, institution_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)
        sess['institution_id'] = institution_id
        sess['_fresh'] = True

def test_add_subject_teacher_flow(multi_teacher_app):
    client = multi_teacher_app.test_client()
    with multi_teacher_app.app_context():
        cls_id = Classroom.query.filter_by(class_code='SCI101MT').first().id
        subject_t_id = User.query.filter_by(username='physics_doc_mt').first().id
        creator = User.query.filter_by(username='class_creator_mt').first()
        creator_id, inst_id = creator.id, creator.institution_id

    login_test_user_by_id(client, creator_id, inst_id)

    res = client.post(f'/teacher/classroom/{cls_id}/add_subject_teacher', data={
        'teacher_id': subject_t_id,
        'subject': 'Physics'
    }, follow_redirects=True)
    assert res.status_code == 200

    with multi_teacher_app.app_context():
        db.session.remove()
        st_record = ClassroomTeacher.query.filter_by(classroom_id=cls_id, teacher_id=subject_t_id).first()
        assert st_record is not None
        assert st_record.subject == 'Physics'

def test_multi_tenancy_isolation(multi_teacher_app):
    client = multi_teacher_app.test_client()
    with multi_teacher_app.app_context():
        cls_id = Classroom.query.filter_by(class_code='SCI101MT').first().id
        external_t_id = User.query.filter_by(username='external_doc_mt').first().id
        creator = User.query.filter_by(username='class_creator_mt').first()
        creator_id, inst_id = creator.id, creator.institution_id

    login_test_user_by_id(client, creator_id, inst_id)

    res = client.post(f'/teacher/classroom/{cls_id}/add_subject_teacher', data={
        'teacher_id': external_t_id,
        'subject': 'Biology'
    }, follow_redirects=True)
    assert res.status_code == 200

    with multi_teacher_app.app_context():
        db.session.remove()
        st_record = ClassroomTeacher.query.filter_by(classroom_id=cls_id, teacher_id=external_t_id).first()
        assert st_record is None

def test_remove_subject_teacher(multi_teacher_app):
    client = multi_teacher_app.test_client()
    with multi_teacher_app.app_context():
        cls = Classroom.query.filter_by(class_code='SCI101MT').first()
        cls_id = cls.id
        subject_t_id = User.query.filter_by(username='physics_doc_mt').first().id
        creator = User.query.filter_by(username='class_creator_mt').first()
        creator_id, inst_id = creator.id, creator.institution_id

        st_record = ClassroomTeacher.query.filter_by(classroom_id=cls_id, teacher_id=subject_t_id).first()
        if not st_record:
            st_record = ClassroomTeacher(institution_id=cls.institution_id, classroom_id=cls_id, teacher_id=subject_t_id, subject='Physics')
            db.session.add(st_record)
            db.session.commit()

    login_test_user_by_id(client, creator_id, inst_id)

    res = client.post(f'/teacher/classroom/{cls_id}/remove_subject_teacher/{subject_t_id}', follow_redirects=True)
    assert res.status_code == 200

    with multi_teacher_app.app_context():
        db.session.remove()
        deleted_record = ClassroomTeacher.query.filter_by(classroom_id=cls_id, teacher_id=subject_t_id).first()
        assert deleted_record is None

def test_chatroom_teacher_labels(multi_teacher_app):
    client = multi_teacher_app.test_client()
    with multi_teacher_app.app_context():
        creator = User.query.filter_by(username='class_creator_mt').first()
        subject_t = User.query.filter_by(username='physics_doc_mt').first()
        cls = Classroom.query.filter_by(class_code='SCI101MT').first()
        cls_id = cls.id
        creator_id, creator_inst = creator.id, creator.institution_id
        subject_t_id = subject_t.id

        st_record = ClassroomTeacher.query.filter_by(classroom_id=cls_id, teacher_id=subject_t_id).first()
        if not st_record:
            st_record = ClassroomTeacher(institution_id=cls.institution_id, classroom_id=cls_id, teacher_id=subject_t_id, subject='Physics')
            db.session.add(st_record)

        msg1 = ChatMessage(classroom_id=cls_id, user_id=creator_id, content='Welcome students')
        msg2 = ChatMessage(classroom_id=cls_id, user_id=subject_t_id, content='Physics homework assigned')
        db.session.add_all([msg1, msg2])
        db.session.commit()

    login_test_user_by_id(client, creator_id, creator_inst)

    res = client.get(f'/chatroom/{cls_id}', follow_redirects=True)
    assert res.status_code == 200
    assert b'Dr. Padma' in res.data
    assert b'Class Teacher' in res.data
    assert b'Dr. Daniel' in res.data
    assert b'Physics' in res.data
