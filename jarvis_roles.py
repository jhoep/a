# -*- coding: utf-8 -*-
"""
jarvis_roles.py  -  Jarvis recuerda los roles de la gente y se los devuelve cuando tu se lo pidas.

INSTALACION (2 lineas en tu archivo principal, junto a las de los otros modulos, antes de "# KEEPALIVE"):

    import jarvis_roles
    jarvis_roles.install(sys.modules[__name__])

COMO FUNCIONA
  - Automatico: cada vez que alguien SALE del servidor (se va solo, lo expulsan o lo banean), Jarvis guarda los roles
    que tenia en ese momento. Si el bot tiene el permiso "Ver registro de auditoria" tambien anota si fue
    expulsado o baneado.
  - Manual: puedes guardar los roles de alguien (o de todos) cuando quieras, por si el bot estaba apagado.
  - Cuando vuelva (o cuando tu quieras), le dices a Jarvis que se los devuelva. NUNCA se los da solo.

EJEMPLOS
    jarvis devuelvele los roles a @pepe          (@menciona, ID, nombre o responde a un mensaje suyo)
    jarvis devuelvele los roles al ultimo que se fue
    jarvis restaura los roles que tenia pepe
    jarvis que roles tenia pepe                  -> ver lo que hay guardado
    jarvis roles guardados                       -> lista de los que se fueron con roles guardados
    jarvis guarda los roles de @pepe
    jarvis guarda los roles de todos
    /roles ver | /roles devolver | /roles guardar

SEGURIDAD
  - Solo la whitelist de Jarvis. Los roles con permisos de moderacion SOLO los devuelve el owner del bot.
  - Nunca se dan roles por encima del rol del bot, roles de integracion (bots, boosters) ni roles que ya no existen:
    el resultado te dice cuales se saltó y por que.

RESPALDO
  Los datos se guardan en data/saved_roles.json y ademas se respaldan en Discord (en tu DM, o en el canal de la
  variable JARVIS_STORAGE_CHANNEL), por si tu host borra la carpeta al reiniciar. Pon ROLES_BACKUP=0 para desactivarlo.
"""
import io
import os
import re
import json
import time
import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands

# ----------------------------------------------------------------------------
# Configuracion
# ----------------------------------------------------------------------------
BACKUP_ON      = os.environ.get('ROLES_BACKUP', '1').strip().lower() not in ('0', 'false', 'no', 'off')
BACKUP_MARKER  = '[JARVIS_ROLES_BACKUP]'
BACKUP_DELAY   = 10                  # segundos de espera para agrupar cambios antes de respaldar
BACKUP_MAX_BYTES = 8 * 1024 * 1024
MAX_LIST       = 15                  # cuantas personas muestra la lista de guardados

REASON_LABELS = {'salio': 'Se salio', 'expulsado': 'Fue expulsado', 'baneado': 'Fue baneado'}

BRAIN_ACTIONS_TEXT = (
    "- roles_save: GUARDAR los roles actuales de alguien. target_ids (o target_query); text=\"all\" para guardar los de TODOS los miembros\n"
    "- roles_restore: DEVOLVER a alguien los roles que tenia guardados (por ejemplo antes de irse del servidor o ser expulsado). "
    "target_ids o target_query (nombre, ID o \"ultimo\" = el ultimo que se fue). La persona puede ya no estar en PERSONAS: en ese caso pon su nombre en target_query\n"
    "- roles_show: VER los roles guardados de alguien (target_ids / target_query) o, sin objetivo, la lista de quienes se fueron con roles guardados\n"
)
BRAIN_RULE_TEXT = (
    "\n24. Si el JEFE pide devolver, restaurar o recuperar los roles (varios) que tenia alguien antes de irse o ser expulsado, es roles_restore "
    "(NO role_add). Dar UN rol concreto por nombre sigue siendo role_add."
)
HELP_FIELD_NAME = 'Roles guardados'
HELP_FIELD_VALUE = (
    'Guardo solo los roles de quien se va (sale, lo expulsan o lo banean). Cuando vuelva, tu me lo dices:\n'
    '`jarvis devuelvele los roles a @user` / `jarvis devuelvele los roles al ultimo que se fue`\n'
    '`jarvis que roles tenia @user` / `jarvis roles guardados`\n'
    '`jarvis guarda los roles de @user` / `jarvis guarda los roles de todos`\n'
    '`/roles ver` `/roles devolver` `/roles guardar`'
)

_NEEDED = (
    'client', 'Intent', 'JarvisError', 'execute_intent', 'short_error', 'normalize_for_compare',
    'JARVIS_WHITELIST', 'OWNER_ID', 'PLAIN_ACTIONS', 'KNOWN_ACTIONS', 'PLAIN_HANDLERS', 'ACTION_LABELS',
    'simple_embed', 'spawn', 'load_json', 'save_json', 'DATA_DIR', 'REF_RE', 'resolve_targets',
    'role_has_mod_perms',
)

RolesError = None            # se crea en install() (hereda de JarvisError del bot)
_data = {}                   # { guildId: { userId: {name, tag, global, roles:[{id,name}], saved_at, left_at, reason, by, source} } }
_file = None
_store_channel_fn = None     # main._wl_store_channel (si existe)
_dirty = False
_push_task = None
_boot_done = False
_installed = False


# ----------------------------------------------------------------------------
# Almacenamiento
# ----------------------------------------------------------------------------
def _gdata(guild_id):
    return _data.setdefault(str(guild_id), {})


def _save(backup=True):
    global _dirty
    save_json(_file, _data)
    if backup and BACKUP_ON and _store_channel_fn is not None:
        _dirty = True
        _schedule_backup()


def _roles_of(member):
    """Roles de un miembro (sin @everyone ni los gestionados por integraciones, como el de booster)."""
    out = []
    for r in sorted(member.roles, key=lambda r: -r.position):
        if r.is_default() or r.managed:
            continue
        out.append({'id': str(r.id), 'name': r.name})
    return out


def snapshot_member(member, source, left=False, reason=None, by=None, persist=True):
    """Guarda los roles actuales de `member`. Devuelve la entrada guardada (o None si no habia nada que guardar)."""
    roles = _roles_of(member)
    g = _gdata(member.guild.id)
    uid = str(member.id)
    old = g.get(uid)
    now = time.time()

    if not roles:
        # alguien que se va sin roles NO debe borrar lo que ya teniamos guardado de antes
        if left and old and old.get('roles'):
            old['left_at'] = now
            old['reason'] = reason or 'salio'
            old['by'] = by
            if persist:
                _save()
            return old
        return None

    entry = {
        'name': member.display_name, 'tag': member.name,
        'global': getattr(member, 'global_name', None) or '',
        'roles': roles, 'saved_at': now,
        'left_at': now if left else None,
        'reason': reason if left else None,
        'by': by, 'source': source,
    }
    g[uid] = entry
    if persist:
        _save()
    return entry


# ----------------------------------------------------------------------------
# Respaldo en Discord (sobrevive aunque el host borre la carpeta data/)
# ----------------------------------------------------------------------------
async def _backup_channel():
    if _store_channel_fn is None:
        return None
    try:
        return await _store_channel_fn()
    except Exception as e:
        print(f'[Roles] No pude abrir el canal de respaldo: {e}')
        return None


async def _backup_find(ch):
    found = []
    try:
        async for m in ch.history(limit=100):
            if (m.author.id == client.user.id and (m.content or '').startswith(BACKUP_MARKER) and m.attachments):
                found.append(m)           # del mas nuevo al mas viejo
    except Exception as e:
        print(f'[Roles] No pude leer el historial del respaldo: {e}')
    return found


async def _backup_push():
    ch = await _backup_channel()
    if ch is None:
        return
    raw = json.dumps(_data, ensure_ascii=False).encode('utf-8')
    if len(raw) > BACKUP_MAX_BYTES:
        print('[Roles] El respaldo es demasiado grande para subirlo a Discord.')
        return
    try:
        old = await _backup_find(ch)
        await ch.send(content=f'{BACKUP_MARKER} {time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}',
                      file=discord.File(io.BytesIO(raw), filename='saved_roles.json'))
        for m in old:
            try:
                await m.delete()
            except Exception:
                pass
    except Exception as e:
        print(f'[Roles] No pude guardar el respaldo en Discord: {e}')


def _schedule_backup():
    global _push_task
    if _push_task is not None and not _push_task.done():
        return

    async def _go():
        global _dirty
        await asyncio.sleep(BACKUP_DELAY)
        while _dirty:
            _dirty = False
            await _backup_push()

    _push_task = spawn(_go())


async def _on_ready():
    """Al arrancar: si no hay archivo local (host que borra el disco), lo recupera del respaldo en Discord."""
    global _boot_done
    if _boot_done:
        return
    _boot_done = True
    if not BACKUP_ON or _store_channel_fn is None:
        return
    if _data:                                    # hay datos locales: dejamos el respaldo igual
        global _dirty
        _dirty = True
        _schedule_backup()
        return
    ch = await _backup_channel()
    if ch is None:
        return
    try:
        msgs = await _backup_find(ch)
        if not msgs:
            return
        loaded = json.loads((await msgs[0].attachments[0].read()).decode('utf-8'))
        if isinstance(loaded, dict):
            _data.update(loaded)
            _save(backup=False)
            n = sum(len(v) for v in _data.values())
            print(f'[Roles] Recuperados {n} registro(s) desde el respaldo en Discord.')
    except Exception as e:
        print(f'[Roles] No pude recuperar el respaldo: {e}')


# ----------------------------------------------------------------------------
# Eventos: guardar al salir
# ----------------------------------------------------------------------------
async def _detect_removal(guild, member):
    """Intenta saber si se fue solo, lo expulsaron o lo banearon (necesita 'Ver registro de auditoria')."""
    await asyncio.sleep(2.5)
    entry = _gdata(guild.id).get(str(member.id))
    me = guild.me
    if not entry or me is None or not me.guild_permissions.view_audit_log:
        return
    now = datetime.now(timezone.utc)
    for action, label in ((discord.AuditLogAction.kick, 'expulsado'), (discord.AuditLogAction.ban, 'baneado')):
        try:
            async for log in guild.audit_logs(limit=6, action=action):
                tgt = getattr(log, 'target', None)
                if tgt is not None and tgt.id == member.id and (now - log.created_at).total_seconds() < 30:
                    entry['reason'] = label
                    entry['by'] = str(log.user.id) if log.user else None
                    _save()
                    return
        except Exception:
            return


async def _on_member_remove(member):
    try:
        if member.bot:
            return
        if snapshot_member(member, 'auto', left=True, reason='salio') is not None:
            spawn(_detect_removal(member.guild, member))
    except Exception as e:
        print(f'[Roles] on_member_remove: {e}')


# ----------------------------------------------------------------------------
# Busqueda de la persona (incluye gente que YA NO esta en el servidor)
# ----------------------------------------------------------------------------
def _entry_time(e):
    return e.get('left_at') or e.get('saved_at') or 0


def _match_saved(guild, query):
    """Busca entre los guardados por ID, 'ultimo' o nombre. Devuelve (uid|None, error|None)."""
    g = _gdata(guild.id)
    q = normalize_for_compare(query or '')
    if not q:
        return None, None
    if re.fullmatch(r'\d{15,21}', q):
        return (q, None) if q in g else (None, None)
    if re.search(r'\bultim[oa]\b', q):
        left = [(e['left_at'], uid) for uid, e in g.items() if e.get('left_at')]
        return (max(left)[1], None) if left else (None, None)

    scored = []
    for uid, e in g.items():
        names = [normalize_for_compare(x) for x in (e.get('name'), e.get('tag'), e.get('global')) if x]
        names = [n for n in names if n]
        tier = None
        if q in names:
            tier = 0
        elif any(n.startswith(q) for n in names):
            tier = 1
        elif len(q) >= 3 and any(q in n for n in names):
            tier = 2
        if tier is not None:
            scored.append((tier, -_entry_time(e), uid))
    if not scored:
        return None, None
    scored.sort()
    top = [s for s in scored if s[0] == scored[0][0]]
    if len(top) > 1:
        names = ', '.join(f'`{g[s[2]]["name"]}` (`{s[2]}`)' for s in top[:5])
        return None, f'Hay varias personas guardadas que encajan con `{query}`: {names}. Dime cual (con su ID o mencionandola).'
    return top[0][2], None


def _pick_user(guild, ids=(), query=None, reply_id=None):
    g = _gdata(guild.id)
    for i in ids:
        if str(i) in g:
            return str(i)
    if query:
        uid, err = _match_saved(guild, query)
        if err:
            raise RolesError(err)
        if uid:
            return uid
    if reply_id and str(reply_id) in g:
        return str(reply_id)
    return None


def _refs_from_ctx(ctx, intent):
    ids = [str(x) for x in (intent.targets or [])]
    ids += [a or b for a, b in REF_RE.findall(ctx.text)]
    ids += [str(u.id) for u in getattr(ctx.message, 'mentions', []) if not getattr(u, 'bot', False)]
    ref = getattr(ctx.message, 'reference', None)
    author = getattr(getattr(ref, 'resolved', None), 'author', None) if ref is not None else None
    ids = list(dict.fromkeys(ids))
    return ids, (author.id if author is not None else None)


def _not_saved_error(shown):
    return RolesError(
        f'No tengo roles guardados de `{shown}`. Solo guardo los de quienes se van estando yo en el servidor, '
        f'o los que me pidas con `jarvis guarda los roles de @alguien`. Mira la lista con `jarvis roles guardados`.')


# ----------------------------------------------------------------------------
# Presentacion
# ----------------------------------------------------------------------------
def _fmt_roles(guild, roles, limit=900):
    parts = []
    for r in roles:
        role = guild.get_role(int(r['id']))
        parts.append(role.mention if role is not None else f'`{r["name"]}` (ya no existe)')
    text, shown = '', 0
    for p in parts:
        if len(text) + len(p) + 1 > limit:
            break
        text += (' ' if text else '') + p
        shown += 1
    if shown < len(parts):
        text += f' \u2026y {len(parts) - shown} mas'
    return text or 'Ninguno'


def _status_line(e):
    if e.get('left_at'):
        return f'{REASON_LABELS.get(e.get("reason") or "salio", "Se salio")} <t:{int(e["left_at"])}:R>'
    return f'Guardado a mano <t:{int(e["saved_at"])}:R>'


def show_embed(guild, uid):
    e = _gdata(guild.id)[uid]
    member_here = guild.get_member(int(uid))
    emb = simple_embed(f'Roles guardados de {e["name"]}', _fmt_roles(guild, e['roles'], 3500), 0x3498db)
    emb.add_field(name='Estado', value=_status_line(e), inline=True)
    emb.add_field(name='Ahora', value='Esta en el servidor' if member_here else 'No esta en el servidor', inline=True)
    emb.add_field(name='ID', value=f'`{uid}`', inline=True)
    if e.get('by') and e.get('reason') in ('expulsado', 'baneado'):
        emb.add_field(name='Por', value=f'<@{e["by"]}>', inline=True)
    emb.set_footer(text=f'{len(e["roles"])} rol(es). Para devolverselos: jarvis devuelvele los roles a {e["name"]}')
    return emb


def list_embed(guild):
    g = _gdata(guild.id)
    rows = sorted(g.items(), key=lambda kv: -_entry_time(kv[1]))
    gone = [(uid, e) for uid, e in rows if e.get('left_at')]
    if not gone:
        return simple_embed('Roles guardados',
                            'Todavia no he guardado a nadie que se haya ido. Cuando alguien salga, lo anoto aqui.', 0x95a5a6)
    lines = []
    for uid, e in gone[:MAX_LIST]:
        here = ' (ya volvio)' if guild.get_member(int(uid)) else ''
        lines.append(f'\u2022 **{discord.utils.escape_markdown(e["name"])}** (`{uid}`) - {len(e["roles"])} rol(es) - '
                     f'{_status_line(e)}{here}')
    extra = len(gone) - MAX_LIST
    if extra > 0:
        lines.append(f'\u2026y {extra} mas')
    return simple_embed(f'Roles guardados ({len(gone)} persona(s) que se fueron)', '\n'.join(lines)[:4000], 0x3498db)


# ----------------------------------------------------------------------------
# Restaurar
# ----------------------------------------------------------------------------
async def restore_roles(guild, uid, requester_is_owner):
    e = _gdata(guild.id)[uid]
    member = guild.get_member(int(uid))
    if member is None:
        try:
            member = await guild.fetch_member(int(uid))
        except discord.HTTPException:
            member = None
    if member is None:
        raise RolesError(
            f'**{e["name"]}** no esta en el servidor ahora mismo, asi que no puedo darle nada. '
            f'Cuando vuelva a entrar, dime y le devuelvo sus {len(e["roles"])} rol(es) guardados.')

    me = guild.me
    to_add, skipped, already = [], [], 0
    for r in e['roles']:
        role = guild.get_role(int(r['id']))
        if role is None:
            skipped.append(f'`{r["name"]}`: ya no existe')
        elif role in member.roles:
            already += 1
        elif role.is_default() or role.managed:
            skipped.append(f'{role.mention}: es de una integracion')
        elif me is None or role >= me.top_role:
            skipped.append(f'{role.mention}: esta por encima de mi rol')
        elif role_has_mod_perms(role) and not requester_is_owner:
            skipped.append(f'{role.mention}: tiene permisos de moderacion (solo el owner lo devuelve)')
        else:
            to_add.append(role)

    if to_add:
        await member.add_roles(*to_add, reason='[Jarvis] Roles guardados devueltos', atomic=True)

    if to_add:
        desc = f'{member.mention} recupero **{len(to_add)}** rol(es):\n' + ' '.join(r.mention for r in to_add)
        color = 0x2ecc71
    elif skipped:
        desc = f'No pude devolverle ningun rol a {member.mention}.'
        color = 0xe67e22
    else:
        desc = f'{member.mention} ya tenia todos sus roles guardados.'
        color = 0x95a5a6
    emb = simple_embed('Roles devueltos', desc[:4000], color)
    if already and to_add:
        emb.add_field(name='Ya los tenia', value=f'{already} rol(es)', inline=True)
    if skipped:
        emb.add_field(name=f'No pude devolver ({len(skipped)})', value='\n'.join(skipped)[:1000], inline=False)
    emb.add_field(name='Guardado', value=_status_line(e), inline=True)
    return emb


# ----------------------------------------------------------------------------
# Handlers de Jarvis (acciones del cerebro)
# ----------------------------------------------------------------------------
def _require_whitelist(user_id):
    if str(user_id) not in JARVIS_WHITELIST:
        raise RolesError('No tienes permiso para manejar los roles guardados.')


async def act_roles_restore(ctx, intent):
    _require_whitelist(ctx.author.id)
    ids, reply_id = _refs_from_ctx(ctx, intent)
    uid = _pick_user(ctx.guild, ids, intent.target_query, reply_id)
    if uid is None:
        if ids or intent.target_query:
            raise _not_saved_error(intent.target_query or ids[0])
        raise RolesError('A quien le devuelvo los roles? Mencionalo, pasame su ID o su nombre, '
                         'o dime `al ultimo que se fue`. Con `jarvis roles guardados` ves la lista.')
    return await restore_roles(ctx.guild, uid, str(ctx.author.id) == str(OWNER_ID))


async def act_roles_show(ctx, intent):
    _require_whitelist(ctx.author.id)
    ids, reply_id = _refs_from_ctx(ctx, intent)
    asked = bool(ids or intent.target_query)
    uid = _pick_user(ctx.guild, ids, intent.target_query, reply_id)
    if uid is not None:
        return show_embed(ctx.guild, uid)
    if asked:
        raise _not_saved_error(intent.target_query or ids[0])
    return list_embed(ctx.guild)


async def act_roles_save(ctx, intent):
    _require_whitelist(ctx.author.id)
    guild = ctx.guild
    if (intent.text or '').strip().lower() == 'all':
        try:
            await guild.chunk()
        except Exception:
            pass
        n = 0
        for m in guild.members:
            if m.bot:
                continue
            if snapshot_member(m, 'manual', persist=False) is not None:
                n += 1
        _save()
        return simple_embed('Roles guardados',
                            f'Guarde los roles de **{n}** miembro(s) con roles. Si alguno se va, ya tengo con que devolvercelos.',
                            0x2ecc71)
    chat = await ctx.get_chat()
    targets, err = await resolve_targets(ctx, intent, chat)
    if err:
        raise RolesError(err)
    t = targets[0]
    if t.member is None:
        raise RolesError(f'{t.name} ya no esta en el servidor, asi que no puedo ver sus roles actuales.')
    entry = snapshot_member(t.member, 'manual')
    if entry is None:
        raise RolesError(f'{t.name} no tiene ningun rol que guardar.')
    return simple_embed(f'Roles de {t.name} guardados', _fmt_roles(guild, entry['roles'], 3500), 0x2ecc71)


# ----------------------------------------------------------------------------
# Atajo por patrones (sin IA)
# ----------------------------------------------------------------------------
RESTORE_VERB = re.compile(r'\b(?:devuelv\w*|restaur\w*|restablec\w*|regres\w*|repon\w*|recuper\w*)\b')
GIVE_VERB = re.compile(r'\b(?:dale|dales|ponle|ponles|da|dame)\b')
SAVED_HINT = re.compile(
    r'\b(?:tenia|tenian|antes|anterior\w*|guardad\w*|original\w*|se\s+fue|se\s+fueron|salio|expuls\w*|kick\w*|'
    r'echaron|baneo|banearon|volvio|volvieron)\b')
SAVE_VERB = re.compile(r'\b(?:guarda\w*|respalda\w*|backup|anota\w*|memoriza\w*|graba\w*)\b')
SHOW_RE = re.compile(
    r'\b(?:que\s+roles\s+(?:tenia|tenian|guardaste|tiene\s+guardados?)|roles\s+guardados|'
    r'(?:muestra\w*|ver|lista\w*|dame)\s+(?:los\s+)?roles\s+guardados|quienes\s+se\s+fueron)\b')
STOPWORDS = {'el', 'la', 'los', 'las', 'sus', 'su', 'que', 'todos', 'todo', 'ese', 'esa', 'este', 'esta', 'antes',
             'mi', 'me', 'un', 'una', 'irse', 'salir', 'de', 'del', 'al', 'a', 'para', 'roles', 'rol', 'guardados'}
WHO_PATTERNS = (
    re.compile(r'\b(?:devuelv\w*|restaur\w*|restablec\w*|regres\w*|repon\w*|recuper\w*|guarda\w*|respalda\w*|dale|dales|ponle|ponles)'
               r'\s+(?:a\s+|al\s+)([a-z0-9_]+)'),
    re.compile(r'\broles\s+(?:que\s+tenia\s+)?(?:de|del|a|al|para)\s+(?:la\s+|el\s+)?([a-z0-9_]+)'),
    re.compile(r'\broles\s+(?:que\s+tenia|que\s+tenian)\s+([a-z0-9_]+)'),
    re.compile(r'\bque\s+roles\s+(?:tenia|tenian)\s+([a-z0-9_]+)'),
    re.compile(r'\broles\s+guardados\s+(?:de|del)\s+([a-z0-9_]+)'),
    re.compile(r'\b(?:a|al|de|del)\s+([a-z0-9_]+)\s*$'),          # ultimo recurso: "... a pepe" al final
)


def _extract_who(raw_text, norm):
    ids = [a or b for a, b in REF_RE.findall(raw_text)]
    if ids:
        return ids, None
    if re.search(r'\bultim[oa]\b', norm):
        return [], 'ultimo'
    for pat in WHO_PATTERNS:
        m = pat.search(norm)
        if m and m.group(1) not in STOPWORDS:
            return [], m.group(1)
    return [], None


async def handle_roles_natural(ctx):
    """'jarvis devuelvele los roles a pepe', 'jarvis roles guardados'... Devuelve True si lo manejo."""
    text = ctx.text.strip()
    n = normalize_for_compare(text)
    if len(n.split()) > 22 or not re.search(r'\broles\b|\bse\s+fueron\b', n):
        return False
    ids, query = _extract_who(text, n)

    if SHOW_RE.search(n):
        action, extra = 'roles_show', {}
    elif SAVE_VERB.search(n):
        action = 'roles_save'
        extra = {'text': 'all'} if re.search(r'\btod[oa]s\b|\bel\s+servidor\b', n) else {}
    elif RESTORE_VERB.search(n) or (GIVE_VERB.search(n) and SAVED_HINT.search(n)):
        action, extra = 'roles_restore', {}
    else:
        return False       # por ejemplo "dale el rol admin" o "dame los roles del server": lo ve el cerebro

    intent = Intent(action=action, source='regex', explicit=True, targets=ids, target_query=query, **extra)
    return await execute_intent(ctx, intent)


# ----------------------------------------------------------------------------
# Slash commands
# ----------------------------------------------------------------------------
def _register_slash_commands():
    grp = app_commands.Group(name='roles', description='Roles guardados de quienes se van del servidor', guild_only=True)

    async def saved_autocomplete(interaction: discord.Interaction, current: str):
        if str(interaction.user.id) not in JARVIS_WHITELIST or interaction.guild is None:
            return []
        cur = normalize_for_compare(current or '')
        rows = sorted(_gdata(interaction.guild.id).items(), key=lambda kv: -_entry_time(kv[1]))
        out = []
        for uid, e in rows:
            names = ' '.join(normalize_for_compare(x) for x in (e.get('name'), e.get('tag'), e.get('global')) if x)
            if cur and cur not in names and not uid.startswith(cur):
                continue
            label = f'{e["name"]} - {len(e["roles"])} rol(es)' + (' - se fue' if e.get('left_at') else '')
            out.append(app_commands.Choice(name=label[:100], value=uid))
            if len(out) >= 25:
                break
        return out

    async def guard(interaction):
        if str(interaction.user.id) not in JARVIS_WHITELIST:
            await interaction.response.send_message('Solo la whitelist de Jarvis puede usar este comando.', ephemeral=True)
            return False
        return True

    @grp.command(name='ver', description='Ver los roles guardados de alguien (o la lista de quienes se fueron)')
    @app_commands.describe(usuario='Persona (omitir = lista de los que se fueron)')
    @app_commands.autocomplete(usuario=saved_autocomplete)
    async def roles_ver(interaction: discord.Interaction, usuario: str = None):
        if not await guard(interaction):
            return
        if not usuario:
            return await interaction.response.send_message(embed=list_embed(interaction.guild))
        try:
            uid, err = _match_saved(interaction.guild, usuario)
            if err:
                raise RolesError(err)
            if uid is None:
                raise _not_saved_error(usuario)
        except RolesError as e:
            return await interaction.response.send_message(str(e), ephemeral=True)
        await interaction.response.send_message(embed=show_embed(interaction.guild, uid))

    @grp.command(name='devolver', description='Devolverle a alguien los roles que tenia guardados')
    @app_commands.describe(usuario='Persona (elige de la lista o escribe su nombre / ID)')
    @app_commands.autocomplete(usuario=saved_autocomplete)
    async def roles_devolver(interaction: discord.Interaction, usuario: str):
        if not await guard(interaction):
            return
        await interaction.response.defer()
        try:
            uid, err = _match_saved(interaction.guild, usuario)
            if err:
                raise RolesError(err)
            if uid is None:
                raise _not_saved_error(usuario)
            embed = await restore_roles(interaction.guild, uid, str(interaction.user.id) == str(OWNER_ID))
        except RolesError as e:
            return await interaction.followup.send(str(e))
        except discord.Forbidden:
            return await interaction.followup.send('No tengo permisos para darle esos roles (revisa mi rol y el permiso Manage Roles).')
        except Exception as e:
            print(f'[Roles] /roles devolver: {e}')
            return await interaction.followup.send(f'Error inesperado: {short_error(e)}')
        await interaction.followup.send(embed=embed)

    @grp.command(name='guardar', description='Guardar los roles actuales de alguien (o de todos)')
    @app_commands.describe(usuario='Persona (omitir y marcar "todos" para guardar a todo el servidor)',
                           todos='Guardar los roles de TODOS los miembros')
    async def roles_guardar(interaction: discord.Interaction, usuario: discord.Member = None, todos: bool = False):
        if not await guard(interaction):
            return
        guild = interaction.guild
        if todos:
            await interaction.response.defer()
            try:
                await guild.chunk()
            except Exception:
                pass
            n = sum(1 for m in guild.members if not m.bot and snapshot_member(m, 'manual', persist=False) is not None)
            _save()
            return await interaction.followup.send(embed=simple_embed(
                'Roles guardados', f'Guarde los roles de **{n}** miembro(s) con roles.', 0x2ecc71))
        if usuario is None:
            return await interaction.response.send_message('Elige un usuario o marca `todos`.', ephemeral=True)
        entry = snapshot_member(usuario, 'manual')
        if entry is None:
            return await interaction.response.send_message(f'{usuario.mention} no tiene ningun rol que guardar.', ephemeral=True)
        await interaction.response.send_message(embed=simple_embed(
            f'Roles de {usuario.display_name} guardados', _fmt_roles(guild, entry['roles'], 3500), 0x2ecc71))

    client.tree.add_command(grp)


# ----------------------------------------------------------------------------
# Instalacion
# ----------------------------------------------------------------------------
def install(main):
    """Conecta este modulo al archivo principal del bot. `main` = sys.modules[__name__] del bot."""
    global _installed, RolesError, _file, _store_channel_fn
    if _installed:
        return
    _installed = True

    missing = [n for n in _NEEDED if not hasattr(main, n)]
    if missing:
        raise RuntimeError('jarvis_roles: faltan estos nombres en el bot principal: ' + ', '.join(missing)
                           + '. Llama a install() DESPUES de definirlos (antes de la seccion KEEPALIVE).')
    g = globals()
    for n in _NEEDED:
        g[n] = getattr(main, n)

    RolesError = type('RolesError', (JarvisError,), {'__doc__': 'Error de roles guardados mostrado tal cual al usuario.'})
    _file = main.DATA_DIR / 'saved_roles.json'
    _store_channel_fn = getattr(main, '_wl_store_channel', None)     # el mismo canal donde ya respaldas la whitelist

    loaded = load_json(_file, {})
    if isinstance(loaded, dict):
        _data.update(loaded)

    for name, handler, label in (
        ('roles_save', act_roles_save, 'Guardar roles'),
        ('roles_restore', act_roles_restore, 'Devolver roles guardados'),
        ('roles_show', act_roles_show, 'Ver roles guardados'),
    ):
        PLAIN_ACTIONS.add(name)
        KNOWN_ACTIONS.add(name)
        PLAIN_HANDLERS[name] = handler
        ACTION_LABELS[name] = label

    prompt = main.INTENT_SYSTEM_PROMPT
    marker = '- chat: el JEFE solo conversa'
    if '- roles_restore:' not in prompt:
        if marker in prompt:
            prompt = prompt.replace(marker, BRAIN_ACTIONS_TEXT + marker, 1)
        else:
            print('[Roles] No encontre el punto de insercion en INTENT_SYSTEM_PROMPT; la IA no conocera las acciones de roles guardados.')
        main.INTENT_SYSTEM_PROMPT = prompt.rstrip() + BRAIN_RULE_TEXT

    original_brain = main.jarvis_brain

    async def jarvis_brain_with_roles(ctx):
        if await handle_roles_natural(ctx):
            return True
        return await original_brain(ctx)

    main.jarvis_brain = jarvis_brain_with_roles

    original_help = main.build_help_embed

    def build_help_embed_with_roles():
        embed = original_help()
        embed.add_field(name=HELP_FIELD_NAME, value=HELP_FIELD_VALUE, inline=False)
        return embed

    main.build_help_embed = build_help_embed_with_roles

    client.add_listener(_on_member_remove, 'on_member_remove')      # se suma al que ya tienes, no lo reemplaza
    client.add_listener(_on_ready, 'on_ready')
    _register_slash_commands()

    total = sum(len(v) for v in _data.values())
    print(f'[Roles] Listo | {total} registro(s) guardados | respaldo_en_discord='
          f'{"SI" if (BACKUP_ON and _store_channel_fn) else "NO"}')
