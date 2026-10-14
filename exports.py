"""Builds the downloadable settlement report (Excel workbook or sectioned CSV)."""

from __future__ import annotations

import io
from datetime import date

import pandas as pd
from openpyxl.styles import Font

import settlement as stl

POSITION_LABELS = {
    "partner": "Party",
    "cash_paid": "Cash paid out",
    "expense_burden": "Share of expenses",
    "cash_received": "Cash received",
    "income_share": "Share of income",
    "settled_out": "Settled (paid)",
    "settled_in": "Settled (received)",
    "net": "Net position",
}


def build_frames(
    *,
    positions: pd.DataFrame,
    transfers: list[dict],
    settlements: pd.DataFrame,
    entries: pd.DataFrame,
    currencies: pd.DataFrame,
    base: str,
    business_name: str,
    partners: pd.DataFrame | None = None,
    as_of: date | None = None,
) -> dict[str, pd.DataFrame]:
    """One DataFrame per sheet/section of the report."""
    as_of = as_of or date.today()
    frames: dict[str, pd.DataFrame] = {}

    totals = stl.summary_totals(entries)
    frames["Summary"] = pd.DataFrame(
        [
            ("Business", business_name),
            ("Report date", as_of.isoformat()),
            ("Base currency", base),
            ("Entries covered", len(entries)),
            (f"Total income ({base})", round(totals["income"], 2)),
            (f"Total expenses ({base})", round(totals["expense"], 2)),
            (f"Net profit ({base})", round(totals["net"], 2)),
            ("Outstanding transfers", len(transfers)),
        ],
        columns=["Item", "Value"],
    )

    pos = positions.drop(columns=["partner_id", "is_firm"], errors="ignore").rename(
        columns=POSITION_LABELS
    )
    money_cols = [c for c in pos.columns if c != "Party"]
    pos = pos.rename(columns={c: f"{c} ({base})" for c in money_cols})
    pos["Status"] = positions["net"].map(
        lambda n: "owed" if n > stl.EPS else ("owes" if n < -stl.EPS else "square")
    ).values
    frames["Net positions"] = pos

    frames["Who pays whom"] = (
        pd.DataFrame(transfers).rename(
            columns={"from": "Pays", "to": "Receives", "amount": f"Amount ({base})"}
        )
        if transfers
        else pd.DataFrame(
            [["—", "—", 0.0]], columns=["Pays", "Receives", f"Amount ({base})"]
        )
    )

    frames["Totals by currency"] = stl.totals_by_currency(entries)

    if settlements.empty:
        frames["Settlements paid"] = pd.DataFrame(
            columns=["Date", "Paid by", "Paid to", "Amount", "Currency",
                     f"Amount ({base})", "Note"]
        )
    else:
        frames["Settlements paid"] = settlements.rename(
            columns={
                "paid_on": "Date", "from_name": "Paid by", "to_name": "Paid to",
                "amount": "Amount", "currency": "Currency",
                "base_amount": f"Amount ({base})", "note": "Note",
            }
        )[["Date", "Paid by", "Paid to", "Amount", "Currency",
           f"Amount ({base})", "Note"]]

    rates = currencies.rename(
        columns={"code": "Currency", "symbol": "Symbol",
                 "rate_to_base": f"Units of {base} per 1"}
    )[["Currency", "Symbol", f"Units of {base} per 1"]]
    frames["Exchange rates"] = rates

    if partners is not None and not partners.empty:
        frames["Sharing ratios"] = partners.rename(
            columns={"name": "Partner", "expense_pct": "Expense %",
                     "profit_pct": "Profit %", "is_firm": "Firm account",
                     "active": "Active"}
        )[["Partner", "Expense %", "Profit %", "Firm account", "Active"]].assign(
            **{
                "Firm account": lambda d: d["Firm account"].astype(bool),
                "Active": lambda d: d["Active"].astype(bool),
            }
        )

    if not entries.empty:
        frames["Ledger"] = entries.rename(
            columns={
                "id": "ID", "entry_date": "Date", "kind": "Type",
                "category": "Category", "description": "Description",
                "amount": "Amount", "currency": "Currency",
                "base_amount": f"Amount ({base})", "cash_partner": "Cash by",
                "share_mode": "Sharing",
            }
        )[["ID", "Date", "Type", "Category", "Description", "Amount", "Currency",
           f"Amount ({base})", "Cash by", "Sharing"]]

    return frames


def statement_frames(
    *,
    trial: pd.DataFrame,
    trial_totals: dict,
    pl: dict,
    sheet: dict,
    base: str,
    business_name: str,
    period_label: str,
    as_at,
) -> dict[str, pd.DataFrame]:
    """The three statements as one set of sheets/sections."""
    frames: dict[str, pd.DataFrame] = {
        "Cover": pd.DataFrame(
            [
                ("Business", business_name),
                ("Reporting currency", base),
                ("Profit & loss covers", period_label),
                ("Trial balance / balance sheet as at", str(as_at)),
                ("Total debits", trial_totals["debit"]),
                ("Total credits", trial_totals["credit"]),
                ("Trial balance difference", trial_totals["difference"]),
                ("Net profit / (loss)", pl["totals"]["net"]),
                ("Balance sheet difference", sheet["totals"]["difference"]),
                ("Basis", "Derived from recorded income, expenses and partner "
                          "settlements. Excludes stock, equipment, loans and "
                          "trade debtors/creditors."),
            ],
            columns=["Item", "Value"],
        ),
        "Trial balance": trial,
    }

    income, expenses = pl["income"].copy(), pl["expenses"].copy()
    income.insert(0, "Section", "Income")
    expenses.insert(0, "Section", "Expenses")
    frames["Profit and loss"] = pd.concat(
        [
            income,
            expenses,
            pd.DataFrame([["Total", "Net profit / (loss)", pl["totals"]["net"]]],
                         columns=["Section", "Category", "Amount"]),
        ],
        ignore_index=True,
    )
    frames["Profit appropriation"] = pl["appropriation"]

    assets, funding = sheet["assets"].copy(), sheet["funding"].copy()
    assets.insert(0, "Section", "Assets")
    funding.insert(0, "Section", "Capital & liabilities")
    frames["Balance sheet"] = pd.concat([assets, funding], ignore_index=True)
    return frames


def to_excel(frames: dict[str, pd.DataFrame]) -> bytes:
    """Multi-sheet .xlsx with sensible column widths."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in frames.items():
            sheet = name[:31]
            frame.to_excel(writer, sheet_name=sheet, index=False)
            worksheet = writer.sheets[sheet]
            for idx, column in enumerate(frame.columns, start=1):
                values = frame[column].astype(str)
                width = max([len(str(column))] + [len(v) for v in values]) + 3
                worksheet.column_dimensions[
                    worksheet.cell(row=1, column=idx).column_letter
                ].width = min(width, 46)
                worksheet.cell(row=1, column=idx).font = Font(bold=True)
            worksheet.freeze_panes = "A2"
    return buffer.getvalue()


def to_csv(frames: dict[str, pd.DataFrame]) -> bytes:
    """All sections in one CSV, each under a '## Section name' header row."""
    out = io.StringIO()
    for name, frame in frames.items():
        out.write(f"## {name}\n")
        frame.to_csv(out, index=False, lineterminator="\n")
        out.write("\n")
    return out.getvalue().encode("utf-8-sig")
