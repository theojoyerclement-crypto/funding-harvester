"""Alertes Telegram (envoi en arrière-plan) + lecture des commandes /status et /kill.

Variables d'environnement : TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
Sans elles, les alertes vont seulement dans le log.
"""

import logging
import os
import queue
import threading
import time

import requests

log = logging.getLogger("notify")


class Telegram:
    def __init__(self, token=None, chat_id=None, prefix="[paper]"):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = str(chat_id or os.environ.get("TELEGRAM_CHAT_ID", ""))
        self.enabled = bool(self.token and self.chat_id)
        self.prefix = prefix
        self._q = queue.Queue()
        self._last = {}                     # anti-spam : clé -> dernier envoi
        self._offset = None
        if self.enabled:
            threading.Thread(target=self._sender, daemon=True).start()
        else:
            log.warning("Telegram desactive (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID absents)")

    def send(self, text, key=None, min_interval_s=0):
        """Envoie une alerte. Avec key + min_interval_s : au plus une par intervalle."""
        log.info("ALERTE %s", text)
        if key:
            now = time.time()
            if now - self._last.get(key, 0) < min_interval_s:
                return
            self._last[key] = now
        if self.enabled:
            self._q.put(f"{self.prefix} {text}")

    def _sender(self):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        while True:
            text = self._q.get()
            for attempt in range(3):
                try:
                    r = requests.post(url, json={"chat_id": self.chat_id, "text": text},
                                      timeout=15)
                    if r.ok:
                        break
                    log.warning("Telegram HTTP %s : %s", r.status_code, r.text[:200])
                except requests.RequestException as e:
                    log.warning("Telegram injoignable : %s", e)
                time.sleep(2 ** attempt)

    def poll_commands(self, timeout_s=30):
        """Commandes reçues depuis le dernier appel (bloquant jusqu'à timeout_s).

        Seuls les messages venant de TELEGRAM_CHAT_ID sont acceptés.
        """
        if not self.enabled:
            time.sleep(timeout_s)
            return []
        params = {"timeout": timeout_s}
        if self._offset is not None:
            params["offset"] = self._offset
        try:
            r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                             params=params, timeout=timeout_s + 10)
            updates = r.json().get("result", [])
        except (requests.RequestException, ValueError) as e:
            log.warning("getUpdates echoue : %s", e)
            time.sleep(5)
            return []
        cmds = []
        for u in updates:
            self._offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) == self.chat_id and msg.get("text"):
                cmds.append(msg["text"].strip().split()[0].split("@")[0].lower())
        return cmds
