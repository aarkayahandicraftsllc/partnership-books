"""Runs the test suites against a real Postgres instead of SQLite.

The app is meant to run on hosted Postgres but the suites default to SQLite so
they stay fast and need no setup. That leaves the deployed dialect as the less
tested one, which is backwards — so this script starts a throwaway Postgres,
points BOOKS_DB_URL at a fresh schema per suite, and runs them all against it.

    python run_postgres_tests.py

Set BOOKS_TEST_PG_URL to use your own database (e.g. the Neon one) instead of
the embedded server.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SUITES = ["test_books.py", "test_migration.py", "test_statements.py",
          "test_ui.py", "test_deploy.py"]
HERE = Path(__file__).resolve().parent

# test_migration builds an old-schema SQLite file by hand, so it only ever
# applies to the SQLite path
SQLITE_ONLY = {"test_migration.py"}


def postgres_url() -> tuple[str, object]:
    supplied = os.environ.get("BOOKS_TEST_PG_URL", "").strip()
    if supplied:
        return supplied, None
    import pgserver

    data_dir = Path(tempfile.gettempdir()) / "pgdata_books"
    data_dir.mkdir(exist_ok=True)
    server = pgserver.get_server(str(data_dir))
    return server.get_uri(), server


def main() -> int:
    url, server = postgres_url()
    print(f"Postgres: {url.split('@')[-1]}\n")

    failures = []
    for suite in SUITES:
        if suite in SQLITE_ONLY:
            print(f"{suite:22} SKIP  (tests the SQLite upgrade path only)")
            continue

        schema = suite.replace(".py", "").replace("test_", "t_")
        # a schema per suite, dropped first, so nothing leaks between runs
        import sqlalchemy

        scratch = sqlalchemy.create_engine(
            url.replace("postgresql://", "postgresql+psycopg://", 1), future=True)
        with scratch.connect() as setup:
            setup.execute(sqlalchemy.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            setup.commit()
        scratch.dispose()

        env = dict(os.environ)
        env["BOOKS_DB_URL"] = url
        env["PYTHONIOENCODING"] = "utf-8"
        env["BOOKS_PG_SCHEMA"] = schema
        env.pop("BOOKS_PASSWORD", None)

        done = subprocess.run([sys.executable, str(HERE / suite)], env=env,
                              cwd=HERE, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        ok = done.returncode == 0
        print(f"{suite:22} {'PASS' if ok else 'FAIL'}")
        if not ok:
            failures.append(suite)
            tail = (done.stdout or "").strip().splitlines()[-25:]
            for line in tail:
                print("   ", line)
            err = (done.stderr or "").strip().splitlines()[-10:]
            for line in err:
                print("  !", line)

    if server is not None:
        server.cleanup()

    print("\n" + ("ALL POSTGRES SUITES PASSED" if not failures
                  else f"FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
