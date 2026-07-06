from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands


log = logging.getLogger(__name__)
DEFAULT_KINGER_ROLE_ID = 1523734381146148864


class CommandLevel(str, Enum):
    KINGER = "kinger"
    ADMIN = "admin"
    USER = "user"


@dataclass(frozen=True)
class CommandSpec:
    names: tuple[str, ...]
    description: str
    level: CommandLevel = CommandLevel.USER
    group: str = "Plugins"

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
    else:
        names = (str(name_or_object).strip(), *(str(item).strip() for item in aliases if str(item).strip()))

    names = tuple(dict.fromkeys(name for name in names if name))
    if not names:
        raise ValueError("command spec needs at least one name")

    return CommandSpec(
        names=names,
        description=description.strip() or "No description.",
        level=normalize_command_level(level),
        group=group,
    )


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
    slash_commands = []
    for slash_name in spec.names:
        async def callback(interaction: discord.Interaction, text: str = "") -> None:
            if not await ensure_interaction_allowed(interaction, spec):
                return
            await handler(SlashCommandContext(interaction), text)

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
    def __init__(self, interaction: discord.Interaction) -> None:
        self.interaction = interaction
        self.bot = interaction.client
        self.author = interaction.user
        self.guild = interaction.guild
        self.message = None

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
    global_count: int | None = None
    try:
        synced = await bot.tree.sync()
        global_count = len(synced)
    except Exception as exc:
        log.warning("could not sync slash commands: %s", exc)

    guild_counts: dict[int, int] = {}
    for guild_id in slash_sync_guild_ids(bot):
        guild = discord.Object(id=guild_id)
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
        except Exception as exc:
            log.warning("could not sync slash commands for guild %s: %s", guild_id, exc)
            continue
        guild_counts[guild_id] = len(synced)

    if global_count is None and not guild_counts:
        return
    log.info(
        "synced slash commands globally=%s guilds=%s",
        global_count if global_count is not None else "failed",
        guild_counts or "none",
    )


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
