import json

from paperbot.market_discovery import parse_market, parse_resolution


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
