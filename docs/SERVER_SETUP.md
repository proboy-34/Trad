# Running TradBot 24/7 on a server

Use this when you are ready for the serious 4–8 week paper test. Your PC no
longer needs to stay on.

## 1. Get a server (Ubuntu 22.04 or 24.04)

| Option | Cost | Notes |
|---|---|---|
| **Oracle Cloud "Always Free"** | Free | Needs a card for identity check (many bank **debit** cards work if online/international payments are enabled). Create an **Ampere (ARM)** VM marked *Always Free Eligible*. |
| **Small VPS paid with USDT** | ~$4–6/month | Many VPS providers accept crypto payment, so no card is needed. 1–2 CPU and 2 GB RAM are enough. |

**Important: do NOT choose a USA region.** Binance blocks US servers.
Choose Singapore, Mumbai, Dubai, Frankfurt or similar.
(TradBot also falls back to Binance's public data mirror, but real trading later needs the main API.)

## 2. Connect

On Windows 10/11, open **Command Prompt**:

```bash
ssh ubuntu@YOUR_SERVER_IP
```

(Oracle uses the user `ubuntu` and the SSH key you downloaded: `ssh -i path\to\key ubuntu@IP`.)

## 3. Install

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
```

Copy the project to the server. Because the GitHub repository is private, the simplest way
is to upload the folder from your PC (run this on your PC, not on the server):

```bash
scp -r C:\TradBot ubuntu@YOUR_SERVER_IP:~/TradBot
```

Then on the server:

```bash
cd ~/TradBot && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python main.py check
```

> If `lightgbm` fails to install on the server, set `ml: enabled: false` in
> `config.yaml`. The bot then works without the ML filter.

## 4. Run it as a service (starts automatically, restarts after crashes)

```bash
sudo tee /etc/systemd/system/tradbot.service > /dev/null <<'EOF'
[Unit]
Description=TradBot paper trading
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/TradBot
ExecStart=/home/ubuntu/TradBot/.venv/bin/python main.py run --no-browser
Restart=always
RestartSec=20

[Install]
WantedBy=multi-user.target
EOF
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now tradbot
```

Check that it's running:

```bash
sudo systemctl status tradbot
```

## 5. Open the dashboard safely

The dashboard only listens on the server itself (127.0.0.1), so nobody on the
internet can reach it. To view it from your PC, open an SSH "tunnel":

```bash
ssh -L 8050:127.0.0.1:8050 ubuntu@YOUR_SERVER_IP
```

Keep that window open and visit <http://127.0.0.1:8050> in your browser.

> Do **not** change `dashboard.host` to `0.0.0.0`. That would expose the
> dashboard (and its controls) to the whole internet.

## 6. Useful commands

| What | Command |
|---|---|
| See live log | `journalctl -u tradbot -f` |
| Restart after changing config | `sudo systemctl restart tradbot` |
| Stop | `sudo systemctl stop tradbot` |
| Update code | upload the new files, then restart |
