import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from okx_quant_bot.data import Storage
from okx_quant_bot.notify import Notifier
from tests.test_strategy_risk import settings_for


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class NotifyControlsTests(unittest.TestCase):
    def test_poll_controls_uses_text_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = settings_for(db)
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "telegram_bot_token": "token",
                    "telegram_chat_id": "123",
                    "telegram_controls_enabled": True,
                }
            )
            payload = {
                "result": [
                    {"update_id": 10, "message": {"text": "/stop", "chat": {"id": 123}}},
                    {"update_id": 11, "message": {"text": "/start", "chat": {"id": 123}}},
                    {"update_id": 12, "message": {"text": "/status", "chat": {"id": 123}}},
                ]
            }
            with patch("urllib.request.urlopen", return_value=_Response(payload)):
                actions = Notifier(settings).poll_controls(storage)

            self.assertEqual(actions, ["stopped", "restarted", "status"])
            self.assertEqual(storage.get_state("bot_paused"), "0")
            self.assertEqual(storage.get_state("telegram_update_offset"), "13")


if __name__ == "__main__":
    unittest.main()
