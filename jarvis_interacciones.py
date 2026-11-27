# -*- coding: utf-8 -*-
"""Comandos sociales por prefijo para Jarvis."""
import asyncio

import aiohttp
import discord

M = None
_installed = False
_counts_file = None
_counts = {}
_count_locks = {}

_ACTIONS = {
    'kiss': ('besa a', 'kiss', 'beso', 'besos', 'Besar de vuelta', '💋'),
    'beso': ('besa a', 'kiss', 'beso', 'besos', 'Besar de vuelta', '💋'),
    'besito': ('besa a', 'kiss', 'beso', 'besos', 'Besar de vuelta', '💋'),
    'besar': ('besa a', 'kiss', 'beso', 'besos', 'Besar de vuelta', '💋'),
    'hug': ('abraza a', 'hug', 'abrazo', 'abrazos', 'Abrazar de vuelta', '🫂'),
    'abrazo': ('abraza a', 'hug', 'abrazo', 'abrazos', 'Abrazar de vuelta', '🫂'),
    'abrazar': ('abraza a', 'hug', 'abrazo', 'abrazos', 'Abrazar de vuelta', '🫂'),
    'pat': ('le da unas palmaditas a', 'pat', 'palmadita', 'palmaditas', 'Dar palmaditas de vuelta', '🐾'),
    'slap': (
        'le da una cachetada de broma a', 'slap', 'cachetada', 'cachetadas',
        'Devolver la cachetada', '🤚',
    ),
    'cuddle': ('se acurruca con', 'cuddle', 'mimo', 'mimos', 'Acurrucarse de vuelta', '🥰'),
    'highfive': (
        'choca los cinco con', 'highfive', 'choque de manos', 'choques de manos',
        'Chocar los cinco', '🙌',
    ),
}


async def _fetch_action_gif(action):
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f'https://nekos.best/api/v2/{action}') as response:
            response.raise_for_status()
            payload = await response.json()

    results = payload.get('results') if isinstance(payload, dict) else None
    result = results[0] if isinstance(results, list) and results else None
    image_url = result.get('url') if isinstance(result, dict) else None
    if not isinstance(image_url, str) or not image_url.startswith('https://nekos.best/'):
        raise ValueError(f'La API no devolvio un GIF valido para {action}.')
    anime_name = result.get('anime_name') or 'Anime'
    return image_url, str(anime_name)


class ActionReturnButton(discord.ui.Button):
    def __init__(self, action):
        _verb, _endpoint, _singular, _plural, label, emoji = _ACTIONS[action]
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if interaction.user.id != view.recipient_id:
            return await interaction.response.send_message(
                'Solo la persona que recibió la acción puede devolverla.',
                ephemeral=True,
            )
        if view.used:
            return await interaction.response.send_message(
                'Esta acción ya fue devuelta.',
                ephemeral=True,
            )

        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                'Esta acción solo se puede devolver dentro del servidor.',
                ephemeral=True,
            )
        channel = interaction.channel
        if channel is None:
            return await interaction.response.send_message(
                'No pude encontrar el canal para devolver la acción.',
                ephemeral=True,
            )

        view.used = True
        actor = guild.get_member(view.actor_id)
        if actor is None:
            try:
                actor = await guild.fetch_member(view.actor_id)
            except discord.HTTPException:
                view.used = False
                return await interaction.response.send_message(
                    'No pude encontrar a la persona para devolverle la acción.',
                    ephemeral=True,
                )

        try:
            await interaction.response.defer()
        except discord.HTTPException:
            view.used = False
            raise

        if interaction.message:
            try:
                await interaction.message.edit(view=None)
            except discord.HTTPException as exc:
                print(f'[Interacciones] No pude quitar el boton de respuesta: {exc}')

        sent = await _send_action(
            channel.send,
            guild,
            interaction.user,
            actor,
            self.action,
            allow_return=False,
        )
        if not sent:
            view.used = False
            if interaction.message:
                try:
                    await interaction.message.edit(view=view)
                except discord.HTTPException as exc:
                    print(f'[Interacciones] No pude restaurar el boton de respuesta: {exc}')


class ActionReturnView(discord.ui.View):
    def __init__(self, actor_id, recipient_id, action):
        super().__init__(timeout=300)
        self.actor_id = actor_id
        self.recipient_id = recipient_id
        self.used = False
        self.add_item(ActionReturnButton(action))


async def _send_action(send, guild, actor, recipient, action, allow_return=True):
    verb, endpoint, singular, plural, _label, _emoji = _ACTIONS[action]
    try:
        image_url, anime_name = await _fetch_action_gif(endpoint)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        print(f'[Interacciones] No pude obtener GIF de {endpoint}: {exc}')
        await send(content='No pude obtener el GIF ahora mismo. Inténtalo de nuevo en un momento.')
        return False

    count_key = f'{guild.id}:{endpoint}:{recipient.id}'
    count_lock = _count_locks.setdefault(count_key, asyncio.Lock())
    async with count_lock:
        count = _counts.get(count_key, 0) + 1
        noun = singular if count == 1 else plural
        embed = discord.Embed(
            description=(
                f'**{actor.display_name} {verb} {recipient.mention}.**\n'
                f'*{recipient.display_name} ha recibido {count} {noun}.*'
            ),
            color=0xf05a78,
        )
        embed.set_image(url=image_url)
        embed.set_footer(text=f'Anime: {anime_name}')
        view = ActionReturnView(actor.id, recipient.id, action) if allow_return else None
        try:
            await send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(users=[recipient], replied_user=False),
            )
        except discord.HTTPException as exc:
            await send(content=f'No pude enviar el GIF de `{endpoint}` (HTTP {exc.status}).')
            return False

        _counts[count_key] = count
        M.save_json(_counts_file, _counts)
        return True


async def _handle_interaction(message: discord.Message):
    if message.author.bot or not message.content.startswith(M.PREFIX):
        return False

    parts = message.content[len(M.PREFIX):].strip().split(maxsplit=1)
    if not parts or parts[0].lower() not in _ACTIONS:
        return False

    command = parts[0].lower()
    targets = list({member.id: member for member in message.mentions}.values())

    if not targets and message.reference:
        replied = message.reference.resolved
        if isinstance(replied, discord.Message):
            targets = [replied.author]

    if not targets:
        await message.reply(
            f'Uso: `{M.PREFIX}{command} @usuario` o responde al mensaje de alguien.'
        )
        return True

    if any(target.id == message.author.id for target in targets):
        await message.reply('Elige a otra persona para esa interacción.')
        return True

    _verb, action, _singular, _plural, _label, _emoji = _ACTIONS[command]
    for target in targets:
        await _send_action(message.reply, message.guild, message.author, target, action)
    return True


def install(main):
    """Conecta los comandos sociales al manejador de prefijo del bot."""
    global M, _installed, _counts_file, _counts
    if _installed:
        return
    _installed = True
    M = main
    _counts_file = M.DATA_DIR / 'interaction_counts.json'
    saved_counts = M.load_json(_counts_file, {})
    if isinstance(saved_counts, dict):
        _counts = {}
        for key, value in saved_counts.items():
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            if count > 0:
                _counts[str(key)] = count

    names = tuple(_ACTIONS)
    if hasattr(M, '_PREFIX_COMMAND_NAMES'):
        M._PREFIX_COMMAND_NAMES = tuple(dict.fromkeys(M._PREFIX_COMMAND_NAMES + names))

    original_handler = M.handle_command

    async def handle_command_with_interactions(message: discord.Message):
        if await _handle_interaction(message):
            return
        return await original_handler(message)

    M.handle_command = handle_command_with_interactions
    print('[Interacciones] Comandos cargados: ' + ', '.join(f'{M.PREFIX}{name}' for name in names))
