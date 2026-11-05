"""Configuration du projet funding-harvester (étape 1 : collecteur)."""

# --- API publique Hyperliquid (aucune clé nécessaire) -----------------------
API_URL = "https://api.hyperliquid.xyz/info"

# --- Collecte ---------------------------------------------------------------
HISTORY_DAYS = 800         # profondeur d'historique visée (l'API s'arrête à la création du marché)
# Bougies : l'API ne garde que les 5000 plus récentes par intervalle.
#   1h -> ~208 jours ; 4h -> ~833 jours. On stocke les deux, le backtest prend
#   la 1h quand elle existe et la 4h (dernière clôture connue) avant.
DB_PATH = "funding.db"     # fichier SQLite (créé à côté des scripts)
REQUEST_PAUSE_S = 0.5    # pause entre requêtes pour rester sous le rate limit

# Hyperliquid paie le funding toutes les heures : taux horaire -> annuel
HOURS_PER_YEAR = 24 * 365

# --- Correspondance spot -> perp ----------------------------------------------
# La plupart des spots "majeurs" sont des tokens wrappés (Unit : UBTC, UETH...,
# HyBridge/LayerZero : LINK0, AVAX0...). collector.py gère ces cas
# automatiquement ; les exceptions sont listées ici :
#   nom du token spot -> (nom du perp, multiplicateur de prix)
# Le multiplicateur sert quand le perp est coté "en milliers" (kBONK = 1000 BONK).
SPOT_ALIASES = {
    "UFART": ("FARTCOIN", 1),
    "UUUSPX": ("SPX", 1),
    "UBONK": ("kBONK", 1000),
    "UDZ": ("2Z", 1),
    "UVIRT": ("VIRTUAL", 1),
}

# Appariement refusé si |prix spot x mult / prix perp - 1| dépasse ce seuil
MAX_PRICE_GAP = 0.05

# --- Frais de trading (palier 0, sans staking) --------------------------------
# Source : https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
# Vérifié le 2026-11-04.
PERP_TAKER_FEE = 0.00045   # 0.045 %
PERP_MAKER_FEE = 0.00015   # 0.015 %
SPOT_TAKER_FEE = 0.00070   # 0.070 %
SPOT_MAKER_FEE = 0.00040   # 0.040 %
# Notes de la doc :
# - Palier calculé sur le volume 14 j : perp + 2 x spot (palier 1 dès > 5 M$).
# - Réduction staking HYPE : de 5 % (>10 HYPE) à 40 % (>500k HYPE).
# - Rebates maker : -0.001 % à -0.003 % selon la part du volume maker.
# - Quote "alignée" (ex. USDH) : taker -20 %, rebates maker +50 %.
#
# Coût aller-retour d'un cash-and-carry (achat spot + short perp, puis sortie),
# en taker des deux côtés : 2 x (0.070 % + 0.045 %) = 0.23 % du notionnel.
ROUND_TRIP_TAKER = 2 * (SPOT_TAKER_FEE + PERP_TAKER_FEE)

# --- Backtest (étape 2) -------------------------------------------------------
BT_CAPITAL = 80.0              # USDC de départ
BT_SLIPPAGE = 0.0005           # 0.05 % par jambe et par exécution (en plus des frais)
BT_SPOT_FEE = SPOT_TAKER_FEE   # on suppose des ordres au marché (taker) : prudent
BT_PERP_FEE = PERP_TAKER_FEE
BT_LEVERAGES = [1, 2, 3]       # levier du short perp (marge isolée, pas de rééquilibrage)
BT_WINDOWS_H = [8, 24, 72]     # N : fenêtre de moyenne du funding (heures)
# Seuils en funding annualisé. Attention : le taux "plancher" de Hyperliquid
# (prime nulle) vaut 0.00125 %/h = ~11 % annualisé.
BT_ENTRY_APR = [0.10, 0.15, 0.25, 0.40]
BT_EXIT_RATIOS = [0.0, 0.5]    # seuil de sortie = ratio x seuil d'entrée (0 = sortie si négatif)
# Levier max si l'API est injoignable (marge de maintenance = 1 / (2 x levier max))
BT_DEFAULT_MAX_LEVERAGE = 3
# Liquidité : paire gardée si |spot/perp - 1| < 1 % pendant au moins 90 % des heures
BT_BASIS_MAX = 0.01
BT_BASIS_MIN_SHARE = 0.90
# Rééquilibrage si la hausse restante avant liquidation passe sous 25 %
BT_REBALANCE_BUFFER = 0.25
# Anti-illusion : réglages choisis avant, testés sur les 30 derniers jours
BT_TEST_DAYS = 30             # (v2, plus utilisé en v3)
# --- v3 ---
BT_TRAIN_SHARE = 2 / 3        # par token : 2/3 de l'historique en train, 1/3 en test
BT_RULE_WINDOW_H = 72         # règle unique : en position si funding moyen 72h > 0
BT_HIGH_APR = 0.25            # "funding élevé" : moyenne du jour > 25 %/an
BT_MIN_DAYS = 90              # historique minimum pour garder un token

# --- Paper trading (étape 3) ----------------------------------------------------
WS_URL = "wss://api.hyperliquid.xyz/ws"
PAPER_DB_PATH = "paper.db"
PAPER_TOKENS = {"HYPE": 40.0, "FARTCOIN": 40.0}   # perp -> capital USDC alloué
PAPER_LEVERAGE = 2
PAPER_SIGNAL_WINDOW_H = 72         # en position si funding moyen 72h > 0
PAPER_REBALANCE_BUFFER = 0.25      # rééquilibrage si hausse restante avant liq. < 25 %
PAPER_RISK_CHECK_S = 60            # vérification du risque chaque minute
PAPER_SNAPSHOT_S = 300             # photo de l'équité en base toutes les 5 min
PAPER_REPRICE_S = 30               # ordre maker repositionné si le meilleur prix s'éloigne
PAPER_EXIT_MAKER_TIMEOUT_S = 900   # sortie maker non exécutée après 15 min -> taker
PAPER_LEG_TIMEOUT_S = 30           # une jambe seule plus de 30 s -> on annule la 1re jambe
PAPER_STALE_S = 10                 # prix plus vieux que 10 s = inutilisable pour un taker
PAPER_STALE_ALERT_S = 300          # alerte si plus de données depuis 5 min
PAPER_MIN_ORDER_USD = 10.0         # minimum Hyperliquid par ordre
PAPER_MAX_LOSS_PCT = 0.10          # kill switch auto si perte totale > 10 % du capital
PAPER_SIM_TAKER_FAIL_PROB = 0.0    # > 0 pour tester la gestion "une jambe sans l'autre"
PAPER_KILL_FILE = "KILL"           # créer ce fichier = kill switch
