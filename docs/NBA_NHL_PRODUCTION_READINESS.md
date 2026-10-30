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
  manifest mismatch before treating it as deployment-integrity proof. Read-only
  validation against the active release and run plist also found mismatches in
  runtime-config, launchd-plist, and scheduler-entrypoint SHA-256 values.
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

## Model v2 feature comparison

The continuation implements `parallax-league-logistic-form-v2`. Its
dependency-free chronological feature builder uses exponentially decayed
overall form, home and away form, opponent-adjusted result residuals, rest
days, and explicit season resets. A regularized logistic model selects its
penalty on a chronological validation slice inside training. A constant
home-rate baseline and the existing calibrated Elo model are evaluated on the
same holdout. Calibration uses a separate later window. Metrics include ten
reliability bins and 95% confidence intervals from 400 deterministic
ET-date-block bootstrap replicates.

The available histories remain 2,460 NBA finals (seasons 2024–2025) and 5,248
NHL finals (seasons 2022–2025). The former holdouts have already been inspected
and are marked `EXPLORATORY_PREVIOUSLY_INSPECTED`; they are not confirmatory
validation. There is no later independently reconciled results file. The NBA
opening-night grading file contains two manually transcribed games, is marked
research-only, and says exchange settlement is unverified; it is not used as a
certification holdout. The NHL forward folder has no graded results file.

| League | Candidate | Games | Brier | Log loss | ECE (10 bins) |
| --- | --- | ---: | ---: | ---: | ---: |
| NBA | v1 calibrated Elo | 516 | 0.19747 | 0.58358 | 0.12777 |
| NBA | v2 feature logistic | 516 | 0.20424 | 0.59787 | 0.12872 |
| NBA | training home-rate baseline | 516 | 0.24657 | 0.68627 | 0.01327 |
| NBA | 50/50 baseline | 516 | 0.25000 | 0.69315 | 0.06008 |
| NHL | v1 calibrated Elo | 1,119 | 0.25012 | 0.69362 | 0.05161 |
| NHL | v2 feature logistic | 1,119 | 0.24935 | 0.69198 | 0.04879 |
| NHL | training home-rate baseline | 1,119 | 0.24989 | 0.69293 | 0.01501 |
| NHL | 50/50 baseline | 1,119 | 0.25000 | 0.69315 | 0.01832 |

These repeated holdout results are exploratory, including the home-rate
comparison. V2 does not pass the existing ECE gate for either league and does
not outperform the v1 Elo model on NBA Brier/log loss. Its artifacts under
`reports/nba_nhl_model_candidates/*_v2_candidate.json` are hash-verified
research outputs. They cannot be loaded by the production Evidence path
because there is no fresh independent forward holdout, source-use approval,
or model-owner criteria approval. No gate was relaxed.

The selected v2 95% ET-date-block bootstrap intervals are NBA Brier
`[0.19749, 0.21154]`, log loss `[0.58367, 0.61325]`, and ECE
`[0.09766, 0.16636]`; NHL Brier `[0.24352, 0.25448]`, log loss
`[0.68005, 0.70247]`, and ECE `[0.01840, 0.07493]`. These intervals quantify
variation across dates in the already-inspected holdout and do not turn it into
a fresh confirmatory sample.

The v2 runtime dispatch is implemented for a future fully certified artifact.
Read-only executable quote/depth/fee capture is available when
`PARALLAX_LEAGUE_QUOTE_SNAPSHOT_DIR` is set; retention is configurable with
`PARALLAX_LEAGUE_QUOTE_RETENTION_DAYS` (default 180 days). Snapshots contain
matched fixture identity, rules hash, timestamps, executable depth and fee
metadata, with no raw provider payloads or credentials. These are forward
observations and do not create historical profitability evidence.

Run `python scripts/diagnose_league_runtime.py` to compare launchd plist,
heartbeat and deployment-manifest identities without writing to runtime
state. Reconcile by staging a fresh immutable release from the intended
committed SHA, generating its manifest from that release's actual config and
plist, and verifying the active process/heartbeat predicates in `deploy/README.md`
before any separately authorized cutover. This PR does not change the active
release.

The read-only diagnostic was run on 2026-10-29. The active plist and
heartbeat both identify release `a517ab94087c3b63d4fe5ac914eb470aef7fb3db`,
but the heartbeat was already older than the 30-second cutover limit and
`launchctl print` could not resolve the job. The runtime manifest instead
claims Git SHA `bf11b4be9588392ba0c200b80b406943ca48cdd5`; its launchd plist,
runtime config, and entrypoint hashes also disagree with the active files.
The diagnostic made no runtime changes.

**Next action:** obtain source-authorized independent NBA and NHL finals
through a later season from a second results provider, reconcile those records
game-for-game with the training snapshots, and collect a later forward sample.
Then rerun `scripts/train_league_models.py --model-version v2` with
`--independent-history` and `--forward-holdout`, alongside the documented
source-use authorization and model-owner approval references. Until that
evaluation passes every existing gate, keep the candidates out of runtime pins.

## Reproducible calibrated candidate replay

The continuation adds `scripts/train_league_models.py` and
`parallax/league_model.py`. The trainer tunes Elo parameters on the first 60%
of unique ET game dates, fits a Platt or isotonic calibrator using only the
next 20% (the choice is selected on an ordered split inside calibration), and
reports the final 20% once as holdout. All games from one ET date share one
pre-date rating state. Acceptance requires a nonempty holdout, improvement
over a 50/50 forecast on Brier, log loss, and ECE, an independently
reconciled second results source, documented source authorization, and model
owner approval of the criteria. No absolute ECE or minimum-sample threshold
was invented. A generated artifact includes its source
hash, split dates/counts, fit/calibration settings, ratings, metrics, gate
results and self-hash. Runtime loading also requires its exact SHA-256 to be
pinned in `PARALLAX_NBA_MODEL_SHA256` or `PARALLAX_NHL_MODEL_SHA256`.

Candidate artifacts from the inspected local snapshots are stored under
`reports/nba_nhl_model_candidates/`. They are reproducible research outputs,
not runtime model files, and both have `certified: false`:

| League | Train / calibration / holdout games | Holdout Brier (50/50) | Holdout log loss (50/50) | ECE | Failed checks |
| --- | ---: | ---: | ---: | ---: | --- |
| NBA | 1,472 / 472 / 516 | 0.19747 (0.25000) | 0.58358 (0.69315) | 0.12777 (0.06008) | ECE baseline; independent source; source authorization; owner approval |
| NHL | 3,058 / 1,071 / 1,119 | 0.25012 (0.25000) | 0.69362 (0.69315) | 0.05161 (0.01832) | Brier, log-loss, ECE baselines; independent source; source authorization; owner approval |

The NBA fitted Elo parameters are home adjustment 40 and K 30; NHL parameters
are home adjustment 20 and K 10. Calibration selected Platt logistic for both
leagues. The NBA holdout starts 2026-01-30; NHL starts 2025-11-02. No holdout
labels were used for parameter or calibrator selection. Candidate SHA-256s
are NBA `2f86382704b76750a9e45440bf2204b1ff5f27671ca2f705e9d51cc30dfaabe1`
and NHL `9191a75e8a7d7404f770a883db18aa592bfe1d9c34c4c1ee415ca77b8d82b699`.
Full machine-readable split/metric records are in the candidate artifacts.

The production scanner now loads only a certified artifact with a matching
runtime SHA pin, predicts from pregame history using the artifact's parameters
and calibration, and builds `CALIBRATED` Evidence only after exact full-game
contract identity mapping. The experimental Elo and shadow evidence are not
used by the production path. Missing, stale, unpinned, or failed artifacts
produce a blocked model state and zero scored rows. Existing qualification,
publication validation and ntfy dedupe gates remain unchanged.

**Current certification result: blocked for both leagues.** NBA fails the
50/50 ECE baseline (0.12777 > 0.06008). NHL fails all three 50/50 baselines
(Brier 0.25012 > 0.25; log loss 0.69362 > 0.69315; ECE 0.05161 > 0.01832).
Neither snapshot has a second independent results source, documented source
authorization, or model-owner criteria approval. The NHL
acquisition manifest explicitly marks the source research-only and official
regular-season membership unverified. No NBA acquisition/license record was
found. The active runtime configuration also has no pinned NBA/NHL model SHA.
Consequently the new production path is implemented but emits no certified
NBA/NHL BUY until these actual gates pass; no training metric is inferred from
historical executable market quotes, which are still absent.

After the continuation changes were prepared, the saved unattended health
heartbeat was still `2026-10-29T00:53:13Z`; at the `2026-10-29T01:14:04Z`
inspection it was more than 20 minutes old. `launchctl print` could not resolve
the service in the available GUI/user domains. Therefore current-process and
30-second heartbeat acceptance could not be proven, and no Mac mini cutover or
runtime-state edit was made.

## Website status contract

For NBA/NHL status presentation, use the sanitized lane fields: `lane`,
`health_state`, `market_data_complete`, `buy_publication_eligible`,
`summary.buy`, `generated_at`, and venue `coverage.state`. A completed scan with
bounded discovery is `DEGRADED` and `market_data_complete: false`; zero BUYs in
that state must not be shown as a healthy, complete no-opportunity scan. A
scanner failure appears as a failed unattended lane and does not replace the
last valid feed.


## Follow-up: launchd-domain detection correction (2026-10-29)

The Mac mini operator subsequently verified the active production job with
`sudo launchctl print system/com.swarmedge.parallax-sports`, showing a running
`parallax_unattended.py` process from release
`a517ab94087c3b63d4fe5ac914eb470aef7fb3db`. The earlier GUI-domain
`launchctl` failure in this report is therefore **not evidence that the
production service is absent or stopped**. The release/manifest identity
discrepancy remains a separate cutover gate and must not be silently waived.

PR #58 now probes `system` before `gui/<uid>` and `user/<uid>` in
`scripts/league_evidence_preflight.py` and
`scripts/diagnose_league_runtime.py`. Regression tests cover system-domain
recognition, GUI fallback, and continued fail-closed manifest drift behavior.
These changes are code-only; they do not deploy a collector, change launchd,
certify a model, or authorize NBA/NHL BUY publication. The newly added tests
still require execution in the target repository environment.

**Next implementation gate:** verify real NBA account/terms authorization,
reconcile the active release manifest without changing the running service
during inspection, and only then stage the isolated NBA evidence collector.
NHL remains research-only until its separate written-source authorization is
verified. Model certification and executable quote validation remain mandatory
before either customer-facing cutover.

## Overnight fail-closed hardening (2026-10-29)

PR #58 now requires the isolated NBA evidence preflight to verify the **loaded**
`com.swarmedge.parallax-sports` launchd plist, not a staged candidate or
Git SHA alone. It confirms the job reports `state = running`, the loaded plist
identifies the active immutable release and `PARALLAX_RELEASE_SHA`, and the
manifest matches the actual loaded plist, scheduler entrypoint, runtime
environment, and dependency lock by SHA-256. Missing fields, unreadable
files, malformed plist structures, or hash drift block collector readiness.
This is read-only and cannot repair the existing production manifest.

The v2 trainer also rejects duplicate primary or forward-holdout fixtures
identified by (season, ET game date, home team, away team), even if a second
provider supplies different game IDs. Forward-holdout team codes must be
canonical. These checks prevent accidental duplicate-game weighting in NBA/NHL
validation; they do not supply independent results, a fresh holdout, source
permission, or model certification. Existing NBA/NHL BUY publication remains
blocked. No Mac mini cutover, launchd reload, or runtime edit is part of this PR.

## 2026-10-30 — ET-date-isolated model selection and release hold

The initial v2 model-selection split used 80% of **game rows** within the
training window. With multiple games on one ET date, that boundary could
place same-date outcomes on both sides of the hyperparameter-selection
partition. PR #58 now selects the first 80% of **distinct ET dates**, keeps
every game from a date entirely in fit or validation, and records the
fit/validation date boundaries and game counts in newly generated artifacts.
Synthetic regression tests enforce this property. This corrects the
validation protocol; it is **not** evidence of improved calibration.

The checked-in `nba_v2_candidate.json` and `nhl_v2_candidate.json` are
**previous exploratory outputs from before this correction**. Their recorded
Brier, log loss, ECE, and artifact hashes are historical observations only,
not results of the revised protocol. They remain uncertified and must not be
pinned or represented as a current release candidate. Regenerate and
re-evaluate both candidates against source-authorized data before comparing
metrics or considering a release; independently reconciled results and a
genuinely later forward holdout remain missing.

**Mac mini production-code release gate (not executed):** the last documented
active sports release is `a517ab94087c3b63d4fe5ac914eb470aef7fb3db`,
while the recorded manifest claims
`bf11b4be9588392ba0c200b80b406943ca48cdd5`. No new live-host
inspection has occurred for this update. Before any separately approved
cutover, obtain a fresh read-only system-domain launchd/heartbeat/manifest
inventory and reconcile the loaded plist, active release SHA, dependency lock,
runtime environment and scheduler-entrypoint hashes. Do **not** repair the
manifest in place. Once model, provenance, quote-economics, and owner gates
are genuinely satisfied, package an immutable candidate from the reviewed
commit with `scripts/migration_preflight.py create-release`, render/lint the
candidate launchd plist, generate and validate its manifest against those
exact staged inputs, and preserve the active plist and known-good release.
Only a separately authorized cutover may reload
`system/com.swarmedge.parallax-sports`; verify process identity, fresh
heartbeat (<=30 seconds), lane isolation, valid feeds and no critical errors
within the documented 90-second window. On failure restore the saved plist,
reload the prior release, and verify its SHA/health. Never touch the separate
publisher or change NFL/MLB/CFB schedules as part of NBA/NHL certification.
**No deployable NBA/NHL release candidate is approved or staged.**

The artifact verifier also independently recomputes the evidence gates:
secondary-source SHA-256 values must be valid and different from the primary
digest **after case normalization**, and a claimed fresh forward holdout must
carry a valid SHA-256 for its source. Rehashing an artifact cannot turn a
missing forward source or a case-only duplicate source digest into verified
evidence. These are integrity checks, not proof of legal provider independence
or authorization.

## 2026-10-30 — League-scoped independent validation CLI (GitHub-only)

The read-only `scripts/reconcile_league_results.py` compares two **local,
canonical final-results CSVs** by season, official ET date, home/away team and
winner, not provider-specific game ID. It rejects missing/extra fixtures,
duplicate fixture identities, wrong winners, empty selected-league inputs and
byte-identical files. It emits a machine-readable count, source SHA-256 hashes
and canonical-history fingerprint on success, with nonzero exit on mismatch.
For NBA, prepare independently acquired and authorized results in the documented
`sport,game_id,season,game_date,home_team,away_team,winner_team` schema:

```sh
python scripts/reconcile_league_results.py \
  --sport NBA \
  --primary-history "$NBA_PRIMARY_FINALS_CSV" \
  --independent-history "$NBA_SECOND_PROVIDER_FINALS_CSV"
```

The output deliberately keeps `provider_independence_verified`,
`source_authorization_verified`, `forward_holdout_verified`,
`model_certified` and `production_eligible` **false**. File disagreement
is disqualifying, but agreement and distinct bytes do **not** prove independent
provider origin, license, sample freshness, or acceptance. Human-reviewed
source provenance and rights records remain external blockers.

`scripts/train_league_models.py --sport NBA` now evaluates NBA without
loading or writing NHL candidate artifacts. `--sport NHL` works separately;
omitting `--sport` preserves the existing two-league default. An explicitly
supplied forward-holdout CSV with zero rows for the selected league now raises
an error rather than silently falling back to an exploratory old holdout.
A future evidence-backed NBA evaluation can use:

```sh
python scripts/train_league_models.py \
  --sport NBA --model-version v2 \
  --independent-history "$NBA_SECOND_PROVIDER_FINALS_CSV" \
  --forward-holdout "$NBA_LATER_FINALS_CSV" \
  --output-dir "$NBA_RESEARCH_OUTPUT_DIR"
```

This training command still requires the local primary research snapshot,
verified data rights, actual later independent finals and separately approved
model-owner criteria. The example deliberately supplies **no fabricated**
authorization or approval references. Its output is a research candidate, not
a release artifact; never set runtime model pins from this example alone.

**Mac mini release hold:** the last documented active release is
`a517ab94087c3b63d4fe5ac914eb470aef7fb3db`, while the last documented
manifest identifies `bf11b4be9588392ba0c200b80b406943ca48cdd5`.
These are historical observations, not a current host check. Before any
separately approved deployment, collect a fresh **read-only** system-domain
launchd, heartbeat, loaded-plist and manifest inventory; reconcile actual
runtime-env, entrypoint, dependency-lock and plist hashes; preserve the current
plist and immutable prior release. Only after all evidence gates pass may an
immutable SHA-addressed release be packaged with
`scripts/migration_preflight.py create-release`, its candidate plist linted,
and its manifest generated and validated. The separately approved cutover must
satisfy the 90-second process/SHA/<=30-second heartbeat/feed/critical-error
checks in `deploy/README.md`; on failure restore the saved plist and previous
release and verify recovery. Do not touch the customer publisher or change
NFL/MLB/CFB lanes. No release candidate has been approved, packaged or
deployed by this GitHub-only work.
