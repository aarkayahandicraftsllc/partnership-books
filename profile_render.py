"""Count the database work one page render does.

The app is slow on Streamlit Cloud, where the database is a network away. What
matters there is the number of ROUND TRIPS per rerun, not CPU. This wraps the
db layer, renders the app once through Streamlit's own harness, and reports
how many transactions and statements that took, plus the slowest statements.

    python -I _profile_render.py
"""

import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP_DB = Path(tempfile.gettempdir()) / "books_profile.db"
if TMP_DB.exists():
    TMP_DB.unlink()
os.environ["BOOKS_DB"] = str(TMP_DB)

# python -I does not prepend the script's directory, so make the project
# modules importable explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402

STATS = {"transactions": 0, "statements": 0, "read_sql": 0}
TIMES = []
BY_SQL = Counter()

_real_connect = db.connect
_real_execute = db.Connection.execute
_real_read_sql = db.Connection.read_sql


def _norm(sql):
    return " ".join(str(sql).split())[:110]


def counting_execute(self, sql, params=()):
    STATS["statements"] += 1
    start = time.perf_counter()
    try:
        return _real_execute(self, sql, params)
    finally:
        elapsed = time.perf_counter() - start
        TIMES.append((elapsed, _norm(sql)))
        BY_SQL[_norm(sql)] += 1


def counting_read_sql(self, sql, params=()):
    STATS["read_sql"] += 1
    STATS["statements"] += 1
    start = time.perf_counter()
    try:
        return _real_read_sql(self, sql, params)
    finally:
        elapsed = time.perf_counter() - start
        TIMES.append((elapsed, "read_sql: " + _norm(sql)))
        BY_SQL["read_sql: " + _norm(sql)] += 1


class counting_connect:
    def __init__(self):
        STATS["transactions"] += 1
        self._cm = _real_connect()

    def __enter__(self):
        return self._cm.__enter__()

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


db.Connection.execute = counting_execute
db.Connection.read_sql = counting_read_sql
db.connect = counting_connect

# seed the schema before measuring, so setup is not counted as render work
with _real_connect() as con:
    pass
db.init_db() if hasattr(db, "init_db") else None
STATS.update(transactions=0, statements=0, read_sql=0)
TIMES.clear()
BY_SQL.clear()

from streamlit.testing.v1 import AppTest  # noqa: E402

APP = Path(__file__).with_name("app.py")
start = time.perf_counter()
at = AppTest.from_file(str(APP), default_timeout=120).run()
wall = time.perf_counter() - start
if at.exception:
    print("app raised:", at.exception)

print(f"one render took {wall:.2f}s against LOCAL SQLITE")
print(f"  transactions opened : {STATS['transactions']}")
print(f"  statements executed : {STATS['statements']}")
print(f"    of which read_sql : {STATS['read_sql']}")

print("\nmost repeated statements:")
for sql, n in BY_SQL.most_common(12):
    print(f"  {n:>4}x  {sql}")

print("\nslowest statements locally:")
for elapsed, sql in sorted(TIMES, reverse=True)[:8]:
    print(f"  {elapsed*1000:>7.1f}ms  {sql}")

# What those counts cost once the database is remote.
print("\nprojected wall time per rerun on a remote database,")
print("assuming each transaction costs 1 round trip to open plus 1 to commit,")
print("and each statement 1 round trip:")
trips = STATS["transactions"] * 2 + STATS["statements"]
for rtt in (10, 30, 80):
    print(f"  at {rtt:>3}ms round trip: {trips} trips = {trips * rtt / 1000:.1f}s")
print(f"  (pool_pre_ping adds one SELECT 1 per checkout: "
      f"+{STATS['transactions']} trips)")
