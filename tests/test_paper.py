"""Tests hors ligne du bot de paper trading (marché simulé, aucune connexion).

Usage :  python -m pytest tests -q     (ou : python tests/test_paper.py)
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from paper.account import Account  # noqa: E402
from paper.bot import Bot, TokenBot  # noqa: E402
from paper.market import Book  # noqa: E402
from paper.store import Store  # noqa: E402

HOUR = 3600


class FakeMarket:
    def __init__(self):
        self.books = {"HYPE": Book(), "@107": Book()}
        self.on_error = None

    def book(self, coin):
        return self.books[coin]

    def set(self, coin, bid, ask, now, mark=None):
        b = self.books[coin]
        b.bid, b.ask, b.bbo_ts = bid, ask, now
        if mark is not None:
            b.mark = b.oracle = mark
            b.ctx_ts = now


class FakeNotify:
    enabled = False

    def __init__(self):
        self.msgs = []

    def send(self, text, key=None, min_interval_s=0):
        self.msgs.append(text)


SPEC = {"perp": "HYPE", "spot_coin": "@107", "mult": 1.0, "lot_decimals": 2, "mmr": 0.05}


def make(rng=lambda: 1.0):
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    store, market, notify = Store(db), FakeMarket(), FakeNotify()
    tb = TokenBot(SPEC, Account("HYPE", 40.0, 40.0), store, market, notify, rng=rng)
    return tb, store, market, notify


def prices(market, now, px, spread=0.01):
    market.set("@107", px - spread, px + spread, now)
    market.set("HYPE", px - spread, px + spread, now, mark=px)


def enter(tb, market, now=1000.0, px=100.0):
    prices(market, now, px)
    tb.signal = True
    tb.tick(now)                                   # pose l'ordre maker au bid
    assert tb.order and tb.order["side"] == "buy"
    market.books["@107"].trades.append((now + 1, px - 0.05))   # trade sous notre prix
    prices(market, now + 1, px)
    tb.tick(now + 1)                               # rempli -> hedge perp
    return now + 1


def test_entry_mixed_and_hedged():
    tb, store, market, notify = make()
    enter(tb, market)
    acc = tb.acc
    assert acc.spot_qty > 0 and abs(acc.spot_qty - acc.perp_size) < 1e-9
    assert acc.trades == 1 and tb.phase() == "EN POSITION"
    liq = store.con.execute("SELECT liquidity, leg FROM fills ORDER BY id").fetchall()
    assert liq == [("maker", "spot"), ("taker", "perp")]
    assert any(m.startswith("ENTREE") for m in notify.msgs)
    # 2x : ~26.6 $ de notionnel pour 40 USDC
    assert 25 < acc.spot_qty * 100 < 27.5
    assert acc.cash > -0.01


def test_touch_does_not_fill():
    tb, _, market, _ = make()
    prices(market, 1000, 100.0)
    tb.signal = True
    tb.tick(1000)
    market.books["@107"].trades.append((1001, tb.order["px"]))  # trade AU prix : pas rempli
    tb.tick(1001)
    assert tb.order is not None and tb.acc.spot_qty == 0


def test_leg_failure_reverts_spot():
    fail = [True]
    tb, store, market, notify = make(rng=lambda: 0.0 if fail[0] else 1.0)
    config.PAPER_SIM_TAKER_FAIL_PROB, old = 0.5, config.PAPER_SIM_TAKER_FAIL_PROB
    try:
        prices(market, 1000, 100.0)
        tb.signal = True
        tb.tick(1000)
        market.books["@107"].trades.append((1001, 99.9))
        tb.tick(1001)                              # spot rempli, perp refusé
        assert tb.acc.spot_qty > 0 and tb.acc.perp_size == 0
        assert tb.phase() == "JAMBE SEULE"
        for t in range(1002, 1030):                # retente chaque seconde, toujours refusé
            prices(market, t, 100.0)
            tb.tick(t)
        assert tb.acc.spot_qty > 0                 # pas encore annulé (< 30 s)
        fail[0] = False                            # le spot taker passe à nouveau...
        config.PAPER_SIM_TAKER_FAIL_PROB = 0.0     # ...et le perp aussi : couverture
        prices(market, 1031, 100.0)
        tb.tick(1031)
        assert tb.phase() == "EN POSITION" and abs(tb.delta()) < 1e-9
        # Second scénario : perp refusé assez longtemps -> spot revendu
        tb2, store2, market2, notify2 = make(rng=lambda: 0.0)
        config.PAPER_SIM_TAKER_FAIL_PROB = 0.5
        prices(market2, 1000, 100.0)
        tb2.signal = True
        tb2.tick(1000)
        market2.books["@107"].trades.append((1001, 99.9))
        tb2.tick(1001)
        # le revert spot passe par taker() aussi : on ne fait échouer que le perp
        orig = tb2.taker

        def taker(leg, side, qty, reason, now):
            if leg == "perp":
                return False
            config.PAPER_SIM_TAKER_FAIL_PROB = 0.0
            ok = orig(leg, side, qty, reason, now)
            config.PAPER_SIM_TAKER_FAIL_PROB = 0.5
            return ok
        tb2.taker = taker
        for t in range(1002, 1040):
            prices(market2, t, 100.0)
            tb2.tick(t)
        assert tb2.acc.is_flat() and tb2.leg_since is None
        assert any("jambe spot annulee" in m for m in notify2.msgs)
        kinds = [k for (k,) in store2.con.execute("SELECT kind FROM events")]
        assert "leg_reverted" in kinds
    finally:
        config.PAPER_SIM_TAKER_FAIL_PROB = old


def test_unhedgeable_leg_triggers_kill():
    tb, store, market, notify = make()
    enter(tb, market)
    tb.taker = lambda *a, **k: False               # plus rien ne s'exécute
    tb.acc.spot_qty += 0.1                          # déséquilibre artificiel
    for t in range(2000, 2200):
        tb.tick(float(t))
    assert tb.halted and any(m.startswith("KILL") for m in notify.msgs)


def test_rebalance_restores_buffer_and_stays_hedged():
    tb, store, market, notify = make()
    now = enter(tb, market)
    d0 = tb.acc.liq_distance(100.0, tb.mmr)
    assert d0 > 0.40                               # 2x, mmr 5 % : ~+43 %
    px = 100.0 * (1 + d0) / 1.2                    # hausse : il reste < 25 %
    prices(market, now + 60, px)
    tb.risk_check(now + 60)
    assert tb.acc.rebalances == 1
    assert abs(tb.delta()) < 1e-9                  # toujours neutre
    d1 = tb.acc.liq_distance(px, tb.mmr)
    assert d1 > 0.35, d1                           # revenu près du levier de départ
    assert any(m.startswith("REEQUILIBRAGE") for m in notify.msgs)
    fills = store.con.execute("SELECT leg, side, reason FROM fills WHERE reason LIKE "
                              "'rebalance%'").fetchall()
    assert fills == [("spot", "sell", "rebalance"), ("perp", "buy", "rebalance_hedge")]


def test_exit_and_pnl_accounting():
    tb, store, market, notify = make()
    now = enter(tb, market)
    tb.signal = False
    prices(market, now + 10, 100.0)
    tb.tick(now + 10)
    assert tb.order["side"] == "sell"
    market.books["@107"].trades.append((now + 11, 100.1))
    tb.tick(now + 11)
    assert tb.acc.is_flat() and tb.phase() == "USDC"
    pnl = tb.acc.cash - 40.0
    # prix inchangé : on perd frais + slippage + spread, ~0.1 $
    assert -0.2 < pnl < 0, pnl
    assert abs(pnl + tb.acc.fees_cum) < 0.05
    assert any(m.startswith("SORTIE") for m in notify.msgs)


def test_funding_credited_once_and_signal():
    tb, store, market, notify = make()
    now = enter(tb, market, now=10 * HOUR + 100)
    size = tb.acc.perp_size
    tb.tick(11 * HOUR + 1)                          # passage de l'heure : photo
    hour_ms = 11 * HOUR * 1000
    rows = [{"time": hour_ms - k * HOUR * 1000 + 58, "fundingRate": "0.0000125"}
            for k in range(72)]
    tb.on_funding(rows, 11 * HOUR + 40)
    expected = 0.0000125 * size * 100.0
    assert abs(tb.acc.funding_cum - expected) < 1e-9
    tb.on_funding(rows, 11 * HOUR + 50)             # même paiement relu : pas de double
    assert abs(tb.acc.funding_cum - expected) < 1e-9
    assert tb.signal is True
    neg = [{"time": r["time"], "fundingRate": "-0.00001"} for r in rows]
    tb.on_funding(neg, 11 * HOUR + 60)
    assert tb.signal is False


def test_kill_flattens_and_persists():
    tb, store, market, notify = make()
    now = enter(tb, market)
    bot = Bot([tb], store, market, notify, fetch_funding=lambda *a: [])
    store.put("kill_request", "test")
    bot.check_kill_requests(now)
    assert tb.halted and tb.acc.is_flat()
    bot.save()
    tb2 = TokenBot(SPEC, Account("HYPE", 40.0, 40.0), store, market, notify)
    tb2.load_state(store.get("token:HYPE"))
    assert tb2.halted and tb2.acc.is_flat()
    tb2.signal = True
    tb2.tick(now + 100)
    assert tb2.order is None                        # arrêté : ne rentre plus
    store.put("resume_request", True)
    bot2 = Bot([tb2], store, market, notify, fetch_funding=lambda *a: [])
    bot2.check_kill_requests(now + 100)
    assert not tb2.halted


def test_global_loss_kill():
    tb, store, market, notify = make()
    now = enter(tb, market)
    bot = Bot([tb], store, market, notify, fetch_funding=lambda *a: [])
    tb.acc.cash -= 5.0                              # perte simulée de 12.5 %
    prices(market, now + 1, 100.0)
    bot.check_global_loss(now + 1)
    assert tb.halted


def test_no_false_loss_kill_without_prices():
    tb, store, market, notify = make()
    enter(tb, market)
    market.books["@107"].bid = market.books["@107"].ask = None   # flux pas encore arrivé
    bot = Bot([tb], store, market, notify, fetch_funding=lambda *a: [])
    bot.check_global_loss(2000)
    assert not tb.halted


def test_stale_prices_block_taker():
    tb, _, market, _ = make()
    prices(market, 1000, 100.0)
    assert tb.taker("perp", "sell", 0.1, "t", 1000 + config.PAPER_STALE_S + 1) is False


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} tests OK")
