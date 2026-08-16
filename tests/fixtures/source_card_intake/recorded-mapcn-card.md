# AnmolSaini16/mapcn (mapcn)

- url: https://github.com/AnmolSaini16/mapcn
- owner/name: AnmolSaini16/mapcn
- date checked: 2026-08-15
- verification state: standard-tier source-only read-only triage, all unauthenticated — GitHub REST repo metadata, GitHub repo HTML page (header/forks/stars/commits/tree), git ls-remote HEAD, plus the gateway-prefetched X post (status 2088649127945195681) treated as untrusted evidence. README was only partially captured (search-snippet + truncated page); install mechanics, component list, and release/tags were NOT fully inspected — recorded below as TODO: verify.
- drift risk: high — young repo (created 2025-12-28) moving fast (105 commits, last push same day as this check) with viral-star growth (11.4k stars in ~8 months); component CLI output shape can change with any commit and no pinned release was verified.
- freshness threshold: refresh at the first real map-UI need in any Trevor project, on a first security advisory, on a first tagged release, or after 2027-02-15.
- current pinned SHA: f39d798e6dfa7dccc203b19792fec10da095789c (git ls-remote HEAD, 2026-08-15)
- research depth: standard tier, source-only — GitHub API metadata + repo page + ls-remote + prefetched post; no clone, build, run, auth, or package install.
- source access status: every inspected surface was public and unauthenticated; nothing required login.
- execution/pilot status: NOT installed, NOT run, NOT built, NOT tested; no Trevor keys, tokens, prompts, or data sent.
- primary research reason: X post intake — @rammcodes post (status 2088649127945195681, https://x.com/i/status/2088649127945195681, created 2026-08-15T15:28:51Z, 3,281 views / 74 likes / 1 RT / 1 reply at prefetch) recommending an "open-source collection of customizable map components" with markers, popups, tooltips, routes, controls. Post names no repo; identity resolved by evidence match.
- identity evidence: the post's feature list (markers, popups, tooltips, routes, controls, "without building everything from scratch", free/open-source map component collection) matches mapcn's README feature-for-feature (Markers & Popups with popups/tooltip/labels; Routes; Controls — zoom, compass, locate, fullscreen; "Free & open-source, ready-to-use, customizable map components for React. Zero config. One command setup."). This is identification by strong feature match, not by a link in the post — post contains no outbound links (links: [], one video media attachment). Residual mismatch risk noted in Q&A log.
- primary point of view: source-only first-pass adoption triage for Trevor's workflows — does a shadcn-style, copy-into-your-project map component kit earn a place in the toolbox now, and what transfers without adoption.
- specific conclusion for this lookup: adopt nothing yet. mapcn is a genuinely popular, MIT-licensed, actively maintained (pushed 2026-08-15) MapLibre-GL-based React map component CLI in the shadcn/ui pattern — a strong default candidate the moment a real map UI need appears in a React project, but there is no identified current map-using surface in Trevor's system to justify pulling it in today.
- latest source signal: GitHub metadata 2026-08-15 — created 2025-12-28T10:57:22Z, last push 2026-08-15T18:54:20Z, 11,398 stars / 658 forks / 21 open issues+PRs, 105 commits, license MIT, default branch main, description "Beautiful map components. 100% Free, Zero config, one command setup.", Vercel OSS Program participant, topics empty, 41 watchers.
- useful signal: the shadcn distribution model applied to maps — one-command copy of themed, composable map primitives (markers, popups, tooltips, routes, zoom/compass/locate/fullscreen controls) built on MapLibre GL with Tailwind styling, theme-aware light/dark, zero-config defaults.
- useful adoption/adaptation path: watch-until — keep as the named default candidate for the first real map UI in a React project; then pilot-scoped: pin the repo SHA, audit the copied component source (it lands in-repo as owned code), review the map style/tile provider's terms and key requirements, and pilot in a throwaway branch.
- transferable patterns: (1) applying the shadcn "registry + copy-in CLI" distribution model to a heavy domain library (MapLibre) so consumers own and can modify the code; (2) theme-aware component defaults that adapt to light/dark with zero config; (3) composable declarative primitives over an imperative map engine.
- license/use boundary: MIT per GitHub metadata (repo code). Upstream deps carry their own terms — MapLibre GL JS is BSD-3-Clause (not re-verified this pass); map tile/style providers chosen at use time may require API keys and have their own pricing/terms. TODO: verify all three before any adoption.
- paid/open-source/build-vs-buy scan: not commercial; no paid surface in inspected metadata — free open-source CLI, Vercel OSS Program badge only. Tile/style provider costs arise at use time, outside the repo.
- security policy / vulnerability reporting: not inspected (README truncated before any SECURITY.md/advisory statement); 0-advisory status NOT verified. TODO: verify security policy and advisory history before adoption.
- credential and secret surfaces: none identified in inspected metadata; components are expected to be copied into the consuming project. Whether any component expects a tile-provider API key at config time: TODO: verify.
- network/API/webhook/tunnel/browser surface: not fully verified. Expected surface (shadcn-pattern CLI): network fetch of component source at add-time plus MapLibre tile/style fetches at runtime. Exact install command and registry endpoint: TODO: verify.
- local file/config mutation surface: not fully verified. Expected (shadcn pattern): writes component files into the target project only. TODO: verify the CLI's exact write scope before use.
- sensitive data classes: none in the repo itself; at runtime a map component renders whatever locations the app surfaces — location data sensitivity is application-defined.
- install/run blast radius: source-mine: low (read-only). Pilot: low-to-medium — copy-in components land inside one React project (contained, reviewable, revertible via git); runtime blast radius is bounded by the chosen tile provider's terms and any API key placement.
- refresh priority: refresh on the first concrete React map-UI requirement or by 2027-02-15.
- adoption-mining priority: mine only when a current project needs map components; no standalone adoption work is justified.
- risk signal: no evidence of malicious code, telemetry, or obfuscation in inspected surfaces; transparency high (public source tree, public CLI). Unaudited surfaces (install scripts, registry, full component source) are unverified — do not treat this pass as a code audit.
- disposition: watch-until: first concrete React map-UI requirement or 2027-02-15 refresh; MIT, active, and promising, but install mechanics and security policy remain unverified.
- next action: on the first concrete map-UI requirement in a React project, re-verify freshness + security policy + install/network mechanics, then pilot-scoped in a throwaway branch with pinned SHA.
- downstream learning targets: none: no current downstream repository has a map-UI requirement.
- Hermes relevance: none: no direct Hermes capability decision; retain only as a future dashboard UI candidate.
- by: Hermes background source-card-intake worker (gateway-prefetched packet), model glm-5.2, 2026-08-15.
- agent provenance: Hermes background source-card-intake worker (gateway-prefetched packet), model glm-5.2, 2026-08-15; intake from X post by @rammcodes (Ram Maheshwari, 3,381 followers at prefetch).

## Primary Research Context

- Intake: single X post URL (https://x.com/i/status/2088649127945195681) submitted 2026-08-15; gateway prefetched the post JSON; worker never re-retrieved the post (per contract).
- Post content: enthusiasm post ("stunning open-source collection of customizable map components ... markers, popups, tooltip, routes, controls ... insanely useful for developers, designers and AI coders") with a 2748x2160 demo video; no outbound links; author is a web-dev/AI tips account, not the project author.
- Subject resolution: post names no repo; identity established by feature-for-feature match of the post's feature list against mapcn's README (via search snippet + repo page). Confidence high but not link-proven; recorded as identification risk in the Q&A log.
- Research boundary: decision-ready triage (identity, license, maintenance, adoption posture). Not a code audit; not an install.

## Standard Secondary Scan

- GitHub repo metadata (unauthenticated REST): dates, stars, forks, issues, license, default branch — captured above.
- Repo HTML page: 105 commits, public source tree (.github, public, scripts, src), Tags link present but no release data verified. TODO: verify releases/tags.
- git ls-remote: HEAD f39d798e6dfa7dccc203b19792fec10da095789c.
- Not scanned (budget): npm package, component source files, CI workflows, issues content, advisory database, demo video contents.

## Transferable Patterns

1. Registry + copy-in CLI (shadcn model) for heavy domain libraries — consumers own the code, updates are explicit re-pulls instead of silent dependency bumps.
2. Zero-config, theme-aware defaults (light/dark adaptation) as the out-of-box contract.
3. Composable declarative primitives (Marker, Popup, Tooltip, Route, Controls) wrapping an imperative engine (MapLibre GL).

## Question And Answer Log

- Q: Which project is the post about? A: AnmolSaini16/mapcn, by strongest feature match (post's exact feature enumeration matches mapcn's README); the post itself links nothing, so this is inference from evidence, marked accordingly. If wrong, the card's subject boundary is the mis-identification — reopen on disproof.
- Q: Is it free/open source? A: Yes — MIT (GitHub metadata, 2026-08-15).
- Q: Actively maintained? A: Yes at check time — last push 2026-08-15T18:54:20Z, 105 commits since 2025-12-28.
- Q: Safe to adopt now? A: Unverifiable at this depth — install/network mechanics, security policy, advisories, and component source not audited. TODO: verify before any pilot.
- Q: Overlap with current system? A: None identified — no current map-UI surface in Trevor's known projects; hence watch-until rather than pilot.

## ChatGPT Feedback

- None yet; no downstream consumer has acted on this card.

## Claude Feedback

- None yet; no downstream consumer has acted on this card.

## Other Named Feedback

- None yet; no downstream consumer has acted on this card.

## Implementation Assessment

- Not implemented anywhere; no pilot performed; zero runtime footprint on Trevor's system as of this card. Adoption requires: freshness re-check, security/advisory verification, install-command and write-scope audit, tile-provider terms review, pinned SHA, throwaway-branch pilot.

## Decision manifest (ER-278)

- decision-key: card:anmolsaini16-mapcn#adopt-on-first-map-ui-need
