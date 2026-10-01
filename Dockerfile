# Portfolio Trader — stage 1: suggestions pushed to your phone. No broker client.
# Code at /opt/trader; /state (working directory) holds .env, sent.json and the
# pause marker; the dashboard's data folder is mounted at /app/data.
FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/justintimefordinner-lang/Portfolio_Trader"
LABEL org.opencontainers.image.description="Trade suggestions from the Portfolio dashboard's quant rules, pushed via ntfy"
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 TRADER_STATE_DIR=/state APP_DATA_DIR=/app/data
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm -f /tmp/requirements.txt
COPY . /opt/trader
RUN rm -f /opt/trader/.env /opt/trader/sent.json && ln -s /state/.env /opt/trader/.env && mkdir -p /state /app/data
WORKDIR /state
CMD ["python", "/opt/trader/trader.py"]
