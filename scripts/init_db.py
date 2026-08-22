"""
Idempotent Database Initialization Script for CampusPlayer (PostgreSQL).

Usage:
    python scripts/init_db.py
"""
import os
import sys
import logging
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger('init_db')

def init_database():
    from factory import create_app
    from extensions import db
    from models import (
        User, Institution, SiteSettings, DailyQuestTemplate,
        backfill_all_tables_with_default_institution
    )
    from crypto_helper import encrypt_password

    app = create_app()

    with app.app_context():
        logger.info("Verifying PostgreSQL database connection...")
        try:
            db.session.execute(db.text("SELECT 1"))
            logger.info("PostgreSQL database connection verified.")
        except Exception as e:
            logger.error(f"Failed to connect to PostgreSQL database: {e}")
            sys.exit(1)

        logger.info("Applying database migrations / schema synchronization...")
        try:
            from flask_migrate import upgrade
            upgrade()
            logger.info("Database migration upgrade completed successfully.")
        except Exception as e:
            logger.warning(f"Flask-Migrate upgrade warning/fallback: {e}")
            db.create_all()
            logger.info("db.create_all() executed.")

        logger.info("Ensuring Default Institution exists...")
        default_inst = Institution.query.filter_by(slug='default').first()
        if not default_inst:
            default_inst = Institution(
                name='Default Institution',
                slug='default',
                status='active',
                allow_manual_video_delete=True,
                allow_auto_video_delete=True,
                max_video_retention_days=365
            )
            db.session.add(default_inst)
            db.session.commit()
            logger.info(f"Created Default Institution (id={default_inst.id})")
        else:
            logger.info(f"Default Institution already exists (id={default_inst.id})")

        # Backfill unattached records into default institution
        backfill_all_tables_with_default_institution(db, logger=logger)

        logger.info("Verifying System Administrator Account...")
        sysadmin_user = os.getenv('SYSADMIN_USERNAME', 'systemadmin')
        sysadmin_pass = os.getenv('SYSADMIN_PASSWORD', 'SystemAdmin123!')

        admin = User.query.filter_by(username=sysadmin_user, role='system_admin').first()
        if not admin:
            admin = User(
                username=sysadmin_user,
                password_hash=encrypt_password(sysadmin_pass),
                role='system_admin',
                institution_id=None,
                display_name='System Administrator',
                is_active_account=True
            )
            db.session.add(admin)
            db.session.commit()
            logger.info(f"Created initial System Administrator: '{sysadmin_user}'")
        else:
            logger.info(f"System Administrator '{sysadmin_user}' exists.")

        # Ensure SiteSettings exists for default institution
        site_settings = SiteSettings.query.filter_by(institution_id=default_inst.id).first()
        if not site_settings:
            site_settings = SiteSettings(
                institution_id=default_inst.id,
                site_name='Campus Player',
                contact_email='admin@campusplayer.internal'
            )
            db.session.add(site_settings)
            db.session.commit()
            logger.info("Created default SiteSettings record.")

        logger.info("Database initialization completed successfully!")

if __name__ == '__main__':
    init_database()
