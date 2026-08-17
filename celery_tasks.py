"""
Celery background tasks for CampusPlayer.
Run: celery -A celery_config.celery worker --loglevel=info
"""
import os
import subprocess
import shutil
import time
import json
import logging
from datetime import datetime, timedelta
from celery import Celery, Task, shared_task
from celery_config import celery, make_celery
from factory import create_app

logger = logging.getLogger(__name__)
flask_app = create_app()

def get_ffmpeg_bin():
    """Resolve absolute path to ffmpeg binary."""
    path = shutil.which('ffmpeg')
    if path:
        return path
    for candidate in ['/usr/bin/ffmpeg', '/usr/local/bin/ffmpeg', 'C:\\ffmpeg\\bin\\ffmpeg.exe']:
        if os.path.exists(candidate):
            return candidate
    return 'ffmpeg'

def get_ffprobe_bin():
    """Resolve absolute path to ffprobe binary."""
    path = shutil.which('ffprobe')
    if path:
        return path
    for candidate in ['/usr/bin/ffprobe', '/usr/local/bin/ffprobe', 'C:\\ffmpeg\\bin\\ffprobe.exe']:
        if os.path.exists(candidate):
            return candidate
    return 'ffprobe'

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
    {'name': '16K',   'width': 15360, 'height': 8640, 'bitrate': '250000k','maxrate':'300000k','bufsize': '500000k'},
]

FFMPEG_PRESET = 'ultrafast'
FFMPEG_HLS_TIME = 10  # 10 second segments


def probe_video(input_path):
    """Get video metadata using ffprobe."""
    info = {'duration': 0, 'width': 0, 'height': 0, 'bitrate': 0, 'codec': 'h264', 'fps': 0, 'has_audio': True}
    try:
        cmd = [
            get_ffprobe_bin(), '-v', 'quiet', '-print_format', 'json',
            '-analyzeduration', '10M', '-probesize', '10M',
            '-show_format', '-show_streams', input_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        data = json.loads(result.stdout)

        if 'format' in data:
            info['duration'] = float(data['format'].get('duration', 0))
            info['bitrate'] = int(data['format'].get('bit_rate', 0))

        has_audio = False
        if 'streams' in data:
            video_candidates = []
            for stream in data['streams']:
                if stream.get('codec_type') == 'video':
                    disp = stream.get('disposition', {})
                    is_attached = (disp.get('attached_pic') == 1)
                    codec_name = stream.get('codec_name', '').lower()
                    is_still = codec_name in ['mjpeg', 'png', 'webp', 'bmp', 'gif']
                    w = int(stream.get('width', 0))
                    h = int(stream.get('height', 0))
                    if w > 0 and h > 0:
                        video_candidates.append({
                            'stream': stream,
                            'w': w,
                            'h': h,
                            'is_attached': is_attached,
                            'is_still': is_still,
                            'pixels': w * h
                        })
                elif stream.get('codec_type') == 'audio' and not has_audio:
                    has_audio = True
                    info['audio_codec'] = stream.get('codec_name', 'aac')

            if video_candidates:
                video_candidates.sort(key=lambda c: (not c['is_attached'], not c['is_still'], c['pixels']), reverse=True)
                chosen = video_candidates[0]
                v_stream = chosen['stream']
                w = chosen['w']
                h = chosen['h']
                side_data = v_stream.get('side_data_list', [])
                rotation = 0
                for sd in side_data:
                    if 'rotation' in sd:
                        try:
                            rotation = abs(int(sd['rotation']))
                        except:
                            pass
                tags = v_stream.get('tags', {})
                if 'rotate' in tags:
                    try:
                        rotation = abs(int(tags['rotate']))
                    except:
                        pass
                if rotation in [90, 270]:
                    w, h = h, w

                info['width'] = w
                info['height'] = h
                info['codec'] = v_stream.get('codec_name', 'h264')
                fps_str = v_stream.get('r_frame_rate', '0/1')
                if '/' in fps_str:
                    num, den = fps_str.split('/')
                    info['fps'] = float(num) / float(den) if float(den) > 0 else 0

        info['has_audio'] = has_audio

    except Exception as e:
        logger.warning(f"Probe failed for {input_path}: {e}")

    return info


def encode_single_rendition(input_path, output_dir, quality, video_id, app):
    """
    Encode a single quality rendition to HLS directly in output_dir (no quality subfolders).
    Returns dict with rendition info or error.
    """
    rendition_name = quality['name']
    output_playlist = os.path.join(output_dir, f'{rendition_name}.m3u8')
    source_info = probe_video(input_path)

    cmd = [
        get_ffmpeg_bin(), '-y',
        '-i', input_path,
        '-c:v', 'libx264',
        '-preset', FFMPEG_PRESET,
        '-profile:v', 'main',
        '-pix_fmt', 'yuv420p',
    ]

    if source_info.get('has_audio', True):
        cmd.extend(['-c:a', 'aac', '-ac', '2', '-b:a', '128k'])
    else:
        cmd.extend(['-an'])

    cmd.extend([
        '-vf', f'scale={quality["width"]}:{quality["height"]}:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2',
        '-b:v', quality['bitrate'],
        '-maxrate', quality['maxrate'],
        '-bufsize', quality['bufsize'],
        '-start_number', '0',
        '-hls_time', str(FFMPEG_HLS_TIME),
        '-hls_list_size', '0',
        '-hls_segment_filename', os.path.join(output_dir, f'{rendition_name}_%05d.ts'),
        '-f', 'hls',
        output_playlist
    ])

    logger.info(f"Starting {rendition_name} encode for video {video_id}")

    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True
        )
        stdout, stderr = process.communicate()

        if process.returncode != 0:
            err_msg = stderr[:500] if stderr else "Unknown error"
            return {'error': f"FFmpeg error ({rendition_name}): {err_msg}"}

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
        with open(output_playlist, 'r') as f:
            for line in f:
                line = line.strip()
                if line.endswith('.ts') and not line.startswith('#'):
                    seg_path = os.path.join(output_dir, line)
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
            'playlist': f'{rendition_name}.m3u8',
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
    """
    Generate master.m3u8 playlist combining all renditions in output_dir.
    Returns the filename (master.m3u8).
    """
    master_path = os.path.join(output_dir, 'master.m3u8')

    # Sort renditions by resolution (highest first)
    sorted_renditions = sorted(
        renditions,
        key=lambda r: r.get('height', 0),
        reverse=True
    )

    with open(master_path, 'w') as f:
        f.write('#EXTM3U\n')
        f.write('#EXT-X-VERSION:3\n')
        f.write(f'# Generated: {datetime.utcnow().isoformat()}\n')

        for r in sorted_renditions:
            bandwidth = r.get('bandwidth', 5000000)
            resolution = r.get('resolution', '1920x1080')
            codecs = r.get('codecs', 'avc1.4d401e,mp4a.40.2')
            playlist = r.get('playlist', '')

            f.write(f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={resolution},CODECS="{codecs}"\n')
            f.write(f'{playlist}\n')

    return 'master.m3u8'


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def process_video_task(self, video_id, input_path):
    """
    Celery task to transcode uploaded video to multi-rendition HLS.
    """
    from factory import create_app
    flask_app = create_app()

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

            # Source probe for dimensions
            source_info = probe_video(input_path)
            src_w = source_info.get('width', 0)
            src_h = source_info.get('height', 0)
            src_max_dim = max(src_w, src_h)
            src_min_dim = min(src_w, src_h)

            # Determine which qualities to generate based on source resolution
            qualities_to_encode = []
            for q in QUALITY_LADDER:
                q_h = q['height']
                q_w = q['width']
                q_max_dim = max(q_w, q_h)
                q_min_dim = min(q_w, q_h)

                if src_min_dim >= (q_min_dim - 24) or src_max_dim >= (q_max_dim - 50):
                    qualities_to_encode.append(q)
                elif not qualities_to_encode and q['name'] == QUALITY_LADDER[0]['name']:
                    qualities_to_encode.append(q)

            if not qualities_to_encode:
                qualities_to_encode = [QUALITY_LADDER[0]]

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
                from services.video_cleanup import permanently_delete_video_assets
                cleanup_res = permanently_delete_video_assets(video, app_instance=flask_app)
                logger.info(f"Permanent auto-deletion file cleanup for video #{video.id}: {cleanup_res}")

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


@celery.task
def process_academic_year_rollovers():
    """Automated background task to process academic-year rollovers, student promotions,
    final-year review flagging, and data archiving per institution."""
    with flask_app.app_context():
        from models import SiteSettings, StudentProfile, Video, Attendance, QuizResult, ActivityLog, Institution
        from extensions import db

        now = datetime.utcnow()
        eligible_settings = SiteSettings.query.filter(
            SiteSettings.scheduled_academic_year_end_date.isnot(None),
            SiteSettings.scheduled_academic_year_end_date <= now,
            SiteSettings.academic_year_rollover_processed == False
        ).all()

        results = []

        year_promotion_map = {
            '1st Year': '2nd Year',
            '2nd Year': '3rd Year',
            '3rd Year': '4th Year',
            '1': '2nd Year',
            '2': '3rd Year',
            '3': '4th Year',
            'I': '2nd Year',
            'II': '3rd Year',
            'III': '4th Year',
        }

        for setting in eligible_settings:
            inst_id = setting.institution_id
            inst = Institution.query.get(inst_id) if inst_id else None
            inst_name = inst.name if inst else "Default Institution"

            try:
                with db.session.begin_nested():
                    # 1. Promote Student Profiles & Flag Final-Year Students
                    students = StudentProfile.query.filter_by(institution_id=inst_id).all() if inst_id else StudentProfile.query.all()
                    promoted_count = 0
                    review_flagged_count = 0

                    for sp in students:
                        current_yr = (sp.year or '').strip()
                        if current_yr in year_promotion_map:
                            sp.year = year_promotion_map[current_yr]
                            promoted_count += 1
                        elif current_yr in ['4th Year', '4', 'IV', 'Final Year']:
                            sp.requires_admin_review = True
                            review_flagged_count += 1

                    # 2. Archive Outgoing Year Data (Videos, Attendance, Quiz Results)
                    video_filter = {'institution_id': inst_id, 'is_archived': False} if inst_id else {'is_archived': False}
                    attendance_filter = {'institution_id': inst_id, 'is_archived': False} if inst_id else {'is_archived': False}
                    quiz_res_filter = {'institution_id': inst_id, 'is_archived': False} if inst_id else {'is_archived': False}

                    archived_videos = Video.query.filter_by(**video_filter).update({'is_archived': True, 'archived_at': now}, synchronize_session=False)
                    archived_attendance = Attendance.query.filter_by(**attendance_filter).update({'is_archived': True, 'archived_at': now}, synchronize_session=False)
                    archived_quizzes = QuizResult.query.filter_by(**quiz_res_filter).update({'is_archived': True, 'archived_at': now}, synchronize_session=False)

                    # 3. Mark Settings as Processed
                    setting.academic_year_rollover_processed = True

                    # 4. Activity Log
                    log = ActivityLog(
                        institution_id=inst_id,
                        action='ACADEMIC_YEAR_ROLLOVER',
                        details=(f"Academic rollover processed for '{inst_name}'. Promoted: {promoted_count}, "
                                 f"Review Flags: {review_flagged_count}, Archived Videos: {archived_videos}, "
                                 f"Archived Attendance: {archived_attendance}, Archived Quizzes: {archived_quizzes}.")
                    )
                    db.session.add(log)

                db.session.commit()
                logger.info(f"Successfully processed academic rollover for institution '{inst_name}' (ID: {inst_id})")
                results.append({
                    'institution_id': inst_id,
                    'institution_name': inst_name,
                    'promoted_students': promoted_count,
                    'flagged_reviews': review_flagged_count,
                    'archived_videos': archived_videos,
                    'archived_attendance': archived_attendance,
                    'archived_quizzes': archived_quizzes,
                    'status': 'success'
                })
            except Exception as e:
                db.session.rollback()
                logger.error(f"Error processing academic rollover for institution ID {inst_id}: {e}")
                results.append({
                    'institution_id': inst_id,
                    'institution_name': inst_name,
                    'error': str(e),
                    'status': 'failed'
                })

        return {'rollovers_processed': len(results), 'details': results}