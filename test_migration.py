"""Checks that a pre-multi-currency database upgrades cleanly.

Builds a database with the original single-symbol schema, then runs init_db()
over it and confirms nothing is lost and everything lands in the right currency.

Run:  python test_migration.py
"""

import os
import sqlite3
import tempfile
from pathlib import Path

TMP_DB = Path(tempfile.gettempdir()) / "books_migration_test.db"
if TMP_DB.exists():
    TMP_DB.unlink()
os.environ["BOOKS_DB"] = str(TMP_DB)

# ---- build the OLD schema by hand ----------------------------------------- #
OLD = """
CREATE TABLE partners (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
    share_pct REAL NOT NULL DEFAULT 0, is_firm INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('expense','income')), UNIQUE (name, kind)
);
CREATE TABLE entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entry_date TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('expense','income')),
    amount REAL NOT NULL CHECK (amount > 0), category TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    cash_partner_id INTEGER NOT NULL REFERENCES partners(id),
    share_mode TEXT NOT NULL CHECK (share_mode IN ('ratio','custom','none')),
    created_at TEXT NOT NULL
);
CREATE TABLE entry_splits (
    entry_id INTEGER NOT NULL, partner_id INTEGER NOT NULL, pct REAL NOT NULL,
    PRIMARY KEY (entry_id, partner_id)
);
CREATE TABLE settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT, paid_on TEXT NOT NULL,
    from_id INTEGER NOT NULL, to_id INTEGER NOT NULL,
    amount REAL NOT NULL CHECK (amount > 0), note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

INSERT INTO partners (id, name, share_pct, is_firm, sort_order) VALUES
    (1, 'Partner A', 60, 0, 1), (2, 'Partner B', 40, 0, 2),
    (3, 'Firm (Business Account)', 0, 1, 99);
INSERT INTO settings (key, value) VALUES ('currency', '₹'),
    ('business_name', 'Old Firm'), ('fy_start_month', '4');
INSERT INTO entries (id, entry_date, kind, amount, category, description,
                     cash_partner_id, share_mode, created_at) VALUES
    (1, '2026-04-10', 'expense', 10000, 'Rent', 'April rent', 1, 'ratio', '2026-04-10'),
    (2, '2026-04-15', 'income', 50000, 'Sales', 'Invoice 001', 3, 'ratio', '2026-04-15');
INSERT INTO entry_splits (entry_id, partner_id, pct) VALUES
    (1, 1, 60), (1, 2, 40), (2, 1, 60), (2, 2, 40);
INSERT INTO settlements (id, paid_on, from_id, to_id, amount, note, created_at)
    VALUES (1, '2026-04-30', 3, 1, 1000, 'advance', '2026-04-30');
"""

conn = sqlite3.connect(TMP_DB)
conn.executescript(OLD)
conn.commit()
conn.close()
print(f"built an old-schema database at {TMP_DB}")

import db  # noqa: E402
import settlement as stl  # noqa: E402

FAILURES = []


def check(label, got, want, tol=0.005):
    ok = abs(got - want) <= tol if isinstance(want, float) else got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(label)


print("\nmigrating...")
db.init_db()

print("\n1) The old rupee symbol becomes an INR base currency")
check("base currency", db.base_currency(), "INR")
check("INR rate is 1", db.rate_map()["INR"], 1.0)
check("USD was seeded too", "USD" in db.rate_map(), True)

print("\n2) Existing records keep their data and gain a currency")
entries = db.get_entries()
check("both entries survived", len(entries), 2)
check("all tagged INR", sorted(set(entries["currency"])), ["INR"])
check("amounts untouched", sorted(entries["amount"].tolist()), [10000.0, 50000.0])
check("base_amount equals amount",
      float((entries["amount"] - entries["base_amount"]).abs().sum()), 0.0)
settlements = db.get_settlements()
check("settlement survived", len(settlements), 1)
check("settlement tagged INR", str(settlements.iloc[0]["currency"]), "INR")
check("settlement base_amount", float(settlements.iloc[0]["base_amount"]), 1000.0)

print("\n3) The single share_pct becomes separate expense and profit percentages")
people = db.get_partners(include_inactive=True).set_index("name")
check("expense_pct column exists", "expense_pct" in people.columns, True)
check("profit_pct column exists", "profit_pct" in people.columns, True)
check("A's expense % from share_pct", float(people.loc["Partner A", "expense_pct"]), 60.0)
check("A's profit % from share_pct", float(people.loc["Partner A", "profit_pct"]), 60.0)
check("B's expense % from share_pct", float(people.loc["Partner B", "expense_pct"]), 40.0)
check("B's profit % from share_pct", float(people.loc["Partner B", "profit_pct"]), 40.0)
check("firm stays at zero",
      float(people.loc["Firm (Business Account)", "expense_pct"]), 0.0)
check("legacy column dropped", "share_pct" in people.columns, False)
check("both ratio totals are 100",
      db.ratio_totals(), {"expense_pct": 100.0, "profit_pct": 100.0})

print("\n4) Settings and splits are intact")
check("business name kept", db.get_setting("business_name"), "Old Firm")
check("fy start kept", db.get_setting("fy_start_month"), "4")
check("splits kept", len(db.get_splits()), 4)
check("categories seeded", len(db.get_categories("expense")) > 0, True)

print("\n5) Settlement maths still works on the migrated data")
pos = stl.compute_positions(
    db.get_entries(), db.get_splits(), db.get_partners(include_inactive=True),
    db.get_settlements(),
)
by_name = {r.partner: r for r in pos.itertuples()}
check("A net", float(by_name["Partner A"].net), 10_000 - 6_000 + 30_000 - 1_000)
check("B net", float(by_name["Partner B"].net), -4_000 + 20_000)
check("firm net", float(by_name["Firm (Business Account)"].net), -50_000 + 1_000)
check("zero-sum", float(pos["net"].sum()), 0.0)

print("\n6) Running init_db again is a no-op")
db.init_db()
check("still 2 entries", len(db.get_entries()), 2)
check("still INR base", db.base_currency(), "INR")
check("rates unchanged", db.rate_map()["USD"], 83.0)

print("\n7) The entries table is rebuilt to accept negative expenses")
with db.connect() as c:
    ddl = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='entries'"
    ).fetchone()["sql"]
    check("old 'amount > 0' CHECK is gone", "CHECK (amount > 0)" in ddl, False)
    check("new CHECK is in place", db.NEGATIVE_MARKER in ddl, True)
    check("date index survived the rebuild",
          c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
                    " AND name='idx_entries_date'").fetchone()[0], 1)
    check("no broken references",
          len(c.execute("PRAGMA foreign_key_check").fetchall()), 0)

check("rows survived the rebuild", len(db.get_entries()), 2)
check("ids survived the rebuild",
      sorted(db.get_entries()["id"].astype(int).tolist()), [1, 2])
check("splits still line up",
      set(db.get_splits()["entry_id"].astype(int)),
      set(db.get_entries()["id"].astype(int)))

partner_a = int(db.get_partners().iloc[0]["id"])
new_id = db.save_entry(entry_date="2026-05-01", kind="expense", amount=-1_500.0,
                       category="Rent", description="rent refund",
                       cash_partner_id=partner_a, share_mode="ratio")
check("next id continues after the rebuild", new_id, 3)
check("negative expense accepted",
      float(db.get_entries().set_index("id").loc[new_id, "amount"]), -1_500.0)
pos = stl.compute_positions(
    db.get_entries(), db.get_splits(), db.get_partners(include_inactive=True),
    db.get_settlements(),
)
check("zero-sum after the refund", float(pos["net"].sum()), 0.0)
db.delete_entry(new_id)
check("back to 2 entries", len(db.get_entries()), 2)

print("\n8) A second init_db does not rebuild again")
before = db.get_setting("base_currency")
db.init_db()
check("entries intact", len(db.get_entries()), 2)
check("settings intact", db.get_setting("base_currency"), before)
check("ids intact", sorted(db.get_entries()["id"].astype(int).tolist()), [1, 2])

print("\n" + ("ALL MIGRATION CHECKS PASSED" if not FAILURES else f"FAILURES: {FAILURES}"))
raise SystemExit(1 if FAILURES else 0)
