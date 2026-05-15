import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from okx_quant_bot.config import Settings


class ConfigTests(unittest.TestCase):
    def test_openai_key_can_expand_mimo_key(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            Path(tmp, ".env").write_text(
                "\n".join(
                    [
                        "MIMO_API_KEY=mimo-secret",
                        "OPENAI_API_KEY=${MIMO_API_KEY}",
                        "AI_REVIEW_ENABLED=true",
                    ]
                ),
                encoding="utf-8",
            )
            os.chdir(tmp)
            try:
                settings = Settings.from_env()
            finally:
                os.chdir(old_cwd)

        self.assertEqual(settings.openai_api_key, "mimo-secret")
        self.assertEqual(settings.ai_config_warning(), "")

    def test_margin_and_swap_require_explicit_switches(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            Path(tmp, ".env").write_text(
                "\n".join(
                    [
                        "ENABLED_MARKET_TYPES=SPOT,MARGIN,SWAP",
                        "ALLOW_LEVERAGED_TRADING=true",
                        "ALLOW_DERIVATIVES_TRADING=true",
                        "DERIVATIVES_DEMO_FIRST=false",
                        "MAX_LEVERAGE=5",
                        "MOMENTUM_ENTRY_MODE=rules_first",
                    ]
                ),
                encoding="utf-8",
            )
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                settings = Settings.from_env()
            finally:
                os.chdir(old_cwd)

        settings.require_safe_trading_config()
        self.assertEqual(settings.enabled_market_types, ("SPOT", "MARGIN", "SWAP"))
        self.assertEqual(settings.max_leverage, 5)
        self.assertEqual(settings.momentum_entry_mode, "rules_first")

    def test_live_trading_requires_spot_only_isolated_db_and_small_book(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            Path(tmp, ".env").write_text(
                "\n".join(
                    [
                        "TRADING_ENABLED=true",
                        "OKX_DEMO=false",
                        "ALLOW_LIVE_TRADING=true",
                        "OKX_SIMULATED_TRADING_HEADER=false",
                        "OKX_API_KEY=k",
                        "OKX_SECRET_KEY=s",
                        "OKX_PASSPHRASE=p",
                        "ENABLED_MARKET_TYPES=SPOT",
                        "MAX_OPEN_POSITIONS=2",
                        "DB_PATH=data/live.sqlite3",
                    ]
                ),
                encoding="utf-8",
            )
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                settings = Settings.from_env()
            finally:
                os.chdir(old_cwd)

        settings.require_safe_trading_config()

    def test_live_trading_blocks_swap_even_with_derivatives_switch(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            Path(tmp, ".env").write_text(
                "\n".join(
                    [
                        "TRADING_ENABLED=true",
                        "OKX_DEMO=false",
                        "ALLOW_LIVE_TRADING=true",
                        "OKX_SIMULATED_TRADING_HEADER=false",
                        "OKX_API_KEY=k",
                        "OKX_SECRET_KEY=s",
                        "OKX_PASSPHRASE=p",
                        "ENABLED_MARKET_TYPES=SPOT,SWAP",
                        "ALLOW_DERIVATIVES_TRADING=true",
                        "DERIVATIVES_DEMO_FIRST=false",
                        "MAX_OPEN_POSITIONS=1",
                        "DB_PATH=data/live.sqlite3",
                    ]
                ),
                encoding="utf-8",
            )
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                settings = Settings.from_env()
            finally:
                os.chdir(old_cwd)

        with self.assertRaisesRegex(ValueError, "SPOT"):
            settings.require_safe_trading_config()


if __name__ == "__main__":
    unittest.main()
