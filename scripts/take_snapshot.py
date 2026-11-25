"""Take the switch-over snapshot (COMMISSION_RULES.md §8c, ONBOARDING.md phase 6).

Run it right when your manual payroll pulls its data for its LAST period, after
`scripts/sync.py`: it saves a consistent copy of data/commissions.sqlite as
data/cutover/snapshot.sqlite, the app's view of FieldRoutes at the same moment the
manual payroll looked. (A plain file copy can miss the newest writes: the database
keeps them in a side file until it is checkpointed.)

    python scripts/take_snapshot.py            # refuses to replace an existing snapshot
    python scripts/take_snapshot.py --replace
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def take(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    d = sqlite3.connect(tmp)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    tmp.replace(dst)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "commissions.sqlite"))
    ap.add_argument("--out", default=str(ROOT / "data" / "cutover" / "snapshot.sqlite"))
    ap.add_argument("--replace", action="store_true")
    a = ap.parse_args()
    src, dst = Path(a.db), Path(a.out)
    if not src.is_file():
        print(f"no database at {src}")
        return 2
    if dst.exists() and not a.replace:
        print(f"{dst} already exists: pass --replace to take it again")
        return 1
    take(src, dst)
    last = sqlite3.connect(f"file:{dst.as_posix()}?mode=ro", uri=True).execute(
        "SELECT value FROM meta WHERE key='last_sync'").fetchone()
    print(f"snapshot saved {datetime.now():%Y-%m-%d %H:%M} -> {dst} "
          f"(data synced up to {last[0] if last else 'unknown'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
