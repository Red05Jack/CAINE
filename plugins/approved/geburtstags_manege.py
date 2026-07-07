import asyncio
import calendar
import datetime
import re
from zoneinfo import ZoneInfo

PLUGIN = {
    "name": "Geburtstags-Manege",
    "description": "Nutzer tragen Geburtstage ein; der Bot erinnert den Server automatisch daran. Vorhang auf fuer Kuchen-Konfetti!"
}

PLUGIN_ID = "birthday"
CONFIG_PREFIX = "birthday:config:"
DATA_PREFIX = "birthday:data:"

DEFAULT_CONFIG = {
    "enabled": True,
    "channelId": None,
    "announceHour": 9,
    "timezone": "Europe/Berlin",
    "mentionUsers": True,
    "messageTemplate": "Heute tanzt das Konfetti fuer: {users}! Alles Gute zum Geburtstag!",
    "lastRunDate": None
}

DATE_RE = re.compile(r"^\s*(?:(\d{4})[-./])?(\d{1,2})\s*[-./]\s*(\d{1,2})\s*$|^\s*(\d{1,2})\s*[-./]\s*(\d{1,2})\s*$")


def _now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _get_arg(args, name, default=None):
    if isinstance(args, dict):
        return args.get(name, default)
    return getattr(args, name, default)


def _get_guild(ctx):
    guild = getattr(ctx, "guild", None)
    if guild is not None:
        return guild
    interaction = getattr(ctx, "interaction", None)
    return getattr(interaction, "guild", None)


def _get_author(ctx):
    author = getattr(ctx, "author", None)
    if author is not None:
        return author
    user = getattr(ctx, "user", None)
    if user is not None:
        return user
    interaction = getattr(ctx, "interaction", None)
    return getattr(interaction, "user", None)


def _entity_id(value):
    if value is None:
        return None
    if hasattr(value, "id"):
        return int(value.id)
    try:
        cleaned = str(value).strip().replace("<#", "").replace(">", "")
        return int(cleaned)
    except Exception:
        return None


def _parse_birthday_date(raw):
    if raw is None:
        raise ValueError("Bitte gib ein Datum an, z. B. 23.04 oder 04-23.")
    text = str(raw).strip()

    # Preferred European format: DD.MM or DD/MM. Also accepts YYYY-MM-DD.
    if re.match(r"^\d{4}[-./]\d{1,2}[-./]\d{1,2}$", text):
        parts = re.split(r"[-./]", text)
        month = int(parts[1])
        day = int(parts[2])
    else:
        parts = re.split(r"[-./]", text)
        if len(parts) != 2:
            raise ValueError("Ich brauche ein Datum wie `23.04` - zwei Zahlen, ein winziger Punkt, grosse Wirkung!")
        day = int(parts[0])
        month = int(parts[1])

    if month < 1 or month > 12:
        raise ValueError("Dieser Monat existiert nicht einmal in meiner digitalen Manege.")

    max_day = 29 if month == 2 else calendar.monthrange(2024, month)[1]
    if day < 1 or day > max_day:
        raise ValueError("Dieser Tag passt nicht zu diesem Monat. Der Kalender hat gezischt und Nein gesagt.")

    return month, day


def _is_leap_year(year):
    return calendar.isleap(year)


def _birthday_date_for_year(year, month, day):
    # 29. Februar wird in Nicht-Schaltjahren am 28. Februar gefeiert.
    if month == 2 and day == 29 and not _is_leap_year(year):
        return datetime.date(year, 2, 28)
    return datetime.date(year, month, day)


def _format_date(month, day):
    return f"{day:02d}.{month:02d}."


def _safe_timezone(name):
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _shorten_lines(lines, max_lines=15):
    shown = lines[:max_lines]
    hidden = max(0, len(lines) - len(shown))
    if hidden:
        shown.append(f"... und {hidden} weitere kleine Kerzen im Schatten.")
    return "\n".join(shown)


async def setup_plugin(api):
    async def storage_get(key, default=None):
        try:
            value = await api.storage_get(key)
        except TypeError:
            value = await api.storage_get(key, default)
        if value is None:
            return default
        return value

    async def storage_set(key, value):
        await api.storage_set(key, value)

    async def safe_reply(ctx, message):
        if len(message) > 1900:
            message = message[:1850] + "\n... die Nachricht wurde gekuerzt, bevor das Konfetti die Decke sprengt."
        await api.reply(ctx, message)

    async def safe_emit(topic, payload):
        if hasattr(api, "emit"):
            try:
                await api.emit(topic, payload)
            except Exception:
                pass

    def config_key(guild_id):
        return f"{CONFIG_PREFIX}{guild_id}"

    def data_key(guild_id):
        return f"{DATA_PREFIX}{guild_id}"

    async def get_config(guild_id):
        stored = await storage_get(config_key(guild_id), None)
        cfg = dict(DEFAULT_CONFIG)
        if isinstance(stored, dict):
            cfg.update(stored)
        return cfg

    async def save_config(guild_id, cfg):
        clean = dict(DEFAULT_CONFIG)
        clean.update(cfg or {})
        await storage_set(config_key(guild_id), clean)
        return clean

    async def get_birthdays(guild_id):
        data = await storage_get(data_key(guild_id), {})
        if not isinstance(data, dict):
            return {}
        return data

    async def save_birthdays(guild_id, data):
        await storage_set(data_key(guild_id), data or {})

    async def set_birthday_record(guild_id, user_id, month, day, source="command"):
        data = await get_birthdays(guild_id)
        data[str(user_id)] = {
            "month": int(month),
            "day": int(day),
            "updatedAt": _now_iso(),
            "source": source
        }
        await save_birthdays(guild_id, data)
        await save_config(guild_id, await get_config(guild_id))
        await safe_emit("birthday.changed", {
            "guildId": int(guild_id),
            "userId": int(user_id),
            "month": int(month),
            "day": int(day),
            "action": "set"
        })
        return data[str(user_id)]

    async def remove_birthday_record(guild_id, user_id):
        data = await get_birthdays(guild_id)
        existed = str(user_id) in data
        if existed:
            del data[str(user_id)]
            await save_birthdays(guild_id, data)
            await safe_emit("birthday.changed", {
                "guildId": int(guild_id),
                "userId": int(user_id),
                "action": "remove"
            })
        return existed

    async def get_birthday_record(guild_id, user_id):
        data = await get_birthdays(guild_id)
        return data.get(str(user_id))

    async def upcoming_birthdays(guild_id, days=30, today=None):
        cfg = await get_config(guild_id)
        tz = _safe_timezone(cfg.get("timezone", "UTC"))
        if today is None:
            today = datetime.datetime.now(tz).date()
        days = max(1, min(int(days), 366))
        data = await get_birthdays(guild_id)
        result = []
        for user_id, rec in data.items():
            try:
                month = int(rec["month"])
                day = int(rec["day"])
                event_date = _birthday_date_for_year(today.year, month, day)
                if event_date < today:
                    event_date = _birthday_date_for_year(today.year + 1, month, day)
                delta = (event_date - today).days
                if delta <= days:
                    result.append({
                        "userId": int(user_id),
                        "month": month,
                        "day": day,
                        "date": event_date.isoformat(),
                        "daysUntil": delta
                    })
            except Exception:
                continue
        result.sort(key=lambda item: (item["daysUntil"], item["userId"]))
        return result

    async def birthday_api_get(guild_id, user_id):
        return await get_birthday_record(int(guild_id), int(user_id))

    async def birthday_api_set(guild_id, user_id, date_string):
        month, day = _parse_birthday_date(date_string)
        return await set_birthday_record(int(guild_id), int(user_id), month, day, source="api")

    async def birthday_api_remove(guild_id, user_id):
        return await remove_birthday_record(int(guild_id), int(user_id))

    async def birthday_api_upcoming(guild_id, days=30):
        return await upcoming_birthdays(int(guild_id), int(days))

    api.shared[f"{PLUGIN_ID}.api"] = {
        "get_birthday": birthday_api_get,
        "set_birthday": birthday_api_set,
        "remove_birthday": birthday_api_remove,
        "upcoming": birthday_api_upcoming,
        "parse_date": _parse_birthday_date
    }

    if hasattr(api, "on"):
        @api.on("birthday.get")
        async def birthday_get_topic(payload):
            return await birthday_api_get(payload.get("guildId"), payload.get("userId"))

        @api.on("birthday.upcoming")
        async def birthday_upcoming_topic(payload):
            return await birthday_api_upcoming(payload.get("guildId"), payload.get("days", 30))

    @api.command({
        "names": ["birthday_set"],
        "description": "Trage deinen Geburtstag ein, z. B. 23.04. Jahr wird nicht gespeichert.",
        "level": "user",
        "options": [
            {"name": "date", "description": "Geburtstag im Format TT.MM, z. B. 23.04", "type": "string", "required": True}
        ]
    })
    async def birthday_set(ctx, args):
        guild = _get_guild(ctx)
        author = _get_author(ctx)
        if guild is None or author is None:
            return await safe_reply(ctx, "Diese Nummer funktioniert nur auf einem Server, nicht im leeren Zwischenzelt der DMs.")
        try:
            month, day = _parse_birthday_date(_get_arg(args, "date"))
        except ValueError as exc:
            return await safe_reply(ctx, f"Kalender-Kobold meldet: {exc}")
        await set_birthday_record(guild.id, author.id, month, day)
        await safe_reply(ctx, f"Eingetragen! Am {_format_date(month, day)} wirft die Geburtstags-Manege fuer dich Konfetti. Kein Jahr gespeichert, nur Tag und Monat.")

    @api.command({
        "names": ["birthday_me"],
        "description": "Zeigt deinen gespeicherten Geburtstag.",
        "level": "user",
        "options": []
    })
    async def birthday_me(ctx, args):
        guild = _get_guild(ctx)
        author = _get_author(ctx)
        if guild is None or author is None:
            return await safe_reply(ctx, "Im DM-Nebel kann ich keine Server-Geburtstage lesen, oh weh!")
        rec = await get_birthday_record(guild.id, author.id)
        if not rec:
            return await safe_reply(ctx, "Du stehst noch nicht im Kuchenregister. Nutze `birthday_set` mit einem Datum wie `23.04`.")
        await safe_reply(ctx, f"Dein Geburtstag ist gespeichert als {_format_date(int(rec['month']), int(rec['day']))}. Sauber verstaut im kleinen Daten-Kabinett!")

    @api.command({
        "names": ["birthday_remove"],
        "description": "Loescht deinen gespeicherten Geburtstag.",
        "level": "user",
        "options": []
    })
    async def birthday_remove(ctx, args):
        guild = _get_guild(ctx)
        author = _get_author(ctx)
        if guild is None or author is None:
            return await safe_reply(ctx, "Diese Loeschkanone zielt nur auf Serverdaten, nicht auf DMs.")
        existed = await remove_birthday_record(guild.id, author.id)
        if existed:
            await safe_reply(ctx, "Dein Geburtstag wurde entfernt. Das Konfetti zieht sich respektvoll zurueck.")
        else:
            await safe_reply(ctx, "Ich fand keinen Eintrag fuer dich. Keine Kerze, kein Krümel, kein Drama.")

    @api.command({
        "names": ["birthday_today"],
        "description": "Zeigt Geburtstagskinder von heute auf diesem Server.",
        "level": "user",
        "options": []
    })
    async def birthday_today(ctx, args):
        guild = _get_guild(ctx)
        if guild is None:
            return await safe_reply(ctx, "Heute gibt es hier keinen Server-Kalender zu bestaunen.")
        cfg = await get_config(guild.id)
        today = datetime.datetime.now(_safe_timezone(cfg.get("timezone", "UTC"))).date()
        data = await get_birthdays(guild.id)
        users = []
        for user_id, rec in data.items():
            try:
                if _birthday_date_for_year(today.year, int(rec["month"]), int(rec["day"])) == today:
                    users.append(f"<@{int(user_id)}>")
            except Exception:
                continue
        if not users:
            return await safe_reply(ctx, "Heute bleibt die Kuchenkanone still. Keine gespeicherten Geburtstage gefunden.")
        await safe_reply(ctx, "Heute im Manegenlicht: " + ", ".join(users))

    @api.command({
        "names": ["birthday_upcoming"],
        "description": "Zeigt kommende Geburtstage.",
        "level": "user",
        "options": [
            {"name": "days", "description": "Zeitraum in Tagen, Standard 30, Maximum 366", "type": "integer", "required": False}
        ]
    })
    async def birthday_upcoming(ctx, args):
        guild = _get_guild(ctx)
        if guild is None:
            return await safe_reply(ctx, "Ohne Server kein Kalenderkarussell.")
        days = _get_arg(args, "days", 30)
        try:
            days = max(1, min(int(days), 366))
        except Exception:
            days = 30
        upcoming = await upcoming_birthdays(guild.id, days)
        if not upcoming:
            return await safe_reply(ctx, f"In den naechsten {days} Tagen sehe ich keine gespeicherten Geburtstage. Nur leere Teller und erwartungsvolle Stille.")
        lines = []
        for item in upcoming:
            when = "heute" if item["daysUntil"] == 0 else f"in {item['daysUntil']} Tagen"
            lines.append(f"- <@{item['userId']}>: {_format_date(item['month'], item['day'])} ({when})")
        await safe_reply(ctx, f"Kommende Geburtstags-Attraktionen fuer {days} Tage:\n" + _shorten_lines(lines))

    @api.command({
        "names": ["birthday_enable"],
        "description": "Aktiviert oder deaktiviert automatische Geburtstagserinnerungen.",
        "level": "admin",
        "options": [
            {"name": "enabled", "description": "true zum Aktivieren, false zum Deaktivieren", "type": "boolean", "required": True}
        ]
    })
    async def birthday_enable(ctx, args):
        guild = _get_guild(ctx)
        if guild is None:
            return await safe_reply(ctx, "Admin-Hebel funktionieren nur im Server-Maschinenraum.")
        enabled = bool(_get_arg(args, "enabled"))
        cfg = await get_config(guild.id)
        cfg["enabled"] = enabled
        await save_config(guild.id, cfg)
        await safe_reply(ctx, f"Automatische Erinnerungen sind nun {'aktiviert' if enabled else 'deaktiviert'}. Der Hebel hat dramatisch geklickt.")

    @api.command({
        "names": ["birthday_channel"],
        "description": "Setzt den Kanal fuer automatische Geburtstagserinnerungen.",
        "level": "admin",
        "options": [
            {"name": "channel", "description": "Zielkanal fuer Geburtstagserinnerungen", "type": "channel", "required": True}
        ]
    })
    async def birthday_channel(ctx, args):
        guild = _get_guild(ctx)
        if guild is None:
            return await safe_reply(ctx, "Dieser Kanal-Kompass braucht einen Server.")
        channel_id = _entity_id(_get_arg(args, "channel"))
        if channel_id is None:
            return await safe_reply(ctx, "Ich konnte diesen Kanal nicht erkennen. Der Kompass dreht sich beleidigt im Kreis.")
        cfg = await get_config(guild.id)
        cfg["channelId"] = channel_id
        await save_config(guild.id, cfg)
        await safe_reply(ctx, f"Geburtstagserinnerungen erscheinen nun in <#{channel_id}>. Manege markiert!")

    @api.command({
        "names": ["birthday_time"],
        "description": "Setzt Stunde und Zeitzone fuer die taegliche Erinnerung.",
        "level": "admin",
        "options": [
            {"name": "hour", "description": "Stunde 0-23", "type": "integer", "required": True},
            {"name": "timezone", "description": "IANA-Zeitzone, z. B. Europe/Berlin", "type": "string", "required": False}
        ]
    })
    async def birthday_time(ctx, args):
        guild = _get_guild(ctx)
        if guild is None:
            return await safe_reply(ctx, "Zeitmaschinen werden nur serverweit geeicht.")
        try:
            hour = int(_get_arg(args, "hour"))
        except Exception:
            return await safe_reply(ctx, "Die Stunde muss eine Zahl von 0 bis 23 sein. Keine Sanduhrpoesie, bitte.")
        if hour < 0 or hour > 23:
            return await safe_reply(ctx, "Die Stunde muss zwischen 0 und 23 liegen. Der Tag hat leider nur 24 kleine Tueren.")
        timezone = _get_arg(args, "timezone", None)
        cfg = await get_config(guild.id)
        cfg["announceHour"] = hour
        if timezone:
            try:
                ZoneInfo(str(timezone))
            except Exception:
                return await safe_reply(ctx, "Diese Zeitzone kenne ich nicht. Nutze z. B. `Europe/Berlin` oder `UTC`.")
            cfg["timezone"] = str(timezone)
        await save_config(guild.id, cfg)
        await safe_reply(ctx, f"Die Geburtstagsuhr schlaegt nun um {cfg['announceHour']:02d}:00 in `{cfg['timezone']}`. Tick-tack, kleines Kuchenmonster!")

    @api.command({
        "names": ["birthday_status"],
        "description": "Zeigt die aktuelle Geburtstags-Konfiguration.",
        "level": "admin",
        "options": []
    })
    async def birthday_status(ctx, args):
        guild = _get_guild(ctx)
        if guild is None:
            return await safe_reply(ctx, "Status ohne Server? Das ist wie Zirkus ohne Zelt.")
        cfg = await get_config(guild.id)
        data = await get_birthdays(guild.id)
        channel = f"<#{cfg['channelId']}>" if cfg.get("channelId") else "nicht gesetzt"
        await safe_reply(ctx,
            "Geburtstags-Manege Status:\n"
            f"- Aktiv: {cfg.get('enabled')}\n"
            f"- Kanal: {channel}\n"
            f"- Uhrzeit: {int(cfg.get('announceHour', 9)):02d}:00\n"
            f"- Zeitzone: `{cfg.get('timezone', 'UTC')}`\n"
            f"- Nutzer-Eintraege: {len(data)}\n"
            f"- Letzter Lauf: {cfg.get('lastRunDate') or 'noch nie'}"
        )

    @api.command({
        "names": ["birthday_audit"],
        "description": "Kinger-Diagnose: prueft Geburtstagsdaten und Konfiguration ohne Daten zu veraendern.",
        "level": "kinger",
        "options": []
    })
    async def birthday_audit(ctx, args):
        guild = _get_guild(ctx)
        if guild is None:
            return await safe_reply(ctx, "Audit nur im Server-Zelt, verehrter Kinger.")
        cfg = await get_config(guild.id)
        data = await get_birthdays(guild.id)
        invalid = 0
        for rec in data.values():
            try:
                _birthday_date_for_year(2024, int(rec["month"]), int(rec["day"]))
            except Exception:
                invalid += 1
        warnings = []
        if cfg.get("enabled") and not cfg.get("channelId"):
            warnings.append("Automatik aktiv, aber kein Erinnerungskanal gesetzt.")
        try:
            ZoneInfo(cfg.get("timezone", "UTC"))
        except Exception:
            warnings.append("Ungueltige Zeitzone in der Config.")
        if not warnings:
            warnings.append("Keine Warnungen. Die Zahnräder schnurren bedenklich zufrieden.")
        await safe_reply(ctx,
            "Kinger-Audit der Geburtstags-Manege:\n"
            f"- Guild-ID: `{guild.id}`\n"
            f"- Eintraege: {len(data)}\n"
            f"- Ungueltige Eintraege: {invalid}\n"
            f"- Config: enabled={cfg.get('enabled')}, channelId={cfg.get('channelId')}, hour={cfg.get('announceHour')}, timezone={cfg.get('timezone')}\n"
            "- Hinweise:\n  - " + "\n  - ".join(warnings)
        )

    async def announce_for_guild(guild):
        cfg = await get_config(guild.id)
        if not cfg.get("enabled", True):
            return
        channel_id = cfg.get("channelId")
        if not channel_id:
            return
        tz = _safe_timezone(cfg.get("timezone", "UTC"))
        now = datetime.datetime.now(tz)
        today = now.date()
        if int(cfg.get("announceHour", 9)) != now.hour:
            return
        if cfg.get("lastRunDate") == today.isoformat():
            return

        data = await get_birthdays(guild.id)
        users = []
        for user_id, rec in data.items():
            try:
                event_date = _birthday_date_for_year(today.year, int(rec["month"]), int(rec["day"]))
                if event_date == today:
                    users.append(int(user_id))
            except Exception:
                continue

        # Mark the daily run even when nobody has birthday, to prevent repeated checks during the same hour.
        cfg["lastRunDate"] = today.isoformat()
        await save_config(guild.id, cfg)

        if not users:
            return

        channel = None
        try:
            channel = api.bot.get_channel(int(channel_id))
            if channel is None:
                channel = await api.bot.fetch_channel(int(channel_id))
        except Exception:
            return
        if channel is None:
            return

        if cfg.get("mentionUsers", True):
            user_text = ", ".join(f"<@{uid}>" for uid in users)
        else:
            user_text = ", ".join(str(uid) for uid in users)
        template = str(cfg.get("messageTemplate") or DEFAULT_CONFIG["messageTemplate"])
        message = template.replace("{users}", user_text).replace("{count}", str(len(users)))
        try:
            await channel.send(message)
            await safe_emit("birthday.announced", {
                "guildId": int(guild.id),
                "channelId": int(channel_id),
                "userIds": users,
                "date": today.isoformat()
            })
        except Exception:
            pass

    async def birthday_loop():
        try:
            await api.bot.wait_until_ready()
        except Exception:
            pass
        while True:
            try:
                if hasattr(api.bot, "is_closed") and api.bot.is_closed():
                    break
                for guild in list(getattr(api.bot, "guilds", [])):
                    await announce_for_guild(guild)
            except Exception as exc:
                print(f"[birthday] daily loop error: {exc}")
            await asyncio.sleep(60)

    try:
        api.bot.loop.create_task(birthday_loop())
    except Exception:
        asyncio.create_task(birthday_loop())
