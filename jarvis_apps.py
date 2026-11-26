# -*- coding: utf-8 -*-
"""Auditoria y proteccion basica contra spam de webhooks y apps externas."""
import discord
from discord import app_commands

M = None
_installed = False


def _is_admin(interaction):
    return bool(interaction.user.guild_permissions.administrator)


async def _deny_non_admin(interaction):
    if _is_admin(interaction):
        return False
    await interaction.response.send_message(
        'Este comando solo lo pueden usar administradores del servidor.',
        ephemeral=True,
    )
    return True


def _short_names(items, limit=8):
    names = [str(item) for item in items[:limit]]
    if len(items) > limit:
        names.append(f'y {len(items) - limit} mas')
    return ', '.join(names) or 'Ninguno visible'


def install(main):
    """Registra los comandos slash de auditoria y proteccion."""
    global M, _installed
    if _installed:
        return
    _installed = True
    M = main

    @M.client.tree.command(
        name='comprobar',
        description='Revisar la exposicion a spam de bots, webhooks y aplicaciones',
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def comprobar(interaction: discord.Interaction):
        if await _deny_non_admin(interaction):
            return

        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                'Usa este comando dentro de un servidor.',
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        everyone = guild.default_role
        everyone_webhooks = everyone.permissions.manage_webhooks
        everyone_admin = everyone.permissions.administrator
        channel_overrides = [
            channel.name
            for channel in guild.channels
            if channel.overwrites_for(everyone).manage_webhooks is True
        ]
        other_channel_grants = [
            f'{channel.name}: {getattr(target, "display_name", getattr(target, "name", str(target)))}'
            for channel in guild.channels
            for target, overwrite in channel.overwrites.items()
            if target != everyone and overwrite.manage_webhooks is True
        ]
        privileged_roles = [
            role.name for role in guild.roles
            if role != everyone and (
                role.permissions.manage_webhooks or role.permissions.administrator
            )
        ]
        visible_bots = [member.display_name for member in guild.members if member.bot]

        webhook_line = 'No se pudieron consultar los webhooks.'
        webhook_count = None
        webhook_checked = False
        bot_permissions = interaction.guild.me.guild_permissions if interaction.guild.me else None
        if bot_permissions and (
            bot_permissions.manage_webhooks or bot_permissions.administrator
        ):
            try:
                webhooks = await guild.webhooks()
                webhook_count = len(webhooks)
                webhook_checked = True
                webhook_line = f'{webhook_count} webhook(s): {_short_names([wh.name for wh in webhooks])}'
            except discord.HTTPException as exc:
                webhook_line = f'Error al consultar webhooks (HTTP {exc.status}).'
        else:
            webhook_line = 'Sin permiso Manage Webhooks: no se pudo comprobar.'

        integrations_line = 'No se pudieron consultar las integraciones.'
        integrations_checked = False
        try:
            integrations = await guild.integrations()
            integrations_checked = True
            integrations_line = _short_names([
                f'{integration.name} ({integration.type})' for integration in integrations
            ])
        except discord.Forbidden:
            integrations_line = 'Sin permiso Manage Server para consultar integraciones.'
        except discord.HTTPException as exc:
            integrations_line = f'Error al consultar integraciones (HTTP {exc.status}).'

        exposure = (
            everyone_webhooks
            or everyone_admin
            or bool(channel_overrides)
            or bool(other_channel_grants)
            or bool(privileged_roles)
            or bool(webhook_count)
            or bool(visible_bots)
        )
        if exposure:
            status = 'Hay puntos de exposicion que revisar.'
        elif not (webhook_checked and integrations_checked):
            status = 'No se pudo descartar la exposicion porque faltan permisos de lectura.'
        else:
            status = 'No se encontro una exposicion directa; esto no garantiza riesgo cero.'
        cfg = M.antiraid_cfg(guild.id)
        antispam_on = bool(M.antiraid_active(guild.id) and cfg.get('spam_enabled'))
        lines = [
            f'**Resultado:** {status}',
            f'**Anti-spam de Jarvis:** {"Activo" if antispam_on else "Inactivo"}',
            f'**@everyone puede gestionar webhooks:** {"Si" if everyone_webhooks else "No"}',
            f'**@everyone tiene Administrador:** {"Si; eso incluye todos los permisos" if everyone_admin else "No"}',
            f'**Canales que lo permiten a @everyone:** {_short_names(channel_overrides)}',
            f'**Otros permisos de canal para webhooks:** {_short_names(other_channel_grants)}',
            f'**Roles con Manage Webhooks o Administrador:** {_short_names(privileged_roles)}',
            f'**Webhooks:** {webhook_line}',
            f'**Integraciones visibles:** {integrations_line}',
            f'**Bots visibles en cache:** {_short_names(visible_bots)}',
            '',
            'Discord no permite garantizar que una app externa no envie mensajes; los bots con permisos y los webhooks existentes pueden seguir publicando.',
        ]
        await interaction.followup.send('\n'.join(lines)[:1950], ephemeral=True)

    @M.client.tree.command(
        name='proteger',
        description='Impedir que @everyone cree webhooks nuevos',
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def proteger(interaction: discord.Interaction):
        if await _deny_non_admin(interaction):
            return

        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                'Usa este comando dentro de un servidor.',
                ephemeral=True,
            )

        bot_member = guild.me
        bot_permissions = bot_member.guild_permissions if bot_member else None
        if not bot_permissions or not (
            bot_permissions.manage_roles or bot_permissions.administrator
        ):
            return await interaction.response.send_message(
                'Necesito el permiso **Manage Roles** para cambiar los permisos de @everyone.',
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        everyone = guild.default_role
        if everyone.permissions.administrator:
            return await interaction.followup.send(
                '@everyone tiene **Administrator**, que concede todos los permisos. '
                'No puedo quitar solo Manage Webhooks mientras ese permiso siga activo.',
                ephemeral=True,
            )
        role_permissions = discord.Permissions(everyone.permissions.value)
        role_permissions.manage_webhooks = False
        try:
            await everyone.edit(
                permissions=role_permissions,
                reason=f'[Jarvis] Proteccion de webhooks solicitada por {interaction.user}',
            )
        except discord.HTTPException as exc:
            return await interaction.followup.send(
                f'No pude actualizar @everyone (HTTP {exc.status}). No se modificaron los permisos de canales.',
                ephemeral=True,
            )

        changed_channels = []
        failures = []
        for channel in guild.channels:
            overwrite = channel.overwrites_for(everyone)
            if overwrite.manage_webhooks is not True:
                continue
            overwrite.manage_webhooks = False
            try:
                await channel.set_permissions(
                    everyone,
                    overwrite=overwrite,
                    reason=f'[Jarvis] Proteccion de webhooks solicitada por {interaction.user}',
                )
                changed_channels.append(channel.name)
            except discord.HTTPException as exc:
                failures.append(f'{channel.name} (HTTP {exc.status})')

        remaining_roles = [
            role.name for role in guild.roles
            if role != everyone and (
                role.permissions.manage_webhooks or role.permissions.administrator
            )
        ]
        remaining_channel_grants = [
            f'{channel.name}: {getattr(target, "display_name", getattr(target, "name", str(target)))}'
            for channel in guild.channels
            for target, overwrite in channel.overwrites.items()
            if target != everyone and overwrite.manage_webhooks is True
        ]
        result = [
            '**Proteccion aplicada:** @everyone ya no tiene permiso global para gestionar webhooks.',
            f'**Permisos de canal corregidos:** {len(changed_channels)}',
        ]
        if failures:
            result.append(f'**Canales con error:** {_short_names(failures)}')
        result.append('Los webhooks existentes no se borraron y pueden seguir enviando mensajes.')
        if remaining_roles:
            result.append(
                '**Otros roles aún pueden gestionar webhooks:** '
                + _short_names(remaining_roles)
                + '. Revísalos si no son de confianza.'
            )
        if remaining_channel_grants:
            result.append(
                '**Otros permisos de canal siguen activos:** '
                + _short_names(remaining_channel_grants)
                + '.'
            )
        await interaction.followup.send('\n'.join(result)[:1950], ephemeral=True)

    print('[Apps] Comandos cargados: /comprobar y /proteger')
