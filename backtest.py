"""Étape 2 (v3) : backtest cash-and-carry (long spot + short perp) sur funding.db.

Usage :  python backtest.py

Règle unique (aucun paramètre à optimiser) :
  - en position dès que le funding moyen 72h est > 0, sortie seulement s'il passe < 0
  - variante "une seule entrée" : après une sortie, on ne revient plus
  - hors position : 100 % USDC

Historique : tout ce que l'API donne (funding depuis la création du perp ; prix 1h
sur ~208 jours, 4h avant). Découpage par token : 2/3 train, 1/3 test.
Le train sert uniquement à : filtre de liquidité, choix du levier (commun à tous).

Scénarios de frais (doc Hyperliquid, palier 0, vérifiée le 2026-11-04) :
  - taker : spot 0.070 % + perp 0.045 %, + 0.05 % de slippage par jambe
  - mixte : spot en maker (0.040 %, sans slippage), puis hedge perp en taker (0.045 % + slip)
  - maker : spot 0.040 % + perp 0.015 %, sans slippage (risque de non-exécution ignoré)
  Les rééquilibrages sont toujours payés en taker (ils sont urgents).

Garde-fous repris de la v2 : exécution à la clôture suivante, funding de l'heure
d'entrée/sortie non compté, marge isolée (maintenance = 1/(2 x levier max)),
rééquilibrage neutre si hausse restante avant liquidation < 25 %, liquidation testée
sur les clôtures (1h ou 4h selon la période : les mèches ne sont PAS vues).
"""

import csv
import datetime as dt

import numpy as np

import config
import hl_api
import storage

HOUR_MS = 3600 * 1000
SLIP = config.BT_SLIPPAGE
SCENARIOS = {   # (coût spot, coût perp) par exécution
    "taker": (config.SPOT_TAKER_FEE + SLIP, config.PERP_TAKER_FEE + SLIP),
    "mixte": (config.SPOT_MAKER_FEE, config.PERP_TAKER_FEE + SLIP),
    "maker": (config.SPOT_MAKER_FEE, config.PERP_MAKER_FEE),
}
REBAL_FEES = SCENARIOS["taker"]


def max_leverages():
    try:
        meta = hl_api.info({"type": "meta"}, retries=2)
        return {a["name"]: a["maxLeverage"] for a in meta["universe"]}
    except RuntimeError:
        print("! API injoignable : levier max par defaut =", config.BT_DEFAULT_MAX_LEVERAGE)
        return {}


def load_token(con, perp):
    """Grille horaire : funding[i], spot[i], perp[i], grid[i] (ms).

    Prix à l'instant i = dernière clôture connue : bougie 1h si elle existe,
    sinon bougie 4h (une bougie ouverte à t n'est connue qu'à sa clôture).
    """
    fund = {t // HOUR_MS * HOUR_MS: r for t, r in
            con.execute("SELECT time, rate FROM funding WHERE perp=?", (perp,))}
    px = {"spot": {}, "perp": {}}
    rows = con.execute("SELECT market, time, close FROM prices WHERE perp=?", (perp,)).fetchall()
    for mkt, t, c in sorted(rows, key=lambda r: r[0].endswith("_4h"), reverse=True):
        kind, four = mkt.split("_")[0], mkt.endswith("_4h")
        px[kind][t + (4 if four else 1) * HOUR_MS] = c        # le 1h écrase le 4h
    start = max(min(fund), min(px["spot"]), min(px["perp"]))
    end = min(max(fund), max(px["spot"]), max(px["perp"]))
    grid = np.arange(start, end + 1, HOUR_MS)

    def series(d, fill):
        out, last = [], np.nan
        for t in grid:
            last = d.get(int(t), last if fill else 0.0)
            out.append(last)
        return np.array(out, dtype=float)

    return series(fund, False), series(px["spot"], True), series(px["perp"], True), grid


def high_days_mask(f, grid):
    """Vrai pour chaque heure d'un jour UTC dont le funding moyen dépasse le seuil 'élevé'."""
    day = grid // (24 * HOUR_MS)
    mask = np.zeros(len(f), dtype=bool)
    for d in np.unique(day):
        sel = day == d
        mask[sel] = f[sel].mean() * config.HOURS_PER_YEAR > config.BT_HIGH_APR
    return mask


def initial_buffer(lev, mmr):
    return (1 + 1 / lev) / (1 + mmr) - 1


class Position:
    """Long spot + short perp de même quantité, short en marge isolée."""

    def __init__(self, cash, s, p, mult, lev, fs, fp):
        notional = cash / (1 + 1 / lev + fs + fp)
        self.qty = notional / s
        self.q_perp = self.qty / mult
        self.entry = p
        self.margin = self.q_perp * p / lev
        self.cost = notional * fs + self.q_perp * p * fp
        self.cash = cash - notional - self.margin - self.cost
        self.mult, self.lev = mult, lev

    def perp_equity(self, p):
        return self.margin + self.q_perp * (self.entry - p)

    def equity(self, s, p):
        return self.cash + self.qty * s + self.perp_equity(p)

    def liq_price(self, mmr):
        return (self.margin + self.q_perp * self.entry) / (self.q_perp * (1 + mmr))

    def close_cost(self, s, p, fs, fp):
        return self.qty * s * fs + self.q_perp * p * fp

    def rebalance(self, s, p):
        """Vend du spot + rachète autant de perp, cash en marge : retour au levier initial."""
        fs, fp = REBAL_FEES
        eq = self.equity(s, p)
        unit_cost = s * fs + p / self.mult * fp
        unit_need = s + p / self.mult / self.lev
        q_new = (eq - self.qty * unit_cost) / (unit_need - unit_cost)
        cost = (self.qty - q_new) * unit_cost
        self.qty, self.q_perp = q_new, q_new / self.mult
        self.entry = p
        self.margin = self.q_perp * p / self.lev
        self.cash = eq - cost - self.qty * s - self.margin
        return cost


def run(f, spot, perp, high, mult, lev, mmr, fees, i0, i1, once=False):
    """Règle 72h > 0 / < 0 sur les heures [i0, i1), départ BT_CAPITAL en USDC."""
    fs, fp = fees
    w = config.BT_RULE_WINDOW_H
    signal = np.convolve(f, np.ones(w) / w)[: len(f)]
    signal[: w - 1] = np.nan

    cash = config.BT_CAPITAL
    pos = None
    fee_tot = funding = funding_high = 0.0
    trades = rebalances = liquidations = hours_in = 0
    worst_buffer = np.inf
    pending, entered_at, done = None, -1, False

    for i in range(i0, i1):
        s, p = spot[i], perp[i]
        if pending == "enter":
            pos = Position(cash, s, p, mult, lev, fs, fp)
            fee_tot += pos.cost
            trades += 1
        elif pending == "exit":
            c = pos.close_cost(s, p, fs, fp)
            fee_tot += c
            cash = pos.equity(s, p) - c
            pos, done = None, once
        pending = None

        if pos:
            if entered_at != i:
                pay = f[i] * pos.q_perp * p
                pos.margin += pay
                funding += pay
                funding_high += pay if high[i] else 0.0
                hours_in += 1
            if pos.perp_equity(p) <= mmr * pos.q_perp * p:       # LIQUIDATION
                liquidations += 1
                c = pos.qty * s * REBAL_FEES[0]
                fee_tot += c
                cash = pos.cash + pos.qty * s - c
                pos = None
                continue
            if pos.liq_price(mmr) / p - 1 < config.BT_REBALANCE_BUFFER:
                fee_tot += pos.rebalance(s, p)
                rebalances += 1
            worst_buffer = min(worst_buffer, pos.liq_price(mmr) / p - 1)

        if np.isnan(signal[i]) or i == i1 - 1:
            continue
        if not pos and not done and signal[i] > 0:
            pending, entered_at = "enter", i + 1
        elif pos and signal[i] < 0:
            pending = "exit"

    if pos:
        s, p = spot[i1 - 1], perp[i1 - 1]
        c = pos.close_cost(s, p, fs, fp)
        fee_tot += c
        cash = pos.equity(s, p) - c

    hours = i1 - i0
    net = funding - fee_tot
    return {"net": net, "net_apr": net / config.BT_CAPITAL * config.HOURS_PER_YEAR / hours,
            "gain": cash - config.BT_CAPITAL, "funding": funding, "funding_high": funding_high,
            "fees": fee_tot, "trades": trades, "rebalances": rebalances,
            "liquidations": liquidations, "pct_in": hours_in / hours,
            "worst_buffer": worst_buffer, "hours": hours}


def day(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def main():
    con = storage.connect()
    tokens = [r[0] for r in con.execute(
        "SELECT perp FROM markets WHERE updated_at=(SELECT MAX(updated_at) FROM markets) "
        "ORDER BY perp")]
    mults = dict(con.execute("SELECT perp, price_mult FROM markets"))
    max_lev = max_leverages()

    print(f"Cash-and-carry v3 : regle unique 'en position si funding moyen "
          f"{config.BT_RULE_WINDOW_H}h > 0', capital {config.BT_CAPITAL:.0f} USDC, "
          f"train {config.BT_TRAIN_SHARE:.0%} / test {1 - config.BT_TRAIN_SHARE:.0%} par token")
    print("Couts par execution (spot / perp) : " + " ; ".join(
        f"{k} {a * 100:.3f}% / {b * 100:.3f}%" for k, (a, b) in SCENARIOS.items()))

    # --- Historique + filtre de liquidité (sur le train) ---------------------------
    print()
    print(f"=== Historique et liquidite (|spot/perp - 1| < {config.BT_BASIS_MAX:.0%} "
          f"pendant >= {config.BT_BASIS_MIN_SHARE:.0%} des heures du train) ===")
    print(f"{'Token':<9}{'Debut':>12}{'Jours':>7}{'Train':>7}{'Test':>6}{'Liquide':>9}  ")
    data = {}
    for tok in tokens:
        f, s, p, grid = load_token(con, tok)
        split = int(len(f) * config.BT_TRAIN_SHARE)
        basis = s[:split] * mults[tok] / p[:split] - 1
        share = np.mean(np.abs(basis) < config.BT_BASIS_MAX)
        ok = share >= config.BT_BASIS_MIN_SHARE and len(f) >= config.BT_MIN_DAYS * 24
        print(f"{tok:<9}{day(grid[0]):>12}{len(f) / 24:>7.0f}{split / 24:>7.0f}"
              f"{(len(f) - split) / 24:>6.0f}{share * 100:>8.1f}%  {'garde' if ok else 'EXCLU'}")
        if ok:
            data[tok] = (f, s, p, grid, split, high_days_mask(f, grid))
    con.close()
    kept = list(data)

    def sim(tok, lev, scen, phase, once=False):
        f, s, p, grid, split, high = data[tok]
        mmr = 1 / (2 * max_lev.get(tok, config.BT_DEFAULT_MAX_LEVERAGE))
        i0, i1 = (0, split) if phase == "train" else (split, len(f))
        return run(f, s, p, high, mults[tok], lev, mmr, SCENARIOS[scen], i0, i1, once)

    def feasible(tok, lev):
        ml = max_lev.get(tok, config.BT_DEFAULT_MAX_LEVERAGE)
        return lev <= ml and initial_buffer(lev, 1 / (2 * ml)) > config.BT_REBALANCE_BUFFER * 1.1

    # --- Choix du levier sur le train (commun, plafonné par token si impossible) ---
    print()
    print("=== Choix du levier sur le TRAIN (scenario taker, plafonne au max faisable) ===")
    lev_of = {}
    best = None                                         # (liquidations, -funding net)
    for lev in config.BT_LEVERAGES:
        levs = {t: max(l for l in config.BT_LEVERAGES if l <= lev and feasible(t, l))
                for t in kept}
        rs = [sim(t, levs[t], "taker", "train") for t in kept]
        tot, liq = sum(r["net"] for r in rs), sum(r["liquidations"] for r in rs)
        print(f"  {lev}x : funding net train {tot:+.2f} $, liquidations {liq}")
        if best is None or (liq, -tot) < best:          # d'abord le moins de liquidations
            best, lev_of = (liq, -tot), levs
        for r, t in zip(rs, kept):
            if r["liquidations"]:
                print(f"     ! {t} liquide au moins une fois sur le train a {levs[t]}x")
    print("  -> retenu : " + ", ".join(f"{t} {l}x" for t, l in lev_of.items()))

    # --- Test : par token, 3 scénarios de frais ---------------------------------
    res = {(t, sc, ph): sim(t, lev_of[t], sc, ph)
           for t in kept for sc in SCENARIOS for ph in ("train", "test")}
    once = {(t, sc): sim(t, lev_of[t], sc, "test", once=True) for t in kept for sc in SCENARIOS}

    print()
    print("=== TEST : funding net par token ($, sur 80 USDC) ===")
    head = (f"{'Token':<9}{'Jours':>6}{'Lev':>4}{'Taker':>8}{'Mixte':>8}{'Maker':>8}"
            f"{'%/an mk':>9}{'Fund. $':>9}{'Trades':>7}{'Reeq.':>6}{'Liq.':>5}{'En pos.':>8}"
            f"{'Gain mk $':>10}")
    print(head)
    print("-" * len(head))
    order = sorted(kept, key=lambda t: -res[(t, "maker", "test")]["net"])
    for t in order:
        tk, mx, mk = (res[(t, sc, "test")] for sc in SCENARIOS)
        print(f"{t:<9}{mk['hours'] / 24:>6.0f}{lev_of[t]:>3}x{tk['net']:>8.2f}{mx['net']:>8.2f}"
              f"{mk['net']:>8.2f}{mk['net_apr'] * 100:>8.1f}%{mk['funding']:>9.2f}"
              f"{mk['trades']:>7}{mk['rebalances']:>6}{mk['liquidations']:>5}"
              f"{mk['pct_in'] * 100:>7.0f}%{mk['gain']:>10.2f}")

    print()
    print("=== Totaux (tous tokens retenus) ===")
    print(f"{'Scenario':<10}{'Net train $':>13}{'Net test $':>12}{'Frais test $':>14}"
          f"{'%/an test':>11}{'Test, 1 seule entree $':>24}")
    for sc in SCENARIOS:
        tr = [res[(t, sc, "train")] for t in kept]
        te = [res[(t, sc, "test")] for t in kept]
        apr = np.mean([r["net_apr"] for r in te])
        print(f"{sc:<10}{sum(r['net'] for r in tr):>13.2f}{sum(r['net'] for r in te):>12.2f}"
              f"{sum(r['fees'] for r in te):>14.2f}{apr * 100:>10.1f}%"
              f"{sum(once[(t, sc)]['net'] for t in kept):>24.2f}")
    liq = sum(r["liquidations"] for r in res.values())
    reb = sum(res[(t, "maker", "test")]["rebalances"] for t in kept)
    buf = min(r["worst_buffer"] for r in res.values())
    print(f"Liquidations (train + test, tous scenarios) : {liq}  {'OK' if liq == 0 else '!!'} ; "
          f"reequilibrages test : {reb} ; plus faible hausse restante avant liq. : {buf:.0%}")

    # --- Périodes de funding élevé ------------------------------------------------
    print()
    print(f"=== Jours a funding eleve (moyenne du jour > {config.BT_HIGH_APR:.0%}/an), "
          "historique complet ===")
    print(f"{'Token':<9}{'Jours':>7}{'Eleves':>8}{'% jours':>9}{'% du funding':>14}"
          f"{'APR jours eleves':>18}{'APR autres':>12}{'Test : $ jours eleves':>23}")
    tot_days = tot_high = 0
    for t in order:
        f, _, _, _, _, high = data[t]
        n_days, n_high = len(f) / 24, high.sum() / 24
        share = f[high].sum() / f.sum() if f.sum() else np.nan
        apr_h = f[high].mean() * config.HOURS_PER_YEAR if high.any() else np.nan
        apr_o = f[~high].mean() * config.HOURS_PER_YEAR
        mk = res[(t, "maker", "test")]
        tot_days += n_days
        tot_high += n_high
        print(f"{t:<9}{n_days:>7.0f}{n_high:>8.0f}{n_high / n_days:>9.1%}{share:>14.0%}"
              f"{apr_h:>18.0%}{apr_o:>12.0%}{mk['funding_high']:>12.2f} / {mk['funding']:.2f}")
    print(f"Total : {tot_high:.0f} jours eleves sur {tot_days:.0f} ({tot_high / tot_days:.1%})")

    out = "backtest_results.csv"
    metrics = ["net", "net_apr", "gain", "funding", "funding_high", "fees", "trades",
               "rebalances", "liquidations", "pct_in", "hours"]
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["token", "lev", "scenario", "phase"] + metrics)
        for (t, sc, ph), r in sorted(res.items()):
            w.writerow([t, lev_of[t], sc, ph] + [r[m] for m in metrics])
    print(f"\nDetail : {out}")


if __name__ == "__main__":
    main()
