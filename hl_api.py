"""Petit client pour l'endpoint public /info de Hyperliquid."""

import time

import requests

import config

_session = requests.Session()


def info(payload, retries=5):
    """POST sur /info avec relance simple en cas de rate limit (429) ou d'erreur réseau."""
    for attempt in range(retries):
        try:
            r = _session.post(config.API_URL, json=payload, timeout=30)
            if r.status_code == 429:
                raise requests.HTTPError("429 rate limited")
            r.raise_for_status()
            time.sleep(config.REQUEST_PAUSE_S)
            return r.json()
        except (requests.RequestException, ValueError) as e:
            wait = 2 ** attempt
            print(f"  ! requete echouee ({e}), nouvel essai dans {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"Echec definitif de la requete {payload}")


def perp_names():
    """Noms des perps actifs (DEX principal, non délistés)."""
    meta = info({"type": "meta"})
    return {a["name"] for a in meta["universe"] if not a.get("isDelisted")}


def perp_marks():
    """Prix mark actuel de chaque perp actif : {nom: prix}."""
    meta, ctxs = info({"type": "metaAndAssetCtxs"})
    # ici la liste ctxs EST alignée sur meta["universe"]
    return {a["name"]: float(c["markPx"]) for a, c in zip(meta["universe"], ctxs)
            if not a.get("isDelisted") and c.get("markPx")}


def spot_pairs():
    """Liste des paires spot avec base, quote, nom complet et volume 24h (en quote)."""
    meta, ctxs = info({"type": "spotMetaAndAssetCtxs"})
    tokens = {t["index"]: t for t in meta["tokens"]}
    # Attention : la liste ctxs n'est PAS alignée sur meta["universe"] -> on associe par "coin"
    ctx_by_coin = {c["coin"]: c for c in ctxs}
    pairs = []
    for pair in meta["universe"]:
        ctx = ctx_by_coin.get(pair["name"], {})
        base = tokens[pair["tokens"][0]]
        quote = tokens[pair["tokens"][1]]
        pairs.append({
            "coin": pair["name"],              # identifiant API : "PURR/USDC" ou "@107"
            "base": base["name"],
            "base_full": base.get("fullName") or "",
            "quote": quote["name"],
            "volume_24h": float(ctx.get("dayNtlVlm") or 0),
            "price": float(ctx.get("midPx") or ctx.get("markPx") or 0),
        })
    return pairs


def funding_history(coin, start_ms, end_ms):
    """Tout l'historique de funding entre deux dates.

    L'API renvoie au plus 500 points par appel : on pagine en avançant startTime.
    """
    out = []
    while start_ms < end_ms:
        batch = info({"type": "fundingHistory", "coin": coin,
                      "startTime": start_ms, "endTime": end_ms})
        if not batch:
            break
        out.extend(batch)
        start_ms = batch[-1]["time"] + 1
        if len(batch) < 500:
            break
    return out


def candles(coin, interval, start_ms, end_ms):
    """Bougies ("1h", "4h"...). L'API ne renvoie que les 5000 plus récentes :
    un seul appel suffit, plus d'historique n'existe pas côté API."""
    return info({"type": "candleSnapshot",
                 "req": {"coin": coin, "interval": interval,
                         "startTime": start_ms, "endTime": end_ms}})
