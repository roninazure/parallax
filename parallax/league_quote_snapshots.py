"""Bounded local storage for read-only pregame executable book observations."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .economics import retail_example
from .models import NormalizedMarket
from .normalization import rules_digest


class LeagueQuoteSnapshotStore:
    """Append sanitized quote/depth snapshots; never stores credentials or raw payloads."""

    def __init__(self, root: str | Path, *, retention_days: int = 180) -> None:
        if not 1 <= retention_days <= 3650:
            raise ValueError("quote snapshot retention must be between 1 and 3650 days")
        self.root = Path(root).expanduser()
        self.retention = timedelta(days=retention_days)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("quote snapshot directory must be an owned directory")
        os.chmod(self.root, 0o700)

    def record(
        self,
        *,
        sport: str,
        fixture: Any,
        market: NormalizedMarket,
        observed_at: str,
        yes_team: str | None,
    ) -> Path | None:
        captured = datetime.now(UTC)
        snapshot = {
            "schema_version": 1,
            "captured_at": captured.isoformat(),
            "source_observed_at": observed_at,
            "book_timestamp": market.book_timestamp,
            "sport": sport,
            "game_id": fixture.game_id,
            "game_date": fixture.kickoff.astimezone(ZoneInfo("America/New_York"))
            .date()
            .isoformat(),
            "scheduled_start": fixture.kickoff.isoformat(),
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "venue": market.venue.value,
            "market_id": market.venue_market_id,
            "slug": market.slug,
            "selected_yes_team": yes_team,
            "rules_sha256": rules_digest(market),
            "status": market.status,
            "yes_bid": market.yes_bid,
            "yes_ask": market.yes_ask,
            "no_bid": market.no_bid,
            "no_ask": market.no_ask,
            "executable_depth": {
                side: [[float(price), float(size)] for price, size in levels]
                for side, levels in market.executable_depth.items()
            },
            "fee_rate": market.mechanics.fee_rate,
            "fee_rounding": market.mechanics.fee_rounding,
            "fee_status": market.mechanics.fee_status,
            "fee_source": market.mechanics.fee_source,
            "fee_observed_at": market.mechanics.fee_observed_at,
            "fee_buffer_per_contract": market.mechanics.fee_buffer_per_contract,
            "fee_scenario_25": {},
        }
        for side, price in (("YES", market.yes_ask), ("NO", market.no_ask)):
            levels = market.executable_depth.get(side, ())
            depth = sum(float(size) for _, size in levels)
            scenario = retail_example(25, price, depth, market.mechanics)
            snapshot["fee_scenario_25"][side] = {
                "available": scenario.available,
                "reason": scenario.reason,
                "contracts": scenario.contracts_or_shares,
                "fees_estimate": scenario.fees_estimate,
                "total_cost": scenario.total_cost,
                "maximum_loss_including_fees": scenario.maximum_loss_including_fees,
            }
        filename = "parallax-league-quotes-" + captured.strftime("%Y%m%d") + ".jsonl"
        target = self.root / filename
        try:
            source_time = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            if source_time.tzinfo is None:
                source_time = source_time.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            raise ValueError("quote snapshot requires a valid source observation timestamp")
        # Only retain observations with usable executable asks and source time.
        if source_time is None or not any(
            market.executable_depth.get(side) and price is not None
            for side, price in (("YES", market.yes_ask), ("NO", market.no_ask))
        ):
            raise ValueError("quote snapshot requires executable ask depth")
        dedupe_key = (sport, fixture.game_id, market.venue.value, market.venue_market_id,
                      snapshot["rules_sha256"], observed_at)
        if target.exists():
            try:
                with target.open("r", encoding="utf-8") as existing:
                    for line in existing:
                        row = json.loads(line)
                        if (row.get("sport"), row.get("game_id"), row.get("venue"),
                            row.get("market_id"), row.get("rules_sha256"),
                            row.get("source_observed_at")) == dedupe_key:
                            return None
            except (OSError, json.JSONDecodeError):
                pass
        encoded = (
            json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        )
        fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("quote snapshot target is not a regular file")
            data = memoryview(encoded.encode("utf-8"))
            while data:
                written = os.write(fd, data)
                data = data[written:]
        finally:
            os.close(fd)
        self.prune(now=captured)
        return target

    def prune(self, *, now: datetime | None = None) -> int:
        cutoff = (now or datetime.now(UTC)) - self.retention
        removed = 0
        for path in self.root.glob("parallax-league-quotes-*.jsonl"):
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff.timestamp():
                    path.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
        return removed
