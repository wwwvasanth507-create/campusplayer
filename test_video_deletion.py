"""
Comprehensive Automated Test Suite for Permanent Video File & Asset Deletion.
Validates that whenever a video is deleted (by teacher, automated task, or user cascade):
1. Database records are deleted.
2. Raw uploaded files are permanently removed from server disk.
3. HLS directories, playlists, and all .ts video segments are permanently removed.
4. Multi-tenant institution files, subtitles, thumbnails, and preview sprites are removed.
5. Active/running conversion jobs and FFmpeg processes are cleanly terminated and files unlocked.
6. Windows read-only file permissions are handled gracefully.
7. Cascade deletion of teachers cleans up all associated video files on disk.
"""

import os
import sys
import time
import stat
import shutil
import logging

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

from app import app
from extensions import db
from models import Video, User, Institution, ConversionJob, SiteSettings, Quiz
from services.video_cleanup import permanently_delete_video_assets, safe_remove_file, safe_remove_dir
from services.conversion_engine import register_active_process, unregister_active_process, cancel_conversion_jobs_for_video

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TestVideoDeletion")


def setup_test_environment():
    """Ensure clean test database state and upload folders."""
    with app.app_context():
        # Ensure upload directories exist
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        os.makedirs(app.config['HLS_FOLDER'], exist_ok=True)
        os.makedirs(app.config['SUBTITLE_FOLDER'], exist_ok=True)


def test_1_standard_teacher_video_deletion():
    """Test 1: Teacher deleting a completed video removes DB row and ALL files on host disk."""
    print("\n[TEST 1] Standard Teacher Video Deletion via Route...")
    setup_test_environment()

    with app.app_context():
        # Create test teacher
        teacher = User.query.filter_by(username='del_teacher_1').first()
        if not teacher:
            teacher = User(username='del_teacher_1', email='del_teacher1@test.com', role='teacher')
            teacher.set_password('password123')
            db.session.add(teacher)
            db.session.commit()

        # Create dummy physical files
        raw_name = f"test_raw_upload_{int(time.time())}.mp4"
        raw_path = os.path.join(app.config['UPLOAD_FOLDER'], raw_name)
        with open(raw_path, 'w') as f:
            f.write("DUMMY_MP4_RAW_DATA_1234567890")

        # Create video DB record
        video = Video(
            title="Test Standard Deletion Video",
            filename=raw_name,
            uploader_id=teacher.id,
            status='completed',
            processing_progress=100
        )
        db.session.add(video)
        db.session.commit()
        vid = video.id

        # Create HLS directory with master.m3u8, renditions, segments, thumbnail, sprite, and vtt
        hls_dir = os.path.join(app.config['HLS_FOLDER'], str(vid))
        os.makedirs(hls_dir, exist_ok=True)

        master_file = os.path.join(hls_dir, 'master.m3u8')
        with open(master_file, 'w') as f:
            f.write("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\n360p.m3u8\n")

        p360 = os.path.join(hls_dir, '360p.m3u8')
        with open(p360, 'w') as f:
            f.write("#EXTM3U\n#EXTINF:6.0,\n360p_000.ts\n")

        ts_seg = os.path.join(hls_dir, '360p_000.ts')
        with open(ts_seg, 'wb') as f:
            f.write(b"\x47\x00\x00\x10" * 100)

        thumb_file = os.path.join(hls_dir, 'thumbnail.jpg')
        with open(thumb_file, 'wb') as f:
            f.write(b"\xFF\xD8\xFF\xE0" * 50)

        sprite_file = os.path.join(hls_dir, 'sprite.jpg')
        with open(sprite_file, 'wb') as f:
            f.write(b"\xFF\xD8\xFF\xE0" * 50)

        vtt_file = os.path.join(hls_dir, 'thumbnails.vtt')
        with open(vtt_file, 'w') as f:
            f.write("WEBVTT\n00:00.000 --> 00:05.000\nsprite.jpg#xywh=0,0,160,90\n")

        video.hls_playlist_path = f"hls/{vid}/master.m3u8"
        video.master_playlist_path = f"hls/{vid}/master.m3u8"
        video.thumbnail_path = f"hls/{vid}/thumbnail.jpg"
        video.sprite_path = f"hls/{vid}/sprite.jpg"
        video.thumbnails_vtt_path = f"hls/{vid}/thumbnails.vtt"
        db.session.commit()

        teacher_id = teacher.id

        # Verify files exist before deletion
        assert os.path.exists(raw_path), "Raw video file must exist before test"
        assert os.path.exists(hls_dir), "HLS directory must exist before test"
        assert os.path.exists(ts_seg), "TS segment must exist before test"
        assert os.path.exists(master_file), "Master playlist must exist before test"

    # Test via test client calling the route /teacher/delete_video/<vid>
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['_user_id'] = str(teacher_id)
            sess['_fresh'] = True
            sess['csrf_token'] = 'test_valid_csrf_token'

        response = client.post(
            f'/teacher/delete_video/{vid}',
            data={'csrf_token': 'test_valid_csrf_token'},
            headers={'X-CSRF-Token': 'test_valid_csrf_token'},
            follow_redirects=True
        )
        assert response.status_code == 200, f"Route returned status {response.status_code}"

    # Verify post-conditions
    with app.app_context():
        # DB check
        v_check = Video.query.get(vid)
        assert v_check is None, "Video record must be deleted from database!"

        # File system check
        assert not os.path.exists(raw_path), f"Raw video file {raw_path} was NOT deleted permanently from server!"
        assert not os.path.exists(hls_dir), f"HLS directory {hls_dir} was NOT deleted permanently from server!"
        assert not os.path.exists(ts_seg), f"TS segment {ts_seg} was NOT deleted permanently!"

    print("  [PASS] Test 1: Standard video deletion removed DB record and all files/folders from disk.")


def test_2_multitenant_institution_video_deletion():
    """Test 2: Deleting an institution-scoped video removes files from tenant storage & subtitles."""
    print("\n[TEST 2] Multi-Tenant Institution Video & Subtitles Deletion...")
    setup_test_environment()

    with app.app_context():
        # Create test institution
        inst_slug = "tenant_del_test"
        inst = Institution.query.filter_by(slug=inst_slug).first()
        if not inst:
            inst = Institution(name="Tenant Deletion Test Inst", slug=inst_slug, allow_manual_video_delete=True)
            db.session.add(inst)
            db.session.commit()

        teacher = User.query.filter_by(username='tenant_teacher_1').first()
        if not teacher:
            teacher = User(username='tenant_teacher_1', email='tenant_teacher1@test.com', role='teacher', institution_id=inst.id)
            teacher.set_password('password123')
            db.session.add(teacher)
            db.session.commit()

        raw_name = f"tenant_raw_{int(time.time())}.mp4"
        raw_path = os.path.join(app.config['UPLOAD_FOLDER'], raw_name)
        with open(raw_path, 'w') as f:
            f.write("DUMMY_TENANT_MP4_CONTENT")

        video = Video(
            title="Tenant Scoped Video",
            filename=raw_name,
            uploader_id=teacher.id,
            institution_id=inst.id,
            status='completed'
        )
        db.session.add(video)
        db.session.commit()
        vid = video.id

        # Multi-tenant HLS directory
        tenant_hls_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'institutions', inst_slug, 'hls', str(vid))
        os.makedirs(tenant_hls_dir, exist_ok=True)
        tenant_master = os.path.join(tenant_hls_dir, 'master.m3u8')
        with open(tenant_master, 'w') as f:
            f.write("#EXTM3U\n")

        tenant_seg = os.path.join(tenant_hls_dir, '720p_001.ts')
        with open(tenant_seg, 'wb') as f:
            f.write(b"\x47" * 200)

        # Multi-tenant Subtitle file
        tenant_sub_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'institutions', inst_slug, 'subtitles')
        os.makedirs(tenant_sub_dir, exist_ok=True)
        sub_name = f"sub_{vid}_english.vtt"
        tenant_sub_file = os.path.join(tenant_sub_dir, sub_name)
        with open(tenant_sub_file, 'w') as f:
            f.write("WEBVTT\n1\n00:00.000 --> 00:04.000\nHello World\n")

        video.hls_playlist_path = f"uploads/institutions/{inst_slug}/hls/{vid}/master.m3u8"
        video.master_playlist_path = f"uploads/institutions/{inst_slug}/hls/{vid}/master.m3u8"
        video.subtitle_path = f"uploads/institutions/{inst_slug}/subtitles/{sub_name}"
        db.session.commit()

        # Perform cleanup via helper
        res = permanently_delete_video_assets(video)
        assert res['success'], f"Cleanup reported failure: {res}"

        db.session.delete(video)
        db.session.commit()

        # Verify disk
        assert not os.path.exists(raw_path), "Tenant raw video must be deleted from disk"
        assert not os.path.exists(tenant_hls_dir), "Tenant HLS directory must be deleted from disk"
        assert not os.path.exists(tenant_sub_file), "Tenant subtitle file must be deleted from disk"

    print("  [PASS] Test 2: Multi-tenant video, HLS folders, and subtitles permanently deleted from disk.")


def test_3_active_conversion_cancellation_and_cleanup():
    """Test 3: Deleting a video while an active conversion is in progress terminates FFmpeg and cleans up."""
    print("\n[TEST 3] Active Conversion Job Termination & File Cleanup...")
    setup_test_environment()

    with app.app_context():
        teacher = User.query.filter_by(role='teacher').first()
        raw_name = f"active_conv_{int(time.time())}.mp4"
        raw_path = os.path.join(app.config['UPLOAD_FOLDER'], raw_name)
        with open(raw_path, 'w') as f:
            f.write("DUMMY_ACTIVE_CONVERSION_SOURCE")

        video = Video(
            title="Video With Active Conversion",
            filename=raw_name,
            uploader_id=teacher.id,
            status='processing'
        )
        db.session.add(video)
        db.session.commit()
        vid = video.id

        hls_dir = os.path.join(app.config['HLS_FOLDER'], str(vid))
        os.makedirs(hls_dir, exist_ok=True)
        partial_seg = os.path.join(hls_dir, '144p_000.ts')
        with open(partial_seg, 'wb') as f:
            f.write(b"\x47" * 100)

        # Create ConversionJob in processing state
        job = ConversionJob(
            job_id=f"job_{vid}_{int(time.time())}",
            video_id=vid,
            input_file=raw_path,
            output_directory=hls_dir,
            status='processing',
            worker_id='worker-test'
        )
        db.session.add(job)
        db.session.commit()
        jid = job.id

        # Simulate a registered active process
        import subprocess
        # Start a sleeping dummy process to simulate ffmpeg
        dummy_proc = subprocess.Popen(['ping', '-n', '10', '127.0.0.1'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        register_active_process(jid, vid, dummy_proc)

        # Execute permanent cleanup
        cleanup_res = permanently_delete_video_assets(video)

        # Check dummy proc was killed
        time.sleep(0.2)
        assert dummy_proc.poll() is not None, "Active process must be terminated when video is deleted!"

        db.session.delete(video)
        db.session.commit()

        # Verify disk
        assert not os.path.exists(raw_path), "Raw file must be deleted"
        assert not os.path.exists(hls_dir), "HLS output dir must be deleted"
        assert not os.path.exists(partial_seg), "Partial TS segment must be deleted"

        # Verify ConversionJob row was removed or cleaned
        j_check = ConversionJob.query.get(jid)
        assert j_check is None or j_check.status == 'failed', "ConversionJob record must be handled"

    print("  [PASS] Test 3: Active conversion process terminated and all in-flight files deleted cleanly.")


def test_4_windows_readonly_and_permission_safety():
    """Test 4: Files and directories with Windows read-only attributes are forcefully removed."""
    print("\n[TEST 4] Windows Read-Only and Permission Safety...")
    setup_test_environment()

    test_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'readonly_test_dir')
    os.makedirs(test_dir, exist_ok=True)
    ro_file = os.path.join(test_dir, 'locked_file.txt')
    with open(ro_file, 'w') as f:
        f.write("READONLY_TEST_CONTENT")

    # Set file and folder read-only
    os.chmod(ro_file, stat.S_IREAD)
    os.chmod(test_dir, stat.S_IREAD)

    # Call safe_remove_dir
    ok = safe_remove_dir(test_dir)
    assert ok, "safe_remove_dir must succeed on read-only directories"
    assert not os.path.exists(test_dir), "Readonly test directory must be completely removed"

    print("  [PASS] Test 4: Read-only attributes handled properly and files deleted.")


def test_5_teacher_cascade_deletion_cleans_video_files():
    """Test 5: When a Teacher is deleted from DB, SQLAlchemy lifecycle hook cleans video files from disk."""
    print("\n[TEST 5] Teacher Cascade Deletion Cleans Video Files from Disk...")
    setup_test_environment()

    with app.app_context():
        teacher = User(username=f'cascade_teacher_{int(time.time())}', email=f'cascade_{int(time.time())}@test.com', role='teacher')
        teacher.set_password('pass123')
        db.session.add(teacher)
        db.session.commit()
        tid = teacher.id

        # Video 1
        r1 = os.path.join(app.config['UPLOAD_FOLDER'], f'cascade_v1_{tid}.mp4')
        with open(r1, 'w') as f: f.write("CASCADE_V1")
        v1 = Video(title="Cascade V1", filename=os.path.basename(r1), uploader_id=tid)
        db.session.add(v1)
        db.session.commit()
        hls1 = os.path.join(app.config['HLS_FOLDER'], str(v1.id))
        os.makedirs(hls1, exist_ok=True)
        with open(os.path.join(hls1, 'master.m3u8'), 'w') as f: f.write("#EXTM3U")
        v1.hls_playlist_path = f"hls/{v1.id}/master.m3u8"

        # Video 2
        r2 = os.path.join(app.config['UPLOAD_FOLDER'], f'cascade_v2_{tid}.mp4')
        with open(r2, 'w') as f: f.write("CASCADE_V2")
        v2 = Video(title="Cascade V2", filename=os.path.basename(r2), uploader_id=tid)
        db.session.add(v2)
        db.session.commit()
        hls2 = os.path.join(app.config['HLS_FOLDER'], str(v2.id))
        os.makedirs(hls2, exist_ok=True)
        with open(os.path.join(hls2, 'master.m3u8'), 'w') as f: f.write("#EXTM3U")
        v2.hls_playlist_path = f"hls/{v2.id}/master.m3u8"

        db.session.commit()

        # Delete teacher (SQLAlchemy cascade deletes v1 and v2)
        db.session.delete(teacher)
        db.session.commit()

        # Verify disk files for v1 and v2 are completely gone
        assert not os.path.exists(r1), f"Video 1 raw file {r1} must be deleted via cascade"
        assert not os.path.exists(hls1), f"Video 1 HLS dir {hls1} must be deleted via cascade"
        assert not os.path.exists(r2), f"Video 2 raw file {r2} must be deleted via cascade"
        assert not os.path.exists(hls2), f"Video 2 HLS dir {hls2} must be deleted via cascade"

    print("  [PASS] Test 5: Teacher cascade deletion cleanly removed all physical video files from disk.")


def test_6_ubuntu_linux_paths_and_instant_performance():
    """Test 6: Verify zero-lag execution speed (< 500ms) and Linux leading-slash path resolution."""
    print("\n[TEST 6] Ubuntu Linux Paths and Zero-Lag Performance...")
    setup_test_environment()

    with app.app_context():
        teacher = User.query.filter_by(username='del_teacher_1').first()
        vid_num = int(time.time()) + 999

        raw_name = f"ubuntu_test_{vid_num}.mp4"
        raw_path = os.path.join(app.config['UPLOAD_FOLDER'], raw_name)
        with open(raw_path, 'w') as f: f.write("LINUX_UBUNTU_TEST_DATA")

        # Create HLS dir
        hls_dir = os.path.join(app.config['HLS_FOLDER'], str(vid_num))
        os.makedirs(hls_dir, exist_ok=True)
        seg1 = os.path.join(hls_dir, "720p_000.ts")
        with open(seg1, 'wb') as f: f.write(b"\x47" * 500)
        m3u8 = os.path.join(hls_dir, "master.m3u8")
        with open(m3u8, 'w') as f: f.write("#EXTM3U\n")

        # Video with leading-slash and static-prefixed paths (simulating Linux stored paths)
        video = Video(
            id=vid_num,
            title="Linux Path Video",
            filename=raw_name,
            uploader_id=teacher.id,
            hls_playlist_path=f"/static/hls/{vid_num}/master.m3u8",
            master_playlist_path=f"static/hls/{vid_num}/master.m3u8",
            status='completed'
        )
        db.session.add(video)
        db.session.commit()

        start_time = time.time()
        res = permanently_delete_video_assets(video)
        duration = time.time() - start_time

        # Verify performance: must take < 0.5s (no lag)
        assert duration < 0.5, f"Deletion was too slow: took {duration:.3f}s (must be < 0.5s)"
        print(f"  [PERF] Deletion completed in {duration * 1000:.1f}ms (Zero-Lag)")

        # Verify disk files removed
        assert not os.path.exists(raw_path), f"Raw file {raw_path} not deleted"
        assert not os.path.exists(hls_dir), f"HLS directory {hls_dir} not deleted"
        assert not os.path.exists(seg1), f"Segment {seg1} not deleted"

        db.session.delete(video)
        db.session.commit()

    print("  [PASS] Test 6: Ubuntu Linux path variants resolved and deleted in < 500ms.")


def test_7_quiz_disassociation_on_video_delete():
    """Test 7: Deleting a video attached to a Quiz disassociates quiz.video_id without FK errors."""
    print("\n[TEST 7] Quiz Disassociation on Video Deletion...")
    setup_test_environment()

    with app.app_context():
        teacher = User.query.filter_by(username='del_teacher_quiz').first()
        if not teacher:
            teacher = User(username='del_teacher_quiz', email='del_teacher_quiz@test.com', role='teacher')
            teacher.set_password('password123')
            db.session.add(teacher)
            db.session.commit()

        video = Video(title='Quiz Attached Video', filename='quiz_vid_123.mp4', uploader_id=teacher.id, status='completed')
        db.session.add(video)
        db.session.commit()

        quiz = Quiz(title='Video Quiz', teacher_id=teacher.id, video_id=video.id)
        db.session.add(quiz)
        db.session.commit()

        # Delete video via route
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess['_user_id'] = str(teacher.id)
                sess['_fresh'] = True
                sess['csrf_token'] = 'test_csrf'

            del_res = client.post(f'/teacher/delete_video/{video.id}', data={'csrf_token': 'test_csrf'}, follow_redirects=True)
            assert del_res.status_code in (200, 302), f"Expected 200/302, got {del_res.status_code}"

        # Verify video deleted and quiz disassociated cleanly
        v_check = db.session.get(Video, video.id)
        q_check = db.session.get(Quiz, quiz.id)

        assert v_check is None, "Video DB record was not deleted"
        assert q_check is not None, "Quiz record was incorrectly deleted"
        assert q_check.video_id is None, f"Quiz video_id expected None, got {q_check.video_id}"

        db.session.delete(quiz)
        db.session.commit()

    print("  [PASS] Test 7: Video deleted and linked Quiz disassociated cleanly without FK error.")


def test_8_sysadmin_video_deletion():
    """Test 8: System Admin can execute delete_video route without 403 Forbidden."""
    print("\n[TEST 8] System Admin Video Deletion Access...")
    setup_test_environment()

    with app.app_context():
        sysadmin = User.query.filter_by(username='del_sysadmin').first()
        if not sysadmin:
            sysadmin = User(username='del_sysadmin', email='del_sysadmin@test.com', role='system_admin')
            sysadmin.set_password('password123')
            db.session.add(sysadmin)
            db.session.commit()

        video = Video(title='Sysadmin Deletion Video', filename='sysadmin_vid_999.mp4', uploader_id=sysadmin.id, status='completed')
        db.session.add(video)
        db.session.commit()

        vid_id = video.id
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess['_user_id'] = str(sysadmin.id)
                sess['_fresh'] = True
                sess['csrf_token'] = 'test_csrf'

            del_res = client.post(f'/teacher/delete_video/{vid_id}', data={'csrf_token': 'test_csrf'}, follow_redirects=True)
            assert del_res.status_code == 200, f"Expected 200, got {del_res.status_code}"

        v_check = db.session.get(Video, vid_id)
        assert v_check is None, "Video DB record was not deleted by System Admin"

    print("  [PASS] Test 8: System Admin executed delete_video route successfully.")


def run_all_tests():
    print("=" * 65)
    print("CAMPUSPLAYER PERMANENT VIDEO DELETION TEST SUITE")
    print("=" * 65)

    test_1_standard_teacher_video_deletion()
    test_2_multitenant_institution_video_deletion()
    test_3_active_conversion_cancellation_and_cleanup()
    test_4_windows_readonly_and_permission_safety()
    test_5_teacher_cascade_deletion_cleans_video_files()
    test_6_ubuntu_linux_paths_and_instant_performance()
    test_7_quiz_disassociation_on_video_delete()
    test_8_sysadmin_video_deletion()

    print("\n" + "=" * 65)
    print("ALL VIDEO DELETION TESTS PASSED SUCCESSFULLY! (8/8)")
    print("=" * 65)


if __name__ == '__main__':
    run_all_tests()
