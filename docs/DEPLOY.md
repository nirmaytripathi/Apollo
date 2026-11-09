# Deploying Apollo for free

One Docker container (FastAPI serving the built React UI and the API on the same origin), with users and runs in a
free Neon Postgres database. Two free hosts; free-plan terms change, so check them when you sign up.

| | **A. Render (no card)** | **B. Oracle Cloud Always Free VM** |
|---|---|---|
| Card needed | No | Yes (identity check; Always Free is not charged) |
| RAM | 512 MB: uses `Dockerfile.render` (no PyTorch; same embedding model via ONNX) | up to 24 GB: uses `Dockerfile` |
| Idle | Sleeps after ~15 min without visits; first visit then takes about a minute | Always on |
| HTTPS / URL | Automatic, `https://<name>.onrender.com` | You set up DuckDNS + Caddy |
| Setup time | ~20 min | ~1 hour |

Both need the Neon step (1) first.

---

## 1. Neon (database)

1. https://neon.tech → **New Project** → region **AWS Asia Pacific 1 (Singapore)** → only **Postgres database** on.
   Skip the "agent prompt"; this app only needs the connection string.
2. **Connect** → **Connection pooling off** → copy the string
   (`postgresql://USER:PASSWORD@ep-....neon.tech/neondb?sslmode=require...`).
3. Locally, put it in `.env` as `DATABASE_URL=...` and copy your local data across once:
   ```powershell
   .venv\Scripts\python backend\scripts\migrate_to_db.py
   ```

## A. Render (free, no card)

`Dockerfile.render` installs `backend/requirements-render.txt` (no PyTorch) and sets `EMBED_BACKEND=onnx`: the same
all-MiniLM-L6-v2 model runs through ONNX Runtime. On a saved run it produced identical vectors, the same best-matching
sentences and the same verification decisions as the PyTorch version.

**CPU is tiny (0.1).** So the Render image also sets `PASSAGE_RANKING=keyword` (Hermes and the writers get
passages chosen by shared words instead of embeddings) and `VERIFY_MAX_SENTENCES=12`. Without them a run sat in the
Review stage for minutes while every sentence was embedded. Claim verification still uses embeddings, checks 4 claims
at a time and shows live progress; over 115 saved claims its partial/not-found decisions were the same for 115.
`EXTRACT_ORDER=provider_first` uses the page text Tavily already returns instead of downloading and parsing every page
(6.6 CPU-seconds, about a minute on Render, for 15 pages; now near 0), and `EMBED_WARM_UP=true` loads the embedding
model while the run plans and searches. Runs are still slower than on a laptop.
With `EXTRACT_ORDER=provider_first` pages are not downloaded, so `FETCH_PUBLISH_DATES=true` (the default) reads
only the first 64 KB of each page to find its publication date. Set it to `false` to skip those requests;
sources then show "publication date unknown".

**Memory is tight.** ONNX Runtime kept memory for every batch shape it embedded: verification took a run from about
250 MB to 584 MB, over the 512 MB limit, and Render restarted the server ("This run was interrupted because the server
restarted"). Embedding now goes shortest text first in batches of 8: the same 1,100 sentences add 45 MB instead of
325 MB (Windows, identical vectors). The image also sets `MALLOC_ARENA_MAX=2`. Not yet measured on Linux. If Render's **Events** tab shows "Ran out of memory", the free plan is
too small for this app: use option B (Oracle), a paid Render instance, or run it on your own PC.

1. Push the latest code to GitHub (`git push origin main`).
2. Sign up at https://render.com with **GitHub** (the free plan does not ask for a card).
3. **New → Web Service** → **Build and deploy from a Git repository** → pick `S-2007/Apollo-Innovista`.
4. Settings:
   - **Name:** `apollo` (gives `https://apollo-xxxx.onrender.com`)
   - **Region:** **Singapore** (next to Neon)
   - **Branch:** `main`
   - **Language / Runtime:** **Docker**
   - **Dockerfile Path:** `./Dockerfile.render`
   - **Instance Type:** **Free**
5. **Environment Variables** → add each (values without quotes):

   | Key | Value |
   |---|---|
   | `GROQ_API_KEY`, `GEMINI_API_KEY`, `TAVILY_API_KEY`, `SERPER_API_KEY` | your keys |
   | `JWT_SECRET` | a new long random string (`python -c "import secrets;print(secrets.token_urlsafe(48))"`) |
   | `DATABASE_URL` | the Neon connection string |
   | `COOKIE_SECURE` | `true` |
   | `LLM_PROVIDER` | `groq` |

6. **Advanced → Health Check Path:** `/`
7. **Create Web Service.** The first build takes 5–10 minutes (Logs tab). When it says **Live**, open the
   `https://….onrender.com` link at the top and sign in with your existing account.

Every `git push origin main` redeploys automatically. On the free plan the service sleeps after about 15 minutes
without visits; the next visit wakes it in about a minute. Finished runs are safe in Neon; a run in progress when it
goes to sleep is lost.

---

## B. Oracle Cloud VM (always on; needs a card)

### B1. Create the VM

1. Sign up at https://www.oracle.com/cloud/free/ . The **home region** you pick is permanent and Always Free VMs
   live there: choose **Singapore** (next to Neon) or **Mumbai / Hyderabad**.
2. Console → **Compute → Instances → Create instance**:
   - **Image:** Canonical **Ubuntu 24.04**
   - **Shape:** **Ampere → VM.Standard.A1.Flex**, **2 OCPU / 12 GB** (up to 4 / 24 is free).
     "Out of capacity" is common: retry later, try another availability domain, or use 1 OCPU / 6 GB.
   - **Networking:** create a new VCN with a **public subnet**, **assign a public IPv4 address**.
   - **SSH keys:** **Generate a key pair** → **Save private key** (e.g. `C:\Users\<you>\.ssh\oracle.key`).
   - **Create**, then copy the instance's **Public IP address**.
3. Open the web ports in Oracle's firewall: instance page → **Subnet** link → **Security Lists** → the default list →
   **Add Ingress Rules**: source `0.0.0.0/0`, TCP, destination port `80`; add another for `443`.

### B2. A free domain name (for HTTPS)

1. https://www.duckdns.org → sign in → create a subdomain (e.g. `apollo-s2007`) → set **current ip** to the VM's
   public IP → **update ip**. Your site will be `https://apollo-s2007.duckdns.org`.

### B3. Set up the server

From PowerShell on your PC:
```powershell
ssh -i C:\Users\<you>\.ssh\oracle.key ubuntu@<PUBLIC_IP>
```
(If SSH refuses the key as "too open": `icacls C:\Users\<you>\.ssh\oracle.key /inheritance:r /grant:r "$($env:USERNAME):R"`.)

Then on the VM:
```bash
# Ubuntu images on Oracle block ports with iptables: open 80 and 443
sudo iptables -I INPUT 6 -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save

# Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
exit
```
SSH in again (so the docker group applies), then:
```bash
git clone https://github.com/S-2007/Apollo-Innovista.git apollo   # private repo: use a GitHub token as the password
cd apollo
nano .env
```
Paste the server's settings, **one per line, no quotes around values** (Docker's `--env-file` keeps quotes):
```
GROQ_API_KEY=...
GEMINI_API_KEY=...
TAVILY_API_KEY=...
SERPER_API_KEY=...
LLM_PROVIDER=groq
JWT_SECRET=<a new long random string>
DATABASE_URL=postgresql://...neon.tech/neondb?sslmode=require
COOKIE_SECURE=true
```
Save (Ctrl+O, Enter, Ctrl+X), then build and start (the first build takes 10–20 minutes):
```bash
docker build -t apollo .
docker run -d --name apollo --restart unless-stopped --env-file .env -p 127.0.0.1:7860:7860 apollo
docker logs -f apollo        # wait for "Uvicorn running", then Ctrl+C
```

### B4. HTTPS with Caddy

```bash
sudo apt-get install -y caddy
echo 'apollo-s2007.duckdns.org {
    reverse_proxy 127.0.0.1:7860
}' | sudo tee /etc/caddy/Caddyfile
sudo systemctl reload caddy
```
Caddy gets a free Let's Encrypt certificate by itself (ports 80/443 must be open). Open
**https://apollo-s2007.duckdns.org** and sign in with your existing account.

### B5. Updating later

```bash
cd ~/apollo && git pull
docker build -t apollo . && docker rm -f apollo
docker run -d --name apollo --restart unless-stopped --env-file .env -p 127.0.0.1:7860:7860 apollo
```

## Troubleshooting

| Problem | Fix |
|---|---|
| "Out of capacity" creating the VM | Retry later / other availability domain / smaller shape (1 OCPU, 6 GB) |
| Site does not load at all | Security List rules for 80/443, the iptables commands, DuckDNS IP = VM public IP |
| Build fails installing `torch==2.15.0` | In `Dockerfile` change it to plain `torch` |
| Container exits | `docker logs apollo`; usually a missing or quoted value in `.env` |
| Sign-in does not stick | Use the `https://` address; `COOKIE_SECURE=true` needs HTTPS |
| `/api/system` (signed in) shows `"database": "sqlite"` | `DATABASE_URL` missing from the server's `.env`; data would be lost on rebuild |
| A run shows "interrupted" although it was still working | Every server sharing the database (Render, a local server on Neon) must run a version with run heartbeats (Phase 3.23). An older server still marks every running run interrupted when it starts, so redeploy Render after updating |
| YouTube link: "Could not read the video … RequestBlocked / IpBlocked" | YouTube often refuses caption requests from cloud servers. Set `GEMINI_API_KEY` so Gemini transcribes the video instead (`VIDEO_GEMINI_FALLBACK=true`, the default) |

## What the repo provides for this

- `Dockerfile`: builds the frontend, installs CPU-only torch, pre-downloads the embedding model, runs uvicorn on
  port 7860 (works on ARM and x86).
- `.dockerignore`: keeps `.env`, `output/` and local databases out of the image.
- `api.py`: serves `frontend/dist` on `/` when it exists, after all `/api` routes.
- `backend/scripts/migrate_to_db.py`: one-time copy of local accounts and runs into `DATABASE_URL`.
