from __future__ import annotations

import json
import urllib.parse
import urllib.request

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
        self.send(message)

    def setup_commands(self) -> None:
        if not (self.settings.telegram_controls_enabled and self.settings.telegram_bot_token):
            return
        commands = [
            {"command": "status", "description": "查看资金状态"},
            {"command": "stop", "description": "停止所有交易"},
            {"command": "start", "description": "恢复交易"},
            {"command": "reset", "description": "重置资金统计"},
        ]
        token = self.settings.telegram_bot_token
        url = f"https://api.telegram.org/bot{token}/setMyCommands"
        body = urllib.parse.urlencode({"commands": json.dumps(commands, ensure_ascii=False)}).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            json.loads(response.read().decode("utf-8"))

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
            message = update.get("message") or {}
            text = str(message.get("text") or "").strip().split(maxsplit=1)[0].lower()
            chat_id = str((message.get("chat") or {}).get("id") or "")
            if chat_id and chat_id != str(self.settings.telegram_chat_id):
                continue
            if text == "/stop":
                storage.set_state("bot_paused", "1")
                actions.append("stopped")
            elif text == "/start":
                storage.set_state("bot_paused", "0")
                actions.append("started")
            elif text in {"/reset", "/restart"}:
                storage.set_state("money_baseline_equity", "")
                actions.append("reset")
            elif text == "/status":
                actions.append("status")
        return actions

    def _send_telegram(self, message: str) -> None:
        token = self.settings.telegram_bot_token
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = urllib.parse.urlencode(
            {"chat_id": self.settings.telegram_chat_id, "text": message}
        ).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            json.loads(response.read().decode("utf-8"))
