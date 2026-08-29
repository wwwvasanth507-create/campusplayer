"""
System Health, Readiness & Telemetry API Blueprint for CampusPlayer.
"""
import os
import sys
import shutil
import psutil
from datetime import datetime
from flask import Blueprint, jsonify, current_app
from extensions import db, cache
from services.storage_backend import get_storage_backend

api_bp = Blueprint('api_system', __name__)

@api_bp.route('/api/health', methods=['GET'])
def api_health():
    """Startup & Liveness Probe verifying PostgreSQL, Redis, Storage, and FFmpeg binaries."""
    status = {
        'status': 'healthy',
        'timestamp': datetime.utcnow().isoformat(),
        'components': {}
    }

    # 1. Database Check
    try:
        db.session.execute(db.text('SELECT 1'))
        status['components']['postgres'] = {'status': 'up'}
    except Exception as e:
        status['components']['postgres'] = {'status': 'down', 'error': str(e)}
        status['status'] = 'degraded'

    # 2. Redis / Cache Check
    try:
        cache.set('health_ping', 'ok', timeout=10)
        cached_val = cache.get('health_ping')
        if cached_val == 'ok':
            status['components']['redis'] = {'status': 'up'}
        else:
            status['components']['redis'] = {'status': 'down', 'error': 'cache get mismatch'}
            status['status'] = 'degraded'
    except Exception as e:
        status['components']['redis'] = {'status': 'down', 'error': str(e)}
        status['status'] = 'degraded'

    # 3. Storage Check
    try:
        storage = get_storage_backend()
        storage.save_bytes('health_test.tmp', b'ping')
        storage.delete_file('health_test.tmp')
        status['components']['storage'] = {'status': 'up', 'provider': os.getenv('STORAGE_BACKEND', 'local')}
    except Exception as e:
        status['components']['storage'] = {'status': 'down', 'error': str(e)}
        status['status'] = 'degraded'

    # 4. FFmpeg / FFprobe Check
    ffmpeg_path = shutil.which('ffmpeg') or os.getenv('FFMPEG_PATH')
    ffprobe_path = shutil.which('ffprobe') or os.getenv('FFPROBE_PATH')

    if not ffmpeg_path:
        try:
            import imageio_ffmpeg
            ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass

    status['components']['ffmpeg'] = {
        'status': 'up' if ffmpeg_path else 'down',
        'ffmpeg_binary': ffmpeg_path or 'not_found',
        'ffprobe_binary': ffprobe_path or 'not_found'
    }

    http_code = 200 if status['status'] == 'healthy' else 503
    return jsonify(status), http_code


@api_bp.route('/api/ready', methods=['GET'])
def api_ready():
    """Readiness Probe for Kubernetes / Load Balancer routing."""
    try:
        db.session.execute(db.text('SELECT 1'))
        return jsonify({'ready': True, 'timestamp': datetime.utcnow().isoformat()}), 200
    except Exception as e:
        return jsonify({'ready': False, 'error': str(e)}), 503


@api_bp.route('/api/version', methods=['GET'])
def api_version():
    """API version & runtime environment info."""
    return jsonify({
        'name': 'CampusPlayer',
        'version': '3.0.0-production',
        'environment': os.getenv('APP_ENV', 'production'),
        'python_version': sys.version.split()[0]
    }), 200


@api_bp.route('/api/telemetry', methods=['GET'])
def api_telemetry():
    """System resource telemetry (CPU, RAM, Disk)."""
    try:
        cpu_pct = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('.')
        return jsonify({
            'cpu_usage_pct': cpu_pct,
            'ram_used_mb': round(mem.used / (1024 * 1024), 2),
            'ram_total_mb': round(mem.total / (1024 * 1024), 2),
            'ram_pct': mem.percent,
            'disk_free_gb': round(disk.free / (1024 * 1024 * 1024), 2),
            'disk_pct': disk.percent,
            'timestamp': datetime.utcnow().isoformat()
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
