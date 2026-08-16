# Receipts and ledger contract

The card manifest and ER-269 ledger must agree exactly.

For every independent unresolved choice, add one stable key:

```text
card:<canonical-flat-filename>#<specific-choice>
```

Create it with `hermes-research-decisions add`, including title, one answerable question, recommendation, rationale, priority, `source-kind=source-card`, exact card filename, primary URL, chat, session, and message identity when available.

Use P0 only for urgent safety or continuity. Use P1 for a known pain or time-sensitive high-leverage choice, P2 for a useful bounded enhancement, and P3 for an optional idea.

When the whole card has no unresolved choice, call `ignore-source` with exactly one of: `watch-only`, `already-implemented`, `informational`, `rejected`, `duplicate`, `superseded`, or `insufficient-evidence`. Put the same reason in the card manifest.

After every card is live on `origin/refs/heads/main`, call `receipt-intake`. Supply exact chat, session, database message row, URL when present, every card filename, configured cards root, full contained commit, and validation evidence. The writer must verify the exact transcript row, live bytes, blobs, remote containment, and validator result.

A URL-free exact acknowledgment or allowed administrative command may use the writer's narrow no-card receipt. Never use it for a named repository, tool, workflow, or URL.
