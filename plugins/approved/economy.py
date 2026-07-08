import asyncio
import re
import time
from typing import Any, Dict, List, Optional, Tuple

PLUGIN = {
    "name": "economy",
    "description": "Schlankes Economy-Basisplugin fuer Coins, Shop, Inventory, Transfers, Admin-Werkzeuge und Topliste."
}


async def setup_plugin(api):
    locks: Dict[str, asyncio.Lock] = {}

    DEFAULT_CONFIG = {
        "currencyName": "Glitzerchips",
        "startingBalance": 50,
        "leaderboardSize": 10,
        "transferEnabled": True
    }

    DEFAULT_SHOP = {
        "confetti": {
            "name": "Konfetti-Kanone",
            "price": 250,
            "description": "Ein einfaches Sammleritem fuer spaetere Plugin-Ideen."
        },
        "ticket": {
            "name": "Goldenes Ticket",
            "price": 750,
            "description": "Ein Platzhalter-Item fuer Economy-Integrationen."
        },
        "crown": {
            "name": "Pixel-Krone",
            "price": 2500,
            "description": "Teuer, sichtbar, absichtlich schlicht."
        }
    }

    async def migrate_legacy_namespace() -> None:
        migrate = getattr(api, "storage_migrate_from", None)
        if callable(migrate):
            try:
                await migrate("glitzerchip_manege", delete_old=True)
            except Exception:
                pass

    await migrate_legacy_namespace()

    def now_ts() -> int:
        return int(time.time())

    def normalize_args(args: Any) -> List[str]:
        if args is None:
            return []
        if isinstance(args, str):
            return [part for part in args.strip().split() if part]
        if isinstance(args, (list, tuple)):
            return [str(part).strip() for part in args if str(part).strip()]
        text = str(args).strip()
        return [text] if text else []

    async def storage_get(key: str, default: Any = None, legacy_key: str | None = None) -> Any:
        try:
            value = await api.storage_get(key)
        except TypeError:
            value = await api.storage_get(key, default)
        if value is not None:
            return value
        if legacy_key:
            try:
                legacy_value = await api.storage_get(legacy_key)
            except TypeError:
                legacy_value = await api.storage_get(legacy_key, None)
            if legacy_value is not None:
                await api.storage_set(key, legacy_value)
                try:
                    await api.storage_delete(legacy_key)
                except Exception:
                    pass
                return legacy_value
        return default

    async def storage_set(key: str, value: Any) -> None:
        await api.storage_set(key, value)

    def config_key(guild_id: str) -> str:
        return f"economy:{guild_id}:config"

    def users_key(guild_id: str) -> str:
        return f"economy:{guild_id}:users"

    def shop_key(guild_id: str) -> str:
        return f"economy:{guild_id}:shop"

    def legacy_config_key(guild_id: str) -> str:
        return f"glitzerchip:{guild_id}:config"

    def legacy_users_key(guild_id: str) -> str:
        return f"glitzerchip:{guild_id}:users"

    def legacy_shop_key(guild_id: str) -> str:
        return f"glitzerchip:{guild_id}:shop"

    def get_lock(guild_id: str) -> asyncio.Lock:
        if guild_id not in locks:
            locks[guild_id] = asyncio.Lock()
        return locks[guild_id]

    def get_author(ctx: Any) -> Any:
        if hasattr(ctx, "author"):
            return ctx.author
        if hasattr(ctx, "user"):
            return ctx.user
        if hasattr(ctx, "message"):
            return getattr(ctx.message, "author", None)
        return None

    def get_guild(ctx: Any) -> Any:
        if hasattr(ctx, "guild"):
            return ctx.guild
        if hasattr(ctx, "message"):
            return getattr(ctx.message, "guild", None)
        return None

    def get_guild_id(ctx: Any) -> str:
        guild = get_guild(ctx)
        if guild is not None and getattr(guild, "id", None) is not None:
            return str(guild.id)
        return "dm"

    def user_id_from_author(author: Any) -> Optional[str]:
        if author is None or getattr(author, "id", None) is None:
            return None
        return str(author.id)

    def display_user(user_id: str) -> str:
        return f"<@{user_id}>"

    def mentioned_users(ctx: Any) -> List[Any]:
        message = getattr(ctx, "message", ctx)
        return list(getattr(message, "mentions", None) or [])

    def parse_user_id(ctx: Any, token: str | None) -> Optional[str]:
        if token is None:
            return None
        mentions = mentioned_users(ctx)
        if mentions and ("<@" in str(token) or str(token).startswith("@")):
            mention_id = getattr(mentions[0], "id", None)
            if mention_id is not None:
                return str(mention_id)
        match = re.search(r"(\d{15,25})", str(token))
        return match.group(1) if match else None

    def parse_positive_int(value: Any) -> Optional[int]:
        cleaned = str(value or "").replace(".", "").replace(",", "").strip()
        if not re.fullmatch(r"\d+", cleaned):
            return None
        amount = int(cleaned)
        return amount if amount > 0 else None

    def sanitize_config(config: Dict[str, Any]) -> Dict[str, Any]:
        clean = dict(DEFAULT_CONFIG)
        if isinstance(config, dict):
            clean.update(config)
        try:
            clean["startingBalance"] = max(0, int(clean["startingBalance"]))
        except Exception:
            clean["startingBalance"] = DEFAULT_CONFIG["startingBalance"]
        try:
            clean["leaderboardSize"] = min(25, max(3, int(clean["leaderboardSize"])))
        except Exception:
            clean["leaderboardSize"] = DEFAULT_CONFIG["leaderboardSize"]
        clean["transferEnabled"] = bool(clean.get("transferEnabled", True))
        clean["currencyName"] = str(clean.get("currencyName", "Glitzerchips"))[:40] or "Glitzerchips"
        return clean

    async def get_config(guild_id: str) -> Dict[str, Any]:
        stored = await storage_get(config_key(guild_id), {}, legacy_key=legacy_config_key(guild_id))
        return sanitize_config(stored if isinstance(stored, dict) else {})

    async def save_config(guild_id: str, config: Dict[str, Any]) -> None:
        await storage_set(config_key(guild_id), sanitize_config(config))

    async def get_users(guild_id: str) -> Dict[str, Any]:
        users = await storage_get(users_key(guild_id), {}, legacy_key=legacy_users_key(guild_id))
        return users if isinstance(users, dict) else {}

    async def save_users(guild_id: str, users: Dict[str, Any]) -> None:
        await storage_set(users_key(guild_id), users)

    async def get_shop(guild_id: str) -> Dict[str, Any]:
        shop = await storage_get(shop_key(guild_id), None, legacy_key=legacy_shop_key(guild_id))
        if not isinstance(shop, dict):
            shop = dict(DEFAULT_SHOP)
            await storage_set(shop_key(guild_id), shop)
        return shop

    async def save_shop(guild_id: str, shop: Dict[str, Any]) -> None:
        await storage_set(shop_key(guild_id), shop if isinstance(shop, dict) else {})

    def ensure_user(users: Dict[str, Any], user_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
        if user_id not in users or not isinstance(users.get(user_id), dict):
            users[user_id] = {
                "balance": int(config.get("startingBalance", 50)),
                "createdAt": now_ts(),
                "earnedTotal": int(config.get("startingBalance", 50)),
                "spentTotal": 0,
                "inventory": {}
            }
        user = users[user_id]
        user.setdefault("balance", int(config.get("startingBalance", 50)))
        user.setdefault("createdAt", now_ts())
        user.setdefault("earnedTotal", 0)
        user.setdefault("spentTotal", 0)
        user.setdefault("inventory", {})
        try:
            user["balance"] = int(user["balance"])
        except Exception:
            user["balance"] = 0
        if not isinstance(user.get("inventory"), dict):
            user["inventory"] = {}
        return user

    def fmt_amount(amount: int, config: Dict[str, Any]) -> str:
        value = f"{int(amount):,}".replace(",", ".")
        return f"{value} {config.get('currencyName', 'Glitzerchips')}"

    async def user_context(ctx: Any) -> Optional[Tuple[str, str, Dict[str, Any]]]:
        guild_id = get_guild_id(ctx)
        author = get_author(ctx)
        user_id = user_id_from_author(author)
        if user_id is None:
            await api.reply(ctx, "Ich kann dich nicht eindeutig erkennen. Die Economy braucht eine User-ID.")
            return None
        config = await get_config(guild_id)
        return guild_id, user_id, config

    async def cmd_balance(ctx: Any, target_id: str, guild_id: str, config: Dict[str, Any]) -> None:
        users = await get_users(guild_id)
        user = ensure_user(users, target_id, config)
        await save_users(guild_id, users)
        await api.reply(ctx, f"Konto von {display_user(target_id)}: **{fmt_amount(user['balance'], config)}**")

    async def cmd_shop(ctx: Any, guild_id: str, config: Dict[str, Any]) -> None:
        shop = await get_shop(guild_id)
        if not shop:
            await api.reply(ctx, "Der Shop ist leer.")
            return
        lines = ["**Economy Shop**"]
        for item_id, item in sorted(shop.items(), key=lambda pair: str(pair[0])):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", item_id))[:80]
            price = max(0, int(item.get("price", 0)))
            desc = str(item.get("description", ""))[:120]
            extra = f" - {desc}" if desc else ""
            lines.append(f"`{item_id}` - **{fmt_amount(price, config)}** - {name}{extra}")
        lines.append("\nBuy with: `!buy <item_id> [amount]`")
        await api.reply(ctx, "\n".join(lines))

    async def cmd_buy(ctx: Any, parts: List[str], guild_id: str, user_id: str, config: Dict[str, Any]) -> None:
        if not parts:
            await api.reply(ctx, "Usage: `!buy <item_id> [amount]`")
            return
        item_id = re.sub(r"[^a-zA-Z0-9_-]", "", parts[0].lower())[:32]
        quantity = parse_positive_int(parts[1]) if len(parts) > 1 else 1
        quantity = min(100, quantity or 1)
        async with get_lock(guild_id):
            shop = await get_shop(guild_id)
            if item_id not in shop or not isinstance(shop.get(item_id), dict):
                await api.reply(ctx, "Dieses Item gibt es nicht im Shop.")
                return
            item = shop[item_id]
            price = max(0, int(item.get("price", 0)))
            total_price = price * quantity
            users = await get_users(guild_id)
            user = ensure_user(users, user_id, config)
            if int(user.get("balance", 0)) < total_price:
                await api.reply(ctx, f"Zu wenig Guthaben. Du hast **{fmt_amount(int(user.get('balance', 0)), config)}**.")
                return
            user["balance"] = int(user["balance"]) - total_price
            user["spentTotal"] = int(user.get("spentTotal", 0)) + total_price
            inventory = user.setdefault("inventory", {})
            inventory[item_id] = int(inventory.get(item_id, 0)) + quantity
            balance = int(user["balance"])
            await save_users(guild_id, users)
        await api.reply(ctx, f"Gekauft: **{quantity}x {item.get('name', item_id)}** fuer **{fmt_amount(total_price, config)}**. Kontostand: **{fmt_amount(balance, config)}**")

    async def cmd_inventory(ctx: Any, target_id: str, guild_id: str, config: Dict[str, Any]) -> None:
        users = await get_users(guild_id)
        user = ensure_user(users, target_id, config)
        await save_users(guild_id, users)
        inventory = user.get("inventory", {})
        if not inventory:
            await api.reply(ctx, f"Inventar von {display_user(target_id)} ist leer.")
            return
        shop = await get_shop(guild_id)
        lines = [f"**Inventar von {display_user(target_id)}**"]
        for item_id, quantity in sorted(inventory.items(), key=lambda pair: str(pair[0])):
            item = shop.get(item_id, {}) if isinstance(shop, dict) else {}
            name = str(item.get("name", item_id)) if isinstance(item, dict) else str(item_id)
            lines.append(f"`{item_id}` - {name}: **{int(quantity)}x**")
        await api.reply(ctx, "\n".join(lines))

    async def cmd_transfer(ctx: Any, parts: List[str], guild_id: str, user_id: str, config: Dict[str, Any]) -> None:
        if len(parts) < 2:
            await api.reply(ctx, "Usage: `!send @user <amount>`")
            return
        target_id = parse_user_id(ctx, parts[0])
        amount = parse_positive_int(parts[1])
        if target_id is None or amount is None:
            await api.reply(ctx, "Bitte nenne einen gueltigen User und Betrag.")
            return
        if target_id == user_id:
            await api.reply(ctx, "Du kannst dir nicht selbst Geld senden.")
            return
        if not bool(config.get("transferEnabled", True)):
            await api.reply(ctx, "Transfers sind in dieser Economy deaktiviert.")
            return
        async with get_lock(guild_id):
            users = await get_users(guild_id)
            sender = ensure_user(users, user_id, config)
            receiver = ensure_user(users, target_id, config)
            if int(sender.get("balance", 0)) < amount:
                await api.reply(ctx, f"Zu wenig Guthaben. Du hast **{fmt_amount(int(sender.get('balance', 0)), config)}**.")
                return
            sender["balance"] = int(sender["balance"]) - amount
            sender["spentTotal"] = int(sender.get("spentTotal", 0)) + amount
            receiver["balance"] = int(receiver["balance"]) + amount
            receiver["earnedTotal"] = int(receiver.get("earnedTotal", 0)) + amount
            await save_users(guild_id, users)
        await api.reply(ctx, f"{display_user(user_id)} sendet {display_user(target_id)} **{fmt_amount(amount, config)}**.")

    async def cmd_top(ctx: Any, guild_id: str, config: Dict[str, Any]) -> None:
        rows = await api_get_top(guild_id, int(config.get("leaderboardSize", 10)))
        if not rows:
            await api.reply(ctx, "Noch keine Konten in dieser Economy.")
            return
        lines = ["**Economy Richest**"]
        for idx, row in enumerate(rows, start=1):
            lines.append(f"**#{idx}** {display_user(row['userId'])} - **{fmt_amount(int(row['balance']), config)}**")
        await api.reply(ctx, "\n".join(lines))

    @api.command({
        "names": ["coins", "konto", "balance", "kontostand"],
        "description": "Zeigt dein Economy-Konto oder das Konto eines Users.",
        "level": "user",
        "options": [{"name": "user", "description": "Optionaler User.", "type": "user", "required": False}],
        "examples": ["!coins", "!coins @User"],
        "routing_when": "Use for economy balance, money account, Guthaben, Konto, or personal currency status.",
        "routing_not_when": "Do not use for Discord, OpenAI, or generic account settings."
    })
    async def konto_command(ctx, args):
        bundle = await user_context(ctx)
        if bundle is None:
            return
        guild_id, user_id, config = bundle
        parts = normalize_args(args)
        target_id = parse_user_id(ctx, parts[0]) if parts else user_id
        await cmd_balance(ctx, target_id or user_id, guild_id, config)

    @api.command({
        "names": ["shop"],
        "description": "Zeigt die kaufbaren Economy-Items.",
        "level": "user",
        "options": [],
        "examples": ["!shop"],
        "routing_when": "Use when the user wants to see the economy shop or buyable items.",
        "routing_not_when": "Do not use for XP or level leaderboards."
    })
    async def shop_command(ctx, args):
        bundle = await user_context(ctx)
        if bundle is None:
            return
        guild_id, _user_id, config = bundle
        await cmd_shop(ctx, guild_id, config)

    @api.command({
        "names": ["buy", "kauf"],
        "description": "Kauft ein Item aus dem Economy-Shop.",
        "level": "user",
        "options": [
            {"name": "item_id", "description": "Item-ID aus !shop.", "type": "string", "required": True},
            {"name": "amount", "description": "Optional amount.", "type": "integer", "required": False}
        ],
        "examples": ["!buy ticket", "!buy confetti 2"],
        "routing_when": "Use when the user wants to buy or purchase an economy shop item.",
        "routing_not_when": "Do not use for sending money to another member."
    })
    async def kauf_command(ctx, args):
        bundle = await user_context(ctx)
        if bundle is None:
            return
        guild_id, user_id, config = bundle
        await cmd_buy(ctx, normalize_args(args), guild_id, user_id, config)

    @api.command({
        "names": ["inventory", "inventar"],
        "description": "Zeigt dein Economy-Inventar oder das Inventar eines Users.",
        "level": "user",
        "options": [{"name": "user", "description": "Optionaler User.", "type": "user", "required": False}],
        "examples": ["!inventory", "!inventory @User"],
        "routing_when": "Use when the user asks for owned items, inventory, or bought economy items.",
        "routing_not_when": "Do not use for balance-only questions; use coins."
    })
    async def inventar_command(ctx, args):
        bundle = await user_context(ctx)
        if bundle is None:
            return
        guild_id, user_id, config = bundle
        parts = normalize_args(args)
        target_id = parse_user_id(ctx, parts[0]) if parts else user_id
        await cmd_inventory(ctx, target_id or user_id, guild_id, config)

    @api.command({
        "names": ["send", "senden", "pay", "transfer"],
        "description": "Sendet Economy-Geld an ein anderes Mitglied.",
        "level": "user",
        "options": [
            {"name": "user", "description": "Receiver.", "type": "user", "required": True},
            {"name": "amount", "description": "Amount.", "type": "integer", "required": True}
        ],
        "examples": ["!send @User 50"],
        "routing_when": "Use when the user wants to send, pay, transfer, or give economy money to another member.",
        "routing_not_when": "Do not use for admin-style level reward configuration."
    })
    async def senden_command(ctx, args):
        bundle = await user_context(ctx)
        if bundle is None:
            return
        guild_id, user_id, config = bundle
        await cmd_transfer(ctx, normalize_args(args), guild_id, user_id, config)

    @api.command({
        "names": ["richest", "top"],
        "description": "Zeigt die reichsten Economy-Konten.",
        "level": "user",
        "options": [],
        "examples": ["!richest"],
        "routing_when": "Use for the economy money leaderboard or richest users.",
        "routing_not_when": "Do not use for XP or level leaderboard requests; use levels."
    })
    async def top_command(ctx, args):
        bundle = await user_context(ctx)
        if bundle is None:
            return
        guild_id, _user_id, config = bundle
        await cmd_top(ctx, guild_id, config)

    def parse_bool(value: Any) -> Optional[bool]:
        lowered = str(value or "").strip().lower()
        if lowered in {"true", "1", "yes", "y", "on", "ja", "an"}:
            return True
        if lowered in {"false", "0", "no", "n", "off", "nein", "aus"}:
            return False
        return None

    async def admin_usage(ctx: Any) -> None:
        await api.reply(ctx, (
            "**Economy Admin**\n"
            "`!economy-admin inspect @user` - show account\n"
            "`!economy-admin add @user <amount> [reason]` - add coins\n"
            "`!economy-admin remove @user <amount> [reason]` - remove coins\n"
            "`!economy-admin set @user <amount> [reason]` - set balance\n"
            "`!economy-admin reset @user confirm` - reset account\n"
            "`!economy-admin shop-add <id> <price> <name...>` - create/update shop item\n"
            "`!economy-admin shop-remove <id>` - remove shop item\n"
            "`!economy-admin config [key value]` - show or edit config"
        ))

    async def admin_inspect(ctx: Any, parts: List[str], guild_id: str, config: Dict[str, Any]) -> None:
        if len(parts) < 2:
            await api.reply(ctx, "Usage: `!economy-admin inspect @user`")
            return
        target_id = parse_user_id(ctx, parts[1])
        if target_id is None:
            await api.reply(ctx, "User not found. Use a mention or user ID.")
            return
        users = await get_users(guild_id)
        user = ensure_user(users, target_id, config)
        await save_users(guild_id, users)
        inventory_count = sum(int(value) for value in user.get("inventory", {}).values())
        await api.reply(ctx, (
            f"**Economy Account: {display_user(target_id)}**\n"
            f"Balance: **{fmt_amount(int(user.get('balance', 0)), config)}**\n"
            f"Earned total: **{fmt_amount(int(user.get('earnedTotal', 0)), config)}**\n"
            f"Spent total: **{fmt_amount(int(user.get('spentTotal', 0)), config)}**\n"
            f"Inventory items: **{inventory_count}**"
        ))

    async def admin_money_change(ctx: Any, mode: str, parts: List[str], guild_id: str, config: Dict[str, Any]) -> None:
        if len(parts) < 3:
            await api.reply(ctx, f"Usage: `!economy-admin {mode} @user <amount> [reason]`")
            return
        target_id = parse_user_id(ctx, parts[1])
        amount = parse_positive_int(parts[2])
        if target_id is None or amount is None:
            await api.reply(ctx, "User or amount is invalid.")
            return
        reason = " ".join(parts[3:]).strip()[:160] if len(parts) > 3 else f"admin:{mode}"
        async with get_lock(guild_id):
            users = await get_users(guild_id)
            user = ensure_user(users, target_id, config)
            before = int(user.get("balance", 0))
            if mode in {"add", "give"}:
                after = before + amount
                user["earnedTotal"] = int(user.get("earnedTotal", 0)) + amount
            elif mode in {"remove", "take"}:
                after = max(0, before - amount)
                user["spentTotal"] = int(user.get("spentTotal", 0)) + min(before, amount)
            else:
                after = amount
            user["balance"] = after
            await save_users(guild_id, users)
        await api.reply(ctx, f"{display_user(target_id)}: **{fmt_amount(before, config)}** -> **{fmt_amount(after, config)}**. Reason: {reason}")

    async def admin_reset(ctx: Any, parts: List[str], guild_id: str, config: Dict[str, Any]) -> None:
        if len(parts) < 3 or parts[2].lower() != "confirm":
            await api.reply(ctx, "Usage: `!economy-admin reset @user confirm`")
            return
        target_id = parse_user_id(ctx, parts[1])
        if target_id is None:
            await api.reply(ctx, "User not found. Use a mention or user ID.")
            return
        async with get_lock(guild_id):
            users = await get_users(guild_id)
            users.pop(target_id, None)
            ensure_user(users, target_id, config)
            await save_users(guild_id, users)
        await api.reply(ctx, f"Account reset for {display_user(target_id)}.")

    async def admin_shop_add(ctx: Any, parts: List[str], guild_id: str) -> None:
        if len(parts) < 4:
            await api.reply(ctx, "Usage: `!economy-admin shop-add <id> <price> <name...>`")
            return
        item_id = re.sub(r"[^a-zA-Z0-9_-]", "", parts[1].lower())[:32]
        price = parse_positive_int(parts[2])
        name = " ".join(parts[3:]).strip()[:80]
        if not item_id or price is None or not name:
            await api.reply(ctx, "Item ID, price, or name is invalid.")
            return
        item = await api_set_shop_item(guild_id, item_id, {
            "name": name,
            "price": price,
            "description": "Admin-created economy item.",
        })
        await api.reply(ctx, f"Shop item `{item_id}` saved: **{item.get('name', item_id)}** for **{fmt_amount(price, await get_config(guild_id))}**.")

    async def admin_shop_remove(ctx: Any, parts: List[str], guild_id: str) -> None:
        if len(parts) < 2:
            await api.reply(ctx, "Usage: `!economy-admin shop-remove <id>`")
            return
        removed = await api_remove_shop_item(guild_id, parts[1])
        if not removed:
            await api.reply(ctx, "This shop item does not exist.")
            return
        await api.reply(ctx, f"Removed shop item `{parts[1].lower()}`.")

    async def admin_config(ctx: Any, parts: List[str], guild_id: str) -> None:
        config = await get_config(guild_id)
        if len(parts) == 1:
            lines = ["**Economy Config**"]
            for key in sorted(DEFAULT_CONFIG):
                lines.append(f"`{key}` = `{config.get(key)}`")
            await api.reply(ctx, "\n".join(lines))
            return
        if len(parts) < 3:
            await api.reply(ctx, "Usage: `!economy-admin config <key> <value>`")
            return
        key = parts[1]
        value = " ".join(parts[2:]).strip()
        if key not in DEFAULT_CONFIG:
            await api.reply(ctx, f"Unknown config key `{key}`. Allowed: {', '.join(sorted(DEFAULT_CONFIG))}")
            return
        new_config = dict(config)
        if isinstance(DEFAULT_CONFIG[key], bool):
            parsed = parse_bool(value)
            if parsed is None:
                await api.reply(ctx, "Use true/false, yes/no, on/off.")
                return
            new_config[key] = parsed
        elif isinstance(DEFAULT_CONFIG[key], int):
            parsed_int = parse_positive_int(value)
            if parsed_int is None and key != "startingBalance":
                await api.reply(ctx, "Use a positive whole number.")
                return
            new_config[key] = int(parsed_int if parsed_int is not None else 0)
        else:
            new_config[key] = value[:40]
        await save_config(guild_id, new_config)
        await api.reply(ctx, f"Config `{key}` set to `{sanitize_config(new_config).get(key)}`.")

    async def admin_audit(ctx: Any, guild_id: str, config: Dict[str, Any]) -> None:
        users = await get_users(guild_id)
        shop = await get_shop(guild_id)
        account_count = 0
        total_balance = 0
        richest_balance = 0
        inventory_items = 0
        for data in users.values():
            if not isinstance(data, dict):
                continue
            account_count += 1
            balance = int(data.get("balance", 0))
            total_balance += balance
            richest_balance = max(richest_balance, balance)
            inventory_items += sum(int(value) for value in data.get("inventory", {}).values())
        await api.reply(ctx, (
            "**Economy Audit**\n"
            f"Accounts: **{account_count}**\n"
            f"Total balance: **{fmt_amount(total_balance, config)}**\n"
            f"Richest balance: **{fmt_amount(richest_balance, config)}**\n"
            f"Shop items: **{len(shop)}**\n"
            f"Inventory item count: **{inventory_items}**"
        ))

    async def run_admin_command(ctx: Any, parts: List[str]) -> None:
        guild_id = get_guild_id(ctx)
        config = await get_config(guild_id)
        if not parts:
            await admin_usage(ctx)
            return
        action = parts[0].lower()
        if action in {"inspect", "info", "coins", "account"}:
            await admin_inspect(ctx, parts, guild_id, config)
        elif action in {"add", "give"}:
            await admin_money_change(ctx, "add", parts, guild_id, config)
        elif action in {"remove", "take"}:
            await admin_money_change(ctx, "remove", parts, guild_id, config)
        elif action == "set":
            await admin_money_change(ctx, "set", parts, guild_id, config)
        elif action == "reset":
            await admin_reset(ctx, parts, guild_id, config)
        elif action in {"shop-add", "shopadd"}:
            await admin_shop_add(ctx, parts, guild_id)
        elif action in {"shop-remove", "shopremove"}:
            await admin_shop_remove(ctx, parts, guild_id)
        elif action == "config":
            await admin_config(ctx, parts, guild_id)
        elif action == "audit":
            await admin_audit(ctx, guild_id, config)
        else:
            await admin_usage(ctx)

    @api.command({
        "names": ["economy-admin", "coins-admin", "geldadmin"],
        "description": "Admin tools for economy accounts, shop, and config.",
        "level": "admin",
        "slash": False,
        "examples": ["!economy-admin inspect @User", "!economy-admin add @User 100 event", "!economy-admin shop-add ticket 750 Golden Ticket"],
        "routing_when": "Use when an admin wants to inspect, edit, reset, or configure economy accounts or shop data.",
        "routing_not_when": "Do not use for normal user balance checks; use coins."
    })
    async def economy_admin_command(ctx, args):
        await run_admin_command(ctx, normalize_args(args))

    @api.command({
        "names": ["coins-inspect", "geldinspect"],
        "description": "Admin: inspect a user's economy account.",
        "level": "admin",
        "slash": False,
        "examples": ["!coins-inspect @User"],
        "routing_when": "Use when an admin wants to inspect one user's economy account.",
        "routing_not_when": "Do not use for public balance checks; use coins."
    })
    async def coins_inspect_command(ctx, args):
        await run_admin_command(ctx, ["inspect"] + normalize_args(args))

    @api.command({
        "names": ["coins-add", "coins-give", "geldadd"],
        "description": "Admin: add coins to a user's account.",
        "level": "admin",
        "slash": False,
        "examples": ["!coins-add @User 100 event"],
        "routing_when": "Use when an admin wants to add economy money to a user.",
        "routing_not_when": "Do not use for level reward configuration."
    })
    async def coins_add_command(ctx, args):
        await run_admin_command(ctx, ["add"] + normalize_args(args))

    @api.command({
        "names": ["coins-remove", "coins-take", "geldremove"],
        "description": "Admin: remove coins from a user's account.",
        "level": "admin",
        "slash": False,
        "examples": ["!coins-remove @User 50 correction"],
        "routing_when": "Use when an admin wants to remove economy money from a user.",
        "routing_not_when": "Do not use for user-to-user transfers; use send."
    })
    async def coins_remove_command(ctx, args):
        await run_admin_command(ctx, ["remove"] + normalize_args(args))

    @api.command({
        "names": ["coins-set", "geldset"],
        "description": "Admin: set a user's exact coin balance.",
        "level": "admin",
        "slash": False,
        "examples": ["!coins-set @User 500 correction"],
        "routing_when": "Use when an admin wants to set an exact economy balance.",
        "routing_not_when": "Do not use for adding a delta; use coins-add."
    })
    async def coins_set_command(ctx, args):
        await run_admin_command(ctx, ["set"] + normalize_args(args))

    @api.command({
        "names": ["coins-reset", "geldreset"],
        "description": "Admin: reset a user's economy account.",
        "level": "admin",
        "slash": False,
        "examples": ["!coins-reset @User confirm"],
        "routing_when": "Use when an admin wants to reset one economy account.",
        "routing_not_when": "Do not use for temporary balance corrections."
    })
    async def coins_reset_command(ctx, args):
        await run_admin_command(ctx, ["reset"] + normalize_args(args))

    @api.command({
        "names": ["shop-add", "geldshopadd"],
        "description": "Admin: create or update a shop item.",
        "level": "admin",
        "slash": False,
        "examples": ["!shop-add ticket 750 Golden Ticket"],
        "routing_when": "Use when an admin wants to create or update an economy shop item.",
        "routing_not_when": "Do not use for buying an item; use buy."
    })
    async def shop_add_command(ctx, args):
        await run_admin_command(ctx, ["shop-add"] + normalize_args(args))

    @api.command({
        "names": ["shop-remove", "geldshopremove"],
        "description": "Admin: remove a shop item.",
        "level": "admin",
        "slash": False,
        "examples": ["!shop-remove ticket"],
        "routing_when": "Use when an admin wants to remove an economy shop item.",
        "routing_not_when": "Do not use for removing coins from a user."
    })
    async def shop_remove_command(ctx, args):
        await run_admin_command(ctx, ["shop-remove"] + normalize_args(args))

    @api.command({
        "names": ["economy-config", "geldconfig"],
        "description": "Admin: show or edit economy config.",
        "level": "admin",
        "slash": False,
        "examples": ["!economy-config", "!economy-config currencyName Coins"],
        "routing_when": "Use when an admin wants to view or edit economy config.",
        "routing_not_when": "Do not use for level payout config; use level-money."
    })
    async def economy_config_command(ctx, args):
        await run_admin_command(ctx, ["config"] + normalize_args(args))

    @api.command({
        "names": ["economy-audit", "geldaudit"],
        "description": "Kinger: read-only audit of economy totals.",
        "level": "kinger",
        "slash": False,
        "examples": ["!economy-audit"],
        "routing_when": "Use when a kinger wants economy totals, account counts, or audit information.",
        "routing_not_when": "Do not use for public richest leaderboard; use richest."
    })
    async def economy_audit_command(ctx, args):
        guild_id = get_guild_id(ctx)
        await admin_audit(ctx, guild_id, await get_config(guild_id))

    async def api_get_balance(guild_id: Any, user_id: Any) -> int:
        gid = str(guild_id)
        uid = str(user_id)
        config = await get_config(gid)
        users = await get_users(gid)
        user = ensure_user(users, uid, config)
        await save_users(gid, users)
        return int(user.get("balance", 0))

    async def api_adjust_balance(guild_id: Any, user_id: Any, delta: Any, reason: str = "plugin-api") -> Dict[str, Any]:
        gid = str(guild_id)
        uid = str(user_id)
        amount = int(delta)
        config = await get_config(gid)
        async with get_lock(gid):
            users = await get_users(gid)
            user = ensure_user(users, uid, config)
            before = int(user.get("balance", 0))
            after = max(0, before + amount)
            user["balance"] = after
            if amount > 0:
                user["earnedTotal"] = int(user.get("earnedTotal", 0)) + amount
            elif amount < 0:
                user["spentTotal"] = int(user.get("spentTotal", 0)) + min(before, abs(amount))
            await save_users(gid, users)
        payload = {
            "guildId": gid,
            "userId": uid,
            "before": before,
            "after": after,
            "delta": after - before,
            "reason": str(reason)[:160]
        }
        try:
            await api.emit("economy.balance_changed", payload)
        except Exception:
            pass
        return payload

    async def api_set_balance(guild_id: Any, user_id: Any, amount: Any, reason: str = "plugin-api") -> Dict[str, Any]:
        gid = str(guild_id)
        uid = str(user_id)
        target = max(0, int(amount))
        config = await get_config(gid)
        async with get_lock(gid):
            users = await get_users(gid)
            user = ensure_user(users, uid, config)
            before = int(user.get("balance", 0))
            user["balance"] = target
            await save_users(gid, users)
        payload = {
            "guildId": gid,
            "userId": uid,
            "before": before,
            "after": target,
            "delta": target - before,
            "reason": str(reason)[:160],
        }
        try:
            await api.emit("economy.balance_changed", payload)
        except Exception:
            pass
        return payload

    async def api_transfer(guild_id: Any, from_user_id: Any, to_user_id: Any, amount: Any, reason: str = "plugin-api") -> Dict[str, Any]:
        gid = str(guild_id)
        sender_id = str(from_user_id)
        receiver_id = str(to_user_id)
        value = max(0, int(amount))
        if not value or sender_id == receiver_id:
            return {"guildId": gid, "fromUserId": sender_id, "toUserId": receiver_id, "amount": 0, "ok": False}
        config = await get_config(gid)
        async with get_lock(gid):
            users = await get_users(gid)
            sender = ensure_user(users, sender_id, config)
            receiver = ensure_user(users, receiver_id, config)
            sender_before = int(sender.get("balance", 0))
            receiver_before = int(receiver.get("balance", 0))
            if sender_before < value:
                return {
                    "guildId": gid,
                    "fromUserId": sender_id,
                    "toUserId": receiver_id,
                    "amount": value,
                    "ok": False,
                    "reason": "insufficient-funds",
                }
            sender["balance"] = sender_before - value
            sender["spentTotal"] = int(sender.get("spentTotal", 0)) + value
            receiver["balance"] = receiver_before + value
            receiver["earnedTotal"] = int(receiver.get("earnedTotal", 0)) + value
            await save_users(gid, users)
        payload = {
            "guildId": gid,
            "fromUserId": sender_id,
            "toUserId": receiver_id,
            "amount": value,
            "senderBefore": sender_before,
            "senderAfter": sender_before - value,
            "receiverBefore": receiver_before,
            "receiverAfter": receiver_before + value,
            "reason": str(reason)[:160],
            "ok": True,
        }
        try:
            await api.emit("economy.transfer", payload)
        except Exception:
            pass
        return payload

    async def api_credit(payload: Any = None, **kwargs: Any) -> Dict[str, Any]:
        data = dict(payload) if isinstance(payload, dict) else {}
        data.update(kwargs)
        return await api_adjust_balance(
            data.get("guild_id", data.get("guildId")),
            data.get("user_id", data.get("userId")),
            data.get("amount", data.get("delta", 0)),
            data.get("reason", "economy.credit"),
        )

    async def api_get_inventory(guild_id: Any, user_id: Any) -> Dict[str, Any]:
        gid = str(guild_id)
        uid = str(user_id)
        config = await get_config(gid)
        users = await get_users(gid)
        user = ensure_user(users, uid, config)
        await save_users(gid, users)
        return dict(user.get("inventory", {}))

    async def api_get_top(guild_id: Any, limit: int = 10) -> List[Dict[str, Any]]:
        gid = str(guild_id)
        users = await get_users(gid)
        rows: List[Tuple[str, int]] = []
        for uid, data in users.items():
            if isinstance(data, dict):
                rows.append((str(uid), int(data.get("balance", 0))))
        rows.sort(key=lambda row: row[1], reverse=True)
        safe_limit = min(25, max(1, int(limit)))
        return [{"userId": uid, "balance": balance} for uid, balance in rows[:safe_limit]]

    async def api_add_inventory(guild_id: Any, user_id: Any, item_id: str, quantity: Any = 1) -> Dict[str, Any]:
        gid = str(guild_id)
        uid = str(user_id)
        clean_item = re.sub(r"[^a-zA-Z0-9_-]", "", str(item_id).lower())[:32]
        qty = int(quantity)
        if not clean_item or qty == 0:
            return {"guildId": gid, "userId": uid, "itemId": clean_item, "quantity": 0}
        config = await get_config(gid)
        async with get_lock(gid):
            users = await get_users(gid)
            user = ensure_user(users, uid, config)
            inventory = user.setdefault("inventory", {})
            inventory[clean_item] = max(0, int(inventory.get(clean_item, 0)) + qty)
            if inventory[clean_item] == 0:
                inventory.pop(clean_item, None)
        await save_users(gid, users)
        return {"guildId": gid, "userId": uid, "itemId": clean_item, "quantity": qty}

    async def api_get_config(guild_id: Any) -> Dict[str, Any]:
        return dict(await get_config(str(guild_id)))

    async def api_set_config(guild_id: Any, values: Dict[str, Any]) -> Dict[str, Any]:
        gid = str(guild_id)
        config = await get_config(gid)
        if isinstance(values, dict):
            config.update(values)
        await save_config(gid, config)
        return dict(await get_config(gid))

    async def api_set_shop_item(guild_id: Any, item_id: str, item: Dict[str, Any]) -> Dict[str, Any]:
        gid = str(guild_id)
        clean_item = re.sub(r"[^a-zA-Z0-9_-]", "", str(item_id).lower())[:32]
        if not clean_item:
            return {}
        shop = await get_shop(gid)
        shop[clean_item] = {
            "name": str(item.get("name", clean_item))[:80],
            "price": max(0, int(item.get("price", 0))),
            "description": str(item.get("description", ""))[:160],
        }
        await save_shop(gid, shop)
        return shop[clean_item]

    async def api_remove_shop_item(guild_id: Any, item_id: str) -> bool:
        gid = str(guild_id)
        clean_item = re.sub(r"[^a-zA-Z0-9_-]", "", str(item_id).lower())[:32]
        if not clean_item:
            return False
        shop = await get_shop(gid)
        if clean_item not in shop:
            return False
        shop.pop(clean_item, None)
        await save_shop(gid, shop)
        return True

    @api.on("economy.credit")
    async def on_economy_credit(payload=None):
        return await api_credit(payload or {})

    shared = getattr(api, "shared", None)
    if isinstance(shared, dict):
        economy_api = {
            "get_balance": api_get_balance,
            "adjust_balance": api_adjust_balance,
            "set_balance": api_set_balance,
            "credit": api_credit,
            "add_money": api_credit,
            "transfer": api_transfer,
            "get_inventory": api_get_inventory,
            "get_top": api_get_top,
            "add_inventory": api_add_inventory,
            "get_config": api_get_config,
            "set_config": api_set_config,
            "get_shop": get_shop,
            "set_shop_item": api_set_shop_item,
            "remove_shop_item": api_remove_shop_item,
        }
        shared["economy.api"] = economy_api
        shared["economy"] = economy_api
