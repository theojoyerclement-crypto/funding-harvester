"""Données de marché live via le WebSocket Hyperliquid.

Abonnements par token : bbo + trades (spot et perp), activeAssetCtx (perp : mark, oracle).
Reconnexion automatique, ping toutes les 30 s (le serveur coupe après 60 s sans trafic).
"""

import asyncio
import collections
import json
import logging
import time

import websockets

log = logging.getLogger("market")


class Book:
    """Dernier état connu d'un marché (une "coin" au sens de l'API : "HYPE", "@107")."""

    def __init__(self):
        self.bid = self.ask = None
        self.bbo_ts = 0.0                    # heure locale de la dernière mise à jour
        self.mark = self.oracle = None
        self.ctx_ts = 0.0
        self.trades = collections.deque(maxlen=500)   # (heure locale, px)

    def age(self, now=None):
        return (now or time.time()) - self.bbo_ts

    @property
    def mid(self):
        return (self.bid + self.ask) / 2 if self.bid and self.ask else None


class MarketData:
    def __init__(self, url, perps, spots):
        self.url = url
        self.perps, self.spots = list(perps), list(spots)
        self.books = collections.defaultdict(Book)
        self.connected = False
        self.last_msg = 0.0
        self.reconnects = 0
        self.on_error = None                 # callback(texte), branché par le bot

    def book(self, coin):
        return self.books[coin]

    def subscriptions(self):
        subs = []
        for c in self.perps + self.spots:
            subs.append({"type": "bbo", "coin": c})
            subs.append({"type": "trades", "coin": c})
        for c in self.perps:
            subs.append({"type": "activeAssetCtx", "coin": c})
        return subs

    def handle(self, msg):
        ch, data = msg.get("channel"), msg.get("data")
        now = time.time()
        self.last_msg = now
        if ch == "bbo":
            b = self.books[data["coin"]]
            bid, ask = data["bbo"]
            b.bid = float(bid["px"]) if bid else None
            b.ask = float(ask["px"]) if ask else None
            b.bbo_ts = now
        elif ch == "trades":
            for t in data:
                self.books[t["coin"]].trades.append((now, float(t["px"])))
        elif ch == "activeAssetCtx":
            b, ctx = self.books[data["coin"]], data["ctx"]
            b.mark = float(ctx["markPx"])
            b.oracle = float(ctx["oraclePx"])
            b.ctx_ts = now

    async def run(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=None,
                                              open_timeout=15, max_size=2 ** 22) as ws:
                    for sub in self.subscriptions():
                        await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
                    self.connected, backoff = True, 1
                    log.info("WebSocket connecte (%d abonnements)", len(self.subscriptions()))
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        async for raw in ws:
                            self.handle(json.loads(raw))
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:                     # réseau, JSON, fermeture serveur...
                log.warning("WebSocket coupe : %r", e)
            self.connected = False
            self.reconnects += 1
            if self.reconnects % 10 == 0 and self.on_error:
                self.on_error(f"WebSocket : {self.reconnects} reconnexions")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _ping(self, ws):
        while True:
            await asyncio.sleep(30)
            await ws.send(json.dumps({"method": "ping"}))
            if time.time() - self.last_msg > 90:       # flux muet : on force une reconnexion
                log.warning("Aucun message depuis 90 s, reconnexion")
                await ws.close()
                return
