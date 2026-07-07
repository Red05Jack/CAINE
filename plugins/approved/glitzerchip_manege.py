import asyncio
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

PLUGIN = {
    "name": "Glitzerchip-Manege",
    "description": "Server-Economy mit Geld, Minigames, Shop, Inventar, Transfers und Admin-Werkzeugen. Vorhang auf fuer die kleine Finanzmanege!"
}


async def setup_plugin(api):
    locks: Dict[str, asyncio.Lock] = {}

    DEFAULT_CONFIG = {
        "currencyName": "Glitzerchips",
        "startingBalance": 50,
        "dailyAmount": 150,
        "dailyCooldownSeconds": 20 * 60 * 60,
        "dailyStreakBonus": 25,
        "dailyMaxStreakBonus": 250,
        "workMin": 20,
        "workMax": 85,
        "workCooldownSeconds": 30 * 60,
        "maxBet": 1000,
        "minBet": 5,
        "transferEnabled": True,
        "leaderboardSize": 10
    }

    DEFAULT_SHOP = {
        "confetti": {
            "name": "Konfetti-Kanone",
            "price": 250,
            "description": "Ein harmloser Knall aus digitalem Papierglanz."
        },
        "ticket": {
            "name": "Goldenes Manege-Ticket",
            "price": 750,
            "description": "Ein Sammlerstueck fuer Freunde der feinen Discord-Dramatik."
        },
        "crown": {
            "name": "Mini-Krone aus Pixelmessing",
            "price": 2500,
            "description": "Sie funkelt nicht wirklich. Aber sie behauptet es sehr ueberzeugend."
        }
    }

    def now_ts() -> int:
        return int(time.time())

    def normalize_args(args: Any) -> List[str]:
        if args is None:
            return []
        if isinstance(args, str):
            return [p for p in args.strip().split() if p]
        if isinstance(args, (list, tuple)):
            out = []
            for item in args:
                if item is None:
                    continue
                s = str(item).strip()
                if s:
                    out.append(s)
            return out
        return [str(args).strip()] if str(args).strip() else []

    def get_author(ctx: Any) -> Any:
        if hasattr(ctx, "author"):
            return ctx.author
        if hasattr(ctx, "message") and hasattr(ctx.message, "author"):
            return ctx.message.author
        return None

    def get_guild(ctx: Any) -> Any:
        if hasattr(ctx, "guild"):
            return ctx.guild
        if hasattr(ctx, "message") and hasattr(ctx.message, "guild"):
            return ctx.message.guild
        return None

    def get_guild_id(ctx: Any) -> str:
        guild = get_guild(ctx)
        if guild is not None and getattr(guild, "id", None) is not None:
            return str(guild.id)
        return "dm"

    def get_user_id_from_author(author: Any) -> Optional[str]:
        if author is None or getattr(author, "id", None) is None:
            return None
        return str(author.id)

    def display_user(user_id: str) -> str:
        return f"<@{user_id}>"

    def fmt_amount(amount: int, config: Dict[str, Any]) -> str:
        name = str(config.get("currencyName", "Glitzerchips"))
        return f"{int(amount):,}".replace(",", ".") + f" {name}"

    def seconds_left(last: int, cooldown: int) -> int:
        return max(0, int(last) + int(cooldown) - now_ts())

    def fmt_duration(seconds: int) -> str:
        seconds = max(0, int(seconds))
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        parts = []
        if h:
            parts.append(f"{h}h")
        if m:
            parts.append(f"{m}m")
        if s or not parts:
            parts.append(f"{s}s")
        return " ".join(parts)

    async def storage_get(key: str, default: Any):
        try:
            value = await api.storage_get(key)
        except TypeError:
            value = await api.storage_get(key, default)
        if value is None:
            return default
        return value

    async def storage_set(key: str, value: Any):
        await api.storage_set(key, value)

    def config_key(guild_id: str) -> str:
        return f"glitzerchip:{guild_id}:config"

    def users_key(guild_id: str) -> str:
        return f"glitzerchip:{guild_id}:users"

    def shop_key(guild_id: str) -> str:
        return f"glitzerchip:{guild_id}:shop"

    def get_lock(guild_id: str) -> asyncio.Lock:
        if guild_id not in locks:
            locks[guild_id] = asyncio.Lock()
        return locks[guild_id]

    async def get_config(guild_id: str) -> Dict[str, Any]:
        stored = await storage_get(config_key(guild_id), {})
        if not isinstance(stored, dict):
            stored = {}
        config = dict(DEFAULT_CONFIG)
        config.update(stored)
        return sanitize_config(config)

    def sanitize_config(config: Dict[str, Any]) -> Dict[str, Any]:
        clean = dict(DEFAULT_CONFIG)
        clean.update(config if isinstance(config, dict) else {})
        int_fields = [
            "startingBalance", "dailyAmount", "dailyCooldownSeconds", "dailyStreakBonus",
            "dailyMaxStreakBonus", "workMin", "workMax", "workCooldownSeconds",
            "maxBet", "minBet", "leaderboardSize"
        ]
        for field in int_fields:
            try:
                clean[field] = int(clean[field])
            except Exception:
                clean[field] = int(DEFAULT_CONFIG[field])
        clean["startingBalance"] = max(0, clean["startingBalance"])
        clean["dailyAmount"] = max(0, clean["dailyAmount"])
        clean["dailyCooldownSeconds"] = max(60, clean["dailyCooldownSeconds"])
        clean["dailyStreakBonus"] = max(0, clean["dailyStreakBonus"])
        clean["dailyMaxStreakBonus"] = max(0, clean["dailyMaxStreakBonus"])
        clean["workMin"] = max(0, clean["workMin"])
        clean["workMax"] = max(clean["workMin"], clean["workMax"])
        clean["workCooldownSeconds"] = max(60, clean["workCooldownSeconds"])
        clean["minBet"] = max(1, clean["minBet"])
        clean["maxBet"] = max(clean["minBet"], clean["maxBet"])
        clean["leaderboardSize"] = min(25, max(3, clean["leaderboardSize"]))
        clean["transferEnabled"] = bool(clean.get("transferEnabled", True))
        clean["currencyName"] = str(clean.get("currencyName", "Glitzerchips"))[:40]
        return clean

    async def save_config(guild_id: str, config: Dict[str, Any]):
        await storage_set(config_key(guild_id), sanitize_config(config))

    async def get_users(guild_id: str) -> Dict[str, Any]:
        users = await storage_get(users_key(guild_id), {})
        if not isinstance(users, dict):
            return {}
        return users

    async def save_users(guild_id: str, users: Dict[str, Any]):
        await storage_set(users_key(guild_id), users)

    async def get_shop(guild_id: str) -> Dict[str, Any]:
        shop = await storage_get(shop_key(guild_id), None)
        if not isinstance(shop, dict):
            shop = dict(DEFAULT_SHOP)
            await storage_set(shop_key(guild_id), shop)
        return shop

    async def save_shop(guild_id: str, shop: Dict[str, Any]):
        await storage_set(shop_key(guild_id), shop)

    def ensure_user(users: Dict[str, Any], user_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
        if user_id not in users or not isinstance(users.get(user_id), dict):
            users[user_id] = {
                "balance": int(config.get("startingBalance", 50)),
                "createdAt": now_ts(),
                "lastDaily": 0,
                "dailyStreak": 0,
                "lastWork": 0,
                "gamesPlayed": 0,
                "gamesWon": 0,
                "earnedTotal": int(config.get("startingBalance", 50)),
                "spentTotal": 0,
                "inventory": {}
            }
        user = users[user_id]
        user.setdefault("balance", int(config.get("startingBalance", 50)))
        user.setdefault("createdAt", now_ts())
        user.setdefault("lastDaily", 0)
        user.setdefault("dailyStreak", 0)
        user.setdefault("lastWork", 0)
        user.setdefault("gamesPlayed", 0)
        user.setdefault("gamesWon", 0)
        user.setdefault("earnedTotal", 0)
        user.setdefault("spentTotal", 0)
        user.setdefault("inventory", {})
        try:
            user["balance"] = int(user["balance"])
        except Exception:
            user["balance"] = 0
        if not isinstance(user["inventory"], dict):
            user["inventory"] = {}
        return user

    def parse_positive_int(value: str) -> Optional[int]:
        if value is None:
            return None
        cleaned = str(value).replace(".", "").replace(",", "").strip()
        if not re.fullmatch(r"\d+", cleaned):
            return None
        amount = int(cleaned)
        if amount <= 0:
            return None
        return amount

    def parse_bool(value: str) -> Optional[bool]:
        lowered = str(value).strip().lower()
        if lowered in ("true", "1", "yes", "ja", "on", "an"):
            return True
        if lowered in ("false", "0", "no", "nein", "off", "aus"):
            return False
        return None

    def mentioned_users(ctx: Any) -> List[Any]:
        msg = getattr(ctx, "message", ctx)
        mentions = getattr(msg, "mentions", None)
        if mentions is None:
            return []
        return list(mentions)

    def parse_user_id(ctx: Any, token: str) -> Optional[str]:
        mentions = mentioned_users(ctx)
        if mentions and ("<@" in str(token) or str(token).startswith("@")):
            mid = getattr(mentions[0], "id", None)
            if mid is not None:
                return str(mid)
        match = re.search(r"(\d{15,25})", str(token))
        if match:
            return match.group(1)
        return None

    async def send_usage(ctx: Any, config: Dict[str, Any]):
        text = (
            "🎪 **Glitzerchip-Manege — Befehle**\n"
            "`!geld konto` — zeigt dein Konto\n"
            "`!geld daily` — taegliche Belohnung abholen\n"
            "`!geld arbeit` — kleine Arbeit erledigen und Geld verdienen\n"
            "`!geld muenze <kopf|zahl> <einsatz>` — Muenzwurf-Minispiel\n"
            "`!geld slots <einsatz>` — Slot-Maschine drehen\n"
            "`!geld shop` — Shop ansehen\n"
            "`!geld kauf <item_id>` — Item kaufen\n"
            "`!geld inventar` — dein Inventar ansehen\n"
            "`!geld senden @user <betrag>` — Geld ueberweisen\n"
            "`!geld top` — reichste Manege-Mitglieder\n\n"
            f"Waehrung: **{config.get('currencyName', 'Glitzerchips')}**"
        )
        await api.reply(ctx, text)

    async def cmd_balance(ctx: Any, parts: List[str], guild_id: str, user_id: str, config: Dict[str, Any]):
        users = await get_users(guild_id)
        user = ensure_user(users, user_id, config)
        await save_users(guild_id, users)
        await api.reply(
            ctx,
            f"🎩 Dein Konto klimpert wunderbar: **{fmt_amount(user['balance'], config)}**. "
            f"Verdient gesamt: {fmt_amount(int(user.get('earnedTotal', 0)), config)}."
        )

    async def cmd_daily(ctx: Any, guild_id: str, user_id: str, config: Dict[str, Any]):
        async with get_lock(guild_id):
            users = await get_users(guild_id)
            user = ensure_user(users, user_id, config)
            wait = seconds_left(int(user.get("lastDaily", 0)), int(config["dailyCooldownSeconds"]))
            if wait > 0:
                await api.reply(ctx, f"⏳ Die Tageskasse ist noch verschlossen. Komm in **{fmt_duration(wait)}** wieder, kleine Schatzsucherseele.")
                return

            last_daily = int(user.get("lastDaily", 0))
            if last_daily and now_ts() - last_daily <= 48 * 60 * 60:
                user["dailyStreak"] = int(user.get("dailyStreak", 0)) + 1
            else:
                user["dailyStreak"] = 1

            streak_bonus = min(
                int(config["dailyMaxStreakBonus"]),
                max(0, int(user["dailyStreak"]) - 1) * int(config["dailyStreakBonus"])
            )
            amount = int(config["dailyAmount"]) + streak_bonus
            user["balance"] = int(user["balance"]) + amount
            user["earnedTotal"] = int(user.get("earnedTotal", 0)) + amount
            user["lastDaily"] = now_ts()
            await save_users(guild_id, users)

        await api.reply(ctx, f"🎁 Tagesbelohnung! Du erhaeltst **{fmt_amount(amount, config)}**. Streak: **{user['dailyStreak']}** Tage. Konfetti der Funktionalitaet!")

    async def cmd_work(ctx: Any, guild_id: str, user_id: str, config: Dict[str, Any]):
        async with get_lock(guild_id):
            users = await get_users(guild_id)
            user = ensure_user(users, user_id, config)
            wait = seconds_left(int(user.get("lastWork", 0)), int(config["workCooldownSeconds"]))
            if wait > 0:
                await api.reply(ctx, f"🧹 Deine Haende sind noch voller digitalem Saegemehl. Naechste Arbeit in **{fmt_duration(wait)}**.")
                return

            jobs = [
                "du hast die Manege gefegt, ohne den Server zu loeschen",
                "du hast einem Roboterclown die Schuhe sortiert",
                "du hast 17 Zahnraeder gezahlt und nur 16 gefunden",
                "du hast den Shop poliert, bis er verdaechtig glaenzte",
                "du hast die Slot-Maschine beruhigt, sie zitterte schon wieder"
            ]
            amount = random.randint(int(config["workMin"]), int(config["workMax"]))
            user["balance"] = int(user["balance"]) + amount
            user["earnedTotal"] = int(user.get("earnedTotal", 0)) + amount
            user["lastWork"] = now_ts()
            await save_users(guild_id, users)

        await api.reply(ctx, f"🛠️ Arbeit erledigt: {random.choice(jobs)}. Lohn: **{fmt_amount(amount, config)}**!")

    async def validate_bet(ctx: Any, user: Dict[str, Any], amount: Optional[int], config: Dict[str, Any]) -> Optional[int]:
        if amount is None:
            await api.reply(ctx, f"🎲 Bitte nenne einen gueltigen Einsatz. Beispiel: `!geld slots {config['minBet']}`")
            return None
        if amount < int(config["minBet"]):
            await api.reply(ctx, f"🪙 Der Mindesteinsatz ist **{fmt_amount(int(config['minBet']), config)}**.")
            return None
        if amount > int(config["maxBet"]):
            await api.reply(ctx, f"🚧 Der Maximaleinsatz ist **{fmt_amount(int(config['maxBet']), config)}**. Die Manege bleibt standfest!")
            return None
        if int(user.get("balance", 0)) < amount:
            await api.reply(ctx, f"💸 Deine Tasche klimpert zu leise. Du hast nur **{fmt_amount(int(user.get('balance', 0)), config)}**.")
            return None
        return amount

    async def cmd_coin(ctx: Any, parts: List[str], guild_id: str, user_id: str, config: Dict[str, Any]):
        if len(parts) < 3:
            await api.reply(ctx, "🪙 Nutzung: `!geld muenze <kopf|zahl> <einsatz>`")
            return
        choice = parts[1].lower()
        if choice in ("heads", "head", "k", "kopf"):
            choice = "kopf"
        elif choice in ("tails", "tail", "z", "zahl"):
            choice = "zahl"
        else:
            await api.reply(ctx, "🪙 Waehle `kopf` oder `zahl`. Die Muenze akzeptiert keine philosophischen Grauzonen.")
            return
        bet = parse_positive_int(parts[2])

        async with get_lock(guild_id):
            users = await get_users(guild_id)
            user = ensure_user(users, user_id, config)
            bet = await validate_bet(ctx, user, bet, config)
            if bet is None:
                return
            result = random.choice(["kopf", "zahl"])
            user["gamesPlayed"] = int(user.get("gamesPlayed", 0)) + 1
            if result == choice:
                user["balance"] = int(user["balance"]) + bet
                user["gamesWon"] = int(user.get("gamesWon", 0)) + 1
                user["earnedTotal"] = int(user.get("earnedTotal", 0)) + bet
                outcome = f"✨ **{result.upper()}!** Du gewinnst **{fmt_amount(bet, config)}**. Die Muenze verbeugt sich."
            else:
                user["balance"] = int(user["balance"]) - bet
                user["spentTotal"] = int(user.get("spentTotal", 0)) + bet
                outcome = f"🌘 **{result.upper()}!** Du verlierst **{fmt_amount(bet, config)}**. Die Muenze lacht sehr leise."
            balance = int(user["balance"])
            await save_users(guild_id, users)

        await api.reply(ctx, f"{outcome}\nKontostand: **{fmt_amount(balance, config)}**")

    async def cmd_slots(ctx: Any, parts: List[str], guild_id: str, user_id: str, config: Dict[str, Any]):
        if len(parts) < 2:
            await api.reply(ctx, "🎰 Nutzung: `!geld slots <einsatz>`")
            return
        bet = parse_positive_int(parts[1])
        symbols = ["🍒", "🍋", "🔔", "⭐", "💎"]

        async with get_lock(guild_id):
            users = await get_users(guild_id)
            user = ensure_user(users, user_id, config)
            bet = await validate_bet(ctx, user, bet, config)
            if bet is None:
                return

            roll = [random.choice(symbols) for _ in range(3)]
            user["balance"] = int(user["balance"]) - bet
            user["spentTotal"] = int(user.get("spentTotal", 0)) + bet
            user["gamesPlayed"] = int(user.get("gamesPlayed", 0)) + 1
            payout = 0
            reason = "kein Treffer"
            if roll[0] == roll[1] == roll[2]:
                if roll[0] == "💎":
                    payout = bet * 8
                    reason = "Diamant-Jackpot"
                elif roll[0] == "⭐":
                    payout = bet * 6
                    reason = "Sternen-Triple"
                else:
                    payout = bet * 4
                    reason = "Triple"
            elif len(set(roll)) == 2:
                payout = int(bet * 1.5)
                reason = "Doppel-Treffer"

            if payout > 0:
                user["balance"] = int(user["balance"]) + payout
                user["earnedTotal"] = int(user.get("earnedTotal", 0)) + payout
                user["gamesWon"] = int(user.get("gamesWon", 0)) + 1
            balance = int(user["balance"])
            await save_users(guild_id, users)

        reel = " | ".join(roll)
        if payout > 0:
            await api.reply(ctx, f"🎰 `{reel}` — **{reason}!** Auszahlung: **{fmt_amount(payout, config)}**. Kontostand: **{fmt_amount(balance, config)}**")
        else:
            await api.reply(ctx, f"🎰 `{reel}` — nichts gewonnen. Die Maschine macht ein unschuldiges Geraeusch. Kontostand: **{fmt_amount(balance, config)}**")

    async def cmd_shop(ctx: Any, guild_id: str, config: Dict[str, Any]):
        shop = await get_shop(guild_id)
        if not shop:
            await api.reply(ctx, "🛒 Der Shop ist leer. Eine traurige Vitrine, aber immerhin staubfrei.")
            return
        lines = ["🛒 **Shop der Glitzerchip-Manege**"]
        for item_id, item in sorted(shop.items(), key=lambda kv: str(kv[0])):
            name = str(item.get("name", item_id))[:80]
            price = int(item.get("price", 0))
            desc = str(item.get("description", ""))[:120]
            lines.append(f"`{item_id}` — **{name}** — {fmt_amount(price, config)}\n↳ {desc}")
        lines.append("\nKaufen mit: `!geld kauf <item_id>`")
        await api.reply(ctx, "\n".join(lines))

    async def cmd_buy(ctx: Any, parts: List[str], guild_id: str, user_id: str, config: Dict[str, Any]):
        if len(parts) < 2:
            await api.reply(ctx, "🎟️ Nutzung: `!geld kauf <item_id>`")
            return
        item_id = parts[1].lower().strip()
        async with get_lock(guild_id):
            shop = await get_shop(guild_id)
            if item_id not in shop:
                await api.reply(ctx, "🕳️ Dieses Item existiert nicht im Shop. Es ist vermutlich in eine andere Dimension gefallen.")
                return
            item = shop[item_id]
            price = int(item.get("price", 0))
            users = await get_users(guild_id)
            user = ensure_user(users, user_id, config)
            if int(user["balance"]) < price:
                await api.reply(ctx, f"💸 Nicht genug Geld. Benoetigt: **{fmt_amount(price, config)}**, vorhanden: **{fmt_amount(int(user['balance']), config)}**.")
                return
            user["balance"] = int(user["balance"]) - price
            user["spentTotal"] = int(user.get("spentTotal", 0)) + price
            inv = user.setdefault("inventory", {})
            inv[item_id] = int(inv.get(item_id, 0)) + 1
            balance = int(user["balance"])
            await save_users(guild_id, users)
        await api.reply(ctx, f"🎁 Gekauft: **{item.get('name', item_id)}** fuer **{fmt_amount(price, config)}**. Neuer Kontostand: **{fmt_amount(balance, config)}**.")

    async def cmd_inventory(ctx: Any, guild_id: str, user_id: str, config: Dict[str, Any]):
        users = await get_users(guild_id)
        user = ensure_user(users, user_id, config)
        await save_users(guild_id, users)
        inv = user.get("inventory", {})
        if not inv:
            await api.reply(ctx, "🎒 Dein Inventar ist leer. Es hallt darin. Sehr dramatisch.")
            return
        shop = await get_shop(guild_id)
        lines = ["🎒 **Dein Inventar**"]
        for item_id, count in sorted(inv.items()):
            item = shop.get(item_id, {})
            name = item.get("name", item_id)
            lines.append(f"`{item_id}` — **{name}** x{int(count)}")
        await api.reply(ctx, "\n".join(lines))

    async def cmd_transfer(ctx: Any, parts: List[str], guild_id: str, user_id: str, config: Dict[str, Any]):
        if not bool(config.get("transferEnabled", True)):
            await api.reply(ctx, "🚧 Ueberweisungen sind auf diesem Server deaktiviert. Die Geldroehren sind versiegelt.")
            return
        if len(parts) < 3:
            await api.reply(ctx, "💌 Nutzung: `!geld senden @user <betrag>`")
            return
        target_id = parse_user_id(ctx, parts[1])
        amount = parse_positive_int(parts[2])
        if target_id is None:
            await api.reply(ctx, "👤 Ich finde diesen User nicht. Bitte nutze eine Erwaehnung oder eine User-ID.")
            return
        if target_id == user_id:
            await api.reply(ctx, "🪞 Du kannst dir nicht selbst Geld senden. Das ist Buchhaltung mit Spiegeln.")
            return
        if amount is None:
            await api.reply(ctx, "💰 Bitte nenne einen gueltigen Betrag.")
            return

        async with get_lock(guild_id):
            users = await get_users(guild_id)
            sender = ensure_user(users, user_id, config)
            receiver = ensure_user(users, target_id, config)
            if int(sender["balance"]) < amount:
                await api.reply(ctx, f"💸 Nicht genug Geld. Dein Kontostand: **{fmt_amount(int(sender['balance']), config)}**.")
                return
            sender["balance"] = int(sender["balance"]) - amount
            receiver["balance"] = int(receiver["balance"]) + amount
            await save_users(guild_id, users)
        await api.reply(ctx, f"💌 {display_user(user_id)} sendet {display_user(target_id)} **{fmt_amount(amount, config)}**. Die Geldtaube ist gelandet.")

    async def cmd_top(ctx: Any, guild_id: str, config: Dict[str, Any]):
        users = await get_users(guild_id)
        if not users:
            await api.reply(ctx, "🏆 Noch keine Konten vorhanden. Die Rangliste wartet hungrig.")
            return
        ranking: List[Tuple[str, int]] = []
        for uid, data in users.items():
            if isinstance(data, dict):
                try:
                    ranking.append((str(uid), int(data.get("balance", 0))))
                except Exception:
                    pass
        ranking.sort(key=lambda x: x[1], reverse=True)
        limit = int(config.get("leaderboardSize", 10))
        lines = ["🏆 **Reichste der Manege**"]
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, bal) in enumerate(ranking[:limit], start=1):
            prefix = medals[i - 1] if i <= 3 else f"`#{i}`"
            lines.append(f"{prefix} {display_user(uid)} — **{fmt_amount(bal, config)}**")
        await api.reply(ctx, "\n".join(lines))

    @api.command({
        "names": ["geld", "money", "coins"],
        "description": "Economy: Konto, Daily, Arbeit, Minigames, Shop, Inventar, Transfers und Rangliste.",
        "level": "user"
    })
    async def geld_command(ctx, args):
        parts = normalize_args(args)
        guild_id = get_guild_id(ctx)
        author = get_author(ctx)
        user_id = get_user_id_from_author(author)
        if user_id is None:
            await api.reply(ctx, "⚠️ Ich kann dich nicht eindeutig erkennen. Die Manege verlangt eine Autor-ID.")
            return
        config = await get_config(guild_id)

        if not parts:
            await send_usage(ctx, config)
            return

        sub = parts[0].lower()
        if sub in ("konto", "balance", "bal", "stand"):
            await cmd_balance(ctx, parts, guild_id, user_id, config)
        elif sub in ("daily", "tag", "taeglich", "täglich"):
            await cmd_daily(ctx, guild_id, user_id, config)
        elif sub in ("arbeit", "work", "job"):
            await cmd_work(ctx, guild_id, user_id, config)
        elif sub in ("muenze", "münze", "coin", "coinflip"):
            await cmd_coin(ctx, parts, guild_id, user_id, config)
        elif sub in ("slots", "slot", "automat"):
            await cmd_slots(ctx, parts, guild_id, user_id, config)
        elif sub in ("shop", "laden"):
            await cmd_shop(ctx, guild_id, config)
        elif sub in ("kauf", "buy", "purchase"):
            await cmd_buy(ctx, parts, guild_id, user_id, config)
        elif sub in ("inventar", "inventory", "inv", "items"):
            await cmd_inventory(ctx, guild_id, user_id, config)
        elif sub in ("senden", "send", "pay", "ueberweisen", "überweisen"):
            await cmd_transfer(ctx, parts, guild_id, user_id, config)
        elif sub in ("top", "leaderboard", "rangliste"):
            await cmd_top(ctx, guild_id, config)
        else:
            await send_usage(ctx, config)

    async def admin_usage(ctx: Any):
        await api.reply(ctx, (
            "🎛️ **Geldadmin — Werkzeuge hinter dem roten Vorhang**\n"
            "`!geldadmin inspect @user` — Konto ansehen\n"
            "`!geldadmin add @user <betrag> [grund]` — Geld hinzufuegen\n"
            "`!geldadmin remove @user <betrag> [grund]` — Geld entfernen\n"
            "`!geldadmin set @user <betrag> [grund]` — Kontostand setzen\n"
            "`!geldadmin reset @user confirm` — Userkonto zuruecksetzen\n"
            "`!geldadmin shopadd <id> <preis> <name...>` — Shop-Item anlegen\n"
            "`!geldadmin shopremove <id>` — Shop-Item entfernen\n"
            "`!geldadmin config` — Config anzeigen\n"
            "`!geldadmin config <key> <value>` — Config-Wert setzen"
        ))

    async def admin_money_change(ctx: Any, mode: str, parts: List[str], guild_id: str, config: Dict[str, Any]):
        if len(parts) < 3:
            await api.reply(ctx, f"🎛️ Nutzung: `!geldadmin {mode} @user <betrag> [grund]`")
            return
        target_id = parse_user_id(ctx, parts[1])
        amount = parse_positive_int(parts[2])
        if target_id is None or amount is None:
            await api.reply(ctx, "⚠️ User oder Betrag ungueltig. Die Maschine frisst nur klare Zahlen und echte IDs.")
            return
        reason = " ".join(parts[3:])[:160] if len(parts) > 3 else "kein Grund angegeben"

        async with get_lock(guild_id):
            users = await get_users(guild_id)
            user = ensure_user(users, target_id, config)
            before = int(user["balance"])
            if mode == "add":
                user["balance"] = before + amount
                user["earnedTotal"] = int(user.get("earnedTotal", 0)) + amount
            elif mode == "remove":
                user["balance"] = max(0, before - amount)
                user["spentTotal"] = int(user.get("spentTotal", 0)) + min(before, amount)
            elif mode == "set":
                user["balance"] = amount
            after = int(user["balance"])
            await save_users(guild_id, users)
        await api.reply(ctx, f"✅ Konto von {display_user(target_id)}: **{fmt_amount(before, config)}** → **{fmt_amount(after, config)}**. Grund: {reason}")

    async def admin_inspect(ctx: Any, parts: List[str], guild_id: str, config: Dict[str, Any]):
        if len(parts) < 2:
            await api.reply(ctx, "🔍 Nutzung: `!geldadmin inspect @user`")
            return
        target_id = parse_user_id(ctx, parts[1])
        if target_id is None:
            await api.reply(ctx, "👤 User nicht gefunden.")
            return
        users = await get_users(guild_id)
        user = ensure_user(users, target_id, config)
        await save_users(guild_id, users)
        inv_count = sum(int(v) for v in user.get("inventory", {}).values())
        await api.reply(ctx, (
            f"🔍 **Konto-Inspektion fuer {display_user(target_id)}**\n"
            f"Balance: **{fmt_amount(int(user.get('balance', 0)), config)}**\n"
            f"Daily-Streak: **{int(user.get('dailyStreak', 0))}**\n"
            f"Games: **{int(user.get('gamesWon', 0))}/{int(user.get('gamesPlayed', 0))}** gewonnen\n"
            f"Verdient gesamt: **{fmt_amount(int(user.get('earnedTotal', 0)), config)}**\n"
            f"Ausgegeben/verloren: **{fmt_amount(int(user.get('spentTotal', 0)), config)}**\n"
            f"Inventar-Items: **{inv_count}**"
        ))

    async def admin_reset(ctx: Any, parts: List[str], guild_id: str, config: Dict[str, Any]):
        if len(parts) < 3 or parts[2].lower() != "confirm":
            await api.reply(ctx, "⚠️ Nutzung: `!geldadmin reset @user confirm` — absichtliche Bestaetigung noetig, damit kein Konfetti in die Datenbank faellt.")
            return
        target_id = parse_user_id(ctx, parts[1])
        if target_id is None:
            await api.reply(ctx, "👤 User nicht gefunden.")
            return
        async with get_lock(guild_id):
            users = await get_users(guild_id)
            if target_id in users:
                del users[target_id]
            ensure_user(users, target_id, config)
            await save_users(guild_id, users)
        await api.reply(ctx, f"♻️ Konto von {display_user(target_id)} wurde auf Startwerte zurueckgesetzt.")

    async def admin_shopadd(ctx: Any, parts: List[str], guild_id: str, config: Dict[str, Any]):
        if len(parts) < 4:
            await api.reply(ctx, "🛒 Nutzung: `!geldadmin shopadd <id> <preis> <name...>`")
            return
        item_id = re.sub(r"[^a-zA-Z0-9_-]", "", parts[1].lower())[:32]
        price = parse_positive_int(parts[2])
        name = " ".join(parts[3:]).strip()[:80]
        if not item_id or price is None or not name:
            await api.reply(ctx, "⚠️ Item-ID, Preis oder Name ungueltig.")
            return
        async with get_lock(guild_id):
            shop = await get_shop(guild_id)
            shop[item_id] = {
                "name": name,
                "price": price,
                "description": "Vom Admin erschaffenes Manege-Item."
            }
            await save_shop(guild_id, shop)
        await api.reply(ctx, f"✅ Shop-Item `{item_id}` erstellt: **{name}** fuer **{fmt_amount(price, config)}**.")

    async def admin_shopremove(ctx: Any, parts: List[str], guild_id: str):
        if len(parts) < 2:
            await api.reply(ctx, "🛒 Nutzung: `!geldadmin shopremove <id>`")
            return
        item_id = parts[1].lower().strip()
        async with get_lock(guild_id):
            shop = await get_shop(guild_id)
            if item_id not in shop:
                await api.reply(ctx, "🕳️ Dieses Shop-Item existiert nicht.")
                return
            removed = shop.pop(item_id)
            await save_shop(guild_id, shop)
        await api.reply(ctx, f"🗑️ Entfernt: `{item_id}` — **{removed.get('name', item_id)}**.")

    async def admin_config(ctx: Any, parts: List[str], guild_id: str, config: Dict[str, Any]):
        if len(parts) == 1:
            lines = ["⚙️ **Economy-Config**"]
            for key in sorted(DEFAULT_CONFIG.keys()):
                lines.append(f"`{key}` = `{config.get(key)}`")
            await api.reply(ctx, "\n".join(lines))
            return
        if len(parts) < 3:
            await api.reply(ctx, "⚙️ Nutzung: `!geldadmin config <key> <value>`")
            return
        key = parts[1]
        raw_value = " ".join(parts[2:]).strip()
        if key not in DEFAULT_CONFIG:
            await api.reply(ctx, f"⚠️ Unbekannter Config-Key `{key}`. Erlaubt: {', '.join(sorted(DEFAULT_CONFIG.keys()))}")
            return
        new_config = dict(config)
        if isinstance(DEFAULT_CONFIG[key], bool):
            parsed_bool = parse_bool(raw_value)
            if parsed_bool is None:
                await api.reply(ctx, "⚠️ Bitte booleschen Wert nutzen: true/false, ja/nein, on/off.")
                return
            new_config[key] = parsed_bool
        elif isinstance(DEFAULT_CONFIG[key], int):
            parsed_int = parse_positive_int(raw_value)
            if parsed_int is None and key != "startingBalance":
                await api.reply(ctx, "⚠️ Bitte eine positive ganze Zahl nutzen.")
                return
            new_config[key] = int(parsed_int if parsed_int is not None else 0)
        else:
            new_config[key] = raw_value[:40]
        new_config = sanitize_config(new_config)
        await save_config(guild_id, new_config)
        await api.reply(ctx, f"✅ Config `{key}` gesetzt auf `{new_config.get(key)}`. Die Stellschraube singt.")

    @api.command({
        "names": ["geldadmin", "economyadmin"],
        "description": "Admin-Werkzeuge fuer die Economy: Konten, Shop und Config verwalten.",
        "level": "admin"
    })
    async def geldadmin_command(ctx, args):
        parts = normalize_args(args)
        guild_id = get_guild_id(ctx)
        config = await get_config(guild_id)
        if not parts:
            await admin_usage(ctx)
            return
        sub = parts[0].lower()
        if sub in ("inspect", "info", "konto"):
            await admin_inspect(ctx, parts, guild_id, config)
        elif sub in ("add", "give"):
            await admin_money_change(ctx, "add", parts, guild_id, config)
        elif sub in ("remove", "take"):
            await admin_money_change(ctx, "remove", parts, guild_id, config)
        elif sub == "set":
            await admin_money_change(ctx, "set", parts, guild_id, config)
        elif sub == "reset":
            await admin_reset(ctx, parts, guild_id, config)
        elif sub == "shopadd":
            await admin_shopadd(ctx, parts, guild_id, config)
        elif sub == "shopremove":
            await admin_shopremove(ctx, parts, guild_id)
        elif sub == "config":
            await admin_config(ctx, parts, guild_id, config)
        else:
            await admin_usage(ctx)
