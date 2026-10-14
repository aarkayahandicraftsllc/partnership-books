"""Trial balance, profit & loss and balance sheet, derived from the entries.

The app records cash movements and burden splits rather than journal entries,
but those are enough to reconstruct a consistent set of accounts:

* an **expense account** per expense category and an **income account** per
  income category;
* a **firm bank / cash** account, holding money that moved through the business
  account rather than a partner's pocket;
* a **current account** per partner, crediting what they funded and debiting
  what they took out.

Two different partner balances matter, and mixing them up is the easy mistake:

``cash_movement``
    cash the partner put in less cash they took out. This is their current
    account *before* the profit or loss is appropriated, and it is what the
    trial balance uses alongside the open income and expense accounts.

``net`` (from ``settlement.compute_positions``)
    the same thing *after* their share of income and expenses is allocated.
    This is the balance-sheet figure, and the one the settlement screen settles.

The identity ``net = cash_movement - expense_burden + income_share`` ties the
two together, which is why the trial balance and the balance sheet both balance
to the paise.
"""

from __future__ import annotations

import pandas as pd

TRIAL_COLUMNS = ["Account", "Section", "Debit", "Credit"]
EPS = 0.005


def up_to(frame: pd.DataFrame, column: str, date_to) -> pd.DataFrame:
    """Rows dated on or before `date_to` — statements are cumulative 'as at'."""
    if date_to is None or frame.empty:
        return frame
    return frame[frame[column] <= str(date_to)]


def cash_movement(positions: pd.DataFrame) -> pd.Series:
    """Cash a party put into the business, less cash they took out."""
    return (
        positions["cash_paid"] - positions["cash_received"]
        + positions["settled_out"] - positions["settled_in"]
    ).round(2)


def _side(amount: float) -> tuple[float, float]:
    """Return (debit, credit) for a signed amount."""
    return (round(amount, 2), 0.0) if amount >= 0 else (0.0, round(-amount, 2))


def trial_balance(entries: pd.DataFrame, positions: pd.DataFrame) -> pd.DataFrame:
    """Pre-appropriation trial balance. Debits and credits total to the same."""
    rows: list[list] = []

    if not entries.empty:
        expenses = entries[entries["kind"] == "expense"]
        income = entries[entries["kind"] == "income"]
        for category, total in (
            expenses.groupby("category")["base_amount"].sum().sort_index().items()
        ):
            debit, credit = _side(float(total))
            rows.append([f"Expense — {category}", "Expenses", debit, credit])
        for category, total in (
            income.groupby("category")["base_amount"].sum().sort_index().items()
        ):
            # income is a credit, so its sign is flipped going onto the sheet
            debit, credit = _side(-float(total))
            rows.append([f"Income — {category}", "Income", debit, credit])

    moved = cash_movement(positions)
    for position, movement in zip(positions.itertuples(), moved):
        movement = float(movement)
        if abs(movement) <= EPS:
            continue  # a trial balance does not list nil accounts
        debit, credit = _side(-movement)
        if position.is_firm:
            # the firm holds cash, so its asset balance is the mirror image
            rows.append([f"{position.partner} — bank / cash", "Assets",
                         debit, credit])
        else:
            rows.append([f"{position.partner} — current account",
                         "Partner accounts", debit, credit])

    return pd.DataFrame(rows, columns=TRIAL_COLUMNS)


def trial_balance_totals(trial: pd.DataFrame) -> dict[str, float]:
    debit = round(float(trial["Debit"].sum()), 2) if not trial.empty else 0.0
    credit = round(float(trial["Credit"].sum()), 2) if not trial.empty else 0.0
    return {"debit": debit, "credit": credit, "difference": round(debit - credit, 2)}


def profit_and_loss(entries: pd.DataFrame, positions: pd.DataFrame) -> dict:
    """Income and expenses by category, plus how the result splits per partner."""
    def by_category(kind: str) -> pd.DataFrame:
        if entries.empty:
            return pd.DataFrame(columns=["Category", "Amount"])
        rows = entries[entries["kind"] == kind]
        if rows.empty:
            return pd.DataFrame(columns=["Category", "Amount"])
        grouped = rows.groupby("category")["base_amount"].sum().round(2)
        return (
            grouped.sort_values(ascending=False).reset_index()
            .rename(columns={"category": "Category", "base_amount": "Amount"})
        )

    income = by_category("income")
    expenses = by_category("expense")
    total_income = round(float(income["Amount"].sum()), 2)
    total_expense = round(float(expenses["Amount"].sum()), 2)

    partners = positions[~positions["is_firm"].astype(bool)]
    appropriation = pd.DataFrame({
        "Partner": partners["partner"],
        "Share of income": partners["income_share"].round(2),
        "Share of expenses": partners["expense_burden"].round(2),
        "Profit / (loss)": (
            partners["income_share"] - partners["expense_burden"]
        ).round(2),
    }).reset_index(drop=True)

    return {
        "income": income,
        "expenses": expenses,
        "appropriation": appropriation,
        "totals": {
            "income": total_income,
            "expense": total_expense,
            "net": round(total_income - total_expense, 2),
        },
    }


def balance_sheet(positions: pd.DataFrame) -> dict:
    """Post-appropriation position: what the business holds and who it belongs to.

    Only covers what the app records — cash through the firm account and the
    partners' current accounts. It has no way to know about stock, equipment,
    loans or trade debtors.
    """
    assets: list[list] = []
    funding: list[list] = []
    moved = cash_movement(positions)

    for position, movement in zip(positions.itertuples(), moved):
        held = round(-float(movement), 2)          # cash the firm is holding
        net = round(float(position.net), 2)
        if position.is_firm:
            if held > EPS:
                assets.append([f"{position.partner} — bank / cash", held])
            elif held < -EPS:
                funding.append([f"{position.partner} — overdrawn", -held])
        elif net > EPS:
            funding.append([f"{position.partner} — current account", net])
        elif net < -EPS:
            assets.append([f"Due from {position.partner}", -net])

    assets_df = pd.DataFrame(assets, columns=["Item", "Amount"])
    funding_df = pd.DataFrame(funding, columns=["Item", "Amount"])
    total_assets = round(float(assets_df["Amount"].sum()), 2) if assets else 0.0
    total_funding = round(float(funding_df["Amount"].sum()), 2) if funding else 0.0

    return {
        "assets": assets_df,
        "funding": funding_df,
        "totals": {
            "assets": total_assets,
            "funding": total_funding,
            "difference": round(total_assets - total_funding, 2),
        },
    }
