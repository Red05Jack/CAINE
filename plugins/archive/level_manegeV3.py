import asyncio
import datetime as _dt
import hashlib
import io
import json
import math
import re
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import discord
except Exception:  # pragma: no cover
    discord = None

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
except Exception:  # pragma: no cover
    Image = None
    ImageDraw = None
    ImageFont = None
    ImageFilter = None

PLUGIN = {
    'name': 'Level-Manege',
    'description': 'XP-, Level- und Rankkarten-Apparat: /rank mit HTML-inspirierter Karte, !levels Top-10-Rangliste, Rank-Farbe, manuelle XP und Recalculate-Werkzeuge.'
}

_STORAGE_KEY = 'level_manege_state_v1'
_DEFAULT_COLOR = '#009FA1'
_OLD_DEFAULT_COLOR = '#FFFFFF'
_HEX_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')
_MENTION_RE = re.compile(r'<@!?(\d+)>')
_ID_RE = re.compile(r'^\d{15,25}$')
_MESSAGE_XP_MIN = 15
_MESSAGE_XP_MAX = 25
_COMMAND_PREFIXES = ('!', '/')
_COOLDOWNS: Dict[Tuple[str, int, int], float] = {}
_STATE_LOCK = asyncio.Lock()

# Die gelieferte rank.html bleibt als Referenz im Plugin. Discord kann HTML nicht
# direkt als Bild verschicken; deshalb zeichnet _make_rank_card_png dieselbe Karte
# mit Pillow als transparentes PNG nach: 980x170 Balken, Avatar links herausragend,
# dunkler Strip, Rang/Level rechts, XP-Fortschritt links.
RANK_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Rank Card</title>

  <style>
    * {{
      box-sizing: border-box;
    }}

    body {{
      min-height: 100vh;
      margin: 0;
      background: transparent;
      display: grid;
      place-items: center;
      font-family: Arial, Helvetica, sans-serif;
    }}

    .rank-card {{
      --progress: {progress_percent}%;

      position: relative;
      width: 980px;
      height: 170px;
      overflow: visible;
    }}

    .card-mask {{
      position: absolute;
      inset: 0;
      border: 4px solid {border_color};
      border-left: none;
      border-radius: 0 86px 86px 0;
      background: #05070c;
      overflow: hidden;
    }}

    .bar-content {{
      position: absolute;
      left: 0;
      top: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      z-index: 1;
    }}

    .bar {{
      position: absolute;
      left: 0;
      top: -100%;
      width: var(--progress);
      height: 300%;

      background: {bar_color};
      filter: blur(26px);
    }}

    .readability-strip {{
      position: absolute;
      left: 72px;
      right: 24px;
      top: 21px;
      height: 122px;
      border-radius: 0 64px 64px 0;
      background: rgba(0, 0, 0, 0.5);
      z-index: 3;
    }}

    .avatar-wrap {{
      position: absolute;
      left: -78px;
      top: 50%;
      width: 180px;
      height: 180px;
      transform: translateY(-50%);
      border-radius: 50%;
      overflow: hidden;
      background: #111;
      z-index: 10;
    }}

    .avatar {{
      width: 100%;
      height: 100%;
      background-image: url("{avatar_url}");
      background-size: cover;
      background-position: center;
      background-repeat: no-repeat;
    }}

    .player-name {{
      position: absolute;
      left: 135px;
      top: 38px;
      font-size: 42px;
      font-weight: 700;
      letter-spacing: 1px;
      line-height: 1;
      color: #ffffff;
      z-index: 4;
    }}

    .xp-text {{
      position: absolute;
      left: 135px;
      top: 98px;
      font-size: 32px;
      font-weight: 400;
      letter-spacing: 1px;
      line-height: 1;
      color: rgba(255, 255, 255, 0.55);
      z-index: 4;
    }}

.rank-info {{
  position: absolute;
  right: 58px;
  top: 50%;
  transform: translateY(-50%);

  display: flex;
  align-items: baseline;
  gap: 8px;
  white-space: nowrap;
  line-height: 1;

  z-index: 4;
}}

    .rank-label {{
      font-size: 30px;
      font-weight: 400;
      letter-spacing: 2px;
      text-transform: uppercase;
      color: rgba(255, 255, 255, 0.6);
    }}

    .rank-value,
    .level-value {{
      font-size: 44px;
      font-weight: 700;
      color: #ffffff;
    }}
  </style>
</head>

<body>

  <div class="rank-card">
    <div class="card-mask">

      <div class="bar-content">
        <div class="bar"></div>
      </div>

      <div class="readability-strip"></div>

      <div class="player-name">{username}</div>
      <div class="xp-text">{xp_text}</div>

      <div class="rank-info">
        <span class="rank-label">RANG</span>
        <span class="rank-value">#{rank}</span>
        <span class="rank-label">LEVEL</span>
        <span class="level-value">{level}</span>
      </div>
    </div>

    <div class="avatar-wrap">
      <div class="avatar"></div>
    </div>
  </div>

</body>
</html>'''


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _empty_state() -> Dict[str, Any]:
    return {'version': 1, 'guilds': {}}


def _guild_id_from_ctx(ctx: Any) -> Optional[int]:
    guild = getattr(ctx, 'guild', None)
    if guild is None and hasattr(ctx, 'message'):
        guild = getattr(ctx.message, 'guild', None)
    gid = getattr(guild, 'id', None)
    return int(gid) if gid is not None else None


def _guild_from_ctx(ctx: Any) -> Any:
    guild = getattr(ctx, 'guild', None)
    if guild is None and hasattr(ctx, 'message'):
        guild = getattr(ctx.message, 'guild', None)
    return guild


def _author_from_ctx(ctx: Any) -> Any:
    author = getattr(ctx, 'author', None)
    if author is None and hasattr(ctx, 'user'):
        author = getattr(ctx, 'user', None)
    if author is None and hasattr(ctx, 'message'):
        author = getattr(ctx.message, 'author', None)
    return author


def _channel_from_ctx(ctx: Any) -> Any:
    channel = getattr(ctx, 'channel', None)
    if channel is None and hasattr(ctx, 'message'):
        channel = getattr(ctx.message, 'channel', None)
    return channel


async def _storage_get(api: Any) -> Dict[str, Any]:
    try:
        value = await api.storage_get(_STORAGE_KEY)
    except TypeError:
        value = await api.storage_get(_STORAGE_KEY, None)
    if not isinstance(value, dict):
        return _empty_state()
    value.setdefault('version', 1)
    value.setdefault('guilds', {})
    return value


async def _storage_set(api: Any, state: Dict[str, Any]) -> None:
    await api.storage_set(_STORAGE_KEY, state)


def _ensure_guild(state: Dict[str, Any], guild_id: int) -> Dict[str, Any]:
    gid = str(guild_id)
    guild_state = state.setdefault('guilds', {}).setdefault(gid, {})
    guild_state.setdefault('users', {})
    guild_state.setdefault('message_xp', {})
    guild_state.setdefault('ledger', [])
    guild_state.setdefault('settings', {
        'messageXpMin': _MESSAGE_XP_MIN,
        'messageXpMax': _MESSAGE_XP_MAX,
        'levelUpChannelId': 0,
        'rewardBots': False
    })
    return guild_state


def _ensure_user(guild_state: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    uid = str(user_id)
    user = guild_state.setdefault('users', {}).setdefault(uid, {})
    user.setdefault('message_xp', 0)
    user.setdefault('voice_xp', 0)
    user.setdefault('invite_xp', 0)
    user.setdefault('manual_xp', 0)
    user.setdefault('message_count', 0)
    user.setdefault('rank_color', _DEFAULT_COLOR)
    user.setdefault('last_seen_name', 'Unbekannt')
    user.setdefault('updated_at', _now_iso())
    return user


def _total_xp(user: Dict[str, Any]) -> int:
    total = int(user.get('message_xp', 0)) + int(user.get('voice_xp', 0)) + int(user.get('invite_xp', 0)) + int(user.get('manual_xp', 0))
    return max(0, total)


def xp_for_next_level(current_level: int) -> int:
    return int(round(20.0 * math.pow(current_level + 1, 1.9)))


def level_from_xp(total_xp: int) -> Tuple[int, int, int]:
    remaining = max(0, int(total_xp))
    level = 0
    while True:
        cost = xp_for_next_level(level)
        if remaining < cost:
            return level, remaining, cost
        remaining -= cost
        level += 1


def compact_number(value: int) -> str:
    value = int(value)
    if abs(value) >= 1_000_000:
        return (f'{value / 1_000_000:.2f}'.rstrip('0').rstrip('.') + 'M')
    if abs(value) >= 1_000:
        return (f'{value / 1_000:.2f}'.rstrip('0').rstrip('.') + 'K')
    return f'{value:,}'.replace(',', '.')


def calculate_message_xp(message_id: int, min_xp: int = _MESSAGE_XP_MIN, max_xp: int = _MESSAGE_XP_MAX) -> int:
    mask = (1 << 64) - 1
    value = (int(message_id) + 0x9E3779B97F4A7C15) & mask
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & mask
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & mask
    value = (value ^ (value >> 31)) & mask
    return int(min_xp) + int(value % (int(max_xp) - int(min_xp) + 1))


def _parse_user_id(tokens: List[str]) -> Optional[int]:
    if not tokens:
        return None
    token = str(tokens[0]).strip()
    match = _MENTION_RE.match(token)
    if match:
        return int(match.group(1))
    if _ID_RE.match(token):
        return int(token)
    return None


def _is_debug_token(token: str) -> bool:
    return str(token).lower() in {'debug', '--debug', '-d', 'true', 'yes', 'ja', '1'}


def _normalize_args(args: Any) -> List[str]:
    if not args:
        return []
    if isinstance(args, str):
        return [part for part in args.strip().split() if part]
    return [str(part) for part in args if str(part).strip()]


def _member_has_admin(member: Any) -> bool:
    perms = getattr(member, 'guild_permissions', None)
    return bool(getattr(perms, 'administrator', False) or getattr(perms, 'manage_guild', False))


def _member_has_kinger(member: Any) -> bool:
    roles = getattr(member, 'roles', []) or []
    for role in roles:
        name = str(getattr(role, 'name', '')).lower()
        if name in {'kinger', 'bot-master', 'botmaster', 'xp-master'}:
            return True
    return _member_has_admin(member)


def _clean_command_name(content: str) -> str:
    if not content:
        return ''
    first = content.strip().split(maxsplit=1)[0].lower()
    if first.startswith(_COMMAND_PREFIXES):
        first = first[1:]
    return first


def _is_xp_excluded_command(content: str) -> bool:
    name = _clean_command_name(content)
    return name in {
        'help', 'levels', 'leaderboard', 'rangliste',
        'rank', 'set-rank-color', 'set-rank-colour', 'remove-xp', 'removexp',
        'give-xp', 'givexp', 'importdb', 'recalculate', 'xp-liste',
        'einladungen-nachbearbeiten'
    }


def _apply_manual_xp(guild_state: Dict[str, Any], guild_id: int, user_id: int, amount: int, reason: str, reference_id: str, actor_id: int) -> Dict[str, Any]:
    user = _ensure_user(guild_state, user_id)
    old_total = _total_xp(user)
    old_level, _, _ = level_from_xp(old_total)
    user['manual_xp'] = int(user.get('manual_xp', 0)) + int(amount)
    user['updated_at'] = _now_iso()
    new_total = _total_xp(user)
    new_level, progress, needed = level_from_xp(new_total)
    movement = {
        'id': hashlib.sha256(f'{guild_id}:{user_id}:{reference_id}'.encode('utf-8')).hexdigest(),
        'guild_id': str(guild_id),
        'user_id': str(user_id),
        'amount': int(amount),
        'reason': reason,
        'reference_id': reference_id,
        'actor_id': str(actor_id),
        'created_at_utc': _now_iso(),
        'old_xp': old_total,
        'new_xp': new_total,
        'old_level': old_level,
        'new_level': new_level,
        'progress': progress,
        'needed': needed,
        'applied': True
    }
    guild_state.setdefault('ledger', []).append(movement)
    guild_state['ledger'] = guild_state['ledger'][-500:]
    return movement


def _ranked_users(guild_state: Dict[str, Any], guild: Any = None) -> List[Tuple[int, Dict[str, Any], int]]:
    users = []
    for uid_str, user in guild_state.get('users', {}).items():
        try:
            uid = int(uid_str)
        except Exception:
            continue
        if guild is not None and hasattr(guild, 'get_member'):
            member = guild.get_member(uid)
            if member is not None and getattr(member, 'bot', False):
                continue
        total = _total_xp(user)
        users.append((uid, user, total))
    users.sort(key=lambda row: (-row[2], row[0]))
    return users


def _rank_for_user(guild_state: Dict[str, Any], user_id: int, guild: Any = None) -> int:
    ranked = _ranked_users(guild_state, guild)
    for idx, (uid, _user, _total) in enumerate(ranked, start=1):
        if uid == int(user_id):
            return idx
    return max(1, len(ranked) + 1)


async def _resolve_member(ctx: Any, user_id: int) -> Any:
    guild = _guild_from_ctx(ctx)
    if guild is None:
        return None
    member = None
    if hasattr(guild, 'get_member'):
        member = guild.get_member(int(user_id))
    if member is None and hasattr(guild, 'fetch_member'):
        try:
            member = await guild.fetch_member(int(user_id))
        except Exception:
            member = None
    return member


def _font(size: int, bold: bool = False) -> Any:
    if ImageFont is None:
        return None
    candidates = []
    if bold:
        candidates.extend([
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf',
            'arialbd.ttf'
        ])
    candidates.extend([
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/dejavu/DejaVuSans.ttf',
        'arial.ttf'
    ])
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    hex_color = str(hex_color or _DEFAULT_COLOR).upper()
    if not _HEX_RE.match(hex_color):
        hex_color = _DEFAULT_COLOR
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


def _rounded_right_rectangle(draw: Any, xy: Tuple[int, int, int, int], radius: int, fill: Any = None, outline: Any = None, width: int = 1) -> None:
    try:
        x1, y1, x2, y2 = [int(round(v)) for v in xy]
    except Exception:
        return
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    rect_w = x2 - x1
    rect_h = y2 - y1
    if rect_w <= 0 or rect_h <= 0:
        return
    radius = max(0, min(int(radius), rect_w // 2, rect_h // 2))
    if radius <= 0:
        try:
            draw.rectangle((x1, y1, x2, y2), fill=fill, outline=outline, width=max(1, int(width)))
        except TypeError:
            draw.rectangle((x1, y1, x2, y2), fill=fill, outline=outline)
        return
    if fill is not None:
        draw.rectangle((x1, y1, x2 - radius, y2), fill=fill)
        mid_top = y1 + radius
        mid_bottom = y2 - radius
        if mid_bottom >= mid_top:
            draw.rectangle((x2 - radius, mid_top, x2, mid_bottom), fill=fill)
        draw.pieslice((x2 - 2 * radius, y1, x2, y1 + 2 * radius), 270, 360, fill=fill)
        draw.pieslice((x2 - 2 * radius, y2 - 2 * radius, x2, y2), 0, 90, fill=fill)
    if outline is not None and width > 0:
        ow = max(1, int(width))
        try:
            draw.line((x1, y1, x2 - radius, y1), fill=outline, width=ow)
            draw.arc((x2 - 2 * radius, y1, x2, y1 + 2 * radius), 270, 360, fill=outline, width=ow)
            if y2 - radius >= y1 + radius:
                draw.line((x2, y1 + radius, x2, y2 - radius), fill=outline, width=ow)
            draw.arc((x2 - 2 * radius, y2 - 2 * radius, x2, y2), 0, 90, fill=outline, width=ow)
            draw.line((x1, y2, x2 - radius, y2), fill=outline, width=ow)
        except TypeError:
            draw.line((x1, y1, x2 - radius, y1), fill=outline)
            draw.arc((x2 - 2 * radius, y1, x2, y1 + 2 * radius), 270, 360, fill=outline)
            if y2 - radius >= y1 + radius:
                draw.line((x2, y1 + radius, x2, y2 - radius), fill=outline)
            draw.arc((x2 - 2 * radius, y2 - 2 * radius, x2, y2), 0, 90, fill=outline)
            draw.line((x1, y2, x2 - radius, y2), fill=outline)


async def _fetch_avatar_bytes(member: Any) -> Optional[bytes]:
    if member is None or discord is None:
        return None
    try:
        asset = member.display_avatar.replace(format='png', size=256)
        return await asset.read()
    except Exception:
        try:
            asset = member.avatar.replace(format='png', size=256)
            return await asset.read()
        except Exception:
            return None


def _fit_text(draw: Any, text: str, font: Any, max_width: int) -> str:
    if not text:
        return 'Unbekannt'
    text = str(text)

    def width_of(t: str) -> int:
        try:
            box = draw.textbbox((0, 0), t, font=font)
            return box[2] - box[0]
        except Exception:
            return len(t) * 20

    if width_of(text) <= max_width:
        return text
    ellipsis = '...'
    while text and width_of(text + ellipsis) > max_width:
        text = text[:-1]
    return text + ellipsis if text else ellipsis


def _make_rank_card_png(username: str, rank: int, level: int, progress: int, needed: int, total_xp: int, color: str, avatar_bytes: Optional[bytes]) -> bytes:
    if Image is None or ImageDraw is None:
        return bytes.fromhex('89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4890000000A49444154789C6360000002000100FFFF03000006000557BFAB0000000049454E44AE426082')

    # HTML layout: rank-card 980x170, avatar 78px protruding left, no body bg.
    W, H = 1094, 180
    card_x, card_y, card_w, card_h = 78, 5, 980, 170
    accent = _hex_to_rgb(color)
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))

    card_mask = Image.new('L', (card_w, card_h), 0)
    mask_draw = ImageDraw.Draw(card_mask)
    _rounded_right_rectangle(mask_draw, (0, 0, card_w - 1, card_h - 1), 86, fill=255)

    card_layer = Image.new('RGBA', (card_w, card_h), (0, 0, 0, 0))
    card_draw = ImageDraw.Draw(card_layer)
    _rounded_right_rectangle(card_draw, (0, 0, card_w - 1, card_h - 1), 86, fill=(5, 7, 12, 255))

    ratio = 0.0 if needed <= 0 else max(0.0, min(1.0, progress / float(needed)))
    glow_w = int(round(card_w * ratio))
    glow = Image.new('RGBA', (card_w, card_h * 3), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    if glow_w > 0:
        gd.rectangle((0, 0, glow_w, card_h * 3), fill=(accent[0], accent[1], accent[2], 230))
    if ImageFilter is not None:
        glow = glow.filter(ImageFilter.GaussianBlur(26))
    card_layer.alpha_composite(glow.crop((0, card_h, card_w, card_h * 2)))

    strip_layer = Image.new('RGBA', (card_w, card_h), (0, 0, 0, 0))
    strip_draw = ImageDraw.Draw(strip_layer)
    _rounded_right_rectangle(strip_draw, (72, 21, card_w - 24, 143), 64, fill=(0, 0, 0, 128))
    card_layer.alpha_composite(strip_layer)

    img.paste(card_layer, (card_x, card_y), card_mask)

    draw = ImageDraw.Draw(img)
    _rounded_right_rectangle(draw, (card_x, card_y, card_x + card_w - 1, card_y + card_h - 1), 86, fill=None, outline=(accent[0], accent[1], accent[2], 255), width=4)

    av_x, av_y, av_s = 0, 0, 180
    avatar = Image.new('RGBA', (av_s, av_s), (17, 17, 17, 255))
    if avatar_bytes:
        try:
            raw = Image.open(io.BytesIO(avatar_bytes)).convert('RGBA')
            side = min(raw.size)
            left = (raw.width - side) // 2
            top = (raw.height - side) // 2
            raw = raw.crop((left, top, left + side, top + side)).resize((av_s, av_s), Image.LANCZOS)
            avatar.alpha_composite(raw)
        except Exception:
            pass
    else:
        ad = ImageDraw.Draw(avatar)
        for y in range(av_s):
            shade = 17 + int(y / av_s * 26)
            ad.line((0, y, av_s, y), fill=(shade, shade, shade, 255))
        initial = (str(username)[:1] or '?').upper()
        f_initial = _font(76, True)
        try:
            box = ad.textbbox((0, 0), initial, font=f_initial)
            ad.text(((av_s - (box[2] - box[0])) / 2, (av_s - (box[3] - box[1])) / 2 - 8), initial, font=f_initial, fill=(255, 255, 255, 210))
        except Exception:
            ad.text((70, 55), initial, fill=(255, 255, 255, 210))
    mask = Image.new('L', (av_s, av_s), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse((0, 0, av_s, av_s), fill=255)
    try:
        img.paste(avatar, (av_x, av_y), mask)
    except Exception:
        img.alpha_composite(avatar, (av_x, av_y))

    draw = ImageDraw.Draw(img)
    f_name = _font(42, True)
    f_xp = _font(32, False)
    f_label = _font(30, False)
    f_value = _font(44, True)
    safe_name = _fit_text(draw, username, f_name, 430)
    draw.text((card_x + 135, card_y + 33), safe_name, font=f_name, fill=(255, 255, 255, 255))
    missing_xp = max(0, int(needed) - int(progress))
    xp_text = f'Noch {compact_number(missing_xp)} XP'
    draw.text((card_x + 135, card_y + 93), xp_text, font=f_xp, fill=(255, 255, 255, 145))

    parts = [('RANG', f_label, (255, 255, 255, 155)), (f'#{rank}', f_value, (255, 255, 255, 255)), ('LEVEL', f_label, (255, 255, 255, 155)), (str(level), f_value, (255, 255, 255, 255))]
    widths = []
    for text, font, _fill in parts:
        try:
            b = draw.textbbox((0, 0), text, font=font)
            widths.append(b[2] - b[0])
        except Exception:
            widths.append(len(text) * 20)
    total_w = sum(widths) + 8 * (len(parts) - 1)
    cur_x = card_x + card_w - 58 - total_w
    for idx, (text, font, fill) in enumerate(parts):
        y = card_y + 72 if font == f_label else card_y + 61
        draw.text((cur_x, y), text, font=font, fill=fill)
        cur_x += widths[idx] + 8

    out = io.BytesIO()
    img.save(out, format='PNG')
    return out.getvalue()


async def _send_rank_card(ctx: Any, api: Any, png_bytes: bytes) -> None:
    channel = _channel_from_ctx(ctx)
    if discord is not None and channel is not None and hasattr(channel, 'send'):
        try:
            file = discord.File(io.BytesIO(png_bytes), filename='rank.png')
            await channel.send(file=file)
            return
        except Exception:
            pass
    await api.reply(ctx, 'Die Rank-Karte wurde gezeichnet, aber ich konnte keine Datei senden. Prüfe bitte meine Kanalrechte: View Channel, Send Messages und Attach Files.')


async def _maybe_send_levelup(api: Any, ctx_or_message: Any, user_id: int, movement: Dict[str, Any]) -> None:
    if not movement.get('applied') or int(movement.get('new_level', 0)) <= int(movement.get('old_level', 0)):
        return
    channel = _channel_from_ctx(ctx_or_message)
    new_level = int(movement.get('new_level', 0))
    total = int(movement.get('new_xp', 0))
    needed = int(movement.get('needed', 0)) - int(movement.get('progress', 0))
    title = 'Der Frischgelevelte'
    if new_level >= 100:
        title = 'Die Legende'
    elif new_level >= 75:
        title = 'Der Unaufhaltsame'
    elif new_level >= 50:
        title = 'Der Champion'
    elif new_level >= 25:
        title = 'Der Veteran'
    elif new_level >= 10:
        title = 'Der Wortgewandte'
    elif new_level >= 5:
        title = 'Der Aufsteiger'
    text = f'Endlich, <@{user_id}> - {title} - hat Level **{new_level}** erreicht!\nGesamt-XP: **{total}** | Bis Level {new_level + 1}: **{max(0, needed)} XP**'
    try:
        if channel is not None and hasattr(channel, 'send'):
            await channel.send(text)
        else:
            await api.send(ctx_or_message, text)
    except Exception:
        try:
            print('[LevelUpSend] Konnte Level-Up nicht senden. Benötigt: View Channel und Send Messages.')
        except Exception:
            pass


def _cooldown_ok(command: str, guild_id: int, user_id: int, seconds: int) -> Tuple[bool, int]:
    now = time.monotonic()
    key = (command, int(guild_id), int(user_id))
    until = _COOLDOWNS.get(key, 0.0)
    if until > now:
        return False, int(math.ceil(until - now))
    _COOLDOWNS[key] = now + seconds
    return True, 0


def _display_name_for(guild: Any, uid: int, user: Dict[str, Any]) -> str:
    member = guild.get_member(uid) if guild is not None and hasattr(guild, 'get_member') else None
    if member is not None:
        return str(getattr(member, 'display_name', getattr(member, 'name', uid)))
    return str(user.get('last_seen_name') or uid)


async def setup_plugin(api):
    @api.command({
        'names': ['levels', 'leaderboard', 'rangliste'],
        'description': 'Zeigt die höchsten 10 Plätze der Server-Rangliste.',
        'level': 'user',
        'options': []
    })
    async def levels_command(ctx, args):
        guild_id = _guild_id_from_ctx(ctx)
        guild = _guild_from_ctx(ctx)
        author = _author_from_ctx(ctx)
        if guild_id is None or author is None:
            await api.reply(ctx, 'Die Rangliste braucht eine Server-Manege. In DMs jongliert hier leider nur Nebel.')
            return
        ok, wait = _cooldown_ok('levels', guild_id, int(author.id), 10)
        if not ok:
            await api.reply(ctx, f'Die Ranglisten-Trommel wirbelt noch aus. Warte {wait}s.')
            return
        async with _STATE_LOCK:
            state = await _storage_get(api)
            gs = _ensure_guild(state, guild_id)
            ranked = _ranked_users(gs, guild)
        if not ranked:
            await api.reply(ctx, 'Noch keine XP in dieser Manege! Schreib ein paar Nachrichten, dann klettert die Rangliste aus der Kiste.')
            return
        top = ranked[:10]
        lines = ['**🏆 Level-Manege Rangliste — Top 10**']
        for idx, (uid, user, total) in enumerate(top, start=1):
            level, progress, needed = level_from_xp(total)
            name = _display_name_for(guild, uid, user)
            medal = '🥇' if idx == 1 else '🥈' if idx == 2 else '🥉' if idx == 3 else '🎪'
            lines.append(f'{medal} **#{idx}** `{name}` — Level **{level}** — **{compact_number(total)} XP** ({compact_number(progress)}/{compact_number(needed)})')
        caller_rank = None
        for idx, (uid, _user, _total) in enumerate(ranked, start=1):
            if uid == int(author.id):
                caller_rank = idx
                break
        if caller_rank is not None and caller_rank > 10:
            caller_user = ranked[caller_rank - 1][1]
            caller_total = ranked[caller_rank - 1][2]
            caller_level, _p, _n = level_from_xp(caller_total)
            lines.append(f'\nDein Platz in der Manege: **#{caller_rank}** — Level **{caller_level}** — **{compact_number(caller_total)} XP**')
        lines.append('\nTipp: Mit `!rank` erscheint deine persönliche Rankkarte im Manegenlicht.')
        await api.reply(ctx, '\n'.join(lines))

    @api.command({
        'names': ['rank'],
        'description': 'Zeigt deine Rank-Karte oder /rank @user. Admins können /rank @user debug nutzen.',
        'level': 'user',
        'options': [
            {'name': 'user', 'description': 'Nutzer fuer die Rank-Karte.', 'type': 'user', 'required': False},
            {'name': 'debug', 'description': 'Debug-Werte anzeigen.', 'type': 'boolean', 'required': False}
        ]
    })
    async def rank_command(ctx, args):
        guild_id = _guild_id_from_ctx(ctx)
        guild = _guild_from_ctx(ctx)
        author = _author_from_ctx(ctx)
        if guild_id is None or author is None:
            await api.reply(ctx, 'Rank-Karten brauchen eine Server-Manege. In DMs fehlt mir der Boden unter den Rädern!')
            return
        ok, wait = _cooldown_ok('rank', guild_id, int(author.id), 8)
        if not ok:
            await api.reply(ctx, f'Die Rank-Druckmaschine kühlt ab. Noch {wait}s, dann sprüht wieder Farbe!')
            return
        tokens = _normalize_args(args)
        target_id = _parse_user_id(tokens) if tokens else None
        debug_requested = False
        if target_id is None:
            target_id = int(author.id)
            debug_requested = bool(tokens and any(_is_debug_token(t) for t in tokens))
        else:
            debug_requested = any(_is_debug_token(t) for t in tokens[1:])
        if debug_requested and not (_member_has_admin(author) or _member_has_kinger(author)):
            await api.reply(ctx, 'Debug ist S2/S1-Zauberei. Dafür brauchst du Administratorrechte oder die Kinger-Rolle.')
            return

        async with _STATE_LOCK:
            state = await _storage_get(api)
            gs = _ensure_guild(state, guild_id)
            user = _ensure_user(gs, target_id)
            member = await _resolve_member(ctx, target_id)
            if member is not None:
                user['last_seen_name'] = getattr(member, 'display_name', getattr(member, 'name', str(target_id)))
            await _storage_set(api, state)

        total = _total_xp(user)
        level, progress, needed = level_from_xp(total)
        rank = _rank_for_user(gs, target_id, guild)
        if debug_requested:
            await api.reply(ctx,
                '**[DEBUG] Rank-Kabinett geöffnet**\n'
                f'User-ID: `{target_id}`\n'
                f'Rang: `#{rank}`\n'
                f'Level: `{level}`\n'
                f'Gesamt-XP: `{total}`\n'
                f'Level-Fortschritt: `{progress}/{needed}`\n'
                f'Nachrichten-XP: `{int(user.get("message_xp", 0))}`\n'
                f'Voice-XP: `{int(user.get("voice_xp", 0))}`\n'
                f'Invite-XP: `{int(user.get("invite_xp", 0))}`\n'
                f'Manual-XP: `{int(user.get("manual_xp", 0))}`\n'
                f'Gewertete Nachrichten: `{int(user.get("message_count", 0))}`\n'
                f'Rank-Farbe: `{user.get("rank_color", _DEFAULT_COLOR)}`'
            )
            return
        username = user.get('last_seen_name') or str(target_id)
        if member is None and target_id == int(author.id):
            member = author
            username = getattr(author, 'display_name', getattr(author, 'name', username))
        avatar_bytes = await _fetch_avatar_bytes(member)
        color = user.get('rank_color', _DEFAULT_COLOR)
        png = _make_rank_card_png(username, rank, level, progress, needed, total, color, avatar_bytes)
        await _send_rank_card(ctx, api, png)

    @api.command({
        'names': ['set-rank-color', 'set-rank-colour'],
        'description': 'Setzt deine Rank-Karten-Farbe, z. B. /set-rank-color #009FA1.',
        'level': 'user',
        'options': [{'name': 'color', 'description': 'Hex-Farbe wie #009FA1.', 'type': 'string', 'required': True}]
    })
    async def set_rank_color_command(ctx, args):
        guild_id = _guild_id_from_ctx(ctx)
        author = _author_from_ctx(ctx)
        if guild_id is None or author is None:
            await api.reply(ctx, 'Farben haften nur auf Server-Rankkarten, nicht im DM-Nebel.')
            return
        parts = _normalize_args(args)
        if not parts:
            await api.reply(ctx, 'Bitte gib eine Farbe an: `/set-rank-color #009FA1` — sechs Hex-Zeichen, kein fauler Zauber.')
            return
        color = str(parts[0]).strip().upper()
        if not _HEX_RE.match(color):
            await api.reply(ctx, 'Ungültige Farbe! Erlaubt ist exakt `#` plus sechs Hex-Zeichen, z. B. `#00A1A1`.')
            return
        async with _STATE_LOCK:
            state = await _storage_get(api)
            gs = _ensure_guild(state, guild_id)
            user = _ensure_user(gs, int(author.id))
            user['rank_color'] = color
            user['last_seen_name'] = getattr(author, 'display_name', getattr(author, 'name', str(author.id)))
            user['updated_at'] = _now_iso()
            await _storage_set(api, state)
        await api.reply(ctx, f'Vorhang auf! Deine Rank-Karten-Glut leuchtet nun in `{color}`.')

    @api.command({
        'names': ['give-xp', 'givexp'],
        'description': 'Gibt einem Nutzer manuelle XP: /give-xp @user amount [Grund].',
        'level': 'admin',
        'options': [
            {'name': 'user', 'description': 'Nutzer, der XP bekommt.', 'type': 'user', 'required': True},
            {'name': 'amount', 'description': 'XP-Menge.', 'type': 'integer', 'required': True},
            {'name': 'reason', 'description': 'Optionaler Grund.', 'type': 'string', 'required': False}
        ]
    })
    async def give_xp_command(ctx, args):
        parts = _normalize_args(args)
        guild_id = _guild_id_from_ctx(ctx)
        author = _author_from_ctx(ctx)
        if guild_id is None or author is None:
            await api.reply(ctx, 'Manuelle XP gibt es nur in einer Server-Manege.')
            return
        if not _member_has_admin(author):
            await api.reply(ctx, 'S2-Stoppschild! Dafür brauchst du Administrator- oder Manage-Guild-Rechte.')
            return
        if len(parts) < 2:
            await api.reply(ctx, 'Syntax: `/give-xp @user amount [Grund]` — der XP-Trichter braucht Ziel und Menge.')
            return
        target_id = _parse_user_id(parts)
        if target_id is None:
            await api.reply(ctx, 'Ich finde keinen gültigen Nutzer. Nutze Mention oder User-ID.')
            return
        try:
            amount = int(parts[1])
        except Exception:
            await api.reply(ctx, 'Die Menge muss eine ganze Zahl sein.')
            return
        if amount <= 0 or amount > 10_000_000:
            await api.reply(ctx, 'Die Menge muss positiv sein und unter 10.000.000 liegen. Keine XP-Lawinen ohne Geländer!')
            return
        reason_text = ' '.join(parts[2:]).strip()
        reason = 'manual-give' + (f':{reason_text[:120]}' if reason_text else '')
        ref_base = getattr(getattr(ctx, 'message', ctx), 'id', int(time.time() * 1000))
        reference_id = f'manual-give:{ref_base}:{target_id}:{amount}'
        async with _STATE_LOCK:
            state = await _storage_get(api)
            gs = _ensure_guild(state, guild_id)
            movement = _apply_manual_xp(gs, guild_id, target_id, amount, reason, reference_id, int(author.id))
            member = await _resolve_member(ctx, target_id)
            if member is not None:
                _ensure_user(gs, target_id)['last_seen_name'] = getattr(member, 'display_name', getattr(member, 'name', str(target_id)))
            await _storage_set(api, state)
        await api.reply(ctx, f'XP-Konfetti abgefeuert für <@{target_id}>: **+{amount} XP**. Manual-XP: **{_ensure_user(gs, target_id).get("manual_xp", 0)}**, Gesamt-XP: **{movement["new_xp"]}**.')
        await _maybe_send_levelup(api, ctx, target_id, movement)

    @api.command({
        'names': ['remove-xp', 'removexp'],
        'description': 'Entfernt manuelle XP: /remove-xp @user amount [Grund].',
        'level': 'admin',
        'options': [
            {'name': 'user', 'description': 'Nutzer, dem XP entfernt wird.', 'type': 'user', 'required': True},
            {'name': 'amount', 'description': 'XP-Menge.', 'type': 'integer', 'required': True},
            {'name': 'reason', 'description': 'Optionaler Grund.', 'type': 'string', 'required': False}
        ]
    })
    async def remove_xp_command(ctx, args):
        parts = _normalize_args(args)
        guild_id = _guild_id_from_ctx(ctx)
        author = _author_from_ctx(ctx)
        if guild_id is None or author is None:
            await api.reply(ctx, 'Manuelle XP-Entfernung funktioniert nur auf einem Server.')
            return
        if not _member_has_admin(author):
            await api.reply(ctx, 'S2-Stoppschild! Dafür brauchst du Administrator- oder Manage-Guild-Rechte.')
            return
        if len(parts) < 2:
            await api.reply(ctx, 'Syntax: `/remove-xp @user amount [Grund]` — Ziel und Menge, sonst beißt das Zahnrad ins Leere.')
            return
        target_id = _parse_user_id(parts)
        if target_id is None:
            await api.reply(ctx, 'Ich finde keinen gültigen Nutzer. Nutze Mention oder User-ID.')
            return
        try:
            amount = int(parts[1])
        except Exception:
            await api.reply(ctx, 'Die Menge muss eine ganze Zahl sein.')
            return
        if amount <= 0 or amount > 10_000_000:
            await api.reply(ctx, 'Die Menge muss positiv sein und unter 10.000.000 liegen.')
            return
        reason_text = ' '.join(parts[2:]).strip()
        reason = 'manual-remove' + (f':{reason_text[:120]}' if reason_text else '')
        ref_base = getattr(getattr(ctx, 'message', ctx), 'id', int(time.time() * 1000))
        reference_id = f'manual-remove:{ref_base}:{target_id}:{amount}'
        async with _STATE_LOCK:
            state = await _storage_get(api)
            gs = _ensure_guild(state, guild_id)
            movement = _apply_manual_xp(gs, guild_id, target_id, -amount, reason, reference_id, int(author.id))
            await _storage_set(api, state)
        await api.reply(ctx, f'XP-Sand abgesaugt bei <@{target_id}>: **-{amount} XP**. Manual-XP darf negativ tanzen: **{_ensure_user(gs, target_id).get("manual_xp", 0)}**, Gesamt-XP: **{movement["new_xp"]}**.')

    @api.command({
        'names': ['importdb'],
        'description': 'Importiert einen bot-db.json Snapshot aus Nachrichten-Anhängen.',
        'level': 'kinger',
        'options': [{'name': 'file', 'description': 'bot-db.json Snapshot.', 'type': 'attachment', 'required': True}]
    })
    async def importdb_command(ctx, args):
        guild_id = _guild_id_from_ctx(ctx)
        author = _author_from_ctx(ctx)
        if guild_id is None or author is None:
            await api.reply(ctx, 'Import nur in einer Server-Manege, bitte.')
            return
        if not _member_has_kinger(author):
            await api.reply(ctx, 'S1-Kuppel geschlossen. Nur Kinger/Bot-Master dürfen die Datenkiste öffnen.')
            return
        message = getattr(ctx, 'message', ctx)
        attachments = getattr(message, 'attachments', []) or []
        if not attachments:
            await api.reply(ctx, 'Bitte hänge eine `bot-db.json` an. Ohne Datei bleibt die Importkanone leer.')
            return
        imported = 0
        async with _STATE_LOCK:
            state = await _storage_get(api)
            gs = _ensure_guild(state, guild_id)
            for att in attachments:
                name = str(getattr(att, 'filename', '')).lower()
                if not name.endswith('.json'):
                    continue
                try:
                    data_bytes = await att.read()
                    snap = json.loads(data_bytes.decode('utf-8'))
                except Exception:
                    continue
                if str(snap.get('guildId')) != str(guild_id):
                    continue
                for entry in snap.get('users', []):
                    try:
                        uid = int(entry.get('userId'))
                    except Exception:
                        continue
                    user = _ensure_user(gs, uid)
                    user['message_xp'] = max(int(user.get('message_xp', 0)), int(entry.get('messageXp', 0)))
                    user['voice_xp'] = max(int(user.get('voice_xp', 0)), int(entry.get('voiceXp', 0)))
                    user['invite_xp'] = max(int(user.get('invite_xp', 0)), int(entry.get('inviteXp', 0)))
                    user['manual_xp'] = max(int(user.get('manual_xp', 0)), int(entry.get('manualXp', 0)))
                    color = str(entry.get('rankColor', _DEFAULT_COLOR)).upper()
                    user['rank_color'] = color if _HEX_RE.match(color) else _DEFAULT_COLOR
                    imported += 1
            await _storage_set(api, state)
        await api.reply(ctx, f'Import-Manege geschlossen: **{imported} Profile** wurden gemerged. Pro XP-Konto gewann der größte Wert.')

    @api.command({
        'names': ['recalculate'],
        'description': '/recalculate all|Messages|invites. Messages scannt erreichbare Channel-History neu.',
        'level': 'kinger',
        'options': [{'name': 'mode', 'description': 'all, messages oder invites.', 'type': 'string', 'required': True}]
    })
    async def recalculate_command(ctx, args):
        parts = _normalize_args(args)
        guild_id = _guild_id_from_ctx(ctx)
        guild = _guild_from_ctx(ctx)
        author = _author_from_ctx(ctx)
        if guild_id is None or guild is None or author is None:
            await api.reply(ctx, 'Recalculate braucht eine echte Server-Manege.')
            return
        if not _member_has_kinger(author):
            await api.reply(ctx, 'S1-Kuppel geschlossen. Nur Kinger/Bot-Master dürfen neu berechnen.')
            return
        mode = (parts[0].lower() if parts else '').strip()
        if mode not in {'all', 'messages', 'message', 'invites', 'invite'}:
            await api.reply(ctx, 'Syntax: `/recalculate all`, `/recalculate Messages` oder `/recalculate invites`.')
            return
        if mode in {'invites', 'invite'}:
            await api.reply(ctx, 'Invite-Recalculate ist in diesem Python-Plugin als sichere Attrappe markiert: Discord.py liefert historische Invite-Zuordnung nicht zuverlässig. Keine Daten wurden verändert.')
            return
        await api.reply(ctx, 'Die Nachrichten-Archivkanone rollt los. Ich scanne erreichbare Textkanäle — Forbidden/NotFound werden höflich übersprungen.')
        scanned = 0
        awarded = 0
        new_message_map: Dict[str, Dict[str, Any]] = {}
        per_user: Dict[str, Dict[str, int]] = {}
        channels = list(getattr(guild, 'text_channels', []) or [])
        for channel in channels:
            if not hasattr(channel, 'history'):
                continue
            try:
                async for msg in channel.history(limit=None, oldest_first=True):
                    if getattr(msg, 'guild', None) is None:
                        continue
                    author_msg = getattr(msg, 'author', None)
                    if author_msg is None or getattr(author_msg, 'bot', False):
                        continue
                    content = str(getattr(msg, 'content', '') or '')
                    if _is_xp_excluded_command(content):
                        continue
                    xp = calculate_message_xp(int(msg.id))
                    mid = str(msg.id)
                    uid = str(author_msg.id)
                    new_message_map[mid] = {'channel_id': str(getattr(channel, 'id', 0)), 'user_id': uid, 'xp': xp, 'created_at_utc': _now_iso()}
                    bucket = per_user.setdefault(uid, {'xp': 0, 'count': 0})
                    bucket['xp'] += xp
                    bucket['count'] += 1
                    scanned += 1
                    awarded += xp
            except Exception:
                continue
        async with _STATE_LOCK:
            state = await _storage_get(api)
            gs = _ensure_guild(state, guild_id)
            gs['message_xp'] = new_message_map
            for _uid, user in gs.get('users', {}).items():
                user['message_xp'] = 0
                user['message_count'] = 0
            for uid, bucket in per_user.items():
                user = _ensure_user(gs, int(uid))
                user['message_xp'] = int(bucket['xp'])
                user['message_count'] = int(bucket['count'])
                member = guild.get_member(int(uid)) if hasattr(guild, 'get_member') else None
                if member is not None:
                    user['last_seen_name'] = getattr(member, 'display_name', getattr(member, 'name', uid))
            await _storage_set(api, state)
        extra = ' Invite-Backfill wurde in dieser Python-Laufzeit nicht verändert.' if mode == 'all' else ''
        await api.reply(ctx, f'Recalculate abgeschlossen! **{scanned} Nachrichten** neu bewertet, **{awarded} Message-XP** verteilt.{extra}')

    @api.event('message')
    async def on_message(message):
        if getattr(message, 'guild', None) is None:
            return
        author = getattr(message, 'author', None)
        if author is None or getattr(author, 'bot', False):
            return
        content = str(getattr(message, 'content', '') or '')
        if _is_xp_excluded_command(content):
            return
        guild_id = int(message.guild.id)
        user_id = int(author.id)
        xp = calculate_message_xp(int(message.id))
        async with _STATE_LOCK:
            state = await _storage_get(api)
            gs = _ensure_guild(state, guild_id)
            mid = str(message.id)
            if mid in gs.setdefault('message_xp', {}):
                return
            user = _ensure_user(gs, user_id)
            old_total = _total_xp(user)
            old_level, _, _ = level_from_xp(old_total)
            gs['message_xp'][mid] = {'channel_id': str(getattr(getattr(message, 'channel', None), 'id', 0)), 'user_id': str(user_id), 'xp': xp, 'created_at_utc': _now_iso()}
            user['message_xp'] = int(user.get('message_xp', 0)) + xp
            user['message_count'] = int(user.get('message_count', 0)) + 1
            user['last_seen_name'] = getattr(author, 'display_name', getattr(author, 'name', str(user_id)))
            user['updated_at'] = _now_iso()
            new_total = _total_xp(user)
            new_level, progress, needed = level_from_xp(new_total)
            movement = {'applied': True, 'old_level': old_level, 'new_level': new_level, 'new_xp': new_total, 'progress': progress, 'needed': needed}
            await _storage_set(api, state)
        await _maybe_send_levelup(api, message, user_id, movement)

    @api.event('message_delete')
    async def on_message_delete(message):
        guild = getattr(message, 'guild', None)
        if guild is None:
            return
        guild_id = int(guild.id)
        mid = str(getattr(message, 'id', ''))
        if not mid:
            return
        async with _STATE_LOCK:
            state = await _storage_get(api)
            gs = _ensure_guild(state, guild_id)
            entry = gs.setdefault('message_xp', {}).pop(mid, None)
            if not entry:
                return
            try:
                uid = int(entry.get('user_id'))
                xp = int(entry.get('xp', 0))
            except Exception:
                await _storage_set(api, state)
                return
            user = _ensure_user(gs, uid)
            user['message_xp'] = max(0, int(user.get('message_xp', 0)) - xp)
            user['message_count'] = max(0, int(user.get('message_count', 0)) - 1)
            user['updated_at'] = _now_iso()
            await _storage_set(api, state)

    @api.event('bulk_message_delete')
    async def on_bulk_message_delete(messages):
        if not messages:
            return
        first = next(iter(messages), None)
        guild = getattr(first, 'guild', None) if first is not None else None
        if guild is None:
            return
        guild_id = int(guild.id)
        ids = {str(getattr(m, 'id', '')) for m in messages}
        async with _STATE_LOCK:
            state = await _storage_get(api)
            gs = _ensure_guild(state, guild_id)
            changed = False
            for mid in list(ids):
                entry = gs.setdefault('message_xp', {}).pop(mid, None)
                if not entry:
                    continue
                try:
                    uid = int(entry.get('user_id'))
                    xp = int(entry.get('xp', 0))
                except Exception:
                    continue
                user = _ensure_user(gs, uid)
                user['message_xp'] = max(0, int(user.get('message_xp', 0)) - xp)
                user['message_count'] = max(0, int(user.get('message_count', 0)) - 1)
                user['updated_at'] = _now_iso()
                changed = True
            if changed:
                await _storage_set(api, state)

    @api.on('level_manege.export_snapshot')
    async def export_snapshot(payload=None):
        state = await _storage_get(api)
        return state
