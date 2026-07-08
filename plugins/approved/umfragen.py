import asyncio
import time
import json
import re
import traceback
from copy import deepcopy

PLUGIN = {
    "name": "Umfragen",
    "description": "Zeitlich begrenzte Discord-Umfragen mit mehreren Optionen, automatischer Beendigung, Abstimmungsverfolgung und Ergebnisspeicherung."
}

STATE_KEY = "umfragen.state.v1"
PLUGIN_ID = "umfragen"

DEFAULT_CONFIG = {
    "enabled": True,
    "defaultDurationMinutes": 60,
    "maxDurationMinutes": 10080,
    "maxOptions": 10,
    "maxActivePolls": 25,
    "allowResultsWhileActive": True,
    "announceResultsOnClose": True,
    "logChannelId": None,
    "archiveLimit": 200
}


def _now():
    return int(time.time())


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return " ".join(str(x) for x in value).strip()
    return str(value).strip()


def _get_arg(args, name, default=None):
    if args is None:
        return default
    aliases = {
        "poll_id": ["poll_id", "id", "umfrage", "umfrage_id"],
        "frage": ["frage", "question", "titel", "title"],
        "optionen": ["optionen", "options", "antworten", "choices"],
        "laufzeit_min": ["laufzeit_min", "laufzeit", "minuten", "duration", "duration_minutes"],
        "option": ["option", "wahl", "choice", "nummer"],
        "key": ["key", "name", "feld"],
        "value": ["value", "wert"]
    }.get(name, [name])

    if isinstance(args, dict):
        for alias in aliases:
            if alias in args and args[alias] is not None:
                return args[alias]
        return default

    if isinstance(args, (list, tuple)):
        positional = {
            "poll_id": 0,
            "option": 1,
            "key": 0,
            "value": 1,
            "laufzeit_min": 2
        }
        if name in positional and len(args) > positional[name]:
            return args[positional[name]]
        return default

    return default


def _split_options(raw):
    text = _as_text(raw)
    if not text:
        return []
    if ";" in text:
        parts = text.split(";")
    elif "|" in text:
        parts = text.split("|")
    else:
        parts = text.split(",")
    cleaned = []
    seen = set()
    for part in parts:
        item = re.sub(r"\s+", " ", part).strip()
        if item and item.lower() not in seen:
            cleaned.append(item[:100])
            seen.add(item.lower())
    return cleaned


def _safe_int(value, default=None):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def _bool_from_text(value):
    if isinstance(value, bool):
        return value
    text = _as_text(value).lower()
    if text in ("true", "1", "yes", "ja", "on", "an", "enabled", "aktiv"):
        return True
    if text in ("false", "0", "no", "nein", "off", "aus", "disabled", "inaktiv"):
        return False
    return None


def _base36(num):
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if num == 0:
        return "0"
    out = ""
    while num:
        num, rem = divmod(num, 36)
        out = chars[rem] + out
    return out


def _new_poll_id(existing):
    base = "u" + _base36(_now())[-5:]
    for i in range(1, 1000):
        pid = f"{base}{i}"
        if pid not in existing:
            return pid
    return f"u{int(time.time() * 1000)}"


def _format_ts(ts):
    return f"<t:{int(ts)}:R>"


def _counts_for_poll(poll):
    options = poll.get("options", [])
    counts = [0 for _ in options]
    votes = poll.get("votes", {}) or {}
    for selected in votes.values():
        idx = _safe_int(selected, -1)
        if idx is not None and 0 <= idx < len(counts):
            counts[idx] += 1
    return counts


def _render_results(poll):
    counts = _counts_for_poll(poll)
    total = sum(counts)
    status = poll.get("status", "active")
    lines = [
        f"🎪 **Umfrage `{poll.get('id')}` — {'aktiv' if status == 'active' else 'beendet'}**",
        f"**Frage:** {poll.get('question', 'Unbekannte Frage')}",
        f"**Stimmen:** {total}"
    ]
    if status == "active":
        lines.append(f"**Endet:** {_format_ts(poll.get('ends_at', _now()))}")
    else:
        lines.append(f"**Beendet:** {_format_ts(poll.get('closed_at', poll.get('ends_at', _now())))}")

    for idx, option in enumerate(poll.get("options", []), start=1):
        count = counts[idx - 1]
        percent = 0 if total == 0 else round((count / total) * 100, 1)
        bar_len = 12
        filled = 0 if total == 0 else int((count / total) * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        lines.append(f"`{idx}.` {option} — **{count}** ({percent}%) `{bar}`")
    return "\n".join(lines)


def _render_poll(poll):
    lines = [
        f"🎠 **Neue Umfrage `{poll.get('id')}`!**",
        f"**Frage:** {poll.get('question')}",
        f"**Endet:** {_format_ts(poll.get('ends_at'))}",
        "**Optionen:**"
    ]
    for idx, option in enumerate(poll.get("options", []), start=1):
        lines.append(f"`{idx}.` {option}")
    lines.append("\nAbstimmen mit `/umfrage_abstimmen poll_id:<id> option:<nummer>` — eine Stimme pro Person, Umschwenken erlaubt!")
    return "\n".join(lines)


def _public_poll_view(poll):
    return {
        "id": poll.get("id"),
        "guild_id": poll.get("guild_id"),
        "channel_id": poll.get("channel_id"),
        "author_id": poll.get("author_id"),
        "question": poll.get("question"),
        "options": list(poll.get("options", [])),
        "status": poll.get("status"),
        "created_at": poll.get("created_at"),
        "ends_at": poll.get("ends_at"),
        "closed_at": poll.get("closed_at"),
        "vote_count": len(poll.get("votes", {}) or {}),
        "counts": _counts_for_poll(poll)
    }


async def setup_plugin(api):
    lock = asyncio.Lock()
    expiry_task = None

    async def storage_get_state():
        try:
            state = await api.storage_get(STATE_KEY)
        except TypeError:
            state = await api.storage_get(STATE_KEY, None)
        if not isinstance(state, dict):
            state = {"guilds": {}}
        if "guilds" not in state or not isinstance(state["guilds"], dict):
            state["guilds"] = {}
        return state

    async def storage_set_state(state):
        await api.storage_set(STATE_KEY, state)

    def guild_id_from_ctx(ctx):
        guild = getattr(ctx, "guild", None)
        if guild is None and hasattr(ctx, "message"):
            guild = getattr(ctx.message, "guild", None)
        if guild is None:
            return None
        return str(getattr(guild, "id", guild))

    def channel_id_from_ctx(ctx):
        channel = getattr(ctx, "channel", None)
        if channel is None and hasattr(ctx, "message"):
            channel = getattr(ctx.message, "channel", None)
        if channel is None:
            return None
        return str(getattr(channel, "id", channel))

    def author_id_from_ctx(ctx):
        author = getattr(ctx, "author", None) or getattr(ctx, "user", None)
        if author is None and hasattr(ctx, "message"):
            author = getattr(ctx.message, "author", None)
        if author is None:
            return None
        return str(getattr(author, "id", author))

    def get_guild_state(state, guild_id):
        guilds = state.setdefault("guilds", {})
        g = guilds.setdefault(str(guild_id), {"config": deepcopy(DEFAULT_CONFIG), "polls": {}, "archive": []})
        if "config" not in g or not isinstance(g["config"], dict):
            g["config"] = deepcopy(DEFAULT_CONFIG)
        merged = deepcopy(DEFAULT_CONFIG)
        merged.update(g["config"])
        g["config"] = merged
        if "polls" not in g or not isinstance(g["polls"], dict):
            g["polls"] = {}
        if "archive" not in g or not isinstance(g["archive"], list):
            g["archive"] = []
        return g

    def find_poll_in_guild(g, poll_id):
        pid = _as_text(poll_id)
        if not pid:
            return None, "missing"
        if pid in g.get("polls", {}):
            return g["polls"][pid], "active"
        for poll in g.get("archive", []):
            if poll.get("id") == pid:
                return poll, "archive"
        return None, "missing"

    def close_poll_in_state(g, poll_id, reason="expired", closed_by_id=None):
        pid = _as_text(poll_id)
        poll = g.get("polls", {}).pop(pid, None)
        if not poll:
            return None
        poll["status"] = "closed"
        poll["closed_at"] = _now()
        poll["closed_reason"] = reason
        if closed_by_id:
            poll["closed_by_id"] = str(closed_by_id)
        poll["result_counts"] = _counts_for_poll(poll)
        archive = g.setdefault("archive", [])
        archive.insert(0, poll)
        limit = _safe_int(g.get("config", {}).get("archiveLimit"), DEFAULT_CONFIG["archiveLimit"])
        if limit and limit > 0:
            del archive[limit:]
        return poll

    async def send_to_channel(channel_id, message):
        if not channel_id:
            return False
        try:
            channel = None
            if hasattr(api, "bot") and api.bot:
                channel = api.bot.get_channel(int(channel_id))
                if channel is None and hasattr(api.bot, "fetch_channel"):
                    channel = await api.bot.fetch_channel(int(channel_id))
            if channel is not None and hasattr(channel, "send"):
                await channel.send(message)
                return True
        except Exception:
            traceback.print_exc()
        return False

    async def announce_closed(g, poll):
        cfg = g.get("config", {})
        if not cfg.get("announceResultsOnClose", True):
            return
        message = "🎪 **Die Umfragen-Glocke schlaegt! Eine Abstimmung ist beendet.**\n" + _render_results(poll)
        target = cfg.get("logChannelId") or poll.get("channel_id")
        await send_to_channel(target, message)

    async def close_expired_all():
        closed_items = []
        async with lock:
            state = await storage_get_state()
            now = _now()
            for guild_id, g in state.get("guilds", {}).items():
                get_guild_state(state, guild_id)
                expired = []
                for pid, poll in list(g.get("polls", {}).items()):
                    if poll.get("status") == "active" and _safe_int(poll.get("ends_at"), 0) <= now:
                        expired.append(pid)
                for pid in expired:
                    closed = close_poll_in_state(g, pid, "expired", None)
                    if closed:
                        closed_items.append((g, closed))
            await storage_set_state(state)
        for g, poll in closed_items:
            await announce_closed(g, poll)
        return closed_items

    async def create_poll_internal(guild_id, channel_id, author_id, question, options, duration_minutes=None):
        question = re.sub(r"\s+", " ", _as_text(question))[:300]
        options = [re.sub(r"\s+", " ", _as_text(o))[:100] for o in options if _as_text(o)]
        duration_minutes = _safe_int(duration_minutes, None)

        async with lock:
            state = await storage_get_state()
            g = get_guild_state(state, guild_id)
            cfg = g["config"]
            if not cfg.get("enabled", True):
                return None, "Das Umfragen-Modul ist auf diesem Server deaktiviert. Der Vorhang bleibt zu."
            if not question:
                return None, "Bitte gib eine Frage an. Eine Umfrage ohne Frage ist nur ein Hut ohne Kaninchen."
            if len(options) < 2:
                return None, "Bitte gib mindestens zwei Optionen an, getrennt mit Semikolon: `Ja;Nein;Vielleicht`."
            if len(options) > int(cfg.get("maxOptions", DEFAULT_CONFIG["maxOptions"])):
                return None, f"Zu viele Optionen. Erlaubt sind maximal {cfg.get('maxOptions')} Optionen."
            if len(g.get("polls", {})) >= int(cfg.get("maxActivePolls", DEFAULT_CONFIG["maxActivePolls"])):
                return None, f"Zu viele aktive Umfragen. Maximum: {cfg.get('maxActivePolls')}. Erst eine Manege schliessen, dann die naechste oeffnen!"
            if duration_minutes is None:
                duration_minutes = int(cfg.get("defaultDurationMinutes", DEFAULT_CONFIG["defaultDurationMinutes"]))
            max_duration = int(cfg.get("maxDurationMinutes", DEFAULT_CONFIG["maxDurationMinutes"]))
            if duration_minutes < 1:
                return None, "Die Laufzeit muss mindestens 1 Minute betragen. Zeitreisen sind leider ausverkauft."
            if duration_minutes > max_duration:
                return None, f"Die Laufzeit ist zu lang. Maximum: {max_duration} Minuten."

            pid = _new_poll_id(g.get("polls", {}))
            now = _now()
            poll = {
                "id": pid,
                "guild_id": str(guild_id),
                "channel_id": str(channel_id) if channel_id else None,
                "author_id": str(author_id) if author_id else None,
                "question": question,
                "options": options,
                "status": "active",
                "created_at": now,
                "ends_at": now + duration_minutes * 60,
                "duration_minutes": duration_minutes,
                "votes": {}
            }
            g["polls"][pid] = poll
            await storage_set_state(state)
            return poll, None

    async def vote_internal(guild_id, user_id, poll_id, option_number):
        option_number = _safe_int(option_number, None)
        if option_number is None:
            return None, "Bitte gib eine Optionsnummer an. Die kleinen Zahlenakrobaten warten schon."
        async with lock:
            state = await storage_get_state()
            g = get_guild_state(state, guild_id)
            poll, where = find_poll_in_guild(g, poll_id)
            if not poll or where != "active":
                return None, "Diese aktive Umfrage wurde nicht gefunden. Vielleicht ist sie bereits aus der Manege gerollt."
            if _safe_int(poll.get("ends_at"), 0) <= _now():
                closed = close_poll_in_state(g, poll.get("id"), "expired", None)
                await storage_set_state(state)
                return closed, "Diese Umfrage war bereits abgelaufen und wurde jetzt beendet. Ergebnisse sind abrufbar."
            if option_number < 1 or option_number > len(poll.get("options", [])):
                return None, f"Ungueltige Option. Bitte waehle eine Zahl zwischen 1 und {len(poll.get('options', []))}."
            poll.setdefault("votes", {})[str(user_id)] = option_number - 1
            await storage_set_state(state)
            return poll, None

    async def get_poll_public(guild_id, poll_id):
        async with lock:
            state = await storage_get_state()
            g = get_guild_state(state, guild_id)
            poll, where = find_poll_in_guild(g, poll_id)
            if not poll:
                return None
            return _public_poll_view(poll)

    async def list_active_public(guild_id):
        async with lock:
            state = await storage_get_state()
            g = get_guild_state(state, guild_id)
            return [_public_poll_view(p) for p in g.get("polls", {}).values() if p.get("status") == "active"]

    async def close_poll_api(guild_id, poll_id, reason="api", closed_by_id=None):
        async with lock:
            state = await storage_get_state()
            g = get_guild_state(state, guild_id)
            closed = close_poll_in_state(g, poll_id, reason, closed_by_id)
            await storage_set_state(state)
        if closed:
            await announce_closed(g, closed)
            try:
                await api.emit("umfragen.poll_closed", _public_poll_view(closed))
            except Exception:
                pass
        return _public_poll_view(closed) if closed else None

    api.shared["umfragen.api"] = {
        "create_poll": create_poll_internal,
        "vote": vote_internal,
        "get_poll": get_poll_public,
        "list_active": list_active_public,
        "close_poll": close_poll_api
    }

    @api.on("umfragen.get_results")
    async def on_get_results(payload):
        payload = payload or {}
        return await get_poll_public(str(payload.get("guild_id")), payload.get("poll_id"))

    @api.on("umfragen.list_active")
    async def on_list_active(payload):
        payload = payload or {}
        return await list_active_public(str(payload.get("guild_id")))

    @api.on("umfragen.close")
    async def on_close(payload):
        payload = payload or {}
        return await close_poll_api(str(payload.get("guild_id")), payload.get("poll_id"), payload.get("reason", "topic"), payload.get("closed_by_id"))

    @api.command({
        "names": ["umfrage_erstellen", "poll_create"],
        "description": "Erstellt eine zeitlich begrenzte Umfrage mit Frage und mehreren Optionen.",
        "level": "user",
        "slash": True,
        "options": [
            {"name": "frage", "description": "Die Frage der Umfrage.", "type": "string", "required": True},
            {"name": "optionen", "description": "Optionen getrennt mit Semikolon, z. B. Ja;Nein;Vielleicht", "type": "string", "required": True},
            {"name": "laufzeit_min", "description": "Laufzeit in Minuten. Leer = Serverstandard.", "type": "integer", "required": False}
        ],
        "routing": {
            "priority": 92,
            "when": "Use when a user wants to create a timed poll with multiple choices.",
            "not_when": "Do not use for voting, listing active polls, showing results, or general help."
        }
    })
    async def umfrage_erstellen(ctx, args):
        await close_expired_all()
        guild_id = guild_id_from_ctx(ctx)
        if not guild_id:
            await api.reply(ctx, "Umfragen funktionieren nur auf Servern, nicht in DMs. Die Manege braucht ein Zelt!")
            return

        question = _get_arg(args, "frage")
        options_raw = _get_arg(args, "optionen")
        duration = _get_arg(args, "laufzeit_min")

        if not question or not options_raw:
            raw = _as_text(args)
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) >= 2:
                question = parts[0]
                options_raw = parts[1]
                if len(parts) >= 3:
                    duration = parts[2]

        poll, err = await create_poll_internal(
            guild_id,
            channel_id_from_ctx(ctx),
            author_id_from_ctx(ctx),
            question,
            _split_options(options_raw),
            duration
        )
        if err:
            await api.reply(ctx, "🎭 " + err)
            return
        await api.reply(ctx, _render_poll(poll))
        try:
            await api.emit("umfragen.poll_created", _public_poll_view(poll))
        except Exception:
            pass

    @api.command({
        "names": ["umfrage_abstimmen", "poll_vote"],
        "description": "Stimmt bei einer aktiven Umfrage ab oder aendert die eigene Stimme.",
        "level": "user",
        "slash": True,
        "options": [
            {"name": "poll_id", "description": "ID der Umfrage, z. B. uabc1", "type": "string", "required": True},
            {"name": "option", "description": "Optionsnummer, beginnend bei 1", "type": "integer", "required": True}
        ],
        "routing": {
            "priority": 95,
            "when": "Use when a user wants to cast or change a vote in an existing active poll.",
            "not_when": "Do not use for creating polls, closing polls, or viewing results without voting."
        }
    })
    async def umfrage_abstimmen(ctx, args):
        await close_expired_all()
        guild_id = guild_id_from_ctx(ctx)
        user_id = author_id_from_ctx(ctx)
        if not guild_id or not user_id:
            await api.reply(ctx, "Diese Abstimmung braucht Server und Nutzer. Ohne Publikum kein Applaus!")
            return
        poll_id = _get_arg(args, "poll_id")
        option = _get_arg(args, "option")
        poll, err = await vote_internal(guild_id, user_id, poll_id, option)
        if err and (not poll or poll.get("status") != "closed"):
            await api.reply(ctx, "🎭 " + err)
            return
        if poll and poll.get("status") == "closed":
            await api.reply(ctx, "🎪 " + (err or "Umfrage beendet.") + "\n" + _render_results(poll))
            return
        selected = poll.get("options", [])[_safe_int(option, 1) - 1]
        await api.reply(ctx, f"✅ Stimme gespeichert fuer Umfrage `{poll.get('id')}`: **{selected}**. Das Statistik-Orchester notiert es!")
        try:
            await api.emit("umfragen.vote_cast", {"guild_id": guild_id, "poll_id": poll.get("id"), "user_id": user_id})
        except Exception:
            pass

    @api.command({
        "names": ["umfrage_ergebnisse", "poll_results"],
        "description": "Zeigt Ergebnisse einer aktiven oder beendeten Umfrage.",
        "level": "user",
        "slash": True,
        "options": [
            {"name": "poll_id", "description": "ID der Umfrage", "type": "string", "required": True}
        ],
        "routing": {
            "priority": 88,
            "when": "Use when a user asks for poll results or statistics for a known poll id.",
            "not_when": "Do not use when the user wants to vote or list all active polls."
        }
    })
    async def umfrage_ergebnisse(ctx, args):
        await close_expired_all()
        guild_id = guild_id_from_ctx(ctx)
        poll_id = _get_arg(args, "poll_id")
        if not guild_id:
            await api.reply(ctx, "Ergebnisse gibt es nur im Serverzelt, nicht in der DM-Gasse.")
            return
        async with lock:
            state = await storage_get_state()
            g = get_guild_state(state, guild_id)
            poll, where = find_poll_in_guild(g, poll_id)
            if not poll:
                await api.reply(ctx, "Keine Umfrage mit dieser ID gefunden. Sie ist wohl im Konfettinebel verschwunden.")
                return
            if poll.get("status") == "active" and not g.get("config", {}).get("allowResultsWhileActive", True):
                await api.reply(ctx, "Zwischenergebnisse sind auf diesem Server deaktiviert. Spannung! Dramaturgie! Geheimnis!")
                return
            rendered = _render_results(poll)
        await api.reply(ctx, rendered)

    @api.command({
        "names": ["umfrage_liste", "poll_list"],
        "description": "Listet aktive Umfragen auf diesem Server.",
        "level": "user",
        "slash": True,
        "options": [],
        "routing": {
            "priority": 82,
            "when": "Use when a user wants to see all currently active polls on the server.",
            "not_when": "Do not use for showing detailed results for a specific poll id."
        }
    })
    async def umfrage_liste(ctx, args):
        await close_expired_all()
        guild_id = guild_id_from_ctx(ctx)
        if not guild_id:
            await api.reply(ctx, "Aktive Umfragen gibt es nur auf Servern. Die Manege braucht Waende!")
            return
        active = await list_active_public(guild_id)
        if not active:
            await api.reply(ctx, "🎪 Keine aktiven Umfragen. Die Manege ist leer, aber der Boden glaenzt.")
            return
        lines = ["🎪 **Aktive Umfragen:**"]
        for poll in sorted(active, key=lambda p: p.get("ends_at", 0)):
            lines.append(f"`{poll['id']}` — {poll['question']} — endet {_format_ts(poll['ends_at'])} — Stimmen: {poll['vote_count']}")
        await api.reply(ctx, "\n".join(lines[:30]))

    @api.command({
        "names": ["umfrage_info", "poll_info"],
        "description": "Zeigt Frage, Optionen und Laufzeit einer Umfrage ohne Ergebnisbalken.",
        "level": "user",
        "slash": True,
        "options": [
            {"name": "poll_id", "description": "ID der Umfrage", "type": "string", "required": True}
        ],
        "routing": {
            "priority": 78,
            "when": "Use when a user wants details about a poll without necessarily viewing the result statistics.",
            "not_when": "Do not use when the user explicitly asks for results or wants to vote."
        }
    })
    async def umfrage_info(ctx, args):
        await close_expired_all()
        guild_id = guild_id_from_ctx(ctx)
        poll_id = _get_arg(args, "poll_id")
        async with lock:
            state = await storage_get_state()
            g = get_guild_state(state, guild_id)
            poll, where = find_poll_in_guild(g, poll_id)
            if not poll:
                await api.reply(ctx, "Diese Umfrage existiert hier nicht. Ich habe sogar unter dem Teppich nachgesehen.")
                return
            lines = [
                f"🎠 **Umfrage `{poll.get('id')}` — {poll.get('status')}**",
                f"**Frage:** {poll.get('question')}",
                f"**Erstellt:** {_format_ts(poll.get('created_at'))}",
                f"**Endet/Endete:** {_format_ts(poll.get('ends_at'))}",
                "**Optionen:**"
            ]
            for idx, option in enumerate(poll.get("options", []), start=1):
                lines.append(f"`{idx}.` {option}")
        await api.reply(ctx, "\n".join(lines))

    @api.command({
        "names": ["umfragen_status"],
        "description": "Admin: Zeigt Status und Konfiguration des Umfragen-Plugins.",
        "level": "admin",
        "slash": False,
        "options": [],
        "routing": {
            "priority": 70,
            "when": "Use when an administrator wants to inspect poll plugin status and configuration.",
            "not_when": "Do not use for public poll results or normal voting."
        }
    })
    async def umfragen_status(ctx, args):
        await close_expired_all()
        guild_id = guild_id_from_ctx(ctx)
        async with lock:
            state = await storage_get_state()
            g = get_guild_state(state, guild_id)
            cfg = g["config"]
            active_count = len(g.get("polls", {}))
            archive_count = len(g.get("archive", []))
        lines = [
            "🎛️ **Umfragen-Status — Admin-Konsole der kleinen Statistikmaschine**",
            f"Aktiviert: `{cfg.get('enabled')}`",
            f"Aktive Umfragen: `{active_count}`",
            f"Archivierte Ergebnisse: `{archive_count}`",
            f"Standardlaufzeit: `{cfg.get('defaultDurationMinutes')}` Minuten",
            f"Max. Laufzeit: `{cfg.get('maxDurationMinutes')}` Minuten",
            f"Max. Optionen: `{cfg.get('maxOptions')}`",
            f"Max. aktive Umfragen: `{cfg.get('maxActivePolls')}`",
            f"Zwischenergebnisse erlaubt: `{cfg.get('allowResultsWhileActive')}`",
            f"Ergebnisse bei Ende posten: `{cfg.get('announceResultsOnClose')}`",
            f"Log-Channel: `{cfg.get('logChannelId')}`"
        ]
        await api.reply(ctx, "\n".join(lines))

    @api.command({
        "names": ["umfragen_enable"],
        "description": "Admin: Aktiviert das Umfragen-Plugin auf diesem Server.",
        "level": "admin",
        "slash": False,
        "options": [],
        "routing": {
            "priority": 72,
            "when": "Use when an administrator wants to enable poll creation on the server.",
            "not_when": "Do not use for creating a poll; use the user create command instead."
        }
    })
    async def umfragen_enable(ctx, args):
        guild_id = guild_id_from_ctx(ctx)
        async with lock:
            state = await storage_get_state()
            g = get_guild_state(state, guild_id)
            g["config"]["enabled"] = True
            await storage_set_state(state)
        await api.reply(ctx, "✅ Umfragen sind aktiviert. Das Statistik-Karussell dreht sich wieder!")

    @api.command({
        "names": ["umfragen_disable"],
        "description": "Admin: Deaktiviert neue Umfragen auf diesem Server. Bestehende laufen weiter.",
        "level": "admin",
        "slash": False,
        "options": [],
        "routing": {
            "priority": 72,
            "when": "Use when an administrator wants to prevent new polls from being created.",
            "not_when": "Do not use to close an existing poll; use umfragen_close."
        }
    })
    async def umfragen_disable(ctx, args):
        guild_id = guild_id_from_ctx(ctx)
        async with lock:
            state = await storage_get_state()
            g = get_guild_state(state, guild_id)
            g["config"]["enabled"] = False
            await storage_set_state(state)
        await api.reply(ctx, "🛑 Neue Umfragen sind deaktiviert. Bereits laufende Nummern tanzen bis zum Schluss weiter.")

    @api.command({
        "names": ["umfragen_config"],
        "description": "Admin: Zeigt oder aendert Konfiguration. Beispiel: !umfragen_config maxDurationMinutes 1440",
        "level": "admin",
        "slash": False,
        "options": [],
        "routing": {
            "priority": 75,
            "when": "Use when an administrator wants to read or change poll configuration values.",
            "not_when": "Do not use for voting, listing polls, or kinger-level exports/resets."
        }
    })
    async def umfragen_config(ctx, args):
        guild_id = guild_id_from_ctx(ctx)
        key = _get_arg(args, "key")
        value = _get_arg(args, "value")
        allowed = {
            "enabled": "bool",
            "defaultDurationMinutes": "int",
            "maxDurationMinutes": "int",
            "maxOptions": "int",
            "maxActivePolls": "int",
            "allowResultsWhileActive": "bool",
            "announceResultsOnClose": "bool",
            "logChannelId": "str_or_none",
            "archiveLimit": "int"
        }
        async with lock:
            state = await storage_get_state()
            g = get_guild_state(state, guild_id)
            cfg = g["config"]
            if not key:
                lines = ["🎛️ **Umfragen-Konfiguration**"]
                for k in allowed:
                    lines.append(f"`{k}` = `{cfg.get(k)}`")
                lines.append("Beispiel: `!umfragen_config defaultDurationMinutes 120`")
                await api.reply(ctx, "\n".join(lines))
                return
            key = _as_text(key)
            if key not in allowed:
                await api.reply(ctx, f"Unbekannter Config-Schalter `{key}`. Erlaubt: {', '.join(allowed.keys())}")
                return
            kind = allowed[key]
            if kind == "bool":
                parsed = _bool_from_text(value)
                if parsed is None:
                    await api.reply(ctx, "Bitte nutze true/false, ja/nein oder an/aus.")
                    return
            elif kind == "int":
                parsed = _safe_int(value, None)
                if parsed is None or parsed < 1:
                    await api.reply(ctx, "Bitte gib eine positive ganze Zahl an.")
                    return
            else:
                parsed_text = _as_text(value)
                parsed = None if parsed_text.lower() in ("none", "null", "aus", "off", "-") else parsed_text
            cfg[key] = parsed
            await storage_set_state(state)
        await api.reply(ctx, f"✅ `{key}` wurde auf `{parsed}` gesetzt. Zahnrad eingerastet!")

    @api.command({
        "names": ["umfragen_close"],
        "description": "Admin: Beendet eine aktive Umfrage manuell und speichert das Ergebnis.",
        "level": "admin",
        "slash": False,
        "options": [],
        "routing": {
            "priority": 86,
            "when": "Use when an administrator wants to manually close an active poll by id.",
            "not_when": "Do not use for simply viewing poll results; use umfrage_ergebnisse."
        }
    })
    async def umfragen_close(ctx, args):
        guild_id = guild_id_from_ctx(ctx)
        poll_id = _get_arg(args, "poll_id")
        if not poll_id:
            await api.reply(ctx, "Bitte gib eine Umfrage-ID an: `!umfragen_close <poll_id>`")
            return
        closed = await close_poll_api(guild_id, poll_id, "admin_closed", author_id_from_ctx(ctx))
        if not closed:
            await api.reply(ctx, "Keine aktive Umfrage mit dieser ID gefunden. Der Vorhang war wohl schon unten.")
            return
        await api.reply(ctx, "🎪 Umfrage manuell beendet und archiviert.\n" + _render_results(closed))

    @api.command({
        "names": ["umfragen_prune_archive"],
        "description": "Admin: Kuerzt das Ergebnisarchiv auf die konfigurierte archiveLimit-Groesse.",
        "level": "admin",
        "slash": False,
        "options": [],
        "routing": {
            "priority": 68,
            "when": "Use when an administrator wants to prune old archived poll results according to archiveLimit.",
            "not_when": "Do not use for deleting active polls or resetting all plugin data."
        }
    })
    async def umfragen_prune_archive(ctx, args):
        guild_id = guild_id_from_ctx(ctx)
        async with lock:
            state = await storage_get_state()
            g = get_guild_state(state, guild_id)
            before = len(g.get("archive", []))
            limit = int(g["config"].get("archiveLimit", DEFAULT_CONFIG["archiveLimit"]))
            if limit > 0:
                del g["archive"][limit:]
            after = len(g.get("archive", []))
            await storage_set_state(state)
        await api.reply(ctx, f"🧹 Archiv gekehrt: `{before}` → `{after}` Eintraege. Kein Konfetti mehr unter der Datenbank.")

    @api.command({
        "names": ["umfragen_audit"],
        "description": "Kinger: Read-only Diagnose ueber Umfragen-Daten dieses Servers.",
        "level": "kinger",
        "slash": False,
        "options": [],
        "routing": {
            "priority": 80,
            "when": "Use when a kinger/master operator wants a read-only diagnostic summary for this plugin.",
            "not_when": "Do not use for normal admin config or public results."
        }
    })
    async def umfragen_audit(ctx, args):
        await close_expired_all()
        guild_id = guild_id_from_ctx(ctx)
        async with lock:
            state = await storage_get_state()
            g = get_guild_state(state, guild_id)
            active = list(g.get("polls", {}).values())
            archived = list(g.get("archive", []))
            total_votes_active = sum(len(p.get("votes", {}) or {}) for p in active)
            total_votes_archive = sum(len(p.get("votes", {}) or {}) for p in archived)
        lines = [
            "🔎 **Umfragen-Audit — Nur-Lesen im Maschinenraum**",
            f"Guild: `{guild_id}`",
            f"Aktive Umfragen: `{len(active)}`",
            f"Archivierte Umfragen: `{len(archived)}`",
            f"Stimmen aktiv: `{total_votes_active}`",
            f"Stimmen archiviert: `{total_votes_archive}`",
            f"Shared API: `api.shared[\"umfragen.api\"]`",
            "Events: `umfragen.poll_created`, `umfragen.vote_cast`, `umfragen.poll_closed`"
        ]
        await api.reply(ctx, "\n".join(lines))

    @api.command({
        "names": ["umfragen_export"],
        "description": "Kinger: Exportiert eine datensparsame JSON-Zusammenfassung ohne einzelne User-Vote-IDs.",
        "level": "kinger",
        "slash": False,
        "options": [],
        "routing": {
            "priority": 79,
            "when": "Use when a kinger/master operator needs a privacy-preserving export summary of poll data.",
            "not_when": "Do not use when raw per-user votes are requested; this plugin intentionally avoids exposing them via command."
        }
    })
    async def umfragen_export(ctx, args):
        await close_expired_all()
        guild_id = guild_id_from_ctx(ctx)
        async with lock:
            state = await storage_get_state()
            g = get_guild_state(state, guild_id)
            export = {
                "plugin": PLUGIN_ID,
                "guild_id": guild_id,
                "generated_at": _now(),
                "config": g.get("config", {}),
                "active": [_public_poll_view(p) for p in g.get("polls", {}).values()],
                "archive": [_public_poll_view(p) for p in g.get("archive", [])]
            }
        text = json.dumps(export, ensure_ascii=False, indent=2)
        if len(text) > 1800:
            text = text[:1800] + "\n... gekuerzt; Export ist zu gross fuer eine Discord-Nachricht."
        await api.reply(ctx, "```json\n" + text + "\n```")

    @api.command({
        "names": ["umfragen_reset_all"],
        "description": "Kinger: Loescht alle Umfragen-Daten dieses Servers nach expliziter Bestaetigung.",
        "level": "kinger",
        "slash": False,
        "options": [],
        "routing": {
            "priority": 90,
            "when": "Use only when a kinger/master operator explicitly wants to delete all poll data for this guild.",
            "not_when": "Do not use for pruning archive, closing one poll, or changing configuration."
        }
    })
    async def umfragen_reset_all(ctx, args):
        guild_id = guild_id_from_ctx(ctx)
        confirmation = _as_text(args)
        required = "CONFIRM_RESET_UMFRAGEN"
        if required not in confirmation:
            await api.reply(ctx, f"⚠️ Das loescht alle Umfragen-Daten dieses Servers. Wiederhole mit: `!umfragen_reset_all {required}`")
            return
        async with lock:
            state = await storage_get_state()
            state.setdefault("guilds", {})[str(guild_id)] = {"config": deepcopy(DEFAULT_CONFIG), "polls": {}, "archive": []}
            await storage_set_state(state)
        await api.reply(ctx, "🧨 Alle Umfragen-Daten dieses Servers wurden geloescht. Die Manege ist leergefegt.")

    @api.event("message")
    async def umfragen_message_tick(message):
        # Leichter, sicherer Trigger fuer automatische Beendigung auch ohne laufenden Scheduler.
        try:
            if getattr(message, "author", None) and getattr(message.author, "bot", False):
                return
            await close_expired_all()
        except Exception:
            traceback.print_exc()

    async def expiry_loop():
        try:
            if hasattr(api, "bot") and hasattr(api.bot, "wait_until_ready"):
                await api.bot.wait_until_ready()
            while True:
                try:
                    await close_expired_all()
                except Exception:
                    traceback.print_exc()
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            return

    try:
        expiry_task = asyncio.create_task(expiry_loop())
        api.shared["umfragen.expiry_task"] = expiry_task
    except Exception:
        traceback.print_exc()
