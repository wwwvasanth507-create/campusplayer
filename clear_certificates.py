import sqlite3
import shutil
import os
from datetime import datetime

DB_PATH = 'app.db'
BACKUP_PATH = f'app.db.bak_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}'

def main():
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, BACKUP_PATH)
        print(f"Backup created at: {BACKUP_PATH}")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 1. Count records before deletion
    cursor.execute("SELECT COUNT(*) FROM academic_certificate")
    count_before = cursor.fetchone()[0]
    print(f"Academic Certificates before cleanup: {count_before}")

    # 2. Delete academic certificates permanently
    cursor.execute("DELETE FROM academic_certificate")
    deleted_certs = cursor.rowcount
    print(f"Deleted {deleted_certs} academic certificate records.")

    # 3. Clear student_profile certificate upload fields if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='student_profile'")
    if cursor.fetchone():
        cursor.execute("""
            UPDATE student_profile 
            SET transfer_certificate_path = NULL, 
                community_certificate_path = NULL, 
                other_certificates_json = NULL
        """)
        updated_profiles = cursor.rowcount
        print(f"Cleared uploaded certificate paths in {updated_profiles} student profiles.")

    conn.commit()

    # 4. Verify cleanup
    cursor.execute("SELECT COUNT(*) FROM academic_certificate")
    count_after = cursor.fetchone()[0]
    print(f"Academic Certificates after cleanup: {count_after}")

    conn.close()

if __name__ == '__main__':
    main()
