"""Telegram delivery (HTML messages, chart photos, and reading /commands)."""
from __future__ import annotations

import logging
import os
import re
import time

import requests

log = logging.getLogger(__name__)
MAX_LEN = 4000


def strip_tags(text: str) -> str:
    t = re.sub(r"<[^>]+>", "", text)
    return t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def chunks(text: str, limit: int = MAX_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single monster line
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


class Telegram:
    def __init__(self, token: str | None, chat_id: str | None, dry_run: bool = False, outbox: str | None = None):
        self.token, self.chat_id = token, chat_id
        self.dry_run = dry_run or not (token and chat_id)
        self.outbox = outbox
        self.sent: list[dict] = []   # record of everything sent this run (used by tests)
        self.migrated_to: str | None = None
        self.session = requests.Session()

    @property
    def base(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    def _record(self, kind: str, text: str, photo: bytes | None = None):
        self.sent.append({"kind": kind, "text": text, "photo": bool(photo)})
        if self.dry_run:
            print(f"\n----- [telegram {kind}] -----\n{strip_tags(text)}\n")
            if self.outbox:
                os.makedirs(self.outbox, exist_ok=True)
                n = len(self.sent)
                with open(os.path.join(self.outbox, f"{n:03d}_{kind}.txt"), "w") as f:
                    f.write(text)
                if photo:
                    with open(os.path.join(self.outbox, f"{n:03d}_chart.png"), "wb") as f:
                        f.write(photo)

    def _post(self, method: str, data: dict, files=None) -> dict:
        for attempt in range(3):
            r = self.session.post(f"{self.base}/{method}", data=data, files=files, timeout=30)
            j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 429:
                time.sleep(int(j.get("parameters", {}).get("retry_after", 3)) + 1)
                continue
            new_id = (j.get("parameters") or {}).get("migrate_to_chat_id")
            if not j.get("ok") and new_id and "chat_id" in data:
                # The group was upgraded to a supergroup: Telegram gives it a new id (-100...).
                log.warning("group upgraded to supergroup, switching chat id")
                self.chat_id = self.migrated_to = str(new_id)
                data = {**data, "chat_id": self.chat_id}
                continue
            return j | {"_status": r.status_code}
        return {"ok": False, "description": "rate limited"}

    def send(self, text: str, silent: bool = False) -> bool:
        ok = True
        for part in chunks(text):
            self._record("message", part)
            if self.dry_run:
                continue
            data = {"chat_id": self.chat_id, "text": part, "parse_mode": "HTML",
                    "disable_web_page_preview": "true", "disable_notification": "true" if silent else "false"}
            j = self._post("sendMessage", data)
            if not j.get("ok") and "parse" in str(j.get("description", "")).lower():
                data.pop("parse_mode")
                data["text"] = strip_tags(part)
                j = self._post("sendMessage", data)
            if not j.get("ok"):
                log.error("telegram sendMessage failed: %s", j.get("description"))
                ok = False
        return ok

    def send_photo(self, png: bytes, caption: str) -> bool:
        self._record("photo", caption, png)
        if self.dry_run:
            return True
        data = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"}
        j = self._post("sendPhoto", data, files={"photo": ("chart.png", png, "image/png")})
        if not j.get("ok"):
            log.error("telegram sendPhoto failed: %s", j.get("description"))
            return False
        return True

    def get_updates(self, offset: int | None) -> list[dict]:
        if self.dry_run:
            return []
        params = {"timeout": 0, "allowed_updates": '["message","channel_post"]'}
        if offset:
            params["offset"] = offset
        try:
            r = self.session.get(f"{self.base}/getUpdates", params=params, timeout=30)
            j = r.json()
            return j.get("result", []) if j.get("ok") else []
        except Exception as e:  # noqa: BLE001
            log.warning("getUpdates failed: %s", e)
            return []
