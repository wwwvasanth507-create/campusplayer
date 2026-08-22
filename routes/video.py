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
from services.utils import (
    sanitize_input, allowed_file, allowed_subtitle_file, apply_media_cors_headers,
    enforce_institution_access, scope_to_institution, get_institution_slug, get_video_storage_dir
)
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


_chunk_assembly_lock = threading.Lock()

@video_bp.route('/teacher/upload_chunk_status', methods=['GET'])
@limiter.exempt
def upload_chunk_status():
    uuid_str = request.args.get('uuid') or request.args.get('upload_uuid')
    if not uuid_str:
        return jsonify({'success': False, 'message': 'Missing uuid parameter.'}), 400
    
    chunks_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'chunks', uuid_str)
    if not os.path.exists(chunks_dir):
        return jsonify({'success': True, 'uploaded_chunks': [], 'count': 0})
    
    saved_files = [f for f in os.listdir(chunks_dir) if f.startswith('chunk_') and f.endswith('.part')]
    uploaded_indices = []
    for f in saved_files:
        try:
            idx = int(f.replace('chunk_', '').replace('.part', ''))
            uploaded_indices.append(idx)
        except ValueError:
            pass
            
    return jsonify({
        'success': True,
        'uploaded_chunks': uploaded_indices,
        'count': len(uploaded_indices)
    })

@video_bp.route('/teacher/upload_chunk', methods=['POST'])
@limiter.exempt
def upload_chunk():
    uuid_str = request.form.get('uuid') or request.form.get('upload_uuid')
    chunk_index_raw = request.form.get('chunkIndex') if request.form.get('chunkIndex') is not None else request.form.get('chunk_index')
    total_chunks_raw = request.form.get('totalChunks') if request.form.get('totalChunks') is not None else request.form.get('total_chunks')
    chunk = request.files.get('chunk') or request.files.get('file') or request.files.get('chunk_data')

    if not uuid_str or chunk_index_raw is None or total_chunks_raw is None or not chunk:
        return jsonify({'success': False, 'message': 'Missing required fields: uuid, chunkIndex, totalChunks, chunk file.'}), 400

    chunk_index = int(chunk_index_raw)
    total_chunks = int(total_chunks_raw)

    orig_filename = secure_filename(request.form.get('filename') or chunk.filename or 'video.mp4')
    title = sanitize_input(request.form.get('title') or orig_filename, 200)
    try:
        classroom_id = int(request.form.get('classroom_id', -1))
    except (ValueError, TypeError):
        classroom_id = -1
    description = sanitize_input(request.form.get('description', ''), 1000)
    tags = sanitize_input(request.form.get('tags', ''), 250)

    chunks_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'chunks', uuid_str)
    os.makedirs(chunks_dir, exist_ok=True)

    part_filename = f"chunk_{chunk_index:06d}.part"
    chunk_path = os.path.join(chunks_dir, part_filename)
    try:
        chunk.save(chunk_path)
    except (OSError, FileNotFoundError):
        assembling_dir = f"{chunks_dir}_assembling"
        if os.path.exists(assembling_dir):
            video = Video.query.filter_by(uploader_id=current_user.id if current_user and current_user.is_authenticated else 1).order_by(Video.id.desc()).first()
            return jsonify({
                'success': True,
                'video_id': video.id if video else None,
                'status': 'processing',
                'message': 'Video assembled and processing started'
            })

    if not os.path.exists(chunks_dir):
        assembling_dir = f"{chunks_dir}_assembling"
        if os.path.exists(assembling_dir):
            video = Video.query.filter_by(uploader_id=current_user.id if current_user and current_user.is_authenticated else 1).order_by(Video.id.desc()).first()
            return jsonify({
                'success': True,
                'video_id': video.id if video else None,
                'status': 'processing',
                'message': 'Video assembled and processing started'
            })
        return jsonify({
            'success': True,
            'chunk_index': chunk_index,
            'saved': total_chunks,
            'total_chunks': total_chunks
        })

    saved_parts = [f for f in os.listdir(chunks_dir) if f.startswith('chunk_') and f.endswith('.part')]

    if len(saved_parts) < total_chunks:
        return jsonify({
            'success': True,
            'chunk_index': chunk_index,
            'saved': len(saved_parts),
            'total_chunks': total_chunks
        })

    with _chunk_assembly_lock:
        if not os.path.exists(chunks_dir):
            video = Video.query.filter_by(uploader_id=current_user.id if current_user and current_user.is_authenticated else 1).order_by(Video.id.desc()).first()
            if video:
                return jsonify({
                    'success': True,
                    'video_id': video.id,
                    'status': video.status,
                    'message': 'Video assembled and processing started'
                })
            return jsonify({'success': True, 'message': 'Chunks uploaded successfully.'})

        assembling_dir = f"{chunks_dir}_assembling"
        try:
            os.rename(chunks_dir, assembling_dir)
        except (OSError, FileNotFoundError):
            video = Video.query.filter_by(uploader_id=current_user.id if current_user and current_user.is_authenticated else 1).order_by(Video.id.desc()).first()
            return jsonify({
                'success': True,
                'video_id': video.id if video else None,
                'status': 'processing',
                'message': 'Video being assembled by worker.'
            })

        uploader_id = getattr(current_user, 'id', None) if current_user and current_user.is_authenticated else None
        if not uploader_id:
            teacher = User.query.filter_by(role='teacher').first()
            uploader_id = teacher.id if teacher else 1

        uploader_user = current_user if current_user and current_user.is_authenticated else User.query.get(uploader_id)
        inst_id = getattr(uploader_user, 'institution_id', None) if uploader_user else None
        slug = get_institution_slug(uploader_id=uploader_id)
        video = Video(
            title=title,
            filename=f"institutions/{slug}/temp/{uuid_str}_{orig_filename}",
            uploader_id=uploader_id,
            institution_id=inst_id,
            classroom_id=classroom_id if classroom_id > 0 else None,
            description=description,
            tags=tags,
            status='processing',
            processing_progress=10
        )
        db.session.add(video)
        db.session.commit()

        app_obj = current_app._get_current_object()
        vid = video.id
        video_dir, slug = get_video_storage_dir(vid, uploader_id=uploader_id, app=app_obj)

        assembled_path = os.path.join(video_dir, 'source.mp4')
        os.makedirs(video_dir, exist_ok=True)

        def fast_zero_copy_assembly(c_dir, n_chunks, out_path):
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            out_fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                for i in range(n_chunks):
                    part_path = os.path.join(c_dir, f"chunk_{i:06d}.part")
                    if not os.path.exists(part_path):
                        continue
                    in_fd = os.open(part_path, os.O_RDONLY)
                    try:
                        size = os.path.getsize(part_path)
                        if hasattr(os, 'sendfile'):
                            offset = 0
                            while offset < size:
                                sent = os.sendfile(out_fd, in_fd, offset, size - offset)
                                if sent == 0:
                                    break
                                offset += sent
                        else:
                            buf_size = 64 * 1024 * 1024
                            while True:
                                chunk_bytes = os.read(in_fd, buf_size)
                                if not chunk_bytes:
                                    break
                                os.write(out_fd, chunk_bytes)
                    finally:
                        os.close(in_fd)
            finally:
                os.close(out_fd)

        def _async_assemble_and_enqueue(app_obj, c_dir, n_chunks, out_path, v_id, u_id, inst_slug):
            with app_obj.app_context():
                try:
                    fast_zero_copy_assembly(c_dir, n_chunks, out_path)
                    shutil.rmtree(c_dir, ignore_errors=True)
                    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                        v = Video.query.get(v_id)
                        if v:
                            v.filename = f"institutions/{inst_slug}/{v_id}/source.mp4"
                            db.session.commit()
                        logger.info(f"[Upload] Async assembly finished for video {v_id} ({os.path.getsize(out_path)} bytes). Enqueuing HLS conversion...")
                        from services.conversion_engine import enqueue_conversion_job
                        enqueue_conversion_job(v_id, out_path, uploader_id=u_id)
                    else:
                        logger.error(f"[Upload] Async chunk assembly failed for video {v_id}: empty file.")
                        v = Video.query.get(v_id)
                        if v:
                            v.status = 'failed'
                            v.processing_error = 'Chunk assembly failed: empty file produced.'
                            db.session.commit()
                except Exception as ex:
                    logger.error(f"[Upload] Error in async assembly for video {v_id}: {ex}")

        # Dispatch background assembly thread for zero HTTP response lag
        asm_thread = threading.Thread(
            target=_async_assemble_and_enqueue,
            args=(app_obj, assembling_dir, total_chunks, assembled_path, vid, uploader_id, slug),
            daemon=True
        )
        asm_thread.start()

        return jsonify({
            'success': True,
            'video_id': vid,
            'status': 'processing',
            'message': 'Upload completed instantly! Video assembly and HLS processing running in background.'
        })

        def background_hls(app_ctx, video_id, input_file, v_dir):
            with app_ctx.app_context():
                try:
                    result = process_video_ultra(
                        input_path=input_file,
                        video_id=video_id,
                        output_dir=v_dir,
                        progress_callback=lambda p: _update_video_progress(video_id, p)
                    )
                    if result.get('success'):
                        _update_video_record(video_id, result)
                    else:
                        err_msg = result.get('error', 'Video processing failed.')
                        _mark_video_failed(video_id, err_msg)
                except Exception as exc:
                    logger.error(f"Unhandled exception in background_hls for video {video_id}: {exc}")
                    _mark_video_failed(video_id, str(exc))

        threading.Thread(
            target=background_hls,
            args=(app_obj, vid, assembled_path, video_dir),
            daemon=True
        ).start()

        return jsonify({
            'success': True,
            'video_id': vid,
            'status': 'processing',
            'message': 'Video assembled successfully and parallel HLS processing started'
        })


def _execute_db_retry(fn, max_retries=5, initial_delay=0.05):
    """Execute a database operation with automatic rollback and exponential backoff retry on SQLite locks."""
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except Exception as ex:
            try:
                db.session.rollback()
            except Exception:
                pass
            if attempt == max_retries:
                logger.error(f"[DB Retry] Failed after {max_retries} attempts: {ex}")
                raise ex
            time.sleep(initial_delay * (1.5 ** (attempt - 1)))

def _update_video_progress(video_id: int, progress: float):
    """Update video processing progress in database with lock resilience."""
    def _op():
        video = db.session.get(Video, video_id)
        if video:
            video.processing_progress = min(99, int(progress))
            db.session.commit()
    try:
        _execute_db_retry(_op, max_retries=3, initial_delay=0.05)
    except Exception as e:
        logger.warning(f"Progress update failed for video {video_id}: {e}")

def _mark_video_failed(v_id: int, err_msg: str):
    """Mark video status as failed and record exact error message with lock resilience."""
    def _op():
        v = db.session.get(Video, v_id)
        if v:
            v.status = 'failed'
            v.processing_progress = 0
            v.processing_error = str(err_msg)
            db.session.commit()
            logger.error(f"Video {v_id} marked failed: {err_msg}")
    try:
        _execute_db_retry(_op, max_retries=3, initial_delay=0.05)
    except Exception as ex:
        logger.error(f"Error marking video {v_id} as failed: {ex}")

def _update_video_record(video_id: int, result: dict):
    """Update video record with complete HLS processing results with lock resilience."""
    def _op():
        video = db.session.get(Video, video_id)
        if not video:
            return

        video.status = 'completed'
        video.processing_progress = 100
        video.processing_error = None
        video.has_adaptive_streams = True

        master_playlist = result.get('master_playlist', 'master.m3u8')
        thumbnail = result.get('thumbnail', '')
        renditions = result.get('renditions', [])

        slug = get_institution_slug(uploader_id=video.uploader_id)
        rel_base = f"uploads/institutions/{slug}/{video_id}"

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
    try:
        _execute_db_retry(_op, max_retries=5, initial_delay=0.1)
    except Exception as e:
        logger.error(f"Failed to update video record {video_id}: {e}")


@video_bp.route('/teacher/upload', methods=['POST'])
@login_required
@teacher_required
def upload_video():
    file = request.files.get('video_file')
    if not file or not allowed_file(file.filename):
        return jsonify({'success': False, 'message': 'Invalid file type.'}), 400
    orig_filename = secure_filename(file.filename)

    slug = get_institution_slug(user=current_user)
    inst_id = getattr(current_user, 'institution_id', None)
    video = Video(
        title=orig_filename,
        filename=f"institutions/{slug}/temp/{orig_filename}",
        uploader_id=current_user.id,
        institution_id=inst_id,
        status='processing'
    )
    db.session.add(video)
    db.session.commit()

    uploader_id = current_user.id
    app_obj = current_app._get_current_object()
    video_dir, slug = get_video_storage_dir(video.id, uploader_id=uploader_id, app=app_obj)

    input_path = os.path.join(video_dir, 'source.mp4')
    file.save(input_path)

    video.filename = f"institutions/{slug}/{video.id}/source.mp4"
    db.session.commit()

    def background_processing(app_ctx, video_id, in_path, v_dir):
        """Use ultra-parallel processing for FULL video with ALL qualities."""
        try:
            with app_ctx.app_context():
                logger.info(f"Starting ultra-parallel processing for video {video_id}: {in_path}")
                result = process_video_ultra(
                    input_path=in_path,
                    video_id=video_id,
                    output_dir=v_dir,
                    progress_callback=lambda p: _update_video_progress(video_id, p)
                )
                if result.get('success'):
                    _update_video_record(video_id, result)
                    logger.info(f"Video {video_id} processed via ultra-parallel: "
                               f"{result.get('qualities_completed', 0)} qualities, "
                               f"full video {result.get('full_video_duration_hours', 0):.1f}h, "
                               f"in {result.get('processing_time', 0):.1f}s")
                else:
                    err_msg = result.get('error', 'Ultra-parallel video processing failed.')
                    logger.error(f"Ultra-parallel failed for video {video_id}: {err_msg}")
                    _mark_video_failed(video_id, err_msg)
        except Exception as e:
            logger.error(f"Background processing failed for video {video_id}: {e}")
            with app_ctx.app_context():
                _mark_video_failed(video_id, str(e))

    threading.Thread(target=background_processing, args=(
        app_obj, video.id, input_path, video_dir
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
        'progress': video.processing_progress or 0,
        'processing_error': video.processing_error or None,
        'hls_playlist_path': video.hls_playlist_path or video.master_playlist_path,
        'thumbnail_path': video.thumbnail_path,
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

    inst_id = getattr(current_user, 'institution_id', None)
    video = Video(
        title=filename,
        filename=filename,
        uploader_id=current_user.id,
        institution_id=inst_id,
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

    uploader_id = video.uploader_id
    app_obj = current_app._get_current_object()

    def assembly_job(app_ctx, uid, vid, tchunks, fname, uploader_uid):
        with app_ctx.app_context():
            try:
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

                    video_dir, slug = get_video_storage_dir(vid, uploader_id=uploader_uid, app=app_ctx)
                    dest_source_path = os.path.join(video_dir, 'source.mp4')
                    try:
                        if os.path.exists(result['file_path']):
                            shutil.move(result['file_path'], dest_source_path)
                    except Exception:
                        dest_source_path = result['file_path']

                    if video_obj:
                        video_obj.filename = f"institutions/{slug}/{vid}/source.mp4"
                        db.session.commit()

                    # Use ULTRA PARALLEL processing for FULL video with ALL qualities
                    logger.info(f"Starting ultra-parallel HLS for video {vid} (full video, all qualities)")
                    hls_result = process_video_ultra(
                        dest_source_path,
                        vid,
                        output_dir=video_dir,
                        progress_callback=lambda p: _update_video_progress(vid, p)
                    )

                    if hls_result.get('success'):
                        _update_video_record(vid, hls_result)
                        logger.info(f"Video {vid} completed via ultra-parallel: "
                                   f"{hls_result.get('qualities_completed', 0)} qualities, "
                                   f"{hls_result.get('total_hls_segments', 0)} HLS segments")
                    else:
                        # Fallback to standard transcoder
                        logger.warning(f"Ultra-parallel failed for {vid}, falling back to standard")
                        hls_result = process_video_to_hls(
                            vid,
                            dest_source_path,
                            max_height=8640
                        )
                        if hls_result.get('success'):
                            _update_video_record(vid, hls_result)
                        else:
                            err_msg = ', '.join(hls_result.get('errors', ['Transcoding failed']))
                            _mark_video_failed(vid, err_msg)
                else:
                    _mark_video_failed(vid, result.get('error', 'Chunk assembly failed.'))
            except Exception as exc:
                logger.error(f"Unhandled exception during video {vid} assembly job: {exc}")
                _mark_video_failed(vid, str(exc))

    thread = threading.Thread(
        target=assembly_job,
        args=(app_obj, upload_uuid, video_id, total_chunks, original_filename, uploader_id),
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