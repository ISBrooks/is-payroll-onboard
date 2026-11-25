"""One-off + idempotent: add upsells.d2d_host and fill it for every booked
upsell whose host subscription was sold by a year-tagged D2D rep (e.g.
'R- Pat Example 2026'). Safe to re-run; new bookings get the tag at intake.

  python scripts/backfill_d2d_host.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from commissions.config import load_settings
from commissions.store import connect
from form_intake import d2d_host_tag


def main() -> None:
    s = load_settings()
    conn = connect(s.db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(upsells)")}
    if "d2d_host" not in cols:
        conn.execute("ALTER TABLE upsells ADD COLUMN d2d_host TEXT")
        print("added upsells.d2d_host")
    n = 0
    for uid, sid in conn.execute(
            "SELECT id, subscription_id FROM upsells "
            "WHERE removed_at IS NULL AND d2d_host IS NULL"):
        row = conn.execute("SELECT sold_by FROM subscriptions "
                           "WHERE subscription_id=?", (sid,)).fetchone()
        tag = d2d_host_tag(s, row[0]) if row else None
        if tag:
            conn.execute("UPDATE upsells SET d2d_host=? WHERE id=?", (tag, uid))
            n += 1
    conn.commit()
    print(f"tagged {n} upsell(s) with their D2D host")
    for r in conn.execute("""SELECT u.rep, s.customer_id, u.amount, u.d2d_host
            FROM upsells u JOIN subscriptions s USING(subscription_id)
            WHERE u.d2d_host IS NOT NULL AND u.removed_at IS NULL
            ORDER BY u.date"""):
        print("  ", r)


if __name__ == "__main__":
    main()
