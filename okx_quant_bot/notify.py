from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from okx_quant_bot.config import Settings


class Notifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, message: str) -> None:
        if self.settings.telegram_bot_token and self.settings.telegram_chat_id:
            self._send_telegram(message)
        else:
            print(message)

    def send_money(self, message: str) -> None:
        if self.settings.telegram_bot_token and self.settings.telegram_chat_id:
            self._send_telegram(message, reply_markup=_control_buttons())
        else:
            print(message)

    def poll_controls(self, storage) -> list[str]:
        if not (
            self.settings.telegram_controls_enabled
            and self.settings.telegram_bot_token
            and self.settings.telegram_chat_id
        ):
            return []
        offset = int(storage.get_state("telegram_update_offset", "0") or "0")
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/getUpdates"
        query = urllib.parse.urlencode({"timeout": 0, "offset": offset})
        request = urllib.request.Request(f"{url}?{query}", method="GET")
        actions: list[str] = []
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        for update in payload.get("result", []):
            update_id = int(update.get("update_id", 0))
            storage.set_state("telegram_update_offset", str(update_id + 1))
            callback = update.get("callback_query") or {}
            data = callback.get("data")
            callback_id = callback.get("id")
            if data == "hermes_stop":
                storage.set_state("bot_paused", "1")
                actions.append("stopped")
                self._answer_callback(callback_id, "已停止开新仓")
            elif data == "hermes_restart":
                storage.set_state("bot_paused", "0")
                storage.set_state("money_baseline_equity", "")
                actions.append("restarted")
                self._answer_callback(callback_id, "已重新开始")
        return actions

    def _send_telegram(self, message: str, reply_markup: dict[str, Any] | None = None) -> None:
        token = self.settings.telegram_bot_token
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload: dict[str, str] = {"chat_id": self.settings.telegram_chat_id, "text": message}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        body = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            json.loads(response.read().decode("utf-8"))

    def _answer_callback(self, callback_id: str | None, text: str) -> None:
        if not callback_id:
            return
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/answerCallbackQuery"
        body = urllib.parse.urlencode({"callback_query_id": callback_id, "text": text}).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            json.loads(response.read().decode("utf-8"))


def _control_buttons() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "停止", "callback_data": "hermes_stop"},
                {"text": "重新开始", "callback_data": "hermes_restart"},
            ]
        ]
    }
