"""
jarvis_gestion.py  -  parche de gestion para Jarvis (roles / canales / cadenas de ordenes)

Que arregla
-----------
1. BUG "crea el rol X y daselo" -> asignaba OTRO rol (el que mas se parecia):
   - guild.create_role() NO mete el rol nuevo en la cache de discord.py hasta que llega el evento del
     gateway, asi que el 2do paso de la cadena no lo encontraba y caia al "parecido" (muy permisivo).
   - find_role_or_fail() borraba palabras como "de"/"a" DENTRO del nombre ("puta de mani" -> "puta mani").
   - resolve_role() aceptaba cualquier rol con similitud >= 0.55 (y los nombres que son solo simbolos
     puntuaban 0.9 contra cualquier cosa).
   Ahora: los roles creados en la orden se recuerdan, la cache se refresca con fetch_roles() si hace falta,
   las coincidencias aproximadas NUNCA se aplican solas (se pregunta "te referias a X?") y ante varios roles
   que encajan se pide precision en vez de adivinar.
2. Mejor IA: el prompt del cerebro recibe reglas extra (crear+dar, dar varios roles, borrar, permisos,
   nombres tal cual) y las respuestas del modelo se corrigen/normalizan antes de ejecutarse.
3. Mejor plan B (sin IA): patrones para crear/borrar roles y canales/categorias y "crea el rol X y daselo".
4. Nuevas capacidades: varios roles en una orden (crear, dar, quitar, borrar), editar permisos de un rol,
   borrar varios canales o una categoria con sus canales, reutilizar un rol que ya existia.

Instalacion: al final de bot.py, DESPUES de los otros jarvis_*.install(...):
    import jarvis_gestion
    jarvis_gestion.install(sys.modules[__name__])
"""
import re
import difflib

import discord

M = None   # modulo principal (se asigna en install)

_I = re.IGNORECASE
_S = re.DOTALL
_QUOTES = '"\'`\u201c\u201d\u2018\u2019\u00ab\u00bb'
_PROMPT_MARK = 'REGLAS EXTRA DE GESTION'


# ============================================================================
# UTILIDADES DE TEXTO
# ============================================================================
def _norm(s):
    return M.normalize_for_compare(s or '')


_GIVE = (
    r'(?:d[a\u00e1]se(?:lo|la)|d[a\u00e1]me(?:lo|la)?|d[a\u00e1]le(?:lo|la)?|'
    r'p[o\u00f3]nse(?:lo|la)|p[o\u00f3]nme(?:lo|la)?|p[o\u00f3]nle(?:lo|la)?|'
    r'asign[a\u00e1]se(?:lo|la)|asign[a\u00e1]me(?:lo|la)?|asign[a\u00e1]le(?:lo|la)?|asign[a\u00e1](?:lo|la)|'
    r'ot[o\u00f3]rgase(?:lo|la)|ot[o\u00f3]rga(?:me|le)(?:lo|la)?)'
)
_GIVE_TAIL = re.compile(r'(?:\s+(?:y|e)\s+|\s*,\s*)(?P<give>' + _GIVE + r')\b(?P<tail>.*)$', _I | _S)
_SELF_GIVE = re.compile(r'(?:d[a\u00e1]me|p[o\u00f3]nme|asign[a\u00e1]me|ot[o\u00f3]rgame)', _I)
_NAMED = re.compile(r'\b(?:llamad[oa]|que\s+se\s+llame|de\s+nombre|con\s+(?:el\s+)?nombre(?:\s+de)?)\s+(?P<n>.+)$', _I | _S)
_COLOR = re.compile(r'\s*\b(?:de\s+)?color\s+(?P<c>#?[0-9a-fA-F]{6}\b|[^\W\d_]+)', _I)
_GENERIC_WORDS = re.compile(
    r'\b(?:el|la|lo|los|las|ese|esa|eso|este|esta|esto|nuevo|nueva|rol|role|recien|creado|creada|mismo|misma|'
    r'que|acabas|acabo|de|crear|crea|creaste)\b')


def clean_name(raw):
    """Limpia el nombre de un rol/canal nuevo sin tocar el nombre en si (conserva 'de', 'la', tildes, emojis)."""
    t = (raw or '').strip()
    if not t:
        return ''
    t = _GIVE_TAIL.sub('', t).strip()
    t = re.sub(r'^(?:el|la|un|una)\s+(?:nuevo\s+|nueva\s+)?(?:rol|role|canal|categor[i\u00ed]a)\s+', '', t, flags=_I)
    t = re.sub(r'^(?:que\s+se\s+llame|llamad[oa]|de\s+nombre|con\s+(?:el\s+)?nombre(?:\s+de)?|el\s+nombre(?:\s+de)?)\s*[:\-]?\s+',
               '', t, flags=_I)
    t = t.strip(_QUOTES + ' ').rstrip('.,;')
    return t.strip(_QUOTES + ' ')


def clean_role_text(raw):
    """Para BUSCAR un rol: quita 'el rol ...' del principio pero respeta mencion/ID y el resto del nombre."""
    t = (raw or '').strip()
    if re.fullmatch(r'<@&\d+>|\d{17,20}', t):
        return t
    t = re.sub(r'^(?:(?:el|la|los|las)\s+)?(?:rol(?:es)?|role)\s+(?:(?:llamado|llamada|de\s+nombre)\s+)?', '', t, flags=_I)
    return t.strip(_QUOTES + ' ')


def is_generic(raw):
    """'el rol', 'ese', 'lo', 'el nuevo'... = se refiere al que acabo de crear."""
    return not _GENERIC_WORDS.sub(' ', _norm(raw)).strip()


# ============================================================================
# RESOLUCION DE ROLES
# ============================================================================
def lookup_role(roles, raw):
    """Devuelve (rol|None, nivel, candidatos).
    nivel: 0 exacto | 1 empieza con | 2 contiene | 3 solo aproximado (NUNCA se aplica solo) | 9 nada."""
    raw = clean_role_text(raw)
    if not raw:
        return None, 9, []
    mm = re.fullmatch(r'<@&(\d+)>|(\d{17,20})', raw)
    if mm:
        rid = int(mm.group(1) or mm.group(2))
        r = next((x for x in roles if x.id == rid), None)
        return (r, 0, [r]) if r else (None, 9, [])

    pool = [r for r in roles if not r.is_default()]
    exact = [r for r in pool if r.name.lower() == raw.lower()]
    if exact:
        return exact[0], 0, exact

    key = _norm(raw)
    if not key:
        return None, 9, []
    named = [(r, _norm(r.name)) for r in pool]
    named = [(r, n) for r, n in named if n]          # nombres hechos solo de simbolos no entran en comparaciones

    hits = [r for r, n in named if n == key]
    if hits:
        return hits[0], 0, hits
    if len(key) >= 2:
        hits = [r for r, n in named if n.startswith(key)]
        if hits:
            hits.sort(key=lambda r: len(r.name))
            return hits[0], 1, hits
    if len(key) >= 3:
        hits = [r for r, n in named if key in n]
        if hits:
            hits.sort(key=lambda r: len(r.name))
            return hits[0], 2, hits
        inside = [(r, n) for r, n in named
                  if len(n) >= 3 and re.search(r'(?<![a-z0-9])' + re.escape(n) + r'(?![a-z0-9])', key)]
        if inside:
            inside.sort(key=lambda x: -len(x[1]))
            return inside[0][0], 2, [inside[0][0]]
    scored = sorted(((difflib.SequenceMatcher(None, key, n).ratio(), r) for r, n in named), key=lambda x: -x[0])
    good = [r for score, r in scored[:4] if score >= 0.6]
    if good:
        return good[0], 3, good
    return None, 9, []


def _pick(raw, role, tier, hits, strict=False):
    if role is None:
        raise M.JarvisError(
            f'No encontre el rol `{clean_role_text(raw) or raw}`. Usa el nombre exacto, el ID o <@&ID>.\n'
            f'Usa `jarvis lista roles` para ver todos los roles disponibles.')
    if tier == 3:
        names = ', '.join(f'**{r.name}**' for r in hits[:3])
        raise M.JarvisError(f'No hay ningun rol llamado `{clean_role_text(raw)}`. Te referias a {names}? '
                            f'Dimelo con el nombre exacto.')
    if tier in (1, 2) and len(hits) > 1:
        names = ', '.join(f'**{r.name}**' for r in hits[:5])
        raise M.JarvisError(f'Hay varios roles que encajan con `{clean_role_text(raw)}`: {names}. Dime cual (nombre exacto).')
    if strict and not (tier == 0 or (tier == 1 and len(hits) == 1)):
        raise M.JarvisError(f'Para borrar necesito el nombre exacto. Te referias a **{role.name}**? Escribelo completo.')
    return role


def _created_roles(ctx):
    lst = getattr(ctx, 'created_roles', None)
    if lst is None:
        lst = []
        try:
            ctx.created_roles = lst
        except Exception:
            pass
    return lst


def _remember(ctx, role):
    lst = _created_roles(ctx)
    if all(r.id != role.id for r in lst):
        lst.append(role)


def role_pool(ctx):
    """Roles del servidor + los que se crearon en esta orden + los traidos con fetch_roles (cache atrasada)."""
    seen, out = set(), []
    for r in (list(ctx.guild.roles) + list(getattr(ctx, '_extra_roles', []) or []) + list(_created_roles(ctx))):
        if r.id not in seen:
            seen.add(r.id)
            out.append(r)
    return out


def _created_match(ctx, raw):
    created = _created_roles(ctx)
    if not created:
        return None
    cleaned = clean_role_text(raw)
    if not cleaned or is_generic(cleaned):
        return created[-1]
    r, tier, _ = lookup_role(created, cleaned)
    return r if (r is not None and tier == 0) else None


async def afind_role(ctx, raw, strict=False):
    """Busca un rol (async: si no aparece refresca la cache de roles antes de rendirse)."""
    if not (raw or '').strip():
        raise M.JarvisError('Que rol quieres que use?')
    hit = _created_match(ctx, raw)
    if hit is not None:
        return hit
    role, tier, hits = lookup_role(role_pool(ctx), raw)
    if role is None or tier == 3:
        try:
            ctx._extra_roles = list(await ctx.guild.fetch_roles())
            role, tier, hits = lookup_role(role_pool(ctx), raw)
        except Exception:
            pass
    return _pick(raw, role, tier, hits, strict)


# versiones sincronas con los mismos nombres de siempre (otros modulos las pueden usar)
def resolve_role(guild, role_name):
    role, tier, _ = lookup_role(list(guild.roles), role_name)
    return role if (role is not None and tier <= 2) else None


def find_role_or_fail(ctx, raw):
    if not (raw or '').strip():
        raise M.JarvisError('Que rol quieres que use?')
    hit = _created_match(ctx, raw)
    if hit is not None:
        return hit
    role, tier, hits = lookup_role(role_pool(ctx), raw)
    return _pick(raw, role, tier, hits)


def find_role_flexible(ctx, raw):
    return find_role_or_fail(ctx, raw)


# ============================================================================
# HANDLERS DE ROLES
# ============================================================================
def _raw_roles(intent):
    raws = [str(r).strip() for r in (intent.names or []) if r and str(r).strip()]
    if not raws and (intent.role or '').strip():
        raws = [intent.role.strip()]
    return raws[:10]


def _temp_secs(intent):
    if not intent.duration:
        return None
    secs = M.parse_natural_duration(intent.duration)
    if not secs or secs > 30 * 86400:
        raise M.JarvisError(f'No entendi la duracion `{intent.duration}` (maximo 30 dias).')
    return secs


async def act_create_role(ctx, intent):
    names = [n for n in (clean_name(x) for x in (intent.names or [])) if n]
    if not names:
        n = clean_name(intent.name or intent.role)
        if n:
            names = [n]
    if not names:
        raise M.JarvisError('Como se llama el rol?')
    names = names[:10]

    base = {}
    if intent.color:
        col = M.parse_color(intent.color)
        if col is None:
            raise M.JarvisError(f'No entendi el color `{intent.color}`. Usa un nombre (rojo, azul...) o un hex como #ff0000.')
        base['colour'] = col
    if intent.hoist is not None:
        base['hoist'] = intent.hoist
    if intent.mentionable is not None:
        base['mentionable'] = intent.mentionable
    if intent.perms:
        perms, bad = M.build_permissions(intent.perms)
        if bad:
            raise M.JarvisError('No reconozco estos permisos: ' + ', '.join(f'`{b}`' for b in bad))
        if str(ctx.author.id) != M.OWNER_ID and M.perms_have_mod(perms):
            raise M.JarvisError('Solo el owner puede crear roles con permisos de moderacion.')
        base['permissions'] = perms

    created, existing, errors = [], [], []
    for n in names:
        dup = next((r for r in role_pool(ctx) if not r.is_default() and r.name.lower() == n.lower()), None)
        if dup is not None:                       # ya existia: lo reutilizo (asi "crea X y daselo" no duplica)
            existing.append(dup)
            _remember(ctx, dup)
            continue
        try:
            role = await ctx.guild.create_role(name=n[:100], reason=f'[Jarvis] por {ctx.author}', **base)
        except Exception as e:
            if not created and not existing:
                raise
            errors.append(f'{n}: {M.short_error(e)}')
            break
        _remember(ctx, role)                      # <- clave: el siguiente paso de la cadena lo encuentra aunque la cache no se haya actualizado
        created.append(role)

    parts = []
    if created:
        parts.append(('Se creo el rol ' if len(created) == 1 else f'Se crearon **{len(created)}** roles:\n')
                     + (f'{created[0].mention} (`{created[0].id}`).' if len(created) == 1
                        else '\n'.join(f'\u2022 {r.mention} (`{r.id}`)' for r in created)))
    if existing:
        parts.append('Ya existia, lo reutilizo: ' + ', '.join(r.mention for r in existing))
    if errors:
        parts.append('No pude seguir: ' + '; '.join(errors))
    title = 'Rol Creado' if (len(created) + len(existing)) == 1 else 'Roles Creados'
    return M.simple_embed(title, '\n'.join(parts), 0x2ecc71)


async def act_role_add(ctx, t, intent):
    if t.member is None:
        raise M.JarvisError(f'{t.name} ya no esta en el servidor.')
    raws = _raw_roles(intent)
    if not raws:
        raise M.JarvisError('Que rol quieres que use?')
    secs = _temp_secs(intent)
    given, notes = [], []
    for raw in raws:
        try:
            role = await afind_role(ctx, raw)
            M.guard_role(ctx, role)
            if role in t.member.roles:
                raise M.JarvisError(f'{t.member} ya tiene el rol **{role.name}**.')
            await t.member.add_roles(role, reason=f'[Jarvis] Asignado por {ctx.author}')
        except M.JarvisError as e:
            notes.append(str(e))
            continue
        given.append(role)
        if secs:
            M.add_temprole(ctx.guild.id, t.id, role.id, 'add', secs)
    if not given:
        raise M.JarvisError('\n'.join(notes))
    extra = ''
    if secs:
        extra += f'\nSe lo quito solo <t:{int(M.time.time() + secs)}:R>.'
    if notes:
        extra += '\nOjo: ' + ' | '.join(notes)
    names = ', '.join(f'**{r.name}**' for r in given)
    return M.simple_embed('Rol Asignado' if len(given) == 1 else 'Roles Asignados',
                          f'Se asigno {names} a {t.member}.{extra}', 0x2ecc71)


async def act_role_remove(ctx, t, intent):
    if t.member is None:
        raise M.JarvisError(f'{t.name} ya no esta en el servidor.')
    raws = _raw_roles(intent)
    if not raws:
        raise M.JarvisError('Que rol quieres que use?')
    if t.id != ctx.author.id and ctx.me is not None and ctx.me.top_role <= t.member.top_role:
        raise M.JarvisError(f'Mi rol es inferior al de {t.member}, no puedo modificar sus roles.')
    secs = _temp_secs(intent)
    removed, notes = [], []
    for raw in raws:
        try:
            role = await afind_role(ctx, raw)
            M.guard_role(ctx, role)
            if role not in t.member.roles:
                raise M.JarvisError(f'{t.member} no tiene el rol **{role.name}**.')
            await t.member.remove_roles(role, reason=f'[Jarvis] Removido por {ctx.author}')
        except M.JarvisError as e:
            notes.append(str(e))
            continue
        removed.append(role)
        if secs:
            M.add_temprole(ctx.guild.id, t.id, role.id, 'remove', secs)
    if not removed:
        raise M.JarvisError('\n'.join(notes))
    extra = ''
    if secs:
        extra += f'\nSe lo devuelvo solo <t:{int(M.time.time() + secs)}:R>.'
    if notes:
        extra += '\nOjo: ' + ' | '.join(notes)
    names = ', '.join(f'**{r.name}**' for r in removed)
    is_self = t.id == ctx.author.id
    embed = M.simple_embed(
        'Rol Removido' if len(removed) == 1 else 'Roles Removidos',
        (f'Se te quito {names}.' if is_self else f'Se quito {names} de {t.member}.') + extra, 0xe67e22)
    embed.add_field(name='Roles', value=', '.join(f'{r.mention} (`{r.id}`)' for r in removed)[:1000], inline=True)
    embed.add_field(name='Miembro', value=f'{t.member}', inline=True)
    embed.set_footer(text=f'Ejecutado por {ctx.author}')
    return embed


async def act_edit_role(ctx, intent):
    role = await afind_role(ctx, intent.role or intent.name)
    M.guard_role(ctx, role)
    kw, changes = {}, []
    new_name = clean_name(intent.new_name)
    if new_name:
        kw['name'] = new_name[:100]
        changes.append(f'Nombre: **{kw["name"]}**')
    if intent.color:
        col = M.parse_color(intent.color)
        if col is None:
            raise M.JarvisError(f'No entendi el color `{intent.color}`.')
        kw['colour'] = col
        changes.append(f'Color: {intent.color}')
    if intent.hoist is not None:
        kw['hoist'] = intent.hoist
        changes.append(f'Mostrar separado: {"si" if intent.hoist else "no"}')
    if intent.mentionable is not None:
        kw['mentionable'] = intent.mentionable
        changes.append(f'Mencionable: {"si" if intent.mentionable else "no"}')
    if intent.perms:
        perms, bad = M.build_permissions(intent.perms)
        if bad:
            raise M.JarvisError('No reconozco estos permisos: ' + ', '.join(f'`{b}`' for b in bad))
        if str(ctx.author.id) != M.OWNER_ID and M.perms_have_mod(perms):
            raise M.JarvisError('Solo el owner puede dar permisos de moderacion a un rol.')
        kw['permissions'] = discord.Permissions(role.permissions.value | perms.value)
        changes.append('Permisos anadidos: ' + ', '.join(f'`{p}`' for p in intent.perms[:10]))
    if not kw:
        raise M.JarvisError('Que quieres cambiarle al rol?')
    await role.edit(reason=f'[Jarvis] por {ctx.author}', **kw)
    return M.simple_embed('Rol Editado', f'{role.mention}\n' + '\n'.join(changes), 0x3498db)


async def act_delete_role(ctx, intent):
    raws = _raw_roles(intent) or ([intent.name.strip()] if (intent.name or '').strip() else [])
    if not raws:
        raise M.JarvisError('Que rol borro? Dime el nombre exacto.')
    deleted, notes = [], []
    for raw in raws:
        try:
            role = await afind_role(ctx, raw, strict=True)
            M.guard_role(ctx, role)
            name = role.name
            await role.delete(reason=f'[Jarvis] por {ctx.author}')
        except M.JarvisError as e:
            notes.append(str(e))
            continue
        deleted.append(name)
        lst = _created_roles(ctx)
        lst[:] = [r for r in lst if r.id != role.id]
    if not deleted:
        raise M.JarvisError('\n'.join(notes))
    desc = ('Se elimino el rol ' + f'**{deleted[0]}**.' if len(deleted) == 1
            else f'Se eliminaron **{len(deleted)}** roles: ' + ', '.join(f'**{n}**' for n in deleted))
    if notes:
        desc += '\nNo pude: ' + ' | '.join(notes)
    return M.simple_embed('Rol Eliminado' if len(deleted) == 1 else 'Roles Eliminados', desc, 0xe74c3c)


# ============================================================================
# HANDLERS DE CANALES
# ============================================================================
def _wrap_create_channel(orig):
    async def act_create_channel(ctx, intent):
        intent.names = [n for n in (clean_name(x) for x in (intent.names or [])) if n]
        if intent.name:
            intent.name = clean_name(intent.name)
        if intent.category:
            intent.category = clean_name(intent.category) or intent.category
        return await orig(ctx, intent)
    return act_create_channel


def _wrap_edit_channel(orig):
    async def act_edit_channel(ctx, intent):
        if intent.new_name:
            intent.new_name = clean_name(intent.new_name)
        return await orig(ctx, intent)
    return act_edit_channel


async def act_delete_channel(ctx, intent):
    guild = ctx.guild
    raws = [str(n).strip() for n in (intent.names or []) if n and str(n).strip()]
    if not raws:
        one = (intent.channel or intent.name or '').strip()
        if one:
            raws = [one]
    if not raws:
        raise M.JarvisError('Cual canal borro? Dime el nombre o mencionalo con #.')
    with_children = bool(re.search(r'canales|children|con_canales|todo|contenido|incluye', _norm(intent.text or '')))
    protected = {c for c in (guild.rules_channel, guild.public_updates_channel) if c is not None}

    deleted, notes, kids = [], [], 0
    for raw in raws[:10]:
        try:
            ch = M.resolve_channel(guild, clean_name(raw) or raw)
        except M.JarvisError as e:
            notes.append(str(e))
            continue
        if ch is None:
            notes.append(f'No encontre el canal `{raw}`.')
            continue
        if ch in protected:
            notes.append(f'**{ch.name}** es requerido por la comunidad del servidor, Discord no deja borrarlo.')
            continue
        if ch.id == ctx.message.channel.id:
            notes.append('No borro el canal donde me estas hablando (no podria responderte). Pidemelo desde otro canal.')
            continue
        is_cat = isinstance(ch, discord.CategoryChannel)
        if is_cat and with_children:
            for c in list(ch.channels):
                if c in protected or c.id == ctx.message.channel.id:
                    continue
                try:
                    await c.delete(reason=f'[Jarvis] por {ctx.author}')
                    kids += 1
                except Exception as e:
                    notes.append(f'{c.name}: {M.short_error(e)}')
        name = ch.name
        await ch.delete(reason=f'[Jarvis] por {ctx.author}')
        deleted.append(name + (' (categoria)' if is_cat else ''))
    if not deleted:
        raise M.JarvisError('\n'.join(notes) or 'No borre nada.')
    desc = (f'Se elimino **{deleted[0]}**.' if len(deleted) == 1
            else f'Se eliminaron **{len(deleted)}**: ' + ', '.join(f'**{n}**' for n in deleted))
    if kids:
        desc += f'\nTambien borre **{kids}** canal(es) de dentro.'
    elif any('(categoria)' in n for n in deleted):
        desc += '\nLos canales que tenia quedaron sin categoria.'
    if notes:
        desc += '\nNo pude: ' + ' | '.join(notes)
    return M.simple_embed('Canal Eliminado' if len(deleted) == 1 else 'Canales Eliminados', desc, 0xe74c3c)


# ============================================================================
# MEJORAS DE LA IA: prompt + correccion de la respuesta del modelo
# ============================================================================
PROMPT_ADDENDUM = """
REGLAS EXTRA DE GESTION (muy importantes, tienen prioridad sobre lo anterior si chocan):
23. Crear + dar en una sola orden ("crea el rol X y daselo", "crea el rol X y damelo", "crea un rol X y ponselo a @user") = {"actions":[{"action":"create_role","name":"X"},{"action":"role_add","role":"X","target_ids":[...]}]}. El "role" del 2do paso es EXACTAMENTE el mismo texto que el "name" del 1ro (con sus "de", "la", tildes y emojis; sin "el rol"). NUNCA lo cambies por otro rol existente que se le parezca.
24. Pronombres: "damelo/ponmelo/dame" = el JEFE (target_ids vacio). "daselo/dale/ponle/ponselo" sin nombrar a nadie: si hay REPLY es el autor del reply; si no, usa la persona mas probable del HISTORIAL con confidence baja (Jarvis pedira confirmacion), o clarify si no hay nadie claro.
25. El nombre de un rol/canal/categoria NUEVO es lo que escribio el JEFE, tal cual: no lo traduzcas, no lo censures, no lo "corrijas", no lo acortes. Solo quita palabras de la orden ("llamado", "que se llame", "el rol", "y daselo").
26. Varios roles o canales en una orden: usa "names":[...] (create_role, create_channel, delete_role, delete_channel, role_add, role_remove). "dale los roles A, B y C a @x" = role_add con names:["A","B","C"]. Varios pasos distintos siguen siendo {"actions":[...]}.
27. Borrar: delete_role / delete_channel con el nombre EXACTO de las listas ROLES / CANALES / CATEGORIAS. "borra la categoria X y sus canales" = delete_channel channel:"X" text:"con canales". Si el nombre no esta en las listas, pon el nombre tal cual lo dijo el JEFE (no lo cambies por uno parecido) y confidence baja.
28. edit_role con permissions:[...] AÑADE esos permisos al rol ("dale permiso de enviar mensajes al rol X"). Cambiar nombre: new_name. Cambiar color: color.
29. "quitale/sacale el rol X a @user" = role_remove; "borra/elimina el rol X" (sin persona) = delete_role. "quitame/sacame el rol X" = role_remove sobre el JEFE.
30. Si el JEFE crea algo que YA existe en las listas, igual usa create_*: Jarvis detecta duplicados de roles y los reutiliza.
31. Cuando dudes entre dos roles/canales parecidos, NO elijas: action="clarify" preguntando cual.
"""


def _is_role_action(a):
    return a in ('role_add', 'role_remove', 'edit_role', 'delete_role')


def _prep(data):
    """Normaliza alias que el modelo suele inventar ('roles', 'role_name'...) antes de convertir a Intent."""
    if not isinstance(data, dict):
        return
    items = [data]
    if isinstance(data.get('actions'), list):
        items += [x for x in data['actions'] if isinstance(x, dict)]
    for x in items:
        if not x.get('role') and isinstance(x.get('role_name'), (str, int)):
            x['role'] = x['role_name']
        if not x.get('channel') and isinstance(x.get('channel_name'), (str, int)):
            x['channel'] = x['channel_name']
        if not x.get('name') and isinstance(x.get('new_role'), (str, int)):
            x['name'] = x['new_role']
        for src in ('roles', 'channels'):
            v = x.get(src)
            if isinstance(v, list) and v and not x.get('names'):
                x['names'] = [str(i) for i in v if isinstance(i, (str, int))]


def _fix_steps(it):
    """Coherencia de la cadena: lo que se crea en un paso es lo que se usa en los siguientes."""
    if it is None:
        return it
    steps = [it] + list(getattr(it, 'chain', None) or [])
    created = []
    for s in steps:
        if s.action == 'create_role':
            names = [clean_name(n) for n in (s.names or [])] or [clean_name(s.name or s.role)]
            created = [n for n in names if n]
        elif _is_role_action(s.action) and created:
            if not s.names and (not (s.role or '').strip() or is_generic(s.role)):
                if len(created) == 1:
                    s.role = created[0]
                else:
                    s.names = list(created)
            elif s.role and not s.names:
                cur = _norm(s.role)
                if all(_norm(c) != cur for c in created):
                    best = max(created, key=lambda c: difflib.SequenceMatcher(None, cur, _norm(c)).ratio())
                    if difflib.SequenceMatcher(None, cur, _norm(best)).ratio() >= 0.85:
                        s.role = best
    return it


# ============================================================================
# PLAN B SIN IA: patrones de creacion / borrado
# ============================================================================
_CREATE_ROLE = re.compile(
    r'^\s*(?:crea(?:r|me|le)?|haz(?:me)?|genera|construye)\s+(?:(?:un|una|el|la|nuevo|nueva|otro|otra)\s+)*roles?\s+'
    r'(?P<rest>.+?)\s*$', _I | _S)
_DEL_ROLE = re.compile(
    r'^\s*(?:borra|elimina|destruye|delete|suprime)(?:r|me)?\s+(?:(?:el|los|un|este)\s+)*roles?\s+(?P<rest>.+?)\s*$', _I | _S)
_CREATE_CH = re.compile(
    r'^\s*(?:crea(?:r|me)?|haz(?:me)?|genera|a[n\u00f1]ade|agrega)\s+(?:(?:un|una|el|la|nuevo|nueva|otro|otra)\s+)*'
    r'(?P<what>canal(?:es)?|categor[i\u00ed]as?|foro|vc)\s+'
    r'(?:de\s+(?P<k2>voz|texto|anuncios?|escenario|foro)\s+)?(?P<rest>.+?)\s*$', _I | _S)
_DEL_CH = re.compile(
    r'^\s*(?:borra|elimina|destruye|delete|suprime)(?:r|me)?\s+(?:(?:el|la|los|las|un|una)\s+)*'
    r'(?P<what>canal(?:es)?|categor[i\u00ed]as?)\s+(?:de\s+(?:voz|texto)\s+)?(?P<rest>.+?)\s*$', _I | _S)


def _names_from(rest):
    return [n for n in (clean_name(p) for p in re.split(r'\s*,\s*', (rest or '').strip())) if n][:10]


def _kind_of(word):
    w = _norm(word or '')
    if w.startswith('categoria'):
        return 'category'
    if w.startswith('voz') or w in ('vc', 'voice'):
        return 'voice'
    if w.startswith('anuncio'):
        return 'announcement'
    if w.startswith('escenario'):
        return 'stage'
    if w.startswith('foro'):
        return 'forum'
    return 'text'


def _rx_create_role(ctx, rest):
    give = tail = None
    gm = _GIVE_TAIL.search(rest)
    if gm:
        give, tail = gm.group('give'), (gm.group('tail') or '').strip()
        rest = rest[:gm.start()]
    pre = ''
    nm = _NAMED.search(rest)
    if nm:
        pre, rest = rest[:nm.start()], nm.group('n')
    color = None
    cm = _COLOR.search(pre)
    if cm:
        color = cm.group('c')
    else:
        cm = _COLOR.search(rest)
        if cm:
            color = cm.group('c')
            rest = rest[:cm.start()] + rest[cm.end():]
    names = _names_from(rest)
    if not names:
        return None
    create = M.Intent(action='create_role', source='regex', name=names[0],
                      names=list(names) if len(names) > 1 else [], color=color)
    if give:
        ids, q = [], None
        if tail:
            if re.fullmatch(r'(?:a\s+)?m[i\u00ed](?:\s+mismo)?', tail, _I):
                ids = [str(ctx.author.id)]
            else:
                ids, q, _only = M.parse_target_phrase(tail)
        if _SELF_GIVE.match(give):
            ids, q = [str(ctx.author.id)], None
        create.chain = [M.Intent(
            action='role_add', source='regex', targets=ids, target_query=q,
            role=names[0] if len(names) == 1 else None, names=list(names) if len(names) > 1 else [])]
    return create


def gestion_regex(ctx):
    text = (ctx.text or '').strip()
    text = re.sub(r'\s+(?:por\s+favor|porfa|porfis|plis|please)\s*[.!?]*\s*$', '', text, flags=_I)
    text = text.rstrip('.!? ')
    if not text:
        return None

    m = _CREATE_ROLE.match(text)
    if m:
        return _rx_create_role(ctx, m.group('rest'))

    m = _DEL_ROLE.match(text)
    if m:
        rest = m.group('rest')
        if M.REF_RE.search(rest):           # "borra el rol X de @user" = quitarselo a alguien (otro parser)
            return None
        names = _names_from(rest)
        if not names:
            return None
        if len(names) == 1 and re.search(r'\s(?:a|al|de|del)\s+\S+$', names[0], _I):
            r, tier, _h = lookup_role(list(ctx.guild.roles), names[0])
            if r is None or tier > 0:
                return None
        return M.Intent(action='delete_role', source='regex', role=names[0], names=list(names) if len(names) > 1 else [])

    m = _DEL_CH.match(text)
    if m:
        rest = m.group('rest')
        text_flag = None
        wc = re.search(r'\s+(?:y|con|incluyendo)\s+(?:todos\s+)?(?:sus|los)\s+canales\b.*$', rest, _I)
        if wc:
            rest, text_flag = rest[:wc.start()], 'con canales'
        names = _names_from(rest)
        if not names:
            return None
        return M.Intent(action='delete_channel', source='regex', channel=names[0],
                        names=list(names) if len(names) > 1 else [], text=text_flag)

    m = _CREATE_CH.match(text)
    if m:
        rest = m.group('rest')
        private = bool(re.search(r'\bprivad[oa]\b', rest, _I))
        rest = re.sub(r'\bprivad[oa]\b', '', rest, flags=_I).strip()
        cat = None
        cm = re.search(r'\s+(?:en|dentro\s+de|bajo)\s+(?:la\s+)?categor[i\u00ed]a\s+(?P<c>.+)$', rest, _I)
        if cm:
            cat = clean_name(cm.group('c'))
            rest = rest[:cm.start()]
        nm = _NAMED.search(rest)
        if nm:
            rest = nm.group('n')
        names = _names_from(rest)
        if not names:
            return None
        what = m.group('what')
        kind = 'category' if _norm(what).startswith('categoria') else _kind_of(m.group('k2') or what)
        return M.Intent(action='create_channel', source='regex', kind=kind, name=names[0],
                        names=list(names) if len(names) > 1 else [], category=cat,
                        private=True if private else None)
    return None


def _wrap_regex_parse_intent(orig):
    def regex_parse_intent(ctx):
        try:
            it = gestion_regex(ctx)
        except Exception as e:
            print(f'[Gestion] regex: {e}')
            it = None
        return it if it is not None else orig(ctx)
    regex_parse_intent._gestion_wrapped = True
    return regex_parse_intent


def _wrap_intents_from_ai(orig):
    def intents_from_ai(data):
        try:
            _prep(data)
        except Exception as e:
            print(f'[Gestion] prep: {e}')
        it = orig(data)
        try:
            return _fix_steps(it)
        except Exception as e:
            print(f'[Gestion] fix_steps: {e}')
            return it
    intents_from_ai._gestion_wrapped = True
    return intents_from_ai


# ============================================================================
# INSTALACION
# ============================================================================
def install(mod):
    global M
    M = mod
    if getattr(M, '_gestion_installed', False):
        return
    M._gestion_installed = True

    # busqueda de roles (misma firma que antes)
    M.resolve_role = resolve_role
    M.find_role_or_fail = find_role_or_fail
    M.find_role_flexible = find_role_flexible

    # handlers (el dict de despacho guarda referencias, asi que hay que reemplazar ahi tambien)
    M.act_create_role, M.act_role_add, M.act_role_remove = act_create_role, act_role_add, act_role_remove
    M.act_edit_role, M.act_delete_role, M.act_delete_channel = act_edit_role, act_delete_role, act_delete_channel
    M.PLAIN_HANDLERS['create_role'] = act_create_role
    M.PLAIN_HANDLERS['edit_role'] = act_edit_role
    M.PLAIN_HANDLERS['delete_role'] = act_delete_role
    M.PLAIN_HANDLERS['delete_channel'] = act_delete_channel
    M.TARGET_HANDLERS['role_add'] = act_role_add
    M.TARGET_HANDLERS['role_remove'] = act_role_remove
    M.PLAIN_HANDLERS['create_channel'] = _wrap_create_channel(M.PLAIN_HANDLERS['create_channel'])
    M.PLAIN_HANDLERS['edit_channel'] = _wrap_edit_channel(M.PLAIN_HANDLERS['edit_channel'])
    M.act_create_channel = M.PLAIN_HANDLERS['create_channel']
    M.act_edit_channel = M.PLAIN_HANDLERS['edit_channel']

    # cerebro: reglas extra + correccion de la respuesta del modelo
    if _PROMPT_MARK not in M.INTENT_SYSTEM_PROMPT:
        M.INTENT_SYSTEM_PROMPT = M.INTENT_SYSTEM_PROMPT + '\n' + PROMPT_ADDENDUM
    if not getattr(M.intents_from_ai, '_gestion_wrapped', False):
        M.intents_from_ai = _wrap_intents_from_ai(M.intents_from_ai)

    # plan B sin IA (y atajo para que la charla enlatada no secuestre estas ordenes)
    if not getattr(M.regex_parse_intent, '_gestion_wrapped', False):
        M.regex_parse_intent = _wrap_regex_parse_intent(M.regex_parse_intent)

    print('[Gestion] Parche de gestion (roles/canales/cadenas) instalado.')
