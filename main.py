"""Expense tracker MCP server (FastMCP) that stores data in Postgres.

Horizon's filesystem is ephemeral, so expenses are kept in an external
database. Set the DATABASE_URL environment variable (for example a Neon or
Supabase connection string) in Horizon -> Settings -> Environment Variables.
"""

import asyncio
import json
import os
from datetime import date as date_type
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

CATEGORIES_PATH = Path(__file__).parent / "categories.json"

mcp = FastMCP(
    "ExpenseTracker",
    instructions=(
        "Tracks personal expenses in a database. Dates are YYYY-MM-DD. "
        "Use lowercase category and subcategory keys from the "
        "expense:///categories resource (for example category 'food', "
        "subcategory 'snacks'). Amounts are plain numbers in the user's own currency."
    ),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id          BIGSERIAL PRIMARY KEY,
    date        DATE NOT NULL,
    amount      NUMERIC(14, 2) NOT NULL,
    category    TEXT NOT NULL,
    subcategory TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses (date);
"""

_pool = None
_pool_lock = asyncio.Lock()


def _clean_dsn(url: str) -> str:
    """Drop query options asyncpg does not understand (Neon adds channel_binding)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "channel_binding"]
    return urlunsplit(parts._replace(query=urlencode(query)))


async def _get_pool():
    """Create the connection pool on first use, not at import time."""
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            url = os.environ.get("DATABASE_URL")
            if not url:
                raise ToolError(
                    "DATABASE_URL is not set. Add it in Horizon under "
                    "Settings -> Environment Variables, then redeploy."
                )
            try:
                pool = await asyncpg.create_pool(
                    _clean_dsn(url),
                    min_size=1,
                    max_size=3,
                    # Needed when the host sits behind a connection pooler (pgbouncer).
                    statement_cache_size=0,
                    command_timeout=30,
                )
                async with pool.acquire() as conn:
                    await conn.execute(SCHEMA)
            except (OSError, asyncpg.PostgresError) as e:
                raise ToolError(f"Could not connect to the database: {e}")
            _pool = pool
    return _pool


def _parse_date(value: str, field: str) -> date_type:
    try:
        return date_type.fromisoformat(value)
    except (TypeError, ValueError):
        raise ToolError(f"{field} must be a date in YYYY-MM-DD format, got {value!r}.")


def _parse_amount(value) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ToolError(f"amount must be a number, got {value!r}.")
    if not amount.is_finite() or amount <= 0:
        raise ToolError("amount must be a number greater than 0.")
    return amount.quantize(Decimal("0.01"))


def _key(value: str) -> str:
    """Normalise category names so 'Food' and 'food' are the same category."""
    return (value or "").strip().lower()


def _row(r) -> dict:
    return {
        "id": r["id"],
        "date": r["date"].isoformat(),
        "amount": float(r["amount"]),
        "category": r["category"],
        "subcategory": r["subcategory"],
        "note": r["note"],
    }


@mcp.tool()
async def add_expense(
    date: str,
    amount: float,
    category: str,
    subcategory: str = "",
    note: str = "",
) -> dict:
    """Add a new expense entry to the database. Date format: YYYY-MM-DD."""
    d = _parse_date(date, "date")
    amt = _parse_amount(amount)
    cat = _key(category)
    if not cat:
        raise ToolError("category is required.")
    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            expense_id = await conn.fetchval(
                "INSERT INTO expenses (date, amount, category, subcategory, note) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING id",
                d, amt, cat, _key(subcategory), note.strip(),
            )
    except (OSError, asyncpg.PostgresError) as e:
        raise ToolError(f"Database error: {e}")
    return {"status": "success", "id": expense_id, "message": "Expense added successfully"}


@mcp.tool()
async def list_expenses(
    start_date: str,
    end_date: str,
    category: str | None = None,
) -> list[dict]:
    """List expense entries within an inclusive date range, newest first."""
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    query = (
        "SELECT id, date, amount, category, subcategory, note "
        "FROM expenses WHERE date BETWEEN $1 AND $2"
    )
    params: list = [start, end]
    if category:
        query += " AND category = $3"
        params.append(_key(category))
    query += " ORDER BY date DESC, id DESC"
    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
    except (OSError, asyncpg.PostgresError) as e:
        raise ToolError(f"Database error: {e}")
    return [_row(r) for r in rows]


@mcp.tool()
async def summarize(
    start_date: str,
    end_date: str,
    category: str | None = None,
) -> list[dict]:
    """Summarize expenses by category within an inclusive date range."""
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    query = (
        "SELECT category, SUM(amount) AS total_amount, COUNT(*) AS count "
        "FROM expenses WHERE date BETWEEN $1 AND $2"
    )
    params: list = [start, end]
    if category:
        query += " AND category = $3"
        params.append(_key(category))
    query += " GROUP BY category ORDER BY total_amount DESC"
    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
    except (OSError, asyncpg.PostgresError) as e:
        raise ToolError(f"Database error: {e}")
    return [
        {"category": r["category"], "total_amount": float(r["total_amount"]), "count": r["count"]}
        for r in rows
    ]


@mcp.tool()
async def delete_expense(expense_id: int) -> dict:
    """Delete one expense by its id (use list_expenses to find the id)."""
    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM expenses WHERE id = $1 RETURNING id", expense_id
            )
    except (OSError, asyncpg.PostgresError) as e:
        raise ToolError(f"Database error: {e}")
    if deleted is None:
        raise ToolError(f"No expense with id {expense_id}.")
    return {"status": "success", "id": deleted, "message": "Expense deleted"}


@mcp.resource("expense:///categories", mime_type="application/json")
def categories() -> str:
    """Category and subcategory keys to use when adding expenses."""
    try:
        return CATEGORIES_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return json.dumps(
            {"misc": ["uncategorized", "other"]}, indent=2
        )


# Horizon ignores this block and starts the server itself; it is for local runs.
if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
