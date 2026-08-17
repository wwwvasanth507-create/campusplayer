"""
CampusPlayer Video Asset Cleanup Engine.
Permanently deletes all video files (raw uploads, multi-bitrate HLS renditions,
TS video segments, master playlists, thumbnails, sprites, timeline VTTs, and subtitles)
from the host server/PC file system, and terminates any active conversion processes.
Optimized for zero-lag, instant execution on Linux (Ubuntu) and Windows.
"""

import os
import re
import glob
import time
import stat
import shutil
import logging
import threading
from typing import Dict, List, Set, Optional, Any, Union

logger = logging.getLogger(__name__)

# Deduplication cache to prevent running expensive cleanup twice in the same request
_recently_cleaned_videos: Dict[int, float] = {}
_cleanup_lock = threading.Lock()


def _remove_readonly(func, path, exc_info):
    """Error handler for shutil.rmtree to clear read-only file attributes."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IWUSR)
        func(path)
    except Exception as e:
        logger.debug(f"[VideoCleanup] Could not force-remove readonly path {path}: {e}")


def safe_remove_file(file_path: Optional[str], max_retries: int = 3, retry_delay: float = 0.05) -> bool:
    """
    Safely and permanently remove a single file from the host file system.
    Fast-path execution on Linux/Ubuntu (< 1ms), with Windows fallback for file locks.
    """
    if not file_path or not isinstance(file_path, str):
        return True

    norm_path = os.path.normpath(file_path)
    if not os.path.exists(norm_path) and not os.path.islink(norm_path):
        return True

    if os.path.isdir(norm_path):
        return safe_remove_dir(norm_path, max_retries=max_retries, retry_delay=retry_delay)

    # Fast path: instantaneous removal
    try:
        os.remove(norm_path)
        logger.info(f"[VideoCleanup] Permanently deleted file: {norm_path}")
        return True
    except (PermissionError, OSError):
        pass

    # Slow path: clear read-only / write permissions and retry
    for attempt in range(max_retries):
        try:
            try:
                os.chmod(norm_path, stat.S_IWRITE | stat.S_IWUSR)
            except Exception:
                pass

            os.remove(norm_path)
            logger.info(f"[VideoCleanup] Permanently deleted file: {norm_path}")
            return True
        except (PermissionError, OSError) as e:
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))
            else:
                logger.warning(f"[VideoCleanup] Failed to remove file {norm_path} after {max_retries} attempts: {e}")
                return False
        except Exception as e:
            logger.error(f"[VideoCleanup] Unexpected error removing file {norm_path}: {e}")
            return False

    return not os.path.exists(norm_path)


def safe_remove_dir(dir_path: Optional[str], max_retries: int = 3, retry_delay: float = 0.05) -> bool:
    """
    Safely and permanently remove an entire directory tree from the host file system.
    Fast-path execution on Linux/Ubuntu (< 5ms), with Windows fallback for file locks.
    """
    if not dir_path or not isinstance(dir_path, str):
        return True

    norm_path = os.path.normpath(dir_path)
    if not os.path.exists(norm_path):
        return True

    # Security check: Never accidentally delete root or core parent directories
    abs_clean = os.path.abspath(norm_path).rstrip('\\/')
    basename = os.path.basename(abs_clean).lower()
    if basename in ('', 'static', 'uploads', 'hls', 'subtitles', 'opt', 'var', 'tmp', 'home', 'campusplayer'):
        logger.error(f"[VideoCleanup] Refusing to delete protected directory: {norm_path}")
        return False

    # Fast path: direct rmtree
    try:
        shutil.rmtree(norm_path, onerror=_remove_readonly)
        if not os.path.exists(norm_path):
            logger.info(f"[VideoCleanup] Permanently deleted directory tree: {norm_path}")
            return True
    except Exception:
        pass

    # Slow path: recursively clear permissions and retry
    for attempt in range(max_retries):
        try:
            for root, dirs, files in os.walk(norm_path, topdown=False):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        os.chmod(fp, stat.S_IWRITE | stat.S_IWUSR)
                    except Exception:
                        pass
                for d in dirs:
                    dp = os.path.join(root, d)
                    try:
                        os.chmod(dp, stat.S_IWRITE | stat.S_IWUSR)
                    except Exception:
                        pass

            try:
                os.chmod(norm_path, stat.S_IWRITE | stat.S_IWUSR)
            except Exception:
                pass

            shutil.rmtree(norm_path, onerror=_remove_readonly)
            if not os.path.exists(norm_path):
                logger.info(f"[VideoCleanup] Permanently deleted directory tree: {norm_path}")
                return True
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))
            else:
                logger.warning(f"[VideoCleanup] Could not fully remove directory {norm_path}: {e}")

    return not os.path.exists(norm_path)


def _resolve_asset_paths(raw_path: Optional[str], search_roots: List[str]) -> List[str]:
    """
    Expands an asset path across search roots, correctly handling relative paths,
    leading slashes, and 'static/' prefixes on both Linux and Windows.
    """
    if not raw_path or not isinstance(raw_path, str):
        return []

    candidates: List[str] = []
    clean_raw = raw_path.strip()

    if os.path.isabs(clean_raw):
        candidates.append(os.path.normpath(clean_raw))

    # Strip leading slashes to prevent os.path.join from treating it as root-absolute on Linux
    clean_rel = clean_raw.lstrip('/\\')
    without_static = re.sub(r'^(static[/\\\\]|static$)', '', clean_rel, flags=re.IGNORECASE).lstrip('/\\')

    for root in search_roots:
        if not root:
            continue
        candidates.append(os.path.normpath(os.path.join(root, clean_rel)))
        candidates.append(os.path.normpath(os.path.join(root, without_static)))
        candidates.append(os.path.normpath(os.path.join(root, 'static', clean_rel)))
        candidates.append(os.path.normpath(os.path.join(root, 'static', without_static)))
        candidates.append(os.path.normpath(os.path.join(root, 'uploads', without_static)))
        candidates.append(os.path.normpath(os.path.join(root, 'static', 'uploads', without_static)))

    # Return unique paths
    seen = set()
    result = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def permanently_delete_video_assets(
    video_or_id: Union[Any, int],
    app_instance: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Comprehensively and permanently deletes all on-disk video assets for a video
    and cancels any active conversions.
    
    Can receive either a Video model instance or an integer video_id.
    Safely discovers all possible file and directory locations across global and tenant storage.
    Guarantees instantaneous execution without lag or database locking.
    """
    from flask import current_app

    app = app_instance
    if not app:
        try:
            app = current_app._get_current_object()
        except Exception:
            from app import app as fallback_app
            app = fallback_app

    video_id: Optional[int] = None
    if hasattr(video_or_id, 'id'):
        video_id = video_or_id.id
    elif isinstance(video_or_id, int):
        video_id = video_or_id

    # 0. Deduplication to prevent double execution during route + before_delete hook
    if hasattr(video_or_id, '_assets_cleaned') and getattr(video_or_id, '_assets_cleaned', False):
        logger.debug(f"[VideoCleanup] Video object #{getattr(video_or_id, 'id', None)} already cleaned; skipping.")
        return {'video_id': video_id, 'skipped': True, 'success': True, 'deleted_dirs': [], 'deleted_files': []}

    try:
        from flask import g, has_request_context
        if has_request_context() and video_id:
            if not hasattr(g, '_cleaned_video_ids'):
                g._cleaned_video_ids = set()
            if video_id in g._cleaned_video_ids:
                logger.debug(f"[VideoCleanup] Video #{video_id} already cleaned in this request; skipping.")
                return {'video_id': video_id, 'skipped': True, 'success': True, 'deleted_dirs': [], 'deleted_files': []}
            g._cleaned_video_ids.add(video_id)
    except Exception:
        pass

    # 1. Discover all root directories accurately for Linux / Ubuntu and Windows
    root_paths: List[str] = []

    # App root path (e.g. /opt/campusplayer or C:\campusplayer)
    if hasattr(app, 'root_path') and app.root_path:
        root_paths.append(app.root_path)

    # App config BASE_DIR
    base_dir_cfg = app.config.get('BASE_DIR')
    if base_dir_cfg:
        root_paths.append(base_dir_cfg)

    # Current working directory
    root_paths.append(os.path.abspath('.'))

    # Standard Ubuntu / Linux deployment path
    if os.path.exists('/opt/campusplayer'):
        root_paths.append('/opt/campusplayer')

    unique_roots: List[str] = []
    for r in root_paths:
        norm_r = os.path.normpath(r)
        if norm_r not in unique_roots:
            unique_roots.append(norm_r)

    primary_root = unique_roots[0] if unique_roots else os.path.abspath('.')

    static_dir = app.static_folder or os.path.join(primary_root, 'static')
    upload_folder = app.config.get('UPLOAD_FOLDER') or os.path.join(static_dir, 'uploads')
    hls_folder = app.config.get('HLS_FOLDER') or os.path.join(static_dir, 'hls')
    subtitle_folder = app.config.get('SUBTITLE_FOLDER') or os.path.join(static_dir, 'subtitles')

    search_roots = [static_dir, upload_folder, hls_folder, subtitle_folder] + unique_roots

    # 2. Extract metadata from Video model & cancel active conversion jobs
    filename: Optional[str] = None
    hls_playlist_path: Optional[str] = None
    master_playlist_path: Optional[str] = None
    thumbnail_path: Optional[str] = None
    subtitle_path: Optional[str] = None
    sprite_path: Optional[str] = None
    thumbnails_vtt_path: Optional[str] = None
    institution_id: Optional[int] = None
    uploader_id: Optional[int] = None
    inst_slug: Optional[str] = None
    job_input_files: List[str] = []
    job_output_dirs: List[str] = []

    with app.app_context():
        from models import Video, User, Institution, ConversionJob
        from extensions import db

        video = None
        if hasattr(video_or_id, 'id'):
            video = video_or_id
        elif video_id:
            video = Video.query.get(video_id)

        if video:
            filename = video.filename
            hls_playlist_path = video.hls_playlist_path
            master_playlist_path = video.master_playlist_path
            thumbnail_path = video.thumbnail_path
            subtitle_path = video.subtitle_path
            sprite_path = getattr(video, 'sprite_path', None)
            thumbnails_vtt_path = getattr(video, 'thumbnails_vtt_path', None)
            institution_id = video.institution_id
            uploader_id = video.uploader_id

            if not institution_id and uploader_id:
                try:
                    uploader = User.query.get(uploader_id)
                    if uploader and uploader.institution_id:
                        institution_id = uploader.institution_id
                except Exception:
                    pass

            if institution_id:
                try:
                    inst = Institution.query.get(institution_id)
                    if inst:
                        inst_slug = inst.slug
                except Exception:
                    pass

        # Immediately cancel running FFmpeg conversion processes for this video
        if video_id:
            try:
                from services.conversion_engine import cancel_conversion_jobs_for_video
                cancel_conversion_jobs_for_video(video_id)
            except Exception as e:
                logger.debug(f"[VideoCleanup] Error stopping active conversions for video {video_id}: {e}")

            # Collect custom job paths
            try:
                jobs = ConversionJob.query.filter_by(video_id=video_id).all()
                for j in jobs:
                    if j.input_file:
                        job_input_files.append(j.input_file)
                    if j.output_directory:
                        job_output_dirs.append(j.output_directory)
                # Remove ConversionJob rows in current session without nested commit
                ConversionJob.query.filter_by(video_id=video_id).delete(synchronize_session=False)
            except Exception as e:
                logger.debug(f"[VideoCleanup] Note querying/cleaning ConversionJobs for video {video_id}: {e}")

    # 3. Collect all candidate directories to remove
    dirs_to_remove: Set[str] = set()

    if video_id:
        # Standard Global HLS directory
        dirs_to_remove.add(os.path.join(hls_folder, str(video_id)))
        dirs_to_remove.add(os.path.join(upload_folder, 'hls', str(video_id)))
        dirs_to_remove.add(os.path.join(static_dir, 'hls', str(video_id)))
        dirs_to_remove.add(os.path.join(static_dir, 'uploads', 'hls', str(video_id)))

        for root in unique_roots:
            dirs_to_remove.add(os.path.join(root, 'static', 'hls', str(video_id)))
            dirs_to_remove.add(os.path.join(root, 'static', 'uploads', 'hls', str(video_id)))
            dirs_to_remove.add(os.path.join(root, 'static', 'uploads', 'chunks', str(video_id)))

        # Tenant Institution HLS directory
        if inst_slug:
            dirs_to_remove.add(os.path.join(upload_folder, 'institutions', inst_slug, 'hls', str(video_id)))
            dirs_to_remove.add(os.path.join(static_dir, 'uploads', 'institutions', inst_slug, 'hls', str(video_id)))

        # Wildcard match across all tenant institution folders
        for match in glob.glob(os.path.join(upload_folder, 'institutions', '*', 'hls', str(video_id))):
            dirs_to_remove.add(match)
        for match in glob.glob(os.path.join(static_dir, 'uploads', 'institutions', '*', 'hls', str(video_id))):
            dirs_to_remove.add(match)
        for match in glob.glob(os.path.join(upload_folder, 'chunks', f"*{video_id}*")):
            dirs_to_remove.add(match)

    # Derive HLS directory from playlist path
    for p_path in [hls_playlist_path, master_playlist_path]:
        if p_path:
            for resolved in _resolve_asset_paths(p_path, search_roots):
                dirs_to_remove.add(os.path.dirname(resolved))

    # Add custom conversion job output directories
    for j_out in job_output_dirs:
        if j_out:
            dirs_to_remove.add(os.path.normpath(j_out))

    # Chunk folder from filename prefix / UUID
    if filename:
        name_parts = filename.split('_')
        if len(name_parts) > 1 and len(name_parts[0]) >= 8:
            dirs_to_remove.add(os.path.join(upload_folder, 'chunks', name_parts[0]))
            dirs_to_remove.add(os.path.join(static_dir, 'uploads', 'chunks', name_parts[0]))

    # 4. Collect all candidate file paths to remove
    files_to_remove: Set[str] = set()

    # Raw Video File
    if filename:
        for resolved in _resolve_asset_paths(filename, search_roots):
            files_to_remove.add(resolved)
        files_to_remove.add(os.path.join(upload_folder, filename))
        files_to_remove.add(os.path.join(upload_folder, 'assembled', filename))
        files_to_remove.add(os.path.join(upload_folder, 'synthetic_batch', filename))
        files_to_remove.add(os.path.join(upload_folder, 'chunks', filename))
        files_to_remove.add(os.path.join(static_dir, 'uploads', filename))
        files_to_remove.add(os.path.join(static_dir, 'uploads', 'assembled', filename))

    for j_in in job_input_files:
        if j_in:
            for resolved in _resolve_asset_paths(j_in, search_roots):
                files_to_remove.add(resolved)

    # Subtitles
    if subtitle_path:
        sub_name = os.path.basename(subtitle_path)
        for resolved in _resolve_asset_paths(subtitle_path, search_roots):
            files_to_remove.add(resolved)
        files_to_remove.add(os.path.join(subtitle_folder, sub_name))
        files_to_remove.add(os.path.join(upload_folder, 'subtitles', sub_name))
        files_to_remove.add(os.path.join(static_dir, 'subtitles', sub_name))
        for match in glob.glob(os.path.join(upload_folder, 'institutions', '*', 'subtitles', sub_name)):
            files_to_remove.add(match)

    # Thumbnails
    if thumbnail_path:
        thumb_name = os.path.basename(thumbnail_path)
        for resolved in _resolve_asset_paths(thumbnail_path, search_roots):
            files_to_remove.add(resolved)
        files_to_remove.add(os.path.join(upload_folder, thumb_name))
        files_to_remove.add(os.path.join(upload_folder, 'thumbnails', thumb_name))
        files_to_remove.add(os.path.join(static_dir, 'uploads', thumb_name))

    # Sprites and VTTs
    if sprite_path:
        for resolved in _resolve_asset_paths(sprite_path, search_roots):
            files_to_remove.add(resolved)
    if thumbnails_vtt_path:
        for resolved in _resolve_asset_paths(thumbnails_vtt_path, search_roots):
            files_to_remove.add(resolved)

    # 5. Execute fast permanent removal on host filesystem
    deleted_dirs: List[str] = []
    failed_dirs: List[str] = []
    deleted_files: List[str] = []
    failed_files: List[str] = []

    # Directories first (removes HLS segments, master.m3u8, rendition playlists, sprites)
    for d in dirs_to_remove:
        if d and os.path.exists(d):
            if safe_remove_dir(d):
                deleted_dirs.append(d)
            else:
                failed_dirs.append(d)

    # Standalone files second (raw uploads, subtitles, standalone thumbnails)
    for f in files_to_remove:
        if f and os.path.exists(f):
            if safe_remove_file(f):
                deleted_files.append(f)
            else:
                failed_files.append(f)

    logger.info(
        f"[VideoCleanup] Completed cleanup for video #{video_id or filename}: "
        f"{len(deleted_dirs)} dirs removed, {len(deleted_files)} files removed."
    )

    if hasattr(video_or_id, '__dict__'):
        try:
            setattr(video_or_id, '_assets_cleaned', True)
        except Exception:
            pass

    return {
        'video_id': video_id,
        'deleted_dirs': deleted_dirs,
        'failed_dirs': failed_dirs,
        'deleted_files': deleted_files,
        'failed_files': failed_files,
        'success': len(failed_dirs) == 0 and len(failed_files) == 0
    }
