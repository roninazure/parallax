"""Bounded, read-only CFB V1 PMUS/Kalshi acceptance scan."""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maker_spread_economics.polymarket_us import PolymarketUSPublicClient, PolymarketUSRateLimit, PolymarketUSDiscoveryFailure
from parallax.alerts import AlertDeliveryStore, AlertDispatcher, dispatch_scored_buy
from parallax.cfb import MODEL_VERSION, VALIDATION_ECE, VALIDATION_REFERENCE, CFBEvidenceProvider, _canonical_team, active_cfb_season, fetch_games, is_supported_market, map_market_to_game, probability_for_game
from parallax.discovery import MAX_ACTIVE_MARKETS_PER_VENUE, paginate, paginate_collection
from parallax.economics import retail_example
from parallax.engine import qualify
from parallax.fees import attach_fees
from parallax.inbox import default_inbox_store
from parallax.models import Action, Side, Venue, utcnow
from parallax.normalization import normalize_kalshi, normalize_pmus
from parallax.sources import KalshiPublicClient
from parallax.track_record import TrackRecord

CFB_MARKET_DISCOVERY_LIMIT = 1_000
PROSPECTIVE_DB = Path("data/parallax-commercial/prospective.sqlite")

# Reviewed directly against https://docs.polymarket.us/fees on 2026-11-22.
# Effective 2026-11-14: tier 1 has the highest taker coefficient (0.095).
# Exclude API/onboarding rebates to produce an account-independent upper bound.
# Short-lived CFB-only policy: do not mutate the NFL/global fee schedule.
CFB_PMUS_FEE_REVIEWED = datetime.fromisoformat("2026-11-22T00:00:00+00:00")
CFB_PMUS_FEE_EXPIRES = datetime.fromisoformat("2026-11-29T00:00:00+00:00")


def _attach_cfb_pmus_fees(market, now):
    """Apply a documented worst-tier CFB-only taker fee; expire closed."""
    if not CFB_PMUS_FEE_REVIEWED <= now < CFB_PMUS_FEE_EXPIRES:
        return attach_fees(market, now)
    source = "https://docs.polymarket.us/fees"
    mechanics = replace(
        market.mechanics,
        fee_rate=0.095,
        fee_source=source,
        fee_rounding="HALF_EVEN",
        fee_status="VERIFIED_UPPER_BOUND",
        fee_observed_at=now.isoformat(),
        fee_valid_until=CFB_PMUS_FEE_EXPIRES.isoformat(),
        fee_buffer_per_contract=0,
    )
    provenance = {
        "schedule": source,
        "reviewed_at": CFB_PMUS_FEE_REVIEWED.isoformat(),
        "review_expires": CFB_PMUS_FEE_EXPIRES.isoformat(),
        "reason": "2026-11-14 tier-1 taker theta 0.095; excludes API and onboarding rebates",
        "status": "VERIFIED_UPPER_BOUND",
        "scope": "CFB full-game standard taker entry; no rebates assumed",
    }
    return replace(market, mechanics=mechanics,
                   original_metadata={**market.original_metadata, "fee_provenance": provenance})



# Independent, expiring CFB-only Kalshi review. Never refresh this date from a quote.
# Source: https://kalshi.com/fee-schedule, reviewed 2026-11-24.
CFB_KALSHI_FEE_REVIEWED = datetime.fromisoformat("2026-11-24T00:00:00+00:00")
CFB_KALSHI_FEE_EXPIRES = datetime.fromisoformat("2026-12-01T00:00:00+00:00")


def _attach_cfb_kalshi_fees(market, now, *, event, series):
    """Conservative quadratic taker estimate only for proven live CFB metadata.

    Unknown fee overrides, stale quotes, unsupported types and missing per-series
    metadata retain the global fail-closed UNVERIFIED status.
    """
    fallback = attach_fees(market, now, event=event, series=series)
    if not CFB_KALSHI_FEE_REVIEWED <= now < CFB_KALSHI_FEE_EXPIRES:
        return fallback
    raw = market.original_metadata.get("market") or {}
    sid = (series or {}).get("ticker")
    eid = (event or {}).get("event_ticker") or (event or {}).get("ticker")
    if not sid or sid not in {"KXNCAAFGAME", "KXCFBGAME"}:
        return fallback
    if not eid or market.event != eid or raw.get("event_ticker") != eid:
        return fallback
    if raw.get("ticker") != market.venue_market_id:
        return fallback
    try:
        observed = datetime.fromisoformat(market.data_timestamp.replace("Z", "+00:00"))
        if observed.tzinfo is None or not 0 <= (now - observed).total_seconds() < 60:
            return fallback
    except (AttributeError, TypeError, ValueError):
        return fallback
    if any(k.startswith("fee_") and v is not None for k, v in raw.items()):
        return fallback  # waiver/market-specific exceptions require independent verification
    kind = (event or {}).get("fee_type_override")
    if kind is None:
        kind = series.get("fee_type")
    multiplier = (event or {}).get("fee_multiplier_override")
    if multiplier is None:
        multiplier = series.get("fee_multiplier")
    try:
        from decimal import Decimal
        if isinstance(multiplier, bool):
            return fallback
        m = Decimal(str(multiplier))
        if not m.is_finite() or m < 0 or m > 10:
            return fallback
    except (TypeError, ValueError, ArithmeticError):
        return fallback
    if kind not in {"quadratic", "quadratic_with_maker_fees"}:
        return fallback
    mechanics = replace(
        market.mechanics,
        fee_rate=float(Decimal("0.07") * m),
        fee_source="https://kalshi.com/fee-schedule",
        fee_rounding="KALSHI_BALANCE",
        fee_status="VERIFIED_UPPER_BOUND",
        fee_observed_at=now.isoformat(),
        fee_valid_until=min(CFB_KALSHI_FEE_EXPIRES, observed + timedelta(seconds=60)).isoformat(),
        fee_buffer_per_contract=0,
        fee_balance_precision=0.01,
    )
    return replace(market, mechanics=mechanics, original_metadata={
        **market.original_metadata,
        "fee_provenance": {
            "schedule": "https://kalshi.com/fee-schedule",
            "reviewed_at": CFB_KALSHI_FEE_REVIEWED.isoformat(),
            "review_expires": CFB_KALSHI_FEE_EXPIRES.isoformat(),
            "verified_series": sid,
            "effective_multiplier": str(m),
            "fee_type": kind,
            "status": "VERIFIED_UPPER_BOUND",
            "scope": "CFB quadratic taker entry; no rebates or waivers",
        },
    })

def _dispatch_eligible_buy_alert(
    dispatcher,
    play,
    market,
    mapping,
    detected_at,
    *,
    publication_eligible: bool,
    economic_key: str | None,
    selected_team: str | None,
):
    """Dispatch only a certified CFB BUY; alert failures cannot stop scanning."""
    if not publication_eligible or play.suggested_action != Action.BUY:
        return None
    try:
        return dispatch_scored_buy(
            dispatcher,
            play,
            market,
            sport="CFB",
            matchup=f"{mapping.game.away_team} at {mapping.game.home_team}",
            detected_at=detected_at.isoformat(),
            game_start=mapping.game.kickoff,
            economic_key=economic_key,
            selected_side=selected_team,
        )
    except Exception:
        return {
            "status": "FAILED",
            "deduplicated": False,
            "http_status": None,
            "error_code": "ALERT_DISPATCH_EXCEPTION",
        }


def _selected_team(mapping, side: Side) -> str | None:
    if mapping.game is None or not mapping.selected_team:
        return None
    yes_key = _canonical_team(mapping.selected_team)
    home_key = _canonical_team(mapping.game.home_team)
    away_key = _canonical_team(mapping.game.away_team)
    if yes_key == home_key:
        yes_team, no_team = mapping.game.home_team, mapping.game.away_team
    elif yes_key == away_key:
        yes_team, no_team = mapping.game.away_team, mapping.game.home_team
    else:
        return None
    return yes_team if side == Side.YES else no_team


def _capture_evaluated(store, market, side, evidence, now):
    """Capture exactly one already-qualified live market-side observation."""
    play = qualify(market, side, evidence, now=now)
    observation = store.capture_prospective(market, side, evidence, now=now)
    observation_id = observation.get("observation_id") if isinstance(observation, dict) else None
    expected = (play.id, play.venue, play.market_id, play.side)
    actual = tuple(observation.get(key) for key in ("play_id", "venue", "market_id", "side")) if isinstance(observation, dict) else None
    if not observation_id or actual != expected or store.prospective_record(observation_id) is None:
        raise ValueError("Prospective capture did not produce a verified durable observation ID")
    return play


def _text(row: dict) -> str:
    return json.dumps(row, sort_keys=True, default=str).lower()


def _is_cfb(row: dict, *parents: dict) -> bool:
    text = " ".join(_text(x) for x in (row, *parents))
    return bool(re.search(r"\b(?:cfb|ncaaf|college football)\b|kx(?:ncaaf|cfb)", text, re.I))


def _scope_pmus(client: PolymarketUSPublicClient, games) -> tuple[list[dict], dict]:
    """Query exact schedule-derived PMUS aec-cfb full-game winner slugs.

    Unlike the highest-volume global market search, this cannot be displaced
    by 1,000 derivative/futures contracts. Unknown team codes fail closed.
    Every returned slug is still independently normalized and mapped below.
    """
    code_path = Path(__file__).resolve().parents[1] / "parallax/data/cfb_pmus_team_codes.json"
    source_codes = json.loads(code_path.read_text(encoding="utf-8"))
    team_codes: dict[str, str] = {}
    ambiguous: set[str] = set()
    for name, code in source_codes.items():
        canonical = _canonical_team(name)
        if canonical in team_codes and team_codes[canonical] != code:
            ambiguous.add(canonical)
        else:
            team_codes[canonical] = code
    for name in ambiguous:
        team_codes.pop(name, None)

    now = utcnow()
    horizon = now + timedelta(days=8)
    slugs: list[str] = []
    unknown_games = 0
    scheduled = 0
    for game in games:
        kickoff = datetime.fromisoformat(game.kickoff.replace("Z", "+00:00")).astimezone(UTC)
        if not now < kickoff <= horizon or (
            game.home_classification != "fbs" or game.away_classification != "fbs"
        ):
            continue
        scheduled += 1
        away = team_codes.get(_canonical_team(game.away_team))
        home = team_codes.get(_canonical_team(game.home_team))
        if not away or not home or away == home:
            unknown_games += 1
            continue
        local_date = kickoff.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        slugs.append(f"aec-cfb-{away}-{home}-{local_date}")
    slugs = list(dict.fromkeys(slugs))
    limited = len(slugs) > 100
    slugs = slugs[:100]
    if not slugs:
        return [], {
            "state": "PARTIAL" if scheduled else "COMPLETE",
            "reason": "no supported schedule-derived PMUS game-winner slugs",
            "scheduled_games": scheduled, "unknown_team_games": unknown_games,
            "candidate_slugs": 0, "request_count": 0,
        }

    rows = client.markets_page(
        limit=len(slugs), offset=0, slugs=slugs,
        retry_transport_errors=False,
    )
    unexpected = [r for r in rows if str(r.get("slug") or "") not in slugs]
    if unexpected:
        raise ValueError("PMUS exact CFB discovery response escaped requested slug scope")
    supported = [
        row for row in rows
        if row.get("active") and not row.get("closed") and _is_cfb(row, row.get("raw", {}))
    ]
    return supported, {
        "state": "PARTIAL" if limited or unknown_games else "TARGETED",
        "reason": "exact schedule-derived PMUS CFB winner slugs queried",
        "scheduled_games": scheduled, "unknown_team_games": unknown_games,
        "candidate_slugs": len(slugs),
        "markets_returned": len(rows), "moneyline_candidates": len(supported),
        "request_count": 1,
    }



def _unique_kalshi_discoveries(rows: list[dict]) -> tuple[list[dict], int, int]:
    """Deduplicate identical contracts and fail closed on conflicting identities.

    A ticker is the contract identity, not an event or selected team. A conflict
    across discovery paths disqualifies that ticker from acquisition entirely.
    """
    selected: dict[str, dict] = {}
    conflicts: set[str] = set()
    duplicates = 0
    for row in rows:
        ticker = str(row.get("ticker") or "").strip()
        if not ticker:
            continue
        if ticker in conflicts:
            continue
        previous = selected.get(ticker)
        if previous is None:
            selected[ticker] = row
        elif previous == row:
            duplicates += 1
        else:
            conflicts.add(ticker)
            selected.pop(ticker, None)
    return list(selected.values()), duplicates, len(conflicts)

def _scope_kalshi(client: KalshiPublicClient) -> tuple[list[dict], dict]:
    """Reserve Kalshi's existing per-client request budget for executable books.

    Discovery is intentionally partial: exhausting the entire public-call budget
    during enumeration would guarantee zero fresh CFB scoring.
    """
    # Keep initial launchd acceptance bounded: no more than eight discovery
    # calls; retain the original per-client ceiling for the whole scan.
    discovery_budget = min(8, max(2, client.max_requests // 2))
    series, coverage = paginate_collection(
        client.series_page, key="series", max_pages=1
    )
    cfb_series = [
        item for item in series
        if str(item.get("ticker") or "").upper() in {"KXNCAAFGAME", "KXCFBGAME"}
    ]
    # The /series first page is not an exhaustive index. If no CFB series
    # appears there, query the two known full-game series directly rather than
    # incorrectly reporting complete coverage and zero available markets.
    direct_series_fallback = not cfb_series
    if direct_series_fallback:
        cfb_series = [{"ticker": ticker} for ticker in ("KXNCAAFGAME", "KXCFBGAME")]
    rows: list[dict] = []
    failures: list[str] = []
    budget_reached = False
    for series_row in cfb_series:
        if client.request_count >= discovery_budget:
            budget_reached = True
            break
        sid = str(series_row.get("ticker") or series_row.get("id") or "")
        try:
            # Direct-series fallback lacks fee metadata; retrieve its live series document.
            if not series_row.get("fee_type") or series_row.get("fee_multiplier") is None:
                if client.request_count + 1 >= discovery_budget:
                    budget_reached = True
                    break
                series_row = client.series(sid)
                if series_row.get("ticker") != sid:
                    failures.append("SERIES_METADATA_MISMATCH")
                    continue
            events = list(client.events_page(series_ticker=sid, limit=100).get("events", []))
            local_today = utcnow().astimezone(ZoneInfo("America/New_York")).date()

            def game_date(event):
                ticker = str(event.get("ticker") or event.get("event_ticker") or "").upper()
                found = re.search(r"-(\d{2}[A-Z]{3}\d{2})", ticker)
                if found:
                    try:
                        return datetime.strptime(found.group(1), "%y%b%d").date()
                    except ValueError:
                        pass
                value = event.get("scheduled_start") or event.get("start_time")
                if value:
                    try:
                        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                        if parsed.tzinfo is None:
                            return None
                        return parsed.astimezone(ZoneInfo("America/New_York")).date()
                    except ValueError:
                        pass
                return None

            # Prefer upcoming games before consuming the bounded discovery pool.
            events.sort(key=lambda event: (
                game_date(event) is None,
                game_date(event) is not None and game_date(event) < local_today,
                game_date(event) or local_today,
            ))
            for event in events:
                date = game_date(event)
                if date is not None and date < local_today:
                    continue
                if client.request_count >= discovery_budget:
                    budget_reached = True
                    break
                eid = str(event.get("ticker") or event.get("event_ticker") or event.get("id") or "")
                markets = client.event_markets_page(event_ticker=eid, limit=100).get("markets", [])
                rows.extend(
                    {**market, "_discovery_event": event, "_discovery_series": series_row}
                    for market in markets
                )
                if len(rows) >= MAX_ACTIVE_MARKETS_PER_VENUE:
                    budget_reached = True
                    break
        except Exception as exc:
            failures.append(type(exc).__name__)
        if budget_reached:
            break
    # A contract discovered by multiple paths must only be scored once.
    # Conflicting discovery metadata is excluded rather than guessed.
    rows, duplicate_contracts, conflicting_contracts = _unique_kalshi_discoveries(rows)
    # Deliberately report partial coverage, even if the first page appeared complete.
    # Uninspected event pages and scopes must never be counted as complete.
    return rows, {
        "state": "PARTIAL" if budget_reached or failures or direct_series_fallback or cfb_series else "COMPLETE",
        "reason": "bounded discovery with direct known-series fallback and reserved book budget",
        "direct_series_fallback": direct_series_fallback,
        "series_pages": coverage.pages,
        "cfb_series": len(cfb_series),
        "discovery_requests": client.request_count,
        "reserved_book_requests": max(0, client.max_requests - client.request_count),
        "failed_scopes": dict(Counter(failures)),
        "duplicate_contracts_skipped": duplicate_contracts,
        "conflicting_contracts_excluded": conflicting_contracts,
    }

def _scan(*, alert_dispatcher=None) -> dict:
    seasons = (active_cfb_season(datetime.now(UTC)),)
    games = fetch_games(seasons)
    result = {"read_only": True, "orders": 0, "alerts": 0, "published": 0, "prospective_captured": 0, "cfbd_calls": len(seasons), "validation_ece": VALIDATION_ECE, "venues": {}, "rows": [], "mapping_failure_reasons": Counter()}
    prospective_store = TrackRecord(PROSPECTIVE_DB)
    if alert_dispatcher is None:
        try:
            alert_dispatcher = AlertDispatcher(
                AlertDeliveryStore(default_inbox_store().path)
            )
        except Exception:
            alert_dispatcher = None
    pmus_client = PolymarketUSPublicClient()
    try:
        pmus_rows, coverage = _scope_pmus(pmus_client, games)
        result["venues"]["PMUS"] = {"coverage": coverage, "discovered": len(pmus_rows)}
    except (PolymarketUSRateLimit, PolymarketUSDiscoveryFailure) as exc:
        # Provider outage/rate limit must not suppress the independent Kalshi lane.
        # Never turn unavailable PMUS quotes into fabricated or cached BUYs.
        pmus_rows = []
        result["venues"]["PMUS"] = {
            "coverage": {"state": "UNAVAILABLE", "reason": "provider_discovery_failure", "error_type": type(exc).__name__},
            "discovered": 0,
        }
    finally:
        pmus_client.close()
    kalshi_client = KalshiPublicClient()
    try:
        kalshi_rows, coverage = _scope_kalshi(kalshi_client)
        result["venues"]["KALSHI"] = {"coverage": coverage, "discovered": len(kalshi_rows), "endpoint_status": coverage["state"]}
    except Exception as exc:
        kalshi_rows = []
        result["venues"]["KALSHI"] = {"coverage": {"state": "PARTIAL", "error_type": type(exc).__name__}, "discovered": 0, "endpoint_status": "UNAVAILABLE"}

    statuses: dict[str, Counter] = {"PMUS": Counter(), "KALSHI": Counter()}
    scored: list[dict] = []
    result["scoring_sqlite_errors"] = Counter()
    # One client across all PMUS public books preserves the per-minute meter.
    pmus_book_client = PolymarketUSPublicClient(timeout_seconds=8)
    pmus_book_limit = 12
    pmus_book_count = 0
    pmus_provider_limited = False
    for venue_name, raw_rows in (("PMUS", pmus_rows), ("KALSHI", kalshi_rows)):
        for raw in raw_rows:
            event = raw.get("_discovery_event") if venue_name == "KALSHI" else None
            series = raw.get("_discovery_series") if venue_name == "KALSHI" else None
            try:
                market = normalize_pmus(raw, {}, utcnow().isoformat()) if venue_name == "PMUS" else normalize_kalshi(raw, {}, utcnow().isoformat(), event=event)
            except (KeyError, TypeError, ValueError):
                statuses[venue_name]["MALFORMED"] += 1
                continue
            if not is_supported_market(market):
                mapping = map_market_to_game(market, games)
                statuses[venue_name][mapping.status] += 1
                result["mapping_failure_reasons"][mapping.status] += 1
                continue
            statuses[venue_name]["ELIGIBLE_MONEYLINE"] += 1
            mapping = map_market_to_game(market, games)
            statuses[venue_name][mapping.status] += 1
            if mapping.status != "MAPPED" or mapping.game is None:
                result["mapping_failure_reasons"][mapping.status] += 1
                continue
            try:
                if venue_name == "PMUS":
                    if pmus_provider_limited:
                        statuses[venue_name]["ACQUISITION_PROVIDER_RATE_LIMIT"] += 1
                        continue
                    if pmus_book_count >= pmus_book_limit:
                        statuses[venue_name]["ACQUISITION_BUDGET_EXHAUSTED"] += 1
                        continue
                    pmus_book_count += 1
                    book = pmus_book_client.book(raw["slug"])
                    market = normalize_pmus(raw, book, utcnow().isoformat())
                    market = _attach_cfb_pmus_fees(market, utcnow())
                else:
                    if kalshi_client.request_count >= kalshi_client.max_requests:
                        statuses[venue_name]["ACQUISITION_BUDGET_EXHAUSTED"] += 1
                        continue
                    book = kalshi_client.book(raw["ticker"])
                    market = _attach_cfb_kalshi_fees(normalize_kalshi(raw, book, utcnow().isoformat(), event=event), utcnow(), event=event, series=series)
                statuses[venue_name]["EXECUTABLE_BOOK"] += 1
                evidence = CFBEvidenceProvider(lambda games=games: games).assess(market)
                if evidence is None:
                    statuses[venue_name]["NO_EVIDENCE"] += 1
                    continue
                statuses[venue_name]["EVIDENCE"] += 1
                for side in Side:
                    decision_at = utcnow()
                    play = _capture_evaluated(
                        prospective_store, market, side, evidence, decision_at
                    )
                    result["prospective_captured"] += 1
                    example_rows = {}
                    for index, stake in ((1, 25), (2, 50), (3, 100)):
                        ex = play.retail_examples[index]
                        example_rows[f"net_ev_{stake}"] = (play.model_probability * ex.estimated_payout_if_correct - ex.total_cost) if play.model_probability is not None and ex.available and ex.total_cost is not None else None
                    selected_team = _selected_team(mapping, side)
                    cfb_safety = play.edge_points is not None and play.edge_points > VALIDATION_ECE * 100 + 1e-9
                    evidence_certified = bool(
                        play.evidence
                        and play.evidence.source == "CollegeFootballData"
                        and play.evidence.model_version == MODEL_VERSION
                        and play.evidence.validation_status == "CALIBRATED"
                        and play.evidence.validation_reference == VALIDATION_REFERENCE
                    )
                    publication_eligible = bool(
                        play.suggested_action == Action.BUY
                        and not play.verdict.failed_gates
                        and play.data_freshness == "FRESH"
                        and play.status == "CURRENT"
                        and market.status == "OPEN"
                        and mapping.status == "MAPPED"
                        and selected_team
                        and cfb_safety
                        and evidence_certified
                    )
                    row = {"venue": venue_name, "market_id": market.venue_market_id, "matchup": f"{mapping.game.away_team} at {mapping.game.home_team}", "game_id": mapping.game.game_id, "away_team": mapping.game.away_team, "home_team": mapping.game.home_team, "selected_team": selected_team, "economic_key": f"CFB:{mapping.game.game_id}:{_canonical_team(selected_team)}" if selected_team else None, "kickoff_utc": mapping.game.kickoff, "game_start": mapping.game.kickoff, "side": side.value, "cfb_v1_probability": play.model_probability, "executable_price": play.executable_price, "raw_edge": play.edge_points, "cfb_safety": cfb_safety, "fee": play.fees_estimate, "fee_status": market.mechanics.fee_status, "net_ev_25": play.expected_value, "expected_return": play.expected_return, **example_rows, "max_loss": play.retail_examples[1].maximum_loss, "payout": play.retail_examples[1].estimated_payout_if_correct, "liquidity": play.executable_size, "verdict": play.suggested_action.value, "failed_gates": list(play.verdict.failed_gates), "mapping_status": mapping.status, "acquisition_status": "ACQUIRED", "market_status": market.status, "data_freshness": play.data_freshness, "status": play.status, "updated_at": play.updated_at, "expires_at": play.expires_at, "evidence_source": play.evidence.source if play.evidence else None, "evidence_model_version": play.evidence.model_version if play.evidence else None, "evidence_validation_status": play.evidence.validation_status if play.evidence else None, "evidence_validation_reference": play.evidence.validation_reference if play.evidence else None, "publication_eligible": publication_eligible}
                    scored.append(row)
                    statuses[venue_name]["FULLY_SCORED_SIDES"] += 1
                    statuses[venue_name][play.suggested_action.value] += 1
                    alert = _dispatch_eligible_buy_alert(
                        alert_dispatcher,
                        play,
                        market,
                        mapping,
                        decision_at,
                        publication_eligible=publication_eligible,
                        economic_key=row["economic_key"],
                        selected_team=selected_team,
                    )
                    if alert is not None:
                        if alert["deduplicated"]:
                            statuses[venue_name]["ALERT_DEDUPLICATED"] += 1
                        elif alert["status"] == "SENT":
                            result["alerts"] += 1
                        elif alert["status"] in {"FAILED", "UNKNOWN", "PENDING"}:
                            statuses[venue_name]["ALERT_ERROR"] += 1
            except Exception as exc:
                statuses[venue_name][f"SCORING_{type(exc).__name__}"] += 1
                if venue_name == "PMUS" and type(exc).__name__ == "PolymarketUSRateLimit":
                    pmus_provider_limited = True
                if type(exc).__name__ == "OperationalError":
                    # SQLite symbolic error names contain no query or secrets.
                    code = getattr(exc, "sqlite_errorname", None) or "UNCLASSIFIED"
                    result["scoring_sqlite_errors"][str(code)[:48]] += 1
    pmus_book_client.close()
    result["scoring_sqlite_errors"] = dict(result["scoring_sqlite_errors"])
    result["venues"]["PMUS"]["book_request_budget"] = pmus_book_limit
    result["venues"]["PMUS"]["book_requests_made"] = pmus_book_count
    for venue, data in result["venues"].items():
        counts = statuses[venue]
        result["venues"][venue].update({"eligible": counts["ELIGIBLE_MONEYLINE"], "mapped": counts["MAPPED"], "evidence": counts["EVIDENCE"], "model_probability": counts["EVIDENCE"], "executable_book": counts["EXECUTABLE_BOOK"], "fee": sum(1 for row in scored if row["venue"] == venue and row["fee_status"] in {"VERIFIED_UPPER_BOUND", "VERIFIED_SCHEDULE", "REVIEWED"}), "edge": sum(1 for row in scored if row["venue"] == venue and row["raw_edge"] is not None), "economics": sum(1 for row in scored if row["venue"] == venue and row["net_ev_25"] is not None), "fully_scored_sides": counts["FULLY_SCORED_SIDES"], "BUY": counts["BUY"], "WATCH": counts["WATCH"], "PASS": counts["PASS"], "status_counts": dict(counts)})
    result["rows"] = sorted(scored, key=lambda row: (row["verdict"] != "BUY", -(row["raw_edge"] or -1)))[:50]
    result["mapping_failure_reasons"] = dict(result["mapping_failure_reasons"])
    return result


if __name__ == "__main__":
    print(json.dumps(_scan(), default=lambda value: value.value if hasattr(value, "value") else value, allow_nan=False))
