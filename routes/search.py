from flask import Blueprint, render_template, request, jsonify, url_for
from flask_login import login_required, current_user
from extensions import limiter
from services.utils import global_search, scope_to_institution
from models import Video, Playlist, Classroom, User

search_bp = Blueprint('search', __name__)


@search_bp.route('/search')
@login_required
def search_page():
    query = request.args.get('q', '').strip()
    results = global_search(query)
    best_type, best_score = None, 0
    for cat in ['videos', 'playlists', 'classes', 'quizzes', 'teachers', 'students']:
        items = results.get(cat, [])
        if items:
            max_score = max(getattr(i, '_search_score', 0) for i in items)
            if max_score > best_score:
                best_score, best_type = max_score, cat
    return render_template('search_results.html', results=results, query=query, best_type=best_type, search_query=query)


@search_bp.route('/api/search/suggest')
@login_required
@limiter.limit('30 per minute')
def search_suggest():
    query = request.args.get('q', '').strip()
    print("ACTUAL USER IN SEARCH:", current_user, "is_auth=", current_user.is_authenticated, "role=", getattr(current_user, 'role', None), "inst=", getattr(current_user, 'institution_id', None))
    if not query or len(query) < 1:
        return jsonify({'suggestions': []})

    suggestions = []
    seen_titles = set()
    term = f"%{query}%"

    v_q = scope_to_institution(Video.query.filter(Video.title.ilike(term)), Video).limit(5).all()


    for v in v_q:
        if v.title not in seen_titles:
            seen_titles.add(v.title)
            suggestions.append({'text': v.title, 'type': 'video', 'icon': 'videocam', 'url': url_for('video.watch_video', video_id=v.id)})

    p_q = scope_to_institution(Playlist.query.filter(Playlist.title.ilike(term)), Playlist).limit(3).all()
    for p in p_q:
        title = f"[Playlist] {p.title}"
        if title not in seen_titles:
            seen_titles.add(title)
            suggestions.append({'text': p.title, 'type': 'playlist', 'icon': 'playlist_play', 'url': url_for('video.view_playlist', playlist_id=p.id)})

    c_q = scope_to_institution(Classroom.query.filter(Classroom.name.ilike(term)), Classroom).limit(3).all()
    for c in c_q:
        title = f"[Class] {c.name}"
        if title not in seen_titles:
            seen_titles.add(title)
            suggestions.append({'text': c.name, 'type': 'class', 'icon': 'school', 'url': '#'})

    u_q = scope_to_institution(User.query.filter(User.username.ilike(term)), User).limit(4).all()
    for u in u_q:
        if u.username not in seen_titles and u.id != current_user.id:
            seen_titles.add(u.username)
            suggestions.append({'text': f"{u.username} ({u.role})", 'type': 'user', 'icon': 'person', 'url': '#'})

    return jsonify({'suggestions': suggestions[:12]})

