"""
Persistent, Resumable, Crash-Safe, and Parallel HLS Conversion Engine for CampusPlayer.

Key Capabilities:
1. Database-backed ConversionJob lifecycle (queued -> processing -> completed / failed / interrupted).
2. Segment-level inspection and crash-recovery (resumes from segment K instead of restarting).
3. Configurable parallel worker pool with atomic job claiming.
4. Server restart & power failure auto-recovery.
5. Throttled database progress updates to prevent SQLite locking.
6. Clean integration with multi-tenant directory structure and existing player URLs.
"""

import os
import re
import sys
import time
import json
import math
import shutil
import signal
import logging
import threading
import subprocess
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any, Set

from extensions import db
from models import Video, User, Institution, SiteSettings, ConversionJob

logger = logging.getLogger('campusplayer.conversion')

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

MAX_CONCURRENT_CONVERSIONS = int(os.getenv('MAX_CONCURRENT_CONVERSIONS', '5'))
MAX_CONVERSION_RETRIES = int(os.getenv('MAX_CONVERSION_RETRIES', '3'))
CONVERSION_PROGRESS_INTERVAL = float(os.getenv('CONVERSION_PROGRESS_INTERVAL', '3.0'))
DEFAULT_SEGMENT_DURATION = int(os.getenv('CONVERSION_SEGMENT_DURATION', '6'))
FFMPEG_PRESET = os.getenv('FFMPEG_PRESET', 'ultrafast')
# When True (default), the original uploaded source file and any leftover chunk
# temp directory are deleted after a confirmed successful HLS conversion.  Set to
# False to keep source files on disk (e.g. for re-processing or archival).
DELETE_SOURCE_AFTER_CONVERSION = os.getenv('DELETE_SOURCE_AFTER_CONVERSION', 'true').lower() not in ('false', '0', 'no')

# Standard Rendition Ladder (144p to 16K)
RENDITIONS_LADDER = [
    ("144p",  256,   144,  "80k",    "100k",   "160k",   "64k"),
    ("240p",  426,   240,  "200k",   "250k",   "400k",   "64k"),
    ("360p",  640,   360,  "500k",   "600k",   "1000k",  "96k"),
    ("480p",  854,   480,  "1000k",  "1200k",  "2000k",  "128k"),
    ("720p",  1280,  720,  "2500k",  "3000k",  "5000k",  "128k"),
    ("1080p", 1920, 1080,  "5000k",  "6000k",  "10000k", "192k"),
    ("2K",    2560, 1440,  "12000k", "15000k", "24000k", "256k"),
    ("4K",    3840, 2160,  "35000k", "45000k", "70000k", "256k"),
    ("8K",    7680, 4320,  "100000k","120000k","200000k","256k"),
    ("16K",   15360, 8640, "250000k","300000k","500000k","320k"),
]


# ═══════════════════════════════════════════════════════════════
# BINARY PATH RESOLUTION
# ═══════════════════════════════════════════════════════════════

def get_ffmpeg_bin() -> str:
    """Resolve absolute path to ffmpeg binary."""
    env_path = os.getenv('FFMPEG_PATH')
    if env_path and (shutil.which(env_path) or os.path.exists(env_path)):
        return env_path
    path = shutil.which('ffmpeg')
    if path:
        return path
    for candidate in ['/usr/bin/ffmpeg', '/usr/local/bin/ffmpeg', 'C:\\ffmpeg\\bin\\ffmpeg.exe']:
        if os.path.exists(candidate):
            return candidate
    return 'ffmpeg'


def get_ffprobe_bin() -> str:
    """Resolve absolute path to ffprobe binary."""
    env_path = os.getenv('FFPROBE_PATH')
    if env_path and (shutil.which(env_path) or os.path.exists(env_path)):
        return env_path
    path = shutil.which('ffprobe')
    if path:
        return path
    for candidate in ['/usr/bin/ffprobe', '/usr/local/bin/ffprobe', 'C:\\ffmpeg\\bin\\ffprobe.exe']:
        if os.path.exists(candidate):
            return candidate
    return 'ffprobe'


# ═══════════════════════════════════════════════════════════════
# VIDEO PROBING
# ═══════════════════════════════════════════════════════════════

def probe_video(input_path: str) -> Dict[str, Any]:
    """Probe video metadata using ffprobe."""
    default_info = {
        'width': 1920,
        'height': 1080,
        'duration': 0.0,
        'bitrate': 0,
        'fps': 30.0,
        'codec': 'h264',
        'audio_codec': 'aac',
        'has_audio': True
    }
    if not os.path.exists(input_path):
        return default_info

    try:
        cmd = [
            get_ffprobe_bin(), '-v', 'error', '-print_format', 'json',
            '-analyzeduration', '10M', '-probesize', '10M',
            '-show_format', '-show_streams', input_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0 or not result.stdout:
            return default_info

        data = json.loads(result.stdout)
        video_stream = None
        audio_stream = None

        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'video' and not video_stream:
                disp = stream.get('disposition', {})
                if disp.get('attached_pic') != 1:
                    video_stream = stream
            elif stream.get('codec_type') == 'audio' and not audio_stream:
                audio_stream = stream

        duration = float(data.get('format', {}).get('duration', 0.0))
        if duration == 0.0 and video_stream:
            duration = float(video_stream.get('duration', 0.0))

        bitrate = int(data.get('format', {}).get('bit_rate', 0))

        width = 1920
        height = 1080
        fps = 30.0
        codec = 'h264'
        if video_stream:
            width = int(video_stream.get('width', 1920))
            height = int(video_stream.get('height', 1080))
            codec = video_stream.get('codec_name', 'h264')
            fps_parts = video_stream.get('avg_frame_rate', '30/1').split('/')
            if len(fps_parts) == 2 and float(fps_parts[1]) > 0:
                fps = float(fps_parts[0]) / float(fps_parts[1])

            # Check rotation tags
            tags = video_stream.get('tags', {})
            rotate = tags.get('rotate', 0)
            if rotate in ['90', '270', 90, 270]:
                width, height = height, width

        audio_codec = audio_stream.get('codec_name', 'aac') if audio_stream else 'aac'

        return {
            'width': width,
            'height': height,
            'duration': duration,
            'bitrate': bitrate,
            'fps': fps,
            'codec': codec,
            'audio_codec': audio_codec,
            'has_audio': audio_stream is not None
        }
    except Exception as e:
        logger.warning(f"Error probing video {input_path}: {e}")
        return default_info


# ═══════════════════════════════════════════════════════════════
# SEGMENT INSPECTION & RECOVERY HELPERS
# ═══════════════════════════════════════════════════════════════

def validate_ts_segment(file_path: str) -> bool:
    """
    Validate that an MPEG-TS segment file is complete and not corrupted.
    MPEG-TS packets are 188 bytes each and start with sync byte 0x47.
    """
    if not os.path.exists(file_path):
        return False
    size = os.path.getsize(file_path)
    if size < 188:
        return False

    try:
        with open(file_path, 'rb') as f:
            header = f.read(188)
            if not header or header[0] != 0x47:
                return False
        return True
    except Exception:
        return False


def get_existing_rendition_segments(output_dir: str, prefix: str) -> Tuple[List[int], int, float]:
    """
    Scan the output directory for existing segments of a rendition.
    Returns: (valid_indices, next_segment_index, completed_seconds)
    Safely discards any trailing corrupt/partial segment.
    """
    if not os.path.exists(output_dir):
        return [], 0, 0.0

    pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)\.ts$")
    found_segments = {}

    for fname in os.listdir(output_dir):
        m = pattern.match(fname)
        if m:
            idx = int(m.group(1))
            fpath = os.path.join(output_dir, fname)
            found_segments[idx] = fpath

    if not found_segments:
        return [], 0, 0.0

    sorted_indices = sorted(found_segments.keys())
    valid_indices = []

    for idx in sorted_indices:
        # Require contiguous sequence starting at 0
        if idx != len(valid_indices):
            break
        fpath = found_segments[idx]
        if validate_ts_segment(fpath):
            valid_indices.append(idx)
        else:
            # Corrupted trailing segment: remove it so it doesn't taint recovery
            try:
                os.remove(fpath)
                logger.info(f"[Recovery] Removed corrupt/partial segment: {fpath}")
            except Exception as e:
                logger.warning(f"Could not remove corrupt segment {fpath}: {e}")
            break

    # Clean up any orphaned out-of-order segment files beyond the valid contiguous chain
    valid_set = set(valid_indices)
    for idx, fpath in found_segments.items():
        if idx not in valid_set and os.path.exists(fpath):
            try:
                os.remove(fpath)
                logger.info(f"[Recovery] Removed orphaned segment: {fpath}")
            except Exception as e:
                logger.warning(f"Could not remove orphaned segment {fpath}: {e}")

    next_idx = len(valid_indices)
    completed_seconds = next_idx * float(DEFAULT_SEGMENT_DURATION)
    return valid_indices, next_idx, completed_seconds


def build_rendition_playlist(output_dir: str, prefix: str, valid_indices: List[int], segment_duration: int) -> str:
    """
    Generate or rebuild a valid .m3u8 playlist file containing all valid segments.
    """
    playlist_path = os.path.join(output_dir, f"{prefix}.m3u8")
    max_target_duration = math.ceil(segment_duration * 1.5)

    with open(playlist_path, 'w', encoding='utf-8') as f:
        f.write("#EXTM3U\n")
        f.write("#EXT-X-VERSION:3\n")
        f.write(f"#EXT-X-TARGETDURATION:{max_target_duration}\n")
        f.write("#EXT-X-MEDIA-SEQUENCE:0\n")
        f.write("#EXT-X-PLAYLIST-TYPE:VOD\n\n")

        for idx in valid_indices:
            seg_name = f"{prefix}_{idx:03d}.ts"
            f.write(f"#EXTINF:{segment_duration:.3f},\n")
            f.write(f"{seg_name}\n")

        f.write("#EXT-X-ENDLIST\n")

    return playlist_path


def generate_master_playlist_content(renditions_info: List[Dict[str, Any]]) -> str:
    """Generate master.m3u8 content referencing all available renditions."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", ""]
    for r in renditions_info:
        bandwidth = r.get('bandwidth', 1000000)
        width = r.get('width', 1920)
        height = r.get('height', 1080)
        playlist = r.get('playlist', f"{r.get('name', 'video')}.m3u8")
        lines.append(f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={width}x{height}")
        lines.append(f"{playlist}")
        lines.append("")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# THUMBNAILS & SPRITE SHEETS
# ═══════════════════════════════════════════════════════════════

def generate_thumbnail_image(input_path: str, output_dir: str, duration: float) -> Optional[str]:
    """Generate thumbnail image at 20% or 5s into video."""
    thumb_path = os.path.join(output_dir, 'thumbnail.jpg')
    if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 1000:
        return 'thumbnail.jpg'

    ss = min(5.0, duration / 2.0) if duration > 0 else 1.0
    ss_str = f"{int(ss // 3600):02d}:{int((ss % 3600) // 60):02d}:{ss % 60:05.2f}"

    cmd = [
        get_ffmpeg_bin(), '-y', '-ss', ss_str, '-i', input_path,
        '-vframes', '1', '-f', 'image2', thumb_path
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=45)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500:
            return 'thumbnail.jpg'
    except Exception as e:
        logger.warning(f"Failed to generate thumbnail: {e}")
    return None


def generate_sprite_and_vtt(input_path: str, output_dir: str, duration: float, video_id: int) -> Tuple[Optional[str], Optional[str]]:
    """Generate seek preview sprite sheet and WebVTT cue file."""
    sprite_path = os.path.join(output_dir, 'sprite.jpg')
    vtt_path = os.path.join(output_dir, 'thumbnails.vtt')

    if (os.path.exists(sprite_path) and os.path.getsize(sprite_path) > 1000 and
            os.path.exists(vtt_path) and os.path.getsize(vtt_path) > 50):
        return 'sprite.jpg', 'thumbnails.vtt'

    if duration <= 0:
        return None, None

    interval = 2 if duration <= 60 else (5 if duration <= 600 else (15 if duration <= 3600 else 60))
    num_frames = max(1, int(duration // interval))
    cols = 10
    rows = max(1, math.ceil(num_frames / cols))
    tile_w, tile_h = 160, 90

    filter_str = f"fps=1/{interval},scale={tile_w}:{tile_h},tile={cols}x{rows}"
    cmd = [
        get_ffmpeg_bin(), '-y', '-i', input_path,
        '-vf', filter_str, '-vsync', 'vfr', '-q:v', '5', sprite_path
    ]

    try:
        subprocess.run(cmd, capture_output=True, timeout=180)
        if os.path.exists(sprite_path) and os.path.getsize(sprite_path) > 500:
            # Build VTT
            norm_out = os.path.normpath(output_dir).replace('\\', '/')
            static_idx = norm_out.find('/static/')
            rel_web_dir = norm_out[static_idx:] if static_idx != -1 else f"/static/hls/{video_id}"
            sprite_url = f"{rel_web_dir}/sprite.jpg"

            with open(vtt_path, 'w', encoding='utf-8') as f:
                f.write("WEBVTT\n\n")
                for i in range(num_frames):
                    start_sec = i * interval
                    end_sec = min(duration, (i + 1) * interval)
                    col_idx = i % cols
                    row_idx = i // cols
                    x = col_idx * tile_w
                    y = row_idx * tile_h

                    def fmt(s):
                        h = int(s // 3600)
                        m = int((s % 3600) // 60)
                        sec = int(s % 60)
                        ms = int((s - int(s)) * 1000)
                        return f"{h:02d}:{m:02d}:{sec:02d}.{ms:03d}"

                    f.write(f"{fmt(start_sec)} --> {fmt(end_sec)}\n")
                    f.write(f"{sprite_url}#xywh={x},{y},{tile_w},{tile_h}\n\n")

            return 'sprite.jpg', 'thumbnails.vtt'
    except Exception as e:
        logger.warning(f"Failed to generate sprite sheet: {e}")

    return None, None


# ═══════════════════════════════════════════════════════════════
# ACTIVE SUBPROCESS REGISTRY & CANCELLATION
# ═══════════════════════════════════════════════════════════════

_active_processes_lock = threading.Lock()
_active_job_processes: Dict[int, subprocess.Popen] = {}
_active_video_jobs: Dict[int, Set[int]] = {}

def register_active_process(job_id: int, video_id: int, process: subprocess.Popen):
    """Track running FFmpeg process for rapid termination if job/video is deleted."""
    if not job_id:
        return
    with _active_processes_lock:
        _active_job_processes[job_id] = process
        if video_id:
            if video_id not in _active_video_jobs:
                _active_video_jobs[video_id] = set()
            _active_video_jobs[video_id].add(job_id)

def unregister_active_process(job_id: int, video_id: Optional[int] = None):
    """Remove completed or terminated process from tracking registry."""
    if not job_id:
        return
    with _active_processes_lock:
        _active_job_processes.pop(job_id, None)
        if video_id and video_id in _active_video_jobs:
            _active_video_jobs[video_id].discard(job_id)
            if not _active_video_jobs[video_id]:
                _active_video_jobs.pop(video_id, None)

# ═══════════════════════════════════════════════════════════════
# RESUMABLE TRANSCODING PER RENDITION
# ═══════════════════════════════════════════════════════════════

def transcode_rendition_resumable(
    input_path: str,
    output_dir: str,
    rendition: Tuple,
    source_info: Dict[str, Any],
    stop_event: threading.Event,
    progress_callback: Optional[Any] = None,
    job_id: Optional[int] = None,
    video_id: Optional[int] = None
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Transcode a single rendition with crash-recovery & segment-level resume.
    """
    name, width, height, bitrate, maxrate, bufsize, audio_bitrate = rendition
    if not input_path or not os.path.exists(input_path):
        logger.warning(f"[HLS] Source video file does not exist on disk: '{input_path}'. Aborting rendition {name}.")
        return None, None

    os.makedirs(output_dir, exist_ok=True)

    video_bps = int(bitrate.replace('k', '')) * 1000
    audio_bps = int(audio_bitrate.replace('k', '')) * 1000
    bandwidth = video_bps + audio_bps

    total_duration = source_info.get('duration', 0.0)
    segment_duration = DEFAULT_SEGMENT_DURATION
    estimated_total_segments = max(1, math.ceil(total_duration / segment_duration)) if total_duration > 0 else 1

    # Step 1: Inspect existing segments on disk
    valid_indices, next_idx, completed_secs = get_existing_rendition_segments(output_dir, name)

    # Check if this rendition is already 100% finished
    is_fully_done = (
        len(valid_indices) >= estimated_total_segments or
        (total_duration > 0 and completed_secs >= (total_duration - 1.0))
    )

    if is_fully_done and len(valid_indices) > 0:
        logger.info(f"[HLS-Resume] Rendition {name} is already complete ({len(valid_indices)} segments). Rebuilding playlist.")
        build_rendition_playlist(output_dir, name, valid_indices, segment_duration)
        rinfo = {
            'name': name,
            'playlist': f"{name}.m3u8",
            'width': width,
            'height': height,
            'bandwidth': bandwidth,
            'resolution': f"{width}x{height}"
        }
        return f"{name}.m3u8", rinfo

    # Determine codec profile & level
    profile = 'high' if height >= 2160 else 'main'
    level = '6.2' if height > 4320 else ('5.1' if height > 1080 else '3.1')

    # Step 2: Build FFmpeg command (resuming from timestamp if segments exist)
    cmd = [get_ffmpeg_bin(), '-y']

    if next_idx > 0 and completed_secs > 0:
        logger.info(f"[HLS-Resume] Resuming rendition {name} from segment {next_idx} ({completed_secs:.1f}s / {total_duration:.1f}s)")
        cmd += ['-ss', str(completed_secs)]
    else:
        logger.info(f"[HLS] Starting fresh conversion for rendition {name} (0/{estimated_total_segments} segments)")

    fps = float(source_info.get('fps') or 30.0)
    gop_size = max(30, int(fps * 2))

    cmd += [
        '-i', input_path,
        '-vf', f'scale=w={width}:h={height}:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2',
        '-c:v', 'libx264', '-profile:v', profile, '-level', level, '-pix_fmt', 'yuv420p',
        '-preset', FFMPEG_PRESET, '-crf', '22',
        '-b:v', bitrate, '-maxrate:v', maxrate, '-bufsize:v', bufsize,
        '-movflags', '+faststart'
    ]

    if source_info.get('has_audio', True):
        cmd += ['-c:a', 'aac', '-b:a', audio_bitrate, '-ar', '48000', '-ac', '2']
    else:
        cmd += ['-an']

    temp_playlist_path = os.path.join(output_dir, f"{name}_temp.m3u8")

    cmd += [
        '-g', str(gop_size), '-keyint_min', str(gop_size), '-sc_threshold', '0',
        '-start_number', str(next_idx),
        '-hls_time', str(segment_duration), '-hls_playlist_type', 'vod',
        '-hls_segment_filename', os.path.join(output_dir, f"{name}_%03d.ts"),
        temp_playlist_path
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        universal_newlines=True
    )

    if job_id:
        register_active_process(job_id, video_id or 0, process)

    try:
        last_time_sec = completed_secs
        time_regex = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
        recent_log_lines = []

        while True:
            if stop_event.is_set():
                try:
                    process.terminate()
                    process.wait(timeout=2)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
                raise InterruptedError("Transcoding was stopped by worker manager.")

            line = ""
            while True:
                char = process.stdout.read(1)
                if not char or char in ['\r', '\n']:
                    break
                line += char

            if not char and not line:
                break

            if line.strip():
                recent_log_lines.append(line.strip())
                if len(recent_log_lines) > 10:
                    recent_log_lines.pop(0)

            m = time_regex.search(line)
            if m:
                hours, mins, secs = m.groups()
                relative_elapsed = int(hours) * 3600 + int(mins) * 60 + float(secs)
                current_abs_sec = completed_secs + relative_elapsed
                last_time_sec = current_abs_sec
                current_seg = int(current_abs_sec // segment_duration)

                if progress_callback:
                    progress_callback(current_abs_sec, current_seg, estimated_total_segments)

        process.wait()
    finally:
        if job_id:
            unregister_active_process(job_id, video_id)

    if process.returncode != 0:
        err_excerpt = " | ".join(recent_log_lines[-3:]) if recent_log_lines else "No output"
        logger.error(f"FFmpeg failed for rendition {name} with code {process.returncode}: {err_excerpt}")
        # Clean up temp playlist
        if os.path.exists(temp_playlist_path):
            try:
                os.remove(temp_playlist_path)
            except Exception:
                pass
        return None, None

    # Step 3: Verify and Rebuild complete playlist from on-disk segments
    all_valid_indices, total_valid_count, _ = get_existing_rendition_segments(output_dir, name)
    build_rendition_playlist(output_dir, name, all_valid_indices, segment_duration)

    if os.path.exists(temp_playlist_path):
        try:
            os.remove(temp_playlist_path)
        except Exception:
            pass

    rinfo = {
        'name': name,
        'playlist': f"{name}.m3u8",
        'width': width,
        'height': height,
        'bandwidth': bandwidth,
        'resolution': f"{width}x{height}"
    }
    return f"{name}.m3u8", rinfo


# ═══════════════════════════════════════════════════════════════
# SOURCE FILE CLEANUP HELPER
# ═══════════════════════════════════════════════════════════════

def _delete_source_files(input_path: str, worker_id: str, video_id: int) -> None:
    """
    Delete the original uploaded source file and, if present, the sibling
    ``_chunks/`` temporary directory left by the chunked-upload assembler.

    Logs the amount of disk space freed.  All errors are caught and logged as
    warnings so a deletion failure never rolls back an otherwise-successful job.
    """
    freed_bytes = 0

    # 1. Remove the assembled source file
    if os.path.exists(input_path):
        try:
            size = os.path.getsize(input_path)
            os.remove(input_path)
            freed_bytes += size
            logger.info(
                f"[{worker_id}][Video {video_id}] Deleted source file "
                f"({size / (1024 ** 2):.1f} MB): {input_path}"
            )
        except Exception as exc:
            logger.warning(
                f"[{worker_id}][Video {video_id}] Could not delete source file "
                f"{input_path}: {exc}"
            )

    # 2. Remove any leftover chunks temp directory (e.g. uploads/<upload_id>_chunks/)
    #    The chunked-upload assembler stores partial chunks in a sibling dir whose
    #    name is derived from the filename: <stem>_chunks/ or <stem>.tmp/
    source_dir = os.path.dirname(input_path)
    stem = os.path.splitext(os.path.basename(input_path))[0]
    for suffix in ('_chunks', '.tmp', '_tmp'):
        candidate = os.path.join(source_dir, f"{stem}{suffix}")
        if os.path.isdir(candidate):
            try:
                dir_size = sum(
                    os.path.getsize(os.path.join(dp, f))
                    for dp, _, files in os.walk(candidate)
                    for f in files
                )
                shutil.rmtree(candidate, ignore_errors=True)
                freed_bytes += dir_size
                logger.info(
                    f"[{worker_id}][Video {video_id}] Deleted chunk temp dir "
                    f"({dir_size / (1024 ** 2):.1f} MB): {candidate}"
                )
            except Exception as exc:
                logger.warning(
                    f"[{worker_id}][Video {video_id}] Could not delete chunk dir "
                    f"{candidate}: {exc}"
                )

    if freed_bytes > 0:
        logger.info(
            f"[{worker_id}][Video {video_id}] Total disk space freed: "
            f"{freed_bytes / (1024 ** 2):.2f} MB"
        )


# ═══════════════════════════════════════════════════════════════
# FULL JOB PROCESSING LOGIC
# ═══════════════════════════════════════════════════════════════

def execute_conversion_job(app, job_id: int, worker_id: str, stop_event: threading.Event) -> bool:
    """
    Execute an enqueued or recovered conversion job with full database persistence.
    """
    with app.app_context():
        job = ConversionJob.query.get(job_id)
        if not job:
            logger.error(f"Job {job_id} not found in database.")
            return False

        video = Video.query.get(job.video_id)
        if not video:
            logger.error(f"Video {job.video_id} not found for job {job_id}.")
            job.status = 'failed'
            job.error_message = 'Associated video record not found'
            db.session.commit()
            return False

        input_path = job.input_file
        if not os.path.isabs(input_path):
            input_path = os.path.join(app.config['UPLOAD_FOLDER'], input_path)

        if not os.path.exists(input_path):
            logger.error(f"Source file {input_path} missing on disk.")
            job.status = 'failed'
            job.error_message = f"Source video file missing: {job.input_file}"
            video.status = 'failed'
            db.session.commit()
            return False

        output_dir = job.output_directory
        os.makedirs(output_dir, exist_ok=True)

        logger.info(f"[{worker_id}] Starting/resuming job {job.id} for Video {video.id} ({video.title})")

        # Probe video
        source_info = probe_video(input_path)
        duration = source_info.get('duration', 0.0)
        video.duration_seconds = int(duration)
        db.session.commit()

        # Determine adaptive ladder
        uploader = User.query.get(video.uploader_id) if (video and getattr(video, 'uploader_id', None)) else None
        settings = None
        if uploader and uploader.institution_id:
            settings = SiteSettings.query.filter_by(institution_id=uploader.institution_id).first()
        if not settings:
            settings = SiteSettings.query.first()

        enable_adaptive = settings.enable_adaptive_streaming if hasattr(settings, 'enable_adaptive_streaming') else True
        max_height = getattr(settings, 'max_rendition_height', 8640) or 8640

        src_w = source_info.get('width', 1920)
        src_h = source_info.get('height', 1080)
        src_max_dim = max(src_w, src_h)
        src_min_dim = min(src_w, src_h)

        selected_renditions = []
        if enable_adaptive:
            for r in RENDITIONS_LADDER:
                r_name, r_w, r_h, r_bitrate, r_maxrate, r_bufsize, r_audio_bitrate = r
                if r_h > max_height:
                    continue
                if src_min_dim >= (r_h - 24) or src_max_dim >= (r_w - 50):
                    selected_renditions.append(r)
                elif not selected_renditions and r_name == RENDITIONS_LADDER[0][0]:
                    selected_renditions.append(r)

        if not selected_renditions:
            selected_renditions = [RENDITIONS_LADDER[0]]

        total_renditions = len(selected_renditions)
        completed_renditions_info = []
        renditions_state = job.get_renditions_state()

        last_db_update_time = [time.time()]

        def update_progress(current_abs_sec: float, current_seg: int, est_total_segs: int, r_idx: int):
            now = time.time()
            if now - last_db_update_time[0] >= CONVERSION_PROGRESS_INTERVAL:
                try:
                    fraction_rendition = min(1.0, current_abs_sec / duration) if duration > 0 else 0.5
                    overall_progress = min(98, int(((r_idx + fraction_rendition) / total_renditions) * 95))

                    job.progress = overall_progress
                    job.current_segment = current_seg
                    job.total_segments = est_total_segs * total_renditions
                    job.last_processed_position = current_abs_sec
                    job.updated_at = datetime.utcnow()

                    video.processing_progress = overall_progress
                    db.session.commit()
                    last_db_update_time[0] = now
                except Exception as e:
                    db.session.rollback()
                    logger.warning(f"Error updating progress in DB: {e}")

        # Process each selected rendition
        for r_idx, rendition in enumerate(selected_renditions):
            if stop_event.is_set():
                raise InterruptedError("Job interrupted during rendition loop")

            r_name = rendition[0]
            logger.info(f"[{worker_id}] Processing rendition {r_name} ({r_idx + 1}/{total_renditions}) for video {video.id}")

            cb = lambda c_sec, c_seg, t_seg, idx=r_idx: update_progress(c_sec, c_seg, t_seg, idx)
            playlist_name, rinfo = transcode_rendition_resumable(
                input_path=input_path,
                output_dir=output_dir,
                rendition=rendition,
                source_info=source_info,
                stop_event=stop_event,
                progress_callback=cb,
                job_id=job.id,
                video_id=video.id
            )

            if not rinfo:
                logger.error(f"[{worker_id}] Rendition {r_name} failed for video {video.id}")
                return False

            completed_renditions_info.append(rinfo)
            renditions_state[r_name] = 'completed'
            job.set_renditions_state(renditions_state)
            db.session.commit()

        # Step 4: Generate Master Playlist & Derived Assets
        master_content = generate_master_playlist_content(completed_renditions_info)
        master_path = os.path.join(output_dir, 'master.m3u8')
        with open(master_path, 'w', encoding='utf-8') as f:
            f.write(master_content)

        thumb_name = generate_thumbnail_image(input_path, output_dir, duration)
        sprite_name, vtt_name = generate_sprite_and_vtt(input_path, output_dir, duration, video.id)

        # Step 5: Finalize Video & Job records
        # Derive rel_base relative to static folder
        norm_out = os.path.normpath(output_dir).replace('\\', '/')
        static_idx = norm_out.find('/static/')
        if static_idx != -1:
            rel_base = norm_out[static_idx + len('/static/'):]
        else:
            rel_base = f"hls/{video.id}"

        video.hls_playlist_path = f"{rel_base}/master.m3u8"
        video.master_playlist_path = f"{rel_base}/master.m3u8"
        video.set_renditions(completed_renditions_info)
        video.has_adaptive_streams = len(completed_renditions_info) > 1
        video.source_width = source_info.get('width', 1920)
        video.source_height = source_info.get('height', 1080)
        video.source_bitrate = source_info.get('bitrate', 0)
        video.video_codec = source_info.get('codec', 'h264')
        video.audio_codec = source_info.get('audio_codec', 'aac')
        video.fps = source_info.get('fps', 30.0)

        if thumb_name:
            video.thumbnail_path = f"{rel_base}/{thumb_name}"
        if sprite_name:
            video.sprite_path = f"{rel_base}/{sprite_name}"
            video.sprite_tile_count = len(completed_renditions_info)
        if vtt_name:
            video.thumbnails_vtt_path = f"{rel_base}/{vtt_name}"

        video.status = 'completed'
        video.processing_progress = 100

        job.status = 'completed'
        job.progress = 100
        job.completed_at = datetime.utcnow()
        job.updated_at = datetime.utcnow()

        if uploader:
            uploader.xp = (uploader.xp or 0) + 50

        db.session.commit()
        logger.info(f"[{worker_id}] SUCCESS: Video {video.id} ({video.title}) conversion completed.")

        # ── Source cleanup (gated by DELETE_SOURCE_AFTER_CONVERSION) ─────────
        if DELETE_SOURCE_AFTER_CONVERSION:
            _delete_source_files(input_path, worker_id, video.id)
        else:
            logger.info(
                f"[{worker_id}] DELETE_SOURCE_AFTER_CONVERSION=False — "
                f"keeping source file: {input_path}"
            )

        return True


# ═══════════════════════════════════════════════════════════════
# CONVERSION WORKER MANAGER (PARALLEL POOL)
# ═══════════════════════════════════════════════════════════════

class ConversionWorkerManager:
    """
    Manages background workers with atomic job claiming, concurrency controls,
    and automatic retry/recovery mechanisms.
    """
    _instance = None
    _lock = threading.Lock()

    def __init__(self, app=None, max_workers: int = MAX_CONCURRENT_CONVERSIONS):
        self.app = app
        self.max_workers = max(1, max_workers)
        self.workers: List[threading.Thread] = []
        self.stop_events: Dict[str, threading.Event] = {}
        self.global_stop = threading.Event()
        self.notify_event = threading.Event()
        self._claim_lock = threading.Lock()
        self.is_running = False

    @classmethod
    def get_instance(cls, app=None):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(app=app)
            elif app and not cls._instance.app:
                cls._instance.app = app
            return cls._instance

    def start(self, app=None):
        """Start the worker pool."""
        with self._lock:
            if self.is_running:
                return
            if app:
                self.app = app
            if not self.app:
                raise ValueError("Flask app required to start worker manager.")

            self.global_stop.clear()
            self.workers = []
            self.stop_events = {}

            logger.info(f"[WorkerManager] Starting {self.max_workers} parallel conversion workers...")

            for i in range(self.max_workers):
                worker_id = f"worker-{i + 1}"
                stop_ev = threading.Event()
                self.stop_events[worker_id] = stop_ev
                t = threading.Thread(
                    target=self._worker_loop,
                    args=(worker_id, stop_ev),
                    name=f"HLS-{worker_id}",
                    daemon=True
                )
                self.workers.append(t)
                t.start()

            self.is_running = True

    def notify(self):
        """Notify workers that a new job is available in the queue."""
        self.notify_event.set()

    def _claim_next_job(self, worker_id: str) -> Optional[int]:
        """
        Atomically claim the oldest queued or interrupted job.
        Uses thread claim lock and a short database transaction.
        """
        if not self.app:
            return None

        with self._claim_lock:
            with self.app.app_context():
                try:
                    # Find oldest candidate
                    job = (
                        ConversionJob.query
                        .filter(ConversionJob.status.in_(['queued', 'interrupted']))
                        .order_by(ConversionJob.created_at.asc())
                        .first()
                    )
                    if job:
                        job.status = 'processing'
                        job.worker_id = worker_id
                        job.started_at = job.started_at or datetime.utcnow()
                        job.updated_at = datetime.utcnow()

                        video = db.session.get(Video, job.video_id)
                        if video:
                            video.status = 'processing'

                        db.session.commit()
                        return job.id
                except Exception as e:
                    db.session.rollback()
                    logger.warning(f"[{worker_id}] Atomic claim error: {e}")
                return None

    def _worker_loop(self, worker_id: str, stop_ev: threading.Event):
        """Continuous execution loop for a worker thread."""
        logger.info(f"[{worker_id}] Worker loop active.")

        while not self.global_stop.is_set() and not stop_ev.is_set():
            job_id = self._claim_next_job(worker_id)

            if job_id:
                logger.info(f"[{worker_id}] Claimed conversion job {job_id}")
                try:
                    success = execute_conversion_job(self.app, job_id, worker_id, stop_ev)
                    if not success and not stop_ev.is_set():
                        self._handle_job_failure(job_id, "FFmpeg conversion returned failure status")
                except InterruptedError:
                    logger.info(f"[{worker_id}] Job {job_id} interrupted gracefully.")
                    self._mark_job_interrupted(job_id)
                except Exception as e:
                    if 'has been deleted' in str(e) or 'ObjectDeletedError' in type(e).__name__:
                        logger.warning(f"[{worker_id}] Job {job_id} target video was deleted during processing.")
                    else:
                        logger.error(f"[{worker_id}] Unexpected error in job {job_id}: {e}", exc_info=True)
                        if not stop_ev.is_set():
                            self._handle_job_failure(job_id, str(e))
                finally:
                    if not self.global_stop.is_set() and stop_ev.is_set():
                        stop_ev.clear()
            else:
                # No jobs found: wait for notification or 4s timeout
                self.notify_event.wait(timeout=4.0)
                self.notify_event.clear()

        logger.info(f"[{worker_id}] Worker loop exited.")

    def _handle_job_failure(self, job_id: int, error_msg: str):
        """Handle failure with retry count logic."""
        with self.app.app_context():
            try:
                job = ConversionJob.query.get(job_id)
                if not job:
                    return
                job.retry_count = (job.retry_count or 0) + 1
                job.error_message = error_msg
                job.updated_at = datetime.utcnow()

                if job.retry_count < (job.max_retries or MAX_CONVERSION_RETRIES):
                    job.status = 'queued'
                    job.worker_id = None
                    logger.warning(f"Job {job_id} failed. Retrying (attempt {job.retry_count}/{job.max_retries})...")
                else:
                    job.status = 'failed'
                    video = Video.query.get(job.video_id)
                    if video:
                        video.status = 'failed'
                        video.processing_error = error_msg
                    logger.error(f"Job {job_id} permanently failed after {job.retry_count} retries.")

                db.session.commit()
            except Exception as e:
                db.session.rollback()
                logger.error(f"Error updating failed job {job_id}: {e}")

    def _mark_job_interrupted(self, job_id: int):
        """Mark job as interrupted so it can be resumed upon next restart."""
        with self.app.app_context():
            try:
                job = ConversionJob.query.get(job_id)
                if job:
                    job.status = 'interrupted'
                    job.worker_id = None
                    job.updated_at = datetime.utcnow()
                    db.session.commit()
            except Exception as e:
                db.session.rollback()
                if '0 were matched' not in str(e) and 'StaleDataError' not in type(e).__name__:
                    logger.error(f"Error marking job {job_id} interrupted: {e}")

    def shutdown(self, timeout: float = 5.0):
        """Gracefully shut down all conversion workers."""
        with self._lock:
            if not self.is_running:
                return
            logger.info("[WorkerManager] Shutting down conversion worker pool...")
            self.global_stop.set()
            for stop_ev in self.stop_events.values():
                stop_ev.set()
            self.notify_event.set()

            for t in self.workers:
                t.join(timeout=timeout)

            self.is_running = False
            logger.info("[WorkerManager] Worker pool shut down cleanly.")


# ═══════════════════════════════════════════════════════════════
# SERVER STARTUP & REBOOT RECOVERY
# ═══════════════════════════════════════════════════════════════

def recover_unfinished_jobs(app) -> int:
    """
    Scans the database on startup for any jobs left in 'processing' or 'interrupted'
    states from a previous crash/reboot and restores them to 'queued' or 'completed'.
    """
    recovered_count = 0
    with app.app_context():
        try:
            # 1. Check existing ConversionJob records
            unfinished_jobs = ConversionJob.query.filter(
                ConversionJob.status.in_(['processing', 'interrupted', 'queued'])
            ).all()

            for job in unfinished_jobs:
                video = Video.query.get(job.video_id)
                if not video:
                    continue

                output_dir = job.output_directory
                master_playlist = os.path.join(output_dir, 'master.m3u8')

                # Check if already 100% completed on disk
                if os.path.exists(master_playlist) and os.path.getsize(master_playlist) > 50:
                    norm_out = os.path.normpath(output_dir).replace('\\', '/')
                    static_idx = norm_out.find('/static/')
                    rel_base = norm_out[static_idx + len('/static/'):] if static_idx != -1 else f"hls/{video.id}"

                    video.status = 'completed'
                    video.processing_progress = 100
                    video.hls_playlist_path = f"{rel_base}/master.m3u8"
                    video.master_playlist_path = f"{rel_base}/master.m3u8"

                    job.status = 'completed'
                    job.progress = 100
                    job.completed_at = datetime.utcnow()
                    logger.info(f"[Recovery] Job {job.id} (Video {video.id}) output was already complete on disk. Marked completed.")
                    continue

                # Unfinished conversion: verify raw input file exists
                input_path = job.input_file
                if not os.path.isabs(input_path):
                    input_path = os.path.join(app.config['UPLOAD_FOLDER'], input_path)

                if os.path.exists(input_path):
                    job.status = 'queued'
                    job.worker_id = None
                    job.updated_at = datetime.utcnow()
                    video.status = 'queued'
                    recovered_count += 1
                    logger.info(f"[Recovery] Recovered unfinished job {job.id} (Video {video.id}, {video.title}). Re-queued for resume.")
                else:
                    job.status = 'failed'
                    job.error_message = "Source video file missing after reboot"
                    video.status = 'failed'
                    logger.warning(f"[Recovery] Source video for job {job.id} missing at {input_path}. Marked failed.")

            # 2. Check for orphan Videos in 'processing' or 'pending' state without a ConversionJob
            orphan_videos = Video.query.filter(Video.status.in_(['processing', 'pending'])).all()
            for v in orphan_videos:
                existing_job = ConversionJob.query.filter_by(video_id=v.id).first()
                if not existing_job:
                    # Resolve paths
                    uploader = User.query.get(v.uploader_id)
                    hls_dir = None
                    if uploader and uploader.institution_id:
                        inst = Institution.query.get(uploader.institution_id)
                        if inst:
                            hls_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'institutions', inst.slug, 'hls', str(v.id))
                    if not hls_dir:
                        hls_dir = os.path.join(app.config['HLS_FOLDER'], str(v.id))

                    raw_path = os.path.join(app.config['UPLOAD_FOLDER'], v.filename)
                    if os.path.exists(raw_path):
                        new_job = ConversionJob(
                            job_id=f"job_{v.id}_{int(time.time())}",
                            video_id=v.id,
                            institution_id=v.institution_id,
                            input_file=raw_path,
                            output_directory=hls_dir,
                            status='queued',
                            progress=0
                        )
                        db.session.add(new_job)
                        v.status = 'queued'
                        recovered_count += 1
                        logger.info(f"[Recovery] Created queued ConversionJob for orphan video {v.id} ({v.title})")

            db.session.commit()
            logger.info(f"[Recovery] Startup recovery completed. Total unfinished jobs ready: {recovered_count}")
        except Exception as e:
            db.session.rollback()
            logger.error(f"[Recovery] Error during startup recovery: {e}")

    return recovered_count


# ═══════════════════════════════════════════════════════════════
# PUBLIC API FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def init_conversion_system(app):
    """
    Initialize persistent conversion system, execute startup recovery, and launch worker pool.
    """
    if app.config.get('TESTING'):
        return None
    manager = ConversionWorkerManager.get_instance(app)
    recover_unfinished_jobs(app)
    manager.start(app)
    return manager


def enqueue_conversion_job(video_id: int, input_path: str, uploader_id: Optional[int] = None) -> ConversionJob:
    """
    Create a persistent ConversionJob record in the database and signal workers.
    """
    from flask import current_app
    app = current_app._get_current_object()

    with app.app_context():
        video = Video.query.get(video_id)
        if not video:
            raise ValueError(f"Video with ID {video_id} does not exist.")

        # Determine output directory
        user = User.query.get(uploader_id or video.uploader_id)
        output_dir = None
        if user and user.institution_id:
            inst = Institution.query.get(user.institution_id)
            if inst:
                output_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'institutions', inst.slug, 'hls', str(video_id))
        if not output_dir:
            output_dir = os.path.join(app.config['HLS_FOLDER'], str(video_id))

        os.makedirs(output_dir, exist_ok=True)

        job_id_str = f"job_{video_id}_{int(time.time())}"
        job = ConversionJob(
            job_id=job_id_str,
            video_id=video_id,
            institution_id=video.institution_id,
            input_file=input_path,
            output_directory=output_dir,
            status='queued',
            progress=0,
            retry_count=0,
            max_retries=MAX_CONVERSION_RETRIES
        )
        db.session.add(job)

        video.status = 'queued'
        video.processing_progress = 0
        db.session.commit()

        logger.info(f"[HLS] Enqueued ConversionJob {job.id} for Video {video_id} ({video.title})")

        # Wake up worker pool
        manager = ConversionWorkerManager.get_instance(app)
        manager.notify()
        return job


def get_active_conversion_jobs(institution_id: Optional[int] = None, admin_id: Optional[int] = None, limit: int = 150) -> List[Dict[str, Any]]:
    """Retrieve all conversion jobs for admin/system_admin monitoring with optional institution/admin filtering."""
    query = ConversionJob.query
    if institution_id:
        query = query.filter(ConversionJob.institution_id == institution_id)
    if admin_id:
        admin_user = User.query.get(admin_id)
        if admin_user and admin_user.institution_id:
            query = query.filter(ConversionJob.institution_id == admin_user.institution_id)
        elif admin_user:
            query = query.join(Video).filter(Video.uploader_id == admin_id)
    jobs = query.order_by(ConversionJob.created_at.desc()).limit(limit).all()
    return [j.to_dict() for j in jobs]


def retry_conversion_job(job_id: int) -> bool:
    """Manually re-queue a failed conversion job."""
    from flask import current_app
    app = current_app._get_current_object()

    with app.app_context():
        job = ConversionJob.query.get(job_id)
        if not job:
            return False

        job.status = 'queued'
        job.error_message = None
        job.retry_count = 0
        job.worker_id = None
        job.updated_at = datetime.utcnow()

        video = Video.query.get(job.video_id)
        if video:
            video.status = 'queued'

        db.session.commit()

        manager = ConversionWorkerManager.get_instance(app)
        manager.notify()
        logger.info(f"[HLS] Manually retried job {job_id}")
        return True


def retry_all_failed_conversion_jobs(institution_id: Optional[int] = None, admin_id: Optional[int] = None) -> int:
    """Manually re-queue all failed and interrupted conversion jobs."""
    from flask import current_app
    app = current_app._get_current_object()

    with app.app_context():
        query = ConversionJob.query.filter(ConversionJob.status.in_(['failed', 'interrupted']))
        if institution_id:
            query = query.filter(ConversionJob.institution_id == institution_id)
        if admin_id:
            admin_user = User.query.get(admin_id)
            if admin_user and admin_user.institution_id:
                query = query.filter(ConversionJob.institution_id == admin_user.institution_id)
            elif admin_user:
                query = query.join(Video).filter(Video.uploader_id == admin_id)

        failed_jobs = query.all()
        retried_count = 0
        for job in failed_jobs:
            job.status = 'queued'
            job.error_message = None
            job.retry_count = 0
            job.worker_id = None
            job.updated_at = datetime.utcnow()

            video = Video.query.get(job.video_id)
            if video:
                video.status = 'queued'
            retried_count += 1

        db.session.commit()

        if retried_count > 0:
            manager = ConversionWorkerManager.get_instance(app)
            manager.notify()
            logger.info(f"[HLS] Re-queued {retried_count} failed conversion jobs (inst={institution_id}, admin={admin_id})")

        return retried_count


def cancel_conversion_job(job_id: int) -> bool:
    """Cancel a conversion job, terminating any running FFmpeg process immediately."""
    with _active_processes_lock:
        proc = _active_job_processes.pop(job_id, None)
        if proc:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception as e:
                logger.debug(f"Could not kill process for job {job_id}: {e}")

    from flask import current_app
    app = None
    try:
        app = current_app._get_current_object()
    except Exception:
        pass

    manager = ConversionWorkerManager._instance
    if not app and manager:
        app = manager.app

    if not app:
        try:
            from app import app as fallback_app
            app = fallback_app
        except Exception:
            pass

    if app:
        with app.app_context():
            from models import ConversionJob, Video
            from extensions import db

            job = ConversionJob.query.get(job_id)
            if not job:
                return False

            if job.worker_id and manager and job.worker_id in manager.stop_events:
                manager.stop_events[job.worker_id].set()

            job.status = 'failed'
            job.error_message = 'Cancelled by administrator'
            job.updated_at = datetime.utcnow()

            video = Video.query.get(job.video_id)
            if video and video.status != 'completed':
                video.status = 'failed'

            db.session.commit()
            logger.info(f"[HLS] Cancelled job {job_id}")
            return True
    return False


def cancel_conversion_jobs_for_video(video_id: int) -> int:
    """
    Immediately terminate all running FFmpeg processes and cancel all active/queued
    conversion jobs associated with a video.
    """
    if not video_id:
        return 0

    cancelled_count = 0
    with _active_processes_lock:
        job_ids = list(_active_video_jobs.get(video_id, []))
        for jid in job_ids:
            proc = _active_job_processes.pop(jid, None)
            if proc:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                except Exception as e:
                    logger.debug(f"Could not kill process for job {jid}: {e}")
        _active_video_jobs.pop(video_id, None)

    manager = ConversionWorkerManager._instance
    from flask import current_app
    app = None
    try:
        app = current_app._get_current_object()
    except Exception:
        pass

    if not app and manager:
        app = manager.app

    if not app:
        try:
            from app import app as fallback_app
            app = fallback_app
        except Exception:
            pass

    if app:
        with app.app_context():
            from models import ConversionJob
            from extensions import db

            jobs = ConversionJob.query.filter_by(video_id=video_id).all()
            for job in jobs:
                if job.worker_id and manager and job.worker_id in manager.stop_events:
                    try:
                        manager.stop_events[job.worker_id].set()
                    except Exception:
                        pass

                if job.status in ['queued', 'processing', 'interrupted']:
                    job.status = 'failed'
                    job.error_message = 'Cancelled due to video deletion'
                    job.updated_at = datetime.utcnow()
                    cancelled_count += 1

            db.session.commit()
            if cancelled_count > 0:
                logger.info(f"[HLS] Cancelled {cancelled_count} active conversion jobs for Video {video_id}")

    return cancelled_count
