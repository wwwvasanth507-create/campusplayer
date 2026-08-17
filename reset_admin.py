from factory import create_app
from extensions import db
from models import User
from werkzeug.security import generate_password_hash

app = create_app()

def reset_admin():
    with app.app_context():
        admin = User.query.filter_by(role='admin').first()
        if admin:
            admin.set_password('admin123')
            db.session.commit()
            print(f"Admin user '{admin.username}' password has been reset to 'admin123'.")
        else:
            admin = User(username='admin', role='admin')
            admin.set_password('admin123')
            db.session.add(admin)
            db.session.commit()
            print("Admin user 'admin' did not exist. Created new admin with password 'admin123'.")

if __name__ == "__main__":
    reset_admin()
