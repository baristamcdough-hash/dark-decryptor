# 🐋 PoriotCloud DarkTunnel Decryptor

Decode `.dark` configs → get a **signed JSON file + a private Vault link** that
**auto-destroys after 6 hours**. 
Credits: **@Poriot_ke**.

```
poriotcloud/
├── decoder.py       # pure .dark decoding engine (AES-CFB128 + MessagePack)
├── server.py        # FastAPI: vault pages, upload APIs, admin, cleanup, bot co-host
├── bot.py           # Telegram bot: progress bar, summary, file + vault link
├── storage.py       # SQLite + disk vaults, 6h TTL, settings, stats
├── templates/       # Jinja pages (landing, vault, destroyed, admin, login)
├── static/          # style.css + app.js (approved design)
├── tests/           # 45 server tests + fixture encoder
├── Dockerfile       # Railway-ready (web + bot in one service)
└── data/            # vaults + vault.db (created at runtime)
```

## The flow

1. **Telegram bot** receives a `.dark` file (or pasted `darktunnel://` link)
2. Animated progress bar (`▰▰▰▰▱▱ 62%`) — message edited ~5× while decoding
3. Bot replies with: summary card **+ Vault link** — the file lives on the
   vault page (that's where the ads are), never in the chat
4. Vault link → page shows the highlighted JSON with **Copy** + **Download**
5. **Auto-destroy after 6h** — file wiped from disk (cleanup loop + on-read check)

## Deploy to Railway (hobby plan)

1. Push this folder to a GitHub repo
2. Railway → **New Project → Deploy from GitHub repo** (Dockerfile is auto-detected)
3. Set env vars (see `.env.example`):
   - `VAULT_PUBLIC_URL` — your domain or Railway URL, e.g. `https://vault.poriot.ke`
   - `VAULT_API_TOKEN` — long random string (shared secret for bot uploads)
   - `VAULT_ADMIN_PASSWORD` — admin panel password
   - `VAULT_SECRET` — optional session signing secret
   - `BOT_TOKEN` — Telegram bot token from @BotFather (bot runs in the same service)
   - `VAULT_TTL_HOURS` — default `6`
4. Deploy. That's it — **web + bot run as one service**.
5. Optional: attach a volume at `/app/data` if you want vaults to survive redeploys
   (not required — the 6h auto-destroy design tolerates resets).

### Custom domain + HTTPS

Railway terminates TLS and issues a **free Let's Encrypt certificate**
automatically — you don't install anything in the app. The app enforces HTTPS
at its own level too (http→https redirect + HSTS behind the proxy, Secure admin
cookie).

**1. Point your domain at Railway**

Railway → service → **Settings → Networking → Custom Domain** → add your domain.
Railway shows a CNAME target (e.g. `xxx.up.railway.app`). At your DNS provider:

| Your domain | Record type | Name | Value |
|---|---|---|---|
| `vault.poriot.ke` (subdomain) | CNAME | `vault` | `xxx.up.railway.app` |
| `poriot.ke` (apex) | ALIAS / ANAME | `@` | `xxx.up.railway.app` |

*(If your registrar can't do ALIAS/ANAME at the apex, use a subdomain such as
`vault.poriot.ke` instead.)*

**2. Cloudflare users (if your DNS is there)**

Keep the CNAME record **DNS-only (grey cloud)**. Don't enable the orange-cloud
proxy for the Railway CNAME — Railway's certificate issuance can fail behind
it. Railway then serves HTTPS end-to-end with its own cert.

**3. Tell the app its public URL**

Add env var (or update it):

```
VAULT_PUBLIC_URL=https://vault.poriot.ke
```

That's the only knob — every vault link, `_vault` signature field, and the
bot's upload target are built from it. A bare `vault.poriot.ke` (no scheme) is
automatically treated as `https://`. Set `FORCE_HTTPS=1` to force the redirect
even when `VAULT_PUBLIC_URL` is not an https URL.

**4. Redeploy & verify**

- `curl -I https://vault.poriot.ke` → `200` + `Strict-Transport-Security` header
- Decode a file → the bot's link reads `https://vault.poriot.ke/v/…`
- `http://vault.poriot.ke/v/…` → 301 redirects to `https://…`
- Certificate auto-renews — nothing else to do

> The generated `*.up.railway.app` URL keeps working too; you can ignore it.

## Run locally

```bash
pip install -r requirements.txt

# web + bot in one process
BOT_TOKEN="..." VAULT_API_TOKEN="..." VAULT_ADMIN_PASSWORD="..." \
VAULT_PUBLIC_URL="http://localhost:8000" uvicorn server:app --port 8000

# or just the bot (Termux / standalone)
python bot.py
```

## Admin panel

`/admin` → login with `VAULT_ADMIN_PASSWORD`:
- **Stats** — uploads, active vaults, expiring today, page views
- **Adsterra Ad Manager** — monetization on/off, placement (top/bottom/both),
  paste your zone `<script>` snippet, save
- **Vaults** — browse, view expiry, delete any vault instantly

## API

| Endpoint | Auth | Body | Returns |
|---|---|---|---|
| `POST /api/decode` | public | multipart `.dark` file | `{ok, id, url, expires_at}` |
| `POST /api/vault` | `X-Vault-Token` | `{name, config}` (decoded dict) | `{ok, id, url, expires_at}` |

## Signature

Every decoded file is stamped (shown at the top of the JSON, first thing visible):

```json
{
  "_decoded_by": "@Poriot_ke · PoriotCloud Vault",
  "_decoded_at": "2026-08-13T23:28:10Z",
  "_vault": "https://vault.poriot.ke/v/Ab3xYz9Q",
  ...
}
```

## Tests

```bash
python tests/test_server.py   # 45 checks: uploads, vault pages, expiry, admin, ads
```

## Security notes

- Decoding happens **in memory**; nothing is logged or stored beyond the vault
- The vault API is locked behind `VAULT_API_TOKEN` (bot) — the public web upload
  
