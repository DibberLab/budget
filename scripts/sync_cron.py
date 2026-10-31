"""Scheduled bank sync (run by the budget-sync systemd timer).

Imports new, changed and removed transactions from Plaid, then lets the AI
file the ones it's sure about. Prints a one-line summary for the journal.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402

app = create_app()
with app.app_context():
    import plaid_client
    from v2.sync import run_ai_filing, run_sync

    if not plaid_client.is_configured():
        print("sync skipped: Plaid isn't configured")
        sys.exit(0)
    r = run_sync()
    if r["busy"]:
        print("sync skipped: another sync is running")
        sys.exit(0)
    filed = 0
    try:
        filed = run_ai_filing()
    except Exception as exc:  # AI is a nicety; never fail the sync over it
        print(f"AI filing failed: {exc}")
    print(f"sync: {r['added']} new ({r['pending']} pending), {r['posted']} posted, {r['modified']} modified, "
          f"{r['removed']} removed, {r['by_rule']} by rule, {filed} filed by AI"
          + (f"; skipped: {r['skipped']}" if r["skipped"] else "") + (f"; errors: {r['errors']}" if r["errors"] else ""))
    sys.exit(1 if r["errors"] else 0)
