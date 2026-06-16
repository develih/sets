from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import io
import logging
import os
import re
import secrets
import sqlite3
import string
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands


DOWNLOAD_CATEGORIES: dict[str, str] = {
    "loud_sets": "loud sets",
    "blazing_sets": "blazing sets",
    "talking_sets": "talking sets",
    "pannings": "pannings",
}

PLUGIN_CATEGORY = "plugins"
PLUGIN_LABEL = "plugins"

ALL_CATEGORIES: dict[str, str] = {
    **DOWNLOAD_CATEGORIES,
    PLUGIN_CATEGORY: PLUGIN_LABEL,
}

UPLOAD_CATEGORY_CHOICES = [
    app_commands.Choice(name=label, value=key)
    for key, label in DOWNLOAD_CATEGORIES.items()
]

PBKDF2_ITERATIONS = 220_000
UPLOAD_READ_TIMEOUT_SECONDS = 30

logger = logging.getLogger("sets_downloader")


class UserFacingError(Exception):
    """An expected error that can be shown directly to a Discord user."""


async def send_ephemeral(interaction: discord.Interaction, message: str, **kwargs: Any) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(message, ephemeral=True, **kwargs)


async def report_interaction_error(
    interaction: discord.Interaction,
    message: str = "Something went wrong. Check the bot console for details.",
) -> None:
    try:
        await send_ephemeral(interaction, message)
    except discord.HTTPException:
        logger.exception("Failed to send Discord error response.")


class SafeCommandTree(app_commands.CommandTree):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, UserFacingError):
            await report_interaction_error(interaction, str(original))
            return

        logger.error(
            "Unhandled slash command error.",
            exc_info=(type(original), original, original.__traceback__),
        )
        await report_interaction_error(interaction)


@dataclass(frozen=True)
class Config:
    discord_token: str
    base_url: str
    host: str
    port: int
    data_dir: Path
    max_files_per_category: int
    max_upload_bytes: int
    plugin_url: str
    sync_guild_id: int | None
    upload_role_id: int | None
    token_ttl_hours: int

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv(Path(".env"))

        port = int(os.getenv("PORT", "8080"))
        base_url = os.getenv("BASE_URL", f"http://localhost:{port}").rstrip("/")

        return cls(
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            base_url=base_url,
            host=os.getenv("HOST", "0.0.0.0"),
            port=port,
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            max_files_per_category=int(os.getenv("MAX_FILES_PER_CATEGORY", "10")),
            max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(2 * 1024 * 1024))),
            plugin_url=os.getenv("PLUGIN_URL", "https://gofile.io/d/hDh8Vu"),
            sync_guild_id=parse_optional_int(os.getenv("SYNC_GUILD_ID")),
            upload_role_id=parse_optional_int(os.getenv("UPLOAD_ROLE_ID")),
            token_ttl_hours=int(os.getenv("TOKEN_TTL_HOURS", "0")),
        )


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def parse_optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    return int(value)


def now_ts() -> int:
    return int(time.time())


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_raw, salt_hex, expected_digest = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations_raw),
        ).hex()
        return hmac.compare_digest(digest, expected_digest)
    except (TypeError, ValueError):
        return False


def generate_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def sanitize_txt_filename(filename: str) -> str:
    name = PurePath(filename).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")

    if not name:
        raise UserFacingError("That file needs a valid name.")
    if not name.lower().endswith(".txt"):
        raise UserFacingError("Only `.txt` files are allowed.")

    stem = Path(name).stem.strip(" .")
    if not stem:
        raise UserFacingError("That `.txt` filename needs a real name before the extension.")

    return f"{stem}.txt"


def category_zip_name(category: str) -> str:
    safe = category.replace("_", "-")
    return f"{safe}.zip"


class Storage:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.data_dir = config.data_dir
        self.sets_dir = self.data_dir / "sets"
        self.db_path = self.data_dir / "downloads.db"
        self.lock = threading.RLock()

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sets_dir.mkdir(parents=True, exist_ok=True)
        for category in DOWNLOAD_CATEGORIES:
            self.category_dir(category).mkdir(parents=True, exist_ok=True)

        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._init_db()

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def _init_db(self) -> None:
        with self.lock:
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS download_tokens (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_by TEXT,
                    created_at INTEGER NOT NULL,
                    used_at INTEGER,
                    used_by_ip TEXT
                )
                """
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_download_tokens_category "
                "ON download_tokens(category)"
            )
            self.db.commit()

    def category_dir(self, category: str) -> Path:
        if category not in DOWNLOAD_CATEGORIES:
            raise ValueError(f"Unknown download category: {category}")
        return self.sets_dir / category

    def list_category_files(self, category: str) -> list[Path]:
        directory = self.category_dir(category)
        return sorted(
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() == ".txt"
        )

    def save_upload(self, category: str, filename: str, data: bytes) -> Path:
        if category not in DOWNLOAD_CATEGORIES:
            raise UserFacingError("You can only upload files to set categories.")

        clean_name = sanitize_txt_filename(filename)
        if len(data) > self.config.max_upload_bytes:
            max_mb = self.config.max_upload_bytes / 1024 / 1024
            raise UserFacingError(f"That file is too large. Max size is {max_mb:.1f} MB.")
        if b"\x00" in data:
            raise UserFacingError("That file looks binary. Only text files are allowed.")

        with self.lock:
            existing = self.list_category_files(category)
            if len(existing) >= self.config.max_files_per_category:
                label = DOWNLOAD_CATEGORIES[category]
                raise UserFacingError(
                    f"`{label}` already has {self.config.max_files_per_category} files."
                )

            existing_names = {path.name.lower() for path in existing}
            final_name = self._unique_filename(clean_name, existing_names)
            destination = self.category_dir(category) / final_name
            destination.write_bytes(data)
            return destination

    @staticmethod
    def _unique_filename(filename: str, existing_names: set[str]) -> str:
        if filename.lower() not in existing_names:
            return filename

        path = Path(filename)
        for index in range(2, 10_000):
            candidate = f"{path.stem}-{index}{path.suffix}"
            if candidate.lower() not in existing_names:
                return candidate

        raise UserFacingError("Could not find a free filename for that upload.")

    def create_download_token(self, category: str, created_by: int | None) -> tuple[str, str]:
        if category not in DOWNLOAD_CATEGORIES:
            raise ValueError(f"Unknown download category: {category}")

        password = generate_password()
        password_hash = hash_password(password)
        created_at = now_ts()
        creator = str(created_by) if created_by is not None else None

        with self.lock:
            for _ in range(5):
                token_id = secrets.token_urlsafe(9)
                try:
                    self.db.execute(
                        """
                        INSERT INTO download_tokens
                            (id, category, password_hash, created_by, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (token_id, category, password_hash, creator, created_at),
                    )
                    self.db.commit()
                    return token_id, password
                except sqlite3.IntegrityError:
                    continue

        raise RuntimeError("Could not create a unique download token.")

    def get_token(self, token_id: str) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute(
                "SELECT * FROM download_tokens WHERE id = ?",
                (token_id,),
            ).fetchone()

    def validate_token_password(self, token_id: str, password: str) -> str:
        row = self.get_token(token_id)
        if row is None:
            raise UserFacingError("This download link does not exist.")
        if row["used_at"] is not None:
            raise UserFacingError("This download link was already used.")
        if self._is_expired(row):
            raise UserFacingError("This download link has expired.")
        if not verify_password(password, row["password_hash"]):
            raise UserFacingError("Wrong password.")
        return str(row["category"])

    def burn_token(self, token_id: str, used_by_ip: str | None) -> None:
        with self.lock:
            row = self.get_token(token_id)
            if row is None:
                raise UserFacingError("This download link does not exist.")
            if row["used_at"] is not None:
                raise UserFacingError("This download link was already used.")
            if self._is_expired(row):
                raise UserFacingError("This download link has expired.")

            self.db.execute(
                """
                UPDATE download_tokens
                SET used_at = ?, used_by_ip = ?
                WHERE id = ? AND used_at IS NULL
                """,
                (now_ts(), used_by_ip, token_id),
            )
            self.db.commit()

    def _is_expired(self, row: sqlite3.Row) -> bool:
        ttl_hours = self.config.token_ttl_hours
        if ttl_hours <= 0:
            return False
        return now_ts() > int(row["created_at"]) + ttl_hours * 3600

    def build_zip(self, category: str) -> bytes:
        files = self.list_category_files(category)
        if not files:
            label = DOWNLOAD_CATEGORIES[category]
            raise UserFacingError(f"No files are uploaded in `{label}` yet.")

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, arcname=path.name)
        return buffer.getvalue()


def user_can_manage_downloads(interaction: discord.Interaction, config: Config) -> bool:
    user = interaction.user
    if isinstance(user, discord.Member) and user.guild_permissions.manage_guild:
        return True

    if config.upload_role_id and isinstance(user, discord.Member):
        return any(role.id == config.upload_role_id for role in user.roles)

    return False


async def require_manager(interaction: discord.Interaction) -> bool:
    client = interaction.client
    if not isinstance(client, DownloaderBot):
        await send_ephemeral(interaction, "Bot is not ready.")
        return False

    if user_can_manage_downloads(interaction, client.config):
        return True

    message = "You need `Manage Server` permission to use this command."
    if client.config.upload_role_id:
        message = "You need `Manage Server` permission or the configured uploader role."

    await send_ephemeral(interaction, message)
    return False


async def read_attachment_data(file: discord.Attachment) -> bytes:
    try:
        return await asyncio.wait_for(
            file.read(use_cached=False),
            timeout=UPLOAD_READ_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise UserFacingError(
            "Discord took too long to send me the file. Please try the upload again."
        ) from exc
    except discord.HTTPException as exc:
        logger.warning(
            "Discord could not provide attachment %s (%s).",
            file.filename,
            file.url,
            exc_info=True,
        )
        raise UserFacingError(
            "I could not download that attachment from Discord. Please attach the file again."
        ) from exc


class CategorySelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label=label, value=key)
            for key, label in ALL_CATEGORIES.items()
        ]
        super().__init__(
            placeholder="select category",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="sets_downloader:category_select:v1",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        client = interaction.client
        if not isinstance(client, DownloaderBot):
            await interaction.response.send_message("Bot is not ready.", ephemeral=True)
            return

        category = self.values[0]
        if category == PLUGIN_CATEGORY:
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Open plugins", url=client.config.plugin_url))
            await interaction.response.send_message(
                f"For ALL apo plugins > {client.config.plugin_url}",
                view=view,
                ephemeral=True,
            )
            return

        files = client.storage.list_category_files(category)
        if not files:
            await interaction.response.send_message(
                f"No files are uploaded in `{DOWNLOAD_CATEGORIES[category]}` yet.",
                ephemeral=True,
            )
            return

        token_id, password = client.storage.create_download_token(
            category,
            interaction.user.id,
        )
        link = f"{client.config.base_url}/download/{token_id}"

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open download", url=link))

        await interaction.response.send_message(
            "\n".join(
                [
                    f"Category: `{DOWNLOAD_CATEGORIES[category]}`",
                    f"Link: {link}",
                    f"Password: `{password}`",
                    "",
                    "The link burns after the first correct password download.",
                ]
            ),
            view=view,
            ephemeral=True,
        )


class CategorySelectView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(CategorySelect())

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        logger.error(
            "Unhandled component error for %s.",
            item,
            exc_info=(type(error), error, error.__traceback__),
        )
        await report_interaction_error(interaction)


send_group = app_commands.Group(name="send", description="Send downloader panels.")
upload_group = app_commands.Group(name="upload", description="Upload downloader files.")


@send_group.command(name="embed", description="Send the category selector embed.")
async def send_embed(interaction: discord.Interaction) -> None:
    if not await require_manager(interaction):
        return

    if interaction.channel is None:
        await interaction.response.send_message(
            "Use this command in a server channel.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="Sets downloader",
        description="Select a category below.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Categories",
        value="\n".join(f"- {label}" for label in ALL_CATEGORIES.values()),
        inline=False,
    )

    await interaction.channel.send(embed=embed, view=CategorySelectView())
    await interaction.response.send_message("Downloader embed sent.", ephemeral=True)


@upload_group.command(name="file", description="Upload one .txt file into a category.")
@app_commands.choices(category=UPLOAD_CATEGORY_CHOICES)
async def upload_file(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
    file: discord.Attachment,
) -> None:
    if not await require_manager(interaction):
        return

    client = interaction.client
    if not isinstance(client, DownloaderBot):
        await interaction.response.send_message("Bot is not ready.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        if not file.filename.lower().endswith(".txt"):
            raise UserFacingError("Only `.txt` files are allowed.")
        if file.size and file.size > client.config.max_upload_bytes:
            max_mb = client.config.max_upload_bytes / 1024 / 1024
            raise UserFacingError(f"That file is too large. Max size is {max_mb:.1f} MB.")

        data = await read_attachment_data(file)
        if not data:
            raise UserFacingError("That file is empty.")

        saved_path = client.storage.save_upload(category.value, file.filename, data)
        count = len(client.storage.list_category_files(category.value))

        await send_ephemeral(
            interaction,
            "\n".join(
                [
                    f"Uploaded `{saved_path.name}` to `{category.name}`.",
                    f"Files in category: `{count}/{client.config.max_files_per_category}`.",
                ]
            )
        )
    except UserFacingError as exc:
        await send_ephemeral(interaction, str(exc))


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def home(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    return html_response(
        page(
            "Sets downloader",
            f"""
            <p>The downloader is online.</p>
            <p>Use the Discord select menu to generate a one-time link.</p>
            <p><a href="{html.escape(config.plugin_url)}">plugins</a></p>
            """,
        )
    )


async def plugins_redirect(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    raise web.HTTPFound(config.plugin_url)


async def download_form(request: web.Request) -> web.Response:
    storage: Storage = request.app["storage"]
    token_id = request.match_info["token_id"]

    error = unavailable_reason(storage, token_id)
    if error:
        return html_response(page("Download unavailable", f"<p>{html.escape(error)}</p>"), 404)

    body = f"""
    <form method="post" action="/download/{html.escape(token_id)}">
        <label for="password">Password</label>
        <input id="password" name="password" type="password" autocomplete="off" autofocus required>
        <button type="submit">Download</button>
    </form>
    <p class="muted">This link can only be used once.</p>
    """
    return html_response(page("Enter password", body))


async def download_post(request: web.Request) -> web.Response:
    storage: Storage = request.app["storage"]
    token_id = request.match_info["token_id"]
    form = await request.post()
    password = str(form.get("password", ""))

    try:
        category = storage.validate_token_password(token_id, password)
        zip_bytes = storage.build_zip(category)
        storage.burn_token(token_id, request_ip(request))
    except UserFacingError as exc:
        return html_response(page("Download unavailable", f"<p>{html.escape(str(exc))}</p>"), 400)

    return web.Response(
        body=zip_bytes,
        headers={
            "Content-Type": "application/zip",
            "Content-Disposition": f'attachment; filename="{category_zip_name(category)}"',
            "Cache-Control": "no-store",
        },
    )


def unavailable_reason(storage: Storage, token_id: str) -> str | None:
    row = storage.get_token(token_id)
    if row is None:
        return "This download link does not exist."
    if row["used_at"] is not None:
        return "This download link was already used."
    if storage._is_expired(row):
        return "This download link has expired."
    return None


def request_ip(request: web.Request) -> str | None:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.headers.get("CF-Connecting-IP") or request.remote


def html_response(body: str, status: int = 200) -> web.Response:
    return web.Response(
        text=body,
        status=status,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(title)}</title>
    <style>
        :root {{
            color-scheme: dark;
            font-family: Arial, sans-serif;
            background: #111318;
            color: #f4f5f7;
        }}
        body {{
            margin: 0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            padding: 24px;
        }}
        main {{
            width: min(420px, 100%);
        }}
        h1 {{
            margin: 0 0 16px;
            font-size: 28px;
            line-height: 1.15;
        }}
        p {{
            line-height: 1.5;
        }}
        label {{
            display: block;
            margin-bottom: 8px;
            font-weight: 700;
        }}
        input {{
            box-sizing: border-box;
            width: 100%;
            padding: 12px;
            border: 1px solid #353b48;
            border-radius: 8px;
            background: #191d24;
            color: #fff;
            font-size: 16px;
        }}
        button {{
            width: 100%;
            margin-top: 12px;
            padding: 12px 14px;
            border: 0;
            border-radius: 8px;
            background: #5865f2;
            color: white;
            font-size: 16px;
            font-weight: 700;
            cursor: pointer;
        }}
        a {{
            color: #8ea1ff;
        }}
        .muted {{
            color: #a8adb8;
            font-size: 14px;
        }}
    </style>
</head>
<body>
    <main>
        <h1>{html.escape(title)}</h1>
        {body}
    </main>
</body>
</html>"""


async def create_web_app(config: Config, storage: Storage) -> web.Application:
    app = web.Application()
    app["config"] = config
    app["storage"] = storage
    app.router.add_get("/", home)
    app.router.add_get("/health", health)
    app.router.add_get("/plugins", plugins_redirect)
    app.router.add_get(r"/download/{token_id:[A-Za-z0-9_-]+}", download_form)
    app.router.add_post(r"/download/{token_id:[A-Za-z0-9_-]+}", download_post)
    return app


class DownloaderBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents, tree_cls=SafeCommandTree)

        self.config = config
        self.storage = Storage(config)
        self.web_runner: web.AppRunner | None = None

    async def setup_hook(self) -> None:
        self.add_view(CategorySelectView())
        self.tree.add_command(send_group)
        self.tree.add_command(upload_group)

        app = await create_web_app(self.config, self.storage)
        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, self.config.host, self.config.port)
        await site.start()

        if self.config.sync_guild_id:
            guild = discord.Object(id=self.config.sync_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Synced slash commands to guild {self.config.sync_guild_id}.")
        else:
            await self.tree.sync()
            print("Synced global slash commands.")

        print(f"Web server listening on http://{self.config.host}:{self.config.port}.")

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} (ID: {self.user.id if self.user else 'unknown'}).")

    async def close(self) -> None:
        if self.web_runner:
            await self.web_runner.cleanup()
        self.storage.close()
        await super().close()


async def main() -> None:
    config = Config.from_env()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not config.discord_token:
        raise SystemExit("Missing DISCORD_TOKEN. Create a .env file first.")

    bot = DownloaderBot(config)
    async with bot:
        await bot.start(config.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
