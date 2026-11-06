# ============================================================================
# jarvis_backup.py  -  /save y /restore completos
#
#   /save     guarda: roles (en orden, con sus permisos), canales (en orden, con sus
#             permisos) y los roles que tiene cada miembro.
#   /restore  deja el servidor como el archivo (roles, orden, permisos, canales) y le
#             devuelve a cada persona los roles que tenia en el backup.
#
# Instalacion: al final de tu bot, junto a los demas install():
#     import jarvis_backup
#     jarvis_backup.install(sys.modules[__name__])
#
# Solo reemplaza los comandos /save y /restore (y las funciones save_server_config /
# restore_server_config) del bot principal. No toca nada mas.
# ============================================================================
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands

M = None                    # modulo principal del bot (lo rellena install)
BACKUP_VERSION = 2
_RESTORING = set()          # servidores con una restauracion en curso

# tipos de canal de Discord
T_TEXT, T_VOICE, T_CATEGORY, T_NEWS, T_STAGE, T_FORUM, T_MEDIA = 0, 2, 4, 5, 13, 15, 16


# ============================================================================
# UTILIDADES
# ============================================================================
def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _short(e, limit=250):
    if M is not None and hasattr(M, 'short_error'):
        return M.short_error(e, limit)
    return str(e)[:limit]


def _bucket(channel_type):
    """Grupo en el que Discord ordena el canal dentro de su categoria."""
    if channel_type == T_CATEGORY:
        return 'category'
    if channel_type in (T_VOICE, T_STAGE):
        return 'voice'
    return 'text'


def _parse_colour(v):
    if isinstance(v, int):
        return discord.Colour(v)
    try:
        return discord.Colour(int(str(v or '').strip().lstrip('#'), 16))
    except ValueError:
        return discord.Colour.default()


def _write_backup(path, backup):
    """JSON legible: una linea por rol / canal / miembro (compacto aunque el server tenga miles de miembros)."""
    def line(x):
        return json.dumps(x, ensure_ascii=False, separators=(',', ':'))

    parts = []
    for key, val in backup.items():
        k = json.dumps(key)
        if isinstance(val, list) and val:
            parts.append(f'  {k}: [\n' + ',\n'.join('    ' + line(x) for x in val) + '\n  ]')
        else:
            parts.append(f'  {k}: {line(val)}')
    Path(path).write_text('{\n' + ',\n'.join(parts) + '\n}\n', encoding='utf-8')


def _ordered_roles(roles):
    """Roles del backup de arriba (mas alto) a abajo. Soporta backups viejos (sin 'order')."""
    roles = [r for r in roles if isinstance(r, dict) and r.get('id') is not None and r.get('name') != '@everyone']
    if roles and all(isinstance(r.get('order'), int) for r in roles):
        return sorted(roles, key=lambda r: r['order'])
    return sorted(roles, key=lambda r: -_int(r.get('position')))


def _names(names, limit=12):
    names = list(dict.fromkeys(names))
    txt = ', '.join(f'`{n}`' for n in names[:limit])
    return txt + (f' y {len(names) - limit} mas' if len(names) > limit else '')


# ============================================================================
# SAVE
# ============================================================================
def _channel_entry(c, idx):
    overwrites = []
    for target, ow in c.overwrites.items():
        allow, deny = ow.pair()
        overwrites.append({
            'id': str(target.id),
            'type': 0 if isinstance(target, discord.Role) else 1,    # 0 = rol, 1 = miembro
            'name': getattr(target, 'name', None),                   # solo informativo
            'allow': str(allow.value),
            'deny': str(deny.value),
        })
    cat = c.category
    return {
        'id': str(c.id),
        'name': c.name,
        'type': int(c.type.value),
        'position': c.position,
        'order': idx,                                   # orden en el que se ve en Discord
        'parentId': str(c.category_id) if c.category_id else None,
        'parentName': cat.name if cat else None,
        'topic': getattr(c, 'topic', None),
        'nsfw': bool(getattr(c, 'nsfw', False)),
        'rateLimitPerUser': getattr(c, 'slowmode_delay', None),
        'bitrate': getattr(c, 'bitrate', None),
        'userLimit': getattr(c, 'user_limit', None),
        'permissionOverwrites': overwrites,
    }


async def save_server_config(guild: discord.Guild):
    """Devuelve (backup: dict, ruta_del_archivo: str)."""
    # los roles de cada persona solo se pueden guardar si tengo a todos los miembros cargados
    if not guild.chunked:
        try:
            await guild.chunk()
        except Exception as e:
            print(f'[Backup] No pude cargar todos los miembros de {guild.name}: {e}')
    complete = bool(guild.chunked)

    # ---- roles: del mas alto al mas bajo, con sus permisos ----
    roles = []
    for idx, r in enumerate(sorted((r for r in guild.roles if not r.is_default()), reverse=True)):
        roles.append({
            'id': str(r.id),
            'name': r.name,
            'color': str(r.color),
            'hoist': r.hoist,
            'mentionable': r.mentionable,
            'position': r.position,
            'order': idx,                               # 0 = el rol mas alto
            'permissions': str(r.permissions.value),
            'managed': r.managed,
            'botId': str(r.tags.bot_id) if (r.tags and r.tags.bot_id) else None,
        })

    # ---- canales: en el mismo orden en el que se ven en Discord, con sus permisos ----
    channels = []
    for category, children in guild.by_category():
        if category is not None:
            channels.append(_channel_entry(category, len(channels)))
        for c in children:
            channels.append(_channel_entry(c, len(channels)))

    # ---- roles de cada persona ----
    members = []
    for m in guild.members:
        role_ids = [str(r.id) for r in sorted(m.roles, reverse=True) if not r.is_default()]
        if role_ids:
            members.append({
                'id': str(m.id),
                'name': m.name,
                'displayName': m.display_name,
                'bot': m.bot,
                'roleIds': role_ids,
            })

    bots = [
        {
            'id': str(member.id),
            'name': str(member),
            'displayName': member.display_name,
            'permissions': str(member.guild_permissions.value),
            'roleIds': [str(role.id) for role in member.roles if not role.is_default()],
            'inviteURL': (
                f'https://discord.com/oauth2/authorize?client_id={member.id}'
                '&scope=bot%20applications.commands'
            ),
        }
        for member in guild.members
        if member.bot
    ]

    backup = {
        'version': BACKUP_VERSION,
        'guildId': str(guild.id),
        'guildName': guild.name,
        'savedAt': datetime.now(timezone.utc).isoformat(),
        'iconURL': str(guild.icon.url) if guild.icon else None,
        'verificationLevel': str(guild.verification_level),
        'afkChannelId': str(guild.afk_channel.id) if guild.afk_channel else None,
        'afkTimeout': guild.afk_timeout,
        'everyonePermissions': str(guild.default_role.permissions.value),
        'membersTotal': guild.member_count,
        'membersComplete': complete,
        'botInventoryComplete': complete,
        'roles': roles,
        'channels': channels,
        'members': members,
        'bots': bots,
    }

    file_path = M.BACKUP_DIR / f'{guild.id}.json'
    _write_backup(file_path, backup)
    return backup, str(file_path)


# ============================================================================
# RESTORE
# ============================================================================
def _role_changes(role, br):
    """Solo lo que de verdad es distinto (asi no se gastan llamadas a la API en vano)."""
    ch = {}
    colour = _parse_colour(br.get('color'))
    perms = discord.Permissions(_int(br.get('permissions')))
    if br.get('name') and role.name != br['name']:
        ch['name'] = br['name']
    if role.colour.value != colour.value:
        ch['colour'] = colour
    if role.hoist != bool(br.get('hoist')):
        ch['hoist'] = bool(br.get('hoist'))
    if role.mentionable != bool(br.get('mentionable')):
        ch['mentionable'] = bool(br.get('mentionable'))
    if role.permissions.value != perms.value:
        ch['permissions'] = perms
    return ch


def _ow_sig(overwrites):
    sig = {}
    for target, ow in overwrites.items():
        allow, deny = ow.pair()
        sig[(isinstance(target, discord.Role), target.id)] = (allow.value, deny.value)
    return sig


def _clamp_bitrate(guild, value):
    value = _int(value)
    if value <= 0:
        return None
    return int(max(8000, min(value, guild.bitrate_limit)))


def _channel_changes(guild, ch, bc, overwrites, parent, parent_unresolved):
    c = {}
    if bc.get('name') and ch.name != bc['name']:
        c['name'] = bc['name']
    if isinstance(ch, (discord.TextChannel, discord.ForumChannel)):
        topic = bc.get('topic') or None
        if (ch.topic or None) != topic:
            c['topic'] = topic
        if bool(ch.nsfw) != bool(bc.get('nsfw')):
            c['nsfw'] = bool(bc.get('nsfw'))
        slow = _int(bc.get('rateLimitPerUser'))
        if _int(ch.slowmode_delay) != slow:
            c['slowmode_delay'] = slow
    elif isinstance(ch, discord.VoiceChannel):
        br = _clamp_bitrate(guild, bc.get('bitrate'))
        if br and ch.bitrate != br:
            c['bitrate'] = br
        limit = min(_int(bc.get('userLimit')), 99)
        if _int(ch.user_limit) != limit:
            c['user_limit'] = limit
    if not isinstance(ch, discord.CategoryChannel) and not parent_unresolved:
        want = parent.id if parent is not None else None
        if ch.category_id != want:
            c['category'] = parent          # None = sacarlo de su categoria
    if _ow_sig(ch.overwrites) != _ow_sig(overwrites):
        c['overwrites'] = overwrites
    return c


async def _create_channel(guild, bc, overwrites, parent):
    """Crea un canal como el del backup. Devuelve None si el tipo no esta soportado."""
    ctype = _int(bc.get('type'))
    kw = {'name': bc['name'], 'overwrites': overwrites, 'reason': '[Restore] Creado desde el backup'}
    if ctype == T_CATEGORY:
        return await guild.create_category(**kw)
    if parent is not None:
        kw['category'] = parent
    topic = bc.get('topic') or None
    slow = _int(bc.get('rateLimitPerUser'))

    if ctype in (T_TEXT, T_NEWS):
        if topic:
            kw['topic'] = topic
        if bc.get('nsfw'):
            kw['nsfw'] = True
        if slow:
            kw['slowmode_delay'] = slow
        if ctype == T_NEWS:
            try:
                return await guild.create_text_channel(news=True, **kw)
            except Exception:
                pass                        # sin comunidad no hay canales de anuncios: va como texto normal
        return await guild.create_text_channel(**kw)
    if ctype == T_VOICE:
        br = _clamp_bitrate(guild, bc.get('bitrate'))
        if br:
            kw['bitrate'] = br
        if _int(bc.get('userLimit')):
            kw['user_limit'] = min(_int(bc['userLimit']), 99)
        return await guild.create_voice_channel(**kw)
    if ctype == T_STAGE:
        return await guild.create_stage_channel(**kw)
    if ctype in (T_FORUM, T_MEDIA):
        if topic:
            kw['topic'] = topic
        if bc.get('nsfw'):
            kw['nsfw'] = True
        if slow:
            kw['slowmode_delay'] = slow
        if ctype == T_MEDIA:
            kw['media'] = True
        return await guild.create_forum(**kw)
    return None


async def _reorder_roles(guild, me, backup_roles, role_map):
    """Deja los roles que estan por debajo del mio en el mismo orden que el backup."""
    fresh = await guild.fetch_roles()                    # posiciones frescas, no las de la cache
    my_ids = {r.id for r in me.roles}
    top = max((r.position for r in fresh if r.id in my_ids and not r.is_default()), default=0)
    below = [r for r in fresh if not r.is_default() and r.position < top]   # lo unico que puedo mover
    if len(below) < 2:
        return True
    rank = {}
    for i, br in enumerate(backup_roles):
        role = role_map.get(str(br['id']))
        if role is not None:
            rank[role.id] = i
    # primero los del backup (en su orden), al final los que no estan en el backup
    below.sort(key=lambda r: (0, rank[r.id]) if r.id in rank else (1, -r.position))
    if [r.id for r in below] == [r.id for r in sorted(below, key=lambda r: -r.position)]:
        return True                                      # ya estaban en orden
    total = len(below)
    await guild.edit_role_positions(
        {r: total - i for i, r in enumerate(below)},     # el primero = posicion mas alta (justo debajo de mi rol)
        reason='[Restore] Orden de roles segun el backup',
    )
    return True


async def _reorder_channels(guild, backup_channels, channel_map):
    """Un solo PATCH con el orden de todos los canales (categorias, texto y voz por separado, como Discord)."""
    cats = sorted((bc for bc in backup_channels if _int(bc.get('type')) == T_CATEGORY),
                  key=lambda bc: (_int(bc.get('position')), _int(bc.get('id'))))
    cat_rank = {str(bc['id']): i for i, bc in enumerate(cats)}
    buckets = {'category': [], 'text': [], 'voice': []}
    for bc in backup_channels:
        ch = channel_map.get(str(bc['id']))
        if ch is None:
            continue
        ctype = _int(bc.get('type'))
        pid = bc.get('parentId')
        # los canales sueltos van primero, despues cada categoria en su orden
        rank = -1 if (ctype == T_CATEGORY or not pid) else cat_rank.get(str(pid), 10 ** 6)
        buckets[_bucket(ctype)].append((rank, _int(bc.get('position')), _int(bc.get('id')), ch))
    payload = []
    for items in buckets.values():
        items.sort(key=lambda t: t[:3])
        payload.extend({'id': t[3].id, 'position': idx} for idx, t in enumerate(items))
    if not payload:
        return True
    await M.client.http.bulk_channel_update(guild.id, payload, reason='[Restore] Orden de canales segun el backup')
    return True


async def restore_server_config(guild: discord.Guild, backup: dict, *,
                                assign_members=True, progress=None, keep_channel_ids=()):
    """Deja el servidor como el backup. Devuelve un dict con el resumen (report)."""
    me = guild.me
    keep_ids = {_int(x) for x in keep_channel_ids}
    report = {
        'rolesCreated': 0, 'rolesUpdated': 0, 'rolesUnchanged': 0, 'rolesDeleted': 0, 'rolesSkipped': 0,
        'rolesReordered': None, 'skippedRoles': [], 'managedMissing': [],
        'channelsCreated': 0, 'channelsUpdated': 0, 'channelsUnchanged': 0, 'channelsDeleted': 0,
        'channelsSkipped': 0, 'channelsReordered': None, 'keptChannels': [], 'overwritesSkipped': 0,
        'everyoneUpdated': False,
        'membersTotal': 0, 'membersUpdated': 0, 'membersOk': 0, 'membersMissing': 0, 'membersBlocked': 0,
        'membersFailed': 0, 'roleAssignments': 0, 'assignmentsBlocked': 0, 'membersNote': None,
        'errors': [], 'errorCount': 0,
    }

    def err(msg):
        report['errorCount'] += 1
        if len(report['errors']) < 40:
            report['errors'].append(msg)

    async def say(text):
        if progress is None:
            return
        try:
            await progress(text)
        except Exception:
            pass                            # p. ej. el token de la interaccion ya expiro (15 min)

    # ------------------------------------------------------------ @everyone --
    if backup.get('everyonePermissions') is not None:
        try:
            perms = discord.Permissions(_int(backup['everyonePermissions']))
            if guild.default_role.permissions.value != perms.value:
                await guild.default_role.edit(
                    permissions=perms, reason='[Restore] Permisos de @everyone sincronizados con el backup')
            report['everyoneUpdated'] = True
        except Exception as e:
            err(f'Permisos de @everyone: {_short(e)}')

    # ---------------------------------------------------------------- ROLES --
    await say('Fase 1/4: roles (crear, actualizar, borrar)...')
    backup_roles = _ordered_roles(backup.get('roles') or [])

    # roles que NO puedo tocar: el mio y los que estan por encima. Se calcula UNA vez, por ID:
    # crear/borrar roles cambia las posiciones, pero estos IDs siguen siendo los mismos.
    locked_ids = {r.id for r in guild.roles if r >= me.top_role}

    role_map = {}                           # ID del rol en el backup -> rol actual del servidor
    used = set()
    current = [r for r in guild.roles if not r.is_default()]

    def claim(br, role):
        role_map[str(br['id'])] = role
        used.add(role.id)

    for br in backup_roles:                 # 1) mismo ID
        r = guild.get_role(_int(br['id']))
        if r is not None and not r.is_default() and r.id not in used:
            claim(br, r)
    for br in backup_roles:                 # 2) rol de bot: el del mismo bot (aunque el ID haya cambiado)
        if str(br['id']) in role_map or not br.get('managed') or not br.get('botId'):
            continue
        r = next((r for r in current if r.managed and r.id not in used and r.tags is not None
                  and str(r.tags.bot_id) == str(br['botId'])), None)
        if r is not None:
            claim(br, r)
    for br in backup_roles:                 # 3) mismo nombre
        if str(br['id']) in role_map or br.get('managed'):
            continue
        r = next((r for r in current if not r.managed and r.id not in used and r.name == br.get('name')), None)
        if r is not None:
            claim(br, r)

    for br in backup_roles:                 # crear los que faltan / actualizar los que cambiaron
        name = br.get('name') or 'rol'
        role = role_map.get(str(br['id']))
        try:
            if role is not None:
                if role.managed:
                    continue
                if role.id in locked_ids:
                    report['rolesSkipped'] += 1
                    report['skippedRoles'].append(role.name)
                    continue
                changes = _role_changes(role, br)
                if not changes:
                    report['rolesUnchanged'] += 1
                    continue
                await role.edit(reason='[Restore] Sincronizado con el backup', **changes)
                report['rolesUpdated'] += 1
            else:
                if br.get('managed'):       # roles de bots: solo los crea Discord al volver a invitar al bot
                    report['managedMissing'].append(name)
                    continue
                role_map[str(br['id'])] = await guild.create_role(
                    name=name, colour=_parse_colour(br.get('color')), hoist=bool(br.get('hoist')),
                    mentionable=bool(br.get('mentionable')),
                    permissions=discord.Permissions(_int(br.get('permissions'))),
                    reason='[Restore] Creado desde el backup',
                )
                report['rolesCreated'] += 1
        except Exception as e:
            err(f'Rol "{name}": {_short(e)}')

    kept = {r.id for r in role_map.values()}
    for role in list(guild.roles):          # borrar los que no estan en el backup
        if role.is_default() or role.managed or role.id in kept:
            continue
        if role.id in locked_ids:
            report['rolesSkipped'] += 1
            report['skippedRoles'].append(role.name)
            continue
        try:
            await role.delete(reason='[Restore] Rol no presente en el backup')
            report['rolesDeleted'] += 1
        except Exception as e:
            err(f'Borrar rol "{role.name}": {_short(e)}')

    await say('Fase 2/4: ordenando los roles como en el backup...')
    try:
        report['rolesReordered'] = await _reorder_roles(guild, me, backup_roles, role_map)
    except Exception as e:
        report['rolesReordered'] = False
        err(f'Orden de roles: {_short(e)}')

    # -------------------------------------------------------------- CANALES --
    await say('Fase 3/4: canales (crear, actualizar, permisos, orden)...')
    backup_channels = [bc for bc in (backup.get('channels') or []) if isinstance(bc, dict) and bc.get('id') is not None]
    current_channels = list(guild.channels)
    channel_map = {}                        # ID del canal en el backup -> canal actual
    used_ch = set()

    for bc in backup_channels:              # 1) mismo ID y mismo tipo
        c = guild.get_channel(_int(bc['id']))
        if c is not None and int(c.type.value) == _int(bc.get('type')) and c.id not in used_ch:
            channel_map[str(bc['id'])] = c
            used_ch.add(c.id)
    for bc in backup_channels:              # 2) mismo nombre y tipo (primero el que esta en la misma categoria)
        if str(bc['id']) in channel_map:
            continue
        cands = [c for c in current_channels
                 if c.id not in used_ch and int(c.type.value) == _int(bc.get('type')) and c.name == bc.get('name')]
        if not cands:
            continue
        pick = next((c for c in cands if (c.category.name if c.category else None) == bc.get('parentName')), cands[0])
        channel_map[str(bc['id'])] = pick
        used_ch.add(pick.id)

    # canales que Discord no deja borrar (los de comunidad) y el canal donde se ejecuto el comando
    protected = {c.id for c in (guild.rules_channel, guild.public_updates_channel,
                                getattr(guild, 'safety_alerts_channel', None)) if c is not None}
    for c in current_channels:              # borrar los que no estan en el backup
        if c.id in used_ch:
            continue
        if c.id in keep_ids:
            report['keptChannels'].append(c.name)
            continue
        if c.id in protected:
            report['channelsSkipped'] += 1
            continue
        try:
            await c.delete(reason='[Restore] Canal no presente en el backup')
            report['channelsDeleted'] += 1
        except Exception as e:
            err(f'Borrar canal "{c.name}": {_short(e)}')

    old_guild_id = str(backup.get('guildId') or '')
    member_cache = {}

    async def lookup_member(uid):
        if uid not in member_cache:
            m = guild.get_member(uid)
            if m is None:
                try:
                    m = await guild.fetch_member(uid)
                except Exception:
                    m = None
            member_cache[uid] = m
        return member_cache[uid]

    async def build_overwrites(bc):
        """Permisos del canal segun el backup, apuntando a los roles/miembros ACTUALES."""
        result = {}
        for o in bc.get('permissionOverwrites') or []:
            oid = _int(o.get('id'))
            if _int(o.get('type')) == 0:
                target = guild.default_role if (str(oid) == old_guild_id or oid == guild.id) else role_map.get(str(oid))
            else:
                target = await lookup_member(oid)       # si ya no esta en el servidor, Discord rechazaria todo el canal
            if target is None:
                report['overwritesSkipped'] += 1
                continue
            result[target] = discord.PermissionOverwrite.from_pair(
                discord.Permissions(_int(o.get('allow'))), discord.Permissions(_int(o.get('deny'))))
        return result

    async def sync_channel(bc):
        name = bc.get('name') or 'canal'
        ch = channel_map.get(str(bc['id']))
        pid = bc.get('parentId')
        parent = channel_map.get(str(pid)) if pid else None
        parent_unresolved = bool(pid) and parent is None      # su categoria no se pudo crear: no lo muevo
        try:
            overwrites = await build_overwrites(bc)
            if ch is not None:
                changes = _channel_changes(guild, ch, bc, overwrites, parent, parent_unresolved)
                if not changes:
                    report['channelsUnchanged'] += 1
                    return
                await ch.edit(reason='[Restore] Sincronizado con el backup', **changes)
                report['channelsUpdated'] += 1
            else:
                new = await _create_channel(guild, bc, overwrites, parent)
                if new is None:
                    report['channelsSkipped'] += 1
                    err(f'Canal "{name}": el tipo {bc.get("type")} no se puede recrear')
                    return
                channel_map[str(bc['id'])] = new
                report['channelsCreated'] += 1
        except Exception as e:
            err(f'Canal "{name}": {_short(e)}')

    for bc in backup_channels:              # primero las categorias, para que los canales puedan entrar en ellas
        if _int(bc.get('type')) == T_CATEGORY:
            await sync_channel(bc)
    for bc in backup_channels:
        if _int(bc.get('type')) != T_CATEGORY:
            await sync_channel(bc)

    try:
        report['channelsReordered'] = await _reorder_channels(guild, backup_channels, channel_map)
    except Exception as e:
        report['channelsReordered'] = False
        err(f'Orden de canales: {_short(e)}')

    # ------------------------------------------------ AFK (config basica) --
    try:
        updates = {}
        if backup.get('afkTimeout') is not None:
            updates['afk_timeout'] = backup['afkTimeout']
        afk = channel_map.get(str(backup.get('afkChannelId'))) if backup.get('afkChannelId') else None
        if afk is not None:
            updates['afk_channel'] = afk
        if updates:
            await guild.edit(**updates)
    except Exception:
        pass

    # ------------------------------------------------- ROLES DE CADA PERSONA --
    if assign_members:
        entries = backup.get('members')
        if isinstance(entries, list):
            if backup.get('membersComplete') is False:
                report['membersNote'] = 'El backup se hizo sin tener cargados a todos los miembros: puede faltar alguien.'
        else:
            entries = backup.get('bots') or []
            report['membersNote'] = ('Este backup es de una version anterior: no trae los roles de las personas '
                                     '(solo los de los bots). Haz /save de nuevo para guardarlos.')
        entries = [e for e in entries if isinstance(e, dict)]
        total = len(entries)
        report['membersTotal'] = total

        if entries and not guild.chunked:
            try:
                await guild.chunk()
            except Exception as e:
                err(f'No pude cargar todos los miembros: {_short(e)}')

        last_say = 0.0
        for i, entry in enumerate(entries, 1):
            if time.monotonic() - last_say >= 5:
                last_say = time.monotonic()
                await say(f'Fase 4/4: devolviendo roles a las personas... {i}/{total}')
            member = guild.get_member(_int(entry.get('id')))
            if member is None:                          # ya no esta en el servidor
                report['membersMissing'] += 1
                continue
            have = {r.id for r in member.roles}
            missing = []
            for rid in entry.get('roleIds') or []:
                role = role_map.get(str(rid))
                if role is None or role.managed or role.is_default() or role.id in have:
                    continue
                if role.id in locked_ids:               # esta por encima de mi rol: no puedo darlo
                    report['assignmentsBlocked'] += 1
                    continue
                missing.append(role)
            if not missing:
                report['membersOk'] += 1
                continue
            try:
                await member.add_roles(*missing, reason='[Restore] Roles del backup')
                report['membersUpdated'] += 1
                report['roleAssignments'] += len(missing)
            except discord.Forbidden:                   # p. ej. su rol mas alto esta por encima del mio
                report['membersBlocked'] += 1
            except Exception as e:
                report['membersFailed'] += 1
                err(f'Roles de {member}: {_short(e)}')

    return report


# ============================================================================
# EMBEDS Y COMANDOS
# ============================================================================
def _yn(v):
    return {True: 'restaurado', False: 'FALLO (mira los errores)', None: 'no aplica'}[v]


def _restore_embed(report, backup, saved_at_ms, assigned):
    r = report
    ok = not r['errorCount']
    when = f' guardado <t:{int(saved_at_ms / 1000)}:R>' if saved_at_ms else ''
    e = M.simple_embed('Restauracion Completada' if ok else 'Restauracion Completada (con avisos)',
                       f'El servidor fue sincronizado con el backup{when}.', 0x2ecc71 if ok else 0xf39c12)
    e.add_field(
        name='Roles',
        value=(f'Creados: **{r["rolesCreated"]}** | Actualizados: **{r["rolesUpdated"]}** | '
               f'Sin cambios: **{r["rolesUnchanged"]}**\n'
               f'Borrados: **{r["rolesDeleted"]}** | Omitidos: **{r["rolesSkipped"]}**\n'
               f'Orden del backup: **{_yn(r["rolesReordered"])}**'),
        inline=False)
    e.add_field(
        name='Canales y permisos',
        value=(f'Creados: **{r["channelsCreated"]}** | Actualizados: **{r["channelsUpdated"]}** | '
               f'Sin cambios: **{r["channelsUnchanged"]}**\n'
               f'Borrados: **{r["channelsDeleted"]}** | Omitidos: **{r["channelsSkipped"]}**\n'
               f'Orden del backup: **{_yn(r["channelsReordered"])}**'
               + (f'\nPermisos que no pude aplicar (rol/persona que ya no existe): **{r["overwritesSkipped"]}**'
                  if r['overwritesSkipped'] else '')),
        inline=False)
    if backup.get('everyonePermissions') is not None:
        e.add_field(name='Permisos de @everyone',
                    value='Restaurados' if r['everyoneUpdated'] else 'No se pudieron restaurar', inline=True)
    if assigned:
        txt = (f'Recibieron roles: **{r["membersUpdated"]}** ({r["roleAssignments"]} roles dados)\n'
               f'Ya los tenian: **{r["membersOk"]}**\n'
               f'Ya no estan en el servidor: **{r["membersMissing"]}**')
        if r['membersBlocked']:
            txt += f'\nSin permiso para editarlos (jerarquia): **{r["membersBlocked"]}**'
        if r['assignmentsBlocked']:
            txt += f'\nRoles que no pude dar por estar encima del mio: **{r["assignmentsBlocked"]}**'
        if r['membersFailed']:
            txt += f'\nFallaron: **{r["membersFailed"]}**'
        if r['membersNote']:
            txt += f'\n{r["membersNote"]}'
        e.add_field(name=f'Roles de las personas ({r["membersTotal"]} en el backup)', value=txt[:1024], inline=False)
    saved_bots = backup.get('bots', [])
    if saved_bots:
        e.add_field(name='Bots del backup',
                    value=(f'{len(saved_bots)} bot(s) en el backup. Discord exige autorizar a cada bot a mano '
                           'para volver a agregarlo (su rol se crea al invitarlo).')[:1024],
                    inline=False)
    notes = []
    if r['skippedRoles']:
        notes.append('Roles que no pude tocar (estan por encima de mi rol): ' + _names(r['skippedRoles']) +
                     '\nSube mi rol al tope de la lista y vuelve a ejecutar /restore.')
    if r['managedMissing']:
        notes.append('Roles de bots que ya no existen: ' + _names(r['managedMissing']))
    if r['keptChannels']:
        notes.append('No borre el canal donde ejecutaste el comando: ' + _names(r['keptChannels']))
    if notes:
        e.add_field(name='Ojo', value='\n'.join(notes)[:1024], inline=False)
    if r['errors']:
        more = r['errorCount'] - len(r['errors'])
        e.add_field(name=f'Errores ({r["errorCount"]})',
                    value=('\n'.join(r['errors'][:8]) + (f'\n... y {more + len(r["errors"]) - 8} mas' if
                                                          r['errorCount'] > 8 else ''))[:1024],
                    inline=False)
    return e


async def _finish(interaction, content=None, embed=None):
    """Entrega el resultado. Si el token ya expiro (la restauracion paso de 15 min) lo manda por DM."""
    try:
        await interaction.edit_original_response(content=content, embed=embed)
        return
    except Exception:
        pass
    try:
        await interaction.user.send(content=content, embed=embed)
    except Exception as e:
        print(f'[Backup] No pude entregar el resultado de la restauracion: {e}')


def install(m):
    global M
    M = m
    # otros modulos que llamen a m.save_server_config / m.restore_server_config usaran estas versiones
    m.save_server_config = save_server_config
    m.restore_server_config = restore_server_config

    tree = m.client.tree
    for old in ('save', 'restore'):
        tree.remove_command(old)            # quita los /save y /restore antiguos del bot principal

    # ── SAVE ──
    @tree.command(name='save', description='Guarda roles (en orden y con permisos), canales con sus permisos y los roles de cada persona')
    @app_commands.guild_only()
    async def slash_save(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                'Necesitas permisos de **Administrador** para usar este comando.', ephemeral=True)
        guild = interaction.guild
        await interaction.response.defer(ephemeral=True)
        try:
            backup, file_path = await save_server_config(guild)
        except Exception as e:
            print(f'[Backup] /save: {e}')
            return await interaction.followup.send(f'Error al guardar la configuracion: {_short(e)}')

        desc = (f'Se guardo una copia de **{guild.name}**.\n\n'
                f'**Roles:** {len(backup["roles"])} (en orden y con sus permisos)\n'
                f'**Canales:** {len(backup["channels"])} (en orden y con sus permisos)\n'
                f'**Personas con roles:** {len(backup["members"])} de {backup["membersTotal"]} miembros\n'
                f'**Bots encontrados:** {len(backup["bots"])}\n')
        if not backup['membersComplete']:
            desc += '\nOjo: no pude cargar a todos los miembros, la lista de roles de personas puede estar incompleta.\n'
        desc += (f'\nLos bots no se pueden invitar automaticamente; el backup incluye enlaces para agregarlos a mano.'
                 f'\n**Fecha:** <t:{int(time.time())}:f>')
        embed = M.simple_embed('Configuracion Guardada', desc, 0x2ecc71)
        try:
            await interaction.followup.send(
                embed=embed, file=discord.File(file_path, filename=f'backup_{guild.id}.json'))
        except discord.HTTPException as e:
            embed.add_field(name='No pude adjuntar el archivo',
                            value=f'{_short(e)}\nQuedo guardado en el servidor del bot: `{file_path}`', inline=False)
            await interaction.followup.send(embed=embed)

    # ── RESTORE ──
    @tree.command(name='restore', description='Restaurar este servidor desde un backup (solo owner del bot)')
    @app_commands.guild_only()
    @app_commands.describe(
        archivo='Archivo .json generado por /save',
        asignar_roles='Devolverle a cada persona los roles que tenia en el backup (por defecto: si)')
    async def slash_restore(interaction: discord.Interaction, archivo: discord.Attachment, asignar_roles: bool = True):
        if str(interaction.user.id) != str(M.OWNER_ID):
            return await interaction.response.send_message(
                'Solo el owner del bot puede restaurar servidores.', ephemeral=True)
        if not archivo or not archivo.filename.lower().endswith('.json'):
            return await interaction.response.send_message(
                'Debes adjuntar un archivo `.json` valido generado por `/save`.', ephemeral=True)
        guild = interaction.guild
        me = guild.me
        if me is None or not (me.guild_permissions.manage_roles and me.guild_permissions.manage_channels):
            return await interaction.response.send_message(
                'Necesito los permisos **Manage Roles** y **Manage Channels** para poder restaurar.', ephemeral=True)
        if guild.id in _RESTORING:
            return await interaction.response.send_message(
                'Ya hay una restauracion en curso en este servidor.', ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        try:
            backup = json.loads(await archivo.read())
        except Exception as e:
            return await interaction.followup.send(f'No pude leer el archivo adjunto: {_short(e)}')
        if (not isinstance(backup, dict) or not isinstance(backup.get('roles'), list)
                or not isinstance(backup.get('channels'), list)):
            return await interaction.followup.send(
                'El archivo no tiene un formato de backup valido (debe ser generado por `/save`).')

        saved_at_ms = None
        if backup.get('savedAt'):
            try:
                saved_at_ms = datetime.fromisoformat(backup['savedAt']).timestamp() * 1000
            except Exception:
                saved_at_ms = None

        async def progress(text):
            await interaction.edit_original_response(content=text)

        await progress(
            f'Restaurando **{backup.get("guildName", guild.name)}**'
            + (f' a como estaba el <t:{int(saved_at_ms / 1000)}:f>' if saved_at_ms else '')
            + '...\nVoy a crear, actualizar y **borrar** roles/canales para que queden igual que el backup.')

        _RESTORING.add(guild.id)
        try:
            report = await restore_server_config(
                guild, backup, assign_members=asignar_roles, progress=progress,
                keep_channel_ids=[interaction.channel_id])
        except Exception as e:
            import traceback
            traceback.print_exc()
            return await _finish(interaction, content=f'Error durante la restauracion: {_short(e)}')
        finally:
            _RESTORING.discard(guild.id)

        await _finish(interaction, embed=_restore_embed(report, backup, saved_at_ms, asignar_roles))
