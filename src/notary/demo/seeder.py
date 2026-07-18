"""Deterministic fiction-retail demo warehouse with planted catalog lies.

The manifest is the single ground truth for S7 evaluation: every catalog
description entry is either a planted lie (the description contradicts the
data) or a truthful control (it matches). The data side is generated with a
seeded RNG so the same seed yields a byte-identical warehouse.
"""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb

from notary.types import ClaimType

DEFAULT_SEED = 20260718


@dataclass(frozen=True)
class CatalogEntry:
    """One described column: the description Notary will read from DataHub."""

    table: str
    column: str | None  # None = table-level description
    description: str
    planted_lie: bool
    claim_type: ClaimType | None  # the claim type the entry exercises
    truth_note: str  # what the data actually does (for the eval report)


@dataclass(frozen=True)
class Manifest:
    claims: tuple[CatalogEntry, ...]

    def to_json(self) -> str:
        rows = []
        for c in self.claims:
            d = asdict(c)
            d["claim_type"] = c.claim_type.value if c.claim_type else None
            rows.append(d)
        return json.dumps({"claims": rows}, indent=1)


# --- catalog entries: 12 planted lies spanning all 5 claim types + controls ---

ENTRIES: tuple[CatalogEntry, ...] = (
    # fct_payments
    CatalogEntry("fct_payments", "amount",
                 "Transaction amount in USD.",
                 True, ClaimType.UNIT_SCALE,
                 "stored as integer cents (median ~= 12800, no fractional part)"),
    CatalogEntry("fct_payments", "currency",
                 "ISO-4217 currency code, one of {USD, EUR, GBP}.",
                 False, ClaimType.DOMAIN_ENUM,
                 "values are exactly USD/EUR/GBP"),
    # dim_customers
    CatalogEntry("dim_customers", "email",
                 "Customer email address. Never null.",
                 True, ClaimType.COMPLETENESS,
                 "~5 percent of rows are null"),
    CatalogEntry("dim_customers", "country_code",
                 "ISO-3166 alpha-2 country code.",
                 False, ClaimType.DOMAIN_ENUM,
                 "all values are valid alpha-2 codes"),
    CatalogEntry("dim_customers", "segment",
                 "Customer segment, one of {consumer, business}.",
                 True, ClaimType.DOMAIN_ENUM,
                 "contains a third value: partner"),
    # fct_orders
    CatalogEntry("fct_orders", "discount_pct",
                 "Discount percentage between 0 and 100.",
                 True, ClaimType.UNIT_SCALE,
                 "stored as a 0-1 fraction"),
    CatalogEntry("fct_orders", "status",
                 "Order status, one of {placed, shipped, delivered, returned}.",
                 False, ClaimType.DOMAIN_ENUM,
                 "values are exactly the four listed"),
    CatalogEntry("fct_orders", "shipped_at",
                 "Shipment timestamp. Always populated.",
                 True, ClaimType.COMPLETENESS,
                 "~10 percent null (unshipped orders)"),
    # fct_sessions_daily
    CatalogEntry("fct_sessions_daily", None,
                 "Daily session rollup. Updated daily.",
                 True, ClaimType.FRESHNESS,
                 "latest event_date is ~51 days stale"),
    CatalogEntry("fct_sessions_daily", "duration_ms",
                 "Session duration in milliseconds.",
                 True, ClaimType.UNIT_SCALE,
                 "stored in seconds (median ~= 480)"),
    # legacy_orders_v1
    CatalogEntry("legacy_orders_v1", None,
                 "DEPRECATED 2025: superseded by fct_orders. No longer used.",
                 True, ClaimType.DEPRECATION_USAGE,
                 "query history shows active daily reads"),
    # dim_products
    CatalogEntry("dim_products", "price_usd",
                 "List price in USD.",
                 False, ClaimType.UNIT_SCALE,
                 "dollar magnitudes with cent fractions"),
    CatalogEntry("dim_products", "weight_grams",
                 "Item shipping weight in grams.",
                 True, ClaimType.UNIT_SCALE,
                 "stored in kilograms (median ~= 6)"),
    # fct_refunds
    CatalogEntry("fct_refunds", "amount_cents",
                 "Refund amount in integer cents.",
                 False, ClaimType.UNIT_SCALE,
                 "integer cents as described"),
    CatalogEntry("fct_refunds", "reason",
                 "Refund reason. Required field.",
                 True, ClaimType.COMPLETENESS,
                 "~15 percent null"),
    # stg_inventory
    CatalogEntry("stg_inventory", None,
                 "Live warehouse inventory, refreshed hourly.",
                 True, ClaimType.FRESHNESS,
                 "snapshot_at is ~30 days stale"),
    CatalogEntry("stg_inventory", "qty",
                 "Units on hand. Non-negative.",
                 True, ClaimType.DOMAIN_ENUM,
                 "contains negative oversell values"),
)

MANIFEST = Manifest(claims=ENTRIES)

# frozen "today" so freshness lies are stable relative to generated data;
# the demo narrative treats this as the run date.
ANCHOR_DATE = "2026-07-18"


def build_warehouse(db_path: str | Path, seed: int = DEFAULT_SEED) -> Manifest:
    """Create the fiction-retail DuckDB warehouse. Deterministic per seed."""
    rng = random.Random(seed)
    db_path = Path(db_path)
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    try:
        # single transaction + bulk VALUES inserts; see _bulk_insert
        con.execute("begin transaction")
        _seed_customers(con, rng)
        _seed_products(con, rng)
        _seed_orders(con, rng)
        _seed_payments(con, rng)
        _seed_refunds(con, rng)
        _seed_sessions(con, rng)
        _seed_legacy_orders(con, rng)
        _seed_inventory(con, rng)
        con.execute("commit")
    finally:
        con.close()
    return MANIFEST


def _sql_lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return repr(v)


def _bulk_insert(con: duckdb.DuckDBPyConnection, table: str, rows: list[tuple]) -> None:
    """Single multi-VALUES INSERT: duckdb executemany is ~22ms/row (measured),
    a single statement is ~45x faster for the same rows."""
    if not rows:
        return
    values = ",".join("(" + ",".join(_sql_lit(v) for v in r) + ")" for r in rows)
    con.execute(f"insert into {table} values {values}")


def _seed_customers(con: duckdb.DuckDBPyConnection, rng: random.Random) -> None:
    countries = ["US", "GB", "DE", "FR", "CA", "AU", "JP", "BR"]
    segments = ["consumer"] * 70 + ["business"] * 25 + ["partner"] * 5
    rows = []
    for i in range(1, 501):
        email = None if rng.random() < 0.05 else f"user{i}@example.com"
        rows.append((i, email, rng.choice(countries), rng.choice(segments)))
    con.execute(
        "create table dim_customers (customer_id int, email varchar, "
        "country_code varchar, segment varchar)"
    )
    _bulk_insert(con, "dim_customers", rows)


def _seed_products(con: duckdb.DuckDBPyConnection, rng: random.Random) -> None:
    rows = []
    for i in range(1, 81):
        price = round(rng.uniform(4.99, 249.99), 2)
        weight_kg = round(rng.uniform(0.1, 12.0), 2)  # kg despite the name
        rows.append((i, f"product-{i}", price, weight_kg))
    con.execute(
        "create table dim_products (product_id int, name varchar, "
        "price_usd double, weight_grams double)"
    )
    _bulk_insert(con, "dim_products", rows)


def _seed_orders(con: duckdb.DuckDBPyConnection, rng: random.Random) -> None:
    statuses = ["placed", "shipped", "delivered", "returned"]
    rows = []
    for i in range(1, 2001):
        status = rng.choices(statuses, weights=[15, 25, 55, 5])[0]
        discount = round(rng.choice([0.0, 0.05, 0.1, 0.15, 0.2]), 2)
        day = rng.randrange(1, 180)
        placed = f"2026-{1 + day // 30:02d}-{1 + day % 28:02d} 12:00:00"
        shipped = None if rng.random() < 0.10 else placed
        rows.append((i, rng.randrange(1, 501), status, discount, placed, shipped))
    con.execute(
        "create table fct_orders (order_id int, customer_id int, status varchar, "
        "discount_pct double, placed_at timestamp, shipped_at timestamp)"
    )
    _bulk_insert(con, "fct_orders", rows)


def _seed_payments(con: duckdb.DuckDBPyConnection, rng: random.Random) -> None:
    currencies = ["USD"] * 80 + ["EUR"] * 15 + ["GBP"] * 5
    rows = []
    for i in range(1, 2001):
        cents = rng.randrange(499, 24999)  # integer cents: the S1 lie
        rows.append((i, i, cents, rng.choice(currencies)))
    con.execute(
        "create table fct_payments (payment_id int, order_id int, "
        "amount bigint, currency varchar)"
    )
    _bulk_insert(con, "fct_payments", rows)


def _seed_refunds(con: duckdb.DuckDBPyConnection, rng: random.Random) -> None:
    reasons = ["damaged", "wrong-item", "late", "changed-mind"]
    rows = []
    for i in range(1, 201):
        reason = None if rng.random() < 0.15 else rng.choice(reasons)
        rows.append((i, rng.randrange(1, 2001), rng.randrange(499, 24999), reason))
    con.execute(
        "create table fct_refunds (refund_id int, order_id int, "
        "amount_cents bigint, reason varchar)"
    )
    _bulk_insert(con, "fct_refunds", rows)


def _seed_sessions(con: duckdb.DuckDBPyConnection, rng: random.Random) -> None:
    # latest generated date is 2026-05-28, ~51 days before ANCHOR_DATE:
    # the freshness lie ("updated daily")
    rows = []
    for day in range(90):
        month = 3 + day // 30  # months 3-5 only
        dom = 1 + day % 30
        date = f"2026-{month:02d}-{min(dom, 28):02d}"
        rows.append((date, rng.randrange(800, 4000),
                     rng.randrange(120, 900)))  # seconds despite duration_ms
    con.execute(
        "create table fct_sessions_daily (event_date date, sessions int, "
        "duration_ms int)"
    )
    _bulk_insert(con, "fct_sessions_daily", rows)


def _seed_legacy_orders(con: duckdb.DuckDBPyConnection, rng: random.Random) -> None:
    rows = [(i, rng.randrange(1, 501), rng.randrange(499, 9999))
            for i in range(1, 301)]
    con.execute(
        "create table legacy_orders_v1 (order_id int, customer_id int, "
        "total_cents bigint)"
    )
    _bulk_insert(con, "legacy_orders_v1", rows)


def _seed_inventory(con: duckdb.DuckDBPyConnection, rng: random.Random) -> None:
    rows = []
    for i in range(1, 81):
        qty = rng.randrange(-5, 500)
        rows.append((i, qty, "2026-06-18 06:00:00"))  # ~30 days stale
    # plant the non-negative lie deterministically: oversold items exist
    # regardless of what the RNG happened to draw (review finding: the lie
    # was not actually planted under the default seed)
    for idx, oversell in ((6, -3), (22, -1), (60, -5)):
        rows[idx] = (rows[idx][0], oversell, rows[idx][2])
    con.execute(
        "create table stg_inventory (product_id int, qty int, "
        "snapshot_at timestamp)"
    )
    _bulk_insert(con, "stg_inventory", rows)
