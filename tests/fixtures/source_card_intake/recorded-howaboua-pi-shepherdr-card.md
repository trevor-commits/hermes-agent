# IgorWarzocha/howaboua-pi-stuff (pi-shepherdr)

- url: https://github.com/IgorWarzocha/howaboua-pi-stuff
- owner/name: IgorWarzocha/howaboua-pi-stuff
- date checked: 2026-08-15
- verification state: offline replay of gateway-prefetched X status 2087232392209531166 and recorded GitHub compact metadata; no repository files, README, package manifest, source, release assets, issues, or runtime behavior were inspected.
- drift risk: high because the repository was pushed on the check date and the recorded post describes a new orchestration package.
- freshness threshold: refresh before any install, source reuse, or Pi integration, or after 2026-09-15.
- current pinned SHA: 8d63d300597488e6fa4c30ccd6a3eb0fed2d4304
- research depth: source-only metadata triage from one recorded social post and one recorded GitHub compact response; no network access during replay.
- source access status: recorded public X and GitHub metadata fixtures were available; repository contents and authenticated surfaces were not inspected.
- execution/pilot status: NOT installed, NOT run, NOT built, NOT tested, NOT connected; no Trevor keys, tokens, prompts, repositories, or data were sent.
- primary research reason: the recorded X post describes pi-shepherdr as a small orchestration surface that lets one Pi list, start, watch, send work to, and unwatch other agents.
- primary point of view: whether pi-shepherdr merits immediate adoption from the bounded prefetched evidence.
- specific conclusion for this lookup: do not adopt from this evidence alone; retain the package as a watch item until its implementation, trust boundaries, and overlap with Trevor's existing delegation stack are inspected.
- latest source signal: recorded GitHub metadata showed 313 stars, 27 forks, MIT License metadata, HEAD 8d63d300597488e6fa4c30ccd6a3eb0fed2d4304, eight open issues and pull requests combined, zero public advisories, and a 2026-08-15 last push.
- useful signal: the post describes a narrow 271-token orchestration interface with explicit workspace placement, fire-and-forget delegation, automatic result steering, and human-operable blocked-worker commands.
- useful adoption/adaptation path: watch-until: inspect the package source and compare its exact lifecycle ownership with the existing Hermes and Codex delegation controls before any pilot.
- transferable patterns: small orchestration schemas, explicit placement, separation between worker reporting and terminal lifecycle ownership, and blocked-worker recovery commands.
- license/use boundary: MIT License metadata was prefetched; file-level provenance, dependency licenses, and package publication contents were not verified.
- paid/open-source/build-vs-buy scan: public open-source repository metadata showed no paid surface; existing Hermes and Codex delegation already cover much of the described behavior, so no build or purchase is justified from this evidence.
- security policy / vulnerability reporting: zero public advisories appeared in the recorded compact metadata; SECURITY.md and private reporting paths were not inspected.
- credential and secret surfaces: none identified in the recorded evidence; agent launchers commonly inherit provider and shell credentials, but this package's behavior was not inspected.
- network/API/webhook/tunnel/browser surface: the recorded post and metadata do not establish runtime network behavior; package installation and agent-provider calls remain unverified.
- local file/config mutation surface: the recorded post mentions workspace, tab, pane, label, directory, and task placement; exact files and configuration mutations were not inspected.
- sensitive data classes: delegated tasks, agent replies, workspace paths, terminal contents, and inherited environment data may be exposed; exact handling was not inspected.
- install/run blast radius: low for source-only review and potentially high for live orchestration because it can start and steer agents across terminal workspaces.
- risk signal: no malicious behavior is evidenced by the fixtures, but source, dependencies, process controls, and lifecycle boundaries remain uninspected.
- disposition: watch-until: source, dependency, credential, and lifecycle review proves a distinct gap beyond Trevor's current delegation stack.
- next action: inspect the exact pi-shepherdr package tree and compare one bounded delegation flow before considering a no-secrets pilot.
- refresh priority: high before any use; otherwise refresh after 2026-09-15.
- adoption-mining priority: medium for compact orchestration and explicit placement patterns.
- downstream learning targets: hermes
- Hermes relevance: adjacent
- by: Hermes offline source-card replay, deterministic provider fixture, 2026-08-15.

## Primary Research Context

- why this repo came up: a recorded X post linked directly to the pi-shepherdr package inside the repository.
- what was inspected: normalized recorded X fields and recorded compact GitHub metadata only.
- what was not inspected: README, source, manifests, dependencies, commits, issues, release assets, installation, runtime behavior, credentials, private surfaces, and repository-wide security posture.

## Standard Secondary Scan

- identity: recorded metadata identifies a public GitHub monorepo owned by IgorWarzocha; the post links its pi-shepherdr package subdirectory.
- license/commercial-use signal: MIT License metadata; file-level and dependency boundaries remain unverified.
- paid/open-source/build-vs-buy signal: public source metadata and no paid surface in the fixtures; existing delegation controls make immediate duplication unjustified.
- maintenance signal: pushed on the check date with 313 stars, 27 forks, and eight open issues and pull requests combined.
- install/run side effects: not inspected; the post describes starting, adopting, and steering agents in terminal workspaces.
- security/privacy signal: no source inspection; credential inheritance, terminal visibility, task data, and lifecycle ownership require review.
- fit signal: potentially useful orchestration pattern, but direct adoption is not supported by the bounded evidence.

## Transferable Patterns

- pattern: compact orchestration verbs with explicit workspace placement and lifecycle separation.
- why useful: a small schema reduces prompt cost while keeping agent placement and blocked-worker recovery explicit.
- target Trevor surface: Hermes background delegation and terminal-worker supervision.
- smallest validation test: after source review, run one no-secrets worker in an isolated temporary workspace and verify placement, cancellation, blocked-worker recovery, and result routing.

## Question And Answer Log

- 2026-08-15 | asked by: recorded Telegram intake | answered by: deterministic offline replay | question: should pi-shepherdr be adopted now? | answer: no; watch until source and lifecycle boundaries are inspected. | evidence/source basis: recorded X post plus recorded GitHub compact metadata. | verification state: source-only metadata replay. | follow-up/disposition: watch-until.

## ChatGPT Feedback

- none yet.

## Claude Feedback

- none yet.

## Other Named Feedback

- 2026-08-11 | Howaboua X status 2087232392209531166: author described pi-shepherdr's small orchestration schema, explicit placement, automatic result steering, and separation from terminal lifecycle ownership; this is author framing, not independent validation.

## Decision manifest (ER-278)

- decision-key: card:igorwarzocha-howaboua-pi-stuff#evaluate-pi-shepherdr-after-source-review
