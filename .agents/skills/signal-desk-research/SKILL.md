---
name: signal-desk-research
description: Apply the installed financial research skills to a frozen Signal Desk judgment, tracking its supporting and opposing evidence and preparing a costed comparison. Use for Signal Desk thesis updates, catalyst reviews or research readiness, rather than general stock screening.
---

# Signal Desk research

Use `thesis-tracker`, `catalyst-calendar`, `stock-liquidity`, `earnings-recap` and
`estimate-analysis` as method references. In this project, use the application's
preserved inputs rather than running the upstream default Yahoo setup or installing
dependencies into the service environment.

## Start from an explicit capture

Export a public decision bundle through the existing administrator capture export.
With the user's explicit decision timestamp, run from the repository:

```sh
.venv/bin/python -m scripts.measure.finance_research_packet \
  --bundle /absolute/path/decision.json.gz \
  --as-of 2026-10-29T13:33:28Z \
  --output /absolute/path/new-research-packet.json
```

The timestamp must belong to that capture's market date and must not precede its
price receipts. Do not derive decision time from the newest receipt. Preserve the
whole captured universe, including deferred prices and insufficient histories.

## Read the relevant evidence

- Read actual `score_contributions`, not just displayed factor values. A calculated
  indicator can have zero selection weight. Separate the recorded rank from a new
  entry decision and from an existing holding's exit policy.
- A fresh last price is not an executable bid/ask or evidence of available size.
  Use `alpaca_stock_data` only when new US source data is needed. Preserve the raw
  output and the request's symbols, feed, adjustment, timezone and source times.
  This MCP supplies no Korean market feed. Reuse the existing Korean adapters.
- Read original company releases when reviewing earnings. Compare periods, units,
  accounting basis and guidance with source dates. Missing consensus or source
  timing stays unknown; do not substitute today's estimate for a past one.
- Keep SEC requests closed until the agreed actual operating contact is configured.
  Installing a skill does not authorize a paid dataset, external message or order.

## Produce a concise research update

Return: **recent change → supporting evidence → counterevidence → what to check
next → remaining unknowns**. Cite the judgment ID, observation ID and source links.
State when a prior thesis is unchanged, weakened or no longer supported, with the
specific new fact. An evidence update is not an automatic buy/add/sell instruction.

Do not present simulated paths as independent historical trades, source coverage as
accuracy, an unverified model confidence as win probability, or briefing completion
as profit verification. No application LLM call is needed to create the packet.

For a subsequent performance study, read
`docs/FINANCE-RESEARCH-TOOLS-2026-10-31.md`. Freeze the candidate, source manifest,
execution/cost contract and evaluation window before opening outcomes. Existing
registered/protected samples and rejected studies retain their current locks.
