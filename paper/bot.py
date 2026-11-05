"""Logique du bot de paper trading : un TokenBot par token, un Bot qui orchestre.

Cycle d'une position (scénario mixte du backtest v3) :
  ENTRÉE : ordre spot ACHAT maker au meilleur bid -> dès qu'il est rempli, short perp taker
  SORTIE : ordre spot VENTE maker au meilleur ask -> dès qu'il est rempli, rachat perp taker
           (si la vente maker traîne plus de 15 min : sortie taker)
  RÉÉQUILIBRAGE (urgent, taker) : vente spot puis rachat perp de la même quantité,
           tout le cash libéré part en marge du short -> retour au levier de départ

Une jambe sans l'autre (perp refusé, données périmées...) :
  1. on retente la jambe manquante à chaque seconde
  2. après PAPER_LEG_TIMEOUT_S, on annule la 1re jambe (spot revendu / racheté en taker)
  3. si même ça échoue après 4 x le délai : kill switch du token + alerte
"""

import asyncio
import json
import logging
import math
import os
import random
import time

import config

log = logging.getLogger("bot")
HOUR_MS = 3600 * 1000
SLIP = config.BT_SLIPPAGE


def floor_lot(q, dec):
    return math.floor(q * 10 ** dec + 1e-9) / 10 ** dec


def ceil_lot(q, dec):
    return math.ceil(q * 10 ** dec - 1e-9) / 10 ** dec


class TokenBot:
    def __init__(self, spec, account, store, market, notify, rng=random.random):
        self.perp = spec["perp"]
        self.spot_coin = spec["spot_coin"]
        self.mult = spec["mult"]
        self.dec = spec["lot_decimals"]
        self.mmr = spec["mmr"]
        self.lev = config.PAPER_LEVERAGE
        self.acc = account
        self.store, self.market, self.notify, self.rng = store, market, notify, rng
        self.order = None          # ordre maker spot en attente
        self.action = None         # entry / exit / rebalance : ce que la 1re jambe a commencé
        self.leg_since = None      # début d'un déséquilibre spot/perp
        self.signal = None         # True = funding 72h > 0 ; None = inconnu
        self.signal_value = None
        self.halted = False
        self.snaps = {}            # heure (ms) -> [taille short, prix oracle] à HH:00
        self.last_hour = None

    # --- état persistant ---------------------------------------------------------
    def to_state(self):
        return {"account": self.acc.to_dict(), "order": self.order, "action": self.action,
                "leg_since": self.leg_since, "signal": self.signal,
                "signal_value": self.signal_value, "halted": self.halted,
                "snaps": self.snaps, "last_hour": self.last_hour}

    def load_state(self, st):
        from paper.account import Account
        self.acc = Account.from_dict(st["account"])
        for k in ("order", "action", "leg_since", "signal", "signal_value", "halted",
                  "last_hour"):
            setattr(self, k, st.get(k))
        self.snaps = {int(h): v for h, v in st.get("snaps", {}).items()}

    # --- marché ----------------------------------------------------------------
    @property
    def sbook(self):
        return self.market.book(self.spot_coin)

    @property
    def pbook(self):
        return self.market.book(self.perp)

    def mark(self):
        b = self.pbook
        return b.mark or b.mid

    def delta(self):
        """Écart d'exposition en tokens spot (> 0 : trop de spot, < 0 : trop de short)."""
        return self.acc.spot_qty - self.acc.perp_size * self.mult

    def tol(self):
        return 0.5 * 10 ** -self.dec

    # --- exécutions simulées ---------------------------------------------------
    def taker(self, leg, side, qty, reason, now):
        """Ordre au marché simulé. Renvoie False si impossible (données périmées, refus)."""
        if qty <= 0:
            return True
        book = self.sbook if leg == "spot" else self.pbook
        if book.bid is None or book.ask is None or book.age(now) > config.PAPER_STALE_S:
            log.warning("%s : %s %s impossible, prix perimes", self.perp, leg, side)
            return False
        if self.rng() < config.PAPER_SIM_TAKER_FAIL_PROB:
            log.warning("%s : %s %s refuse (echec simule)", self.perp, leg, side)
            return False
        px = book.ask * (1 + SLIP) if side == "buy" else book.bid * (1 - SLIP)
        if leg == "spot":
            fee_rate = config.SPOT_TAKER_FEE
            fee = (self.acc.buy_spot if side == "buy" else self.acc.sell_spot)(qty, px, fee_rate)
        else:
            fee_rate = config.PERP_TAKER_FEE
            fee = (self.acc.open_short(qty, px, fee_rate, self.lev) if side == "sell"
                   else self.acc.close_short(qty, px, fee_rate))
        self.store.fill(self.perp, leg, side, qty, px, fee, "taker", reason)
        log.info("%s : FILL taker %s %s %.6g @ %.6g (frais %.4f)", self.perp, leg, side, qty,
                 px, fee)
        return True

    def post_maker(self, side, now):
        b = self.sbook
        if b.bid is None or b.ask is None or b.age(now) > config.PAPER_STALE_S:
            return
        px = b.bid if side == "buy" else b.ask
        if side == "buy":
            notional = self.acc.cash / (1 + 1 / self.lev + config.SPOT_MAKER_FEE
                                        + config.PERP_TAKER_FEE + SLIP)
            qty = floor_lot(notional / px, self.dec)
        else:
            qty = self.acc.spot_qty
        if qty * px < config.PAPER_MIN_ORDER_USD:
            self.notify.send(f"{self.perp} : ordre {side} trop petit ({qty * px:.2f} $ < "
                             f"{config.PAPER_MIN_ORDER_USD:.0f} $)", key=f"small{self.perp}",
                             min_interval_s=3600)
            return
        self.order = {"side": side, "px": px, "qty": qty, "ts": now, "created": now}
        log.info("%s : ordre maker spot %s %.6g @ %.6g", self.perp, side, qty, px)

    def check_order(self, now):
        o, b = self.order, self.sbook
        buy = o["side"] == "buy"
        fresh = b.age(now) <= config.PAPER_STALE_S
        # Rempli si le marché traverse notre prix (un trade strictement au-delà, ou le
        # meilleur prix opposé qui l'atteint). Un simple contact ne suffit pas : file d'attente.
        crossed = fresh and ((b.ask is not None and b.ask <= o["px"]) if buy
                             else (b.bid is not None and b.bid >= o["px"]))
        traded = any(ts >= o["ts"] and (px < o["px"] if buy else px > o["px"])
                     for ts, px in b.trades)
        if crossed or traded:
            self.order = None
            fee = (self.acc.buy_spot if buy else self.acc.sell_spot)(
                o["qty"], o["px"], config.SPOT_MAKER_FEE)
            self.store.fill(self.perp, "spot", o["side"], o["qty"], o["px"], fee, "maker",
                            "entry" if buy else "exit")
            log.info("%s : FILL maker spot %s %.6g @ %.6g", self.perp, o["side"], o["qty"],
                     o["px"])
            self.action = "entry" if buy else "exit"
            self.repair(now)                       # hedge perp tout de suite
            return
        # Annulation si le signal a changé entre-temps
        if self.signal is not None and self.signal != buy:
            log.info("%s : ordre %s annule (signal change)", self.perp, o["side"])
            self.order = None
            return
        # Sortie maker trop lente -> taker
        if not buy and now - o["created"] > config.PAPER_EXIT_MAKER_TIMEOUT_S:
            self.order = None
            self.action = "exit"
            if self.taker("spot", "sell", self.acc.spot_qty, "exit_timeout", now):
                self.repair(now)
            return
        # Repositionnement au meilleur prix
        best = b.bid if buy else b.ask
        if (fresh and best and now - o["ts"] > config.PAPER_REPRICE_S
                and (best > o["px"] if buy else best < o["px"])):
            o["px"], o["ts"] = best, now
            if buy:
                notional = self.acc.cash / (1 + 1 / self.lev + config.SPOT_MAKER_FEE
                                            + config.PERP_TAKER_FEE + SLIP)
                o["qty"] = floor_lot(notional / best, self.dec)

    # --- une jambe sans l'autre ------------------------------------------------
    def repair(self, now):
        d = self.delta()
        if abs(d) <= self.tol():
            self.leg_since = None
            return
        if self.leg_since is None:
            self.leg_since = now
        # 1) on complète la jambe perp
        ok = (self.taker("perp", "sell", d / self.mult, f"{self.action}_hedge", now) if d > 0
              else self.taker("perp", "buy", -d / self.mult, f"{self.action}_hedge", now))
        if ok:
            waited = now - self.leg_since
            self.leg_since = None
            if waited > 1:
                self.store.event(self.perp, "leg_hedged", seconds=round(waited, 1))
            self.complete(now)
            return
        waited = now - self.leg_since
        self.notify.send(f"{self.perp} : jambe perp en attente depuis {waited:.0f}s "
                         f"({self.action})", key=f"leg{self.perp}", min_interval_s=60)
        # 2) trop long : on annule la jambe spot
        if waited > config.PAPER_LEG_TIMEOUT_S:
            ok = (self.taker("spot", "sell", d, "leg_revert", now) if d > 0
                  else self.taker("spot", "buy", -d, "leg_revert", now))
            if ok:
                self.store.event(self.perp, "leg_reverted", action=self.action,
                                 seconds=round(waited, 1))
                self.notify.send(f"{self.perp} : perp impossible pendant {waited:.0f}s, "
                                 f"jambe spot annulee ({self.action} abandonne)")
                self.leg_since, self.action = None, None
                return
            # 3) rien ne passe : arrêt du token
            if waited > 4 * config.PAPER_LEG_TIMEOUT_S:
                self.kill(f"jambe non couverte depuis {waited:.0f}s", now)

    def complete(self, now):
        """Les deux jambes sont alignées : on termine l'action commencée."""
        a, self.action = self.action, None
        mark, spot = self.mark(), self.sbook.mid
        if a == "entry":
            self.acc.trades += 1
            dist = self.acc.liq_distance(mark, self.mmr)
            self.store.event(self.perp, "entry", qty=self.acc.spot_qty,
                             entry=self.acc.perp_entry, liq_distance=dist)
            self.notify.send(f"ENTREE {self.perp} : {self.acc.spot_qty:g} spot + short perp "
                             f"@ {self.acc.perp_entry:.6g} ({self.acc.spot_qty * spot:.2f} $), "
                             f"liquidation a +{dist:.0%}")
        elif a == "exit":
            pnl = self.acc.cash - self.acc.capital
            self.store.event(self.perp, "exit", cash=self.acc.cash, pnl=pnl)
            self.notify.send(f"SORTIE {self.perp} : {self.acc.cash:.2f} USDC "
                             f"(PnL {pnl:+.2f} $, funding {self.acc.funding_cum:+.3f}, "
                             f"frais {self.acc.fees_cum:.3f})")
        elif a == "rebalance":
            self.acc.add_margin(self.acc.cash)
            self.acc.rebalances += 1
            dist = self.acc.liq_distance(mark, self.mmr)
            self.store.event(self.perp, "rebalance", qty=self.acc.spot_qty,
                             margin=self.acc.margin, liq_distance=dist)
            self.notify.send(f"REEQUILIBRAGE {self.perp} : taille {self.acc.spot_qty:g}, "
                             f"marge {self.acc.margin:.2f} $, liquidation a +{dist:.0%}")

    # --- risque ------------------------------------------------------------------
    def risk_check(self, now):
        if self.acc.perp_size <= 0:
            return
        mark = self.mark()
        if mark is None or now - self.pbook.ctx_ts > config.PAPER_STALE_ALERT_S:
            self.notify.send(f"{self.perp} : pas de prix mark depuis "
                             f"{config.PAPER_STALE_ALERT_S // 60} min, risque non verifie",
                             key=f"stale{self.perp}", min_interval_s=1800)
            return
        if self.acc.perp_equity(mark) <= self.mmr * self.acc.perp_size * mark:
            # Ne devrait jamais arriver : le short est perdu (marge isolée), spot gardé
            self.store.event(self.perp, "liquidation", mark=mark, margin=self.acc.margin)
            self.acc.perp_size, self.acc.perp_entry, self.acc.margin = 0.0, 0.0, 0.0
            self.kill(f"LIQUIDATION simulee a {mark:.6g}", now)
            return
        dist = self.acc.liq_distance(mark, self.mmr)
        if dist < config.PAPER_REBALANCE_BUFFER and self.leg_since is None:
            self.rebalance(now, mark)

    def rebalance(self, now, mark):
        if self.order:                             # la sécurité passe avant une sortie maker
            self.order = None
        s = self.sbook.bid
        if s is None:
            return
        acc = self.acc
        eq = acc.equity(s, mark)
        unit_cost = s * (config.SPOT_TAKER_FEE + SLIP) + mark / self.mult * (
            config.PERP_TAKER_FEE + SLIP)
        unit_need = s + mark / self.mult / self.lev
        q_new = (eq - acc.spot_qty * unit_cost) / (unit_need - unit_cost)
        dq = max(acc.spot_qty - q_new, config.PAPER_MIN_ORDER_USD / s)
        dq = min(ceil_lot(dq, self.dec), acc.spot_qty)
        self.action = "rebalance"
        # spot d'abord : si le perp échoue ensuite, la réparation rachète du perp (bon sens)
        if self.taker("spot", "sell", dq, "rebalance", now):
            self.repair(now)
        else:
            self.action = None
            self.notify.send(f"{self.perp} : reequilibrage impossible (spot), nouvel essai "
                             "dans 1 min", key=f"reb{self.perp}", min_interval_s=300)

    # --- kill switch -------------------------------------------------------------
    def kill(self, reason, now):
        if not self.halted:
            self.halted = True
            self.order, self.action, self.leg_since = None, None, None
            self.store.event(self.perp, "kill", reason=reason)
            self.notify.send(f"KILL {self.perp} : {reason} -> fermeture de tout, bot arrete")
        self.flatten(now)

    def flatten(self, now):
        if self.acc.spot_qty > 0:
            self.taker("spot", "sell", self.acc.spot_qty, "kill", now)
        if self.acc.perp_size > 0:
            self.taker("perp", "buy", self.acc.perp_size, "kill", now)

    # --- funding -------------------------------------------------------------------
    def on_funding(self, rows, now):
        """rows : historique fundingHistory des ~73 dernières heures."""
        horizon = now * 1000 - config.PAPER_SIGNAL_WINDOW_H * HOUR_MS
        recent = [float(r["fundingRate"]) for r in rows if r["time"] > horizon]
        if len(recent) >= 0.8 * config.PAPER_SIGNAL_WINDOW_H:
            value = sum(recent) / len(recent)
            new = value > 0
            if new != self.signal:
                log.info("%s : signal %s (funding 72h %.2f%%/an)", self.perp,
                         "ON" if new else "OFF", value * config.HOURS_PER_YEAR * 100)
                self.store.event(self.perp, "signal", on=new, apr=value * config.HOURS_PER_YEAR)
            self.signal, self.signal_value = new, value
        for r in rows:
            hour = r["time"] // HOUR_MS * HOUR_MS
            snap = self.snaps.get(hour)
            if not snap or snap[0] <= 0:
                continue
            rate = float(r["fundingRate"])
            amount = rate * snap[0] * snap[1]
            if self.store.funding(self.perp, hour, rate, snap[0], snap[1], amount):
                self.acc.credit_funding(amount)
                log.info("%s : funding %+.5f $ (taux %.6f%%)", self.perp, amount, rate * 100)

    # --- boucle ------------------------------------------------------------------
    def tick(self, now):
        hour = int(now * 1000) // HOUR_MS * HOUR_MS
        if self.last_hour is None:
            self.last_hour = hour                  # démarrage : pas de photo d'une heure passée
        elif hour != self.last_hour:
            self.last_hour = hour
            self.snaps[hour] = [self.acc.perp_size, self.pbook.oracle or self.mark() or 0.0]
            for h in sorted(self.snaps)[:-12]:
                del self.snaps[h]

        if self.halted:
            if not self.acc.is_flat():
                self.flatten(now)                  # on insiste jusqu'à être à plat
            return
        if self.order:
            self.check_order(now)
        elif self.leg_since is not None or abs(self.delta()) > self.tol():
            self.repair(now)
        elif self.signal is not None:
            if self.acc.is_flat() and self.signal:
                self.post_maker("buy", now)
            elif not self.acc.is_flat() and not self.signal:
                self.post_maker("sell", now)

    def phase(self):
        if self.halted:
            return "ARRETE" + ("" if self.acc.is_flat() else " (fermeture en cours)")
        if self.leg_since is not None:
            return "JAMBE SEULE"
        if self.order:
            return "ENTREE (maker)" if self.order["side"] == "buy" else "SORTIE (maker)"
        return "USDC" if self.acc.is_flat() else "EN POSITION"

    def summary(self):
        mark, spot = self.mark(), self.sbook.mid
        acc = self.acc
        eq = acc.equity(spot, mark) if mark and spot else acc.cash
        dist = acc.liq_distance(mark, self.mmr) if mark else None
        return {"cash": acc.cash, "spot_value": acc.spot_qty * (spot or 0),
                "perp_equity": acc.perp_equity(mark) if mark else acc.margin,
                "equity": eq, "pnl": eq - acc.capital, "funding_cum": acc.funding_cum,
                "fees_cum": acc.fees_cum, "liq_distance": dist}


class Bot:
    def __init__(self, token_bots, store, market, notify, fetch_funding):
        self.bots = token_bots
        self.store, self.market, self.notify = store, market, notify
        self.fetch_funding = fetch_funding        # (perp, start_ms, end_ms) -> rows
        self.capital = sum(tb.acc.capital for tb in token_bots)
        self._saved = {}
        market.on_error = lambda txt: notify.send(f"ERREUR {txt}", key="ws", min_interval_s=600)

    def save(self):
        for tb in self.bots:
            st = tb.to_state()
            js = json.dumps(st, sort_keys=True)
            if self._saved.get(tb.perp) != js:
                self.store.put(f"token:{tb.perp}", st)
                self._saved[tb.perp] = js

    def kill_all(self, reason, now=None):
        now = now or time.time()
        for tb in self.bots:
            tb.kill(reason, now)
        self.save()

    def status_text(self):
        lines = []
        tot = 0.0
        for tb in self.bots:
            s = tb.summary()
            tot += s["pnl"]
            sig = ("?" if tb.signal_value is None
                   else f"{tb.signal_value * config.HOURS_PER_YEAR:+.1%}/an")
            dist = "-" if s["liq_distance"] is None else f"+{s['liq_distance']:.0%}"
            lines.append(f"{tb.perp} : {tb.phase()} | funding 72h {sig} | equite "
                         f"{s['equity']:.2f} $ (PnL {s['pnl']:+.2f}) | funding recu "
                         f"{s['funding_cum']:+.3f} | frais {s['fees_cum']:.3f} | liq {dist} | "
                         f"reequil. {tb.acc.rebalances}")
        lines.append(f"Total PnL : {tot:+.2f} $ sur {self.capital:.0f} USDC")
        return "\n".join(lines)

    def check_kill_requests(self, now=None):
        req = self.store.get("kill_request")
        if req:
            self.store.put("kill_request", None)
            self.kill_all(f"demande manuelle ({req})", now)
        if os.path.exists(config.PAPER_KILL_FILE):
            if not all(tb.halted for tb in self.bots):
                self.kill_all(f"fichier {config.PAPER_KILL_FILE} present", now)
        if self.store.get("resume_request"):
            self.store.put("resume_request", None)
            for tb in self.bots:
                tb.halted = False
            self.store.event(None, "resume")
            self.notify.send("Reprise demandee : bot relance")

    def check_global_loss(self, now=None):
        # sans prix (démarrage, flux coupé) l'équité serait fausse : on ne juge pas
        if any(tb.mark() is None or tb.sbook.mid is None for tb in self.bots
               if not tb.acc.is_flat()):
            return
        pnl = sum(tb.summary()["pnl"] for tb in self.bots)
        if pnl < -config.PAPER_MAX_LOSS_PCT * self.capital and not all(
                tb.halted for tb in self.bots):
            self.kill_all(f"perte totale {pnl:.2f} $ > {config.PAPER_MAX_LOSS_PCT:.0%} "
                          "du capital", now)

    async def update_funding(self):
        now = time.time()
        start = int(now * 1000) - (config.PAPER_SIGNAL_WINDOW_H + 1) * HOUR_MS
        for tb in self.bots:
            rows = await asyncio.to_thread(self.fetch_funding, tb.perp, start, int(now * 1000))
            tb.on_funding(rows, time.time())
        self.save()

    async def funding_loop(self):
        while True:
            try:
                await self.update_funding()
                # prochain passage : HH:00:40 (le paiement est publié juste après l'heure)
                now = time.time()
                wait = 3600 - now % 3600 + 40
            except Exception as e:
                log.exception("funding")
                self.notify.send(f"ERREUR funding : {e!r}", key="funding", min_interval_s=1800)
                wait = 120
            await asyncio.sleep(wait)

    async def main_loop(self):
        last_risk = last_snap = last_flags = 0.0
        while True:
            now = time.time()
            try:
                if now - last_flags >= 5:
                    last_flags = now
                    self.check_kill_requests(now)
                for tb in self.bots:
                    tb.tick(now)
                if now - last_risk >= config.PAPER_RISK_CHECK_S:
                    last_risk = now
                    for tb in self.bots:
                        tb.risk_check(now)
                    self.check_global_loss(now)
                if now - last_snap >= config.PAPER_SNAPSHOT_S:
                    last_snap = now
                    for tb in self.bots:
                        self.store.snapshot(tb.perp, tb.summary())
                self.save()
            except Exception as e:
                log.exception("boucle principale")
                self.store.event(None, "error", error=repr(e))
                self.notify.send(f"ERREUR : {e!r}", key=f"err{type(e).__name__}",
                                 min_interval_s=600)
            await asyncio.sleep(1)

    async def telegram_loop(self):
        while True:
            cmds = await asyncio.to_thread(self.notify.poll_commands, 30)
            for c in cmds:
                if c == "/status":
                    self.notify.send(self.status_text())
                elif c == "/kill":
                    self.kill_all("commande Telegram /kill")
                else:
                    self.notify.send("Commandes : /status, /kill (reprise : "
                                     "python paper_bot.py --resume sur le serveur)")

    async def wait_for_data(self, timeout_s=60):
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if all(tb.sbook.bid and tb.pbook.bid and tb.pbook.mark for tb in self.bots):
                return True
            await asyncio.sleep(1)
        return False

    async def run(self):
        tasks = [asyncio.create_task(self.market.run(), name="ws")]
        if not await self.wait_for_data():
            self.notify.send("ERREUR : pas de donnees de marche apres 60 s, on continue "
                             "d'attendre")
        tasks += [asyncio.create_task(self.funding_loop(), name="funding"),
                  asyncio.create_task(self.main_loop(), name="main"),
                  asyncio.create_task(self.telegram_loop(), name="telegram")]
        self.notify.send("Bot demarre\n" + self.status_text())
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                t.result()                          # remonte l'exception -> systemd relance
        finally:
            for t in tasks:
                t.cancel()
            self.save()
