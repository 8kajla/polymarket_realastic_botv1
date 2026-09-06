#!/usr/bin/env python3
"""
Entry point for the paper-trading bot.

    python3 run_bot.py                          # all six assets, paper mode
    python3 run_bot.py --assets Bitcoin BNB      # restrict to a subset
    QUEUE_SAFETY_FACTOR=0.3 python3 run_bot.py   # tune the fill model

This is PAPER TRADING ONLY -- see paperbot/config.py for the safety gate.
"""
from paperbot.bot import main

if __name__ == "__main__":
    main()
