"""
Celery background tasks for CampusPlayer.
Run: celery -A celery_config.celery worker --loglevel=info
"""
import os
import subprocess
import time
import json
import logging
from datetime import datetime, timedelta
from celery import Celery, Task
from celery_config import celery, make_celery
from factory import create_app

logger = logging.getLogger(__name__)
flask_app = create_app()

def init_celery(app):
    """Initialize celery with Flask app context."""
    global celery
    celery = make_celery(app)
    return celery


# Quality ladder definitions for adaptive HLS — FULL SPECTRUM 144p → 8K
QUALITY_LADDER = [
    {'name': '144p',  'width': 256,  'height': 144,  'bitrate': '80k',   'maxrate': '100k',   'bufsize': '160k'},
    {'name': '240p',  'width': 426,  'height': 240,  'bitrate': '200k',  'maxrate': '250k',   'bufsize': '400k'},
    {'name': '360p',  'width': 640,  'height': 360,  'bitrate': '500k',  'maxrate': '600k',   'bufsize': '1000k'},
    {'name': '480p',  'width': 854,  'height': 480,  'bitrate': '1000k', 'maxrate': '1200k',  'bufsize': '2000k'},
    {'name': '720p',  'width': 1280, 'height': 720,  'bitrate': '2500k', 'maxrate': '3000k',  'bufsize': '5000k'},
    {'name': '1080p', 'width': 1920, 'height': 1080, 'bitrate': '5000k', 'maxrate': '6000k',  'bufsize': '10000k'},
    {'name': '2K',    'width': 2560, 'height': 1440, 'bitrate': '12000k','maxrate': '15000k', 'bufsize': '24000k'},
    {'name': '4K',    'width': 3840, 'height': 2160, 'bitrate': '35000k','maxrate': '45000k', 'bufsize': '70000k'},
    {'name': '8K',    'width': 7680, 'height': 4320, 'bitrate': '100000k','maxrate':'120000k','bufsize': '200000k'},
]

FFMPEG_PRESET = 'ultrafast'
FFMPEG_HLS_TIME = 10  # 10 second segments


def probe_video(input_path):
    """Get video metadata using ffprobe."""
    info = {'duration': 0, 'width': 0, 'height': 0, 'bitrate': 0, 'codec': 'h264', 'fps': 0}
    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', input_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        data = json.loads(result.stdout)

        if 'format' in data:
            info['duration'] = float(data['format'].get('duration', 0))
            info['bitrate'] = int(data['format'].get('bit_rate', 0))

        if 'streams' in data:
            for stream in data['streams']:
                if stream['codec_type'] == 'video':
                    info['width'] = int(stream.get('width', 0))
                    info['height'] = int(stream.get('height', 0))
                    info['codec'] = stream.get('codec_name', 'h264')
                    fps_str = stream.get('r_frame_rate', '0/1')
                    if '/' in fps_str:
                        num, den = fps_str.split('/')
                        info['fps'] = float(num) / float(den) if float(den) > 0 else 0
                    break

        for stream in data.get('streams', []):
            if stream['codec_type'] == 'audio':
                info['audio_codec'] = stream.get('codec_name', 'aac')
                break

    except Exception as e:
        logger.warning(f"Probe failed for {input_path}: {e}")

    return info


def encode_single_rendition(input_path, output_dir, quality, video_id, app):
    """
    Encode a single quality rendition to HLS.
    Returns dict with rendition info or error.
    """
    rendition_name = quality['name']
    rendition_dir = os.path.join(output_dir, rendition_name)
    os.makedirs(rendition_dir, exist_ok=True)

    output_playlist = os.path.join(rendition_dir, f'{rendition_name}.m3u8')

    cmd = [
        'ffmpeg', '-y',
        '-i', input_path,
        '-c:v', 'libx264',
        '-preset', FFMPEG_PRESET,
        '-profile:v', 'main',
        '-c:a', 'aac',
        '-ac', '2',
        '-b:a', '128k',
        '-vf', f'scale={quality["width"]}:{quality["height"]}:force_original_aspect_ratio=decrease,'
               f'pad={quality["width"]}:{quality["height"]}:(ow-iw)/2:(oh-ih)/2',
        '-b:v', quality['bitrate'],
        '-maxrate', quality['maxrate'],
        '-bufsize', quality['bufsize'],
        '-start_number', '0',
        '-hls_time', str(FFMPEG_HLS_TIME),
        '-hls_list_size', '0',
        '-hls_segment_filename', os.path.join(rendition_dir, f'{rendition_name}_%05d.ts'),
        '-f', 'hls',
        output_playlist
    ]

    logger.info(f"Starting {rendition_name} encode for video {video_id}")

    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1
        )

        # Read and discard stdout/stderr line by line to prevent memory buildup
        for _ in process.stdout:
            pass

        process.wait()

        if process.returncode != 0:
            stderr = process.stderr.read()[:500]
            return {'error': f"FFmpeg error ({rendition_name}): {stderr}"}

        if not os.path.exists(output_playlist):
            return {'error': f"Output playlist not created for {rendition_name}"}

        # Count segments
        segment_count = 0
        with open(output_playlist, 'r') as f:
            for line in f:
                if line.strip().endswith('.ts'):
                    segment_count += 1

        # Calculate actual bitrate from first segment
        actual_bitrate = quality['bitrate']
        for line in open(output_playlist):
            line = line.strip()
            if line.endswith('.ts') and not line.startswith('#'):
                seg_path = os.path.join(rendition_dir, line)
                if os.path.exists(seg_path):
                    size = os.path.getsize(seg_path)
                    est_bitrate = (size * 8) // FFMPEG_HLS_TIME
                    actual_bitrate = f'{int(est_bitrate / 1000)}k'
                break

        result = {
            'name': rendition_name,
            'width': quality['width'],
            'height': quality['height'],
            'bitrate': actual_bitrate,
            'playlist': f'{rendition_name}/{rendition_name}.m3u8',
            'bandwidth': int(quality['bitrate'].replace('k', '000')),
            'resolution': f'{quality["width"]}x{quality["height"]}',
            'codecs': 'avc1.4d401e,mp4a.40.2',
            'segments': segment_count
        }

        logger.info(f"{rendition_name} encode complete: {segment_count} segments for video {video_id}")
        return {'result': result}

    except Exception as e:
        return {'error': f"{rendition_name} encode exception: {str(e)}"}


def generate_master_playlist(output_dir, renditions):
    """Generate master.m3u8 playlist with all renditions."""
    master_path = os.path.join(output_dir, 'master.m3u8')

    sorted_renditions = sorted(
        renditions,
        key=lambda r: r.get('height', 0),
        reverse=True
    )

    with open(master_path, 'w') as f:
        f.write('#EXTM3U\n')
        f.write('#EXT-X-VERSION:6\n')
        f.write(f'# Generated: {datetime.utcnow().isoformat()}\n')
        f.write(f'# Adaptive HLS with {len(sorted_renditions)} quality levels\n')

        for r in sorted_renditions:
            bandwidth = r.get('bandwidth', 5000000)
            resolution = r.get('resolution', '1920x1080')
            codecs = r.get('codecs', 'avc1.4d401e,mp4a.40.2')
            playlist = r.get('playlist', '')

            f.write(f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={resolution},CODECS="{codecs}"\n')
            f.write(f'{playlist}\n')

    logger.info(f"Master playlist created: {master_path}")
    return 'master.m3u8'


@celery.task(bind=True, max_retries=3, acks_late=True)
def process_video_task(self, video_id, input_path):
    """Background task to convert video to HLS with adaptive quality ladder.
    Handles videos of ANY duration (including 30+ hours) with proper timeout handling.
    """
    with flask_app.app_context():
        from models import Video, User
        from extensions import db

        video = Video.query.get(video_id)
        if not video:
            return {'error': 'Video not found'}

        try:
            video.status = 'processing'
            video.processing_progress = 5
            db.session.commit()

            # Get duration via ffprobe
            cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                   '-of', 'default=noprint_wrappers=1:nokey=1', input_path]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
            duration = float(result.stdout.strip()) if result.stdout.strip() else 0
            video.duration_seconds = int(duration)

            logger.info(f"Video {video_id}: duration={duration}s, input={input_path}")

            # Source probe for max height
            source_info = probe_video(input_path)
            source_height = source_info.get('height', 1080) or 1080

            # Determine which qualities to generate based on source height
            qualities_to_encode = [q for q in QUALITY_LADDER if q['height'] <= source_height]
            if not qualities_to_encode:
                qualities_to_encode = [QUALITY_LADDER[2]]  # Default to 360p minimum

            db.session.commit()

            # Create HLS output directory
            output_dir = flask_app.config['HLS_FOLDER']
            video_hls_dir = os.path.join(output_dir, str(video_id))
            os.makedirs(video_hls_dir, exist_ok=True)

            # Encode each rendition sequentially to avoid overwhelming the system
            # (Sequential is more reliable for very long videos on shared systems)
            renditions = []
            errors = []
            total_qualities = len(qualities_to_encode)

            for idx, quality in enumerate(qualities_to_encode):
                # Update base progress (10% to 80% across all renditions)
                base_progress = 10 + int((idx / total_qualities) * 70)
                video.processing_progress = base_progress
                video.status = f'encoding_{quality["name"]}'
                db.session.commit()

                logger.info(f"Video {video_id}: Starting {quality['name']} encode ({idx+1}/{total_qualities})")

                encode_result = encode_single_rendition(
                    input_path, video_hls_dir, quality, video_id, flask_app
                )

                if 'error' in encode_result:
                    errors.append(encode_result['error'])
                    logger.error(f"Video {video_id} {quality['name']} failed: {encode_result['error']}")
                else:
                    renditions.append(encode_result['result'])
                    logger.info(f"Video {video_id} {quality['name']} completed successfully")

            # Check if we have at least one successful rendition
            if not renditions:
                raise Exception(f"All renditions failed. Errors: {errors[:3]}")

            # Generate master playlist
            master_playlist_name = generate_master_playlist(video_hls_dir, renditions)
            master_path = os.path.join(video_hls_dir, master_playlist_name)

            # Generate thumbnail
            thumb_cmd = [
                'ffmpeg', '-y', '-i', input_path,
                '-ss', '00:00:05', '-vframes', '1',
                '-vf', f'scale={source_info.get("width", 1280)}:{source_info.get("height", 720)}',
                '-q:v', '5',
                os.path.join(video_hls_dir, 'thumbnail.jpg')
            ]
            subprocess.run(thumb_cmd, capture_output=True, timeout=60)

            # Update video record
            if os.path.exists(master_path):
                video.hls_playlist_path = f'hls/{video_id}/{master_playlist_name}'
                video.master_playlist_path = f'hls/{video_id}/{master_playlist_name}'
                video.has_adaptive_streams = len(renditions) > 1

            if os.path.exists(os.path.join(video_hls_dir, 'thumbnail.jpg')):
                video.thumbnail_path = f'hls/{video_id}/thumbnail.jpg'

            # Store renditions
            video.set_renditions(renditions)

            # Update source resolution
            if renditions:
                video.source_width = renditions[0].get('width', 1920)
                video.source_height = renditions[0].get('height', 1080)

            video.status = 'completed' if not errors else 'completed_with_errors'
            video.processing_progress = 100

            # Award XP to uploader
            uploader = User.query.get(video.uploader_id)
            if uploader:
                uploader.xp += 50

            db.session.commit()

            summary = f"Video {video_id}: {len(renditions)}/{total_qualities} qualities, {errors[:2] if errors else 'no errors'}"

            # Log final segment counts
            for r in renditions:
                logger.info(f"  {r['name']}: {r.get('segments', '?')} segments")

            logger.info(f"Video {video_id} processed successfully via Celery: {summary}")
            return {
                'status': 'completed',
                'video_id': video_id,
                'renditions': len(renditions),
                'total_duration': duration,
                'warnings': errors[:3] if errors else []
            }

        except Exception as e:
            logger.error(f"Video processing error: {e}")
            try:
                video.status = 'failed'
                video.processing_progress = 0
                db.session.commit()
            except:
                pass
            raise self.retry(exc=e, countdown=60)


@celery.task
def send_pending_emails():
    """Process pending email queue."""
    with flask_app.app_context():
        from models import EmailQueue
        from extensions import db, mail
        from flask_mail import Message

        pending = EmailQueue.query.filter_by(status='pending').order_by(EmailQueue.created_at).limit(10).all()
        for email in pending:
            try:
                msg = Message(
                    subject=email.subject,
                    recipients=[email.recipient_email],
                    body=email.body_text or '',
                    html=email.body_html or ''
                )
                mail.send(msg)
                email.status = 'sent'
                email.sent_at = datetime.utcnow()
            except Exception as e:
                email.retry_count += 1
                email.error_message = str(e)[:200]
                if email.retry_count >= 3:
                    email.status = 'failed'
            db.session.commit()
    return {'sent': len([e for e in pending if e.status == 'sent'])}


@celery.task
def cleanup_temp_files():
    """Clean up old temporary files."""
    import shutil

    upload_dir = flask_app.config['UPLOAD_FOLDER']
    now = time.time()
    cleaned = 0

    for root, dirs, files in os.walk(upload_dir):
        for f in files:
            filepath = os.path.join(root, f)
            if os.path.isfile(filepath) and (now - os.path.getmtime(filepath)) > 86400:  # 1 day
                try:
                    os.remove(filepath)
                    cleaned += 1
                except:
                    pass
    return {'cleaned_files': cleaned}


@celery.task
def record_system_metrics():
    """Record system metrics periodically."""
    with flask_app.app_context():
        from models import SystemMetric
        from extensions import db
        import psutil

        metrics = [
            ('cpu_percent', psutil.cpu_percent()),
            ('memory_percent', psutil.virtual_memory().percent),
            ('disk_percent', psutil.disk_usage('/').percent),
        ]

        for name, value in metrics:
            metric = SystemMetric(metric_name=name, metric_value=value)
            db.session.add(metric)

        db.session.commit()
    return {'metrics_recorded': len(metrics)}


@celery.task(name='cleanup_expired_videos_task')
def cleanup_expired_videos_task():
    """
    Automated background worker to purge videos past their fixed expiration date.
    Respects Sysadmin 'allow_auto_video_delete' per-institution permission settings.
    """
    with flask_app.app_context():
        from models import Video, Institution, Notification
        from extensions import db
        import os
        import shutil

        now = datetime.utcnow()
        expired_videos = Video.query.filter(
            Video.auto_delete_at.isnot(None),
            Video.auto_delete_at <= now
        ).all()

        deleted_count = 0
        skipped_count = 0

        for video in expired_videos:
            inst = Institution.query.get(video.institution_id) if video.institution_id else None
            
            # Check if Sysadmin allowed auto-deletion for this institution
            if inst and not inst.allow_auto_video_delete:
                logger.info(f"Skipping auto-deletion for video #{video.id} ({video.title}): Disabled by Sysadmin for institution '{inst.name}'")
                skipped_count += 1
                continue

            try:
                # Remove original uploaded file
                input_path = os.path.join(flask_app.config['UPLOAD_FOLDER'], video.filename)
                if os.path.exists(input_path):
                    os.remove(input_path)

                # Remove HLS folder and derived assets
                hls_dirs_to_remove = set()
                for rel in (video.hls_playlist_path, video.master_playlist_path):
                    if rel:
                        hls_dirs_to_remove.add(os.path.dirname(os.path.join(flask_app.root_path, 'static', rel)))
                hls_dirs_to_remove.add(os.path.join(flask_app.config['HLS_FOLDER'], str(video.id)))

                for hls_dir in hls_dirs_to_remove:
                    if hls_dir and os.path.exists(hls_dir):
                        shutil.rmtree(hls_dir, ignore_errors=True)

                # Notify uploader
                notif = Notification(
                    user_id=video.uploader_id,
                    institution_id=video.institution_id,
                    message=f"⏰ Video '{video.title}' was automatically deleted as scheduled (Expiration Date: {video.auto_delete_at.strftime('%Y-%m-%d %H:%M UTC')}).",
                    notification_type='info'
                )
                db.session.add(notif)

                # Remove DB record
                db.session.delete(video)
                deleted_count += 1
                logger.info(f"Auto-deleted expired video #{video.id} '{video.title}'")

            except Exception as e:
                logger.error(f"Error during auto-deletion of video #{video.id}: {e}")

        db.session.commit()
        return {'auto_deleted': deleted_count, 'skipped_by_sysadmin_policy': skipped_count}


@celery.task
def refresh_leaderboard():
    """Refresh leaderboard cache."""
    with flask_app.app_context():
        from models import User, LeaderboardEntry
        from extensions import db

        # Clear old entries
        LeaderboardEntry.query.delete()

        # Add all users
        users = User.query.order_by(User.xp.desc()).all()
        for rank, user in enumerate(users, 1):
            entry = LeaderboardEntry(
                user_id=user.id,
                username=user.username,
                role=user.role,
                xp=user.xp,
                level=(user.xp // 500) + 1,
                streak_days=user.streak_days,
                quiz_count=user.total_quizzes_taken,
                category='global',
                rank=rank
            )
            db.session.add(entry)

        db.session.commit()
    return {'users_refreshed': len(users)}