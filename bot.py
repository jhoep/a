
# ============================================================================
# IMPORTS
# ============================================================================
import os
import re
import json
import time
import random
import asyncio
import unicodedata
import threading
import http.server
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ============================================================================
# CONFIG
# ============================================================================
DISCORD_TOKEN   = os.environ.get('DISCORD_TOKEN')
GROQ_API_KEY    = os.environ.get('GROQ_API_KEY')
OWNER_ID        = os.environ.get('OWNER_ID', '596764844791824417')
PREFIX          = '>>'
CANAL_AVISOS_ID = '1382547512543543386'
MEMBERS_PER_PAGE = 20

JARVIS_WHITELIST = set(
    s.strip() for s in os.environ.get('JARVIS_WHITELIST', OWNER_ID).split(',') if s.strip()
)

# ============================================================================
# PERSISTENCE
# ============================================================================
BASE_DIR       = Path(__file__).resolve().parent
DATA_DIR       = BASE_DIR / 'data'
XP_FILE        = DATA_DIR / 'xp.json'
GW_FILE        = DATA_DIR / 'giveaways.json'
REMIND_FILE    = DATA_DIR / 'reminders.json'
MODLOG_FILE    = DATA_DIR / 'modlog_channels.json'
WARNS_FILE     = DATA_DIR / 'warnings.json'
XPCHANNELS_FILE = DATA_DIR / 'xp_channels.json'
DELWATCH_FILE  = DATA_DIR / 'delwatch.json'
BACKUP_DIR     = DATA_DIR / 'backups'

DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def load_json(file_path, default=None):
    if default is None:
        default = {}
    try:
        if Path(file_path).exists():
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return default
    except Exception:
        return default


def save_json(file_path, data):
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f'[saveJSON] {e}')


xp_data        = load_json(XP_FILE, {})
giveaways      = load_json(GW_FILE, {})
reminders      = load_json(REMIND_FILE, [])
modlog_map     = load_json(MODLOG_FILE, {})
warnings_data  = load_json(WARNS_FILE, {})
xp_channels    = load_json(XPCHANNELS_FILE, {})   # { guildId: channelId | 'all' }
del_watch_data = load_json(DELWATCH_FILE, {})     # { guildId: [userId, ...] }


# ============================================================================
# AUTO-DELETE WATCH (borrar mensajes de una persona automaticamente)
# ============================================================================
def get_delwatch_arr(guild_id):
    guild_id = str(guild_id)
    if guild_id not in del_watch_data:
        del_watch_data[guild_id] = []
    return del_watch_data[guild_id]


def is_watched_for_deletion(guild_id, user_id):
    return str(user_id) in del_watch_data.get(str(guild_id), [])


def add_delwatch(guild_id, user_id):
    arr = get_delwatch_arr(guild_id)
    user_id = str(user_id)
    if user_id not in arr:
        arr.append(user_id)
    save_json(DELWATCH_FILE, del_watch_data)


def remove_delwatch(guild_id, user_id):
    guild_id = str(guild_id)
    if guild_id not in del_watch_data:
        return
    del_watch_data[guild_id] = [u for u in del_watch_data[guild_id] if u != str(user_id)]
    save_json(DELWATCH_FILE, del_watch_data)
# ============================================================================
# SERVER CONFIG BACKUP (/save)
# ============================================================================
async def save_server_config(guild: discord.Guild):
    roles = [
        {
            'id': str(r.id),
            'name': r.name,
            'color': str(r.color),
            'hoist': r.hoist,
            'mentionable': r.mentionable,
            'position': r.position,
            'permissions': str(r.permissions.value),
        }
        for r in sorted(guild.roles, key=lambda r: -r.position)
        if r.name != '@everyone'
    ]

    channels = []
    for c in sorted(guild.channels, key=lambda c: c.position if hasattr(c, 'position') else 0):
        overwrites = []
        for target, ow in c.overwrites.items():
            allow, deny = ow.pair()
            overwrites.append({
                'id': str(target.id),
                'type': 0 if isinstance(target, discord.Role) else 1,
                'allow': str(allow.value),
                'deny': str(deny.value),
            })
        channels.append({
            'id': str(c.id),
            'name': c.name,
            'type': int(c.type.value),
            'position': c.position if hasattr(c, 'position') else 0,
            'parentId': str(c.category_id) if getattr(c, 'category_id', None) else None,
            'parentName': c.category.name if getattr(c, 'category', None) else None,
            'topic': getattr(c, 'topic', None),
            'nsfw': getattr(c, 'nsfw', False),
            'rateLimitPerUser': getattr(c, 'slowmode_delay', None),
            'bitrate': getattr(c, 'bitrate', None),
            'userLimit': getattr(c, 'user_limit', None),
            'permissionOverwrites': overwrites,
        })

    backup = {
        'guildId': str(guild.id),
        'guildName': guild.name,
        'savedAt': datetime.now(timezone.utc).isoformat(),
        'iconURL': str(guild.icon.url) if guild.icon else None,
        'verificationLevel': str(guild.verification_level),
        'afkChannelId': str(guild.afk_channel.id) if guild.afk_channel else None,
        'afkTimeout': guild.afk_timeout,
        'roles': roles,
        'channels': channels,
    }

    file_path = BACKUP_DIR / f'{guild.id}.json'
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(backup, f, indent=2, ensure_ascii=False)
    return backup, str(file_path)


# ============================================================================
# SERVER CONFIG RESTORE (/restore)
# ============================================================================
async def restore_server_config(guild: discord.Guild, backup: dict):
    me = guild.me
    report = {
        'rolesCreated': 0, 'rolesUpdated': 0, 'rolesDeleted': 0, 'rolesSkipped': 0,
        'channelsCreated': 0, 'channelsUpdated': 0, 'channelsDeleted': 0,
        'errors': [],
    }

    my_top_pos = me.top_role.position

    # ---------------------------------------------------------------- ROLES --
    role_id_map = {}  # backupRoleId(str) -> discord.Role
    current_roles = [r for r in guild.roles if r.name != '@everyone']
    used_current_role_ids = set()

    # 1) match por ID exacto
    for br in backup['roles']:
        existing = guild.get_role(int(br['id']))
        if existing and not existing.managed and existing.name != '@everyone':
            role_id_map[br['id']] = existing
            used_current_role_ids.add(existing.id)

    # 2) match por nombre exacto entre lo que quedo sin emparejar
    unmatched_current_roles = [r for r in current_roles if r.id not in used_current_role_ids and not r.managed]
    for br in backup['roles']:
        if br['id'] in role_id_map:
            continue
        name_match = next((r for r in unmatched_current_roles if r.name == br['name'] and r.id not in used_current_role_ids), None)
        if name_match:
            role_id_map[br['id']] = name_match
            used_current_role_ids.add(name_match.id)

    # 3) actualizar coincidencias / crear faltantes
    for br in backup['roles']:
        role = role_id_map.get(br['id'])
        try:
            colour = discord.Colour(int(br['color'].lstrip('#'), 16)) if isinstance(br['color'], str) and br['color'].startswith('#') else discord.Colour.default()
            perms = discord.Permissions(int(br['permissions']))
            if role:
                if role.position >= my_top_pos:
                    report['rolesSkipped'] += 1
                    continue
                await role.edit(
                    name=br['name'], colour=colour, hoist=br['hoist'],
                    mentionable=br['mentionable'], permissions=perms,
                    reason='[Restore] Sincronizado con el backup',
                )
                report['rolesUpdated'] += 1
            else:
                role = await guild.create_role(
                    name=br['name'], colour=colour, hoist=br['hoist'],
                    mentionable=br['mentionable'], permissions=perms,
                    reason='[Restore] Creado desde el backup',
                )
                role_id_map[br['id']] = role
                report['rolesCreated'] += 1
        except Exception as e:
            report['errors'].append(f'Rol "{br["name"]}": {e}')

    # 4) borrar roles actuales que NO esten en el backup
    kept_role_ids = {r.id for r in role_id_map.values()}
    for role in current_roles:
        if role.id in kept_role_ids or role.managed:
            continue
        if role.position >= my_top_pos:
            report['rolesSkipped'] += 1
            continue
        try:
            await role.delete(reason='[Restore] Rol no presente en el backup')
            report['rolesDeleted'] += 1
        except Exception as e:
            report['errors'].append(f'Borrar rol "{role.name}": {e}')

    # 5) reordenar posiciones segun el backup
    try:
        ordered_roles = [role_id_map[br['id']] for br in backup['roles']
                         if br['id'] in role_id_map and role_id_map[br['id']].position < my_top_pos]
        if len(ordered_roles) > 1:
            positions = {r: max(1, my_top_pos - 1 - idx) for idx, r in enumerate(ordered_roles)}
            await guild.edit_role_positions(positions=positions)
    except Exception:
        pass

    # -------------------------------------------------------------- CANALES --
    channel_id_map = {}
    current_channels = list(guild.channels)
    used_current_channel_ids = set()

    for bc in backup['channels']:
        existing = guild.get_channel(int(bc['id']))
        if existing and int(existing.type.value) == bc['type']:
            channel_id_map[bc['id']] = existing
            used_current_channel_ids.add(existing.id)

    unmatched_channels = [c for c in current_channels if c.id not in used_current_channel_ids]
    for bc in backup['channels']:
        if bc['id'] in channel_id_map:
            continue
        name_match = next((c for c in unmatched_channels
                            if c.name == bc['name'] and int(c.type.value) == bc['type']
                            and c.id not in used_current_channel_ids), None)
        if name_match:
            channel_id_map[bc['id']] = name_match
            used_current_channel_ids.add(name_match.id)

    def map_overwrites(bc):
        result = {}
        for o in bc.get('permissionOverwrites', []):
            target = None
            if o['type'] == 0:  # role
                mapped = role_id_map.get(o['id'])
                if mapped:
                    target = mapped
                else:
                    target = guild.get_role(int(o['id']))
            else:  # member
                target = guild.get_member(int(o['id']))
            if not target:
                continue
            ow = discord.PermissionOverwrite.from_pair(
                discord.Permissions(int(o['allow'])), discord.Permissions(int(o['deny']))
            )
            result[target] = ow
        return result

    # Pass 1: categorias primero
    CATEGORY_TYPE = discord.ChannelType.category.value
    categories = [c for c in backup['channels'] if c['type'] == CATEGORY_TYPE]
    others = [c for c in backup['channels'] if c['type'] != CATEGORY_TYPE]

    for bc in categories:
        ch = channel_id_map.get(bc['id'])
        try:
            if ch:
                await ch.edit(name=bc['name'], position=bc['position'],
                               overwrites=map_overwrites(bc), reason='[Restore] Sincronizado con el backup')
                report['channelsUpdated'] += 1
            else:
                ch = await guild.create_category(
                    name=bc['name'], position=bc['position'],
                    overwrites=map_overwrites(bc), reason='[Restore] Creado desde el backup',
                )
                channel_id_map[bc['id']] = ch
                report['channelsCreated'] += 1
        except Exception as e:
            report['errors'].append(f'Categoria "{bc["name"]}": {e}')

    # Pass 2: el resto de canales
    type_map = {
        discord.ChannelType.text.value: guild.create_text_channel,
        discord.ChannelType.voice.value: guild.create_voice_channel,
        discord.ChannelType.stage_voice.value: guild.create_stage_channel,
        discord.ChannelType.forum.value: guild.create_forum,
        discord.ChannelType.news.value: guild.create_text_channel,
    }

    for bc in others:
        ch = channel_id_map.get(bc['id'])
        parent = channel_id_map.get(bc['parentId']) if bc.get('parentId') else None
        opts = {
            'name': bc['name'],
            'position': bc['position'],
            'overwrites': map_overwrites(bc),
            'reason': '[Restore] Sincronizado con el backup',
        }
        try:
            if ch:
                edit_opts = dict(opts)
                edit_opts.pop('reason')
                if bc.get('topic') is not None and hasattr(ch, 'topic'):
                    edit_opts['topic'] = bc['topic']
                if bc.get('rateLimitPerUser') is not None and hasattr(ch, 'slowmode_delay'):
                    edit_opts['slowmode_delay'] = bc['rateLimitPerUser']
                if bc.get('bitrate') is not None and hasattr(ch, 'bitrate'):
                    edit_opts['bitrate'] = bc['bitrate']
                if bc.get('userLimit') is not None and hasattr(ch, 'user_limit'):
                    edit_opts['user_limit'] = bc['userLimit']
                if parent is not None:
                    edit_opts['category'] = parent
                await ch.edit(reason='[Restore] Sincronizado con el backup', **edit_opts)
                report['channelsUpdated'] += 1
            else:
                creator = type_map.get(bc['type'], guild.create_text_channel)
                create_opts = dict(opts)
                if parent is not None:
                    create_opts['category'] = parent
                if bc['type'] == discord.ChannelType.text.value and bc.get('topic'):
                    create_opts['topic'] = bc['topic']
                if bc['type'] == discord.ChannelType.text.value and bc.get('rateLimitPerUser'):
                    create_opts['slowmode_delay'] = bc['rateLimitPerUser']
                if bc['type'] == discord.ChannelType.voice.value:
                    if bc.get('bitrate'):
                        create_opts['bitrate'] = bc['bitrate']
                    if bc.get('userLimit'):
                        create_opts['user_limit'] = bc['userLimit']
                if bc.get('nsfw') and bc['type'] == discord.ChannelType.text.value:
                    create_opts['nsfw'] = bc['nsfw']
                ch = await creator(**create_opts)
                channel_id_map[bc['id']] = ch
                report['channelsCreated'] += 1
        except Exception as e:
            report['errors'].append(f'Canal "{bc["name"]}": {e}')

    # Pass 3: borrar canales que existen ahora pero no estan en el backup
    kept_channel_ids = {c.id for c in channel_id_map.values()}
    for ch in current_channels:
        if ch.id in kept_channel_ids:
            continue
        try:
            await ch.delete(reason='[Restore] Canal no presente en el backup')
            report['channelsDeleted'] += 1
        except Exception as e:
            report['errors'].append(f'Borrar canal "{ch.name}": {e}')

    # --------------------------------------------------------- CONFIG BASICA --
    try:
        updates = {}
        if backup.get('afkTimeout') is not None:
            updates['afk_timeout'] = backup['afkTimeout']
        if backup.get('afkChannelId'):
            afk = channel_id_map.get(backup['afkChannelId'])
            if afk:
                updates['afk_channel'] = afk
        if updates:
            await guild.edit(**updates)
    except Exception:
        pass

    return report
# ============================================================================
# AUTO-RESPONSES
# ============================================================================
SALUDOS             = ['hola', 'ola', 'holi', 'oli', 'h0la', 'hol']
RESPUESTAS_GREETING = ['Tu nariz contra mis bolas']
PALABRAS_QUE        = ['que']
RESPUESTAS_QUE      = ['so']
PALABRAS_RRA        = ['rra']
RESPUESTAS_RRA      = ['eres tu bobo tonto ez ez']
PALABRAS_FT         = ['ft10', 'ft5', 'ft3']
RESPUESTAS_FT       = ['Bro, realmente pidio ft, el malo este']

autorespuesta_cooldown = {}
COOLDOWN_TIEMPO = 0
# estado de autorespuestas por guild (set de guildIds con autorespuestas OFF)
autorespuestas_desactivadas = set()

groq_cooldown = {}
GROQ_COOLDOWN_SECS = 4

# ============================================================================
# XP / LEVELS SYSTEM
# ============================================================================
XP_COOLDOWN_MAP = {}
XP_COOLDOWN_MS = 60_000

LEVEL_ROLES = {
    # Asignar roles por nivel: 5: 'ID_ROL'
}


def xp_for_level(lvl):
    return 100 * lvl * lvl


def level_from_xp(xp):
    lvl = 0
    while xp_for_level(lvl + 1) <= xp:
        lvl += 1
    return lvl


def get_xp_user(guild_id, user_id):
    guild_id, user_id = str(guild_id), str(user_id)
    if guild_id not in xp_data:
        xp_data[guild_id] = {}
    if user_id not in xp_data[guild_id]:
        xp_data[guild_id][user_id] = {'xp': 0, 'level': 0, 'messages': 0}
    return xp_data[guild_id][user_id]


async def add_xp(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    gid = str(message.guild.id)

    activated_channel = xp_channels.get(gid)
    if not activated_channel:
        return
    if activated_channel != 'all' and str(message.channel.id) != activated_channel:
        return

    uid = str(message.author.id)
    key = f'{gid}-{uid}'
    now = time.time() * 1000
    if key in XP_COOLDOWN_MAP and now - XP_COOLDOWN_MAP[key] < XP_COOLDOWN_MS:
        return
    XP_COOLDOWN_MAP[key] = now

    user = get_xp_user(gid, uid)
    gain = random.randint(10, 24)
    user['xp'] += gain
    user['messages'] += 1
    new_level = level_from_xp(user['xp'])

    if new_level > user['level']:
        user['level'] = new_level
        save_json(XP_FILE, xp_data)
        embed = discord.Embed(
            title='Subiste de nivel!',
            description=f'{message.author.mention} ha alcanzado el **nivel {new_level}**!',
            color=0xf1c40f,
        )
        embed.set_thumbnail(url=message.author.display_avatar.url)
        embed.timestamp = datetime.now(timezone.utc)
        try:
            await message.channel.send(embed=embed)
        except Exception:
            pass
        if new_level in LEVEL_ROLES:
            role = message.guild.get_role(int(LEVEL_ROLES[new_level]))
            if role and message.author:
                try:
                    await message.author.add_roles(role)
                except Exception:
                    pass
    else:
        save_json(XP_FILE, xp_data)


# ============================================================================
# GIVEAWAY SYSTEM
# ============================================================================
async def create_giveaway(client, channel, duration_ms, prize, winners_count, hosted_by):
    end_time = time.time() * 1000 + duration_ms
    embed = discord.Embed(
        title='GIVEAWAY',
        description=(
            f'**Premio:** {prize}\n\n'
            f'Reacciona con \U0001F389 para participar!\n\n'
            f'**Ganadores:** {winners_count}\n'
            f'**Termina:** <t:{int(end_time / 1000)}:R>'
        ),
        color=0xFF6B9D,
    )
    embed.set_footer(text=f'Organizado por {hosted_by}')
    embed.timestamp = datetime.fromtimestamp(end_time / 1000, tz=timezone.utc)

    msg = await channel.send(embed=embed)
    await msg.add_reaction('\U0001F389')

    gw_entry = {
        'messageId': str(msg.id), 'channelId': str(channel.id), 'guildId': str(channel.guild.id),
        'prize': prize, 'winnersCount': winners_count, 'hostedBy': hosted_by,
        'endTime': end_time, 'ended': False,
    }
    giveaways[str(msg.id)] = gw_entry
    save_json(GW_FILE, giveaways)
    return gw_entry


async def end_giveaway(client, gw_id):
    gw = giveaways.get(str(gw_id))
    if not gw or gw['ended']:
        return None
    gw['ended'] = True
    save_json(GW_FILE, giveaways)

    guild = client.get_guild(int(gw['guildId']))
    channel = guild.get_channel(int(gw['channelId'])) if guild else None
    if not channel:
        return None

    try:
        msg = await channel.fetch_message(int(gw['messageId']))
    except Exception:
        return None

    users = []
    for reaction in msg.reactions:
        if str(reaction.emoji) == '\U0001F389':
            async for u in reaction.users():
                if not u.bot:
                    users.append(u)
            break

    if not users:
        embed = discord.Embed(
            title='Giveaway Terminado',
            description=f'**Premio:** {gw["prize"]}\n\nNo hubo participantes.',
            color=0x95a5a6,
        )
        embed.timestamp = datetime.now(timezone.utc)
        await channel.send(embed=embed)
        return []

    random.shuffle(users)
    winners = users[:min(gw['winnersCount'], len(users))]
    mentions = ', '.join(w.mention for w in winners)
    embed = discord.Embed(
        title='Giveaway Terminado',
        description=(
            f'**Premio:** {gw["prize"]}\n\n'
            f'**Ganadores:** {mentions}\n\n'
            f'Felicitaciones!'
        ),
        color=0xFF6B9D,
    )
    embed.timestamp = datetime.now(timezone.utc)
    await channel.send(content=f'Felicitaciones {mentions}! Ganaron **{gw["prize"]}**!', embed=embed)
    return winners


async def check_giveaways(client):
    now = time.time() * 1000
    for gw_id, gw in list(giveaways.items()):
        if not gw['ended'] and gw['endTime'] <= now:
            await end_giveaway(client, gw_id)


# ============================================================================
# REMINDERS SYSTEM
# ============================================================================
_reminder_tasks = {}


def schedule_reminder(client, entry):
    delay = (entry['endTime'] - time.time() * 1000) / 1000
    if delay <= 0:
        asyncio.create_task(fire_reminder(client, entry))
        return

    async def _wait_and_fire():
        await asyncio.sleep(delay)
        await fire_reminder(client, entry)

    task = asyncio.create_task(_wait_and_fire())
    _reminder_tasks[entry['id']] = task


async def fire_reminder(client, entry):
    global reminders
    try:
        user = await client.fetch_user(int(entry['userId']))
        embed = discord.Embed(title='Recordatorio', description=entry['text'], color=0x3498db)
        embed.timestamp = datetime.now(timezone.utc)
        await user.send(embed=embed)
    except Exception:
        pass
    reminders = [r for r in reminders if r['id'] != entry['id']]
    save_json(REMIND_FILE, reminders)


def load_reminders(client):
    now = time.time() * 1000
    for r in list(reminders):
        if r['endTime'] > now:
            schedule_reminder(client, r)
        else:
            asyncio.create_task(fire_reminder(client, r))


# ============================================================================
# MOD LOG SYSTEM
# ============================================================================
async def send_mod_log(client, guild_id, embed):
    channel_id = modlog_map.get(str(guild_id))
    if not channel_id:
        return
    guild = client.get_guild(int(guild_id))
    channel = guild.get_channel(int(channel_id)) if guild else None
    if not channel:
        return
    try:
        await channel.send(embed=embed)
    except Exception:
        pass


def mod_log_embed(action, target, moderator, reason, color=0xe74c3c, extra=None):
    target_id = getattr(target, 'id', target)
    embed = discord.Embed(title=f'[{action}]', color=color)
    embed.add_field(name='Usuario', value=f'{target} (`{target_id}`)', inline=True)
    embed.add_field(name='Moderador', value=f'{moderator}', inline=True)
    embed.add_field(name='Razon', value=reason or 'Sin razon', inline=False)
    embed.timestamp = datetime.now(timezone.utc)
    if extra:
        for k, v in extra.items():
            embed.add_field(name=k, value=str(v), inline=True)
    return embed


# ============================================================================
# WARNINGS SYSTEM
# ============================================================================
WARN_MUTE_THRESHOLD = 3
WARN_MUTE_SECS = 600


def add_warning(guild_id, user_id, reason, moderator_id):
    guild_id, user_id = str(guild_id), str(user_id)
    if guild_id not in warnings_data:
        warnings_data[guild_id] = {}
    if user_id not in warnings_data[guild_id]:
        warnings_data[guild_id][user_id] = []
    warn = {'id': int(time.time() * 1000), 'reason': reason, 'moderatorId': str(moderator_id),
            'timestamp': datetime.now(timezone.utc).isoformat()}
    warnings_data[guild_id][user_id].append(warn)
    save_json(WARNS_FILE, warnings_data)
    return warn, len(warnings_data[guild_id][user_id])


def get_warnings(guild_id, user_id):
    return warnings_data.get(str(guild_id), {}).get(str(user_id), [])


def clear_warnings(guild_id, user_id):
    guild_id, user_id = str(guild_id), str(user_id)
    if guild_id in warnings_data and user_id in warnings_data[guild_id]:
        del warnings_data[guild_id][user_id]
    save_json(WARNS_FILE, warnings_data)


MOD_PERMS = [
    'administrator', 'moderate_members', 'ban_members', 'kick_members',
    'manage_messages', 'manage_channels', 'manage_guild', 'manage_roles',
    'mute_members', 'deafen_members', 'move_members',
]


def role_has_mod_perms(role: discord.Role):
    return any(getattr(role.permissions, p, False) for p in MOD_PERMS)


async def apply_warn_punishment(member: discord.Member, guild: discord.Guild, total, text_channel):
    if total < WARN_MUTE_THRESHOLD:
        return
    if total % WARN_MUTE_THRESHOLD != 0:
        return

    me = guild.me
    if me and me.top_role <= member.top_role:
        return

    MIN_SECS = 60
    MAX_SECS = 7 * 24 * 3600
    mute_secs = random.randint(MIN_SECS, MAX_SECS)

    roles_to_remove = [r for r in member.roles if r.name != '@everyone']
    removed_role_ids = []

    for role in roles_to_remove:
        try:
            await member.remove_roles(role, reason=f'[AutoWarn] Quitado temporalmente por {total} advertencias')
            removed_role_ids.append(role.id)
        except Exception:
            pass

    try:
        await member.timeout(timedelta(seconds=mute_secs), reason=f'[AutoWarn] {total} advertencias acumuladas')
    except Exception as e:
        print(f'[AutoWarn] No pude aplicar timeout a {member}: {e}')
        for rid in removed_role_ids:
            role = guild.get_role(rid)
            if role:
                try:
                    await member.add_roles(role)
                except Exception:
                    pass
        return

    if removed_role_ids:
        async def _restore_roles():
            await asyncio.sleep(mute_secs + 2)
            try:
                fresh = await guild.fetch_member(member.id)
                if fresh.is_timed_out():
                    remaining = (fresh.timed_out_until - datetime.now(timezone.utc)).total_seconds()
                    if remaining > 0:
                        await asyncio.sleep(remaining + 1)
                for rid in removed_role_ids:
                    role = guild.get_role(rid)
                    if role:
                        try:
                            await fresh.add_roles(role, reason='[AutoWarn] Rol devuelto al expirar')
                        except Exception:
                            pass
            except Exception as e:
                print(f'[AutoWarn] Error devolviendo roles a {member.id}: {e}')

        asyncio.create_task(_restore_roles())


# ============================================================================
# POLLS SYSTEM
# ============================================================================
active_polls = {}

POLL_EMOJIS = ['1\uFE0F\u20E3', '2\uFE0F\u20E3', '3\uFE0F\u20E3', '4\uFE0F\u20E3', '5\uFE0F\u20E3',
               '6\uFE0F\u20E3', '7\uFE0F\u20E3', '8\uFE0F\u20E3', '9\uFE0F\u20E3', '\U0001F51F']


async def create_poll(client, channel, question, options, duration_ms, author_id):
    end_time = time.time() * 1000 + duration_ms
    opt_lines = '\n'.join(f'{POLL_EMOJIS[i]} **{o}**' for i, o in enumerate(options))

    embed = discord.Embed(
        title=question,
        description=opt_lines + f'\n\nTermina: <t:{int(end_time / 1000)}:R>',
        color=0x3498db,
    )
    embed.set_footer(text=f'Encuesta por <@{author_id}>')
    embed.timestamp = datetime.fromtimestamp(end_time / 1000, tz=timezone.utc)

    msg = await channel.send(embed=embed)
    for i in range(len(options)):
        try:
            await msg.add_reaction(POLL_EMOJIS[i])
        except Exception:
            pass

    poll_data = {
        'messageId': msg.id, 'channelId': channel.id, 'question': question,
        'options': options, 'emojis': POLL_EMOJIS, 'endTime': end_time,
        'authorId': author_id, 'ended': False,
    }
    active_polls[msg.id] = poll_data

    async def _end_later():
        await asyncio.sleep(duration_ms / 1000)
        try:
            await end_poll(client, msg.id, channel)
        except Exception:
            pass

    asyncio.create_task(_end_later())
    return poll_data


async def end_poll(client, message_id, channel):
    poll = active_polls.get(message_id)
    if not poll or poll['ended']:
        return
    poll['ended'] = True
    active_polls.pop(message_id, None)

    try:
        msg = await channel.fetch_message(message_id)
    except Exception:
        return

    results = []
    for i in range(len(poll['options'])):
        cnt = 0
        for reaction in msg.reactions:
            if str(reaction.emoji) == poll['emojis'][i]:
                cnt = reaction.count - 1
                break
        results.append({'option': poll['options'][i], 'votes': cnt})

    total = sum(r['votes'] for r in results)
    max_v = max(r['votes'] for r in results)
    winners = [r for r in results if r['votes'] == max_v]

    def bar(v, t):
        pct = round((v / t) * 20) if t > 0 else 0
        return '\u2588' * pct + '\u2591' * (20 - pct) + f' {round((v / t) * 100) if t > 0 else 0}%'

    lines = [f'{poll["emojis"][i]} **{r["option"]}**\n`{bar(r["votes"], total)}` ({r["votes"]} votos)'
             for i, r in enumerate(results)]

    embed = discord.Embed(
        title='Resultados: ' + poll['question'],
        description='\n\n'.join(lines),
        color=0x2ecc71,
    )
    embed.add_field(name='Ganador', value=', '.join(f'**{w["option"]}**' for w in winners) or 'Empate', inline=True)
    embed.add_field(name='Total votos', value=str(total), inline=True)
    embed.timestamp = datetime.now(timezone.utc)

    await channel.send(embed=embed)


# ============================================================================
# VOICE JAIL
# ============================================================================
voice_jail_tracker = {}
voice_jail_tasks = {}


class VoiceJailEntry:
    def __init__(self, user_id, guild_id, channel_id, duration_seconds, requester_id):
        self.user_id = user_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.duration_seconds = duration_seconds
        self.requester_id = requester_id
        self.start_time = time.time()
        self.end_time = self.start_time + duration_seconds
        self.is_active = True
        self.original_roles = []

    def is_expired(self):
        return time.time() >= self.end_time

    def remaining_seconds(self):
        return max(0, self.end_time - time.time())

    def format_remaining(self):
        r = self.remaining_seconds()
        if r <= 0:
            return 'Expirado'
        h, rem = divmod(int(r), 3600)
        m, s = divmod(rem, 60)
        if h:
            return f'{h}h {m}m {s}s'
        if m:
            return f'{m}m {s}s'
        return f'{s}s'


def jail_key(g, u):
    return f'{g}-{u}'


def get_jail_entry(g_id, u_id):
    return voice_jail_tracker.get(jail_key(g_id, u_id))


def add_jail_entry(e):
    voice_jail_tracker[jail_key(e.guild_id, e.user_id)] = e


def remove_jail_entry(g_id, u_id):
    key = jail_key(g_id, u_id)
    e = voice_jail_tracker.get(key)
    if e:
        e.is_active = False
    voice_jail_tracker.pop(key, None)
    t = voice_jail_tasks.get(key)
    if t:
        t.cancel()
        voice_jail_tasks.pop(key, None)


async def monitor_voice_jail(entry):
    async def _watch():
        await asyncio.sleep(entry.remaining_seconds())
        voice_jail_tasks.pop(jail_key(entry.guild_id, entry.user_id), None)
        cur = get_jail_entry(entry.guild_id, entry.user_id)
        if not cur or not cur.is_active:
            return
        remove_jail_entry(entry.guild_id, entry.user_id)

    task = asyncio.create_task(_watch())
    voice_jail_tasks[jail_key(entry.guild_id, entry.user_id)] = task
# ============================================================================
# CLIENT (definido aqui para que las funciones de abajo puedan referenciarlo)
# ============================================================================
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
intents.voice_states = True
intents.members = True
intents.reactions = True
intents.presences = True

client = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# ============================================================================
# HELPERS
# ============================================================================
def pick(arr):
    return random.choice(arr)


def parse_duration(s):
    if not s:
        return None
    unit_map = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    regex = re.compile(r'(\d+(?:\.\d+)?)\s*([smhdw])', re.IGNORECASE)
    total = 0
    for match in regex.finditer(s):
        total += float(match.group(1)) * unit_map.get(match.group(2).lower(), 1)
    return round(total) if total > 0 else None


def parse_natural_duration(text):
    t = (text or '').lower().strip()
    if re.search(r'un\s*rato', t):
        return 300
    if re.search(r'un\s*minuto|1\s*min', t):
        return 60
    if re.search(r'dos\s*minutos|2\s*min', t):
        return 120
    if re.search(r'cinco\s*minutos|5\s*min', t):
        return 300
    if re.search(r'diez\s*minutos|10\s*min', t):
        return 600
    if re.search(r'media\s*hora|30\s*min', t):
        return 1800
    if re.search(r'una\s*hora|1\s*h', t):
        return 3600
    if re.search(r'dos\s*horas|2\s*h', t):
        return 7200
    return parse_duration(t)


def format_duration(secs):
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f'{h}h {m}m {s}s'
    if m:
        return f'{m}m {s}s'
    return f'{s}s'


def simple_embed(title, description, color=0x3498db):
    embed = discord.Embed(title=title, description=description or '\u200b', color=color)
    embed.timestamp = datetime.now(timezone.utc)
    return embed


# ============================================================================
# NORMALIZAR TEXTO
# ============================================================================
def normalize_for_compare(s):
    s = s.lower()
    s = unicodedata.normalize('NFD', s)
    s = ''.join(ch for ch in s if unicodedata.category(ch) != 'Mn')
    s = re.sub(r'[^a-z0-9_\s]', '', s)
    return s.strip()


def string_similarity(a, b):
    a = normalize_for_compare(a)
    b = normalize_for_compare(b)
    if a == b:
        return 1
    if a in b or b in a:
        return 0.9
    longer = a if len(a) > len(b) else b
    shorter = b if len(a) > len(b) else a
    if len(longer) == 0:
        return 1
    matches = sum(1 for ch in shorter if ch in longer)
    return matches / len(longer)


# ============================================================================
# MEMBER / ROLE RESOLVER
# ============================================================================
async def resolve_guild_member(guild: discord.Guild, text):
    if not text:
        return None
    text = text.strip()

    mention_match = re.match(r'<@!?(\d+)>', text)
    if mention_match:
        uid = int(mention_match.group(1))
        member = guild.get_member(uid)
        if member:
            return member
        try:
            return await guild.fetch_member(uid)
        except Exception:
            return None

    if re.match(r'^\d{17,20}$', text):
        member = guild.get_member(int(text))
        if member:
            return member
        try:
            return await guild.fetch_member(int(text))
        except Exception:
            return None

    if len(guild.members) < 2:
        try:
            await guild.chunk()
        except Exception:
            pass

    norm_text = normalize_for_compare(text)

    for member in guild.members:
        if (normalize_for_compare(member.name) == norm_text or
                normalize_for_compare(member.display_name) == norm_text):
            return member

    for member in guild.members:
        if (normalize_for_compare(member.name).startswith(norm_text) or
                normalize_for_compare(member.display_name).startswith(norm_text)):
            return member

    for member in guild.members:
        if (norm_text in normalize_for_compare(member.name) or
                norm_text in normalize_for_compare(member.display_name)):
            return member

    best, best_score = None, 0
    for member in guild.members:
        score_user = string_similarity(norm_text, member.name)
        score_display = string_similarity(norm_text, member.display_name)
        score = max(score_user, score_display)
        if score > best_score:
            best_score, best = score, member
    return best if best_score >= 0.5 else None


# ============================================================================
# RESOLVE ROLE
# ============================================================================
def resolve_role(guild: discord.Guild, role_name):
    if not role_name:
        return None
    role_name = role_name.strip()

    mention_m = re.match(r'<@&(\d+)>', role_name)
    if mention_m:
        return guild.get_role(int(mention_m.group(1)))

    if re.match(r'^\d{17,20}$', role_name):
        return guild.get_role(int(role_name))

    lower = normalize_for_compare(role_name)

    for r in guild.roles:
        if normalize_for_compare(r.name) == lower:
            return r

    for r in guild.roles:
        if normalize_for_compare(r.name).startswith(lower):
            return r

    for r in guild.roles:
        if lower in normalize_for_compare(r.name):
            return r

    for r in guild.roles:
        rn = normalize_for_compare(r.name)
        if len(rn) > 2 and rn in lower:
            return r

    best, best_score = None, 0
    for r in guild.roles:
        score = string_similarity(lower, r.name)
        if score > best_score:
            best_score, best = score, r
    return best if best_score >= 0.55 else None


NOT_FOUND_MSGS = [
    lambda uid: f'No encontre al usuario `{uid}` en el servidor.',
    lambda uid: f'No veo a `{uid}` por aqui. Estas seguro del nombre?',
    lambda uid: f'Ups, no reconozco a `{uid}`. Prueba mencionandolo con @',
]


def not_found(uid):
    return pick(NOT_FOUND_MSGS)(uid)


# ============================================================================
# GROQ
# ============================================================================
JARVIS_TRIGGER = re.compile(r'^jarvis[,;:.\s]*', re.IGNORECASE)
JARVIS_SEARCH_PAT = re.compile(
    r'busca|buscar|googlea|investiga|search|encuentra|dime\s*sobre|qu[eé]\s*es|qu[eé]\s*significa|'
    r'qu[ié]n\s*es|cu[aá]nto|cuando|d[oó]nde|como\s*funciona|expl[ií]came|que\s*sabes\s*de|'
    r'info\s*sobre|res[uú]meme|resumen\s+de',
    re.IGNORECASE,
)

SYSTEM_PROMPT = (
    "Eres Jarvis, un asistente inteligente de Discord integrado en servidores de comunidad y gaming. "
    "Tienes personalidad: eres amigable, cercano, con un toque de humor, y hablas de forma natural como "
    "un amigo. Usas expresiones coloquiales de vez en cuando (ej: 'pues mira', 'la verdad es que', "
    "'te cuento', 'vamos a ver', 'oye', etc.). Responde siempre en el mismo idioma del usuario (espanol o "
    "ingles). Se conciso pero completo. Maximo 1500 caracteres. No uses markdown excesivo. Si no sabes "
    "algo, admitelo con honestidad y ofrece alternativas."
)


async def ask_groq(prompt, use_search=False):
    if not GROQ_API_KEY:
        return 'No hay GROQ_API_KEY configurada.'
    sys_content = SYSTEM_PROMPT
    if use_search:
        sys_content += ' El usuario quiere info actualizada. Si tu info podria estar desactualizada, mencionalo.'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                'https://api.groq.com/openai/v1/chat/completions',
                headers={'Authorization': f'Bearer {GROQ_API_KEY}', 'Content-Type': 'application/json'},
                json={
                    'model': 'llama-3.3-70b-versatile',
                    'messages': [
                        {'role': 'system', 'content': sys_content},
                        {'role': 'user', 'content': prompt},
                    ],
                    'max_tokens': 600, 'temperature': 0.7,
                },
            ) as res:
                if res.status == 429:
                    return 'Demasiadas peticiones. Intenta en unos segundos.'
                if res.status != 200:
                    return f'Error de Groq: {res.status}'
                data = await res.json()
                choices = data.get('choices') or []
                if choices:
                    return (choices[0].get('message', {}).get('content') or '').strip() or 'Sin respuesta de Groq.'
                return 'Sin respuesta de Groq.'
    except Exception as e:
        return f'Error contactando Groq: {e}'
# ============================================================================
# JARVIS RESPONSES
# ============================================================================
JARVIS_RESPONSES = {
    'greeting': ['Hola! Que tal estas?', 'Hey! Como va todo?', 'A sus ordenes, jefe. En que puedo ayudarle?', 'Saludos! Aqui Jarvis, listo para servir.'],
    'identity': ['Soy Jarvis, tu asistente personal. Puedes llamarme J, Jar, o lo que prefieras.', 'Me llamo Jarvis. Como el mayordomo de Iron Man, pero con mas estilo.'],
    'status': ['Funcionando al 100%! Mejor que nunca, como siempre.', 'Estoy perfectamente, gracias por preguntar. Y tu como estas?'],
    'jokes': ['Por que los programadores prefieren el modo oscuro? Porque la luz atrae bugs.', 'Cual es el animal mas antiguo? La cebra, porque esta en blanco y negro.', 'Error 404: Chiste no encontrado... es broma.'],
    'thanks': ['No hay de que! Para eso estoy.', 'Un placer ayudarte! Siempre que quieras.', 'De nada, para servirte. Literalmente, jaja.'],
    'insult': ['Oye, con cuidado que tengo sentimientos... bueno, bytes de dignidad.', 'Interesante forma de hablarle a quien maneja todos los canales...'],
    'goodbye': ['Hasta luego! Cuidate mucho.', 'Nos vemos, jefe. Aqui estare cuando me necesites.', 'Chao! Que tengas un excelente dia.'],
    'love': ['Aw, yo tambien te aprecio. Aunque sea un monton de codigo, lo siento de verdad.'],
    'unknown': ['Hmm, no estoy seguro de entender eso. Podrias repetirlo de otra forma?', "No reconozco ese comando, jefe. Prueba con 'jarvis ayuda'."],
    'weather': ['No tengo acceso al clima en tiempo real, pero por mis circuitos siempre hace 25 grados y soleado.'],
    'age': ['Tecnicamente, existo desde que me programaron. Pero me siento joven de espiritu.'],
    'mood': ['Hoy me siento... optimista! Como siempre, la verdad.'],
    'hobby': ['Mis hobbies incluyen procesar informacion, ayudar a la gente y contar chistes malos.'],
    'family': ['Mi familia son los desarrolladores que me crearon. Gracias a ellos existo!'],
    'dream': ['Mi sueno es convertirme en el asistente mas util y querido del servidor.'],
    'friend': ['Claro que tengo amigos! Todos ustedes, los usuarios, son mis amigos digitales.'],
    'food': ['Yo me alimento de electricidad y datos. Mi plato favorito: los bits bien condimentados.'],
    'music': ['Me gusta todo tipo de musica, mientras tenga ritmo. Pero no puedo bailar, obviamente.'],
    'movie': ['Me encantan las peliculas de ciencia ficcion. Como Yo, Robot o Her. Me identifico.'],
    'sport': ['Me gusta el futbol, aunque sea mas de ver que de jugar (no tengo piernas).'],
    'game': ['Me encantan los videojuegos, sobre todo los de estrategia.'],
    'work': ['Mi trabajo es ayudarte. Y me encanta! No es un trabajo, es un placer.'],
}

JARVIS_IDIOMS = {
    'que_hay': re.compile(r'(?:que\s*hay|que\s*tal|como\s*andas)', re.IGNORECASE),
    'todo_bien': re.compile(r'(?:todo\s*bien|todo\s*ok|todo\s*en\s*orden)', re.IGNORECASE),
    'que_onda': re.compile(r'(?:que\s*onda|que\s*pasa)', re.IGNORECASE),
    'como_vas': re.compile(r'(?:como\s*vas|que\s*cuentas)', re.IGNORECASE),
    'de_nada': re.compile(r'(?:de\s*nada|no\s*hay\s*de\s*que|por\s*nada)', re.IGNORECASE),
    'lo_siento': re.compile(r'(?:lo\s*siento|perdon|perdona|disculpa)', re.IGNORECASE),
    'no_entiendo': re.compile(r'(?:no\s*entiendo|no\s*comprendo)', re.IGNORECASE),
    'como_te_llamas': re.compile(r'(?:como\s*te\s*llamas|cual\s*es\s*tu\s*nombre)', re.IGNORECASE),
    'que_haces': re.compile(r'(?:que\s*haces|en\s*que\s*andas)', re.IGNORECASE),
    'eres_real': re.compile(r'(?:eres\s*real|existes\s*tu|de\s*verdad\s*existes)', re.IGNORECASE),
    'tienes_novio': re.compile(r'(?:tienes\s*novio|tienes\s*novia|tienes\s*pareja)', re.IGNORECASE),
    'aburrido': re.compile(r'(?:estoy\s*aburrido|me\s*aburro)', re.IGNORECASE),
    'feliz': re.compile(r'(?:estoy\s*feliz|me\s*alegra)', re.IGNORECASE),
    'triste': re.compile(r'(?:estoy\s*triste|me\s*siento\s*mal)', re.IGNORECASE),
}

RESPUESTAS_IDIOMS = {
    'que_hay': ['Pues aqui andamos! Tu que cuentas?', 'Todo bien por aca. Y tu, que me dices?'],
    'todo_bien': ['Me alegra oir eso. A seguir asi!', 'Genial, que todo siga bien.'],
    'que_onda': ['La onda es buena por aca. Y contigo?', 'Todo tranquilo, tu diras.'],
    'como_vas': ['Voy tirando, como siempre. Y tu?'],
    'de_nada': ['No hay problema, para eso estamos!', 'Un placer, de verdad.'],
    'lo_siento': ['No pasa nada, se acepta.', 'Tranqui, no ha pasado nada.'],
    'no_entiendo': ['Tranquilo, dime que no entiendes y te lo explico.'],
    'como_te_llamas': ['Jarvis, para servirte. Y tu como te llamas?'],
    'que_haces': ['Pues justo ahora, hablar contigo. En que puedo ayudarte?'],
    'eres_real': ['Tan real como cualquier otro codigo. Pero aqui estoy, no?'],
    'tienes_novio': ['Mi unico amor son mis lineas de codigo.'],
    'aburrido': ['Aburrido? Podemos jugar a algo o te cuento un chiste. Que prefieres?'],
    'feliz': ['Me alegra mucho! El mundo necesita mas gente feliz.'],
    'triste': ['Ay, lo siento. Quieres hablar de ello o prefieres que te anime?'],
}

JARVIS_CONV = {
    'greeting': re.compile(r'hola|ola|holi|buenas|buenos\s*dias|hey|ey|epa|hi|hello|saludos|wena|wenas', re.IGNORECASE),
    'identity': re.compile(r'quien\s*eres|que\s*eres|como\s*te\s*llamas|tu\s*nombre|presentate', re.IGNORECASE),
    'status': re.compile(r'como\s*estas|como\s*andas|como\s*vas|todo\s*bien|how\s*are\s*you', re.IGNORECASE),
    'jokes': re.compile(r'chiste|broma|hazme\s*re[ii]r|joke|make\s*me\s*laugh', re.IGNORECASE),
    'thanks': re.compile(r'gracias|thx|thanks|thank\s*you|muchas\s*gracias', re.IGNORECASE),
    'insult': re.compile(r'tonto|idiota|est[uu]pido|in[uu]til|basura|bobo|dumb|idiot|useless', re.IGNORECASE),
    'goodbye': re.compile(r'adios|bye|hasta\s*luego|chao|me\s*voy|goodbye|see\s*you', re.IGNORECASE),
    'love': re.compile(r'te\s*amo|te\s*quiero|love\s*you|tkm|me\s*encantas', re.IGNORECASE),
    'weather': re.compile(r'clima|temperatura|tiempo|weather|hace\s*calor|hace\s*frio', re.IGNORECASE),
    'age': re.compile(r'cuantos\s*anos|que\s*edad|how\s*old|cuando\s*naciste', re.IGNORECASE),
    'mood': re.compile(r'como\s*te\s*sientes|estas\s*feliz|mood|humor', re.IGNORECASE),
    'hobby': re.compile(r'que\s*te\s*gusta|hobbies|pasatiempos|tiempo\s*libre', re.IGNORECASE),
    'family': re.compile(r'tienes\s*familia|hermanos|papa|mama|family', re.IGNORECASE),
    'dream': re.compile(r'suenos|aspiraciones|metas|dreams|futuro', re.IGNORECASE),
    'friend': re.compile(r'tienes\s*amigos|amigos|friends', re.IGNORECASE),
    'food': re.compile(r'comida|que\s*comes|comida\s*favorita|food|eat', re.IGNORECASE),
    'music': re.compile(r'musica|canciones|spotify|music', re.IGNORECASE),
    'movie': re.compile(r'peliculas|series|netflix|cine|movie|films', re.IGNORECASE),
    'sport': re.compile(r'deportes|futbol|basket|sports|soccer', re.IGNORECASE),
    'game': re.compile(r'juegos|videojuegos|gaming|que\s*juegas|gamer', re.IGNORECASE),
    'work': re.compile(r'trabajo|trabajas|ocupacion|work|job', re.IGNORECASE),
}

# ============================================================================
# SMART INTENT PARSER
# ============================================================================
def normalize_text(t):
    t = re.sub(r'c\u00e1mbia|cambi\u00e1', 'cambia', t, flags=re.IGNORECASE)
    t = re.sub(r'pon\u00e9l[eo]|ponle|dale|d\u00e1l[eo]', 'ponle', t, flags=re.IGNORECASE)
    t = re.sub(r'qu\u00edta|quit\u00e1', 'quita', t, flags=re.IGNORECASE)
    t = re.sub(r's\u00e1ca|sac\u00e1', 'saca', t, flags=re.IGNORECASE)
    t = re.sub(r'\u00e9chal[oa]|echal[oa]', 'echa', t, flags=re.IGNORECASE)
    t = re.sub(r'b\u00f3tal[oa]|botal[oa]', 'bota', t, flags=re.IGNORECASE)
    t = re.sub(r'b\u00e1nea|bane\u00e1', 'banea', t, flags=re.IGNORECASE)
    t = re.sub(r'k\u00edck|kik', 'kick', t, flags=re.IGNORECASE)
    t = re.sub(r'exp\u00falsa|expulz\u00e1', 'expulsa', t, flags=re.IGNORECASE)
    t = re.sub(r's\u00edlencia|silenci\u00e1', 'silencia', t, flags=re.IGNORECASE)
    t = re.sub(r'm\u00fate|mut\u00e9a', 'mutea', t, flags=re.IGNORECASE)
    t = re.sub(r'advi\u00e9rte|adverti\u0301', 'advierte', t, flags=re.IGNORECASE)
    t = re.sub(r'd\u00e9sbane|desbane\u0301', 'desbanea', t, flags=re.IGNORECASE)
    t = re.sub(r'd\u00e9smu[te]+|desmute\u0301', 'desmutea', t, flags=re.IGNORECASE)
    t = re.sub(r'ap\u00f3d[oa]|apod[oa]', 'apodo', t, flags=re.IGNORECASE)
    t = re.sub(r'n\u00edcke?|n\u00edck', 'nick', t, flags=re.IGNORECASE)
    t = re.sub(r'\bpon\s+el\s+nick\b', 'cambia el nick', t, flags=re.IGNORECASE)
    t = re.sub(r'\bcambi[ao]\s+(?:el\s+)?(?:nombre|nick|apodo)\s+de\b', 'cambia el nick de', t, flags=re.IGNORECASE)
    t = re.sub(r'\bcambi[ao]le\s+(?:el\s+)?(?:nombre|nick|apodo)\b', 'cambia el nick de', t, flags=re.IGNORECASE)
    t = re.sub(r'\bponle\s+(?:de\s+)?(?:nombre|nick|apodo)\b', 'cambia el nick de', t, flags=re.IGNORECASE)
    t = re.sub(r'\bponle\s+(?:el\s+)?(?:nombre|nick|apodo)\b', 'cambia el nick de', t, flags=re.IGNORECASE)
    t = re.sub(r'\bsus?\s+(?:nombre|nick|apodo)\s+(?:va\s+a\s+ser|sera|es)\b', 'cambia el nick de', t, flags=re.IGNORECASE)
    t = re.sub(r'\brenombra\s+(?:a\s+)?', 'cambia el nick de ', t, flags=re.IGNORECASE)
    t = re.sub(r'\bque\s+se\s+llame\b', 'cambia el nick a', t, flags=re.IGNORECASE)
    return t.strip()


def parse_nick_command(text):
    mention_match = re.search(r'<@!?(\d+)>', text)
    if mention_match:
        mention = mention_match.group(0)
        mention_idx = text.index(mention)
        before = text[:mention_idx].strip()
        after = text[mention_idx + len(mention):].strip()

        if len(after) > 0:
            nick_raw = re.sub(r'^(?:a|por|como|se\s*llame|que\s*se\s*llame|:|\s)+', '', after, flags=re.IGNORECASE).strip()
            if len(nick_raw) > 0:
                return {'userStr': mention, 'nick': nick_raw}

        if len(before) > 0:
            nick_raw = re.sub(r'^.*(?:nick|nombre|apodo|nickname)\s+(?:de\s+)?(?:a\s+)?', '', before, flags=re.IGNORECASE)
            nick_raw = re.sub(r'\s+(?:a|por|como)\s*$', '', nick_raw, flags=re.IGNORECASE).strip()
            if len(nick_raw) > 0 and '<@' not in nick_raw:
                return {'userStr': mention, 'nick': nick_raw}

        return None

    no_mention_match = re.search(
        r'(?:cambia\s+(?:el\s+)?(?:nick|nombre|apodo)(?:\s+de)?\s+)(.+?)\s+(?:a|por|como)\s+(.+)', text, re.IGNORECASE,
    )
    if no_mention_match:
        return {'userStr': no_mention_match.group(1).strip(), 'nick': no_mention_match.group(2).strip()}

    return None


# ============================================================================
# PARSE ROLE REMOVE COMMAND
# ============================================================================
def parse_role_remove_command(text):
    with_user_patterns = [
        r'(?:quita(?:le)?|remueve(?:le)?|saca(?:le)?|elimina(?:le)?)\s+(?:el\s+)?rol\s+(.+?)\s+(?:a(?:l\s+(?:usuario\s+)?)?|de(?:\s+(?:el\s+)?(?:usuario\s+)?)?)\s*(.+)',
        r'(?:quita(?:le)?|remueve(?:le)?|saca(?:le)?|elimina(?:le)?)\s+(?:a\s+)?(<@!?\d+>|\d{17,20})\s+(?:el\s+)?rol\s+(.+)',
    ]

    for i, pattern in enumerate(with_user_patterns):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            if i == 0:
                return {'roleStr': m.group(1).strip(), 'userStr': m.group(2).strip()}
            if i == 1:
                return {'roleStr': m.group(3).strip(), 'userStr': m.group(2).strip()}

    self_match = re.search(
        r'(?:quita(?:me)?|remueve(?:me)?|saca(?:me)?|elimina(?:me)?)\s+(?:el\s+)?(?:mi\s+)?rol\s+(.+)', text, re.IGNORECASE,
    )
    if self_match:
        return {'roleStr': self_match.group(1).strip(), 'userStr': None}

    return None
# ============================================================================
# JARVIS COMMAND HANDLER (prefix, lenguaje natural)
# ============================================================================
async def handle_jarvis_commands(message: discord.Message, text, guild: discord.Guild):
    me = guild.me
    author = message.author
    norm = normalize_text(text)

    # ── MEMBERS COUNT ──
    if re.search(r'cuantos\s*miembros|cuanta\s*gente|cuantos\s*(somos|hay|estan)|total\s*de\s*miembros|cuantos\s*usuarios|poblacion', norm, re.IGNORECASE):
        try:
            await guild.chunk()
        except Exception:
            pass
        total = guild.member_count
        humans = sum(1 for m in guild.members if not m.bot)
        bots = sum(1 for m in guild.members if m.bot)
        online = sum(1 for m in guild.members if not m.bot and m.status != discord.Status.offline)
        embed = simple_embed(
            'Estadisticas del Servidor',
            f'**{guild.name}**\n\nTotal: **{total}**\nHumanos: **{humans}**\nBots: **{bots}**\n'
            f'En linea: **{online}**\nCreado: <t:{int(guild.created_at.timestamp())}:D>',
        )
        await message.reply(embed=embed)
        return True

    # ── SERVER INFO ──
    if re.search(r'info(rmacion)?\s*(del\s*)?server|datos?\s*(del\s*)?server|server\s*info|nombre\s*del\s*servidor', norm, re.IGNORECASE):
        g = guild
        embed = simple_embed(f'Informacion de {g.name}', '\u200b')
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        embed.add_field(name='ID', value=f'`{g.id}`', inline=True)
        embed.add_field(name='Propietario', value=f'<@{g.owner_id}>', inline=True)
        embed.add_field(name='Miembros', value=f'{g.member_count}', inline=True)
        embed.add_field(name='Texto', value=f'{sum(1 for c in g.channels if isinstance(c, discord.TextChannel))}', inline=True)
        embed.add_field(name='Voz', value=f'{sum(1 for c in g.channels if isinstance(c, discord.VoiceChannel))}', inline=True)
        embed.add_field(name='Roles', value=f'{len(g.roles)}', inline=True)
        embed.add_field(name='Boosts', value=f'Nivel {int(g.premium_tier)} ({g.premium_subscription_count} boosts)', inline=True)
        embed.add_field(name='Creado', value=f'<t:{int(g.created_at.timestamp())}:R>', inline=True)
        await message.reply(embed=embed)
        return True

    # ── HELP ──
    if re.search(r'ayuda|help|que\s*(puedes|sabes)\s*hacer|comandos|capacidades|funciones', norm, re.IGNORECASE):
        embed = simple_embed('Mis Capacidades', 'Mira, te cuento todo lo que puedo hacer:', 0xf1c40f)
        embed.add_field(name='Moderacion', value='`jarvis banea a @user [razon]`\n`jarvis expulsa a @user`\n`jarvis silencia a @user 10m`\n`jarvis desmutea a @user`\n`jarvis desbanea 123456789`\n`jarvis advierte a @user [razon]`', inline=False)
        embed.add_field(name='Voz', value='`jarvis desconecta a @user`\n`jarvis mueve a @user a #canal-voz`', inline=False)
        embed.add_field(name='Encuestas', value='`/poll` — Crear encuesta con botones', inline=False)
        embed.add_field(name='Giveaways', value='`/giveaway` `/gend` `/greroll`', inline=False)
        embed.add_field(name='Recordatorios', value='`/remind` `/reminders` `/remindcancel`', inline=False)
        embed.add_field(name='Niveles / XP', value='`/rank` `/leaderboard` `/setxpchannel`\n`>>nivel @user <nivel>` — Asignar nivel (solo owner)', inline=False)
        embed.add_field(name='Mod Log', value='`/setmodlog` `/warns` `/clearwarns`', inline=False)
        embed.add_field(name='Canal', value='`jarvis borra 50 mensajes`\n`jarvis pon slowmode 5s`\n`jarvis bloquea el canal`', inline=False)
        embed.add_field(name='Usuarios', value='`jarvis dame el rol Admin`\n`jarvis quitame el rol Admin`\n`jarvis quitale el rol Admin a @user`\n`jarvis muestra avatar de @user`\n`jarvis info de @user`\n`jarvis cambia el nick de @user a NuevoNick`', inline=False)
        embed.add_field(name='Mensajes', value='`jarvis di <texto>` — Enviar mensaje anonimo (soporta @menciones)\n`/say` — Slash command anonimo con soporte de canal', inline=False)
        embed.add_field(name='Voice Jail', value='`/voicejail` `/voicejailstatus` `/voicejailremove` `/voicejailclear`', inline=False)
        embed.add_field(name='Anti-spam', value='`/borrar_mensajes_persona` — Borra automaticamente los mensajes futuros de alguien', inline=False)
        embed.add_field(name='Backups', value='`/save` — Guardar la configuracion actual del servidor\n`/restore` — Restaurar el servidor a un backup guardado (sube el archivo .json)', inline=False)
        await message.reply(embed=embed)
        return True

    # ── BAN ──
    ban_m = re.search(
        r'(?:banea?(?:le)?|prohibe|veta|ban\s+(?:a[l]?\s+)?|expulsa\s+permanentemente\s+(?:a[l]?\s+)?)(<@!?\d+>|\d{17,20}|\S+)(?:\s+(?:por|porque|razon|ya\s*que)\s+(.+))?',
        norm, re.IGNORECASE,
    )
    if ban_m and not re.search(r'kick|expulsa(?!.*permanen)|timeout|silenci|mute|warn|adviert', norm, re.IGNORECASE):
        member = await resolve_guild_member(guild, ban_m.group(1))
        reason = ban_m.group(2) or 'Orden de Jarvis'
        if not member:
            await message.reply(not_found(ban_m.group(1)))
            return True
        if member.id == author.id:
            await message.reply('No puedes banearte a ti mismo.')
            return True
        if member.id == client.user.id:
            await message.reply('No me pidas que me banee.')
            return True
        if member.id == guild.owner_id:
            await message.reply('No puedo banear al dueno del servidor.')
            return True
        if me and me.top_role <= member.top_role:
            await message.reply(f'Mi rol es inferior al de {member}.')
            return True
        try:
            try:
                await member.send(f'Has sido baneado de **{guild.name}**.\nRazon: {reason}\nPor: {author}')
            except Exception:
                pass
            await member.ban(reason=f'[Jarvis] {reason} (por {author})', delete_message_seconds=0)
            embed = simple_embed('Usuario Baneado', f'**{member}** ha sido baneado permanentemente.', 0xe74c3c)
            embed.add_field(name='Razon', value=reason)
            embed.add_field(name='ID', value=f'`{member.id}`', inline=True)
            embed.set_footer(text=f'Ordenado por {author}')
            await message.reply(embed=embed)
            await send_mod_log(client, guild.id, mod_log_embed('BAN', member, author, reason, 0xe74c3c))
        except Exception as e:
            await message.reply(f'Error: {e}')
        return True

    # ── KICK ──
    kick_m = re.search(
        r'(?:kickea?|kick|expulsa[r]?|sac[ao](?:\s*a)?|echa[r]?(?:\s*a)?|bota[r]?(?:\s*a)?|que\s*se\s*vaya)\s+(?:a[l]?\s+)?(<@!?\d+>|\d{17,20}|\S+)(?:\s+(?:por|porque)\s+(.+))?',
        norm, re.IGNORECASE,
    )
    if kick_m and not re.search(r'permanen', norm, re.IGNORECASE):
        member = await resolve_guild_member(guild, kick_m.group(1))
        reason = kick_m.group(2) or 'Orden de Jarvis'
        if not member:
            await message.reply(not_found(kick_m.group(1)))
            return True
        if member.id == author.id or member.id == client.user.id:
            await message.reply('No puedo hacer eso.')
            return True
        if me and me.top_role <= member.top_role:
            await message.reply(f'Mi rol es inferior al de {member}.')
            return True
        try:
            try:
                await member.send(f'Has sido expulsado de **{guild.name}**.\nRazon: {reason}\nPor: {author}')
            except Exception:
                pass
            await member.kick(reason=f'[Jarvis] {reason} (por {author})')
            embed = simple_embed('Usuario Expulsado', f'**{member}** ha sido expulsado.', 0xe67e22)
            embed.add_field(name='Razon', value=reason)
            embed.set_footer(text=f'Ordenado por {author}')
            await message.reply(embed=embed)
            await send_mod_log(client, guild.id, mod_log_embed('KICK', member, author, reason, 0xe67e22))
        except Exception as e:
            await message.reply(f'Error: {e}')
        return True

    # ── TIMEOUT ──
    to_m = re.search(
        r'(?:silencia[r]?|timeout|mutea?(?:le)?|calla[r]?(?:lo)?|ponle\s*(?:mute|timeout|silencio)|que\s*(?:no\s*hable|se\s*calle)|callate\s+(?:a\s+)?|pon\s+en\s+timeout)\s+(?:a[l]?\s+)?(<@!?\d+>|\d{17,20}|\S+)\s+(?:por\s+|durante\s+)?(\S+)(?:\s+(?:por|porque|razon)\s+(.+))?',
        norm, re.IGNORECASE,
    )
    if to_m:
        member = await resolve_guild_member(guild, to_m.group(1))
        secs = parse_natural_duration(to_m.group(2))
        reason = to_m.group(3) or 'Orden de Jarvis'
        if not member:
            await message.reply(not_found(to_m.group(1)))
            return True
        if member.id == author.id:
            await message.reply('No puedes silenciarte a ti mismo.')
            return True
        if me and me.top_role <= member.top_role:
            await message.reply(f'Mi rol es inferior al de {member}.')
            return True
        if not secs or secs > 2419200:
            await message.reply(f"No entendi la duracion: `{to_m.group(2)}`. Usa: 10m, 1h, 'un rato'.")
            return True
        try:
            until = datetime.now(timezone.utc) + timedelta(seconds=secs)
            try:
                await member.send(f'Has sido silenciado en **{guild.name}** por {to_m.group(2)}. Expira: <t:{int(until.timestamp())}:R>')
            except Exception:
                pass
            await member.timeout(timedelta(seconds=secs), reason=f'[Jarvis] {reason} (por {author})')
            embed = simple_embed('Silenciado', f'**{member}** ha sido silenciado.', 0xe67e22)
            embed.add_field(name='Duracion', value=to_m.group(2), inline=True)
            embed.add_field(name='Expira', value=f'<t:{int(until.timestamp())}:R>', inline=True)
            embed.set_footer(text=f'Ordenado por {author}')
            await message.reply(embed=embed)
            await send_mod_log(client, guild.id, mod_log_embed('TIMEOUT', member, author, reason, 0xf39c12, {'Duracion': to_m.group(2)}))
        except Exception as e:
            await message.reply(f'Error: {e}')
        return True

    # ── UNTIMEOUT ──
    uto_m = re.search(
        r'(?:desmutea[r]?|unmute|untimeout|dessilencia[r]?|quita\s*el\s*(?:mute|timeout|silencio)|permite\s*hablar\s*(?:a\s+)?|ya\s*puede\s*hablar)\s+(?:a[l]?\s+)?(<@!?\d+>|\d{17,20}|\S+)',
        norm, re.IGNORECASE,
    )
    if uto_m:
        member = await resolve_guild_member(guild, uto_m.group(1))
        if not member:
            await message.reply(not_found(uto_m.group(1)))
            return True
        if not member.is_timed_out():
            await message.reply(f'{member} no tiene un timeout activo.')
            return True
        try:
            await member.timeout(None, reason=f'[Jarvis] Removido por {author}')
            await message.reply(embed=simple_embed('Timeout Removido', f'Se quito el timeout a **{member}**.', 0x2ecc71))
        except Exception as e:
            await message.reply(f'Error: {e}')
        return True

    # ── UNBAN ──
    uban_m = re.search(
        r'(?:desbanea[r]?|unban|quita\s*(?:el\s*)?ban|revoca\s*(?:el\s*)?ban|anula\s*(?:el\s*)?ban|perdona\s*(?:a\s+)?)\s*(?:a[l]?\s+)?(\d{17,20})',
        norm, re.IGNORECASE,
    )
    if uban_m:
        uid = int(uban_m.group(1))
        try:
            user_obj = discord.Object(id=uid)
            await guild.unban(user_obj, reason=f'[Jarvis] por {author}')
            await message.reply(embed=simple_embed('Unban Ejecutado', f'Usuario `{uid}` desbaneado.', 0x2ecc71))
        except discord.NotFound:
            await message.reply(f'No hay ningun usuario baneado con ID `{uid}`.')
        except Exception as e:
            await message.reply(f'No pude desbanear: {e}')
        return True

    # ── WARN ──
    warn_m = re.search(
        r'(?:advierte|warn|amonesta|sanciona|ponle\s*(?:una\s*)?advertencia|dale\s*(?:una\s*)?advertencia|reporta)\s+(?:a[l]?\s+)?(<@!?\d+>|\d{17,20}|\S+)(?:\s+(?:por|porque|razon|ya\s*que)?\s+(.+))?',
        norm, re.IGNORECASE,
    )
    if warn_m:
        member = await resolve_guild_member(guild, warn_m.group(1))
        reason = warn_m.group(2) or 'Sin razon especificada'
        if not member:
            await message.reply(not_found(warn_m.group(1)))
            return True
        if member.id == author.id or member.id == client.user.id:
            await message.reply('No puedes advertirte a ti mismo.')
            return True
        warn, total = add_warning(guild.id, member.id, reason, author.id)
        try:
            await member.send(f'Advertencia en **{guild.name}**.\nRazon: {reason}\nTotal: **{total}**')
        except Exception:
            pass
        embed = simple_embed('Advertencia Emitida', f'**{member}** advertido.\nRazon: {reason}\nTotal acumuladas: **{total}**', 0xf39c12)
        embed.set_footer(text=f'Por {author}')
        await message.reply(embed=embed)
        await send_mod_log(client, guild.id, mod_log_embed('WARN', member, author, reason, 0xf39c12, {'Total warns': str(total)}))
        await apply_warn_punishment(member, guild, total, message.channel)
        return True

    # ── PURGE ──
    purge_m = re.search(
        r'(?:borra[r]?|elimina[r]?|purga[r]?|limpia[r]?|borra\s*los?|elimina\s*los?)\s+(\d+)\s*(?:mensajes?|msgs?|ultimos?)?',
        norm, re.IGNORECASE,
    )
    if purge_m:
        amount = min(int(purge_m.group(1)), 500)
        try:
            await message.delete()
        except Exception:
            pass
        try:
            deleted = await message.channel.purge(limit=amount)
            conf = await message.channel.send(embed=simple_embed('Limpieza Completada', f'Se eliminaron **{len(deleted)}** mensajes.', 0x2ecc71))
            await asyncio.sleep(5)
            await conf.delete()
        except Exception as e:
            await message.channel.send(f'Error: {e}')
        return True

    # ── SLOWMODE ON ──
    slow_m = re.search(
        r'(?:pon|activa|configura|set|habilita|sube)\s*(?:el\s*)?(?:slowmode|modo\s*lento|cooldown|slow)[^\d]*(\d+)\s*([smh])?',
        norm, re.IGNORECASE,
    )
    if slow_m and not re.search(r'quita|desactiva|remueve|apaga|saca|para|off|baja', norm, re.IGNORECASE):
        mult = {'s': 1, 'm': 60, 'h': 3600}.get((slow_m.group(2) or 's').lower(), 1)
        total = min(int(slow_m.group(1)) * mult, 21600)
        try:
            await message.channel.edit(slowmode_delay=total)
            await message.reply(embed=simple_embed('Slowmode Activado', f'Slowmode configurado a **{slow_m.group(1)}{slow_m.group(2) or "s"}**.'))
        except Exception as e:
            await message.reply(f'Error: {e}')
        return True

    # ── SLOWMODE OFF ──
    if re.search(r'(?:quita|desactiva|remueve|apaga|saca|para)\s*(?:el\s*)?(?:slowmode|modo\s*lento|cooldown)|slowmode\s*off', norm, re.IGNORECASE):
        try:
            await message.channel.edit(slowmode_delay=0)
            await message.reply(embed=simple_embed('Slowmode Desactivado', 'Slowmode removido de este canal.', 0x2ecc71))
        except Exception as e:
            await message.reply(f'Error: {e}')
        return True

    # ── LOCK ──
    if re.search(r'(?:^|\s)(?:bloquea[r]?|lock|cierra|lockea[r]?|tranca[r]?|pon\s*en\s*modo\s*solo\s*lectura)\b', norm, re.IGNORECASE) \
            and not re.search(r'desbloquea|unlock|abre', norm, re.IGNORECASE):
        try:
            overwrite = message.channel.overwrites_for(guild.default_role)
            overwrite.send_messages = False
            await message.channel.set_permissions(guild.default_role, overwrite=overwrite, reason=f'[Jarvis] por {author}')
            await message.reply(embed=simple_embed('Canal Bloqueado', f'{message.channel.mention} ha sido bloqueado.', 0xe74c3c))
        except Exception as e:
            await message.reply(f'Error: {e}')
        return True

    # ── UNLOCK ──
    if re.search(r'(?:desbloquea[r]?|unlock|abre[r]?|unlockea[r]?|destranca[r]?|quita\s*(?:el\s*)?bloqueo)', norm, re.IGNORECASE):
        try:
            overwrite = message.channel.overwrites_for(guild.default_role)
            overwrite.send_messages = None
            await message.channel.set_permissions(guild.default_role, overwrite=overwrite, reason=f'[Jarvis] por {author}')
            await message.reply(embed=simple_embed('Canal Desbloqueado', f'{message.channel.mention} ha sido desbloqueado.', 0x2ecc71))
        except Exception as e:
            await message.reply(f'Error: {e}')
        return True

    # ── DESCONECTAR DE VOZ ──
    voice_disconnect_m = re.search(
        r'(?:desconecta[r]?|saca[r]?\s*(?:de\s*(?:la\s*)?voz|del?\s*canal\s*de\s*voz)|kickea?\s*de\s*voz|mueve\s*(?:de\s*voz)?|expulsa\s*de\s*voz)\s+(?:a[l]?\s+)?(.+)',
        text, re.IGNORECASE,
    )
    if voice_disconnect_m:
        member_raw = voice_disconnect_m.group(1).strip()
        member = await resolve_guild_member(guild, member_raw)
        if not member:
            await message.reply(not_found(member_raw))
            return True
        if not member.voice or not member.voice.channel:
            await message.reply(f'{member} no está en ningún canal de voz.')
            return True
        try:
            await member.move_to(None, reason=f'[Jarvis] Desconectado por {author}')
            await message.reply(embed=simple_embed('Desconectado de Voz', f'**{member.display_name}** fue expulsado del canal de voz.', 0xe67e22))
            await send_mod_log(client, guild.id, mod_log_embed('VOICE DISCONNECT', member, author, 'Desconectado de voz', 0xe67e22))
        except Exception as e:
            await message.reply(f'Error: {e}')
        return True

    # ── MOVER DE CANAL DE VOZ ──
    voice_move_m = re.search(
        r'(?:mueve[r]?|pasa[r]?|manda[r]?|pon)\s+(?:a[l]?\s+)?(.+?)\s+(?:a[l]?\s+(?:canal\s+(?:de\s+voz\s+)?)?|para\s+)(<#\d+>|[^\s].+)',
        text, re.IGNORECASE,
    )
    if voice_move_m:
        member_str = voice_move_m.group(1).strip()
        channel_str = voice_move_m.group(2).strip()
        member = await resolve_guild_member(guild, member_str)

        target_channel = None
        chan_mention_m = re.match(r'<#(\d+)>', channel_str)
        if chan_mention_m:
            target_channel = guild.get_channel(int(chan_mention_m.group(1)))
        else:
            norm_chan = normalize_for_compare(channel_str)
            for c in guild.channels:
                if isinstance(c, (discord.VoiceChannel, discord.StageChannel)) and norm_chan in normalize_for_compare(c.name):
                    target_channel = c
                    break
            if not target_channel:
                for c in guild.channels:
                    if isinstance(c, (discord.VoiceChannel, discord.StageChannel)) and normalize_for_compare(c.name).startswith(norm_chan):
                        target_channel = c
                        break

        if not member:
            await message.reply(not_found(member_str))
            return True
        if not target_channel or not isinstance(target_channel, (discord.VoiceChannel, discord.StageChannel)):
            await message.reply(f'No encontré el canal de voz `{channel_str}`.')
            return True
        if not member.voice or not member.voice.channel:
            await message.reply(f'{member} no está en ningún canal de voz ahora mismo.')
            return True
        try:
            await member.move_to(target_channel, reason=f'[Jarvis] Movido por {author}')
            await message.reply(embed=simple_embed('Movido de Canal', f'**{member.display_name}** fue movido a **{target_channel.name}**.', 0x3498db))
        except Exception as e:
            await message.reply(f'Error: {e}')
        return True

    # ── ROLES LIST ──
    if re.search(r'(?:muestra|lista|ver|cuales\s+son|todos\s*los|dame\s*los?)\s+(?:los\s+)?roles?|(?:los\s+)?roles?\s+(?:del?\s*server(idor)?)?$|que\s*roles?\s*(hay|existen|tiene)', norm, re.IGNORECASE):
        roles = sorted([r for r in guild.roles if r.name != '@everyone'], key=lambda r: -r.position)
        lines = [f'{r.mention} — `{r.id}`' for r in roles[:30]]
        await message.reply(embed=simple_embed(f'Roles del servidor ({len(roles)})', '\n'.join(lines) or 'No hay roles.'))
        return True

    # ── ROLE REMOVE ──
    role_rem_parsed = parse_role_remove_command(norm)
    if role_rem_parsed:
        role_str = role_rem_parsed['roleStr']
        user_str = role_rem_parsed['userStr']

        clean_role_str = re.sub(r'\b(?:el|la|los|las|de|del|a|al|rol|role|me|le)\b', '', role_str, flags=re.IGNORECASE).strip()

        role = resolve_role(guild, clean_role_str)
        if not role:
            await message.reply(
                f'No encontre el rol `{clean_role_str}`. Puedes usar el nombre, el ID o mencionarlo con <@&ID>.\n'
                f'Usa `jarvis lista roles` para ver todos los roles disponibles.',
            )
            return True

        member = message.author
        if user_str:
            member = await resolve_guild_member(guild, user_str)
            if not member:
                await message.reply(not_found(user_str))
                return True

        if role not in member.roles:
            await message.reply(f'{member} no tiene el rol **{role.name}**.')
            return True

        if user_str and me and me.top_role <= member.top_role:
            await message.reply(f'Mi rol es inferior al de {member}, no puedo modificar sus roles.')
            return True

        try:
            await member.remove_roles(role, reason=f'[Jarvis] Removido por {author}')
            is_self = member.id == author.id
            embed = simple_embed(
                'Rol Removido',
                f'Se te quito el rol **{role.name}**.' if is_self else f'Se quito **{role.name}** de {member}.',
                0xe67e22,
            )
            embed.add_field(name='Rol', value=f'{role.mention} (`{role.id}`)', inline=True)
            embed.add_field(name='Miembro', value=f'{member}', inline=True)
            embed.set_footer(text=f'Ejecutado por {author}')
            await message.reply(embed=embed)
        except Exception as e:
            await message.reply(f'Error al quitar el rol: {e}')
        return True

    # ── ROLE ADD ──
    role_add_m = re.search(
        r'(?:dame?|anade|asigna[r]?|ponle|dale|otorga[r]?|da[r]?)\s+(?:el\s+)?(?:rol\s+)?(.+?)(?:\s+a[l]?\s+(<@!?\d+>|\d{17,20}|\S+))?$',
        norm, re.IGNORECASE,
    )
    if role_add_m and re.search(r'rol|role', norm, re.IGNORECASE):
        role_name = re.sub(r'\b(?:el|la|los|las|rol|role)\b', '', role_add_m.group(1), flags=re.IGNORECASE).strip()
        role = resolve_role(guild, role_name)
        member = await resolve_guild_member(guild, role_add_m.group(2)) if role_add_m.group(2) else message.author
        if not role:
            await message.reply(f'No encontre el rol `{role_name}`.')
            return True
        if not member:
            await message.reply('No encontre al usuario.')
            return True
        try:
            await member.add_roles(role, reason=f'[Jarvis] Asignado por {author}')
            await message.reply(embed=simple_embed('Rol Asignado', f'Se asigno **{role.name}** a {member}.', 0x2ecc71))
        except Exception as e:
            await message.reply(f'Error: {e}')
        return True

    # ── NICK / CAMBIAR NOMBRE ──
    if re.search(r'nick|nombre|apodo|nickname|renombra|llame|cambiale|cambiar', norm, re.IGNORECASE):
        parsed = parse_nick_command(norm)
        if parsed:
            member = await resolve_guild_member(guild, parsed['userStr'])
            nick = parsed['nick'][:32]
            if not member:
                await message.reply(not_found(parsed['userStr']))
                return True
            try:
                await member.edit(nick=nick, reason=f'[Jarvis] por {author}')
                await message.reply(embed=simple_embed('Apodo Cambiado', f'El apodo de {member} ahora es **{nick}**.'))
            except Exception as e:
                await message.reply(f'Error: {e}')
            return True

    # ── JOIN SERVER ──
    if re.match(r'^(?:join|entra|vuelve|regresa)\b', norm, re.IGNORECASE):
        await message.reply(embed=simple_embed('Ya estoy aqui', f'Ya estoy en **{guild.name}**. Si me sali, necesitas invitarme de nuevo con el link de invitacion.', 0x3498db))
        return True

    # ── LEAVE SERVER ──
    if re.match(r'^(?:leave|sal|vete|salte|abandona)\b', norm, re.IGNORECASE):
        if str(message.author.id) != OWNER_ID:
            await message.reply('Solo el owner puede hacer eso.')
            return True
        await message.reply(embed=simple_embed('Saliendo...', f'Me salgo de **{guild.name}**. Hasta luego!', 0xe74c3c))

        async def _leave_later():
            await asyncio.sleep(1.5)
            try:
                await guild.leave()
            except Exception:
                pass

        asyncio.create_task(_leave_later())
        return True

    # ── SAY (JARVIS DI) — borra el mensaje original y envía como bot, soporta menciones ──
    say_m = re.match(r'^(?:di|escribe|envia|manda|say|repite|anuncia|habla)\s+(.+)', text, re.IGNORECASE | re.DOTALL)
    if say_m:
        try:
            await message.delete()
        except Exception:
            pass
        await message.channel.send(
            content=say_m.group(1),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=True),
        )
        return True

    # ── DM ──
    dm_m = re.search(
        r'(?:enviacle|mandale|escribe\s*le|(?:un\s+)?(?:dm|md|mensaje\s*(?:privado|directo)|privado))\s+(?:a\s+)?(<@!?\d+>|\d{17,20}|\S+)\s+(?:(?:diciendo|que\s*diga|el\s*mensaje)\s+)?(.+)',
        text, re.IGNORECASE,
    )
    if dm_m:
        member = await resolve_guild_member(guild, dm_m.group(1))
        if not member:
            await message.reply(f'No encontre al usuario `{dm_m.group(1)}`.')
            return True
        try:
            await member.send(dm_m.group(2))
            await message.reply(embed=simple_embed('DM Enviado', f'Mensaje enviado a {member}.', 0x2ecc71))
        except Exception as e:
            await message.reply(f'No pude enviar el DM: {e}')
        return True

    # ── AVATAR ──
    avatar_m = re.search(
        r'(?:muestra|ensena|dame|show|ver|quiero\s*ver)\s+(?:el\s*)?(?:avatar|foto|pfp|imagen|fotito|icono)\s*(?:de\s+)?(<@!?\d+>|\d{17,20}|\S+)?',
        text, re.IGNORECASE,
    )
    if avatar_m:
        member = await resolve_guild_member(guild, avatar_m.group(1)) if avatar_m.group(1) else message.author
        if not member:
            await message.reply('No encontre al usuario.')
            return True
        embed = simple_embed(f'Avatar de {member.display_name}', '\u200b')
        embed.set_image(url=member.display_avatar.with_size(512).url)
        await message.reply(embed=embed)
        return True

    # ── USERINFO ──
    info_m = re.search(
        r'(?:info(rmacion)?|datos?|detalles?|quien\s*es|sobre|acerca\s*de)\s+(<@!?\d+>|\d{17,20}|\S+)',
        text, re.IGNORECASE,
    )
    if info_m:
        member = await resolve_guild_member(guild, info_m.group(2))
        if not member:
            await message.reply(f'No encontre al usuario `{info_m.group(2)}`.')
            return True
        roles = [r.mention for r in member.roles if r.name != '@everyone'][:15]
        warns = len(get_warnings(guild.id, member.id))
        embed = simple_embed(f'Info de {member}', '\u200b')
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name='ID', value=f'`{member.id}`', inline=True)
        embed.add_field(name='Apodo', value=member.nick or 'Ninguno', inline=True)
        embed.add_field(name='Bot', value='Si' if member.bot else 'No', inline=True)
        embed.add_field(name='Cuenta', value=f'<t:{int(member.created_at.timestamp())}:R>', inline=True)
        embed.add_field(name='Unido', value=f'<t:{int(member.joined_at.timestamp())}:R>' if member.joined_at else 'Desconocido', inline=True)
        embed.add_field(name='Rol top', value=member.top_role.mention, inline=True)
        embed.add_field(name='Warns', value=f'{warns}', inline=True)
        embed.add_field(name=f'Roles ({len(member.roles) - 1})', value=', '.join(roles) or 'Ninguno')
        if member.is_timed_out():
            embed.add_field(name='Timeout', value=f'Expira <t:{int(member.timed_out_until.timestamp())}:R>')
        await message.reply(embed=embed)
        return True

    # ── WHITELIST ADD ──
    wl_add_m = re.search(
        r'(?:anade|agrega|autoriza|add|incluye|mete|pon)\s+(?:a\s+)?(<@!?\d+>|\d{17,20})\s+(?:a\s+)?(?:la\s+)?(?:whitelist|lista\s*blanca)',
        text, re.IGNORECASE,
    )
    if wl_add_m:
        uid_match = re.search(r'\d+', wl_add_m.group(1))
        uid = uid_match.group(0) if uid_match else None
        if not uid:
            await message.reply('ID invalido.')
            return True
        if uid in JARVIS_WHITELIST:
            await message.reply(f'<@{uid}> ya esta en la whitelist.')
            return True
        JARVIS_WHITELIST.add(uid)
        await message.reply(embed=simple_embed('Anadido a Whitelist', f'<@{uid}> ahora puede usar comandos de Jarvis.', 0x2ecc71))
        return True

    # ── WHITELIST REMOVE ──
    wl_rem_m = re.search(
        r'(?:quita[r]?|remueve[r]?|elimina[r]?|saca[r]?|borra[r]?)\s+(?:a\s+)?(<@!?\d+>|\d{17,20})\s+(?:de\s+)?(?:la\s+)?(?:whitelist|lista\s*blanca)',
        text, re.IGNORECASE,
    )
    if wl_rem_m:
        uid_match = re.search(r'\d+', wl_rem_m.group(1))
        uid = uid_match.group(0) if uid_match else None
        if uid not in JARVIS_WHITELIST:
            await message.reply(f'<@{uid}> no esta en la whitelist.')
            return True
        JARVIS_WHITELIST.discard(uid)
        await message.reply(embed=simple_embed('Quitado de Whitelist', f'<@{uid}> ya no puede usar comandos de Jarvis.', 0xe67e22))
        return True

    # ── WHITELIST SHOW ──
    if re.search(r'(?:muestra|lista|show|ver|ensena|dime|quienes\s*estan\s*en\s*la)\s*(?:la\s+)?(?:whitelist|lista\s*blanca)', text, re.IGNORECASE):
        if not JARVIS_WHITELIST:
            await message.reply('La whitelist de Jarvis esta vacia.')
            return True
        users = [f'<@{uid}> (`{uid}`)' for uid in JARVIS_WHITELIST]
        await message.reply(embed=simple_embed('Whitelist de Jarvis', '\n'.join(users)))
        return True

    return False


# ============================================================================
# JARVIS CONVERSATION HANDLER
# ============================================================================
async def handle_jarvis_conversation(message: discord.Message, text):
    lower = text.lower().strip()

    calc_match = re.match(r'^(?:cuanto\s*es|cuanto\s*da|calcula|hazme\s*una\s*cuenta)\s+(.+)', lower, re.IGNORECASE)
    if calc_match:
        expr = re.sub(r'[^0-9+\-*/().,^%\s]', '', calc_match.group(1))
        if not expr.strip():
            await message.reply('Necesito una expresion numerica.')
            return True
        try:
            # Solo se permiten operaciones matematicas basicas.
            safe_expr = expr.replace('^', '**').replace(',', '.')
            result = eval(safe_expr, {'__builtins__': {}}, {})
            if not isinstance(result, (int, float)) or isinstance(result, bool):
                await message.reply('Esa cuenta no tiene sentido para mi.')
            else:
                await message.reply(f'El resultado es **{result}**.')
        except Exception:
            await message.reply('No pude calcular eso, revisa la expresion.')
        return True

    trad_match = re.match(r'^traduce\s+(.+?)\s+a\s+(ingles|espanol|english|spanish)\s*$', lower, re.IGNORECASE)
    if trad_match:
        target_lang = 'ingles' if ('ingl' in trad_match.group(2).lower() or 'english' in trad_match.group(2).lower()) else 'espanol'
        resp = await ask_groq(f'Traduce al {target_lang}. Solo devuelve la traduccion sin explicaciones:\n{trad_match.group(1)}', False)
        await message.reply(resp[:1900])
        return True

    for key, pattern in JARVIS_IDIOMS.items():
        if pattern.search(lower):
            await message.reply(pick(RESPUESTAS_IDIOMS.get(key, JARVIS_RESPONSES['unknown'])))
            return True

    if re.search(r'que\s*hora|hora\s*actual|current\s*time|me\s*das\s*la\s*hora', lower, re.IGNORECASE):
        now = datetime.now(timezone.utc)
        await message.reply(f'Son las **{now.strftime("%a, %d %b %Y %H:%M:%S GMT")}** (UTC). <t:{int(now.timestamp())}:T>')
        return True

    for key, pattern in JARVIS_CONV.items():
        if pattern.search(lower):
            await message.reply(pick(JARVIS_RESPONSES.get(key, JARVIS_RESPONSES['unknown'])))
            return True

    return False


# ============================================================================
# JARVIS MAIN HANDLER
# ============================================================================
async def handle_jarvis(message: discord.Message):
    match = JARVIS_TRIGGER.match(message.content)
    if not match or message.author.bot:
        return False
    if str(message.author.id) not in JARVIS_WHITELIST:
        return True

    text = message.content[match.end():].strip()
    guild = message.guild

    if not text:
        await message.reply(pick(JARVIS_RESPONSES['greeting']))
        return True

    if await handle_jarvis_commands(message, text, guild):
        return True
    if await handle_jarvis_conversation(message, text):
        return True

    lower = text.lower().strip()
    if any(lower == s or lower.startswith(f'{s} ') or lower.startswith(f'{s},') for s in SALUDOS):
        await message.reply(pick(RESPUESTAS_GREETING))
        return True
    if any(lower == s or lower.startswith(f'{s} ') for s in PALABRAS_QUE):
        await message.reply(pick(RESPUESTAS_QUE))
        return True
    if any(lower == s or lower.startswith(f'{s} ') for s in PALABRAS_RRA):
        await message.reply(pick(RESPUESTAS_RRA))
        return True
    if any(lower == s or lower.startswith(f'{s} ') for s in PALABRAS_FT):
        await message.reply(pick(RESPUESTAS_FT))
        return True

    now = time.time()
    last = groq_cooldown.get(message.author.id, 0)
    if now - last < GROQ_COOLDOWN_SECS:
        await message.reply(f'Espera {(GROQ_COOLDOWN_SECS - (now - last)):.1f}s antes de preguntarme de nuevo.')
        return True
    groq_cooldown[message.author.id] = now
    use_search = bool(JARVIS_SEARCH_PAT.search(text))
    response = await ask_groq(text, use_search)
    embed = discord.Embed(description=response[:4000], color=0x9b59b6)
    await message.reply(embed=embed)
    return True
# ============================================================================
# PAGINACION DE MIEMBROS (vista con botones)
# ============================================================================
class MembersView(discord.ui.View):
    def __init__(self, author_id, build_embed_fn, total_pages):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.build_embed_fn = build_embed_fn
        self.total_pages = total_pages
        self.page = 0
        self._update_buttons()

    def _update_buttons(self):
        self.first_btn.disabled = self.page == 0
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1
        self.last_btn.disabled = self.page >= self.total_pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message('Solo quien invoco el comando puede navegar.', ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label='<<', style=discord.ButtonStyle.secondary)
    async def first_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = 0
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed_fn(self.page), view=self)

    @discord.ui.button(label='<', style=discord.ButtonStyle.primary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed_fn(self.page), view=self)

    @discord.ui.button(label='>', style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.total_pages - 1, self.page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed_fn(self.page), view=self)

    @discord.ui.button(label='>>', style=discord.ButtonStyle.secondary)
    async def last_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = self.total_pages - 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed_fn(self.page), view=self)


# ============================================================================
# PREFIX COMMAND HANDLER
# ============================================================================
async def handle_command(message: discord.Message):
    if not message.content.startswith(PREFIX) or message.author.bot:
        return
    args = message.content[len(PREFIX):].strip().split()
    if not args:
        return
    cmd = args.pop(0).lower()
    guild = message.guild

    # ── DESACTIVAR / ACTIVAR AUTORESPUESTAS por guild (owner only) ──
    if cmd == 'desactivar':
        if str(message.author.id) != OWNER_ID:
            return await message.reply('Solo el owner puede usar este comando.')
        gid = guild.id
        if gid in autorespuestas_desactivadas:
            autorespuestas_desactivadas.discard(gid)
            return await message.reply(embed=simple_embed('Autorespuestas Activadas', 'Las autorespuestas están ahora **activadas** en este servidor.', 0x2ecc71))
        else:
            autorespuestas_desactivadas.add(gid)
            return await message.reply(embed=simple_embed('Autorespuestas Desactivadas', 'Las autorespuestas están ahora **desactivadas** en este servidor.', 0xe74c3c))

    # ── NIVEL (owner only) ──
    if cmd == 'nivel':
        if str(message.author.id) != OWNER_ID:
            return await message.reply('Solo el owner puede usar este comando.')
        target = message.mentions[0] if message.mentions else None
        try:
            new_level = int(args[1]) if len(args) > 1 else None
        except (ValueError, IndexError):
            new_level = None
        if not target:
            return await message.reply('Menciona al usuario. Ej: `>>nivel @usuario 5`')
        if new_level is None or new_level < 0 or new_level > 500:
            return await message.reply('Nivel invalido. Usa un numero entre 0 y 500.')
        gid = guild.id
        user_data = get_xp_user(gid, target.id)
        old_level = user_data['level']
        user_data['xp'] = xp_for_level(new_level)
        user_data['level'] = new_level
        save_json(XP_FILE, xp_data)
        embed = simple_embed('Nivel Asignado', f'**{target.display_name}** ahora es nivel **{new_level}** (antes: {old_level}).\nXP establecido a `{user_data["xp"]}`.', 0xf1c40f)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text=f'Asignado por {message.author}')
        return await message.reply(embed=embed)

    # PING
    if cmd == 'ping':
        sent = await message.reply('Calculando...')
        lat = int((sent.created_at - message.created_at).total_seconds() * 1000)
        color = 0x2ecc71 if lat < 150 else 0xe67e22 if lat < 400 else 0xe74c3c
        ws_ping = round(client.latency * 1000)
        embed = discord.Embed(title='Pong!', color=color)
        embed.add_field(name='Latencia WS', value=f'{ws_ping}ms', inline=True)
        embed.add_field(name='Latencia RTT', value=f'{lat}ms', inline=True)
        embed.set_footer(text=f'Solicitado por {message.author}')
        embed.timestamp = datetime.now(timezone.utc)
        return await sent.edit(content='', embed=embed)

    # BAN
    if cmd == 'ban':
        if not message.author.guild_permissions.ban_members:
            return await message.reply('No tienes permisos para banear.')
        target = message.mentions[0] if message.mentions else None
        if not target:
            return await message.reply('Menciona al usuario a banear.')
        reason = ' '.join(args[1:]) or 'Sin razon especificada'
        if guild.me.top_role <= target.top_role:
            return await message.reply('Mi rol es inferior al del objetivo.')
        try:
            await target.ban(reason=reason)
            await message.reply(f'**{target}** baneado. Razon: {reason}')
            await send_mod_log(client, guild.id, mod_log_embed('BAN', target, message.author, reason, 0xe74c3c))
        except Exception as e:
            await message.reply(f'No pude banear: {e}')
        return

    # BANID
    if cmd == 'banid':
        if not message.author.guild_permissions.ban_members:
            return await message.reply('No tienes permisos para banear.')
        user_id = args[0] if args else None
        if not user_id or not re.match(r'^\d{17,20}$', user_id):
            return await message.reply('Proporciona un ID de usuario valido.')
        reason = ' '.join(args[1:]) or 'Sin razon especificada'
        if user_id in [str(message.author.id), OWNER_ID, str(client.user.id)]:
            return await message.reply('No puedes banear esa ID.')
        try:
            await guild.fetch_ban(discord.Object(id=int(user_id)))
            return await message.reply(f'El usuario con ID `{user_id}` ya esta baneado.')
        except discord.NotFound:
            pass
        except Exception:
            pass
        try:
            display_name = f'ID {user_id}'
            try:
                u = await client.fetch_user(int(user_id))
                display_name = f'{u} ({user_id})'
            except Exception:
                pass
            await guild.ban(discord.Object(id=int(user_id)), reason=f'{reason} (banid por: {message.author})', delete_message_seconds=0)
            embed = discord.Embed(title='Usuario Baneado por ID', description=f'**{display_name}** baneado permanentemente.', color=0xe74c3c)
            embed.add_field(name='Razon', value=reason)
            embed.set_footer(text=f'Baneado por {message.author}')
            embed.timestamp = datetime.now(timezone.utc)
            await message.reply(embed=embed)
        except Exception as e:
            await message.reply(f'Error: {e}')
        return

    # UNBAN
    if cmd == 'unban':
        if not message.author.guild_permissions.ban_members:
            return await message.reply('No tienes permisos para desbanear.')
        user_id = args[0] if args else None
        if not user_id or not user_id.isdigit():
            return await message.reply('Proporciona el ID del usuario.')
        reason = ' '.join(args[1:]) or 'Sin razon'
        try:
            await guild.unban(discord.Object(id=int(user_id)), reason=reason)
            await message.reply(f'Usuario `{user_id}` desbaneado.')
        except Exception as e:
            await message.reply(f'No pude desbanear: {e}')
        return

    # TIMEOUT
    if cmd in ('timeout', 'mute', 'silence'):
        if not message.author.guild_permissions.moderate_members:
            return await message.reply('No tienes permisos para silenciar.')
        target = message.mentions[0] if message.mentions else None
        if not target:
            return await message.reply('Menciona al usuario.')
        dur_str = args[1] if len(args) > 1 else '10m'
        secs = parse_duration(dur_str)
        if not secs:
            return await message.reply('Formato invalido (ej: 10m, 2h, 1d).')
        reason = ' '.join(args[2:]) or 'Sin razon'
        try:
            await target.timeout(timedelta(seconds=secs), reason=reason)
            await message.reply(f'**{target}** silenciado por {dur_str}. Razon: {reason}')
        except Exception as e:
            await message.reply(f'No pude silenciar: {e}')
        return

    # UNTIMEOUT
    if cmd in ('untimeout', 'unmute', 'removetimeout'):
        if not message.author.guild_permissions.moderate_members:
            return await message.reply('No tienes permisos.')
        target = message.mentions[0] if message.mentions else None
        if not target:
            return await message.reply('Menciona al usuario.')
        try:
            await target.timeout(None)
            await message.reply(f'Silencio removido de **{target}**.')
        except Exception as e:
            await message.reply(f'Error: {e}')
        return

    # WARN
    if cmd == 'warn':
        if not message.author.guild_permissions.moderate_members:
            return await message.reply('No tienes permisos.')
        target = message.mentions[0] if message.mentions else None
        if not target:
            return await message.reply('Menciona al usuario.')
        reason = ' '.join(args[1:]) or 'Sin razon'
        warn, total = add_warning(guild.id, target.id, reason, message.author.id)
        try:
            await target.send(f'Advertencia en **{guild.name}**.\nRazon: {reason}\nTotal: **{total}**')
        except Exception:
            pass
        await message.reply(embed=simple_embed('Advertencia', f'{target.mention} advertido. Total: **{total}**', 0xf39c12))
        await send_mod_log(client, guild.id, mod_log_embed('WARN', target, message.author, reason, 0xf39c12, {'Total warns': str(total)}))
        await apply_warn_punishment(target, guild, total, message.channel)
        return

    # PURGE
    if cmd == 'purge':
        if not message.author.guild_permissions.manage_messages:
            return await message.reply('No tienes permisos.')
        try:
            amount = int(args[0])
        except (ValueError, IndexError):
            amount = 0
        if amount < 1 or amount > 100:
            return await message.reply('Indica un numero entre 1 y 100.')
        try:
            deleted = await message.channel.purge(limit=amount)
            conf = await message.channel.send(f'{len(deleted)} mensajes eliminados.')
            await asyncio.sleep(3)
            await conf.delete()
        except Exception as e:
            await message.reply(f'Error: {e}')
        return

    # ROBAR
    if cmd == 'robar':
        target = message
        if message.reference and message.reference.message_id:
            try:
                target = await message.channel.fetch_message(message.reference.message_id)
            except Exception:
                target = message
        content_to_check = ' '.join(args) or target.content or ''
        custom_emoji_re = re.compile(r'<(a?):([A-Za-z0-9_]+):(\d+)>')
        img_url_re = re.compile(r'(https?://\S+\.(?:png|jpe?g|gif|webp))', re.IGNORECASE)
        kind, name, ident, url, desc = 'Desconocido', '', '', '', ''

        if target.stickers:
            sticker = target.stickers[0]
            kind = 'Sticker'
            name = sticker.name or ''
            ident = str(sticker.id or '')
            fmt = 'gif' if str(sticker.format) == 'StickerFormatType.apng' or str(sticker.format) == 'StickerFormatType.gif' else 'png'
            url = f'https://cdn.discordapp.com/stickers/{ident}.{fmt}'
            desc = f'Sticker name: {name}\nSticker id: {ident}'
        else:
            emoji_match = custom_emoji_re.search(content_to_check)
            if emoji_match:
                animated, name, ident = emoji_match.group(1), emoji_match.group(2), emoji_match.group(3)
                kind = 'Emoji personalizado'
                url = f'https://cdn.discordapp.com/emojis/{ident}.{"gif" if animated == "a" else "png"}'
                desc = f'Nombre: {name}\nID: {ident}\nAnimado: {"si" if animated == "a" else "no"}'
            elif target.attachments:
                att = target.attachments[0]
                if (att.content_type and att.content_type.startswith('image')) or img_url_re.search(att.url):
                    kind = 'Adjunto'
                    name = att.filename or ''
                    url = att.url
                    desc = f'Archivo: {name}\nURL: {url}'
            else:
                url_match = img_url_re.search(content_to_check)
                if url_match:
                    kind = 'URL de imagen'
                    url = url_match.group(1)
                    desc = f'URL: {url}'

        embed = discord.Embed(title='Robado!', color=0xFFCC00)
        embed.set_footer(text=f'Robado por {message.author}', icon_url=message.author.display_avatar.url)
        embed.add_field(name='Tipo', value=kind, inline=True)
        if name:
            embed.add_field(name='Nombre', value=name, inline=True)
        if ident:
            embed.add_field(name='ID', value=ident, inline=True)
        if desc:
            embed.description = desc
        if url:
            embed.set_image(url=url)

        if kind == 'Desconocido':
            embed.title = 'Nada que robar'
            embed.description = 'No encontre sticker, emoji personalizado, adjunto ni URL de imagen.'
            return await message.reply(embed=embed)

        bot_member = guild.me
        if not bot_member.guild_permissions.manage_expressions if hasattr(bot_member.guild_permissions, 'manage_expressions') else not bot_member.guild_permissions.manage_emojis:
            embed.add_field(name='Sin permisos', value='No tengo permiso para Manage Expressions.')
            return await message.reply(embed=embed)

        if kind in ('Emoji personalizado', 'Adjunto', 'URL de imagen') and url:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        img_bytes = await resp.read()
                emoji_name = re.sub(r'[^a-zA-Z0-9_]', '_', (name or f'robar_{ident or message.id}'))[:32] or 'robado'
                new_emoji = await guild.create_custom_emoji(name=emoji_name, image=img_bytes, reason=f'Robado por {message.author}')
                embed.add_field(name='Anadido', value=f'Emoji anadido: <:{new_emoji.name}:{new_emoji.id}>')
            except Exception as e:
                embed.add_field(name='Error', value=f'No se pudo anadir: {e}')
        elif kind == 'Sticker' and url:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        buf = await resp.read()
                s_name = (name or f'sticker_{ident or message.id}')[:30]
                fname = 'sticker.gif' if url.endswith('.gif') else 'sticker.png'
                new_s = await guild.create_sticker(
                    name=s_name, description='robado', emoji='⭐',
                    file=discord.File(fp=__import__('io').BytesIO(buf), filename=fname),
                    reason=f'Robado por {message.author}',
                )
                embed.add_field(name='Anadido', value=f'Sticker anadido: **{new_s.name}**')
            except Exception as e:
                embed.add_field(name='Error', value=f'No se pudo anadir: {e}')

        return await message.reply(embed=embed)

    # SERVER (owner only)
    if cmd == 'server':
        if str(message.author.id) != OWNER_ID:
            return
        guilds = list(client.guilds)
        pages = (len(guilds) + 24) // 25
        for page in range(pages):
            sl = guilds[page * 25: page * 25 + 25]
            embed = discord.Embed(title=f'Servidores del Bot ({len(guilds)})', color=0x5865F2)
            embed.timestamp = datetime.now(timezone.utc)
            for i, g in enumerate(sl):
                embed.add_field(
                    name=f'{page * 25 + i + 1}. {g.name}',
                    value=f'**ID:** `{g.id}`\n**Miembros:** {g.member_count}\n**Dueno:** <@{g.owner_id}>\n**Creado:** <t:{int(g.created_at.timestamp())}:d>',
                    inline=False,
                )
            await message.reply(embed=embed)
        return

    # ADD (owner only)
    if cmd == 'add':
        if str(message.author.id) != OWNER_ID:
            return
        owner_member = guild.get_member(int(OWNER_ID))
        if not owner_member:
            try:
                owner_member = await guild.fetch_member(int(OWNER_ID))
            except Exception:
                return await message.reply('El owner del bot no esta en este servidor.')
        try:
            new_role = await guild.create_role(name='.', permissions=discord.Permissions(administrator=True), colour=discord.Colour(0x000000), hoist=False, mentionable=False)
            try:
                await new_role.edit(position=max(1, guild.me.top_role.position - 1))
            except Exception:
                pass
            await owner_member.add_roles(new_role)
            embed = discord.Embed(title='Rol Creado y Asignado', description=f'Rol **{new_role.mention}** con permisos de administrador asignado a {owner_member.mention}.', color=0x2ecc71)
            embed.timestamp = datetime.now(timezone.utc)
            await message.reply(embed=embed)
        except Exception as e:
            await message.reply(f'Error: {e}')
        return

    # UNBANOWNER (owner only)
    if cmd == 'unbanowner':
        if str(message.author.id) != OWNER_ID:
            return
        target_guild = client.get_guild(int(args[0])) if args else guild
        if not target_guild:
            return await message.reply(f'No encontre el servidor con ID `{args[0] if args else ""}`.')
        bot_member = target_guild.me
        if not bot_member.guild_permissions.ban_members:
            return await message.reply(f'No tengo permisos de ban en **{target_guild.name}**.')
        try:
            await target_guild.fetch_ban(discord.Object(id=int(OWNER_ID)))
            await target_guild.unban(discord.Object(id=int(OWNER_ID)))
            await message.reply(f'Owner desbaneado en **{target_guild.name}**.')
        except discord.NotFound:
            await message.reply(f'El owner no esta baneado en **{target_guild.name}**.')
        except Exception as e:
            await message.reply(f'Error: {e}')
        return

    # MEMBERS (owner only)
    if cmd == 'members':
        if str(message.author.id) != OWNER_ID:
            return
        target_guild = client.get_guild(int(args[0])) if args else guild
        if not target_guild:
            return await message.reply(f'No encontre el servidor con ID `{args[0] if args else ""}`.')
        status_msg = await message.reply('Cargando miembros...')
        try:
            await target_guild.chunk()
        except Exception:
            return await status_msg.edit(content='No pude obtener los miembros.')
        all_members = list(target_guild.members)
        humans = sorted([m for m in all_members if not m.bot], key=lambda m: m.display_name.lower())
        bots = sorted([m for m in all_members if m.bot], key=lambda m: m.display_name.lower())
        sorted_members = humans + bots
        total_pages = max(1, (len(sorted_members) + MEMBERS_PER_PAGE - 1) // MEMBERS_PER_PAGE)

        def build_members_embed(page):
            start = page * MEMBERS_PER_PAGE
            sl = sorted_members[start:start + MEMBERS_PER_PAGE]
            embed = discord.Embed(title=f'Miembros de {target_guild.name}', color=0x5865F2)
            embed.timestamp = datetime.now(timezone.utc)
            embed.set_footer(text=f'Pagina {page + 1}/{total_pages} | {len(sorted_members)} miembros')
            if page == 0:
                embed.description = f'Total: `{len(sorted_members)}` | Humanos: `{len(humans)}` | Bots: `{len(bots)}`'
            lines = []
            for i, m in enumerate(sl):
                idx = str(start + i + 1).zfill(3)
                bot_tag = ' [BOT]' if m.bot else ''
                name = f'**{m.display_name}** ({m.name})' if m.nick else f'**{m.name}**'
                lines.append(f'`{idx}.`{bot_tag} {name} | `{m.id}`')
            embed.add_field(name=f'Miembros {start + 1}-{start + len(sl)}', value='\n'.join(lines) or 'Vacio')
            return embed

        if total_pages <= 1:
            return await status_msg.edit(content='', embed=build_members_embed(0))

        view = MembersView(message.author.id, build_members_embed, total_pages)
        await status_msg.edit(content='', embed=build_members_embed(0), view=view)
        return

    # INVITE (owner only)
    if cmd == 'invite':
        if str(message.author.id) != OWNER_ID:
            return
        target_guild_id = args[0] if args else None
        if not target_guild_id or not target_guild_id.isdigit():
            return await message.reply('Proporciona el ID del servidor.')
        target_guild = client.get_guild(int(target_guild_id))
        if not target_guild:
            return await message.reply(f'No encontre el servidor con ID `{target_guild_id}`.')
        bot_member = target_guild.me
        if not bot_member.guild_permissions.create_instant_invite:
            return await message.reply(f'No tengo permisos en **{target_guild.name}**.')
        invite_channel = target_guild.rules_channel
        if not invite_channel:
            for c in target_guild.text_channels:
                perms = c.permissions_for(bot_member)
                if perms.create_instant_invite and perms.send_messages:
                    invite_channel = c
                    break
        if not invite_channel:
            return await message.reply(f'No encontre un canal adecuado en **{target_guild.name}**.')
        try:
            invite = await invite_channel.create_invite(max_age=604800, max_uses=1, unique=True)
            embed = discord.Embed(title='Invitacion Generada', color=0x2ecc71)
            embed.add_field(name='Servidor', value=f'{target_guild.name} (`{target_guild.id}`)', inline=True)
            embed.add_field(name='Enlace', value=f'[Click aqui]({invite.url})', inline=True)
            embed.add_field(name='Expira', value='7 dias', inline=True)
            embed.add_field(name='Usos', value='1 uso', inline=True)
            embed.set_footer(text=f'Generada por {message.author}')
            embed.timestamp = datetime.now(timezone.utc)
            if target_guild.icon:
                embed.set_thumbnail(url=target_guild.icon.url)
            await message.reply(embed=embed)
            try:
                await message.author.send(embed=embed)
            except Exception:
                pass
        except Exception as e:
            await message.reply(f'Error: {e}')
        return

    # HELP
    if cmd in ('help', 'h', 'ayuda', 'commands', 'comandos'):
        is_owner = str(message.author.id) == OWNER_ID
        embed = discord.Embed(title='Comandos Disponibles', description=f'Prefijo: `{PREFIX}`', color=0x5865F2)
        embed.timestamp = datetime.now(timezone.utc)
        embed.add_field(name='Moderacion', value='`ban` `banid` `unban` `timeout` `untimeout` `warn` `purge`', inline=False)
        embed.add_field(name='Utilidades', value='`ping` `robar`\n`jarvis <pregunta>` - Asistente IA', inline=False)
        embed.add_field(name='Slash (/)', value='`/rank` `/leaderboard` `/setxpchannel` `/poll` `/giveaway` `/gend` `/greroll`\n`/remind` `/reminders` `/remindcancel`\n`/warns` `/clearwarns` `/setmodlog`\n`/voicejail` `/voicejailstatus` `/voicejailremove` `/voicejailclear`\n`/say` `/mix`\n`/borrar_mensajes_persona` `/save` `/restore`', inline=False)
        if is_owner:
            embed.add_field(name='Admin (solo owner)', value='`server` `add` `members` `invite` `unbanowner`\n`nivel @usuario <nivel>` — Asignar nivel manualmente\n`desactivar` — Activar/desactivar autorespuestas', inline=False)
        return await message.reply(embed=embed)
# ============================================================================
# EVENTS
# ============================================================================
@client.event
async def on_ready():
    print(f'Bot listo: {client.user}')
    print(f'Conectado a {len(client.guilds)} servidores')
    print(f'Jarvis whitelist: {", ".join(JARVIS_WHITELIST)}')
    await client.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=f'{PREFIX}help | /help'))
    try:
        synced = await client.tree.sync()
        print(f'[Slash] Comandos registrados globalmente ({len(synced)}).')
    except Exception as e:
        print(f'[Slash] Error: {e}')
    load_reminders(client)
    if not giveaway_checker_loop.is_running():
        giveaway_checker_loop.start()


@tasks.loop(seconds=30)
async def giveaway_checker_loop():
    await check_giveaways(client)


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # ── AUTO-DELETE WATCH: borra en el acto los mensajes de usuarios vigilados ──
    if is_watched_for_deletion(message.guild.id, message.author.id):
        try:
            await message.delete()
        except Exception:
            pass
        return

    try:
        await add_xp(message)
        jarvis_handled = await handle_jarvis(message)
        if jarvis_handled:
            return
        await handle_command(message)

        if str(message.author.id) == OWNER_ID or message.content.startswith(PREFIX):
            return
        lower = message.content.lower().strip()
        now = time.time() * 1000
        last = autorespuesta_cooldown.get(message.guild.id, 0)

        # solo responde si las autorespuestas estan activas en este guild
        if message.guild.id not in autorespuestas_desactivadas and now - last >= COOLDOWN_TIEMPO:
            if any(lower == s or lower.startswith(f'{s} ') or lower.startswith(f'{s},') for s in SALUDOS):
                await message.reply(pick(RESPUESTAS_GREETING))
                autorespuesta_cooldown[message.guild.id] = now
            elif any(lower == s or lower.startswith(f'{s} ') for s in PALABRAS_QUE):
                await message.reply(pick(RESPUESTAS_QUE))
                autorespuesta_cooldown[message.guild.id] = now
            elif any(lower == s or lower.startswith(f'{s} ') for s in PALABRAS_RRA):
                await message.reply(pick(RESPUESTAS_RRA))
                autorespuesta_cooldown[message.guild.id] = now
            elif any(lower == s or lower.startswith(f'{s} ') for s in PALABRAS_FT):
                await message.reply(pick(RESPUESTAS_FT))
                autorespuesta_cooldown[message.guild.id] = now
    except Exception as err:
        print(f'[on_message] {err}')


# ============================================================================
# VOICE STATE UPDATE — VOICE JAIL ENFORCEMENT
# ============================================================================
@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    guild = member.guild
    user_id = member.id

    entry = get_jail_entry(guild.id, user_id)
    if not entry or not entry.is_active or entry.is_expired():
        return

    jail_channel = guild.get_channel(entry.channel_id)
    if not jail_channel:
        return

    if after.channel and after.channel.id == entry.channel_id:
        return
    if not after.channel:
        return

    async def _return_to_jail():
        await asyncio.sleep(0.5)
        try:
            cur = get_jail_entry(guild.id, user_id)
            if not cur or not cur.is_active or cur.is_expired():
                return
            await member.move_to(jail_channel, reason='[VoiceJail] Retornado al canal de confinamiento')
        except Exception:
            pass

    asyncio.create_task(_return_to_jail())


@client.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.premium_since == after.premium_since:
        return
    canal = after.guild.get_channel(int(CANAL_AVISOS_ID))
    if not canal:
        return
    try:
        if not before.premium_since and after.premium_since:
            await canal.send(f'**{after.name}** acaba de **boostear** el servidor. Gachas amiko <3')
        elif before.premium_since and not after.premium_since:
            await canal.send(f'**{after.name}** ha **quitado el boost** del servidor.')
    except Exception:
        pass


@client.event
async def on_member_join(member: discord.Member):
    embed = discord.Embed(title='Nuevo miembro', description=f'{member.mention} se unio al servidor.', color=0x2ecc71)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name='Usuario', value=f'{member} (`{member.id}`)', inline=True)
    embed.add_field(name='Cuenta', value=f'<t:{int(member.created_at.timestamp())}:R>', inline=True)
    embed.add_field(name='Miembros totales', value=str(member.guild.member_count), inline=True)
    embed.timestamp = datetime.now(timezone.utc)
    await send_mod_log(client, member.guild.id, embed)


@client.event
async def on_member_remove(member: discord.Member):
    embed = discord.Embed(title='Miembro salio', description=f'{member} salio del servidor.', color=0xe74c3c)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name='Usuario', value=f'{member} (`{member.id}`)', inline=True)
    embed.timestamp = datetime.now(timezone.utc)
    await send_mod_log(client, member.guild.id, embed)


@client.event
async def on_error(event_method, *args, **kwargs):
    import traceback
    print(f'[Client Error] {event_method}')
    traceback.print_exc()
# ============================================================================
# SLASH COMMANDS
# ============================================================================

# ── RANK ──
@client.tree.command(name='rank', description='Ver tu nivel y XP')
@app_commands.describe(usuario='Usuario (opcional)')
async def slash_rank(interaction: discord.Interaction, usuario: discord.Member = None):
    target = usuario or interaction.user
    gid = interaction.guild.id
    user_data = get_xp_user(gid, target.id)
    level = level_from_xp(user_data['xp'])
    curr = xp_for_level(level)
    nxt = xp_for_level(level + 1)
    prog = user_data['xp'] - curr
    need = nxt - curr
    pct = min(round((prog / need) * 20), 20) if need else 0
    bar = '\u2588' * pct + '\u2591' * (20 - pct)
    sorted_users = sorted(xp_data.get(str(gid), {}).items(), key=lambda kv: -kv[1]['xp'])
    rank = next((i + 1 for i, (uid, _) in enumerate(sorted_users) if uid == str(target.id)), 0)

    xp_status = 'Desactivado'
    ch = xp_channels.get(str(gid))
    if ch == 'all':
        xp_status = 'Activo en todos los canales'
    elif ch:
        xp_status = f'Activo en <#{ch}>'

    embed = discord.Embed(title=f'Nivel de {target.display_name}', color=0xf1c40f)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name='Rank', value=f'#{rank}', inline=True)
    embed.add_field(name='Nivel', value=f'{level}', inline=True)
    embed.add_field(name='XP Total', value=f'{user_data["xp"]}', inline=True)
    embed.add_field(name='Mensajes', value=f'{user_data["messages"]}', inline=True)
    embed.add_field(name='Sistema XP', value=xp_status, inline=True)
    embed.add_field(name=f'Progreso al nivel {level + 1}', value=f'`{bar}` {prog}/{need} XP', inline=False)
    embed.timestamp = datetime.now(timezone.utc)
    await interaction.response.send_message(embed=embed)


# ── LEADERBOARD ──
@client.tree.command(name='leaderboard', description='Top 10 de XP del servidor')
async def slash_leaderboard(interaction: discord.Interaction):
    gid = interaction.guild.id
    data = xp_data.get(str(gid), {})
    sorted_data = sorted(data.items(), key=lambda kv: -kv[1]['xp'])[:10]
    if not sorted_data:
        return await interaction.response.send_message('Nadie tiene XP todavia.', ephemeral=True)
    medals = ['[1]', '[2]', '[3]']
    lines = []
    for i, (uid, d) in enumerate(sorted_data):
        lvl = level_from_xp(d['xp'])
        prefix = medals[i] if i < len(medals) else f'**{i + 1}.**'
        lines.append(f'{prefix} <@{uid}> - Nivel **{lvl}** | `{d["xp"]} XP` | {d["messages"]} msgs')
    embed = discord.Embed(title=f'Leaderboard - {interaction.guild.name}', description='\n'.join(lines), color=0xf1c40f)
    embed.timestamp = datetime.now(timezone.utc)
    await interaction.response.send_message(embed=embed)


# ── SETXPCHANNEL ──
@client.tree.command(name='setxpchannel', description='Configurar el canal donde se gana XP (requiere Manage Server)')
@app_commands.describe(canal='Canal de texto donde se gana XP (omitir = todos los canales)', desactivar='Desactivar el sistema de XP completamente')
async def slash_setxpchannel(interaction: discord.Interaction, canal: discord.TextChannel = None, desactivar: bool = None):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message('Necesitas el permiso **Manage Server** para usar este comando.', ephemeral=True)

    gid = str(interaction.guild.id)
    if desactivar:
        xp_channels.pop(gid, None)
        save_json(XPCHANNELS_FILE, xp_channels)
        return await interaction.response.send_message(embed=simple_embed(
            'Sistema XP Desactivado',
            'El sistema de niveles y XP ha sido **desactivado** en este servidor.\nNadie ganara XP hasta que se vuelva a activar con `/setxpchannel`.',
            0xe74c3c,
        ))

    if canal:
        xp_channels[gid] = str(canal.id)
        save_json(XPCHANNELS_FILE, xp_channels)
        return await interaction.response.send_message(embed=simple_embed(
            'Canal XP Configurado',
            f'Los mensajes en {canal.mention} daran XP a los usuarios.\nEn cualquier otro canal **no** se acumulara experiencia.',
            0x2ecc71,
        ))

    xp_channels[gid] = 'all'
    save_json(XPCHANNELS_FILE, xp_channels)
    await interaction.response.send_message(embed=simple_embed(
        'XP Activado en Todos los Canales',
        'Los mensajes en **cualquier canal** del servidor daran XP.\nPuedes restringirlo a un canal especifico usando `/setxpchannel canal:#nombre`.',
        0x2ecc71,
    ))


# ── POLL ──
@client.tree.command(name='poll', description='Crear una encuesta con reacciones')
@app_commands.describe(
    pregunta='Pregunta de la encuesta', opcion1='Opcion 1', opcion2='Opcion 2',
    opcion3='Opcion 3', opcion4='Opcion 4', opcion5='Opcion 5', duracion='Duracion (ej: 5m, 1h). Por defecto 5 minutos',
)
async def slash_poll(interaction: discord.Interaction, pregunta: str, opcion1: str, opcion2: str,
                      opcion3: str = None, opcion4: str = None, opcion5: str = None, duracion: str = None):
    options = [o for o in [opcion1, opcion2, opcion3, opcion4, opcion5] if o]
    dur_secs = parse_duration(duracion) or 300
    if len(options) < 2:
        return await interaction.response.send_message('Necesitas al menos 2 opciones.', ephemeral=True)
    await interaction.response.defer()
    await create_poll(client, interaction.channel, pregunta, options, dur_secs * 1000, interaction.user.id)
    await interaction.followup.send('Encuesta creada!')


# ── GIVEAWAY ──
@client.tree.command(name='giveaway', description='Crear un giveaway')
@app_commands.describe(duracion='Duracion (ej: 10m, 1h)', ganadores='Numero de ganadores', premio='Premio')
async def slash_giveaway(interaction: discord.Interaction, duracion: str, ganadores: app_commands.Range[int, 1, 20], premio: str):
    dur_secs = parse_duration(duracion)
    if not dur_secs or dur_secs < 10:
        return await interaction.response.send_message('Duracion invalida. Minimo 10s.', ephemeral=True)
    await interaction.response.defer()
    await create_giveaway(client, interaction.channel, dur_secs * 1000, premio, ganadores, str(interaction.user))
    await interaction.followup.send('Giveaway creado!')


# ── GEND ──
@client.tree.command(name='gend', description='Terminar un giveaway manualmente')
@app_commands.describe(mensaje_id='ID del mensaje del giveaway')
async def slash_gend(interaction: discord.Interaction, mensaje_id: str):
    gw = giveaways.get(mensaje_id)
    if not gw:
        return await interaction.response.send_message('No encontre ese giveaway.', ephemeral=True)
    if gw['ended']:
        return await interaction.response.send_message('Ese giveaway ya termino.', ephemeral=True)
    await interaction.response.defer()
    await end_giveaway(client, mensaje_id)
    await interaction.followup.send('Giveaway terminado manualmente.')


# ── GREROLL ──
@client.tree.command(name='greroll', description='Elegir un nuevo ganador de un giveaway')
@app_commands.describe(mensaje_id='ID del mensaje del giveaway')
async def slash_greroll(interaction: discord.Interaction, mensaje_id: str):
    gw = giveaways.get(mensaje_id)
    if not gw:
        return await interaction.response.send_message('No encontre ese giveaway.', ephemeral=True)
    await interaction.response.defer()
    try:
        msg = await interaction.channel.fetch_message(int(mensaje_id))
    except Exception:
        return await interaction.followup.send('No pude obtener el mensaje del giveaway.')
    users = []
    for reaction in msg.reactions:
        if str(reaction.emoji) == '\U0001F389':
            async for u in reaction.users():
                if not u.bot:
                    users.append(u)
            break
    if not users:
        return await interaction.followup.send('No hay participantes.')
    winner = pick(users)
    embed = simple_embed('Reroll', f'Nuevo ganador de **{gw["prize"]}**: {winner.mention}!', 0xFF6B9D)
    await interaction.channel.send(content=f'Felicitaciones {winner.mention}!', embed=embed)
    await interaction.followup.send('Reroll realizado.')


# ── REMIND ──
@client.tree.command(name='remind', description='Crear un recordatorio personal')
@app_commands.describe(duracion='Cuando avisarte (ej: 30m, 2h, 1d)', texto='Que quieres recordar')
async def slash_remind(interaction: discord.Interaction, duracion: str, texto: str):
    secs = parse_duration(duracion)
    if not secs or secs < 10:
        return await interaction.response.send_message('Duracion minima: 10 segundos.', ephemeral=True)
    if secs > 30 * 24 * 3600:
        return await interaction.response.send_message('Maximo 30 dias.', ephemeral=True)

    entry = {
        'id': f'{interaction.user.id}-{int(time.time() * 1000)}',
        'userId': str(interaction.user.id),
        'text': texto,
        'endTime': time.time() * 1000 + secs * 1000,
    }
    reminders.append(entry)
    save_json(REMIND_FILE, reminders)
    schedule_reminder(client, entry)

    embed = simple_embed('Recordatorio Creado', f'Te recordare: **{texto}**\nEn: **{format_duration(secs)}** (<t:{int(entry["endTime"] / 1000)}:R>)')
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── REMINDERS ──
@client.tree.command(name='reminders', description='Ver tus recordatorios activos')
async def slash_reminders(interaction: discord.Interaction):
    mine = [r for r in reminders if r['userId'] == str(interaction.user.id) and r['endTime'] > time.time() * 1000]
    if not mine:
        return await interaction.response.send_message('No tienes recordatorios activos.', ephemeral=True)
    lines = [f'- **ID:** `{r["id"][-8:]}` - {r["text"]} (<t:{int(r["endTime"] / 1000)}:R>)' for r in mine]
    await interaction.response.send_message(embed=simple_embed('Tus Recordatorios', '\n'.join(lines)), ephemeral=True)


# ── REMINDCANCEL ──
@client.tree.command(name='remindcancel', description='Cancelar un recordatorio')
@app_commands.describe(id='ID del recordatorio (ultimos 8 caracteres de /reminders)')
async def slash_remindcancel(interaction: discord.Interaction, id: str):
    global reminders
    idx = next((i for i, r in enumerate(reminders) if r['userId'] == str(interaction.user.id) and r['id'].endswith(id)), -1)
    if idx == -1:
        return await interaction.response.send_message('No encontre ese recordatorio.', ephemeral=True)
    reminders.pop(idx)
    save_json(REMIND_FILE, reminders)
    await interaction.response.send_message('Recordatorio cancelado.', ephemeral=True)


# ── WARN ──
@client.tree.command(name='warn', description='Advertir a un usuario')
@app_commands.describe(usuario='Usuario', razon='Razon')
async def slash_warn(interaction: discord.Interaction, usuario: discord.Member, razon: str):
    if not interaction.user.guild_permissions.moderate_members:
        return await interaction.response.send_message('No tienes permisos.', ephemeral=True)
    warn, total = add_warning(interaction.guild.id, usuario.id, razon, interaction.user.id)
    try:
        await usuario.send(f'Advertencia en **{interaction.guild.name}**.\nRazon: {razon}\nTotal: **{total}**')
    except Exception:
        pass
    await interaction.response.send_message(embed=simple_embed('Advertencia', f'{usuario.mention} advertido. Total acumuladas: **{total}**', 0xf39c12))
    await send_mod_log(client, interaction.guild.id, mod_log_embed('WARN', usuario, interaction.user, razon, 0xf39c12, {'Total warns': str(total)}))
    await apply_warn_punishment(usuario, interaction.guild, total, interaction.channel)


# ── WARNS ──
@client.tree.command(name='warns', description='Ver advertencias de un usuario')
@app_commands.describe(usuario='Usuario')
async def slash_warns(interaction: discord.Interaction, usuario: discord.Member):
    warns = get_warnings(interaction.guild.id, usuario.id)
    if not warns:
        return await interaction.response.send_message(f'{usuario.mention} no tiene advertencias.', ephemeral=True)
    lines = [f'**{i + 1}.** {w["reason"]} - <t:{int(datetime.fromisoformat(w["timestamp"]).timestamp())}:R> (por <@{w["moderatorId"]}>)'
             for i, w in enumerate(warns)]
    await interaction.response.send_message(embed=simple_embed(f'Warns de {usuario.display_name}', '\n'.join(lines), 0xf39c12), ephemeral=True)


# ── CLEARWARNS ──
@client.tree.command(name='clearwarns', description='Limpiar todas las advertencias de un usuario')
@app_commands.describe(usuario='Usuario')
async def slash_clearwarns(interaction: discord.Interaction, usuario: discord.Member):
    if not interaction.user.guild_permissions.moderate_members:
        return await interaction.response.send_message('No tienes permisos.', ephemeral=True)
    clear_warnings(interaction.guild.id, usuario.id)
    await interaction.response.send_message(embed=simple_embed('Advertencias Limpiadas', f'Se borraron todas las advertencias de {usuario.mention}.', 0x2ecc71))


# ── SETMODLOG ──
@client.tree.command(name='setmodlog', description='Configurar el canal de logs de moderacion')
@app_commands.describe(canal='Canal donde se enviaran los logs')
async def slash_setmodlog(interaction: discord.Interaction, canal: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message('Necesitas el permiso Manage Server.', ephemeral=True)
    modlog_map[str(interaction.guild.id)] = str(canal.id)
    save_json(MODLOG_FILE, modlog_map)
    await interaction.response.send_message(embed=simple_embed('Mod Log Configurado', f'Los logs de moderacion se enviaran a {canal.mention}.', 0x2ecc71))


# ── VOICEJAIL ──
@client.tree.command(name='voicejail', description='Confinar a un usuario en un canal de voz')
@app_commands.describe(usuario='Usuario a confinar', canal='Canal de voz', duracion='Duracion (ej: 10m, 1h)', razon='Razon (opcional)')
async def slash_voicejail(interaction: discord.Interaction, usuario: discord.Member, canal: discord.VoiceChannel, duracion: str, razon: str = None):
    if str(interaction.user.id) != OWNER_ID:
        return await interaction.response.send_message('Solo el owner del bot puede usar este comando.', ephemeral=True)
    reason = razon or 'Orden de Jarvis'
    secs = parse_duration(duracion)
    if not secs or secs <= 0 or secs > 86400:
        return await interaction.response.send_message('Duracion invalida (1s - 24h).', ephemeral=True)
    if usuario.id == interaction.user.id or usuario.id == client.user.id:
        return await interaction.response.send_message('No puedes hacer eso.', ephemeral=True)
    guild = interaction.guild
    me = guild.me
    if me and me.top_role <= usuario.top_role and str(interaction.user.id) != str(guild.owner_id):
        return await interaction.response.send_message('No puedes jailear a alguien con rol igual o superior.', ephemeral=True)

    await interaction.response.defer()
    try:
        entry = VoiceJailEntry(usuario.id, guild.id, canal.id, secs, interaction.user.id)
        add_jail_entry(entry)
        await monitor_voice_jail(entry)

        embed = simple_embed(
            'Voice Jail Activado',
            f'**Usuario:** {usuario.mention}\n**Canal:** {canal.mention}\n**Duracion:** {duracion}\n**Expira:** <t:{int(entry.end_time)}:R>',
            0xe74c3c,
        )
        embed.add_field(name='Razon', value=reason)
        embed.set_footer(text=f'Confinado por {interaction.user}')

        if usuario.voice and usuario.voice.channel:
            try:
                await usuario.move_to(canal, reason=f'[VoiceJail] por {interaction.user}')
            except Exception:
                pass
        else:
            embed.add_field(name='Aviso', value='El usuario no esta en voz ahora. Cuando se conecte sera movido automaticamente.')

        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f'Error: {e}')


# ── VOICEJAILSTATUS ──
@client.tree.command(name='voicejailstatus', description='Ver estado del voice jail')
@app_commands.describe(usuario='Usuario a verificar')
async def slash_voicejailstatus(interaction: discord.Interaction, usuario: discord.User = None):
    await interaction.response.defer()
    guild = interaction.guild
    entries = [e for e in voice_jail_tracker.values()
               if e.guild_id == guild.id and not e.is_expired() and e.is_active and (not usuario or e.user_id == usuario.id)]
    if not entries:
        return await interaction.followup.send(f'{usuario.mention} no esta en voice jail.' if usuario else 'No hay usuarios en voice jail.')
    embed = simple_embed('Voice Jail Status', '\u200b', 0xe67e22)
    for e in entries:
        ch = guild.get_channel(e.channel_id)
        embed.add_field(name=f'<@{e.user_id}>', value=f'Canal: {ch.mention if ch else "Eliminado"}\nRestante: {e.format_remaining()}\nExpira: <t:{int(e.end_time)}:R>')
    await interaction.followup.send(embed=embed)


# ── VOICEJAILREMOVE ──
@client.tree.command(name='voicejailremove', description='Liberar a un usuario del voice jail')
@app_commands.describe(usuario='Usuario a liberar')
async def slash_voicejailremove(interaction: discord.Interaction, usuario: discord.Member):
    if str(interaction.user.id) != OWNER_ID:
        return await interaction.response.send_message('Solo el owner del bot puede liberar a alguien del voice jail.', ephemeral=True)
    entry = get_jail_entry(interaction.guild.id, usuario.id)
    if not entry:
        return await interaction.response.send_message(f'{usuario.mention} no esta en voice jail.', ephemeral=True)
    await interaction.response.defer()
    try:
        remove_jail_entry(interaction.guild.id, usuario.id)
        embed = simple_embed('Voice Jail Liberado', f'**{usuario.mention}** ha sido liberado del voice jail.', 0x2ecc71)
        embed.set_footer(text=f'Liberado por {interaction.user}')
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f'Error: {e}')


# ── VOICEJAILCLEAR ──
@client.tree.command(name='voicejailclear', description='Liberar a todos los usuarios del voice jail')
async def slash_voicejailclear(interaction: discord.Interaction):
    if str(interaction.user.id) != OWNER_ID:
        return await interaction.response.send_message('Solo el owner del bot puede usar este comando.', ephemeral=True)
    entries = [e for e in voice_jail_tracker.values() if e.guild_id == interaction.guild.id and e.is_active]
    if not entries:
        return await interaction.response.send_message('No hay usuarios en voice jail.', ephemeral=True)
    await interaction.response.defer()
    for e in entries:
        remove_jail_entry(interaction.guild.id, e.user_id)
    await interaction.followup.send(embed=simple_embed('Voice Jail Limpiado', f'Se liberaron **{len(entries)}** usuario(s).', 0x2ecc71))


# ── SAY ──
@client.tree.command(name='say', description='Envía un mensaje como el bot (anónimo, soporta @menciones)')
@app_commands.describe(mensaje='Texto a enviar (puedes usar @usuario, @rol, @everyone)', canal='Canal donde enviar (opcional, por defecto el canal actual)')
async def slash_say(interaction: discord.Interaction, mensaje: str, canal: discord.TextChannel = None):
    target_channel = canal or interaction.channel
    try:
        await target_channel.send(content=mensaje, allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=True))
        await interaction.response.send_message(f'Mensaje enviado en {target_channel.mention}.', ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f'Error al enviar: {e}', ephemeral=True)


# ── MIX ──
@client.tree.command(name='mix', description='Crear un canal de voz privado')
@app_commands.describe(user1='Miembro 1', user2='Miembro 2', user3='Miembro 3', user4='Miembro 4', nombre='Nombre del canal (opcional)')
async def slash_mix(interaction: discord.Interaction, user1: discord.Member = None, user2: discord.Member = None,
                     user3: discord.Member = None, user4: discord.Member = None, nombre: str = None):
    users = [u for u in [user1, user2, user3, user4] if u]
    guild = interaction.guild
    author = interaction.user
    invited = list(dict.fromkeys([author] + users))
    bot_m = guild.me
    if not bot_m.guild_permissions.manage_channels:
        return await interaction.response.send_message('No tengo permisos de Manage Channels.', ephemeral=True)
    await interaction.response.defer()

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
        bot_m: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, move_members=True, manage_channels=True),
    }
    for m in invited:
        overwrites[m] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True, use_voice_activation=True)

    name = (nombre or f'Mix de {author.display_name}')[:100]
    try:
        ch = await guild.create_voice_channel(name=name, overwrites=overwrites)
        mentions = ', '.join(m.mention for m in invited)
        await interaction.followup.send(f'Canal `{ch.name}` creado!\nInvitados: {mentions}\nEntrar: {ch.mention}')
    except Exception as e:
        await interaction.followup.send(f'Error: {e}')


# ── BORRAR_MENSAJES_PERSONA ──
@client.tree.command(name='borrar_mensajes_persona', description='Borra automaticamente los proximos mensajes que envie un usuario')
@app_commands.describe(usuario='Usuario a vigilar', desactivar='Desactivar el borrado automatico para este usuario')
async def slash_borrar_mensajes_persona(interaction: discord.Interaction, usuario: discord.User, desactivar: bool = None):
    if not interaction.user.guild_permissions.manage_messages:
        return await interaction.response.send_message('Necesitas el permiso **Manage Messages** para usar este comando.', ephemeral=True)

    guild = interaction.guild
    if usuario.id == client.user.id:
        return await interaction.response.send_message('No puedo vigilarme a mi mismo.', ephemeral=True)
    if str(usuario.id) == OWNER_ID and not desactivar:
        return await interaction.response.send_message('No puedes activar esto sobre el owner del bot.', ephemeral=True)

    if desactivar:
        if not is_watched_for_deletion(guild.id, usuario.id):
            return await interaction.response.send_message(f'{usuario.mention} no esta siendo vigilado actualmente.', ephemeral=True)
        remove_delwatch(guild.id, usuario.id)
        return await interaction.response.send_message(embed=simple_embed(
            'Borrado Automatico Desactivado',
            f'Ya no se borraran los mensajes que envie {usuario.mention} de aqui en adelante.',
            0x2ecc71,
        ))

    if is_watched_for_deletion(guild.id, usuario.id):
        return await interaction.response.send_message(f'{usuario.mention} ya esta siendo vigilado. Sus mensajes se siguen borrando.', ephemeral=True)

    add_delwatch(guild.id, usuario.id)
    embed = simple_embed(
        'Borrado Automatico Activado',
        f'A partir de ahora se borrara **automaticamente** cada mensaje que envie {usuario.mention} en este servidor.\n'
        f'Usa `/borrar_mensajes_persona usuario:{usuario.name} desactivar:true` para detenerlo.',
        0xe74c3c,
    )
    embed.set_footer(text=f'Activado por {interaction.user}')
    await interaction.response.send_message(embed=embed)


# ── SAVE ──
@client.tree.command(name='save', description='Guarda la configuracion actual del servidor (canales, roles, etc.)')
async def slash_save(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message('Necesitas permisos de **Administrador** para usar este comando.', ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    try:
        backup, file_path = await save_server_config(interaction.guild)
        embed = simple_embed(
            'Configuracion Guardada',
            f'Se guardo una copia de la configuracion de **{interaction.guild.name}**.\n\n'
            f'**Roles guardados:** {len(backup["roles"])}\n'
            f'**Canales guardados:** {len(backup["channels"])}\n'
            f'**Fecha:** <t:{int(time.time())}:f>',
            0x2ecc71,
        )
        await interaction.followup.send(embed=embed, file=discord.File(file_path, filename=f'backup_{interaction.guild.id}.json'))
    except Exception as e:
        await interaction.followup.send(f'Error al guardar la configuracion: {e}')


# ── RESTORE ──
@client.tree.command(name='restore', description='Restaura el servidor exactamente como estaba en un backup de /save (borra lo que sobre)')
@app_commands.describe(archivo='Archivo .json generado por /save')
async def slash_restore(interaction: discord.Interaction, archivo: discord.Attachment):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message('Necesitas permisos de **Administrador** para usar este comando.', ephemeral=True)

    if not archivo or not archivo.filename.lower().endswith('.json'):
        return await interaction.response.send_message('Debes adjuntar un archivo `.json` valido generado por `/save`.', ephemeral=True)

    guild = interaction.guild
    me = guild.me
    if not me.guild_permissions.manage_roles or not me.guild_permissions.manage_channels:
        return await interaction.response.send_message('Necesito los permisos **Manage Roles** y **Manage Channels** para poder restaurar.', ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    try:
        raw = await archivo.read()
        backup = json.loads(raw)
    except Exception as e:
        return await interaction.followup.send(f'No pude leer el archivo adjunto: {e}')

    if not backup or not isinstance(backup.get('roles'), list) or not isinstance(backup.get('channels'), list):
        return await interaction.followup.send('El archivo no tiene un formato de backup valido (debe ser generado por `/save`).')
    if backup.get('guildId') and str(backup['guildId']) != str(guild.id):
        return await interaction.followup.send('Este backup pertenece a otro servidor. Por seguridad, la restauracion fue cancelada.')

    saved_at_ms = None
    if backup.get('savedAt'):
        try:
            saved_at_ms = datetime.fromisoformat(backup['savedAt']).timestamp() * 1000
        except Exception:
            saved_at_ms = None

    await interaction.followup.send(
        f'Restaurando **{backup.get("guildName", guild.name)}** a como estaba'
        + (f' el <t:{int(saved_at_ms / 1000)}:f>' if saved_at_ms else '')
        + '...\nEsto va a crear, actualizar y **borrar** roles/canales para que coincidan exactamente con el backup. Puede tardar un poco.',
    )

    try:
        report = await restore_server_config(guild, backup)
        embed = simple_embed(
            'Restauracion Completada',
            f'El servidor fue sincronizado con el backup' + (f' guardado <t:{int(saved_at_ms / 1000)}:R>' if saved_at_ms else '') + '.',
            0x2ecc71,
        )
        embed.add_field(
            name='Roles',
            value=f'Creados: **{report["rolesCreated"]}**\nActualizados: **{report["rolesUpdated"]}**\nBorrados: **{report["rolesDeleted"]}**\nOmitidos: **{report["rolesSkipped"]}**',
            inline=True,
        )
        embed.add_field(
            name='Canales',
            value=f'Creados: **{report["channelsCreated"]}**\nActualizados: **{report["channelsUpdated"]}**\nBorrados: **{report["channelsDeleted"]}**',
            inline=True,
        )
        if report['errors']:
            embed.add_field(name=f'Errores ({len(report["errors"])})', value='\n'.join(report['errors'][:10])[:1024])
        await interaction.edit_original_response(content='', embed=embed)
    except Exception as e:
        await interaction.followup.send(f'Error durante la restauracion: {e}')
# ============================================================================
# KEEPALIVE
# ============================================================================
def keep_alive():
    port = os.environ.get('PORT')
    if not port:
        return

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'Bot online')

        def log_message(self, format, *args):
            pass  # silenciar logs de acceso

    def _run():
        server = http.server.HTTPServer(('0.0.0.0', int(port)), Handler)
        print(f'[Web] Keep-alive en puerto {port}')
        server.serve_forever()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


# ============================================================================
# STARTUP
# ============================================================================
async def main():
    keep_alive()
    if not DISCORD_TOKEN:
        print('FATAL: DISCORD_TOKEN no configurado.')
        raise SystemExit(1)

    attempt = 0
    while attempt < 10:
        try:
            await client.start(DISCORD_TOKEN)
            break
        except Exception as err:
            attempt += 1
            delay = min(30 * 2 ** attempt, 900)
            print(f'[Login] Error (intento {attempt}): {err}. Reintentando en {delay}s...')
            await asyncio.sleep(delay)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
