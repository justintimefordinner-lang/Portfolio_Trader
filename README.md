# Portfolio Trader

Companion to the [Portfolio dashboard](https://github.com/justintimefordinner-lang/Trading_Dashboard_App). Stage 1: it turns the
dashboard's quant rules into trade **suggestions** and pushes them to your phone with [ntfy](https://ntfy.sh). It places no orders and
holds no broker credentials.

What it suggests:

- short puts to close once 50% of the credit is captured — checked every 15 minutes from 11:00 ET (the backtest managed its open puts in its 11:00 run) to the close;
- at the top of each entry hour (11:00 to 15:00 ET, i.e. 9:00 to 1:00 Mountain): new cash-secured puts for every name the Quant
  scan qualifies, sized to the account; covered calls on 100+ share lots with no call on; notes when a name is over its cap or
  collateral exceeds cash. When a slot opens the trader asks the bridge for a fresh scan and waits for it.

Every account the dashboard shows gets its own run — each Schwab login, imported or hand-entered accounts — and each notification
names the account it is for.

The dashboard's Trader page has a **Run now** button for a full pass any time, any day, against the scan on disk.

**Auto Trader (paper).** The same rules are also traded, without asking, in a manual account named Auto Trader funded with
`PAPER_CASH` (default $400,000). New puts and calls are booked as positions, closes at 50% and expiries (assignment, called away,
expired) are settled with the bridge's prices, and each round-trip lands in the account's realized P&L, so the dashboard shows the
strategy's paper record under that account. Entries follow the same hourly slots (one new put per name per day) or Run now.
`TRADER_PAPER=0` turns it off.

Each suggestion is pushed once (again after a day, or if the price moves 15%), and logged to `data/trade-suggestions.json`, which the
dashboard's Trader page shows so you can mark each one good, bad or done.

## Install (alongside the dashboard stack)

The image is published to GHCR on every push, for amd64 and arm64, so the stack pulls it like the dashboard and bridge. In the
stack folder on the host, once:

```bash
cd ~/portfolio-manager && curl -fsSL https://raw.githubusercontent.com/justintimefordinner-lang/Portfolio_Trader/main/docker-compose.trader.yml -o docker-compose.trader.yml && mkdir -p trader-state bridge-state/task_inbox && { [ -f trader-state/.env ] || curl -fsSL https://raw.githubusercontent.com/justintimefordinner-lang/Portfolio_Trader/main/.env.example -o trader-state/.env; } && { grep -q "^COMPOSE_FILE=" .env || echo "COMPOSE_FILE=docker-compose.yml:docker-compose.trader.yml" >> .env; }
```

Edit `trader-state/.env`: set `NTFY_TOPIC` to a long random name and `APP_URL` to how you reach the dashboard. Then start:

```bash
cd ~/portfolio-manager && docker compose pull trader && docker compose up -d trader && docker compose logs --tail 5 trader
```

Updates: Settings → Update now in the dashboard pulls the newest trader image along with the others.

On your phone, install the ntfy app and subscribe to the same topic. Pause pushes any time with `touch trader-state/paused`.
