"""Prove the request-scoped read cache cannot serve stale data.

The cache exists because a hosted database made every rerun cost hundreds of
round trips. The risk it introduces is showing a figure that the books no
longer hold, so each check here writes and then reads back through the cache.

Run:  python test_cache.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP_DB = Path(tempfile.gettempdir()) / "books_cache_test.db"
if TMP_DB.exists():
    TMP_DB.unlink()
os.environ["BOOKS_DB"] = str(TMP_DB)

import db  # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILED.append(label)


def count_statements(fn):
    """How many statements fn() costs, so we can prove the memo is working."""
    seen = {"n": 0}
    real = db.Connection.execute
    real_read = db.Connection.read_sql

    def counting(self, sql, params=()):
        seen["n"] += 1
        return real(self, sql, params)

    def counting_read(self, sql, params=()):
        seen["n"] += 1
        return real_read(self, sql, params)

    db.Connection.execute = counting
    db.Connection.read_sql = counting_read
    try:
        fn()
    finally:
        db.Connection.execute = real
        db.Connection.read_sql = real_read
    return seen["n"]


db.init_db()

print("1) The memo is off by default, so scripts and tests are unaffected")
db.enable_read_cache(False)
cold = count_statements(lambda: [db.get_setting("currency") for _ in range(5)])
check("five reads cost five statements with the memo off", cold, 5)

print("\n2) With it on, repeat reads cost nothing")
db.enable_read_cache(True)
warm = count_statements(lambda: [db.get_setting("currency") for _ in range(5)])
check("five reads cost one statement with the memo on", warm, 1)

print("\n3) A write empties the memo - settings")
db.enable_read_cache(True)
db.set_setting("cache_probe", "first")
check("the memo starts from the stored value", db.get_setting("cache_probe"), "first")
db.set_setting("cache_probe", "second")
check("a second write is seen too", db.get_setting("cache_probe"), "second")

print("\n4) A write empties the memo - categories")
db.enable_read_cache(True)
db.get_categories("expense")
db.add_category("Cache Probe", "expense")
check("a new category is visible immediately",
      "Cache Probe" in db.get_categories("expense"), True)
db.delete_category("Cache Probe", "expense")
check("and its removal too",
      "Cache Probe" in db.get_categories("expense"), False)

print("\n5) A write empties the memo - partners")
db.enable_read_cache(True)
db.get_partners()
db.add_partner("Cache Probe Partner", 0.0, 0.0)
names = set(db.get_partners()["name"])
check("a new partner is visible immediately", "Cache Probe Partner" in names, True)
pid = int(db.get_partners().set_index("name").loc["Cache Probe Partner", "id"])
db.delete_partner(pid)
check("and its removal too",
      "Cache Probe Partner" not in set(db.get_partners()["name"]), True)

print("\n6) A write empties the memo - currencies and rates")
db.enable_read_cache(True)
db.rate_map()
db.upsert_currency("ZWL", "Z$", 123.0)
check("a new rate is visible immediately", db.rate_map().get("ZWL"), 123.0)
db.upsert_currency("ZWL", "Z$", 456.0)
check("an amended rate too", db.rate_map().get("ZWL"), 456.0)
db.delete_currency("ZWL")
check("and a deletion too", "ZWL" in db.rate_map(), False)

print("\n7) Entries added through the normal path are visible at once")
db.enable_read_cache(True)
start = len(db.get_entries())
partners = db.get_partners()
first = int(partners.iloc[0]["id"])
db.save_entry(
    entry_date="2026-01-01",
    kind="expense",
    amount=10.0,
    category=db.get_categories("expense")[0],
    description="Cache probe entry",
    cash_partner_id=first,
    share_mode="ratio",
    currency=db.base_currency(),
)
check("the new entry shows up", len(db.get_entries()), start + 1)

print("\n8) A cached DataFrame cannot be mutated by one caller for another")
db.enable_read_cache(True)
one = db.get_partners()
one["name"] = "tampered"
two = db.get_partners()
check("the second caller gets clean data", "tampered" in set(two["name"]), False)

print("\n9) Switching database forgets both the schema flag and the memo")
db.enable_read_cache(True)
db.get_setting("currency")
db.reset_engine()
check("the memo was emptied", len(db._read_cache), 0)
check("the schema flag was cleared", len(db._schema_ready), 0)

db.enable_read_cache(False)
print()
if FAILED:
    print(f"FAILURES: {FAILED}")
    raise SystemExit(1)
print("ALL CACHE CHECKS PASSED")
