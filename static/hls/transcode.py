import os
import subprocess
import json
import math
import logging
import shutil

logger = logging.getLogger(__name__)

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

# Full spectrum quality ladder: 144p up to 16K
RENDITIONS = [
    ("144p",  256, 144,   "80k",    "100k",   "160k",   "64k"),
    ("240p",  426, 240,   "200k",   "250k",   "400k",   "64k"),
    ("360p",  640, 360,   "500k",   "600k",   "1000k",  "96k"),
    ("480p",  854, 480,   "1000k",  "1200k",  "2000k",  "128k"),
    ("720p",  1280, 720,  "2500k",  "3000k",  "5000k",  "128k"),
    ("1080p", 1920, 1080, "5000k",  "6000k",  "10000k", "192k"),
    ("2K",    2560, 1440, "12000k", "15000k", "24000k", "256k"),
    ("4K",    3840, 2160, "35000k", "45000k", "70000k", "256k"),
    ("8K",    7680, 4320, "100000k","120000k","200000k","256k"),
    ("16K",   15360, 8640, "250000k","300000k","500000k","320k"),
]

def get_source_info(input_path):
    """Get video information using ffprobe."""
    try:
        cmd = [
            get_ffprobe_bin(), '-v', 'error', '-print_format', 'json',
            '-analyzeduration', '10M', '-probesize', '10M',
            '-show_format', '-show_streams', input_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        info = json.loads(result.stdout)
        
        video_stream = None
        audio_stream = None
        streams = info.get('streams', [])
        
        video_candidates = []
        for stream in streams:
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
            elif stream.get('codec_type') == 'audio' and not audio_stream:
                audio_stream = stream
        
        if not video_candidates:
            return {
                'width': 15360,
                'height': 8640,
                'duration': 0,
                'bitrate': 0,
                'fps': 30.0,
                'codec': 'h264',
                'audio_codec': 'aac',
                'has_audio': audio_stream is not None
            }
            
        video_candidates.sort(key=lambda c: (not c['is_attached'], not c['is_still'], c['pixels']), reverse=True)
        chosen = video_candidates[0]
        video_stream = chosen['stream']
        width = chosen['w']
        height = chosen['h']
        
        # Check rotation metadata
        if video_stream:
            side_data = video_stream.get('side_data_list', [])
            rotation = 0
            for sd in side_data:
                if 'rotation' in sd:
                    try:
                        rotation = abs(int(sd['rotation']))
                    except:
                        pass
            tags = video_stream.get('tags', {})
            if 'rotate' in tags:
                try:
                    rotation = abs(int(tags['rotate']))
                except:
                    pass
            if rotation in [90, 270]:
                width, height = height, width
        else:
            width = 15360
            height = 8640

        duration = float(info.get('format', {}).get('duration', 0))
        bitrate = int(info.get('format', {}).get('bit_rate', 0))
        
        fps = 30.0
        if video_stream:
            fps_parts = video_stream.get('avg_frame_rate', '0/1').split('/')
            fps = float(fps_parts[0]) / float(fps_parts[1]) if len(fps_parts) == 2 and float(fps_parts[1]) > 0 else 30.0
        
        codec = video_stream.get('codec_name', 'h264') if video_stream else 'h264'
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
        logger.error(f"Error in get_source_info: {e}")
        # Return fallback values supporting full quality spectrum up to 16K
        return {
            'width': 15360,
            'height': 8640,
            'duration': 0,
            'bitrate': 0,
            'fps': 30.0,
            'codec': 'h264',
            'audio_codec': 'aac',
            'has_audio': True
        }

def transcode_rendition(input_path, output_dir, rendition, source_info):
    """
    Transcode a single rendition using FFmpeg.
    rendition: (name, width, height, bitrate, maxrate, bufsize, audio_bitrate)
    """
    name, width, height, bitrate, maxrate, bufsize, audio_bitrate = rendition
    playlist_name = f"{name}.m3u8"
    playlist_path = os.path.join(output_dir, playlist_name)
    
    # Calculate bandwidth in bps for M3U8
    video_bps = int(bitrate.replace('k', '')) * 1000
    audio_bps = int(audio_bitrate.replace('k', '')) * 1000
    bandwidth = video_bps + audio_bps
    
    # Determine codec level and profile based on resolution (up to 16K)
    if height > 4320:
        level = '6.2'
    elif height > 2160:
        level = '6.1'
    elif height > 1440:
        level = '5.2'
    elif height > 1080:
        level = '5.1'
    elif height > 720:
        level = '4.2'
    else:
        level = '3.1'
        
    profile = 'high' if height >= 2160 else 'main'
    
    # FFmpeg command for HLS segmenting of this quality
    # Force output resolution to be divisible by 2 to prevent libx264 scaling errors
    cmd = [
        get_ffmpeg_bin(), '-y', '-i', input_path,
        '-vf', f'scale=w={width}:h={height}:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2',
        '-c:v', 'libx264', '-profile:v', profile, '-level', level, '-pix_fmt', 'yuv420p',
        '-preset', 'medium', '-crf', '23',
        '-b:v', bitrate, '-maxrate:v', maxrate, '-bufsize:v', bufsize,
    ]

    if source_info.get('has_audio', True):
        cmd += ['-c:a', 'aac', '-b:a', audio_bitrate, '-ac', '2']
    else:
        cmd += ['-an']

    cmd += [
        '-g', '60', '-keyint_min', '60', '-sc_threshold', '0',
        '-hls_time', '6', '-hls_playlist_type', 'vod',
        '-hls_segment_filename', os.path.join(output_dir, f"{name}_%03d.ts"),
        playlist_path
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=None)
        if result.returncode != 0:
            logger.error(f"FFmpeg error for rendition {name}: {result.stderr[-2000:]}")
            return None, None
            
        rinfo = {
            'name': name,
            'playlist': playlist_name,
            'width': width,
            'height': height,
            'bandwidth': bandwidth,
            'resolution': f"{width}x{height}"
        }
        return playlist_name, rinfo
    except Exception as e:
        logger.error(f"Exception transcoding {name}: {e}")
        return None, None

def generate_master_playlist(output_dir, renditions_info):
    """Generate the master.m3u8 playlist file."""
    master_path = os.path.join(output_dir, 'master.m3u8')
    try:
        with open(master_path, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n")
            f.write("#EXT-X-VERSION:3\n\n")
            for r in renditions_info:
                f.write(f"#EXT-X-STREAM-INF:BANDWIDTH={r['bandwidth']},RESOLUTION={r['width']}x{r['height']}\n")
                f.write(f"{r['playlist']}\n\n")
        return master_path
    except Exception as e:
        logger.error(f"Error generating master playlist: {e}")
        return None

def generate_thumbnail(input_path, output_dir, source_info, video_id):
    """Generate a thumbnail image from the video."""
    thumbnail_path = os.path.join(output_dir, 'thumbnail.jpg')
    duration = source_info.get('duration', 0)
    ss = min(5.0, duration / 2.0) if duration > 0 else 1.0
    ss_str = f"{int(ss // 3600):02d}:{int((ss % 3600) // 60):02d}:{ss % 60:05.2f}"
    
    cmd = [
        get_ffmpeg_bin(), '-y', '-i', input_path,
        '-ss', ss_str, '-vframes', '1',
        '-f', 'image2', thumbnail_path
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=30)
        return thumbnail_path if os.path.exists(thumbnail_path) else None
    except Exception as e:
        logger.error(f"Error generating thumbnail: {e}")
        return None

def generate_sprite_sheet(input_path, output_dir, source_info, video_id):
    """
    Generate a seek preview sprite sheet and VTT file for videos of ANY duration (up to 30+ hours).
    Uses adaptive interval sampling to keep tile count within JPEG limits and safe memory bounds.
    """
    duration = source_info.get('duration', 0)
    if duration <= 0:
        return None, None
        
    # Adaptive sampling interval based on video duration
    if duration <= 60:
        interval = 2
    elif duration <= 600:       # <= 10 mins
        interval = 5
    elif duration <= 3600:      # <= 1 hour
        interval = 15
    elif duration <= 36000:     # <= 10 hours
        interval = 60
    else:                       # 30+ hours
        interval = max(60, int(duration // 300))
        
    num_frames = max(1, int(duration // interval))
    cols = 10
    rows = max(1, math.ceil(num_frames / cols))
    
    sprite_filename = 'sprite.jpg'
    vtt_filename = 'thumbnails.vtt'
    
    sprite_path = os.path.join(output_dir, sprite_filename)
    vtt_path = os.path.join(output_dir, vtt_filename)
    
    tile_w = 160
    tile_h = 90
    
    # Ultra-fast fps filter sampling for sprite grid
    filter_str = f"fps=1/{interval},scale={tile_w}:{tile_h},tile={cols}x{rows}"
    
    cmd = [
        get_ffmpeg_bin(), '-y', '-i', input_path,
        '-vf', filter_str, '-vsync', 'vfr',
        '-q:v', '5', sprite_path
    ]
    
    try:
        # Extended timeout for 30+ hour videos (10 minutes)
        subprocess.run(cmd, capture_output=True, timeout=600)
        
        if not os.path.exists(sprite_path):
            logger.warning(f"Sprite sheet not generated at {sprite_path}")
            return None, None
            
        norm_out = os.path.normpath(output_dir).replace('\\', '/')
        static_idx = norm_out.find('/static/')
        if static_idx != -1:
            rel_web_dir = norm_out[static_idx:]
        else:
            rel_web_dir = f"/static/hls/{video_id}"
            
        sprite_url = f"{rel_web_dir}/{sprite_filename}"
        
        with open(vtt_path, 'w', encoding='utf-8') as f:
            f.write("WEBVTT\n\n")
            for i in range(num_frames):
                start_sec = i * interval
                end_sec = min(duration, (i + 1) * interval)
                
                def format_time(seconds):
                    h = int(seconds // 3600)
                    m = int((seconds % 3600) // 60)
                    s = int(seconds % 60)
                    ms = int((seconds - int(seconds)) * 1000)
                    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
                    
                start_str = format_time(start_sec)
                end_str = format_time(end_sec)
                
                col_idx = i % cols
                row_idx = i // cols
                x = col_idx * tile_w
                y = row_idx * tile_h
                
                f.write(f"{start_str} --> {end_str}\n")
                f.write(f"{sprite_url}#xywh={x},{y},{tile_w},{tile_h}\n\n")
                
        return sprite_path, vtt_path
    except Exception as e:
        logger.error(f"Error generating sprite sheet: {e}")
        return None, None
