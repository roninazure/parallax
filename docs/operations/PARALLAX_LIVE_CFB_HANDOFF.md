# PARALLAX LIVE CFB — verified handoff boundary

**Status:** Adapter implemented and tested; end-to-end production website handoff NOT deployed or verified.

## Current runtime evidence
- Mac mini successfully activated CFB independent launchd bridge at 2026-11-26T02:14:10Z.
- First scan: 34 scored positions, 12 publication-eligible BUYs, 8 alerts sent, 4 deduplicated.
- Sanitized local feed: `~/Library/Application Support/SwarmEdge/state/parallax/public_feed/cfb.json`.
- NFL production remains pinned to `46e87dfa7a7697488d14f19d83f4479031cde510`.
- The approved PARALLAX LIVE v0.2 package supplied with this project is a **September 25 static prototype**, with `apiUrl: null`, old snapshot, and CFB PAUSED.

## Contract boundary
The website **MUST NOT** connect directly to the Mac mini, its databases, its private Tailscale IP, or its writable runtime files.

1. CFB scanner publishes **sanitized** local `cfb.json` with `read_only: true`.
2. An outbound-only authenticated publisher (not yet implemented/deployed) transports a validated, sanitized payload to a public edge/cache. No inbound Mac mini access.
3. The website consumes that sanitized feed through its current read-only PARALLAX LIVE data adapter. The target API in v0.2 documentation is `GET /api/v1/parallax/live`, but its hosting/implementation must be verified from the **current** Sites deployment, not the old ZIP.
4. Only a fresh CFB lane may be merged into the current full dashboard feed. **Do not replace existing live NFL or MLB rows with an old v0.2 snapshot.** Do not relabel the whole dashboard LIVE based only on fresh CFB.
5. An unavailable/stale CFB feed produces clear stale/unavailable status and no current BUY calls; no hardcoded Sept 25 snapshot plays passed off as live.

## Changes in PR
- CFB public sanitizer explicitly exports verified `selected_team` for eligible CFB BUYs; it never infers selection from Kalshi YES/NO tickers.
- `integrations/parallax_live/cfb_v02_adapter.py` is a read-only, staging-only CFB supplement converter.
- Tests reject stale/future-dated source data, uncertified BUYs, missing team, incorrect lane, expired quote, and invalid price/probability.

## Remaining deployment gates
- Identify the **current** connected `swarm-axis-flagship` Sites source and existing `/parallax/live` route and deployed API architecture.
- Build the actual outbound publisher and edge cache endpoint with bounded credentials, payload signatures, access controls and rollback.
- Test new endpoint via HTTPS and verify site rendering against real CFB evidence while preserving approved dashboard screenshot/design.
- Confirm that the CFB hourly health timestamp advances, and that the current NFL feed remains intact.
- **Only after all gates pass** change the production page or claim PARALLAX LIVE CFB alerts operational.

The GitHub tests and draft PR do **not** activate publishing or deploy the Sites project.
