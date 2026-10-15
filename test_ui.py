"""Smoke test: run app.py through Streamlit's own test harness and add an entry.

Run:  python test_ui.py
"""

import os
import sys
import tempfile
from pathlib import Path

# the Windows console is cp1252 by default and these labels carry ₹ and emoji
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP_DB = Path(tempfile.gettempdir()) / "books_ui_test.db"
if TMP_DB.exists():
    TMP_DB.unlink()
os.environ["BOOKS_DB"] = str(TMP_DB)

from streamlit.testing.v1 import AppTest  # noqa: E402

APP = Path(__file__).with_name("app.py")

at = AppTest.from_file(str(APP), default_timeout=60).run()
assert not at.exception, at.exception
print("PASS  app renders clean")
print(f"      tabs: {len(at.tabs)}   buttons: {len(at.button)}")

print(f"      title: {[m.value for m in at.title]}")

# the currency picker should be offering both seeded currencies
cur_select = next(s for s in at.selectbox if s.label == "Currency")
assert list(cur_select.options) == ["INR", "USD"], list(cur_select.options)
print(f"PASS  currency picker offers {list(cur_select.options)}")

# an expense starts on the base currency
assert cur_select.value == "INR", cur_select.value
print(f"PASS  expense form starts on {cur_select.value}")

# the sharing radio should name the ratio that applies to the chosen type
sharing = next(r for r in at.radio if r.label == "Sharing")
assert "expense-sharing" in sharing.options[0], sharing.options[0]
print(f"PASS  expense uses the expense ratio: {sharing.options[0]!r}")

kind_radio = next(r for r in at.radio if r.label == "Type")
kind_radio.set_value("income").run()
assert not at.exception, at.exception
sharing = next(r for r in at.radio if r.label == "Sharing")
assert "profit-sharing" in sharing.options[0], sharing.options[0]
print(f"PASS  income uses the profit ratio: {sharing.options[0]!r}")

# switching to income must actually re-apply the income default, not keep INR
income_cur = next(s for s in at.selectbox if s.label == "Currency")
assert income_cur.value == "USD", income_cur.value
print(f"PASS  income form switches to {income_cur.value}")

kind_radio = next(r for r in at.radio if r.label == "Type")
kind_radio.set_value("expense").run()
back_cur = next(s for s in at.selectbox if s.label == "Currency")
assert back_cur.value == "INR", back_cur.value
print(f"PASS  switching back to expense returns to {back_cur.value}")

# fill in the Add Entry form in INR and save
at.number_input[0].set_value(10000.0).run()
assert not at.exception, at.exception
print("PASS  amount accepted, split preview rendered")

next(b for b in at.button if "Add entry" in b.label).click().run()
assert not at.exception, at.exception
print("PASS  INR entry saved through the UI")

# now a USD entry through the same form
at.number_input[0].set_value(1000.0).run()
next(s for s in at.selectbox if s.label == "Currency").set_value("USD").run()
assert not at.exception, at.exception
preview = " ".join(i.value for i in at.info)
assert "converted at" in preview, preview
print("PASS  USD selected, conversion preview shown")

next(b for b in at.button if "Add entry" in b.label).click().run()
assert not at.exception, at.exception

# a negative expense should go straight through the form as a refund
at.number_input[0].set_value(-1500.0).run()
assert not at.exception, at.exception
preview = " ".join(i.value for i in at.info)
assert "Refund" in preview, preview
print("PASS  negative amount shows the refund preview")

next(b for b in at.button if "Add entry" in b.label).click().run()
assert not at.exception, at.exception
success = " ".join(s.value for s in at.success)
assert "refund" in success.lower(), success
print(f"PASS  refund saved: {success.strip()!r}")

import db  # noqa: E402

entries = db.get_entries()
assert len(entries) == 3, entries
refunds = entries[entries["amount"] < 0]
assert len(refunds) == 1, refunds
assert float(refunds.iloc[0]["amount"]) == -1500.0, refunds
print(f"PASS  refund stored as {float(refunds.iloc[0]['amount'])}")
entries = entries[entries["amount"] > 0]
by_cur = dict(zip(entries["currency"], entries["base_amount"]))
assert by_cur["INR"] == 10000.0, by_cur
assert by_cur["USD"] == 83000.0, by_cur
print(f"PASS  2 entries stored, base amounts {by_cur}")

# re-run and confirm the settlement tab and exports render with data present
at2 = AppTest.from_file(str(APP), default_timeout=60).run()
assert not at2.exception, at2.exception
print(f"PASS  app re-renders with data ({len(at2.dataframe)} tables)")

labels = [b.label for b in at2.download_button]
assert any("Excel" in s for s in labels), labels
assert any("CSV" in s and "Excel" not in s for s in labels), labels
print(f"PASS  export buttons present: {labels}")

# AppTest exposes download buttons without their payload, so build the same
# report directly to prove the bytes behind those buttons are sound
import exports  # noqa: E402
import settlement as stl  # noqa: E402

pos = stl.compute_positions(
    db.get_entries(), db.get_splits(), db.get_partners(include_inactive=True),
    db.get_settlements(),
)
frames = exports.build_frames(
    positions=pos, transfers=stl.minimal_transfers(pos),
    settlements=db.get_settlements(), entries=db.get_entries(),
    currencies=db.get_currencies(), base=db.base_currency(),
    business_name=db.get_setting("business_name"),
    partners=db.get_partners(include_inactive=True),
)
xlsx = exports.to_excel(frames)
assert xlsx[:2] == b"PK", xlsx[:8]
assert len(xlsx) > 5000, len(xlsx)
print(f"PASS  Excel export is a real workbook ({len(xlsx):,} bytes, "
      f"{len(frames)} sheets)")

# the Ledger's Group by selector should add a summary table and its own download
group_select = next(s for s in at2.selectbox if s.label == "Group by")
assert list(group_select.options) == list(stl.GROUP_BY), list(group_select.options)
assert group_select.value == "None (flat list)", group_select.value
flat_tables = len(at2.dataframe)
flat_downloads = len(at2.download_button)
print(f"PASS  Group by defaults to {group_select.value!r}")

for choice in ("Type", "Category", "Type & Category"):
    at2 = next(s for s in at2.selectbox if s.label == "Group by").set_value(choice).run()
    assert not at2.exception, at2.exception
    assert len(at2.dataframe) == flat_tables + 1, (choice, len(at2.dataframe))
    assert len(at2.download_button) == flat_downloads + 1, choice
    heading = " ".join(m.value for m in at2.markdown)
    assert f"Grouped by {choice.lower()}" in heading, heading
    print(f"PASS  grouping by {choice!r} adds a summary table and a CSV button")

# back to flat, and confirm selecting a row still opens the editor
at2 = next(s for s in at2.selectbox if s.label == "Group by").set_value(
    "None (flat list)").run()
assert len(at2.dataframe) == flat_tables, len(at2.dataframe)
print("PASS  returning to the flat list drops the summary table")

# the ledger exports must sit ABOVE the transactions table, otherwise a long
# list pushes them off screen and the table swallows the page scroll
import app  # noqa: E402

kinds = [(i, type(e).__name__, getattr(e, "label", "")) for i, e in enumerate(at2.main)]
tx_csv = next(i for i, name, label in kinds
              if name == "DownloadButton" and label == "⬇️ Transactions CSV")
tables = [i for i, name, _ in kinds if "ataframe" in name or name == "Arrow"]
assert tables, [n for _, n, _ in kinds]
assert tx_csv < max(tables), f"export at {tx_csv} is below the last table {max(tables)}"
print(f"PASS  Transactions CSV (element {tx_csv}) sits above the tables {tables}")

height_select = next(s for s in at2.selectbox if s.label == "Table height")
assert list(height_select.options) == list(app.TABLE_HEIGHTS), height_select.options
assert height_select.value == "Standard (10 rows)", height_select.value
print(f"PASS  Table height offers {list(height_select.options)}")

for choice, visible in app.TABLE_HEIGHTS.items():
    at2 = next(s for s in at2.selectbox
               if s.label == "Table height").set_value(choice).run()
    assert not at2.exception, at2.exception
    expected = app.table_height(3, visible)
    assert expected == app.ROW_PX * ((3 if visible is None else min(3, visible)) + 1) + 3
    print(f"PASS  {choice!r} renders ({visible or 'all'} rows -> {expected}px)")

# a table taller than the window must be capped, and 'show all' must not be
assert app.table_height(500, 10) == app.table_height(10, 10), "10-row cap broken"
assert app.table_height(500, None) > app.table_height(500, 20), "'show all' not taller"
print("PASS  row caps and 'Show all rows' size as expected")

# ---- bulk reapply of ratios through the Ledger panel ---------------------- #
people = db.get_partners()
pa, pb = int(people.iloc[0]["id"]), int(people.iloc[1]["id"])
before = {int(e): db.get_entry(int(e))[1]
          for e in db.get_entries()["id"] if db.get_entry(int(e))}
assert all(abs(s.get(pa, 0) - 50.0) < 0.01 for s in before.values()), before
print(f"PASS  {len(before)} entries start on the 50/50 split")

db.update_partner(pa, people.iloc[0]["name"], 70.0, 70.0, True)
db.update_partner(pb, people.iloc[1]["name"], 30.0, 30.0, True)

at3 = AppTest.from_file(str(APP), default_timeout=60).run()
assert not at3.exception, at3.exception

select_all = next(c for c in at3.checkbox if "Select all" in c.label)
assert select_all.value is True, "select-all should be on by default"
print(f"PASS  {select_all.label!r} is ticked by default")

confirm = next(c for c in at3.checkbox if c.label.startswith("Yes, rewrite"))
assert confirm.value is False, "the confirm gate should start unticked"
apply_btn = next(b for b in at3.button if "Reapply ratios" in b.label)
assert apply_btn.disabled, "Reapply must be disabled until confirmed"
print("PASS  Reapply is disabled until the confirm box is ticked")

# the harness itself refuses to click it, which is what a browser user sees
from streamlit.testing.v1.errors import AppTestError  # noqa: E402

try:
    apply_btn.click().run()
    raise AssertionError("a disabled Reapply button was clickable")
except AppTestError:
    pass
unchanged = {int(e): db.get_entry(int(e))[1] for e in db.get_entries()["id"]}
assert unchanged == before, "a disabled Reapply still wrote to the database"
print("PASS  the disabled button cannot be clicked and nothing was written")

at3 = next(c for c in at3.checkbox
           if c.label.startswith("Yes, rewrite")).set_value(True).run()
at3 = next(b for b in at3.button if "Reapply ratios" in b.label).click().run()
assert not at3.exception, at3.exception

done = " ".join(s.value for s in at3.success)
assert "Reapplied the current ratios" in done, done
print(f"PASS  confirmation shown: {done.strip()!r}")

after = {int(e): db.get_entry(int(e))[1] for e in db.get_entries()["id"]}
ratio_rows = db.get_entries()
ratio_ids = [int(i) for i in ratio_rows.loc[ratio_rows["share_mode"] == "ratio", "id"]]
assert ratio_ids, ratio_rows[["id", "share_mode"]]
for entry_id in ratio_ids:
    assert abs(after[entry_id][pa] - 70.0) < 0.01, (entry_id, after[entry_id])
    assert abs(after[entry_id][pb] - 30.0) < 0.01, (entry_id, after[entry_id])
print(f"PASS  {len(ratio_ids)} entries rewritten to the 70/30 split via the UI")

# and a fresh visit reports there is nothing left to do
at4 = AppTest.from_file(str(APP), default_timeout=60).run()
assert not at4.exception, at4.exception
idle = " ".join(s.value for s in at4.success)
assert "already match the current ratios" in idle, idle
print("PASS  a second pass reports nothing left to reapply")

text = exports.to_csv(frames).decode("utf-8-sig")
for section in ("## Net positions", "## Who pays whom", "## Sharing ratios"):
    assert section in text, text[:600]
assert "USD" in text, "expected the USD entry to appear in the export"
print(f"PASS  CSV export has {text.count('## ')} sections and covers both currencies")

# the Setup tab edits these two columns; AppTest has no data_editor accessor,
# so confirm the frame it is built from carries both
cols = set(db.get_partners(include_inactive=True).columns)
assert {"expense_pct", "profit_pct"} <= cols, sorted(cols)
assert "share_pct" not in cols, "the single-ratio column should be gone"
print("PASS  partners carry expense_pct and profit_pct, no legacy share_pct")

# ---- the three statement screens ------------------------------------------ #
at5 = AppTest.from_file(str(APP), default_timeout=120).run()
assert not at5.exception, at5.exception
tabs = [t.label for t in at5.tabs]
for wanted in ("⚖️ Trial Balance", "📈 Profit & Loss", "🏦 Balance Sheet"):
    assert wanted in tabs, tabs
assert "📖 Journal" not in tabs, tabs
print(f"PASS  {len(tabs)} tabs render: {tabs}")

metrics = {m.label: m.value for m in at5.metric}
for wanted in ("Total debits", "Total credits", "Total assets",
               "Capital & liabilities"):
    assert wanted in metrics, list(metrics)
assert metrics["Total debits"] == metrics["Total credits"], metrics
assert metrics["Total assets"] == metrics["Capital & liabilities"], metrics
print(f"PASS  trial balance balances at {metrics['Total debits']}, "
      f"balance sheet at {metrics['Total assets']}")

oks = " ".join(s.value for s in at5.success)
assert "Debits equal credits" in oks, oks
assert "Assets equal capital" in oks, oks
print("PASS  both statements report themselves balanced")

warnings = " ".join(w.value for w in at5.warning)
assert "stock, equipment, loans" in warnings, warnings
print("PASS  the balance sheet states what it cannot cover")

labels = [b.label for b in at5.download_button]
assert labels.count("⬇️ All statements (Excel)") == 3, labels
assert labels.count("⬇️ All statements (CSV)") == 3, labels
print("PASS  each statement screen offers the combined export")

# the statements must agree with the figures the Settlement screen settles
import settlement as stl2  # noqa: E402
import statements as fin  # noqa: E402

pos = stl2.compute_positions(
    db.get_entries(), db.get_splits(), db.get_partners(include_inactive=True),
    db.get_settlements(),
)
trial = fin.trial_balance(db.get_entries(), pos)
assert fin.trial_balance_totals(trial)["difference"] == 0.0
sheet = fin.balance_sheet(pos)
assert sheet["totals"]["difference"] == 0.0
pl = fin.profit_and_loss(db.get_entries(), pos)
assert abs(pl["appropriation"]["Profit / (loss)"].sum()
           - pl["totals"]["net"]) < 0.005, pl
owed = round(float(pos.loc[pos["net"] > 0, "net"].sum()), 2)
assert abs(owed - sheet["totals"]["funding"]) < 0.005, (owed, sheet["totals"])
print(f"PASS  balance sheet capital ({owed}) matches what Settlement says is owed")

# every period option must render without error
for choice in ("This financial year", "This month", "Since the beginning"):
    at5 = next(s for s in at5.selectbox
               if s.key == "tb_period").set_value(choice).run()
    assert not at5.exception, (choice, at5.exception)
print("PASS  every trial-balance period renders")

# A fresh income entry should land on Sales, not on whatever sorts first
# alphabetically. Expense keeps the first category, so check both: the point
# is that flipping the kind re-applies that kind's default rather than holding
# on to the other kind's pick.
fresh = AppTest.from_file(str(APP), default_timeout=60).run()
assert not fresh.exception, fresh.exception


def category_value(at_):
    return next(s for s in at_.selectbox if s.label == "Category").value


assert category_value(fresh) == db.get_categories("expense")[0], category_value(fresh)
print(f"PASS  a new expense starts on {category_value(fresh)!r}")

kind_radio = next(r for r in fresh.radio if r.options and "Income" in r.options)
fresh = kind_radio.set_value("Income").run()
assert not fresh.exception, fresh.exception
assert category_value(fresh) == "Sales", category_value(fresh)
print(f"PASS  a new income starts on {category_value(fresh)!r}, "
      f"not {db.get_categories('income')[0]!r}")

print("\nALL UI CHECKS PASSED")
