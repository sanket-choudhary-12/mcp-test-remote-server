# Expense Tracker MCP server

A FastMCP server that lets Claude add and read your expenses. Data is stored in
Postgres, so it survives restarts and redeploys on Prefect Horizon, and you can
open it in your database provider's dashboard as well as through Claude.

## Tools

| Tool | What it does |
|------|--------------|
| `add_expense(date, amount, category, subcategory, note)` | Save an expense (`date` is `YYYY-MM-DD`) |
| `list_expenses(start_date, end_date, category)` | List expenses in a date range, newest first |
| `summarize(start_date, end_date, category)` | Totals per category in a date range |
| `delete_expense(expense_id)` | Remove one expense by id |

The `expense:///categories` resource serves `categories.json`.

## Setup

1. Create a free Postgres database at [Neon](https://neon.tech) (or Supabase) and copy the connection string.
2. In Horizon, open your server, go to **Settings -> Environment Variables** and add `DATABASE_URL` with that string.
3. Redeploy. Changing an environment variable only takes effect on a new build and deploy.
4. The `expenses` table is created automatically on the first call.

## Viewing your data outside Claude

- **Neon:** Tables page, or the SQL Editor, for example `SELECT * FROM expenses ORDER BY date DESC;`
- **Supabase:** Table Editor.
- Any SQL client (TablePlus, DBeaver, psql) works with the same connection string.

## Run locally

```bash
export DATABASE_URL="postgresql://user:password@host/dbname?sslmode=require"
uv run python main.py
```
