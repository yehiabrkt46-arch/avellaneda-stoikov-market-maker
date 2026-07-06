# VPS deploy (milestone 4)

One-time setup on the VPS (Ubuntu assumed, pm2 already installed for CSAlpha):

    sudo mkdir -p /opt/mm-bot && sudo chown $USER /opt/mm-bot
    git clone <repo-or-rsync-from-dev-machine> /opt/mm-bot
    cd /opt/mm-bot
    python3.12 -m venv .venv          # python3 --version must be >= 3.12
    .venv/bin/pip install -e .
    mkdir -p logs data
    .venv/bin/python -m pytest -q     # all green before starting

Start the 7-day dual-strategy run:

    pm2 start deploy/ecosystem.config.js
    pm2 save
    pm2 logs mm-bot-paper --lines 20  # expect per-strategy stats lines each minute

Daily health check:

    sqlite3 data/mm.sqlite "SELECT strategy, MAX(ts_ms), COUNT(*) FROM rollups GROUP BY strategy;"
    pm2 status                        # restarts count = disclosed downtime events

Stop at the end of the measurement window:

    pm2 stop mm-bot-paper

Notes:
- data/mm.sqlite and data/raw-*.jsonl grow ~50-100 MB/day combined; ensure
  a few GB free.
- Every pm2 restart starts a new session row (new session_id); the write-up
  must disclose restart count and gaps (query the sessions table).
- Do NOT run the testnet demo on the VPS; it is a local supervised script.
