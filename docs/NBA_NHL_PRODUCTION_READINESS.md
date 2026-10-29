# NBA/NHL production readiness snapshot

Snapshot inspected for issue #57 on 2026-10-28. This report records the
currently deployed behavior and the evidence available to the repository. It
does not certify the model or authorize BUY publication.

## Runtime inventory

- The active launchd plist is
  `~/Library/Application Support/SwarmEdge/staging/com.swarmedge.parallax-sports.a517ab94087c3b63d4fe5ac914eb470aef7fb3db.plist`.
  It runs `scripts/parallax_unattended.py` from release
  `a517ab94087c3b63d4fe5ac914eb470aef7fb3db` at a five second supervisor
  interval. The supervised NBA and NHL lanes run hourly after startup offsets.
- The unattended health file reports release SHA
  `a517ab94087c3b63d4fe5ac914eb470aef7fb3db`. Its last successful NBA scan
  was `2026-10-29T00:00:40Z`; its last successful NHL scan was
  `2026-10-29T00:00:49Z`. Both lanes were `DEGRADED` because market coverage is
  bounded, and the top-level health state was `DEGRADED`.
- Both persisted feeds had zero BUYs and `buy_publication_eligible: false`.
  NBA had 4 PASS and 4 WATCH rows; NHL had 5 PASS and 3 WATCH rows. Both feeds
  reported `market_data_complete: false`.
- `run/parallax-sports-manifest.json` claims Git SHA
  `bf11b4be9588392ba0c200b80b406943ca48cdd5`, which does not match the active
  release directory, launchd configuration, or health heartbeat. Resolve this
  manifest mismatch before treating it as deployment-integrity proof.
- Runtime environment key names were inspected without reading or disclosing
  their values. They contain no NBA/NHL history path or model certificate
  setting, so scanners use the verified research snapshot fallback.

## Available historical replay

The research snapshots on the Mac mini contain final winners, not historical
executable prices. Chronological replay freezes each official ET game's
forecast before applying any result from that date. Metrics below are
descriptive only: the experimental Elo parameters (home adjustment/K of
NBA 60/20 and NHL 35/20) are hand-set, uncalibrated starting values.

| League / holdout season | Games | Brier | Log loss | ECE (10 bins) |
| --- | ---: | ---: | ---: | ---: |
| NBA 2024 | 1,230 | 0.2178 | 0.6244 | 0.0403 |
| NBA 2025 | 1,230 | 0.2108 | 0.6112 | 0.0258 |
| NHL 2022 | 1,312 | 0.2380 | 0.6682 | 0.0360 |
| NHL 2023 | 1,312 | 0.2385 | 0.6704 | 0.0407 |
| NHL 2024 | 1,312 | 0.2410 | 0.6756 | 0.0440 |
| NHL 2025 | 1,312 | 0.2528 | 0.7001 | 0.0692 |

The source coverage is 2,460 NBA games from seasons 2024–2025 and 5,248 NHL
games from seasons 2022–2025. The replay tests prevent same-day result leakage
and reject duplicate game IDs. These metrics alone do not establish calibration
or profitable market performance. No independently approved model artifact,
league-specific certificate, or reproducible fit/selection record was found.

## Inputs still required for certification

1. A reviewed, provenance-backed NBA model artifact and separate NHL artifact,
   with frozen training and holdout definitions, reproducible fit/selection,
   artifact hashes, expiry, and an authorized model owner.
2. A pre-registered baseline comparison and acceptance criteria approved by the
   model owner. The current Elo parameters were not tuned against the reported
   holdouts, and the metrics above are not a certification decision.
3. Timestamped historical executable quotes/order books aligned to each
   pregame forecast, including bid/ask spread, depth, available liquidity,
   fees, and contract resolution mapping. No such NBA/NHL dataset is present in
   the repository or the inspected runtime state.
4. Documented data source licensing/retention approval for use of the research
   history and any market history used in model validation. The host has a
   BALLDONTLIE credential and local result snapshots, but their licensing
   authorization was not evidenced in the inspected deployment configuration.
5. Reconciled deployment manifest matching the active `a517ab…` release, plus
   post-change Mac mini health/feed/ntfy evidence. No deployment was performed
   for this PR.

Until these inputs exist, `league_elo_experimental` stays research-only and
`shadow_evidence` remains unable to pass the existing production certificate
checks. The normal `qualify` economics/risk gates, feed validation, and ntfy
dedupe remain in force. No order placement is introduced.

## Website status contract

For NBA/NHL status presentation, use the sanitized lane fields: `lane`,
`health_state`, `market_data_complete`, `buy_publication_eligible`,
`summary.buy`, `generated_at`, and venue `coverage.state`. A completed scan with
bounded discovery is `DEGRADED` and `market_data_complete: false`; zero BUYs in
that state must not be shown as a healthy, complete no-opportunity scan. A
scanner failure appears as a failed unattended lane and does not replace the
last valid feed.
