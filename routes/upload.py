"""
Resumable Multipart Video Upload API Blueprint.
"""
from flask import Blueprint, request, jsonify, session, abort
from flask_login import login_required, current_user
from services.upload_engine import (
    init_upload_session, save_upload_part, get_upload_status,
    complete_upload_session, abort_upload_session
)
from services.utils import get_current_institution_id

upload_bp = Blueprint('upload_api', __name__)

@upload_bp.route('/api/uploads/init', methods=['POST'])
@login_required
def api_init_upload():
    """Initialize a new resumable multipart upload session."""
    data = request.get_json(silent=True) or request.form
    original_filename = data.get('filename') or data.get('original_filename')
    title = data.get('title') or original_filename
    total_bytes = int(data.get('total_bytes') or data.get('file_size') or 0)
    part_size = data.get('part_size')
    part_size = int(part_size) if part_size else None

    if not original_filename or total_bytes <= 0:
        return jsonify({'error': 'filename and valid total_bytes are required'}), 400

    inst_id = get_current_institution_id()
    if not inst_id and current_user.role != 'system_admin':
        return jsonify({'error': 'Institution context required'}), 403

    try:
        session_obj = init_upload_session(
            institution_id=inst_id or 1,
            uploader_id=current_user.id,
            original_filename=original_filename,
            title=title,
            total_bytes=total_bytes,
            part_size=part_size,
            description=data.get('description'),
            subject=data.get('subject'),
            grade_level=data.get('grade_level'),
            content_type=data.get('content_type', 'video/mp4')
        )

        return jsonify({
            'upload_id': session_obj.upload_id,
            'part_size': session_obj.part_size,
            'total_parts': session_obj.total_parts,
            'status': session_obj.status,
            'storage_path': session_obj.storage_path
        }), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@upload_bp.route('/api/uploads/<upload_id>/parts/<int:part_number>', methods=['POST', 'PUT'])
@login_required
def api_upload_part(upload_id, part_number):
    """Upload a single chunk stream."""
    if 'file' in request.files:
        file_obj = request.files['file']
        stream = file_obj.stream
    else:
        stream = request.stream

    if not stream:
        return jsonify({'error': 'No file or stream data received'}), 400

    try:
        part = save_upload_part(
            upload_id=upload_id,
            part_number=part_number,
            part_stream=stream
        )
        return jsonify({
            'upload_id': upload_id,
            'part_number': part.part_number,
            'part_size': part.part_size,
            'etag_checksum': part.etag_checksum,
            'status': 'received'
        }), 200
    except ValueError as val_err:
        return jsonify({'error': str(val_err)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@upload_bp.route('/api/uploads/<upload_id>', methods=['GET'])
@login_required
def api_get_upload_status(upload_id):
    """Get active session status and uploaded/missing parts list."""
    res = get_upload_status(upload_id)
    if not res.get('found'):
        return jsonify({'error': 'Upload session not found'}), 404
    return jsonify(res), 200


@upload_bp.route('/api/uploads/<upload_id>/complete', methods=['POST'])
@login_required
def api_complete_upload(upload_id):
    """Assemble chunks into final target video and trigger background video processing."""
    try:
        res = complete_upload_session(upload_id)
        return jsonify(res), 200
    except ValueError as val_err:
        return jsonify({'error': str(val_err)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@upload_bp.route('/api/uploads/<upload_id>/abort', methods=['POST'])
@login_required
def api_abort_upload(upload_id):
    """Abort upload session and clean up temporary parts."""
    res = abort_upload_session(upload_id)
    return jsonify(res), 200
