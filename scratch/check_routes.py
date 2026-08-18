import sys, os, re, glob
sys.path.insert(0, os.path.abspath('.'))
from app import app

with app.app_context():
    endpoints = set(app.view_functions.keys())

broken = []
for filepath in glob.glob('templates/**/*.html', recursive=True):
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    matches = re.findall(r"url_for\(\s*['\"]([a-zA-Z0-9_\.]+)", content)
    for ep in matches:
        if ep not in endpoints and ep != 'static':
            broken.append((filepath, ep))

if broken:
    print(f'BROKEN URL_FOR ENDPOINTS FOUND ({len(broken)}):')
    for f, ep in sorted(list(set(broken))):
        print(f'  File: {f} -> Unknown Endpoint: {ep}')
else:
    print('ALL URL_FOR ENDPOINTS IN TEMPLATES ARE VALID!')
