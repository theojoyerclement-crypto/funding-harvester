"""Étape 1 : collecteur de données funding-harvester.

Usage :  python collector.py

1. Liste les tokens qui ont à la fois un marché spot et un perp sur Hyperliquid.
2. Récupère pour chacun le funding horaire et les prix 1h (spot + perp) sur 90 jours.
3. Stocke tout en SQLite (relançable : ne télécharge que ce qui manque, sans doublons).
4. Affiche funding moyen annualisé et % d'heures positives.
"""

import time

import config
import hl_api
import storage

HOUR_MS = 3600 * 1000


def perp_for_spot(pair, perps):
    """Retourne (perp, multiplicateur de prix) pour une paire spot, ou None.

    Règles, dans l'ordre :
      - alias explicite dans config.SPOT_ALIASES (UFART -> FARTCOIN, UBONK -> kBONK...)
      - même nom (HYPE -> HYPE)
      - token Unit : "U" + nom, fullName "Unit ..." (UBTC -> BTC)
      - token bridgé : nom + "0" (LINK0 -> LINK)
    """
    base = pair["base"]
    if base in config.SPOT_ALIASES:
        perp, mult = config.SPOT_ALIASES[base]
        return (perp, mult) if perp in perps else None
    if base in perps:
        return base, 1
    if base.startswith("U") and pair["base_full"].startswith("Unit ") and base[1:] in perps:
        return base[1:], 1
    if base.endswith("0") and base[:-1] in perps:
        return base[:-1], 1
    return None


def find_markets():
    """Un marché spot par perp : on garde la paire spot la plus liquide (volume 24h).

    Garde-fou : le prix spot (x multiplicateur) doit être proche du prix du perp,
    sinon c'est un homonyme (ex. un spot "TRUMP" qui n'est pas le vrai TRUMP).
    """
    marks = hl_api.perp_marks()
    perps = set(marks)
    best = {}
    for pair in hl_api.spot_pairs():
        match = perp_for_spot(pair, perps)
        if not match or pair["volume_24h"] <= 0:   # paire spot sans échanges = inutilisable
            continue
        perp, mult = match
        ratio = pair["price"] * mult / marks[perp] if marks[perp] else 0
        if abs(ratio - 1) > config.MAX_PRICE_GAP:
            print(f"  ! {pair['base']}/{pair['quote']} ignore : prix spot = {ratio:.4f} x "
                  f"perp {perp} -> pas le meme actif")
            continue
        if perp not in best or pair["volume_24h"] > best[perp]["volume_24h"]:
            best[perp] = {"perp": perp, "spot_coin": pair["coin"], "spot_base": pair["base"],
                          "spot_quote": pair["quote"], "price_mult": mult,
                          "volume_24h": pair["volume_24h"]}
    return sorted(best.values(), key=lambda m: m["perp"])


def collect(con, market, now_ms, start_default):
    perp = market["perp"]

    # Funding : on reprend juste après le dernier point stocké...
    last = storage.last_time(con, "funding", perp)
    start = last + 1 if last else start_default
    rows = hl_api.funding_history(perp, start, now_ms)
    # ... et on complète vers le passé si HISTORY_DAYS a été augmenté
    first = storage.first_time(con, perp)
    if first and first > start_default + HOUR_MS:
        rows += hl_api.funding_history(perp, start_default, first - 1)
    storage.insert_funding(con, perp, rows)

    # Prix perp + spot, en 1h et en 4h : on repart de la dernière bougie (peut-être incomplète)
    n_candles = 0
    for interval, suffix in (("1h", ""), ("4h", "_4h")):
        for kind, coin in (("perp", perp), ("spot", market["spot_coin"])):
            market_type = kind + suffix
            last = storage.last_time(con, "prices", perp, market_type)
            start = last if last else start_default
            candles = hl_api.candles(coin, interval, start, now_ms)
            storage.insert_candles(con, perp, market_type, candles)
            n_candles += len(candles)

    con.commit()
    return len(rows), n_candles


def print_table(con, since_ms):
    rows = storage.summary(con, since_ms)
    print()
    print(f"Funding sur {config.HISTORY_DAYS} jours (positif = les shorts perp sont payes)")
    print(f"{'Token':<10}{'Spot':<16}{'Heures':>8}{'Funding annualise':>20}{'% heures > 0':>15}")
    print("-" * 69)
    for perp, base, quote, n, apr, pct in rows:
        print(f"{perp:<10}{base + '/' + quote:<16}{n:>8}{apr * 100:>19.2f}%{pct:>14.1f}%")
    print()
    print(f"Frais aller-retour cash-and-carry (taker spot + perp) : "
          f"{config.ROUND_TRIP_TAKER * 100:.2f}% du notionnel")


def main():
    now_ms = int(time.time() * 1000)
    start_default = now_ms - config.HISTORY_DAYS * 24 * HOUR_MS

    con = storage.connect()
    markets = find_markets()
    print(f"{len(markets)} tokens avec spot + perp trouves")

    for i, m in enumerate(markets, 1):
        storage.upsert_market(con, m, now_ms)
        n_f, n_c = collect(con, m, now_ms, start_default)
        print(f"[{i:>2}/{len(markets)}] {m['perp']:<10} spot={m['spot_base']}/{m['spot_quote']:<6}"
              f" +{n_f} funding, {n_c} bougies")

    print_table(con, start_default)
    con.close()


if __name__ == "__main__":
    main()
