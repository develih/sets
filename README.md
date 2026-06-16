# Discord Sets Downloader

Bot de Discord con `/send embed` y `/upload file`.

## What it does

- `/send embed` sends an embed with a category select menu.
- Categories: `loud sets`, `blazing sets`, `talking sets`, `pannings`, `plugins`.
- Selecting a set category creates a one-time private vault link plus password.
- The password burns the link after the first correct unlock.
- The vault lists every `.txt` in the category with preview, copy, single-file download, and ZIP download actions.
- `plugins` redirects to `https://gofile.io/d/hDh8Vu`.
- `/upload file` uploads one `.txt` at a time into a category.
- Each set category can hold max `10` `.txt` files by default.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set:

```env
DISCORD_TOKEN=your_bot_token
BASE_URL=https://edating.gay
```

Optional frontend build:

```powershell
cd private-set-vault-main
npm install
npm run build
cd ..
```

If the frontend build is missing, the bot serves a built-in vault page automatically.

For faster slash command updates while testing, set `SYNC_GUILD_ID` to your Discord server ID.

Then run:

```powershell
python bot.py
```

## Discord bot permissions

In the Discord Developer Portal:

1. Create an app and bot.
2. Invite it with scopes `bot` and `applications.commands`.
3. Give it permissions to send messages, embed links, and use slash commands.
4. Users need `Manage Server` to run `/send embed` and `/upload file`.

If you want a specific uploader role instead, put that role ID in `UPLOAD_ROLE_ID`.

## Domain setup

The bot starts a web server on `PORT` (`8080` by default). Your domain `edating.gay` needs to point to that server.

Example Nginx reverse proxy:

```nginx
server {
    server_name edating.gay;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Use HTTPS through Nginx, Caddy, Cloudflare Tunnel, or your hosting panel.

## Storage

Uploaded files are saved in:

```text
data/sets/loud_sets/
data/sets/blazing_sets/
data/sets/talking_sets/
data/sets/pannings/
```

One-time link state is saved in:

```text
data/downloads.db
```
