"""
jarvis_tts.py - Jarvis lee el chat en voz alta cuando esta en un canal de voz.

Instalacion:
    pip install edge-tts            (voces neuronales de Microsoft, gratis, recomendado)
    pip install gTTS                (opcional: respaldo si edge-tts falla)
    + FFmpeg, PyNaCl y opus (ya los usa tu musica)

En bot.py, junto a los otros modulos (despues de jarvis_gestion.install):
    import jarvis_tts
    jarvis_tts.install(sys.modules[__name__])

Uso:
    /tts activar [canal]   /tts desactivar   /tts voz   /tts velocidad   /tts parar   /tts estado
    jarvis lee el chat  |  jarvis desactiva el tts  |  jarvis estado del tts

Reglas:
  - Solo lee mensajes del canal configurado (puede ser el chat de un canal de voz).
  - Solo lee a quienes estan en el MISMO canal de voz que el bot.
  - Si esta sonando musica, no lee (la musica tiene prioridad).
  - Ignora bots, comandos (>>, /, "jarvis ..."), links y emojis.
"""
import asyncio
import os
import re
import tempfile
import time
from typing import Union

import discord
from discord import app_commands

try:
    import edge_tts
except Exception:
    edge_tts = None
try:
    from gtts import gTTS
except Exception:
    gTTS = None

M = None            # modulo del bot (se asigna en install)
TTS_FILE = None
cfg = {}            # { guildId: {'enabled': bool, 'channel': str, 'voice': str, 'rate': int} }
states = {}         # guildId -> GuildState

VOICES = {
    'Salome (Colombia, mujer)': 'es-CO-SalomeNeural',
    'Gonzalo (Colombia, hombre)': 'es-CO-GonzaloNeural',
    'Dalia (Mexico, mujer)': 'es-MX-DaliaNeural',
    'Jorge (Mexico, hombre)': 'es-MX-JorgeNeural',
    'Elvira (Espana, mujer)': 'es-ES-ElviraNeural',
    'Alvaro (Espana, hombre)': 'es-ES-AlvaroNeural',
    'Elena (Argentina, mujer)': 'es-AR-ElenaNeural',
    'Tomas (Argentina, hombre)': 'es-AR-TomasNeural',
}
VOICE_LABELS = {v: k for k, v in VOICES.items()}
DEFAULT_VOICE = 'es-CO-SalomeNeural'

MAX_CHARS = 220          # largo maximo de lo que se lee por mensaje
QUEUE_MAX = 8            # mensajes en cola; si se llena, se descartan los nuevos
NAME_REPEAT_SECS = 30    # vuelve a decir "X dice:" si pasa este tiempo o cambia quien escribe
USER_COOLDOWN = 1.0      # segundos minimos entre mensajes leidos de la misma persona
TEXT_TYPES = (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)

URL_RE = re.compile(r'https?://\S+|www\.\S+', re.IGNORECASE)
EMOJI_RE = re.compile('[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3]')


# ----------------------------------------------------------------- estado --
class GuildState:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.task = None
        self.playing = False
        self.last_author = None
        self.last_time = 0.0
        self.user_last = {}


def get_state(gid):
    st = states.get(gid)
    if st is None:
        st = states[gid] = GuildState()
    return st


def save_cfg():
    M.save_json(TTS_FILE, cfg)


def safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ------------------------------------------------------------ limpieza de texto --
def spoken_name(member):
    n = re.sub(r'[_\-.]+', ' ', member.display_name or '')
    n = EMOJI_RE.sub('', n)
    n = re.sub(r'[^\w\s]', '', n).strip()
    return n[:30] or 'alguien'


def clean_text(message):
    t = message.content or ''
    for u in message.mentions:
        t = t.replace(f'<@{u.id}>', f' {spoken_name(u)} ').replace(f'<@!{u.id}>', f' {spoken_name(u)} ')
    for r in message.role_mentions:
        t = t.replace(f'<@&{r.id}>', f' {r.name} ')
    for c in message.channel_mentions:
        t = t.replace(f'<#{c.id}>', f' {c.name} ')
    t = re.sub(r'<a?:\w+:\d+>', ' ', t)               # emojis personalizados
    t = URL_RE.sub(' enlace ', t)
    t = re.sub(r'<@[!&]?\d+>|<#\d+>', ' ', t)         # menciones que no se pudieron resolver
    t = EMOJI_RE.sub('', t)
    t = re.sub(r'[*_~`|>#]+', ' ', t)                 # markdown
    t = re.sub(r'(.)\1{4,}', r'\1\1\1', t)            # "aaaaaaaa" -> "aaa"
    t = re.sub(r'\s+', ' ', t).strip()
    if not t and message.attachments:
        t = 'envio un archivo'
    if not re.search(r'\w', t):
        return ''
    if len(t) > MAX_CHARS:
        t = t[:MAX_CHARS].rsplit(' ', 1)[0] + '...'
    return t


# ----------------------------------------------------------------- sintesis --
async def synth(text, voice, rate):
    """Genera un mp3 temporal con la voz y devuelve su ruta."""
    fd, path = tempfile.mkstemp(prefix='jarvis_tts_', suffix='.mp3')
    os.close(fd)
    try:
        if edge_tts is not None:
            try:
                comm = edge_tts.Communicate(text, voice, rate=f'{int(rate):+d}%')
                await asyncio.wait_for(comm.save(path), timeout=20)
                if os.path.getsize(path) > 0:
                    return path
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if gTTS is None:
                    raise
                print(f'[TTS] edge-tts fallo ({M.short_error(e)}); uso gTTS.')
        if gTTS is None:
            raise RuntimeError('No hay motor de voz disponible (instala edge-tts).')
        loop = asyncio.get_running_loop()

        def _g():
            gTTS(text=text, lang='es', tld='com.mx').save(path)

        await asyncio.wait_for(loop.run_in_executor(None, _g), timeout=20)
        return path
    except BaseException:
        safe_remove(path)
        raise


# ---------------------------------------------------------- cola y reproduccion --
def music_busy(guild):
    player = M.music_players.get(guild.id)
    return bool(player and (player.current or player.queue))


def clear_queue(st):
    n = 0
    while True:
        try:
            task = st.queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        n += 1
        if task.done() and not task.cancelled() and task.exception() is None:
            safe_remove(task.result())
        else:
            task.cancel()
    return n


def ensure_worker(gid):
    st = get_state(gid)
    if st.task is None or st.task.done():
        st.task = M.spawn(_worker(gid))


async def _play(guild, st, path):
    vc = guild.voice_client if guild else None
    if not vc or not vc.is_connected():
        return
    for _ in range(20):                       # espera hasta 5s a que el canal quede libre
        if not vc.is_playing() and not vc.is_paused():
            break
        if music_busy(guild):
            return                            # la musica manda
        await asyncio.sleep(0.25)
    else:
        return

    loop = asyncio.get_running_loop()
    ev = asyncio.Event()

    def after(err):
        if err:
            print(f'[TTS] error de reproduccion: {err}')
        loop.call_soon_threadsafe(ev.set)

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(path, executable=M.FFMPEG_EXE, options='-vn'), volume=1.0)
    st.playing = True
    try:
        vc.play(source, after=after)
        await asyncio.wait_for(ev.wait(), timeout=90)
    except asyncio.TimeoutError:
        try:
            vc.stop()
        except Exception:
            pass
    except Exception as e:
        print(f'[TTS] no pude reproducir: {M.short_error(e)}')
    finally:
        st.playing = False


async def _worker(gid):
    st = get_state(gid)
    while True:
        try:
            task = await asyncio.wait_for(st.queue.get(), timeout=600)
        except asyncio.TimeoutError:
            st.task = None                    # inactivo: se cierra, se reabre con el siguiente mensaje
            return
        try:
            path = await task
        except asyncio.CancelledError:
            continue
        except Exception as e:
            print(f'[TTS] no pude generar la voz: {M.short_error(e)}')
            continue
        try:
            await _play(M.client.get_guild(gid), st, path)
        finally:
            await asyncio.sleep(0.3)          # ffmpeg suelta el archivo (importante en Windows)
            safe_remove(path)


# ------------------------------------------------------------------ listener --
async def on_message(message):
    try:
        await _handle(message)
    except Exception as e:
        print(f'[TTS] on_message: {e}')


async def _handle(message):
    guild = message.guild
    if message.author.bot or guild is None or getattr(message, 'webhook_id', None):
        return
    c = cfg.get(str(guild.id))
    if not c or not c.get('enabled') or str(message.channel.id) != c.get('channel'):
        return
    content = message.content or ''
    if not content.strip() and not message.attachments:
        return
    if content.startswith(M.PREFIX) or content.startswith('/') or M.JARVIS_TRIGGER.match(content):
        return
    if M.is_watched_for_deletion(guild.id, message.author.id):
        return

    vc = guild.voice_client
    if not vc or not vc.is_connected() or not vc.channel:
        return
    author_vc = M.voice_channel_of(message.author)
    if author_vc is None or author_vc.id != vc.channel.id:
        return                                # solo se lee a quienes estan en mi canal de voz
    if music_busy(guild):
        return

    text = clean_text(message)
    if not text:
        return

    st = get_state(guild.id)
    now = time.time()
    if now - st.user_last.get(message.author.id, 0) < USER_COOLDOWN:
        return
    st.user_last[message.author.id] = now
    if st.queue.qsize() >= QUEUE_MAX:
        return

    speak = text
    if st.last_author != message.author.id or now - st.last_time > NAME_REPEAT_SECS:
        speak = f'{spoken_name(message.author)} dice: {text}'
    st.last_author, st.last_time = message.author.id, now

    # la voz se genera ya (mientras suena el mensaje anterior), asi casi no hay espera
    task = asyncio.create_task(synth(speak, c.get('voice', DEFAULT_VOICE), int(c.get('rate', 0))))
    st.queue.put_nowait(task)
    ensure_worker(guild.id)


# --------------------------------------------------------------- operaciones --
def status_embed(guild, title='Lectura del chat'):
    c = cfg.get(str(guild.id)) or {}
    on = bool(c.get('enabled'))
    emb = M.simple_embed(
        title,
        'Leo en voz alta los mensajes de quienes estan en mi canal de voz.' if on
        else 'Apagado. Usa `/tts activar` o `jarvis lee el chat`.',
        0x2ecc71 if on else 0x95a5a6)
    emb.add_field(name='Estado', value='Activado' if on else 'Desactivado', inline=True)
    if on:
        emb.add_field(name='Canal que leo', value=f'<#{c["channel"]}>', inline=True)
        emb.add_field(name='Voz', value=VOICE_LABELS.get(c.get('voice'), c.get('voice', DEFAULT_VOICE)), inline=True)
        emb.add_field(name='Velocidad', value=f'{int(c.get("rate", 0)):+d}%', inline=True)
    vc = guild.voice_client
    emb.add_field(name='Canal de voz',
                  value=vc.channel.mention if vc and vc.is_connected() and vc.channel else 'No estoy en ninguno',
                  inline=True)
    emb.set_footer(text='Solo leo a quienes estan en mi canal de voz. Si suena musica, no leo.')
    return emb


async def tts_enable(guild, member, text_channel):
    if edge_tts is None and gTTS is None:
        raise M.JarvisError('Falta instalar el motor de voz en el servidor del bot: `pip install edge-tts`.')
    if not M.FFMPEG_EXE:
        raise M.JarvisError('Me falta FFmpeg en el servidor del bot.')
    if not M.NACL_OK:
        raise M.JarvisError('Me falta `PyNaCl` en el servidor del bot para hablar en voz.')

    vc = guild.voice_client
    if not (vc and vc.is_connected()):
        ch = M.voice_channel_of(member)
        if ch is None:
            raise M.JarvisError('Entra a un canal de voz (o conectame con `/connect`) y repite.')
        perms = ch.permissions_for(guild.me)
        if not (perms.view_channel and perms.connect and (perms.speak or isinstance(ch, discord.StageChannel))):
            raise M.JarvisError(f'No tengo permisos para conectarme o hablar en {ch.mention}.')
        join = getattr(M, '_afk_join', None) or M.music_connect
        try:
            await join(guild, ch)
        except Exception as e:
            raise M.JarvisError(f'No pude conectarme a {ch.mention}: {M.short_error(e)}')

    c = cfg.setdefault(str(guild.id), {})
    c['enabled'] = True
    c['channel'] = str(text_channel.id)
    c.setdefault('voice', DEFAULT_VOICE)
    c.setdefault('rate', 0)
    save_cfg()
    return status_embed(guild, 'Lectura del chat activada')


async def tts_halt(guild):
    """Corta lo que esta leyendo y vacia la cola. Devuelve cuantos mensajes habia en cola."""
    st = states.get(guild.id)
    if not st:
        return 0
    n = clear_queue(st)
    vc = guild.voice_client
    if st.playing and vc and vc.is_playing():
        vc.stop()
    return n


async def tts_disable(guild):
    c = cfg.get(str(guild.id))
    if not c or not c.get('enabled'):
        raise M.JarvisError('La lectura del chat ya esta desactivada.')
    c['enabled'] = False
    save_cfg()
    await tts_halt(guild)
    return M.simple_embed('Lectura desactivada',
                          'Ya no leo el chat. Sigo en el canal de voz; usa `jarvis desconectate` o `/leave` para sacarme.',
                          0xe67e22)


async def tts_stop(guild):
    n = await tts_halt(guild)
    return M.simple_embed('Silencio', f'Corte la lectura y limpie {n} mensaje(s) de la cola.', 0xe67e22)


def _need_cfg(guild):
    c = cfg.get(str(guild.id))
    if not c:
        raise M.JarvisError('Primero activa la lectura con `/tts activar`.')
    return c


async def tts_set_voice(guild, voice):
    _need_cfg(guild)['voice'] = voice
    save_cfg()
    return status_embed(guild, 'Voz cambiada')


async def tts_set_rate(guild, rate):
    _need_cfg(guild)['rate'] = int(rate)
    save_cfg()
    return status_embed(guild, 'Velocidad cambiada')


# ------------------------------------------------------------- slash commands --
tts_group = app_commands.Group(name='tts', description='Leer el chat en voz alta', guild_only=True)


async def _slash(interaction, fn):
    if not M.ar_can_manage(interaction.user):
        return await interaction.response.send_message(
            'Necesitas el permiso **Manage Server** (o estar en la whitelist de Jarvis) para usar esto.', ephemeral=True)
    await interaction.response.defer()
    try:
        embed = await fn()
    except (M.JarvisError, M.MusicError) as e:
        return await interaction.followup.send(str(e), ephemeral=True)
    except Exception as e:
        print(f'[TTS] slash: {e}')
        return await interaction.followup.send(f'Error inesperado: {M.short_error(e)}', ephemeral=True)
    await interaction.followup.send(embed=embed)


@tts_group.command(name='activar', description='Leer en voz alta el chat de un canal (me uno a tu canal de voz)')
@app_commands.describe(canal='Canal de texto (o chat de un canal de voz) a leer. Omitir = este canal')
async def tts_activar(interaction: discord.Interaction,
                      canal: Union[discord.TextChannel, discord.VoiceChannel] = None):
    target = canal or interaction.channel
    await _slash(interaction, lambda: tts_enable(interaction.guild, interaction.user, target))


@tts_group.command(name='desactivar', description='Dejar de leer el chat')
async def tts_desactivar(interaction: discord.Interaction):
    await _slash(interaction, lambda: tts_disable(interaction.guild))


@tts_group.command(name='voz', description='Cambiar la voz con la que leo')
@app_commands.describe(voz='Voz a usar')
@app_commands.choices(voz=[app_commands.Choice(name=label, value=value) for label, value in VOICES.items()])
async def tts_voz(interaction: discord.Interaction, voz: app_commands.Choice[str]):
    await _slash(interaction, lambda: tts_set_voice(interaction.guild, voz.value))


@tts_group.command(name='velocidad', description='Cambiar la velocidad de lectura (-50 lento, +100 muy rapido)')
@app_commands.describe(valor='Porcentaje: 0 normal, +20 un poco mas rapido, -20 mas lento')
async def tts_velocidad(interaction: discord.Interaction, valor: app_commands.Range[int, -50, 100]):
    await _slash(interaction, lambda: tts_set_rate(interaction.guild, valor))


@tts_group.command(name='parar', description='Callar lo que estoy leyendo y vaciar la cola')
async def tts_parar(interaction: discord.Interaction):
    await _slash(interaction, lambda: tts_stop(interaction.guild))


@tts_group.command(name='estado', description='Ver la configuracion de la lectura del chat')
async def tts_estado(interaction: discord.Interaction):
    async def _s():
        return status_embed(interaction.guild)
    await _slash(interaction, _s)


# ------------------------------------------------------------ integracion Jarvis --
async def act_tts_on(ctx, intent):
    ch = M.pick_channel(ctx, intent)
    if not isinstance(ch, TEXT_TYPES):
        raise M.JarvisError('Tiene que ser un canal de texto (o el chat de un canal de voz).')
    return await tts_enable(ctx.guild, ctx.author, ch)


async def act_tts_off(ctx, intent):
    return await tts_disable(ctx.guild)


async def act_tts_status(ctx, intent):
    return status_embed(ctx.guild)


async def act_tts_stop(ctx, intent):
    return await tts_stop(ctx.guild)


TTS_ACTIONS = {'tts_on', 'tts_off', 'tts_status', 'tts_stop'}

TTS_PROMPT = """
ACCIONES EXTRA (leer el chat en voz alta / TTS):
- tts_on: el bot lee en voz alta los mensajes del chat de quienes estan en su canal de voz. channel (canal de texto a leer; vacio = el canal actual). Si no esta en voz, se une al canal del JEFE.
- tts_off | tts_status | tts_stop: desactivar / ver estado / callar ya lo que esta leyendo y vaciar la cola
REGLA EXTRA: "lee el chat", "lee los mensajes en voz alta", "activa el tts", "texto a voz" = tts_on. "deja de leer el chat", "desactiva el tts" = tts_off. "calla el tts" = tts_stop. Nunca son voice_disconnect ni music_*.
"""

_TTS_WORDS = re.compile(
    r'\btts\b|texto\s+a\s+voz|voz\s+alta|\blee(?:r|me)?\s+(?:el\s+|los\s+|todo\s+|este\s+)?(?:chat|canal|mensajes)\b|\bleer\s+chat\b')
_TTS_OFF = re.compile(r'\b(?:desactiva\w*|apaga\w*|quita\w*|deshabilita\w*|deja\s+de|dejes\s+de|ya\s+no|no\s+leas|para\s+de|off)\b')
_TTS_STOP = re.compile(r'\b(?:calla\w*|callate|detente|silencia\w*|shh+)\b')
_TTS_STATUS = re.compile(r'\b(?:estado|status|config\w*)\b')


def tts_regex_intent(ctx):
    nn = M.normalize_for_compare(ctx.text)
    if len(nn.split()) > 16 or not _TTS_WORDS.search(nn):
        return None
    if _TTS_OFF.search(nn):
        action = 'tts_off'
    elif _TTS_STOP.search(nn):
        action = 'tts_stop'
    elif _TTS_STATUS.search(nn):
        action = 'tts_status'
    else:
        action = 'tts_on'
    mention = re.search(r'<#\d+>', ctx.text)
    return M.Intent(action=action, source='regex', explicit=True, channel=mention.group(0) if mention else None)


def install(module):
    global M, TTS_FILE, cfg
    M = module
    TTS_FILE = M.DATA_DIR / 'tts.json'
    loaded = M.load_json(TTS_FILE, {})
    cfg = loaded if isinstance(loaded, dict) else {}

    M.client.add_listener(on_message, 'on_message')
    M.client.tree.add_command(tts_group)

    # acciones de Jarvis (lenguaje natural)
    M.PLAIN_ACTIONS.update(TTS_ACTIONS)
    M.KNOWN_ACTIONS.update(TTS_ACTIONS)
    M.PLAIN_HANDLERS.update({
        'tts_on': act_tts_on, 'tts_off': act_tts_off, 'tts_status': act_tts_status, 'tts_stop': act_tts_stop,
    })
    M.ACTION_LABELS.update({
        'tts_on': 'Leer el chat en voz alta', 'tts_off': 'Dejar de leer el chat',
        'tts_status': 'Ver lectura del chat', 'tts_stop': 'Callar la lectura',
    })
    M.INTENT_SYSTEM_PROMPT += TTS_PROMPT

    orig_regex = M.regex_parse_intent

    def regex_wrapper(ctx):
        try:
            it = tts_regex_intent(ctx)
        except Exception as e:
            print(f'[TTS] patron: {e}')
            it = None
        return it if it is not None else orig_regex(ctx)

    M.regex_parse_intent = regex_wrapper

    # si piden musica mientras estoy leyendo, corto la lectura (la musica tiene prioridad)
    orig_music_play = M.music_play

    async def music_play_wrapper(guild, member, query, text_channel, play_next=False):
        await tts_halt(guild)
        for _ in range(10):
            st = states.get(guild.id)
            if not (st and st.playing):
                break
            await asyncio.sleep(0.2)
        return await orig_music_play(guild, member, query, text_channel, play_next)

    M.music_play = music_play_wrapper

    # ayuda
    orig_help = M.build_help_embed

    def help_wrapper():
        emb = orig_help()
        value = ('`/tts activar [canal]` o `jarvis lee el chat` - leo en voz alta lo que escriben los que estan en mi canal de voz\n'
                 '`/tts voz` `/tts velocidad` `/tts parar` `/tts estado`\n'
                 '`jarvis desactiva el tts` / `jarvis calla el tts`')
        if len(emb.fields) < 25 and len(emb) + len(value) + 30 < 5900:
            emb.add_field(name='Leer el chat en voz alta (TTS)', value=value, inline=False)
        return emb

    M.build_help_embed = help_wrapper

    print(f'[TTS] edge-tts={"OK" if edge_tts else "NO"} | gTTS={"OK" if gTTS else "NO"} | '
          f'ffmpeg={M.FFMPEG_EXE or "NO"} | pynacl={"OK" if M.NACL_OK else "NO"}')
