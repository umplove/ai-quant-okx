import tempfile
import unittest
from pathlib import Path

from okx_quant_bot.doctor import CheckStatus, Doctor, format_results
from tests.test_strategy_risk import settings_for


class DoctorTests(unittest.TestCase):
    def test_config_output_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "okx_api_key": "real-api-key",
                    "okx_secret_key": "real-secret",
                    "okx_passphrase": "real-passphrase",
                    "telegram_bot_token": "real-token",
                    "telegram_chat_id": "123456",
                }
            )
            output = format_results(Doctor(settings).run(include_network=False))
            self.assertNotIn("real-api-key", output)
            self.assertNotIn("real-secret", output)
            self.assertNotIn("real-passphrase", output)
            self.assertNotIn("real-token", output)
            self.assertIn("OKX credentials", output)

    def test_live_mode_is_warning_not_secret_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "okx_demo": False,
                    "allow_live_trading": True,
                    "trading_enabled": True,
                }
            )
            results = Doctor(settings).run(include_network=False)
            warnings = [result for result in results if result.status == CheckStatus.WARN]
            self.assertGreaterEqual(len(warnings), 3)
            self.assertFalse(Doctor.has_failures([result for result in results if result.name != "OKX credentials"]))

    def test_missing_telegram_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            result = Doctor(settings).check_telegram()
            self.assertEqual(result.status, CheckStatus.WARN)


if __name__ == "__main__":
    unittest.main()

