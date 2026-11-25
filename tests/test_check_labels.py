"""check_labels shows which of a company's FieldRoutes labels the rules recognise."""
from __future__ import annotations

import sqlite3

import check_labels


def test_labels_are_matched_the_way_the_engine_matches_them():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE subscriptions (source TEXT, service_type TEXT, cancel_reason TEXT)")
    c.execute("CREATE TABLE service_types (type_id TEXT, description TEXT)")
    c.executemany("INSERT INTO subscriptions VALUES (?,?,?)", [
        ("Web Lead", "Quarterly Pest", "Moved OOC"),
        ("Inside Sales Renewal", "Monthly Pest", "Renewal"),
        ("Upsell", "8 Month Mosquito", "Customer Moved"),     # NOT recognised as moved
    ])
    c.executemany("INSERT INTO service_types VALUES (?,?)",
                  [("2", "Initial Service"), ("26", "Cancellation Charge"), ("240", "Tip")])
    lines: list[str] = []
    check_labels.report(c, alt={"web lead"}, commercial=set(), out=lines.append)
    text = "\n".join(lines)
    row = lambda label: next(x for x in lines if f"  {label:<40} " in x)  # noqa: E731
    assert "alternate rate (ALT_RATE_SOURCES)" in row("Web Lead")
    assert "contains 'renewal'" in row("Inside Sales Renewal")
    assert "moved: no deduction" in row("Moved OOC")
    assert row("Customer Moved").rstrip().endswith("-")
    assert "renewal: not a real cancel" in row("Renewal")
    assert "cancellation-fee invoice" in row("Cancellation Charge")
    assert "ids above 199 are never read" in text


def test_take_snapshot_copies_what_is_not_yet_checkpointed(tmp_path):
    import take_snapshot
    from commissions.store import connect, set_meta
    db = tmp_path / "commissions.sqlite"
    c = connect(db)                                    # WAL mode, kept open like a running app
    set_meta(c, "last_sync", "2026-09-26 08:00:00")
    c.commit()
    out = tmp_path / "cutover" / "snapshot.sqlite"
    take_snapshot.take(db, out)
    got = sqlite3.connect(out).execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
    assert got == ("2026-09-26 08:00:00",)
    c.close()
