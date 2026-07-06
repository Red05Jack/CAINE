import asyncio
import datetime as _dt
import hashlib
import json
import math
import re
import secrets
import time

PLUGIN = {
    "name": "Leveling XP",
    "description": "XP- und Levelsystem mit getrennten Konten für Nachrichten, Voice, Invites und manuelle XP. Enthält Rank-/Admin-Befehle, Levelkurve, Snapshots und kleine digitale Manege für Fortschritt."
}

# Nachbau-nahe Defaults aus der Spezifikation. Ohne externe Config bleiben die alten Befehle nutzbar.
MESSAGE_MIN_XP = 15
MESSAGE_MAX_XP = 25
VOICE_MIN_XP_PER_BLOCK = 5
VOICE_MAX_XP_PER_BLOCK = 15
VOICE_BLOCK_SECONDS = 5 * 60
VOICE_CHECKPOINT_SECONDS = 60
VOICE_SINGLE_USER_MULTIPLIER = 0.5
VOICE_MULTI_USER_LOG_BONUS = 0.25
VOICE_STREAM_MULTIPLIER = 1.25
INVITE_REWARD_MIN_XP = 750
INVITE_REWARD_MAX_XP = 1250
INVITE_RETENTION_DAYS = 7
INVITE_CHECK_INTERVAL_SECONDS = 60 * 60
SNAPSHOT_DELAY_SECONDS = 3
SNAPSHOT_MARKER = "BOT_DB_V1"
DEFAULT_RANK_COLOR = "#FFFFFF"
# Wenn leer, bleibt das alte Verhalten erhalten: ManageGuild/ManageMessages/Admin darf Admin-XP nutzen.
BOT_MASTER_ROLE_IDS = set()


def _utc_now():
    return _dt.datetime.now(_dt.timezone.utc)


def _iso_now():
    return _utc_now().isoformat()


def _parse_iso(value):
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone(_dt.timezone.utc)
    except Exception:
        return None


async def setup_plugin(api):
    locks = {}
    voice_sessions = {}  # (guild_id, user_id) -> {started, awarded_blocks, channel_id, streaming}
    invite_cache = {}    # guild_id -> {code: {uses, inviter_id}}
    snapshot_tasks = {}

    def now_monotonic():
        return time.monotonic()

    def lock_for(key):
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
        return lock

    async def storage_delete(key):
        deleter = getattr(api, "storage_delete", None)
        if deleter is not None:
            await deleter(key)
        else:
            await api.storage_set(key, None)

    def key_account(guild_id, user_id):
        return f"leveling_xp:v2:account:{guild_id}:{user_id}"

    def key_legacy(guild_id, user_id):
        return f"leveling:v1:{guild_id}:{user_id}"

    def key_index(guild_id):
        return f"leveling_xp:v2:index:{guild_id}"

    def key_message(guild_id, message_id):
        return f"leveling_xp:v2:message:{guild_id}:{message_id}"

    def key_message_index(guild_id):
        return f"leveling_xp:v2:message_index:{guild_id}"

    def key_ref(guild_id, reference_id):
        digest = hashlib.sha256(str(reference_id).encode("utf-8")).hexdigest()
        return f"leveling_xp:v2:ref:{guild_id}:{digest}"

    def key_ledger(guild_id):
        return f"leveling_xp:v2:ledger:{guild_id}"

    def key_invite(guild_id, member_id):
        return f"leveling_xp:v2:invite_reward:{guild_id}:{member_id}"

    def key_invite_index(guild_id):
        return f"leveling_xp:v2:invite_index:{guild_id}"

    def key_snapshot(guild_id):
        return f"leveling_xp:v2:snapshot:{guild_id}"

    def normalize_color(value):
        value = str(value or DEFAULT_RANK_COLOR).strip().upper()
        if re.fullmatch(r"#[0-9A-F]{6}", value):
            return value
        return DEFAULT_RANK_COLOR

    def source_from_reason(reason):
        r = str(reason or "").lower()
        if r.startswith("voice"):
            return "voice_xp"
        if r.startswith("invite"):
            return "invite_xp"
        if r.startswith("manual") or r.startswith("givexp") or r.startswith("removexp"):
            return "manual_xp"
        return "message_xp"

    def total_xp(account):
        return max(0, int(account.get("message_xp", 0) or 0) + int(account.get("voice_xp", 0) or 0) + int(account.get("invite_xp", 0) or 0) + int(account.get("manual_xp", 0) or 0))

    def normalize_account(data):
        if not isinstance(data, dict):
            data = {}
        account = {
            "message_xp": max(0, int(data.get("message_xp", 0) or 0)),
            "voice_xp": max(0, int(data.get("voice_xp", 0) or 0)),
            "invite_xp": max(0, int(data.get("invite_xp", 0) or 0)),
            "manual_xp": int(data.get("manual_xp", 0) or 0),
            "messages": max(0, int(data.get("messages", data.get("message_count", 0)) or 0)),
            "voice_seconds": max(0, int(data.get("voice_seconds", 0) or 0)),
            "rank_color": normalize_color(data.get("rank_color", DEFAULT_RANK_COLOR)),
            "updated_at_utc": data.get("updated_at_utc") or _iso_now(),
        }
        account["total_xp"] = total_xp(account)
        level, progress, next_cost = level_info(account["total_xp"])
        account["current_level"] = level
        account["current_level_progress"] = progress
        account["xp_for_next_level"] = next_cost
        return account

    async def get_index(guild_id):
        data = await api.storage_get(key_index(guild_id))
        if isinstance(data, list):
            return [str(x) for x in data]
        return []

    async def add_to_index(guild_id, user_id):
        idx_key = key_index(guild_id)
        async with lock_for(idx_key):
            data = await api.storage_get(idx_key)
            users = [str(x) for x in data] if isinstance(data, list) else []
            uid = str(user_id)
            if uid not in users:
                users.append(uid)
                await api.storage_set(idx_key, users)

    async def get_account(guild_id, user_id):
        data = await api.storage_get(key_account(guild_id, user_id))
        if not isinstance(data, dict):
            # Migration aus der alten Plugin-Version: deren Gesamt-XP wird als ManualXp übernommen,
            # damit niemand XP verliert, auch wenn die alten Quellen nicht mehr trennbar sind.
            legacy = await api.storage_get(key_legacy(guild_id, user_id))
            if isinstance(legacy, dict):
                data = {
                    "manual_xp": int(legacy.get("xp", 0) or 0),
                    "messages": int(legacy.get("messages", 0) or 0),
                    "voice_seconds": int(legacy.get("voice_seconds", 0) or 0),
                    "rank_color": DEFAULT_RANK_COLOR,
                }
            else:
                data = {}
        return normalize_account(data)

    async def set_account(guild_id, user_id, account):
        account = normalize_account(account)
        account["updated_at_utc"] = _iso_now()
        await api.storage_set(key_account(guild_id, user_id), account)
        await add_to_index(guild_id, user_id)
        return account

    async def append_ledger(guild_id, movement):
        led_key = key_ledger(guild_id)
        async with lock_for(led_key):
            data = await api.storage_get(led_key)
            ledger = data if isinstance(data, list) else []
            ledger.append(movement)
            if len(ledger) > 1000:
                ledger = ledger[-1000:]
            await api.storage_set(led_key, ledger)

    def xp_for_next_level(current_level):
        current_level = max(0, int(current_level))
        # Python round nutzt Banker's Rounding und passt damit zu MidpointRounding.ToEven.
        return max(1, int(round(20.0 * math.pow(current_level + 1, 1.9))))

    def level_info(xp):
        remaining = max(0, int(xp or 0))
        level = 0
        while True:
            cost = xp_for_next_level(level)
            if remaining < cost:
                return level, remaining, cost
            remaining -= cost
            level += 1

    def level_for_xp(xp):
        return level_info(xp)[0]

    def deterministic_message_xp(message_id, min_xp=MESSAGE_MIN_XP, max_xp=MESSAGE_MAX_XP):
        mask = (1 << 64) - 1
        value = (int(message_id) + 0x9E3779B97F4A7C15) & mask
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & mask
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & mask
        value = (value ^ (value >> 31)) & mask
        return int(min_xp) + int(value % (int(max_xp) - int(min_xp) + 1))

    def compact_xp(value):
        value = int(value or 0)
        if abs(value) >= 1_000_000:
            return f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
        if abs(value) >= 1_000:
            return f"{value / 1_000:.2f}".rstrip("0").rstrip(".") + "K"
        return f"{value:,}".replace(",", ".")

    def format_duration(seconds):
        seconds = int(seconds or 0)
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

    def user_mention(user=None, user_id=None):
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

    def is_command_message(content):
        text = (content or "").strip()
        if not text:
            return False
        first = text.split(maxsplit=1)[0].lower()
        return first.startswith("!") or first in {
            "xp", "level", "addxp", "removexp", "givexp", "myrank", "set-rank-color",
            "recalculate", "importdb", "xp-liste", "einladungen-nachbearbeiten"
        }

    def get_bot_guild(guild_id):
        bot = getattr(api, "bot", None)
        if bot is None:
            return None
        try:
            guild = bot.get_guild(int(guild_id)) if hasattr(bot, "get_guild") else None
            if guild is not None:
                return guild
        except Exception:
            pass
        for guild in getattr(bot, "guilds", []) or []:
            if str(getattr(guild, "id", "")) == str(guild_id):
                return guild
        return None

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

    def is_bot_master(ctx):
        author = get_ctx_author(ctx)
        if author is None:
            return False
        role_ids = {int(getattr(role, "id", 0) or 0) for role in (getattr(author, "roles", []) or [])}
        if BOT_MASTER_ROLE_IDS and role_ids.intersection(BOT_MASTER_ROLE_IDS):
            return True
        return has_manage_permission(ctx)

    async def reply(ctx, content):
        await api.reply(ctx, content)

    async def safe_channel_send(channel, content=None, **kwargs):
        if channel is None or not hasattr(channel, "send"):
            return None
        try:
            if kwargs:
                return await channel.send(content, **kwargs)
            return await channel.send(content)
        except Exception:
            return None

    async def emit_log(message):
        try:
            await api.emit("leveling_xp_log", message)
        except Exception:
            pass

    def rank_title(level):
        if level >= 100:
            return "Die Legende"
        if level >= 75:
            return "Der Unaufhaltsame"
        if level >= 50:
            return "Der Champion"
        if level >= 25:
            return "Der Veteran"
        if level >= 10:
            return "Der Wortgewandte"
        if level >= 5:
            return "Der Aufsteiger"
        return "Der Frischgelevelte"

    def find_text_channel(guild, preferred_names=("level-ups", "mee6-xp-befehle")):
        if guild is None:
            return None
        for attr in ("system_channel", "public_updates_channel"):
            channel = getattr(guild, attr, None)
            if channel is not None:
                return channel
        names = {n.lower() for n in preferred_names}
        for channel in getattr(guild, "text_channels", []) or []:
            if str(getattr(channel, "name", "")).lower() in names:
                return channel
        channels = getattr(guild, "text_channels", []) or []
        return channels[0] if channels else None

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
            channel = find_text_channel(guild, ("level-ups",))
        next_cost = xp_for_next_level(new_level)
        account = await get_account(guild_id, user_id)
        text = (
            f"Endlich, {user_mention(member, user_id)} - {rank_title(new_level)} - hat Level **{new_level}** erreicht!\n"
            f"Gesamt-XP: **{account['total_xp']}** | Bis Level {new_level + 1}: **{max(0, next_cost - account['current_level_progress'])} XP**"
        )
        sent = await safe_channel_send(channel, text)
        if sent is None:
            await emit_log("[LevelUpSend] Konnte Level-Up nicht senden. Benötigt: View Channel und Send Messages.")

    async def apply_xp(guild_id, user_id, amount, reason, reference_id, extra_mutator=None):
        guild_id = str(guild_id)
        user_id = str(user_id)
        amount = int(amount)
        reference_id = str(reference_id)
        ref_key = key_ref(guild_id, reference_id)
        acc_key = key_account(guild_id, user_id)
        async with lock_for(acc_key):
            if await api.storage_get(ref_key):
                account = await get_account(guild_id, user_id)
                return {"applied": False, "account": account}
            account = await get_account(guild_id, user_id)
            old_total = total_xp(account)
            old_level = level_for_xp(old_total)
            source = source_from_reason(reason)
            account[source] = int(account.get(source, 0) or 0) + amount
            if source != "manual_xp":
                account[source] = max(0, int(account[source]))
            if extra_mutator is not None:
                extra_mutator(account)
            account = await set_account(guild_id, user_id, account)
            new_total = total_xp(account)
            new_level = level_for_xp(new_total)
            movement = {
                "id": hashlib.sha256(f"{guild_id}:{user_id}:{reference_id}".encode("utf-8")).hexdigest(),
                "guild_id": guild_id,
                "user_id": user_id,
                "amount": amount,
                "reason": str(reason),
                "reference_id": reference_id,
                "created_at_utc": _iso_now(),
                "old_xp": old_total,
                "new_xp": new_total,
                "old_level": old_level,
                "new_level": new_level,
                "applied": True,
            }
            await api.storage_set(ref_key, movement)
            await append_ledger(guild_id, movement)
            schedule_snapshot(guild_id)
            return {"applied": True, "movement": movement, "account": account}

    async def add_message_index(guild_id, message_id):
        idx_key = key_message_index(guild_id)
        async with lock_for(idx_key):
            data = await api.storage_get(idx_key)
            ids = [str(x) for x in data] if isinstance(data, list) else []
            mid = str(message_id)
            if mid not in ids:
                ids.append(mid)
                await api.storage_set(idx_key, ids)

    async def remove_message_index(guild_id, message_id):
        idx_key = key_message_index(guild_id)
        async with lock_for(idx_key):
            data = await api.storage_get(idx_key)
            ids = [str(x) for x in data] if isinstance(data, list) else []
            mid = str(message_id)
            if mid in ids:
                ids = [x for x in ids if x != mid]
                await api.storage_set(idx_key, ids)

    async def award_message_xp(message):
        if getattr(message, "guild", None) is None:
            return
        author = getattr(message, "author", None)
        if author is None or getattr(author, "bot", False):
            return
        content = getattr(message, "content", "") or ""
        if not content.strip() or is_command_message(content):
            return
        guild_id = str(message.guild.id)
        user_id = str(author.id)
        message_id = str(getattr(message, "id", ""))
        if not message_id:
            return
        msg_key = key_message(guild_id, message_id)
        if await api.storage_get(msg_key):
            return
        xp = deterministic_message_xp(int(message_id))
        record = {
            "guild_id": guild_id,
            "channel_id": str(getattr(getattr(message, "channel", None), "id", "0")),
            "message_id": message_id,
            "user_id": user_id,
            "xp": xp,
            "created_at_utc": _iso_now(),
        }
        await api.storage_set(msg_key, record)
        await add_message_index(guild_id, message_id)
        result = await apply_xp(
            guild_id,
            user_id,
            xp,
            "message",
            f"message:{message_id}",
            extra_mutator=lambda acc: acc.__setitem__("messages", int(acc.get("messages", 0) or 0) + 1),
        )
        if result.get("applied"):
            movement = result["movement"]
            if movement["new_level"] > movement["old_level"]:
                await send_level_up_announcement(guild_id, user_id, movement["old_level"], movement["new_level"], member=author, channel=getattr(message, "channel", None))

    async def rollback_message_xp(guild_id, message_id):
        if guild_id is None or message_id is None:
            return
        guild_id = str(guild_id)
        message_id = str(message_id)
        msg_key = key_message(guild_id, message_id)
        record = await api.storage_get(msg_key)
        if not isinstance(record, dict):
            return
        user_id = str(record.get("user_id"))
        xp = int(record.get("xp", 0) or 0)
        await storage_delete(msg_key)
        await remove_message_index(guild_id, message_id)
        await apply_xp(
            guild_id,
            user_id,
            -xp,
            "message-delete",
            f"message-delete:{message_id}",
            extra_mutator=lambda acc: acc.__setitem__("messages", max(0, int(acc.get("messages", 0) or 0) - 1)),
        )

    def is_countable_voice(member, channel):
        if channel is None or member is None:
            return False
        if getattr(member, "bot", False):
            return False
        guild = getattr(member, "guild", None)
        if guild is not None and getattr(guild, "afk_channel", None) is not None:
            if getattr(channel, "id", None) == getattr(guild.afk_channel, "id", None):
                return False
        return True

    def participant_count(channel):
        return len([m for m in (getattr(channel, "members", []) or []) if not getattr(m, "bot", False)])

    def voice_multiplier(channel, member=None):
        count = max(1, participant_count(channel) if channel is not None else 1)
        if count <= 1:
            participant = VOICE_SINGLE_USER_MULTIPLIER
        elif count == 2:
            participant = 1.0
        else:
            participant = 1.0 + math.log(count - 1, 2) * VOICE_MULTI_USER_LOG_BONUS
        streaming = bool(getattr(member, "self_stream", False) or getattr(getattr(member, "voice", None), "self_stream", False))
        return participant * (VOICE_STREAM_MULTIPLIER if streaming else 1.0), count, streaming

    async def award_voice_blocks(member, session, channel=None, closing=False):
        if member is None or session is None:
            return 0
        guild = getattr(member, "guild", None)
        if guild is None:
            return 0
        elapsed = max(0, now_monotonic() - float(session.get("started", now_monotonic())))
        total_blocks = int(elapsed // VOICE_BLOCK_SECONDS)
        already = int(session.get("awarded_blocks", 0) or 0)
        new_blocks = max(0, total_blocks - already)
        if new_blocks <= 0:
            return 0
        raw = sum(secrets.randbelow(VOICE_MAX_XP_PER_BLOCK - VOICE_MIN_XP_PER_BLOCK + 1) + VOICE_MIN_XP_PER_BLOCK for _ in range(new_blocks))
        mult, count, streaming = voice_multiplier(channel, member)
        gained = int(math.floor(raw * mult + 0.5))
        session["awarded_blocks"] = total_blocks
        minutes = new_blocks * (VOICE_BLOCK_SECONDS // 60)
        await emit_log(f"[DEBUG] [VOICE-XP-PROZENT] User={getattr(member, 'id', '?')} | Kanal={getattr(channel, 'id', '?')} | VC-User={count} | Stream={'ja' if streaming else 'nein'} | XP-Prozent={mult * 100:.0f}")
        if gained <= 0:
            return 0
        result = await apply_xp(
            str(guild.id),
            str(member.id),
            gained,
            "voice",
            f"voice:{guild.id}:{member.id}:{int(time.time())}:{total_blocks}:{secrets.token_hex(4)}",
            extra_mutator=lambda acc: acc.__setitem__("voice_seconds", int(acc.get("voice_seconds", 0) or 0) + minutes * 60),
        )
        if result.get("applied"):
            movement = result["movement"]
            if movement["new_level"] > movement["old_level"]:
                await send_level_up_announcement(guild.id, member.id, movement["old_level"], movement["new_level"], member=member)
        return gained

    async def start_voice_session(member, channel):
        if member is None or not is_countable_voice(member, channel):
            return
        guild = getattr(member, "guild", None)
        if guild is None:
            return
        voice_sessions[(str(guild.id), str(member.id))] = {
            "started": now_monotonic(),
            "awarded_blocks": 0,
            "channel_id": str(getattr(channel, "id", "0")),
        }

    async def stop_voice_session(member, channel=None):
        if member is None:
            return
        guild = getattr(member, "guild", None)
        if guild is None:
            return
        key = (str(guild.id), str(member.id))
        session = voice_sessions.pop(key, None)
        if session is not None:
            await award_voice_blocks(member, session, channel=channel, closing=True)

    async def checkpoint_voice_sessions():
        while True:
            await asyncio.sleep(VOICE_CHECKPOINT_SECONDS)
            for (guild_id, user_id), session in list(voice_sessions.items()):
                try:
                    guild = get_bot_guild(guild_id)
                    member = guild.get_member(int(user_id)) if guild is not None and hasattr(guild, "get_member") else None
                    if member is None:
                        continue
                    channel = getattr(getattr(member, "voice", None), "channel", None)
                    if not is_countable_voice(member, channel):
                        await stop_voice_session(member, channel=channel)
                        continue
                    await award_voice_blocks(member, session, channel=channel)
                except Exception as exc:
                    await emit_log(f"[VoiceCheckpoint] {type(exc).__name__}")

    async def seed_current_voice_sessions():
        bot = getattr(api, "bot", None)
        if bot is None:
            return
        for guild in getattr(bot, "guilds", []) or []:
            for channel in (getattr(guild, "voice_channels", []) or []) + (getattr(guild, "stage_channels", []) or []):
                for member in getattr(channel, "members", []) or []:
                    if is_countable_voice(member, channel):
                        await start_voice_session(member, channel)

    async def load_invites_for_guild(guild):
        cache = {}
        if guild is None or not hasattr(guild, "invites"):
            return cache
        try:
            invites = await guild.invites()
            for inv in invites or []:
                code = str(getattr(inv, "code", ""))
                if not code:
                    continue
                inviter = getattr(inv, "inviter", None)
                cache[code] = {
                    "uses": int(getattr(inv, "uses", 0) or 0),
                    "inviter_id": str(getattr(inviter, "id", "0") or "0"),
                }
        except Exception as exc:
            await emit_log(f"[InviteLoad] {type(exc).__name__}: Bot benötigt meist Manage Server, um Invites zu lesen.")
        return cache

    async def refresh_invite_cache(guild):
        if guild is None:
            return {}
        cache = await load_invites_for_guild(guild)
        invite_cache[str(guild.id)] = cache
        return cache

    async def add_invite_index(guild_id, member_id):
        idx_key = key_invite_index(guild_id)
        async with lock_for(idx_key):
            data = await api.storage_get(idx_key)
            ids = [str(x) for x in data] if isinstance(data, list) else []
            mid = str(member_id)
            if mid not in ids:
                ids.append(mid)
                await api.storage_set(idx_key, ids)

    async def store_pending_invite(member, inviter_id, invite_code):
        guild = getattr(member, "guild", None)
        if guild is None or not inviter_id or str(inviter_id) == "0":
            return
        if str(inviter_id) == str(getattr(member, "id", "")):
            return
        reward_xp = secrets.randbelow(INVITE_REWARD_MAX_XP - INVITE_REWARD_MIN_XP + 1) + INVITE_REWARD_MIN_XP
        reward = {
            "id": hashlib.sha256(f"{guild.id}:{member.id}:{invite_code}:{_iso_now()}".encode("utf-8")).hexdigest(),
            "guild_id": str(guild.id),
            "member_id": str(member.id),
            "inviter_id": str(inviter_id),
            "invite_code": str(invite_code),
            "joined_at_utc": _iso_now(),
            "reward_xp": reward_xp,
            "reward_given": False,
            "rewarded_at_utc": None,
            "auto_process": True,
        }
        await api.storage_set(key_invite(guild.id, member.id), reward)
        await add_invite_index(guild.id, member.id)
        schedule_snapshot(str(guild.id))

    async def check_due_invite_rewards(guild=None):
        guilds = [guild] if guild is not None else (getattr(getattr(api, "bot", None), "guilds", []) or [])
        total_rewarded = 0
        cutoff = _utc_now() - _dt.timedelta(days=INVITE_RETENTION_DAYS)
        for g in guilds:
            if g is None:
                continue
            guild_id = str(getattr(g, "id", ""))
            ids = await api.storage_get(key_invite_index(guild_id))
            for member_id in ([str(x) for x in ids] if isinstance(ids, list) else []):
                reward = await api.storage_get(key_invite(guild_id, member_id))
                if not isinstance(reward, dict) or reward.get("reward_given") or not reward.get("auto_process"):
                    continue
                joined = _parse_iso(reward.get("joined_at_utc"))
                if joined is None or joined > cutoff:
                    continue
                member = g.get_member(int(member_id)) if hasattr(g, "get_member") else None
                if member is None:
                    await storage_delete(key_invite(guild_id, member_id))
                    continue
                xp = int(reward.get("reward_xp", 0) or 0)
                inviter_id = str(reward.get("inviter_id"))
                result = await apply_xp(guild_id, inviter_id, xp, "invite-reward", f"invite-reward:{reward.get('id')}")
                if result.get("applied"):
                    reward["reward_given"] = True
                    reward["rewarded_at_utc"] = _iso_now()
                    await api.storage_set(key_invite(guild_id, member_id), reward)
                    total_rewarded += 1
                    movement = result["movement"]
                    inviter = g.get_member(int(inviter_id)) if hasattr(g, "get_member") else None
                    if movement["new_level"] > movement["old_level"]:
                        await send_level_up_announcement(guild_id, inviter_id, movement["old_level"], movement["new_level"], member=inviter)
        return total_rewarded

    async def invite_reward_loop():
        while True:
            await asyncio.sleep(INVITE_CHECK_INTERVAL_SECONDS)
            try:
                await check_due_invite_rewards()
            except Exception as exc:
                await emit_log(f"[InviteRewardLoop] {type(exc).__name__}")

    async def handle_member_join(member):
        guild = getattr(member, "guild", None)
        if guild is None or getattr(member, "bot", False):
            return
        old = invite_cache.get(str(guild.id), {})
        new = await load_invites_for_guild(guild)
        used_code = None
        inviter_id = None
        for code, data in new.items():
            if int(data.get("uses", 0) or 0) > int(old.get(code, {}).get("uses", 0) or 0):
                used_code = code
                inviter_id = data.get("inviter_id")
                break
        invite_cache[str(guild.id)] = new
        if used_code and inviter_id:
            await store_pending_invite(member, inviter_id, used_code)

    async def handle_member_leave(member):
        guild = getattr(member, "guild", None)
        if guild is None:
            return
        guild_id = str(guild.id)
        member_id = str(getattr(member, "id", ""))
        reward = await api.storage_get(key_invite(guild_id, member_id))
        if not isinstance(reward, dict):
            return
        if reward.get("reward_given"):
            xp = int(reward.get("reward_xp", 0) or 0)
            inviter_id = str(reward.get("inviter_id"))
            await apply_xp(guild_id, inviter_id, -xp, "invite-revocation", f"invite-revocation:{reward.get('id')}")
        await storage_delete(key_invite(guild_id, member_id))
        schedule_snapshot(guild_id)

    def schedule_snapshot(guild_id):
        guild_id = str(guild_id)
        old = snapshot_tasks.get(guild_id)
        if old is not None and not old.done():
            old.cancel()
        async def delayed():
            try:
                await asyncio.sleep(SNAPSHOT_DELAY_SECONDS)
                await write_snapshot(guild_id)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                await emit_log(f"[Snapshot] {type(exc).__name__}")
        try:
            snapshot_tasks[guild_id] = asyncio.create_task(delayed())
        except Exception:
            pass

    async def build_snapshot(guild_id):
        users = []
        for uid in await get_index(guild_id):
            acc = await get_account(guild_id, uid)
            users.append({
                "userId": int(uid) if str(uid).isdigit() else uid,
                "totalXp": total_xp(acc),
                "messageXp": int(acc.get("message_xp", 0) or 0),
                "voiceXp": int(acc.get("voice_xp", 0) or 0),
                "inviteXp": int(acc.get("invite_xp", 0) or 0),
                "rankColor": normalize_color(acc.get("rank_color")),
                "manualXp": int(acc.get("manual_xp", 0) or 0),
            })
        users.sort(key=lambda u: int(u["userId"]) if str(u["userId"]).isdigit() else 0)
        return {
            "version": 1,
            "guildId": int(guild_id) if str(guild_id).isdigit() else guild_id,
            "generatedAtUtc": _iso_now(),
            "users": users,
        }

    async def find_or_create_db_channel(guild):
        if guild is None:
            return None
        for channel in getattr(guild, "text_channels", []) or []:
            if str(getattr(channel, "name", "")).lower() == "bot-db":
                return channel
        if hasattr(guild, "create_text_channel"):
            try:
                return await guild.create_text_channel("bot-db")
            except Exception:
                return None
        return None

    async def write_snapshot(guild_id):
        snapshot = await build_snapshot(guild_id)
        await api.storage_set(key_snapshot(guild_id), snapshot)
        guild = get_bot_guild(guild_id)
        channel = await find_or_create_db_channel(guild)
        if channel is None:
            return
        text = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        if len(text) <= 1800:
            await safe_channel_send(channel, f"{SNAPSHOT_MARKER}\n```json\n{text}\n```")
        else:
            # Ohne Annahme einer konkreten Discord-Datei-API bleibt zumindest ein Hinweis im Kanal;
            # die vollständige Kopie liegt zuverlässig im Plugin-Storage.
            await safe_channel_send(channel, f"{SNAPSHOT_MARKER}\nSnapshot ist zu groß für Inline-Ausgabe; vollständige JSON-Kopie liegt im Plugin-Storage `{key_snapshot(guild_id)}`.")

    async def merge_snapshot(guild_id, snapshot):
        if not isinstance(snapshot, dict):
            return 0
        if str(snapshot.get("guildId")) != str(guild_id):
            raise ValueError("Snapshot gehört zu einer anderen Guild.")
        count = 0
        for user in snapshot.get("users", []) or []:
            if not isinstance(user, dict) or user.get("userId") is None:
                continue
            uid = str(user.get("userId"))
            async with lock_for(key_account(guild_id, uid)):
                acc = await get_account(guild_id, uid)
                msg = int(user.get("messageXp", 0) or 0)
                voice = int(user.get("voiceXp", 0) or 0)
                invite = int(user.get("inviteXp", 0) or 0)
                if "manualXp" in user:
                    manual = int(user.get("manualXp", 0) or 0)
                else:
                    total = int(user.get("totalXp", 0) or 0)
                    manual = max(0, total - msg - voice - invite)
                acc["message_xp"] = max(int(acc.get("message_xp", 0) or 0), msg)
                acc["voice_xp"] = max(int(acc.get("voice_xp", 0) or 0), voice)
                acc["invite_xp"] = max(int(acc.get("invite_xp", 0) or 0), invite)
                acc["manual_xp"] = max(int(acc.get("manual_xp", 0) or 0), manual)
                acc["rank_color"] = normalize_color(user.get("rankColor", acc.get("rank_color")))
                await set_account(guild_id, uid, acc)
                count += 1
        schedule_snapshot(guild_id)
        return count

    async def rank_position(guild, target_user_id):
        guild_id = str(getattr(guild, "id", "0"))
        rows = []
        for uid in await get_index(guild_id):
            member = guild.get_member(int(uid)) if guild is not None and hasattr(guild, "get_member") and str(uid).isdigit() else None
            if member is not None and getattr(member, "bot", False):
                continue
            acc = await get_account(guild_id, uid)
            rows.append((total_xp(acc), int(uid) if str(uid).isdigit() else 0, uid))
        rows.sort(key=lambda x: (-x[0], x[1]))
        for i, (_, _, uid) in enumerate(rows, start=1):
            if str(uid) == str(target_user_id):
                return i
        return len(rows) + 1

    async def send_rank(ctx, member, debug=False):
        guild = get_ctx_guild(ctx)
        if guild is None:
            await reply(ctx, "Dieser Befehl funktioniert nur auf einem Server.")
            return
        account = await get_account(guild.id, member.id)
        level, progress, next_cost = level_info(account["total_xp"])
        missing = max(0, next_cost - progress)
        rank = await rank_position(guild, member.id)
        if debug:
            await reply(ctx,
                f"[DEBUG] Rank-Daten für {user_mention(member, member.id)}\n"
                f"User-ID: `{member.id}`\n"
                f"Rang: **#{rank}** | Level: **{level}**\n"
                f"Gesamt-XP: **{account['total_xp']}** | Fortschritt: **{progress}/{next_cost}**\n"
                f"Nachrichten-XP: **{account['message_xp']}**\n"
                f"Voice-XP: **{account['voice_xp']}**\n"
                f"Invite-XP: **{account['invite_xp']}**\n"
                f"Manual-XP: **{account['manual_xp']}**\n"
                f"Gewertete Nachrichten: **{account['messages']}**\n"
                f"Voice-Zeit: **{format_duration(account['voice_seconds'])}**\n"
                f"Rank-Farbe: **{account['rank_color']}**"
            )
            return
        await reply(ctx,
            f"🎪 **Rank-Karte: {display_name(member)}**\n"
            f"RANG **#{rank}** · LEVEL **{level}** · Farbe `{account['rank_color']}`\n"
            f"Gesamt-XP: **{compact_xp(account['total_xp'])}** | Noch **{compact_xp(missing)} XP** bis Level {level + 1}\n"
            f"Fortschritt: **{progress}/{next_cost} XP**\n"
            f"Text **{account['message_xp']}** · Voice **{account['voice_xp']}** · Invite **{account['invite_xp']}** · Manual **{account['manual_xp']}**\n"
            f"Gewertete Nachrichten: **{account['messages']}** · Voice-Zeit: **{format_duration(account['voice_seconds'])}**"
        )

    def parse_amount_after_target(tokens, target_token, mentions_used):
        for token in tokens:
            if re.fullmatch(r"[+]?\d+", token):
                if not mentions_used and target_token is not None and token == target_token:
                    continue
                return int(token)
        return None

    async def manual_xp_command(ctx, args, sign):
        if get_ctx_guild(ctx) is None:
            await reply(ctx, "Dieser Befehl funktioniert nur auf einem Server.")
            return
        if not is_bot_master(ctx):
            await reply(ctx, "Dafür brauchst du Bot-Master-Rechte oder Server-/Nachrichtenverwaltung. Die Manege bleibt sonst verriegelt!")
            return
        tokens = split_args(args)
        if len(tokens) < 2 and not get_ctx_mentions(ctx):
            await reply(ctx, "Nutzung: `givexp @User 100 [Grund]` oder `removexp @User 100 [Grund]`")
            return
        target_token = tokens[0] if tokens else None
        member = await resolve_member(ctx, target_token, allow_self=False)
        if member is None:
            await reply(ctx, "Nutzer nicht gefunden.")
            return
        amount = parse_amount_after_target(tokens, target_token, bool(get_ctx_mentions(ctx)))
        if amount is None or amount <= 0:
            await reply(ctx, "Bitte gib eine positive XP-Menge an.")
            return
        guild = get_ctx_guild(ctx)
        signed_amount = amount if sign > 0 else -amount
        command_name = "manual-give" if sign > 0 else "manual-remove"
        # Optionaler Grund: alles nach dem Betrag.
        amount_seen = False
        reason_parts = []
        for token in tokens[1:]:
            if not amount_seen and re.fullmatch(r"[+]?\d+", token) and int(token) == amount:
                amount_seen = True
                continue
            if amount_seen:
                reason_parts.append(token)
        reason = command_name + ((":" + " ".join(reason_parts)) if reason_parts else "")
        msg = getattr(ctx, "message", None)
        msg_id = getattr(msg, "id", int(time.time() * 1000))
        result = await apply_xp(guild.id, member.id, signed_amount, reason, f"{command_name}:{msg_id}:{member.id}")
        account = result["account"]
        action = "erhalten" if sign > 0 else "entfernt bekommen"
        await reply(ctx, f"{user_mention(member, member.id)} hat **{amount} XP** {action}. Manual-XP: **{account['manual_xp']}**, Gesamt-XP: **{account['total_xp']}**, Level **{account['current_level']}**.")
        if result.get("applied"):
            movement = result["movement"]
            if movement["new_level"] > movement["old_level"]:
                await send_level_up_announcement(guild.id, member.id, movement["old_level"], movement["new_level"], member=member, channel=get_ctx_channel(ctx))

    async def read_attachment_json(attachment):
        if hasattr(attachment, "read"):
            data = await attachment.read()
            return json.loads(data.decode("utf-8"))
        text = getattr(attachment, "text", None)
        if text:
            return json.loads(text)
        raise ValueError("Anhang kann nicht gelesen werden.")

    @api.event("message")
    async def on_message(message):
        await award_message_xp(message)

    @api.event("message_delete")
    @api.event("message_deleted")
    async def on_message_deleted(message):
        guild = getattr(message, "guild", None)
        guild_id = getattr(guild, "id", None) or getattr(message, "guild_id", None)
        await rollback_message_xp(guild_id, getattr(message, "id", None))

    @api.event("bulk_message_delete")
    @api.event("messages_bulk_deleted")
    async def on_messages_bulk_deleted(messages):
        for message in messages or []:
            guild = getattr(message, "guild", None)
            guild_id = getattr(guild, "id", None) or getattr(message, "guild_id", None)
            await rollback_message_xp(guild_id, getattr(message, "id", None))

    @api.event("voice_state_update")
    async def on_voice_state_update(member, before, after):
        if member is None or getattr(member, "bot", False):
            return
        before_channel = getattr(before, "channel", None)
        after_channel = getattr(after, "channel", None)
        before_counted = is_countable_voice(member, before_channel)
        after_counted = is_countable_voice(member, after_channel)
        if before_counted and not after_counted:
            await stop_voice_session(member, channel=before_channel)
        elif not before_counted and after_counted:
            await start_voice_session(member, after_channel)
        elif before_counted and after_counted and getattr(before_channel, "id", None) != getattr(after_channel, "id", None):
            await stop_voice_session(member, channel=before_channel)
            await start_voice_session(member, after_channel)
        elif after_counted:
            key = (str(member.guild.id), str(member.id))
            if key not in voice_sessions:
                await start_voice_session(member, after_channel)

    @api.event("member_join")
    @api.event("guild_member_add")
    async def on_member_join(member):
        await handle_member_join(member)

    @api.event("member_remove")
    @api.event("member_leave")
    @api.event("guild_member_remove")
    async def on_member_leave(member):
        await handle_member_leave(member)

    @api.event("invite_create")
    @api.event("invite_delete")
    async def on_invite_changed(invite):
        guild = getattr(invite, "guild", None)
        if guild is not None:
            await refresh_invite_cache(guild)

    @api.event("ready")
    async def on_ready():
        await seed_current_voice_sessions()
        bot = getattr(api, "bot", None)
        for guild in getattr(bot, "guilds", []) or []:
            await refresh_invite_cache(guild)

    @api.command("xp", description="Zeigt deine XP und dein Level an. Optional: xp @User")
    async def xp_command(ctx, args):
        tokens = split_args(args)
        member = await resolve_member(ctx, tokens[0] if tokens else None, allow_self=True)
        if member is None:
            await reply(ctx, "Nutzer nicht gefunden.")
            return
        await send_rank(ctx, member, debug=False)

    @api.command("level", description="Zeigt deine XP und dein Level an. Optional: level @User")
    async def level_command(ctx, args):
        await xp_command(ctx, args)

    @api.command("myrank", description="Zeigt deine Rank-Karte. Optional: myrank @User [debug]")
    async def myrank_command(ctx, args):
        tokens = split_args(args)
        debug = any(str(t).lower() == "debug" for t in tokens)
        if debug and not is_bot_master(ctx):
            await reply(ctx, "Debug-Rank ist nur für Bot-Master. Die Geheimklappe bleibt zu.")
            return
        member = await resolve_member(ctx, tokens[0] if tokens else None, allow_self=True)
        if member is None:
            await reply(ctx, "Nutzer nicht gefunden.")
            return
        await send_rank(ctx, member, debug=debug)

    @api.command("set-rank-color", description="Setzt deine Rank-Farbe. Nutzung: set-rank-color #FFFFFF")
    async def set_rank_color_command(ctx, args):
        guild = get_ctx_guild(ctx)
        author = get_ctx_author(ctx)
        if guild is None or author is None:
            await reply(ctx, "Dieser Befehl funktioniert nur auf einem Server.")
            return
        tokens = split_args(args)
        if not tokens or not re.fullmatch(r"#[0-9A-Fa-f]{6}", tokens[0]):
            await reply(ctx, "Bitte nutze exakt `#` plus sechs Hex-Zeichen, z. B. `#00A1FF`.")
            return
        async with lock_for(key_account(guild.id, author.id)):
            acc = await get_account(guild.id, author.id)
            acc["rank_color"] = normalize_color(tokens[0])
            await set_account(guild.id, author.id, acc)
        schedule_snapshot(str(guild.id))
        await reply(ctx, f"Rank-Farbe gespeichert: **{acc['rank_color']}**. Das Konfetti hat jetzt eine Farbe!")

    @api.command("addxp", description="Fügt einem Nutzer Manual-XP hinzu. Nutzung: addxp @User <Menge>")
    async def addxp_command(ctx, args):
        await manual_xp_command(ctx, args, +1)

    @api.command("givexp", description="Fügt einem Nutzer Manual-XP hinzu. Nutzung: givexp @User <Menge> [Grund]")
    async def givexp_command(ctx, args):
        await manual_xp_command(ctx, args, +1)

    @api.command("removexp", description="Entfernt einem Nutzer Manual-XP. Nutzung: removexp @User <Menge> [Grund]")
    async def removexp_command(ctx, args):
        await manual_xp_command(ctx, args, -1)

    @api.command("xp-liste", description="Gibt die aktuelle XP-Liste aus. Erfordert Server verwalten.")
    async def xp_liste_command(ctx, args):
        guild = get_ctx_guild(ctx)
        if guild is None:
            await reply(ctx, "Dieser Befehl funktioniert nur auf einem Server.")
            return
        if not has_manage_permission(ctx):
            await reply(ctx, "Für `/xp-liste` brauchst du Serververwaltung.")
            return
        rows = []
        for uid in await get_index(guild.id):
            member = guild.get_member(int(uid)) if hasattr(guild, "get_member") and str(uid).isdigit() else None
            if member is not None and getattr(member, "bot", False):
                continue
            acc = await get_account(guild.id, uid)
            rows.append((acc["total_xp"], int(uid) if str(uid).isdigit() else 0, uid, acc))
        rows.sort(key=lambda x: (-x[0], x[1]))
        if not rows:
            await reply(ctx, "Noch keine XP-Daten vorhanden.")
            return
        pages = []
        current = ""
        for rank, (_, _, uid, acc) in enumerate(rows, start=1):
            level, progress, next_cost = level_info(acc["total_xp"])
            line = f"#{rank} <@{uid}> · Lvl {level} · {acc['total_xp']} XP · {progress}/{next_cost} · T:{acc['message_xp']} V:{acc['voice_xp']} I:{acc['invite_xp']} M:{acc['manual_xp']} · Msg:{acc['messages']}\n"
            if len(current) + len(line) > 1450:
                pages.append(current)
                current = line
            else:
                current += line
        if current:
            pages.append(current)
        for i, page in enumerate(pages, start=1):
            await reply(ctx, f"🎪 **XP-Liste Seite {i}/{len(pages)}**\n{page}")

    @api.command("einladungen-nachbearbeiten", description="Schaltet pending Invite-Rewards frei und prüft sofort. Erfordert Server verwalten.")
    async def invite_backfill_command(ctx, args):
        guild = get_ctx_guild(ctx)
        if guild is None:
            await reply(ctx, "Dieser Befehl funktioniert nur auf einem Server.")
            return
        if not has_manage_permission(ctx):
            await reply(ctx, "Für diese Nummer brauchst du Serververwaltung.")
            return
        ids = await api.storage_get(key_invite_index(guild.id))
        unlocked = 0
        for member_id in ([str(x) for x in ids] if isinstance(ids, list) else []):
            reward = await api.storage_get(key_invite(guild.id, member_id))
            if isinstance(reward, dict) and not reward.get("auto_process"):
                reward["auto_process"] = True
                await api.storage_set(key_invite(guild.id, member_id), reward)
                unlocked += 1
        rewarded = await check_due_invite_rewards(guild)
        await reply(ctx, f"Invite-Nachbearbeitung abgeschlossen: **{unlocked}** Einträge freigeschaltet, **{rewarded}** sofort vergütet.")

    @api.command("recalculate", description="Rekalkuliert gespeicherte Message-XP/Invites. Nutzung: recalculate all|messages|invites")
    async def recalculate_command(ctx, args):
        guild = get_ctx_guild(ctx)
        if guild is None:
            await reply(ctx, "Dieser Befehl funktioniert nur auf einem Server.")
            return
        if not is_bot_master(ctx):
            await reply(ctx, "Nur Bot-Master dürfen diese große Zahnrad-Operation starten.")
            return
        mode = (split_args(args)[0].lower() if split_args(args) else "").strip()
        if mode not in {"all", "messages", "invites"}:
            await reply(ctx, "Nutzung: `recalculate all`, `recalculate messages` oder `recalculate invites`.")
            return
        changed_users = 0
        if mode in {"all", "messages"}:
            sums = {}
            ids = await api.storage_get(key_message_index(guild.id))
            for mid in ([str(x) for x in ids] if isinstance(ids, list) else []):
                rec = await api.storage_get(key_message(guild.id, mid))
                if not isinstance(rec, dict):
                    continue
                uid = str(rec.get("user_id"))
                sums.setdefault(uid, {"xp": 0, "messages": 0})
                sums[uid]["xp"] += int(rec.get("xp", 0) or 0)
                sums[uid]["messages"] += 1
            for uid in set((await get_index(guild.id)) + list(sums.keys())):
                async with lock_for(key_account(guild.id, uid)):
                    acc = await get_account(guild.id, uid)
                    acc["message_xp"] = max(0, int(sums.get(uid, {}).get("xp", 0)))
                    acc["messages"] = max(0, int(sums.get(uid, {}).get("messages", 0)))
                    await set_account(guild.id, uid, acc)
                    changed_users += 1
        rewarded = 0
        if mode in {"all", "invites"}:
            rewarded = await check_due_invite_rewards(guild)
        schedule_snapshot(str(guild.id))
        await reply(ctx, f"Recalculate `{mode}` fertig: **{changed_users}** User-Message-Konten aktualisiert, **{rewarded}** Invite-Rewards verarbeitet.")

    @api.command("importdb", description="Importiert bot-db.json Snapshot-Anhänge. Nur Bot-Master.")
    async def importdb_command(ctx, args):
        guild = get_ctx_guild(ctx)
        if guild is None:
            await reply(ctx, "Dieser Befehl funktioniert nur auf einem Server.")
            return
        if not is_bot_master(ctx):
            await reply(ctx, "Nur Bot-Master dürfen die Datenbank-Wunderkammer öffnen.")
            return
        msg = getattr(ctx, "message", None)
        attachments = list(getattr(msg, "attachments", []) or [])
        if not attachments:
            await reply(ctx, "Bitte hänge mindestens eine `bot-db.json` an.")
            return
        imported = 0
        errors = 0
        for attachment in attachments:
            try:
                snap = await read_attachment_json(attachment)
                imported += await merge_snapshot(str(guild.id), snap)
            except Exception as exc:
                errors += 1
                await emit_log(f"[ImportDb] {type(exc).__name__}")
        await reply(ctx, f"Import abgeschlossen: **{imported}** Profile gemerged, **{errors}** Anhänge übersprungen.")

    @api.command("help", description="Zeigt XP-Bot-Befehle an.")
    async def help_command(ctx, args):
        admin = is_bot_master(ctx)
        manage = has_manage_permission(ctx)
        lines = [
            "🎪 **Leveling XP – Befehle**",
            "`!myrank` / `xp` / `level` – eigene XP anzeigen",
            "`!myrank @User` – Rank eines Mitglieds anzeigen",
            "`!set-rank-color #FFFFFF` – Rank-Farbe setzen",
        ]
        if admin:
            lines += [
                "`!givexp @User 100 [Grund]` / `addxp` – Manual-XP geben",
                "`!removexp @User 100 [Grund]` – Manual-XP entfernen; Manual-XP darf negativ werden",
                "`!importdb` – bot-db.json Snapshot-Anhänge importieren",
                "`!myrank @User debug` – Quellen und Werte anzeigen",
                "`!recalculate all|messages|invites` – gespeicherte XP neu zusammenziehen",
            ]
        if manage:
            lines += [
                "`!xp-liste` – interne XP-Liste ausgeben",
                "`!einladungen-nachbearbeiten` – offene Invite-Rewards prüfen",
            ]
        await reply(ctx, "\n".join(lines))

    # Hintergrundnummern starten.
    try:
        loop = getattr(getattr(api, "bot", None), "loop", None)
        if loop is not None:
            loop.create_task(checkpoint_voice_sessions())
            loop.create_task(invite_reward_loop())
        else:
            asyncio.create_task(checkpoint_voice_sessions())
            asyncio.create_task(invite_reward_loop())
    except Exception:
        try:
            asyncio.create_task(checkpoint_voice_sessions())
            asyncio.create_task(invite_reward_loop())
        except Exception:
            pass

    try:
        bot = getattr(api, "bot", None)
        if bot is not None and getattr(bot, "is_ready", lambda: False)():
            await seed_current_voice_sessions()
            for guild in getattr(bot, "guilds", []) or []:
                await refresh_invite_cache(guild)
    except Exception:
        pass
