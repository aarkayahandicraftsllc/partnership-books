"""Settlement maths: who is out of pocket, and who should pay whom.

Entries may be in any currency. Every figure in this module is in the **base
currency**, converted through ``base_amount`` / ``split_base``, so a single net
position per party can be settled with one payment.

For each party (partner or the firm account):

    net = expenses whose cash they paid
        - their share of the expense burden
        + their share of the income
        - income that landed in their pocket
        + settlements they have already paid out
        - settlements they have already received

A positive ``net`` means the party is owed money; negative means they owe.
Because every entry's split percentages total 100, the column sums to zero.
"""

from __future__ import annotations

import math

import pandas as pd

EPS = 0.005  # half a paisa / cent - anything smaller is a rounding artefact

POSITION_COLUMNS = [
    "partner_id", "partner", "is_firm",
    "cash_paid", "expense_burden", "cash_received", "income_share",
    "settled_out", "settled_in", "net",
]


CARRY = ["id", "kind", "entry_date", "category", "description",
         "amount", "currency", "base_amount"]


def allocate(total: float, percentages: list[float]) -> list[float]:
    """Split `total` by `percentages` into 2-decimal parts that sum to it exactly.

    Rounding each share independently can drift: ₹3,124.55 halved gives
    ₹1,562.275 twice, which rounds up to ₹3,124.56 and silently breaks the
    zero-sum guarantee the settlement rests on. So the shares are computed in
    whole paise/cents and the leftover is handed to the largest fractional
    remainders (the "largest remainder" method). Works for negative totals too.
    """
    if not percentages:
        return []
    units = int(round(total * 100))
    raw = [units * pct / 100.0 for pct in percentages]
    floors = [math.floor(value) for value in raw]
    leftover = units - sum(floors)
    # ties break towards the earlier partner, which keeps results reproducible
    order = sorted(range(len(raw)), key=lambda i: (floors[i] - raw[i], i))
    for position in range(leftover):
        floors[order[position % len(order)]] += 1
    return [value / 100.0 for value in floors]


def per_partner_amounts(entries: pd.DataFrame, splits: pd.DataFrame) -> pd.DataFrame:
    """Explode entries into one row per (entry, partner) with that partner's share.

    ``split_amount`` is in the entry's own currency; ``split_base`` is the same
    share converted to the base currency. Within each entry both columns sum to
    the entry's amount to the paise.
    """
    if entries.empty or splits.empty:
        return pd.DataFrame(
            columns=["entry_id", "partner_id", "pct", *CARRY[1:],
                     "split_amount", "split_base"]
        )
    merged = splits.merge(
        entries[CARRY], left_on="entry_id", right_on="id", how="inner"
    ).drop(columns=["id"]).sort_values(["entry_id", "partner_id"]).reset_index(drop=True)

    for source, target in (("amount", "split_amount"), ("base_amount", "split_base")):
        merged[target] = 0.0
        for _, rows in merged.groupby("entry_id", sort=False):
            shares = allocate(float(rows[source].iloc[0]), list(rows["pct"]))
            merged.loc[rows.index, target] = shares
    return merged


def compute_positions(
    entries: pd.DataFrame,
    splits: pd.DataFrame,
    partners: pd.DataFrame,
    settlements: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return one row per party with the cash/burden breakdown and net position."""
    positions = pd.DataFrame({
        "partner_id": partners["id"].astype(int),
        "partner": partners["name"],
        "is_firm": partners["is_firm"].astype(bool),
    }).reset_index(drop=True)

    for col in ("cash_paid", "expense_burden", "cash_received", "income_share",
                "settled_out", "settled_in"):
        positions[col] = 0.0

    def _add(column: str, series: pd.Series) -> None:
        mapped = positions["partner_id"].map(series).fillna(0.0)
        positions[column] = positions[column] + mapped.astype(float)

    if not entries.empty:
        expenses = entries[entries["kind"] == "expense"]
        income = entries[entries["kind"] == "income"]
        _add("cash_paid", expenses.groupby("cash_partner_id")["base_amount"].sum())
        _add("cash_received", income.groupby("cash_partner_id")["base_amount"].sum())

        exploded = per_partner_amounts(entries, splits)
        if not exploded.empty:
            _add("expense_burden",
                 exploded[exploded["kind"] == "expense"]
                 .groupby("partner_id")["split_base"].sum())
            _add("income_share",
                 exploded[exploded["kind"] == "income"]
                 .groupby("partner_id")["split_base"].sum())

    if settlements is not None and not settlements.empty:
        _add("settled_out", settlements.groupby("from_id")["base_amount"].sum())
        _add("settled_in", settlements.groupby("to_id")["base_amount"].sum())

    positions["net"] = (
        positions["cash_paid"] - positions["expense_burden"]
        + positions["income_share"] - positions["cash_received"]
        + positions["settled_out"] - positions["settled_in"]
    ).round(2)

    return positions[POSITION_COLUMNS]


def minimal_transfers(positions: pd.DataFrame) -> list[dict]:
    """Greedy largest-debtor-to-largest-creditor matching.

    Produces at most (parties - 1) transfers, which is the minimum possible
    count for all but a few contrived balance sets.
    """
    creditors = [
        [row.partner, round(float(row.net), 2)]
        for row in positions.itertuples()
        if float(row.net) > EPS
    ]
    debtors = [
        [row.partner, round(-float(row.net), 2)]
        for row in positions.itertuples()
        if float(row.net) < -EPS
    ]
    creditors.sort(key=lambda x: -x[1])
    debtors.sort(key=lambda x: -x[1])

    transfers: list[dict] = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        amount = round(min(debtors[i][1], creditors[j][1]), 2)
        if amount > EPS:
            transfers.append({
                "from": debtors[i][0],
                "to": creditors[j][0],
                "amount": amount,
            })
        debtors[i][1] = round(debtors[i][1] - amount, 2)
        creditors[j][1] = round(creditors[j][1] - amount, 2)
        if debtors[i][1] <= EPS:
            i += 1
        if creditors[j][1] <= EPS:
            j += 1
    return transfers


def summary_totals(entries: pd.DataFrame) -> dict[str, float]:
    """Income / expense / net for the period, in base currency."""
    if entries.empty:
        return {"expense": 0.0, "income": 0.0, "net": 0.0}
    totals = entries.groupby("kind")["base_amount"].sum()
    expense = float(totals.get("expense", 0.0))
    income = float(totals.get("income", 0.0))
    return {"expense": expense, "income": income, "net": income - expense}


# what the Ledger's "Group by" choices translate to
GROUP_BY = {
    "None (flat list)": [],
    "Type": ["kind"],
    "Category": ["category"],
    "Type & Category": ["kind", "category"],
}
GROUP_LABELS = {"kind": "Type", "category": "Category"}


def group_totals(entries: pd.DataFrame, splits: pd.DataFrame,
                 partners: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """Subtotals per group, in base currency, with each partner's net share.

    A partner's column is their share of the group's income minus their share
    of its expenses, so it carries the same sign as the group's ``Net``.
    """
    sharing = partners[partners["is_firm"] == 0]
    partner_names = list(sharing["name"])
    columns = (
        [GROUP_LABELS[b] for b in by]
        + ["Entries", "Income", "Expenses", "Net"]
        + [f"{name} net" for name in partner_names]
    )
    if entries.empty or not by:
        return pd.DataFrame(columns=columns)

    exploded = per_partner_amounts(entries, splits)
    rows = []
    for key, group in entries.groupby(by, sort=True):
        key = key if isinstance(key, tuple) else (key,)
        keys = [str(k).capitalize() if col == "kind" else str(k)
                for col, k in zip(by, key)]

        totals = group.groupby("kind")["base_amount"].sum()
        income = float(totals.get("income", 0.0))
        expense = float(totals.get("expense", 0.0))

        mine = (
            exploded[exploded["entry_id"].isin(set(group["id"].astype(int)))]
            if not exploded.empty else exploded
        )
        shares = []
        for partner_id in sharing["id"].astype(int):
            own = mine[mine["partner_id"] == partner_id] if not mine.empty else mine
            if own.empty:
                shares.append(0.0)
                continue
            per_kind = own.groupby("kind")["split_base"].sum()
            shares.append(round(float(per_kind.get("income", 0.0))
                                - float(per_kind.get("expense", 0.0)), 2))

        rows.append([*keys, len(group), round(income, 2), round(expense, 2),
                     round(income - expense, 2), *shares])

    return pd.DataFrame(rows, columns=columns)


def totals_by_currency(entries: pd.DataFrame) -> pd.DataFrame:
    """Income / expense / net per original currency, for reconciliation."""
    cols = ["Currency", "Income", "Expenses", "Net", "Income (base)",
            "Expenses (base)", "Net (base)"]
    if entries.empty:
        return pd.DataFrame(columns=cols)

    rows = []
    for code, grp in entries.groupby("currency", sort=True):
        own = grp.groupby("kind")["amount"].sum()
        base = grp.groupby("kind")["base_amount"].sum()
        inc, exp = float(own.get("income", 0.0)), float(own.get("expense", 0.0))
        binc, bexp = float(base.get("income", 0.0)), float(base.get("expense", 0.0))
        rows.append([code, inc, exp, inc - exp, binc, bexp, binc - bexp])
    return pd.DataFrame(rows, columns=cols)
