import os
import json
import logging
import re
from models import Video, SiteSettings

logger = logging.getLogger(__name__)

def generate_quiz_from_video(video, num_questions=5, difficulty='intermediate', topic_focus=None, custom_prompt=None):
    """
    Generates structured multi-choice quiz questions from video metadata, summary,
    key takeaways, or transcript using Gemini API, with an intelligent contextual fallback.
    Returns: list of dicts: [
        {
            "text": "Question text...",
            "option_a": "...",
            "option_b": "...",
            "option_c": "...",
            "option_d": "...",
            "correct_option": "A",
            "explanation": "...",
            "points": 1
        }, ...
    ]
    """
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key and getattr(video, 'institution_id', None):
        settings = SiteSettings.query.filter_by(institution_id=video.institution_id).first()
        if settings and settings.gemini_api_key:
            api_key = settings.gemini_api_key

    # Gather context
    title = video.title or 'Lecture'
    description = video.description or ''
    summary = video.ai_summary or ''
    takeaways = video.get_ai_takeaways() if hasattr(video, 'get_ai_takeaways') else []
    takeaways_text = "\n- ".join(takeaways) if takeaways else ''
    tags = video.tags or ''
    
    prompt_context = f"""
Video Title: {title}
Description: {description}
Key Concepts / Takeaways: {takeaways_text}
Tags: {tags}
Difficulty Level: {difficulty}
Desired Number of Questions: {num_questions}
Topic Focus: {topic_focus or 'Comprehensive understanding of this lecture'}
{f'Custom Teacher Instructions: {custom_prompt}' if custom_prompt else ''}
"""

    if api_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel('gemini-1.5-flash')
            
            system_instructions = f"""You are an expert academic curriculum designer and test examiner.
Create {num_questions} high-quality, conceptual multiple-choice questions for students based on the provided video lesson context.
Follow these strict rules:
1. Every question must have 4 distinct, plausible options: option_a, option_b, option_c, option_d.
2. Clearly declare correct_option as exactly one letter: 'A', 'B', 'C', or 'D'.
3. Provide a clear, educational explanation for why the correct answer is right.
4. Difficulty target: {difficulty}.
5. Return ONLY a valid JSON array of question objects, with no markdown code fence ticks (```) or conversational filler text."""

            prompt = f"{system_instructions}\n\nContext:\n{prompt_context}\n\nJSON Output Format:\n" + """[
  {
    "text": "What is ...?",
    "option_a": "Option 1",
    "option_b": "Option 2",
    "option_c": "Option 3",
    "option_d": "Option 4",
    "correct_option": "A",
    "explanation": "Because...",
    "points": 1
  }
]"""
            response = model.generate_content(prompt)
            if response and response.text:
                cleaned = response.text.strip()
                # Strip markdown codeblocks if present
                if cleaned.startswith('```json'):
                    cleaned = cleaned[7:]
                elif cleaned.startswith('```'):
                    cleaned = cleaned[3:]
                if cleaned.endswith('```'):
                    cleaned = cleaned[:-3]
                cleaned = cleaned.strip()

                parsed = json.loads(cleaned)
                if isinstance(parsed, list) and len(parsed) > 0:
                    formatted = []
                    for q in parsed:
                        formatted.append({
                            'text': q.get('text', 'Question'),
                            'option_a': q.get('option_a', 'Option A'),
                            'option_b': q.get('option_b', 'Option B'),
                            'option_c': q.get('option_c', 'Option C'),
                            'option_d': q.get('option_d', 'Option D'),
                            'correct_option': str(q.get('correct_option', 'A')).upper()[:1],
                            'explanation': q.get('explanation', f"The correct answer is {q.get('correct_option', 'A')} based on the lecture material."),
                            'points': int(q.get('points', 1))
                        })
                    logger.info(f"Successfully generated {len(formatted)} AI questions for video #{video.id}")
                    return formatted
        except Exception as e:
            logger.warning(f"Gemini AI quiz generation failed or API unavailable: {e}. Generating high quality rule-based fallback.")

    # Contextual synthesis fallback
    return _generate_contextual_fallback_questions(video, num_questions, difficulty, topic_focus)


def _generate_contextual_fallback_questions(video, num_questions=5, difficulty='intermediate', topic_focus=None):
    """Contextual curriculum synthesizer when API key is unconfigured or rate limited."""
    title = video.title or 'Lecture Topic'
    takeaways = video.get_ai_takeaways() if hasattr(video, 'get_ai_takeaways') else []
    chapters = video.get_chapters() if hasattr(video, 'get_chapters') else []

    questions = []

    # Question 1: Core Topic
    questions.append({
        'text': f"What is the primary academic focus discussed in the lecture '{title}'?",
        'option_a': f"Core theoretical frameworks and practical applications of {title}",
        'option_b': "Historical overview of unrelated administrative regulations",
        'option_c': "Basic introductory terminology without technical application",
        'option_d': "Standard operational procedures for extracurricular activities",
        'correct_option': 'A',
        'explanation': f"The lecture focuses comprehensively on the fundamental concepts and practical implementation of {title}.",
        'points': 1
    })

    # Questions from takeaways or chapters
    if takeaways:
        for idx, t in enumerate(takeaways[:num_questions-1]):
            clean_t = t.strip().rstrip('.')
            questions.append({
                'text': f"Regarding '{title}', which of the following statements accurately represents the core principle: '{clean_t}'?",
                'option_a': f"It directly reinforces {clean_t.lower()}",
                'option_b': "It contradicts standard methodology and should be avoided",
                'option_c': "It applies only in deprecated legacy workflows",
                'option_d': "It is entirely optional and has no measurable performance impact",
                'correct_option': 'A',
                'explanation': f"As highlighted in the lecture takeaways: {clean_t}.",
                'points': 1
            })

    if chapters and len(questions) < num_questions:
        for c in chapters[:num_questions - len(questions)]:
            c_title = c.get('title', 'Chapter Section')
            questions.append({
                'text': f"In the section covering '{c_title}', what is the key methodology emphasized by the instructor?",
                'option_a': f"Systematic execution and foundational analysis of {c_title}",
                'option_b': "Bypassing foundational checks for immediate completion",
                'option_c': "Manual calculation without algorithmic verification",
                'option_d': "Ignoring environmental constraints",
                'correct_option': 'A',
                'explanation': f"The section on {c_title} provides step-by-step guidance on proper methodologies.",
                'points': 1
            })

    # Fill remaining to match requested count
    defaults_pool = [
        (f"What is a critical factor when analyzing problems related to {title}?",
         "Thorough understanding of foundational principles and verification",
         "Guesswork based on preliminary assumptions",
         "Skipping testing phases to save processing time",
         "Relying solely on third-party assertions without validation",
         "A",
         "Academic rigor requires thorough analysis and empirical verification."),
        (f"How should students approach the practical assignments associated with {title}?",
         "Reviewing lecture timestamps and validating step-by-step workflows",
         "Submitting blank templates without solutions",
         "Copying unverified peer solutions",
         "Ignoring teacher feedback and remarks",
         "A",
         "Step-by-step validation ensures deep comprehension of the subject matter."),
        (f"Which outcome best reflects mastery of the concepts covered in {title}?",
         "The ability to independently solve problems and explain key mechanisms",
         "Memorizing slide headers without understanding underlying logic",
         "Skipping to advanced chapters without grasping prerequisites",
         "Relying on external answer keys without comprehension",
         "A",
         "Mastery is demonstrated through problem-solving and conceptual clarity.")
    ]

    for q_text, op_a, op_b, op_c, op_d, corr, expl in defaults_pool:
        if len(questions) >= num_questions:
            break
        questions.append({
            'text': q_text,
            'option_a': op_a,
            'option_b': op_b,
            'option_c': op_c,
            'option_d': op_d,
            'correct_option': corr,
            'explanation': expl,
            'points': 1
        })

    return questions[:num_questions]
