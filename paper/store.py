"""Journal SQLite du paper trading : événements, fills, funding, équité, état du bot."""

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    token   TEXT,
    kind    TEXT NOT NULL,     -- entry, exit, rebalance, leg, error, kill, liquidation...
    detail  TEXT               -- JSON
);
CREATE TABLE IF NOT EXISTS fills (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    token     TEXT NOT NULL,
    leg       TEXT NOT NULL,   -- spot / perp
    side      TEXT NOT NULL,   -- buy / sell
    qty       REAL NOT NULL,
    px        REAL NOT NULL,
    fee       REAL NOT NULL,
    liquidity TEXT NOT NULL,   -- maker / taker
    reason    TEXT
);
CREATE TABLE IF NOT EXISTS funding (
    token   TEXT NOT NULL,
    hour    INTEGER NOT NULL,  -- ms, heure du paiement
    rate    REAL NOT NULL,
    size    REAL NOT NULL,
    px      REAL NOT NULL,
    amount  REAL NOT NULL,     -- USDC reçus (négatif = payés)
    PRIMARY KEY (token, hour)  -- jamais crédité deux fois, même après redémarrage
);
CREATE TABLE IF NOT EXISTS equity (
    ts          REAL NOT NULL,
    token       TEXT NOT NULL,
    cash        REAL, spot_value REAL, perp_equity REAL, equity REAL,
    pnl         REAL, funding_cum REAL, fees_cum REAL, liq_distance REAL
);
CREATE TABLE IF NOT EXISTS state (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL       -- JSON
);
"""


class Store:
    def __init__(self, path):
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.executescript(SCHEMA)
        self.con.commit()

    def event(self, token, kind, **detail):
        self.con.execute("INSERT INTO events (ts, token, kind, detail) VALUES (?,?,?,?)",
                         (time.time(), token, kind, json.dumps(detail, default=str)))
        self.con.commit()

    def fill(self, token, leg, side, qty, px, fee, liquidity, reason):
        self.con.execute(
            "INSERT INTO fills (ts, token, leg, side, qty, px, fee, liquidity, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), token, leg, side, qty, px, fee, liquidity, reason))
        self.con.commit()

    def funding(self, token, hour, rate, size, px, amount):
        """Enregistre un paiement ; renvoie False s'il était déjà compté."""
        cur = self.con.execute("INSERT OR IGNORE INTO funding VALUES (?,?,?,?,?,?)",
                               (token, hour, rate, size, px, amount))
        self.con.commit()
        return cur.rowcount == 1

    def snapshot(self, token, row):
        self.con.execute(
            "INSERT INTO equity VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), token, row["cash"], row["spot_value"], row["perp_equity"],
             row["equity"], row["pnl"], row["funding_cum"], row["fees_cum"], row["liq_distance"]))
        self.con.commit()

    def get(self, key, default=None):
        row = self.con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.con.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))
        self.con.commit()
