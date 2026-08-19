import os
import re
import logging

logger = logging.getLogger(__name__)

def parse_vtt_or_srt_text_to_cues(content):
    """
    Parses WebVTT or SRT string content into an array of structured cue objects.
    """
    if not content:
        return []

    cues = []
    try:
        # Regular expressions for VTT (00:00:12.500 --> 00:00:15.000) or SRT (00:00:12,500 --> 00:00:15,000)
        pattern = re.compile(
            r'(?:(\d+)\s*\n)?'  # optional SRT cue number
            r'(\d{1,2}:)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{1,2}:)?(\d{2}):(\d{2})[.,](\d{3})'
            r'(?:[^\n]*\n)'  # optional line settings
            r'([\s\S]*?)(?=\n\s*\n|\Z)'
        )

        for match in pattern.finditer(content):
            # Parse start time
            sh = int(match.group(2).rstrip(':')) if match.group(2) else 0
            sm = int(match.group(3))
            ss = int(match.group(4))
            sms = int(match.group(5))
            start_sec = (sh * 3600) + (sm * 60) + ss + (sms / 1000.0)

            # Parse end time
            eh = int(match.group(6).rstrip(':')) if match.group(6) else 0
            em = int(match.group(7))
            es = int(match.group(8))
            ems = int(match.group(9))
            end_sec = (eh * 3600) + (em * 60) + es + (ems / 1000.0)

            raw_text = match.group(10)
            clean_text = re.sub(r'<[^>]+>', '', raw_text).strip()
            clean_text = " ".join(clean_text.split())

            if clean_text:
                mins = int(start_sec // 60)
                secs = int(start_sec % 60)
                cues.append({
                    'start': round(start_sec, 2),
                    'end': round(end_sec, 2),
                    'start_formatted': f"{mins:02d}:{secs:02d}",
                    'text': clean_text
                })

    except Exception as e:
        logger.warning(f"Error parsing subtitle text: {e}")

    return cues

def parse_vtt_or_srt_to_cues(file_path):
    """
    Parses a WebVTT or SRT subtitle file into an array of structured cues.
    """
    if not file_path or not os.path.exists(file_path):
        return []

    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        return parse_vtt_or_srt_text_to_cues(content)
    except Exception as e:
        logger.warning(f"Error reading subtitle file {file_path}: {e}")
        return []
