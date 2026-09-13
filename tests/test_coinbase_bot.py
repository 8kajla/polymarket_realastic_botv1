"""
Wiring tests for CoinbaseMomentumBot -- confirms the momentum override
actually reaches strategy.build_order_intent's forced_first_entry_side,
and that this bot otherwise behaves exactly like PaperBot (hedging,
sizing, cross-market trackers all inherited unchanged). Mirrors
test_bot.py's own no-network wiring-test style.
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


class TestForcedFirstEntrySideHook:
    def test_returns_none_without_any_momentum_data(self):
        bot = CoinbaseMomentumBot(assets=["Bitcoin"], seed=1)
        market = make_market_for_asset("Bitcoin")
        up_book = wire_market(bot, market)
        down_book = bot.book_states[market.token_id_down]
        assert bot._forced_first_entry_side(market, up_book, down_book, now=1000.0) is None

    def test_returns_the_momentum_side_when_actionable(self):
        bot = CoinbaseMomentumBot(assets=["Bitcoin"], seed=1)
        market = make_market_for_asset("Bitcoin")
        up_book = wire_market(bot, market, up_price=0.20, down_price=0.80)  # CHEAP
        down_book = bot.book_states[market.token_id_down]
        seed_momentum(bot, "Bitcoin", config.MOMENTUM_MIN_MOVE * 5, now=1000.0)
        assert bot._forced_first_entry_side(market, up_book, down_book, now=1000.0) == "Up"

    def test_returns_none_outside_cheap_mid(self):
        bot = CoinbaseMomentumBot(assets=["Bitcoin"], seed=1)
        market = make_market_for_asset("Bitcoin")
        up_book = wire_market(bot, market, up_price=0.95, down_price=0.05)  # HIGH
        down_book = bot.book_states[market.token_id_down]
        seed_momentum(bot, "Bitcoin", config.MOMENTUM_MIN_MOVE * 5, now=1000.0)
        assert bot._forced_first_entry_side(market, up_book, down_book, now=1000.0) is None


class TestCoinbaseMomentumBotFullPipeline:
    """Confirms the momentum override reaches all the way through
    build_order_intent to a real placement, AND that everything else
    (hedging, sizing) is the unmodified PaperBot pipeline -- unlike the
    bot's first iteration, this is no longer a simplified/one-shot
    strategy."""

    def test_places_a_first_entry_on_the_momentum_side(self):
        bot = CoinbaseMomentumBot(assets=["Bitcoin"], seed=1)
        market = make_market_for_asset("Bitcoin", end_time=2000.0)
        wire_market(bot, market, up_price=0.20, down_price=0.80)  # CHEAP
        seed_momentum(bot, "Bitcoin", config.MOMENTUM_MIN_MOVE * 5, now=1000.0)

        bot._evaluate_one_market(market, now=1000.0)

        assert market.condition_id in bot.resting_order_ids
        order_id = next(iter(bot.resting_order_ids[market.condition_id]))
        order = bot.fill_sim.orders[order_id]
        assert order.side == "Up"
        assert order.regime == "CHEAP"

    def test_falls_back_to_ordinary_decide_side_outside_cheap_mid(self, monkeypatch):
        # HIGH -- momentum_forced_side is out of scope, so this must fall
        # all the way through to strategy.decide_side, same as paperbot.
        bot = CoinbaseMomentumBot(assets=["Bitcoin"], seed=1)
        market = make_market_for_asset("Bitcoin", end_time=2000.0)
        wire_market(bot, market, up_price=0.95, down_price=0.05)  # HIGH
        seed_momentum(bot, "Bitcoin", config.MOMENTUM_MIN_MOVE * 5, now=1000.0)

        import paperbot.coinbase_bot as coinbase_bot_mod
        captured = {}
        real = coinbase_bot_mod.momentum_forced_side

        def spy(*args, **kwargs):
            result = real(*args, **kwargs)
            captured["called"] = True
            captured["result"] = result
            return result

        monkeypatch.setattr(coinbase_bot_mod, "momentum_forced_side", spy)

        bot._evaluate_one_market(market, now=1000.0)

        assert captured["called"] is True
        assert captured["result"] is None
        # Still places something (decide_side always returns a side on a
        # first entry) -- just not forced by momentum.
        assert market.condition_id in bot.resting_order_ids

    def test_hedging_is_fully_inherited_and_reachable(self, monkeypatch):
        """Unlike the bot's original one-shot design, a hedge must be
        reachable here -- confirms decide_hedge/HEDGE_SIZE_RATIO etc. are
        wired in exactly as they are for paperbot, not bypassed."""
        import paperbot.strategy as stratmod

        bot = CoinbaseMomentumBot(assets=["Bitcoin"], seed=1)
        market = make_market_for_asset("Bitcoin", end_time=2000.0)
        up_book = wire_market(bot, market, up_price=0.80, down_price=0.15)
        activity = bot.activity[market.condition_id]
        activity.record_entry("Up", notional_usd=10.0, regime="CORE", price=0.80)
        activity.record_real_fill(is_hedge=False)

        monkeypatch.setattr(
            stratmod, "decide_hedge",
            lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None,
            prev_hedge_rate=None, after_big_loss=None: "Down")

        bot._evaluate_one_market(market, now=1000.0)

        assert market.condition_id in bot.resting_order_ids
        order_id = next(iter(bot.resting_order_ids[market.condition_id]))
        order = bot.fill_sim.orders[order_id]
        assert order.side == "Down"

    def test_second_entry_is_not_forced_by_momentum(self):
        """momentum_forced_side must only ever apply to a market's FIRST
        entry -- a second, ordinary (non-hedge) entry uses decide_side's
        normal held-side persistence, exactly like paperbot."""
        bot = CoinbaseMomentumBot(assets=["Bitcoin"], seed=1)
        market = make_market_for_asset("Bitcoin", end_time=2000.0)
        wire_market(bot, market, up_price=0.20, down_price=0.80)
        activity = bot.activity[market.condition_id]
        activity.record_entry("Down", notional_usd=5.0, regime="CHEAP", price=0.80)
        activity.record_real_fill(is_hedge=False)
        # Momentum says "Up" -- but this is a 2nd entry (entry_count==1),
        # so the hook must never even be consulted for it.
        seed_momentum(bot, "Bitcoin", config.MOMENTUM_MIN_MOVE * 5, now=1000.0)

        called = []
        original = bot._forced_first_entry_side

        def spy(*args, **kwargs):
            called.append(True)
            return original(*args, **kwargs)

        bot._forced_first_entry_side = spy

        bot._evaluate_one_market(market, now=1000.0)

        assert called == []

    def test_feed_only_polls_configured_assets(self):
        bot = CoinbaseMomentumBot(assets=["Bitcoin", "Ethereum", "Solana"], seed=1)
        assert sorted(bot.feed.assets) == ["Bitcoin", "Ethereum", "Solana"]


class TestPaperBotDefaultHookIsANoop:
    def test_paperbot_itself_never_forces_a_side(self):
        from paperbot.bot import PaperBot
        bot = PaperBot(assets=["Bitcoin"], seed=1)
        market = make_market_for_asset("Bitcoin")
        up_book = wire_market(bot, market)
        down_book = bot.book_states[market.token_id_down]
        assert bot._forced_first_entry_side(market, up_book, down_book, now=1000.0) is None
