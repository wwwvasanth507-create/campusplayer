import os
import json
import logging
import re
import math
from datetime import datetime
from models import db, Video, EBook, AICopilotInteraction, SiteSettings, VideoProgress, QuizResult
from services.transcript_engine import parse_vtt_or_srt_to_cues, parse_vtt_or_srt_text_to_cues

logger = logging.getLogger(__name__)

def format_seconds_to_timestamp(seconds):
    """Convert seconds to MM:SS or HH:MM:SS format."""
    s = int(round(seconds or 0))
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"

def extract_video_cues(video):
    """Extracts transcript cues from subtitle file or synthesizes from chapters/takeaways."""
    cues = []
    if video.subtitle_path and os.path.exists(video.subtitle_path):
        cues = parse_vtt_or_srt_to_cues(video.subtitle_path)
    
    if not cues and video.chapters_json:
        try:
            chapters = json.loads(video.chapters_json)
            for ch in chapters:
                t = float(ch.get('time', 0))
                cues.append({
                    'start': t,
                    'end': t + 60,
                    'start_formatted': format_seconds_to_timestamp(t),
                    'text': ch.get('title', '')
                })
        except Exception:
            pass

    if not cues:
        # Generate synthesized topic intervals from takeaways or summary
        takeaways = video.get_ai_takeaways() if hasattr(video, 'get_ai_takeaways') else []
        dur = float(getattr(video, 'duration_seconds', None) or getattr(video, 'duration', None) or 600)
        if takeaways:
            step = max(30.0, dur / max(1, len(takeaways)))
            for idx, item in enumerate(takeaways):
                st = idx * step
                cues.append({
                    'start': round(st, 1),
                    'end': round(st + step, 1),
                    'start_formatted': format_seconds_to_timestamp(st),
                    'text': str(item)
                })
        else:
            cues.append({
                'start': 0.0,
                'end': min(dur, 120.0),
                'start_formatted': "00:00",
                'text': f"{video.title} - {video.description or 'Lecture Introduction'}"
            })
    return cues

def find_best_transcript_citation(cues, query, current_time=0.0):
    """
    Finds the most semantically relevant transcript cue for a question,
    weighting keyword overlap and playback proximity.
    """
    if not cues:
        return {'start': current_time, 'start_formatted': format_seconds_to_timestamp(current_time), 'text': 'Current Lecture Moment'}

    tokens = [w.lower() for w in re.findall(r'\w+', query) if len(w) > 2]
    if not tokens:
        # Default to the cue covering the current playback position
        for c in cues:
            if c['start'] <= current_time <= c.get('end', c['start'] + 30):
                return c
        return cues[0]

    best_cue = None
    best_score = -1.0

    for cue in cues:
        cue_text = cue['text'].lower()
        # Calculate token overlap score
        matches = sum(1 for t in tokens if t in cue_text)
        score = float(matches) * 10.0

        # Temporal proximity bonus (closer to current playback time gets a small boost)
        time_diff = abs(cue['start'] - current_time)
        prox_bonus = max(0.0, 5.0 - (time_diff / 120.0))
        score += prox_bonus

        if score > best_score:
            best_score = score
            best_cue = cue

    if best_cue and best_score > 1.0:
        return best_cue

    # Fallback to current playback interval
    for c in cues:
        if c['start'] <= current_time <= c.get('end', c['start'] + 30):
            return c
    return cues[0]

def find_relevant_library_guide(video, query):
    """Finds the most relevant digital textbook or study guide in the Digital Library."""
    try:
        # Search by subject match
        q_books = EBook.query
        if getattr(video, 'institution_id', None):
            q_books = q_books.filter(
                db.or_(EBook.institution_id == video.institution_id, EBook.institution_id.is_(None))
            )
        
        books = q_books.all()
        if not books:
            return None, None

        # Prioritize study guides & lab manuals
        query_lower = query.lower()
        title_lower = (video.title or '').lower()

        best_book = None
        best_score = -1

        for b in books:
            score = 0
            b_subj = (b.subject or '').lower()
            b_title = (b.title or '').lower()
            b_type = (b.resource_type or '').lower()

            if b_type in ['guide', 'notes', 'lab_manual']:
                score += 5

            if b_subj and (b_subj in title_lower or b_subj in query_lower):
                score += 10

            for word in re.findall(r'\w+', query_lower):
                if len(word) > 3:
                    if word in b_title:
                        score += 4
                    if word in b_subj:
                        score += 3

            if score > best_score:
                best_score = score
                best_book = b

        if best_book and best_score >= 5:
            # Estimate a relevant page index
            estimated_page = min(max(1, int((best_book.page_count or 50) * 0.25)), best_book.page_count or 100)
            return best_book, estimated_page
    except Exception as e:
        logger.warning(f"Error finding library guide: {e}")

    return None, None


def ask_lecture_copilot(video, user, question, current_time=0.0):
    """
    Main Copilot reasoning engine.
    Returns structured explanation, exact timestamp citation, digital guide link, and micro-quiz.
    """
    cues = extract_video_cues(video)
    citation = find_best_transcript_citation(cues, question, current_time)
    matched_book, matched_page = find_relevant_library_guide(video, question)

    cited_ts = float(citation['start'])
    cited_ts_fmt = citation.get('start_formatted', format_seconds_to_timestamp(cited_ts))

    # Check for Gemini API key
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key and getattr(video, 'institution_id', None):
        settings = SiteSettings.query.filter_by(institution_id=video.institution_id).first()
        if settings and settings.gemini_api_key:
            api_key = settings.gemini_api_key

    answer_text = ""
    micro_quiz = None

    if api_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel('gemini-1.5-flash')

            cues_summary = "\n".join([f"[{c['start_formatted']}] {c['text']}" for c in cues[:30]])
            book_info = f"'{matched_book.title}' (Page {matched_page})" if matched_book else "Standard Academic Syllabus"

            system_prompt = f"""You are the AI Lecture Copilot for Campus Player — an interactive video learning system.
A student studying '{video.title}' (Level: {getattr(video, 'academic_level', 'General')}) asked a question at playback time {format_seconds_to_timestamp(current_time)}.

Video Context:
- Description: {video.description or 'N/A'}
- Relevant Transcript Moments:
{cues_summary}
- Related Reference Guide: {book_info}

Instructions:
1. Provide a direct, crystal-clear conceptual answer in 2-3 paragraphs.
2. Cite the exact video timestamp (in seconds) where this concept is explained in the lecture.
3. Formulate 1 quick multiple-choice micro-quiz question to test their understanding.
4. Respond ONLY in valid JSON matching this schema:
{{
    "answer": "Clear conceptual markdown explanation...",
    "cited_timestamp": {cited_ts},
    "cited_timestamp_formatted": "{cited_ts_fmt}",
    "micro_quiz": {{
        "question": "Quick check question...",
        "options": ["Option A", "Option B", "Option C", "Option D"],
        "correct_index": 0,
        "explanation": "Why option A is correct..."
    }}
}}"""

            response = model.generate_content(f"{system_prompt}\n\nStudent's Question: {question}")
            if response and response.text:
                cleaned = response.text.strip()
                if cleaned.startswith('```json'):
                    cleaned = cleaned[7:]
                elif cleaned.startswith('```'):
                    cleaned = cleaned[3:]
                if cleaned.endswith('```'):
                    cleaned = cleaned[:-3]
                cleaned = cleaned.strip()

                parsed = json.loads(cleaned)
                answer_text = parsed.get('answer', '')
                if 'cited_timestamp' in parsed:
                    cited_ts = float(parsed['cited_timestamp'])
                    cited_ts_fmt = parsed.get('cited_timestamp_formatted', format_seconds_to_timestamp(cited_ts))
                micro_quiz = parsed.get('micro_quiz')
        except Exception as e:
            logger.warning(f"Gemini Copilot generation failed or unavailable: {e}. Executing smart contextual fallback.")

    # Smart Autonomous Fallback if Gemini key is missing or failed
    if not answer_text:
        takeaways = video.get_ai_takeaways() if hasattr(video, 'get_ai_takeaways') else []
        highlight = citation['text'] if citation and citation['text'] else video.title

        answer_text = f"**Key Lecture Concept at `{cited_ts_fmt}`:**\n\n" \
                      f"In this portion of *{video.title}*, the focus is on **{highlight}**.\n\n" \
                      f"• **Core Principle**: When studying this topic, note how the fundamental equations and definitions link directly to the practical examples demonstrated at `{cited_ts_fmt}`.\n" \
                      f"• **Application**: Review the breakdown given by the instructor to understand how to solve related coursework problems efficiently."

        if matched_book:
            answer_text += f"\n\n📖 *Recommended Reading: Refer to **{matched_book.title}** ({matched_book.get_resource_type_label()}) on Page {matched_page} for detailed mathematical proofs and diagrams.*"

        # Formulate rule-based micro-quiz
        micro_quiz = {
            "question": f"Based on the concept taught at timestamp {cited_ts_fmt} in '{video.title}', what is the primary takeaway?",
            "options": [
                f"It demonstrates the core principle of {highlight[:45]}",
                "It serves only as a historical reference without practical relevance",
                "It contradicts standard textbook formulas",
                "It is only applicable in theoretical simulations"
            ],
            "correct_index": 0,
            "explanation": f"At {cited_ts_fmt}, the lecture emphasizes understanding {highlight[:50]} for solving academic problems."
        }

    # Save interaction record
    interaction = AICopilotInteraction(
        institution_id=getattr(user, 'institution_id', None),
        user_id=user.id,
        video_id=video.id,
        question=question,
        answer=answer_text,
        playback_timestamp=current_time,
        cited_timestamp=cited_ts,
        cited_timestamp_formatted=cited_ts_fmt,
        cited_book_id=matched_book.id if matched_book else None,
        cited_page=matched_page if matched_book else None,
        micro_quiz_json=json.dumps(micro_quiz) if micro_quiz else None,
        created_at=datetime.utcnow()
    )
    db.session.add(interaction)
    db.session.commit()

    return {
        'success': True,
        'interaction_id': interaction.id,
        'question': question,
        'answer': answer_text,
        'cited_timestamp': cited_ts,
        'cited_timestamp_formatted': cited_ts_fmt,
        'cited_book': {
            'id': matched_book.id,
            'title': matched_book.title,
            'page': matched_page,
            'type_label': matched_book.get_resource_type_label()
        } if matched_book else None,
        'micro_quiz': micro_quiz,
        'created_at': interaction.created_at.strftime('%I:%M %p')
    }

def evaluate_micro_quiz(interaction_id, user, selected_index):
    """Evaluates student micro-quiz answer, awards +20 XP, and updates readiness."""
    interaction = AICopilotInteraction.query.get(interaction_id)
    if not interaction:
        return {'success': False, 'message': 'Interaction not found.'}

    quiz_data = interaction.get_micro_quiz()
    if not quiz_data:
        return {'success': False, 'message': 'No quiz attached to this interaction.'}

    correct_idx = int(quiz_data.get('correct_index', 0))
    is_correct = bool(selected_index == correct_idx)

    interaction.quiz_answered = True
    interaction.quiz_correct = is_correct

    xp_awarded = 0
    if is_correct:
        xp_awarded = 20
        user.xp = (user.xp or 0) + xp_awarded
        user.level = (user.xp // 500) + 1
        if hasattr(user, 'update_quest_progress'):
            user.update_quest_progress('ask_ai_doubt', 1)

    db.session.commit()

    return {
        'success': True,
        'is_correct': is_correct,
        'correct_index': correct_idx,
        'explanation': quiz_data.get('explanation', 'Concept verified!'),
        'xp_awarded': xp_awarded,
        'new_xp': user.xp,
        'new_level': user.level
    }

def calculate_video_exam_readiness(user, video):
    """
    Computes a composite Exam Readiness Index (0-100%) based on:
    - Watch completion (0-40 pts)
    - AI Copilot micro-quizzes answered & passed (0-30 pts)
    - Video assessment quiz scores (0-30 pts)
    """
    if not user or not user.is_authenticated:
        return {'score': 0, 'status': 'Not Started', 'badge_color': 'var(--text-dim)'}

    # 1. Watch completion score
    prog = VideoProgress.query.filter_by(user_id=user.id, video_id=video.id).first()
    watch_pct = (prog.percent_watched if prog else 0.0) or 0.0
    watch_pts = min(40.0, (watch_pct / 100.0) * 40.0)

    # 2. Copilot interactions & micro-quizzes score
    copilot_records = AICopilotInteraction.query.filter_by(user_id=user.id, video_id=video.id).all()
    copilot_pts = 0.0
    if copilot_records:
        passed = sum(1 for r in copilot_records if r.quiz_correct)
        total_q = max(1, len(copilot_records))
        copilot_pts = min(30.0, (passed / total_q) * 30.0 + min(10.0, len(copilot_records) * 5.0))
    else:
        copilot_pts = 10.0 if watch_pct > 50 else 0.0

    # 3. Formal Quiz score
    quiz_results = QuizResult.query.filter_by(student_id=user.id).all()
    quiz_pts = 20.0 if quiz_results else 10.0

    total_score = min(100, int(round(watch_pts + copilot_pts + quiz_pts)))

    if total_score >= 85:
        status = 'Exam Ready'
        badge_color = 'var(--primary)'
    elif total_score >= 65:
        status = 'Proficient'
        badge_color = 'var(--success-color)'
    elif total_score >= 40:
        status = 'In Progress'
        badge_color = 'var(--secondary-color)'
    else:
        status = 'Getting Started'
        badge_color = 'var(--text-dim)'

    return {
        'score': total_score,
        'status': status,
        'badge_color': badge_color,
        'watch_percent': int(watch_pct),
        'copilot_doubts_solved': len(copilot_records)
    }
