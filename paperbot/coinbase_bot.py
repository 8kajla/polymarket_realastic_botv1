"""
Standalone Coinbase-momentum bot.

CHANGED 2026-09-13 (was: a wholly separate, one-shot, flat-sized, no-
hedge strategy). Explicit instruction: inherit everything else this
project has learned about the real trader and already built into
PaperBot -- hedging (all of it: liquidity/weekend/adverse-move/cross-
market-rate/conviction/after-big-loss-conditioned triggers and sizing),
the full calibrated sizing multiplier stack (TTC, within-band, cross-
market momentum, bankroll-linked, resumption-caution, reentry-fatigue,
hedge-count-reinforcement), cross-market side persistence, the
resolution-feedback loop, and the $100-bankroll-style drawdown safety
controls. This bot now IS paperbot in every respect except one: a
market's FIRST entry in CHEAP/MID is chosen by live Coinbase spot
momentum instead of decide_side's persistence logic -- see PaperBot's
_forced_first_entry_side docstring in bot.py and coinbase_strategy.
momentum_forced_side's docstring for exactly how and when that override
fires (falling back to the ordinary trader-replica decide_side outside
CHEAP/MID, or whenever there's no actionable momentum reading yet).

    python3 run_coinbase_bot.py                          # BTC/ETH/SOL, paper mode
    python3 run_coinbase_bot.py --assets Bitcoin Ethereum
    BANKROLL_USD=100 PAPERBOT_DATA_DIR=/opt/coinbase-bot/data python3 run_coinbase_bot.py

Subclasses PaperBot and overrides only two things: strategy_tick (polls
the Coinbase feed before every ordinary tick) and _forced_first_entry_side
(the one hook this whole design needed -- see bot.py). Everything else
-- market discovery, book/WS handling, fill simulation, ledger,
resolution tracking, EVERY cross-market tracker, and _evaluate_one_market
itself -- is the exact same inherited PaperBot code paperbot/paperbot-100
run.

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
from .book import BookState
from .coinbase_feed import CoinbaseSpotFeed
from .coinbase_strategy import momentum_forced_side
from .market_discovery import Market

logger = logging.getLogger("paperbot.coinbase_bot")


class CoinbaseMomentumBot(PaperBot):
    def __init__(self, assets: Optional[list[str]] = None,
                 queue_safety_factor: float = config.QUEUE_SAFETY_FACTOR,
                 seed: Optional[int] = None):
        super().__init__(assets=assets, queue_safety_factor=queue_safety_factor, seed=seed)
        self.feed = CoinbaseSpotFeed(assets=self.assets, session=self.session,
                                      poll_interval=config.COINBASE_POLL_INTERVAL_SECONDS)

    async def strategy_tick(self, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        await self.feed.poll_tick(now)
        await super().strategy_tick(now)

    def _forced_first_entry_side(self, market: Market, up_book: BookState, down_book: BookState,
                                  now: float) -> Optional[str]:
        trailing_return = self.feed.trailing_return(
            market.asset, now=now, minutes=config.MOMENTUM_LOOKBACK_MINUTES)
        return momentum_forced_side(up_book, down_book, trailing_return)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", nargs="*", default=["Bitcoin", "Ethereum", "Solana"],
                         help="restrict to a subset (default: the 3 assets this edge was validated on)")
    parser.add_argument("--queue-safety-factor", type=float, default=config.QUEUE_SAFETY_FACTOR)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, stream=sys.stdout,
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    bot = CoinbaseMomentumBot(assets=args.assets, queue_safety_factor=args.queue_safety_factor,
                               seed=args.seed)
    logger.info("Starting coinbase-momentum bot in PAPER mode (assets=%s, queue_safety_factor=%.3f)",
                bot.assets, args.queue_safety_factor)
    try:
        asyncio.run(bot.run_forever())
    except KeyboardInterrupt:
        # Fallback path only -- run_forever installs its own SIGINT/SIGTERM
        # handlers and normally returns cleanly (ledger already saved)
        # before this would ever fire.
        logger.info("Shutting down. Realized PnL: %.4f", bot.ledger.realized_pnl())


if __name__ == "__main__":
    main()
