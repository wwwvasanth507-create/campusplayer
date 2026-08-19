import logging
from models import Video, ViewAnalytics, VideoProgress

logger = logging.getLogger(__name__)

def calculate_video_retention_curve(video_id, num_buckets=50):
    """
    Computes an aggregated viewer retention curve across the duration of a video.
    Returns:
    {
        "duration_seconds": total_duration,
        "total_views": total_views,
        "labels": ["0:00", "0:30", "1:00", ...],
        "retention_percent": [100.0, 95.2, 88.0, ...],
        "hotspots": [{"time_formatted": "02:15", "type": "spike", "label": "High Re-watch Rate"}]
    }
    """
    video = Video.query.get(video_id)
    if not video:
        return {'labels': [], 'retention_percent': [], 'total_views': 0, 'duration_seconds': 0, 'hotspots': []}

    duration = video.duration_seconds or 300  # default 5 mins if 0
    if duration <= 0:
        duration = 300

    bucket_size = max(1, duration / float(num_buckets))
    labels = []
    bucket_times = []

    for i in range(num_buckets):
        t = i * bucket_size
        m = int(t // 60)
        s = int(t % 60)
        labels.append(f"{m:02d}:{s:02d}")
        bucket_times.append(t)

    views = ViewAnalytics.query.filter_by(video_id=video_id).all()
    progress_records = VideoProgress.query.filter_by(video_id=video_id).all()

    total_sessions = len(views) + len(progress_records)
    if total_sessions == 0:
        # Default smooth baseline for new videos
        retention = [max(10.0, round(100.0 - (i * (60.0 / num_buckets)), 1)) for i in range(num_buckets)]
        return {
            'duration_seconds': duration,
            'total_views': 0,
            'labels': labels,
            'retention_percent': retention,
            'hotspots': []
        }

    # Aggregate watch position reach
    reach_counts = [0] * num_buckets

    for v in views:
        watched_sec = min(duration, (v.duration_seconds or 0))
        pct = v.percent_watched or 0.0
        if pct > 0:
            watched_sec = max(watched_sec, duration * (pct / 100.0))
        for i in range(num_buckets):
            if bucket_times[i] <= watched_sec:
                reach_counts[i] += 1

    for p in progress_records:
        prog_sec = min(duration, p.progress_seconds or 0)
        for i in range(num_buckets):
            if bucket_times[i] <= prog_sec:
                reach_counts[i] += 1

    max_reach = max(reach_counts[0], 1)
    retention_percent = []
    for count in reach_counts:
        pct = round((count / float(max_reach)) * 100.0, 1)
        retention_percent.append(pct)

    # Detect hotspots (where retention drops less or spikes)
    hotspots = []
    for i in range(1, num_buckets - 1):
        if retention_percent[i] >= retention_percent[i - 1] and retention_percent[i] > 20:
            hotspots.append({
                'time_formatted': labels[i],
                'type': 'spike',
                'label': 'Concept Review Hotspot'
            })

    return {
        'duration_seconds': duration,
        'total_views': total_sessions,
        'labels': labels,
        'retention_percent': retention_percent,
        'hotspots': hotspots[:3]  # top 3 hotspots
    }
