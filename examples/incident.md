# Operational incident raised by the recorded run

- Title: `Notary: dangerous unit/scale lie on fiction_retail.fct_payments`
- Category: Operational; raised via GraphQL on the asset
- Gate: a CONTRADICTED unit/scale claim AND catalog usage evidence of real
  query traffic (fails closed without it)
- Lifecycle: resolved by a later clean run, or by `python -m notary.rollback`
