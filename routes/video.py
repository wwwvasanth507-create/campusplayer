import os
import json
import time
import threading
import uuid
import subprocess
import shutil
import logging
from datetime import datetime
from functools import wraps
from flask import Blueprint, render_template, request, jsonify, url_for, abort, current_app, flash, redirect, send_file, make_response
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from extensions import db, limiter
from models import Video, Playlist, Comment, SiteSettings, VideoLike, User, Institution
from services.utils import sanitize_input, allowed_file, allowed_subtitle_file, apply_media_cors_headers
from services.auth import teacher_required, log_activity
from services.upload_engine import (
    init_upload_engine, handle_chunk_upload, handle_chunk_upload_direct,
    assemble_chunks, create_batch_video_jobs, process_batch_videos,
    get_upload_job, get_all_upload_jobs, get_batch_progress,
    get_chunk_writer_stats, get_overall_stats, process_video_to_hls,
    VideoJob, VideoJobStatus, _job_registry, _job_registry_lock,
    UPLOAD_CHUNKS_DIR, _chunk_buffer
)
from services.ultra_parallel_processor import (
    process_video_ultra, process_video_ultra_async,
    get_job_status as get_ultra_job_status,
    process_batch_ultra, get_processor, ULTRA_QUALITY_LADDER,
    shutdown_processor as shutdown_ultra_processor
)

logger = logging.getLogger(__name__)

video_bp = Blueprint('video', __name__)


@video_bp.route('/watch/<int:video_id>', endpoint='watch_video')
@video_bp.route('/video/<int:video_id>')
@login_required
def watch_video(video_id):
    video = Video.query.get_or_404(video_id)
    enforce_institution_access(video)
    video.view_count = (video.view_count or 0) + 1

    if not video.hls_playlist_path and video.master_playlist_path:
        video.hls_playlist_path = video.master_playlist_path

    db.session.commit()

    rel_q = Video.query.filter(Video.uploader_id == video.uploader_id, Video.id != video.id)
    related_videos = scope_to_institution(rel_q, Video).limit(5).all()
    top_level_comments = Comment.query.filter_by(video_id=video_id, parent_id=None).order_by(Comment.timestamp.desc()).all()
    settings = SiteSettings.query.first()
    user_liked = VideoLike.query.filter_by(user_id=current_user.id, video_id=video_id).first() is not None

    # Compute hls_source here so the template gets it directly
    hls_source = (video.hls_playlist_path or video.master_playlist_path) if video.status == 'completed' else None

    # Build the hls_url from the video's stored playlist path
    hls_url = ''
    if hls_source:
        # hls_source is a relative path like:
        #   hls/<id>/master.m3u8                          (legacy)
        #   uploads/institutions/<slug>/hls/<id>/master.m3u8  (institution)
        # The serve_hls endpoint expects just the filename portion after the video dir
        # We pass the full relative path as the filename so serve_hls can resolve it.
        hls_url = url_for('serve_hls', video_id=video_id, filename=os.path.basename(hls_source))

    return render_template('video_player.html',
        video=video,
        related_videos=related_videos,
        comments=top_level_comments,
        settings=settings,
        user_liked=user_liked,
        hls_source=hls_source,
        hls_url=hls_url,
    )


def serve_hls_file(video_dir, filename):
    file_path = os.path.join(video_dir, filename)
    if not os.path.realpath(file_path).startswith(os.path.realpath(video_dir)):
        return jsonify({'error': 'Forbidden'}), 403
    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        return jsonify({'error': 'File not found'}), 404

    ext = os.path.splitext(filename)[1].lower()
    content_types = {
        '.m3u8': 'application/vnd.apple.mpegurl',
        '.ts': 'video/mp2t',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.vtt': 'text/vtt; charset=utf-8',
        '.webvtt': 'text/vtt; charset=utf-8',
    }
    content_type = content_types.get(ext, 'application/octet-stream')

    response = make_response(send_file(file_path, mimetype=content_type, conditional=True))
    response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    response = apply_media_cors_headers(response)
    response.headers['Access-Control-Expose-Headers'] = 'Content-Length, Content-Range'
    response.headers['Cross-Origin-Resource-Policy'] = 'cross-origin'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    if ext == '.ts':
        response.headers['Accept-Ranges'] = 'bytes'
    return response


@video_bp.route('/hls/<int:video_id>/<path:filename>', endpoint='serve_hls')
@login_required
def serve_hls(video_id, filename):
    """Serve HLS segments and playlists.
    Resolves the video directory from the stored hls_playlist_path in the DB,
    supporting both legacy (static/hls/<id>/) and institution-based
    (static/uploads/institutions/<slug>/hls/<id>/) storage.
    """
    video = Video.query.get(video_id)
    if not video:
        return jsonify({'error': 'Video not found'}), 404
    enforce_institution_access(video)
    static_dir = os.path.join(current_app.root_path, 'static')

    # Try to derive video_dir from the stored hls_playlist_path
    video_dir = None
    if video and (video.hls_playlist_path or video.master_playlist_path):
        stored_path = video.hls_playlist_path or video.master_playlist_path
        # stored_path is relative to static/, e.g. "hls/3/master.m3u8"
        # or "uploads/institutions/abc/hls/3/master.m3u8"
        full_stored = os.path.join(static_dir, stored_path)
        video_dir = os.path.dirname(full_stored)

    # Fall back to legacy location: static/hls/<video_id>/
    if not video_dir or not os.path.exists(video_dir):
        video_dir = os.path.join(current_app.config['HLS_FOLDER'], str(video_id))

    if not os.path.exists(video_dir):
        return jsonify({'error': 'Video not found'}), 404

    return serve_hls_file(video_dir, filename)


@video_bp.route('/playlist/<int:playlist_id>')
@login_required
def view_playlist(playlist_id):
    playlist = Playlist.query.get_or_404(playlist_id)
    enforce_institution_access(playlist)
    return render_template('playlist_view.html', playlist=playlist)


@video_bp.route('/teacher/upload_chunk', methods=['POST'])
def upload_chunk():
    uuid_str = request.form.get('uuid')
    total_chunks = int(request.form.get('total_chunks', 0))
    chunk = request.files.get('file')
    if not uuid_str or not chunk:
        return jsonify({'success': False, 'message': 'Missing upload chunk.'}), 400
    chunks_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'chunks', uuid_str)
    os.makedirs(chunks_dir, exist_ok=True)
    chunk_path = os.path.join(chunks_dir, secure_filename(chunk.filename))
    chunk.save(chunk_path)
    return jsonify({'success': True})


def _update_video_progress(video_id: int, progress: float):
    """Update video processing progress in database."""
    try:
        video = Video.query.get(video_id)
        if video:
            video.processing_progress = min(99, int(progress))
            db.session.commit()
    except Exception as e:
        logger.warning(f"Progress update failed: {e}")


def _update_video_record(video_id: int, result: dict):
    """Update video record with complete HLS processing results."""
    try:
        video = Video.query.get(video_id)
        if not video:
            return

        video.status = 'completed'
        video.processing_progress = 100
        video.has_adaptive_streams = True

        master_playlist = result.get('master_playlist', 'master.m3u8')
        thumbnail = result.get('thumbnail', '')
        renditions = result.get('renditions', [])

        uploader = User.query.get(video.uploader_id)
        if uploader and uploader.institution_id:
            inst = Institution.query.get(uploader.institution_id)
            if inst:
                rel_base = f"uploads/institutions/{inst.slug}/hls/{video_id}"
            else:
                rel_base = f"hls/{video_id}"
        else:
            rel_base = f"hls/{video_id}"

        video.hls_playlist_path = f"{rel_base}/{master_playlist}"
        video.master_playlist_path = f"{rel_base}/{master_playlist}"

        if renditions:
            video.set_renditions(renditions)
            video.source_width = renditions[0].get('width', 15360) if renditions else 1920
            video.source_height = renditions[0].get('height', 8640) if renditions else 1080

        if thumbnail:
            video.thumbnail_path = f"{rel_base}/{thumbnail}"

        if result.get('sprite'):
            video.sprite_path = f"{rel_base}/{result['sprite']}"
            video.sprite_tile_count = len(renditions) if renditions else 0
        if result.get('thumbnails_vtt'):
            video.thumbnails_vtt_path = f"{rel_base}/{result['thumbnails_vtt']}"

        # Store processing stats
        video.processing_stats = json.dumps({
            'encoder': result.get('encoder_used', 'unknown'),
            'parallel_tasks': result.get('total_parallel_tasks', 0),
            'processing_time_s': result.get('processing_time', 0),
            'full_video_duration_hours': result.get('full_video_duration_hours', 0),
            'qualities_completed': result.get('qualities_completed', 0),
            'hls_segments': result.get('total_hls_segments', 0)
        })

        db.session.commit()
        logger.info(f"Video {video_id} updated with ALL {len(renditions)} quality renditions "
                   f"(ultra-parallel: {result.get('total_parallel_tasks', 0)} tasks)")
    except Exception as e:
        logger.error(f"Failed to update video record {video_id}: {e}")


@video_bp.route('/teacher/upload', methods=['POST'])
@login_required
@teacher_required
def upload_video():
    file = request.files.get('video_file')
    if not file or not allowed_file(file.filename):
        return jsonify({'success': False, 'message': 'Invalid file type.'}), 400
    filename = secure_filename(file.filename)
    save_name = f"{uuid.uuid4().hex}_{filename}"
    input_path = os.path.join(current_app.config['UPLOAD_FOLDER'], save_name)
    file.save(input_path)

    video = Video(title=filename, filename=save_name, uploader_id=current_user.id, status='processing')
    db.session.add(video)
    db.session.commit()

    uploader_id = current_user.id  # Capture user_id before thread starts

    def background_processing(app, video_id, input_path, uploader_id):
        """Use ultra-parallel processing for FULL video with ALL qualities."""
        try:
            with app.app_context():
                logger.info(f"Starting ultra-parallel processing for video {video_id}: {input_path}")
                
                # Retrieve output directory based on tenant
                user = User.query.get(uploader_id)
                hls_dir = None
                if user and user.institution_id:
                    inst = Institution.query.get(user.institution_id)
                    if inst:
                        hls_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'institutions', inst.slug, 'hls', str(video_id))
                
                result = process_video_ultra(
                    input_path=input_path,
                    video_id=video_id,
                    output_dir=hls_dir,
                    progress_callback=lambda p: _update_video_progress(video_id, p)
                )
                if result['success']:
                    _update_video_record(video_id, result)
                    logger.info(f"Video {video_id} processed via ultra-parallel: "
                               f"{result['qualities_completed']} qualities, "
                               f"full video {result.get('full_video_duration_hours', 0):.1f}h, "
                               f"in {result['processing_time']:.1f}s")
                else:
                    logger.error(f"Ultra-parallel failed for video {video_id}: {result.get('error')}")
                    try:
                        v = Video.query.get(video_id)
                        if v:
                            v.status = 'failed'
                            db.session.commit()
                    except:
                        pass
        except Exception as e:
            logger.error(f"Background processing failed for video {video_id}: {e}")

    threading.Thread(target=background_processing, args=(
        current_app._get_current_object(), video.id, input_path, uploader_id
    ), daemon=True).start()

    return jsonify({
        'success': True,
        'video_id': video.id,
        'processor': 'ultra_parallel_nvenc',
        'qualities': [q['name'] for q in ULTRA_QUALITY_LADDER],
        'message': 'Processing started with GPU-accelerated ultra-parallel engine'
    })


@video_bp.route('/api/video_status/<int:video_id>')
@login_required
def get_video_status(video_id):
    video = Video.query.get_or_404(video_id)
    enforce_institution_access(video)
    return jsonify({
        'status': video.status,
        'progress': video.processing_progress,
        'processor': 'ultra_parallel'
    })


@video_bp.route('/teacher/upload_subtitles/<int:video_id>', methods=['POST'])
@login_required
@teacher_required
def upload_subtitles(video_id):
    video = Video.query.get_or_404(video_id)
    enforce_institution_access(video)
    file = request.files.get('subtitle_file')
    language = request.form.get('language', 'en')
    
    if file and allowed_subtitle_file(file.filename):
        filename = secure_filename(file.filename)
        save_name = f"sub_{video_id}_{filename}"
        
        # Check tenant directory
        uploader = User.query.get(video.uploader_id)
        if uploader and uploader.institution_id:
            inst = Institution.query.get(uploader.institution_id)
            if inst:
                tenant_subtitle_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'institutions', inst.slug, 'subtitles')
                os.makedirs(tenant_subtitle_dir, exist_ok=True)
                subtitle_path = os.path.join(tenant_subtitle_dir, save_name)
                file.save(subtitle_path)
                video.subtitle_path = f'uploads/institutions/{inst.slug}/subtitles/{save_name}'
            else:
                subtitle_path = os.path.join(current_app.config['SUBTITLE_FOLDER'], save_name)
                file.save(subtitle_path)
                video.subtitle_path = f'subtitles/{save_name}'
        else:
            subtitle_path = os.path.join(current_app.config['SUBTITLE_FOLDER'], save_name)
            file.save(subtitle_path)
            video.subtitle_path = f'subtitles/{save_name}'
            
        video.subtitle_language = language
        db.session.commit()
        
        # For compatibility, redirect if it was form submit or json
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True})
            
        flash('Subtitles uploaded successfully.', 'success')
        return redirect(url_for('video.watch_video', video_id=video_id))
    
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'error': 'Invalid subtitle file. Use .vtt or .srt'}), 400
        
    flash('Invalid subtitle file format. Use .vtt or .srt', 'error')
    return redirect(url_for('video.watch_video', video_id=video_id))


# ═══════════════════════════════════════════════════════════════
#  HIGH-PERFORMANCE CHUNK UPLOAD API
# ═══════════════════════════════════════════════════════════════

@video_bp.route('/api/upload/init', methods=['POST'])
@login_required
@teacher_required
def init_upload():
    data = request.get_json(silent=True) or request.form
    filename = data.get('filename', '')
    total_size = int(data.get('total_size', 0))
    total_chunks = int(data.get('total_chunks', 0))
    chunk_size = int(data.get('chunk_size', 10 * 1024 * 1024))

    if not filename or total_size <= 0 or total_chunks <= 0:
        return jsonify({'success': False, 'message': 'Missing required fields: filename, total_size, total_chunks'}), 400

    upload_uuid = str(uuid.uuid4())

    video = Video(
        title=filename,
        filename=filename,
        uploader_id=current_user.id,
        status='uploading',
        processing_progress=0
    )
    db.session.add(video)
    db.session.commit()

    job = VideoJob(
        job_id=upload_uuid,
        upload_uuid=upload_uuid,
        original_filename=filename,
        total_size=total_size,
        total_chunks=total_chunks,
        status=VideoJobStatus.QUEUED
    )
    with _job_registry_lock:
        _job_registry[upload_uuid] = job

    logger.info(f"Upload initialized: uuid={upload_uuid}, filename={filename}, "
                f"size={total_size / (1024**3):.2f}GB, chunks={total_chunks}")

    return jsonify({
        'success': True,
        'upload_uuid': upload_uuid,
        'video_id': video.id,
        'chunk_size': chunk_size,
        'total_chunks': total_chunks,
        'max_chunk_rate_per_min': 10000000
    })


@video_bp.route('/api/upload/chunk', methods=['POST'])
@limiter.exempt
def upload_chunk_high_perf():
    if request.is_json:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'success': False, 'message': 'Invalid JSON'}), 400
        upload_uuid = data.get('upload_uuid', '')
        chunk_index = int(data.get('chunk_index', -1))
        total_chunks = int(data.get('total_chunks', 0))
        chunk_b64 = data.get('data', '')

        if not upload_uuid or chunk_index < 0 or not chunk_b64:
            return jsonify({'success': False, 'message': 'Missing required fields'}), 400

        try:
            import base64
            chunk_data = base64.b64decode(chunk_b64)
        except Exception as e:
            return jsonify({'success': False, 'message': f'Base64 decode error: {e}'}), 400

    elif request.files:
        upload_uuid = request.form.get('upload_uuid', '')
        chunk_index = int(request.form.get('chunk_index', -1))
        total_chunks = int(request.form.get('total_chunks', 0))
        chunk_file = request.files.get('chunk_data') or request.files.get('file')

        if not upload_uuid or chunk_index < 0 or not chunk_file:
            return jsonify({'success': False, 'message': 'Missing required fields'}), 400

        chunk_data = chunk_file.read()

    else:
        return jsonify({'success': False, 'message': 'No data provided (JSON or multipart)'}), 400

    result = handle_chunk_upload(upload_uuid, chunk_index, total_chunks, chunk_data)

    if isinstance(result, tuple):
        return jsonify(result[0]), result[1]

    return jsonify(result)


@video_bp.route('/api/upload/chunk/direct', methods=['POST'])
@limiter.exempt
def upload_chunk_direct():
    upload_uuid = request.form.get('upload_uuid', '')
    chunk_index = int(request.form.get('chunk_index', -1))
    total_chunks = int(request.form.get('total_chunks', 0))
    chunk_file = request.files.get('chunk_data') or request.files.get('file')

    if not upload_uuid or chunk_index < 0 or not chunk_file:
        return jsonify({'success': False, 'message': 'Missing required fields'}), 400

    chunk_data = chunk_file.read()
    result = handle_chunk_upload_direct(upload_uuid, chunk_index, total_chunks, chunk_data)

    if isinstance(result, tuple):
        return jsonify(result[0]), result[1]

    return jsonify(result)


@video_bp.route('/api/upload/complete', methods=['POST'])
@login_required
def complete_upload():
    """
    Finalize a chunk upload: assemble chunks and process with ultra-parallel engine.
    Processes the FULL video with ALL quality levels.
    """
    data = request.get_json(silent=True) or request.form
    upload_uuid = data.get('upload_uuid', '')
    video_id = int(data.get('video_id', 0))
    original_filename = data.get('original_filename', 'video.mp4')
    total_chunks = int(data.get('total_chunks', 0))

    if not upload_uuid or not video_id:
        return jsonify({'success': False, 'message': 'Missing upload_uuid or video_id'}), 400

    video = Video.query.get(video_id)
    if not video:
        return jsonify({'success': False, 'message': 'Video not found'}), 404

    video.status = 'assembling'
    db.session.commit()

    uploader_id = video.uploader_id  # Capture uploader_id before thread starts

    def assembly_job(uid, vid, tchunks, fname, uploader_id):
        with current_app.app_context():
            from services.upload_engine import _chunk_buffer
            if _chunk_buffer:
                _chunk_buffer._flush_to_disk()

            result = assemble_chunks(uid, tchunks, fname)

            if result.get('success'):
                video_obj = Video.query.get(vid)
                if video_obj:
                    video_obj.status = 'processing'
                    video_obj.processing_progress = 50
                    db.session.commit()

                # Determine output directory based on tenant
                user = User.query.get(uploader_id)
                hls_dir = None
                if user and user.institution_id:
                    inst = Institution.query.get(user.institution_id)
                    if inst:
                        hls_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'institutions', inst.slug, 'hls', str(vid))

                # Use ULTRA PARALLEL processing for FULL video with ALL qualities
                logger.info(f"Starting ultra-parallel HLS for video {vid} (full video, all qualities)")
                hls_result = process_video_ultra(
                    result['file_path'],
                    vid,
                    output_dir=hls_dir,
                    progress_callback=lambda p: _update_video_progress(vid, p)
                )

                if hls_result.get('success'):
                    video_obj = Video.query.get(vid)
                    if video_obj:
                        _update_video_record(vid, hls_result)
                        logger.info(f"Video {vid} completed via ultra-parallel: "
                                   f"{hls_result.get('qualities_completed', 0)} qualities, "
                                   f"{hls_result.get('total_hls_segments', 0)} HLS segments")
                else:
                    # Fallback to standard transcoder
                    logger.warning(f"Ultra-parallel failed for {vid}, falling back to standard")
                    hls_result = process_video_to_hls(
                        vid,
                        result['file_path'],
                        max_height=8640  # Max height for full quality ladder (up to 16K)
                    )
                    if hls_result.get('success'):
                        video_obj = Video.query.get(vid)
                        if video_obj:
                            master_playlist = hls_result.get('master_playlist', 'master.m3u8')
                            thumbnail = hls_result.get('thumbnail', '')
                            video_obj.status = 'completed'
                            video_obj.processing_progress = 100
                            video_obj.hls_playlist_path = f"hls/{vid}/{master_playlist}"
                            video_obj.master_playlist_path = f"hls/{vid}/{master_playlist}"
                            video_obj.has_adaptive_streams = True
                            renditions = hls_result.get('renditions', [])
                            video_obj.set_renditions(renditions)
                            if renditions:
                                video_obj.source_width = renditions[0].get('width', 1920)
                                video_obj.source_height = renditions[0].get('height', 1080)
                            if thumbnail:
                                video_obj.thumbnail_path = f"hls/{vid}/{thumbnail}"
                            db.session.commit()
                    else:
                        if video_obj:
                            video_obj.status = 'failed'
                            video_obj.processing_progress = 0
                            errors = hls_result.get('errors', ['Transcoding failed'])
                            logger.error(f"Video {vid} HLS processing failed: {errors}")
                            db.session.commit()
            else:
                video_obj = Video.query.get(vid)
                if video_obj:
                    video_obj.status = 'failed'
                    video_obj.processing_progress = 0
                    db.session.commit()

    thread = threading.Thread(
        target=assembly_job,
        args=(upload_uuid, video_id, total_chunks, original_filename, uploader_id),
        daemon=True
    )
    thread.start()

    return jsonify({
        'success': True,
        'message': 'Upload finalized, ultra-parallel processing started',
        'video_id': video_id,
        'upload_uuid': upload_uuid,
        'processor': 'ultra_parallel_nvenc'
    })


# ═══════════════════════════════════════════════════════════════
#  SMART QUALITY / DEVICE CAPABILITY DETECTION API
# ═══════════════════════════════════════════════════════════════

@video_bp.route('/api/video/capabilities', methods=['GET'])
@login_required
def get_video_capabilities():
    video_id = request.args.get('video_id', type=int)
    if not video_id:
        return jsonify({'success': False, 'message': 'Missing video_id'}), 400

    video = Video.query.get(video_id)
    if not video:
        return jsonify({'success': False, 'message': 'Video not found'}), 404

    renditions = video.get_renditions() or []

    quality_info = []
    for r in renditions:
        quality_info.append({
            'name': r.get('name', ''),
            'width': r.get('width', 0),
            'height': r.get('height', 0),
            'bitrate': r.get('bitrate', 0),
            'bandwidth': r.get('bandwidth', 0),
            'resolution': r.get('resolution', ''),
            'playlist': r.get('playlist', '')
        })

    quality_info.sort(key=lambda x: x['height'], reverse=True)

    return jsonify({
        'success': True,
        'video_id': video_id,
        'qualities': quality_info,
        'total_qualities': len(quality_info),
        'master_playlist': video.master_playlist_path or video.hls_playlist_path,
        'has_adaptive': video.has_adaptive_streams or False,
        'source_height': video.source_height or 0,
        'source_width': video.source_width or 0,
        'duration': video.duration or 0
    })


# ═══════════════════════════════════════════════════════════════
#  ULTRA PARALLEL PROCESSING API ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@video_bp.route('/api/ultra/process', methods=['POST'])
@login_required
@teacher_required
def ultra_process_video():
    """
    Start ultra-parallel processing for a video.
    Processes FULL video with ALL qualities (144p through 8K).
    """
    data = request.get_json(silent=True) or request.form
    video_id = int(data.get('video_id', 0))

    if not video_id:
        return jsonify({'success': False, 'message': 'Missing video_id'}), 400

    video = Video.query.get(video_id)
    if not video:
        return jsonify({'success': False, 'message': 'Video not found'}), 404

    input_path = os.path.join(current_app.config['UPLOAD_FOLDER'], video.filename)

    # Start async processing
    job_id = process_video_ultra_async(input_path, video_id)

    return jsonify({
        'success': True,
        'message': 'Ultra-parallel processing started',
        'video_id': video_id,
        'job_id': job_id,
        'processor': 'ultra_parallel_nvenc',
        'qualities': [q['name'] for q in ULTRA_QUALITY_LADDER],
        'watch_url': url_for('video.watch_video', video_id=video_id)
    })


@video_bp.route('/api/ultra/status/<job_id>', methods=['GET'])
@login_required
def ultra_job_status(job_id):
    """Get status of an ultra-parallel processing job."""
    status = get_ultra_job_status(job_id)
    if not status:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(status)


@video_bp.route('/api/ultra/jobs', methods=['GET'])
@login_required
def ultra_list_jobs():
    """List all active ultra-parallel jobs."""
    processor = get_processor()
    jobs = processor.get_all_jobs()
    return jsonify({
        'total_jobs': len(jobs),
        'jobs': jobs
    })


# ═══════════════════════════════════════════════════════════════
#  BATCH VIDEO PROCESSING API
# ═══════════════════════════════════════════════════════════════

@video_bp.route('/api/batch/create', methods=['POST'])
@login_required
@teacher_required
def batch_create_videos():
    data = request.get_json(silent=True) or request.form
    count = int(data.get('count', 200))
    size_gb = int(data.get('size_gb', 20))

    if count < 1 or count > 1000000000:
        return jsonify({'success': False, 'message': 'Count must be between 1 and 1,000,000,000'}), 400
    if size_gb < 1 or size_gb > 1000:
        return jsonify({'success': False, 'message': 'Size must be between 1GB and 1000GB'}), 400

    total_storage = count * size_gb
    logger.info(f"Starting batch creation: {count} videos x {size_gb}GB = {total_storage}TB total")

    jobs = create_batch_video_jobs(count=count, size_gb=size_gb, uploader_id=current_user.id)

    if not jobs:
        return jsonify({'success': False, 'message': 'Failed to create batch jobs'}), 500

    def batch_processor(jobs_list):
        with current_app.app_context():
            from services.upload_engine import process_batch_videos
            result = process_batch_videos(jobs_list)
            logger.info(f"Batch processing completed: {result}")

    thread = threading.Thread(
        target=batch_processor,
        args=(jobs,),
        daemon=True
    )
    thread.start()

    return jsonify({
        'success': True,
        'message': f'Batch created: {len(jobs)} videos x {size_gb}GB = {total_storage}TB',
        'total_jobs': len(jobs),
        'total_size_tb': total_storage / 1024,
        'jobs': jobs[:10],
        'status_check_url': '/api/batch/progress'
    })


@video_bp.route('/api/batch/progress', methods=['GET'])
@login_required
def batch_progress():
    progress = get_batch_progress()
    stats = get_overall_stats()
    return jsonify({
        'progress': progress,
        'stats': stats
    })


@video_bp.route('/api/upload/stats', methods=['GET'])
@login_required
def upload_stats():
    stats = get_overall_stats()
    return jsonify(stats)


@video_bp.route('/api/upload/jobs', methods=['GET'])
@login_required
def list_upload_jobs():
    jobs = get_all_upload_jobs()
    return jsonify({
        'total_jobs': len(jobs),
        'jobs': jobs
    })


@video_bp.route('/api/upload/job/<upload_uuid>', methods=['GET'])
@login_required
def get_job(upload_uuid):
    job = get_upload_job(upload_uuid)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


@video_bp.route('/api/upload/cancel/<upload_uuid>', methods=['POST'])
@login_required
def cancel_upload(upload_uuid):
    from services.upload_engine import _job_registry, _job_registry_lock, UPLOAD_CHUNKS_DIR, _chunk_buffer

    with _job_registry_lock:
        job = _job_registry.pop(upload_uuid, None)

    if not job:
        return jsonify({'error': 'Job not found'}), 404

    chunks_dir = os.path.join(UPLOAD_CHUNKS_DIR, upload_uuid)
    if os.path.exists(chunks_dir):
        shutil.rmtree(chunks_dir, ignore_errors=True)

    if _chunk_buffer:
        _chunk_buffer.remove_upload(upload_uuid)

    return jsonify({'success': True, 'message': 'Upload cancelled and cleaned up'})


# ═══════════════════════════════════════════════════════════════
#  ULTRA PARALLEL SYSTEM INFO
# ═══════════════════════════════════════════════════════════════

@video_bp.route('/api/ultra/system_info', methods=['GET'])
@login_required
def ultra_system_info():
    """Get ultra-parallel processing system information."""
    from services.ultra_parallel_processor import (
        BEST_ENCODER, MAX_WORKER_POOL_SIZE, SEGMENT_DURATION_SECONDS,
        ULTRA_QUALITY_LADDER, detect_best_encoder
    )
    import multiprocessing

    return jsonify({
        'success': True,
        'system': {
            'cpu_cores': multiprocessing.cpu_count(),
            'best_encoder': BEST_ENCODER,
            'worker_pool_size': MAX_WORKER_POOL_SIZE,
            'segment_duration_seconds': SEGMENT_DURATION_SECONDS,
            'quality_levels': [
                {'name': q['name'], 'resolution': f"{q['width']}x{q['height']}",
                 'bitrate': q['bitrate']}
                for q in ULTRA_QUALITY_LADDER
            ],
            'total_qualities': len(ULTRA_QUALITY_LADDER),
            'has_gpu': BEST_ENCODER != 'software',
            'gpu_encoder': BEST_ENCODER,
            'max_parallel_tasks': MAX_WORKER_POOL_SIZE
        }
    })