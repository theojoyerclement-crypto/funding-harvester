# Bot de paper trading : déploiement sur VPS

Aucun ordre réel n'est envoyé : le bot lit le marché en direct (WebSocket Hyperliquid)
et simule ses fills. Aucune clé API Hyperliquid n'est nécessaire.

## Installation (Debian / Ubuntu)

```bash
# depuis ton PC : copier le projet sur le serveur
scp -r "funding harvester" user@vps:~/funding-harvester
# sur le serveur
cd ~/funding-harvester
sudo bash deploy/install.sh
sudo nano /opt/funding-harvester/.env      # token + chat id Telegram
sudo systemctl restart funding-paper
```

Le service redémarre tout seul en cas de crash (10 s plus tard) et au reboot du VPS.
L'état (positions, ordres, funding déjà crédité) est dans `paper.db` : un redémarrage
reprend exactement où le bot s'était arrêté.

## Telegram

1. Créer un bot avec @BotFather, récupérer le token.
2. Envoyer un message au bot, puis ouvrir `https://api.telegram.org/bot<TOKEN>/getUpdates`
   et lire `chat.id`.
3. Mettre les deux valeurs dans `.env`.

Alertes : démarrage, ENTREE, SORTIE, REEQUILIBRAGE, jambe en attente ou annulée, KILL,
erreurs (une seule alerte par type toutes les 10 min).
Commandes acceptées (uniquement depuis ce chat) : `/status`, `/kill`.

## Kill switch

N'importe lequel de ces moyens ferme les deux jambes en taker et arrête le trading :

| Moyen | Commande |
|---|---|
| Telegram | `/kill` |
| Ligne de commande | `sudo -u funding /opt/funding-harvester/.venv/bin/python /opt/funding-harvester/paper_bot.py --kill` |
| Fichier | `sudo -u funding touch /opt/funding-harvester/KILL` |
| Automatique | perte totale > 10 % du capital, liquidation, jambe non couverte > 2 min |

L'arrêt survit aux redémarrages. Pour reprendre : supprimer `KILL` s'il existe, puis
`... paper_bot.py --resume`.

## Suivi

```bash
journalctl -u funding-paper -f                         # logs en direct
sudo -u funding /opt/funding-harvester/.venv/bin/python /opt/funding-harvester/paper_bot.py --status
sqlite3 /opt/funding-harvester/paper.db "SELECT token, SUM(amount) FROM funding GROUP BY token"
```

Tables de `paper.db` : `fills` (chaque exécution, maker/taker, frais), `funding`
(chaque paiement horaire), `events` (entrées, sorties, rééquilibrages, jambes, erreurs,
kill), `equity` (photo toutes les 5 min : cash, spot, marge, PnL, distance de liquidation),
`state` (état courant).
