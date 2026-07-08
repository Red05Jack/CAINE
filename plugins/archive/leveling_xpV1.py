import asyncio
import time
import re

PLUGIN = {
    "name": "Leveling XP",
    "description": "XP- und Levelsystem für Nachrichten und Voice-Chat-Zeit mit Admin-Befehlen zum Hinzufügen und Entfernen von XP."
}

# Balancing
MESSAGE_XP = 15
MESSAGE_COOLDOWN_SECONDS = 60
VOICE_XP_PER_MINUTE = 5
LEVEL_XP_FACTOR = 100  # Level n benötigt insgesamt n^2 * LEVEL_XP_FACTOR XP


async def setup_plugin(api):
    message_cooldowns = {}  # (guild_id, user_id) -> timestamp
    voice_sessions = {}     # (guild_id, user_id) -> timestamp of last checkpoint
    locks = {}              # storage-key -> asyncio.Lock

    def now():
        return time.time()

    def storage_key(guild_id, user_id):
        return f"leveling:v1:{guild_id}:{user_id}"

    def lock_for(key):
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
        return lock

    async def get_stats(guild_id, user_id):
        key = storage_key(guild_id, user_id)
        data = await api.storage_get(key)
        if not isinstance(data, dict):
            data = {}
        return {
            "xp": int(data.get("xp", 0) or 0),
            "messages": int(data.get("messages", 0) or 0),
            "voice_seconds": int(data.get("voice_seconds", 0) or 0),
            "voice_remainder_seconds": float(data.get("voice_remainder_seconds", 0) or 0),
        }

    async def set_stats(guild_id, user_id, stats):
        key = storage_key(guild_id, user_id)
        stats["xp"] = max(0, int(stats.get("xp", 0) or 0))
        stats["messages"] = max(0, int(stats.get("messages", 0) or 0))
        stats["voice_seconds"] = max(0, int(stats.get("voice_seconds", 0) or 0))
        stats["voice_remainder_seconds"] = max(0.0, float(stats.get("voice_remainder_seconds", 0) or 0))
        await api.storage_set(key, stats)

    async def mutate_stats(guild_id, user_id, mutator):
        key = storage_key(guild_id, user_id)
        async with lock_for(key):
            stats = await get_stats(guild_id, user_id)
            result = mutator(stats)
            await set_stats(guild_id, user_id, stats)
            return stats, result

    def level_for_xp(xp):
        xp = max(0, int(xp))
        return int((xp / LEVEL_XP_FACTOR) ** 0.5)

    def xp_for_level(level):
        level = max(0, int(level))
        return level * level * LEVEL_XP_FACTOR

    def format_duration(seconds):
        seconds = int(seconds)
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {secs}s"
        return f"{secs}s"

    def display_name(user):
        if user is None:
            return "Unbekannt"
        return getattr(user, "display_name", None) or getattr(user, "name", None) or str(user)

    def user_mention(user, user_id=None):
        mention = getattr(user, "mention", None)
        if mention:
            return mention
        if user_id is not None:
            return f"<@{user_id}>"
        return display_name(user)

    def get_ctx_author(ctx):
        return getattr(ctx, "author", None) or getattr(getattr(ctx, "message", None), "author", None) or getattr(ctx, "user", None)

    def get_ctx_guild(ctx):
        return getattr(ctx, "guild", None) or getattr(getattr(ctx, "message", None), "guild", None)

    def get_ctx_channel(ctx):
        return getattr(ctx, "channel", None) or getattr(getattr(ctx, "message", None), "channel", None)

    def get_ctx_mentions(ctx):
        mentions = getattr(ctx, "mentions", None)
        if mentions is None:
            mentions = getattr(getattr(ctx, "message", None), "mentions", None)
        return list(mentions or [])

    def args_to_text(args):
        if args is None:
            return ""
        if isinstance(args, str):
            return args.strip()
        if isinstance(args, (list, tuple)):
            return " ".join(str(x) for x in args).strip()
        return str(args).strip()

    def split_args(args):
        text = args_to_text(args)
        return text.split() if text else []

    def get_bot_guild(guild_id):
        bot = getattr(api, "bot", None)
        if bot is None:
            return None
        if hasattr(bot, "get_guild"):
            try:
                guild = bot.get_guild(int(guild_id))
                if guild is not None:
                    return guild
            except Exception:
                pass
        for guild in getattr(bot, "guilds", []) or []:
            if getattr(guild, "id", None) == guild_id:
                return guild
        return None

    def find_announcement_channel(guild):
        if guild is None:
            return None
        channel = getattr(guild, "system_channel", None)
        if channel is not None:
            return channel
        channel = getattr(guild, "public_updates_channel", None)
        if channel is not None:
            return channel
        for channel in getattr(guild, "text_channels", []) or []:
            return channel
        return None

    async def safe_channel_send(channel, content):
        if channel is None or not hasattr(channel, "send"):
            return False
        try:
            await channel.send(content)
            return True
        except Exception:
            return False

    async def send_level_up_announcement(guild_id, user_id, old_level, new_level, member=None, channel=None):
        if new_level <= old_level:
            return

        guild = getattr(member, "guild", None) if member is not None else None
        if guild is None:
            guild = get_bot_guild(guild_id)

        if member is None and guild is not None and hasattr(guild, "get_member"):
            try:
                member = guild.get_member(int(user_id))
            except Exception:
                member = None

        if channel is None:
            channel = find_announcement_channel(guild)

        text = f"🎉 Glückwunsch {user_mention(member, user_id)}! Du bist auf **Level {new_level}** aufgestiegen!"
        await safe_channel_send(channel, text)

    async def resolve_member(ctx, token=None, allow_self=True):
        guild = get_ctx_guild(ctx)
        author = get_ctx_author(ctx)
        mentions = get_ctx_mentions(ctx)
        if mentions:
            return mentions[0]
        if guild is None:
            return author if allow_self else None
        if token:
            match = re.search(r"\d{15,25}", str(token))
            if match:
                user_id = int(match.group(0))
                member = guild.get_member(user_id) if hasattr(guild, "get_member") else None
                if member is not None:
                    return member
                if hasattr(guild, "fetch_member"):
                    try:
                        return await guild.fetch_member(user_id)
                    except Exception:
                        pass
        return author if allow_self else None

    def has_manage_permission(ctx):
        author = get_ctx_author(ctx)
        perms = getattr(author, "guild_permissions", None)
        return bool(
            getattr(perms, "administrator", False)
            or getattr(perms, "manage_guild", False)
            or getattr(perms, "manage_messages", False)
        )

    async def add_message_xp(message):
        if getattr(message, "guild", None) is None:
            return
        author = getattr(message, "author", None)
        if author is None or getattr(author, "bot", False):
            return
        content = getattr(message, "content", "") or ""
        if not content.strip():
            return

        gid = message.guild.id
        uid = author.id
        key = (gid, uid)
        t = now()
        last = message_cooldowns.get(key, 0)
        if t - last < MESSAGE_COOLDOWN_SECONDS:
            return
        message_cooldowns[key] = t

        def mutator(stats):
            old_level = level_for_xp(stats["xp"])
            stats["xp"] += MESSAGE_XP
            stats["messages"] += 1
            new_level = level_for_xp(stats["xp"])
            return old_level, new_level

        _stats, levels = await mutate_stats(gid, uid, mutator)
        if levels:
            old_level, new_level = levels
            if new_level > old_level:
                await send_level_up_announcement(gid, uid, old_level, new_level, member=author, channel=getattr(message, "channel", None))

    async def award_voice_seconds(guild_id, user_id, elapsed_seconds, member=None, channel=None):
        elapsed_seconds = max(0.0, float(elapsed_seconds))
        if elapsed_seconds <= 0:
            return 0

        def mutator(stats):
            old_level = level_for_xp(stats["xp"])
            stats["voice_seconds"] += int(elapsed_seconds)
            remainder = float(stats.get("voice_remainder_seconds", 0) or 0) + elapsed_seconds
            full_minutes = int(remainder // 60)
            if full_minutes > 0:
                gained = full_minutes * VOICE_XP_PER_MINUTE
                stats["xp"] += gained
                remainder -= full_minutes * 60
            else:
                gained = 0
            stats["voice_remainder_seconds"] = remainder
            new_level = level_for_xp(stats["xp"])
            return gained, old_level, new_level

        _stats, result = await mutate_stats(guild_id, user_id, mutator)
        if not result:
            return 0
        gained, old_level, new_level = result
        if gained and new_level > old_level:
            await send_level_up_announcement(guild_id, user_id, old_level, new_level, member=member, channel=channel)
        return gained or 0

    def is_countable_voice(member, channel):
        if channel is None:
            return False
        guild = getattr(member, "guild", None)
        if guild is not None and getattr(guild, "afk_channel", None) is not None:
            if getattr(channel, "id", None) == getattr(guild.afk_channel, "id", None):
                return False
        return True

    async def start_voice_session(member):
        if member is None or getattr(member, "bot", False):
            return
        guild = getattr(member, "guild", None)
        if guild is None:
            return
        voice_sessions[(guild.id, member.id)] = now()

    async def stop_voice_session(member):
        if member is None or getattr(member, "bot", False):
            return
        guild = getattr(member, "guild", None)
        if guild is None:
            return
        key = (guild.id, member.id)
        started = voice_sessions.pop(key, None)
        if started is None:
            return
        await award_voice_seconds(guild.id, member.id, now() - started, member=member)

    async def checkpoint_voice_sessions():
        while True:
            await asyncio.sleep(60)
            t = now()
            for key, started in list(voice_sessions.items()):
                guild_id, user_id = key
                elapsed = t - started
                if elapsed <= 0:
                    continue
                voice_sessions[key] = t
                try:
                    guild = get_bot_guild(guild_id)
                    member = None
                    if guild is not None and hasattr(guild, "get_member"):
                        member = guild.get_member(int(user_id))
                    await award_voice_seconds(guild_id, user_id, elapsed, member=member)
                except Exception:
                    # Nicht den gesamten Hintergrund-Task abbrechen, falls ein einzelner Storage-Zugriff fehlschlägt.
                    pass

    async def seed_current_voice_sessions():
        bot = getattr(api, "bot", None)
        if bot is None:
            return
        for guild in getattr(bot, "guilds", []) or []:
            for channel in getattr(guild, "voice_channels", []) or []:
                for member in getattr(channel, "members", []) or []:
                    if not getattr(member, "bot", False) and is_countable_voice(member, channel):
                        voice_sessions[(guild.id, member.id)] = now()

    async def send_xp_card(ctx, member):
        guild = get_ctx_guild(ctx)
        if guild is None:
            await api.reply(ctx, "Dieser Befehl funktioniert nur auf einem Server.")
            return
        stats = await get_stats(guild.id, member.id)
        xp = int(stats["xp"])
        level = level_for_xp(xp)
        next_xp = xp_for_level(level + 1)
        current_level_xp = xp_for_level(level)
        progress = xp - current_level_xp
        needed = next_xp - current_level_xp
        remaining = max(0, next_xp - xp)
        await api.reply(
            ctx,
            f"**XP von {display_name(member)}**\n"
            f"Level: **{level}**\n"
            f"XP: **{xp}**\n"
            f"Fortschritt: **{progress}/{needed} XP** bis Level {level + 1} "
            f"(**{remaining} XP** fehlen)\n"
            f"Gewertete Nachrichten: **{stats['messages']}**\n"
            f"Voice-Zeit: **{format_duration(stats['voice_seconds'])}**"
        )

    @api.event("message")
    async def on_message(message):
        await add_message_xp(message)

    @api.event("voice_state_update")
    async def on_voice_state_update(member, before, after):
        if member is None or getattr(member, "bot", False):
            return
        before_channel = getattr(before, "channel", None)
        after_channel = getattr(after, "channel", None)
        before_counted = is_countable_voice(member, before_channel)
        after_counted = is_countable_voice(member, after_channel)

        if before_counted and not after_counted:
            await stop_voice_session(member)
        elif not before_counted and after_counted:
            await start_voice_session(member)
        elif before_counted and after_counted:
            # Channelwechsel oder Voice-State-Änderung: Session weiterlaufen lassen.
            key = (member.guild.id, member.id)
            if key not in voice_sessions:
                voice_sessions[key] = now()

    @api.event("ready")
    async def on_ready():
        await seed_current_voice_sessions()

    @api.command("xp", description="Zeigt deine XP und dein Level an. Optional: xp @User")
    async def xp_command(ctx, args):
        tokens = split_args(args)
        member = await resolve_member(ctx, tokens[0] if tokens else None, allow_self=True)
        if member is None:
            await api.reply(ctx, "Nutzer nicht gefunden.")
            return
        await send_xp_card(ctx, member)

    @api.command("level", description="Zeigt deine XP und dein Level an. Optional: level @User")
    async def level_command(ctx, args):
        await xp_command(ctx, args)

    @api.command("addxp", description="Fügt einem Nutzer XP hinzu. Nutzung: addxp @User <Menge>")
    async def addxp_command(ctx, args):
        if get_ctx_guild(ctx) is None:
            await api.reply(ctx, "Dieser Befehl funktioniert nur auf einem Server.")
            return
        if not has_manage_permission(ctx):
            await api.reply(ctx, "Dafür brauchst du `Server verwalten`, `Nachrichten verwalten` oder Administratorrechte.")
            return

        tokens = split_args(args)
        if len(tokens) < 2 and not get_ctx_mentions(ctx):
            await api.reply(ctx, "Nutzung: `addxp @User <Menge>`")
            return

        target_token = tokens[0] if tokens else None
        member = await resolve_member(ctx, target_token, allow_self=False)
        if member is None:
            await api.reply(ctx, "Nutzer nicht gefunden. Nutzung: `addxp @User <Menge>`")
            return

        amount = None
        for token in tokens:
            if re.fullmatch(r"[+]?\d+", token):
                # Wenn kein Mention genutzt wurde, ist das erste reine Zahlen-Token oft die User-ID.
                if not get_ctx_mentions(ctx) and token == target_token:
                    continue
                amount = int(token)
                break
        if amount is None or amount <= 0:
            await api.reply(ctx, "Bitte gib eine positive XP-Menge an. Beispiel: `addxp @User 250`")
            return

        guild = get_ctx_guild(ctx)

        def mutator(stats):
            old_level = level_for_xp(stats["xp"])
            stats["xp"] += amount
            new_level = level_for_xp(stats["xp"])
            return old_level, new_level

        stats, levels = await mutate_stats(guild.id, member.id, mutator)
        await api.reply(ctx, f"{display_name(member)} hat **{amount} XP** erhalten. Neuer Stand: **{stats['xp']} XP** / Level **{level_for_xp(stats['xp'])}**.")
        if levels:
            old_level, new_level = levels
            if new_level > old_level:
                await send_level_up_announcement(guild.id, member.id, old_level, new_level, member=member, channel=get_ctx_channel(ctx))

    @api.command("removexp", description="Entfernt einem Nutzer XP. Nutzung: removexp @User <Menge>")
    async def removexp_command(ctx, args):
        if get_ctx_guild(ctx) is None:
            await api.reply(ctx, "Dieser Befehl funktioniert nur auf einem Server.")
            return
        if not has_manage_permission(ctx):
            await api.reply(ctx, "Dafür brauchst du `Server verwalten`, `Nachrichten verwalten` oder Administratorrechte.")
            return

        tokens = split_args(args)
        if len(tokens) < 2 and not get_ctx_mentions(ctx):
            await api.reply(ctx, "Nutzung: `removexp @User <Menge>`")
            return

        target_token = tokens[0] if tokens else None
        member = await resolve_member(ctx, target_token, allow_self=False)
        if member is None:
            await api.reply(ctx, "Nutzer nicht gefunden. Nutzung: `removexp @User <Menge>`")
            return

        amount = None
        for token in tokens:
            if re.fullmatch(r"[+]?\d+", token):
                if not get_ctx_mentions(ctx) and token == target_token:
                    continue
                amount = int(token)
                break
        if amount is None or amount <= 0:
            await api.reply(ctx, "Bitte gib eine positive XP-Menge an. Beispiel: `removexp @User 250`")
            return

        guild = get_ctx_guild(ctx)

        def mutator(stats):
            removed = min(amount, stats["xp"])
            stats["xp"] = max(0, stats["xp"] - amount)
            return removed

        stats, removed = await mutate_stats(guild.id, member.id, mutator)
        await api.reply(ctx, f"{display_name(member)} wurden **{removed} XP** entfernt. Neuer Stand: **{stats['xp']} XP** / Level **{level_for_xp(stats['xp'])}**.")

    # Hintergrund-Task für laufende Voice-Sessions, damit Voice-XP auch ohne Leave-Event regelmäßig gespeichert wird.
    try:
        api.bot.loop.create_task(checkpoint_voice_sessions())
    except Exception:
        asyncio.create_task(checkpoint_voice_sessions())

    # Falls der Bot bereits bereit ist, vorhandene Voice-Sessions direkt erfassen.
    try:
        if getattr(api.bot, "is_ready", lambda: False)():
            await seed_current_voice_sessions()
    except Exception:
        pass
