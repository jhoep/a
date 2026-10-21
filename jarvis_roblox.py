
import os
import re
import time
import asyncio
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands

# ----------------------------------------------------------------------------
# Configuracion
# ----------------------------------------------------------------------------
ROBLOX_DOMAIN         = os.environ.get('ROBLOX_API_DOMAIN', 'roblox.com').strip().lower().strip('/') or 'roblox.com'
ROBLOX_WHITELIST_ONLY = os.environ.get('ROBLOX_WHITELIST_ONLY', '0').strip().lower() in ('1', 'true', 'si', 'yes', 'on')
ROBLOX_COOLDOWN       = 8            # segundos entre consultas por persona (el owner no tiene)
ROBLOX_COLOR          = 0xE2231A
HTTP_TIMEOUT          = 20
VIEW_TIMEOUT          = 300
MAX_OUTFITS           = 25           # maximo que cabe en el menu de Discord
HELP_FIELD_NAME  = 'Roblox'
HELP_FIELD_VALUE = ('`/roblox usuario:<nombre, ID o link>` - perfil, avatar, outfits, creaciones, grupos y limiteds\n'
                    '`jarvis roblox <usuario>`')

_NEEDED = ('client', 'JARVIS_WHITELIST', 'OWNER_ID')

ASSET_TYPES = {
    2: 'Camiseta (T-Shirt)', 8: 'Sombrero', 11: 'Camisa', 12: 'Pantalon', 17: 'Cabeza', 18: 'Cara', 19: 'Gear',
    27: 'Torso', 28: 'Brazo der.', 29: 'Brazo izq.', 30: 'Pierna izq.', 31: 'Pierna der.', 32: 'Paquete',
    41: 'Accesorio de pelo', 42: 'Accesorio de cara', 43: 'Accesorio de cuello', 44: 'Accesorio de hombro',
    45: 'Accesorio frontal', 46: 'Accesorio de espalda', 47: 'Accesorio de cintura', 48: 'Animacion de climb',
    49: 'Animacion de muerte', 50: 'Animacion de caida', 51: 'Animacion idle', 52: 'Animacion de salto',
    53: 'Animacion de correr', 54: 'Animacion de nadar', 55: 'Animacion de caminar', 56: 'Animacion de pose',
    61: 'Emote', 64: 'Ropa (T-Shirt)', 65: 'Ropa (Camisa)', 66: 'Ropa (Pantalon)', 67: 'Ropa (Chaqueta)',
    68: 'Ropa (Sueter)', 69: 'Ropa (Shorts)', 70: 'Zapato izq.', 71: 'Zapato der.', 72: 'Vestido/Falda',
    76: 'Ceja', 77: 'Pestana', 78: 'Animacion de estado', 79: 'Cabeza dinamica',
}


class RobloxError(Exception):
    """Error que se le muestra tal cual al usuario."""


class _Private(RobloxError):
    """401/403: dato privado o consulta bloqueada."""


class _Missing(RobloxError):
    """400/404: no existe."""


_cooldown = {}
_installed = False


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def _url(sub, path):
    return f'https://{sub}.{ROBLOX_DOMAIN}{path}'


def _session():
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
        headers={'User-Agent': 'JarvisDiscordBot/1.0', 'Accept': 'application/json'},
    )


async def _req(session, method, url, **kw):
    try:
        async with session.request(method, url, **kw) as r:
            if r.status == 429:
                raise RobloxError('Roblox esta limitando las consultas. Prueba en unos segundos.')
            if r.status in (401, 403):
                raise _Private('Privado o bloqueado (HTTP %d).' % r.status)
            if r.status in (400, 404):
                raise _Missing('No existe (HTTP %d).' % r.status)
            if r.status != 200:
                raise RobloxError(f'Roblox respondio con error {r.status}.')
            return await r.json(content_type=None)
    except asyncio.TimeoutError:
        raise RobloxError('Roblox tardo demasiado en responder.')
    except aiohttp.ClientError as e:
        raise RobloxError(f'No pude contactar a Roblox: {str(e)[:150]}')


async def _thumb(session, kind, uid, size='420x420'):
    """kind: avatar | avatar-headshot | avatar-bust. Devuelve la URL de la imagen o None."""
    for attempt in range(3):
        js = await _req(session, 'GET', _url('thumbnails', f'/v1/users/{kind}'),
                        params={'userIds': uid, 'size': size, 'format': 'Png', 'isCircular': 'false'})
        item = (js.get('data') or [{}])[0]
        state = item.get('state')
        if state == 'Completed' and item.get('imageUrl'):
            return item['imageUrl']
        if state in ('Blocked', 'Error', 'NotApproved'):
            return None
        await asyncio.sleep(1.2)          # Pending: la imagen se esta generando
    return None


async def _outfit_thumbs(session, ids, size='420x420'):
    if not ids:
        return {}
    js = await _req(session, 'GET', _url('thumbnails', '/v1/users/outfits'),
                    params={'userOutfitIds': ','.join(str(i) for i in ids), 'size': size,
                            'format': 'Png', 'isCircular': 'false'})
    return {int(d['targetId']): d['imageUrl'] for d in (js.get('data') or [])
            if d.get('state') == 'Completed' and d.get('imageUrl')}


# ----------------------------------------------------------------------------
# Utilidades de formato
# ----------------------------------------------------------------------------
def _n(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return '?'
    for div, suf in ((1e9, 'B'), (1e6, 'M'), (1e3, 'K')):
        if v >= div:
            return f'{v / div:.1f}{suf}'.replace('.0', '')
    return str(int(v))


def _parse_dt(s):
    try:
        s = re.sub(r'\.\d+', '', str(s)).replace('Z', '+00:00')
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _clean(text, limit):
    t = discord.utils.escape_markdown(discord.utils.escape_mentions(str(text or '')))
    return t if len(t) <= limit else t[:limit - 1] + '\u2026'


def _profile_link(uid):
    return f'https://www.roblox.com/users/{uid}/profile'


def _fit_lines(lines, limit=3800, more_label='mas'):
    out, used = [], 0
    for i, ln in enumerate(lines):
        if used + len(ln) + 1 > limit:
            out.append(f'\u2026y {len(lines) - i} {more_label}')
            break
        out.append(ln)
        used += len(ln) + 1
    return '\n'.join(out)


def _title(data, suffix=None):
    u = data.user
    base = f'{u.get("displayName") or u.get("name")} (@{u.get("name")})'
    return (f'{base} - {suffix}' if suffix else base)[:256]


def _base(data, suffix=None, description=None):
    e = discord.Embed(title=_title(data, suffix), url=_profile_link(data.uid), description=description,
                      color=ROBLOX_COLOR)
    if data.headshot:
        e.set_thumbnail(url=data.headshot)
    return e


def _error_embed(data, section, msg):
    return _base(data, section, msg)


# ----------------------------------------------------------------------------
# Datos del usuario
# ----------------------------------------------------------------------------
class RbxData:
    def __init__(self, uid, user):
        self.uid = uid
        self.user = user
        self.headshot = None
        self.body = None
        self.friends = self.followers = self.followings = None
        self.old_names = []
        self.groups_count = None
        self.outfits = []            # [{id, name}]


async def resolve_user(session, text):
    """Acepta nombre, ID o link del perfil. Devuelve el dict del usuario (users.roblox.com/v1/users/{id})."""
    raw = (text or '').strip().strip('<>')
    m = re.search(r'roblox\.com/(?:[a-z\-]+/)?users/(\d+)', raw, re.IGNORECASE)
    uid = int(m.group(1)) if m else None
    name = None
    if uid is None:
        raw = raw.lstrip('@').strip()
        if re.fullmatch(r'\d{1,16}', raw):
            uid = int(raw)
            name = raw          # por si resulta ser un nombre formado solo por numeros
        elif re.fullmatch(r'[A-Za-z0-9_]{3,20}', raw):
            name = raw
        else:
            raise RobloxError('Ese nombre no es valido. Usa el nombre de usuario (3-20 caracteres: letras, numeros y _), '
                              'el ID numerico o el link del perfil.')

    async def by_id(i):
        return await _req(session, 'GET', _url('users', f'/v1/users/{i}'))

    async def by_name(n):
        js = await _req(session, 'POST', _url('users', '/v1/usernames/users'),
                        json={'usernames': [n], 'excludeBannedUsers': False})
        rows = js.get('data') or []
        if not rows:
            raise _Missing('No existe.')
        return await by_id(rows[0]['id'])

    try:
        if uid is not None:
            try:
                return await by_id(uid)
            except _Missing:
                if name and not m:
                    return await by_name(name)
                raise
        return await by_name(name)
    except _Missing:
        raise RobloxError('No encontre ese usuario de Roblox. Revisa el nombre o el ID.')
    except _Private:
        raise RobloxError('Roblox bloqueo la consulta desde el servidor del bot (HTTP 403). Si pasa seguido, '
                          'el dueno del bot puede probar con la variable `ROBLOX_API_DOMAIN=roproxy.com`.')


async def load_profile(session, user):
    uid = int(user['id'])
    data = RbxData(uid, user)
    results = await asyncio.gather(
        _thumb(session, 'avatar-headshot', uid, '150x150'),
        _thumb(session, 'avatar', uid, '420x420'),
        _req(session, 'GET', _url('friends', f'/v1/users/{uid}/friends/count')),
        _req(session, 'GET', _url('friends', f'/v1/users/{uid}/followers/count')),
        _req(session, 'GET', _url('friends', f'/v1/users/{uid}/followings/count')),
        _req(session, 'GET', _url('users', f'/v1/users/{uid}/username-history'),
             params={'limit': 10, 'sortOrder': 'Desc'}),
        _req(session, 'GET', _url('groups', f'/v2/users/{uid}/groups/roles')),
        return_exceptions=True,
    )
    ok = lambda r: not isinstance(r, Exception)
    data.headshot = results[0] if ok(results[0]) else None
    data.body = results[1] if ok(results[1]) else None
    data.friends = results[2].get('count') if ok(results[2]) else None
    data.followers = results[3].get('count') if ok(results[3]) else None
    data.followings = results[4].get('count') if ok(results[4]) else None
    if ok(results[5]):
        data.old_names = [x.get('name') for x in (results[5].get('data') or []) if x.get('name')]
    if ok(results[6]):
        data.groups_count = len(results[6].get('data') or [])
    return data


# ----------------------------------------------------------------------------
# Paginas
# ----------------------------------------------------------------------------
async def page_profile(data):
    u = data.user
    desc = (u.get('description') or '').strip()
    e = _base(data, None, _clean(desc, 450) if desc else '*Sin descripcion.*')
    if data.body:
        e.set_image(url=data.body)
    created = _parse_dt(u.get('created'))
    if created:
        years = (datetime.now(timezone.utc) - created).days / 365.25
        e.add_field(name='Cuenta creada',
                    value=f'<t:{int(created.timestamp())}:D>\n(<t:{int(created.timestamp())}:R>, {years:.1f} anos)',
                    inline=True)
    e.add_field(name='ID', value=f'`{data.uid}`', inline=True)
    state = 'Baneado' if u.get('isBanned') else 'Activo'
    if u.get('hasVerifiedBadge'):
        state += ' | Verificado'
    e.add_field(name='Estado', value=state, inline=True)
    e.add_field(name='Amigos', value=_n(data.friends) if data.friends is not None else '?', inline=True)
    e.add_field(name='Seguidores', value=_n(data.followers) if data.followers is not None else '?', inline=True)
    e.add_field(name='Siguiendo', value=_n(data.followings) if data.followings is not None else '?', inline=True)
    if u.get('displayName') and u['displayName'] != u.get('name'):
        e.add_field(name='Nombre visible', value=_clean(u['displayName'], 60), inline=True)
    if data.groups_count is not None:
        e.add_field(name='Grupos', value=str(data.groups_count), inline=True)
    if data.old_names:
        e.add_field(name='Nombres anteriores', value=_clean(', '.join(data.old_names[:6]), 300), inline=False)
    e.set_footer(text='Usa los botones para ver avatar, outfits, creaciones, grupos y limiteds')
    return [e]


async def page_avatar(data, session):
    uid = data.uid
    info, img = await asyncio.gather(
        _req(session, 'GET', _url('avatar', f'/v1/users/{uid}/avatar')),
        _thumb(session, 'avatar', uid, '720x720'),
        return_exceptions=True,
    )
    if isinstance(info, Exception):
        if isinstance(info, _Private):
            raise RobloxError('No pude ver su avatar (consulta bloqueada).')
        raise info
    e = _base(data, 'Avatar')
    pic = img if not isinstance(img, Exception) and img else data.body
    if pic:
        e.set_image(url=pic)
    sc = info.get('scales') or {}
    e.add_field(name='Tipo', value=info.get('playerAvatarType') or '?', inline=True)
    if sc:
        e.add_field(name='Medidas',
                    value=f'Altura {float(sc.get("height", 1)) * 100:.0f}% | Ancho {float(sc.get("width", 1)) * 100:.0f}%',
                    inline=True)
    assets = info.get('assets') or []
    e.add_field(name='Items puestos', value=str(len(assets)), inline=True)
    lines = []
    for a in assets:
        atype = ((a.get('assetType') or {}).get('id'))
        tname = ASSET_TYPES.get(atype) or (a.get('assetType') or {}).get('name') or 'Item'
        lines.append(f'[{_clean(a.get("name") or a.get("id"), 60)}](https://www.roblox.com/catalog/{a.get("id")}) - {tname}')
    e.description = _fit_lines(lines, 3800, 'items') if lines else '*No lleva nada puesto.*'
    return [e]


async def _fetch_outfits(session, uid):
    params = {'page': 1, 'itemsPerPage': MAX_OUTFITS}
    try:
        js = await _req(session, 'GET', _url('avatar', f'/v2/avatar/users/{uid}/outfits'), params=params)
    except _Private:
        raise
    except RobloxError:
        js = await _req(session, 'GET', _url('avatar', f'/v1/users/{uid}/outfits'),
                        params={**params, 'isEditable': 'true'})        # endpoint antiguo como plan B
    rows = js.get('data') or []
    return [{'id': int(o['id']), 'name': o.get('name') or f'Outfit {o["id"]}'} for o in rows if o.get('id')][:MAX_OUTFITS]


async def page_outfits(data, session):
    try:
        data.outfits = await _fetch_outfits(session, data.uid)
    except _Private:
        raise RobloxError('Sus outfits no son visibles (privados o bloqueados por Roblox).')
    if not data.outfits:
        return [_base(data, 'Outfits', 'No tiene outfits guardados visibles.')]
    lines = [f'`{i}.` {_clean(o["name"], 60)}' for i, o in enumerate(data.outfits, 1)]
    head = _base(data, 'Outfits',
                 f'**{len(data.outfits)}** outfit(s) guardado(s).\n\n' + _fit_lines(lines, 3300, 'outfits')
                 + '\n\nElige uno en el menu de abajo para verlo con sus items.')
    embeds = [head]
    try:
        thumbs = await _outfit_thumbs(session, [o['id'] for o in data.outfits[:4]])
    except RobloxError:
        thumbs = {}
    # varios embeds con el MISMO url se muestran como una galeria de imagenes
    first = True
    for o in data.outfits[:4]:
        img = thumbs.get(o['id'])
        if not img:
            continue
        if first:
            head.set_image(url=img)
            first = False
        else:
            ge = discord.Embed(url=_profile_link(data.uid), color=ROBLOX_COLOR)
            ge.set_image(url=img)
            embeds.append(ge)
    return embeds


async def page_outfit_detail(data, session, outfit_id):
    name = next((o['name'] for o in data.outfits if o['id'] == outfit_id), f'Outfit {outfit_id}')
    det, thumbs = await asyncio.gather(
        _req(session, 'GET', _url('avatar', f'/v1/outfits/{outfit_id}/details')),
        _outfit_thumbs(session, [outfit_id]),
        return_exceptions=True,
    )
    if isinstance(det, Exception):
        raise RobloxError('No pude abrir ese outfit.')
    e = _base(data, f'Outfit: {name}'[:200])
    img = None if isinstance(thumbs, Exception) else thumbs.get(outfit_id)
    if img:
        e.set_image(url=img)
    assets = det.get('assets') or []
    lines = []
    for a in assets:
        tname = ASSET_TYPES.get((a.get('assetType') or {}).get('id')) or (a.get('assetType') or {}).get('name') or 'Item'
        lines.append(f'[{_clean(a.get("name") or a.get("id"), 60)}](https://www.roblox.com/catalog/{a.get("id")}) - {tname}')
    e.description = _fit_lines(lines, 3800, 'items') if lines else '*Este outfit no tiene items visibles.*'
    e.add_field(name='Tipo', value=det.get('playerAvatarType') or '?', inline=True)
    e.add_field(name='Items', value=str(len(assets)), inline=True)
    return [e]


async def page_creations(data, session):
    uid = data.uid
    games_r, catalog_r = await asyncio.gather(
        _req(session, 'GET', _url('games', f'/v2/users/{uid}/games'),
             params={'accessFilter': 'Public', 'limit': 50, 'sortOrder': 'Desc'}),
        _req(session, 'GET', _url('catalog', '/v1/search/items/details'),
             params={'Category': 1, 'CreatorType': 'User', 'CreatorTargetId': uid, 'Limit': 30}),
        return_exceptions=True,
    )
    e = _base(data, 'Creaciones')
    parts = []

    # ---- experiencias (juegos)
    if isinstance(games_r, Exception):
        parts.append('**Experiencias:** no pude consultarlas ahora mismo.')
    else:
        games = games_r.get('data') or []
        if not games:
            parts.append('**Experiencias:** no ha publicado ninguna experiencia publica.')
        else:
            ids = [g['id'] for g in games if g.get('id')][:50]
            det_r, votes_r = await asyncio.gather(
                _req(session, 'GET', _url('games', '/v1/games'), params={'universeIds': ','.join(map(str, ids))}),
                _req(session, 'GET', _url('games', '/v1/games/votes'), params={'universeIds': ','.join(map(str, ids))}),
                return_exceptions=True,
            )
            det = {} if isinstance(det_r, Exception) else {d['id']: d for d in (det_r.get('data') or [])}
            votes = {} if isinstance(votes_r, Exception) else {v['id']: v for v in (votes_r.get('data') or [])}
            rows = []
            for g in games:
                d = det.get(g.get('id'), {})
                visits = d.get('visits', g.get('placeVisits', 0)) or 0
                place_id = d.get('rootPlaceId') or (g.get('rootPlace') or {}).get('id')
                v = votes.get(g.get('id'), {})
                up, down = v.get('upVotes'), v.get('downVotes')
                like = f'{up / (up + down) * 100:.0f}% likes' if up is not None and (up + down) > 0 else None
                rows.append((visits, g.get('name') or 'Sin nombre', place_id, d.get('playing'), like))
            rows.sort(key=lambda r: -r[0])
            total = sum(r[0] for r in rows)
            lines = []
            for visits, name, place_id, playing, like in rows[:8]:
                label = f'[{_clean(name, 50)}](https://www.roblox.com/games/{place_id})' if place_id else _clean(name, 50)
                bits = [f'{_n(visits)} visitas']
                if playing:
                    bits.append(f'{_n(playing)} jugando')
                if like:
                    bits.append(like)
                lines.append(f'\u2022 **{label}** - ' + ' | '.join(bits))
            more = f'\n\u2026y {len(rows) - 8} mas' if len(rows) > 8 else ''
            parts.append(f'**Experiencias ({len(rows)})** - {_n(total)} visitas en total\n' + '\n'.join(lines) + more)

    # ---- items que vende en el catalogo (si la consulta esta disponible)
    if not isinstance(catalog_r, Exception):
        items = catalog_r.get('data') or []
        if items:
            lines = []
            for it in items[:8]:
                iid = it.get('id')
                price = it.get('price')
                ptxt = f'R$ {_n(price)}' if isinstance(price, (int, float)) and price > 0 else (
                    'Gratis' if price == 0 else 'No a la venta')
                lines.append(f'\u2022 [{_clean(it.get("name") or iid, 50)}](https://www.roblox.com/catalog/{iid}) - {ptxt}')
            more = f'\n\u2026y {len(items) - 8} mas' if len(items) > 8 else ''
            parts.append(f'**Items del catalogo ({len(items)})**\n' + '\n'.join(lines) + more)

    e.description = '\n\n'.join(parts)[:4000] if parts else 'No encontre creaciones publicas.'
    return [e]


async def page_groups(data, session):
    try:
        js = await _req(session, 'GET', _url('groups', f'/v2/users/{data.uid}/groups/roles'))
    except _Private:
        raise RobloxError('Sus grupos no son visibles.')
    rows = js.get('data') or []
    if not rows:
        return [_base(data, 'Grupos', 'No esta en ningun grupo (o no son visibles).')]
    rows.sort(key=lambda r: -((r.get('role') or {}).get('rank') or 0))
    lines = []
    for r in rows:
        g, role = r.get('group') or {}, r.get('role') or {}
        verified = ' (verificado)' if g.get('hasVerifiedBadge') else ''
        lines.append(f'\u2022 **[{_clean(g.get("name"), 50)}](https://www.roblox.com/groups/{g.get("id")}){verified}** - '
                     f'{_clean(role.get("name"), 30)} | {_n(g.get("memberCount"))} miembros')
    e = _base(data, 'Grupos', f'**{len(rows)}** grupo(s), del rango mas alto al mas bajo:\n\n' + _fit_lines(lines, 3600, 'grupos'))
    return [e]


async def page_limiteds(data, session):
    try:
        js = await _req(session, 'GET', _url('inventory', f'/v1/users/{data.uid}/assets/collectibles'),
                        params={'sortOrder': 'Desc', 'limit': 100})
    except _Private:
        return [_base(data, 'Limiteds', 'Su inventario es **privado**: Roblox no deja ver sus coleccionables.')]
    items = js.get('data') or []
    if not items:
        return [_base(data, 'Limiteds', 'No tiene Limiteds (o no se pudieron ver).')]
    more = bool(js.get('nextPageCursor'))
    rap_total = sum((i.get('recentAveragePrice') or 0) for i in items)
    items.sort(key=lambda i: -(i.get('recentAveragePrice') or 0))
    lines = []
    for i in items[:8]:
        serial = f' #{i["serialNumber"]}' if i.get('serialNumber') else ''
        lines.append(f'\u2022 [{_clean(i.get("name") or i.get("assetId"), 50)}](https://www.roblox.com/catalog/{i.get("assetId")})'
                     f'{serial} - RAP R$ {_n(i.get("recentAveragePrice") or 0)}')
    count = f'{len(items)}{"+" if more else ""}'
    e = _base(data, 'Limiteds',
              f'**{count}** Limited(s) | RAP aprox. **R$ {_n(rap_total)}**'
              + (' (solo de los primeros 100)' if more else '')
              + '\n\n**Los mas valiosos:**\n' + '\n'.join(lines))
    return [e]


# ----------------------------------------------------------------------------
# Vista con botones
# ----------------------------------------------------------------------------
PAGES = (('perfil', 'Perfil'), ('avatar', 'Avatar'), ('outfits', 'Outfits'),
         ('creaciones', 'Creaciones'), ('grupos', 'Grupos'), ('limiteds', 'Limiteds'))


class OutfitSelect(discord.ui.Select):
    def __init__(self, outfits, selected=None):
        options = [discord.SelectOption(label=(o['name'] or f'Outfit {o["id"]}')[:100], value=str(o['id']),
                                        default=(o['id'] == selected)) for o in outfits[:MAX_OUTFITS]]
        super().__init__(placeholder='Elige un outfit para verlo...', min_values=1, max_values=1,
                         options=options, row=2)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.view._show(interaction, 'outfits', int(self.values[0]))


class RobloxView(discord.ui.View):
    def __init__(self, data, requester_id, page='perfil'):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.data = data
        self.requester_id = requester_id
        self.page = page
        self.outfit_id = None
        self.message = None
        self.cache = {}
        self._buttons = {}
        for i, (key, label) in enumerate(PAGES):
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, row=i // 3)
            btn.callback = self._make_cb(key)
            self.add_item(btn)
            self._buttons[key] = btn
        self._select = None
        self._sync()

    def _make_cb(self, key):
        async def cb(interaction: discord.Interaction):
            await interaction.response.defer()
            await self._show(interaction, key)
        return cb

    def _sync(self):
        for key, btn in self._buttons.items():
            btn.style = discord.ButtonStyle.primary if key == self.page else discord.ButtonStyle.secondary
        if self._select is not None:
            self.remove_item(self._select)
            self._select = None
        if self.page == 'outfits' and self.data.outfits:
            self._select = OutfitSelect(self.data.outfits, self.outfit_id)
            self.add_item(self._select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message('Solo quien uso el comando puede cambiar de seccion. '
                                                'Usa `/roblox` para hacer tu propia consulta.', ephemeral=True)
        return False

    async def embeds_for(self, key, outfit_id=None):
        ck = (key, outfit_id)
        if ck in self.cache:
            return self.cache[ck]
        d = self.data
        try:
            if key == 'perfil':
                embeds = await page_profile(d)
            else:
                async with _session() as s:
                    if key == 'avatar':
                        embeds = await page_avatar(d, s)
                    elif key == 'outfits':
                        if outfit_id is not None:
                            if not d.outfits:
                                d.outfits = await _fetch_outfits(s, d.uid)
                            embeds = await page_outfit_detail(d, s, outfit_id)
                        else:
                            embeds = await page_outfits(d, s)
                    elif key == 'creaciones':
                        embeds = await page_creations(d, s)
                    elif key == 'grupos':
                        embeds = await page_groups(d, s)
                    else:
                        embeds = await page_limiteds(d, s)
        except RobloxError as e:
            return [_error_embed(d, dict(PAGES).get(key, ''), str(e))]       # los errores no se guardan en cache
        except Exception as e:
            print(f'[Roblox] pagina {key}: {e}')
            return [_error_embed(d, dict(PAGES).get(key, ''), 'Algo fallo al consultar Roblox. Intenta de nuevo.')]
        self.cache[ck] = embeds
        return embeds

    async def _show(self, interaction, key, outfit_id=None):
        embeds = await self.embeds_for(key, outfit_id)
        self.page = key
        self.outfit_id = outfit_id
        self._sync()
        try:
            await interaction.edit_original_response(embeds=embeds, view=self)
        except Exception as e:
            print(f'[Roblox] No pude editar el mensaje: {e}')

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


# ----------------------------------------------------------------------------
# Punto de entrada comun (slash y "jarvis roblox ...")
# ----------------------------------------------------------------------------
def _check_cooldown(user_id):
    if str(user_id) == str(OWNER_ID):
        return
    now = time.time()
    left = ROBLOX_COOLDOWN - (now - _cooldown.get(user_id, 0))
    if left > 0:
        raise RobloxError(f'Espera {left:.0f}s antes de hacer otra consulta de Roblox.')
    _cooldown[user_id] = now


def _allowed(user_id):
    return (not ROBLOX_WHITELIST_ONLY) or str(user_id) in JARVIS_WHITELIST or str(user_id) == str(OWNER_ID)


async def build_lookup(query, requester_id, page='perfil'):
    """Devuelve (embeds, view) para mostrar."""
    async with _session() as s:
        user = await resolve_user(s, query)
        data = await load_profile(s, user)
    view = RobloxView(data, requester_id, page)
    embeds = await view.embeds_for(page)
    return embeds, view


# ----------------------------------------------------------------------------
# Slash command
# ----------------------------------------------------------------------------
def _register_slash_commands():
    @client.tree.command(name='roblox', description='Busca un usuario de Roblox: perfil, avatar, outfits, creaciones y mas')
    @app_commands.describe(usuario='Nombre de usuario, ID o link del perfil de Roblox',
                           seccion='Seccion con la que abrir (por defecto el perfil)')
    @app_commands.choices(seccion=[app_commands.Choice(name=label, value=key) for key, label in PAGES])
    async def slash_roblox(interaction: discord.Interaction, usuario: str,
                           seccion: app_commands.Choice[str] = None):
        if not _allowed(interaction.user.id):
            return await interaction.response.send_message(
                'Solo la whitelist de Jarvis puede usar /roblox en este servidor.', ephemeral=True)
        try:
            _check_cooldown(interaction.user.id)
        except RobloxError as e:
            return await interaction.response.send_message(str(e), ephemeral=True)
        await interaction.response.defer()
        try:
            embeds, view = await build_lookup(usuario, interaction.user.id, seccion.value if seccion else 'perfil')
        except RobloxError as e:
            return await interaction.followup.send(str(e))
        except Exception as e:
            print(f'[Roblox] /roblox: {e}')
            return await interaction.followup.send('Algo fallo al consultar Roblox. Intenta de nuevo en un momento.')
        view.message = await interaction.followup.send(embeds=embeds, view=view, wait=True)


# ----------------------------------------------------------------------------
# Atajo "jarvis roblox <usuario>"
# ----------------------------------------------------------------------------
NL_ROBLOX = (
    re.compile(r'^roblox\s+(?:de\s+|del\s+|user\s+|usuario\s+)?(?P<q>\S+)\s*$', re.IGNORECASE),
    re.compile(r'^(?:busca|buscame|mu[eé]strame|muestrame|mira|dame|info|perfil)\s+(?:de\s+|del\s+|el\s+)*'
               r'(?:perfil\s+|usuario\s+|cuenta\s+)?(?:de\s+)?roblox\s+(?:de\s+|del\s+|a\s+|al\s+)?(?P<q>\S+)\s*$',
               re.IGNORECASE),
)
NL_STOPWORDS = {'es', 'esta', 'hola', 'juego', 'juegos', 'game', 'games', 'ayuda', 'help', 'bien', 'mal', 'si', 'no'}


async def handle_roblox_natural(ctx):
    text = ctx.text.strip()
    for pat in NL_ROBLOX:
        m = pat.match(text)
        if m and m.group('q').lower() not in NL_STOPWORDS:
            query = m.group('q')
            break
    else:
        return False
    try:
        _check_cooldown(ctx.author.id)
        async with ctx.message.channel.typing():
            embeds, view = await build_lookup(query, ctx.author.id)
    except RobloxError as e:
        await ctx.message.reply(str(e))
        return True
    except Exception as e:
        print(f'[Roblox] jarvis roblox: {e}')
        await ctx.message.reply('Algo fallo al consultar Roblox. Intenta de nuevo en un momento.')
        return True
    try:
        view.message = await ctx.message.reply(embeds=embeds, view=view)
    except discord.HTTPException:
        view.message = await ctx.message.channel.send(embeds=embeds, view=view)
    return True


# ----------------------------------------------------------------------------
# Instalacion
# ----------------------------------------------------------------------------
def install(main):
    """Conecta este modulo al archivo principal del bot. `main` = sys.modules[__name__] del bot."""
    global _installed
    if _installed:
        return
    _installed = True

    missing = [n for n in _NEEDED if not hasattr(main, n)]
    if missing:
        raise RuntimeError('jarvis_roblox: faltan estos nombres en el bot principal: ' + ', '.join(missing)
                           + '. Llama a install() DESPUES de definirlos (antes de la seccion KEEPALIVE).')
    g = globals()
    for n in _NEEDED:
        g[n] = getattr(main, n)

    if hasattr(main, 'jarvis_brain'):
        original_brain = main.jarvis_brain

        async def jarvis_brain_with_roblox(ctx):
            if await handle_roblox_natural(ctx):
                return True
            return await original_brain(ctx)

        main.jarvis_brain = jarvis_brain_with_roblox

    if hasattr(main, 'build_help_embed'):
        original_help = main.build_help_embed

        def build_help_embed_with_roblox():
            embed = original_help()
            embed.add_field(name=HELP_FIELD_NAME, value=HELP_FIELD_VALUE, inline=False)
            return embed

        main.build_help_embed = build_help_embed_with_roblox

    _register_slash_commands()
    print(f'[Roblox] Listo | dominio={ROBLOX_DOMAIN} | acceso={"solo whitelist" if ROBLOX_WHITELIST_ONLY else "todos (con cooldown)"}')
