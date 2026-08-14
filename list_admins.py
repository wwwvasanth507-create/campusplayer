from factory import create_app
from models import User

app = create_app()

def list_admins():
    with app.app_context():
        admins = User.query.filter_by(role='admin').all()
        print(f"Total admins found: {len(admins)}")
        for admin in admins:
            print(f"ID: {admin.id}, Username: {admin.username}")

if __name__ == "__main__":
    list_admins()
