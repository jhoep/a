"""
jarvis_antinuke.py  -  Proteccion anti-nuke + anti-raid + auditoria detallada.

Dos amenazas distintas, un solo modulo
---------------------------------------
NUKE  (alguien de adentro: un admin comprometido, un bot/app con permisos de mas)
      -> crea/borra canales o roles en rafaga, cambia permisos, crea webhooks,
         expulsa o banea miembros en rafaga.
RAID  (alguien de afuera inundando el servidor)
      -> muchas cuentas entrando en poco tiempo, muchas de ellas recien creadas.

Para NUKE: se atribuye cada accion a su autor via el Audit Log, se registra SIEMPRE
(auditoria completa) y, si se pasa el umbral, se neutraliza: quarantine si es humano,
kick/ban si es bot o app.

Para RAID: se cuenta la rafaga de ingresos y, si se pasa el umbral, se entra en
"lockdown": verificacion al maximo + invitaciones pausadas durante un rato (se
revierte solo, y sobrevive a un reinicio del bot). Hay tambien un filtro aparte de
edad minima de cuenta, y un boton de panico manual.

Importante: esto NO reutiliza las funciones antiraid_* ya existentes en bot.py. Es
una implementacion nueva e independiente, pensada para convivir con ellas (podes
apagar las partes que se superpongan en /antiraid si no las queres duplicadas).

Seguridad de la configuracion: siguiendo el mismo criterio que usan los bots de
este tipo mas serios (p. ej. RB3 Guard), SOLO el dueno del servidor (ademas del
owner del bot y la whitelist de Jarvis) puede tocar la configuracion de este
modulo. Un admin comprometido no deberia poder apagar su propia proteccion.
`/antinuke estado` es la unica excepcion: cualquier admin lo puede consultar.

Instalacion: al final de bot.py, junto a los otros jarvis_*.install(...)
(despues de que existan quarantine_data, _ensure_quarantine_role, etc.):
    import jarvis_antinuke
    jarvis_antinuke.install(sys.modules[__name__])
"""
import asyncio
import time
from collections import deque
from datetime import datetime, timezone

import discord
from discord import app_commands

M = None   # modulo principal del bot (se asigna en install, por si hace falta algo mas adelante)

_NEEDED = (
    'client', 'DATA_DIR', 'load_json', 'save_json', 'OWNER_ID', 'JARVIS_WHITELIST',
    'modlog_map', 'JarvisError', 'short_error', 'simple_embed', 'build_help_embed',
    'quarantine_data', '_save_quarantines', '_ensure_quarantine_role', '_enforce_quarantine',
    '_restore_quarantine',
)

ANTINUKE_FILE = None     # se fija en install(): DATA_DIR / 'antinuke.json'
_data = {}                # { guildId(str): {config, 'raid': {...}} }
_installed = False

DEFAULTS = {
    'enabled': False,           # apagado por defecto en cada servidor; activar con /antinuke activar
    'log_channel': None,        # canal de logs propio; si no hay, usa el modlog general del servidor
    'auto_action': True,        # si es False: solo alerta, no castiga a nadie automaticamente
    'structure_threshold': 5,   # canales/roles creados o borrados...
    'structure_window': 30,     # ...en esta ventana (segundos) por la misma persona = posible nuke
    'webhook_threshold': 3,     # webhooks creados...
    'webhook_window': 60,       # ...en esta ventana (segundos) = posible abuso de webhooks
    'membership_threshold': 5,  # miembros expulsados o baneados...
    'membership_window': 30,    # ...en esta ventana (segundos) = posible purga maliciosa
    'member_action': 'quarantine',  # quarantine | kick | ban | none  (si quien dispara es humano)
    'bot_action': 'kick',           # kick | ban | none               (si quien dispara es bot/app)
    'exempt': [],               # IDs extra exentos, ademas de owner / whitelist / dueno del server
}

RAID_DEFAULTS = {
    'enabled': False,
    'join_threshold': 8,        # cuentas entrando...
    'join_window': 10,          # ...en esta ventana (segundos) = posible raid
    'join_action': 'lockdown',  # lockdown | kick | ban | none  (que hacer con la rafaga de ingresos)
    'auto_lockdown': True,      # ademas de join_action, subir verificacion + pausar invitaciones
    'lockdown_minutes': 15,     # cuanto dura el lockdown automatico antes de revertirse solo
    'min_account_age_days': 0,  # 0 = desactivado; si no, rechaza cuentas mas nuevas que esto
    'min_account_age_action': 'kick',  # kick | ban
    'log_channel': None,        # si no hay, usa el mismo canal que el resto de antinuke / modlog
}

# Permisos que, si un role_update se los da a un rol que no los tenia, son razon de alarma.
DANGEROUS_PERMS = (
    'administrator', 'manage_guild', 'manage_roles', 'manage_channels',
    'manage_webhooks', 'ban_members', 'kick_members', 'mention_everyone',
)

# Estado en memoria (ventanas de segundos/minutos: no hace falta guardarlas en disco).
_hits = {}                  # (guildId, userId) -> deque[(ts, etiqueta)]          -> lado NUKE
_seen_webhook_entries = {}  # guildId -> set(auditLogEntryId) ya procesados
_join_hits = {}             # guildId -> deque[(ts, memberId)]                   -> lado RAID
_lockdown_state = {}        # guildId -> {'prev_level': int, 'manual': bool, 'until': ts|None}


# ============================================================================
# CONFIG POR SERVIDOR
# ============================================================================
def _cfg(guild_id):
    cfg = _data.setdefault(str(guild_id), {})
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, list(v) if isinstance(v, list) else v)
    return cfg


def _raid_cfg(guild_id):
    cfg = _cfg(guild_id)
    raid = cfg.setdefault('raid', {})
    for k, v in RAID_DEFAULTS.items():
        raid.setdefault(k, v)
    return raid


def _save():
    save_json(ANTINUKE_FILE, _data)


def _is_exempt(guild, user_id, cfg):
    user_id = int(user_id)
    if client.user and user_id == client.user.id:
        return True
    if str(user_id) == str(OWNER_ID) or str(user_id) in JARVIS_WHITELIST:
        return True
    if guild.owner_id and user_id == guild.owner_id:
        return True
    return str(user_id) in {str(x) for x in cfg.get('exempt', [])}


# ============================================================================
# AUDITORIA: encontrar quien hizo que
# ============================================================================
async def _find_actor(guild, action, target_id=None, within=15):
    """Busca en el audit log la entrada mas reciente de `action` (y de `target_id` si se da),
    siempre que tenga menos de `within` segundos. Devuelve el AuditLogEntry o None."""
    try:
        async for entry in guild.audit_logs(limit=15, action=action):
            age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
            if age > within:
                break   # el audit log viene del mas nuevo al mas viejo: no hay nada mas util despues
            if target_id is None or getattr(entry.target, 'id', None) == target_id:
                return entry
    except discord.Forbidden:
        print(f'[AntiNuke] Sin permiso de Ver registro de auditoria en {guild.id}.')
    except discord.HTTPException as e:
        print(f'[AntiNuke] No pude leer el audit log ({action}) en {guild.id}: {e}')
    return None


async def _send_log(guild, cfg, embed):
    channel_id = cfg.get('log_channel') or modlog_map.get(str(guild.id))
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.DiscordException as e:
        print(f'[AntiNuke] No pude escribir en el canal de logs de {guild.id}: {e}')


async def _log_audit(guild, cfg, title, color, actor, reason, fields):
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name='Quien', value=f'{actor} (`{actor.id}`)', inline=True)
    embed.add_field(name='Cuando', value=f'<t:{int(time.time())}:f>', inline=True)
    for name, value in fields.items():
        embed.add_field(name=name, value=str(value)[:1024], inline=False)
    if reason:
        embed.add_field(name='Razon indicada', value=str(reason)[:1024], inline=False)
    embed.timestamp = datetime.now(timezone.utc)
    await _send_log(guild, cfg, embed)


# ============================================================================
# LADO NUKE: CONTADOR DE RAFAGAS Y RESPUESTA AUTOMATICA
# ============================================================================
async def _register_hit(guild, cfg, actor, bucket, label):
    key = (guild.id, actor.id)
    dq = _hits.setdefault(key, deque(maxlen=200))
    now = time.time()
    dq.append((now, label))
    window, threshold = {
        'webhook': (cfg['webhook_window'], cfg['webhook_threshold']),
        'membership': (cfg['membership_window'], cfg['membership_threshold']),
    }.get(bucket, (cfg['structure_window'], cfg['structure_threshold']))
    recent = [item for item in dq if now - item[0] <= window]
    if len(recent) < threshold:
        return
    dq.clear()
    await _neutralize(guild, cfg, actor, recent)


async def _apply_quarantine(guild, member):
    existing = next(
        (e for e in quarantine_data if e['guildId'] == str(guild.id) and e['userId'] == str(member.id)),
        None,
    )
    if existing is not None:
        await _enforce_quarantine(guild, member, existing)
        return
    role = await _ensure_quarantine_role(guild, guild.me)
    entry = {
        'guildId': str(guild.id),
        'userId': str(member.id),
        'roleId': str(role.id),
        'roleIds': [str(r.id) for r in member.roles if not r.is_default() and r.id != role.id],
        'channelOverwrites': [],
        'until': None,            # permanente a proposito: requiere revision humana (/antinuke liberar)
        'source': 'antinuke',
    }
    quarantine_data.append(entry)
    try:
        _save_quarantines()
    except OSError:
        quarantine_data.remove(entry)
        raise
    await _enforce_quarantine(guild, member, entry)


async def _neutralize(guild, cfg, actor, recent_hits):
    member = guild.get_member(actor.id)
    is_bot = bool(getattr(actor, 'bot', False))
    action_label = 'ninguna'
    result_text = 'La respuesta automatica esta desactivada (`auto_action`); revisa esto a mano cuanto antes.'

    if cfg['auto_action']:
        setting = (cfg['bot_action'] if is_bot else cfg['member_action']) or 'none'
        try:
            if member is not None:
                if is_bot:
                    if setting == 'kick':
                        await guild.kick(member, reason='[AntiNuke] Rafaga de cambios destructivos detectada')
                        action_label, result_text = 'kick', f'Expulse a {member} (bot/app) del servidor.'
                    elif setting == 'ban':
                        await guild.ban(member, reason='[AntiNuke] Rafaga de cambios destructivos detectada', delete_message_seconds=0)
                        action_label, result_text = 'ban', f'Banee a {member} (bot/app) del servidor.'
                    else:
                        result_text = 'La accion automatica para bots esta en "ninguna"; revisa esto a mano.'
                else:
                    if setting == 'quarantine':
                        await _apply_quarantine(guild, member)
                        action_label = 'quarantine'
                        result_text = (f'Puse a {member.mention} en quarantine: le quite sus roles y le bloquee '
                                       'todos los canales. Usa `/antinuke liberar` si esto fue un error.')
                    elif setting == 'kick':
                        await guild.kick(member, reason='[AntiNuke] Rafaga de cambios destructivos detectada')
                        action_label, result_text = 'kick', f'Expulse a {member}.'
                    elif setting == 'ban':
                        await guild.ban(member, reason='[AntiNuke] Rafaga de cambios destructivos detectada', delete_message_seconds=0)
                        action_label, result_text = 'ban', f'Banee a {member}.'
                    else:
                        result_text = 'La accion automatica para humanos esta en "ninguna"; revisa esto a mano.'
            else:
                # ya no esta en el servidor (patron clasico: nukear y salirse antes de que lo frenen)
                if setting not in ('none', None):
                    await guild.ban(discord.Object(id=actor.id),
                                     reason='[AntiNuke] Rafaga de cambios destructivos (ya no esta en el servidor)',
                                     delete_message_seconds=0)
                    action_label = 'ban preventivo'
                    result_text = f'{actor} ya no estaba en el servidor; lo banee preventivamente por si intenta volver.'
                else:
                    result_text = f'{actor} ya no esta en el servidor. No se aplico nada (accion en "ninguna").'
        except JarvisError as e:
            result_text = f'Detecte la rafaga pero no pude actuar: {e}'
        except discord.Forbidden:
            result_text = (f'Detecte la rafaga pero NO pude actuar sobre {actor}: me falta permiso o mi rol '
                            'esta por debajo del suyo. **Revisalo a mano ahora mismo.**')
        except discord.HTTPException as e:
            result_text = f'Detecte la rafaga pero la accion automatica fallo: {short_error(e)}'

    embed = discord.Embed(
        title='🚨 Posible nuke detectado',
        description=f'**{actor}** (`{actor.id}`) hizo **{len(recent_hits)}** cambios destructivos en poco tiempo.',
        color=0xe74c3c,
    )
    recientes = '\n'.join(f'- {label}' for _, label in list(recent_hits)[-10:])
    embed.add_field(name='Acciones detectadas', value=recientes or 'N/D', inline=False)
    embed.add_field(name='Respuesta', value=result_text, inline=False)
    embed.set_footer(text=f'Accion automatica: {action_label}')
    embed.timestamp = datetime.now(timezone.utc)
    await _send_log(guild, cfg, embed)
    print(f'[AntiNuke] {guild.id}: {actor} ({actor.id}) -> {action_label}. {result_text}')


# ============================================================================
# EVENTOS NUKE: CANALES
# ============================================================================
async def _on_guild_channel_create(channel):
    guild = getattr(channel, 'guild', None)
    if guild is None:
        return
    cfg = _cfg(guild.id)
    if not cfg['enabled']:
        return
    entry = await _find_actor(guild, discord.AuditLogAction.channel_create, target_id=channel.id)
    if entry is None or entry.user is None or _is_exempt(guild, entry.user.id, cfg):
        return
    nombre = getattr(channel, 'name', str(channel.id))
    await _log_audit(guild, cfg, '🗂️ Canal creado', 0x95a5a6, entry.user, entry.reason,
                      {'Canal': f'#{nombre} (`{channel.id}`)'})
    await _register_hit(guild, cfg, entry.user, 'structure', f'creo el canal #{nombre}')


async def _on_guild_channel_delete(channel):
    guild = getattr(channel, 'guild', None)
    if guild is None:
        return
    cfg = _cfg(guild.id)
    if not cfg['enabled']:
        return
    entry = await _find_actor(guild, discord.AuditLogAction.channel_delete, target_id=channel.id)
    if entry is None or entry.user is None or _is_exempt(guild, entry.user.id, cfg):
        return
    nombre = getattr(channel, 'name', str(channel.id))
    await _log_audit(guild, cfg, '🗑️ Canal borrado', 0xe67e22, entry.user, entry.reason,
                      {'Canal': f'#{nombre} (`{channel.id}`)'})
    await _register_hit(guild, cfg, entry.user, 'structure', f'borro el canal #{nombre}')


# ============================================================================
# EVENTOS NUKE: ROLES
# ============================================================================
async def _on_guild_role_create(role):
    guild = role.guild
    cfg = _cfg(guild.id)
    if not cfg['enabled']:
        return
    entry = await _find_actor(guild, discord.AuditLogAction.role_create, target_id=role.id)
    if entry is None or entry.user is None or _is_exempt(guild, entry.user.id, cfg):
        return
    await _log_audit(guild, cfg, '🎭 Rol creado', 0x95a5a6, entry.user, entry.reason,
                      {'Rol': f'{role.name} (`{role.id}`)'})
    await _register_hit(guild, cfg, entry.user, 'structure', f'creo el rol {role.name}')


async def _on_guild_role_delete(role):
    guild = role.guild
    cfg = _cfg(guild.id)
    if not cfg['enabled']:
        return
    entry = await _find_actor(guild, discord.AuditLogAction.role_delete, target_id=role.id)
    if entry is None or entry.user is None or _is_exempt(guild, entry.user.id, cfg):
        return
    await _log_audit(guild, cfg, '🎭 Rol borrado', 0xe67e22, entry.user, entry.reason,
                      {'Rol': f'{role.name} (`{role.id}`)'})
    await _register_hit(guild, cfg, entry.user, 'structure', f'borro el rol {role.name}')


async def _on_guild_role_update(before, after):
    if before.permissions.value == after.permissions.value:
        return   # cambio cosmetico (color, nombre, hoist...): no es relevante para seguridad
    guild = after.guild
    cfg = _cfg(guild.id)
    if not cfg['enabled']:
        return
    entry = await _find_actor(guild, discord.AuditLogAction.role_update, target_id=after.id)
    if entry is None or entry.user is None or _is_exempt(guild, entry.user.id, cfg):
        return
    gained = [p for p in DANGEROUS_PERMS
              if not getattr(before.permissions, p) and getattr(after.permissions, p)]
    detalle = f'Rol **{after.name}**: ' + (f'gano **{", ".join(gained)}**' if gained else 'se modificaron permisos')
    await _log_audit(guild, cfg, '⚠️ Permisos de rol modificados' if gained else '🎭 Permisos de rol modificados',
                      0xe74c3c if gained else 0x95a5a6, entry.user, entry.reason, {'Detalle': detalle})
    if gained:
        await _register_hit(guild, cfg, entry.user, 'structure', f'{after.name} gano {", ".join(gained)}')


# ============================================================================
# EVENTOS NUKE: WEBHOOKS (vector clasico para seguir spameando aunque ya te hayan echado)
# ============================================================================
async def _on_webhooks_update(channel):
    guild = channel.guild
    cfg = _cfg(guild.id)
    if not cfg['enabled']:
        return
    seen = _seen_webhook_entries.setdefault(guild.id, set())
    try:
        async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.webhook_create):
            age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
            if age > 60:
                break
            if entry.id in seen:
                continue
            seen.add(entry.id)
            if len(seen) > 100:
                seen.clear()
                seen.add(entry.id)
            if entry.user is None or _is_exempt(guild, entry.user.id, cfg):
                continue
            nombre = getattr(entry.target, 'name', None) or 'webhook'
            await _log_audit(guild, cfg, '🪝 Webhook creado', 0xe67e22, entry.user, entry.reason,
                              {'Canal': channel.mention if hasattr(channel, 'mention') else str(channel),
                               'Webhook': nombre})
            await _register_hit(guild, cfg, entry.user, 'webhook', f'creo el webhook "{nombre}" en #{channel}')
    except discord.Forbidden:
        print(f'[AntiNuke] Sin permiso de Ver registro de auditoria en {guild.id} (webhooks).')
    except discord.HTTPException as e:
        print(f'[AntiNuke] No pude leer el audit log (webhooks) en {guild.id}: {e}')


# ============================================================================
# EVENTOS NUKE: EXPULSION/BANEO MASIVO DE MIEMBROS
# ============================================================================
async def _on_member_remove_check_kick(member):
    guild = member.guild
    cfg = _cfg(guild.id)
    if not cfg['enabled']:
        return
    # on_member_remove se dispara tanto si alguien se va solo como si lo expulsan o banean.
    # Si no hay una entrada de "kick" reciente a su nombre, fue una salida normal: no hacer nada.
    entry = await _find_actor(guild, discord.AuditLogAction.kick, target_id=member.id, within=5)
    if entry is None or entry.user is None or _is_exempt(guild, entry.user.id, cfg):
        return
    await _log_audit(guild, cfg, '👢 Miembro expulsado', 0xe67e22, entry.user, entry.reason,
                      {'Miembro': f'{member} (`{member.id}`)'})
    await _register_hit(guild, cfg, entry.user, 'membership', f'expulso a {member}')


async def _on_member_ban_check(guild, user):
    cfg = _cfg(guild.id)
    if not cfg['enabled']:
        return
    entry = await _find_actor(guild, discord.AuditLogAction.ban, target_id=user.id)
    if entry is None or entry.user is None or _is_exempt(guild, entry.user.id, cfg):
        return
    await _log_audit(guild, cfg, '🔨 Miembro baneado', 0xe67e22, entry.user, entry.reason,
                      {'Miembro': f'{user} (`{user.id}`)'})
    await _register_hit(guild, cfg, entry.user, 'membership', f'baneo a {user}')


# ============================================================================
# LADO RAID: LOCKDOWN (subir verificacion + pausar invitaciones)
# ============================================================================
def _persist_lockdown(guild_id):
    raid = _raid_cfg(guild_id)
    state = _lockdown_state.get(guild_id)
    if state is None:
        raid.pop('_lockdown', None)
    else:
        raid['_lockdown'] = state
    _save()


async def _enter_lockdown(guild, minutes, manual=False, motivo=''):
    if guild.id in _lockdown_state:
        return False
    try:
        prev_level = guild.verification_level
        await guild.edit(verification_level=discord.VerificationLevel.highest, invites_disabled=True,
                          reason=f'[AntiRaid] Lockdown: {motivo}'[:512])
    except discord.Forbidden:
        if manual:
            raise JarvisError('Necesito el permiso **Manage Server** para activar el lockdown.')
        print(f'[AntiRaid] Sin permiso Manage Server para lockdown en {guild.id}.')
        return False
    except discord.HTTPException as e:
        if manual:
            raise JarvisError(f'No pude activar el lockdown: {short_error(e)}')
        print(f'[AntiRaid] No pude activar el lockdown en {guild.id}: {e}')
        return False
    _lockdown_state[guild.id] = {
        'prev_level': int(prev_level.value), 'manual': manual,
        'until': (time.time() + minutes * 60) if not manual else None,
    }
    _persist_lockdown(guild.id)
    if not manual:
        asyncio.create_task(_schedule_unlock(guild.id, minutes))
    return True


async def _exit_lockdown(guild):
    state = _lockdown_state.get(guild.id)
    if state is None:
        return False
    level = discord.VerificationLevel(state['prev_level'])
    try:
        await guild.edit(verification_level=level, invites_disabled=False, reason='[AntiRaid] Fin del lockdown')
    except discord.DiscordException as e:
        print(f'[AntiRaid] No pude revertir el lockdown en {guild.id}: {e}')
    _lockdown_state.pop(guild.id, None)
    _persist_lockdown(guild.id)
    return True


async def _schedule_unlock(guild_id, minutes):
    await asyncio.sleep(minutes * 60)
    state = _lockdown_state.get(guild_id)
    if state is None or state.get('manual'):
        return
    guild = client.get_guild(guild_id)
    if guild is not None:
        await _exit_lockdown(guild)


async def _resume_lockdowns_on_ready():
    """Si el bot se reinicio en medio de un lockdown automatico, lo retoma donde iba
    (o lo revierte ya mismo si el tiempo ya se cumplio)."""
    for gid_str, cfg in list(_data.items()):
        state = (cfg.get('raid') or {}).get('_lockdown')
        if not state:
            continue
        gid = int(gid_str)
        _lockdown_state[gid] = state
        if state.get('manual'):
            continue
        guild = client.get_guild(gid)
        if guild is None:
            continue
        remaining = (state.get('until') or 0) - time.time()
        if remaining <= 0:
            asyncio.create_task(_exit_lockdown(guild))
        else:
            asyncio.create_task(_schedule_unlock(gid, remaining / 60))


# ============================================================================
# EVENTOS RAID: INGRESOS
# ============================================================================
async def _on_member_join_raid(member):
    guild = member.guild
    rcfg = _raid_cfg(guild.id)
    if not rcfg['enabled']:
        return

    # 1) cuenta la rafaga de ingresos, siempre (independiente del filtro de edad de abajo)
    dq = _join_hits.setdefault(guild.id, deque(maxlen=500))
    now = time.time()
    dq.append((now, member.id))
    recent = [x for x in dq if now - x[0] <= rcfg['join_window']]
    raid_triggered = len(recent) >= rcfg['join_threshold']
    if raid_triggered:
        dq.clear()

    if _is_exempt(guild, member.id, rcfg):
        if raid_triggered:
            await _trigger_raid_response(guild, rcfg, recent)
        return

    # 2) filtro individual: cuenta demasiado nueva (no depende de la rafaga)
    if rcfg['min_account_age_days'] > 0:
        age_days = (datetime.now(timezone.utc) - member.created_at).total_seconds() / 86400
        if age_days < rcfg['min_account_age_days']:
            try:
                if rcfg['min_account_age_action'] == 'ban':
                    await member.ban(reason=f'[AntiRaid] Cuenta de {age_days:.1f} dias', delete_message_seconds=0)
                    accion = 'ban'
                else:
                    await member.kick(reason=f'[AntiRaid] Cuenta de {age_days:.1f} dias')
                    accion = 'kick'
                await _log_audit(guild, rcfg, '🆕 Cuenta muy nueva rechazada', 0xe67e22, member, None,
                                  {'Edad de la cuenta': f'{age_days:.1f} dias (minimo {rcfg["min_account_age_days"]})',
                                   'Accion': accion})
            except discord.Forbidden:
                print(f'[AntiRaid] Sin permiso para actuar sobre cuentas nuevas en {guild.id}.')
            except discord.HTTPException as e:
                print(f'[AntiRaid] Fallo al actuar sobre cuenta nueva en {guild.id}: {e}')

    if raid_triggered:
        await _trigger_raid_response(guild, rcfg, recent)


async def _trigger_raid_response(guild, rcfg, recent_joins):
    action = rcfg['join_action']
    acted = 0
    if action in ('kick', 'ban'):
        for _, uid in recent_joins:
            m = guild.get_member(uid)
            if m is None or _is_exempt(guild, uid, rcfg):
                continue
            try:
                if action == 'ban':
                    await m.ban(reason='[AntiRaid] Parte de una rafaga de ingresos', delete_message_seconds=0)
                else:
                    await m.kick(reason='[AntiRaid] Parte de una rafaga de ingresos')
                acted += 1
            except discord.Forbidden:
                pass

    lockdown_applied = False
    if rcfg['auto_lockdown'] and action != 'none':
        lockdown_applied = await _enter_lockdown(guild, rcfg['lockdown_minutes'], motivo='rafaga de ingresos detectada')

    resumen = []
    if action in ('kick', 'ban'):
        verbo = 'Banee' if action == 'ban' else 'Expulse'
        resumen.append(f'{verbo} a {acted} de esas cuentas.')
    if lockdown_applied:
        resumen.append(f'Subi la verificacion al maximo y pause las invitaciones por {rcfg["lockdown_minutes"]} min '
                        '(se revierte solo).')
    if not resumen:
        resumen.append('Solo se registro la alerta (respuesta automatica en "ninguna" o sin lockdown).')

    embed = discord.Embed(
        title='🚨 Posible raid detectado',
        description=f'**{len(recent_joins)}** cuentas se unieron en los ultimos {rcfg["join_window"]}s.',
        color=0xe74c3c,
    )
    embed.add_field(name='Respuesta', value='\n'.join(resumen), inline=False)
    embed.timestamp = datetime.now(timezone.utc)
    await _send_log(guild, rcfg, embed)
    print(f'[AntiRaid] {guild.id}: rafaga de {len(recent_joins)} ingresos. {" ".join(resumen)}')


# ============================================================================
# PERMISOS PARA USAR LOS COMANDOS (solo dueno del server + owner/whitelist del bot)
# ============================================================================
def _an_can_manage(user, guild):
    if str(user.id) == str(OWNER_ID) or str(user.id) in JARVIS_WHITELIST:
        return True
    return guild.owner_id == user.id


def _an_can_view(user, guild):
    # /antinuke estado es de solo lectura: cualquier admin lo puede consultar.
    return _an_can_manage(user, guild) or user.guild_permissions.administrator


async def _an_slash(interaction, fn, view_only=False):
    checker = _an_can_view if view_only else _an_can_manage
    if not checker(interaction.user, interaction.guild):
        msg = ('Necesitas ser el dueno del servidor (o estar en la whitelist de Jarvis).' if not view_only else
               'Necesitas ser administrador.')
        return await interaction.response.send_message(msg, ephemeral=True)
    try:
        embed = fn()
    except JarvisError as e:
        return await interaction.response.send_message(str(e), ephemeral=True)
    except Exception as e:
        print(f'[AntiNuke] slash: {e}')
        return await interaction.response.send_message(f'Error inesperado: {short_error(e)}', ephemeral=True)
    await interaction.response.send_message(embed=embed)


# ============================================================================
# SLASH COMMANDS: /antinuke
# ============================================================================
antinuke_group = app_commands.Group(name='antinuke', description='Proteccion anti-nuke, anti-raid y auditoria', guild_only=True)
raid_group = app_commands.Group(name='raid', description='Proteccion anti-raid (ingresos masivos)', parent=antinuke_group)


def _an_status_embed(guild):
    cfg = _cfg(guild.id)
    rcfg = _raid_cfg(guild.id)
    embed = simple_embed(f'Anti-Nuke / Anti-Raid · {guild.name}', color=0x2ecc71 if cfg['enabled'] else 0x95a5a6)

    embed.add_field(name='── NUKE (adentro) ──', value='\u200b', inline=False)
    embed.add_field(name='Estado', value='🟢 Activado' if cfg['enabled'] else '🔴 Desactivado', inline=True)
    embed.add_field(name='Respuesta automatica', value='Si' if cfg['auto_action'] else 'Solo alerta', inline=True)
    canal = f'<#{cfg["log_channel"]}>' if cfg['log_channel'] else 'canal de mod-logs del servidor'
    embed.add_field(name='Canal de logs', value=canal, inline=True)
    embed.add_field(name='Canales/roles', value=f'{cfg["structure_threshold"]} en {cfg["structure_window"]}s', inline=True)
    embed.add_field(name='Webhooks', value=f'{cfg["webhook_threshold"]} en {cfg["webhook_window"]}s', inline=True)
    embed.add_field(name='Expulsiones/baneos', value=f'{cfg["membership_threshold"]} en {cfg["membership_window"]}s', inline=True)
    embed.add_field(name='Accion (humanos)', value=cfg['member_action'], inline=True)
    embed.add_field(name='Accion (bots/apps)', value=cfg['bot_action'], inline=True)
    exentos = cfg.get('exempt') or []
    embed.add_field(name='Exentos extra', value=('\n'.join(f'<@{x}>' for x in exentos[:10]) or 'Ninguno'), inline=True)

    embed.add_field(name='── RAID (afuera) ──', value='\u200b', inline=False)
    embed.add_field(name='Estado', value='🟢 Activado' if rcfg['enabled'] else '🔴 Desactivado', inline=True)
    embed.add_field(name='Ingresos', value=f'{rcfg["join_threshold"]} en {rcfg["join_window"]}s → {rcfg["join_action"]}', inline=True)
    edad = f'{rcfg["min_account_age_days"]} dias → {rcfg["min_account_age_action"]}' if rcfg['min_account_age_days'] else 'Desactivado'
    embed.add_field(name='Edad minima de cuenta', value=edad, inline=True)
    lockdown = _lockdown_state.get(guild.id)
    estado_lockdown = '🔒 Activo (manual)' if (lockdown and lockdown.get('manual')) else \
        ('🔒 Activo (automatico)' if lockdown else '🔓 Sin lockdown')
    embed.add_field(name='Lockdown ahora mismo', value=estado_lockdown, inline=True)
    return embed


def _an_enable(guild, enabled):
    _cfg(guild.id)['enabled'] = enabled
    _save()
    return simple_embed('Anti-Nuke', f'Anti-nuke (lado nuke) {"activado" if enabled else "desactivado"} en **{guild.name}**.',
                         0x2ecc71 if enabled else 0xe74c3c)


def _an_set_logs(guild, channel):
    cfg = _cfg(guild.id)
    cfg['log_channel'] = str(channel.id) if channel else None
    _save()
    texto = f'Los logs del anti-nuke van a ir a {channel.mention}.' if channel else \
        'Quite el canal de logs propio; se usara el canal de mod-logs general del servidor.'
    return simple_embed('Anti-Nuke', texto, 0x2ecc71)


@antinuke_group.command(name='activar', description='Activar la proteccion anti-nuke (canales, roles, webhooks, purgas)')
async def an_slash_on(interaction: discord.Interaction):
    await _an_slash(interaction, lambda: _an_enable(interaction.guild, True))


@antinuke_group.command(name='desactivar', description='Desactivar la proteccion anti-nuke')
async def an_slash_off(interaction: discord.Interaction):
    await _an_slash(interaction, lambda: _an_enable(interaction.guild, False))


@antinuke_group.command(name='estado', description='Ver el estado y la configuracion de anti-nuke y anti-raid')
async def an_slash_status(interaction: discord.Interaction):
    await _an_slash(interaction, lambda: _an_status_embed(interaction.guild), view_only=True)


@antinuke_group.command(name='logs', description='Elegir el canal de logs del anti-nuke')
@app_commands.describe(canal='Canal de texto (omitir = este canal)', quitar='Quitar el canal propio (usa el de mod-logs general)')
async def an_slash_logs(interaction: discord.Interaction, canal: discord.TextChannel = None, quitar: bool = False):
    target = None if quitar else (canal or interaction.channel)
    await _an_slash(interaction, lambda: _an_set_logs(interaction.guild, target))


@antinuke_group.command(name='config', description='Ajustar los umbrales y la respuesta del anti-nuke')
@app_commands.describe(
    umbral_estructura='Canales/roles creados o borrados para considerarlo un nuke',
    ventana_estructura='Ventana de tiempo para contar eso (segundos)',
    umbral_webhooks='Webhooks creados para considerarlo sospechoso',
    ventana_webhooks='Ventana de tiempo para contar webhooks (segundos)',
    umbral_membresia='Miembros expulsados o baneados para considerarlo una purga',
    ventana_membresia='Ventana de tiempo para contar eso (segundos)',
    accion_humanos='Que hacerle a una persona que dispara la alerta',
    accion_bots='Que hacerle a un bot/app que dispara la alerta',
    respuesta_automatica='Si es "No", solo avisa en el log; no castiga a nadie solo',
)
@app_commands.choices(accion_humanos=[
    app_commands.Choice(name='Quarantine (recomendado)', value='quarantine'),
    app_commands.Choice(name='Expulsar', value='kick'),
    app_commands.Choice(name='Banear', value='ban'),
    app_commands.Choice(name='Ninguna (solo avisar)', value='none'),
])
@app_commands.choices(accion_bots=[
    app_commands.Choice(name='Expulsar (recomendado)', value='kick'),
    app_commands.Choice(name='Banear', value='ban'),
    app_commands.Choice(name='Ninguna (solo avisar)', value='none'),
])
@app_commands.choices(respuesta_automatica=[
    app_commands.Choice(name='Si', value='si'),
    app_commands.Choice(name='No (solo alertar)', value='no'),
])
async def an_slash_config(
    interaction: discord.Interaction,
    umbral_estructura: app_commands.Range[int, 2, 100] = None,
    ventana_estructura: app_commands.Range[int, 2, 600] = None,
    umbral_webhooks: app_commands.Range[int, 1, 50] = None,
    ventana_webhooks: app_commands.Range[int, 2, 600] = None,
    umbral_membresia: app_commands.Range[int, 2, 100] = None,
    ventana_membresia: app_commands.Range[int, 2, 600] = None,
    accion_humanos: app_commands.Choice[str] = None,
    accion_bots: app_commands.Choice[str] = None,
    respuesta_automatica: app_commands.Choice[str] = None,
):
    changes = []
    if umbral_estructura is not None: changes.append(('structure_threshold', umbral_estructura))
    if ventana_estructura is not None: changes.append(('structure_window', ventana_estructura))
    if umbral_webhooks is not None: changes.append(('webhook_threshold', umbral_webhooks))
    if ventana_webhooks is not None: changes.append(('webhook_window', ventana_webhooks))
    if umbral_membresia is not None: changes.append(('membership_threshold', umbral_membresia))
    if ventana_membresia is not None: changes.append(('membership_window', ventana_membresia))
    if accion_humanos is not None: changes.append(('member_action', accion_humanos.value))
    if accion_bots is not None: changes.append(('bot_action', accion_bots.value))
    if respuesta_automatica is not None: changes.append(('auto_action', respuesta_automatica.value == 'si'))

    def run():
        if not changes:
            raise JarvisError('Indica al menos un ajuste. Usa `/antinuke estado` para ver los actuales.')
        cfg = _cfg(interaction.guild.id)
        for key, value in changes:
            cfg[key] = value
        _save()
        return _an_status_embed(interaction.guild)
    await _an_slash(interaction, run)


@antinuke_group.command(name='exento', description='Agregar o quitar a alguien de la lista de exentos del anti-nuke/raid')
@app_commands.describe(usuario='Persona o bot a exentar', quitar='Quitarlo de la lista en vez de agregarlo')
async def an_slash_exempt(interaction: discord.Interaction, usuario: discord.Member, quitar: bool = False):
    def run():
        cfg = _cfg(interaction.guild.id)
        exempt = {str(x) for x in cfg.get('exempt', [])}
        if quitar:
            exempt.discard(str(usuario.id))
            texto = f'{usuario.mention} ya no esta exento.'
        else:
            exempt.add(str(usuario.id))
            texto = f'{usuario.mention} ahora esta exento (Jarvis nunca actuara sobre el/ella, ni en nuke ni en raid).'
        cfg['exempt'] = sorted(exempt)
        _save()
        return simple_embed('Anti-Nuke', texto, 0x2ecc71)
    await _an_slash(interaction, run)


@antinuke_group.command(name='liberar', description='Revertir una quarantine que puso el anti-nuke por error')
@app_commands.describe(usuario='Persona a liberar de la quarantine')
async def an_slash_release(interaction: discord.Interaction, usuario: discord.Member):
    if not _an_can_manage(interaction.user, interaction.guild):
        return await interaction.response.send_message('Necesitas ser el dueno del servidor.', ephemeral=True)
    entry = next(
        (e for e in quarantine_data
         if e['guildId'] == str(interaction.guild.id) and e['userId'] == str(usuario.id)),
        None,
    )
    if entry is None:
        return await interaction.response.send_message(f'{usuario.mention} no tiene una quarantine activa.', ephemeral=True)
    await interaction.response.defer()
    try:
        await _restore_quarantine(entry)
    except discord.DiscordException as e:
        return await interaction.followup.send(f'No pude restaurar todo: {short_error(e)}. Revisa los roles a mano.')
    remaining = [saved for saved in quarantine_data if saved is not entry]
    _save_quarantines(remaining)
    quarantine_data[:] = remaining
    await interaction.followup.send(embed=simple_embed(
        'Anti-Nuke', f'Saque a {usuario.mention} de quarantine y le devolvi sus roles anteriores.', 0x2ecc71))


@antinuke_group.command(name='idban', description='Banear preventivamente por ID (funciona aunque no este en el servidor)')
@app_commands.describe(id_usuario='ID numerico de la cuenta a banear', razon='Motivo (opcional)')
async def an_slash_idban(interaction: discord.Interaction, id_usuario: str, razon: str = None):
    if not _an_can_manage(interaction.user, interaction.guild):
        return await interaction.response.send_message('Necesitas ser el dueno del servidor.', ephemeral=True)
    if not id_usuario.isdigit():
        return await interaction.response.send_message('Eso no es un ID valido (deben ser puros numeros).', ephemeral=True)
    try:
        await interaction.guild.ban(discord.Object(id=int(id_usuario)),
                                     reason=(razon or 'Ban preventivo por ID')[:512], delete_message_seconds=0)
    except discord.Forbidden:
        return await interaction.response.send_message('No tengo permiso para banear.', ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.response.send_message(f'No se pudo banear: {short_error(e)}', ephemeral=True)
    texto = f'Banee preventivamente el ID `{id_usuario}`.' + (f'\nRazon: {razon}' if razon else '')
    await interaction.response.send_message(embed=simple_embed('Anti-Nuke', texto, 0x2ecc71))


# ---- /antinuke raid ... --------------------------------------------------
def _raid_status_embed(guild):
    return _an_status_embed(guild)   # el estado ya muestra ambos lados juntos


def _raid_enable(guild, enabled):
    _raid_cfg(guild.id)['enabled'] = enabled
    _save()
    return simple_embed('Anti-Raid', f'Anti-raid (ingresos) {"activado" if enabled else "desactivado"} en **{guild.name}**.',
                         0x2ecc71 if enabled else 0xe74c3c)


@raid_group.command(name='activar', description='Activar la proteccion anti-raid (ingresos masivos)')
async def raid_slash_on(interaction: discord.Interaction):
    await _an_slash(interaction, lambda: _raid_enable(interaction.guild, True))


@raid_group.command(name='desactivar', description='Desactivar la proteccion anti-raid')
async def raid_slash_off(interaction: discord.Interaction):
    await _an_slash(interaction, lambda: _raid_enable(interaction.guild, False))


@raid_group.command(name='config', description='Ajustar los umbrales y la respuesta del anti-raid')
@app_commands.describe(
    umbral_ingresos='Cuentas entrando para considerarlo un raid',
    ventana_ingresos='Ventana de tiempo para contar ingresos (segundos)',
    accion_ingresos='Que hacer con las cuentas de la rafaga',
    lockdown_automatico='Si ademas de la accion, subir verificacion y pausar invitaciones',
    minutos_lockdown='Cuanto dura el lockdown automatico antes de revertirse solo',
    edad_minima_dias='Rechazar cuentas mas nuevas que esto (0 = desactivado)',
    accion_edad_minima='Que hacerle a una cuenta demasiado nueva',
)
@app_commands.choices(accion_ingresos=[
    app_commands.Choice(name='Solo lockdown (recomendado)', value='lockdown'),
    app_commands.Choice(name='Expulsar a la rafaga', value='kick'),
    app_commands.Choice(name='Banear a la rafaga', value='ban'),
    app_commands.Choice(name='Ninguna (solo avisar)', value='none'),
])
@app_commands.choices(lockdown_automatico=[
    app_commands.Choice(name='Si', value='si'),
    app_commands.Choice(name='No', value='no'),
])
@app_commands.choices(accion_edad_minima=[
    app_commands.Choice(name='Expulsar', value='kick'),
    app_commands.Choice(name='Banear', value='ban'),
])
async def raid_slash_config(
    interaction: discord.Interaction,
    umbral_ingresos: app_commands.Range[int, 2, 200] = None,
    ventana_ingresos: app_commands.Range[int, 2, 600] = None,
    accion_ingresos: app_commands.Choice[str] = None,
    lockdown_automatico: app_commands.Choice[str] = None,
    minutos_lockdown: app_commands.Range[int, 1, 1440] = None,
    edad_minima_dias: app_commands.Range[int, 0, 365] = None,
    accion_edad_minima: app_commands.Choice[str] = None,
):
    changes = []
    if umbral_ingresos is not None: changes.append(('join_threshold', umbral_ingresos))
    if ventana_ingresos is not None: changes.append(('join_window', ventana_ingresos))
    if accion_ingresos is not None: changes.append(('join_action', accion_ingresos.value))
    if lockdown_automatico is not None: changes.append(('auto_lockdown', lockdown_automatico.value == 'si'))
    if minutos_lockdown is not None: changes.append(('lockdown_minutes', minutos_lockdown))
    if edad_minima_dias is not None: changes.append(('min_account_age_days', edad_minima_dias))
    if accion_edad_minima is not None: changes.append(('min_account_age_action', accion_edad_minima.value))

    def run():
        if not changes:
            raise JarvisError('Indica al menos un ajuste. Usa `/antinuke estado` para ver los actuales.')
        rcfg = _raid_cfg(interaction.guild.id)
        for key, value in changes:
            rcfg[key] = value
        _save()
        return _an_status_embed(interaction.guild)
    await _an_slash(interaction, run)


@raid_group.command(name='lockdown', description='Boton de panico: bloquear o desbloquear el servidor ahora mismo')
@app_commands.describe(activar='Si (bloquear ahora) o No (quitar el bloqueo)')
@app_commands.choices(activar=[
    app_commands.Choice(name='Si, bloquear ahora', value='si'),
    app_commands.Choice(name='No, quitar el bloqueo', value='no'),
])
async def raid_slash_lockdown(interaction: discord.Interaction, activar: app_commands.Choice[str]):
    if not _an_can_manage(interaction.user, interaction.guild):
        return await interaction.response.send_message('Necesitas ser el dueno del servidor.', ephemeral=True)
    await interaction.response.defer()
    guild = interaction.guild
    if activar.value == 'si':
        try:
            ok = await _enter_lockdown(guild, 0, manual=True, motivo='lockdown manual')
        except JarvisError as e:
            return await interaction.followup.send(str(e))
        texto = ('🔒 Lockdown activado: verificacion al maximo e invitaciones pausadas. Se queda asi hasta que lo '
                 'quites con `/antinuke raid lockdown activar:No`.') if ok else \
            'Ya habia un lockdown activo (automatico o manual).'
    else:
        ok = await _exit_lockdown(guild)
        texto = '🔓 Lockdown desactivado. Verificacion e invitaciones vueltas a la normalidad.' if ok else \
            'No habia ningun lockdown activo.'
    await interaction.followup.send(embed=simple_embed('Anti-Raid', texto, 0x2ecc71))


# ============================================================================
# INSTALACION
# ============================================================================
def install(main):
    """Conecta este modulo al archivo principal del bot. `main` = sys.modules[__name__] del bot."""
    global M, _installed, ANTINUKE_FILE
    if _installed:
        return
    _installed = True
    M = main

    missing = [n for n in _NEEDED if not hasattr(main, n)]
    if missing:
        raise RuntimeError('jarvis_antinuke: faltan estos nombres en el bot principal: ' + ', '.join(missing)
                            + '. Llama a install() DESPUES de definirlos (antes de la seccion KEEPALIVE).')
    g = globals()
    for n in _NEEDED:
        g[n] = getattr(main, n)

    ANTINUKE_FILE = main.DATA_DIR / 'antinuke.json'
    loaded = load_json(ANTINUKE_FILE, {})
    if isinstance(loaded, dict):
        _data.update(loaded)

    # lado NUKE -- se suma a los listeners existentes, no los reemplaza
    client.add_listener(_on_guild_channel_create, 'on_guild_channel_create')
    client.add_listener(_on_guild_channel_delete, 'on_guild_channel_delete')
    client.add_listener(_on_guild_role_create, 'on_guild_role_create')
    client.add_listener(_on_guild_role_delete, 'on_guild_role_delete')
    client.add_listener(_on_guild_role_update, 'on_guild_role_update')
    client.add_listener(_on_webhooks_update, 'on_webhooks_update')
    client.add_listener(_on_member_remove_check_kick, 'on_member_remove')
    client.add_listener(_on_member_ban_check, 'on_member_ban')

    # lado RAID
    client.add_listener(_on_member_join_raid, 'on_member_join')
    client.add_listener(_resume_lockdowns_on_ready, 'on_ready')

    client.tree.add_command(antinuke_group)

    original_help = main.build_help_embed

    def build_help_embed_with_antinuke():
        embed = original_help()
        embed.add_field(
            name='Anti-Nuke / Anti-Raid y auditoria',
            value='`/antinuke activar|desactivar|estado` — proteccion de canales/roles/webhooks/purgas\n'
                  '`/antinuke config` `/antinuke exento` `/antinuke logs` `/antinuke liberar` `/antinuke idban`\n'
                  '`/antinuke raid activar|desactivar|config` — ingresos masivos y cuentas nuevas\n'
                  '`/antinuke raid lockdown` — boton de panico manual\n'
                  '(Solo el dueno del servidor puede configurar esto, por seguridad.)',
            inline=False,
        )
        return embed

    main.build_help_embed = build_help_embed_with_antinuke

    total_nuke = sum(1 for c in _data.values() if c.get('enabled'))
    total_raid = sum(1 for c in _data.values() if (c.get('raid') or {}).get('enabled'))
    print(f'[AntiNuke] Listo | nuke activo en {total_nuke} servidor(es) | raid activo en {total_raid} servidor(es).')
