from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord.ext import commands
from openai import OpenAIError

from caine.config import Settings, load_settings
from caine.openai_agent import OpenAIAgent
from caine.plugin_manager import PluginManager
from caine.plugin_validation import PluginValidationError, validate_plugin_source


log = logging.getLogger("caine")


class CaineBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(
            command_prefix=settings.command_prefix,
            intents=intents,
            help_command=commands.DefaultHelpCommand(no_category="CAINE"),
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

    async def setup_hook(self) -> None:
        self.plugins.ensure_dirs()
        if self.settings.plugin_autoload:
            loaded = await self.plugins.load_all_approved()
            log.info("loaded %s approved plugins", len(loaded))

    async def on_ready(self) -> None:
        guilds = ", ".join(guild.name for guild in self.guilds) or "no guilds"
        log.info("logged in as %s (%s)", self.user, guilds)

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


def is_caine_admin() -> commands.Check:
    async def predicate(ctx: commands.Context) -> bool:
        bot = ctx.bot
        if await bot.is_owner(ctx.author):
            return True
        if isinstance(ctx.author, discord.Member):
            if ctx.author.guild_permissions.administrator:
                return True
            settings = getattr(bot, "settings", None)
            allowed_role_names = getattr(settings, "admin_role_names", set())
            allowed_role_ids = getattr(settings, "admin_role_ids", set())
            if allowed_role_ids and any(role.id in allowed_role_ids for role in ctx.author.roles):
                return True
            if allowed_role_names and any(role.name in allowed_role_names for role in ctx.author.roles):
                return True
        return False

    return commands.check(predicate)


def in_allowed_guild() -> commands.Check:
    async def predicate(ctx: commands.Context) -> bool:
        settings = getattr(ctx.bot, "settings", None)
        allowed_ids = getattr(settings, "allowed_guild_ids", set())
        if not allowed_ids:
            return True
        return ctx.guild is not None and ctx.guild.id in allowed_ids

    return commands.check(predicate)


def install_commands(bot: CaineBot) -> None:
    @bot.command(name="ask", aliases=["frag"])
    @in_allowed_guild()
    async def ask(ctx: commands.Context, *, prompt: str) -> None:
        """Ask C.A.I.N.E. via OpenAI."""
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

    @bot.command(name="evolve", aliases=["entwickle"])
    @in_allowed_guild()
    @is_caine_admin()
    async def evolve(ctx: commands.Context, *, request: str) -> None:
        """Generate a pending plugin from a feature request."""
        if bot.agent is None:
            await ctx.reply("OpenAI ist nicht konfiguriert. Setze `OPENAI_API_KEY`.", mention_author=False)
            return
        try:
            async with ctx.typing():
                draft = await bot.agent.create_plugin(request, ctx.author.display_name)
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

    @bot.command(name="evolve_plugin", aliases=["modify_plugin", "update_plugin", "erweitere"])
    @in_allowed_guild()
    @is_caine_admin()
    async def evolve_plugin(ctx: commands.Context, plugin_id: str, *, request: str) -> None:
        """Update an existing plugin and save the new version as pending."""
        if bot.agent is None:
            await ctx.reply("OpenAI ist nicht konfiguriert. Setze `OPENAI_API_KEY`.", mention_author=False)
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
                    request,
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

    @bot.command(name="pending")
    @in_allowed_guild()
    @is_caine_admin()
    async def pending(ctx: commands.Context) -> None:
        """List pending plugin drafts."""
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

    @bot.command(name="review")
    @in_allowed_guild()
    @is_caine_admin()
    async def review(ctx: commands.Context, plugin_id: str) -> None:
        """Show pending plugin source."""
        path = bot.plugins.pending_path(plugin_id)
        if not path.exists():
            await ctx.reply(f"`{plugin_id}` existiert nicht in pending.", mention_author=False)
            return
        source = path.read_text(encoding="utf-8")
        validation = bot.plugins.validate_source(source)
        header = f"Review `{path.stem}` - Validation: `{validation.summary()}`\n"
        await send_long(ctx, header + code_block(source))

    @bot.command(name="approve")
    @in_allowed_guild()
    @is_caine_admin()
    async def approve(ctx: commands.Context, plugin_id: str) -> None:
        """Approve and load a pending plugin."""
        async with ctx.typing():
            loaded = await bot.plugins.approve(plugin_id)
        commands_text = ", ".join(f"`{bot.settings.command_prefix}{name}`" for name in loaded.commands)
        await ctx.reply(
            f"Plugin `{loaded.name}` ist aktiv. Commands: {commands_text or 'keine'}",
            mention_author=False,
        )

    @bot.command(name="reject", aliases=["ablehnen"])
    @in_allowed_guild()
    @is_caine_admin()
    async def reject(ctx: commands.Context, plugin_id: str) -> None:
        """Delete a pending plugin."""
        removed = bot.plugins.reject(plugin_id)
        text = f"`{plugin_id}` geloescht." if removed else f"`{plugin_id}` nicht gefunden."
        await ctx.reply(text, mention_author=False)

    @bot.command(name="plugins")
    @in_allowed_guild()
    async def plugins(ctx: commands.Context) -> None:
        """List loaded plugins."""
        if not bot.plugins.loaded:
            await ctx.reply("Keine Plugins geladen.", mention_author=False)
            return
        lines = ["Geladene Plugins:"]
        for plugin in bot.plugins.loaded.values():
            command_text = ", ".join(f"`{bot.settings.command_prefix}{name}`" for name in plugin.commands)
            lines.append(f"- `{plugin.name}`: {plugin.description} ({command_text})")
        await ctx.reply("\n".join(lines)[:1900], mention_author=False)

    @bot.command(name="reload_plugins")
    @in_allowed_guild()
    @is_caine_admin()
    async def reload_plugins(ctx: commands.Context) -> None:
        """Reload approved plugins."""
        async with ctx.typing():
            loaded = await bot.plugins.reload_all_approved()
        await ctx.reply(f"{len(loaded)} Plugins neu geladen.", mention_author=False)

    @bot.command(name="health")
    @in_allowed_guild()
    @is_caine_admin()
    async def health(ctx: commands.Context) -> None:
        """Check CAINE runtime state."""
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


async def send_long(ctx: commands.Context, content: str) -> None:
    chunks = [content[index : index + 1900] for index in range(0, len(content), 1900)] or [""]
    for chunk in chunks[:5]:
        await ctx.reply(chunk, mention_author=False)


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
