# -*- coding: utf-8 -*-
"""
jarvis_devolver_rol.py
======================
Devolver roles usando el backup que hace /save.

QUE HACE
  - /save ahora tambien guarda que roles tiene cada miembro (antes solo guardaba los de los bots).
  - /devolver_rol  y  "jarvis devuelve el rol X": compara el backup con el servidor AHORA, te muestra
    quien tenia el rol, quien ya lo tiene y a quien le falta, y con un boton se lo devuelve a los que faltan.

COMO INSTALARLO (en tu archivo principal, debajo de los otros jarvis_xxx.install(...)):
    import jarvis_devolver_rol
    jarvis_devolver_rol.install(sys.modules[__name__])

DESPUES: haz un /save nuevo (los backups viejos no traen los roles de los miembros, solo los de los bots).

COMO SE USA
    /devolver_rol rol:@VIP                    -> mira quien tenia VIP en el backup y a quien le falta
    /devolver_rol usuario:@Juan               -> todos los roles que tenia Juan en el backup
    /devolver_rol rol:@VIP usuario:@Juan      -> solo ese rol a esa persona
    /devolver_rol todos:True                  -> todos los roles de todos (cuidado, usa el backup tal cual esta)
    /devolver_rol ... archivo:<backup.json>   -> usar un backup concreto (por si el host borro el disco)
    jarvis devuelve el rol VIP / jarvis devuelvele sus roles a @Juan / jarvis quien tenia el rol VIP en el backup
Siempre muestra primero el resumen y espera el boton de confirmar.
"""
import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands

M = None                  # modulo principal del bot (lo asigna install)
_installed = False
_locks = {}               # guildId -> asyncio.Lock: una devolucion a la vez por servidor

MAX_BACKUP_BYTES = 25 * 1024 * 1024
LIST_SHOW = 15            # personas que se muestran por lista en el resumen
PROGRESS_EVERY = 4.0      # segundos entre ediciones del mensaje de progreso


# ============================================================================
# UTILIDADES
# ============================================================================
def _err(msg):
    return M.JarvisError(msg)


def _norm(s):
    return M.normalize_for_compare(s or '')


def _name_key(name):
    # nombre normalizado; si queda vacio (rol solo con emojis) se usa el texto tal cual
    return _norm(name) or str(name or '')


def is_owner(user):
    return str(user.id) == str(M.OWNER_ID)


def is_admin(user):
    return bool(getattr(getattr(user, 'guild_permissions', None), 'administrator', False))


def can_use(user):
    return is_owner(user) or is_admin(user) or str(user.id) in M.JARVIS_WHITELIST


def _fit(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1] + '\u2026'


def _mentions(ids, limit=LIST_SHOW):
    ids = list(ids)
    out = ' '.join(f'<@{i}>' for i in ids[:limit])
    if len(ids) > limit:
        out += f' y {len(ids) - limit} mas'
    return out or '-'


def _saved_text(backup):
    try:
        ts = int(datetime.fromisoformat(str(backup['savedAt'])).timestamp())
        return f'<t:{ts}:R> (<t:{ts}:f>)'
    except Exception:
        return 'en una fecha que no se'


# ============================================================================
# BACKUP: leerlo y entender quien tenia que
# ============================================================================
def backup_path(guild_id):
    return Path(M.BACKUP_DIR) / f'{guild_id}.json'


def parse_backup(raw):
    try:
        data = json.loads(raw.decode('utf-8-sig'))
    except Exception as e:
        raise _err(f'No pude leer ese backup: {M.short_error(e)}')
    if not isinstance(data, dict) or not isinstance(data.get('roles'), list):
        raise _err('Ese archivo no parece un backup de `/save` (no trae la lista de roles).')
    return data


async def load_backup(guild, attachment=None):
    """Devuelve (backup, origen). Si no hay adjunto usa el ultimo backup guardado de este servidor."""
    if attachment is not None:
        if not attachment.filename.lower().endswith('.json'):
            raise _err('Adjunta el archivo `.json` que te dio `/save`.')
        if attachment.size and attachment.size > MAX_BACKUP_BYTES:
            raise _err('Ese archivo es demasiado grande para ser un backup de `/save`.')
        return parse_backup(await attachment.read()), 'archivo adjunto'
    path = backup_path(guild.id)
    if not path.is_file():
        raise _err('No tengo ningun backup guardado de este servidor (si el host reinicia, el disco puede borrarse). '
                   'Haz un `/save`, o adjunta el `.json` que te dio `/save`.')
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise _err(f'No pude abrir el backup: {M.short_error(e)}')
    return parse_backup(raw), 'ultimo backup guardado'


def backup_people(backup):
    """{uid: {'name': str, 'roles': set(id_de_rol)}} juntando la lista de miembros y la de bots."""
    people = {}
    for key in ('members', 'bots'):
        for e in backup.get(key) or []:
            if not isinstance(e, dict):
                continue
            uid = str(e.get('id', ''))
            if not uid.isdigit():
                continue
            roles = {str(r) for r in (e.get('roleIds') or []) if str(r).isdigit()}
            name = str(e.get('name') or e.get('displayName') or uid)
            cur = people.get(uid)
            if cur is None:
                people[uid] = {'name': name, 'roles': roles}
            else:
                cur['roles'] |= roles
    return people


def map_roles(guild, backup):
    """{id_del_rol_en_el_backup: (rol_actual | None, nombre_en_el_backup)}.
    Primero por ID; si el rol se borro y se volvio a crear (otro ID), por nombre (solo si es unico)."""
    by_name = {}
    for r in guild.roles:
        if not r.is_default():
            by_name.setdefault(_name_key(r.name), []).append(r)
    entries = []
    counts = {}
    for br in backup.get('roles') or []:
        if not isinstance(br, dict):
            continue
        bid = str(br.get('id', ''))
        if not bid.isdigit():
            continue
        name = str(br.get('name') or bid)
        entries.append((bid, name))
        counts[_name_key(name)] = counts.get(_name_key(name), 0) + 1
    out = {}
    for bid, name in entries:
        role = guild.get_role(int(bid))
        if role is not None and role.is_default():
            role = None
        if role is None and counts.get(_name_key(name), 0) == 1:
            hits = by_name.get(_name_key(name), [])
            role = hits[0] if len(hits) == 1 else None
        out[bid] = (role, name)
    return out


def role_block_reason(guild, role, actor):
    """Motivo por el que NO se puede devolver ese rol (o None si se puede)."""
    if role.is_default():
        return 'es @everyone'
    if role.managed:
        return 'es de un bot/integracion (no se asigna a mano)'
    me = guild.me
    if me is None or role >= me.top_role:
        return 'esta por encima de mi rol'
    if actor is not None and not (is_owner(actor) or is_admin(actor)) and M.role_has_mod_perms(role):
        return 'tiene permisos de moderacion (solo el owner o un admin puede devolverlo)'
    return None


# ============================================================================
# PLAN: comparar el backup con el servidor ahora
# ============================================================================
@dataclass
class RoleStat:
    role: object
    have: list = field(default_factory=list)        # ids de los que ya lo tienen
    missing: list = field(default_factory=list)     # ids de los que les falta
    absent: list = field(default_factory=list)      # ids de los que ya no estan en el servidor
    extra_now: object = None                        # cuantos lo tienen ahora sin estar en el backup (None = no se sabe)


@dataclass
class Plan:
    backup: dict
    source: str
    scope_role: object = None
    person_filter: bool = False
    stats: dict = field(default_factory=dict)       # role.id -> RoleStat
    adds: dict = field(default_factory=dict)        # member.id -> (member, [roles])
    absent: dict = field(default_factory=dict)      # uid -> nombre que tenia en el backup
    skipped: list = field(default_factory=list)     # [(nombre_rol, motivo)]
    notes: list = field(default_factory=list)


@dataclass
class Result:
    done: int = 0
    given: int = 0
    already: int = 0
    left: int = 0
    failed: list = field(default_factory=list)
    by_role: dict = field(default_factory=dict)


async def build_plan(guild, backup, source, actor, role=None, member_ids=None):
    """role = un rol concreto (o None = todos los del backup); member_ids = set de ids (o None = todos)."""
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        raise _err('Me falta el permiso **Manage Roles** para devolver roles.')
    if not guild.chunked:
        try:
            await guild.chunk()
        except Exception:
            pass

    people = backup_people(backup)
    rmap = map_roles(guild, backup)
    plan = Plan(backup=backup, source=source, scope_role=role, person_filter=member_ids is not None)

    if backup.get('guildId') and str(backup['guildId']) != str(guild.id):
        plan.notes.append(f'Ojo: este backup es de **otro servidor** ({backup.get("guildName", "?")}). '
                          'Los roles se emparejan por nombre.')
    if 'members' not in backup:
        plan.notes.append('Ese backup es anterior a esta actualizacion y no trae los roles de los miembros: '
                          'solo puedo devolver roles a **bots**. Haz un `/save` nuevo y de ahora en adelante si los guarda.')
    elif backup.get('memberInventoryComplete') is False:
        plan.notes.append('La lista de miembros de ese backup puede estar incompleta.')

    if role is not None:
        if not any(cur is not None and cur.id == role.id for cur, _n in rmap.values()):
            raise _err(f'El rol **{role.name}** no esta en ese backup.')
        why = role_block_reason(guild, role, actor)
        if why:
            raise _err(f'No puedo devolver **{role.name}**: {why}.')

    # roles del backup que entran en esta operacion
    scope = {}
    for bid, (cur, bname) in rmap.items():
        if role is not None and (cur is None or cur.id != role.id):
            continue
        if not any(bid in info['roles'] for info in people.values()):
            continue
        if cur is None:
            plan.skipped.append((bname, 'ya no existe en el servidor (usa /restore primero)'))
            continue
        why = role_block_reason(guild, cur, actor)
        if why:
            if not cur.managed:
                plan.skipped.append((cur.name, why))
            continue
        scope[bid] = cur

    holders = {}
    for uid, info in people.items():
        for bid in info['roles']:
            cur = scope.get(bid)
            if cur is not None:
                holders.setdefault(cur.id, set()).add(uid)

    for uid, info in people.items():
        if member_ids is not None and int(uid) not in member_ids:
            continue
        wanted = [scope[b] for b in info['roles'] if b in scope]
        if not wanted:
            continue
        member = guild.get_member(int(uid))
        if member is None and member_ids is not None:
            try:
                member = await guild.fetch_member(int(uid))
            except Exception:
                member = None
        for cur in wanted:
            st = plan.stats.setdefault(cur.id, RoleStat(role=cur))
            if member is None:
                if uid not in st.absent:
                    st.absent.append(uid)
                plan.absent[uid] = info['name']
            elif cur in member.roles:
                if member.id not in st.have:
                    st.have.append(member.id)
            elif member.id not in st.missing:
                st.missing.append(member.id)
                entry = plan.adds.setdefault(member.id, (member, []))
                if cur not in entry[1]:
                    entry[1].append(cur)

    if 'members' in backup and member_ids is None and backup.get('memberInventoryComplete') is not False:
        for st in plan.stats.values():
            before = holders.get(st.role.id, set())
            st.extra_now = sum(1 for mem in st.role.members if str(mem.id) not in before)
    return plan


async def apply_plan(guild, plan, actor, progress=None):
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        raise _err('Me falta el permiso **Manage Roles**.')
    lock = _locks.setdefault(guild.id, asyncio.Lock())
    if lock.locked():
        raise _err('Ya estoy devolviendo roles en este servidor. Espera a que termine.')
    res = Result()
    async with lock:
        total = len(plan.adds)
        last = time.monotonic()
        for i, (uid, (_old, roles)) in enumerate(list(plan.adds.items()), 1):
            live = guild.get_member(uid)           # se vuelve a mirar: pudo cambiar desde el resumen
            if live is None:
                res.left += 1
                continue
            needed = [r for r in roles if r not in live.roles]
            if not needed:
                res.already += 1
                continue
            todo = []
            for r in needed:
                why = role_block_reason(guild, r, actor)
                if why:
                    res.failed.append((live.display_name, f'{r.name}: {why}'))
                else:
                    todo.append(r)
            if todo:
                try:
                    await live.add_roles(*todo, reason=f'[Jarvis] Devolver rol segun el backup (por {actor})'[:500],
                                         atomic=False)
                    res.done += 1
                    res.given += len(todo)
                    for r in todo:
                        res.by_role[r.name] = res.by_role.get(r.name, 0) + 1
                except discord.Forbidden:
                    res.failed.append((live.display_name, 'sin permisos o jerarquia'))
                except discord.HTTPException as e:
                    res.failed.append((live.display_name, M.short_error(e, 80)))
            if progress is not None and time.monotonic() - last >= PROGRESS_EVERY:
                last = time.monotonic()
                await progress(i, total)
    return res


# ============================================================================
# EMBEDS
# ============================================================================
def preview_embed(plan):
    n_members = len(plan.adds)
    n_roles = sum(len(v[1]) for v in plan.adds.values())
    role = plan.scope_role
    title = f'Devolver rol: {role.name}' if role is not None else 'Devolver roles desde el backup'
    desc = [f'Backup guardado {_saved_text(plan.backup)} ({plan.source}).']
    desc.extend(plan.notes)

    if role is None:
        rows = sorted(plan.stats.values(), key=lambda s: -s.role.position)
        pending = [s for s in rows if s.missing]
        if pending:
            desc.append('')
            for s in pending[:20]:
                line = f'{s.role.mention}: faltan **{len(s.missing)}** | ya lo tienen {len(s.have)}'
                if s.absent:
                    line += f' | ya no estan {len(s.absent)}'
                desc.append(line)
            if len(pending) > 20:
                desc.append(f'... y {len(pending) - 20} roles mas')
        elif not plan.stats:
            desc.append('')
            desc.append('En el backup no hay roles que devolver (o nadie los tenia).')
        quiet = len(rows) - len(pending)
        if quiet:
            desc.append('')
            desc.append(f'Roles sin pendientes: {quiet}')

    e = discord.Embed(title=_fit(title, 256), description=_fit('\n'.join(desc), 4000),
                      color=0xf1c40f if n_members else 0x95a5a6)

    if role is not None:
        st = plan.stats.get(role.id)
        if st is None:
            extra = ' (entre las personas que pediste).' if plan.person_filter else '.'
            e.description = _fit(e.description + '\n\nEn el backup **nadie** tenia ese rol' + extra, 4000)
        else:
            total = len(st.have) + len(st.missing) + len(st.absent)
            e.add_field(name='Lo tenian en el backup', value=str(total), inline=True)
            e.add_field(name='Ya lo tienen', value=str(len(st.have)), inline=True)
            e.add_field(name='Les falta', value=str(len(st.missing)), inline=True)
            if st.have:
                e.add_field(name='Ya lo tienen (cuales)', value=_fit(_mentions(st.have), 1024), inline=False)
            if st.missing:
                e.add_field(name='Les falta (se les devuelve)', value=_fit(_mentions(st.missing), 1024), inline=False)
            if st.absent:
                names = [plan.absent.get(u, u) for u in st.absent]
                shown = ', '.join(str(n) for n in names[:LIST_SHOW])
                if len(names) > LIST_SHOW:
                    shown += f' y {len(names) - LIST_SHOW} mas'
                e.add_field(name='Ya no estan en el servidor', value=_fit(shown, 1024), inline=False)
            if st.extra_now:
                e.add_field(name='Lo tienen ahora pero no estaban en el backup',
                            value=f'{st.extra_now} (no los toco)', inline=False)

    if plan.skipped:
        rows = [f'{n}: {why}' for n, why in plan.skipped[:8]]
        if len(plan.skipped) > 8:
            rows.append(f'... y {len(plan.skipped) - 8} mas')
        e.add_field(name='No puedo devolver', value=_fit('\n'.join(rows), 1024), inline=False)

    if n_members:
        e.set_footer(text=f'Se devolveran {n_roles} rol(es) a {n_members} miembro(s). Confirma con el boton.')
    else:
        e.set_footer(text='No hay nadie a quien devolverle roles.')
    e.timestamp = datetime.now(timezone.utc)
    return e


def result_embed(plan, res, actor):
    ok = res.done > 0
    e = discord.Embed(
        title='Roles devueltos' if ok else 'No se devolvio ningun rol',
        description=f'Segun el backup guardado {_saved_text(plan.backup)}.',
        color=0x2ecc71 if ok else 0xe67e22,
    )
    e.add_field(name='Miembros con roles devueltos', value=str(res.done), inline=True)
    e.add_field(name='Roles dados', value=str(res.given), inline=True)
    if res.already:
        e.add_field(name='Ya lo tenian', value=str(res.already), inline=True)
    if res.left:
        e.add_field(name='Ya no estan en el servidor', value=str(res.left), inline=True)
    if res.by_role:
        lines = [f'**{n}**: {c}' for n, c in sorted(res.by_role.items(), key=lambda kv: -kv[1])[:15]]
        if len(res.by_role) > 15:
            lines.append(f'... y {len(res.by_role) - 15} roles mas')
        e.add_field(name='Por rol', value=_fit('\n'.join(lines), 1024), inline=False)
    if res.failed:
        lines = [f'{n}: {why}' for n, why in res.failed[:8]]
        if len(res.failed) > 8:
            lines.append(f'... y {len(res.failed) - 8} mas')
        e.add_field(name=f'No pude ({len(res.failed)})', value=_fit('\n'.join(lines), 1024), inline=False)
    e.set_footer(text=f'Ordenado por {actor}')
    e.timestamp = datetime.now(timezone.utc)
    return e


# ============================================================================
# BOTONES
# ============================================================================
async def _finish(interaction, text, embed):
    """Deja el resultado en el mensaje; si el token de la interaccion ya caduco, lo manda como mensaje nuevo."""
    try:
        await interaction.edit_original_response(content=text or None, embed=embed, view=None)
        return
    except Exception:
        pass
    try:
        ch = interaction.channel or (interaction.message.channel if interaction.message else None)
        if ch is not None:
            await ch.send(content=text or None, embed=embed)
    except Exception as e:
        print(f'[DevolverRol] No pude mostrar el resultado: {e}')


class DevolverRolView(discord.ui.View):
    def __init__(self, guild, author_id, plan):
        super().__init__(timeout=180)
        self.guild = guild
        self.author_id = author_id
        self.plan = plan
        self.message = None
        self.finished = False
        self.go_btn.label = f'Devolver a {len(plan.adds)} miembro(s)'[:80]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id or is_owner(interaction.user):
            return True
        await interaction.response.send_message('Solo quien lanzo el comando puede confirmar o cancelar.', ephemeral=True)
        return False

    async def on_timeout(self):
        if self.finished or self.message is None:
            return
        try:
            await self.message.edit(content='La confirmacion expiro, no hice nada.', embed=None, view=None)
        except Exception:
            pass

    @discord.ui.button(label='Devolver', style=discord.ButtonStyle.success)
    async def go_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            return await interaction.response.defer()
        self.finished = True
        self.stop()
        total = len(self.plan.adds)
        await interaction.response.edit_message(
            content=f'Devolviendo roles a {total} miembro(s)... (0/{total})', embed=None, view=None)

        async def progress(i, n):
            try:
                await interaction.edit_original_response(content=f'Devolviendo roles... ({i}/{n})')
            except Exception:
                pass

        try:
            res = await apply_plan(self.guild, self.plan, interaction.user, progress)
        except M.JarvisError as e:
            return await _finish(interaction, str(e), None)
        except Exception as e:
            print(f'[DevolverRol] apply: {e}')
            return await _finish(interaction, f'Error inesperado: {M.short_error(e)}', None)
        embed = result_embed(self.plan, res, interaction.user)
        await _finish(interaction, None, embed)
        try:
            await M.send_mod_log(M.client, self.guild.id, embed)
        except Exception:
            pass

    @discord.ui.button(label='Cancelar', style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            return await interaction.response.defer()
        self.finished = True
        self.stop()
        await interaction.response.edit_message(content='Cancelado, no hice nada.', embed=None, view=None)


async def prepare(guild, actor, attachment=None, role=None, member_ids=None):
    backup, source = await load_backup(guild, attachment)
    plan = await build_plan(guild, backup, source, actor, role=role, member_ids=member_ids)
    return plan, preview_embed(plan)


# ============================================================================
# /devolver_rol
# ============================================================================
@app_commands.command(name='devolver_rol',
                      description='Devuelve roles segun el backup de /save: quien lo tenia y a quien le falta')
@app_commands.guild_only()
@app_commands.describe(
    rol='Rol que quieres devolver',
    usuario='Solo a esta persona (omitir = a todos los del backup)',
    todos='Devolver TODOS los roles del backup (solo si no eliges rol ni usuario)',
    archivo='Backup .json de /save (omitir = el ultimo que guardo el bot)',
)
async def slash_devolver_rol(interaction: discord.Interaction, rol: discord.Role = None,
                             usuario: discord.Member = None, todos: bool = False,
                             archivo: discord.Attachment = None):
    guild = interaction.guild
    if not can_use(interaction.user):
        return await interaction.response.send_message(
            'Necesitas ser admin del servidor o estar en la whitelist de Jarvis para usar esto.', ephemeral=True)
    if rol is None and usuario is None and not todos:
        return await interaction.response.send_message(
            'Elige un **rol**, un **usuario**, o marca **todos** para devolver todo lo del backup.', ephemeral=True)
    await interaction.response.defer()
    try:
        plan, embed = await prepare(guild, interaction.user, archivo, rol, {usuario.id} if usuario else None)
    except M.JarvisError as e:
        return await interaction.followup.send(str(e))
    except Exception as e:
        print(f'[DevolverRol] /devolver_rol: {e}')
        return await interaction.followup.send(f'Error inesperado: {M.short_error(e)}')
    if not plan.adds:
        return await interaction.followup.send(embed=embed)
    view = DevolverRolView(guild, interaction.user.id, plan)
    view.message = await interaction.followup.send(embed=embed, view=view)


# ============================================================================
# JARVIS: "jarvis devuelve el rol X"
# ============================================================================
async def act_devolver_rol(ctx, intent):
    guild, author = ctx.guild, ctx.author
    if not can_use(author):
        raise _err('Solo el owner, los admins y la whitelist de Jarvis pueden devolver roles.')
    attachment = next((a for a in ctx.message.attachments if a.filename.lower().endswith('.json')), None)

    role = None
    raw_role = (intent.role or intent.name or '').strip()
    if raw_role:
        role = M.find_role_flexible(ctx, raw_role)

    member_ids = None
    if intent.targets or intent.target_query:
        chat = await ctx.get_chat()
        targets, err = await M.resolve_targets(ctx, intent, chat)
        if err:
            raise _err(err)
        member_ids = {t.id for t in targets}

    all_mode = (intent.text or '').strip().lower() == 'todos' or bool(re.search(r'\b(?:todos|todas)\b', _norm(ctx.text)))
    if role is None and member_ids is None and not all_mode:
        raise _err('Que rol quieres devolver? Dime el nombre (`jarvis devuelve el rol VIP`) o '
                   '`jarvis devuelve todos los roles del backup`.')

    plan, embed = await prepare(guild, author, attachment, role, member_ids)
    reply = M.make_replier(ctx.message)
    if not plan.adds:
        await reply(embed=embed)
        return None
    view = DevolverRolView(guild, author.id, plan)
    view.message = await reply(embed=embed, view=view)
    return None


# ------------------------------------------------------------ patrones (sin IA) --
_RX_STRONG = re.compile(r'\b(?:devuelv\w*|devolv\w*)\b')
_RX_WEAK = re.compile(r'\b(?:restaur\w*|recuper\w*|repon\w*|regres\w*|vuelv\w*)\b')
_RX_ROL = re.compile(r'\broles?\b')
_RX_BACKUP = re.compile(r'\b(?:backups?|respaldos?|copia)\b')
_RX_WHO = re.compile(r'\b(?:quien|quienes|cual|cuales)\b')
_RX_STATE = re.compile(r'\b(?:tenia\w*|tenian|perdio|perdieron|falta\w*|quito\w*|quitaron)\b')
_RX_SELF = re.compile(r'\b(?:devuelveme|devolverme|devuelvanme|devuelvaseme)\b')
_USER_MENTION = re.compile(r'<@!?(\d+)>')
_ROLE_MENTION = re.compile(r'<@&\d+>')
_BACKUP_PART = (r'(?:del?|en|con|segun|desde|por|usando)\s+(?:(?:el|la|mi|tu|ese|este)\s+)?'
                r'(?:backups?|respaldos?|copia)\b')
_BACKUP_TAIL = re.compile(r'\s+' + _BACKUP_PART + r'.*$', re.I | re.S)
_BACKUP_TAIL_ONLY = re.compile(_BACKUP_PART, re.I)


def devolver_regex_intent(ctx):
    text = ctx.text or ''
    nn = _norm(text)
    if not _RX_ROL.search(nn):
        return None
    backup_word = _RX_BACKUP.search(nn)
    asking = _RX_WHO.search(nn) and _RX_STATE.search(nn) and backup_word
    if not (_RX_STRONG.search(nn) or (_RX_WEAK.search(nn) and backup_word) or asking):
        return None

    people = _USER_MENTION.findall(text)
    if _RX_SELF.search(nn):
        people.append(str(ctx.author.id))
    people = list(dict.fromkeys(people))[:5]

    clean_tail = True
    rm = _ROLE_MENTION.search(text)
    if rm:
        role_txt = rm.group(0)
    else:
        m = re.search(r'\broles?\b\s*(.*)$', text, re.I | re.S)
        rest = ' ' + (m.group(1) if m else '')
        tail_m = _BACKUP_TAIL.search(rest)
        if tail_m:
            clean_tail = bool(_BACKUP_TAIL_ONLY.fullmatch(tail_m.group(0).strip()))
            rest = rest[:tail_m.start()]
        rest = _USER_MENTION.sub(' ', rest)
        rest = re.sub(r'\b(?:por\s+favor|porfa|plis|please)\b', ' ', rest, flags=re.I)
        rest = re.sub(r'\s+(?:a|para)\s+(?:todos|todas)\s*$', '', rest.rstrip(' .,:;!?'), flags=re.I)
        rest = re.sub(r'(?:^|\s)(?:a|al|para|de|del)\s*$', ' ', rest.strip(), flags=re.I)
        rest = re.sub(r'^(?:de|del)\s+', '', rest.strip(), flags=re.I)
        role_txt = re.sub(r'\s+', ' ', rest).strip(' .,:;!?')

    all_mode = not role_txt and not people and bool(re.search(r'\b(?:todos|todas)\b', nn))
    nwords = len(nn.split())
    explicit = clean_tail and nwords <= 12 and len(role_txt.split()) <= 4 and bool(role_txt or people or all_mode)
    return M.Intent(action='devolver_rol', source='regex', role=role_txt or None, targets=people,
                    text='todos' if all_mode else None, explicit=explicit)


INTENT_EXTRA = """
ACCIONES EXTRA (devolver roles con el backup):
- devolver_rol: recuperar un rol segun el BACKUP del servidor (el que se guarda con /save): Jarvis mira quien lo tenia en el backup y a quien le falta ahora, lo muestra y pide confirmar antes de devolverlo. role = nombre o @mencion del rol; target_ids = solo esa(s) persona(s) (vacio = todos los del backup); text="todos" si piden devolver TODOS los roles del backup sin nombrar uno.
REGLA EXTRA: "devuelve el rol X", "devuelvele el rol X a Juan", "devuelvele sus roles a Juan", "restaura el rol X del backup", "quien tenia el rol X y ya no", "mira a quien le falta el rol X" = devolver_rol (NUNCA role_add: role_add es para dar un rol SIN usar el backup). "devuelveme el rol X" = devolver_rol con el id del JEFE en target_ids.
"""


# ============================================================================
# INSTALACION
# ============================================================================
async def add_members_to_backup(guild, backup, file_path):
    """Anade al backup que roles tiene cada miembro y vuelve a escribir el archivo."""
    if not guild.chunked:
        try:
            await guild.chunk()
        except Exception:
            pass
    members = []
    for mem in guild.members:
        rids = [str(r.id) for r in mem.roles if not r.is_default()]
        if rids:
            members.append({'id': str(mem.id), 'name': mem.name, 'bot': bool(mem.bot), 'roleIds': rids})
    backup['members'] = members
    backup['memberInventoryComplete'] = bool(guild.chunked)
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(backup, f, indent=2, ensure_ascii=False)


def install(m):
    global M, _installed
    M = m
    if _installed:
        return
    _installed = True

    # 1) /save tambien guarda los roles de cada miembro
    orig_save = m.save_server_config

    async def save_server_config(guild):
        backup, file_path = await orig_save(guild)
        try:
            await add_members_to_backup(guild, backup, file_path)
        except Exception as e:
            print(f'[DevolverRol] No pude anadir los miembros al backup: {e}')
        return backup, file_path

    m.save_server_config = save_server_config

    # 2) accion de Jarvis
    m.PLAIN_ACTIONS.add('devolver_rol')
    m.KNOWN_ACTIONS.add('devolver_rol')
    m.PLAIN_HANDLERS['devolver_rol'] = act_devolver_rol
    m.ACTION_LABELS['devolver_rol'] = 'Devolver rol (backup)'
    m.INTENT_SYSTEM_PROMPT = m.INTENT_SYSTEM_PROMPT + INTENT_EXTRA

    # 3) patron sin IA (va antes que los demas)
    orig_regex = m.regex_parse_intent

    def regex_parse_intent(ctx):
        try:
            it = devolver_regex_intent(ctx)
        except Exception as e:
            print(f'[DevolverRol] patron: {e}')
            it = None
        return it if it is not None else orig_regex(ctx)

    m.regex_parse_intent = regex_parse_intent

    # 4) ayuda
    orig_help = m.build_help_embed

    def build_help_embed():
        emb = orig_help()
        value = ('`/devolver_rol rol:@VIP` - miro el backup de /save: quien tenia el rol, quien ya lo tiene y a quien le falta; '
                 'con un boton se lo devuelvo\n'
                 '`/devolver_rol usuario:@Juan` (todos sus roles) | `todos:True` (todo el backup) | `archivo:` (un backup concreto)\n'
                 '`jarvis devuelve el rol VIP` / `jarvis devuelvele sus roles a @Juan` / `jarvis quien tenia el rol VIP en el backup`\n'
                 'Haz `/save` para actualizar el backup (ahora tambien guarda los roles de cada miembro).')
        if len(emb.fields) < 25 and len(emb) + len(value) + 40 < 5900:
            emb.add_field(name='Devolver roles (backup)', value=value, inline=False)
        return emb

    m.build_help_embed = build_help_embed

    # 5) comando slash
    m.client.tree.add_command(slash_devolver_rol, override=True)
    print('[DevolverRol] listo: /devolver_rol y "jarvis devuelve el rol X"')
