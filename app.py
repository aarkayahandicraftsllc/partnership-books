"""Partnership expense & income tracker - Streamlit front end.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import calendar
import hmac
import os
import time
from datetime import date

import pandas as pd
import streamlit as st

import db
import exports
import settlement as stl
import statements as fin

st.set_page_config(page_title="Partnership Books", page_icon="📒", layout="wide")


SECRET_KEYS = ("BOOKS_DB_URL", "BOOKS_PASSWORD")


def bridge_secrets() -> None:
    """Copy Streamlit secrets into the environment.

    Streamlit Community Cloud delivers configuration through ``st.secrets``
    rather than environment variables, but `db.py` reads the environment so it
    stays usable from plain scripts. This bridges the two, and the environment
    wins so a local override is never clobbered.
    """
    for key in SECRET_KEYS:
        if os.environ.get(key, "").strip():
            continue
        for candidate in (key, key.lower()):
            try:
                value = st.secrets.get(candidate)
            except Exception:
                value = None  # no secrets file, which is normal locally
            if value:
                os.environ[key] = str(value).strip()
                break


def _configured_password() -> str:
    """The shared password. Blank means no gate, which is right locally."""
    return os.environ.get("BOOKS_PASSWORD", "").strip()


MIN_PASSWORD = 16  # the app URL is public, so the password is the whole defence


def require_password() -> None:
    """Gate the whole app behind a shared password when one is configured."""
    expected = _configured_password()
    if not expected or st.session_state.get("unlocked"):
        return

    st.title("📒 Partnership Books")
    st.caption("Enter the shared password to open the books.")
    typed = st.text_input("Password", type="password", key="pw_input")
    if st.button("Unlock", type="primary"):
        # compare_digest keeps the check from leaking the password's length
        if hmac.compare_digest(typed, expected):
            st.session_state["unlocked"] = True
            st.session_state.pop("pw_attempts", None)
            st.rerun()
        else:
            attempts = st.session_state.get("pw_attempts", 0) + 1
            st.session_state["pw_attempts"] = attempts
            # Back off a little more each time. Community Cloud gives no way to
            # rate-limit by address, so this is the only brake available on
            # automated guessing — which is why the password must be long.
            time.sleep(min(0.4 * attempts, 4.0))
            st.error("That password is not right.")
    st.stop()


bridge_secrets()
require_password()

# Streamlit re-runs this script from the top on every interaction, and renders
# every tab body rather than just the visible one, so the same reference rows
# were being read dozens of times per rerun - 334 round trips to a hosted
# database for one click. Memoise reads for the life of this rerun; db clears
# the memo the moment anything is written, so nothing stale can be displayed.
db.enable_read_cache()

try:
    db.init_db()
except Exception as exc:  # a hosted database that cannot be reached
    st.title("📒 Partnership Books")
    st.error("**Could not reach the database.** Nothing has been changed.")
    st.caption(f"Backend **{db.dialect()}** · target `{db.safe_target()}`")
    st.markdown(
        "Check `BOOKS_DB_URL` in the app's secrets. The usual causes, in "
        "the order they normally turn out to be:\n\n"
        "1. **The password was rotated** and the secret still has the old "
        "one — or the secret was updated and the rotation never happened.\n"
        "2. **The connection string was truncated** when pasted. The host "
        "above should match the one in your database console.\n"
        "3. **The database is asleep** and took too long to wake. Reload "
        "once; if it works the second time, that was it.\n"
        "4. **The project or branch was deleted or recreated**, which "
        "changes both the host and the password."
    )
    with st.expander("The underlying error (credentials removed)"):
        st.code(db.scrub(exc)[:1500] or "(no message)")
    st.stop()

# A password set with no database URL almost certainly means this is hosted on
# a filesystem that gets wiped — say so loudly rather than losing the books.
if _configured_password() and db.is_sqlite():
    st.error(
        "⚠️ **This looks like a deployment storing data in a local SQLite "
        "file.** If the host restarts, everything entered here is gone. Set "
        "`BOOKS_DB_URL` in the app's secrets to a Postgres connection string, "
        "then restore from a backup."
    )

BASE = db.base_currency()
SYMBOLS = db.symbol_map()
RATES = db.rate_map()
BASE_SYMBOL = SYMBOLS.get(BASE, BASE)
DEFAULT_CURRENCY = {kind: db.default_currency(kind) for kind in db.RATIO_COLUMN}
# Where a fresh entry's Category lands. Without this the box opens on whatever
# sorts first alphabetically, which for income is Commission rather than the
# one nearly every income line uses. Falls back to the first category if the
# named one has been renamed or removed.
DEFAULT_CATEGORY = {"income": "Sales"}
BUSINESS = db.get_setting("business_name", "My Partnership")
FY_START_MONTH = int(db.get_setting("fy_start_month", "4"))

SHARE_LABELS = {
    "ratio": "Per partnership ratio",
    "custom": "Custom split",
    "none": "Not shared",
}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def money(value: float, code: str | None = None) -> str:
    """Format with the sign outside the symbol, so -₹500.00 not ₹-500.00."""
    symbol = SYMBOLS.get(code or BASE, code or BASE)
    sign = "-" if value < 0 else ""
    return f"{sign}{symbol}{abs(value):,.2f}"


def base_col(label: str) -> st.column_config.NumberColumn:
    """Money column known to be in the base currency."""
    return st.column_config.NumberColumn(label, format=f"{BASE_SYMBOL}%.2f")


def plain_col(label: str) -> st.column_config.NumberColumn:
    """Money column whose currency varies row by row."""
    return st.column_config.NumberColumn(label, format="%.2f")


def flash(message: str, kind: str = "success") -> None:
    """Queue a confirmation that survives the st.rerun() following an action.

    Calling st.success() straight before st.rerun() throws the message away,
    because the rerun discards everything already rendered.
    """
    st.session_state["flash"] = (kind, message)


def show_flash() -> None:
    queued = st.session_state.pop("flash", None)
    if queued:
        kind, message = queued
        getattr(st, kind)(message)


ROW_PX = 35  # Streamlit renders dataframe rows and the header at 35px

# how many rows the ledger shows before scrolling inside its own box
TABLE_HEIGHTS = {
    "Standard (10 rows)": 10,
    "Tall (20 rows)": 20,
    "Show all rows": None,
}


def table_height(row_count: int, visible: int | None) -> int:
    """Pixel height for a dataframe. `visible=None` fits every row."""
    rows = row_count if visible is None else min(row_count, visible)
    return ROW_PX * (max(rows, 1) + 1) + 3


def nonce(name: str) -> int:
    """Widget-key salt, bumped to reset a group of widgets after a save."""
    return st.session_state.setdefault(f"nonce_{name}", 0)


def bump(name: str) -> None:
    st.session_state[f"nonce_{name}"] = nonce(name) + 1


def fy_bounds(today: date, start_month: int) -> tuple[date, date]:
    start_year = today.year if today.month >= start_month else today.year - 1
    start = date(start_year, start_month, 1)
    end_month = 12 if start_month == 1 else start_month - 1
    end_year = start_year if start_month == 1 else start_year + 1
    end = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
    return start, end


def month_bounds(anchor: date) -> tuple[date, date]:
    first = anchor.replace(day=1)
    last = anchor.replace(day=calendar.monthrange(anchor.year, anchor.month)[1])
    return first, last


def statement_period(key: str) -> tuple[date | None, date, str]:
    """Period picker shared by the three statement screens."""
    fy_start, fy_end = fy_bounds(date.today(), FY_START_MONTH)
    month_start, month_end = month_bounds(date.today())
    choices = {
        "This financial year": (fy_start, min(fy_end, date.today())),
        "This month": (month_start, min(month_end, date.today())),
        "Since the beginning": (None, date.today()),
        "Up to a date": None,
    }
    c1, c2 = st.columns([2, 2])
    with c1:
        choice = st.selectbox("Period", list(choices), key=f"{key}_period")
    if choice == "Up to a date":
        with c2:
            as_at = st.date_input("As at", value=date.today(),
                                  format="DD/MM/YYYY", key=f"{key}_asat")
        return None, as_at, f"everything up to {as_at.strftime('%d/%m/%Y')}"
    date_from, date_to = choices[choice]
    label = (
        f"{choice.lower()} "
        f"({date_from.strftime('%d/%m/%Y')} to {date_to.strftime('%d/%m/%Y')})"
        if date_from else f"everything up to {date_to.strftime('%d/%m/%Y')}"
    )
    return date_from, date_to, label


def statement_data(date_from, date_to):
    """Entries for the period, plus positions cumulative to `date_to`.

    A P&L covers a period while a trial balance and balance sheet are 'as at' a
    date, so the two are deliberately scoped differently.
    """
    period_entries = db.get_entries(date_from=date_from, date_to=date_to)
    cumulative = db.get_entries(date_to=date_to)
    settlements = fin.up_to(db.get_settlements(), "paid_on", date_to)
    positions = stl.compute_positions(
        cumulative,
        db.get_splits(cumulative["id"].astype(int).tolist()
                      if not cumulative.empty else []),
        db.get_partners(include_inactive=True),
        settlements,
    )
    return period_entries, cumulative, positions


def statement_table(frame: pd.DataFrame, money_columns: list[str],
                    **column_config) -> None:
    st.dataframe(
        frame, hide_index=True, width="stretch",
        height=table_height(len(frame), None),
        column_config={**{c: base_col(c) for c in money_columns}, **column_config},
    )


# --------------------------------------------------------------------------- #
# the shared entry editor (used for both new entries and edits)
# --------------------------------------------------------------------------- #
def entry_editor(prefix: str, partners: pd.DataFrame, existing: dict | None = None,
                 existing_split: dict[int, float] | None = None) -> dict | None:
    """Render the entry widgets. Returns a payload dict when saved, else None."""
    n = nonce(prefix)
    k = lambda name: f"{prefix}_{name}_{n}"  # noqa: E731

    ids = [int(i) for i in partners["id"]]
    names = dict(zip(partners["id"].astype(int), partners["name"]))
    sharers = partners[partners["is_firm"] == 0]
    codes = list(RATES)

    c1, c2, c3 = st.columns(3)
    with c1:
        kind = st.radio(
            "Type", ["expense", "income"],
            format_func=str.capitalize, horizontal=True, key=k("kind"),
            index=0 if (existing or {}).get("kind", "expense") == "expense" else 1,
        )
        entry_date = st.date_input(
            "Date",
            value=date.fromisoformat(existing["entry_date"]) if existing else date.today(),
            format="DD/MM/YYYY", key=k("date"),
        )
    with c2:
        acol, ccol = st.columns([3, 2])
        with acol:
            amount = st.number_input(
                "Amount",
                # expenses may go negative for refunds; income may not
                min_value=None if kind == "expense" else 0.0,
                step=100.0, format="%.2f",
                value=float(existing["amount"]) if existing else 0.0, key=k("amount"),
                help="Enter a negative amount to record a refund, credit note or "
                     "correction against an expense."
                     if kind == "expense" else None,
            )
        with ccol:
            default_code = (existing or {}).get("currency") or DEFAULT_CURRENCY[kind]
            currency = st.selectbox(
                "Currency", codes,
                index=codes.index(default_code) if default_code in codes else 0,
                # the kind is part of the key so flipping Expense/Income really
                # re-applies that kind's default instead of keeping the old pick
                key=k(f"cur_{kind}"),
            )
        cash_label = "Paid by" if kind == "expense" else "Received by"
        default_cash = int(existing["cash_partner_id"]) if existing else ids[0]
        cash_partner_id = st.selectbox(
            cash_label, ids,
            format_func=lambda i: names[i],
            index=ids.index(default_cash) if default_cash in ids else 0,
            key=k("cash"),
            help="Whose pocket or account the money actually moved through.",
        )
    with c3:
        cats = db.get_categories(kind)
        existing_cat = (existing or {}).get("category", "")
        if existing_cat and existing_cat not in cats:
            cats = [existing_cat] + cats
        default_cat = existing_cat or DEFAULT_CATEGORY.get(kind, "")
        category = st.selectbox(
            "Category", cats + ["➕ New category…"],
            index=cats.index(default_cat) if default_cat in cats else 0,
            # the kind is part of the key for the same reason as the currency
            # above: flipping Expense/Income should re-apply that kind's
            # default rather than hold on to the category picked for the other
            key=k(f"cat_{kind}"),
        )
        if category == "➕ New category…":
            category = st.text_input("New category name", key=k("newcat")).strip()
        description = st.text_input(
            "Description / notes", value=(existing or {}).get("description", ""),
            key=k("desc"),
        )

    modes = ["ratio", "custom", "none"]
    default_mode = (existing or {}).get("share_mode", "ratio")
    ratio_column = db.RATIO_COLUMN[kind]
    ratio_name = "expense-sharing" if kind == "expense" else "profit-sharing"
    ratio_hint = " / ".join(
        f"{r.name} {getattr(r, ratio_column):g}%" for r in sharers.itertuples()
    ) or "no ratios set"
    share_mode = st.radio(
        "Sharing",
        modes,
        index=modes.index(default_mode) if default_mode in modes else 0,
        horizontal=True,
        key=k("mode"),
        format_func=lambda m: {
            "ratio": f"Per {ratio_name} ratio ({ratio_hint})",
            "custom": "Custom split for this entry",
            "none": f"Not shared — fully {names.get(cash_partner_id, '?')}",
        }[m],
    )

    custom_split: dict[int, float] = {}
    if share_mode == "custom":
        st.caption(
            "Percentages must add up to 100. Set a partner to 0 to leave them out."
        )
        cols = st.columns(max(len(sharers), 1))
        for col, row in zip(cols, sharers.itertuples()):
            pid = int(row.id)
            with col:
                custom_split[pid] = st.number_input(
                    row.name, min_value=0.0, max_value=100.0, step=5.0,
                    value=float((existing_split or {}).get(pid, 0.0)),
                    key=k(f"split_{pid}"),
                )
        total_pct = sum(custom_split.values())
        if abs(total_pct - 100.0) > 0.01:
            st.warning(f"Custom split totals {total_pct:g}% — it must be 100%.")

    # live preview of who bears what
    if amount != 0:
        try:
            preview = db.resolve_split(share_mode, cash_partner_id, custom_split,
                                       partners, kind=kind)
            refund = kind == "expense" and amount < 0
            if refund:
                verb, noun = "gets back", "is credited"
            elif kind == "expense":
                verb, noun = "pays out", "bears"
            else:
                verb, noun = "receives", "earns"
            rate = RATES.get(currency, 1.0)
            fx = (
                ""
                if currency == BASE
                else f"  ·  converted at {rate:,.4g} → {money(abs(amount) * rate)}"
            )
            st.info(
                ("↩️ **Refund / credit note.** " if refund else "")
                + f"{names.get(cash_partner_id, '?')} {verb} "
                f"{money(abs(amount), currency)}  ·  "
                + "   ".join(
                    f"**{names.get(pid, pid)}** {noun} "
                    f"{money(abs(amount) * pct / 100, currency)}"
                    for pid, pct in preview.items()
                )
                + fx
            )
        except ValueError as exc:
            st.warning(str(exc))

    saved = st.button(
        "💾 Save changes" if existing else "➕ Add entry",
        type="primary", key=k("save"),
    )
    if not saved:
        return None

    if amount == 0:
        st.error("Enter an amount other than zero.")
        return None
    if amount < 0 and kind != "expense":
        st.error("Only expenses can be negative. Record income as a positive amount.")
        return None
    if not category:
        st.error("Pick or type a category.")
        return None
    if share_mode == "custom" and abs(sum(custom_split.values()) - 100.0) > 0.01:
        st.error("The custom split must total exactly 100%.")
        return None

    if category not in db.get_categories(kind):
        db.add_category(category, kind)

    return {
        "entry_date": entry_date,
        "kind": kind,
        "amount": amount,
        "currency": currency,
        "category": category,
        "description": description,
        "cash_partner_id": cash_partner_id,
        "share_mode": share_mode,
        "custom_split": custom_split or None,
    }


# --------------------------------------------------------------------------- #
# several lines at once
#
# The header fields are entered once; the grid holds one row per amount, each
# with its own sharing choice. Every row is saved as its own entry, which is
# what the schema already models - one amount per entry, with its own split -
# so the Ledger and the statements need no changes to understand them.
# --------------------------------------------------------------------------- #
SHARE_GRID_LABELS = {"Ratio": "ratio", "Custom": "custom", "Not shared": "none"}


def effective_pct(mode: str, cash_partner_id: int, typed: dict[int, float],
                  partners: pd.DataFrame, kind: str,
                  sharer_ids: list[int]) -> dict[int, float]:
    """What each partner's percentage really is for one grid row.

    The grid's percentage columns are an input only for Custom rows. For Ratio
    and Not shared the figures are worked out, so showing anything else - the
    ratio against a Not shared row, say - states a split that will not be
    used. This returns what db.resolve_split will actually apply, so the grid
    can show that instead.
    """
    if mode == "custom":
        return {pid: float(typed.get(pid, 0.0) or 0.0) for pid in sharer_ids}
    try:
        resolved = db.resolve_split(mode, int(cash_partner_id), None,
                                    partners, kind=kind)
    except ValueError:
        return {pid: 0.0 for pid in sharer_ids}
    # Not shared can land on the firm, which has no column of its own; such a
    # row simply shows zero against every sharer, which is the truth.
    # round: resolve_split normalises, which can leave 55.00000000000001
    return {pid: round(float(resolved.get(pid, 0.0)), 2) for pid in sharer_ids}


def multi_entry_form(partners: pd.DataFrame) -> None:
    n = nonce("multi")
    k = lambda name: f"multi_{name}_{n}"  # noqa: E731

    ids = [int(i) for i in partners["id"]]
    names = dict(zip(partners["id"].astype(int), partners["name"]))
    sharers = partners[partners["is_firm"] == 0]
    pct_columns = {int(r.id): f"{r.name} %" for r in sharers.itertuples()}

    c1, c2, c3 = st.columns(3)
    with c1:
        kind = st.radio("Type", ["expense", "income"], format_func=str.capitalize,
                        horizontal=True, key=k("kind"))
        entry_date = st.date_input("Date", value=date.today(),
                                   format="DD/MM/YYYY", key=k("date"))
    with c2:
        codes = list(RATES)
        default_code = DEFAULT_CURRENCY[kind]
        currency = st.selectbox(
            "Currency", codes,
            index=codes.index(default_code) if default_code in codes else 0,
            key=k(f"cur_{kind}"),
        )
        cash_label = "Paid by" if kind == "expense" else "Received by"
        cash_partner_id = st.selectbox(
            cash_label, ids, format_func=lambda i: names[i], key=k("cash"),
            help="Whose pocket or account the money actually moved through. "
                 "It applies to every line below.",
        )
    with c3:
        cats = db.get_categories(kind)
        default_cat = DEFAULT_CATEGORY.get(kind, "")
        category = st.selectbox(
            "Category", cats,
            index=cats.index(default_cat) if default_cat in cats else 0,
            key=k(f"cat_{kind}"),
        )
        description = st.text_input("Description / notes", key=k("desc"))

    # expense_pct for expenses, profit_pct for income
    ratio_column = db.RATIO_COLUMN[kind]
    ratio_hint = " / ".join(
        f"{r.name} {getattr(r, ratio_column):g}%" for r in sharers.itertuples()
    ) or "no ratios set"
    st.caption(
        f"One row per amount. **Ratio** uses the {ratio_hint} split, "
        f"**Custom** uses the per-partner percentages on that row (they must "
        f"total 100), **Not shared** puts the whole line on "
        f"{names.get(cash_partner_id, '?')}. Blank rows are ignored."
    )

    # The grid is rebuilt from what it held last run, with the percentage
    # columns recomputed from each row's Sharing choice: Ratio shows the Setup
    # ratio, Not shared shows the whole line on whoever handled the cash, and
    # Custom keeps whatever was typed.
    #
    # The widget key carries a fingerprint of the Sharing column. Editing an
    # amount leaves the fingerprint alone, so Streamlit's own edit tracking
    # handles it; changing a Sharing cell changes the key, which rebuilds the
    # editor from the frame below - carrying the amounts over but with the
    # percentages put right. Without that, Streamlit would keep re-applying
    # the stale cells it remembers.
    sharer_ids = [int(r.id) for r in sharers.itertuples()]
    previous = st.session_state.get(k(f"rows_{kind}"))
    if previous is None:
        previous = [{"Amount": None, "Sharing": "Ratio"} for _ in range(3)]

    rows = []
    for row in previous:
        mode = SHARE_GRID_LABELS.get(row.get("Sharing") or "Ratio", "ratio")
        typed = {pid: row.get(pct_columns[pid]) for pid in sharer_ids}
        shown = effective_pct(mode, cash_partner_id, typed, partners, kind,
                              sharer_ids)
        rows.append({
            "Amount": row.get("Amount"),
            "Sharing": row.get("Sharing") or "Ratio",
            **{pct_columns[pid]: shown[pid] for pid in sharer_ids},
        })
    blank = pd.DataFrame(rows)

    # A readable, deterministic fingerprint - "RCN" for Ratio, Custom, Not
    # shared - rather than hash(), whose value is salted per process.
    fingerprint = "".join(str(r["Sharing"])[0] for r in rows) or "0"
    edited = st.data_editor(
        blank, key=k(f"grid_{kind}_{fingerprint}"),
        num_rows="dynamic", width="stretch",
        column_config={
            "Amount": st.column_config.NumberColumn(
                "Amount", format="%.2f", step=100.0,
                help="Negative is allowed on expenses only, for a refund."),
            "Sharing": st.column_config.SelectboxColumn(
                "Sharing", options=list(SHARE_GRID_LABELS), required=True,
                width="medium"),
            **{col: st.column_config.NumberColumn(col, format="%.2f",
                                                  min_value=0.0, max_value=100.0,
                                                  step=5.0)
               for col in pct_columns.values()},
        },
    )

    # Build the lines, collecting complaints rather than stopping at the first:
    # it is quicker to fix a grid once than to be told about one row at a time.
    lines, problems = [], []
    st.session_state[k(f"rows_{kind}")] = edited.to_dict("records")

    # records, not itertuples: a column called "Deepa %" is not a valid
    # attribute name, so itertuples would rename it and the percentages would
    # silently read as zero.
    for position, row in enumerate(edited.to_dict("records"), start=1):
        raw = row.get("Amount")
        if raw is None or pd.isna(raw) or float(raw) == 0:
            continue
        amount = float(raw)
        mode = SHARE_GRID_LABELS.get(row.get("Sharing", "Ratio"), "ratio")
        split = {}
        if mode == "custom":
            split = {pid: float(row.get(col) or 0.0)
                     for pid, col in pct_columns.items()}
        if amount < 0 and kind != "expense":
            problems.append(f"Row {position}: only expenses can be negative.")
            continue
        if mode == "custom" and abs(sum(split.values()) - 100.0) > 0.01:
            problems.append(
                f"Row {position}: custom split totals {sum(split.values()):g}%"
                " — it must be 100%.")
            continue
        lines.append({
            "entry_date": entry_date, "kind": kind, "amount": amount,
            "currency": currency, "category": category,
            "description": description, "cash_partner_id": int(cash_partner_id),
            "share_mode": mode, "custom_split": split or None,
        })

    for message in problems:
        st.warning(message)

    if lines:
        total = sum(line["amount"] for line in lines)
        borne: dict[int, float] = {}
        for line in lines:
            for pid, pct in db.resolve_split(
                    line["share_mode"], line["cash_partner_id"],
                    line["custom_split"], partners, kind=kind).items():
                borne[pid] = borne.get(pid, 0.0) + line["amount"] * pct / 100
        verb = "bears" if kind == "expense" else "earns"
        st.info(
            f"**{len(lines)} {'line' if len(lines) == 1 else 'lines'}** · total "
            f"{money(total, currency)}  ·  "
            + "   ".join(f"**{names.get(pid, pid)}** {verb} "
                         f"{money(value, currency)}"
                         for pid, value in borne.items())
        )

    if lines:
        # The authoritative statement of what is about to be written. Whatever
        # the grid cells say, this is computed from the same resolve_split the
        # save uses, so the two cannot disagree.
        breakdown = []
        for position, line in enumerate(lines, start=1):
            applied = db.resolve_split(
                line["share_mode"], line["cash_partner_id"],
                line["custom_split"], partners, kind=kind)
            breakdown.append({
                "Line": position,
                "Amount": line["amount"],
                "Sharing": next(label for label, mode
                                in SHARE_GRID_LABELS.items()
                                if mode == line["share_mode"]),
                **{names.get(pid, str(pid)): line["amount"] * pct / 100
                   for pid, pct in applied.items()},
            })
        with st.expander(f"What will be saved — {len(breakdown)} "
                         f"{'line' if len(breakdown) == 1 else 'lines'}",
                         expanded=True):
            frame = pd.DataFrame(breakdown)
            st.dataframe(
                frame, hide_index=True, width="stretch",
                column_config={c: st.column_config.NumberColumn(c, format="%.2f")
                               for c in frame.columns if c not in ("Line", "Sharing")},
            )

    if st.button(f"➕ Add {len(lines) or ''} "
                 f"{'entry' if len(lines) == 1 else 'entries'}".replace("  ", " "),
                 type="primary", key=k("save"), disabled=not lines):
        try:
            saved = db.save_entries(lines)
            flash(f"Saved {len(saved)} entries · total "
                  f"{money(sum(line['amount'] for line in lines), currency)} "
                  f"({category}).")
            bump("multi")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))


# --------------------------------------------------------------------------- #
# ledger table builder
# --------------------------------------------------------------------------- #
def ledger_table(entries: pd.DataFrame, splits: pd.DataFrame,
                 partners: pd.DataFrame) -> pd.DataFrame:
    """Entries with one extra column per partner showing their share.

    Partner share columns are in the row's own currency, so every row reads
    consistently; ``Amount (BASE)`` carries the converted figure.
    """
    if entries.empty:
        return pd.DataFrame()

    view = entries.rename(columns={
        "id": "ID", "entry_date": "Date", "kind": "Type", "category": "Category",
        "description": "Description", "amount": "Amount", "currency": "Cur",
        "base_amount": f"Amount ({BASE})", "cash_partner": "Cash by",
        "share_mode": "Sharing",
    })[["ID", "Date", "Type", "Category", "Description", "Amount", "Cur",
        f"Amount ({BASE})", "Cash by", "Sharing"]]
    view["Type"] = view["Type"].str.capitalize()
    view["Sharing"] = view["Sharing"].map(SHARE_LABELS)

    exploded = stl.per_partner_amounts(entries, splits)
    if not exploded.empty:
        pivot = exploded.pivot_table(
            index="entry_id", columns="partner_id", values="split_amount",
            aggfunc="sum", fill_value=0.0,
        )
        names = dict(zip(partners["id"].astype(int), partners["name"]))
        pivot.columns = [f"{names.get(int(c), c)} share" for c in pivot.columns]
        view = view.merge(pivot, left_on="ID", right_index=True, how="left")
        view[pivot.columns] = view[pivot.columns].fillna(0.0)
    return view


# --------------------------------------------------------------------------- #
# header
# --------------------------------------------------------------------------- #
partners = db.get_partners()
if partners.empty:
    st.error("No partners configured. Add at least one in Setup.")
    st.stop()

st.title(f"📒 {BUSINESS}")
st.caption(
    f"Base currency **{BASE}** · "
    + " · ".join(
        f"1 {code} = {rate:,.4g} {BASE}" for code, rate in RATES.items() if code != BASE
    )
    + f" · new entries start on {DEFAULT_CURRENCY['expense']} for expenses, "
      f"{DEFAULT_CURRENCY['income']} for income"
)

sharers = partners[partners["is_firm"] == 0]
off_ratios = {
    label: total
    for label, total in (
        ("Expense sharing", float(sharers["expense_pct"].sum())),
        ("Profit sharing", float(sharers["profit_pct"].sum())),
    )
    if abs(total - 100.0) > 0.01
}
show_flash()

if off_ratios:
    st.warning(
        " ".join(f"**{label}** adds up to {total:g}%, not 100%."
                 for label, total in off_ratios.items())
        + " Ratio-based entries are normalised pro-rata — fix them in **Setup**."
    )

(tab_add, tab_ledger, tab_settle, tab_trial, tab_pl, tab_bs, tab_setup) = st.tabs(
    ["➕ Add Entry", "📋 Ledger", "🤝 Settlement", "⚖️ Trial Balance",
     "📈 Profit & Loss", "🏦 Balance Sheet", "⚙️ Setup"]
)

# --------------------------------------------------------------------------- #
# tab: add entry
# --------------------------------------------------------------------------- #
with tab_add:
    st.subheader("Record an expense or income")
    if st.toggle(
        "Several amounts at once",
        key="new_multi_mode",
        help="One row per amount, all sharing the date, category, description, "
             "partner and currency. Each row keeps its own sharing choice and "
             "is saved as its own entry.",
    ):
        multi_entry_form(partners)
        payload = None
    else:
        payload = entry_editor("new", partners)
    if payload:
        try:
            db.save_entry(**payload)
            label = ("expense refund" if payload["amount"] < 0
                     else payload["kind"])
            flash(
                f"Saved {label} of "
                f"{money(abs(payload['amount']), payload['currency'])} "
                f"({payload['category']})."
            )
            bump("new")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

# --------------------------------------------------------------------------- #
# tab: ledger
# --------------------------------------------------------------------------- #
with tab_ledger:
    fy_start, fy_end = fy_bounds(date.today(), FY_START_MONTH)
    month_start, month_end = month_bounds(date.today())
    periods = {
        "All time": (None, None),
        "This month": (month_start, month_end),
        "This financial year": (fy_start, fy_end),
        "Custom range": None,
    }
    fcol1, fcol2, fcol3, fcol4, fcol5 = st.columns([2, 3, 2, 2, 2])
    with fcol1:
        period = st.selectbox("Period", list(periods), key="led_period")
    if period == "Custom range":
        with fcol2:
            rng = st.date_input(
                "From / to", value=(fy_start, date.today()),
                format="DD/MM/YYYY", key="led_range",
            )
        date_from, date_to = rng if isinstance(rng, tuple) and len(rng) == 2 else (None, None)
    else:
        date_from, date_to = periods[period]
    with fcol3:
        kind_filter = st.selectbox(
            "Type", ["All", "expense", "income"],
            format_func=lambda s: s.capitalize(), key="led_kind",
        )
    with fcol4:
        cur_filter = st.selectbox("Currency", ["All"] + list(RATES), key="led_cur")
    with fcol5:
        group_choice = st.selectbox("Group by", list(stl.GROUP_BY), key="led_group")
    group_by = stl.GROUP_BY[group_choice]

    entries = db.get_entries(
        date_from=date_from, date_to=date_to,
        kind=None if kind_filter == "All" else kind_filter,
        currency=None if cur_filter == "All" else cur_filter,
    )
    splits = db.get_splits(entries["id"].astype(int).tolist() if not entries.empty else [])

    totals = stl.summary_totals(entries)
    m1, m2, m3, m4 = st.columns(4)
    refunds = (
        0 if entries.empty
        else int(((entries["kind"] == "expense") & (entries["amount"] < 0)).sum())
    )
    m1.metric(f"Income ({BASE})", money(totals["income"]))
    m2.metric(
        f"Expenses ({BASE})", money(totals["expense"]),
        help=f"Net of {refunds} refund/credit entr"
             f"{'y' if refunds == 1 else 'ies'}." if refunds else None,
    )
    m3.metric(f"Net profit ({BASE})", money(totals["net"]))
    m4.metric("Entries", f"{len(entries)}")

    if entries.empty:
        st.info("No entries for this period yet.")
    else:
        view = ledger_table(entries, splits, partners)
        if group_by:
            # order the detail rows so they read under their group
            view = view.sort_values(
                [stl.GROUP_LABELS[b] for b in group_by] + ["Date"],
                ascending=[True] * len(group_by) + [False],
            ).reset_index(drop=True)
        summary = (
            stl.group_totals(entries, splits, partners, group_by)
            if group_by else None
        )

        # Toolbar above the tables, so the exports are reachable without
        # scrolling past a long list.
        stamp = date.today().isoformat()
        t1, t2, t3 = st.columns([2, 2, 3])
        with t1:
            height_choice = st.selectbox(
                "Table height", list(TABLE_HEIGHTS), key="led_height",
                help="The list scrolls inside its own box. Choose *Show all rows* "
                     "to make the page scroll instead.",
            )
        with t2:
            st.write("")
            st.download_button(
                "⬇️ Transactions CSV",
                data=view.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"ledger_{stamp}.csv", mime="text/csv",
                width="stretch",
            )
        with t3:
            st.write("")
            if summary is not None:
                st.download_button(
                    f"⬇️ {group_choice} summary CSV",
                    data=summary.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"ledger_summary_{stamp}.csv", mime="text/csv",
                )
        visible_rows = TABLE_HEIGHTS[height_choice]

        by_cur = stl.totals_by_currency(entries)
        if len(by_cur) > 1:
            with st.expander("Breakdown by currency"):
                st.dataframe(
                    by_cur, hide_index=True, width="stretch",
                    height=table_height(len(by_cur), None),
                    column_config={
                        **{c: plain_col(c) for c in ("Income", "Expenses", "Net")},
                        **{c: base_col(c) for c in by_cur.columns if "(base)" in c},
                    },
                )

        if summary is not None:
            st.markdown(f"**Grouped by {group_choice.lower()}** — {len(summary)} groups")
            money_summary = [c for c in summary.columns if c not in
                             ("Type", "Category", "Entries")]
            st.dataframe(
                summary, hide_index=True, width="stretch",
                height=table_height(len(summary), None),
                column_config={
                    "Entries": st.column_config.NumberColumn("Entries", width="small"),
                    **{c: base_col(c) for c in money_summary},
                },
            )
            st.caption(
                f"Subtotals in **{BASE}**. A partner's column is their share of the "
                "group's income minus their share of its expenses, so it carries "
                "the same sign as **Net** — negative means money went out."
            )
            st.divider()

        st.markdown(f"**Transactions** — {len(view)} rows")
        share_cols = [c for c in view.columns if c.endswith(" share")]
        selection = st.dataframe(
            view, hide_index=True, width="stretch",
            height=table_height(len(view), visible_rows),
            on_select="rerun", selection_mode="single-row",
            column_config={
                "ID": st.column_config.NumberColumn("ID", width="small"),
                "Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
                "Cur": st.column_config.TextColumn("Cur", width="small"),
                "Amount": plain_col("Amount"),
                f"Amount ({BASE})": base_col(f"Amount ({BASE})"),
                **{c: plain_col(c) for c in share_cols},
            },
        )
        st.caption(
            f"`Amount` and the partner share columns are in each row's own currency "
            f"(`Cur`); `Amount ({BASE})` is the converted figure."
            + ("" if visible_rows is None or len(view) <= visible_rows else
               f"  ·  Showing {visible_rows} of {len(view)} rows — scroll inside the "
               "table, or switch **Table height** to *Show all rows*.")
        )

        rows = list((selection or {}).get("selection", {}).get("rows", []))
        if rows:
            sel_id = int(view.iloc[rows[0]]["ID"])
            record = db.get_entry(sel_id)
            if record:
                existing, existing_split = record
                st.divider()
                st.subheader(f"Edit entry #{sel_id}")
                edited = entry_editor(
                    f"edit{sel_id}", partners, existing=existing,
                    existing_split=existing_split,
                )
                if edited:
                    try:
                        db.save_entry(entry_id=sel_id, **edited)
                        flash(f"Entry #{sel_id} updated.")
                        bump(f"edit{sel_id}")
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))

                if st.checkbox("I want to delete this entry", key=f"delchk_{sel_id}"):
                    if st.button("🗑️ Delete permanently", key=f"del_{sel_id}"):
                        db.delete_entry(sel_id)
                        flash(f"Entry #{sel_id} deleted.")
                        st.rerun()
        else:
            st.caption("Select a row to edit or delete it.")

        # ---- bulk re-apply of the current ratios -------------------------- #
        st.divider()
        with st.expander("🔄 Reapply current ratios to existing transactions"):
            st.caption(
                "Each entry stores the split it was saved with, so changing a "
                "ratio in Setup normally leaves history alone. Use this when you "
                "have corrected a ratio and want it applied to entries you "
                "already recorded."
            )
            b1, b2 = st.columns([3, 2])
            with b1:
                scope = st.radio(
                    "Which transactions",
                    ["Only the ones in this view", "Every transaction"],
                    key="bulk_scope",
                )
            with b2:
                include_custom = st.checkbox(
                    "Also overwrite custom splits", key="bulk_custom",
                    help="Off by default: a custom split is a deliberate override. "
                         "Turning this on converts those entries back to the ratio.",
                )
                st.caption(
                    "*Not shared* entries are never touched — the payer always "
                    "bears 100% of them."
                )

            pool = entries if scope == "Only the ones in this view" else db.get_entries()
            modes = ("ratio", "custom") if include_custom else ("ratio",)
            eligible = pool[pool["share_mode"].isin(modes)]
            eligible_ids = [int(i) for i in eligible["id"]]

            select_all = st.checkbox(
                f"Select all {len(eligible_ids)} eligible transactions",
                value=True, key="bulk_all",
            )
            if select_all:
                chosen = eligible_ids
            else:
                chosen = st.multiselect(
                    "Pick the transactions to update", eligible_ids,
                    default=[], key="bulk_pick",
                    format_func=lambda i: (
                        f"#{i} · {pool.loc[pool['id'] == i, 'entry_date'].iloc[0]}"
                        f" · {pool.loc[pool['id'] == i, 'category'].iloc[0]}"
                    ),
                )

            if not chosen:
                st.info("Nothing selected.")
            else:
                changes = db.ratio_refresh_preview(chosen, include_custom)
                if changes.empty:
                    st.success(
                        f"✅ All {len(chosen)} selected transactions already match "
                        "the current ratios — nothing to do."
                    )
                else:
                    st.warning(
                        f"{len(changes)} of the {len(chosen)} selected transactions "
                        "would change:"
                    )
                    st.dataframe(
                        changes, hide_index=True, width="stretch",
                        height=table_height(len(changes), 10),
                        column_config={
                            "ID": st.column_config.NumberColumn("ID", width="small"),
                            "Date": st.column_config.DateColumn(
                                "Date", format="DD/MM/YYYY"),
                            "Cur": st.column_config.TextColumn("Cur", width="small"),
                            "Amount": plain_col("Amount"),
                            "Sharing": st.column_config.TextColumn("Was"),
                        },
                    )
                    st.caption(
                        "This rewrites the stored split on those entries and will "
                        "move the settlement balances. It cannot be undone — keep a "
                        "copy of the data file first if you are unsure."
                    )
                    confirmed = st.checkbox(
                        f"Yes, rewrite the split on these {len(changes)} transactions",
                        key="bulk_confirm",
                    )
                    if st.button("🔄 Reapply ratios", type="primary",
                                 disabled=not confirmed):
                        updated = db.apply_ratio_refresh(chosen, include_custom)
                        flash(f"Reapplied the current ratios to {updated} "
                              f"transaction{'' if updated == 1 else 's'}.")
                        st.rerun()

# --------------------------------------------------------------------------- #
# tab: settlement
# --------------------------------------------------------------------------- #
with tab_settle:
    all_entries = db.get_entries()
    all_splits = db.get_splits()
    settlements = db.get_settlements()
    all_partners = db.get_partners(include_inactive=True)

    positions = stl.compute_positions(all_entries, all_splits, all_partners, settlements)
    active_or_used = positions[
        positions["partner_id"].isin(partners["id"].astype(int))
        | (positions[["cash_paid", "expense_burden", "cash_received",
                      "income_share", "settled_out", "settled_in"]].abs().sum(axis=1) > 0)
    ]
    transfers = stl.minimal_transfers(active_or_used)

    st.subheader(f"Where everyone stands — in {BASE}")
    display = active_or_used.rename(columns={
        "partner": "Party",
        "cash_paid": "Cash paid out",
        "expense_burden": "Share of expenses",
        "cash_received": "Cash received",
        "income_share": "Share of income",
        "settled_out": "Settled (paid)",
        "settled_in": "Settled (received)",
        "net": "Net position",
    }).drop(columns=["partner_id", "is_firm"])
    st.dataframe(
        display, hide_index=True, width="stretch",
        column_config={c: base_col(c) for c in display.columns if c != "Party"},
    )
    st.caption(
        f"All figures converted to **{BASE}** at the rates in Setup. "
        "**Net position** — positive means the party is owed money, negative means "
        "they owe. Not-shared entries wash out: the payer bears 100% of them."
    )

    st.subheader("Who pays whom to square up")
    if not transfers:
        st.success("✅ All square — nothing to settle.")
    else:
        for idx, t in enumerate(transfers):
            c1, c2 = st.columns([4, 1])
            c1.markdown(f"**{t['from']}** pays **{t['to']}** → {money(t['amount'])}")
            if c2.button("Record it", key=f"rec_{idx}"):
                st.session_state["prefill_settlement"] = t
                st.rerun()

    # ---- export ----------------------------------------------------------- #
    frames = exports.build_frames(
        positions=active_or_used, transfers=transfers, settlements=settlements,
        entries=all_entries, currencies=db.get_currencies(), base=BASE,
        business_name=BUSINESS, partners=all_partners,
    )
    st.markdown("**Export this settlement**")
    e1, e2, e3 = st.columns([2, 2, 4])
    stamp = date.today().isoformat()
    with e1:
        st.download_button(
            "⬇️ Excel workbook",
            data=exports.to_excel(frames),
            file_name=f"settlement_{stamp}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )
    with e2:
        st.download_button(
            "⬇️ CSV",
            data=exports.to_csv(frames),
            file_name=f"settlement_{stamp}.csv",
            mime="text/csv",
        )
    with e3:
        st.caption(
            "Sheets: " + ", ".join(frames) + ". The CSV holds the same sections "
            "one after another under `##` headers."
        )

    st.divider()
    st.subheader("Record a settlement payment")
    prefill = st.session_state.get("prefill_settlement")
    party_ids = [int(i) for i in all_partners["id"]]
    party_names = dict(zip(all_partners["id"].astype(int), all_partners["name"]))
    name_to_id = {v: k for k, v in party_names.items()}

    s1, s2, s3, s4, s5 = st.columns([3, 3, 2, 2, 2])
    from_default = name_to_id.get((prefill or {}).get("from"), party_ids[0])
    to_default = name_to_id.get(
        (prefill or {}).get("to"), party_ids[1] if len(party_ids) > 1 else party_ids[0]
    )
    with s1:
        from_id = st.selectbox(
            "Paid by", party_ids, format_func=lambda i: party_names[i],
            index=party_ids.index(from_default), key="set_from",
        )
    with s2:
        to_id = st.selectbox(
            "Paid to", party_ids, format_func=lambda i: party_names[i],
            index=party_ids.index(to_default), key="set_to",
        )
    with s3:
        set_amount = st.number_input(
            "Amount", min_value=0.0, step=100.0, format="%.2f",
            value=float((prefill or {}).get("amount", 0.0)), key="set_amt",
        )
    with s4:
        set_cur = st.selectbox("Currency", list(RATES), key="set_cur")
    with s5:
        paid_on = st.date_input("Date", value=date.today(), format="DD/MM/YYYY",
                                key="set_date")
    set_note = st.text_input("Note (optional)", key="set_note")
    if set_cur != BASE and set_amount > 0:
        st.caption(
            f"{money(set_amount, set_cur)} counts as "
            f"{money(set_amount * RATES.get(set_cur, 1.0))} against the balances."
        )

    if st.button("🤝 Record settlement", type="primary"):
        try:
            db.add_settlement(paid_on, from_id, to_id, set_amount, set_note, set_cur)
            st.session_state.pop("prefill_settlement", None)
            flash("Settlement recorded.")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    if not settlements.empty:
        st.subheader("Settlement history")
        hist = settlements.rename(columns={
            "id": "ID", "paid_on": "Date", "from_name": "Paid by",
            "to_name": "Paid to", "amount": "Amount", "currency": "Cur",
            "base_amount": f"Amount ({BASE})", "note": "Note",
        })[["ID", "Date", "Paid by", "Paid to", "Amount", "Cur",
            f"Amount ({BASE})", "Note"]]
        st.dataframe(
            hist, hide_index=True, width="stretch",
            height=table_height(len(hist), 10),
            column_config={
                "Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
                "Cur": st.column_config.TextColumn("Cur", width="small"),
                "Amount": plain_col("Amount"),
                f"Amount ({BASE})": base_col(f"Amount ({BASE})"),
            },
        )
        h1, h2 = st.columns([1, 3])
        with h1:
            kill = st.selectbox("Remove settlement #", [None] + hist["ID"].tolist(),
                                key="set_kill")
        with h2:
            st.write("")
            if kill and st.button("🗑️ Remove", key="set_kill_btn"):
                db.delete_settlement(int(kill))
                flash(f"Settlement #{kill} removed.")
                st.rerun()

# --------------------------------------------------------------------------- #
# the three statements
# --------------------------------------------------------------------------- #
def statements_download(trial, trial_totals, pl, sheet, period_label, as_at,
                        key: str) -> None:
    """One button per format, offering all three statements together."""
    frames = exports.statement_frames(
        trial=trial, trial_totals=trial_totals, pl=pl, sheet=sheet,
        base=BASE, business_name=BUSINESS, period_label=period_label, as_at=as_at,
    )
    d1, d2, d3 = st.columns([2, 2, 4])
    with d1:
        st.download_button(
            "⬇️ All statements (Excel)", data=exports.to_excel(frames),
            file_name=f"statements_{as_at}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{key}_xlsx", type="primary",
        )
    with d2:
        st.download_button(
            "⬇️ All statements (CSV)", data=exports.to_csv(frames),
            file_name=f"statements_{as_at}.csv", mime="text/csv", key=f"{key}_csv",
        )
    with d3:
        st.caption("Sheets: " + ", ".join(frames))


with tab_trial:
    st.subheader("Trial Balance")
    t_from, t_to, t_label = statement_period("tb")
    _, t_cumulative, t_positions = statement_data(None, t_to)
    trial = fin.trial_balance(t_cumulative, t_positions)
    t_totals = fin.trial_balance_totals(trial)
    t_pl = fin.profit_and_loss(t_cumulative, t_positions)
    t_sheet = fin.balance_sheet(t_positions)

    st.caption(
        f"As at **{t_to.strftime('%d/%m/%Y')}**, in **{BASE}**. A trial balance is "
        "cumulative, so this covers everything recorded up to that date whatever "
        "period is selected above. Nil accounts are omitted."
    )
    k1, k2, k3 = st.columns(3)
    k1.metric("Total debits", money(t_totals["debit"]))
    k2.metric("Total credits", money(t_totals["credit"]))
    k3.metric("Difference", money(t_totals["difference"]))

    if abs(t_totals["difference"]) > 0.005:
        st.error(
            f"The trial balance is out by {money(t_totals['difference'])}. That "
            "should be impossible — please report it."
        )
    elif trial.empty:
        st.info("Nothing recorded up to this date yet.")
    else:
        st.success("✅ Debits equal credits.")

    if not trial.empty:
        shown = pd.concat(
            [trial, pd.DataFrame([["Total", "", t_totals["debit"],
                                   t_totals["credit"]]],
                                 columns=fin.TRIAL_COLUMNS)],
            ignore_index=True,
        )
        statement_table(shown, ["Debit", "Credit"],
                        Account=st.column_config.TextColumn("Account", width="large"))
        st.caption(
            "Partner current accounts here show **cash put in less cash taken "
            "out** — the profit is still sitting in the income and expense "
            "accounts above. The Balance Sheet shows them after the profit is "
            "allocated."
        )
        statements_download(trial, t_totals, t_pl, t_sheet, t_label, t_to, "tb")

with tab_pl:
    st.subheader("Profit & Loss")
    p_from, p_to, p_label = statement_period("pl")
    p_entries, p_cumulative, p_positions = statement_data(p_from, p_to)
    # the appropriation must reflect the period's own income and expenses, so it
    # is computed from the period entries rather than the cumulative positions
    period_positions = stl.compute_positions(
        p_entries,
        db.get_splits(p_entries["id"].astype(int).tolist()
                      if not p_entries.empty else []),
        db.get_partners(include_inactive=True),
        db.get_settlements().iloc[0:0],
    )
    p_pl = fin.profit_and_loss(p_entries, period_positions)
    st.caption(f"Covering **{p_label}**, in **{BASE}**.")

    k1, k2, k3 = st.columns(3)
    k1.metric("Income", money(p_pl["totals"]["income"]))
    k2.metric("Expenses", money(p_pl["totals"]["expense"]))
    k3.metric(
        "Net profit" if p_pl["totals"]["net"] >= 0 else "Net loss",
        money(abs(p_pl["totals"]["net"])),
    )

    if p_entries.empty:
        st.info("No entries in this period.")
    else:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**Income** — {money(p_pl['totals']['income'])}")
            if p_pl["income"].empty:
                st.caption("No income in this period.")
            else:
                statement_table(p_pl["income"], ["Amount"])
        with c2:
            st.markdown(f"**Expenses** — {money(p_pl['totals']['expense'])}")
            if p_pl["expenses"].empty:
                st.caption("No expenses in this period.")
            else:
                statement_table(p_pl["expenses"], ["Amount"])

        result = p_pl["totals"]["net"]
        st.markdown(
            f"### {'Net profit' if result >= 0 else 'Net loss'}: {money(abs(result))}"
        )

        st.markdown("**How the result splits between the partners**")
        statement_table(
            p_pl["appropriation"],
            ["Share of income", "Share of expenses", "Profit / (loss)"],
        )
        st.caption(
            "Income is divided on the **profit-sharing** ratio and expenses on the "
            "**expense-sharing** ratio, so a partner's result is their share of "
            "income less their share of costs. The column adds back to the net "
            f"figure above ({money(result)})."
        )

        p_trial = fin.trial_balance(p_cumulative, p_positions)
        statements_download(p_trial, fin.trial_balance_totals(p_trial),
                            p_pl, fin.balance_sheet(p_positions), p_label, p_to, "pl")

with tab_bs:
    st.subheader("Balance Sheet")
    b_from, b_to, b_label = statement_period("bs")
    _, b_cumulative, b_positions = statement_data(None, b_to)
    b_sheet = fin.balance_sheet(b_positions)
    b_pl = fin.profit_and_loss(b_cumulative, b_positions)

    st.caption(f"As at **{b_to.strftime('%d/%m/%Y')}**, in **{BASE}**.")
    st.warning(
        "This covers only what the app records — cash that moved through the firm "
        "account and the partners' current accounts. It does **not** include "
        "stock, equipment, loans, or money owed by or to anyone outside the "
        "partnership, because there is nowhere to record those yet."
    )

    k1, k2, k3 = st.columns(3)
    k1.metric("Total assets", money(b_sheet["totals"]["assets"]))
    k2.metric("Capital & liabilities", money(b_sheet["totals"]["funding"]))
    k3.metric("Difference", money(b_sheet["totals"]["difference"]))

    if abs(b_sheet["totals"]["difference"]) > 0.005:
        st.error(
            f"The balance sheet is out by {money(b_sheet['totals']['difference'])}. "
            "That should be impossible — please report it."
        )
    elif not b_sheet["assets"].empty or not b_sheet["funding"].empty:
        st.success("✅ Assets equal capital and liabilities.")

    if b_sheet["assets"].empty and b_sheet["funding"].empty:
        st.info("Nothing recorded up to this date yet.")
    else:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**Assets** — {money(b_sheet['totals']['assets'])}")
            if b_sheet["assets"].empty:
                st.caption("None.")
            else:
                statement_table(b_sheet["assets"], ["Amount"])
        with c2:
            st.markdown(
                f"**Capital & liabilities** — {money(b_sheet['totals']['funding'])}"
            )
            if b_sheet["funding"].empty:
                st.caption("None.")
            else:
                statement_table(b_sheet["funding"], ["Amount"])
        st.caption(
            "A partner's current account is what they have funded plus their share "
            "of the profit, less anything they have taken out or been settled. "
            "A partner who owes the business appears on the asset side as "
            "*Due from …* rather than as negative capital. Cumulative profit to "
            f"date: {money(b_pl['totals']['net'])}."
        )

        b_trial = fin.trial_balance(b_cumulative, b_positions)
        statements_download(b_trial, fin.trial_balance_totals(b_trial),
                            b_pl, b_sheet, b_label, b_to, "bs")

# --------------------------------------------------------------------------- #
# tab: setup
# --------------------------------------------------------------------------- #
with tab_setup:
    st.subheader("Appearance")
    # Streamlit owns the theme - there is no API to set it from the script, so
    # the switcher in its own menu is the only real control. All this does is
    # say where it lives and what is currently in force, because the menu is
    # easy to miss.
    current = (st.context.theme or {}).get("type") or "system"
    st.caption(
        f"This screen is in **{current}** mode. To change it, open the "
        "**☰ menu** at the top right → **Settings** → **Theme**, and pick "
        "*Light*, *Dark* or *Use system setting*. The choice is remembered in "
        "your browser, so each of you can have it your own way."
    )

    st.divider()
    st.subheader("Business")
    g1, g2 = st.columns(2)
    with g1:
        new_name = st.text_input("Business name", value=BUSINESS)
    with g2:
        new_fy = st.selectbox(
            "Financial year starts", list(range(1, 13)),
            index=FY_START_MONTH - 1, format_func=lambda m: calendar.month_name[m],
        )
    if st.button("Save business settings"):
        db.set_setting("business_name", new_name.strip() or "My Partnership")
        db.set_setting("fy_start_month", str(new_fy))
        flash("Saved.")
        st.rerun()

    st.divider()
    st.subheader("Currencies & exchange rates")
    st.caption(
        f"Entries are stored in the currency you enter them in. Everything is "
        f"converted to the base currency (**{BASE}**) for the settlement, using "
        "these rates live — change a rate and the whole history re-converts."
    )
    currencies = db.get_currencies()
    cur_edit = st.data_editor(
        currencies[["code", "symbol", "rate_to_base"]].copy(),
        hide_index=True, width="stretch", key="cur_editor", disabled=["code"],
        column_config={
            "code": st.column_config.TextColumn("Code", width="small"),
            "symbol": st.column_config.TextColumn("Symbol", width="small"),
            "rate_to_base": st.column_config.NumberColumn(
                f"1 unit = ? {BASE}", min_value=0.000001, step=0.25, format="%.6g",
                help=f"How many {BASE} one unit of this currency is worth. "
                     f"{BASE} itself is always 1.",
            ),
        },
    )
    cu1, cu2 = st.columns([1, 3])
    with cu1:
        if st.button("Save rates", type="primary"):
            try:
                for row in cur_edit.itertuples():
                    db.upsert_currency(row.code, row.symbol, float(row.rate_to_base))
                flash("Rates updated.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    with cu2:
        counts = {c: db.currency_usage(c) for c in currencies["code"]}
        st.caption(
            "Entries using each currency — "
            + ", ".join(f"{c}: {n}" for c, n in counts.items())
        )

    st.markdown("**Which currency the entry form starts on**")
    d1, d2, d3 = st.columns([2, 2, 4])
    codes_now = list(currencies["code"])
    picks = {}
    for col, kind in ((d1, "expense"), (d2, "income")):
        current = DEFAULT_CURRENCY[kind]
        with col:
            picks[kind] = st.selectbox(
                f"Default for {kind}s", codes_now,
                index=codes_now.index(current) if current in codes_now else 0,
                key=f"defcur_{kind}",
            )
    with d3:
        st.write("")
        if st.button("Save default currencies"):
            try:
                for kind, code in picks.items():
                    db.set_default_currency(kind, code)
                flash("Default currencies saved.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        st.caption(
            "Only pre-selects the dropdown — you can still switch currency on any "
            "individual entry."
        )

    with st.expander("Add a currency, or change the base currency"):
        a1, a2, a3, a4 = st.columns([1, 1, 1, 1])
        with a1:
            add_code = st.text_input("Code", placeholder="AED", key="add_code")
        with a2:
            add_sym = st.text_input("Symbol", placeholder="AED", key="add_sym")
        with a3:
            add_rate = st.number_input(f"1 unit = ? {BASE}", min_value=0.000001,
                                       value=1.0, step=0.25, key="add_rate")
        with a4:
            st.write("")
            if st.button("Add currency") and add_code.strip():
                try:
                    db.upsert_currency(add_code, add_sym or add_code, add_rate)
                    flash(f"Added {add_code.strip().upper()}.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

        b1, b2 = st.columns([2, 2])
        codes = list(currencies["code"])
        with b1:
            new_base = st.selectbox(
                "Base currency", codes,
                index=codes.index(BASE) if BASE in codes else 0, key="base_pick",
            )
            if new_base != BASE:
                st.caption(
                    f"Switching to {new_base} re-expresses every rate against it, "
                    "so relative values are preserved."
                )
            if st.button("Set as base currency") and new_base != BASE:
                try:
                    db.set_base_currency(new_base)
                    flash(f"Base currency is now {new_base}.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        with b2:
            removable = [c for c in codes if c != BASE]
            victim_cur = st.selectbox("Remove currency", [None] + removable,
                                      key="rm_cur")
            if victim_cur:
                used = db.currency_usage(victim_cur)
                if used:
                    st.info(f"{victim_cur} is used by {used} record(s) — "
                            "it cannot be removed.")
                elif st.button("🗑️ Remove currency"):
                    try:
                        db.delete_currency(victim_cur)
                        flash(f"{victim_cur} removed.")
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))

    st.divider()
    st.subheader("Partners & sharing ratios")
    st.caption(
        "**Expense %** splits expenses, **Profit %** splits income — they do not "
        "have to match. Each column should total 100% on its own. The firm row is "
        "the business bank account: it moves cash but never carries a share."
    )
    editable = db.get_partners(include_inactive=True)[
        ["id", "name", "expense_pct", "profit_pct", "is_firm", "active"]
    ].copy()
    editable["active"] = editable["active"].astype(bool)
    editable["is_firm"] = editable["is_firm"].astype(bool)
    edited_partners = st.data_editor(
        editable, hide_index=True, width="stretch", key="partner_editor",
        disabled=["id", "is_firm"],
        column_config={
            "id": st.column_config.NumberColumn("ID", width="small"),
            "name": st.column_config.TextColumn("Name", required=True),
            "expense_pct": st.column_config.NumberColumn(
                "Expense %", min_value=0.0, max_value=100.0, step=1.0, format="%.2f",
                help="Share of every ratio-based expense this partner bears.",
            ),
            "profit_pct": st.column_config.NumberColumn(
                "Profit %", min_value=0.0, max_value=100.0, step=1.0, format="%.2f",
                help="Share of every ratio-based income this partner earns.",
            ),
            "is_firm": st.column_config.CheckboxColumn("Firm account?"),
            "active": st.column_config.CheckboxColumn("Active"),
        },
    )
    p1, p2, p3 = st.columns([2, 1, 1])
    with p1:
        if st.button("Save partners", type="primary"):
            for row in edited_partners.itertuples():
                db.update_partner(
                    int(row.id), row.name,
                    0.0 if row.is_firm else float(row.expense_pct),
                    0.0 if row.is_firm else float(row.profit_pct),
                    bool(row.active),
                )
            flash("Partners updated.")
            st.rerun()
    non_firm = edited_partners.loc[~edited_partners["is_firm"]]
    for col, (column, label) in zip(
        (p2, p3), (("expense_pct", "Expense %"), ("profit_pct", "Profit %")),
    ):
        total = float(non_firm[column].sum())
        with col:
            st.metric(f"{label} total", f"{total:g}%",
                      delta=None if abs(total - 100) < 0.01 else f"{total - 100:+g}%")
    st.caption(
        "Changing a ratio only affects entries you add from now on — saved entries "
        "keep the split they were recorded with. To push a corrected ratio onto "
        "entries you already recorded, use **Ledger → Reapply current ratios to "
        "existing transactions**."
    )

    with st.expander("Add or remove a partner"):
        a1, a2, a3, a4 = st.columns([2, 1, 1, 1])
        with a1:
            add_name = st.text_input("New partner name", key="add_pname")
        with a2:
            add_exp = st.number_input("Expense %", min_value=0.0, max_value=100.0,
                                      step=1.0, key="add_pexp")
        with a3:
            add_prof = st.number_input("Profit %", min_value=0.0, max_value=100.0,
                                       step=1.0, key="add_pprof")
        with a4:
            st.write("")
            if st.button("Add partner") and add_name.strip():
                db.add_partner(add_name, add_exp, add_prof)
                flash(f"Added {add_name.strip()}.")
                st.rerun()

        removable = db.get_partners(include_inactive=True)
        removable = removable[removable["is_firm"] == 0]
        r1, r2 = st.columns([2, 2])
        with r1:
            victim = st.selectbox(
                "Remove partner", [None] + [int(i) for i in removable["id"]],
                format_func=lambda i: "—" if i is None else db.partner_names()[i],
                key="rm_partner",
            )
        with r2:
            st.write("")
            if victim:
                used = db.partner_usage(int(victim))
                if used:
                    st.info(
                        f"{db.partner_names()[int(victim)]} appears in {used} record(s). "
                        "Untick **Active** above to hide them instead of deleting."
                    )
                elif st.button("🗑️ Remove partner"):
                    db.delete_partner(int(victim))
                    flash("Partner removed.")
                    st.rerun()

    st.divider()
    st.subheader("Categories")
    for kind in ("expense", "income"):
        st.markdown(f"**{kind.capitalize()} categories**")
        existing = db.get_categories(kind)
        c1, c2, c3 = st.columns([2, 2, 3])
        with c1:
            fresh = st.text_input("Add", key=f"cat_add_{kind}",
                                  label_visibility="collapsed",
                                  placeholder=f"New {kind} category")
        with c2:
            if st.button("Add category", key=f"cat_btn_{kind}") and fresh.strip():
                db.add_category(fresh, kind)
                st.rerun()
        with c3:
            drop = st.selectbox("Remove", [None] + existing, key=f"cat_rm_{kind}",
                                label_visibility="collapsed")
            if drop and st.button(f"Remove '{drop}'", key=f"cat_rmbtn_{kind}"):
                db.delete_category(drop, kind)
                st.rerun()
        st.caption(", ".join(existing) if existing else "none yet")

    st.divider()
    st.subheader("Backup & restore")
    st.caption(
        "A backup is every table in one small compressed file. Download it to "
        "keep a copy off the server, or upload one to move your books to "
        "another machine — it works the same whether the data sits in a local "
        "SQLite file or a hosted Postgres."
    )
    bk1, bk2 = st.columns([2, 3])
    with bk1:
        st.download_button(
            "⬇️ Download a backup",
            data=db.backup_bytes(),
            file_name=f"partnership-books-{date.today().isoformat()}.books.gz",
            mime="application/gzip", type="primary",
        )
    with bk2:
        st.caption(
            f"Currently {len(db.get_entries())} entries and "
            f"{len(db.get_settlements())} settlement(s)."
        )

    with st.expander("Restore from a backup"):
        st.warning(
            "Restoring **replaces everything** currently in the app. The books "
            "being replaced are copied aside first, next to the data file."
        )
        # some browsers strip the .gz when they decompress on download, so
        # accept the bare name too
        uploaded = st.file_uploader("Backup file (.books.gz)",
                                    type=["gz", "books", "json"],
                                    key="restore_file")
        if uploaded is not None:
            raw = uploaded.getvalue()
            try:
                counts = db.describe_backup(raw)
                st.info(
                    f"That file holds {counts['entries']} entries, "
                    f"{counts['settlements']} settlement(s) and "
                    f"{counts['partners']} partners."
                )
                if st.checkbox("Yes, replace the current books with this file",
                               key="restore_confirm"):
                    if st.button("♻️ Restore now", type="primary"):
                        restored = db.restore_bytes(raw)
                        flash(
                            f"Restored {restored['entries']} entries and "
                            f"{restored['settlements']} settlement(s)."
                        )
                        st.rerun()
            except ValueError as exc:
                st.error(str(exc))

    gate = _configured_password()
    if gate and len(gate) < MIN_PASSWORD:
        st.warning(
            f"The shared password is only {len(gate)} characters. This app is "
            f"reachable by anyone who has the URL, so use at least "
            f"{MIN_PASSWORD} — ideally a random string. Change it in the app's "
            "secrets and reboot."
        )

    backend = "Postgres" if not db.is_sqlite() else f"SQLite at `{db.DB_PATH}`"
    st.caption(
        f"Storage: {backend}"
        + ("  ·  password protection is **on**." if gate
           else "  ·  no password set — fine locally, set `BOOKS_PASSWORD` "
                "before putting this on a server.")
    )
