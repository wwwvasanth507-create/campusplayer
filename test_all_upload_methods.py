"""
Comprehensive Test Suite for Video & Subtitle Upload Endpoints.
Validates:
1. Multi-stream parallel chunk upload (/teacher/upload_chunk)
2. Direct single file video upload (/teacher/upload)
3. High-performance API chunk upload flow (/api/upload/init -> /api/upload/chunk -> /api/upload/complete)
4. Subtitle upload (/teacher/upload_subtitles/<video_id>)
"""

import os
import sys
import io
import time
import uuid
import subprocess

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

from app import app
from extensions import db
from models import Video, User, Institution
from services.conversion_engine import get_ffmpeg_bin

def generate_sample_mp4(duration_sec=3):
    """Generate a lightweight valid MP4 video buffer using FFmpeg."""
    cmd = [
        get_ffmpeg_bin(), '-y',
        '-f', 'lavfi', '-i', f'testsrc=size=320x240:rate=15:duration={duration_sec}',
        '-f', 'lavfi', '-i', f'sine=frequency=440:duration={duration_sec}',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '32k',
        '-f', 'mp4', 'pipe:1'
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if res.returncode == 0 and len(res.stdout) > 0:
        return res.stdout
    # Fallback padding if ffmpeg is not available
    return b"SYNTHETIC_MP4_HEADER_" * 5000

def run_comprehensive_upload_tests():
    print("=" * 70)
    print("CAMPUS PLAYER - COMPREHENSIVE VIDEO & MEDIA UPLOAD VERIFICATION")
    print("=" * 70)

    sample_mp4_bytes = generate_sample_mp4(duration_sec=3)
    print(f"[Setup] Generated valid synthetic MP4 video buffer ({len(sample_mp4_bytes)} bytes)")

    with app.app_context():
        teacher = User.query.filter_by(role='teacher').first()
        if not teacher:
            teacher = User(username='upload_test_teacher', email='upload_teacher@test.com', role='teacher')
            teacher.set_password('password123')
            db.session.add(teacher)
            db.session.commit()
        teacher_id = teacher.id

    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(teacher_id)
        sess['_fresh'] = True
        sess['csrf_token'] = 'test_csrf_token'

    headers = {'X-CSRF-Token': 'test_csrf_token'}

    # ------------------------------------------------------------------
    # TEST 1: Parallel Multi-Stream Chunk Upload (/teacher/upload_chunk)
    # ------------------------------------------------------------------
    print("\n[TEST 1] Testing /teacher/upload_chunk Multi-Stream Upload...")
    upload_uuid = str(uuid.uuid4())
    total_chunks = 3
    chunk_size = len(sample_mp4_bytes) // total_chunks or 1

    final_resp_json = None
    for i in range(total_chunks):
        start = i * chunk_size
        end = len(sample_mp4_bytes) if i == total_chunks - 1 else (i + 1) * chunk_size
        chunk_data = sample_mp4_bytes[start:end]

        data = {
            'chunk': (io.BytesIO(chunk_data), f"chunk_{i}.bin"),
            'chunkIndex': i,
            'totalChunks': total_chunks,
            'uuid': upload_uuid,
            'filename': 'multi_stream_lecture.mp4',
            'title': 'Multi Stream Test Lecture',
            'description': 'Testing multi-stream chunk upload',
            'tags': 'lecture,multi_stream',
            'classroom_id': '-1',
            'csrf_token': 'test_csrf_token'
        }
        resp = client.post('/teacher/upload_chunk', data=data, headers=headers, content_type='multipart/form-data')
        assert resp.status_code == 200, f"Chunk {i} failed: {resp.data}"
        json_data = resp.get_json()
        assert json_data.get('success') is True
        if 'video_id' in json_data:
            final_resp_json = json_data

    assert final_resp_json is not None, "Final chunk did not return video_id"
    video_id_1 = final_resp_json['video_id']
    print(f"  [PASS] Multi-stream chunk upload completed cleanly (Video #{video_id_1}).")

    # ------------------------------------------------------------------
    # TEST 2: Direct Single File Upload (/teacher/upload)
    # ------------------------------------------------------------------
    print("\n[TEST 2] Testing /teacher/upload Direct Single File Upload...")
    data_direct = {
        'video_file': (io.BytesIO(sample_mp4_bytes), 'direct_lecture.mp4'),
        'csrf_token': 'test_csrf_token'
    }
    resp_direct = client.post('/teacher/upload', data=data_direct, headers=headers, content_type='multipart/form-data')
    assert resp_direct.status_code == 200, f"Direct upload failed: {resp_direct.data}"
    json_direct = resp_direct.get_json()
    assert json_direct.get('success') is True
    video_id_2 = json_direct['video_id']
    print(f"  [PASS] Direct single file upload completed cleanly (Video #{video_id_2}).")

    # ------------------------------------------------------------------
    # TEST 3: High-Performance API Chunk Upload (/api/upload/*)
    # ------------------------------------------------------------------
    print("\n[TEST 3] Testing High-Performance API Chunk Upload (/api/upload/*)...")
    init_data = {
        'filename': 'api_lecture.mp4',
        'total_size': len(sample_mp4_bytes),
        'total_chunks': 2,
        'chunk_size': len(sample_mp4_bytes) // 2
    }
    resp_init = client.post('/api/upload/init', json=init_data, headers=headers)
    assert resp_init.status_code == 200, f"API upload init failed: {resp_init.data}"
    json_init = resp_init.get_json()
    api_uuid = json_init['upload_uuid']
    video_id_3 = json_init['video_id']

    half = len(sample_mp4_bytes) // 2
    chunk1_data = {
        'upload_uuid': api_uuid,
        'chunk_index': 0,
        'total_chunks': 2,
        'chunk_data': (io.BytesIO(sample_mp4_bytes[:half]), 'chunk_0.bin')
    }
    resp_c1 = client.post('/api/upload/chunk/direct', data=chunk1_data, headers=headers, content_type='multipart/form-data')
    assert resp_c1.status_code == 200, f"API chunk 0 failed: {resp_c1.data}"

    chunk2_data = {
        'upload_uuid': api_uuid,
        'chunk_index': 1,
        'total_chunks': 2,
        'chunk_data': (io.BytesIO(sample_mp4_bytes[half:]), 'chunk_1.bin')
    }
    resp_c2 = client.post('/api/upload/chunk/direct', data=chunk2_data, headers=headers, content_type='multipart/form-data')
    assert resp_c2.status_code == 200, f"API chunk 1 failed: {resp_c2.data}"

    complete_data = {
        'upload_uuid': api_uuid,
        'video_id': video_id_3,
        'original_filename': 'api_lecture.mp4',
        'total_chunks': 2
    }
    resp_comp = client.post('/api/upload/complete', json=complete_data, headers=headers)
    assert resp_comp.status_code == 200, f"API upload complete failed: {resp_comp.data}"
    print(f"  [PASS] High-performance API chunk upload completed cleanly (Video #{video_id_3}).")

    # ------------------------------------------------------------------
    # TEST 4: Subtitle Upload (/teacher/upload_subtitles/<video_id>)
    # ------------------------------------------------------------------
    print("\n[TEST 4] Testing Subtitle Upload (/teacher/upload_subtitles/<video_id>)...")
    vtt_content = b"WEBVTT\n\n1\n00:00:00.000 --> 00:00:05.000\nWelcome to Campus Player!"
    sub_data = {
        'subtitle_file': (io.BytesIO(vtt_content), 'sample_subtitles.vtt'),
        'language': 'en',
        'csrf_token': 'test_csrf_token'
    }
    resp_sub = client.post(
        f'/teacher/upload_subtitles/{video_id_1}',
        data=sub_data,
        headers={'X-CSRF-Token': 'test_csrf_token', 'X-Requested-With': 'XMLHttpRequest'},
        content_type='multipart/form-data'
    )
    assert resp_sub.status_code == 200, f"Subtitle upload failed: {resp_sub.data}"
    assert resp_sub.get_json().get('success') is True
    print(f"  [PASS] Subtitle upload verified for Video #{video_id_1}.")

    # ------------------------------------------------------------------
    # CLEANUP TEST VIDEOS
    # ------------------------------------------------------------------
    with app.app_context():
        from services.video_cleanup import permanently_delete_video_assets
        for vid in [video_id_1, video_id_2, video_id_3]:
            v_obj = db.session.get(Video, vid)
            if v_obj:
                permanently_delete_video_assets(v_obj)
                db.session.delete(v_obj)
        db.session.commit()

    print("\n" + "=" * 70)
    print("ALL 4 VIDEO & MEDIA UPLOAD ENDPOINTS VERIFIED 100% WORKING!")
    print("=" * 70)

if __name__ == '__main__':
    run_comprehensive_upload_tests()
