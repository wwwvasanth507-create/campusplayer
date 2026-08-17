from factory import create_app
from models import User

app = create_app()

with app.app_context():
    u = User.query.filter_by(role='admin').first()
    if u:
        print(f"Username: '{u.username}'")
        print(f"Role: '{u.role}'")
    else:
        print("No admin user found.")
