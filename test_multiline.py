"""Several amounts in one go: each line its own entry, its own sharing.

Checks the data layer (db.save_entries) and that the Add tab offers the grid.

Run:  python test_multiline.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP_DB = Path(tempfile.gettempdir()) / "books_multiline_test.db"
if TMP_DB.exists():
    TMP_DB.unlink()
os.environ["BOOKS_DB"] = str(TMP_DB)

import db  # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILED.append(label)


db.init_db()
partners = db.get_partners()
sharers = partners[partners["is_firm"] == 0]
first = int(sharers.iloc[0]["id"])
second = int(sharers.iloc[1]["id"])
cash = int(partners.iloc[0]["id"])
income_cat = db.get_categories("income")[0]

print("1) Three lines, three different sharing choices, one call")
lines = [
    {"entry_date": "2026-02-01", "kind": "income", "amount": 25000.0,
     "category": "Sales", "description": "batch", "cash_partner_id": cash,
     "share_mode": "ratio", "custom_split": None},
    {"entry_date": "2026-02-01", "kind": "income", "amount": 10000.0,
     "category": "Sales", "description": "batch", "cash_partner_id": cash,
     "share_mode": "custom", "custom_split": {first: 100.0, second: 0.0}},
    {"entry_date": "2026-02-01", "kind": "income", "amount": 4500.0,
     "category": "Sales", "description": "batch", "cash_partner_id": cash,
     "share_mode": "none", "custom_split": None},
]
ids = db.save_entries(lines)
check("three ids come back", len(ids), 3)
check("the ids are distinct", len(set(ids)), 3)

entries = db.get_entries()
check("three entries exist", len(entries), 3)
check("each carries its own amount",
      sorted(float(a) for a in entries["amount"]), [4500.0, 10000.0, 25000.0])
check("each kept its own share mode",
      sorted(entries["share_mode"]), ["custom", "none", "ratio"])
check("they share the one category", set(entries["category"]), {"Sales"})
check("and the one date", set(entries["entry_date"]), {"2026-02-01"})

print("\n2) The splits are per line, not shared")
splits = db.get_splits([int(i) for i in ids])
by_entry = {int(e): dict(zip(g["partner_id"].astype(int), g["pct"].astype(float)))
            for e, g in splits.groupby("entry_id")}
custom_id = int(entries.loc[entries["share_mode"] == "custom", "id"].iloc[0])
check("the custom line is 100% to one partner",
      by_entry[custom_id].get(first), 100.0)
ratio_id = int(entries.loc[entries["share_mode"] == "ratio", "id"].iloc[0])
check("the ratio line follows the ratio, so more than one partner",
      len([p for p, v in by_entry[ratio_id].items() if v > 0]) > 1, True)
none_id = int(entries.loc[entries["share_mode"] == "none", "id"].iloc[0])
check("the not-shared line is all on the cash partner",
      by_entry[none_id], {cash: 100.0})

print("\n3) A bad line stops the whole batch - nothing half-saved")
before = len(db.get_entries())
bad = [
    {"entry_date": "2026-02-02", "kind": "income", "amount": 500.0,
     "category": income_cat, "description": "", "cash_partner_id": cash,
     "share_mode": "ratio", "custom_split": None},
    {"entry_date": "2026-02-02", "kind": "income", "amount": -500.0,
     "category": income_cat, "description": "", "cash_partner_id": cash,
     "share_mode": "ratio", "custom_split": None},
]
try:
    db.save_entries(bad)
    check("it refused the batch", "saved", "raised ValueError")
except ValueError as exc:
    check("it refused the batch", "raised ValueError", "raised ValueError")
    check("and said which line", str(exc).startswith("Line 2:"), True)
check("nothing was written", len(db.get_entries()), before)

print("\n4) An empty batch is a no-op")
check("no ids, no error", db.save_entries([]), [])

print("\n5) The Add tab offers the grid, and it saves through the UI")
from streamlit.testing.v1 import AppTest  # noqa: E402

at = AppTest.from_file(str(Path(__file__).with_name("app.py")),
                       default_timeout=120).run()
assert not at.exception, at.exception
toggles = [t for t in at.toggle if "Several amounts" in t.label]
check("the toggle is there", len(toggles), 1)

at = toggles[0].set_value(True).run()
assert not at.exception, at.exception

# Scope to the Add tab: every tab body renders on every run, so at.selectbox
# would also pick up Setup's Category and at.number_input Settlement's Amount.
add_tab = at.tabs[0]
labels = [s.label for s in add_tab.selectbox]
check("the header fields are asked once each",
      sorted(l for l in labels if l in ("Category", "Currency")),
      ["Category", "Currency"])
check("the single-amount box is gone from the Add tab",
      [ni.label for ni in add_tab.number_input if ni.label == "Amount"], [])

# An empty grid must not offer a save. AppTest has no way to type into a
# data_editor, so the end-to-end grid save is NOT covered here - the button
# state and db.save_entries above are what is checked.
add_buttons = [b for b in at.tabs[0].button]
save = [b for b in add_buttons if "entr" in b.label]
check("the Add tab offers one save button", len(save), 1)
check("and it is disabled while the grid is empty",
      bool(getattr(save[0], "disabled", False)), True)

# re-find the toggle on the CURRENT run: an element handle from an earlier
# run points at widget state that no longer exists
back = next(t for t in at.toggle if "Several amounts" in t.label)
at2 = back.set_value(False).run()
assert not at2.exception, at2.exception
check("turning it off brings the single form back",
      [ni.label for ni in at2.tabs[0].number_input if ni.label == "Amount"],
      ["Amount"])

print()
print("6) The grid seeds the percentage columns from Setup, per kind")
# Give the two kinds different ratios so a mix-up cannot pass unnoticed.
rows = db.get_partners()
rows = rows[rows["is_firm"] == 0]
a, b = int(rows.iloc[0]["id"]), int(rows.iloc[1]["id"])
db.update_partner(a, str(rows.iloc[0]["name"]), 70.0, 55.0, True)
db.update_partner(b, str(rows.iloc[1]["name"]), 30.0, 45.0, True)

import app  # noqa: E402  (imported bare; we only want the module's helpers)


def seeded(kind):
    partners_now = db.get_partners()
    sharers_now = partners_now[partners_now["is_firm"] == 0]
    column = db.RATIO_COLUMN[kind]
    return {int(r.id): float(getattr(r, column)) for r in sharers_now.itertuples()}


check("expense seeds come from expense_pct", seeded("expense"), {a: 70.0, b: 30.0})
check("income seeds come from profit_pct", seeded("income"), {a: 55.0, b: 45.0})
check("and they differ, so the kind really is honoured",
      seeded("expense") != seeded("income"), True)

# what the app renders must agree with those, for each kind in turn
fresh = AppTest.from_file(str(Path(__file__).with_name("app.py")),
                          default_timeout=120).run()
toggle = next(t for t in fresh.toggle if "Several amounts" in t.label)
fresh = toggle.set_value(True).run()
assert not fresh.exception, fresh.exception
# AppTest surfaces st.data_editor under .dataframe, not .data_editor
grid = fresh.tabs[0].dataframe[0]
first_row = grid.value.iloc[0].to_dict()
names_now = dict(zip(rows["id"].astype(int), rows["name"]))
check("the grid's first row defaults to Ratio", first_row["Sharing"], "Ratio")
check(f"and shows the expense ratio for {names_now[a]}",
      float(first_row[f"{names_now[a]} %"]), 70.0)

# format_func=str.capitalize means the options read "Expense"/"Income"
kind_radio = next(r for r in fresh.tabs[0].radio if r.label == "Type")
fresh = kind_radio.set_value("Income").run()
assert not fresh.exception, fresh.exception
# AppTest surfaces st.data_editor under .dataframe, not .data_editor
grid = fresh.tabs[0].dataframe[0]
first_row = grid.value.iloc[0].to_dict()
check(f"flipping to income re-seeds {names_now[a]} to the profit ratio",
      float(first_row[f"{names_now[a]} %"]), 55.0)

print()
print("7) The grid's % columns show the split each row will really use")
partners_now = db.get_partners()
sharer_ids = [a, b]
firm_id = int(partners_now[partners_now["is_firm"] == 1].iloc[0]["id"])

check("Ratio on an expense shows expense_pct",
      app.effective_pct("ratio", a, {}, partners_now, "expense", sharer_ids),
      {a: 70.0, b: 30.0})
check("Ratio on income shows profit_pct",
      app.effective_pct("ratio", a, {}, partners_now, "income", sharer_ids),
      {a: 55.0, b: 45.0})
check("Not shared puts 100 on whoever handled the cash",
      app.effective_pct("none", a, {}, partners_now, "expense", sharer_ids),
      {a: 100.0, b: 0.0})
check("Not shared follows the cash partner, not the first partner",
      app.effective_pct("none", b, {}, partners_now, "expense", sharer_ids),
      {a: 0.0, b: 100.0})
check("Not shared on the firm shows nothing against the sharers",
      app.effective_pct("none", firm_id, {}, partners_now, "expense", sharer_ids),
      {a: 0.0, b: 0.0})
check("Custom shows what was typed",
      app.effective_pct("custom", a, {a: 25.0, b: 75.0}, partners_now,
                        "expense", sharer_ids),
      {a: 25.0, b: 75.0})
check("Custom treats a blank cell as zero",
      app.effective_pct("custom", a, {a: 100.0, b: None}, partners_now,
                        "expense", sharer_ids),
      {a: 100.0, b: 0.0})

db.enable_read_cache(False)
print()
if FAILED:
    print(f"FAILURES: {FAILED}")
    raise SystemExit(1)
print("ALL MULTILINE CHECKS PASSED")
