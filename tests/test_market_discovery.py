import json

import requests

from paperbot import config
from paperbot.market_discovery import (
    BUCKET_SECONDS,
    _candidate_slugs,
    fetch_active_markets,
    infer_resolution_from_price,
    parse_market,
    parse_resolution,
)


def gamma_market_raw(slug="btc-updown-5m-1780138200", asset_ok=True):
    return {
        "conditionId": "0xabc",
        "slug": slug if asset_ok else "unrelated-market-slug",
        "question": "Bitcoin Up or Down - May 30, 6:50AM-6:55AM ET",
        "clobTokenIds": json.dumps(["111", "222"]),
        "outcomes": json.dumps(["Up", "Down"]),
        "endDate": "2026-05-30T06:55:00Z",
        "orderMinSize": "5",
        "orderPriceMinTickSize": "0.01",
    }


class TestParseMarket:
    def test_parses_a_valid_btc_market(self):
        market = parse_market(gamma_market_raw())
        assert market is not None
        assert market.asset == "Bitcoin"
        assert market.token_id_up == "111"
        assert market.token_id_down == "222"
        assert market.order_min_size == 5.0
        assert market.order_min_tick_size == 0.01

    def test_returns_none_for_unrelated_slug(self):
        market = parse_market(gamma_market_raw(asset_ok=False))
        assert market is None

    def test_returns_none_for_missing_token_ids(self):
        raw = gamma_market_raw()
        del raw["clobTokenIds"]
        assert parse_market(raw) is None

    def test_returns_none_for_non_up_down_outcomes(self):
        raw = gamma_market_raw()
        raw["outcomes"] = json.dumps(["Yes", "No"])
        assert parse_market(raw) is None

    def test_returns_none_for_missing_end_date(self):
        raw = gamma_market_raw()
        del raw["endDate"]
        assert parse_market(raw) is None

    def test_bnb_and_hyperliquid_slugs_recognized(self):
        bnb = parse_market(gamma_market_raw(slug="bnb-updown-5m-1786127400"))
        hype = parse_market(gamma_market_raw(slug="hype-updown-5m-1780138200"))
        assert bnb.asset == "BNB"
        assert hype.asset == "Hyperliquid"


class TestCandidateSlugs:
    def test_slugs_align_to_the_300_second_grid(self):
        # 1786127401 is one second into a bucket that starts at 1786127400.
        slugs = _candidate_slugs("BNB", now=1786127401)
        assert slugs == ["bnb-updown-5m-1786127400", "bnb-updown-5m-1786127700"]

    def test_exactly_on_a_boundary(self):
        assert 1786127400 % BUCKET_SECONDS == 0
        slugs = _candidate_slugs("BNB", now=1786127400)
        assert slugs[0] == "bnb-updown-5m-1786127400"

    def test_uses_the_right_prefix_per_asset(self):
        for asset, prefix in config.ASSET_SLUG_PREFIXES.items():
            slugs = _candidate_slugs(asset, now=1786127401)
            assert all(s.startswith(prefix) for s in slugs)


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        return self._payload


class FakeSession:
    """Simulates exactly the endpoints market_discovery.py is allowed to
    call, and fails loudly if anything tries the old full-table-scan
    pagination shape (offset param) that caused the real 422 in
    production."""

    def __init__(self, by_slug: dict):
        self.by_slug = by_slug
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        assert "offset" not in (params or {}), (
            "market discovery must never paginate the full active-markets "
            "table -- that's what produced the real 422 in production"
        )
        slug = (params or {}).get("slug")
        if slug in self.by_slug:
            return FakeResponse(200, [self.by_slug[slug]])
        if slug == "boom":
            return FakeResponse(422, None)
        return FakeResponse(200, [])


class TestFetchActiveMarketsUsesSlugLookupNotPagination:
    def test_never_sends_an_offset_param_and_finds_known_markets(self):
        raw_btc = gamma_market_raw(slug="btc-updown-5m-1786127400")
        session = FakeSession({"btc-updown-5m-1786127400": raw_btc})

        markets = fetch_active_markets(session=session, assets=["Bitcoin"],
                                        now=1786127401)

        assert len(markets) == 1
        assert markets[0].asset == "Bitcoin"
        # exactly 2 lookups (current + next bucket), no full-table scan
        assert len(session.calls) == 2

    def test_a_422_on_one_slug_does_not_abort_the_whole_poll(self):
        """Direct regression test for the production crash: a bad response
        for one slug must not prevent the other five assets' lookups
        (or the other bucket) from succeeding."""
        raw_eth = gamma_market_raw(slug="eth-updown-5m-1786127700")

        class FlakySession(FakeSession):
            def get(self, url, params=None, timeout=None):
                self.calls.append(params)
                slug = (params or {}).get("slug", "")
                if slug.startswith("btc-"):
                    return FakeResponse(422, None)
                return super().get(url, params=params, timeout=timeout)

        session = FlakySession({"eth-updown-5m-1786127700": raw_eth})
        markets = fetch_active_markets(session=session, assets=["Bitcoin", "Ethereum"],
                                        now=1786127401)

        assert len(markets) == 1
        assert markets[0].asset == "Ethereum"

    def test_closed_markets_are_excluded(self):
        raw = gamma_market_raw(slug="btc-updown-5m-1786127400")
        raw["closed"] = True
        session = FakeSession({"btc-updown-5m-1786127400": raw})
        markets = fetch_active_markets(session=session, assets=["Bitcoin"], now=1786127401)
        assert markets == []


class TestParseResolution:
    def test_open_market_returns_none(self):
        raw = {"closed": False, "outcomes": json.dumps(["Up", "Down"]),
               "outcomePrices": json.dumps(["0.5", "0.5"])}
        assert parse_resolution(raw) is None

    def test_closed_and_decisively_resolved_up(self):
        raw = {"closed": True, "outcomes": json.dumps(["Up", "Down"]),
               "outcomePrices": json.dumps(["1", "0"])}
        assert parse_resolution(raw) == "Up"

    def test_closed_and_decisively_resolved_down(self):
        raw = {"closed": True, "outcomes": json.dumps(["Up", "Down"]),
               "outcomePrices": json.dumps(["0.02", "0.98"])}
        assert parse_resolution(raw) == "Down"

    def test_closed_but_not_yet_decisive_returns_none(self):
        raw = {"closed": True, "outcomes": json.dumps(["Up", "Down"]),
               "outcomePrices": json.dumps(["0.5", "0.5"])}
        assert parse_resolution(raw) is None

    def test_malformed_payload_returns_none_not_an_exception(self):
        assert parse_resolution({"closed": True}) is None


class TestInferResolutionFromPrice:
    """Direct regression tests for a live-confirmed issue: Gamma's `closed`
    flag can stay False for 30+ minutes on these 5-minute markets even
    once the price is fully decisive and stable -- confirmed by
    pending_resolution growing unbounded (36+ markets, zero ever resolving
    via parse_resolution alone) while individual markets sat at
    outcomePrices like ["0.9995", "0.0005"] for 10+ minutes straight."""

    def test_ignores_closed_entirely(self):
        raw = {"closed": False, "outcomes": json.dumps(["Up", "Down"]),
               "outcomePrices": json.dumps(["0.995", "0.005"])}
        assert infer_resolution_from_price(raw) == "Up"

    def test_below_threshold_returns_none(self):
        raw = {"closed": False, "outcomes": json.dumps(["Up", "Down"]),
               "outcomePrices": json.dumps(["0.9", "0.1"])}
        assert infer_resolution_from_price(raw, threshold=0.99) is None

    def test_default_threshold_is_stricter_than_parse_resolutions(self):
        # 0.95 would pass parse_resolution's 0.9 bar (if closed) but not
        # this function's default 0.99 -- there's no `closed` confirmation
        # to lean on here, so the bar is deliberately higher.
        raw = {"closed": False, "outcomes": json.dumps(["Up", "Down"]),
               "outcomePrices": json.dumps(["0.95", "0.05"])}
        assert infer_resolution_from_price(raw) is None

    def test_malformed_payload_returns_none(self):
        assert infer_resolution_from_price({"closed": False}) is None

    def test_picks_the_higher_priced_outcome(self):
        raw = {"outcomes": json.dumps(["Up", "Down"]),
               "outcomePrices": json.dumps(["0.001", "0.999"])}
        assert infer_resolution_from_price(raw) == "Down"
