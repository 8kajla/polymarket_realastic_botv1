"""
Wiring tests for CoinbaseMomentumBot -- confirms the momentum decision
path is actually reachable end-to-end (feed -> coinbase_strategy ->
placement) and that this bot's deliberately-simpler contract (one-shot,
no hedging, drawdown breaker still inherited) holds, without needing a
real network. Mirrors test_bot.py's own no-network wiring-test style.
"""
from paperbot import config
from paperbot.book import BookState
from paperbot.coinbase_bot import CoinbaseMomentumBot
from paperbot.market_discovery import Market
from paperbot.strategy import MarketActivityState


def make_market_for_asset(asset, condition_id="cond-1", end_time=2000.0):
    return Market(
        condition_id=condition_id, slug=f"x-updown-5m-{condition_id}", question="q",
        asset=asset, end_time=end_time,
        token_id_up=f"{condition_id}-up", token_id_down=f"{condition_id}-down",
        order_min_size=None, order_min_tick_size=0.01,
    )


def wire_market(bot, market, up_price=0.20, down_price=0.80, depth=200.0):
    bot.markets_by_condition[market.condition_id] = market
    bot.activity[market.condition_id] = MarketActivityState()
    up = BookState(token_id=market.token_id_up)
    up.apply_snapshot(bids=[(up_price, depth)], asks=[(round(up_price + 0.01, 6), depth)])
    bot.book_states[market.token_id_up] = up
    down = BookState(token_id=market.token_id_down)
    down.apply_snapshot(bids=[(down_price, depth)], asks=[(round(down_price + 0.01, 6), depth)])
    bot.book_states[market.token_id_down] = down
    return up


def seed_momentum(bot, asset, ret, now):
    """Directly seeds the feed's history with two samples `minutes` apart
    producing exactly `ret` -- avoids any real network call."""
    minutes = config.MOMENTUM_LOOKBACK_MINUTES
    past_ts = now - minutes * 60.0
    bot.feed._history[asset].append((past_ts, 100.0))
    bot.feed._history[asset].append((now, 100.0 * (1.0 + ret)))


class TestCoinbaseMomentumBotPlacement:
    def test_places_an_order_when_momentum_is_actionable_in_cheap(self):
        bot = CoinbaseMomentumBot(assets=["Bitcoin"], seed=1)
        market = make_market_for_asset("Bitcoin", end_time=2000.0)
        wire_market(bot, market, up_price=0.20, down_price=0.80)  # CHEAP from Up's side
        seed_momentum(bot, "Bitcoin", config.MOMENTUM_MIN_MOVE * 5, now=1000.0)

        bot._evaluate_one_market(market, now=1000.0)

        assert market.condition_id in bot.resting_order_ids
        order_id = next(iter(bot.resting_order_ids[market.condition_id]))
        order = bot.fill_sim.orders[order_id]
        assert order.side == "Up"
        assert order.regime == "CHEAP"

    def test_negative_momentum_buys_down(self):
        bot = CoinbaseMomentumBot(assets=["Bitcoin"], seed=1)
        market = make_market_for_asset("Bitcoin", end_time=2000.0)
        wire_market(bot, market, up_price=0.55, down_price=0.45)  # both MID
        seed_momentum(bot, "Bitcoin", -config.MOMENTUM_MIN_MOVE * 5, now=1000.0)

        bot._evaluate_one_market(market, now=1000.0)

        assert market.condition_id in bot.resting_order_ids
        order_id = next(iter(bot.resting_order_ids[market.condition_id]))
        order = bot.fill_sim.orders[order_id]
        assert order.side == "Down"

    def test_no_placement_without_any_momentum_data(self):
        bot = CoinbaseMomentumBot(assets=["Bitcoin"], seed=1)
        market = make_market_for_asset("Bitcoin", end_time=2000.0)
        wire_market(bot, market)
        # feed has no history at all -- trailing_return is None

        bot._evaluate_one_market(market, now=1000.0)

        assert market.condition_id not in bot.resting_order_ids

    def test_no_placement_when_regime_is_out_of_scope(self):
        bot = CoinbaseMomentumBot(assets=["Bitcoin"], seed=1)
        market = make_market_for_asset("Bitcoin", end_time=2000.0)
        wire_market(bot, market, up_price=0.95, down_price=0.05)  # HIGH
        seed_momentum(bot, "Bitcoin", config.MOMENTUM_MIN_MOVE * 5, now=1000.0)

        bot._evaluate_one_market(market, now=1000.0)

        assert market.condition_id not in bot.resting_order_ids

    def test_one_shot_no_second_entry_in_the_same_market(self):
        bot = CoinbaseMomentumBot(assets=["Bitcoin"], seed=1)
        market = make_market_for_asset("Bitcoin", end_time=2000.0)
        wire_market(bot, market, up_price=0.20, down_price=0.80)
        seed_momentum(bot, "Bitcoin", config.MOMENTUM_MIN_MOVE * 5, now=1000.0)

        bot._evaluate_one_market(market, now=1000.0)
        assert len(bot.resting_order_ids[market.condition_id]) == 1

        # Free the market for a hypothetical 2nd entry and re-run -- unlike
        # PaperBot's trader-replica path, this bot must never place a
        # second order in the same market (no hedging, no re-entry).
        del bot.resting_order_ids[market.condition_id]
        bot._evaluate_one_market(market, now=1001.0)
        assert market.condition_id not in bot.resting_order_ids

    def test_drawdown_circuit_breaker_still_applies(self, monkeypatch):
        bot = CoinbaseMomentumBot(assets=["Bitcoin"], seed=1)
        market = make_market_for_asset("Bitcoin", end_time=2000.0)
        wire_market(bot, market, up_price=0.20, down_price=0.80)
        seed_momentum(bot, "Bitcoin", config.MOMENTUM_MIN_MOVE * 5, now=1000.0)
        monkeypatch.setattr(bot, "_new_markets_paused", lambda: True)

        bot._evaluate_one_market(market, now=1000.0)

        assert market.condition_id not in bot.resting_order_ids

    def test_feed_only_polls_configured_assets(self):
        bot = CoinbaseMomentumBot(assets=["Bitcoin", "Ethereum", "Solana"], seed=1)
        assert sorted(bot.feed.assets) == ["Bitcoin", "Ethereum", "Solana"]
