"""
Standalone Coinbase-momentum paper-trading bot -- a deliberately separate
experiment from PaperBot's trader-replica strategy. See config.py's
Coinbase-momentum section and coinbase_strategy.py's module docstring for
the full backstory on why this exists as its own thing.

    python3 run_coinbase_bot.py                          # BTC/ETH/SOL, paper mode
    python3 run_coinbase_bot.py --assets Bitcoin Ethereum
    BANKROLL_USD=100 PAPERBOT_DATA_DIR=/opt/coinbase-bot/data python3 run_coinbase_bot.py

Subclasses PaperBot to reuse its market discovery, live book/WS handling,
fill simulation, ledger, resolution tracking, and bankroll/drawdown
safety controls -- all generic infra, none of it trader-replica-specific.
Overrides only strategy_tick (to poll the Coinbase feed first) and
_evaluate_one_market (the momentum decision path, one-shot per market, no
hedging) -- every trader-replica cross-market tracker PaperBot otherwise
maintains (last_first_entry_side_by_asset, ewma_size_residual_by_asset,
after_big_loss_by_asset, etc.) is simply never read once
_evaluate_one_market is replaced, and costs nothing by sitting unused.

This is PAPER TRADING ONLY -- see paperbot/config.py's trading-mode gate,
inherited unchanged from PaperBot.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from typing import Optional

from . import config
from .bot import PaperBot
from .coinbase_feed import CoinbaseSpotFeed
from .coinbase_strategy import build_momentum_order_intent
from .market_discovery import Market
from .strategy import safe_postonly_price, would_cross_spread

logger = logging.getLogger("paperbot.coinbase_bot")


class CoinbaseMomentumBot(PaperBot):
    def __init__(self, assets: Optional[list[str]] = None, seed: Optional[int] = None):
        super().__init__(assets=assets, seed=seed)
        self.feed = CoinbaseSpotFeed(assets=self.assets, session=self.session,
                                      poll_interval=config.COINBASE_POLL_INTERVAL_SECONDS)

    async def strategy_tick(self, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        await self.feed.poll_tick(now)
        await super().strategy_tick(now)

    def _evaluate_one_market(self, market: Market, now: float,
                              at_open_order_cap: bool = False) -> None:
        up_book = self.book_states.get(market.token_id_up)
        down_book = self.book_states.get(market.token_id_down)
        if up_book is None or down_book is None or up_book.best_bid is None \
                or down_book.best_bid is None:
            return

        activity = self.activity[market.condition_id]
        if activity.entry_count > 0:
            # One-shot: no hedging, no re-entry -- a pure directional bet
            # on momentum at a market's first opportunity only. Unlike
            # PaperBot's trader-replica path, there is no decide_hedge
            # equivalent here to ever justify a second entry.
            return
        if at_open_order_cap:
            return
        # DRAWDOWN CIRCUIT BREAKER (inherited safety net, only meaningful
        # when this instance sets BANKROLL_USD and MAX_HOURLY_DRAWDOWN_PCT
        # -- see PaperBot._update_drawdown_tracking's docstring): every
        # entry here is a "new market" in PaperBot's sense (entry_count
        # was just confirmed 0 above), so this always applies, unlike the
        # trader-replica bot where it's conditional on is_first_entry.
        if self._new_markets_paused():
            logger.info("CIRCUIT_BREAKER_SKIP asset=%s: new-market entries paused after a drawdown",
                        market.asset)
            return

        trailing_return = self.feed.trailing_return(
            market.asset, now=now, minutes=config.MOMENTUM_LOOKBACK_MINUTES)
        intent = build_momentum_order_intent(market, up_book, down_book, trailing_return, now=now)
        if intent is None:
            return

        order_book = self.book_states.get(intent.token_id)
        if order_book is None:
            return

        price = intent.price
        if would_cross_spread(order_book, price):
            price = safe_postonly_price(order_book, price)
            if price is None:
                logger.info("SKIP market=%s: postOnly order would cross spread even after retry",
                            market.slug)
                return
            intent.price = price
            intent.size_shares = intent.notional_usd / price if price > 0 else 0.0

        cash = self.available_cash()
        if cash is not None and intent.notional_usd > cash:
            logger.info("SKIP market=%s: insufficient bankroll (need $%.4f, have $%.4f)",
                        market.slug, intent.notional_usd, cash)
            return

        order = self.fill_sim.place_order(intent, order_book, now=now)
        self.resting_order_ids.setdefault(market.condition_id, set()).add(order.order_id)
        activity.record_entry(intent.side, intent.notional_usd, intent.regime,
                               is_hedge=False, price=intent.price)
        self._record_global_trade(now)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", nargs="*", default=["Bitcoin", "Ethereum", "Solana"],
                         help="restrict to a subset (default: the 3 assets this edge was validated on)")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, stream=sys.stdout,
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    bot = CoinbaseMomentumBot(assets=args.assets, seed=args.seed)
    logger.info("Starting coinbase-momentum bot in PAPER mode (assets=%s)", bot.assets)
    try:
        asyncio.run(bot.run_forever())
    except KeyboardInterrupt:
        # Fallback path only -- run_forever installs its own SIGINT/SIGTERM
        # handlers and normally returns cleanly (ledger already saved)
        # before this would ever fire.
        logger.info("Shutting down. Realized PnL: %.4f", bot.ledger.realized_pnl())


if __name__ == "__main__":
    main()
