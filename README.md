# Portfolio Trader

Companion to the [Portfolio dashboard](https://github.com/justintimefordinner-lang/Trading_Dashboard_App). Stage 1: it turns the
dashboard's quant rules into trade **suggestions** and pushes them to your phone with [ntfy](https://ntfy.sh). It places no orders and
holds no broker credentials.

What it suggests, every 15 minutes during the session:

- new cash-secured puts from the Quant scan, sized to the account and ranked by the Morning Brief's score;
- short puts to close once 50% of the credit is captured;
- covered calls on 100+ share lots with no call on;
- notes when a name is over its cap or collateral exceeds cash.

Each is pushed once (again after a day, or if the price moves 15%), and logged to `data/trade-suggestions.json`, which the dashboard's
Trader page shows so you can mark each one good, bad or done.

## Install (alongside the dashboard stack)

While this repo is private there is no published image, so the Pi builds one from a copy of this folder. From the
machine that has the repo, copy it into the stack folder (re-run this to update):

```bash
scp -r /path/to/Portfolio_Trader <user>@<your-pi>:~/portfolio-manager/trader
```

On the Pi, once:

```bash
cd ~/portfolio-manager && cp trader/docker-compose.trader.yml . && mkdir -p trader-state && cp -n trader/.env.example trader-state/.env && grep -q "^COMPOSE_FILE=" .env || echo "COMPOSE_FILE=docker-compose.yml:docker-compose.trader.yml" >> .env
```

Edit `trader-state/.env`: set `NTFY_TOPIC` to a long random name and `APP_URL` to how you reach the dashboard. Then build and start:

```bash
cd ~/portfolio-manager && docker compose up -d --build trader && docker compose logs --tail 5 trader
```

After each update: scp again, then the same `up -d --build trader`.

On your phone, install the ntfy app and subscribe to the same topic. Pause pushes any time with `touch trader-state/paused`.
