#!/usr/bin/env python3
"""
Database Migration Script for Adaptive HLS Streaming
======================================================
Adds new columns to the Video and SiteSettings models for adaptive streaming support.

Usage:
    python migrate_adaptive.py
"""

import os
import sys
import sqlite3
from datetime import datetime

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, 'instance', 'app.db')

def run_migration():
    """Run database migration to add adaptive streaming columns."""
    
    if not os.path.exists(DB_PATH):
        print(f"❌ Database not found at {DB_PATH}")
        print("   Run the Flask app first to create the database.")
        sys.exit(1)
    
    print(f"🔧 Connecting to database: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Get existing columns
    cursor.execute("PRAGMA table_info(video)")
    existing_columns = [row[1] for row in cursor.fetchall()]
    
    print(f"\n📋 Existing video columns: {len(existing_columns)}")
    
    # New columns for Video model (adaptive streaming)
    new_video_columns = {
        'master_playlist_path': 'VARCHAR(500)',
        'available_renditions': 'TEXT DEFAULT \'[]\'',
        'source_width': 'INTEGER DEFAULT 0',
        'source_height': 'INTEGER DEFAULT 0',
        'source_bitrate': 'INTEGER DEFAULT 0',
        'video_codec': 'VARCHAR(50) DEFAULT \'h264\'',
        'audio_codec': 'VARCHAR(50) DEFAULT \'aac\'',
        'fps': 'FLOAT DEFAULT 0.0',
        'has_adaptive_streams': 'BOOLEAN DEFAULT 0',
        'sprite_path': 'VARCHAR(500)',
        'sprite_tile_count': 'INTEGER DEFAULT 0',
        'thumbnails_vtt_path': 'VARCHAR(500)',
    }
    
    # New columns for SiteSettings model
    new_settings_columns = {
        'enable_adaptive_streaming': 'BOOLEAN DEFAULT 1',
        'max_rendition_height': 'INTEGER DEFAULT 1080',
        'hls_segment_duration': 'INTEGER DEFAULT 6',
    }
    
    # New column for ViewAnalytics
    new_view_columns = {
        'quality_selected': 'VARCHAR(20)',
    }
    
    # Apply video columns
    video_added = 0
    for col_name, col_type in new_video_columns.items():
        if col_name not in existing_columns:
            try:
                cursor.execute(f"ALTER TABLE video ADD COLUMN {col_name} {col_type}")
                print(f"  ✅ Added video.{col_name} ({col_type})")
                video_added += 1
            except sqlite3.OperationalError as e:
                print(f"  ⚠️  Could not add video.{col_name}: {e}")
    
    if video_added == 0:
        print("  ℹ️  No new video columns needed (all already exist)")
    
    # Apply settings columns
    cursor.execute("PRAGMA table_info(site_settings)")
    settings_cols = [row[1] for row in cursor.fetchall()]
    
    settings_added = 0
    for col_name, col_type in new_settings_columns.items():
        if col_name not in settings_cols:
            try:
                cursor.execute(f"ALTER TABLE site_settings ADD COLUMN {col_name} {col_type}")
                print(f"  ✅ Added site_settings.{col_name} ({col_type})")
                settings_added += 1
            except sqlite3.OperationalError as e:
                print(f"  ⚠️  Could not add site_settings.{col_name}: {e}")
    
    if settings_added == 0:
        print("  ℹ️  No new settings columns needed (all already exist)")
    
    # Apply view analytics columns
    cursor.execute("PRAGMA table_info(view_analytics)")
    view_cols = [row[1] for row in cursor.fetchall()]
    
    view_added = 0
    for col_name, col_type in new_view_columns.items():
        if col_name not in view_cols:
            try:
                cursor.execute(f"ALTER TABLE view_analytics ADD COLUMN {col_name} {col_type}")
                print(f"  ✅ Added view_analytics.{col_name} ({col_type})")
                view_added += 1
            except sqlite3.OperationalError as e:
                print(f"  ⚠️  Could not add view_analytics.{col_name}: {e}")
    
    if view_added == 0:
        print("  ℹ️  No new view_analytics columns needed (all already exist)")
    
    conn.commit()
    conn.close()
    
    print(f"\n✅ Migration completed successfully!")
    print(f"   Video columns added: {video_added}")
    print(f"   Settings columns added: {settings_added}")
    print(f"   View columns added: {view_added}")
    print(f"\n📊 Run the Flask app now to use the new adaptive streaming features.")


if __name__ == "__main__":
    try:
        import sys
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
    print("=" * 60)
    print("  CampusPlayer - Adaptive Streaming DB Migration")
    print("=" * 60)
    print()
    run_migration()