from __future__ import annotations

import asyncio
import copy
import datetime as dt
import io
import json
import logging
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands


log = logging.getLogger(__name__)

BACKUP_CHANNEL_NAME = "bot-db"
BACKUP_FILE_NAME = "bot-db.json"
BACKUP_MARKER = "CAINE bot-db backup"
BACKUP_VERSION = 1


class InMemoryPluginStorage:
    def __init__(self, initial_data: dict[str, Any] | None = None) -> None:
        self._data: dict[str, dict[str, Any]] = _plugin_data_from_payload(initial_data or {})
        self.loaded = False
        self.last_loaded_source = "memory"
        self.last_backup_message_id: int | None = None

    async def load(self) -> None:
        self.loaded = True

    async def get(self, plugin_name: str, key: str, default: Any = None) -> Any:
        plugin = self._data.get(_clean_plugin_name(plugin_name), {})
        if key not in plugin:
            return _json_clone(default)
        return _json_clone(plugin[key])

    async def set(self, plugin_name: str, key: str, value: Any) -> None:
        plugin = self._data.setdefault(_clean_plugin_name(plugin_name), {})
        plugin[str(key)] = _json_value(value)
        await self.backup()

    async def delete(self, plugin_name: str, key: str) -> None:
        plugin = self._data.setdefault(_clean_plugin_name(plugin_name), {})
        plugin.pop(str(key), None)
        await self.backup()

    async def backup(self) -> None:
        self.loaded = True

    def snapshot(self) -> dict[str, Any]:
        return _snapshot_payload(self._data)


class DiscordBackedPluginStorage(InMemoryPluginStorage):
    def __init__(self, bot: commands.Bot, legacy_data_dir: Path | None = None) -> None:
        super().__init__()
        self.bot = bot
        self.legacy_data_dir = Path(legacy_data_dir) if legacy_data_dir is not None else None
        self.channel_name = BACKUP_CHANNEL_NAME
        self._backup_lock = asyncio.Lock()
        self._channel_id: int | None = None

    async def load(self) -> None:
        channel = await self._resolve_backup_channel(create=True)
        payload = await self._load_latest_discord_payload(channel) if channel is not None else None
        if payload is not None:
            self._data = _plugin_data_from_payload(payload)
            self.loaded = True
            self.last_loaded_source = "discord"
            return

        migrated = self._load_legacy_plugin_files()
        if migrated:
            self._data = migrated
            self.loaded = True
            self.last_loaded_source = "legacy-local"
            await self.backup()
            return

        self.loaded = True
        self.last_loaded_source = "empty"
        await self.backup()

    async def backup(self) -> None:
        if not self.loaded:
            return
        async with self._backup_lock:
            channel = await self._resolve_backup_channel(create=True)
            if channel is None:
                log.warning("could not write bot-db backup: channel '%s' not available", self.channel_name)
                return

            payload = self.snapshot()
            encoded = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True).encode("utf-8")
            file = discord.File(io.BytesIO(encoded), filename=BACKUP_FILE_NAME)
            content = f"{BACKUP_MARKER} v{BACKUP_VERSION} - {payload['updated_at_utc']}"
            sent = await channel.send(content=content, file=file)
            self.last_backup_message_id = int(getattr(sent, "id", 0) or 0) or None
            await self._delete_older_backup_messages(channel, keep_message_id=self.last_backup_message_id)

    async def _resolve_backup_channel(self, create: bool) -> Any | None:
        if self._channel_id is not None:
            channel = self.bot.get_channel(self._channel_id)
            if channel is not None:
                return channel

        for guild in getattr(self.bot, "guilds", []) or []:
            for channel in getattr(guild, "text_channels", []) or []:
                if str(getattr(channel, "name", "")).lower() == self.channel_name:
                    self._channel_id = getattr(channel, "id", None)
                    return channel

        if not create:
            return None

        guilds = list(getattr(self.bot, "guilds", []) or [])
        if not guilds:
            return None
        guild = guilds[0]
        create_text_channel = getattr(guild, "create_text_channel", None)
        if not callable(create_text_channel):
            return None
        try:
            channel = await create_text_channel(self.channel_name, reason="CAINE Discord-backed plugin database")
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("could not create #%s: %s", self.channel_name, exc)
            return None
        except Exception as exc:
            log.warning("could not create #%s: %s", self.channel_name, exc)
            return None
        self._channel_id = getattr(channel, "id", None)
        return channel

    async def _load_latest_discord_payload(self, channel: Any) -> dict[str, Any] | None:
        history = getattr(channel, "history", None)
        if not callable(history):
            return None
        try:
            messages = history(limit=100)
            async for message in messages:
                payload = await self._payload_from_message(message)
                if payload is not None:
                    self.last_backup_message_id = getattr(message, "id", None)
                    return payload
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("could not read #%s backup history: %s", self.channel_name, exc)
        except Exception as exc:
            log.warning("could not read #%s backup history: %s", self.channel_name, exc)
        return None

    async def _payload_from_message(self, message: Any) -> dict[str, Any] | None:
        attachments = list(getattr(message, "attachments", []) or [])
        for attachment in attachments:
            if str(getattr(attachment, "filename", "")).lower() != BACKUP_FILE_NAME:
                continue
            try:
                data = await attachment.read()
                payload = json.loads(data.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError, OSError, discord.HTTPException) as exc:
                log.warning("could not decode %s attachment: %s", BACKUP_FILE_NAME, exc)
                continue
            if isinstance(payload, dict):
                return payload
        return None

    async def _delete_older_backup_messages(self, channel: Any, keep_message_id: int | None) -> None:
        history = getattr(channel, "history", None)
        if not callable(history):
            return
        try:
            async for message in history(limit=25):
                message_id = getattr(message, "id", None)
                if keep_message_id is not None and message_id == keep_message_id:
                    continue
                if not _message_has_backup_attachment(message):
                    continue
                delete = getattr(message, "delete", None)
                if callable(delete):
                    await delete()
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.debug("could not prune old bot-db backups: %s", exc)
        except Exception as exc:
            log.debug("could not prune old bot-db backups: %s", exc)

    def _load_legacy_plugin_files(self) -> dict[str, dict[str, Any]]:
        if self.legacy_data_dir is None or not self.legacy_data_dir.exists():
            return {}
        migrated: dict[str, dict[str, Any]] = {}
        for path in sorted(self.legacy_data_dir.glob("*.json")):
            if path.name in {BACKUP_FILE_NAME, "slash_sync_state.json"}:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                migrated[path.stem] = _json_clone(payload)
        return migrated


def _snapshot_payload(data: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": BACKUP_VERSION,
        "updated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "plugins": _json_clone(data),
    }


def _plugin_data_from_payload(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_plugins = payload.get("plugins", payload)
    if not isinstance(raw_plugins, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for plugin_name, value in raw_plugins.items():
        if isinstance(value, dict):
            result[_clean_plugin_name(plugin_name)] = _json_clone(value)
    return result


def _message_has_backup_attachment(message: Any) -> bool:
    for attachment in getattr(message, "attachments", []) or []:
        if str(getattr(attachment, "filename", "")).lower() == BACKUP_FILE_NAME:
            return True
    return False


def _clean_plugin_name(value: object) -> str:
    return str(value or "plugin").strip() or "plugin"


def _json_clone(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=True))
    except TypeError:
        return copy.deepcopy(value)


def _json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True))
