from flask import Blueprint, render_template, request, jsonify, url_for
from flask_login import login_required, current_user
from extensions import limiter
from services.utils import global_search
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
    if not query or len(query) < 1:
        return jsonify({'suggestions': []})
    suggestions = []
    seen_titles = set()
    for v in Video.query.filter(Video.title.contains(query)).limit(5).all():
        if v.title not in seen_titles:
            seen_titles.add(v.title)
            suggestions.append({'text': v.title, 'type': 'video', 'icon': 'videocam', 'url': url_for('video.watch_video', video_id=v.id)})
    for p in Playlist.query.filter(Playlist.title.contains(query)).limit(3).all():
        title = f"[Playlist] {p.title}"
        if title not in seen_titles:
            seen_titles.add(title)
            suggestions.append({'text': p.title, 'type': 'playlist', 'icon': 'playlist_play', 'url': url_for('video.view_playlist', playlist_id=p.id)})
    for c in Classroom.query.filter(Classroom.name.contains(query)).limit(3).all():
        title = f"[Class] {c.name}"
        if title not in seen_titles:
            seen_titles.add(title)
            suggestions.append({'text': c.name, 'type': 'class', 'icon': 'school', 'url': '#'})
    for u in User.query.filter(User.username.contains(query)).limit(4).all():
        if u.username not in seen_titles and u.id != current_user.id:
            seen_titles.add(u.username)
            suggestions.append({'text': f"{u.username} ({u.role})", 'type': 'user', 'icon': 'person', 'url': '#'})
    return jsonify({'suggestions': suggestions[:12]})
