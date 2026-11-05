"""Étape 3 : bot de paper trading cash-and-carry (aucun ordre réel n'est envoyé).

Usage :
  python paper_bot.py            lance le bot (reprend l'état sauvé dans paper.db)
  python paper_bot.py --status   état des positions (lu dans paper.db)
  python paper_bot.py --kill     kill switch : ferme tout et arrête le trading
  python paper_bot.py --resume   lève le kill switch (supprimer aussi le fichier KILL)
  python paper_bot.py --reset    efface l'état (repart de zéro, garde l'historique)

Kill switch, au choix : --kill, commande Telegram /kill, fichier KILL dans le dossier,
ou automatique (perte > 10 % du capital, liquidation, jambe non couverte).
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time

import config
import hl_api
from collector import find_markets
from paper.account import Account
from paper.bot import Bot, TokenBot
from paper.market import MarketData
from paper.notify import Telegram
from paper.store import Store

log = logging.getLogger("paper_bot")


def load_env(path=".env"):
    """Charge un fichier .env simple (CLE=valeur) sans dépendance externe."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_specs():
    """Paire spot, multiplicateur, décimales et marge de maintenance de chaque token."""
    markets = {m["perp"]: m for m in find_markets()}
    meta = hl_api.info({"type": "meta"})
    perp_meta = {a["name"]: a for a in meta["universe"]}
    spot_dec = {t["name"]: t["szDecimals"] for t in hl_api.info({"type": "spotMeta"})["tokens"]}
    specs = []
    for perp in config.PAPER_TOKENS:
        if perp not in markets:
            sys.exit(f"{perp} : aucune paire spot valide trouvee")
        m, pm = markets[perp], perp_meta[perp]
        if config.PAPER_LEVERAGE > pm["maxLeverage"]:
            sys.exit(f"{perp} : levier {config.PAPER_LEVERAGE}x > max {pm['maxLeverage']}x")
        mmr = 1 / (2 * pm["maxLeverage"])
        buffer0 = (1 + 1 / config.PAPER_LEVERAGE) / (1 + mmr) - 1
        if buffer0 <= config.PAPER_REBALANCE_BUFFER:
            sys.exit(f"{perp} : a {config.PAPER_LEVERAGE}x la liquidation est a +{buffer0:.0%} "
                     f"des l'entree, sous le seuil de reequilibrage")
        specs.append({"perp": perp, "spot_coin": m["spot_coin"], "mult": m["price_mult"],
                      "lot_decimals": min(pm["szDecimals"], spot_dec[m["spot_base"]]),
                      "mmr": mmr, "spot_name": f"{m['spot_base']}/{m['spot_quote']}"})
    return specs


def build(store, notify):
    specs = load_specs()
    market = MarketData(config.WS_URL, [s["perp"] for s in specs],
                        [s["spot_coin"] for s in specs])
    bots = []
    for spec in specs:
        cap = config.PAPER_TOKENS[spec["perp"]]
        tb = TokenBot(spec, Account(spec["perp"], cap, cap), store, market, notify)
        st = store.get(f"token:{spec['perp']}")
        if st:
            tb.load_state(st)
            log.info("%s : etat recharge (%s)", spec["perp"], tb.phase())
        log.info("%s : spot %s (%s), lot %d dec., marge maintenance %.1f%%", spec["perp"],
                 spec["spot_name"], spec["spot_coin"], spec["lot_decimals"], spec["mmr"] * 100)
        bots.append(tb)
    return Bot(bots, store, market, notify, hl_api.funding_history)


def print_status(store):
    for perp in config.PAPER_TOKENS:
        st = store.get(f"token:{perp}")
        if not st:
            print(f"{perp} : jamais lance")
            continue
        a = st["account"]
        print(f"{perp} : {'ARRETE' if st['halted'] else 'actif'} | cash {a['cash']:.2f} | "
              f"spot {a['spot_qty']:g} | short {a['perp_size']:g} @ {a['perp_entry']:.6g} | "
              f"marge {a['margin']:.2f} | funding {a['funding_cum']:+.4f} | "
              f"frais {a['fees_cum']:.4f} | reequil. {a['rebalances']}")
    for ts, tok, kind, detail in store.con.execute(
            "SELECT datetime(ts,'unixepoch'), token, kind, detail FROM events "
            "ORDER BY id DESC LIMIT 10"):
        print(f"  {ts} {tok or '-':<9} {kind:<12} {detail}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true")
    g.add_argument("--kill", action="store_true")
    g.add_argument("--resume", action="store_true")
    g.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    load_env()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = Store(config.PAPER_DB_PATH)

    if args.status:
        return print_status(store)
    if args.kill:
        store.put("kill_request", "ligne de commande")
        return print("Kill switch demande : le bot ferme tout dans les 5 s.")
    if args.resume:
        store.put("resume_request", True)
        for perp in config.PAPER_TOKENS:      # bot arrêté : on lève le drapeau directement
            st = store.get(f"token:{perp}")
            if st:
                st["halted"] = False
                store.put(f"token:{perp}", st)
        if os.path.exists(config.PAPER_KILL_FILE):
            print(f"Attention : supprimer aussi le fichier {config.PAPER_KILL_FILE}")
        return print("Reprise demandee.")
    if args.reset:
        for perp in config.PAPER_TOKENS:
            store.con.execute("DELETE FROM state WHERE key=?", (f"token:{perp}",))
        store.con.commit()
        return print("Etat efface (historique conserve).")

    def on_sigterm(*_):                       # systemd stop : arrêt propre, état sauvé
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_sigterm)

    notify = Telegram()
    bot = build(store, notify)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass
    finally:
        bot.save()
        notify.send("Bot arrete (etat sauvegarde)")
        if notify.enabled:
            time.sleep(3)                         # laisse partir le dernier message
        log.info("arret")


if __name__ == "__main__":
    main()
