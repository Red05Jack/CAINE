from __future__ import annotations

import logging
import re
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
from caine.openai_agent import (
    CommandRoute,
    OpenAIAgent,
    chatgpt_activity_log_path,
    read_chatgpt_activity_log,
    summarize_chatgpt_activity_entry,
)
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
CAINE_TRIGGER_SPELLINGS = (
    "caine",
    "c.a.i.n.e",
    "c-a-i-n-e",
    "c a i n e",
    "caine bot",
    "caine-bot",
)
CAINE_ROUTE_FILLER_WORDS = {
    "hey",
    "hi",
    "hallo",
    "servus",
    "bitte",
    "mal",
    "kurz",
}
CAINE_HELP_HINTS = (
    "was kann ich machen",
    "was kann ich tun",
    "was kannst du",
    "was geht",
    "hilfe",
    "help",
    "commands",
    "befehle",
    "commandliste",
)
CAINE_REPLY_APPROVAL_CONFIRMATIONS = {
    "approve",
    "aktivieren",
    "freigeben",
    "genehmigen",
    "ja",
    "ja bitte",
    "jo",
    "jo machen wir so",
    "mach",
    "mach das",
    "machen",
    "machen wir so",
    "ok",
    "okay",
    "passt",
    "passt so",
    "so machen",
    "yes",
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
    "chatgpt_logs": CommandSpec(
        ("chatgpt-logs", "ai-logs"),
        "Shows recent ChatGPT activity audit logs.",
        CommandLevel.KINGER,
        "System",
        (CommandOption("limit", "Number of recent entries.", "integer", False),),
    ),
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
            OpenAIAgent(
                settings.openai_api_key,
                settings.openai_model,
                settings.trusted_plugins,
                chatgpt_activity_log_path(settings.data_dir),
            )
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

    async def on_message(self, message: discord.Message) -> None:
        author = getattr(message, "author", None)
        if author is None or getattr(author, "bot", False):
            return

        ctx = await self.get_context(message)
        if ctx.valid:
            await self.invoke(ctx)
            return

        await route_caine_mention_to_command(self, message)


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
            try:
                loaded = await bot.plugins.approve(plugin_id)
            except FileNotFoundError:
                source_info = bot.plugins.find_plugin_source(plugin_id)
                if source_info is not None and source_info.location == "approved":
                    loaded = bot.plugins.loaded.get(source_info.plugin_id)
                    if loaded is None:
                        loaded = await bot.plugins.load_plugin_file(source_info.path)
                        await sync_application_commands(bot)
                        commands_text = ", ".join(f"`{bot.settings.command_prefix}{name}`" for name in loaded.commands)
                        await ctx.reply(
                            f"Plugin `{loaded.name}` war bereits approved und wurde geladen. "
                            f"Commands: {commands_text or 'keine'}",
                            mention_author=False,
                        )
                        return
                    commands_text = ", ".join(f"`{bot.settings.command_prefix}{name}`" for name in loaded.commands)
                    await ctx.reply(
                        f"Plugin `{loaded.name}` ist bereits aktiv. Kein pending Review offen. "
                        f"Commands: {commands_text or 'keine'}",
                        mention_author=False,
                    )
                    return
                await ctx.reply(f"`{plugin_id}` existiert nicht in pending.", mention_author=False)
                return
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

    async def chatgpt_logs(ctx: commands.Context, args: str = "") -> None:
        path = chatgpt_activity_log_path(bot.settings.data_dir)
        limit = parse_log_limit(args, default=10, minimum=1, maximum=25)
        entries = read_chatgpt_activity_log(path, limit)
        if not entries:
            await ctx.reply(f"Keine ChatGPT-Logs gefunden. Datei: `{relative(path, bot.settings.project_root)}`", mention_author=False)
            return
        lines = [
            "ChatGPT System-Logs:",
            f"Datei: `{relative(path, bot.settings.project_root)}`",
            "",
        ]
        lines.extend(f"- {summarize_chatgpt_activity_entry(entry)}" for entry in entries)
        await send_long(ctx, "\n".join(lines))

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
        "chatgpt_logs": chatgpt_logs,
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


async def route_caine_mention_to_command(bot: commands.Bot, message: discord.Message) -> bool:
    content = str(getattr(message, "content", "") or "")
    replied_caine_message = await resolve_replied_caine_message_content(bot, message)
    if not content_mentions_caine(content, getattr(bot, "user", None)) and not replied_caine_message:
        return False

    command_catalog = await build_command_routing_catalog(bot, getattr(message, "author", None))
    if not command_catalog:
        return False

    cleaned_request = strip_caine_triggers(content, getattr(bot, "user", None))
    route = local_caine_command_route(cleaned_request, command_catalog)
    if route is None and replied_caine_message:
        route = local_caine_reply_route(cleaned_request, replied_caine_message, command_catalog, bot_command_prefix(bot))
    if route is None:
        agent = getattr(bot, "agent", None)
        if agent is None:
            return False
        try:
            typing = getattr(getattr(message, "channel", None), "typing", None)
            if callable(typing):
                async with typing():
                    route = await agent.select_command_for_message(
                        content,
                        cleaned_request,
                        str(getattr(message.author, "display_name", getattr(message.author, "name", "User"))),
                        command_catalog,
                        bot_command_prefix(bot),
                        replied_caine_message,
                    )
            else:
                route = await agent.select_command_for_message(
                    content,
                    cleaned_request,
                    str(getattr(message.author, "display_name", getattr(message.author, "name", "User"))),
                    command_catalog,
                    bot_command_prefix(bot),
                    replied_caine_message,
                )
        except OpenAIError as exc:
            log.warning("could not route CAINE mention through OpenAI: %s", exc)
            return False
        except Exception as exc:
            log.warning("could not route CAINE mention: %s", exc)
            return False

    if route is None or not route.command_name or route.confidence < 0.35:
        return False

    command = bot.get_command(route.command_name)
    if command is None or not await command_visible_to_user(bot, command, getattr(message, "author", None)):
        return False

    ctx = await bot.get_context(message)
    try:
        await ctx.invoke(command, args=route.args)
        return True
    except commands.CommandError as exc:
        await bot.on_command_error(ctx, exc)
    except Exception as exc:
        log.exception("routed CAINE command failed", exc_info=exc)
        channel = getattr(message, "channel", None)
        if channel is not None and hasattr(channel, "send"):
            await channel.send(f"Fehler beim Ausfuehren von `{bot_command_prefix(bot)}{command.name}`.")
    return False


async def resolve_replied_caine_message_content(bot: commands.Bot, message: discord.Message) -> str:
    reference = getattr(message, "reference", None)
    if reference is None:
        return ""

    replied_message = getattr(reference, "resolved", None) or getattr(reference, "cached_message", None)
    if replied_message is None:
        message_id = getattr(reference, "message_id", None)
        channel = getattr(message, "channel", None)
        if message_id is not None and channel is not None and hasattr(channel, "fetch_message"):
            try:
                replied_message = await channel.fetch_message(message_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound) as exc:
                log.debug("could not fetch replied CAINE message: %s", exc)
                return ""
            except Exception as exc:
                log.debug("could not resolve replied CAINE message: %s", exc)
                return ""

    if not _same_discord_user(getattr(replied_message, "author", None), getattr(bot, "user", None)):
        return ""
    return str(getattr(replied_message, "content", "") or "").strip()


async def build_command_routing_catalog(
    bot: commands.Bot,
    user: discord.abc.User | None,
) -> list[dict[str, object]]:
    prefix = bot_command_prefix(bot)
    result = []
    for command in sorted(bot.commands, key=lambda item: item.name):
        if command.hidden or not await command_visible_to_user(bot, command, user):
            continue
        spec = getattr(command, "caine_spec", None)
        result.append(
            {
                "name": command.name,
                "aliases": list(command.aliases or []),
                "description": getattr(spec, "description", None) or command.help or command.short_doc or "",
                "level": command_level_label(getattr(spec, "level", CommandLevel.USER)),
                "usage": f"{prefix}{command.name}",
                "plugin": getattr(command, "caine_plugin_name", None) or "core",
            }
        )
    return result


def content_mentions_caine(content: str, bot_user: discord.abc.User | None = None) -> bool:
    if not content:
        return False
    if bot_user is not None:
        bot_id = getattr(bot_user, "id", None)
        if bot_id is not None and (f"<@{bot_id}>" in content or f"<@!{bot_id}>" in content):
            return True
    return any(_contains_caine_spelling(content, spelling) for spelling in CAINE_TRIGGER_SPELLINGS)


def strip_caine_triggers(content: str, bot_user: discord.abc.User | None = None) -> str:
    cleaned = str(content or "")
    if bot_user is not None:
        bot_id = getattr(bot_user, "id", None)
        if bot_id is not None:
            cleaned = cleaned.replace(f"<@{bot_id}>", " ").replace(f"<@!{bot_id}>", " ")
    for spelling in CAINE_TRIGGER_SPELLINGS:
        cleaned = _caine_spelling_pattern(spelling).sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,:;.!?")
    return cleaned.strip()


def local_caine_command_route(
    cleaned_request: str,
    command_catalog: list[dict[str, object]],
) -> CommandRoute | None:
    alias_to_name = command_alias_map(command_catalog)

    raw_tokens = cleaned_request.split()
    token_index = 0
    while token_index < len(raw_tokens):
        normalized_token = raw_tokens[token_index].strip(" ,:;.!?").lower()
        if normalized_token not in CAINE_ROUTE_FILLER_WORDS:
            break
        token_index += 1
    if token_index < len(raw_tokens):
        command_name = alias_to_name.get(raw_tokens[token_index].strip(" ,:;.!?").lower())
        if command_name:
            return CommandRoute(
                command_name=command_name,
                args=" ".join(raw_tokens[token_index + 1:]).strip(),
                confidence=1.0,
                reason="direct command",
            )

    normalized = cleaned_request.casefold()
    if any(hint in normalized for hint in CAINE_HELP_HINTS) and "help" in alias_to_name:
        return CommandRoute(command_name="help", args="", confidence=0.95, reason="help question")
    return None


def local_caine_reply_route(
    cleaned_request: str,
    replied_caine_message: str,
    command_catalog: list[dict[str, object]],
    prefix: str = "!",
) -> CommandRoute | None:
    alias_to_name = command_alias_map(command_catalog)
    if not is_caine_reply_approval(cleaned_request):
        return None
    route = extract_suggested_command_route(replied_caine_message, alias_to_name, prefix)
    if route is None:
        return None
    return route


def command_alias_map(command_catalog: list[dict[str, object]]) -> dict[str, str]:
    alias_to_name: dict[str, str] = {}
    for command in command_catalog:
        name = str(command.get("name", "")).lower()
        if not name:
            continue
        alias_to_name[name] = name
        aliases = command.get("aliases", [])
        if isinstance(aliases, list):
            for alias in aliases:
                alias_to_name[str(alias).lower()] = name
    return alias_to_name


def is_caine_reply_approval(cleaned_request: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(cleaned_request or "").casefold()).strip(" ,:;.!?")
    if normalized in CAINE_REPLY_APPROVAL_CONFIRMATIONS:
        return True
    return normalized.startswith(("ja ", "ok ", "okay ", "jo ")) and any(
        word in normalized for word in ("passt", "mach", "aktivier", "freigeb", "approve")
    )


def extract_suggested_command_route(
    replied_caine_message: str,
    alias_to_name: dict[str, str],
    prefix: str = "!",
) -> CommandRoute | None:
    suggestions = list(iter_suggested_command_routes(replied_caine_message, alias_to_name, prefix))
    if not suggestions:
        return None

    for route in reversed(suggestions):
        if route.command_name == "approve":
            return route
    return suggestions[-1]


def iter_suggested_command_routes(
    replied_caine_message: str,
    alias_to_name: dict[str, str],
    prefix: str = "!",
):
    prefixes = {str(prefix or "!"), "!", "/"}
    escaped_prefixes = "|".join(re.escape(item) for item in sorted(prefixes, key=len, reverse=True))
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_/-])(?:{escaped_prefixes})([A-Za-z][A-Za-z0-9_-]{{0,31}})(?:\s+([^\n`]*))?",
        re.IGNORECASE,
    )
    for match in pattern.finditer(str(replied_caine_message or "")):
        command_name = alias_to_name.get(match.group(1).casefold())
        if not command_name:
            continue
        args = clean_suggested_command_args(match.group(2) or "")
        yield CommandRoute(
            command_name=command_name,
            args=args,
            confidence=1.0,
            reason="confirmation reply to suggested command",
        )


def clean_suggested_command_args(args: str) -> str:
    cleaned = str(args or "").strip()
    cleaned = re.split(r"\s+(?:und|oder|mit|via)\s+[`']?[!/][A-Za-z]", cleaned, maxsplit=1)[0].strip()
    for _ in range(2):
        cleaned = cleaned.strip(" `'\t\r\n")
        cleaned = cleaned.rstrip(".,;:!)?]}")
        cleaned = cleaned.lstrip("([{'")
    return cleaned.strip()


def extract_approve_plugin_id(replied_caine_message: str, prefix: str = "!") -> str:
    route = extract_suggested_command_route(str(replied_caine_message or ""), {"approve": "approve"}, prefix)
    return route.args if route and route.command_name == "approve" else ""


def _contains_caine_spelling(content: str, spelling: str) -> bool:
    return bool(_caine_spelling_pattern(spelling).search(content))


def _caine_spelling_pattern(spelling: str) -> re.Pattern[str]:
    escaped = re.escape(spelling.casefold()).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", re.IGNORECASE)


def _same_discord_user(left: discord.abc.User | None, right: discord.abc.User | None) -> bool:
    if left is None or right is None:
        return False
    left_id = getattr(left, "id", None)
    right_id = getattr(right, "id", None)
    if left_id is not None and right_id is not None:
        return left_id == right_id
    return left == right


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


def parse_log_limit(args: str, default: int = 10, minimum: int = 1, maximum: int = 25) -> int:
    try:
        value = int(str(args or "").strip().split(maxsplit=1)[0])
    except (IndexError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


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
