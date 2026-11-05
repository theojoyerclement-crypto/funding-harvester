"""Stockage SQLite. Les clés primaires empêchent les doublons entre deux lancements."""

import sqlite3

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    perp        TEXT PRIMARY KEY,   -- nom du perp (ex. BTC)
    spot_coin   TEXT NOT NULL,      -- identifiant API de la paire spot (ex. @142)
    spot_base   TEXT NOT NULL,      -- token spot (ex. UBTC)
    spot_quote  TEXT NOT NULL,      -- quote (ex. USDC)
    price_mult  REAL NOT NULL,      -- prix perp = prix spot x price_mult
    updated_at  INTEGER NOT NULL    -- ms
);

CREATE TABLE IF NOT EXISTS funding (
    perp     TEXT NOT NULL,
    time     INTEGER NOT NULL,      -- ms, horodatage du paiement
    rate     REAL NOT NULL,         -- taux horaire (0.0001 = 0.01 %/h)
    premium  REAL,
    PRIMARY KEY (perp, time)
);

CREATE TABLE IF NOT EXISTS prices (
    perp     TEXT NOT NULL,
    market   TEXT NOT NULL,         -- 'perp'/'spot' (1h) ou 'perp_4h'/'spot_4h'
    time     INTEGER NOT NULL,      -- ms, ouverture de la bougie
    close    REAL NOT NULL,
    volume   REAL,
    PRIMARY KEY (perp, market, time)
);
"""


def connect():
    con = sqlite3.connect(config.DB_PATH)
    con.executescript(SCHEMA)
    return con


def upsert_market(con, m, now_ms):
    # Si la paire spot retenue a changé (liquidité déplacée), ses anciens prix ne valent plus rien
    row = con.execute("SELECT spot_coin FROM markets WHERE perp=?", (m["perp"],)).fetchone()
    if row and row[0] != m["spot_coin"]:
        con.execute("DELETE FROM prices WHERE perp=? AND market LIKE 'spot%'", (m["perp"],))
    con.execute(
        "INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?,?)",
        (m["perp"], m["spot_coin"], m["spot_base"], m["spot_quote"], m["price_mult"], now_ms),
    )


def last_time(con, table, perp, market=None):
    """Dernier horodatage stocké (None si rien) : sert à reprendre la collecte."""
    if market:
        row = con.execute(f"SELECT MAX(time) FROM {table} WHERE perp=? AND market=?",
                          (perp, market)).fetchone()
    else:
        row = con.execute(f"SELECT MAX(time) FROM {table} WHERE perp=?", (perp,)).fetchone()
    return row[0]


def first_time(con, perp):
    """Premier horodatage de funding stocké (None si rien) : sert à compléter vers le passé."""
    return con.execute("SELECT MIN(time) FROM funding WHERE perp=?", (perp,)).fetchone()[0]


def insert_funding(con, perp, rows):
    # INSERT OR IGNORE : un point déjà présent n'est jamais dupliqué
    con.executemany(
        "INSERT OR IGNORE INTO funding VALUES (?,?,?,?)",
        [(perp, r["time"], float(r["fundingRate"]), float(r["premium"])) for r in rows],
    )


def insert_candles(con, perp, market, rows):
    # INSERT OR REPLACE : la dernière bougie (encore ouverte) est mise à jour au lancement suivant
    con.executemany(
        "INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?)",
        [(perp, market, c["t"], float(c["c"]), float(c["v"])) for c in rows],
    )


def summary(con, since_ms):
    """Funding moyen annualisé et % d'heures positives depuis since_ms."""
    return con.execute(
        """
        SELECT f.perp, m.spot_base, m.spot_quote,
               COUNT(*)                         AS n_hours,
               AVG(f.rate) * ?                  AS apr,
               100.0 * AVG(f.rate > 0)          AS pct_pos
        FROM funding f JOIN markets m ON m.perp = f.perp
        WHERE f.time >= ?
          -- uniquement les marchés retenus au dernier lancement
          AND m.updated_at = (SELECT MAX(updated_at) FROM markets)
        GROUP BY f.perp
        ORDER BY apr DESC
        """,
        (config.HOURS_PER_YEAR, since_ms),
    ).fetchall()
