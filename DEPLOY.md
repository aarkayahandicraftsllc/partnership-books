# Deploying to Streamlit Community Cloud + Neon Postgres

Free hosting, free database, no monthly cost.

The one thing that matters: **Streamlit Cloud's filesystem is wiped on every
restart.** A SQLite file there would silently lose your books, so the data has
to live in a hosted Postgres. The app supports both backends from one code
path — SQLite locally, Postgres when `BOOKS_DB_URL` is set.

The app shows a red banner if it ever finds itself password-protected *and* on
SQLite, which is the signature of a misconfigured deployment.

---

## 1. Create the database (Neon)

1. Sign up at <https://neon.tech> (free tier, no card).
2. Create a project — name it `partnership-books`. Region: pick the one
   closest to whoever will use the app.
3. On the project dashboard, copy the **connection string**. It looks like:

   ```
   postgresql://user:password@ep-xyz-123.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
   ```

   **Use the pooled connection string** if Neon offers you a choice (the host
   usually contains `-pooler`). Neon's free tier scales to zero when idle; the
   pooled endpoint handles the wake-up better.

Supabase works the same way, but its free projects pause after a week of
inactivity, which is a worse fit for books you touch occasionally.

## 2. Move your books across

From this machine, with the connection string in quotes:

```bash
cd business-expense-tracker
python migrate_to_postgres.py "postgresql://...your string..."
```

It reads the local SQLite file, writes to Postgres, then re-reads the target and
checks the entry count, the total, and that the trial balance and balance sheet
still balance. It prints `MIGRATION OK` only if all of that holds.

Try `--dry-run` first if you want to see what it would move without writing.

## 3. Deploy the app

1. Go to <https://share.streamlit.io> and sign in with GitHub.
2. **New app** → pick `aarkayahandicraftsllc/partnership-books`, branch `main`,
   main file `app.py`.
3. The repo is private and owned by an org, so you may need to grant Streamlit
   access to **aarkayahandicraftsllc** specifically — not just your personal
   account.
4. Before deploying, open **Advanced settings → Secrets** and paste:

   ```toml
   BOOKS_DB_URL = "postgresql://...your string..."
   BOOKS_PASSWORD = "something long"
   ```

   There's a template at `.streamlit/secrets.toml.example`. Secrets are never
   committed — the real file is gitignored.
5. Deploy. The first build takes a few minutes.

## 4. Check it

* The app should show **the password box, not the books**. If you see the books
  without typing a password, `BOOKS_PASSWORD` didn't take.
* No red banner about SQLite. If you see one, `BOOKS_DB_URL` didn't take.
* After unlocking, the figures should match what you see locally:

  | Screen | Expected |
  | --- | --- |
  | Ledger | the same entry count as your local copy |
  | Trial Balance | equal debits and credits, difference 0.00 |
  | Balance Sheet | assets equal capital, difference 0.00 |

  Compare the actual totals against your local app side by side — they should
  match to the paise.

## Things to know

**It will feel slower than local.** Every page interaction runs a few dozen
small queries, and each is now a network round trip instead of a local file
read. Connections are pooled so there's no handshake per query, but expect a
noticeable pause rather than the instant response you get locally.

**Neon sleeps.** On the free tier the database suspends when idle, so the first
request after a quiet spell takes a second or two longer. `pool_pre_ping`
quietly replaces connections it finds dead.

**Free tier limits.** Neon's free storage is far more than these books will
ever need — the whole database is under 100 KB. Watch the compute hours if you
leave the app open all day.

**Take backups.** **Setup → Backup & restore → Download a backup** gives you a
small compressed file of every table. A managed database is not a backup.

**Redeploys** happen automatically when you push to `main`. Your data is in
Neon, untouched by deploys.

---

## Running locally

Nothing changes. No `BOOKS_DB_URL` means SQLite at `data/tracker.db`, and no
`BOOKS_PASSWORD` means no prompt:

```bash
streamlit run app.py
```

To rehearse the deployed setup:

```bash
BOOKS_DB_URL="postgresql://..." BOOKS_PASSWORD='something' streamlit run app.py
```

## Testing both backends

The suites default to SQLite so they're fast and need no setup:

```bash
python test_books.py && python test_statements.py && python test_ui.py
python test_deploy.py && python test_migration.py
```

That would leave Postgres — the deployed dialect — as the less-tested path, so
there's a runner that starts a throwaway Postgres and runs everything against
it:

```bash
pip install pgserver
python run_postgres_tests.py
```

Or point it at a real database:

```bash
BOOKS_TEST_PG_URL="postgresql://..." python run_postgres_tests.py
```

Use a scratch database for that, not the one holding your books — each suite
drops and recreates its own schema.
