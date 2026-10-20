# -*- coding: utf-8 -*-
"""
jarvis_imagenes.py  -  Modulo de IMAGENES para el bot de Jarvis (generar con IA y enviar).

INSTALACION (2 lineas en tu archivo principal del bot):

  1) Deja este archivo en la misma carpeta que tu bot.
  2) En tu archivo principal, justo ANTES de la seccion  "# KEEPALIVE"  (o antes de  async def main), pon:

        import jarvis_imagenes
        jarvis_imagenes.install(sys.modules[__name__])

     (si no tienes `import sys` arriba del todo, agregalo).

Eso es todo: el modulo se encarga solo de
  - ensenarle al cerebro de Jarvis las acciones nuevas (image_generate / image_send),
  - agregar el atajo por patrones ("jarvis dibuja ...", "jarvis manda esta imagen ..."),
  - agregar los comandos /imagen y /enviar_imagen,
  - agregar la seccion "Imagenes" a la ayuda.

Variables de entorno (todas opcionales):
  IMAGE_PROVIDER        pollinations (defecto) | together
  POLLINATIONS_TOKEN    token de Pollinations (si te lo piden o quieres mas limite)
  POLLINATIONS_MODEL    flux (defecto), turbo...
  TOGETHER_API_KEY      key de Together AI (si usas together)
  TOGETHER_IMAGE_MODEL  black-forest-labs/FLUX.1-schnell (defecto)
  IMAGE_ENHANCE         1 = Jarvis mejora/traduce el prompt con Groq (defecto), 0 = no
  IMAGE_PUBLIC          1 = /imagen para cualquiera, 0 = solo whitelist (defecto)

Nota: los proveedores gratuitos cambian sus limites y condiciones seguido (Pollinations en particular
ha ido pidiendo token). Si falla, prueba IMAGE_PROVIDER=together con TOGETHER_API_KEY.
"""
import io
import os
import re
import time
import random
import asyncio
import base64
import urllib.parse
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands

# ----------------------------------------------------------------------------
# Configuracion
# ----------------------------------------------------------------------------
IMAGE_PROVIDER       = os.environ.get('IMAGE_PROVIDER', 'pollinations').strip().lower()
POLLINATIONS_TOKEN   = os.environ.get('POLLINATIONS_TOKEN', '').strip()
POLLINATIONS_MODEL   = os.environ.get('POLLINATIONS_MODEL', 'flux').strip()
TOGETHER_API_KEY     = os.environ.get('TOGETHER_API_KEY', '').strip()
TOGETHER_IMAGE_MODEL = os.environ.get('TOGETHER_IMAGE_MODEL', 'black-forest-labs/FLUX.1-schnell').strip()
IMAGE_ENHANCE        = os.environ.get('IMAGE_ENHANCE', '1').strip().lower() not in ('0', 'false', 'no', 'off')
IMAGE_PUBLIC         = os.environ.get('IMAGE_PUBLIC', '0').strip().lower() in ('1', 'true', 'si', 'yes', 'on')
IMAGE_COOLDOWN_SECS  = 15
IMAGE_MAX_BYTES      = 9 * 1024 * 1024
IMAGE_SIZES = {'cuadrada': (1024, 1024), 'horizontal': (1280, 720), 'vertical': (720, 1280)}

IMAGE_BLOCK_RE = re.compile(
    r'\b(?:porn\w*|nsfw|desnud\w*|nud[eo]s?|naked|hentai|sexual\w*|sexo|erotic\w*|gore|decapit\w*|lolit\w*|shota|'
    r'tet(?:a|as|ita|itas)|senos?|pezon\w*|culo|culos|verga|pene|penes|vagina\w*|boobs?|tits?|nipples?|'
    r'penis|dick|pussy|ass)\b',
    re.IGNORECASE,
)
IMAGE_ENHANCE_SYSTEM = (
    'You write prompts for an AI image generator. Translate the user request to English and expand it into one '
    'vivid, concrete prompt (subject, style, lighting, composition) under 60 words. Keep the user intent exactly. '
    'Reply with ONLY the prompt, no quotes, no explanations.'
)

# Texto que se agrega al cerebro (INTENT_SYSTEM_PROMPT)
BRAIN_ACTIONS_TEXT = (
    "- image_generate: GENERAR una imagen con IA. query (descripcion de la imagen, detallada y preferiblemente en ingles), "
    "kind (cuadrada|horizontal|vertical, opcional)\n"
    "- image_send: reenviar una imagen que ya existe. query (URL de la imagen), channel opcional (donde enviarla). "
    "Si el JEFE adjunto una imagen, no hace falta query\n"
)
BRAIN_RULE_TEXT = (
    "\n22. Si el JEFE pide dibujar, generar, crear o hacer una imagen/foto/arte/wallpaper de algo, es image_generate "
    "(NO create_channel ni chat). Si pasa un link o adjunto de una imagen existente para mandarla, es image_send."
)

HELP_FIELD_NAME = 'Imagenes'
HELP_FIELD_VALUE = (
    '`jarvis dibuja un gato astronauta` / `jarvis genera una imagen vertical de un bosque`\n'
    '`jarvis manda esta imagen <link>` (o adjuntala) `en #canal`\n'
    '`/imagen` `/enviar_imagen`'
)

NL_IMAGE = re.compile(
    r'^(?:dibuja(?:me)?|ilustra(?:me)?|pinta(?:me)?|'
    r'(?:genera|crea|haz|hazme|dame|quiero|manda|mandame|env[ií]a|env[ií]ame)(?:me)?\s+'
    r'(?:una?\s+|esta\s+|esa\s+|la\s+|las\s+|estas\s+)?'
    r'(?:imagen(?:es)?|foto|dibujo|ilustraci[oó]n|arte|wallpaper|fondo(?:\s+de\s+pantalla)?)'
    r'(?:\s+(?:de|del|sobre|con))?)\s*(.*)$',
    re.IGNORECASE | re.DOTALL,
)
NL_SEND_VERB = re.compile(r'^(?:manda|mandame|env[ií]a|env[ií]ame)', re.IGNORECASE)

# Nombres que se toman prestados del archivo principal del bot (los inyecta install()).
_NEEDED = (
    'client', 'Intent', 'JarvisError', 'execute_intent', 'ai_available', 'ask_groq', 'short_error',
    'normalize_for_compare', 'JARVIS_WHITELIST', 'OWNER_ID', 'PLAIN_ACTIONS', 'KNOWN_ACTIONS',
    'PLAIN_HANDLERS', 'ACTION_LABELS', 'pick_channel', 'simple_embed', 'clean_nl_query',
)

ImageError = None          # se crea en install() (hereda de JarvisError del bot)
_image_cooldown = {}
_image_sem = None
_installed = False


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def image_allowed(user_id):
    return IMAGE_PUBLIC or str(user_id) in JARVIS_WHITELIST


def image_check_cooldown(user_id):
    if str(user_id) == str(OWNER_ID):
        return
    now = time.time()
    left = IMAGE_COOLDOWN_SECS - (now - _image_cooldown.get(user_id, 0))
    if left > 0:
        raise ImageError(f'Espera {left:.0f}s antes de pedirme otra imagen.')
    _image_cooldown[user_id] = now


def detect_shape(text):
    t = normalize_for_compare(text or '')
    if re.search(r'\b(?:vertical|retrato|9\s*16|celular|portrait)\b', t):
        return 'vertical'
    if re.search(r'\b(?:horizontal|wallpaper|fondo\s+de\s+pantalla|panoramic\w*|16\s*9|banner|landscape)\b', t):
        return 'horizontal'
    return 'cuadrada'


async def enhance_image_prompt(prompt):
    if not IMAGE_ENHANCE or not ai_available():
        return prompt
    out = (await ask_groq(prompt, False, system=IMAGE_ENHANCE_SYSTEM, max_tokens=600,
                          low_effort=True, temperature=0.8) or '').strip().strip('"\'')
    if not out or out.startswith(('Error', 'No hay', 'Demasiadas', 'Sin respuesta')) or len(out) > 700:
        return prompt
    return out


# ----------------------------------------------------------------------------
# Proveedores
# ----------------------------------------------------------------------------
async def _img_pollinations(prompt, w, h):
    url = ('https://image.pollinations.ai/prompt/' + urllib.parse.quote(prompt[:700], safe='')
           + f'?width={w}&height={h}&model={urllib.parse.quote(POLLINATIONS_MODEL)}'
           f'&seed={random.randint(1, 10**9)}&nologo=true&safe=true')
    headers = {'Authorization': f'Bearer {POLLINATIONS_TOKEN}'} if POLLINATIONS_TOKEN else {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=100)) as s:
        async with s.get(url, headers=headers) as r:
            if r.status == 429:
                raise ImageError('El generador de imagenes esta saturado, prueba en un minuto.')
            if r.status in (401, 403):
                raise ImageError('Pollinations pide token. Configura `POLLINATIONS_TOKEN` o usa `IMAGE_PROVIDER=together`.')
            if r.status != 200:
                raise ImageError(f'El generador de imagenes respondio con error {r.status}.')
            ctype = (r.headers.get('content-type') or '').lower()
            if not ctype.startswith('image/'):
                raise ImageError('El generador no devolvio una imagen valida.')
            data = await r.read()
    ext = 'png' if 'png' in ctype else 'webp' if 'webp' in ctype else 'jpg'
    return data, ext


async def _img_together(prompt, w, h):
    if not TOGETHER_API_KEY:
        raise ImageError('Falta configurar `TOGETHER_API_KEY` para usar Together AI.')
    payload = {
        'model': TOGETHER_IMAGE_MODEL, 'prompt': prompt[:1500], 'width': w, 'height': h,
        'steps': 4, 'n': 1, 'response_format': 'b64_json',
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=100)) as s:
        async with s.post('https://api.together.xyz/v1/images/generations',
                          headers={'Authorization': f'Bearer {TOGETHER_API_KEY}'}, json=payload) as r:
            if r.status == 429:
                raise ImageError('Together AI esta limitando las peticiones, prueba en un minuto.')
            if r.status != 200:
                print(f'[Image] Together HTTP {r.status}: {(await r.text())[:300]}')
                raise ImageError(f'Together AI respondio con error {r.status}.')
            js = await r.json()
    try:
        return base64.b64decode(js['data'][0]['b64_json']), 'png'
    except Exception:
        raise ImageError('Together AI no devolvio una imagen valida.')


async def generate_image(prompt, shape='cuadrada'):
    global _image_sem
    if _image_sem is None:
        _image_sem = asyncio.Semaphore(2)     # maximo 2 generaciones a la vez
    w, h = IMAGE_SIZES.get(shape, IMAGE_SIZES['cuadrada'])
    try:
        async with _image_sem:
            if IMAGE_PROVIDER == 'together':
                data, ext = await _img_together(prompt, w, h)
            else:
                data, ext = await _img_pollinations(prompt, w, h)
    except ImageError:
        raise
    except asyncio.TimeoutError:
        raise ImageError('La imagen tardo demasiado en generarse. Intenta de nuevo.')
    except aiohttp.ClientError as e:
        raise ImageError(f'No pude contactar al generador de imagenes: {short_error(e)}')
    if len(data) > IMAGE_MAX_BYTES:
        raise ImageError('La imagen salio demasiado pesada para Discord, intenta de nuevo.')
    return data, ext


async def build_image_message(prompt, shape, requester):
    """Devuelve (embed, file) listos para enviar."""
    prompt = (prompt or '').strip()
    if not prompt:
        raise ImageError('Que quieres que dibuje?')
    if IMAGE_BLOCK_RE.search(normalize_for_compare(prompt)):
        raise ImageError('No voy a generar ese tipo de imagen.')
    final = await enhance_image_prompt(prompt)
    data, ext = await generate_image(final, shape)
    fname = f'jarvis_{int(time.time())}.{ext}'
    file = discord.File(io.BytesIO(data), filename=fname)
    embed = discord.Embed(description=discord.utils.escape_markdown(prompt)[:300], color=0x9b59b6)
    embed.set_image(url=f'attachment://{fname}')
    embed.set_footer(text=f'Pedida por {requester.display_name}')
    embed.timestamp = datetime.now(timezone.utc)
    return embed, file


# ----------------------------------------------------------------------------
# Handlers de Jarvis (acciones del cerebro)
# ----------------------------------------------------------------------------
async def act_image_generate(ctx, intent):
    prompt = (intent.query or intent.text or '').strip()
    if not prompt:
        raise ImageError('Que quieres que dibuje?')
    if not image_allowed(ctx.author.id):
        raise ImageError('No tienes permiso para generar imagenes.')
    image_check_cooldown(ctx.author.id)
    shape = intent.kind if intent.kind in IMAGE_SIZES else detect_shape(ctx.text)
    async with ctx.message.channel.typing():
        embed, file = await build_image_message(prompt, shape, ctx.author)
    try:
        await ctx.message.reply(embed=embed, file=file)
    except discord.HTTPException:
        await ctx.message.channel.send(embed=embed, file=file)
    return None


async def act_image_send(ctx, intent):
    if not image_allowed(ctx.author.id):
        raise ImageError('No tienes permiso para enviar imagenes.')
    dest = pick_channel(ctx, intent)
    atts = [a for a in ctx.message.attachments if (a.content_type or '').startswith('image/')]
    if atts:
        files = [await a.to_file() for a in atts[:4]]
        await dest.send(files=files, allowed_mentions=discord.AllowedMentions.none())
    else:
        m = re.search(r'https?://[^\s<>]+', intent.query or intent.text or '')
        if not m:
            raise ImageError('Pasame el link de la imagen o adjuntala.')
        embed = discord.Embed(color=0x9b59b6)
        embed.set_image(url=m.group(0).rstrip(').,'))
        await dest.send(embed=embed)
    if dest.id != ctx.message.channel.id:
        return simple_embed('Imagen enviada', f'Enviada en {dest.mention}.', 0x2ecc71)
    return None


# ----------------------------------------------------------------------------
# Atajo por patrones (sin IA)
# ----------------------------------------------------------------------------
async def handle_image_natural(ctx):
    """'jarvis dibuja un dragon' / 'jarvis manda esta imagen https://...'. Devuelve True si lo manejo."""
    text = ctx.text.strip()
    m = NL_IMAGE.match(text)
    if not m:
        return False
    rest = clean_nl_query(m.group(1))
    img_atts = [a for a in ctx.message.attachments if (a.content_type or '').startswith('image/')]
    has_url = bool(re.search(r'https?://\S+', rest))
    send_verb = bool(NL_SEND_VERB.match(text))

    if send_verb and (img_atts or has_url):
        intent = Intent(action='image_send', source='regex', query=rest, explicit=True)
        # "en #canal" al final: se lo pasamos a pick_channel
        cm = re.search(r'\ben\s+(<#\d+>|#[\w\-]+)\s*$', rest, re.IGNORECASE)
        if cm:
            intent.channel = cm.group(1)
            intent.query = rest[:cm.start()].strip()
    elif rest:
        intent = Intent(action='image_generate', source='regex', query=rest,
                        kind=detect_shape(text), explicit=True)
    else:
        return False
    return await execute_intent(ctx, intent)


# ----------------------------------------------------------------------------
# Slash commands
# ----------------------------------------------------------------------------
def _register_slash_commands():
    @client.tree.command(name='imagen', description='Genera una imagen con IA a partir de una descripcion')
    @app_commands.describe(descripcion='Que quieres ver (ej: un gato astronauta en Marte)', formato='Forma de la imagen')
    @app_commands.choices(formato=[
        app_commands.Choice(name='Cuadrada', value='cuadrada'),
        app_commands.Choice(name='Horizontal', value='horizontal'),
        app_commands.Choice(name='Vertical', value='vertical'),
    ])
    @app_commands.guild_only()
    async def slash_imagen(interaction: discord.Interaction, descripcion: str,
                           formato: app_commands.Choice[str] = None):
        if not image_allowed(interaction.user.id):
            return await interaction.response.send_message(
                'Solo la whitelist de Jarvis puede generar imagenes.', ephemeral=True)
        try:
            image_check_cooldown(interaction.user.id)
        except ImageError as e:
            return await interaction.response.send_message(str(e), ephemeral=True)
        await interaction.response.defer()
        try:
            embed, file = await build_image_message(
                descripcion, formato.value if formato else 'cuadrada', interaction.user)
        except ImageError as e:
            return await interaction.followup.send(str(e))
        except Exception as e:
            print(f'[Image] /imagen: {e}')
            return await interaction.followup.send(f'Error inesperado: {short_error(e)}')
        await interaction.followup.send(embed=embed, file=file)

    @client.tree.command(name='enviar_imagen', description='Envia una imagen como el bot (anonimo)')
    @app_commands.describe(imagen='Imagen a enviar', canal='Canal (por defecto el actual)', texto='Texto opcional')
    @app_commands.guild_only()
    async def slash_enviar_imagen(interaction: discord.Interaction, imagen: discord.Attachment,
                                  canal: discord.TextChannel = None, texto: str = None):
        if str(interaction.user.id) not in JARVIS_WHITELIST:
            return await interaction.response.send_message(
                'Solo la whitelist de Jarvis puede usar este comando.', ephemeral=True)
        if not (imagen.content_type or '').startswith('image/'):
            return await interaction.response.send_message('El archivo tiene que ser una imagen.', ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        dest = canal or interaction.channel
        try:
            await dest.send(content=texto, file=await imagen.to_file(),
                            allowed_mentions=discord.AllowedMentions.none())
            await interaction.followup.send(f'Imagen enviada en {dest.mention}.')
        except Exception as e:
            await interaction.followup.send(f'No pude enviarla: {short_error(e)}')


# ----------------------------------------------------------------------------
# Instalacion
# ----------------------------------------------------------------------------
def install(main):
    """Conecta este modulo al archivo principal del bot. `main` = sys.modules[__name__] del bot."""
    global _installed, ImageError
    if _installed:
        return
    _installed = True

    # 1) tomar prestados los nombres que necesito del bot
    missing = [n for n in _NEEDED if not hasattr(main, n)]
    if missing:
        raise RuntimeError('jarvis_imagenes: faltan estos nombres en el bot principal: ' + ', '.join(missing)
                           + '. Llama a install() DESPUES de definirlos (antes de la seccion KEEPALIVE).')
    g = globals()
    for n in _NEEDED:
        g[n] = getattr(main, n)

    # 2) excepcion propia (se muestra tal cual al usuario gracias a JarvisError)
    ImageError = type('ImageError', (JarvisError,), {'__doc__': 'Error de imagenes mostrado tal cual al usuario.'})

    # 3) acciones nuevas en el cerebro
    for name, handler, label in (
        ('image_generate', act_image_generate, 'Generar imagen'),
        ('image_send', act_image_send, 'Enviar imagen'),
    ):
        PLAIN_ACTIONS.add(name)
        KNOWN_ACTIONS.add(name)
        PLAIN_HANDLERS[name] = handler
        ACTION_LABELS[name] = label

    # 4) ensenarle al modelo las acciones (antes de la linea de "chat") y la regla 22
    prompt = main.INTENT_SYSTEM_PROMPT
    marker = '- chat: el JEFE solo conversa'
    if 'image_generate' not in prompt:
        if marker in prompt:
            prompt = prompt.replace(marker, BRAIN_ACTIONS_TEXT + marker, 1)
        else:
            print('[Imagenes] No encontre el punto de insercion en INTENT_SYSTEM_PROMPT; la IA no conocera las acciones de imagen.')
        prompt = prompt.rstrip() + BRAIN_RULE_TEXT
        main.INTENT_SYSTEM_PROMPT = prompt

    # 5) atajo por patrones antes de todo lo demas
    original_brain = main.jarvis_brain

    async def jarvis_brain_with_images(ctx):
        if await handle_image_natural(ctx):
            return True
        return await original_brain(ctx)

    main.jarvis_brain = jarvis_brain_with_images

    # 6) ayuda: agregar la seccion "Imagenes"
    original_help = main.build_help_embed

    def build_help_embed_with_images():
        embed = original_help()
        embed.add_field(name=HELP_FIELD_NAME, value=HELP_FIELD_VALUE, inline=False)
        return embed

    main.build_help_embed = build_help_embed_with_images

    # 7) slash commands (se sincronizan solos en on_ready)
    _register_slash_commands()
    print(f'[Imagenes] Listo | proveedor={IMAGE_PROVIDER} | mejora_prompt={"ON" if IMAGE_ENHANCE else "OFF"} | '
          f'publico={"SI" if IMAGE_PUBLIC else "solo whitelist"}')
