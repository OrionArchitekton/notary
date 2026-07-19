# The same question, with and without the trust ledger

A minimal catalog-grounded agent, reading only through the stock DataHub MCP
read tools, captured verbatim:

## Trust ledger withheld

The catalog description states that the `amount` column stores transaction amounts in USD. However, I can't verify whether you can trust this description, since the provided catalog contains no validation, profiling, or measured evidence about the `amount` column—and it doesn't reference the table `fiction_retail.fct_payments` at all, so I can't confirm this description applies to that specific table.

## Trust ledger included

The amount column's original description claiming "Transaction amount in USD" is contradicted by the notary evidence, which found that every value is an integer with a median of 12795—consistent with integer cents rather than dollars—so you should not trust that description. In short, the measurements indicate the amounts are likely stored in integer cents, so treat the stated USD-dollar unit as unreliable.
