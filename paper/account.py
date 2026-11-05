"""Comptabilité d'un token : USDC libre, spot détenu, short perp en marge isolée."""

from dataclasses import asdict, dataclass

EPS = 1e-12


@dataclass
class Account:
    token: str
    capital: float            # USDC alloué au départ
    cash: float               # USDC libre
    spot_qty: float = 0.0     # tokens spot détenus
    perp_size: float = 0.0    # taille du short (positive)
    perp_entry: float = 0.0   # prix d'entrée moyen du short
    margin: float = 0.0       # marge isolée (inclut funding reçu et PnL réalisé)
    funding_cum: float = 0.0
    fees_cum: float = 0.0
    trades: int = 0           # entrées complètes
    rebalances: int = 0

    # --- lecture -------------------------------------------------------------
    def perp_equity(self, mark):
        return self.margin + self.perp_size * (self.perp_entry - mark)

    def equity(self, spot_px, mark):
        return self.cash + self.spot_qty * spot_px + self.perp_equity(mark)

    def liq_price(self, mmr):
        """Prix du perp qui liquide le short (marge isolée), None si pas de short."""
        if self.perp_size <= EPS:
            return None
        return (self.margin + self.perp_size * self.perp_entry) / (self.perp_size * (1 + mmr))

    def liq_distance(self, mark, mmr):
        """Hausse restante avant liquidation (0.25 = +25 %), None si pas de short."""
        liq = self.liq_price(mmr)
        return None if liq is None else liq / mark - 1

    def is_flat(self):
        return self.spot_qty <= EPS and self.perp_size <= EPS

    # --- mouvements (chaque méthode renvoie les frais payés) -------------------
    def buy_spot(self, qty, px, fee_rate):
        fee = qty * px * fee_rate
        self.cash -= qty * px + fee
        self.spot_qty += qty
        return self._fee(fee)

    def sell_spot(self, qty, px, fee_rate):
        qty = min(qty, self.spot_qty)
        fee = qty * px * fee_rate
        self.cash += qty * px - fee
        self.spot_qty -= qty
        if self.spot_qty < EPS:
            self.spot_qty = 0.0
        return self._fee(fee)

    def open_short(self, qty, px, fee_rate, lev):
        fee = qty * px * fee_rate
        new_size = self.perp_size + qty
        self.perp_entry = (self.perp_size * self.perp_entry + qty * px) / new_size
        self.perp_size = new_size
        add = qty * px / lev
        self.margin += add
        self.cash -= add + fee
        return self._fee(fee)

    def close_short(self, qty, px, fee_rate):
        qty = min(qty, self.perp_size)
        fee = qty * px * fee_rate
        frac = qty / self.perp_size
        release = self.margin * frac
        realized = qty * (self.perp_entry - px)
        self.margin -= release
        self.perp_size -= qty
        self.cash += release + realized - fee
        if self.perp_size < EPS:                 # position fermée : toute la marge revient
            self.perp_size, self.perp_entry = 0.0, 0.0
            self.cash += self.margin
            self.margin = 0.0
        return self._fee(fee)

    def add_margin(self, amount):
        amount = max(0.0, min(amount, self.cash))
        self.cash -= amount
        self.margin += amount
        return amount

    def credit_funding(self, amount):
        self.margin += amount                    # Hyperliquid verse le funding sur la marge
        self.funding_cum += amount

    def _fee(self, fee):
        self.fees_cum += fee
        return fee

    # --- persistance -----------------------------------------------------------
    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
