import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

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
                    {"update_id": 12, "message": {"text": "/reset", "chat": {"id": 123}}},
                    {"update_id": 13, "message": {"text": "/status", "chat": {"id": 123}}},
                    {"update_id": 14, "message": {"text": "/ai", "chat": {"id": 123}}},
                    {"update_id": 15, "message": {"text": "/positions", "chat": {"id": 123}}},
                    {"update_id": 16, "message": {"text": "/training", "chat": {"id": 123}}},
                    {"update_id": 17, "message": {"text": "/health", "chat": {"id": 123}}},
                    {"update_id": 18, "message": {"text": "/errors", "chat": {"id": 123}}},
                    {"update_id": 19, "message": {"text": "/shadow", "chat": {"id": 123}}},
                    {"update_id": 20, "message": {"text": "/execution", "chat": {"id": 123}}},
                    {"update_id": 21, "message": {"text": "/lessons", "chat": {"id": 123}}},
                    {"update_id": 22, "message": {"text": "/market", "chat": {"id": 123}}},
                ]
            }
            with patch("urllib.request.urlopen", return_value=_Response(payload)):
                actions = Notifier(settings).poll_controls(storage)

            self.assertEqual(
                actions,
                [
                    "stopped",
                    "started",
                    "reset",
                    "status",
                    "ai",
                    "positions",
                    "training",
                    "health",
                    "errors",
                    "shadow",
                    "execution",
                    "lessons",
                    "market",
                ],
            )
            self.assertEqual(storage.get_state("bot_paused"), "0")
            self.assertEqual(storage.get_state("telegram_update_offset"), "23")
            self.assertEqual(storage.get_state("telegram_poll_status"), "ok actions=13")
            self.assertEqual(storage.get_state("telegram_last_update_chat_id"), "123")

    def test_poll_controls_still_works_when_menu_controls_disabled(self):
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
                    "telegram_controls_enabled": False,
                }
            )
            payload = {"result": [{"update_id": 10, "message": {"text": "/status", "chat": {"id": 123}}}]}
            with patch("urllib.request.urlopen", return_value=_Response(payload)):
                actions = Notifier(settings).poll_controls(storage)

            self.assertEqual(actions, ["status"])
            self.assertEqual(storage.get_state("telegram_poll_status"), "ok actions=1")

    def test_setup_commands_registers_function_panel(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = request.data.decode("utf-8")
            return _Response({"ok": True})

        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "telegram_bot_token": "token",
                    "telegram_chat_id": "123",
                    "telegram_controls_enabled": True,
                }
            )
            with patch("urllib.request.urlopen", fake_urlopen):
                Notifier(settings).setup_commands()

        self.assertIn("/bot", captured["url"])
        for command in (
            "status",
            "ai",
            "positions",
            "training",
            "health",
            "errors",
            "shadow",
            "execution",
            "lessons",
            "market",
            "stop",
            "start",
            "reset",
        ):
            self.assertIn(command, captured["body"])

    def test_send_failure_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "telegram_bot_token": "token",
                    "telegram_chat_id": "123",
                }
            )
            notifier = Notifier(settings)

            with patch("urllib.request.urlopen", side_effect=URLError("timeout")):
                ok = notifier.send("hello")

        self.assertFalse(ok)
        self.assertIn("timeout", notifier.last_error)


if __name__ == "__main__":
    unittest.main()
