"""Headless checks for the storage layer and the settlement maths.

Run:  python -I test_books.py
Uses a throwaway database so your real books are never touched.
"""

import os
import tempfile
from datetime import date
from pathlib import Path

TMP_DB = Path(tempfile.gettempdir()) / "books_test.db"
if TMP_DB.exists():
    TMP_DB.unlink()
os.environ["BOOKS_DB"] = str(TMP_DB)

import db  # noqa: E402  (must come after BOOKS_DB is set)
import settlement as stl  # noqa: E402

FAILURES = []


def check(label, got, want, tol=0.005):
    ok = abs(got - want) <= tol if isinstance(want, float) else got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(label)


def positions_by_name():
    pos = stl.compute_positions(
        db.get_entries(), db.get_splits(),
        db.get_partners(include_inactive=True), db.get_settlements(),
    )
    return {row.partner: row for row in pos.itertuples()}, pos


db.init_db()
people = db.get_partners()
ids = {r.name: int(r.id) for r in people.itertuples()}
A, B, FIRM = ids["Partner A"], ids["Partner B"], ids["Firm (Business Account)"]

print("\n1) 60/40 ratio expense paid by A")
# expense 60/40, profit 60/40 for now - section 21 pulls them apart
db.update_partner(A, "Partner A", 60.0, 60.0, True)
db.update_partner(B, "Partner B", 40.0, 40.0, True)
db.save_entry(entry_date=date(2026, 4, 10), kind="expense", amount=10_000.0,
              category="Rent", description="April rent", cash_partner_id=A,
              share_mode="ratio")
pos, frame = positions_by_name()
check("A bears 60%", float(pos["Partner A"].expense_burden), 6_000.0)
check("B bears 40%", float(pos["Partner B"].expense_burden), 4_000.0)
check("A is owed 4,000", float(pos["Partner A"].net), 4_000.0)
check("B owes 4,000", float(pos["Partner B"].net), -4_000.0)
check("balances net to zero", float(frame["net"].sum()), 0.0)

print("\n2) Non-shared expense paid by B washes out")
db.save_entry(entry_date=date(2026, 4, 11), kind="expense", amount=3_000.0,
              category="Travel", description="B's own trip", cash_partner_id=B,
              share_mode="none")
pos, frame = positions_by_name()
check("B's burden now 4,000 + 3,000", float(pos["Partner B"].expense_burden), 7_000.0)
check("A's net unchanged", float(pos["Partner A"].net), 4_000.0)
check("B's net unchanged", float(pos["Partner B"].net), -4_000.0)

print("\n3) Income into the firm account, split by ratio")
db.save_entry(entry_date=date(2026, 4, 15), kind="income", amount=50_000.0,
              category="Sales", description="Invoice 001", cash_partner_id=FIRM,
              share_mode="ratio")
pos, frame = positions_by_name()
check("A's income share", float(pos["Partner A"].income_share), 30_000.0)
check("B's income share", float(pos["Partner B"].income_share), 20_000.0)
check("A is owed 34,000", float(pos["Partner A"].net), 34_000.0)
check("B is owed 16,000", float(pos["Partner B"].net), 16_000.0)
check("firm owes 50,000", float(pos["Firm (Business Account)"].net), -50_000.0)
check("balances net to zero", float(frame["net"].sum()), 0.0)

print("\n4) Custom split - 100% of an expense to A only, paid by B")
db.save_entry(entry_date=date(2026, 4, 20), kind="expense", amount=2_000.0,
              category="Professional Fees", description="A's licence",
              cash_partner_id=B, share_mode="custom",
              custom_split={A: 100.0, B: 0.0})
pos, frame = positions_by_name()
check("A's burden +2,000", float(pos["Partner A"].expense_burden), 8_000.0)
check("B's burden unchanged", float(pos["Partner B"].expense_burden), 7_000.0)
check("A now owed 32,000", float(pos["Partner A"].net), 32_000.0)
check("B now owed 18,000", float(pos["Partner B"].net), 18_000.0)

print("\n5) Suggested transfers")
transfers = stl.minimal_transfers(frame)
check("one transfer per creditor", len(transfers), 2)
check("firm pays A 32,000",
      next((t["amount"] for t in transfers
            if t["from"].startswith("Firm") and t["to"] == "Partner A"), 0.0), 32_000.0)
check("firm pays B 18,000",
      next((t["amount"] for t in transfers
            if t["from"].startswith("Firm") and t["to"] == "Partner B"), 0.0), 18_000.0)

print("\n6) Recording the payouts clears the balances")
for t in transfers:
    db.add_settlement(date(2026, 4, 30), FIRM,
                      A if t["to"] == "Partner A" else B, t["amount"], "payout")
pos, frame = positions_by_name()
check("A square", float(pos["Partner A"].net), 0.0)
check("B square", float(pos["Partner B"].net), 0.0)
check("firm square", float(pos["Firm (Business Account)"].net), 0.0)
check("no transfers left", len(stl.minimal_transfers(frame)), 0)

print("\n7) Edit and delete")
first_id = int(db.get_entries()["id"].min())
db.save_entry(entry_id=first_id, entry_date=date(2026, 4, 10), kind="expense",
              amount=20_000.0, category="Rent", description="April rent (revised)",
              cash_partner_id=A, share_mode="ratio")
pos, _ = positions_by_name()
check("A's burden after edit", float(pos["Partner A"].expense_burden), 20_000.0 * 0.6 + 2_000.0)
db.delete_entry(first_id)
check("entry deleted", len(db.get_entries()), 3)
# ids are renumbered on delete, so check for orphans globally rather than by id
live_ids = set(db.get_entries()["id"].astype(int))
check("no orphan splits", set(db.get_splits()["entry_id"].astype(int)) - live_ids, set())
check("every remaining entry still has a split",
      set(db.get_splits()["entry_id"].astype(int)), live_ids)

print("\n8) Rejects bad input")
for label, kwargs in [
    ("zero amount", dict(amount=0.0, share_mode="ratio")),
    ("empty custom split", dict(amount=100.0, share_mode="custom", custom_split={})),
]:
    try:
        db.save_entry(entry_date=date(2026, 5, 1), kind="expense",
                      category="Misc", description="", cash_partner_id=A, **kwargs)
        check(label, "accepted", "rejected")
    except ValueError:
        check(label, "rejected", "rejected")

def reset_books():
    """Clear every entry and settlement so a section starts from id 1 again."""
    with db.connect() as conn:
        conn.execute("DELETE FROM entry_splits")
        conn.execute("DELETE FROM entries")
        conn.execute("DELETE FROM settlements")
    db.resequence_entries()
    db.resequence_settlements()


print("\n9) Multi-currency: a USD expense converts at the Setup rate")
reset_books()
check("default base", db.base_currency(), "INR")
check("seeded USD rate", db.rate_map()["USD"], 83.0)
db.save_entry(entry_date=date(2026, 5, 1), kind="expense", amount=10_000.0,
              currency="INR", category="Rent", description="May rent",
              cash_partner_id=A, share_mode="ratio")
db.save_entry(entry_date=date(2026, 5, 2), kind="expense", amount=1_000.0,
              currency="USD", category="Travel", description="Flight",
              cash_partner_id=A, share_mode="ratio")
usd_rows = db.get_entries(currency="USD")
check("stored in USD", str(usd_rows.iloc[0]["currency"]), "USD")
check("amount kept as entered", float(usd_rows.iloc[0]["amount"]), 1_000.0)
check("base_amount converted", float(usd_rows.iloc[0]["base_amount"]), 83_000.0)
pos, frame = positions_by_name()
check("A paid 93,000 base", float(pos["Partner A"].cash_paid), 93_000.0)
check("A bears 60% = 55,800", float(pos["Partner A"].expense_burden), 55_800.0)
check("B bears 40% = 37,200", float(pos["Partner B"].expense_burden), 37_200.0)
check("A owed 37,200", float(pos["Partner A"].net), 37_200.0)
check("B owes 37,200", float(pos["Partner B"].net), -37_200.0)
check("still zero-sum", float(frame["net"].sum()), 0.0)

print("\n10) Changing the rate in Setup re-converts the whole history")
db.upsert_currency("USD", "$", 90.0)
pos, _ = positions_by_name()
check("A paid 100,000 base", float(pos["Partner A"].cash_paid), 100_000.0)
check("A owed 40,000", float(pos["Partner A"].net), 40_000.0)
db.upsert_currency("USD", "$", 83.0)
pos, _ = positions_by_name()
check("rate restored, A owed 37,200", float(pos["Partner A"].net), 37_200.0)

print("\n11) A settlement paid in USD clears the right base amount")
db.add_settlement(date(2026, 5, 3), B, A, 400.0, "part payment", "USD")
pos, frame = positions_by_name()
check("$400 counts as 33,200", float(pos["Partner B"].settled_out), 33_200.0)
check("A owed 4,000 now", float(pos["Partner A"].net), 4_000.0)
check("B owes 4,000 now", float(pos["Partner B"].net), -4_000.0)
check("zero-sum with mixed settlement", float(frame["net"].sum()), 0.0)

print("\n12) Rejects an unknown currency and protects the base")
try:
    db.save_entry(entry_date=date(2026, 5, 4), kind="expense", amount=10.0,
                  currency="XYZ", category="Misc", description="",
                  cash_partner_id=A, share_mode="ratio")
    check("unknown currency", "accepted", "rejected")
except ValueError:
    check("unknown currency", "rejected", "rejected")
try:
    db.delete_currency("INR")
    check("delete base currency", "accepted", "rejected")
except ValueError:
    check("delete base currency", "rejected", "rejected")
try:
    db.delete_currency("USD")
    check("delete currency in use", "accepted", "rejected")
except ValueError:
    check("delete currency in use", "rejected", "rejected")

print("\n13) Rebasing INR -> USD preserves relative value")
check("pre-rebase base_amount",
      float(db.get_entries(currency="USD").iloc[0]["base_amount"]), 83_000.0)
db.set_base_currency("USD")
check("base is USD", db.base_currency(), "USD")
check("INR rate is 1/83", db.rate_map()["INR"], 1 / 83.0, tol=1e-6)
check("USD rate is 1", db.rate_map()["USD"], 1.0)
check("USD entry now 1,000 base",
      float(db.get_entries(currency="USD").iloc[0]["base_amount"]), 1_000.0)
check("INR entry now ~120.48 base",
      float(db.get_entries(currency="INR").iloc[0]["base_amount"]),
      round(10_000 / 83.0, 2), tol=0.02)
pos, frame = positions_by_name()
check("zero-sum after rebase", float(frame["net"].sum()), 0.0)
check("A still the creditor", float(pos["Partner A"].net) > 0, True)
db.set_base_currency("INR")
check("back to INR", db.base_currency(), "INR")
check("USD rate restored", db.rate_map()["USD"], 83.0, tol=1e-6)
pos, _ = positions_by_name()
check("A owed 4,000 again", float(pos["Partner A"].net), 4_000.0, tol=0.5)

print("\n14) Settlement export builds every section")
import exports  # noqa: E402

pos_frame = stl.compute_positions(
    db.get_entries(), db.get_splits(), db.get_partners(include_inactive=True),
    db.get_settlements(),
)
frames = exports.build_frames(
    positions=pos_frame, transfers=stl.minimal_transfers(pos_frame),
    settlements=db.get_settlements(), entries=db.get_entries(),
    currencies=db.get_currencies(), base=db.base_currency(),
    business_name="Test Firm",
)
for sheet in ("Summary", "Net positions", "Who pays whom", "Totals by currency",
              "Settlements paid", "Exchange rates", "Ledger"):
    check(f"sheet '{sheet}' present", sheet in frames, True)
xlsx = exports.to_excel(frames)
check("xlsx is a zip container", xlsx[:2], b"PK")
check("xlsx has bytes", len(xlsx) > 5000, True)
csv_bytes = exports.to_csv(frames)
text = csv_bytes.decode("utf-8-sig")
check("csv has section headers", text.count("## "), len(frames))
check("csv names the base currency", "(INR)" in text, True)
by_cur = stl.totals_by_currency(db.get_entries())
check("two currencies in breakdown", sorted(by_cur["Currency"]), ["INR", "USD"])

print("\n15) Deleting an entry closes the gap in the ids")
reset_books()


def add(label, amount, partner=None, mode="ratio"):
    return db.save_entry(entry_date=date(2026, 6, 1), kind="expense", amount=amount,
                         category="Misc", description=label,
                         cash_partner_id=partner or A, share_mode=mode)


for i, amt in enumerate([100.0, 200.0, 300.0, 400.0, 500.0], start=1):
    check(f"entry {i} gets id {i}", add(f"e{i}", amt), i)

db.delete_entry(2)
ids = sorted(db.get_entries()["id"].astype(int).tolist())
check("ids are contiguous after deleting #2", ids, [1, 2, 3, 4])
remaining = db.get_entries().set_index("id")["description"].to_dict()
check("old #3 is now #2", remaining[2], "e3")
check("old #5 is now #4", remaining[4], "e5")
check("next insert continues at 5", add("e6", 600.0), 5)

print("\n16) Splits follow their entry's new id")
splits = db.get_splits()
check("every split points at a live entry",
      set(splits["entry_id"].astype(int)) <= set(db.get_entries()["id"].astype(int)),
      True)
check("split count is 2 per entry", len(splits), 2 * len(db.get_entries()))
for eid in db.get_entries()["id"].astype(int):
    row = db.get_entry(int(eid))
    assert row is not None
    check(f"entry {eid} splits total 100%", sum(row[1].values()), 100.0)

print("\n17) Deleting the newest entry rewinds the counter")
newest = int(db.get_entries()["id"].max())
db.delete_entry(newest)
check("ids now 1..4", sorted(db.get_entries()["id"].astype(int).tolist()),
      [1, 2, 3, 4])
check("next insert reuses the freed number", add("e7", 700.0), 5)

print("\n18) Emptying the table restarts at 1")
while len(db.get_entries()):
    # always delete the lowest id, since the rest renumber under us each time
    db.delete_entry(int(db.get_entries()["id"].min()))
check("no entries left", len(db.get_entries()), 0)
check("no orphan splits", len(db.get_splits()), 0)
check("first new entry is id 1", add("fresh", 50.0), 1)

print("\n19) Settlement ids renumber the same way")
reset_books()
for n in (1, 2, 3):
    db.add_settlement(date(2026, 6, n), A, B, 100.0 * n, f"s{n}")
check("three settlements", sorted(db.get_settlements()["id"].astype(int)), [1, 2, 3])
db.delete_settlement(1)
after = db.get_settlements().set_index("id")["note"].to_dict()
check("ids contiguous", sorted(after), [1, 2])
check("old #2 is now #1", after[1], "s2")
check("old #3 is now #2", after[2], "s3")
db.add_settlement(date(2026, 6, 9), A, B, 900.0, "s4")
check("next settlement is #3", int(db.get_settlements()["id"].max()), 3)

print("\n20) Renumbering never disturbs the balances")
reset_books()
db.save_entry(entry_date=date(2026, 7, 1), kind="expense", amount=10_000.0,
              category="Rent", description="keep", cash_partner_id=A,
              share_mode="ratio")
db.save_entry(entry_date=date(2026, 7, 2), kind="expense", amount=5_000.0,
              category="Travel", description="drop me", cash_partner_id=B,
              share_mode="ratio")
db.save_entry(entry_date=date(2026, 7, 3), kind="income", amount=20_000.0,
              category="Sales", description="keep", cash_partner_id=FIRM,
              share_mode="ratio")
pos_before, _ = positions_by_name()
a_before = float(pos_before["Partner A"].net)
db.delete_entry(2)
pos_after, frame_after = positions_by_name()
# dropping that expense drops A's 60% burden of it, so A is owed 3,000 more
check("A's net moves by exactly A's share of the deleted expense",
      float(pos_after["Partner A"].net), a_before + (5_000.0 * 0.6))
check("still zero-sum after renumber", float(frame_after["net"].sum()), 0.0)
check("ids are 1 and 2", sorted(db.get_entries()["id"].astype(int).tolist()), [1, 2])

print("\n21) Expense sharing and profit sharing use separate percentages")
reset_books()
# A bears 70% of costs but takes only 40% of the profit
db.update_partner(A, "Partner A", 70.0, 40.0, True)
db.update_partner(B, "Partner B", 30.0, 60.0, True)
check("ratio totals both 100",
      db.ratio_totals(), {"expense_pct": 100.0, "profit_pct": 100.0})

db.save_entry(entry_date=date(2026, 8, 1), kind="expense", amount=10_000.0,
              category="Rent", description="Aug rent", cash_partner_id=FIRM,
              share_mode="ratio")
db.save_entry(entry_date=date(2026, 8, 2), kind="income", amount=20_000.0,
              category="Sales", description="Aug sales", cash_partner_id=FIRM,
              share_mode="ratio")
pos, frame = positions_by_name()
check("A bears 70% of the expense", float(pos["Partner A"].expense_burden), 7_000.0)
check("B bears 30% of the expense", float(pos["Partner B"].expense_burden), 3_000.0)
check("A earns 40% of the income", float(pos["Partner A"].income_share), 8_000.0)
check("B earns 60% of the income", float(pos["Partner B"].income_share), 12_000.0)
check("A net = 8,000 - 7,000", float(pos["Partner A"].net), 1_000.0)
check("B net = 12,000 - 3,000", float(pos["Partner B"].net), 9_000.0)
check("firm owes both", float(pos["Firm (Business Account)"].net), -10_000.0)
check("zero-sum with split ratios", float(frame["net"].sum()), 0.0)

print("\n22) Each ratio is resolved independently")
exp_split = db.resolve_split("ratio", A, None, kind="expense")
inc_split = db.resolve_split("ratio", A, None, kind="income")
check("expense split for A", exp_split[A], 70.0)
check("income split for A", inc_split[A], 40.0)
check("expense split sums to 100", sum(exp_split.values()), 100.0)
check("income split sums to 100", sum(inc_split.values()), 100.0)
try:
    db.resolve_split("ratio", A, None, kind="transfer")
    check("unknown kind", "accepted", "rejected")
except ValueError:
    check("unknown kind", "rejected", "rejected")

print("\n23) Lopsided ratios are normalised pro-rata, per column")
db.update_partner(A, "Partner A", 30.0, 10.0, True)   # expense totals 60, profit 70
db.update_partner(B, "Partner B", 30.0, 60.0, True)
exp_split = db.resolve_split("ratio", A, None, kind="expense")
inc_split = db.resolve_split("ratio", A, None, kind="income")
check("expense normalised to 50/50", exp_split[A], 50.0)
check("income normalised to 1/7", inc_split[A], 100.0 * 10 / 70, tol=0.01)
check("income still sums to 100", sum(inc_split.values()), 100.0)
db.update_partner(A, "Partner A", 70.0, 40.0, True)
db.update_partner(B, "Partner B", 30.0, 60.0, True)

print("\n24) A saved entry keeps its split when the ratios change")
before = db.get_entry(1)[1]
db.update_partner(A, "Partner A", 90.0, 90.0, True)
db.update_partner(B, "Partner B", 10.0, 10.0, True)
check("split unchanged by a ratio edit", db.get_entry(1)[1], before)
db.update_partner(A, "Partner A", 70.0, 40.0, True)
db.update_partner(B, "Partner B", 30.0, 60.0, True)

print("\n25) The export documents the ratios it used")
pos_frame = stl.compute_positions(
    db.get_entries(), db.get_splits(), db.get_partners(include_inactive=True),
    db.get_settlements(),
)
frames = exports.build_frames(
    positions=pos_frame, transfers=stl.minimal_transfers(pos_frame),
    settlements=db.get_settlements(), entries=db.get_entries(),
    currencies=db.get_currencies(), base=db.base_currency(),
    business_name="Test Firm", partners=db.get_partners(include_inactive=True),
)
check("'Sharing ratios' sheet present", "Sharing ratios" in frames, True)
ratios = frames["Sharing ratios"].set_index("Partner")
check("A's expense % exported", float(ratios.loc["Partner A", "Expense %"]), 70.0)
check("A's profit % exported", float(ratios.loc["Partner A", "Profit %"]), 40.0)
check("csv carries the ratios",
      "## Sharing ratios" in exports.to_csv(frames).decode("utf-8-sig"), True)

print("\n26) A negative expense records a refund and reduces the burden")
reset_books()
db.update_partner(A, "Partner A", 60.0, 60.0, True)
db.update_partner(B, "Partner B", 40.0, 40.0, True)
db.save_entry(entry_date=date(2026, 9, 1), kind="expense", amount=10_000.0,
              category="Travel", description="Two flights", cash_partner_id=A,
              share_mode="ratio")
pos, _ = positions_by_name()
check("A owed 4,000 before the refund", float(pos["Partner A"].net), 4_000.0)

refund_id = db.save_entry(entry_date=date(2026, 9, 5), kind="expense",
                          amount=-2_500.0, category="Travel",
                          description="One flight cancelled", cash_partner_id=A,
                          share_mode="ratio")
stored = db.get_entries().set_index("id").loc[refund_id]
check("negative amount stored as-is", float(stored["amount"]), -2_500.0)
check("base_amount is negative too", float(stored["base_amount"]), -2_500.0)
pos, frame = positions_by_name()
check("A's cash paid nets to 7,500", float(pos["Partner A"].cash_paid), 7_500.0)
check("A's burden nets to 4,500", float(pos["Partner A"].expense_burden), 4_500.0)
check("B's burden nets to 3,000", float(pos["Partner B"].expense_burden), 3_000.0)
check("A owed 3,000 after the refund", float(pos["Partner A"].net), 3_000.0)
check("B owes 3,000 after the refund", float(pos["Partner B"].net), -3_000.0)
check("still zero-sum with a refund", float(frame["net"].sum()), 0.0)

print("\n27) Refunds net off the period totals")
totals = stl.summary_totals(db.get_entries())
check("expenses net of the refund", totals["expense"], 7_500.0)
check("net profit reflects it", totals["net"], -7_500.0)
by_cur = stl.totals_by_currency(db.get_entries())
check("currency breakdown nets too", float(by_cur.loc[0, "Expenses"]), 7_500.0)

print("\n28) A refund in a foreign currency converts with its sign")
db.save_entry(entry_date=date(2026, 9, 6), kind="expense", amount=-100.0,
              currency="USD", category="Travel", description="USD refund",
              cash_partner_id=A, share_mode="ratio")
usd = db.get_entries(currency="USD").iloc[0]
check("USD refund base_amount", float(usd["base_amount"]), -8_300.0)
pos, frame = positions_by_name()
check("A's cash paid drops by 8,300", float(pos["Partner A"].cash_paid),
      7_500.0 - 8_300.0)
check("zero-sum with a foreign refund", float(frame["net"].sum()), 0.0)

print("\n29) Zero is still refused, and income cannot go negative")
for label, kwargs in [
    ("zero expense", dict(kind="expense", amount=0.0)),
    ("zero income", dict(kind="income", amount=0.0)),
    ("negative income", dict(kind="income", amount=-500.0)),
]:
    try:
        db.save_entry(entry_date=date(2026, 9, 7), category="Misc", description="",
                      cash_partner_id=A, share_mode="ratio", **kwargs)
        check(label, "accepted", "rejected")
    except ValueError:
        check(label, "rejected", "rejected")

print("\n30) A negative expense survives an edit in both directions")
flip = db.save_entry(entry_date=date(2026, 9, 8), kind="expense", amount=500.0,
                     category="Misc", description="flip me", cash_partner_id=A,
                     share_mode="ratio")
db.save_entry(entry_id=flip, entry_date=date(2026, 9, 8), kind="expense",
              amount=-500.0, category="Misc", description="flipped negative",
              cash_partner_id=A, share_mode="ratio")
check("edit to negative sticks",
      float(db.get_entries().set_index("id").loc[flip, "amount"]), -500.0)
db.save_entry(entry_id=flip, entry_date=date(2026, 9, 8), kind="expense",
              amount=750.0, category="Misc", description="flipped back",
              cash_partner_id=A, share_mode="ratio")
check("edit back to positive sticks",
      float(db.get_entries().set_index("id").loc[flip, "amount"]), 750.0)
try:
    db.save_entry(entry_id=flip, entry_date=date(2026, 9, 8), kind="income",
                  amount=-750.0, category="Sales", description="bad flip",
                  cash_partner_id=A, share_mode="ratio")
    check("edit into negative income", "accepted", "rejected")
except ValueError:
    check("edit into negative income", "rejected", "rejected")

print("\n31) The database constraint backs the Python check")
RAW_INSERT = (
    "INSERT INTO entries (entry_date, kind, amount, currency, category,"
    " description, cash_partner_id, share_mode, created_at)"
    " VALUES ('2026-09-09', ?, ?, 'INR', 'Misc', '', ?, 'none', 'x')"
)
# each attempt gets its own transaction: Postgres aborts the whole transaction
# after a failed statement, so batching these would poison the later one
for label, raw_amount, raw_kind, expected in [
    ("raw zero", 0.0, "expense", "rejected"),
    ("raw negative income", -5.0, "income", "rejected"),
    ("raw negative expense", -5.0, "expense", "accepted"),
]:
    try:
        with db.connect() as _c:
            _c.execute(RAW_INSERT, (raw_kind, raw_amount, A))
        check(label, "accepted", expected)
    except Exception:
        check(label, "rejected", expected)

print("\n32) The entry form's starting currency differs by kind")
check("expenses start on the base currency", db.default_currency("expense"), "INR")
check("income starts on USD", db.default_currency("income"), "USD")

db.set_default_currency("income", "INR")
check("income default is overridable", db.default_currency("income"), "INR")
db.set_default_currency("expense", "USD")
check("expense default is overridable", db.default_currency("expense"), "USD")
db.set_default_currency("expense", "INR")
db.set_default_currency("income", "USD")

try:
    db.set_default_currency("income", "XYZ")
    check("unknown default currency", "accepted", "rejected")
except ValueError:
    check("unknown default currency", "rejected", "rejected")
try:
    db.set_default_currency("transfer", "USD")
    check("unknown kind", "accepted", "rejected")
except ValueError:
    check("unknown kind", "rejected", "rejected")

print("\n33) A stale default falls back instead of breaking the form")
db.set_setting("default_currency_income", "AED")  # never added to the table
check("missing currency falls back to USD", db.default_currency("income"), "USD")
db.set_default_currency("income", "USD")
db.upsert_currency("AED", "AED", 22.6)
db.set_default_currency("income", "AED")
check("a real currency is honoured", db.default_currency("income"), "AED")
db.delete_currency("AED")
check("removing it falls back again", db.default_currency("income"), "USD")
check("the default is still only a hint — entries keep their own currency",
      str(db.get_entries(currency="USD").iloc[0]["currency"]), "USD")

print("\n34) Ledger grouping by type and category")
reset_books()
db.update_partner(A, "Partner A", 60.0, 60.0, True)
db.update_partner(B, "Partner B", 40.0, 40.0, True)
for when, kind, amount, category in [
    (1, "expense", 10_000.0, "Rent"),
    (2, "expense", 4_000.0, "Travel"),
    (3, "expense", 1_000.0, "Travel"),
    (4, "expense", -500.0, "Travel"),      # a refund inside a group
    (5, "income", 30_000.0, "Sales"),
    (6, "income", 20_000.0, "Service Income"),
]:
    db.save_entry(entry_date=date(2026, 10, when), kind=kind, amount=amount,
                  category=category, description="", cash_partner_id=FIRM,
                  share_mode="ratio")

entries, splits = db.get_entries(), db.get_splits()
people = db.get_partners()

check("group options", list(stl.GROUP_BY),
      ["None (flat list)", "Type", "Category", "Type & Category"])
check("no grouping gives an empty frame",
      len(stl.group_totals(entries, splits, people, [])), 0)

by_type = stl.group_totals(entries, splits, people, ["kind"]).set_index("Type")
check("two type groups", sorted(by_type.index), ["Expense", "Income"])
check("expense group counts 4 entries", int(by_type.loc["Expense", "Entries"]), 4)
check("expense group nets the refund", float(by_type.loc["Expense", "Expenses"]),
      14_500.0)
check("expense group net is negative", float(by_type.loc["Expense", "Net"]), -14_500.0)
check("income group totals 50,000", float(by_type.loc["Income", "Income"]), 50_000.0)
check("A's net on expenses is -60%",
      float(by_type.loc["Expense", "Partner A net"]), -8_700.0)
check("B's net on expenses is -40%",
      float(by_type.loc["Expense", "Partner B net"]), -5_800.0)
check("A's net on income is +60%",
      float(by_type.loc["Income", "Partner A net"]), 30_000.0)
check("partner columns reconcile to Net",
      float(by_type.loc["Expense", "Partner A net"])
      + float(by_type.loc["Expense", "Partner B net"]),
      float(by_type.loc["Expense", "Net"]))

by_cat = stl.group_totals(entries, splits, people, ["category"]).set_index("Category")
check("four category groups", sorted(by_cat.index),
      ["Rent", "Sales", "Service Income", "Travel"])
check("Travel groups three lines", int(by_cat.loc["Travel", "Entries"]), 3)
check("Travel nets its refund", float(by_cat.loc["Travel", "Expenses"]), 4_500.0)
check("Rent is one line", float(by_cat.loc["Rent", "Expenses"]), 10_000.0)
check("Sales is income only", float(by_cat.loc["Sales", "Income"]), 30_000.0)
check("category subtotals sum to the period total",
      float(by_cat["Net"].sum()), stl.summary_totals(entries)["net"])

both = stl.group_totals(entries, splits, people, ["kind", "category"])
check("type & category gives 4 rows", len(both), 4)
check("both key columns present", list(both.columns[:2]), ["Type", "Category"])
check("rows are sorted by type then category",
      both[["Type", "Category"]].values.tolist(),
      [["Expense", "Rent"], ["Expense", "Travel"],
       ["Income", "Sales"], ["Income", "Service Income"]])
check("grouped totals still reconcile",
      float(both["Net"].sum()), stl.summary_totals(entries)["net"])

print("\n35) Grouping handles a filtered or mixed-currency ledger")
db.save_entry(entry_date=date(2026, 10, 7), kind="expense", amount=100.0,
              currency="USD", category="Travel", description="USD leg",
              cash_partner_id=FIRM, share_mode="ratio")
entries, splits = db.get_entries(), db.get_splits()
by_cat = stl.group_totals(entries, splits, db.get_partners(),
                          ["category"]).set_index("Category")
check("USD line converts into the Travel subtotal",
      float(by_cat.loc["Travel", "Expenses"]), 4_500.0 + 8_300.0)

only_income = db.get_entries(kind="income")
grouped = stl.group_totals(only_income, db.get_splits(
    only_income["id"].astype(int).tolist()), db.get_partners(), ["kind"])
check("a filtered ledger groups to one row", len(grouped), 1)
check("filtered group is income only", str(grouped.iloc[0]["Type"]), "Income")
check("empty ledger groups to nothing",
      len(stl.group_totals(db.get_entries(date_from="2030-01-01"),
                           splits, db.get_partners(), ["kind"])), 0)

print("\n36) Splitting an odd amount stays exact to the paise")
check("3124.55 halved sums back", sum(stl.allocate(3124.55, [50.0, 50.0])), 3124.55)
check("the odd paisa goes to the first partner",
      stl.allocate(3124.55, [50.0, 50.0]), [1562.28, 1562.27])
check("negative odd amount sums back",
      sum(stl.allocate(-3124.55, [50.0, 50.0])), -3124.55)
check("thirds sum back", sum(stl.allocate(100.0, [100 / 3] * 3)), 100.0)
check("thirds are 2dp", stl.allocate(100.0, [100 / 3] * 3), [33.34, 33.33, 33.33])
check("a 70/30 odd split sums back",
      sum(stl.allocate(0.01, [70.0, 30.0])), 0.01)
check("an empty split is empty", stl.allocate(10.0, []), [])
check("a sole partner takes it all", stl.allocate(3124.55, [100.0]), [3124.55])

reset_books()
for amount in (3124.55, 1000.01, -0.01, 7.77, 12_345.67):
    db.save_entry(entry_date=date(2026, 11, 1), kind="expense", amount=amount,
                  category="Misc", description=f"{amount}", cash_partner_id=FIRM,
                  share_mode="ratio")
exploded = stl.per_partner_amounts(db.get_entries(), db.get_splits())
per_entry = exploded.groupby("entry_id").agg(
    total=("base_amount", "first"), split=("split_base", "sum"))
drift = (per_entry["total"] - per_entry["split"]).abs().max()
check("every entry's splits sum to its amount", float(drift), 0.0)
pos, frame = positions_by_name()
check("nets sum to exactly zero", float(frame["net"].sum()), 0.0, tol=0.0)

print("\n37) Grouped partner columns reconcile to the group's Net")
grouped = stl.group_totals(db.get_entries(), db.get_splits(),
                           db.get_partners(), ["category"])
partner_cols = [c for c in grouped.columns if c.endswith(" net")]
check("partner columns add up to Net",
      float((grouped[partner_cols].sum(axis=1) - grouped["Net"]).abs().max()), 0.0)

print("\n38) Bulk reapply of the current ratios")
reset_books()
db.update_partner(A, "Partner A", 50.0, 50.0, True)
db.update_partner(B, "Partner B", 50.0, 50.0, True)

ratio_1 = db.save_entry(entry_date=date(2026, 12, 1), kind="expense", amount=10_000.0,
                        category="Rent", description="ratio entry",
                        cash_partner_id=A, share_mode="ratio")
ratio_2 = db.save_entry(entry_date=date(2026, 12, 2), kind="income", amount=20_000.0,
                        category="Sales", description="ratio income",
                        cash_partner_id=FIRM, share_mode="ratio")
custom = db.save_entry(entry_date=date(2026, 12, 3), kind="expense", amount=4_000.0,
                       category="Travel", description="custom override",
                       cash_partner_id=A, share_mode="custom",
                       custom_split={A: 100.0, B: 0.0})
unshared = db.save_entry(entry_date=date(2026, 12, 4), kind="expense", amount=2_000.0,
                         category="Misc", description="B's own",
                         cash_partner_id=B, share_mode="none")

check("nothing to do while the ratios are unchanged",
      len(db.ratio_refresh_preview()), 0)
check("applying is a no-op too", db.apply_ratio_refresh(), 0)

# now correct the ratios: expenses 70/30, profit 40/60
db.update_partner(A, "Partner A", 70.0, 40.0, True)
db.update_partner(B, "Partner B", 30.0, 60.0, True)

preview = db.ratio_refresh_preview().set_index("ID")
check("both ratio entries are flagged", sorted(preview.index), [ratio_1, ratio_2])
check("the custom entry is left alone", custom in preview.index, False)
check("the not-shared entry is left alone", unshared in preview.index, False)
check("before/after is spelled out", str(preview.loc[ratio_1, "Before"]),
      "Partner A 50% / Partner B 50%")
check("expense entry moves to 70/30", str(preview.loc[ratio_1, "After"]),
      "Partner A 70% / Partner B 30%")
check("income entry moves to the profit ratio",
      str(preview.loc[ratio_2, "After"]), "Partner A 40% / Partner B 60%")

check("a preview does not change anything",
      db.get_entry(ratio_1)[1], {A: 50.0, B: 50.0})

check("applying updates both", db.apply_ratio_refresh(), 2)
check("expense split rewritten", db.get_entry(ratio_1)[1], {A: 70.0, B: 30.0})
check("income split rewritten", db.get_entry(ratio_2)[1], {A: 40.0, B: 60.0})
check("custom split untouched", db.get_entry(custom)[1], {A: 100.0})
check("not-shared untouched", db.get_entry(unshared)[1], {B: 100.0})
check("re-running is now a no-op", db.apply_ratio_refresh(), 0)
pos, frame = positions_by_name()
check("still zero-sum after the bulk update", float(frame["net"].sum()), 0.0, tol=0.0)

print("\n39) Scoping the bulk update")
db.update_partner(A, "Partner A", 90.0, 90.0, True)
db.update_partner(B, "Partner B", 10.0, 10.0, True)
check("a subset only touches what was passed",
      db.apply_ratio_refresh([ratio_1]), 1)
check("the chosen entry moved", db.get_entry(ratio_1)[1], {A: 90.0, B: 10.0})
check("the other one did not", db.get_entry(ratio_2)[1], {A: 40.0, B: 60.0})
check("an empty selection does nothing", db.apply_ratio_refresh([]), 0)
check("unknown ids are ignored", db.apply_ratio_refresh([9999]), 0)

print("\n40) Overwriting custom splits is opt-in")
check("custom excluded by default", len(db.ratio_refresh_preview([custom])), 0)
with_custom = db.ratio_refresh_preview([custom], include_custom=True)
check("custom appears when asked", len(with_custom), 1)
check("it reports its old mode", str(with_custom.iloc[0]["Sharing"]), "custom")
check("applying converts it", db.apply_ratio_refresh([custom], include_custom=True), 1)
check("it now carries the ratio", db.get_entry(custom)[1], {A: 90.0, B: 10.0})
check("and its mode says ratio",
      str(db.get_entries().set_index("id").loc[custom, "share_mode"]), "ratio")
check("the not-shared entry still resists",
      db.apply_ratio_refresh([unshared], include_custom=True), 0)
pos, frame = positions_by_name()
check("zero-sum holds throughout", float(frame["net"].sum()), 0.0, tol=0.0)

print("\n41) A refund and a foreign-currency entry reapply cleanly")
refund = db.save_entry(entry_date=date(2026, 12, 5), kind="expense", amount=-1_000.0,
                       category="Rent", description="refund", cash_partner_id=A,
                       share_mode="ratio")
usd = db.save_entry(entry_date=date(2026, 12, 6), kind="expense", amount=120.0,
                    currency="USD", category="Travel", description="usd leg",
                    cash_partner_id=A, share_mode="ratio")
db.update_partner(A, "Partner A", 60.0, 60.0, True)
db.update_partner(B, "Partner B", 40.0, 40.0, True)
touched = db.apply_ratio_refresh()
# ratio_1, ratio_2, the ex-custom entry, the refund and the USD leg - the
# not-shared entry is the only one left out
check("every ratio entry refreshed", touched, 5)
check("the refund kept its sign",
      float(db.get_entries().set_index("id").loc[refund, "amount"]), -1_000.0)
check("the refund took the new split", db.get_entry(refund)[1], {A: 60.0, B: 40.0})
check("the USD entry kept its currency",
      str(db.get_entries().set_index("id").loc[usd, "currency"]), "USD")
check("the USD entry took the new split", db.get_entry(usd)[1], {A: 60.0, B: 40.0})
pos, frame = positions_by_name()
check("zero-sum after refreshing mixed entries",
      float(frame["net"].sum()), 0.0, tol=0.0)

print("\n" + ("ALL CHECKS PASSED" if not FAILURES else f"FAILURES: {FAILURES}"))
raise SystemExit(1 if FAILURES else 0)
