---
name: usage-router
description: Use before delegating work or choosing a model, or when asked about AI usage/allowances/resets — consults live allowance state across Claude, Codex, GLM, Copilot, Hermes and picks the right executor.
---

# Usage-aware routing

Run:

    "/Users/gillettes/Coding Projects/global-implementations/scripts/usage-router" --need exec --top     # cheap execution
    "/Users/gillettes/Coding Projects/global-implementations/scripts/usage-router" --need audit --top    # independent review
    "/Users/gillettes/Coding Projects/global-implementations/scripts/usage-router" --need strong --top   # novel/hardest work

Output: provider<TAB>model<TAB>reason. Exit 2 = nothing viable → fail closed (say so; do not guess).
Shared principles: cheap work leaves the Claude account entirely; allowance refilling soon is spent first; purchased reset credits (~/.usage-snapshot/credits.json) redeem when weekly is nearly spent and always before expiry.
Raw JSON: scripts/usage-snapshot (same directory). Human scoreboard: ~/.usage-snapshot/dashboard.html.
