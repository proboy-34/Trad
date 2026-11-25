# Running TradBot 24/7 on a server

## Option H: Hugging Face Spaces (free, no card)

Free "CPU basic" Spaces have plenty of power (2 CPU, 16 GB RAM) but two catches,
and TradBot handles both:

| Catch | How TradBot handles it |
|---|---|
| The Space **sleeps after 48 hours without visitors** | Open your dashboard at least once every 2 days (bookmark it on your phone). |
| **Files are deleted** whenever the Space restarts | TradBot **backs itself up every 30 minutes** (and when stopping) to a **private Hugging Face dataset**, and **restores itself** after a restart. |

### One-time setup (about 15 minutes)

1. **Account:** sign up at huggingface.co (free, no card).
2. **Token:** profile picture → **Settings → Access Tokens → Create new token** → type **Write** →
   name it `tradbot` → copy it (keep it secret).
3. **Space:** **New → Space** → name `tradbot` → SDK **Docker** → template **Blank** →
   hardware **CPU basic (free)** → visibility **Private** → **Create Space**.
4. **Secrets:** in the Space → **Settings → Variables and secrets → New secret**, add:
   | Name | Value |
   |---|---|
   | `DASHBOARD_PASSWORD` | a long password you choose (needed, or the dashboard stays closed) |
   | `HF_TOKEN` | the Write token from step 2 (for the automatic backup) |
   | `GEMINI_API_KEY` | your Gemini key (keys typed in the dashboard Settings page are lost on restart) |
5. **Code - automatic way (recommended):** on GitHub open the TradBot repository →
   **Settings → Secrets and variables → Actions**:
   - **Secrets** tab → *New repository secret* → `HF_TOKEN` = the same Write token
   - **Variables** tab → *New repository variable* → `HF_SPACE` = `yourname/tradbot`
   From then on, every TradBot update is copied to your Space automatically
   (GitHub → **Actions** tab shows "Deploy to Hugging Face Space").
   **Code - manual way:** Space → **Files → Add file → Upload files** → upload every file and
   folder of the TradBot ZIP **except** `README.md`, `data` and `.env` (the Space's own README
   must stay). Repeat this for every update.
6. Wait until the Space shows **Running** (first build ~5 minutes), then open
   **https://yourname-tradbot.hf.space** (replace `yourname`), type any user name and your
   `DASHBOARD_PASSWORD`.

### Keep your PC's progress (optional)

Stop the bot on your PC. On Hugging Face: **New → Dataset** → name `tradbot-data` →
**Private**. Upload from your PC's `data` folder: `tradbot.db`, `strategies.json`,
`trade_filter.json`, and the `models` folder. Then restart the Space: it restores them
and continues with all strategies and trades. (Without this it starts fresh and creates
`tradbot-data` by itself on its first backup.)

Run the bot in ONE place only: when the Space runs, don't run it on your PC too.

## Option A: a bot-hosting panel (e.g. Botkeep)

Measured TradBot needs: **~170 MB RAM**, about half of one CPU core while researching,
and ~150 MB disk. That fits free bot plans with **512 MB RAM / 1 GB storage**.

1. Create a **Python** server (Python 3.11-3.13).
2. Get the code in: **Upload a ZIP** of the project from GitHub (Code -> Download ZIP),
   or **Import from GitHub** (the repository is private, so the panel must be connected to
   your GitHub account). Don't upload your PC's `.env` file or `data` folder.
3. Install command (if the panel asks): `pip install -r requirements.txt`
4. Startup command: `python main.py run --server`
5. Environment variable: `DASHBOARD_PASSWORD` = a long password you choose.
   Without it the dashboard refuses to be reachable from the internet (safety).
   The port is taken automatically from the panel's `PORT` / `SERVER_PORT` variable.
6. Start the server, open the address the panel gives you, enter the password
   (any user name), then add your Gemini key in **Settings**.

Notes:
- The `data` folder holds the paper account, strategies and models. Check that the panel
  keeps files between restarts, otherwise the bot starts from zero after each restart.
- If the panel's CPU limit makes things slow, raise `research.pause_seconds` (e.g. 60) in `config.yaml`.
- If the panel's server is in the USA, Binance's main API is blocked there; TradBot then uses
  Binance's public data mirror automatically, which is fine for paper trading.
- Your Gemini key will be stored on that service. Only use a host you trust.

## Option B: your own VPS

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
