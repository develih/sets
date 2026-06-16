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
from urllib.parse import quote

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
DEFAULT_DOWNLOAD_SESSION_TTL_MINUTES = 60

logger = logging.getLogger("sets_downloader")


class UserFacingError(Exception):
    """An expected error that can be shown directly to a Discord user."""


async def send_ephemeral(
    interaction: discord.Interaction,
    message: str | None = None,
    **kwargs: Any,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content=message, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(content=message, ephemeral=True, **kwargs)


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
    download_session_ttl_minutes: int
    frontend_dist_dir: Path

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
            download_session_ttl_minutes=int(
                os.getenv(
                    "DOWNLOAD_SESSION_TTL_MINUTES",
                    str(DEFAULT_DOWNLOAD_SESSION_TTL_MINUTES),
                )
            ),
            frontend_dist_dir=Path(
                os.getenv("FRONTEND_DIST_DIR", "private-set-vault-main/dist/client")
            ),
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


def hash_session_token(session_token: str) -> str:
    return hashlib.sha256(session_token.encode("utf-8")).hexdigest()


def session_cookie_name(token_id: str) -> str:
    return f"sets_session_{token_id}"


def iso_from_ts(timestamp: int | float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def safe_attachment_filename(filename: str) -> str:
    return filename.replace("\\", "_").replace('"', "'")


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
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS download_sessions (
                    id TEXT PRIMARY KEY,
                    token_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used_by_ip TEXT
                )
                """
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_download_sessions_token "
                "ON download_sessions(token_id)"
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

    def list_category_file_entries(self, category: str) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for path in self.list_category_files(category):
            stat = path.stat()
            entries.append(
                {
                    "id": path.name,
                    "name": path.name,
                    "size": stat.st_size,
                    "uploadedAt": iso_from_ts(stat.st_mtime),
                }
            )
        return entries

    def get_category_file(self, category: str, file_id: str) -> Path:
        name = PurePath(file_id).name
        if name != file_id or not name.lower().endswith(".txt"):
            raise UserFacingError("This file does not exist.")

        path = self.category_dir(category) / name
        if not path.is_file():
            raise UserFacingError("This file does not exist.")
        return path

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

    def create_download_session(
        self,
        token_id: str,
        password: str,
        used_by_ip: str | None,
    ) -> tuple[str, str, int]:
        created_at = now_ts()
        ttl_seconds = max(1, self.config.download_session_ttl_minutes) * 60
        expires_at = created_at + ttl_seconds

        with self.lock:
            self._delete_expired_sessions_locked(created_at)
            row = self.get_token(token_id)
            if row is None:
                raise UserFacingError("This download link does not exist.")
            if row["used_at"] is not None:
                raise UserFacingError("This download link was already used.")
            if self._is_expired(row):
                raise UserFacingError("This download link has expired.")
            if not verify_password(password, row["password_hash"]):
                raise UserFacingError("Wrong password.")

            category = str(row["category"])
            session_token = secrets.token_urlsafe(32)
            session_hash = hash_session_token(session_token)
            cursor = self.db.execute(
                """
                UPDATE download_tokens
                SET used_at = ?, used_by_ip = ?
                WHERE id = ? AND used_at IS NULL
                """,
                (created_at, used_by_ip, token_id),
            )
            if cursor.rowcount != 1:
                raise UserFacingError("This download link was already used.")

            self.db.execute(
                """
                INSERT INTO download_sessions
                    (id, token_id, category, created_at, expires_at, used_by_ip)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_hash, token_id, category, created_at, expires_at, used_by_ip),
            )
            self.db.commit()
            return session_token, category, expires_at

    def validate_download_session(
        self,
        token_id: str,
        session_token: str | None,
    ) -> tuple[str, int]:
        if not session_token:
            raise UserFacingError("This download link was already used.")

        session_hash = hash_session_token(session_token)
        current_ts = now_ts()
        with self.lock:
            row = self.db.execute(
                """
                SELECT * FROM download_sessions
                WHERE id = ? AND token_id = ?
                """,
                (session_hash, token_id),
            ).fetchone()
            if row is None:
                raise UserFacingError("This download link was already used.")
            if current_ts > int(row["expires_at"]):
                self.db.execute("DELETE FROM download_sessions WHERE id = ?", (session_hash,))
                self.db.commit()
                raise UserFacingError("This download link has expired.")

            return str(row["category"]), int(row["expires_at"])

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

    def _delete_expired_sessions_locked(self, current_ts: int) -> None:
        self.db.execute(
            "DELETE FROM download_sessions WHERE expires_at < ?",
            (current_ts,),
        )

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
            view.add_item(discord.ui.Button(label="open plugins", url=client.config.plugin_url))
            embed = discord.Embed(
                title="plugins",
                description="open the plugins library below.",
                color=discord.Color.from_rgb(18, 18, 18),
            )
            await interaction.response.send_message(
                embed=embed,
                view=view,
                ephemeral=True,
            )
            return

        files = client.storage.list_category_files(category)
        if not files:
            embed = discord.Embed(
                title="no files yet",
                description=f"`{DOWNLOAD_CATEGORIES[category]}` is empty right now.",
                color=discord.Color.from_rgb(18, 18, 18),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        token_id, password = client.storage.create_download_token(
            category,
            interaction.user.id,
        )
        link = f"{client.config.base_url}/d/{token_id}"

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="open vault", url=link))

        embed = discord.Embed(
            title="private link created",
            description="send this link and password to the user who should access the sets.",
            color=discord.Color.from_rgb(18, 18, 18),
        )
        embed.add_field(name="category", value=f"`{DOWNLOAD_CATEGORIES[category]}`", inline=True)
        embed.add_field(name="files", value=f"`{len(files)}`", inline=True)
        embed.add_field(name="password", value=f"`{password}`", inline=False)
        embed.add_field(name="link", value=link, inline=False)
        embed.set_footer(text="the link burns after the first correct password.")

        await interaction.response.send_message(
            embed=embed,
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
        title="private set vault",
        description="select a category to create a private one-time download link.",
        color=discord.Color.from_rgb(18, 18, 18),
    )
    embed.add_field(
        name="categories",
        value="\n".join(f"- {label}" for label in ALL_CATEGORIES.values()),
        inline=False,
    )
    embed.add_field(
        name="access",
        value="links require a generated password and burn after a correct unlock.",
        inline=False,
    )

    await interaction.channel.send(embed=embed, view=CategorySelectView())
    await interaction.response.send_message("embed sent.", ephemeral=True)


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

        embed = discord.Embed(
            title="file uploaded",
            color=discord.Color.from_rgb(18, 18, 18),
        )
        embed.add_field(name="file", value=f"`{saved_path.name}`", inline=False)
        embed.add_field(name="category", value=f"`{category.name}`", inline=True)
        embed.add_field(
            name="count",
            value=f"`{count}/{client.config.max_files_per_category}`",
            inline=True,
        )

        await send_ephemeral(interaction, embed=embed)
    except UserFacingError as exc:
        await send_ephemeral(interaction, str(exc))


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def api_error(exc: UserFacingError) -> web.Response:
    message = str(exc)
    lower = message.lower()
    if "wrong password" in lower:
        status = 401
    elif "does not exist" in lower or "not found" in lower:
        status = 404
    elif "already used" in lower or "session" in lower:
        status = 409
    elif "expired" in lower:
        status = 410
    else:
        status = 400

    return web.json_response({"ok": False, "error": message}, status=status)


def category_payload(storage: Storage, category: str, expires_at: int) -> dict[str, Any]:
    files = storage.list_category_file_entries(category)
    total_size = sum(int(file["size"]) for file in files)
    return {
        "category": {
            "name": DOWNLOAD_CATEGORIES[category],
            "fileCount": len(files),
            "totalSize": total_size,
            "expiresAt": iso_from_ts(expires_at),
        },
        "files": files,
    }


def session_token_from_request(request: web.Request, token_id: str) -> str | None:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            return token

    return request.cookies.get(session_cookie_name(token_id))


def set_session_cookie(
    response: web.StreamResponse,
    config: Config,
    token_id: str,
    session_token: str,
    expires_at: int,
) -> None:
    max_age = max(1, expires_at - now_ts())
    response.set_cookie(
        session_cookie_name(token_id),
        session_token,
        max_age=max_age,
        path=f"/api/download/{token_id}",
        httponly=True,
        secure=config.base_url.startswith("https://"),
        samesite="Lax",
    )


def authorized_download(request: web.Request) -> tuple[str, int, str]:
    storage: Storage = request.app["storage"]
    token_id = request.match_info["token_id"]
    session_token = session_token_from_request(request, token_id)
    category, expires_at = storage.validate_download_session(token_id, session_token)
    return category, expires_at, token_id


async def api_verify_download(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    storage: Storage = request.app["storage"]
    token_id = request.match_info["token_id"]

    try:
        try:
            data = await request.json()
        except ValueError:
            data = {}

        password = str(data.get("password", ""))
        if not password:
            raise UserFacingError("Password is required.")

        session_token, category, expires_at = storage.create_download_session(
            token_id,
            password,
            request_ip(request),
        )
    except UserFacingError as exc:
        return api_error(exc)

    response = web.json_response(
        {
            "ok": True,
            "sessionToken": session_token,
            **category_payload(storage, category, expires_at),
        }
    )
    set_session_cookie(response, config, token_id, session_token, expires_at)
    return response


async def api_list_files(request: web.Request) -> web.Response:
    storage: Storage = request.app["storage"]
    try:
        category, expires_at, _ = authorized_download(request)
    except UserFacingError as exc:
        return api_error(exc)

    return web.json_response(category_payload(storage, category, expires_at))


async def api_file_raw(request: web.Request) -> web.Response:
    storage: Storage = request.app["storage"]
    try:
        category, _, _ = authorized_download(request)
        path = storage.get_category_file(category, request.match_info["file_id"])
    except UserFacingError as exc:
        return api_error(exc)

    return web.Response(
        body=path.read_bytes(),
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Cache-Control": "no-store",
        },
    )


async def api_file_download(request: web.Request) -> web.Response:
    storage: Storage = request.app["storage"]
    try:
        category, _, _ = authorized_download(request)
        path = storage.get_category_file(category, request.match_info["file_id"])
    except UserFacingError as exc:
        return api_error(exc)

    return web.Response(
        body=path.read_bytes(),
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{safe_attachment_filename(path.name)}"',
            "Cache-Control": "no-store",
        },
    )


async def api_zip_download(request: web.Request) -> web.Response:
    storage: Storage = request.app["storage"]
    try:
        category, _, _ = authorized_download(request)
        zip_bytes = storage.build_zip(category)
    except UserFacingError as exc:
        return api_error(exc)

    return web.Response(
        body=zip_bytes,
        headers={
            "Content-Type": "application/zip",
            "Content-Disposition": f'attachment; filename="{category_zip_name(category)}"',
            "Cache-Control": "no-store",
        },
    )


async def home(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    return html_response(
        page(
            "sets downloader",
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


async def legacy_download_redirect(request: web.Request) -> web.Response:
    token_id = request.match_info["token_id"]
    raise web.HTTPFound(f"/d/{quote(token_id)}")


def builtin_download_app(token_id: str) -> web.Response:
    escaped_token = html.escape(token_id, quote=True)
    body = f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="robots" content="noindex,nofollow">
    <title>private set vault</title>
    <style>
        :root {{
            color-scheme: dark;
            --bg: #000;
            --panel: rgba(255, 255, 255, 0.035);
            --panel-strong: rgba(255, 255, 255, 0.065);
            --line: rgba(255, 255, 255, 0.11);
            --text: rgba(255, 255, 255, 0.92);
            --muted: rgba(255, 255, 255, 0.48);
            --faint: rgba(255, 255, 255, 0.24);
            --danger: #ff6b6b;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            min-height: 100vh;
            background:
                radial-gradient(circle at 50% 38%, rgba(255,255,255,0.105), transparent 28rem),
                radial-gradient(circle at 50% 100%, rgba(255,255,255,0.04), transparent 36rem),
                #000;
            color: var(--text);
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            -webkit-font-smoothing: antialiased;
        }}
        body::before {{
            content: "";
            position: fixed;
            inset: 0;
            pointer-events: none;
            opacity: 0.06;
            background-image:
                linear-gradient(to right, #fff 1px, transparent 1px),
                linear-gradient(to bottom, #fff 1px, transparent 1px);
            background-size: 64px 64px;
            mask-image: radial-gradient(ellipse at center, black 35%, transparent 78%);
        }}
        button, input {{ font: inherit; }}
        button, a.button {{
            height: 40px;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: rgba(255,255,255,0.05);
            color: var(--text);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            padding: 0 14px;
            text-decoration: none;
            cursor: pointer;
            transition: border-color .16s ease, background .16s ease, color .16s ease;
        }}
        button:hover, a.button:hover {{ background: rgba(255,255,255,0.09); border-color: rgba(255,255,255,0.22); }}
        button.primary, a.primary {{ background: #fff; color: #000; border-color: #fff; font-weight: 650; }}
        button.primary:hover, a.primary:hover {{ background: rgba(255,255,255,0.88); }}
        .screen {{ position: relative; z-index: 1; min-height: 100vh; }}
        .login {{
            display: grid;
            place-items: center;
            padding: 32px 18px;
        }}
        .login-inner {{
            width: min(360px, 100%);
            text-align: center;
        }}
        .mark {{
            width: 104px;
            height: 104px;
            margin: 0 auto 28px;
            border-radius: 999px;
            display: grid;
            place-items: center;
            border: 1px solid var(--line);
            background: radial-gradient(circle at 35% 25%, rgba(255,255,255,0.18), rgba(255,255,255,0.03));
            box-shadow: 0 30px 80px -30px #000;
            letter-spacing: .18em;
            text-transform: lowercase;
            color: rgba(255,255,255,0.72);
            font-size: 12px;
        }}
        h1 {{
            margin: 0;
            font-size: 13px;
            line-height: 1.2;
            letter-spacing: .34em;
            text-transform: lowercase;
            font-weight: 650;
            color: rgba(255,255,255,0.68);
        }}
        .subtitle {{
            margin: 9px 0 32px;
            color: var(--faint);
            font-size: 11px;
            letter-spacing: .18em;
            text-transform: lowercase;
        }}
        .password {{
            width: 100%;
            height: 48px;
            border: 0;
            border-bottom: 1px solid var(--line);
            border-radius: 0;
            background: transparent;
            color: var(--text);
            outline: none;
            text-align: center;
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            font-size: 18px;
            letter-spacing: .32em;
        }}
        .password::placeholder {{
            color: var(--faint);
            font-family: inherit;
            font-size: 13px;
            letter-spacing: .16em;
        }}
        .hint {{
            margin-top: 22px;
            color: var(--faint);
            font-size: 10px;
            letter-spacing: .28em;
            text-transform: lowercase;
        }}
        .error {{
            min-height: 18px;
            margin-top: 14px;
            color: var(--danger);
            font-size: 12px;
        }}
        .dashboard {{
            width: min(1040px, 100%);
            margin: 0 auto;
            padding: 30px 18px 80px;
        }}
        header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            margin-bottom: 22px;
        }}
        .brand {{
            display: flex;
            align-items: center;
            gap: 12px;
            min-width: 0;
        }}
        .brand-mark {{
            width: 38px;
            height: 38px;
            border-radius: 999px;
            border: 1px solid var(--line);
            background: var(--panel-strong);
            display: grid;
            place-items: center;
            color: var(--muted);
            font-size: 10px;
        }}
        .brand-title {{ font-size: 14px; font-weight: 650; text-transform: lowercase; }}
        .brand-subtitle {{ color: var(--muted); font-size: 10px; letter-spacing: .2em; text-transform: lowercase; }}
        .summary {{
            border: 1px solid var(--line);
            border-radius: 8px;
            background: linear-gradient(180deg, rgba(255,255,255,0.052), rgba(255,255,255,0.016));
            padding: 22px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 18px;
            box-shadow: 0 20px 60px -42px #000;
        }}
        .eyebrow {{
            color: var(--muted);
            font-size: 10px;
            letter-spacing: .28em;
            text-transform: lowercase;
        }}
        .category {{
            margin-top: 8px;
            font-size: clamp(22px, 4vw, 34px);
            font-weight: 720;
            letter-spacing: 0;
            text-transform: lowercase;
        }}
        .meta {{
            margin-top: 8px;
            color: var(--muted);
            font-size: 13px;
        }}
        .files {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 12px;
            margin-top: 18px;
        }}
        .file {{
            border: 1px solid var(--line);
            border-radius: 8px;
            background: var(--panel);
            padding: 16px;
            min-width: 0;
        }}
        .file-name {{
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            font-size: 14px;
            font-weight: 650;
        }}
        .file-meta {{
            margin-top: 5px;
            color: var(--muted);
            font-size: 12px;
        }}
        .actions {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 7px;
            margin-top: 14px;
        }}
        .actions button, .actions a.button {{
            height: 36px;
            padding: 0 8px;
            font-size: 12px;
        }}
        .empty {{
            margin-top: 18px;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: var(--panel);
            padding: 52px 18px;
            text-align: center;
            color: var(--muted);
        }}
        .modal {{
            position: fixed;
            inset: 0;
            z-index: 20;
            display: none;
            align-items: center;
            justify-content: center;
            padding: 18px;
            background: rgba(0,0,0,.82);
            backdrop-filter: blur(8px);
        }}
        .modal.open {{ display: flex; }}
        .modal-panel {{
            width: min(760px, 100%);
            height: min(680px, 84vh);
            border: 1px solid var(--line);
            border-radius: 8px;
            background: #070707;
            display: flex;
            flex-direction: column;
        }}
        .modal-head {{
            border-bottom: 1px solid var(--line);
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            padding: 12px 14px;
        }}
        .modal-title {{
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            font-size: 13px;
            font-weight: 650;
        }}
        pre {{
            margin: 0;
            padding: 14px;
            overflow: auto;
            white-space: pre-wrap;
            word-break: break-word;
            flex: 1;
            font-size: 12px;
            line-height: 1.55;
            color: rgba(255,255,255,.86);
        }}
        .hidden {{ display: none !important; }}
        @media (max-width: 760px) {{
            .summary {{ align-items: stretch; flex-direction: column; }}
            .summary .primary {{ width: 100%; }}
            .files {{ grid-template-columns: 1fr; }}
            header {{ align-items: flex-start; }}
        }}
    </style>
</head>
<body>
    <main class="screen">
        <section id="login" class="login">
            <div class="login-inner">
                <div class="mark">sets</div>
                <h1>private set vault</h1>
                <p class="subtitle">one-time private link</p>
                <form id="login-form">
                    <input id="password" class="password" type="password" autocomplete="current-password" placeholder="enter password" autofocus>
                    <div id="error" class="error"></div>
                    <button id="unlock" class="primary" type="submit" style="width:100%; margin-top: 6px;">unlock vault</button>
                    <div class="hint">password required</div>
                </form>
            </div>
        </section>

        <section id="dashboard" class="dashboard hidden">
            <header>
                <div class="brand">
                    <div class="brand-mark">sets</div>
                    <div>
                        <div class="brand-title">private set vault</div>
                        <div class="brand-subtitle">private link</div>
                    </div>
                </div>
                <button id="lock">lock</button>
            </header>
            <section class="summary">
                <div>
                    <div class="eyebrow">category</div>
                    <div id="category" class="category">sets</div>
                    <div id="meta" class="meta"></div>
                </div>
                <a id="download-all" class="button primary" href="#">download all zip</a>
            </section>
            <div id="empty" class="empty hidden">no files are available in this category.</div>
            <section id="files" class="files"></section>
        </section>
    </main>

    <section id="modal" class="modal" aria-hidden="true">
        <div class="modal-panel">
            <div class="modal-head">
                <div id="modal-title" class="modal-title"></div>
                <button id="modal-close">close</button>
            </div>
            <pre id="preview"></pre>
        </div>
    </section>

    <script>
        const tokenId = "{escaped_token}";
        let sessionToken = sessionStorage.getItem("sets_session_" + tokenId) || "";

        const login = document.getElementById("login");
        const dashboard = document.getElementById("dashboard");
        const form = document.getElementById("login-form");
        const password = document.getElementById("password");
        const error = document.getElementById("error");
        const unlock = document.getElementById("unlock");
        const filesEl = document.getElementById("files");
        const emptyEl = document.getElementById("empty");
        const categoryEl = document.getElementById("category");
        const metaEl = document.getElementById("meta");
        const downloadAll = document.getElementById("download-all");
        const modal = document.getElementById("modal");
        const modalTitle = document.getElementById("modal-title");
        const preview = document.getElementById("preview");

        function bytes(n) {{
            if (!Number.isFinite(n)) return "-";
            if (n < 1024) return n + " b";
            const units = ["kb", "mb", "gb"];
            let v = n / 1024;
            let i = 0;
            while (v >= 1024 && i < units.length - 1) {{
                v /= 1024;
                i++;
            }}
            return (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + " " + units[i];
        }}

        function dateLabel(iso) {{
            if (!iso) return "-";
            const d = new Date(iso);
            if (Number.isNaN(d.getTime())) return "-";
            return d.toLocaleString(undefined, {{ month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }});
        }}

        function apiHeaders() {{
            return sessionToken ? {{ Authorization: "Bearer " + sessionToken }} : {{}};
        }}

        function showLogin(message) {{
            dashboard.classList.add("hidden");
            login.classList.remove("hidden");
            error.textContent = message || "";
            password.focus();
        }}

        function showDashboard() {{
            login.classList.add("hidden");
            dashboard.classList.remove("hidden");
        }}

        function statusMessage(status) {{
            if (status === 401) return "wrong password.";
            if (status === 404) return "link not found.";
            if (status === 409) return "this link was already used.";
            if (status === 410) return "this link has expired.";
            return "something went wrong.";
        }}

        async function verify(pass) {{
            const res = await fetch("/api/download/" + encodeURIComponent(tokenId) + "/verify", {{
                method: "POST",
                headers: {{ "Content-Type": "application/json" }},
                body: JSON.stringify({{ password: pass }}),
            }});
            if (!res.ok) throw new Error(statusMessage(res.status));
            const data = await res.json();
            sessionToken = data.sessionToken || "";
            sessionStorage.setItem("sets_session_" + tokenId, sessionToken);
            render(data);
        }}

        async function loadFiles() {{
            const res = await fetch("/api/download/" + encodeURIComponent(tokenId) + "/files", {{
                headers: apiHeaders(),
            }});
            if (!res.ok) throw new Error(statusMessage(res.status));
            render(await res.json());
        }}

        function render(data) {{
            const files = data.files || [];
            const category = data.category || {{ name: "sets", totalSize: 0 }};
            categoryEl.textContent = category.name || "sets";
            const total = category.totalSize || files.reduce((sum, file) => sum + (file.size || 0), 0);
            metaEl.textContent = files.length + " file" + (files.length === 1 ? "" : "s") + " / " + bytes(total);
            downloadAll.href = "/api/download/" + encodeURIComponent(tokenId) + "/zip";
            filesEl.textContent = "";
            emptyEl.classList.toggle("hidden", files.length !== 0);
            for (const file of files) {{
                const card = document.createElement("article");
                card.className = "file";
                const raw = "/api/download/" + encodeURIComponent(tokenId) + "/files/" + encodeURIComponent(file.id) + "/raw";
                const download = "/api/download/" + encodeURIComponent(tokenId) + "/files/" + encodeURIComponent(file.id) + "/download";
                card.innerHTML = `
                    <div class="file-name"></div>
                    <div class="file-meta">${{bytes(file.size || 0)}} / ${{dateLabel(file.uploadedAt)}}</div>
                    <div class="actions">
                        <button type="button" data-action="preview">preview</button>
                        <button type="button" data-action="copy">copy</button>
                        <a class="button primary" href="${{download}}" download>get</a>
                    </div>
                `;
                card.querySelector(".file-name").textContent = file.name || file.id;
                card.querySelector('[data-action="preview"]').addEventListener("click", async () => {{
                    modal.classList.add("open");
                    modal.setAttribute("aria-hidden", "false");
                    modalTitle.textContent = file.name || file.id;
                    preview.textContent = "loading...";
                    const res = await fetch(raw, {{ headers: apiHeaders() }});
                    preview.textContent = res.ok ? await res.text() : statusMessage(res.status);
                }});
                card.querySelector('[data-action="copy"]').addEventListener("click", async (event) => {{
                    const button = event.currentTarget;
                    const res = await fetch(raw, {{ headers: apiHeaders() }});
                    if (!res.ok) return;
                    await navigator.clipboard.writeText(await res.text());
                    button.textContent = "copied";
                    setTimeout(() => button.textContent = "copy", 1200);
                }});
                filesEl.appendChild(card);
            }}
            showDashboard();
        }}

        form.addEventListener("submit", async (event) => {{
            event.preventDefault();
            if (!password.value) return;
            unlock.disabled = true;
            error.textContent = "";
            try {{
                await verify(password.value);
                password.value = "";
            }} catch (err) {{
                error.textContent = err.message || "something went wrong.";
            }} finally {{
                unlock.disabled = false;
            }}
        }});

        document.getElementById("lock").addEventListener("click", () => {{
            sessionStorage.removeItem("sets_session_" + tokenId);
            sessionToken = "";
            showLogin("");
        }});
        document.getElementById("modal-close").addEventListener("click", () => {{
            modal.classList.remove("open");
            modal.setAttribute("aria-hidden", "true");
            preview.textContent = "";
        }});
        modal.addEventListener("click", (event) => {{
            if (event.target === modal) document.getElementById("modal-close").click();
        }});
        window.addEventListener("keydown", (event) => {{
            if (event.key === "Escape") document.getElementById("modal-close").click();
        }});

        if (sessionToken) loadFiles().catch(() => showLogin(""));
    </script>
</body>
</html>"""
    return web.Response(
        text=body,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def download_app(request: web.Request) -> web.StreamResponse:
    frontend_index: Path | None = request.app.get("frontend_index")
    if frontend_index and frontend_index.is_file():
        return web.FileResponse(frontend_index)

    return builtin_download_app(request.match_info["token_id"])


async def frontend_asset_or_app(request: web.Request) -> web.StreamResponse:
    frontend_root: Path | None = request.app.get("frontend_root")
    frontend_index: Path | None = request.app.get("frontend_index")
    if not frontend_root or not frontend_index:
        return html_response(page("not found", "<p>not found.</p>"), 404)

    rel_path = request.match_info.get("path", "")
    if rel_path:
        candidate = (frontend_root / rel_path).resolve()
        if (candidate == frontend_root or frontend_root in candidate.parents) and candidate.is_file():
            cache_control = "public, max-age=31536000" if "." in candidate.name else "no-store"
            return web.FileResponse(candidate, headers={"Cache-Control": cache_control})

    return web.FileResponse(frontend_index, headers={"Cache-Control": "no-store"})


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


def find_frontend_root(config: Config) -> Path | None:
    candidates = [
        config.frontend_dist_dir,
        Path("private-set-vault-main/dist/client"),
        Path("private-set-vault-main/dist"),
        Path("private-set-vault-main/.output/public"),
    ]
    for candidate in candidates:
        index = candidate / "index.html"
        if index.is_file():
            return candidate.resolve()
    return None


async def create_web_app(config: Config, storage: Storage) -> web.Application:
    app = web.Application()
    app["config"] = config
    app["storage"] = storage
    frontend_root = find_frontend_root(config)
    if frontend_root:
        app["frontend_root"] = frontend_root
        app["frontend_index"] = frontend_root / "index.html"
        logger.info("Serving frontend from %s.", frontend_root)
    else:
        logger.info(
            "Frontend build not found under %s; using built-in vault page.",
            config.frontend_dist_dir,
        )

    app.router.add_get("/", home)
    app.router.add_get("/health", health)
    app.router.add_get("/plugins", plugins_redirect)
    app.router.add_get(r"/d/{token_id:[A-Za-z0-9_-]+}", download_app)
    app.router.add_get(r"/download/{token_id:[A-Za-z0-9_-]+}", legacy_download_redirect)
    app.router.add_post(r"/download/{token_id:[A-Za-z0-9_-]+}", legacy_download_redirect)
    app.router.add_post(r"/api/download/{token_id:[A-Za-z0-9_-]+}/verify", api_verify_download)
    app.router.add_get(r"/api/download/{token_id:[A-Za-z0-9_-]+}/files", api_list_files)
    app.router.add_get(
        r"/api/download/{token_id:[A-Za-z0-9_-]+}/files/{file_id}/raw",
        api_file_raw,
    )
    app.router.add_get(
        r"/api/download/{token_id:[A-Za-z0-9_-]+}/files/{file_id}/download",
        api_file_download,
    )
    app.router.add_get(r"/api/download/{token_id:[A-Za-z0-9_-]+}/zip", api_zip_download)
    app.router.add_get(r"/{path:.*}", frontend_asset_or_app)
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
