from __future__ import annotations

import hashlib
import json
import logging
import inspect
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands


log = logging.getLogger(__name__)
DEFAULT_KINGER_ROLE_ID = 1523734381146148864
SLASH_SYNC_STATE_FILE = "slash_sync_state.json"
DEFAULT_ROUTING_PRIORITY = 50


class CommandLevel(str, Enum):
    KINGER = "kinger"
    ADMIN = "admin"
    USER = "user"


@dataclass(frozen=True)
class CommandOption:
    name: str
    description: str
    type: str = "string"
    required: bool = False


@dataclass(frozen=True)
class CommandSpec:
    names: tuple[str, ...]
    description: str
    level: CommandLevel = CommandLevel.USER
    group: str = "Plugins"
    options: tuple[CommandOption, ...] = ()
    slash_enabled: bool | None = None
    routing_priority: int = DEFAULT_ROUTING_PRIORITY
    routing_when: str = ""
    routing_not_when: str = ""

    @property
    def name(self) -> str:
        return self.names[0]

    @property
    def aliases(self) -> tuple[str, ...]:
        return self.names[1:]


CommandHandler = Callable[[Any, str], Awaitable[None]]


def command_spec(
    name_or_object: str | dict[str, Any] | CommandSpec,
    description: str = "",
    level: str | CommandLevel = CommandLevel.USER,
    aliases: tuple[str, ...] | list[str] = (),
    group: str = "Plugins",
    options: tuple[CommandOption, ...] | list[CommandOption | dict[str, Any]] = (),
    slash_enabled: bool | None = None,
    routing_priority: int | str = DEFAULT_ROUTING_PRIORITY,
    routing_when: str = "",
    routing_not_when: str = "",
) -> CommandSpec:
    if isinstance(name_or_object, CommandSpec):
        return name_or_object

    if isinstance(name_or_object, dict):
        names_value = name_or_object.get("names", name_or_object.get("name", ()))
        if isinstance(names_value, str):
            names = (names_value,)
        else:
            names = tuple(str(item).strip() for item in names_value if str(item).strip())
        description = str(name_or_object.get("description", description)).strip()
        level = name_or_object.get("level", level)
        group = str(name_or_object.get("group", group)).strip() or group
        options = name_or_object.get("options", options)
        routing = name_or_object.get("routing", {})
        if not isinstance(routing, dict):
            routing = {}
        routing_priority = name_or_object.get("routing_priority", routing.get("priority", routing_priority))
        routing_when = str(
            name_or_object.get(
                "routing_when",
                name_or_object.get("use_when", routing.get("when", routing.get("use_when", routing_when))),
            )
        )
        routing_not_when = str(
            name_or_object.get(
                "routing_not_when",
                name_or_object.get(
                    "not_when",
                    name_or_object.get(
                        "avoid_when",
                        routing.get("not_when", routing.get("avoid_when", routing_not_when)),
                    ),
                ),
            )
        )
        slash_enabled = normalize_slash_enabled(
            name_or_object.get("slash_enabled", name_or_object.get("slash", slash_enabled))
        )
    else:
        names = (str(name_or_object).strip(), *(str(item).strip() for item in aliases if str(item).strip()))

    normalized_level = normalize_command_level(level)
    names = tuple(dict.fromkeys(normalize_command_name(name) for name in names if normalize_command_name(name)))
    if not names:
        raise ValueError("command spec needs at least one name")

    return CommandSpec(
        names=names,
        description=description.strip() or "No description.",
        level=normalized_level,
        group=group,
        options=normalize_command_options(options),
        slash_enabled=slash_enabled,
        routing_priority=normalize_routing_priority(routing_priority),
        routing_when=normalize_routing_text(routing_when),
        routing_not_when=normalize_routing_text(routing_not_when),
    )


def normalize_command_name(value: str) -> str:
    normalized = str(value).strip().lower()
    normalized = normalized.replace(" ", "-")
    normalized = re.sub(r"[^a-z0-9_-]+", "-", normalized)
    normalized = re.sub(r"[-_]{2,}", "-", normalized).strip("-_")
    return normalized[:32]


def normalize_option_name(value: str) -> str:
    normalized = normalize_command_name(value).replace("-", "_")
    return normalized[:32]


def normalize_command_options(
    options: tuple[CommandOption, ...] | list[CommandOption | dict[str, Any]],
) -> tuple[CommandOption, ...]:
    result: list[CommandOption] = []
    seen: set[str] = set()
    for option in options or ():
        if isinstance(option, CommandOption):
            candidate = option
        else:
            candidate = CommandOption(
                name=str(option.get("name", "")),
                description=str(option.get("description", "")),
                type=str(option.get("type", "string")),
                required=bool(option.get("required", False)),
            )
        name = normalize_option_name(candidate.name)
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(
            CommandOption(
                name=name,
                description=(candidate.description or name)[:100],
                type=normalize_option_type(candidate.type),
                required=candidate.required,
            )
        )
    return tuple(sorted(result, key=lambda item: not item.required))


def normalize_option_type(value: str) -> str:
    normalized = str(value).strip().lower()
    aliases = {
        "str": "string",
        "text": "string",
        "string": "string",
        "int": "integer",
        "integer": "integer",
        "float": "number",
        "num": "number",
        "number": "number",
        "bool": "boolean",
        "boolean": "boolean",
        "user": "user",
        "member": "user",
        "channel": "channel",
        "role": "role",
        "attachment": "attachment",
        "file": "attachment",
    }
    return aliases.get(normalized, "string")


def normalize_slash_enabled(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on", "slash"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "prefix", "prefix-only"}:
        return False
    return None


def normalize_routing_priority(value: int | str) -> int:
    try:
        priority = int(value)
    except (TypeError, ValueError):
        priority = DEFAULT_ROUTING_PRIORITY
    return max(0, min(100, priority))


def normalize_routing_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:300]


def normalize_command_level(value: str | CommandLevel) -> CommandLevel:
    if isinstance(value, CommandLevel):
        return value
    normalized = str(value).strip().lower()
    aliases = {
        "s1": CommandLevel.KINGER,
        "stage1": CommandLevel.KINGER,
        "level1": CommandLevel.KINGER,
        "kinger": CommandLevel.KINGER,
        "s2": CommandLevel.ADMIN,
        "stage2": CommandLevel.ADMIN,
        "level2": CommandLevel.ADMIN,
        "admin": CommandLevel.ADMIN,
        "s3": CommandLevel.USER,
        "stage3": CommandLevel.USER,
        "level3": CommandLevel.USER,
        "user": CommandLevel.USER,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError(f"unknown command level: {value}") from exc


def attach_command_spec(command: commands.Command, spec: CommandSpec, plugin_name: str | None = None) -> None:
    command.caine_spec = spec
    command.caine_level = spec.level
    command.caine_plugin_name = plugin_name


def register_dual_command(
    bot: commands.Bot,
    spec: CommandSpec,
    handler: CommandHandler,
    plugin_name: str | None = None,
) -> commands.Command:
    prefix_command = build_prefix_command(bot, spec, handler, plugin_name)
    bot.add_command(prefix_command)
    if should_register_slash_command(spec):
        add_slash_command(bot, spec, handler, plugin_name)
    return prefix_command


def build_prefix_command(
    bot: commands.Bot,
    spec: CommandSpec,
    handler: CommandHandler,
    plugin_name: str | None = None,
) -> commands.Command:
    async def callback(ctx: commands.Context, *, args: str = "") -> None:
        if not await ensure_command_allowed(ctx, spec):
            return
        await handler(ctx, args)

    command = commands.Command(
        callback,
        name=spec.name,
        aliases=list(spec.aliases),
        help=spec.description,
    )
    attach_command_spec(command, spec, plugin_name)
    command.caine_handler = handler
    return command


def add_slash_command(
    bot: commands.Bot,
    spec: CommandSpec,
    handler: CommandHandler,
    plugin_name: str | None = None,
) -> list[app_commands.Command]:
    if not should_register_slash_command(spec):
        return []
    slash_commands = []
    for slash_name in spec.names:
        async def callback(interaction: discord.Interaction, **kwargs: Any) -> None:
            if not await ensure_interaction_allowed(interaction, spec):
                return
            args, attachments = slash_kwargs_to_args(spec, kwargs)
            await handler(SlashCommandContext(interaction, attachments=attachments), args)

        configure_slash_callback(callback, spec)

        description = spec.description[:100] or "No description."
        slash_command = app_commands.Command(
            name=slash_name,
            description=description,
            callback=callback,
        )
        slash_command.extras["caine_spec"] = spec
        slash_command.extras["caine_plugin_name"] = plugin_name
        remove_slash_command(bot, slash_name)
        bot.tree.add_command(slash_command)
        slash_commands.append(slash_command)
    return slash_commands


def should_register_slash_command(spec: CommandSpec) -> bool:
    if spec.level is not CommandLevel.USER:
        return False
    return spec.slash_enabled is not False


def configure_slash_callback(callback: Callable[..., Awaitable[None]], spec: CommandSpec) -> None:
    parameters = [
        inspect.Parameter(
            "interaction",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=discord.Interaction,
        )
    ]
    descriptions = {}
    for option in spec.options:
        default = inspect.Parameter.empty if option.required else None
        parameters.append(
            inspect.Parameter(
                option.name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=slash_option_annotation(option.type),
                default=default,
            )
        )
        descriptions[option.name] = option.description
    callback.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    callback.__discord_app_commands_param_description__ = descriptions  # type: ignore[attr-defined]


def slash_option_annotation(option_type: str) -> type:
    return {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "user": discord.User,
        "channel": discord.abc.GuildChannel,
        "role": discord.Role,
        "attachment": discord.Attachment,
    }.get(option_type, str)


def slash_kwargs_to_args(spec: CommandSpec, kwargs: dict[str, Any]) -> tuple[str, list[discord.Attachment]]:
    parts: list[str] = []
    attachments: list[discord.Attachment] = []
    for option in spec.options:
        value = kwargs.get(option.name)
        if value is None:
            continue
        if option.type == "attachment" and isinstance(value, discord.Attachment):
            attachments.append(value)
            continue
        parts.append(slash_value_to_arg(value))
    return " ".join(part for part in parts if part).strip(), attachments


def slash_value_to_arg(value: Any) -> str:
    value_id = getattr(value, "id", None)
    if value_id is not None and isinstance(value, (discord.User, discord.Member)):
        return f"<@{value_id}>"
    if value_id is not None and isinstance(value, discord.Role):
        return f"<@&{value_id}>"
    if value_id is not None and isinstance(value, discord.abc.GuildChannel):
        return f"<#{value_id}>"
    return str(value)


def remove_slash_command(bot: commands.Bot, command_name: str) -> None:
    try:
        bot.tree.remove_command(command_name)
    except Exception:
        log.debug("could not remove slash command %s", command_name, exc_info=True)


async def ensure_command_allowed(ctx: commands.Context, spec: CommandSpec) -> bool:
    if not is_allowed_guild(ctx.bot, ctx.guild):
        return False
    if await has_command_access(ctx.bot, ctx.author, spec.level):
        return True
    await ctx.reply(permission_denied_text(spec.level), mention_author=False)
    return False


async def ensure_interaction_allowed(interaction: discord.Interaction, spec: CommandSpec) -> bool:
    if not is_allowed_guild(interaction.client, interaction.guild):
        await respond_to_interaction(interaction, "Dieser Server ist fuer CAINE nicht freigegeben.", ephemeral=True)
        return False
    if await has_command_access(interaction.client, interaction.user, spec.level):
        return True
    await respond_to_interaction(interaction, permission_denied_text(spec.level), ephemeral=True)
    return False


def is_allowed_guild(bot: commands.Bot, guild: discord.Guild | None) -> bool:
    settings = getattr(bot, "settings", None)
    allowed_ids = getattr(settings, "allowed_guild_ids", set())
    if not allowed_ids:
        return True
    return guild is not None and guild.id in allowed_ids


async def has_command_access(
    bot: commands.Bot,
    user: discord.abc.User,
    level: CommandLevel,
) -> bool:
    if level is CommandLevel.USER:
        return True

    if level is CommandLevel.KINGER:
        return user_has_any_role(user, load_kinger_role_ids(bot))

    if level is CommandLevel.ADMIN:
        permissions = getattr(user, "guild_permissions", None)
        if getattr(permissions, "administrator", False):
            return True
        try:
            return await bot.is_owner(user)
        except Exception:
            return False

    return False


def user_has_any_role(user: discord.abc.User, role_ids: set[int]) -> bool:
    roles = getattr(user, "roles", [])
    return bool(role_ids and any(getattr(role, "id", None) in role_ids for role in roles))


def load_kinger_role_ids(bot: commands.Bot) -> set[int]:
    settings = getattr(bot, "settings", None)
    path = getattr(settings, "command_permissions_path", None)
    if path is None:
        return {DEFAULT_KINGER_ROLE_ID}
    return load_command_permission_config(Path(path)).kinger_role_ids


@dataclass(frozen=True)
class CommandPermissionConfig:
    kinger_role_ids: set[int]


def load_command_permission_config(path: Path) -> CommandPermissionConfig:
    default = CommandPermissionConfig(kinger_role_ids={DEFAULT_KINGER_ROLE_ID})
    if not path.exists():
        return default

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default

    role_values = data.get("kingerRoleIds", data.get("kinger_role_ids", []))
    role_ids: set[int] = set()
    for value in role_values:
        try:
            role_ids.add(int(value))
        except (TypeError, ValueError):
            continue
    return CommandPermissionConfig(kinger_role_ids=role_ids or default.kinger_role_ids)


def permission_denied_text(level: CommandLevel) -> str:
    if level is CommandLevel.KINGER:
        return "Dafuer brauchst du die Kinger-Rolle."
    if level is CommandLevel.ADMIN:
        return "Dafuer brauchst du Discord-Administratorrechte."
    return "Dafuer hast du keine Berechtigung."


class SlashCommandContext:
    def __init__(
        self,
        interaction: discord.Interaction,
        attachments: list[discord.Attachment] | None = None,
    ) -> None:
        self.interaction = interaction
        self.bot = interaction.client
        self.author = interaction.user
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.message = SimpleNamespace(
            attachments=list(attachments or []),
            author=self.author,
            guild=self.guild,
            channel=self.channel,
            id=interaction.id,
        )

    async def reply(self, content: str, mention_author: bool = False) -> None:
        await respond_to_interaction(self.interaction, content)

    async def send(self, content: str) -> None:
        await respond_to_interaction(self.interaction, content)

    @asynccontextmanager
    async def typing(self):
        if not self.interaction.response.is_done():
            await self.interaction.response.defer(thinking=True)
        yield


async def respond_to_interaction(
    interaction: discord.Interaction,
    content: str,
    ephemeral: bool = False,
) -> None:
    content = content[:1900]
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=ephemeral)
        return
    await interaction.response.send_message(content, ephemeral=ephemeral)


async def sync_application_commands(bot: commands.Bot) -> None:
    guild_ids = slash_sync_guild_ids(bot)
    signature = slash_command_sync_signature(bot)
    if slash_sync_state_matches(bot, guild_ids, signature):
        log.info("slash commands unchanged; skipping Discord sync")
        return

    if guild_ids:
        guild_counts = await sync_guild_application_commands(bot, guild_ids)
        global_state = await clear_remote_global_commands(bot)
        if not guild_counts and global_state != "cleared":
            return
        if len(guild_counts) == len(guild_ids) and global_state in {"cleared", "already-empty"}:
            save_slash_sync_state(bot, guild_ids, signature, global_commands_cleared=True)
        log.info(
            "synced slash commands globally=%s guilds=%s",
            global_state,
            guild_counts or "none",
        )
        return

    try:
        synced = await bot.tree.sync()
    except Exception as exc:
        log.warning("could not sync slash commands globally: %s", exc)
        return
    save_slash_sync_state(bot, guild_ids, signature, global_commands_cleared=True)
    log.info("synced slash commands globally=%s guilds=none", len(synced))


async def sync_guild_application_commands(bot: commands.Bot, guild_ids: list[int]) -> dict[int, int]:
    guild_counts: dict[int, int] = {}
    for guild_id in guild_ids:
        guild = discord.Object(id=guild_id)
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
        except Exception as exc:
            log.warning("could not sync slash commands for guild %s: %s", guild_id, exc)
            continue
        guild_counts[guild_id] = len(synced)
    return guild_counts


async def clear_remote_global_commands(bot: commands.Bot) -> str:
    local_global_commands = list(bot.tree.get_commands(guild=None))
    fetch_commands = getattr(bot.tree, "fetch_commands", None)
    if callable(fetch_commands):
        try:
            remote_global_commands = await fetch_commands()
            if not remote_global_commands:
                return "already-empty"
        except Exception as exc:
            log.debug("could not inspect global slash commands before clearing: %s", exc)

    try:
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
    except Exception as exc:
        log.warning("could not clear global slash commands: %s", exc)
        return "clear-failed"
    finally:
        for command in local_global_commands:
            try:
                bot.tree.add_command(command)
            except app_commands.CommandAlreadyRegistered:
                continue
            except Exception:
                log.debug("could not restore local slash command %s", command.name, exc_info=True)
    return "cleared"


def slash_command_sync_signature(bot: commands.Bot) -> str:
    payload = []
    for command in sorted(bot.tree.get_commands(guild=None), key=lambda item: item.name):
        extras = getattr(command, "extras", {}) or {}
        spec = extras.get("caine_spec") if isinstance(extras, dict) else None
        item = {
            "name": str(getattr(command, "name", "")),
            "description": str(getattr(command, "description", "")),
            "plugin": extras.get("caine_plugin_name") if isinstance(extras, dict) else None,
        }
        if isinstance(spec, CommandSpec):
            item["spec"] = {
                "names": list(spec.names),
                "description": spec.description,
                "level": spec.level.value,
                "group": spec.group,
                "options": [
                    {
                        "name": option.name,
                        "description": option.description,
                        "type": option.type,
                        "required": option.required,
                    }
                    for option in spec.options
                ],
            }
        else:
            item["parameters"] = [
                {
                    "name": str(getattr(parameter, "name", "")),
                    "description": str(getattr(parameter, "description", "")),
                    "type": str(getattr(getattr(parameter, "type", ""), "name", getattr(parameter, "type", ""))),
                    "required": bool(getattr(parameter, "required", False)),
                }
                for parameter in getattr(command, "parameters", []) or []
            ]
        payload.append(item)

    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def slash_sync_state_matches(bot: commands.Bot, guild_ids: list[int], signature: str) -> bool:
    state = load_slash_sync_state(bot)
    if not state:
        return False
    mode = "guild" if guild_ids else "global"
    if state.get("mode") != mode:
        return False
    if state.get("guild_ids") != guild_ids:
        return False
    if state.get("signature") != signature:
        return False
    if guild_ids and not state.get("global_commands_cleared"):
        return False
    return True


def load_slash_sync_state(bot: commands.Bot) -> dict[str, Any]:
    path = slash_sync_state_path(bot)
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_slash_sync_state(
    bot: commands.Bot,
    guild_ids: list[int],
    signature: str,
    global_commands_cleared: bool,
) -> None:
    path = slash_sync_state_path(bot)
    if path is None:
        return
    payload = {
        "version": 1,
        "mode": "guild" if guild_ids else "global",
        "guild_ids": guild_ids,
        "signature": signature,
        "global_commands_cleared": global_commands_cleared,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        log.debug("could not save slash sync state: %s", exc)


def slash_sync_state_path(bot: commands.Bot) -> Path | None:
    settings = getattr(bot, "settings", None)
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is None:
        return None
    return Path(data_dir) / SLASH_SYNC_STATE_FILE


def slash_sync_guild_ids(bot: commands.Bot) -> list[int]:
    settings = getattr(bot, "settings", None)
    allowed_ids = set(getattr(settings, "allowed_guild_ids", set()) or set())
    if allowed_ids:
        return sorted(allowed_ids)

    guild_ids = {
        int(guild.id)
        for guild in getattr(bot, "guilds", []) or []
        if getattr(guild, "id", None) is not None
    }
    return sorted(guild_ids)
