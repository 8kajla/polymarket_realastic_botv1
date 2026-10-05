#!/usr/bin/env python3
"""
Entry point for the consolidated-engine paper-trading bot, 2026-09-24.

    python3 run_consolidated_bot.py                          # BTC/ETH/SOL, paper mode
    python3 run_consolidated_bot.py --assets Bitcoin Ethereum
    PAPERBOT_DATA_DIR=/opt/coinbase-bot/data python3 run_consolidated_bot.py

REPLACES the standalone Coinbase-momentum bot (run_coinbase_bot.py /
CoinbaseMomentumBot) in the coinbase-bot deployment slot. That bot's own
differentiator -- a live Coinbase-spot-momentum override for CHEAP/MID
first entries -- was a separate live-momentum EXPERIMENT, not a
verified trader-replication finding, so it is deliberately not carried
over here. This is plain PaperBot (same market discovery, book/WS
handling, fill simulation, ledger, resolution tracking, every
cross-market tracker) with ONE thing swapped: which function decides
each order, via paperbot.consolidated_engine.build_order_intent instead
of paperbot.strategy.build_order_intent -- see that module's docstring
for exactly what it folds together and why.

This is PAPER TRADING ONLY -- see paperbot/config.py for the safety gate.
"""
import argparse
import asyncio
import logging
import sys

from paperbot import config
from paperbot.bot import PaperBot
from paperbot.consolidated_engine import build_order_intent as consolidated_build_order_intent

logger = logging.getLogger("paperbot.run_consolidated_bot")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", nargs="*", default=["Bitcoin", "Ethereum", "Solana"],
                         help="restrict to a subset (default: the 3 assets every consolidated "
                              "mechanism is actually calibrated on)")
    parser.add_argument("--queue-safety-factor", type=float, default=config.QUEUE_SAFETY_FACTOR)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, stream=sys.stdout,
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    bot = PaperBot(assets=args.assets, queue_safety_factor=args.queue_safety_factor,
                    seed=args.seed, build_order_intent_fn=consolidated_build_order_intent)
    logger.info("Starting consolidated-engine bot in PAPER mode (assets=%s, queue_safety_factor=%.3f)",
                bot.assets, args.queue_safety_factor)
    try:
        asyncio.run(bot.run_forever())
    except KeyboardInterrupt:
        logger.info("Shutting down. Realized PnL: %.4f", bot.ledger.realized_pnl())


if __name__ == "__main__":
    main()
