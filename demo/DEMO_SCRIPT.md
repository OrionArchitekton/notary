# Notary demo script

All artifacts shown are real: the replay site serves frozen captured outputs
(disclosed on-page), the terminal card shows the stdout lines of the recorded
demo run verbatim (raw transcript committed at
demo/captures/run-cli-2026-07-18.txt), and the DataHub shots drive a live
local quickstart that the run actually wrote to.

### SHOT hook
- target: dashboard
- url: /
- narration: A column says transaction amount in U S D. The warehouse stores integer cents. An agent grounded on that catalog can put a revenue calculation one hundred times off, with the catalog's own authority behind it. Notary is the agent that cross examines catalog claims against measured reality.
- action: goto url="/"
- action: wait ms=3000
- action: highlight selector="h1"

### SHOT run-cli
- target: dashboard
- url: file:///home/orion/.worktrees/codex/notary-demo-video/demo/cards/run-cli.html
- narration: One command. Notary reads the asset's live descriptions from DataHub, probes the warehouse with bounded read only sequel, and adjudicates every extracted claim with evidence. Here is the real run. Amount, contradicted. The measured median is twelve thousand seven hundred ninety five, and every value is an integer. Cents, not dollars. Currency checks out, confirmed. Then it writes everything back through the DataHub M C P server.
- action: goto url="file:///home/orion/.worktrees/codex/notary-demo-video/demo/cards/run-cli.html"
- action: wait ms=2000
- action: highlight selector=".bad"

### SHOT datahub-writeback
- target: dashboard
- url: http://localhost:9002/login
- narration: This is DataHub itself after the run. Signing into the local quickstart. On the schema, the amount column now carries a provenance labeled correction. Contradicted by Notary, the original claim preserved, the measurement inline, and a pointer to the evidence dossier. And because the catalog records nine hundred thirty queries against this table in the last month, Notary also raised an operational incident. Dangerous context gets flagged where engineers actually look.
- action: goto url="http://localhost:9002/login"
- action: type selector="input#username" text="datahub"
- action: type selector="input#password" text="datahub"
- action: click selector="button:has-text('Login')"
- action: wait ms=2500
- action: goto url="http://localhost:9002/dataset/urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)/Schema"
- action: wait ms=3500
- action: click selector="text=Show more"
- action: wait ms=4000
- action: goto url="http://localhost:9002/dataset/urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)/Incidents"
- action: wait ms=4000

### SHOT s5-flip
- target: dashboard
- url: /
- narration: Does it change what the next agent does? A minimal catalog grounded agent answered the same question twice, reading only through the stock DataHub M C P tools. Without Notary's trust ledger, the best it can do is relay the unverified claim and admit it cannot check it. With the ledger, it refuses the contradicted claim and quotes the measured evidence instead. Integer cents, median twelve seven ninety five.
- action: goto url="/"
- action: scroll selector="#s5-view1"
- action: wait ms=2500
- action: highlight selector="#s5-view2"

### SHOT honest-table
- target: dashboard
- url: /
- narration: And we publish the score sheet, misses included. Twelve planted lies across five claim types. Nine caught, zero false positives, and the misses declared, with their reasons in the read me. Our own adversarial review killed a rubric that could not tell a stored fraction from a real sub one percent value. This project does not guess.
- action: goto url="/"
- action: scroll selector="#eval-table"
- action: wait ms=2500
- action: highlight selector="#eval-table table"

### SHOT close
- target: dashboard
- url: /
- narration: Everything you just saw is real. Frozen captured outputs, disclosed as such, and a live catalog the run actually wrote to. All of it reproducible: the hosted replay, the one command eval table, and a full local quickstart in the read me. Notary, the context lie detector, built on DataHub.
- action: goto url="/"
- action: wait ms=2000
- action: highlight selector=".disclosure"
