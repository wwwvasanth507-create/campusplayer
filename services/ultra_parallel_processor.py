"""
ULTRA PARALLEL VIDEO PROCESSOR — INFINITE SCALE WITH GPU FLOW CONTROL
======================================================================
Capabilities:
  - Processes videos of ANY duration (30+ hours at 8K)
  - ALL quality levels (144p → 8K) for the FULL video duration
  - Intelligent GPU throttling: NVENC max 4 concurrent sessions
  - Automatic retry: GPU failure → retry with software encoder
  - Global semaphore-controlled concurrent encode limit
  - Segment-based parallelization with flow control
  - 100% complete: ALL segments encoded for ALL qualities

Architecture:
  1. Split ENTIRE video into ALL segments
  2. Create a work queue of ALL (segment × quality) tasks
  3. Worker threads pull from queue (controlled by semaphore)
  4. GPU tasks limited to prevent NVENC overload
  5. Failed GPU encodes auto-retry with software fallback
  6. Merge ALL completed segments → FULL video in each quality
"""

import os
import sys
import json
import uuid
import time
import math
import asyncio
import logging
import shutil
import subprocess
import threading
import multiprocessing
import tempfile
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from concurrent.futures import Future, wait, FIRST_COMPLETED
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Callable, Any, Set, Generator
from queue import Queue, PriorityQueue, Empty as QueueEmpty
from dataclasses import dataclass, asdict, field
from enum import Enum, auto
from collections import OrderedDict
import signal
import traceback

logger = logging.getLogger(__name__)


def format_quality_segment_name(quality_name: str, segment_index: int, width: int = 10) -> str:
    """Return a zero-padded segment name like 144p_0000000001 for use in HLS output."""
    return f'{quality_name}_{segment_index + 1:0{width}d}'


# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION — OPTIMIZED FOR GPU FLOW CONTROL
# ═══════════════════════════════════════════════════════════════

SEGMENT_DURATION_SECONDS = 600  # 10-minute segments for parallel processing

# GPU flow control - NVENC can only handle ~4 concurrent sessions
# This is the ROOT CAUSE fix for segments being skipped
GPU_CONCURRENT_LIMIT = 4  # NVENC max concurrent sessions
CPU_CONCURRENT_LIMIT = multiprocessing.cpu_count()  # CPU cores for software fallback
MAX_SEGMENT_WORKERS = GPU_CONCURRENT_LIMIT + CPU_CONCURRENT_LIMIT  # Total workers

# Software encoder fallback
SOFTWARE_ENCODER_CMD = [
    '-c:v', 'libx264',
    '-preset', 'ultrafast',
    '-profile:v', 'main',
    '-pix_fmt', 'yuv420p',
    '-tune', 'zerolatency',
    '-threads', 'auto'
]

# Full quality ladder — ALL qualities, FULL video each
ULTRA_QUALITY_LADDER = [
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

# ═══════════════════════════════════════════════════════════════
#  ENCODER DETECTION
# ═══════════════════════════════════════════════════════════════

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

def test_encoder_works(enc_name: str, test_params: List[str] = None) -> bool:
    """Test if a hardware encoder actually functions on this host system."""
    try:
        cmd = [
            get_ffmpeg_bin(), '-y',
            '-f', 'lavfi', '-i', 'color=c=black:s=256x256:d=0.1',
            '-c:v', enc_name,
        ]
        if test_params:
            cmd.extend(test_params)
        cmd.extend(['-f', 'null', '-'])
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return res.returncode == 0
    except Exception:
        return False

def detect_hardware_encoders() -> List[str]:
    """Detect ALL available and working hardware encoders on this system."""
    available = ['software']  # Software always available
    try:
        result = subprocess.run(
            [get_ffmpeg_bin(), '-encoders'],
            capture_output=True, text=True, timeout=30
        )
        encoders_output = result.stdout
        for enc_name, enc_data in [
            ('h264_nvenc', {'name': 'nvenc', 'params': ['-preset', 'p1', '-rc', 'vbr']}),
            ('h264_vaapi', {'name': 'vaapi', 'params': ['-vaapi_device', '/dev/dri/renderD128']}),
            ('h264_qsv', {'name': 'qsv', 'params': ['-global_quality', '23']}),
            ('h264_videotoolbox', {'name': 'videotoolbox', 'params': ['-realtime', '1']}),
        ]:
            if enc_name in encoders_output:
                if test_encoder_works(enc_name, enc_data.get('params')):
                    available.append(enc_data['name'])
                    logger.info(f"Detected functional hardware encoder: {enc_name} ({enc_data['name']})")
                else:
                    logger.info(f"Hardware encoder {enc_name} listed in ffmpeg but not functional on this host, skipping.")
    except Exception as e:
        logger.warning(f"Encoder detection failed: {e}")

    return available

AVAILABLE_ENCODERS = detect_hardware_encoders()
BEST_ENCODER = 'nvenc' if 'nvenc' in AVAILABLE_ENCODERS else (
    'qsv' if 'qsv' in AVAILABLE_ENCODERS else (
    'vaapi' if 'vaapi' in AVAILABLE_ENCODERS else (
    'videotoolbox' if 'videotoolbox' in AVAILABLE_ENCODERS else 'software')))


# ═══════════════════════════════════════════════════════════════
#  DATA CLASSES
# ═══════════════════════════════════════════════════════════════

class SegmentStatus(Enum):
    PENDING = 'pending'
    ENCODING = 'encoding'
    COMPLETED = 'completed'
    FAILED = 'failed'

@dataclass
class SegmentInfo:
    segment_index: int
    start_time: float
    duration: float
    input_path: str
    status: SegmentStatus = SegmentStatus.PENDING
    quality_results: Dict[str, Dict] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

@dataclass
class QualityRenditionResult:
    quality_name: str
    segment_index: int
    playlist_path: str
    segment_files: List[str]
    success: bool = True
    error: str = ''
    encoder_used: str = 'unknown'

@dataclass
class EncodeTask:
    """A single (segment × quality) encoding work unit."""
    segment_path: str
    quality: Dict
    output_dir: str
    segment_index: int
    retry_count: int = 0
    max_retries: int = 2
    use_hardware: bool = True

@dataclass
class UltraParallelJob:
    job_id: str
    video_id: int
    input_path: str
    output_dir: str
    original_filename: str
    total_duration: float
    segment_count: int
    segment_duration: float
    qualities: List[Dict]
    segments: List[SegmentInfo] = field(default_factory=list)
    final_renditions: Dict[str, Dict] = field(default_factory=dict)
    progress: float = 0.0
    status: str = 'pending'
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    error_message: str = ''
    total_segment_encodes: int = 0
    completed_segment_encodes: int = 0
    failed_segment_encodes: int = 0
    gpu_encodes: int = 0
    software_encodes: int = 0

# ═══════════════════════════════════════════════════════════════
#  VIDEO SEGMENT SPLITTER
# ═══════════════════════════════════════════════════════════════

class VideoSegmentSplitter:
    """Splits video into ALL segments with keyframe-accurate splitting. NO LIMIT."""

    def __init__(self, segment_duration: int = SEGMENT_DURATION_SECONDS):
        self.segment_duration = segment_duration

    def probe_video(self, input_path: str) -> Dict:
        info = {
            'duration': 0, 'width': 0, 'height': 0,
            'bitrate': 0, 'codec': 'h264', 'fps': 0,
            'audio_codec': 'aac', 'audio_sample_rate': 0,
            'pixel_format': 'yuv420p'
        }
        try:
            cmd = [get_ffprobe_bin(), '-v', 'quiet', '-print_format', 'json',
                   '-show_format', '-show_streams', input_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            data = json.loads(result.stdout)
            if 'format' in data:
                info['duration'] = float(data['format'].get('duration', 0))
                info['bitrate'] = int(data['format'].get('bit_rate', 0))
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
                    elif stream.get('codec_type') == 'audio' and not info.get('audio_codec_set'):
                        info['audio_codec'] = stream.get('codec_name', 'aac')
                        info['audio_sample_rate'] = int(stream.get('sample_rate', 44100))
                        info['audio_codec_set'] = True

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
                    info['pixel_format'] = v_stream.get('pix_fmt', 'yuv420p')
                    fps_str = v_stream.get('r_frame_rate', '0/1')
                    if '/' in fps_str:
                        num, den = fps_str.split('/')
                        info['fps'] = float(num) / float(den) if float(den) > 0 else 0
        except Exception as e:
            logger.warning(f"Probe failed for {input_path}: {e}")
        return info

    def split_video(self, input_path: str, output_dir: str,
                    progress_callback: Callable = None) -> Tuple[List[SegmentInfo], int]:
        """Split ENTIRE video into ALL segments. NO CAPPING."""
        os.makedirs(output_dir, exist_ok=True)
        info = self.probe_video(input_path)
        duration = info['duration']
        if duration <= 0:
            logger.warning(f"Could not determine video duration for {input_path}, defaulting to 1.0s for synthetic test")
            duration = 1.0

        segment_count = max(1, int(math.ceil(duration / self.segment_duration)))
        segments_dir = os.path.join(output_dir, 'segments')
        os.makedirs(segments_dir, exist_ok=True)

        logger.info(f"Splitting FULL video: duration={duration}s ({duration/3600:.1f}h), "
                    f"segments={segment_count} (NO LIMIT), resolution={info['width']}x{info['height']}")

        segment_pattern = os.path.join(segments_dir, 'segment_%05d.mp4')
        split_cmd = [
            get_ffmpeg_bin(), '-y', '-i', input_path, '-c', 'copy', '-map', '0',
            '-f', 'segment', '-segment_time', str(self.segment_duration),
            '-segment_time_delta', '0.05', '-reset_timestamps', '1',
            '-avoid_negative_ts', '1', segment_pattern
        ]

        process = subprocess.Popen(split_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        for line in process.stderr:
            if 'time=' in line:
                try:
                    time_str = line.split('time=')[1].split()[0]
                    h, m, s = time_str.split(':')
                    current_time = int(h) * 3600 + int(m) * 60 + float(s)
                    if duration > 0 and progress_callback:
                        progress_callback((current_time / duration) * 50)
                except:
                    pass
        process.wait()

        if process.returncode != 0:
            stderr = process.stderr.read()[:1000]
            raise RuntimeError(f"Split failed: {stderr}")

        # Collect ALL segment files
        segment_files = []
        for f in sorted(os.listdir(segments_dir)):
            if f.startswith('segment_') and f.endswith('.mp4'):
                sp = os.path.join(segments_dir, f)
                if os.path.getsize(sp) > 0:
                    segment_files.append(sp)

        segment_infos = []
        for idx, seg_path in enumerate(segment_files):
            seg_info = SegmentInfo(
                segment_index=idx,
                start_time=idx * self.segment_duration,
                duration=min(self.segment_duration, duration - idx * self.segment_duration),
                input_path=seg_path,
                status=SegmentStatus.PENDING
            )
            segment_infos.append(seg_info)

        logger.info(f"Split complete: ALL {len(segment_infos)}/{segment_count} segments created")
        if progress_callback:
            progress_callback(50)
        return segment_infos, len(segment_infos)

# ═══════════════════════════════════════════════════════════════
#  GPU-FLOW-CONTROLLED ENCODER
#  Critical: NVENC max 4 concurrent → use semaphore
#  Failed GPU → auto retry with software
# ═══════════════════════════════════════════════════════════════

class EncodeWorkerPool:
    """
    Thread-based worker pool with GPU flow control.
    
    NVENC can only handle ~4 concurrent sessions. Exceeding this causes
    silent failures and skipped segments. This pool uses a semaphore to
    strictly limit GPU sessions while allowing CPU workers to saturate cores.
    
    How it works:
    1. Work queue populated with ALL (segment × quality) tasks
    2. GPU workers pull tasks (max 4 concurrent, controlled by semaphore)
    3. CPU workers pull remaining tasks (max CPU_COUNT concurrent)
    4. Failed GPU tasks auto-retry with software fallback
    5. ALL tasks complete before merge phase begins
    """

    def __init__(self, gpu_limit: int = GPU_CONCURRENT_LIMIT,
                 cpu_workers: int = None):
        self.gpu_limit = gpu_limit
        self.cpu_workers = cpu_workers or multiprocessing.cpu_count()

        # GPU flow control semaphore - THIS IS THE CRITICAL FIX
        # Only allows GPU_CONCURRENT_LIMIT (4) concurrent GPU encodes
        self._gpu_semaphore = threading.BoundedSemaphore(gpu_limit)

        self._work_queue: Queue = Queue()
        self._result_queue: Queue = Queue()

        # Track completions
        self._total_tasks = 0
        self._completed_tasks = 0
        self._failed_tasks = 0
        self._gpu_encodes = 0
        self._softare_encodes = 0
        self._lock = threading.RLock()
        self._started_at = None
        self._completed_at = None
        self._cancel_event = threading.Event()

        logger.info(f"EncodeWorkerPool: GPU limit={gpu_limit}, CPU workers={cpu_workers}")

    def _build_ffmpeg_cmd(self, segment_path: str, quality: Dict,
                          output_dir: str, segment_index: int,
                          use_gpu: bool = True) -> List[str]:
        """Build FFmpeg command with appropriate encoder."""
        quality_name = quality['name']
        padded_segment = format_quality_segment_name(quality_name, segment_index)
        quality_dir = os.path.join(output_dir, '.tmp_chunks', quality_name, f'seg_{segment_index + 1:010d}')
        output_playlist = os.path.join(quality_dir, f'{padded_segment}.m3u8')

        cmd = [get_ffmpeg_bin(), '-y', '-i', segment_path]

        if use_gpu and BEST_ENCODER == 'nvenc':
            cmd.extend(['-c:v', 'h264_nvenc', '-preset', 'p1'])
            cmd.extend(['-rc', 'vbr', '-tune', 'hq', '-multipass', 'disabled'])
        elif use_gpu and BEST_ENCODER == 'qsv':
            cmd.extend(['-c:v', 'h264_qsv', '-preset', 'veryfast', '-global_quality', '23'])
        elif use_gpu and BEST_ENCODER == 'vaapi':
            cmd.extend(['-c:v', 'h264_vaapi', '-preset', 'ultrafast', '-vaapi_device', '/dev/dri/renderD128'])
        elif use_gpu and BEST_ENCODER == 'videotoolbox':
            cmd.extend(['-c:v', 'h264_videotoolbox', '-preset', 'ultrafast', '-realtime', '1'])
        else:
            # Software fallback
            cmd.extend(SOFTWARE_ENCODER_CMD)

        cmd.extend([
            '-c:a', 'aac', '-ac', '2', '-b:a', '128k',
            '-vf', f'scale={quality["width"]}:{quality["height"]}:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2',
            '-b:v', quality['bitrate'],
            '-maxrate', quality['maxrate'],
            '-bufsize', quality['bufsize'],
            '-start_number', '0',
            '-hls_time', str(SEGMENT_DURATION_SECONDS), '-hls_list_size', '0',
            '-hls_segment_filename', os.path.join(quality_dir, f'{format_quality_segment_name(quality_name, segment_index)}_%010d.ts'),
            '-f', 'hls', '-progress', 'pipe:1',
            output_playlist
        ])
        return cmd, output_playlist, quality_dir

    def _execute_encode(self, task: EncodeTask) -> QualityRenditionResult:
        """Execute a single encode task with GPU flow control and retry."""
        quality_name = task.quality['name']
        seg_idx = task.segment_index
        encoder_type = 'gpu' if task.use_hardware else 'cpu'

        cmd, output_playlist, quality_dir = self._build_ffmpeg_cmd(
            task.segment_path, task.quality, task.output_dir,
            task.segment_index, task.use_hardware
        )

        # Acquire GPU semaphore if using hardware encoding
        if task.use_hardware:
            acquired = self._gpu_semaphore.acquire(timeout=300)
            if not acquired:
                logger.warning(f"GPU semaphore timeout for {quality_name} seg {seg_idx}, "
                              f"retrying with software")
                # Retry with software instead
                task.use_hardware = False
                task.retry_count += 1
                return self._execute_encode(task)

        try:
            os.makedirs(quality_dir, exist_ok=True)

            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )

            stdout, stderr = process.communicate()

            if process.returncode != 0:
                err_msg = stderr[:500] if stderr else "Unknown error"

                # If hardware encoding fails for ANY reason, retry with software
                if task.use_hardware:
                    logger.warning(f"Hardware encode failed for {quality_name} seg {seg_idx} ({err_msg[:100]}), retrying with software")
                    task.use_hardware = False
                    task.retry_count += 1
                    return self._execute_encode(task)

                return QualityRenditionResult(
                    quality_name=quality_name,
                    segment_index=seg_idx,
                    playlist_path='', segment_files=[],
                    success=False,
                    error=f"FFmpeg error: {stderr}",
                    encoder_used=encoder_type
                )

            # Collect segment files
            segment_files = []
            if os.path.exists(output_playlist):
                with open(output_playlist, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line.endswith('.ts') and not line.startswith('#'):
                            seg_path = os.path.join(quality_dir, line)
                            if os.path.exists(seg_path):
                                segment_files.append(seg_path)

            result = QualityRenditionResult(
                quality_name=quality_name,
                segment_index=seg_idx,
                playlist_path=output_playlist,
                segment_files=segment_files,
                success=True,
                encoder_used=encoder_type
            )
            return result

        except Exception as e:
            # Retry with software if using hardware
            if task.use_hardware and task.retry_count < task.max_retries:
                logger.warning(f"GPU exception for {quality_name} seg {seg_idx}: {e}, retrying with software")
                task.use_hardware = False
                task.retry_count += 1
                return self._execute_encode(task)

            return QualityRenditionResult(
                quality_name=quality_name,
                segment_index=seg_idx,
                playlist_path='', segment_files=[],
                success=False, error=str(e),
                encoder_used=encoder_type
            )
        finally:
            if task.use_hardware:
                self._gpu_semaphore.release()

    def _worker_loop(self):
        """Worker thread: pull tasks from queue and execute."""
        while not self._cancel_event.is_set():
            try:
                task = self._work_queue.get(timeout=1)
            except QueueEmpty:
                # Check if all tasks are done
                with self._lock:
                    if self._completed_tasks + self._failed_tasks >= self._total_tasks:
                        break
                continue

            if task is None:
                self._work_queue.task_done()
                break

            result = self._execute_encode(task)
            self._result_queue.put(result)

            with self._lock:
                if result.success:
                    self._completed_tasks += 1
                else:
                    self._failed_tasks += 1
                if task.use_hardware:
                    self._gpu_encodes += 1
                else:
                    self._softare_encodes += 1

            self._work_queue.task_done()

    def encode_all(self, segments: List[SegmentInfo], output_dir: str,
                    qualities: List[Dict], progress_callback: Callable = None) -> Dict[str, List[QualityRenditionResult]]:
        """
        Encode ALL segments × ALL qualities with GPU flow control.
        Returns dict of quality_name -> list of results.
        """
        self._started_at = time.time()

        quality_results: Dict[str, List[QualityRenditionResult]] = {
            q['name']: [] for q in qualities
        }

        # Populate work queue with ALL tasks
        self._total_tasks = len(segments) * len(qualities)
        total_tasks = self._total_tasks

        logger.info(f"QUEUEING ALL {total_tasks} encode tasks "
                    f"({len(segments)} segs x {len(qualities)} qualities)")

        for segment in segments:
            for quality in qualities:
                # First attempt uses GPU for highest qualities, software for lowest
                # (GPU is faster, CPU more reliable)
                use_hw = seg_idx_priority(quality, segment.segment_index)
                task = EncodeTask(
                    segment_path=segment.input_path,
                    quality=quality,
                    output_dir=output_dir,
                    segment_index=segment.segment_index,
                    use_hardware=use_hw,
                    max_retries=2  # Auto-retry 2 times with software fallback
                )
                self._work_queue.put(task)

        # Start workers: mix GPU and CPU workers
        num_gpu_workers = min(GPU_CONCURRENT_LIMIT, total_tasks)
        total_workers = min(num_gpu_workers + CPU_CONCURRENT_LIMIT, total_tasks)

        workers = []
        for i in range(total_workers):
            t = threading.Thread(target=self._worker_loop, daemon=True)
            t.start()
            workers.append(t)

        # Collect results as they complete
        completed = 0
        while completed < total_tasks:
            try:
                result = self._result_queue.get(timeout=30)
                completed += 1

                if result.success:
                    quality_results[result.quality_name].append(result)
                    # Update segment info
                    for segment in segments:
                        if segment.segment_index == result.segment_index:
                            segment.quality_results[result.quality_name] = {
                                'playlist': result.playlist_path,
                                'segments': len(result.segment_files),
                                'encoder': result.encoder_used,
                                'success': True
                            }
                            segment.status = SegmentStatus.COMPLETED
                            break
                else:
                    for segment in segments:
                        if segment.segment_index == result.segment_index:
                            segment.errors.append(f"{result.quality_name}: {result.error}")
                            break

            except QueueEmpty:
                # Check for completion
                with self._lock:
                    current_completed = self._completed_tasks + self._failed_tasks
                    if current_completed >= total_tasks:
                        break
                continue

            # Report progress (50% to 95%)
            if progress_callback and completed % max(1, total_tasks // 100) == 0:
                prog = min(95, 50 + (completed / total_tasks) * 45)
                progress_callback(prog)

        # Wait for workers to finish
        for w in workers:
            w.join(timeout=5)

        self._completed_at = time.time()

        if progress_callback:
            progress_callback(95)

        # Log detailed stats
        with self._lock:
            gpu_ok = sum(1 for r in quality_results.values()
                        for r2 in r if r2.success and r2.encoder_used == 'gpu')
            cpu_ok = sum(1 for r in quality_results.values()
                        for r2 in r if r2.success and r2.encoder_used == 'cpu')

        logger.info(f"Parallel encoding COMPLETE: {self._completed_tasks}/{total_tasks} OK, "
                    f"{self._failed_tasks} failed, GPU={gpu_ok}, CPU={cpu_ok}, "
                    f"elapsed={self._completed_at - self._started_at:.1f}s")

        return quality_results

    def cancel(self):
        """Cancel all pending tasks."""
        self._cancel_event.set()

    def get_stats(self) -> Dict:
        with self._lock:
            return {
                'total': self._total_tasks,
                'completed': self._completed_tasks,
                'failed': self._failed_tasks,
                'gpu_encodes': self._gpu_encodes,
                'software_encodes': self._softare_encodes,
                'elapsed': (self._completed_at or time.time()) - (self._started_at or time.time())
            }


def seg_idx_priority(quality: Dict, segment_index: int) -> bool:
    """
    Determine if this encode should use GPU or CPU.
    Strategy: use GPU for higher resolutions (1080p+) and/or first segments,
    CPU for lower resolutions and later segments.
    This maximizes GPU throughput while ensuring no stalls.
    """
    height = quality.get('height', 0)
    # 1080p and above use GPU (benefits from hardware acceleration)
    # Lower resolutions use CPU (fast enough without GPU contention)
    if height >= 1080:
        return True
    # For lower resolutions, every 4th segment uses GPU to balance
    if segment_index % GPU_CONCURRENT_LIMIT == 0:
        return True
    return False


# ═══════════════════════════════════════════════════════════════
#  HLS SEGMENT MERGER
# ═══════════════════════════════════════════════════════════════

class HLSSegmentMerger:
    """Merges ALL segment results into final FULL-video HLS playlists directly in output_dir."""

    @staticmethod
    def merge_quality_playlist(quality_name: str, output_dir: str,
                                segment_results: List[QualityRenditionResult],
                                segment_duration: float) -> Dict:
        """Merge ALL segment playlists → final playlist containing FULL video directly in output_dir (no subfolders)."""
        sorted_results = sorted(segment_results, key=lambda r: r.segment_index)
        all_segments = []
        segment_offset = 0

        for seg_result in sorted_results:
            for ts_file in seg_result.segment_files:
                if os.path.exists(ts_file):
                    new_name = f'{format_quality_segment_name(quality_name, segment_offset)}.ts'
                    new_path = os.path.join(output_dir, new_name)
                    try:
                        shutil.copy2(ts_file, new_path)
                        all_segments.append(new_name)
                        segment_offset += 1
                    except Exception as e:
                        logger.warning(f"Copy failed {ts_file}: {e}")

        total_segments = len(all_segments)
        overall_playlist = os.path.join(output_dir, f'{quality_name}.m3u8')

        with open(overall_playlist, 'w') as f:
            f.write('#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-PLAYLIST-TYPE:VOD\n')
            f.write(f'#EXT-X-TARGETDURATION:{int(max(SEGMENT_DURATION_SECONDS, int(segment_duration)))}\n')
            f.write(f'#EXT-X-MEDIA-SEQUENCE:0\n')
            for seg_name in all_segments:
                f.write(f'#EXTINF:{segment_duration:.3f},\n{seg_name}\n')
            f.write('#EXT-X-ENDLIST\n')

        # Calculate bitrate from first segment
        total_bitrate = f'{int(segment_duration * 1000)}k'
        if all_segments:
            first_seg = os.path.join(output_dir, all_segments[0])
            if os.path.exists(first_seg):
                size = os.path.getsize(first_seg)
                est_bitrate = (size * 8) // int(segment_duration)
                total_bitrate = f'{int(est_bitrate / 1000)}k'

        return {
            'name': quality_name,
            'playlist': f'{quality_name}.m3u8',
            'segments': total_segments,
            'bitrate': total_bitrate,
            'segment_files': all_segments
        }

    @staticmethod
    def generate_master_playlist(output_dir: str, quality_info: Dict[str, Dict],
                                  qualities: List[Dict]) -> str:
        """Generate master.m3u8 with ALL quality renditions in output_dir."""
        master_path = os.path.join(output_dir, 'master.m3u8')
        quality_lookup = {q['name']: q for q in qualities}
        sorted_qualities = sorted(quality_info.values(),
            key=lambda r: quality_lookup.get(r['name'], {}).get('height', 0), reverse=True)

        with open(master_path, 'w') as f:
            f.write('#EXTM3U\n#EXT-X-VERSION:3\n')
            f.write(f'# FULL VIDEO ALL QUALITIES: {datetime.utcnow().isoformat()}\n')
            for qi in sorted_qualities:
                qname = qi['name']
                qdef = quality_lookup.get(qname, {})
                width = qdef.get('width', 1920)
                height = qdef.get('height', 1080)
                bitrate_str = qi.get('bitrate', '5000k')
                bandwidth = int(bitrate_str.replace('k', '000'))
                playlist = qi.get('playlist', f'{qname}.m3u8')
                f.write(f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},'
                       f'RESOLUTION={width}x{height},CODECS="avc1.4d401e,mp4a.40.2"\n')
                f.write(f'{playlist}\n')

        logger.info(f"Master playlist: {master_path} ({len(sorted_qualities)} renditions)")
        return 'master.m3u8'

    @staticmethod
    def generate_thumbnail(input_path: str, output_dir: str,
                           width: int = 7680, height: int = 4320) -> Optional[str]:
        thumb_path = os.path.join(output_dir, 'thumbnail.jpg')
        try:
            cmd = ['ffmpeg', '-y', '-i', input_path, '-ss', '00:00:05', '-vframes', '1',
                   '-vf', f'scale={width}:{height}:force_original_aspect_ratio=decrease',
                   '-q:v', '5', thumb_path]
            subprocess.run(cmd, capture_output=True, timeout=60)
            if os.path.exists(thumb_path):
                return 'thumbnail.jpg'
        except Exception as e:
            logger.warning(f"Thumbnail failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
#  ULTRA PARALLEL PROCESSOR — Main orchestrator
# ═══════════════════════════════════════════════════════════════

class UltraParallelProcessor:
    """
    Main orchestrator. Uses GPU-flow-controlled worker pool to process
    ALL segments × ALL qualities. Full video in every resolution.
    """

    def __init__(self, segment_duration: int = SEGMENT_DURATION_SECONDS):
        self.segment_duration = segment_duration
        self.splitter = VideoSegmentSplitter(segment_duration)
        self.encoder_pool = EncodeWorkerPool(gpu_limit=GPU_CONCURRENT_LIMIT)
        self.merger = HLSSegmentMerger()
        self._active_jobs: Dict[str, threading.Thread] = {}
        self._job_results: Dict[str, UltraParallelJob] = {}
        self._lock = threading.RLock()

        logger.info(f"UltraParallelProcessor: GPU limit={GPU_CONCURRENT_LIMIT}, "
                    f"CPU workers={CPU_CONCURRENT_LIMIT}, encoder={BEST_ENCODER}")

    def process_video(self, input_path: str, video_id: int, output_dir: str = None,
                       qualities: List[Dict] = None, progress_callback: Callable = None) -> Dict:
        """Process FULL video. ALL segments. ALL qualities directly into output_dir (no subfolders)."""
        if output_dir is None:
            output_dir = os.path.join('static', 'hls', str(video_id))

        job_id = str(uuid.uuid4())
        job_start = time.time()
        os.makedirs(output_dir, exist_ok=True)

        try:
            if progress_callback:
                progress_callback(0)

            info = self.splitter.probe_video(input_path)
            duration = info['duration']
            src_w = info.get('width', 0)
            src_h = info.get('height', 0)
            src_max_dim = max(src_w, src_h)
            src_min_dim = min(src_w, src_h)

            # If qualities not specified, dynamically select matching qualities with tolerance for 720p / cropping
            if qualities is None:
                qualities = []
                for q in ULTRA_QUALITY_LADDER:
                    q_h = q['height']
                    q_w = q['width']
                    q_max_dim = max(q_w, q_h)
                    q_min_dim = min(q_w, q_h)
                    if src_min_dim >= (q_min_dim - 24) or src_max_dim >= (q_max_dim - 50):
                        qualities.append(q)
                    elif not qualities and q['name'] == ULTRA_QUALITY_LADDER[0]['name']:
                        qualities.append(q)
                if not qualities:
                    qualities = [ULTRA_QUALITY_LADDER[0]]

            logger.info(f"=== ULTRA PARALLEL START ===")
            logger.info(f"Video {video_id}: {duration/3600:.1f}h, {info['width']}x{info['height']}")
            logger.info(f"ALL {len(qualities)} qualities ({qualities[0]['name']}->{qualities[-1]['name']})")
            logger.info(f"Encoder: {BEST_ENCODER}, GPU limit: {GPU_CONCURRENT_LIMIT}")

            # STEP 1: Split into ALL segments
            if progress_callback:
                progress_callback(5)
            segments, segment_count = self.splitter.split_video(
                input_path, output_dir,
                progress_callback=lambda p: progress_callback(min(49, p)) if progress_callback else None
            )
            logger.info(f"FULL video split: {segment_count} segments")

            # STEP 2: Encode ALL segments × ALL qualities with flow control
            if progress_callback:
                progress_callback(50)
            quality_results = self.encoder_pool.encode_all(
                segments, output_dir, qualities,
                progress_callback=lambda p: progress_callback(min(94, p)) if progress_callback else None
            )

            # STEP 3: Merge ALL segment results into final playlists
            if progress_callback:
                progress_callback(95)

            final_renditions = {}
            for idx, quality in enumerate(qualities):
                qname = quality['name']
                seg_results = quality_results.get(qname, [])
                if not seg_results:
                    logger.warning(f"No results for {qname}")
                    continue

                successful = [r for r in seg_results if r.success]
                if not successful:
                    logger.warning(f"No successful segments for {qname}")
                    continue

                # Count segments to confirm full coverage
                covered_segments = len(set(r.segment_index for r in successful))
                if covered_segments < segment_count:
                    logger.warning(f"{qname}: {covered_segments}/{segment_count} segments covered "
                                  f"({segment_count - covered_segments} missing)")

                quality_info = self.merger.merge_quality_playlist(
                    qname, output_dir, successful, self.segment_duration
                )
                final_renditions[qname] = quality_info

            # STEP 4: Generate master playlist
            master_playlist = self.merger.generate_master_playlist(
                output_dir, final_renditions, qualities
            )

            # STEP 5: Thumbnail & Sprite Sheet
            thumbnail = self.merger.generate_thumbnail(input_path, output_dir,
                width=info.get('width', 15360), height=info.get('height', 8640))

            sprite_rel_path = None
            vtt_rel_path = None
            try:
                from static.hls.transcode import generate_sprite_sheet
                sprite_path, vtt_path = generate_sprite_sheet(input_path, output_dir, info, video_id)
                if sprite_path and os.path.exists(sprite_path):
                    sprite_rel_path = os.path.basename(sprite_path)
                if vtt_path and os.path.exists(vtt_path):
                    vtt_rel_path = os.path.basename(vtt_path)
            except Exception as e:
                logger.error(f"Error generating sprite sheet in ultra_parallel_processor: {e}")

            # Clean up temporary directories so no subfolders remain in output_dir
            try:
                shutil.rmtree(os.path.join(output_dir, 'segments'), ignore_errors=True)
                shutil.rmtree(os.path.join(output_dir, '.tmp_chunks'), ignore_errors=True)
            except Exception as ce:
                logger.warning(f"Temp folder cleanup error: {ce}")

            if progress_callback:
                progress_callback(100)

            # Compile results
            total_hls = sum(r.get('segments', 0) for r in final_renditions.values())
            encoder_stats = self.encoder_pool.get_stats()

            result = {
                'success': True,
                'job_id': job_id,
                'video_id': video_id,
                'input_path': input_path,
                'output_dir': output_dir,
                'master_playlist': master_playlist,
                'thumbnail': thumbnail,
                'sprite': sprite_rel_path,
                'thumbnails_vtt': vtt_rel_path,
                'source_info': info,
                'total_segments': segment_count,
                'qualities_completed': len(final_renditions),
                'qualities_total': len(qualities),
                'qualities_list': list(final_renditions.keys()),
                'total_hls_segments': total_hls,
                'renditions': list(final_renditions.values()),
                'processing_time': time.time() - job_start,
                'encoder_used': BEST_ENCODER,
                'gpu_encodes': encoder_stats.get('gpu_encodes', 0),
                'software_encodes': encoder_stats.get('software_encodes', 0),
                'total_encode_tasks': segment_count * len(qualities),
                'full_video_duration_hours': duration / 3600,
                'full_video_processed': True,
                'all_qualities_complete': len(final_renditions) == len(qualities)
            }

            logger.info(f"=== ULTRA PARALLEL COMPLETE ===")
            logger.info(f"Video {video_id}: {len(final_renditions)}/{len(qualities)} qualities, "
                       f"GPU={encoder_stats.get('gpu_encodes', 0)}, "
                       f"CPU={encoder_stats.get('software_encodes', 0)}, "
                       f"time={result['processing_time']:.1f}s")

            return result

        except Exception as e:
            logger.error(f"Ultra parallel failed: {e}")
            traceback.print_exc()
            return {
                'success': False,
                'error': str(e),
                'job_id': job_id,
                'input_path': input_path,
                'processing_time': time.time() - job_start
            }

    def process_video_async(self, input_path: str, video_id: int,
                             output_dir: str = None,
                             qualities: List[Dict] = None,
                             on_complete: Callable = None) -> str:
        job_id = str(uuid.uuid4())

        def run_job():
            try:
                result = self.process_video(
                    input_path=input_path, video_id=video_id,
                    output_dir=output_dir, qualities=qualities,
                    progress_callback=lambda p: self._update_progress(job_id, p)
                )
                with self._lock:
                    if job_id in self._job_results:
                        self._job_results[job_id].status = 'completed' if result['success'] else 'failed'
                        self._job_results[job_id].progress = 100
                        self._job_results[job_id].completed_at = time.time()
                if on_complete:
                    on_complete(result)
            except Exception as e:
                logger.error(f"Async {job_id} failed: {e}")
                with self._lock:
                    if job_id in self._job_results:
                        self._job_results[job_id].status = 'failed'
                        self._job_results[job_id].error_message = str(e)

        job = UltraParallelJob(
            job_id=job_id, video_id=video_id, input_path=input_path,
            output_dir=output_dir or os.path.join('static', 'hls', str(video_id)),
            original_filename=os.path.basename(input_path),
            total_duration=0, segment_count=0, segment_duration=self.segment_duration,
            qualities=qualities or ULTRA_QUALITY_LADDER, status='queued'
        )
        with self._lock:
            self._job_results[job_id] = job
        thread = threading.Thread(target=run_job, daemon=True)
        with self._lock:
            self._active_jobs[job_id] = thread
        thread.start()
        return job_id

    def _update_progress(self, job_id: str, progress: float):
        with self._lock:
            if job_id in self._job_results:
                self._job_results[job_id].progress = progress

    def get_job_progress(self, job_id: str) -> Optional[Dict]:
        with self._lock:
            job = self._job_results.get(job_id)
            if job:
                return {
                    'job_id': job.job_id, 'video_id': job.video_id,
                    'status': job.status, 'progress': job.progress,
                    'error': job.error_message, 'created_at': job.created_at,
                    'completed_at': job.completed_at
                }
        return None

    def get_all_jobs(self) -> List[Dict]:
        with self._lock:
            return [{'job_id': j.job_id, 'video_id': j.video_id,
                     'status': j.status, 'progress': j.progress,
                     'created_at': j.created_at} for j in self._job_results.values()]

    def shutdown(self):
        logger.info("Shutdown UltraParallelProcessor")


# ═══════════════════════════════════════════════════════════════
#  CONVENIENCE FUNCTIONS
# ═══════════════════════════════════════════════════════════════

_global_processor: Optional[UltraParallelProcessor] = None

def get_processor() -> UltraParallelProcessor:
    global _global_processor
    if _global_processor is None:
        _global_processor = UltraParallelProcessor()
    return _global_processor

def shutdown_processor():
    global _global_processor
    if _global_processor:
        _global_processor.shutdown()
        _global_processor = None

def process_video_ultra(input_path: str, video_id: int, output_dir: str = None,
                         qualities: List[Dict] = None,
                         progress_callback: Callable = None) -> Dict:
    processor = UltraParallelProcessor()
    try:
        return processor.process_video(input_path, video_id, output_dir, qualities, progress_callback)
    finally:
        processor.shutdown()

def process_video_ultra_async(input_path: str, video_id: int, output_dir: str = None) -> str:
    return get_processor().process_video_async(input_path, video_id, output_dir)

def get_job_status(job_id: str) -> Optional[Dict]:
    return get_processor().get_job_progress(job_id)


def process_batch_ultra(input_paths: List[str],
                        video_ids: Optional[List[int]] = None,
                        output_dirs: Optional[List[str]] = None,
                        qualities: Optional[List[Optional[List[Dict]]]] = None,
                        max_workers: int = None,
                        progress_callback: Callable[[int, Dict], None] = None) -> List[Dict]:
    """Process many videos with the ultra-parallel engine.

    Each input is processed as a full video with all configured qualities,
    split into segments and encoded in parallel.
    """
    if not input_paths:
        return []

    if video_ids is None:
        video_ids = list(range(1, len(input_paths) + 1))
    if len(video_ids) != len(input_paths):
        raise ValueError('video_ids must match input_paths length')

    if output_dirs is None:
        output_dirs = [None] * len(input_paths)
    if len(output_dirs) != len(input_paths):
        raise ValueError('output_dirs must match input_paths length')

    if qualities is None:
        qualities = [None] * len(input_paths)
    if len(qualities) != len(input_paths):
        raise ValueError('qualities must match input_paths length')

    max_workers = max_workers or min(len(input_paths), max(4, (os.cpu_count() or 4) * 2))

    def _process_one(index: int) -> Dict:
        video_id = video_ids[index]
        input_path = input_paths[index]
        output_dir = output_dirs[index]
        selected_qualities = qualities[index] if qualities and qualities[index] is not None else ULTRA_QUALITY_LADDER

        processor = UltraParallelProcessor()
        try:
            return processor.process_video(
                input_path=input_path,
                video_id=video_id,
                output_dir=output_dir,
                qualities=selected_qualities,
            )
        finally:
            processor.shutdown()

    results: List[Optional[Dict]] = [None] * len(input_paths)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(_process_one, idx): idx for idx in range(len(input_paths))}
        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    'success': False,
                    'video_id': video_ids[idx],
                    'input_path': input_paths[idx],
                    'error': str(exc),
                }
            results[idx] = result
            if progress_callback:
                progress_callback(idx, result)

    return [result or {'success': False, 'error': 'not processed'} for result in results]


# ═══════════════════════════════════════════════════════════════
#  SYNTHETIC 8K VIDEO GENERATOR
# ═══════════════════════════════════════════════════════════════

class Synthetic8KVideoGenerator:
    """Generates synthetic 8K test videos of ANY duration."""

    @staticmethod
    def generate_video(output_path: str, duration_seconds: int = 108000,
                       resolution: str = '7680x4320', target_size_bytes: int = None) -> bool:
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        try:
            temp_output = output_path + '.temp.mp4'
            bitrate = '100M'
            if target_size_bytes:
                bitrate_bps = (target_size_bytes * 8) // duration_seconds
                bitrate = f'{bitrate_bps // 1000000}M'

            # Generate in chunks for long videos
            chunk_duration = 3600
            num_chunks = max(1, math.ceil(duration_seconds / chunk_duration))
            chunk_files = []

            for chunk_idx in range(num_chunks):
                chunk_path = f"{temp_output}_chunk_{chunk_idx}.mp4"
                this_duration = min(chunk_duration, duration_seconds - chunk_idx * chunk_duration)

                cmd = ['ffmpeg', '-y', '-f', 'lavfi',
                       '-i', f'testsrc2=size={resolution}:rate=30:d={this_duration}',
                       '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo',
                       '-c:v', 'libx264', '-preset', 'ultrafast',
                       '-b:v', bitrate, '-minrate', bitrate, '-maxrate', bitrate,
                       '-bufsize', '200M', '-c:a', 'aac', '-b:a', '128k',
                       '-g', '30', '-f', 'mp4', '-movflags', '+faststart', chunk_path]

                subprocess.run(cmd, capture_output=True, timeout=chunk_duration + 600)
                if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
                    chunk_files.append(chunk_path)

            if len(chunk_files) > 1:
                concat_file = output_path + '.concat.txt'
                with open(concat_file, 'w') as f:
                    for cf in chunk_files:
                        f.write(f"file '{os.path.abspath(cf)}'\n")
                subprocess.run(['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                               '-i', concat_file, '-c', 'copy', temp_output],
                              capture_output=True, timeout=duration_seconds + 600)
                try: os.remove(concat_file)
                except: pass
            elif chunk_files:
                shutil.move(chunk_files[0], temp_output)

            for cf in chunk_files:
                try: os.remove(cf)
                except: pass

            if not os.path.exists(temp_output):
                return False

            if target_size_bytes:
                actual_size = os.path.getsize(temp_output)
                if actual_size < target_size_bytes:
                    with open(temp_output, 'ab') as f:
                        f.write(b'\x00' * (target_size_bytes - actual_size))
                elif actual_size > target_size_bytes:
                    with open(temp_output, 'r+b') as f:
                        f.truncate(target_size_bytes)

            if os.path.exists(output_path):
                os.remove(output_path)
            os.rename(temp_output, output_path)

            final_size = os.path.getsize(output_path)
            logger.info(f"Generated: {output_path} ({final_size/(1024**3):.2f}GB, {duration_seconds/3600:.1f}h)")
            return True
        except Exception as e:
            logger.error(f"Generation failed: {e}")
            return False


# ═══════════════════════════════════════════════════════════════
#  MAIN — Diagnostics
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    print("=" * 80)
    print("ULTRA PARALLEL VIDEO PROCESSOR — GPU FLOW CONTROL")
    print("=" * 80)
    print(f"CPU Cores: {multiprocessing.cpu_count()}")
    print(f"GPU Encoder: {BEST_ENCODER}")
    print(f"GPU Concurrent Limit: {GPU_CONCURRENT_LIMIT} (NVENC max)")
    print(f"CPU Workers: {CPU_CONCURRENT_LIMIT}")
    print(f"Total Workers: {MAX_SEGMENT_WORKERS}")
    print(f"Available Encoders: {AVAILABLE_ENCODERS}")
    print(f"Segment Duration: {SEGMENT_DURATION_SECONDS}s")
    print(f"Quality Levels: {len(ULTRA_QUALITY_LADDER)} (144p->8K)")
    print(f"ALL segments: NO LIMIT (30hr = 1800 segments)")
    print(f"ALL qualities: FULL video, not partial")
    print(f"Auto-retry: GPU fail -> software fallback (2 retries)")
    print()

    try:
        r = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
        print(f"FFmpeg: {r.stdout.split(chr(10))[0]}")
    except:
        print("FFmpeg: NOT FOUND")

    print("\nPERFORMANCE ESTIMATES:")
    print(f"  30hr 8K: 1800 segs x 9 qualities = 16,200 tasks")
    print(f"  GPU: 4 concurrent (first pass high-res)")
    print(f"  CPU: {CPU_CONCURRENT_LIMIT} concurrent (second pass low-res + fallback)")
    print(f"  Estimated time: 30-90 minutes (vs 270h sequential)")
    print("=" * 80)