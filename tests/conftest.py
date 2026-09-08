"""
Shared test fixtures.

Autouse: redirects config.DATA_DIR/LEDGER_PATH to a per-test tmp directory.
Without this, any test that exercises the real settlement path (bot.
resolution_tick's successful-resolution branch calls self.ledger.save())
writes to the actual project-local data/paper_ledger.json on whatever
machine runs the suite -- confirmed live while building TestBankrollGate
(tests/test_bot.py): a stray settlement record left over from an earlier
local run (order_id=1, condition_id="cond-0", pnl=3.5) sat on disk and
silently polluted every fresh Ledger.load() in every subsequent test run
system-wide. It went unnoticed for a long time by coincidence only -- every
pre-existing test either checked a PNL delta or happened to reuse the exact
same fixture order_id/price/size, so the polluted state matched what a
fresh run would have produced anyway. TestBankrollGate's whole point is
checking an ABSOLUTE cash balance, not a delta, which is what finally
exposed the leak. Isolating every test's data dir here closes it for good,
for any test in this suite, not just the ones that happened to notice.
"""
import pytest

from paperbot import config


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LEDGER_PATH", tmp_path / "paper_ledger.json")
