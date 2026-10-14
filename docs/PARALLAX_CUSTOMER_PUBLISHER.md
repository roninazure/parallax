# PARALLAX customer publisher cutover

`scripts/parallax_customer_publisher.py` is the canonical NFL, CFB, and MLB
customer publisher. It consumes only the three `parallax.public.v1` files for
BUY decisions. The alert database is opened read-only and queried by the full
sport, normalized venue, market ID, and side key for missing display fields.
For NFL BUYs absent from that table, the sanitized title and slate supply a
matchup and scheduled start. A missing or ambiguous display identity, count
mismatch, dirty worktree, push race, or remote SHA mismatch fails the run and
records `FAILED` health. A stale or unavailable source lane is published as an
empty `STALE` customer lane in the same atomic commit, while fresh lanes keep
publishing with exact source/customer BUY parity. Stale BUYs are never marked
`LIVE`; health reports source BUY count and lane freshness separately.

The publisher takes one nonblocking lock and commits all three customer files
in one Git commit. The remote branch update makes them visible together. A
failed push is never retried with force. On the next run, a stranded local
commit is pushed only when the remote is its ancestor, every intervening commit
is a unified customer publish, the commits touch only the three feed files, and
all three payloads still match the current certified source. Divergence,
unrelated commits, or changed source data remain fail-closed for operator
inspection.

Persistent publisher failures and stale source lanes are written to the health
file each run. After five minutes of continuous failure or degradation, the
publisher sends one actionable message through the existing ntfy configuration;
repeats are limited to one per six hours. It sends a recovery message after a
previously alerted condition clears. Healthy runs do not send status messages.

## Read-only check

From a release checkout, run:

```sh
/usr/bin/python3 scripts/parallax_customer_publisher.py --dry-run
```

This reads current feeds, the alert DB, and customer worktree status. It does
not fetch, write health, lock, commit, or push. It cannot attest the current
remote SHA when GitHub is unreachable. A live publish verifies the remote SHA
with `git ls-remote` after the push.

## Reviewed deployment procedure

The deployment helper is inert unless explicitly run with `--apply`. It needs
an immutable, clean checkout at the tested full SHA. Its preflight checks the
customer worktree, remote SHA, and current read-only dry run. It backs up all
four publisher plists, stops them, removes the three legacy plists, installs
one canonical plist pointing to the release checkout, starts it, and waits for
healthy or explicitly degraded state with fresh-lane source/customer BUY parity
and stale lanes suppressed. Any cutover error
restores the prior plists and previously loaded jobs. The backup directory is
printed after success.

```sh
sudo /usr/bin/python3 scripts/deploy_parallax_customer_publisher.py \
  --apply --release-dir /absolute/path/to/immutable/release \
  --sha FULL_40_CHARACTER_TESTED_COMMIT_SHA --user scottsteele
```

Manual rollback uses the printed backup path:

```sh
sudo /usr/bin/python3 scripts/deploy_parallax_customer_publisher.py \
  --rollback '/Users/scottsteele/Library/Application Support/SwarmEdge/backups/parallax-customer-cutover-YYYYMMDDTHHMMSSZ' \
  --user scottsteele
```

Rollback restores the prior publisher arrangement. Because that arrangement
contains competing writers, use it only to recover from a failed cutover and
resolve the original publication risk before leaving it unattended.

The sports scanner service `com.swarmedge.parallax-sports` is outside this
cutover and is never addressed by the helper.
