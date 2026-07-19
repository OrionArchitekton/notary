# Notary evidence dossier

- Asset: urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)
- Field: amount
- Claim (unit_scale): "Transaction amount in USD."
- Verdict: CONTRADICTED
- Run date: 2026-07-18
- Rationale: described as USD but every value is an integer with median 12795; consistent with integer cents, not dollars

## Description pre-image (before any Notary correction)

Transaction amount in USD.

## Probe SQL

```sql
with base as (select median(v) as median, avg(case when v is null then null when v = floor(v) then 1.0 else 0.0 end) as integer_share, avg(case when v is null then null when v * 100 = floor(v * 100) then 1.0 else 0.0 end) as centi_integer_share, min(v) as min, max(v) as max, count(v) as row_count, count(*) as rows_scanned, 100000 as scan_limit from (select "amount" as v from "fct_payments" limit 100000)), suspect as (select "order_id" as k, "amount" as v from "fct_payments" limit 100000), recon as (select count(*) as recon_joined, count(distinct s.k) as recon_matched_keys, avg(case when r."total_major" is null then 0.0 when r."total_major" = 0 then 0.0 when abs(s.v / r."total_major" - 100.0) <= 1e-6 then 1.0 else 0.0 end) as recon_ratio_share from suspect s join (select "order_id" as rk, "total_major" from "billing_invoices" limit 100000) r on s.k = r.rk where s.v is not null), recon_suspect as (select count(distinct k) as recon_suspect_keys, count(*) as recon_suspect_rows from suspect where v is not null)  select base.*, recon.recon_joined, recon.recon_matched_keys, recon.recon_ratio_share, recon_suspect.recon_suspect_keys, recon_suspect.recon_suspect_rows, (select count(*) from (select 1 from "billing_invoices" limit 100000)) as recon_reference_rows_scanned from base, recon, recon_suspect
```

## Measurements

```json
{
 "unit_claimed": "USD",
 "median": 12795.0,
 "integer_share": 1.0,
 "min": 511.0,
 "max": 24952.0,
 "row_count": 2000,
 "probe_sql": "with base as (select median(v) as median, avg(case when v is null then null when v = floor(v) then 1.0 else 0.0 end) as integer_share, avg(case when v is null then null when v * 100 = floor(v * 100) then 1.0 else 0.0 end) as centi_integer_share, min(v) as min, max(v) as max, count(v) as row_count, count(*) as rows_scanned, 100000 as scan_limit from (select \"amount\" as v from \"fct_payments\" limit 100000)), suspect as (select \"order_id\" as k, \"amount\" as v from \"fct_payments\" limit 100000), recon as (select count(*) as recon_joined, count(distinct s.k) as recon_matched_keys, avg(case when r.\"total_major\" is null then 0.0 when r.\"total_major\" = 0 then 0.0 when abs(s.v / r.\"total_major\" - 100.0) <= 1e-6 then 1.0 else 0.0 end) as recon_ratio_share from suspect s join (select \"order_id\" as rk, \"total_major\" from \"billing_invoices\" limit 100000) r on s.k = r.rk where s.v is not null), recon_suspect as (select count(distinct k) as recon_suspect_keys, count(*) as recon_suspect_rows from suspect where v is not null)  select base.*, recon.recon_joined, recon.recon_matched_keys, recon.recon_ratio_share, recon_suspect.recon_suspect_keys, recon_suspect.recon_suspect_rows, (select count(*) from (select 1 from \"billing_invoices\" limit 100000)) as recon_reference_rows_scanned from base, recon, recon_suspect",
 "rubric": "CONTRADICTED iff integer_share == 1.0 and median > 1000 AND a declared reconciliation corroborates (>= 100 DISTINCT matched keys covering EVERY suspect key, keys unique on both sides, every ratio at 100x, reference scan complete); the distribution alone is suspicion and falls to UNVERIFIABLE. CONFIRMED iff fractional_share >= 0.3 and 0 < median <= 1000 (fractional values are impossible under integer-cents storage, so the dollars confirmation is earned by distribution); otherwise UNVERIFIABLE; every verdict requires a complete scan (under the scan limit)",
 "recon_joined": 2000,
 "recon_matched_keys": 2000,
 "recon_suspect_keys": 2000,
 "recon_suspect_rows": 2000,
 "recon_ratio_share": 1.0,
 "recon_reference_rows_scanned": 2000
}
```

Written by Notary (the context lie detector). This dossier is machine-generated evidence; the next agent reading this asset inherits it.
