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
    PluginPatchApplyError,
    chatgpt_activity_log_path,
    read_chatgpt_activity_log,
    summarize_chatgpt_activity_entry,
)
from caine.plugin_manager import PluginManager, slugify
from caine.plugin_validation import PluginValidationError
from caine.storage import DiscordBackedPluginStorage


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


class AttachmentInputError(ValueError):
    pass


CORE_COMMANDS = {
    "help": CommandSpec(
        ("help",),
        "Shows core commands and plugin details.",
        CommandLevel.USER,
        "General",
        (CommandOption("topic", "Plugin name for detailed help.", "string", False),),
        routing_priority=35,
        routing_when="Use when the user explicitly asks for command syntax, a command list, or details for a named plugin.",
        routing_not_when="Do not use for vague help, small talk, general questions, or capability questions; use ask instead.",
    ),
    "ask": CommandSpec(
        ("ask",),
        "Asks C.A.I.N.E. via OpenAI.",
        CommandLevel.USER,
        "General",
        (CommandOption("prompt", "Question or prompt.", "string", True),),
        routing_priority=80,
        routing_when="Use for broad questions, small talk, vague requests, capability questions, or anything not clearly handled by a specific command.",
        routing_not_when="Do not use when the user clearly asks for an exact bot command with enough required arguments.",
    ),
    "plugins": CommandSpec(
        ("plugins",),
        "Lists loaded plugins.",
        CommandLevel.USER,
        "General",
        routing_priority=75,
        routing_when="Use when the user asks which plugins are loaded, active, running, or available right now.",
        routing_not_when="Do not use for detailed command help for one plugin; use help with the plugin name.",
    ),
    "evolve": CommandSpec(
        ("evolve",),
        "Creates a pending plugin from a feature request.",
        CommandLevel.ADMIN,
        "Plugin Lab",
        (
            CommandOption("request", "Feature request text.", "string", False),
            CommandOption("file", "Optional text attachment.", "attachment", False),
        ),
        routing_priority=90,
        routing_when="Use when an admin asks CAINE to create a new plugin or new bot feature.",
        routing_not_when="Do not use for normal chat, existing plugin changes, or feature questions without a create request.",
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
        routing_priority=90,
        routing_when="Use when an admin asks to update, repair, extend, or change an existing plugin.",
        routing_not_when="Do not use for creating a brand-new plugin; use evolve for that.",
    ),
    "pending": CommandSpec(
        ("pending",),
        "Lists pending plugin drafts.",
        CommandLevel.ADMIN,
        "Plugin Lab",
        routing_priority=70,
        routing_when="Use when an admin asks what plugin drafts are waiting for review or approval.",
        routing_not_when="Do not use to show source code for one draft; use review with the plugin id.",
    ),
    "review": CommandSpec(
        ("review",),
        "Shows pending plugin source.",
        CommandLevel.ADMIN,
        "Plugin Lab",
        (CommandOption("plugin_id", "Pending plugin ID.", "string", True),),
        routing_priority=80,
        routing_when="Use when an admin asks to inspect or show the source of a specific pending plugin.",
        routing_not_when="Do not use without a plugin id; use pending to list available drafts first.",
    ),
    "approve": CommandSpec(
        ("approve",),
        "Approves and loads a pending plugin.",
        CommandLevel.ADMIN,
        "Plugin Lab",
        (CommandOption("plugin_id", "Pending plugin ID.", "string", True),),
        routing_priority=85,
        routing_when="Use when an admin clearly wants to approve, activate, or load a specific pending plugin.",
        routing_not_when="Do not use without an explicit plugin id or when the user only wants to review code.",
    ),
    "reject": CommandSpec(
        ("reject",),
        "Deletes a pending plugin draft.",
        CommandLevel.ADMIN,
        "Plugin Lab",
        (CommandOption("plugin_id", "Pending plugin ID.", "string", True),),
        routing_priority=85,
        routing_when="Use when an admin clearly wants to reject or delete a specific pending plugin draft.",
        routing_not_when="Do not use without an explicit plugin id.",
    ),
    "reload_plugins": CommandSpec(
        ("reload_plugins",),
        "Reloads approved plugins.",
        CommandLevel.ADMIN,
        "Plugin Lab",
        routing_priority=70,
        routing_when="Use when an admin asks to reload approved plugins from disk.",
        routing_not_when="Do not use for approving pending plugins or listing loaded plugins.",
    ),
    "chatgpt_logs": CommandSpec(
        ("chatgpt-logs", "ai-logs"),
        "Shows recent ChatGPT activity audit logs.",
        CommandLevel.KINGER,
        "System",
        (CommandOption("limit", "Number of recent entries.", "integer", False),),
        routing_priority=70,
        routing_when="Use when a kinger asks for recent ChatGPT, OpenAI, AI, or router activity logs.",
        routing_not_when="Do not use for ordinary user questions about commands or plugins.",
    ),
    "health": CommandSpec(
        ("health",),
        "Checks CAINE runtime state.",
        CommandLevel.KINGER,
        "System",
        routing_priority=70,
        routing_when="Use when a kinger asks for CAINE runtime status, configured models, tokens-present status, or OpenAI ping.",
        routing_not_when="Do not use for a casual 'how are you' question; use ask for that.",
    ),
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

ROUTING_HINT_OVERRIDES = {
    "hello": {
        "priority": 20,
        "when": "Use only when the user explicitly wants to run the hello/greeting demo command.",
        "not_when": "Do not use for small talk such as 'wie gehts' or 'wie geht es dir'; use ask.",
    },
    "konto": {
        "priority": 80,
        "when": "Use when the user asks for their Glitzerchip or economy account balance/status.",
        "not_when": "Do not use for Discord account, OpenAI account, or vague account-management questions.",
    },
    "geld": {
        "priority": 60,
        "when": "Use when the user asks for the economy command overview covering account, daily, shop, games, transfers, or leaderboard.",
        "not_when": "Do not use for a personal balance question; use konto for that.",
    },
    "level-money": {
        "priority": 65,
        "when": "Use when the user asks how level-ups and Glitzerchip rewards are connected.",
        "not_when": "Do not use for personal rank cards, XP leaderboards, or economy balance.",
    },
    "level-money-info": {
        "priority": 65,
        "when": "Use when the user asks for info about level-up money rewards or the level/economy connection.",
        "not_when": "Do not use for personal rank cards, XP leaderboards, or economy balance.",
    },
    "levels": {
        "priority": 75,
        "when": "Use when the user asks for the server XP leaderboard, top ranks, or highest levels.",
        "not_when": "Do not use for one user's rank card; use rank for that.",
    },
    "rank": {
        "priority": 80,
        "when": "Use when the user asks for their own or another user's rank card, XP, or level.",
        "not_when": "Do not use for the server leaderboard; use levels for that.",
    },
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
                settings.openai_text_model,
                settings.trusted_plugins,
                chatgpt_activity_log_path(settings.data_dir),
                code_model=settings.openai_code_model,
                router_model=settings.openai_router_model,
            )
            if settings.openai_enabled
            else None
        )
        self.plugin_storage = DiscordBackedPluginStorage(self, legacy_data_dir=settings.data_dir)
        self.plugins = PluginManager(
            bot=self,
            pending_dir=settings.pending_plugins_dir,
            approved_dir=settings.approved_plugins_dir,
            data_dir=settings.data_dir,
            trusted_plugins=settings.trusted_plugins,
        )
        self._slash_synced_after_ready = False
        self._runtime_loaded_after_ready = False

    async def setup_hook(self) -> None:
        self.plugins.ensure_dirs()

    async def on_ready(self) -> None:
        guilds = ", ".join(guild.name for guild in self.guilds) or "no guilds"
        log.info("logged in as %s (%s)", self.user, guilds)
        if not self._runtime_loaded_after_ready:
            self._runtime_loaded_after_ready = True
            await self.plugin_storage.load()
            log.info("plugin db loaded from %s", self.plugin_storage.last_loaded_source)
            if self.settings.plugin_autoload:
                loaded = await self.plugins.load_all_approved()
                log.info("loaded %s approved plugins", len(loaded))
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
                    pending_activation_hint(bot.settings.command_prefix, plugin_id, validation),
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
        approved_source = approved_plugin_source_for_reference(bot, target_plugin_id, source_info.path)
        try:
            async with ctx.typing():
                draft = await bot.agent.update_plugin(
                    target_plugin_id,
                    current_source,
                    request_text,
                    ctx.author.display_name,
                    approved_source=approved_source,
                )
                pending_id, path, validation = bot.plugins.save_pending_revision(
                    target_plugin_id,
                    draft.code,
                    source_info.path,
                )
        except OpenAIError as exc:
            await ctx.reply(f"OpenAI-Fehler beim Bearbeiten: `{str(exc)[:700]}`", mention_author=False)
            return
        except PluginPatchApplyError as exc:
            await ctx.reply(
                "Update konnte nicht eindeutig angewendet werden: "
                f"`{str(exc)[:700]}`. Bitte versuch es mit einer etwas genaueren Aenderungsbeschreibung erneut.",
                mention_author=False,
            )
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
                    pending_activation_hint(bot.settings.command_prefix, pending_id, validation, replacement=True),
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
            except PluginValidationError as exc:
                await ctx.reply(
                    "Plugin-Validation fehlgeschlagen: "
                    f"`{str(exc)[:900]}`. Kein Approve ausgefuehrt. "
                    f"Pruefen mit `{bot.settings.command_prefix}review {plugin_id}`.",
                    mention_author=False,
                )
                return
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
            f"- OpenAI Text Model: `{bot.settings.openai_text_model}`",
            f"- OpenAI Router Model: `{bot.settings.openai_router_model}`",
            f"- OpenAI Code Model: `{bot.settings.openai_code_model}`",
            f"- Geladene Plugins: {plugin_count}",
            f"- Trusted Plugins: `{bot.settings.trusted_plugins}`",
            f"- Plugin DB: `{getattr(getattr(bot, 'plugin_storage', None), 'last_loaded_source', 'unbekannt')}`",
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


def pending_activation_hint(prefix: str, plugin_id: str, validation: object, replacement: bool = False) -> str:
    if getattr(validation, "ok", False):
        action = "Aktivieren/alte Version ersetzen" if replacement else "Aktivieren"
        return f"{action} mit `{prefix}approve {plugin_id}`."
    return (
        "Nicht aktivierbar, bis Validation OK ist. "
        f"Pruefen mit `{prefix}review {plugin_id}`."
    )


def approved_plugin_source_for_reference(bot: commands.Bot, plugin_id: str, edit_path: Path) -> str:
    plugins = getattr(bot, "plugins", None)
    approved_dir = getattr(plugins, "approved_dir", None)
    if approved_dir is None:
        return ""
    approved_path = Path(approved_dir) / f"{slugify(plugin_id)}.py"
    try:
        if approved_path.resolve() == Path(edit_path).resolve():
            return ""
        if not approved_path.exists():
            return ""
        return approved_path.read_text(encoding="utf-8")
    except OSError:
        return ""


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
                "options": command_routing_options(spec),
                "examples": command_examples(spec),
                "routing": command_routing_hints(command, spec),
            }
        )
    return result


def command_routing_options(spec: CommandSpec | None) -> list[dict[str, object]]:
    if not isinstance(spec, CommandSpec):
        return []
    return [
        {
            "name": option.name,
            "description": option.description,
            "type": option.type,
            "required": option.required,
        }
        for option in spec.options
    ]


def command_examples(spec: CommandSpec | None) -> list[str]:
    if not isinstance(spec, CommandSpec):
        return []
    return list(spec.examples)


def command_routing_hints(command: commands.Command, spec: CommandSpec | None) -> dict[str, object]:
    description = ""
    priority = 50
    when = ""
    not_when = ""
    if isinstance(spec, CommandSpec):
        description = spec.description
        priority = spec.routing_priority
        when = spec.routing_when
        not_when = spec.routing_not_when
    if not description:
        description = command.help or command.short_doc or command.name

    override = ROUTING_HINT_OVERRIDES.get(command.name)
    if override:
        priority = int(override.get("priority", priority))
        when = str(override.get("when", when))
        not_when = str(override.get("not_when", not_when))

    return {
        "priority": priority,
        "use_when": when or f"Use when the user asks for this behavior: {description}",
        "avoid_when": not_when or "Avoid when another command is a clearer match; use ask for unrelated general chat.",
    }


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
    line = f"- `{prefix}{command.name}`{level}{alias_text}: {description}"
    examples = command_examples(spec)
    if examples:
        example_text = ", ".join(f"`{example}`" for example in examples)
        line += f"\n  Beispiel: {example_text}"
    return line


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
