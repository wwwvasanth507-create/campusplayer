"""
Automated Test Suite for Fast Parallel Chunk Upload & Server Reception
Validates:
1. High-speed multi-threaded concurrent chunk uploads.
2. Out-of-order chunk arrival (e.g. chunk 3 before chunk 1).
3. Metadata persistence and video record creation upon assembly.
4. 16MB buffer fast assembly and instant chunk cleanup.
"""

import os
import sys
import io
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

from app import app
from extensions import db
from models import Video, User, Institution, ConversionJob

def test_fast_chunk_upload_flow():
    print("\n[TEST] Fast Parallel Chunk Upload Flow...")
    with app.app_context():
        teacher = User.query.filter_by(username='del_teacher_1').first()
        if not teacher:
            teacher = User(username='del_teacher_1', email='del_teacher1@test.com', role='teacher')
            teacher.set_password('password123')
            db.session.add(teacher)
            db.session.commit()
        teacher_id = teacher.id

    upload_uuid = str(uuid.uuid4())
    total_chunks = 6
    chunk_size = 1024 * 1024  # 1 MB per chunk = 6 MB test file
    full_payload = b"PARALLEL_STREAM_TEST_DATA_" * (total_chunks * chunk_size // 26)

    # Chunk chunks in memory
    chunks = [
        full_payload[i * chunk_size : (i + 1) * chunk_size]
        for i in range(total_chunks)
    ]

    # Upload chunks concurrently in parallel out-of-order
    upload_order = [2, 0, 4, 1, 5, 3] # Out-of-order delivery
    responses = {}

    def upload_single_chunk(idx):
        with app.test_client() as thread_client:
            with thread_client.session_transaction() as sess:
                sess['_user_id'] = str(teacher_id)
                sess['_fresh'] = True
                sess['csrf_token'] = 'test_csrf'

            data = {
                'chunk': (io.BytesIO(chunks[idx]), f"chunk_{idx}.bin"),
                'chunkIndex': idx,
                'totalChunks': total_chunks,
                'uuid': upload_uuid,
                'filename': 'fast_parallel_video.mp4',
                'title': 'High Speed Parallel Video',
                'description': 'Testing fast chunk upload',
                'tags': 'speed,parallel,hls',
                'classroom_id': '-1',
                'csrf_token': 'test_csrf'
            }
            resp = thread_client.post(
                '/teacher/upload_chunk',
                data=data,
                headers={'X-CSRF-Token': 'test_csrf'},
                content_type='multipart/form-data'
            )
            return idx, resp.status_code, resp.get_json()

    start_time = time.time()
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(upload_single_chunk, idx) for idx in upload_order]
        for f in futures:
            idx, status, json_data = f.result()
            responses[idx] = (status, json_data)

    duration = time.time() - start_time
    print(f"  [PERF] 6 chunks uploaded and assembled concurrently in {duration * 1000:.1f}ms")

    # Verify all chunks succeeded
    for idx in range(total_chunks):
        status, json_data = responses[idx]
        assert status == 200, f"Chunk {idx} failed with status {status}: {json_data}"
        assert json_data.get('success') is True, f"Chunk {idx} returned error: {json_data}"

    # At least one response (the completing chunk or subsequent concurrent thread) should contain video_id
    completion_responses = [resp_json for _, (_, resp_json) in responses.items() if 'video_id' in resp_json]
    assert len(completion_responses) >= 1, f"Expected at least 1 completion response, got {len(completion_responses)}"
    
    vid = completion_responses[0]['video_id']
    assert vid is not None, "video_id must not be None"

    # Verify Database and File System
    with app.app_context():
        created_video = Video.query.get(vid)
        assert created_video is not None, "Video record must exist in database"
        assert created_video.title == "High Speed Parallel Video"
        assert created_video.tags == "speed,parallel,hls"
        assert created_video.status in ('queued', 'processing', 'completed')

        # Check assembled file on disk (poll for async background assembly completion)
        target_path = ""
        for _ in range(30):
            db.session.refresh(created_video)
            target_path = os.path.join(app.config['UPLOAD_FOLDER'], created_video.filename)
            if os.path.exists(target_path) and os.path.getsize(target_path) == len(full_payload):
                break
            time.sleep(0.1)
        assert os.path.exists(target_path), f"Assembled file {target_path} must exist on disk"
        
        with open(target_path, 'rb') as f:
            assembled_content = f.read()
        assert assembled_content == full_payload, "Assembled video content does not match original bytes!"

        # Chunks directory must be cleaned up
        chunks_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'chunks', upload_uuid)
        assert not os.path.exists(chunks_dir), f"Chunks directory {chunks_dir} should be cleaned up after assembly"

        # Cleanup test video
        from services.video_cleanup import permanently_delete_video_assets
        permanently_delete_video_assets(created_video)
        db.session.delete(created_video)
        db.session.commit()

    print("  [PASS] Fast Parallel Chunk Upload Flow test passed successfully!")

if __name__ == '__main__':
    test_fast_chunk_upload_flow()
