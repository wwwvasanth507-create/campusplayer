"""
CampusPlayer - Platform Audit CLI Tool.

Usage:
    python audit_platform.py                      # Print audit summary
    python audit_platform.py --save-baseline      # Save baseline metrics to audit_baseline.json
    python audit_platform.py --verify-baseline    # Compare current audit with audit_baseline.json
"""

import os
import sys
import json
import argparse
from services.audit_engine import run_platform_audit

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
BASELINE_FILE = os.path.join(BASE_DIR, 'audit_baseline.json')


def main():
    parser = argparse.ArgumentParser(description="CampusPlayer Data Audit Tool")
    parser.add_argument('--save-baseline', action='store_true', help="Save audit report as deployment baseline")
    parser.add_argument('--verify-baseline', action='store_true', help="Verify current state against saved baseline")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    healthy, report = run_platform_audit()

    print("=" * 60)
    print("CampusPlayer Data & Platform Audit Report")
    print("=" * 60)
    print(f"Timestamp:           {report['timestamp']}")
    print(f"Integrity Status:    {report['integrity']['status'].upper()}")
    print(f"Foreign Key Status:  {report['foreign_keys']['status'].upper()}")
    print(f"Orphan Check Status: {report['orphaned_records']['status'].upper()}")
    print("-" * 60)
    print("Core Table Record Counts:")
    for k, v in report['counts'].items():
        print(f"  - {k:<25}: {v}")
    print("=" * 60)

    if not healthy:
        print("[FAIL] AUDIT FAILED - Database integrity or foreign key errors detected!")
        if report['integrity']['message']:
            print(f"  Integrity Error: {report['integrity']['message']}")
        if report['foreign_keys']['violations']:
            print("  FK Violations:")
            for v in report['foreign_keys']['violations']:
                print(f"    - {v}")
        if report['orphaned_records']['orphans']:
            print("  Orphaned Records:")
            for o in report['orphaned_records']['orphans']:
                print(f"    - {o}")
        sys.exit(1)

    if args.save_baseline:
        try:
            with open(BASELINE_FILE, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2)
            print(f"[OK] Saved baseline report to {BASELINE_FILE}")
        except Exception as e:
            print(f"[FAIL] Failed to save baseline file: {e}")
            sys.exit(1)

    elif args.verify_baseline:
        if not os.path.exists(BASELINE_FILE):
            print(f"[NOTICE] No baseline file found at {BASELINE_FILE}. Saving current state as baseline...")
            with open(BASELINE_FILE, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2)
            print("[OK] Baseline saved.")
            sys.exit(0)

        try:
            with open(BASELINE_FILE, 'r', encoding='utf-8') as f:
                baseline = json.load(f)

            b_counts = baseline.get('counts', {})
            c_counts = report.get('counts', {})

            critical_keys = ['institutions', 'users_total', 'videos', 'playlists', 'classrooms', 'quizzes', 'comments']
            drops = []
            for k in critical_keys:
                b_val = b_counts.get(k, 0)
                c_val = c_counts.get(k, 0)
                if c_val < b_val:
                    drops.append(f"{k}: baseline={b_val}, current={c_val} (DROPPED by {b_val - c_val})")

            if drops:
                print("[FAIL] BASELINE VERIFICATION FAILED! Critical record count drop detected:")
                for d in drops:
                    print(f"  - {d}")
                sys.exit(1)
            else:
                print("[OK] BASELINE VERIFICATION PASSED! Zero unexpected data drops detected.")

        except Exception as e:
            print(f"[FAIL] Error verifying baseline: {e}")
            sys.exit(1)

    print("[OK] Audit completed successfully.")


    sys.exit(0)


if __name__ == '__main__':
    main()
