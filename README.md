# Partnership Books — expense & income tracker

A Streamlit app for a partnership firm's day-to-day books. Any partner can incur
an expense or collect income; the app tracks **whose pocket the cash came from**
separately from **who the amount economically belongs to**, then tells you who
owes whom.

## Running it

```bash
cd business-expense-tracker
pip install -r requirements.txt
streamlit run app.py
```

It opens at <http://localhost:8501>. Data lives in `data/tracker.db` (SQLite) —
back up that one file and you've backed up your books, or use **Setup → Backup
& restore**. Set the `BOOKS_DB` environment variable to point somewhere else
(e.g. a OneDrive folder).

## Deploying it

See **[DEPLOY.md](DEPLOY.md)**. In short: Streamlit Community Cloud for the app,
Neon for the database, both free.

Three settings control it, read from the environment locally and from
`st.secrets` on Streamlit Cloud:

| Variable | Effect |
| --- | --- |
| `BOOKS_DB_URL` | A Postgres connection string. Unset means SQLite. |
| `BOOKS_DB` | Where the SQLite file lives when no URL is set. |
| `BOOKS_PASSWORD` | When set, nothing renders until the password is given. Unset locally means no prompt. |

The storage layer targets SQLite and Postgres from one code path, so the local
and deployed behaviour are the same thing rather than two implementations.
`python run_postgres_tests.py` runs every suite against a throwaway Postgres to
keep it that way.

`python migrate_to_postgres.py "postgresql://..."` moves a local SQLite file
into Postgres and verifies the totals and both statements afterwards.

Move your books between machines with **Setup → Backup & restore**: download a
file from one, upload it to the other. A restore checks the file is really one
of ours, says how many entries it holds before touching anything, and copies
aside whatever it replaces.

## The four tabs

| Tab | What it does |
| --- | --- |
| **Add Entry** | One form for both expenses and income. Pick the currency, who paid/received, the category, and how it's shared. Expenses can be negative for refunds. A live preview shows what each partner bears — and the converted figure — before you save. |
| **Ledger** | Filterable list (all time / this month / this FY / custom range, plus a currency filter) with a column per partner showing their share of every line. Group by type and/or category for subtotals. Click a row to edit or delete it. CSV download included. |
| **Settlement** | Net position per party in the base currency, the minimum set of transfers to square up, a log of payments already made, and the Excel/CSV export. |
| **Trial Balance** | Every account with its debit or credit balance, as at a date. Proves the books hang together. |
| **Profit & Loss** | Income and expenses by category for a period, the result, and how it splits between the partners. |
| **Balance Sheet** | What the business holds and whose it is, as at a date. |
| **Setup** | Partner names with separate expense-sharing and profit-sharing percentages, currencies and exchange rates, categories, financial-year start month. |

## The three sharing modes

* **Per expense-sharing / profit-sharing ratio** — splits by the percentages from
  Setup. The default for most entries.
* **Custom split for this entry** — type your own percentages, must total 100.
  Use this for an expense shared by only two of three partners, or one borne
  entirely by someone who didn't pay for it.
* **Not shared** — the payer bears 100%. Their own expense, their own income.

Percentages are **frozen into the entry when you save it**. Changing a ratio
later affects new entries only; it never silently rewrites history — but when a
ratio was simply wrong, **Ledger → Reapply current ratios to existing
transactions** pushes the corrected one onto entries you already recorded.

### Reapplying a corrected ratio

The panel at the bottom of the Ledger does a bulk re-split:

* **Scope** — only the transactions in the current filtered view, or every one.
* **Select all** is ticked by default; untick it to pick individual entries.
* **Not shared** entries are never touched. The payer bears 100% of them by
  definition, so there is no ratio to apply.
* **Custom splits are left alone** unless you tick *Also overwrite custom
  splits*, since a custom split is a deliberate override. Turning it on converts
  those entries back into ratio entries.

Before anything is written you get a table of exactly which entries would move
and from what split to what split, a confirm checkbox, and only then an enabled
button. Entries already matching the current ratios are skipped, so the action
is idempotent — running it twice does nothing the second time.

It rewrites stored splits and will move the settlement balances, so it cannot be
undone. Copy `data/tracker.db` first if you want a way back.

## Two separate ratios

Each partner carries **two** percentages in Setup, and they need not match:

| Column | Applies to |
| --- | --- |
| **Expense %** | every ratio-based **expense** |
| **Profit %** | every ratio-based **income** |

So a partner can bear 70% of the costs while taking 40% of the profit. Each
column should total 100% on its own; the Setup tab shows a running total for
each and the app warns you if either is off. If a column doesn't total 100, that
column's entries are normalised pro-rata rather than refused.

The *Sharing* radio on the entry form names which ratio is in play and shows the
current percentages, switching as you flip between Expense and Income.

## Ledger layout

A Streamlit dataframe scrolls **inside its own box**, which has two consequences
worth knowing:

* With the pointer over the table, the wheel scrolls the table, not the page.
* Anything placed below a long table gets pushed off screen.

So the ledger's exports sit in a **toolbar above the tables**, never below them,
and **Table height** controls the box: `Standard (10 rows)`, `Tall (20 rows)` or
`Show all rows`. Pick *Show all rows* and the table renders at full height with
no inner scrollbar, so the page scrolls normally and you see every transaction
in one pass. When rows are being hidden, the caption under the table says so.

## Grouping the ledger

The **Group by** filter offers `None (flat list)`, `Type`, `Category` and
`Type & Category`. Pick one and a subtotal table appears above the transactions:

```
Category          Entries   Income   Expenses        Net   A net      B net
Freight                 1     0.00   40,000.00  -40,000.00  -20,000.00  -20,000.00
Marketplace            11     0.00   20,000.00  -20,000.00  -10,000.00  -10,000.00
Sales                   6  30,000.00     0.00    30,000.00   18,000.00   12,000.00
Stock (A only)          2     0.00   34,000.00  -34,000.00  -34,000.00        0.00
```

Subtotals are in the base currency, so a group containing both INR and USD lines
adds up correctly. A partner's column is their share of the group's income minus
their share of its expenses, carrying the same sign as **Net** — negative means
money went out. A group's partner columns always add back to its `Net`, and the
groups always add back to the period totals in the tiles above.

The transactions list stays below, re-sorted so rows read under their group, and
row selection still works for editing and deleting. The summary has its own CSV
download separate from the transactions one.

## Rounding

Shares are allocated in whole paise using the largest-remainder method, not by
rounding each partner's share on its own. Rounding independently drifts: ₹3,124.55
split 50/50 is ₹1,562.275 each, which rounds up on both sides to ₹3,124.56 and
quietly breaks the zero-sum guarantee the settlement depends on. Instead the odd
paisa is handed to one partner (ties going to whoever appears first, so results
are reproducible), and each entry's shares sum to its amount exactly. Negative
amounts are handled the same way.

## Refunds: negative expenses

An expense can be negative. That's how you record a refund, a credit note or a
correction — enter it as an expense in the same category with a minus sign:

```
19/09  Expense  Travel  ₹10,000.00   paid by Partner A   ratio
05/10  Expense  Travel  -₹2,500.00   paid by Partner A   ratio   one flight cancelled
```

The sign flows through everything. The cash the payer is out of pocket drops, so
does each partner's share of the burden, and the period's expense total nets the
refund off. The Ledger's Expenses tile tells you how many refund lines are
inside the figure. The entry form recognises a negative amount and labels the
preview as a refund so you can see the effect before saving.

Zero is still refused, and **income must be positive** — record a customer
refund as a negative expense, or ask and it can be opened up for income too.

## Entry numbering

Entry and settlement IDs are renumbered to stay contiguous: delete entry #2 of
five and the remaining entries become 1–4, with the next one you add taking #5.
Split rows follow their entry's new number automatically.

The trade-off: an ID is a position in the ledger, not a permanent handle. If you
exported a report that referred to entry #7 and later delete an earlier entry,
that #7 now points at a different entry. Where the exact entry matters, go by
date plus description, or keep the export.

## Currencies

Each entry is stored **in the currency it was incurred in** — ₹8,000 stays ₹8,000,
$500 stays $500. One currency is the **base** (INR out of the box), and everything
is converted to it for the settlement so there is a single net figure per partner
rather than one per currency.

Rates live in **Setup → Currencies & exchange rates** as "1 unit = ? base", e.g.
`USD → 83`. They are applied **live at read time**, not frozen into each entry, so
correcting a rate re-converts the whole history at once. That is the simple
behaviour you want for internal partner settlement; it does mean the books are
always valued at today's rate rather than the rate on each transaction date, so
for statutory accounts take an export at period end and keep it.

Changing the base currency re-expresses every stored rate against the new base
(an INR base with `USD = 83` becomes a USD base with `INR = 1/83`), so relative
values survive the switch.

The entry form pre-selects a currency per entry type — **INR for expenses, USD
for income** out of the box, since that's where the foreign receipts land. Both
are set in **Setup → Which currency the entry form starts on**, and it only
pre-selects the dropdown: any individual entry can still be switched to any
currency.

The Ledger shows `Amount` and the partner-share columns in each row's own
currency, with `Amount (BASE)` alongside; the Settlement tab is entirely in base
currency. Where more than one currency is in play, the Ledger grows a
**Breakdown by currency** panel so you can reconcile against each bank account.

## The three statements

The app records cash movements and burden splits rather than journal entries,
but those are enough to reconstruct a consistent set of accounts: an expense
account per expense category, an income account per income category, a firm
bank/cash account, and a current account per partner.

### Two partner balances, which are not the same thing

This is the one distinction worth understanding:

| | What it is | Where it appears |
| --- | --- | --- |
| **Cash movement** | Cash the partner put in, less cash they took out | Trial Balance — the income and expense accounts are still open alongside it |
| **Net position** | The same, *after* their share of income and expenses is allocated | Balance Sheet, and the Settlement screen |

They are tied by `net = cash movement − share of expenses + share of income`,
which is why both statements balance to the paise rather than approximately.

### Trial Balance

Cumulative as at a date, pre-appropriation, nil accounts omitted. Debits must
equal credits — the screen says so explicitly and shouts if they ever don't.

### Profit & Loss

For a period. Income and expenses by category, largest first, then the result
split per partner: **income on the profit-sharing ratio, expenses on the
expense-sharing ratio**. Because your two ratios differ, a partner's result is
their share of income less their share of costs, and that column adds back to
the net figure exactly.

### Balance Sheet

As at a date, post-appropriation. Assets are the firm's cash plus anything
*Due from* a partner whose net position is negative; capital and liabilities are
the partners' current accounts. A partner who owes the business shows up as a
receivable rather than as negative capital.

**What it cannot show:** stock, equipment, loans, or money owed by or to anyone
outside the partnership — there is nowhere to record those. The screen says so
too. Adding them would mean a proper asset/liability ledger.

### Period selection

The P&L covers a **period**; the Trial Balance and Balance Sheet are **as at**
a date, cumulative from the start of the books. That's standard, but it means
they only tie to the P&L when the period starts at the beginning of the books.

Each statement screen offers the whole set as one Excel workbook or one
sectioned CSV: Cover, Trial balance, Profit and loss, Profit appropriation,
Balance sheet.

## Exporting the settlement

**Settlement → Export this settlement** gives you either an Excel workbook or a
sectioned CSV, both covering the same eight tables:

| Sheet | Contents |
| --- | --- |
| Summary | Business, report date, base currency, totals, outstanding transfer count |
| Net positions | The full per-party breakdown with an `owed` / `owes` / `square` status |
| Who pays whom | The suggested transfers |
| Totals by currency | Income/expense/net per currency, in both original and base |
| Settlements paid | Every transfer already recorded between partners |
| Exchange rates | The rates the report was built with |
| Sharing ratios | Each partner's expense % and profit % at the time of export |
| Ledger | Every entry behind the numbers |

In the CSV the sections follow one another under `## Section name` header rows.
The **Exchange rates** sheet matters: it is the record of what the figures were
converted at, which is exactly the thing a partner might later query.

## The firm account

`Firm (Business Account)` is a built-in party. Use it as "paid by" / "received by"
when money moves through the business bank account rather than a partner's own
pocket. It moves cash but never carries a share of the burden, so income banked
by the firm shows up as the firm owing each partner their share — which is
exactly a pending drawing.

## How the net position is calculated

For each party, all figures converted to the base currency:

```
net = expenses whose cash they paid
    − their share of the expense burden
    + their share of the income
    − income that landed in their pocket
    + settlements they have already paid out
    − settlements they have already received
```

Positive means **owed money**, negative means **owes**. The column always sums to
zero, so if it doesn't, something is wrong with the data.

Non-shared entries cancel themselves out of the settlement (payer pays 100%,
payer bears 100%) while still counting in the income/expense totals and the P&L
numbers at the top of the Ledger.

Once partners actually transfer money, record it under **Settlement → Record a
settlement payment** (the *Record it* button next to each suggestion prefills the
form). Recorded settlements reduce the outstanding balances rather than being
treated as new business expenses.

## Tests

```bash
python test_books.py       # storage, splits, two ratios, multi-currency, ids, grouping, export
python test_migration.py   # upgrades an older database and verifies nothing is lost
python test_statements.py  # trial balance, P&L and balance sheet against hand-worked figures
python test_ui.py          # renders all 7 tabs and drives the forms end to end
python test_deploy.py      # password gate, backup/restore, backend selection, config
```

All of the above run on SQLite. To run them against Postgres as well — the
dialect the deployed app actually uses:

```bash
pip install pgserver
python run_postgres_tests.py
```

All three use throwaway databases in your temp folder and never touch
`data/tracker.db`. `test_books.py` asserts the zero-sum property after every
step, including across a base-currency switch — run it after touching the
settlement maths.

Note: don't run these with `python -I`; that strips the script's own directory
from `sys.path`, so `import db` fails.
