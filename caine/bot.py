from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord.ext import commands
from openai import OpenAIError

from caine.command_system import (
    CommandLevel,
    CommandOption,
    CommandSpec,
    has_command_access,
    load_kinger_role_ids,
    register_dual_command,
    sync_application_commands,
    user_has_any_role,
)
from caine.config import Settings, load_settings
from caine.openai_agent import OpenAIAgent
from caine.plugin_manager import PluginManager, slugify
from caine.plugin_validation import PluginValidationError


log = logging.getLogger("caine")

MAX_REQUEST_ATTACHMENT_BYTES = 128 * 1024
TEXT_ATTACHMENT_EXTENSIONS = {
    ".cfg",
    ".conf",
    ".csv",
    ".ini",
    ".js",
    ".json",
    ".log",
    ".md",
    ".py",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}
TEXT_ATTACHMENT_CONTENT_TYPES = {
    "application/json",
    "application/toml",
    "application/x-yaml",
    "application/yaml",
}


class AttachmentInputError(ValueError):
    pass


CORE_COMMANDS = {
    "help": CommandSpec(
        ("help",),
        "Shows core commands and plugin details.",
        CommandLevel.USER,
        "General",
        (CommandOption("topic", "Plugin name for detailed help.", "string", False),),
    ),
    "ask": CommandSpec(
        ("ask",),
        "Asks C.A.I.N.E. via OpenAI.",
        CommandLevel.USER,
        "General",
        (CommandOption("prompt", "Question or prompt.", "string", True),),
    ),
    "plugins": CommandSpec(("plugins",), "Lists loaded plugins.", CommandLevel.USER, "General"),
    "evolve": CommandSpec(
        ("evolve",),
        "Creates a pending plugin from a feature request.",
        CommandLevel.ADMIN,
        "Plugin Lab",
        (
            CommandOption("request", "Feature request text.", "string", False),
            CommandOption("file", "Optional text attachment.", "attachment", False),
        ),
    ),
    "evolve_plugin": CommandSpec(
        ("evolve_plugin",),
        "Creates a pending update for an existing plugin.",
        CommandLevel.ADMIN,
        "Plugin Lab",
        (
            CommandOption("plugin_id", "Plugin ID to update.", "string", True),
            CommandOption("request", "Change request text.", "string", False),
            CommandOption("file", "Optional text attachment.", "attachment", False),
        ),
    ),
    "pending": CommandSpec(("pending",), "Lists pending plugin drafts.", CommandLevel.ADMIN, "Plugin Lab"),
    "review": CommandSpec(
        ("review",),
        "Shows pending plugin source.",
        CommandLevel.ADMIN,
        "Plugin Lab",
        (CommandOption("plugin_id", "Pending plugin ID.", "string", True),),
    ),
    "approve": CommandSpec(
        ("approve",),
        "Approves and loads a pending plugin.",
        CommandLevel.ADMIN,
        "Plugin Lab",
        (CommandOption("plugin_id", "Pending plugin ID.", "string", True),),
    ),
    "reject": CommandSpec(
        ("reject",),
        "Deletes a pending plugin draft.",
        CommandLevel.ADMIN,
        "Plugin Lab",
        (CommandOption("plugin_id", "Pending plugin ID.", "string", True),),
    ),
    "reload_plugins": CommandSpec(("reload_plugins",), "Reloads approved plugins.", CommandLevel.ADMIN, "Plugin Lab"),
    "health": CommandSpec(("health",), "Checks CAINE runtime state.", CommandLevel.KINGER, "System"),
}

COMMAND_LEVEL_HELP_GROUPS = (
    (CommandLevel.USER, "Commands"),
    (CommandLevel.ADMIN, "Admin commands"),
    (CommandLevel.KINGER, "Kinger"),
)

COMMAND_LEVEL_ORDER = {
    CommandLevel.USER: 0,
    CommandLevel.ADMIN: 1,
    CommandLevel.KINGER: 2,
}


class CaineBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(
            command_prefix=settings.command_prefix,
            intents=intents,
            help_command=None,
        )
        self.settings = settings
        self.agent = (
            OpenAIAgent(settings.openai_api_key, settings.openai_model, settings.trusted_plugins)
            if settings.openai_enabled
            else None
        )
        self.plugins = PluginManager(
            bot=self,
            pending_dir=settings.pending_plugins_dir,
            approved_dir=settings.approved_plugins_dir,
            data_dir=settings.data_dir,
            trusted_plugins=settings.trusted_plugins,
        )
        self._slash_synced_after_ready = False

    async def setup_hook(self) -> None:
        self.plugins.ensure_dirs()
        if self.settings.plugin_autoload:
            loaded = await self.plugins.load_all_approved()
            log.info("loaded %s approved plugins", len(loaded))
        await sync_application_commands(self)

    async def on_ready(self) -> None:
        guilds = ", ".join(guild.name for guild in self.guilds) or "no guilds"
        log.info("logged in as %s (%s)", self.user, guilds)
        if not self._slash_synced_after_ready:
            self._slash_synced_after_ready = True
            await sync_application_commands(self)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CheckFailure):
            await ctx.reply("Dafuer brauchst du CAINE-Adminrechte.", mention_author=False)
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(f"Es fehlt ein Argument: `{error.param.name}`", mention_author=False)
            return
        if isinstance(error, commands.CommandInvokeError) and isinstance(
            error.original, PluginValidationError
        ):
            await ctx.reply(f"Plugin-Validation fehlgeschlagen: `{str(error.original)[:900]}`", mention_author=False)
            return
        log.exception("command error", exc_info=error)
        await ctx.reply(f"Fehler: `{str(error)[:400]}`", mention_author=False)


def install_commands(bot: CaineBot) -> None:
    async def caine_help(ctx: commands.Context, args: str = "") -> None:
        topic = clean_help_topic(args)
        if topic:
            plugin_help = await build_plugin_help_text(bot, topic, ctx.author)
            if plugin_help is None:
                plugin_names = ", ".join(
                    f"`{plugin.name}`"
                    for plugin in sorted(bot.plugins.loaded.values(), key=lambda item: item.name)
                )
                await ctx.reply(
                    f"Plugin `{topic}` nicht gefunden. Geladene Plugins: {plugin_names or 'keine'}.",
                    mention_author=False,
                )
                return
            await send_long(ctx, plugin_help)
            return

        await send_long(ctx, await build_general_help_text(bot, ctx.author))

    async def ask(ctx: commands.Context, args: str = "") -> None:
        prompt = args.strip()
        if not prompt:
            await ctx.reply("Bitte gib eine Frage an.", mention_author=False)
            return
        if bot.agent is None:
            await ctx.reply("OpenAI ist nicht konfiguriert. Setze `OPENAI_API_KEY`.", mention_author=False)
            return
        try:
            async with ctx.typing():
                answer = await bot.agent.answer(prompt, ctx.author.display_name)
        except OpenAIError as exc:
            await ctx.reply(f"OpenAI-Fehler: `{str(exc)[:700]}`", mention_author=False)
            return
        await send_long(ctx, answer)

    async def evolve(ctx: commands.Context, args: str = "") -> None:
        if bot.agent is None:
            await ctx.reply("OpenAI ist nicht konfiguriert. Setze `OPENAI_API_KEY`.", mention_author=False)
            return
        try:
            request_text = await request_text_from_message(ctx, args)
        except AttachmentInputError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        if not request_text:
            await ctx.reply(
                "Bitte gib einen Plugin-Wunsch ein oder haenge eine Textdatei an.",
                mention_author=False,
            )
            return
        try:
            async with ctx.typing():
                draft = await bot.agent.create_plugin(request_text, ctx.author.display_name)
                plugin_id, path, validation = bot.plugins.save_pending(draft.name or draft.command_name, draft.code)
        except OpenAIError as exc:
            await ctx.reply(f"OpenAI-Fehler beim Generieren: `{str(exc)[:700]}`", mention_author=False)
            return

        status = "bereit fuer Review" if validation.ok else "Validator hat Probleme gefunden"
        details = validation.summary()
        await ctx.reply(
            "\n".join(
                [
                    f"Plugin-Vorschlag `{plugin_id}` gespeichert: {status}.",
                    f"Beschreibung: {draft.description}",
                    f"Command: `{bot.settings.command_prefix}{draft.command_name}`",
                    f"Datei: `{relative(path, bot.settings.project_root)}`",
                    f"Validation: `{details}`",
                    f"Aktivieren mit `{bot.settings.command_prefix}approve {plugin_id}`.",
                ]
            )[:1900],
            mention_author=False,
        )

    async def evolve_plugin(ctx: commands.Context, args: str = "") -> None:
        if bot.agent is None:
            await ctx.reply("OpenAI ist nicht konfiguriert. Setze `OPENAI_API_KEY`.", mention_author=False)
            return

        plugin_id, request = split_first_arg(args)
        if not plugin_id:
            await ctx.reply(
                "Bitte gib die Plugin-ID an, z. B. "
                f"`{bot.settings.command_prefix}evolve_plugin levelxp` plus Text oder Textdatei.",
                mention_author=False,
            )
            return

        try:
            request_text = await request_text_from_message(ctx, request)
        except AttachmentInputError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        if not request_text:
            await ctx.reply(
                "Bitte gib den Aenderungswunsch ein oder haenge eine Textdatei an.",
                mention_author=False,
            )
            return

        source_info = bot.plugins.find_plugin_source(plugin_id)
        if source_info is None:
            await ctx.reply(
                f"Plugin-Datei `{plugin_id}` nicht gefunden. Suche in `plugins/pending` und `plugins/approved`.",
                mention_author=False,
            )
            return

        current_source = source_info.path.read_text(encoding="utf-8")
        target_plugin_id = source_info.replaces or source_info.plugin_id
        try:
            async with ctx.typing():
                draft = await bot.agent.update_plugin(
                    target_plugin_id,
                    current_source,
                    request_text,
                    ctx.author.display_name,
                )
                pending_id, path, validation = bot.plugins.save_pending_revision(
                    target_plugin_id,
                    draft.code,
                    source_info.path,
                )
        except OpenAIError as exc:
            await ctx.reply(f"OpenAI-Fehler beim Bearbeiten: `{str(exc)[:700]}`", mention_author=False)
            return

        status = "bereit fuer Review" if validation.ok else "Validator hat Probleme gefunden"
        await ctx.reply(
            "\n".join(
                [
                    f"Update fuer `{target_plugin_id}` als pending `{pending_id}` gespeichert: {status}.",
                    f"Quelle: `{relative(source_info.path, bot.settings.project_root)}` ({source_info.location})",
                    f"Neue Datei: `{relative(path, bot.settings.project_root)}`",
                    f"Aenderung: {draft.change_summary}",
                    f"Validation: `{validation.summary()}`",
                    f"Aktivieren/alte Version ersetzen mit `{bot.settings.command_prefix}approve {pending_id}`.",
                ]
            )[:1900],
            mention_author=False,
        )

    async def pending(ctx: commands.Context, args: str = "") -> None:
        items = bot.plugins.list_pending()
        if not items:
            await ctx.reply("Keine wartenden Plugin-Vorschlaege.", mention_author=False)
            return
        lines = ["Wartende Plugins:"]
        for plugin_id, _, validation in items:
            marker = "OK" if validation.ok else "FEHLER"
            description = validation.metadata.get("description", "ohne Beschreibung")
            meta = bot.plugins.pending_metadata(plugin_id)
            replaces = meta.get("target_plugin_id")
            suffix = f" -> ersetzt `{replaces}`" if replaces else ""
            lines.append(f"- `{plugin_id}` [{marker}] {description}{suffix}")
        await ctx.reply("\n".join(lines)[:1900], mention_author=False)

    async def review(ctx: commands.Context, args: str = "") -> None:
        plugin_id = args.strip()
        if not plugin_id:
            await ctx.reply("Bitte gib die Plugin-ID an.", mention_author=False)
            return
        path = bot.plugins.pending_path(plugin_id)
        if not path.exists():
            await ctx.reply(f"`{plugin_id}` existiert nicht in pending.", mention_author=False)
            return
        source = path.read_text(encoding="utf-8")
        validation = bot.plugins.validate_source(source)
        header = f"Review `{path.stem}` - Validation: `{validation.summary()}`\n"
        await send_long(ctx, header + code_block(source))

    async def approve(ctx: commands.Context, args: str = "") -> None:
        plugin_id = args.strip()
        if not plugin_id:
            await ctx.reply("Bitte gib die Plugin-ID an.", mention_author=False)
            return
        async with ctx.typing():
            loaded = await bot.plugins.approve(plugin_id)
            await sync_application_commands(bot)
        commands_text = ", ".join(f"`{bot.settings.command_prefix}{name}`" for name in loaded.commands)
        await ctx.reply(
            f"Plugin `{loaded.name}` ist aktiv. Commands: {commands_text or 'keine'}",
            mention_author=False,
        )

    async def reject(ctx: commands.Context, args: str = "") -> None:
        plugin_id = args.strip()
        if not plugin_id:
            await ctx.reply("Bitte gib die Plugin-ID an.", mention_author=False)
            return
        removed = bot.plugins.reject(plugin_id)
        text = f"`{plugin_id}` geloescht." if removed else f"`{plugin_id}` nicht gefunden."
        await ctx.reply(text, mention_author=False)

    async def plugins(ctx: commands.Context, args: str = "") -> None:
        if not bot.plugins.loaded:
            await ctx.reply("Keine Plugins geladen.", mention_author=False)
            return
        lines = ["Geladene Plugins:"]
        for plugin in bot.plugins.loaded.values():
            commands_for_user = await registered_plugin_commands(bot, plugin, ctx.author)
            command_text = ", ".join(f"`{bot.settings.command_prefix}{command.name}`" for command in commands_for_user)
            lines.append(f"- `{plugin.name}`: {plugin.description} ({command_text})")
        await ctx.reply("\n".join(lines)[:1900], mention_author=False)

    async def reload_plugins(ctx: commands.Context, args: str = "") -> None:
        async with ctx.typing():
            loaded = await bot.plugins.reload_all_approved()
            await sync_application_commands(bot)
        await ctx.reply(f"{len(loaded)} Plugins neu geladen.", mention_author=False)

    async def health(ctx: commands.Context, args: str = "") -> None:
        env_status = "gesetzt" if bot.settings.discord_token else "fehlt"
        openai_status = "gesetzt" if bot.agent is not None else "fehlt"
        plugin_count = len(bot.plugins.loaded)
        lines = [
            "CAINE Health:",
            f"- Discord Token: {env_status}",
            f"- OpenAI Key: {openai_status}",
            f"- OpenAI Model: `{bot.settings.openai_model}`",
            f"- Geladene Plugins: {plugin_count}",
            f"- Trusted Plugins: `{bot.settings.trusted_plugins}`",
        ]
        if bot.agent is not None:
            async with ctx.typing():
                result = await bot.agent.health_check()
            lines.append(f"- OpenAI Ping: `{result[:300]}`")
        await ctx.reply("\n".join(lines), mention_author=False)

    handlers = {
        "help": caine_help,
        "ask": ask,
        "plugins": plugins,
        "evolve": evolve,
        "evolve_plugin": evolve_plugin,
        "pending": pending,
        "review": review,
        "approve": approve,
        "reject": reject,
        "reload_plugins": reload_plugins,
        "health": health,
    }
    for command_name, handler in handlers.items():
        register_dual_command(bot, CORE_COMMANDS[command_name], handler)


async def send_long(ctx: commands.Context, content: str) -> None:
    chunks = [content[index : index + 1900] for index in range(0, len(content), 1900)] or [""]
    for chunk in chunks[:5]:
        await ctx.reply(chunk, mention_author=False)


async def build_general_help_text(bot: commands.Bot, user: discord.abc.User | None = None) -> str:
    prefix = bot_command_prefix(bot)
    plugin_command_names = loaded_plugin_command_names(bot)
    lines = [
        "CAINE Help",
        "",
    ]

    visible_commands = []
    for command in bot.commands:
        if command.name in plugin_command_names or command.hidden:
            continue
        if await command_visible_to_user(bot, command, user):
            visible_commands.append(command)
    if visible_commands:
        visible_commands.sort(key=lambda command: (command_level_sort_key(command), command.name))
        lines.extend(format_commands_by_level(visible_commands, prefix))
    else:
        lines.append("Commands:")
        lines.append("- Keine fuer dich sichtbaren Commands.")

    lines.append("\nPlugins:")
    plugins = sorted(getattr(getattr(bot, "plugins", None), "loaded", {}).values(), key=lambda plugin: plugin.name)
    if not plugins:
        lines.append("- Keine Plugins geladen.")
    for plugin in plugins:
        command_count = len(await registered_plugin_commands(bot, plugin, user))
        description = plugin.description or "ohne Beschreibung"
        lines.append(
            f"- `{plugin.name}` ({command_count} Commands): {description} "
            f"Details: `{prefix}help {plugin.name}`"
        )

    return "\n".join(lines)[:3900]


async def build_plugin_help_text(
    bot: commands.Bot,
    plugin_query: str,
    user: discord.abc.User | None = None,
) -> str | None:
    plugin = find_loaded_plugin(bot, plugin_query)
    if plugin is None:
        return None

    prefix = bot_command_prefix(bot)
    lines = [
        f"Plugin `{plugin.name}`",
        plugin.description or "ohne Beschreibung",
        "",
    ]
    commands_for_plugin = await registered_plugin_commands(bot, plugin, user)
    if not commands_for_plugin:
        lines.append("Commands:")
        lines.append("- Keine fuer dich sichtbaren Commands.")
    else:
        lines.extend(format_commands_by_level(commands_for_plugin, prefix))

    return "\n".join(lines)[:3900]


async def registered_plugin_commands(
    bot: commands.Bot,
    plugin: object,
    user: discord.abc.User | None = None,
) -> list[commands.Command]:
    result = []
    for command_name in getattr(plugin, "commands", []):
        command = bot.get_command(command_name)
        if command is not None and not command.hidden and await command_visible_to_user(bot, command, user):
            result.append(command)
    return sorted(result, key=lambda command: (command_level_sort_key(command), command.name))


async def command_visible_to_user(
    bot: commands.Bot,
    command: commands.Command,
    user: discord.abc.User | None,
) -> bool:
    if user is None:
        return True
    spec = getattr(command, "caine_spec", None)
    level = getattr(spec, "level", CommandLevel.USER)
    return await has_command_access(bot, user, level)


def can_view_level_labels(bot: commands.Bot, user: discord.abc.User | None) -> bool:
    if user is None:
        return True
    return user_has_any_role(user, load_kinger_role_ids(bot))


def loaded_plugin_command_names(bot: commands.Bot) -> set[str]:
    manager = getattr(bot, "plugins", None)
    loaded = getattr(manager, "loaded", {})
    return {
        command_name
        for plugin in loaded.values()
        for command_name in getattr(plugin, "commands", [])
    }


def find_loaded_plugin(bot: commands.Bot, plugin_query: str) -> object | None:
    query = slugify(clean_help_topic(plugin_query))
    if not query:
        return None

    manager = getattr(bot, "plugins", None)
    plugins = sorted(getattr(manager, "loaded", {}).values(), key=lambda plugin: plugin.name)
    for plugin in plugins:
        candidates = {plugin.name, slugify(plugin.path.stem), slugify(plugin.description)}
        if query in candidates:
            return plugin

    for plugin in plugins:
        if query in {slugify(command_name) for command_name in plugin.commands}:
            return plugin

    for plugin in plugins:
        candidates = {plugin.name, slugify(plugin.path.stem), slugify(plugin.description)}
        if any(query in candidate or candidate in query for candidate in candidates if candidate):
            return plugin

    return None


def format_command_help_line(
    command: commands.Command,
    prefix: str,
    show_level_labels: bool = True,
) -> str:
    aliases = [alias for alias in command.aliases if alias != command.name]
    alias_text = ""
    if aliases:
        alias_values = ", ".join(f"`{prefix}{alias}`" for alias in aliases)
        alias_text = f" (Aliase: {alias_values})"
    spec = getattr(command, "caine_spec", None)
    level = f" {command_level_label(getattr(spec, 'level', None))}" if show_level_labels else ""
    description = getattr(spec, "description", None) or command.help or command.short_doc or "ohne Beschreibung"
    return f"- `{prefix}{command.name}`{level}{alias_text}: {description}"


def format_commands_by_level(
    command_list: list[commands.Command],
    prefix: str,
) -> list[str]:
    lines: list[str] = []
    for level, heading in COMMAND_LEVEL_HELP_GROUPS:
        group_commands = [command for command in command_list if command_level(command) == level]
        if not group_commands:
            continue
        if lines:
            lines.append("")
        lines.append(f"{heading}:")
        lines.extend(
            format_command_help_line(command, prefix, show_level_labels=False)
            for command in group_commands
        )
    return lines


def command_level(command: commands.Command) -> CommandLevel:
    spec = getattr(command, "caine_spec", None)
    level = getattr(spec, "level", CommandLevel.USER)
    if level == CommandLevel.KINGER or str(level).lower() == CommandLevel.KINGER.value:
        return CommandLevel.KINGER
    if level == CommandLevel.ADMIN or str(level).lower() == CommandLevel.ADMIN.value:
        return CommandLevel.ADMIN
    return CommandLevel.USER


def command_level_sort_key(command: commands.Command) -> int:
    return COMMAND_LEVEL_ORDER.get(command_level(command), COMMAND_LEVEL_ORDER[CommandLevel.USER])


def command_level_label(level: object) -> str:
    if level == CommandLevel.KINGER or str(level).lower() == CommandLevel.KINGER.value:
        return "[S1]"
    if level == CommandLevel.ADMIN or str(level).lower() == CommandLevel.ADMIN.value:
        return "[S2]"
    return "[S3]"


def bot_command_prefix(bot: commands.Bot) -> str:
    settings = getattr(bot, "settings", None)
    return str(getattr(settings, "command_prefix", "!") or "!")


def clean_help_topic(topic: str) -> str:
    return topic.strip().strip("\"'")


def split_first_arg(value: str) -> tuple[str, str]:
    parts = value.strip().split(maxsplit=1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1].strip()


async def request_text_from_message(ctx: commands.Context, request: str = "") -> str:
    parts = [request.strip()] if request.strip() else []
    attachments = list(getattr(ctx.message, "attachments", []) or [])
    skipped_files: list[str] = []

    for attachment in attachments:
        if not is_text_attachment(attachment):
            skipped_files.append(attachment.filename)
            continue

        size = int(getattr(attachment, "size", 0) or 0)
        if size > MAX_REQUEST_ATTACHMENT_BYTES:
            raise AttachmentInputError(
                f"`{attachment.filename}` ist zu gross. Maximal erlaubt sind "
                f"{MAX_REQUEST_ATTACHMENT_BYTES // 1024} KB."
            )

        try:
            content = await read_attachment_bytes(attachment)
        except discord.HTTPException as exc:
            raise AttachmentInputError(
                f"`{attachment.filename}` konnte nicht gelesen werden: {str(exc)[:200]}"
            ) from exc

        if len(content) > MAX_REQUEST_ATTACHMENT_BYTES:
            raise AttachmentInputError(
                f"`{attachment.filename}` ist zu gross. Maximal erlaubt sind "
                f"{MAX_REQUEST_ATTACHMENT_BYTES // 1024} KB."
            )

        text = decode_attachment_text(content, attachment.filename).strip()
        if text:
            parts.append(f"Datei {attachment.filename}:\n{text}")

    if parts:
        return "\n\n".join(parts).strip()

    if skipped_files:
        allowed = ", ".join(sorted(TEXT_ATTACHMENT_EXTENSIONS))
        raise AttachmentInputError(
            "Ich kann fuer diesen Befehl nur Textdateien lesen. "
            f"Erlaubte Endungen: {allowed}."
        )

    return ""


async def read_attachment_bytes(attachment: discord.Attachment) -> bytes:
    try:
        return await attachment.read()
    except discord.HTTPException as original_exc:
        try:
            return await attachment.read(use_cached=True)
        except discord.HTTPException:
            raise original_exc


def is_text_attachment(attachment: discord.Attachment) -> bool:
    content_type = (getattr(attachment, "content_type", None) or "").split(";")[0].strip().lower()
    if content_type.startswith("text/") or content_type in TEXT_ATTACHMENT_CONTENT_TYPES:
        return True
    return Path(attachment.filename).suffix.lower() in TEXT_ATTACHMENT_EXTENSIONS


def decode_attachment_text(content: bytes, filename: str) -> str:
    if b"\x00" in content:
        raise AttachmentInputError(f"`{filename}` sieht nicht wie eine Textdatei aus.")

    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise AttachmentInputError(f"`{filename}` konnte nicht als Text gelesen werden.")


def code_block(source: str) -> str:
    shortened = source[:1600]
    suffix = "\n# ... gekuerzt" if len(source) > len(shortened) else ""
    return f"```python\n{shortened}{suffix}\n```"


def relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()
    if not settings.discord_token:
        env_path = settings.project_root / ".env"
        if env_path.exists():
            raise RuntimeError(
                f"DISCORD_TOKEN fehlt in {env_path} oder ist noch ein Platzhalter. "
                "Trage dort deinen neu generierten Discord Bot Token ein."
            )
        raise RuntimeError(
            f"DISCORD_TOKEN fehlt. Lege {env_path} aus .env.example an und trage dort deinen Token ein."
        )

    bot = CaineBot(settings)
    install_commands(bot)
    bot.run(settings.discord_token)
