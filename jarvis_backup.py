"""
jarvis_backup.py  -  Backups automaticos del servidor + restauracion rapida desde el historial.

Por que existe
--------------
El bot YA tenia `/save` (descarga un .json) y `/restore` (hay que volver a subir ese .json a
mano). Eso sirve, pero en medio de un nuke nadie tiene ese archivo a mano, y si nunca corriste
`/save` antes, no hay nada que restaurar.

Este modulo agrega, SIN TOCAR `/save` ni `/restore`:
  - Backups automaticos cada X horas (configurable), guardados en el servidor con fecha.
  - Un historial de las ultimas N copias (no solo la ultima) para poder elegir "la de antes
    del ataque" en vez de la mas reciente.
  - Opcionalmente, una copia extra enviada a un canal de Discord (si el hosting borra el disco
    al reiniciar, esa copia en Discord sobrevive).
  - `/backup restaurar` para recuperar el servidor SIN tener que volver a subir ningun archivo.

Reutiliza `save_server_config` / `restore_server_config` de bot.py (no duplica esa logica).

Instalacion: al final de bot.py, junto a los otros jarvis_*.install(...):
    import jarvis_backup
    jarvis_backup.install(sys.modules[__name__])
"""
import json
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import tasks

M = None

_NEEDED = (
    'client', 'DATA_DIR', 'BACKUP_DIR', 'load_json', 'save_json', 'OWNER_ID', 'JARVIS_WHITELIST',
    'JarvisError', 'short_error', 'simple_embed', 'build_help_embed',
    'save_server_config', 'restore_server_config',
)

AUTOBACKUP_FILE = None   # DATA_DIR / 'autobackup.json'
HISTORY_ROOT = None      # BACKUP_DIR / 'history'
_data = {}                 # { guildId(str): {config} }
_installed = False

DEFAULTS = {
    'enabled': False,       # apagado por defecto: hay que activarlo con /backup activar
    'interval_hours': 6,
    'keep': 10,
    'channel': None,        # canal donde tambien se sube cada snapshot (resiliencia extra)
    'last_run': 0,
}


def _cfg(guild_id):
    cfg = _data.setdefault(str(guild_id), {})
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    return cfg


def _save():
    save_json(AUTOBACKUP_FILE, _data)


def _history_dir(guild_id):
    d = HISTORY_ROOT / str(guild_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _list_snapshots(guild_id):
    """Devuelve [(timestamp_str, Path), ...] mas reciente primero."""
    d = _history_dir(guild_id)
    files = sorted(d.glob('*.json'), reverse=True)
    return [(f.stem, f) for f in files]


def _prune(guild_id, keep):
    snaps = _list_snapshots(guild_id)
    for _, path in snaps[max(keep, 1):]:
        try:
            path.unlink()
        except OSError:
            pass


async def _take_snapshot(guild):
    backup, _canonical_path = await save_server_config(guild)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = _history_dir(guild.id) / f'{stamp}.json'
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(backup, f, indent=2, ensure_ascii=False)

    cfg = _cfg(guild.id)
    _prune(guild.id, cfg['keep'])

    channel_id = cfg.get('channel')
    if channel_id:
        channel = guild.get_channel(int(channel_id))
        if channel is not None:
            try:
                await channel.send(
                    content=f'🗄️ Backup automatico de **{guild.name}** — <t:{int(time.time())}:f>',
                    file=discord.File(path, filename=f'backup_{guild.id}_{stamp}.json'),
                )
            except discord.DiscordException as e:
                print(f'[Backup] No pude subir la copia al canal de {guild.id}: {e}')
    return backup, path


async def _restore_from_path(guild, path):
    with open(path, 'r', encoding='utf-8') as f:
        backup = json.load(f)
    report = await restore_server_config(guild, backup)
    return backup, report


# ============================================================================
# LOOP AUTOMATICO
# ============================================================================
@tasks.loop(minutes=20)
async def _autobackup_loop():
    now = time.time()
    for guild in list(client.guilds):
        cfg = _cfg(guild.id)
        if not cfg['enabled']:
            continue
        due = now - float(cfg.get('last_run') or 0) >= cfg['interval_hours'] * 3600
        if not due:
            continue
        try:
            await _take_snapshot(guild)
            cfg['last_run'] = now
            _save()
            print(f'[Backup] Snapshot automatico guardado para {guild.name} ({guild.id}).')
        except discord.DiscordException as e:
            print(f'[Backup] Fallo el snapshot automatico de {guild.id}: {e}')
        except OSError as e:
            print(f'[Backup] No pude escribir el snapshot de {guild.id} en disco: {e}')


async def _on_ready_start_loop():
    if not _autobackup_loop.is_running():
        _autobackup_loop.start()


# ============================================================================
# SLASH COMMANDS: /backup
# ============================================================================
backup_group = app_commands.Group(name='backup', description='Backups automaticos y restauracion rapida', guild_only=True)


def _bk_can_manage(user):
    if str(user.id) == str(OWNER_ID) or str(user.id) in JARVIS_WHITELIST:
        return True
    return bool(user.guild_permissions.administrator or user.guild_permissions.manage_guild)


async def _bk_slash(interaction, fn, defer=False):
    if not _bk_can_manage(interaction.user):
        return await interaction.response.send_message(
            'Necesitas **Manage Server** (o estar en la whitelist de Jarvis) para usar los backups.', ephemeral=True)
    try:
        if defer:
            await interaction.response.defer()
            embed = await fn()
            await interaction.followup.send(embed=embed)
        else:
            embed = fn()
            await interaction.response.send_message(embed=embed)
    except JarvisError as e:
        target = interaction.followup if defer else interaction.response
        await target.send(str(e), ephemeral=not defer)
    except Exception as e:
        print(f'[Backup] slash: {e}')
        target = interaction.followup if defer else interaction.response
        await target.send(f'Error inesperado: {short_error(e)}', ephemeral=not defer)


def _bk_status_embed(guild):
    cfg = _cfg(guild.id)
    snaps = _list_snapshots(guild.id)
    embed = simple_embed(f'Backups automaticos · {guild.name}', color=0x2ecc71 if cfg['enabled'] else 0x95a5a6)
    embed.add_field(name='Estado', value='🟢 Activado' if cfg['enabled'] else '🔴 Desactivado', inline=True)
    embed.add_field(name='Cada', value=f'{cfg["interval_hours"]} h', inline=True)
    embed.add_field(name='Guarda las ultimas', value=f'{cfg["keep"]} copias', inline=True)
    canal = f'<#{cfg["channel"]}>' if cfg.get('channel') else 'Ninguno (solo en disco)'
    embed.add_field(name='Copia tambien en', value=canal, inline=True)
    ultima = f'<t:{int(cfg["last_run"])}:R>' if cfg.get('last_run') else 'Nunca'
    embed.add_field(name='Ultimo backup automatico', value=ultima, inline=True)
    embed.add_field(name='Copias guardadas ahora', value=str(len(snaps)), inline=True)
    return embed


def _bk_enable(guild, enabled):
    cfg = _cfg(guild.id)
    cfg['enabled'] = enabled
    if enabled and not cfg.get('last_run'):
        cfg['last_run'] = 0   # fuerza que el proximo paso del loop haga un primer backup pronto
    _save()
    return simple_embed('Backups', f'Backups automaticos {"activados" if enabled else "desactivados"} en **{guild.name}**.',
                         0x2ecc71 if enabled else 0xe74c3c)


def _bk_set_interval(guild, hours):
    _cfg(guild.id)['interval_hours'] = hours
    _save()
    return simple_embed('Backups', f'Voy a guardar un backup automatico cada **{hours}** hora(s).', 0x2ecc71)


def _bk_set_keep(guild, keep):
    cfg = _cfg(guild.id)
    cfg['keep'] = keep
    _prune(guild.id, keep)
    _save()
    return simple_embed('Backups', f'Voy a conservar las ultimas **{keep}** copias de **{guild.name}**.', 0x2ecc71)


def _bk_set_channel(guild, channel):
    cfg = _cfg(guild.id)
    cfg['channel'] = str(channel.id) if channel else None
    _save()
    texto = (f'Cada backup automatico tambien se subira a {channel.mention} (ademas de guardarse en disco).'
             if channel else 'Ya no se sube ninguna copia extra a Discord; solo queda en disco.')
    return simple_embed('Backups', texto, 0x2ecc71)


async def _bk_now(guild):
    backup, path = await _take_snapshot(guild)
    return simple_embed(
        'Backup Guardado',
        f'Snapshot manual de **{guild.name}** guardado en el historial.\n\n'
        f'**Roles:** {len(backup["roles"])}\n'
        f'**Canales:** {len(backup["channels"])}\n'
        f'**Miembros con roles:** {len(backup["members"])}\n'
        f'**Archivo:** `{path.name}`\n'
        f'**Fecha:** <t:{int(time.time())}:f>',
        0x2ecc71,
    )


def _bk_list_embed(guild):
    snaps = _list_snapshots(guild.id)
    if not snaps:
        return simple_embed('Backups', 'Todavia no hay ninguna copia guardada para este servidor. Usa `/backup ahora`.', 0x95a5a6)
    embed = simple_embed(f'Historial de backups · {guild.name}')
    lines = []
    for i, (stamp, path) in enumerate(snaps[:15], start=1):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            info = f'{len(data.get("roles", []))} roles, {len(data.get("channels", []))} canales'
        except (OSError, json.JSONDecodeError):
            info = 'no se pudo leer'
        try:
            dt = datetime.strptime(stamp, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
            cuando = f'<t:{int(dt.timestamp())}:R>'
        except ValueError:
            cuando = stamp
        lines.append(f'**{i}.** {cuando} — {info}')
    embed.description = '\n'.join(lines)
    embed.set_footer(text='Usa /backup restaurar indice:<numero> (solo el owner del bot) para restaurar una de estas.')
    return embed


async def _bk_restore(interaction, indice):
    snaps = _list_snapshots(interaction.guild.id)
    if not snaps or indice < 1 or indice > len(snaps):
        return await interaction.followup.send(f'No hay una copia #{indice}. Usa `/backup listar` para ver las disponibles.')
    stamp, path = snaps[indice - 1]
    await interaction.followup.send(
        f'Restaurando **{interaction.guild.name}** a la copia del `{stamp}`...\n'
        'Esto va a crear, actualizar y **borrar** roles/canales para que coincidan con ese backup. Puede tardar un poco.',
    )
    try:
        backup, report = await _restore_from_path(interaction.guild, path)
    except (OSError, json.JSONDecodeError) as e:
        return await interaction.followup.send(f'No pude leer esa copia: {short_error(e)}')
    except discord.DiscordException as e:
        return await interaction.followup.send(f'Error restaurando: {short_error(e)}')

    embed = simple_embed('Restauracion Completada',
                          f'El servidor fue sincronizado con la copia `{stamp}`.', 0x2ecc71)
    embed.add_field(name='Roles', value=f'Creados: **{report["rolesCreated"]}**\nActualizados: **{report["rolesUpdated"]}**\n'
                                         f'Borrados: **{report["rolesDeleted"]}**\nOmitidos: **{report["rolesSkipped"]}**', inline=True)
    embed.add_field(name='Canales', value=f'Creados: **{report["channelsCreated"]}**\nActualizados: **{report["channelsUpdated"]}**\n'
                                           f'Borrados: **{report["channelsDeleted"]}**', inline=True)
    await interaction.followup.send(embed=embed)


@backup_group.command(name='activar', description='Activar los backups automaticos de este servidor')
async def bk_slash_on(interaction: discord.Interaction):
    await _bk_slash(interaction, lambda: _bk_enable(interaction.guild, True))


@backup_group.command(name='desactivar', description='Desactivar los backups automaticos')
async def bk_slash_off(interaction: discord.Interaction):
    await _bk_slash(interaction, lambda: _bk_enable(interaction.guild, False))


@backup_group.command(name='estado', description='Ver la configuracion de backups de este servidor')
async def bk_slash_status(interaction: discord.Interaction):
    await _bk_slash(interaction, lambda: _bk_status_embed(interaction.guild))


@backup_group.command(name='intervalo', description='Cada cuantas horas se guarda un backup automatico')
@app_commands.describe(horas='Cada cuantas horas (1-168)')
async def bk_slash_interval(interaction: discord.Interaction, horas: app_commands.Range[int, 1, 168]):
    await _bk_slash(interaction, lambda: _bk_set_interval(interaction.guild, horas))


@backup_group.command(name='mantener', description='Cuantas copias del historial conservar')
@app_commands.describe(cantidad='Cuantas copias guardar (1-30)')
async def bk_slash_keep(interaction: discord.Interaction, cantidad: app_commands.Range[int, 1, 30]):
    await _bk_slash(interaction, lambda: _bk_set_keep(interaction.guild, cantidad))


@backup_group.command(name='canal', description='Subir tambien cada backup automatico a un canal (resiliencia extra)')
@app_commands.describe(canal='Canal de texto donde subir la copia', quitar='Dejar de subir la copia a Discord')
async def bk_slash_channel(interaction: discord.Interaction, canal: discord.TextChannel = None, quitar: bool = False):
    target = None if quitar else (canal or interaction.channel)
    await _bk_slash(interaction, lambda: _bk_set_channel(interaction.guild, target))


@backup_group.command(name='ahora', description='Guardar un backup manual en el historial ahora mismo')
async def bk_slash_now(interaction: discord.Interaction):
    await _bk_slash(interaction, lambda: _bk_now(interaction.guild), defer=True)


@backup_group.command(name='listar', description='Ver las copias de backup guardadas de este servidor')
async def bk_slash_list(interaction: discord.Interaction):
    await _bk_slash(interaction, lambda: _bk_list_embed(interaction.guild))


@backup_group.command(name='restaurar', description='Restaurar el servidor desde una copia del historial (solo owner del bot)')
@app_commands.describe(indice='Numero de la copia segun /backup listar (1 = la mas reciente)')
async def bk_slash_restore(interaction: discord.Interaction, indice: app_commands.Range[int, 1, 100]):
    if str(interaction.user.id) != OWNER_ID:
        return await interaction.response.send_message('Solo el owner del bot puede restaurar servidores.', ephemeral=True)
    me = interaction.guild.me
    if not me.guild_permissions.manage_roles or not me.guild_permissions.manage_channels:
        return await interaction.response.send_message(
            'Necesito los permisos **Manage Roles** y **Manage Channels** para poder restaurar.', ephemeral=True)
    await interaction.response.defer()
    await _bk_restore(interaction, indice)


# ============================================================================
# INSTALACION
# ============================================================================
def install(main):
    """Conecta este modulo al archivo principal del bot. `main` = sys.modules[__name__] del bot."""
    global M, _installed, AUTOBACKUP_FILE, HISTORY_ROOT
    if _installed:
        return
    _installed = True
    M = main

    missing = [n for n in _NEEDED if not hasattr(main, n)]
    if missing:
        raise RuntimeError('jarvis_backup: faltan estos nombres en el bot principal: ' + ', '.join(missing)
                            + '. Llama a install() DESPUES de definirlos (antes de la seccion KEEPALIVE).')
    g = globals()
    for n in _NEEDED:
        g[n] = getattr(main, n)

    AUTOBACKUP_FILE = main.DATA_DIR / 'autobackup.json'
    HISTORY_ROOT = main.BACKUP_DIR / 'history'
    HISTORY_ROOT.mkdir(parents=True, exist_ok=True)

    loaded = load_json(AUTOBACKUP_FILE, {})
    if isinstance(loaded, dict):
        _data.update(loaded)

    client.add_listener(_on_ready_start_loop, 'on_ready')   # se suma, no reemplaza
    client.tree.add_command(backup_group)

    original_help = main.build_help_embed

    def build_help_embed_with_backup():
        embed = original_help()
        embed.add_field(
            name='Backups automaticos',
            value='`/backup activar` `/backup desactivar` `/backup estado`\n'
                  '`/backup intervalo` `/backup mantener` `/backup canal`\n'
                  '`/backup ahora` — snapshot manual al historial\n'
                  '`/backup listar` / `/backup restaurar` — recuperar sin subir ningun archivo',
            inline=False,
        )
        return embed

    main.build_help_embed = build_help_embed_with_backup

    total_activos = sum(1 for c in _data.values() if c.get('enabled'))
    print(f'[Backup] Listo | automatico activo en {total_activos} servidor(es) guardado(s).')
