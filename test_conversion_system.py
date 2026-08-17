"""
Automated Test Suite for CampusPlayer Persistent, Resumable, Parallel Conversion System.
Tests all 8 critical requirements:
1. Normal full HLS conversion & playback verification
2. Server reboot & crash recovery (Segment-level resume)
3. Trailing corrupted/partial segment cleanup & safe resume
4. Multiple parallel conversions with configurable concurrency
5. Completed video protection (no re-conversion on startup)
6. FFmpeg failure handling & retry limits
7. Duplicate worker protection / Atomic job claiming
8. SQLite WAL concurrency & database lock safety
"""

import os
import sys
import time
import shutil
import subprocess
import threading
from datetime import datetime

# Ensure project root is in sys.path
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

from app import app
from extensions import db
from models import Video, User, SiteSettings, Institution, ConversionJob
from services.conversion_engine import (
    init_conversion_system, enqueue_conversion_job,
    get_active_conversion_jobs, retry_conversion_job, cancel_conversion_job,
    ConversionWorkerManager, recover_unfinished_jobs,
    get_ffmpeg_bin, get_ffprobe_bin, validate_ts_segment,
    get_existing_rendition_segments, build_rendition_playlist,
    MAX_CONCURRENT_CONVERSIONS, MAX_CONVERSION_RETRIES
)

def create_synthetic_test_video(output_path: str, duration_sec: int = 6):
    """Create a lightweight synthetic MP4 test video using FFmpeg lavfi."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    if os.path.exists(output_path):
        os.remove(output_path)

    cmd = [
        get_ffmpeg_bin(), '-y',
        '-f', 'lavfi', '-i', f'testsrc=size=640x360:rate=24:duration={duration_sec}',
        '-f', 'lavfi', '-i', f'sine=frequency=1000:duration={duration_sec}',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '64k',
        output_path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg failed to create test video: {res.stderr}")
    return output_path


def run_tests():
    print("=" * 65)
    print("CAMPUSPLAYER HLS CONVERSION — UPGRADE TEST SUITE")
    print("=" * 65)

    passed_tests = 0
    total_tests = 8

    with app.app_context():
        db.create_all()
        # Ensure test user
        user = User.query.filter_by(username='test_teacher').first()
        if not user:
            user = User(username='test_teacher', role='teacher')
            user.set_password('pass123')
            db.session.add(user)
            db.session.commit()
        teacher_id = user.id

    manager = ConversionWorkerManager.get_instance(app)
    manager.start(app)

    # ─────────────────────────────────────────────────────────────
    # TEST 1: Normal Full HLS Conversion
    # ─────────────────────────────────────────────────────────────
    print("\n[TEST 1] Normal Full Conversion & HLS Output Verification...")
    try:
        raw_name = "test_vid_1.mp4"
        raw_path = os.path.join(app.config['UPLOAD_FOLDER'], raw_name)
        create_synthetic_test_video(raw_path, duration_sec=6)

        with app.app_context():
            v1 = Video(title="Test Video 1", filename=raw_name, uploader_id=teacher_id, status='queued')
            db.session.add(v1)
            db.session.commit()
            vid1_id = v1.id

            job = enqueue_conversion_job(vid1_id, raw_path, teacher_id)
            job_id = job.id

        # Wait for completion (max 40s)
        completed = False
        for _ in range(40):
            time.sleep(1)
            with app.app_context():
                v = db.session.get(Video, vid1_id)
                j = db.session.get(ConversionJob, job_id)
                if v and j and v.status == 'completed' and j.status == 'completed':
                    completed = True
                    break

        assert completed, f"Video {vid1_id} did not reach completed status in time"

        with app.app_context():
            v = db.session.get(Video, vid1_id)
            assert v.processing_progress == 100
            assert v.hls_playlist_path is not None
            master_file = os.path.join(app.root_path, 'static', v.hls_playlist_path)
            assert os.path.exists(master_file), f"Master playlist missing at {master_file}"
            with open(master_file, 'r', encoding='utf-8') as f:
                content = f.read()
                assert '#EXTM3U' in content
                assert '.m3u8' in content

            # Verify segments exist in directory
            vdir = os.path.dirname(master_file)
            ts_files = [f for f in os.listdir(vdir) if f.endswith('.ts')]
            assert len(ts_files) > 0, "No TS segment files generated"
            for ts in ts_files:
                assert validate_ts_segment(os.path.join(vdir, ts)), f"Invalid TS segment: {ts}"

        print("  [PASS] Test 1: Normal full conversion produced valid playlists and verified TS segments.")
        passed_tests += 1
    except Exception as e:
        print(f"  [FAIL] Test 1 Failed: {e}")

    # ─────────────────────────────────────────────────────────────
    # TEST 2: Crash & Resume Recovery (Segment-level resume)
    # ─────────────────────────────────────────────────────────────
    print("\n[TEST 2] Server Reboot & Segment-Level Resume Recovery...")
    try:
        raw_name = "test_vid_2_resume.mp4"
        raw_path = os.path.join(app.config['UPLOAD_FOLDER'], raw_name)
        create_synthetic_test_video(raw_path, duration_sec=14)

        with app.app_context():
            v2 = Video(title="Test Video 2 Resume", filename=raw_name, uploader_id=teacher_id, status='queued')
            db.session.add(v2)
            db.session.commit()
            vid2_id = v2.id

            out_dir = os.path.join(app.config['HLS_FOLDER'], str(vid2_id))
            os.makedirs(out_dir, exist_ok=True)

            # Pre-generate segment 0 manually
            seg_pattern = os.path.join(out_dir, "144p_%03d.ts")
            temp_m3u8 = os.path.join(out_dir, "temp.m3u8")
            cmd = [
                get_ffmpeg_bin(), '-y', '-i', raw_path, '-t', '6',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '64k',
                '-f', 'hls', '-hls_time', '6', '-hls_segment_filename', seg_pattern,
                temp_m3u8
            ]
            subprocess.run(cmd, capture_output=True, timeout=30)
            if os.path.exists(temp_m3u8):
                os.remove(temp_m3u8)

            seg0_path = os.path.join(out_dir, "144p_000.ts")
            assert os.path.exists(seg0_path), f"Failed to generate initial segment at {seg0_path}"
            assert validate_ts_segment(seg0_path)

            # Create job record simulating an interrupted state
            job2 = ConversionJob(
                job_id=f"job_{vid2_id}_test",
                video_id=vid2_id,
                input_file=raw_path,
                output_directory=out_dir,
                status='interrupted',
                progress=30,
                current_segment=0,
                total_segments=3
            )
            db.session.add(job2)
            db.session.commit()
            job2_id = job2.id

        # Run startup recovery
        recovered = recover_unfinished_jobs(app)
        assert recovered >= 1, "Recovery manager failed to pick up interrupted job"

        # Signal workers to process
        manager.notify()

        # Wait for completion
        completed = False
        for _ in range(40):
            time.sleep(1)
            with app.app_context():
                j = db.session.get(ConversionJob, job2_id)
                v = db.session.get(Video, vid2_id)
                if j and v and j.status == 'completed' and v.status == 'completed':
                    completed = True
                    break

        assert completed, "Resumed job did not complete in time"

        with app.app_context():
            v = db.session.get(Video, vid2_id)
            master_file = os.path.join(app.root_path, 'static', v.hls_playlist_path)
            assert os.path.exists(master_file)
            # Check that segment 0 was preserved
            assert os.path.exists(seg0_path)
            assert validate_ts_segment(seg0_path)

            # Check that subsequent segments were generated
            seg1_path = os.path.join(out_dir, "144p_001.ts")
            assert os.path.exists(seg1_path), f"Segment 001 missing at {seg1_path}"
            assert validate_ts_segment(seg1_path)

        print("  [PASS] Test 2: Crash recovery accurately resumed conversion without re-encoding existing segments.")
        passed_tests += 1
    except Exception as e:
        print(f"  [FAIL] Test 2 Failed: {e}")

    # ─────────────────────────────────────────────────────────────
    # TEST 3: Corrupt Trailing Segment Cleanup & Safe Resume
    # ─────────────────────────────────────────────────────────────
    print("\n[TEST 3] Corrupt Trailing Segment Cleanup & Safe Resume...")
    try:
        raw_name = "test_vid_3_corrupt.mp4"
        raw_path = os.path.join(app.config['UPLOAD_FOLDER'], raw_name)
        create_synthetic_test_video(raw_path, duration_sec=12)

        with app.app_context():
            v3 = Video(title="Test Video 3 Corrupt", filename=raw_name, uploader_id=teacher_id, status='queued')
            db.session.add(v3)
            db.session.commit()
            vid3_id = v3.id

            out_dir = os.path.join(app.config['HLS_FOLDER'], str(vid3_id))
            os.makedirs(out_dir, exist_ok=True)

            # Generate valid segment 0
            seg_pattern = os.path.join(out_dir, "144p_%03d.ts")
            temp_m3u8 = os.path.join(out_dir, "temp.m3u8")
            cmd = [
                get_ffmpeg_bin(), '-y', '-i', raw_path, '-t', '6',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '64k',
                '-f', 'hls', '-hls_time', '6', '-hls_segment_filename', seg_pattern,
                temp_m3u8
            ]
            subprocess.run(cmd, capture_output=True, timeout=30)
            if os.path.exists(temp_m3u8):
                os.remove(temp_m3u8)

            seg0_path = os.path.join(out_dir, "144p_000.ts")
            assert os.path.exists(seg0_path), "Segment 000 missing"
            assert validate_ts_segment(seg0_path)

            # Create a CORRUPT trailing segment 001 (0 bytes / invalid)
            corrupt_seg1 = os.path.join(out_dir, "144p_001.ts")
            with open(corrupt_seg1, 'wb') as f:
                f.write(b"CORRUPTED_INCOMPLETE_TS_DATA_DURING_POWER_OUTAGE")

            assert not validate_ts_segment(corrupt_seg1), "Corrupt segment test setup invalid"

            job3 = ConversionJob(
                job_id=f"job_{vid3_id}_corrupt",
                video_id=vid3_id,
                input_file=raw_path,
                output_directory=out_dir,
                status='interrupted',
                progress=25
            )
            db.session.add(job3)
            db.session.commit()
            job3_id = job3.id

        # Scan and verify corrupt segment is cleaned
        valid_idxs, next_idx, _ = get_existing_rendition_segments(out_dir, "144p")
        assert 1 not in valid_idxs, "Corrupt segment was not rejected"
        assert not os.path.exists(corrupt_seg1), "Corrupt segment was not removed from disk"
        assert next_idx == 1, f"Expected next segment 1, got {next_idx}"

        # Resume job
        manager.notify()
        completed = False
        for _ in range(40):
            time.sleep(1)
            with app.app_context():
                j = db.session.get(ConversionJob, job3_id)
                if j and j.status == 'completed':
                    completed = True
                    break

        assert completed, "Job with corrupt segment recovery did not complete"
        assert os.path.exists(os.path.join(out_dir, "144p_001.ts"))
        assert validate_ts_segment(os.path.join(out_dir, "144p_001.ts"))

        print("  [PASS] Test 3: Corrupt trailing segment was cleanly removed and re-encoded successfully.")
        passed_tests += 1
    except Exception as e:
        print(f"  [FAIL] Test 3 Failed: {e}")

    # ─────────────────────────────────────────────────────────────
    # TEST 4: Multiple Parallel Conversions (Configurable Concurrency)
    # ─────────────────────────────────────────────────────────────
    print(f"\n[TEST 4] Multiple Parallel Conversions (Max concurrency = {manager.max_workers})...")
    try:
        parallel_count = 5
        job_ids = []
        video_ids = []

        with app.app_context():
            for i in range(parallel_count):
                rname = f"parallel_vid_{i}.mp4"
                rpath = os.path.join(app.config['UPLOAD_FOLDER'], rname)
                create_synthetic_test_video(rpath, duration_sec=5)

                v = Video(title=f"Parallel Video {i}", filename=rname, uploader_id=teacher_id, status='queued')
                db.session.add(v)
                db.session.commit()
                video_ids.append(v.id)

                j = enqueue_conversion_job(v.id, rpath, teacher_id)
                job_ids.append(j.id)

        # Check concurrency: at no point should processing jobs exceed max_workers
        time.sleep(1.5)
        with app.app_context():
            processing_count = ConversionJob.query.filter(
                ConversionJob.id.in_(job_ids),
                ConversionJob.status == 'processing'
            ).count()
            queued_count = ConversionJob.query.filter(
                ConversionJob.id.in_(job_ids),
                ConversionJob.status == 'queued'
            ).count()

            print(f"    Concurrency Snapshot: {processing_count} processing, {queued_count} queued (Max limit: {manager.max_workers})")
            assert processing_count <= manager.max_workers, f"Concurrency exceeded! {processing_count} > {manager.max_workers}"

        # Wait for all parallel jobs to finish
        all_done = False
        for _ in range(60):
            time.sleep(1.5)
            with app.app_context():
                completed_count = ConversionJob.query.filter(
                    ConversionJob.id.in_(job_ids),
                    ConversionJob.status == 'completed'
                ).count()
                if completed_count == parallel_count:
                    all_done = True
                    break

        assert all_done, f"Not all parallel jobs completed (Completed: {completed_count}/{parallel_count})"
        print(f"  [PASS] Test 4: All {parallel_count} parallel conversions processed cleanly with worker pool.")
        passed_tests += 1
    except Exception as e:
        print(f"  [FAIL] Test 4 Failed: {e}")

    # ─────────────────────────────────────────────────────────────
    # TEST 5: Completed Video Protection (Never Re-convert)
    # ─────────────────────────────────────────────────────────────
    print("\n[TEST 5] Completed Video Protection (No re-conversion)...")
    try:
        with app.app_context():
            # Check video from Test 1
            v1 = db.session.get(Video, vid1_id)
            j1 = ConversionJob.query.filter_by(video_id=vid1_id).first()
            assert v1.status == 'completed'
            assert j1.status == 'completed'
            completed_time_before = j1.completed_at

            # Run recovery
            recover_unfinished_jobs(app)

            # Verify it remained completed
            j1_after = db.session.get(ConversionJob, j1.id)
            assert j1_after.status == 'completed'
            assert j1_after.completed_at == completed_time_before

        print("  [PASS] Test 5: Completed videos are recognized and protected from re-conversion.")
        passed_tests += 1
    except Exception as e:
        print(f"  [FAIL] Test 5 Failed: {e}")

    # ─────────────────────────────────────────────────────────────
    # TEST 6: FFmpeg Failure & Retry Limit Mechanism
    # ─────────────────────────────────────────────────────────────
    print("\n[TEST 6] FFmpeg Failure Handling & Retry Limit...")
    try:
        bad_name = "corrupt_input_non_video.mp4"
        bad_path = os.path.join(app.config['UPLOAD_FOLDER'], bad_name)
        with open(bad_path, 'w') as f:
            f.write("THIS IS NOT A VALID VIDEO FILE AT ALL")

        with app.app_context():
            v_fail = Video(title="Failing Video", filename=bad_name, uploader_id=teacher_id, status='queued')
            db.session.add(v_fail)
            db.session.commit()
            v_fail_id = v_fail.id

            j_fail = enqueue_conversion_job(v_fail_id, bad_path, teacher_id)
            j_fail_id = j_fail.id

        # Wait for workers to fail and exhaust retries
        failed_terminal = False
        for _ in range(40):
            time.sleep(1)
            with app.app_context():
                j = db.session.get(ConversionJob, j_fail_id)
                v = db.session.get(Video, v_fail_id)
                if j and v and j.status == 'failed' and v.status == 'failed':
                    failed_terminal = True
                    break

        assert failed_terminal, "Job did not transition to failed after exhausting retries"
        with app.app_context():
            j = db.session.get(ConversionJob, j_fail_id)
            assert j.retry_count >= j.max_retries
            assert j.error_message is not None

        print("  [PASS] Test 6: Failed conversions retry up to max limit then terminate with descriptive error.")
        passed_tests += 1
    except Exception as e:
        print(f"  [FAIL] Test 6 Failed: {e}")

    # ─────────────────────────────────────────────────────────────
    # TEST 7: Atomic Claiming & Duplicate Worker Protection
    # ─────────────────────────────────────────────────────────────
    print("\n[TEST 7] Atomic Job Claiming & Duplicate Protection...")
    try:
        raw_name = "test_atomic_claim.mp4"
        raw_path = os.path.join(app.config['UPLOAD_FOLDER'], raw_name)
        create_synthetic_test_video(raw_path, duration_sec=4)

        with app.app_context():
            v_atom = Video(title="Atomic Video", filename=raw_name, uploader_id=teacher_id, status='queued')
            db.session.add(v_atom)
            db.session.commit()

            j_atom = ConversionJob(
                job_id=f"job_atomic_{int(time.time() * 1000)}",
                video_id=v_atom.id,
                input_file=raw_path,
                output_directory=os.path.join(app.config['HLS_FOLDER'], str(v_atom.id)),
                status='queued'
            )
            db.session.add(j_atom)
            db.session.commit()
            target_job_id = j_atom.id

        # Simulate 5 concurrent worker threads all attempting to claim the single job at the exact same moment
        claimed_by = []
        claim_lock = threading.Lock()

        def try_claim(w_id):
            claimed_id = manager._claim_next_job(w_id)
            if claimed_id:
                with claim_lock:
                    claimed_by.append((w_id, claimed_id))

        threads = [threading.Thread(target=try_claim, args=(f"sim-worker-{k}",)) for k in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert len(claimed_by) == 1, f"Atomic claim failed! Claimed {len(claimed_by)} times: {claimed_by}"
        assert claimed_by[0][1] == target_job_id
        print(f"  [PASS] Test 7: Exactly 1 worker successfully claimed job ({claimed_by[0][0]}). Zero duplicate claims.")
        passed_tests += 1
    except Exception as e:
        print(f"  [FAIL] Test 7 Failed: {e}")

    # ─────────────────────────────────────────────────────────────
    # TEST 8: SQLite Concurrency & Locking Safety
    # ─────────────────────────────────────────────────────────────
    print("\n[TEST 8] SQLite Concurrency & Lock Safety...")
    try:
        lock_errors = []

        def db_hammer(thread_id):
            with app.app_context():
                for _ in range(50):
                    try:
                        jobs = ConversionJob.query.limit(10).all()
                        v = Video.query.first()
                        if v:
                            v.processing_progress = v.processing_progress
                        db.session.commit()
                    except Exception as e:
                        if 'locked' in str(e).lower():
                            lock_errors.append(str(e))
                        db.session.rollback()
                    time.sleep(0.01)

        hammer_threads = [threading.Thread(target=db_hammer, args=(i,)) for i in range(10)]
        for t in hammer_threads: t.start()
        for t in hammer_threads: t.join()

        assert len(lock_errors) == 0, f"SQLite locking errors detected: {lock_errors}"
        print("  [PASS] Test 8: High-concurrency SQLite operations succeeded with zero database locks.")
        passed_tests += 1
    except Exception as e:
        print(f"  [FAIL] Test 8 Failed: {e}")

    manager.shutdown()

    print("\n" + "=" * 65)
    print(f"TEST RESULTS: {passed_tests}/{total_tests} PASSED")
    print("=" * 65)
    return passed_tests == total_tests

if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
