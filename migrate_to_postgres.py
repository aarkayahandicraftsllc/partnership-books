"""Copies your books from the local SQLite file into a Postgres database.

    python migrate_to_postgres.py "postgresql://user:pass@host/dbname"

Reads from BOOKS_DB (default data/tracker.db), writes to the URL you pass.
Nothing is written to the source, and the destination is only touched once the
source has been read and summarised. Pass --dry-run to see what would move.

The same path is available without the command line: take a backup from
Setup on one machine and restore it on the other.
"""

from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    dry_run = "--dry-run" in argv
    if not args:
        print(__doc__)
        return 2
    target_url = args[0]

    # read the source first, with BOOKS_DB_URL unset so db.py picks SQLite
    os.environ.pop("BOOKS_DB_URL", None)
    import db

    source_path = db.DB_PATH
    if not source_path.exists():
        print(f"No SQLite database at {source_path}")
        return 1
    db.init_db()
    payload = db.dump_all()
    counts = {name: len(rows) for name, rows in payload["tables"].items()}
    print(f"Source  {source_path}")
    for name, count in counts.items():
        print(f"  {name:14} {count:>5}")

    totals = db.get_entries()
    if not totals.empty:
        print(f"  entries total  {float(totals['base_amount'].sum()):,.2f} "
              f"{db.base_currency()}")

    if dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    # now point db.py at the destination and load
    blob = db.backup_bytes()
    os.environ["BOOKS_DB_URL"] = target_url
    db.reset_engine()
    print(f"\nTarget  {target_url.split('@')[-1]}")
    print(f"  dialect        {db.dialect()}")
    if db.dialect() != "postgresql":
        print("  ! that URL is not Postgres — refusing to run")
        return 1

    db.init_db()
    existing = len(db.get_entries())
    if existing:
        print(f"  ! the target already holds {existing} entries; "
              "restore will replace them")

    restored = db.restore_bytes(blob)
    print(f"  restored       {restored}")

    moved = db.get_entries()
    print("\nVerifying the target:")
    print(f"  entries        {len(moved)}")
    print(f"  settlements    {len(db.get_settlements())}")
    print(f"  partners       {len(db.get_partners(include_inactive=True))}")
    if not moved.empty:
        print(f"  entries total  {float(moved['base_amount'].sum()):,.2f} "
              f"{db.base_currency()}")

    import settlement as stl
    import statements as fin

    positions = stl.compute_positions(
        moved, db.get_splits(), db.get_partners(include_inactive=True),
        db.get_settlements())
    trial = fin.trial_balance_totals(fin.trial_balance(moved, positions))
    sheet = fin.balance_sheet(positions)["totals"]
    print(f"  trial balance  {trial['debit']:,.2f} vs {trial['credit']:,.2f} "
          f"(difference {trial['difference']})")
    print(f"  balance sheet  {sheet['assets']:,.2f} vs {sheet['funding']:,.2f} "
          f"(difference {sheet['difference']})")

    ok = (len(moved) == counts.get("entries", 0)
          and trial["difference"] == 0.0 and sheet["difference"] == 0.0)
    print("\n" + ("MIGRATION OK" if ok else "MIGRATION DID NOT VERIFY"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
