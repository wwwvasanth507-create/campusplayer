import os
import subprocess
import json
import math
import logging

logger = logging.getLogger(__name__)

def get_source_info(input_path):
    """Get video information using ffprobe."""
    try:
        cmd = [
            'ffprobe', '-v', 'error', '-print_format', 'json',
            '-show_format', '-show_streams', input_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        info = json.loads(result.stdout)
        
        video_stream = None
        audio_stream = None
        for stream in info.get('streams', []):
            if stream['codec_type'] == 'video' and not video_stream:
                video_stream = stream
            elif stream['codec_type'] == 'audio' and not audio_stream:
                audio_stream = stream
                
        if not video_stream:
            raise ValueError("No video stream found")
            
        width = int(video_stream.get('width', 0))
        height = int(video_stream.get('height', 0))
        duration = float(info.get('format', {}).get('duration', 0))
        bitrate = int(info.get('format', {}).get('bit_rate', 0))
        
        fps_parts = video_stream.get('avg_frame_rate', '0/1').split('/')
        fps = float(fps_parts[0]) / float(fps_parts[1]) if len(fps_parts) == 2 and float(fps_parts[1]) > 0 else 30.0
        
        codec = video_stream.get('codec_name', 'h264')
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
        # Return fallback values
        return {
            'width': 1280,
            'height': 720,
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
    
    # Determine codec level based on resolution
    if height > 1080:
        level = '4.2' if height <= 1440 else '5.1' if height <= 2160 else '6.1'
    else:
        level = '3.1'
    
    # FFmpeg command for HLS segmenting of this quality
    cmd = [
        'ffmpeg', '-y', '-i', input_path,
        '-vf', f'scale=w={width}:h={height}:force_original_aspect_ratio=decrease,pad=w={width}:h={height}:x=(ow-iw)/2:y=(oh-ih)/2',
        '-c:v', 'libx264', '-profile:v', 'main', '-level', level, '-pix_fmt', 'yuv420p',
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
        # No timeout: some renditions of very large/long videos can legitimately take
        # a long time to encode. A huge numeric timeout (e.g. 2764800s / 32 days) can
        # raise "OverflowError: timeout is too large" on some platforms, so we simply
        # don't enforce one here and let the calling code manage overall job limits.
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
    ss = min(5.0, source_info['duration'] / 2.0) if source_info['duration'] > 0 else 1.0
    ss_str = f"{int(ss // 3600):02d}:{int((ss % 3600) // 60):02d}:{ss % 60:05.2f}"
    
    cmd = [
        'ffmpeg', '-y', '-i', input_path,
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
    Generate a seek preview sprite sheet and VTT file.
    """
    duration = source_info['duration']
    if duration <= 0:
        return None, None
        
    interval = 5
    num_frames = max(1, int(duration // interval))
    cols = 10
    rows = math.ceil(num_frames / cols)
    
    sprite_filename = 'sprite.jpg'
    vtt_filename = 'thumbnails.vtt'
    
    sprite_path = os.path.join(output_dir, sprite_filename)
    vtt_path = os.path.join(output_dir, vtt_filename)
    
    tile_w = 160
    tile_h = 90
    
    fps = source_info['fps']
    frame_interval = int(fps * interval)
    if frame_interval <= 0:
        frame_interval = 30
        
    filter_str = f"select='not(mod(n,{frame_interval}))',scale={tile_w}:{tile_h},tile={cols}x{rows}"
    
    cmd = [
        'ffmpeg', '-y', '-i', input_path,
        '-vf', filter_str, '-vsync', 'vfr',
        '-q:v', '5', sprite_path
    ]
    
    try:
        subprocess.run(cmd, capture_output=True, timeout=60)
        
        if not os.path.exists(sprite_path):
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
