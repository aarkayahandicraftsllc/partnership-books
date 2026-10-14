"""Checks the things a deployment depends on: the password gate and backups.

Run:  python test_deploy.py
Uses throwaway databases; your real books are never touched.
"""

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP_DB = Path(tempfile.gettempdir()) / "books_deploy_test.db"
if TMP_DB.exists():
    TMP_DB.unlink()
os.environ["BOOKS_DB"] = str(TMP_DB)
os.environ.pop("BOOKS_PASSWORD", None)

import db  # noqa: E402

APP = Path(__file__).with_name("app.py")
FAILURES = []


def check(label, got, want, tol=0.005):
    ok = abs(got - want) <= tol if isinstance(want, float) else got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(label)


def fresh_app():
    """A new AppTest, picking up the current environment."""
    from streamlit.testing.v1 import AppTest
    return AppTest.from_file(str(APP), default_timeout=120).run()


print("\n1) Requirements cover every third-party import")
needed = {"streamlit", "pandas", "openpyxl"}
listed = {
    line.split(">=")[0].split("==")[0].strip().lower()
    for line in Path(APP).with_name("requirements.txt").read_text().splitlines()
    if line.strip() and not line.startswith("#")
}
check("requirements.txt lists them all", needed - listed, set())

print("\n2) With no password set the app opens straight up (local use)")
db.init_db()
partner_a = int(db.get_partners().iloc[0]["id"])
for n, amount in enumerate([1_000.0, 2_500.0], start=1):
    db.save_entry(entry_date=date(2026, 9, n), kind="expense", amount=amount,
                  category="Rent", description=f"e{n}",
                  cash_partner_id=partner_a, share_mode="ratio")
db.add_settlement(date(2026, 9, 9), partner_a,
                  int(db.get_partners().iloc[1]["id"]), 500.0, "part")

at = fresh_app()
check("no exception", bool(at.exception), False)
check("the books are visible", len(at.tabs), 7)
check("no password prompt", any(t.label == "Password" for t in at.text_input), False)

print("\n3) With a password set, nothing renders until it is given")
os.environ["BOOKS_PASSWORD"] = "correct horse"
at = fresh_app()
check("no exception", bool(at.exception), False)
check("tabs are hidden", len(at.tabs), 0)
check("a password box is shown",
      any(t.label == "Password" for t in at.text_input), True)

at = at.text_input[0].set_value("wrong").run()
at = next(b for b in at.button if b.label == "Unlock").click().run()
check("a wrong password is refused",
      any("not right" in e.value for e in at.error), True)
check("still nothing visible", len(at.tabs), 0)

at = fresh_app()
at = at.text_input[0].set_value("correct horse").run()
at = next(b for b in at.button if b.label == "Unlock").click().run()
check("the right password opens it", bool(at.exception), False)
check("the books are now visible", len(at.tabs), 7)
check("and it stays open on the next interaction",
      len(next(s for s in at.selectbox if s.key == "led_period")
          .set_value("All time").run().tabs), 7)

print("\n4) A public URL means the gate itself must hold up")
import app as books_app  # noqa: E402

check("a long password is demanded", books_app.MIN_PASSWORD >= 16, True)

# wrong guesses must be slowed down, and must not leak via the attempt counter
os.environ["BOOKS_PASSWORD"] = "correct horse"
at = fresh_app()
at = at.text_input[0].set_value("nope").run()
import time as _time  # noqa: E402

started = _time.monotonic()
at = next(b for b in at.button if b.label == "Unlock").click().run()
elapsed = _time.monotonic() - started
check("a failed attempt is delayed", elapsed >= 0.3, True)
check("the failure was counted", at.session_state.get("pw_attempts"), 1)
check("and it stays locked", len(at.tabs), 0)

print("\n5) Setup reports the backend and whether the gate is on")
# unlock a fresh instance: the one above was deliberately left locked
unlocked = fresh_app()
unlocked = unlocked.text_input[0].set_value("correct horse").run()
unlocked = next(b for b in unlocked.button
                if b.label == "Unlock").click().run()
captions = " ".join(c.value for c in unlocked.caption)
check("it says protection is on",
      "password protection is **on**" in captions, True)
# the backend depends on how this run was launched
expected_backend = "Postgres" if not db.is_sqlite() else "SQLite"
check("it names the backend", f"Storage: {expected_backend}" in captions, True)
os.environ.pop("BOOKS_PASSWORD")
plain = " ".join(c.value for c in fresh_app().caption)
check("and warns when it is off", "no password set" in plain, True)

print("\n6) A backup is a real database holding the real counts")
raw = db.backup_bytes()
check("it is a gzip file", raw[:2], bytes((0x1F, 0x8B)))
counts = db.describe_backup(raw)
check("entry count matches", counts["entries"], len(db.get_entries()))
check("settlement count matches", counts["settlements"], len(db.get_settlements()))
check("partner count matches", counts["partners"], 3)

print("\n7) Rubbish uploads are refused, not restored")
for label, payload in [
    ("a text file", b"this is not a database"),
    ("an empty file", b""),
    ("a truncated gzip header", bytes((0x1F, 0x8B))),
    ("valid json that is not a backup", b'{"hello": "world"}'),
]:
    try:
        db.describe_backup(payload)
        check(label, "accepted", "rejected")
    except ValueError:
        check(label, "rejected", "rejected")

other = Path(tempfile.gettempdir()) / "unrelated.db"
other.unlink(missing_ok=True)
import sqlite3  # noqa: E402

conn = sqlite3.connect(other)
conn.execute("CREATE TABLE something (id INTEGER)")
conn.commit()
conn.close()
try:
    db.describe_backup(other.read_bytes())
    check("someone else's database", "accepted", "rejected")
except ValueError as exc:
    check("someone else's database", "rejected", "rejected")
    check("and the message mentions a backup",
          "backup" in str(exc).lower(), True)

partial = json.dumps({"format": 2, "tables": {"partners": []}}).encode()
try:
    db.describe_backup(partial)
    check("a backup missing entries", "accepted", "rejected")
except ValueError as exc:
    check("a backup missing entries", "rejected", "rejected")
    check("and it names what is missing", "entries" in str(exc), True)

print("\n8) Restoring brings the books back exactly")
snapshot = db.backup_bytes()
before_entries = db.get_entries()["id"].astype(int).tolist()
before_total = round(float(db.get_entries()["base_amount"].sum()), 2)

# wreck the live books, then restore
for entry_id in list(before_entries):
    db.delete_entry(entry_id)
check("books emptied", len(db.get_entries()), 0)

restored = db.restore_bytes(snapshot)
check("restore reports the counts", restored["entries"], len(before_entries))
check("entries are back", db.get_entries()["id"].astype(int).tolist(),
      before_entries)
check("amounts are intact",
      round(float(db.get_entries()["base_amount"].sum()), 2), before_total)
check("splits came back too", len(db.get_splits()), 2 * len(before_entries))
check("the settlement came back", len(db.get_settlements()), 1)

replaced = sorted(TMP_DB.parent.glob("replaced-*.books.gz"))
check("the replaced books were kept aside", len(replaced) >= 1, True)
for leftover in replaced:
    leftover.unlink()

print("\n9) The app still runs on the restored database")
at = fresh_app()
check("no exception after a restore", bool(at.exception), False)
metrics = {m.label: m.value for m in at.metric}
check("trial balance still balances",
      metrics["Total debits"], metrics["Total credits"])
check("balance sheet still balances",
      metrics["Total assets"], metrics["Capital & liabilities"])

print("\n10) Backend selection behaves")
# this section is about the selection logic, so ignore whatever backend the
# surrounding run is using (run_postgres_tests.py sets BOOKS_DB_URL)
_ambient_url = os.environ.pop("BOOKS_DB_URL", None)
check("no URL means SQLite", db.dialect(), "sqlite")
check("SQLite path is honoured", str(db.DB_PATH), str(TMP_DB))
for url, want in [
    ("postgresql://u:p@h/d", "postgresql"),
    ("postgres://u:p@h/d", "postgresql"),
    ("postgresql+psycopg://u:p@h/d", "postgresql"),
]:
    os.environ["BOOKS_DB_URL"] = url
    check(f"{url.split(':')[0]} selects Postgres", db.dialect(), want)
    check("and psycopg is the driver",
          "postgresql+psycopg://" in db.database_url(), True)
os.environ.pop("BOOKS_DB_URL")
check("back to SQLite", db.dialect(), "sqlite")
if _ambient_url:
    os.environ["BOOKS_DB_URL"] = _ambient_url

print("\n11) The deploy files say what they must")
required = Path(APP).with_name("requirements.txt").read_text(encoding="utf-8")
for package in ("streamlit", "pandas", "openpyxl", "SQLAlchemy", "psycopg"):
    check(f"requirements lists {package!r}", package in required, True)

example = Path(APP).with_name(".streamlit") / "secrets.toml.example"
check("there is a secrets example", example.exists(), True)
sample = example.read_text(encoding="utf-8")
for fragment in ("BOOKS_DB_URL", "BOOKS_PASSWORD", "postgresql://"):
    check(f"the example shows {fragment!r}", fragment in sample, True)
check("the real secrets file is not committed",
      (Path(APP).with_name(".streamlit") / "secrets.toml").exists(), False)

ignored = Path(APP).with_name(".gitignore").read_text(encoding="utf-8")
for fragment in ("data/", "*.db", ".streamlit/secrets.toml"):
    check(f".gitignore excludes {fragment!r}", fragment in ignored, True)

print("\n" + ("ALL DEPLOY CHECKS PASSED" if not FAILURES
             else f"FAILURES: {FAILURES}"))
raise SystemExit(1 if FAILURES else 0)
