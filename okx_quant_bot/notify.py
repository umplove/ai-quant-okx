from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from okx_quant_bot.config import Settings


class Notifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_error = ""

    def send(self, message: str) -> bool:
        try:
            if self.settings.telegram_bot_token and self.settings.telegram_chat_id:
                self._send_telegram(message)
            else:
                print(message)
            self.last_error = ""
            return True
        except Exception as exc:
            self.last_error = str(exc)
            print(f"Telegram发送失败: {exc}")
            return False

    def send_money(self, message: str) -> bool:
        return self.send(message)

    def setup_commands(self) -> bool:
        if not (self.settings.telegram_controls_enabled and self.settings.telegram_bot_token):
            return False
        commands = [
            {"command": "status", "description": "查看资产、持仓和最近订单"},
            {"command": "ai", "description": "查看AI配置、调用和错误统计"},
            {"command": "positions", "description": "查看当前持仓和AI最近卖出意见"},
            {"command": "training", "description": "查看本周AI训练token进度"},
            {"command": "health", "description": "查看线程、DB、Telegram和配置健康状态"},
            {"command": "errors", "description": "查看最近异常、timeout和下单失败"},
            {"command": "shadow", "description": "查看影子全市场最近建议"},
            {"command": "execution", "description": "查看最近AI执行决策和订单方式"},
            {"command": "lessons", "description": "查看最近交易归因和经验"},
            {"command": "market", "description": "查看当前AI行情状态"},
            {"command": "stop", "description": "暂停交易主循环"},
            {"command": "start", "description": "恢复交易主循环"},
            {"command": "reset", "description": "重置资金统计"},
        ]
        try:
            token = self.settings.telegram_bot_token
            url = f"https://api.telegram.org/bot{token}/setMyCommands"
            body = urllib.parse.urlencode({"commands": json.dumps(commands, ensure_ascii=False)}).encode("utf-8")
            request = urllib.request.Request(url, data=body, method="POST")
            with urllib.request.urlopen(request, timeout=10) as response:
                json.loads(response.read().decode("utf-8"))
            self.last_error = ""
            return True
        except Exception as exc:
            self.last_error = str(exc)
            print(f"Telegram菜单设置失败: {exc}")
            return False

    def poll_controls(self, storage) -> list[str]:
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        try:
            storage.set_state("telegram_poll_started_at", now)
        except Exception:
            pass
        if not (self.settings.telegram_bot_token and self.settings.telegram_chat_id):
            try:
                storage.set_state("telegram_poll_status", "disabled_missing_token_or_chat_id")
            except Exception:
                pass
            return []
        try:
            actions = self._poll_controls(storage)
            storage.set_state("telegram_poll_status", f"ok actions={len(actions)}")
            storage.set_state("telegram_poll_finished_at", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
            return actions
        except Exception as exc:
            self.last_error = str(exc)
            print(f"Telegram poll failed: {exc}", flush=True)
            try:
                storage.set_state("telegram_poll_status", f"error: {str(exc)[:200]}")
                storage.set_state("telegram_poll_finished_at", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
                storage.save_bot_error("telegram_poll", "Telegram轮询失败", str(exc))
            except Exception:
                pass
            return []

    def _poll_controls(self, storage) -> list[str]:
        offset = int(storage.get_state("telegram_update_offset", "0") or "0")
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/getUpdates"
        query = urllib.parse.urlencode({"timeout": 0, "offset": offset})
        request = urllib.request.Request(f"{url}?{query}", method="GET")
        actions: list[str] = []
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        updates = payload.get("result", [])
        storage.set_state("telegram_last_update_count", str(len(updates)))
        for update in updates:
            update_id = int(update.get("update_id", 0))
            storage.set_state("telegram_update_offset", str(update_id + 1))
            message = update.get("message") or {}
            text = str(message.get("text") or "").strip().split(maxsplit=1)[0].lower()
            chat_id = str((message.get("chat") or {}).get("id") or "")
            storage.set_state("telegram_last_update_id", str(update_id))
            storage.set_state("telegram_last_update_text", text[:80])
            storage.set_state("telegram_last_update_chat_id", chat_id)
            if chat_id and chat_id != str(self.settings.telegram_chat_id):
                storage.set_state("telegram_last_update_ignored_reason", "chat_id_mismatch")
                continue
            storage.set_state("telegram_last_update_ignored_reason", "")
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
            elif text == "/ai":
                actions.append("ai")
            elif text == "/positions":
                actions.append("positions")
            elif text == "/training":
                actions.append("training")
            elif text == "/health":
                actions.append("health")
            elif text == "/errors":
                actions.append("errors")
            elif text == "/shadow":
                actions.append("shadow")
            elif text == "/execution":
                actions.append("execution")
            elif text == "/lessons":
                actions.append("lessons")
            elif text == "/market":
                actions.append("market")
        self.last_error = ""
        return actions

    def _send_telegram(self, message: str) -> None:
        token = self.settings.telegram_bot_token
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = urllib.parse.urlencode({"chat_id": self.settings.telegram_chat_id, "text": message}).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            json.loads(response.read().decode("utf-8"))
