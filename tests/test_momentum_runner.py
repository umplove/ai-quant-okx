import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from okx_quant_bot.ai_reviewer import AiReview
from okx_quant_bot.data import Storage
from okx_quant_bot.models import CandidateScore
from okx_quant_bot.momentum import MomentumScan
from okx_quant_bot.momentum_runner import MomentumBotRunner
from tests.test_strategy_risk import settings_for


class _Exchange:
    pass


class _Notifier:
    def send(self, message):
        pass

    def send_money(self, message):
        pass

    def poll_controls(self, storage):
        return []


class MomentumRunnerTests(unittest.TestCase):
    def test_tradable_candidates_fill_available_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = settings_for(db)
            settings = settings.__class__(**{**settings.__dict__, "max_open_positions": 3})
            runner = MomentumBotRunner(settings, storage, _Exchange(), _Notifier())
            scan = MomentumScan(
                tickers=[],
                info_signals=[],
                candidates=[
                    _candidate("AAA-USDT"),
                    _candidate("BBB-USDT"),
                    _candidate("CCC-USDT", confirmed=False),
                    _candidate("DDD-USDT"),
                ],
            )

            candidates = runner._tradable_candidates(scan)

        self.assertEqual([c.symbol for c in candidates], ["AAA-USDT", "BBB-USDT", "DDD-USDT"])

    def test_ai_timeout_is_silent_in_money_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = settings_for(db)
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "ai_review_enabled": True,
                    "ai_review_interval_scans": 1,
                    "telegram_money_only": True,
                }
            )
            runner = MomentumBotRunner(settings, storage, _Exchange(), _Notifier())
            scan = MomentumScan(tickers=[], info_signals=[], candidates=[])

            with patch("okx_quant_bot.momentum_runner.AiReviewClient") as client:
                client.return_value.review_scan.return_value = AiReview(False, "", "timeout")
                text = runner._ai_review_text(scan)

        self.assertEqual(text, "")


def _candidate(symbol: str, confirmed: bool = True) -> CandidateScore:
    return CandidateScore(
        symbol=symbol,
        price=1,
        change_pct_24h=0.1,
        amplitude_pct_24h=0.2,
        volume_quote_24h=1000,
        news_score=0,
        polymarket_score=0,
        total_score=10,
        reason="test",
        confirmed=confirmed,
    )


if __name__ == "__main__":
    unittest.main()
