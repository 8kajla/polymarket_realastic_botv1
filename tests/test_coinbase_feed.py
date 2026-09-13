import asyncio

import pytest

from paperbot.coinbase_feed import CoinbaseSpotFeed


class FakeResponse:
    def __init__(self, price):
        self._price = price

    def raise_for_status(self):
        pass

    def json(self):
        return {"price": str(self._price)}


class FakeSession:
    """Returns a fixed (or sequenced) price per product, recording every
    call so tests can assert on poll cadence without a real network."""

    def __init__(self, price_by_product):
        self.price_by_product = price_by_product
        self.calls = []

    def get(self, url, timeout=None, headers=None):
        product = url.split("/products/")[1].split("/")[0]
        self.calls.append(product)
        return FakeResponse(self.price_by_product[product])


class TestTrailingReturn:
    def test_none_before_any_poll(self):
        feed = CoinbaseSpotFeed(assets=["Bitcoin"])
        assert feed.trailing_return("Bitcoin", now=1000.0, minutes=3.0) is None

    def test_none_for_an_unconfigured_asset(self):
        feed = CoinbaseSpotFeed(assets=["Bitcoin"])
        assert feed.trailing_return("Dogecoin", now=1000.0, minutes=3.0) is None

    def test_computes_return_from_two_samples(self):
        feed = CoinbaseSpotFeed(assets=["Bitcoin"])
        feed._history["Bitcoin"].append((0.0, 100.0))
        feed._history["Bitcoin"].append((180.0, 105.0))
        ret = feed.trailing_return("Bitcoin", now=180.0, minutes=3.0)
        assert ret == pytest.approx(0.05)

    def test_negative_return(self):
        feed = CoinbaseSpotFeed(assets=["Bitcoin"])
        feed._history["Bitcoin"].append((0.0, 100.0))
        feed._history["Bitcoin"].append((180.0, 95.0))
        ret = feed.trailing_return("Bitcoin", now=180.0, minutes=3.0)
        assert ret == pytest.approx(-0.05)

    def test_uses_the_oldest_sample_at_or_after_the_target_time(self):
        feed = CoinbaseSpotFeed(assets=["Bitcoin"])
        feed._history["Bitcoin"].append((0.0, 90.0))    # older than the 3min window
        feed._history["Bitcoin"].append((10.0, 100.0))  # this is the one that should be used
        feed._history["Bitcoin"].append((190.0, 110.0))
        ret = feed.trailing_return("Bitcoin", now=190.0, minutes=3.0)
        assert ret == pytest.approx((110.0 - 100.0) / 100.0)

    def test_none_when_nothing_old_enough_exists_yet(self):
        # Only 30s of history so far -- can't answer a 3-minute question.
        feed = CoinbaseSpotFeed(assets=["Bitcoin"])
        feed._history["Bitcoin"].append((0.0, 100.0))
        feed._history["Bitcoin"].append((30.0, 101.0))
        assert feed.trailing_return("Bitcoin", now=30.0, minutes=3.0) is None

    def test_tracks_each_asset_independently(self):
        feed = CoinbaseSpotFeed(assets=["Bitcoin", "Ethereum"])
        feed._history["Bitcoin"].append((0.0, 100.0))
        feed._history["Bitcoin"].append((180.0, 110.0))
        feed._history["Ethereum"].append((0.0, 50.0))
        feed._history["Ethereum"].append((180.0, 45.0))
        assert feed.trailing_return("Bitcoin", now=180.0, minutes=3.0) == pytest.approx(0.10)
        assert feed.trailing_return("Ethereum", now=180.0, minutes=3.0) == pytest.approx(-0.10)


class TestPollTick:
    def test_polls_every_configured_asset_on_first_tick(self):
        session = FakeSession({"BTC-USD": 100.0, "ETH-USD": 50.0, "SOL-USD": 20.0})
        feed = CoinbaseSpotFeed(assets=["Bitcoin", "Ethereum", "Solana"], session=session,
                                 poll_interval=15.0)
        asyncio.run(feed.poll_tick(now=1000.0))
        assert sorted(session.calls) == ["BTC-USD", "ETH-USD", "SOL-USD"]
        assert feed._history["Bitcoin"][-1] == (1000.0, 100.0)

    def test_respects_the_poll_interval_per_asset(self):
        session = FakeSession({"BTC-USD": 100.0})
        feed = CoinbaseSpotFeed(assets=["Bitcoin"], session=session, poll_interval=15.0)
        asyncio.run(feed.poll_tick(now=1000.0))
        asyncio.run(feed.poll_tick(now=1005.0))  # too soon -- must not poll again
        assert len(session.calls) == 1
        asyncio.run(feed.poll_tick(now=1016.0))  # past the interval -- polls again
        assert len(session.calls) == 2

    def test_ignores_assets_not_in_the_product_map(self):
        feed = CoinbaseSpotFeed(assets=["Bitcoin", "Dogecoin"])
        assert feed.assets == ["Bitcoin"]

    def test_a_failed_fetch_for_one_asset_does_not_raise_or_affect_others(self):
        class FlakySession(FakeSession):
            def get(self, url, timeout=None, headers=None):
                if "BTC-USD" in url:
                    raise ConnectionError("boom")
                return super().get(url, timeout=timeout, headers=headers)

        session = FlakySession({"ETH-USD": 50.0})
        feed = CoinbaseSpotFeed(assets=["Bitcoin", "Ethereum"], session=session, poll_interval=15.0)
        asyncio.run(feed.poll_tick(now=1000.0))  # must not raise
        assert len(feed._history["Bitcoin"]) == 0
        assert feed._history["Ethereum"][-1] == (1000.0, 50.0)

    def test_prunes_history_older_than_the_retention_window(self):
        feed = CoinbaseSpotFeed(assets=["Bitcoin"])
        feed._history["Bitcoin"].append((0.0, 100.0))
        session = FakeSession({"BTC-USD": 200.0})
        feed.session = session
        feed._last_poll_at["Bitcoin"] = 0.0
        # Now is far beyond the retention window -- the stale 0.0 sample
        # should be pruned, leaving only the fresh one.
        asyncio.run(feed.poll_tick(now=100_000.0))
        assert list(feed._history["Bitcoin"]) == [(100_000.0, 200.0)]
