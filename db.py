"""Storage layer for the partnership books — SQLite locally, Postgres hosted.

Pick the backend with ``BOOKS_DB_URL``:

* unset — SQLite at ``BOOKS_DB`` (default ``data/tracker.db``). This is how it
  runs on your own machine and how the tests run, with no setup at all.
* ``postgresql+psycopg://...`` — a hosted Postgres, which is what Streamlit
  Community Cloud uses since its filesystem is wiped on every restart.

One set of SQL serves both. Queries keep SQLite's ``?`` placeholders and a
small shim rewrites them to named parameters for whichever driver is in use, so
there is a single code path rather than a well-tested local one and a shaky
deployed one. Only four things genuinely differ, and each is marked
``sqlite-only`` or branches on :func:`dialect`: the ``AUTOINCREMENT`` /
``SERIAL`` spelling, ``PRAGMA``, the id-renumbering sequence reset, and the
one-off migrations that upgrade an older SQLite file.

Design notes
------------
* Every entry records ONE cash movement (``cash_partner_id`` = whose pocket the
  money actually moved through) and a *burden split* in ``entry_splits``
  (who the amount economically belongs to).
* The split is materialised at save time for all sharing modes, so changing a
  partner's profit-share percentage later never rewrites history.
* "Firm (Business Account)" is stored as a partner row with ``is_firm = 1``.
  It can move cash but never carries a share of the burden, which keeps the
  settlement balances summing to exactly zero.
* Amounts are stored in the currency they were incurred in. Conversion to the
  base currency happens at read time from the live ``currencies.rate_to_base``,
  so editing a rate in Setup re-converts the whole history at once.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd
from functools import wraps
import copy

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("BOOKS_DB", APP_DIR / "data" / "tracker.db"))


def database_url() -> str:
    """The SQLAlchemy URL for whichever backend is configured."""
    configured = os.environ.get("BOOKS_DB_URL", "").strip()
    if configured:
        # accept the plain postgres:// URLs the hosting providers hand out
        if configured.startswith("postgres://"):
            configured = configured.replace("postgres://", "postgresql://", 1)
        if configured.startswith("postgresql://"):
            configured = configured.replace(
                "postgresql://", "postgresql+psycopg://", 1)
        return configured
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{DB_PATH}"


def dialect() -> str:
    """``'sqlite'`` or ``'postgresql'``."""
    return "postgresql" if "postgresql" in database_url() else "sqlite"


def is_sqlite() -> bool:
    return dialect() == "sqlite"


def safe_target() -> str:
    """Host and database, with the credentials removed — safe to display."""
    url = database_url()
    if is_sqlite():
        return str(DB_PATH)
    tail = url.rsplit("@", 1)[-1]
    return tail.split("?")[0] or "(no host in the URL)"


def scrub(message: str) -> str:
    """Strip anything credential-shaped out of an error before showing it."""
    cleaned = re.sub(r"password=[^\s&\"']+", "password=***", str(message))
    cleaned = re.sub(r"://[^:/@\s]+:[^@\s]+@", "://***:***@", cleaned)
    return cleaned

SHARE_MODES = ("ratio", "custom", "none")

# which partner column drives a 'ratio' split, per entry kind
RATIO_COLUMN = {"expense": "expense_pct", "income": "profit_pct"}

# only expenses may be negative, for refunds / credit notes / reversals
NEGATIVE_KINDS = ("expense",)

# presence of this text in the entries DDL means the table already allows them
NEGATIVE_MARKER = "amount <> 0"

# SQLite cannot alter a CHECK constraint, so the table is rebuilt in place.
# Foreign keys go off for the swap because entry_splits points at entries(id).
ENTRIES_REBUILD = """
PRAGMA foreign_keys = OFF;
BEGIN;
CREATE TABLE entries_rebuilt (
    id              {PK},
    entry_date      TEXT    NOT NULL,
    kind            TEXT    NOT NULL CHECK (kind IN ('expense', 'income')),
    amount          {REAL}    NOT NULL CHECK (amount <> 0 AND (kind = 'expense' OR amount > 0)),
    currency        TEXT    NOT NULL DEFAULT 'INR',
    category        TEXT    NOT NULL DEFAULT '',
    description     TEXT    NOT NULL DEFAULT '',
    cash_partner_id INTEGER NOT NULL REFERENCES partners(id),
    share_mode      TEXT    NOT NULL CHECK (share_mode IN ('ratio', 'custom', 'none')),
    created_at      TEXT    NOT NULL
);
INSERT INTO entries_rebuilt (id, entry_date, kind, amount, currency, category,
                             description, cash_partner_id, share_mode, created_at)
    SELECT id, entry_date, kind, amount, currency, category, description,
           cash_partner_id, share_mode, created_at FROM entries;
DROP TABLE entries;
ALTER TABLE entries_rebuilt RENAME TO entries;
COMMIT;
PRAGMA foreign_keys = ON;
CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(entry_date);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS partners (
    id          {PK},
    name        TEXT    NOT NULL UNIQUE,
    expense_pct {REAL}    NOT NULL DEFAULT 0,
    profit_pct  {REAL}    NOT NULL DEFAULT 0,
    is_firm     INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS categories (
    id   {PK},
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('expense', 'income')),
    UNIQUE (name, kind)
);

CREATE TABLE IF NOT EXISTS currencies (
    code         TEXT PRIMARY KEY,
    symbol       TEXT NOT NULL,
    rate_to_base {REAL} NOT NULL DEFAULT 1 CHECK (rate_to_base > 0),
    sort_order   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS entries (
    id              {PK},
    entry_date      TEXT    NOT NULL,
    kind            TEXT    NOT NULL CHECK (kind IN ('expense', 'income')),
    amount          {REAL}    NOT NULL CHECK (amount <> 0 AND (kind = 'expense' OR amount > 0)),
    currency        TEXT    NOT NULL DEFAULT 'INR',
    category        TEXT    NOT NULL DEFAULT '',
    description     TEXT    NOT NULL DEFAULT '',
    cash_partner_id INTEGER NOT NULL REFERENCES partners(id),
    share_mode      TEXT    NOT NULL CHECK (share_mode IN ('ratio', 'custom', 'none')),
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS entry_splits (
    entry_id   INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    partner_id INTEGER NOT NULL REFERENCES partners(id),
    pct        {REAL} NOT NULL,
    PRIMARY KEY (entry_id, partner_id)
);

CREATE TABLE IF NOT EXISTS settlements (
    id         {PK},
    paid_on    TEXT    NOT NULL,
    from_id    INTEGER NOT NULL REFERENCES partners(id),
    to_id      INTEGER NOT NULL REFERENCES partners(id),
    amount     {REAL}    NOT NULL CHECK (amount > 0),
    currency   TEXT    NOT NULL DEFAULT 'INR',
    note       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(entry_date);
CREATE INDEX IF NOT EXISTS idx_splits_partner ON entry_splits(partner_id);
"""

DEFAULT_SETTINGS = {
    "base_currency": "INR",
    "business_name": "My Partnership",
    "fy_start_month": "4",
}

# code -> (symbol, rate to 1 unit of the base currency, display order)
DEFAULT_CURRENCIES = [
    ("INR", "₹", 1.0, 1),
    ("USD", "$", 83.0, 2),
]

# used when migrating an older single-symbol database
SYMBOL_TO_CODE = {"₹": "INR", "$": "USD", "€": "EUR", "£": "GBP",
                  "A$": "AUD", "C$": "CAD"}
KNOWN_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£",
                 "AUD": "A$", "CAD": "C$", "AED": "AED", "SGD": "S$",
                 "JPY": "¥", "CHF": "CHF"}

DEFAULT_EXPENSE_CATEGORIES = [
    "Rent", "Salaries & Wages", "Travel", "Utilities", "Office Supplies",
    "Professional Fees", "Marketing", "Bank Charges", "Repairs", "Taxes",
    "Miscellaneous",
]
DEFAULT_INCOME_CATEGORIES = [
    "Sales", "Service Income", "Commission", "Interest", "Other Income",
]


# --------------------------------------------------------------------------- #
# connection helpers
# --------------------------------------------------------------------------- #
DDL_TYPES = {
    "sqlite": {"PK": "INTEGER PRIMARY KEY AUTOINCREMENT", "REAL": "REAL"},
    "postgresql": {"PK": "SERIAL PRIMARY KEY", "REAL": "DOUBLE PRECISION"},
}

_QUESTION_MARK = re.compile(r"\?")
_engines: dict[str, object] = {}


def engine():
    """One pooled engine per URL.

    Pooling matters once the database is across a network: without it every
    query would pay a fresh TCP and TLS handshake, and the app issues dozens
    per rerun. ``pool_pre_ping`` quietly replaces connections that a
    scale-to-zero Postgres has dropped underneath us.
    """
    url = database_url()
    schema = os.environ.get("BOOKS_PG_SCHEMA", "").strip()
    cache_key = f"{url}#{schema}"
    if cache_key not in _engines:
        if url.startswith("sqlite"):
            created = create_engine(url, future=True)
        else:
            connect_args = {"connect_timeout": 30}
            if schema:
                # used by the test runner to keep suites from colliding
                safe = schema.replace('"', "")
                with create_engine(url, future=True).connect() as setup:
                    setup.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{safe}"'))
                    setup.commit()
                connect_args["options"] = f"-csearch_path={safe}"
            created = create_engine(
                url, future=True, pool_size=3, max_overflow=2,
                pool_recycle=280, pool_pre_ping=True,
                connect_args=connect_args,
            )
        _engines[cache_key] = created
    return _engines[cache_key]


def reset_engine() -> None:
    """Drop pooled connections — used after switching or restoring a database.

    Also forgets that the schema was initialised and empties the read cache:
    the next call is pointing at a different database.
    """
    _schema_ready.clear()
    clear_read_cache()
    for existing in _engines.values():
        try:
            existing.dispose()
        except Exception:
            pass
    _engines.clear()


class Row(dict):
    """A result row readable by column name or by position, like sqlite3.Row."""

    def __init__(self, mapping):
        super().__init__(mapping)
        self._order = list(mapping.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._order[key]
        return super().__getitem__(key)


class Cursor:
    def __init__(self, result):
        self._result = result

    def _wrap(self, row):
        return None if row is None else Row(row._mapping)

    def fetchone(self):
        return self._wrap(self._result.fetchone())

    def fetchall(self):
        return [Row(row._mapping) for row in self._result.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    """Thin wrapper letting the rest of this module keep writing `?` SQL."""

    def __init__(self, raw):
        self.raw = raw
        self.wrote = False

    @staticmethod
    def _bind(sql: str, params):
        """Rewrite `?` placeholders into the named form SQLAlchemy wants."""
        if not params:
            return text(sql), {}
        names: list[str] = []

        def swap(_match):
            names.append(f"p{len(names)}")
            return f":{names[-1]}"

        return text(_QUESTION_MARK.sub(swap, sql)), dict(zip(names, params))

    def execute(self, sql: str, params=()):
        if _is_write(sql):
            self.wrote = True
        statement, bound = self._bind(sql, params)
        return Cursor(self.raw.execute(statement, bound))

    def executemany(self, sql: str, rows) -> None:
        if _is_write(sql):
            self.wrote = True
        rows = list(rows)
        if not rows:
            return
        statement, _ = self._bind(sql, rows[0])
        self.raw.execute(
            statement,
            [dict(zip((f"p{i}" for i in range(len(row))), row)) for row in rows],
        )

    def executescript(self, script: str) -> None:
        self.wrote = True
        for statement in _split_statements(script):
            self.raw.execute(text(statement))

    def read_sql(self, sql: str, params=()) -> pd.DataFrame:
        statement, bound = self._bind(sql, params)
        return pd.read_sql_query(statement, self.raw, params=bound or None)


def _split_statements(script: str) -> list[str]:
    """Break a DDL script into statements; drivers take one at a time."""
    return [chunk.strip() for chunk in script.split(";") if chunk.strip()]


# --------------------------------------------------------------------------- #
# request-scoped read cache
#
# Streamlit re-executes the whole script on every interaction, and st.tabs
# renders every tab body whether or not it is the visible one. The app was
# therefore asking for the same reference rows dozens of times per rerun - 24
# reads of a single setting, 21 of the currency table - and on a hosted
# Postgres each one is a network round trip.
#
# These reads are memoised for the life of a rerun. Correctness comes from
# invalidation being automatic rather than remembered: _mark_write below
# notices any INSERT/UPDATE/DELETE/DDL and the commit that follows empties the
# cache, so a cached value cannot survive a change to the data it came from.
# --------------------------------------------------------------------------- #
_read_cache: dict = {}
_cache_enabled = False


def enable_read_cache(enabled: bool = True) -> None:
    """Turn the memo on (the app does this once at the top of a rerun)."""
    global _cache_enabled
    _cache_enabled = enabled
    _read_cache.clear()


def clear_read_cache() -> None:
    _read_cache.clear()


def cached_read(func):
    """Memoise a read for the rest of this rerun, keyed on its arguments."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not _cache_enabled:
            return func(*args, **kwargs)
        try:
            key = (func.__name__, args, tuple(sorted(kwargs.items())))
            hash(key)
        except TypeError:
            # an unhashable argument (a DataFrame, a list) - just do the read
            return func(*args, **kwargs)
        if key not in _read_cache:
            _read_cache[key] = func(*args, **kwargs)
        value = _read_cache[key]
        # Hand back a copy of a frame: callers mutate what they are given, and
        # a shared cached frame would carry those edits to the next caller.
        if isinstance(value, pd.DataFrame):
            return value.copy()
        if isinstance(value, (dict, list)) and not isinstance(value, Row):
            return copy.copy(value)
        return value

    wrapper.__wrapped__ = func
    return wrapper


_WRITE_VERBS = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP",
                "ALTER", "TRUNCATE")


def _is_write(sql: str) -> bool:
    return str(sql).lstrip().upper().startswith(_WRITE_VERBS)


@contextmanager
def connect():
    """A transaction. Commits on success, rolls back on any exception."""
    with engine().connect() as raw:
        if is_sqlite():
            raw.execute(text("PRAGMA foreign_keys = ON"))
        wrapper = Connection(raw)
        try:
            yield wrapper
            raw.commit()
            if wrapper.wrote:
                # the data moved, so nothing memoised about it can be trusted
                clear_read_cache()
        except Exception:
            raw.rollback()
            raise


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _migrate(conn: Connection) -> None:
    """Bring a database created by an earlier version up to the current schema."""
    def columns(table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    # the single `currency` symbol setting became `base_currency` + rate table
    legacy = conn.execute(
        "SELECT value FROM settings WHERE key = 'currency'"
    ).fetchone()
    base = conn.execute(
        "SELECT value FROM settings WHERE key = 'base_currency'"
    ).fetchone()
    if base is None:
        code = SYMBOL_TO_CODE.get(legacy["value"], "INR") if legacy else "INR"
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('base_currency', ?)", (code,)
        )
        base_code = code
    else:
        base_code = base["value"]

    # a single `share_pct` became separate expense and profit percentages
    partner_cols = columns("partners")
    if "share_pct" in partner_cols and "expense_pct" not in partner_cols:
        conn.execute(
            "ALTER TABLE partners ADD COLUMN expense_pct REAL NOT NULL DEFAULT 0"
        )
        conn.execute(
            "ALTER TABLE partners ADD COLUMN profit_pct REAL NOT NULL DEFAULT 0"
        )
        conn.execute(
            "UPDATE partners SET expense_pct = share_pct, profit_pct = share_pct"
        )
        try:
            conn.execute("ALTER TABLE partners DROP COLUMN share_pct")
        except sqlite3.OperationalError:
            pass  # older SQLite cannot drop columns; the stale one is harmless

    for table in ("entries", "settlements"):
        if "currency" not in columns(table):
            # SQLite only accepts a literal in ADD COLUMN ... DEFAULT, so add the
            # column empty and backfill it with the base currency.
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN currency TEXT NOT NULL DEFAULT ''"
            )
            conn.execute(f"UPDATE {table} SET currency = ?", (base_code,))


def _allow_negative_expenses() -> None:
    """Rebuild `entries` so negative expense amounts pass the CHECK constraint.

    Runs on its own connection, after everything else is committed, because
    `PRAGMA foreign_keys` is silently ignored inside an open transaction.
    """
    if not is_sqlite():
        return  # Postgres gets the current CHECK when the table is created
    with connect() as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'entries'"
        ).fetchone()
        if row is None or NEGATIVE_MARKER in (row["sql"] or ""):
            return
        conn.executescript(ENTRIES_REBUILD.format(**DDL_TYPES['sqlite']))
        highest = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM entries"
        ).fetchone()[0]
        _reset_sequence(conn, "entries", int(highest))
        orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
        if orphans:
            raise RuntimeError(
                f"entries rebuild left {len(orphans)} broken reference(s); "
                "restore your backup and report this"
            )


_schema_ready: set = set()


def init_db(force: bool = False) -> None:
    """Create or migrate the schema.

    Cheap on SQLite, but on a hosted Postgres it was costing a dozen round
    trips on every rerun to re-check a schema that cannot have changed. Once a
    URL has been initialised in this process we skip it; reset_engine() forgets
    that, so switching or restoring a database still re-runs it.
    """
    key = f"{database_url()}#{os.environ.get('BOOKS_PG_SCHEMA', '').strip()}"
    if not force and key in _schema_ready:
        return
    _init_db_impl()
    _schema_ready.add(key)


def _init_db_impl() -> None:
    """Create the schema and seed first-run defaults. Safe to call repeatedly."""
    with connect() as conn:
        conn.executescript(SCHEMA.format(**DDL_TYPES[dialect()]))
        if is_sqlite():
            # these only exist to upgrade an older local file; a Postgres
            # database is always created at the current schema
            _migrate(conn)

        if conn.execute("SELECT COUNT(*) FROM partners").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO partners (name, expense_pct, profit_pct, is_firm,"
                " sort_order) VALUES (?, ?, ?, ?, ?)",
                [
                    ("Partner A", 50.0, 50.0, 0, 1),
                    ("Partner B", 50.0, 50.0, 0, 2),
                    ("Firm (Business Account)", 0.0, 0.0, 1, 99),
                ],
            )

        if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO categories (name, kind) VALUES (?, ?)",
                [(c, "expense") for c in DEFAULT_EXPENSE_CATEGORIES]
                + [(c, "income") for c in DEFAULT_INCOME_CATEGORIES],
            )

        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT DO NOTHING",
                (key, value),
            )

        if conn.execute("SELECT COUNT(*) FROM currencies").fetchone()[0] == 0:
            base_code = conn.execute(
                "SELECT value FROM settings WHERE key = 'base_currency'"
            ).fetchone()["value"]
            seeds = list(DEFAULT_CURRENCIES)
            if base_code not in {c for c, *_ in seeds}:
                seeds.insert(0, (base_code, KNOWN_SYMBOLS.get(base_code, base_code),
                                 1.0, 0))
            conn.executemany(
                "INSERT INTO currencies (code, symbol, rate_to_base, sort_order)"
                " VALUES (?, ?, ?, ?)",
                [(c, s, 1.0 if c == base_code else r, o) for c, s, r, o in seeds],
            )

    # needs its own connection with nothing in flight - see the docstring
    _allow_negative_expenses()


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
@cached_read
def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# --------------------------------------------------------------------------- #
# currencies
# --------------------------------------------------------------------------- #
@cached_read
def base_currency() -> str:
    return get_setting("base_currency", "INR")


@cached_read
def get_currencies() -> pd.DataFrame:
    with connect() as conn:
        return conn.read_sql(
            "SELECT * FROM currencies ORDER BY sort_order, code"
        )


@cached_read
def rate_map() -> dict[str, float]:
    """{currency code: how many base-currency units one unit is worth}."""
    cur = get_currencies()
    rates = dict(zip(cur["code"], cur["rate_to_base"].astype(float)))
    rates[base_currency()] = 1.0
    return rates


@cached_read
def symbol_map() -> dict[str, str]:
    cur = get_currencies()
    return dict(zip(cur["code"], cur["symbol"]))


@cached_read
def default_currency(kind: str) -> str:
    """Which currency the entry form should pre-select for this kind of entry.

    Income defaults to USD when it exists, since that is where the foreign
    receipts land; expenses default to the base currency. Both are overridable
    in Setup.
    """
    rates = rate_map()
    fallback = "USD" if kind == "income" and "USD" in rates else base_currency()
    code = get_setting(f"default_currency_{kind}", fallback)
    return code if code in rates else fallback


def set_default_currency(kind: str, code: str) -> None:
    if kind not in RATIO_COLUMN:
        raise ValueError(f"Unknown entry kind {kind!r}")
    code = code.strip().upper()
    if code not in rate_map():
        raise ValueError(f"Unknown currency {code!r} — add it first.")
    set_setting(f"default_currency_{kind}", code)


def upsert_currency(code: str, symbol: str, rate_to_base: float) -> None:
    code = code.strip().upper()
    if not code:
        raise ValueError("Currency code cannot be blank.")
    rate = 1.0 if code == base_currency() else float(rate_to_base)
    if rate <= 0:
        raise ValueError("Exchange rate must be greater than zero.")
    with connect() as conn:
        nxt = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM currencies"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO currencies (code, symbol, rate_to_base, sort_order)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(code) DO UPDATE SET symbol = excluded.symbol,"
            " rate_to_base = excluded.rate_to_base",
            (code, symbol.strip() or code, rate, nxt),
        )


def currency_usage(code: str) -> int:
    with connect() as conn:
        return (
            conn.execute(
                "SELECT COUNT(*) FROM entries WHERE currency = ?", (code,)
            ).fetchone()[0]
            + conn.execute(
                "SELECT COUNT(*) FROM settlements WHERE currency = ?", (code,)
            ).fetchone()[0]
        )


def delete_currency(code: str) -> None:
    if code == base_currency():
        raise ValueError("The base currency cannot be removed.")
    if currency_usage(code):
        raise ValueError(f"{code} is used by existing records.")
    with connect() as conn:
        conn.execute("DELETE FROM currencies WHERE code = ?", (code,))


def set_base_currency(code: str) -> None:
    """Switch the base currency, re-expressing every stored rate against it.

    A rate of 83 for USD against an INR base becomes 1/83 for INR once USD is
    the base, so the relative value of every currency is preserved.
    """
    code = code.strip().upper()
    old_rates = rate_map()
    if code not in old_rates:
        raise ValueError(f"{code} is not in the currency list — add it first.")
    divisor = float(old_rates[code])
    if divisor <= 0:
        raise ValueError(f"{code} has no usable exchange rate.")
    with connect() as conn:
        for existing, rate in old_rates.items():
            conn.execute(
                "UPDATE currencies SET rate_to_base = ? WHERE code = ?",
                (1.0 if existing == code else float(rate) / divisor, existing),
            )
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('base_currency', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (code,),
        )


# --------------------------------------------------------------------------- #
# partners
# --------------------------------------------------------------------------- #
@cached_read
def get_partners(include_inactive: bool = False) -> pd.DataFrame:
    sql = "SELECT * FROM partners"
    if not include_inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY sort_order, name"
    with connect() as conn:
        return conn.read_sql(sql)


def partner_names(df: pd.DataFrame | None = None) -> dict[int, str]:
    df = get_partners(include_inactive=True) if df is None else df
    return dict(zip(df["id"], df["name"]))


def add_partner(name: str, expense_pct: float = 0.0,
                profit_pct: float | None = None) -> int:
    """Add a partner. ``profit_pct`` defaults to the same value as expense_pct."""
    with connect() as conn:
        nxt = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM partners WHERE is_firm = 0"
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO partners (name, expense_pct, profit_pct, is_firm, sort_order)"
            " VALUES (?, ?, ?, 0, ?) RETURNING id",
            (name.strip(), float(expense_pct),
             float(expense_pct if profit_pct is None else profit_pct), nxt),
        )
        return int(cur.fetchone()[0])


def update_partner(partner_id: int, name: str, expense_pct: float,
                   profit_pct: float, active: bool) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE partners SET name = ?, expense_pct = ?, profit_pct = ?,"
            " active = ? WHERE id = ?",
            (name.strip(), float(expense_pct), float(profit_pct),
             1 if active else 0, partner_id),
        )


@cached_read
def ratio_totals() -> dict[str, float]:
    """Sum of each ratio column across active, non-firm partners."""
    sharing = get_partners()
    sharing = sharing[sharing["is_firm"] == 0]
    return {col: float(sharing[col].sum()) for col in RATIO_COLUMN.values()}


def partner_usage(partner_id: int) -> int:
    """How many rows reference this partner (entries, splits or settlements)."""
    with connect() as conn:
        counts = [
            conn.execute(
                "SELECT COUNT(*) FROM entries WHERE cash_partner_id = ?", (partner_id,)
            ).fetchone()[0],
            conn.execute(
                "SELECT COUNT(*) FROM entry_splits WHERE partner_id = ?", (partner_id,)
            ).fetchone()[0],
            conn.execute(
                "SELECT COUNT(*) FROM settlements WHERE from_id = ? OR to_id = ?",
                (partner_id, partner_id),
            ).fetchone()[0],
        ]
    return sum(counts)


def delete_partner(partner_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM partners WHERE id = ? AND is_firm = 0", (partner_id,))


# --------------------------------------------------------------------------- #
# categories
# --------------------------------------------------------------------------- #
@cached_read
def get_categories(kind: str) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT name FROM categories WHERE kind = ? ORDER BY name", (kind,)
        ).fetchall()
    return [r["name"] for r in rows]


def add_category(name: str, kind: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO categories (name, kind) VALUES (?, ?)"
            " ON CONFLICT DO NOTHING",
            (name.strip(), kind),
        )


def delete_category(name: str, kind: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM categories WHERE name = ? AND kind = ?", (name, kind))


# --------------------------------------------------------------------------- #
# entries
# --------------------------------------------------------------------------- #
def resolve_split(
    share_mode: str,
    cash_partner_id: int,
    custom: dict[int, float] | None,
    partners: pd.DataFrame | None = None,
    kind: str = "expense",
) -> dict[int, float]:
    """Turn a sharing mode into concrete {partner_id: pct} burden percentages.

    In ``ratio`` mode an expense uses each partner's expense-sharing percentage
    and income uses their profit-sharing percentage.
    """
    if share_mode == "custom":
        if not custom:
            raise ValueError("A custom split needs at least one partner percentage.")
        return {int(k): float(v) for k, v in custom.items() if float(v) != 0}

    if share_mode == "none":
        return {int(cash_partner_id): 100.0}

    column = RATIO_COLUMN.get(kind)
    if column is None:
        raise ValueError(f"Unknown entry kind {kind!r}")

    partners = get_partners() if partners is None else partners
    sharing = partners[partners["is_firm"] == 0]
    total = float(sharing[column].sum())
    if total <= 0:
        label = "expense-sharing" if kind == "expense" else "profit-sharing"
        raise ValueError(
            f"No {label} percentages are set. Fix the ratios in Setup first."
        )
    return {int(r.id): float(getattr(r, column)) / total * 100.0
            for r in sharing.itertuples()}


def save_entry(
    *,
    entry_date,
    kind: str,
    amount: float,
    category: str,
    description: str,
    cash_partner_id: int,
    share_mode: str,
    currency: str | None = None,
    custom_split: dict[int, float] | None = None,
    entry_id: int | None = None,
) -> int:
    """Insert or update one entry together with its burden split.

    Expenses may be negative — that is how you record a refund, a credit note
    or a correction. Income must be positive.
    """
    if share_mode not in SHARE_MODES:
        raise ValueError(f"Unknown share mode {share_mode!r}")
    if float(amount) == 0:
        raise ValueError("Amount cannot be zero.")
    if float(amount) < 0 and kind not in NEGATIVE_KINDS:
        raise ValueError(
            "Only expenses can be negative — use a negative expense for a refund "
            "or credit note."
        )

    code = (currency or base_currency()).strip().upper()
    if code not in rate_map():
        raise ValueError(f"Unknown currency {code!r} — add it in Setup first.")

    splits = resolve_split(share_mode, cash_partner_id, custom_split, kind=kind)
    date_str = entry_date.isoformat() if hasattr(entry_date, "isoformat") else str(entry_date)

    with connect() as conn:
        if entry_id is None:
            cur = conn.execute(
                "INSERT INTO entries (entry_date, kind, amount, currency, category,"
                " description, cash_partner_id, share_mode, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (date_str, kind, float(amount), code, category.strip(),
                 description.strip(), int(cash_partner_id), share_mode, _now()),
            )
            entry_id = int(cur.fetchone()[0])
        else:
            conn.execute(
                "UPDATE entries SET entry_date = ?, kind = ?, amount = ?, currency = ?,"
                " category = ?, description = ?, cash_partner_id = ?, share_mode = ?"
                " WHERE id = ?",
                (date_str, kind, float(amount), code, category.strip(),
                 description.strip(), int(cash_partner_id), share_mode, int(entry_id)),
            )
            conn.execute("DELETE FROM entry_splits WHERE entry_id = ?", (entry_id,))

        conn.executemany(
            "INSERT INTO entry_splits (entry_id, partner_id, pct) VALUES (?, ?, ?)",
            [(entry_id, pid, pct) for pid, pct in splits.items()],
        )
    return int(entry_id)


def _reset_sequence(conn: Connection, table: str, highest: int) -> None:
    """Point the id counter at `highest` so the next id is highest + 1."""
    if not is_sqlite():
        # setval(..., 1, false) makes the next value 1, which is how you say
        # "empty table" to a Postgres sequence
        conn.execute(
            "SELECT setval(pg_get_serial_sequence(?, 'id'), ?, ?)",
            (table, highest if highest else 1, bool(highest)),
        )
        return
    if highest:
        if conn.execute(
            "SELECT COUNT(*) FROM sqlite_sequence WHERE name = ?", (table,)
        ).fetchone()[0]:
            conn.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (highest, table)
            )
        else:
            conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                (table, highest),
            )
    else:
        conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))


def resequence_entries() -> int:
    """Renumber entries 1..N in their original order, closing gaps from deletes.

    Child split rows are removed and reinserted against the new ids rather than
    updated in place, so foreign keys stay satisfied the whole way through.
    Returns how many entries were renumbered.
    """
    with connect() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM entries ORDER BY id")]
        mapping = {old: new for new, old in enumerate(ids, start=1) if old != new}
        if mapping:
            splits = conn.execute(
                "SELECT entry_id, partner_id, pct FROM entry_splits"
            ).fetchall()
            conn.execute("DELETE FROM entry_splits")
            # ascending order is safe: a renumbered id is always lower than the
            # old one, and the slot it moves into has already been vacated
            for old, new in sorted(mapping.items()):
                conn.execute("UPDATE entries SET id = ? WHERE id = ?", (new, old))
            conn.executemany(
                "INSERT INTO entry_splits (entry_id, partner_id, pct) VALUES (?, ?, ?)",
                [(mapping.get(s["entry_id"], s["entry_id"]), s["partner_id"], s["pct"])
                 for s in splits],
            )
        _reset_sequence(conn, "entries", len(ids))
    return len(mapping)


def delete_entry(entry_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM entry_splits WHERE entry_id = ?", (entry_id,))
        conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    resequence_entries()


def add_base_amount(df: pd.DataFrame, rates: dict[str, float] | None = None,
                    source: str = "amount", target: str = "base_amount") -> pd.DataFrame:
    """Add a base-currency column by applying the live rate for each row."""
    rates = rate_map() if rates is None else rates
    if df.empty:
        df[target] = pd.Series(dtype="float64")
        return df
    factor = df["currency"].map(rates).astype("float64").fillna(1.0)
    df[target] = (df[source].astype("float64") * factor).round(2)
    return df


def get_entries(
    date_from=None, date_to=None, kind: str | None = None,
    currency: str | None = None,
) -> pd.DataFrame:
    sql = (
        "SELECT e.*, p.name AS cash_partner FROM entries e"
        " JOIN partners p ON p.id = e.cash_partner_id WHERE 1 = 1"
    )
    params: list = []
    if date_from is not None:
        sql += " AND e.entry_date >= ?"
        params.append(str(date_from))
    if date_to is not None:
        sql += " AND e.entry_date <= ?"
        params.append(str(date_to))
    if kind:
        sql += " AND e.kind = ?"
        params.append(kind)
    if currency:
        sql += " AND e.currency = ?"
        params.append(currency)
    sql += " ORDER BY e.entry_date DESC, e.id DESC"

    with connect() as conn:
        return add_base_amount(conn.read_sql(sql, params))


def get_splits(entry_ids: list[int] | None = None) -> pd.DataFrame:
    sql = (
        "SELECT s.entry_id, s.partner_id, s.pct, p.name AS partner"
        " FROM entry_splits s JOIN partners p ON p.id = s.partner_id"
    )
    params: list = []
    if entry_ids is not None:
        if not entry_ids:
            return pd.DataFrame(columns=["entry_id", "partner_id", "pct", "partner"])
        sql += f" WHERE s.entry_id IN ({','.join('?' * len(entry_ids))})"
        params = list(entry_ids)
    with connect() as conn:
        return conn.read_sql(sql, params)


def format_split(split: dict[int, float], names: dict[int, str] | None = None) -> str:
    """'Partner A 70% / Partner B 30%' - a split rendered for one cell."""
    names = partner_names() if names is None else names
    return " / ".join(
        f"{names.get(pid, pid)} {pct:g}%" for pid, pct in sorted(split.items())
    )


def _refresh_targets(
    entry_ids: list[int] | None = None, include_custom: bool = False
) -> list[tuple[dict, dict[int, float], dict[int, float]]]:
    """Entries whose split would change if the current ratios were re-applied.

    Never touches `none` (deliberately not shared) entries, and only touches
    `custom` ones when asked, since a custom split is an explicit override.
    """
    modes = ("ratio", "custom") if include_custom else ("ratio",)
    people = get_partners()
    current = get_entries()
    if current.empty:
        return []
    if entry_ids is not None:
        current = current[current["id"].isin([int(i) for i in entry_ids])]

    targets = []
    for row in current[current["share_mode"].isin(modes)].itertuples():
        record = get_entry(int(row.id))
        if record is None:
            continue
        entry, old = record
        try:
            new = resolve_split("ratio", int(row.cash_partner_id), None,
                                people, kind=str(row.kind))
        except ValueError:
            continue
        same = (
            set(old) == set(new)
            and all(abs(old[pid] - new[pid]) < 0.005 for pid in new)
        )
        if not same:
            targets.append((entry, old, new))
    return targets


def ratio_refresh_preview(
    entry_ids: list[int] | None = None, include_custom: bool = False
) -> pd.DataFrame:
    """What a bulk re-apply would change, one row per affected entry."""
    columns = ["ID", "Date", "Type", "Category", "Amount", "Cur", "Sharing",
               "Before", "After"]
    targets = _refresh_targets(entry_ids, include_custom)
    if not targets:
        return pd.DataFrame(columns=columns)

    names = partner_names()
    return pd.DataFrame(
        [
            [
                entry["id"], entry["entry_date"], str(entry["kind"]).capitalize(),
                entry["category"], entry["amount"], entry["currency"],
                entry["share_mode"], format_split(old, names), format_split(new, names),
            ]
            for entry, old, new in targets
        ],
        columns=columns,
    )


def apply_ratio_refresh(
    entry_ids: list[int] | None = None, include_custom: bool = False
) -> int:
    """Re-apply the current ratios to the matching entries. Returns rows changed.

    An entry that had a custom split becomes a `ratio` entry, so the Ledger
    stops claiming it carries an override it no longer has.
    """
    targets = _refresh_targets(entry_ids, include_custom)
    for entry, _old, _new in targets:
        save_entry(
            entry_id=int(entry["id"]),
            entry_date=entry["entry_date"],
            kind=entry["kind"],
            amount=entry["amount"],
            currency=entry["currency"],
            category=entry["category"],
            description=entry["description"],
            cash_partner_id=int(entry["cash_partner_id"]),
            share_mode="ratio",
        )
    return len(targets)


def get_entry(entry_id: int) -> tuple[dict, dict[int, float]] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            return None
        splits = conn.execute(
            "SELECT partner_id, pct FROM entry_splits WHERE entry_id = ?", (entry_id,)
        ).fetchall()
    return dict(row), {r["partner_id"]: r["pct"] for r in splits}


# --------------------------------------------------------------------------- #
# settlements (actual money moved between partners)
# --------------------------------------------------------------------------- #
def add_settlement(paid_on, from_id: int, to_id: int, amount: float,
                   note: str = "", currency: str | None = None) -> None:
    if int(from_id) == int(to_id):
        raise ValueError("A settlement needs two different parties.")
    if float(amount) <= 0:
        raise ValueError("Settlement amount must be greater than zero.")
    code = (currency or base_currency()).strip().upper()
    if code not in rate_map():
        raise ValueError(f"Unknown currency {code!r} — add it in Setup first.")
    date_str = paid_on.isoformat() if hasattr(paid_on, "isoformat") else str(paid_on)
    with connect() as conn:
        conn.execute(
            "INSERT INTO settlements (paid_on, from_id, to_id, amount, currency, note,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date_str, int(from_id), int(to_id), float(amount), code,
             note.strip(), _now()),
        )


@cached_read
def get_settlements() -> pd.DataFrame:
    sql = (
        "SELECT s.*, pf.name AS from_name, pt.name AS to_name FROM settlements s"
        " JOIN partners pf ON pf.id = s.from_id"
        " JOIN partners pt ON pt.id = s.to_id"
        " ORDER BY s.paid_on DESC, s.id DESC"
    )
    with connect() as conn:
        return add_base_amount(conn.read_sql(sql))


def resequence_settlements() -> int:
    """Same gap-closing renumber for settlements, which have no child rows."""
    with connect() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM settlements ORDER BY id")]
        mapping = {old: new for new, old in enumerate(ids, start=1) if old != new}
        for old, new in sorted(mapping.items()):
            conn.execute("UPDATE settlements SET id = ? WHERE id = ?", (new, old))
        _reset_sequence(conn, "settlements", len(ids))
    return len(mapping)


def delete_settlement(settlement_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM settlements WHERE id = ?", (settlement_id,))
    resequence_settlements()


# --------------------------------------------------------------------------- #
# backup and restore
# --------------------------------------------------------------------------- #
# Backups are a gzipped JSON dump of every table rather than a copy of the
# SQLite file, so one works on either backend — and the same code moves your
# books from a local SQLite file into a hosted Postgres.
DUMP_TABLES = ["partners", "categories", "currencies", "settings",
               "entries", "entry_splits", "settlements"]
DUMP_FORMAT = 2


def dump_all() -> dict:
    """Every table as plain Python data, newest schema first."""
    payload = {"format": DUMP_FORMAT, "taken_at": _now(), "tables": {}}
    with connect() as conn:
        for table in DUMP_TABLES:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            payload["tables"][table] = [dict(row) for row in rows]
    return payload


def backup_bytes() -> bytes:
    """A gzipped JSON backup, safe to take while the app is running."""
    return gzip.compress(
        json.dumps(dump_all(), default=str, indent=1).encode("utf-8")
    )


def _parse_backup(raw: bytes) -> dict:
    if raw[:15] == b"SQLite format 3":
        raise ValueError(
            "That is a raw SQLite database, not a backup file. Older versions "
            "of this app downloaded backups in that format. Take a fresh "
            "backup from Setup — the current format is a small .gz file "
            "(a couple of KB, not tens of KB)."
        )
    try:
        gzipped = raw[:2] == bytes((0x1F, 0x8B))
        decoded = gzip.decompress(raw) if gzipped else raw
        payload = json.loads(decoded.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"That is not a readable backup file ({exc}).") from exc
    if not isinstance(payload, dict) or "tables" not in payload:
        raise ValueError("That file is not a Partnership Books backup.")
    missing = [t for t in ("partners", "entries", "entry_splits")
               if t not in payload["tables"]]
    if missing:
        raise ValueError(
            "That backup is missing " + ", ".join(missing)
            + " — it does not look like a Partnership Books backup."
        )
    return payload


def describe_backup(raw: bytes) -> dict:
    """Validate an uploaded backup and summarise it, without changing anything."""
    tables = _parse_backup(raw)["tables"]
    return {
        "entries": len(tables.get("entries", [])),
        "settlements": len(tables.get("settlements", [])),
        "partners": len(tables.get("partners", [])),
    }


def restore_bytes(raw: bytes) -> dict:
    """Replace everything with an uploaded backup. Returns its counts.

    The current books are dumped to a file next to the database first, so a
    restore is recoverable even though it is destructive.
    """
    counts = describe_backup(raw)
    tables = _parse_backup(raw)["tables"]

    try:
        previous = backup_bytes()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        (DB_PATH.parent / f"replaced-{stamp}.books.gz").write_bytes(previous)
    except Exception:
        pass  # an empty or unreachable database is nothing to preserve

    init_db()
    with connect() as conn:
        # children first so foreign keys stay satisfied on the way out
        for table in reversed(DUMP_TABLES):
            conn.execute(f"DELETE FROM {table}")
        for table in DUMP_TABLES:
            rows = tables.get(table, [])
            if not rows:
                continue
            columns = list(rows[0])
            placeholders = ", ".join("?" for _ in columns)
            conn.executemany(
                f"INSERT INTO {table} ({', '.join(columns)})"
                f" VALUES ({placeholders})",
                [tuple(row.get(column) for column in columns) for row in rows],
            )
        for table in ("entries", "settlements", "partners"):
            highest = conn.execute(
                f"SELECT COALESCE(MAX(id), 0) FROM {table}"
            ).fetchone()[0]
            _reset_sequence(conn, table, int(highest))
    return counts
