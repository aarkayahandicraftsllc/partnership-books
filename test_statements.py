"""Checks the trial balance, P&L and balance sheet against hand-worked figures.

Run:  python test_statements.py
Uses a throwaway database so your real books are never touched.
"""

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP_DB = Path(tempfile.gettempdir()) / "books_statements_test.db"
if TMP_DB.exists():
    TMP_DB.unlink()
os.environ["BOOKS_DB"] = str(TMP_DB)

import db  # noqa: E402
import settlement as stl  # noqa: E402
import statements as fin  # noqa: E402

FAILURES = []


def check(label, got, want, tol=0.005):
    ok = abs(got - want) <= tol if isinstance(want, float) else got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(label)


def positions(date_to=None):
    entries = db.get_entries(date_to=date_to)
    settlements = fin.up_to(db.get_settlements(), "paid_on", date_to)
    return entries, stl.compute_positions(
        entries, db.get_splits(entries["id"].astype(int).tolist()),
        db.get_partners(include_inactive=True), settlements,
    )


db.init_db()
people = {r.name: int(r.id) for r in db.get_partners().itertuples()}
A, B, FIRM = people["Partner A"], people["Partner B"], people["Firm (Business Account)"]

# expenses shared 60/40, profits shared 40/60 - deliberately different
db.update_partner(A, "Partner A", 60.0, 40.0, True)
db.update_partner(B, "Partner B", 40.0, 60.0, True)

# A funds 10,000 of rent; the firm banks 50,000 of sales; B pays 2,000 of travel
db.save_entry(entry_date=date(2026, 4, 10), kind="expense", amount=10_000.0,
              category="Rent", description="", cash_partner_id=A, share_mode="ratio")
db.save_entry(entry_date=date(2026, 4, 15), kind="income", amount=50_000.0,
              category="Sales", description="", cash_partner_id=FIRM,
              share_mode="ratio")
db.save_entry(entry_date=date(2026, 4, 20), kind="expense", amount=2_000.0,
              category="Travel", description="", cash_partner_id=B,
              share_mode="ratio")

entries, pos = positions()
by_name = {r.partner: r for r in pos.itertuples()}
trial = fin.trial_balance(entries, pos)
totals = fin.trial_balance_totals(trial)
accounts = trial.set_index("Account")

print("\n1) The trial balance balances")
check("debits equal credits", totals["difference"], 0.0, tol=0.0)
check("debit total", totals["debit"], 12_000.0 + 50_000.0 - 12_000.0 + 12_000.0)
check("credit total", totals["credit"], totals["debit"])

print("\n2) Every account lands on the right side")
check("rent is a debit", float(accounts.loc["Expense — Rent", "Debit"]), 10_000.0)
check("rent has no credit", float(accounts.loc["Expense — Rent", "Credit"]), 0.0)
check("travel is a debit", float(accounts.loc["Expense — Travel", "Debit"]), 2_000.0)
check("sales is a credit", float(accounts.loc["Income — Sales", "Credit"]), 50_000.0)
check("sales has no debit", float(accounts.loc["Income — Sales", "Debit"]), 0.0)
check("the firm holds 50,000 of cash",
      float(accounts.loc["Firm (Business Account) — bank / cash", "Debit"]), 50_000.0)
check("A funded 10,000",
      float(accounts.loc["Partner A — current account", "Credit"]), 10_000.0)
check("B funded 2,000",
      float(accounts.loc["Partner B — current account", "Credit"]), 2_000.0)
check("sections are labelled", sorted(set(trial["Section"])),
      ["Assets", "Expenses", "Income", "Partner accounts"])

print("\n3) The P&L adds up and splits on the right ratios")
pl = fin.profit_and_loss(entries, pos)
check("total income", pl["totals"]["income"], 50_000.0)
check("total expenses", pl["totals"]["expense"], 12_000.0)
check("net profit", pl["totals"]["net"], 38_000.0)
check("income lines", pl["income"]["Category"].tolist(), ["Sales"])
check("expense lines biggest first", pl["expenses"]["Category"].tolist(),
      ["Rent", "Travel"])
check("income lines sum to the total",
      round(float(pl["income"]["Amount"].sum()), 2), 50_000.0)

appr = pl["appropriation"].set_index("Partner")
check("A earns 40% of income", float(appr.loc["Partner A", "Share of income"]),
      20_000.0)
check("B earns 60% of income", float(appr.loc["Partner B", "Share of income"]),
      30_000.0)
check("A bears 60% of expenses",
      float(appr.loc["Partner A", "Share of expenses"]), 7_200.0)
check("B bears 40% of expenses",
      float(appr.loc["Partner B", "Share of expenses"]), 4_800.0)
check("A's profit share", float(appr.loc["Partner A", "Profit / (loss)"]), 12_800.0)
check("B's profit share", float(appr.loc["Partner B", "Profit / (loss)"]), 25_200.0)
check("appropriation equals the net profit",
      round(float(appr["Profit / (loss)"].sum()), 2), pl["totals"]["net"])
check("the firm takes no profit share", "Firm (Business Account)" in appr.index, False)

print("\n4) The balance sheet balances")
bs = fin.balance_sheet(pos)
check("assets equal capital", bs["totals"]["difference"], 0.0, tol=0.0)
assets = bs["assets"].set_index("Item")
funding = bs["funding"].set_index("Item")
check("firm cash is an asset",
      float(assets.loc["Firm (Business Account) — bank / cash", "Amount"]), 50_000.0)
check("A's current account is funding",
      float(funding.loc["Partner A — current account", "Amount"]), 22_800.0)
check("B's current account is funding",
      float(funding.loc["Partner B — current account", "Amount"]), 27_200.0)
check("total assets", bs["totals"]["assets"], 50_000.0)
check("A = 10,000 funded + 12,800 profit",
      float(funding.loc["Partner A — current account", "Amount"]),
      10_000.0 + 12_800.0)

print("\n5) The two partner balances mean different things")
moved = dict(zip(pos["partner"], fin.cash_movement(pos)))
check("A's cash movement is cash only", float(moved["Partner A"]), 10_000.0)
check("A's net adds the profit", float(by_name["Partner A"].net), 22_800.0)
check("net = cash - burden + income share",
      float(by_name["Partner A"].net),
      float(moved["Partner A"]) - 7_200.0 + 20_000.0)

print("\n6) A partner who owes becomes a receivable, not negative capital")
# B takes the 50,000 of sales into their own pocket instead of the firm
db.save_entry(entry_id=2, entry_date=date(2026, 4, 15), kind="income",
              amount=50_000.0, category="Sales", description="",
              cash_partner_id=B, share_mode="ratio")
entries, pos = positions()
bs = fin.balance_sheet(pos)
trial = fin.trial_balance(entries, pos)
check("balance sheet still balances", bs["totals"]["difference"], 0.0, tol=0.0)
check("trial balance still balances",
      fin.trial_balance_totals(trial)["difference"], 0.0, tol=0.0)
# B put in 2,000 and took out 50,000, bears 4,800 and earns 30,000
check("B now owes the business",
      float(bs["assets"].set_index("Item").loc["Due from Partner B", "Amount"]),
      -(2_000.0 - 4_800.0 + 30_000.0 - 50_000.0))
check("A's capital is unchanged",
      float(bs["funding"].set_index("Item").loc["Partner A — current account",
                                                "Amount"]), 22_800.0)
check("the firm no longer appears as cash",
      any("bank / cash" in i for i in bs["assets"]["Item"]), False)
check("B's current account is a debit in the trial balance",
      float(fin.trial_balance(entries, pos).set_index("Account")
            .loc["Partner B — current account", "Debit"]), 48_000.0)

print("\n7) Refunds, foreign currency and settlements keep it balanced")
db.save_entry(entry_date=date(2026, 5, 1), kind="expense", amount=-1_500.0,
              category="Rent", description="refund", cash_partner_id=A,
              share_mode="ratio")
db.save_entry(entry_date=date(2026, 5, 2), kind="expense", amount=200.0,
              currency="USD", category="Travel", description="usd",
              cash_partner_id=A, share_mode="ratio")
db.save_entry(entry_date=date(2026, 5, 3), kind="expense", amount=3_124.55,
              category="Custom Charges", description="odd paise",
              cash_partner_id=A, share_mode="ratio")
db.add_settlement(date(2026, 5, 10), B, A, 5_000.0, "part settlement")

entries, pos = positions()
trial = fin.trial_balance(entries, pos)
bs = fin.balance_sheet(pos)
pl = fin.profit_and_loss(entries, pos)
check("trial balance balances to the paise",
      fin.trial_balance_totals(trial)["difference"], 0.0, tol=0.0)
check("balance sheet balances to the paise", bs["totals"]["difference"], 0.0, tol=0.0)
check("USD expense is in the trial balance at base rate",
      float(trial.set_index("Account").loc["Expense — Travel", "Debit"]),
      2_000.0 + 200.0 * 83.0)
check("the rent refund nets off its account",
      float(trial.set_index("Account").loc["Expense — Rent", "Debit"]), 8_500.0)
check("appropriation still equals net profit",
      round(float(pl["appropriation"]["Profit / (loss)"].sum()), 2),
      pl["totals"]["net"])
check("a settlement does not change the profit", pl["totals"]["income"], 50_000.0)

print("\n8) Statements are cumulative 'as at' a date")
early_entries, early_pos = positions(date_to="2026-04-30")
early_trial = fin.trial_balance(early_entries, early_pos)
check("April only sees 3 entries", len(early_entries), 3)
check("April trial balance balances",
      fin.trial_balance_totals(early_trial)["difference"], 0.0, tol=0.0)
check("the May refund is excluded",
      float(early_trial.set_index("Account").loc["Expense — Rent", "Debit"]),
      10_000.0)
check("the May settlement is excluded",
      float(fin.cash_movement(early_pos)[
          early_pos["partner"] == "Partner A"].iloc[0]), 10_000.0)
check("April balance sheet balances",
      fin.balance_sheet(early_pos)["totals"]["difference"], 0.0, tol=0.0)

print("\n9) Empty books produce empty but valid statements")
blank = db.get_entries(date_from="2030-01-01")
blank_pos = stl.compute_positions(
    blank, db.get_splits([]), db.get_partners(include_inactive=True),
    db.get_settlements().iloc[0:0],
)
blank_trial = fin.trial_balance(blank, blank_pos)
check("nothing to show — nil accounts are omitted", len(blank_trial), 0)
check("totals are zero", fin.trial_balance_totals(blank_trial)["difference"], 0.0)
blank_pl = fin.profit_and_loss(blank, blank_pos)
check("no profit", blank_pl["totals"]["net"], 0.0)
check("appropriation lists the partners anyway", len(blank_pl["appropriation"]), 2)
blank_bs = fin.balance_sheet(blank_pos)
check("balance sheet is empty but balanced",
      blank_bs["totals"]["difference"], 0.0, tol=0.0)
check("no asset lines", len(blank_bs["assets"]), 0)

print("\n" + ("ALL STATEMENT CHECKS PASSED" if not FAILURES
             else f"FAILURES: {FAILURES}"))
raise SystemExit(1 if FAILURES else 0)
