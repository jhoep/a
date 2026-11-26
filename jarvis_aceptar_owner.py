d# -*- coding: utf-8 -*-
"""
jarvis_aceptar_owner.py
=======================
Comando de prefijo `>>aceptar owner <guild_id>` (EXCLUSIVO para el OWNER_ID del bot).
Aprueba UNICAMENTE la solicitud de ingreso pendiente del owner del bot en ese servidor.
"""
import discord
from discord.http import Route

M = None
_installed = False


async def _approve_owner_request(guild: discord.Guild, owner_user_id: str):
    """Busca y aprueba SOLO la solicitud de ingreso (SUBMITTED) del owner del bot."""
    approved = []
    errors = []

    try:
        data = await M.client.http.request(
            Route('GET', '/guilds/{guild_id}/requests', guild_id=guild.id)
        )
    except discord.HTTPException as e:
        return approved, [f'HTTP {e.status} al leer solicitudes: {e.text}']

    requests_list = (data or {}).get('guild_join_requests') or []
    for req in requests_list:
        if req.get('application_status') != 'SUBMITTED':
            continue

        # FILTRO: Solo procesa si el solicitante es el OWNER_ID del bot
        uid = str(req.get('user_id') or '')
        if uid != str(owner_user_id):
            continue

        req_id = req.get('id')
        if not req_id:
            continue

        try:
            await M.client.http.request(
                Route(
                    'PATCH',
                    '/guilds/{guild_id}/requests/{request_id}',
                    guild_id=guild.id,
                    request_id=req_id,
                ),
                json={'action': 'APPROVED'},
            )
            approved.append(uid)
        except discord.HTTPException as e:
            errors.append(f'ID `{uid}`: HTTP {e.status} ({e.text})')

    return approved, errors


async def _handle_aceptar_command(message: discord.Message):
    if message.author.bot or not message.content.startswith(M.PREFIX):
        return False

    raw = message.content[len(M.PREFIX):].strip()
    if not raw:
        return False

    parts = raw.split()
    if parts[0].lower() != 'aceptar':
        return False

    # EXCLUSIVO OWNER DEL BOT: si no es el OWNER_ID, sale de inmediato sin responder nada
    if str(message.author.id) != str(M.OWNER_ID):
        return True

    # Soporta: >>aceptar owner <guild_id>  y  >>aceptar <guild_id>
    idx = 1
    if len(parts) > 1 and parts[1].lower() == 'owner':
        idx = 2

    if len(parts) <= idx or not parts[idx].isdigit():
        await message.reply(f'Uso: `{M.PREFIX}aceptar owner <ID_del_servidor>`')
        return True

    guild_id = int(parts[idx])
    target_guild = M.client.get_guild(guild_id)
    if target_guild is None:
        await message.reply(f'No encontre ningun servidor con ID `{guild_id}` (asegurate de que el bot este dentro).')
        return True

    me = target_guild.me
    if me is not None and not (me.guild_permissions.kick_members or me.guild_permissions.administrator):
        await message.reply(
            f'Me falta el permiso **Kick Members** (o **Administrator**) en **{target_guild.name}** para aprobar solicitudes.'
        )
        return True

    # Aprueba SOLO la solicitud del OWNER_ID del bot
    approved, errors = await _approve_owner_request(target_guild, str(M.OWNER_ID))

    server_owner_id = target_guild.owner_id
    server_owner_mention = f'<@{server_owner_id}> (`{server_owner_id}`)' if server_owner_id else 'Desconocido'

    if approved:
        status_text = f'Solicitud del owner del bot (<@{M.OWNER_ID}>) **aprobada**.'
        color = 0x2ecc71
    elif errors:
        status_text = 'No se pudo aprobar la solicitud por un error.'
        color = 0xe74c3c
    else:
        status_text = f'No encontre ninguna solicitud pendiente para <@{M.OWNER_ID}> en ese servidor.'
        color = 0xe67e22

    desc_lines = [
        f'**Servidor:** {target_guild.name} (`{target_guild.id}`)',
        f'**Owner del servidor:** {server_owner_mention}',
        f'**Estado:** {status_text}',
    ]
    if errors:
        desc_lines.append('**Errores:**\n' + '\n'.join(errors[:5]))

    embed = M.simple_embed(
        'Aceptacion de Owner del Bot',
        '\n'.join(desc_lines),
        color,
    )
    embed.set_footer(text=f'Ejecutado por {message.author}')

    await message.reply(embed=embed)
    return True


def install(main):
    """Conecta este modulo al bot principal."""
    global M, _installed
    if _installed:
        return
    _installed = True
    M = main

    if hasattr(M, '_PREFIX_COMMAND_NAMES') and 'aceptar' not in M._PREFIX_COMMAND_NAMES:
        M._PREFIX_COMMAND_NAMES = tuple(M._PREFIX_COMMAND_NAMES) + ('aceptar',)

    orig_handle_command = M.handle_command

    async def handle_command_with_aceptar(message: discord.Message):
        if await _handle_aceptar_command(message):
            return
        return await orig_handle_command(message)

    M.handle_command = handle_command_with_aceptar
    print('[AceptarOwner] Modulo cargado (solo aprueba al owner del bot): >>aceptar owner <guild_id>')
