from factory import create_app
from extensions import db
from models import User, Institution

app = create_app()

def reset_admin():
    with app.app_context():
        default_inst = Institution.query.filter_by(slug='default').first()
        inst_id = default_inst.id if default_inst else None

        admin = User.query.filter_by(username='admin').first()
        if admin:
            admin.set_password('admin123')
            if inst_id and not admin.institution_id:
                admin.institution_id = inst_id
            db.session.commit()
            print(f"Admin user '{admin.username}' password has been reset to 'admin123'.")
        else:
            admin = User(username='admin', role='admin', institution_id=inst_id)
            admin.set_password('admin123')
            db.session.add(admin)
            db.session.commit()
            if default_inst and not default_inst.owner_admin_id:
                default_inst.owner_admin_id = admin.id
                db.session.commit()
            print("Admin user 'admin' did not exist. Created new admin with password 'admin123'.")

if __name__ == "__main__":
    reset_admin()

