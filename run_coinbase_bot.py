#!/usr/bin/env python3
"""
Entry point for the standalone Coinbase-momentum paper-trading bot.

    python3 run_coinbase_bot.py                          # BTC/ETH/SOL, paper mode
    python3 run_coinbase_bot.py --assets Bitcoin Ethereum
    PAPERBOT_DATA_DIR=/opt/coinbase-bot/data python3 run_coinbase_bot.py

This is a deliberately separate experiment from run_bot.py's trader-
replica bot -- see paperbot/config.py's Coinbase-momentum section and
paperbot/coinbase_strategy.py's module docstring for why.

This is PAPER TRADING ONLY -- see paperbot/config.py for the safety gate.
"""
from paperbot.coinbase_bot import main

if __name__ == "__main__":
    main()
