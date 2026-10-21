# -*- coding: utf-8 -*-
"""
jarvis_suplantar.py  -  /suplantar: manda un mensaje con el nombre y el avatar de otro miembro (broma entre amigos).

INSTALACION (2 lineas en tu archivo principal, junto a las de los otros modulos, antes de "# KEEPALIVE"):

    import jarvis_suplantar
    jarvis_suplantar.install(sys.modules[__name__])

USO
    /suplantar usuario:@pepe mensaje:hola a todos            -> el mensaje sale con el nombre y el avatar de pepe
    /suplantar usuario:@pepe mensaje:... canal:#general con_banner:True
    /nosuplantar activar:True                                 -> cualquier persona puede pedir que NO la suplanten
    /suplantarshow [cantidad] [usuario] [suplantado]         -> historial de usos (SOLO el owner del bot)

COMO FUNCIONA
  Usa un webhook del canal (el bot lo crea solo; necesita el permiso "Gestionar webhooks"). Un webhook solo puede cambiar
  el NOMBRE y el AVATAR de quien envia: usa el apodo del servidor y el avatar del servidor de la persona.
  El BANNER no se puede cambiar en un mensaje (Discord no lo permite), asi que con `con_banner:True` se agrega debajo
  como una imagen aparte (solo si la persona tiene banner).
  Discord siempre marca estos mensajes con la etiqueta APP.

PROTECCIONES (para que sea una broma y no una forma de meter en problemas a alguien)
  - Solo la whitelist de Jarvis puede usarlo.
  - Nadie puede suplantar a: bots ni a quien haya activado /nosuplantar (eso se respeta siempre, tambien con el owner).
  - Las demas personas de la whitelist tampoco pueden suplantar al dueno del servidor, a moderadores/administradores
    ni al owner del bot. El OWNER DEL BOT si puede suplantar a cualquiera de ellos.
  - Solo puedes usarlo en canales donde TU podrias escribir.
  - No hace ping a @everyone, @here ni roles.
  - Opcional: IMPERSONATE_FOOTER=1 agrega una linea pequena al final: "enviado por <quien> con Jarvis" (apagado por defecto).
  - Todo queda registrado: archivo data/impersonate_log.json, el canal de mod log (si lo tienes) y un DM al owner del bot
    cuando lo usa otra persona.
  - Cooldown de 10 segundos por persona.

Variables de entorno (opcionales):
  IMPERSONATE_FOOTER   1 = agrega la linea "enviado por ... con Jarvis", 0 = no (defecto)
"""
import os
import time
from datetime import datetime, timezone

import discord
from discord import app_commands

# ----------------------------------------------------------------------------
# Configuracion
# ----------------------------------------------------------------------------
FOOTER_ON       = os.environ.get('IMPERSONATE_FOOTER', '0').strip().lower() in ('1', 'true', 'si', 'yes', 'on')
COOLDOWN_SECS   = 10
MAX_TEXT        = 1800
LOG_MAX         = 200
WEBHOOK_NAME    = 'Jarvis'
DEFAULT_MOD_PERMS = ('administrator', 'moderate_members', 'ban_members', 'kick_members', 'manage_messages',
                     'manage_channels', 'manage_guild', 'manage_roles', 'mute_members', 'deafen_members', 'move_members')
HELP_FIELD_NAME = 'Suplantar (broma)'
HELP_FIELD_VALUE = ('`/suplantar usuario mensaje [canal] [con_banner]` - envia un mensaje con el nombre y avatar de otro miembro '
                    '(queda registrado; a staff solo lo puede suplantar el owner del bot)\n'
                    '`/nosuplantar` - pide que a ti no te suplanten\n'
                    '`/suplantarshow` - historial de usos (solo owner del bot)')

_NEEDED = ('client', 'JARVIS_WHITELIST', 'OWNER_ID', 'load_json', 'save_json', 'DATA_DIR', 'short_error')

SuplantarError = None
_hooks = {}                  # channel_id -> discord.Webhook
_cooldown = {}
_optout = set()
_log = []
_optout_file = None
_log_file = None
_installed = False


class _Refused(Exception):
    """Mensaje que se le muestra tal cual a quien uso el comando."""


# ----------------------------------------------------------------------------
# Reglas
# ----------------------------------------------------------------------------
def protected_reason(guild, target, requester):
    """Devuelve el motivo por el que NO se puede suplantar a `target`, o None si se puede."""
    if target.bot:
        return 'es un bot'
    if target.id == requester.id:
        return None
    if str(target.id) in _optout:                       # quien pidio no ser suplantado se respeta SIEMPRE
        return 'pidio que no lo suplanten'
    if str(requester.id) == str(OWNER_ID):              # el owner del bot puede suplantar a cualquiera (staff incluido)
        return None
    if target.id == guild.owner_id:
        return 'es el dueno del servidor'
    if str(target.id) == str(OWNER_ID):
        return 'es el owner del bot'
    perms = target.guild_permissions
    for p in MOD_PERMS:
        if getattr(perms, p, False):
            return 'es parte del staff (moderador o administrador)'
    return None


def _check_cooldown(user_id):
    if str(user_id) == str(OWNER_ID):
        return
    now = time.time()
    left = COOLDOWN_SECS - (now - _cooldown.get(user_id, 0))
    if left > 0:
        raise _Refused(f'Espera {left:.0f}s antes de volver a usarlo.')
    _cooldown[user_id] = now


def _save_optout():
    save_json(_optout_file, sorted(_optout))


# ----------------------------------------------------------------------------
# Webhook
# ----------------------------------------------------------------------------
async def _get_webhook(parent):
    hook = _hooks.get(parent.id)
    if hook is not None:
        return hook
    try:
        for wh in await parent.webhooks():
            if wh.user is not None and wh.user.id == client.user.id and wh.token:
                _hooks[parent.id] = wh
                return wh
        wh = await parent.create_webhook(name=WEBHOOK_NAME, reason='[Jarvis] Webhook para /suplantar')
    except discord.Forbidden:
        raise _Refused('Me falta el permiso **Gestionar webhooks** en ese canal.')
    _hooks[parent.id] = wh
    return wh


def _build_content(text, requester):
    text = text.strip()
    if FOOTER_ON:
        who = discord.utils.escape_markdown(discord.utils.escape_mentions(requester.display_name))[:40]
        footer = f'\n-# enviado por {who} con Jarvis'
        return text[:2000 - len(footer)] + footer
    return text[:2000]


async def send_as(guild, channel, requester, target, text, with_banner=False):
    """Envia `text` como `target` en `channel`. Devuelve (mensaje_enviado, aviso_o_None)."""
    text = (text or '').strip()
    if not text:
        raise _Refused('Escribe el mensaje que quieres enviar.')
    if len(text) > MAX_TEXT:
        raise _Refused(f'El mensaje es muy largo (maximo {MAX_TEXT} caracteres).')

    reason = protected_reason(guild, target, requester)
    if reason:
        raise _Refused(f'No puedo suplantar a **{target.display_name}**: {reason}.')

    thread = None
    parent = channel
    if isinstance(channel, discord.Thread):
        thread, parent = channel, channel.parent
    if not isinstance(parent, discord.TextChannel):
        raise _Refused('Solo puedo hacerlo en canales de texto o hilos.')

    rp = channel.permissions_for(requester)
    can_write = rp.send_messages_in_threads if thread is not None else rp.send_messages
    if not (rp.view_channel and can_write):
        raise _Refused('Solo puedes usarlo en canales donde tu mismo puedes escribir.')

    name = (target.display_name or target.name)[:80]
    avatar = target.display_avatar.with_static_format('png').with_size(256).url

    embeds, notice = [], None
    if with_banner:
        try:
            full = await client.fetch_user(target.id)
            if full.banner:
                emb = discord.Embed(color=full.accent_color or 0x2b2d31)
                emb.set_image(url=full.banner.url)
                embeds.append(emb)
            else:
                notice = f'{target.display_name} no tiene banner, asi que no pude agregarlo.'
        except Exception:
            notice = 'No pude obtener su banner.'

    kwargs = dict(
        content=_build_content(text, requester), username=name, avatar_url=avatar, wait=True,
        allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
    )
    if embeds:
        kwargs['embeds'] = embeds
    if thread is not None:
        kwargs['thread'] = thread

    sent = None
    for attempt in range(2):
        hook = await _get_webhook(parent)
        try:
            sent = await hook.send(**kwargs)
            break
        except discord.NotFound:          # alguien borro el webhook: se recrea una vez
            _hooks.pop(parent.id, None)
            if attempt == 1:
                raise _Refused('El webhook desaparecio y no pude recrearlo. Intenta de nuevo.')
        except discord.HTTPException as e:
            if 'username' in str(e).lower():
                raise _Refused(f'Discord no deja usar el nombre **{name}** en webhooks (no acepta nombres con '
                               f'"discord" ni "clyde").')
            raise _Refused(f'Discord rechazo el mensaje: {short_error(e)}')

    await _record(guild, channel, requester, target, text, sent)
    return sent, notice


# ----------------------------------------------------------------------------
# Registro
# ----------------------------------------------------------------------------
async def _record(guild, channel, requester, target, text, sent):
    entry = {
        'ts': time.time(), 'guildId': str(guild.id), 'guildName': getattr(guild, 'name', '') or '',
        'channelId': str(channel.id),
        'requesterId': str(requester.id), 'requesterName': str(getattr(requester, 'name', None) or requester)[:40],
        'targetId': str(target.id), 'targetName': str(getattr(target, 'name', None) or target)[:40],
        'messageId': str(getattr(sent, 'id', '')), 'text': text,
    }
    _log.append(entry)
    del _log[:-LOG_MAX]
    try:
        save_json(_log_file, _log)
    except Exception as e:
        print(f'[Suplantar] No pude guardar el registro: {e}')

    emb = discord.Embed(title='Suplantacion enviada', color=0xe67e22)
    emb.add_field(name='Quien lo uso', value=f'{requester.mention} (`{requester.id}`)', inline=True)
    emb.add_field(name='Como quien', value=f'{target.mention} (`{target.id}`)', inline=True)
    emb.add_field(name='Canal', value=getattr(channel, 'mention', str(channel.id)), inline=True)
    emb.add_field(name='Mensaje', value=(text[:1000] or '\u200b'), inline=False)
    emb.timestamp = datetime.now(timezone.utc)
    try:
        sml = _send_mod_log
        if sml is not None:
            await sml(client, guild.id, emb)
    except Exception as e:
        print(f'[Suplantar] mod log: {e}')
    if str(requester.id) != str(OWNER_ID):
        try:
            owner = await client.fetch_user(int(OWNER_ID))
            await owner.send(embed=emb)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Slash commands
# ----------------------------------------------------------------------------
def _register_slash_commands():
    @client.tree.command(name='suplantar', description='Envia un mensaje con el nombre y avatar de otro miembro (broma)')
    @app_commands.describe(usuario='A quien suplantar', mensaje='Lo que va a decir',
                           canal='Canal donde enviarlo (por defecto este)',
                           con_banner='Agregar su banner debajo como imagen (si tiene)')
    @app_commands.guild_only()
    async def slash_suplantar(interaction: discord.Interaction, usuario: discord.Member,
                              mensaje: app_commands.Range[str, 1, MAX_TEXT],
                              canal: discord.TextChannel = None, con_banner: bool = False):
        if str(interaction.user.id) not in JARVIS_WHITELIST:
            return await interaction.response.send_message(
                'Solo la whitelist de Jarvis puede usar este comando.', ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            _check_cooldown(interaction.user.id)
            sent, notice = await send_as(interaction.guild, canal or interaction.channel, interaction.user,
                                         usuario, mensaje, con_banner)
        except _Refused as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        except Exception as e:
            print(f'[Suplantar] /suplantar: {e}')
            return await interaction.followup.send(f'Error inesperado: {short_error(e)}', ephemeral=True)
        where = (canal or interaction.channel).mention
        extra = f'\n{notice}' if notice else ''
        await interaction.followup.send(f'Enviado como **{usuario.display_name}** en {where}.{extra}', ephemeral=True)

    @client.tree.command(name='suplantarshow', description='Ver quien uso /suplantar y que mando (solo owner del bot)')
    @app_commands.describe(cantidad='Cuantos mostrar (1-25, por defecto 10)',
                           usuario='Filtrar por quien lo uso (opcional)',
                           suplantado='Filtrar por a quien suplanto (opcional)')
    async def slash_suplantarshow(interaction: discord.Interaction,
                                  cantidad: app_commands.Range[int, 1, 25] = 10,
                                  usuario: discord.User = None, suplantado: discord.User = None):
        if str(interaction.user.id) != str(OWNER_ID):
            return await interaction.response.send_message('Solo el owner del bot puede usar este comando.', ephemeral=True)
        rows = [e for e in _log
                if (not usuario or e.get('requesterId') == str(usuario.id))
                and (not suplantado or e.get('targetId') == str(suplantado.id))]
        if not rows:
            return await interaction.response.send_message('Nadie ha usado /suplantar todavia (con ese filtro).', ephemeral=True)
        lines = []
        for e in reversed(rows[-cantidad:]):
            gname = e.get('guildName') or ''
            if not gname:
                g = getattr(client, 'get_guild', lambda _i: None)(int(e['guildId'])) if e.get('guildId', '').isdigit() else None
                gname = getattr(g, 'name', None) or f'servidor {e.get("guildId", "?")}'
            txt = discord.utils.escape_mentions((e.get('text') or '').replace('\n', ' '))
            if len(txt) > 150:
                txt = txt[:149] + '\u2026'
            link = ''
            if e.get('messageId') and e.get('guildId') and e.get('channelId'):
                link = f' [ver](https://discord.com/channels/{e["guildId"]}/{e["channelId"]}/{e["messageId"]})'
            lines.append(f'<t:{int(e["ts"])}:R> <@{e["requesterId"]}> (`{str(e.get("requesterName", "?"))[:40]}`) como '
                         f'<@{e["targetId"]}> (`{str(e.get("targetName", "?"))[:40]}`) en **{str(gname)[:60]}** <#{e["channelId"]}>{link}\n> {txt}')
        desc = ''
        for ln in lines:
            if len(desc) + len(ln) + 2 > 4000:
                break
            desc += ln + '\n\n'
        emb = discord.Embed(title=f'Usos de /suplantar ({len(rows)} guardados)', description=desc.strip(), color=0xe67e22)
        emb.set_footer(text=f'Se guardan los ultimos {LOG_MAX} usos.')
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @client.tree.command(name='nosuplantar', description='Pide que nadie pueda suplantarte con /suplantar')
    @app_commands.describe(activar='True = protegerme, False = permitir de nuevo')
    async def slash_nosuplantar(interaction: discord.Interaction, activar: bool = True):
        uid = str(interaction.user.id)
        if activar:
            _optout.add(uid)
            msg = 'Listo: nadie podra suplantarte con `/suplantar`. Puedes quitarlo con `/nosuplantar activar:False`.'
        else:
            _optout.discard(uid)
            msg = 'Listo: ahora otras personas de la whitelist podran suplantarte (con las protecciones de siempre).'
        _save_optout()
        await interaction.response.send_message(msg, ephemeral=True)


# ----------------------------------------------------------------------------
# Instalacion
# ----------------------------------------------------------------------------
MOD_PERMS = DEFAULT_MOD_PERMS
_send_mod_log = None


def install(main):
    """Conecta este modulo al archivo principal del bot. `main` = sys.modules[__name__] del bot."""
    global _installed, _optout_file, _log_file, MOD_PERMS, _send_mod_log
    if _installed:
        return
    _installed = True

    missing = [n for n in _NEEDED if not hasattr(main, n)]
    if missing:
        raise RuntimeError('jarvis_suplantar: faltan estos nombres en el bot principal: ' + ', '.join(missing)
                           + '. Llama a install() DESPUES de definirlos (antes de la seccion KEEPALIVE).')
    g = globals()
    for n in _NEEDED:
        g[n] = getattr(main, n)
    MOD_PERMS = tuple(getattr(main, 'MOD_PERMS', DEFAULT_MOD_PERMS))
    _send_mod_log = getattr(main, 'send_mod_log', None)

    _optout_file = main.DATA_DIR / 'impersonate_optout.json'
    _log_file = main.DATA_DIR / 'impersonate_log.json'
    loaded = load_json(_optout_file, [])
    if isinstance(loaded, list):
        _optout.update(str(x) for x in loaded)
    loaded = load_json(_log_file, [])
    if isinstance(loaded, list):
        _log.extend(loaded[-LOG_MAX:])

    if hasattr(main, 'build_help_embed'):
        original_help = main.build_help_embed

        def build_help_embed_with_suplantar():
            embed = original_help()
            embed.add_field(name=HELP_FIELD_NAME, value=HELP_FIELD_VALUE, inline=False)
            return embed

        main.build_help_embed = build_help_embed_with_suplantar

    _register_slash_commands()
    print(f'[Suplantar] Listo | {len(_optout)} persona(s) protegidas | nota_al_final={"SI" if FOOTER_ON else "NO"}')
