# -*- coding: utf-8 -*-
"""
jarvis_musica.py - Modulo de MUSICA para Jarvis.

Se instala igual que tus otros modulos (al final de bot.py):

    import jarvis_musica
    jarvis_musica.install(sys.modules[__name__])

DEPENDENCIAS (requirements.txt)
    discord.py[voice]>=2.7      # incluye PyNaCl y `davey` (Discord exige cifrado DAVE en voz desde marzo 2026)
    yt-dlp[default]             # `default` trae yt-dlp-ejs. Mantenlo actualizado.
    imageio-ffmpeg              # opcional: FFmpeg portatil si el host no tiene `ffmpeg`
  Ademas, para YouTube yt-dlp necesita un runtime de JavaScript en el host: Deno (recomendado) o Node 22+.

VARIABLES DE ENTORNO (todas opcionales)
    JARVIS_YT_COOKIES          ruta a un cookies.txt (formato Netscape). Ayuda cuando YouTube pide "confirma que no eres un bot".
    JARVIS_YT_PROXY            proxy para yt-dlp (ej: http://usuario:clave@host:puerto)
    JARVIS_YT_JS_RUNTIME       runtime JS: "node", "deno", o "node:/ruta/a/node"
    JARVIS_YT_REMOTE_COMPONENTS  ej: "ejs:github" (deja que yt-dlp descargue el solver de retos si falta)
    JARVIS_MUSIC_MAX_QUEUE     maximo de canciones en cola (200)
    JARVIS_MUSIC_MAX_PLAYLIST  maximo de canciones que se cargan de una playlist (50)
    JARVIS_MUSIC_MAX_MINUTES   rechaza canciones mas largas que esto (180; 0 = sin limite)

USO
    Slash:   /play /pause /resume /skip /stop /queue /nowplaying /volume /loop /shuffle /remove /clearqueue /musica
    Prefijo: >>play <cancion|link>  (>>p)  >>skip  >>pause  >>queue  >>volume 70  >>loop  ...
    Hablando (lo puede usar cualquiera, no solo la whitelist):
             jarvis pon despacito | jarvis toca <link> | jarvis pausa | jarvis salta | jarvis que suena
             jarvis volumen 70 | jarvis repite la cancion | jarvis mezcla la cola | jarvis para la musica
"""
import asyncio
import os
import random
import re
import shutil
import time
import traceback
from collections import deque
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands

_m = None   # modulo principal (se asigna en install)


# ============================================================================
# CONFIG
# ============================================================================
def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


MAX_QUEUE = _env_int('JARVIS_MUSIC_MAX_QUEUE', 200)
MAX_PLAYLIST = _env_int('JARVIS_MUSIC_MAX_PLAYLIST', 50)
MAX_DURATION = _env_int('JARVIS_MUSIC_MAX_MINUTES', 180) * 60
COOKIES = (os.environ.get('JARVIS_YT_COOKIES') or '').strip()
PROXY = (os.environ.get('JARVIS_YT_PROXY') or '').strip()
JS_RUNTIME = (os.environ.get('JARVIS_YT_JS_RUNTIME') or '').strip() or ('deno' if shutil.which('deno') else '')
REMOTE_COMPONENTS = [x.strip() for x in (os.environ.get('JARVIS_YT_REMOTE_COMPONENTS') or '').split(',') if x.strip()]

IDLE_LEAVE_SECS = 180        # sin nada en la cola: me salgo del canal
ALONE_LEAVE_SECS = 60        # solo en el canal de voz: me salgo
DEFAULT_VOLUME = 50
MAX_VOLUME = 150
STREAM_TTL = 15 * 60         # los links de stream caducan; se reutilizan hasta 15 min
COLOR = 0x1DB954

FFMPEG_BEFORE = '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin'
FFMPEG_OPTIONS = '-vn'

URL_RE = re.compile(r'^https?://\S+$', re.IGNORECASE)
BARE_DOMAIN_RE = re.compile(r'^(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be|soundcloud\.com)/', re.IGNORECASE)


# ============================================================================
# UTILIDADES
# ============================================================================
def _err(msg):
    return _m.JarvisError(msg)


def _embed(title, desc, color=COLOR):
    return _m.simple_embed(title, desc, color)


def _short(e):
    fn = getattr(_m, 'short_error', None)
    return fn(e) if fn else str(e)[:200]


def _trunc(s, n):
    s = str(s or '')
    return s if len(s) <= n else s[:n - 1] + '\u2026'


def _clean(s):
    return discord.utils.escape_markdown(str(s or '')).replace('[', '(').replace(']', ')')


def _fmt(sec):
    sec = int(max(0, sec or 0))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


def _bar(pos, dur, width=18):
    if not dur:
        return 'EN VIVO'
    i = min(width - 1, int(max(0, pos) / dur * width))
    return '\u25AC' * i + '\U0001F518' + '\u25AC' * (width - 1 - i)


def _link(t):
    title = _clean(_trunc(t.title, 60))
    return f'[{title}]({t.url})' if str(t.url).startswith('http') else f'**{title}**'


def _afk_entry(gid):
    return (getattr(_m, 'afk_data', None) or {}).get(str(gid))


def _afk_anti(gid):
    e = _afk_entry(gid)
    return bool(e and e.get('anti'))


def _user_channel(member):
    voice = getattr(member, 'voice', None)
    return voice.channel if voice and voice.channel else None


def _is_dj(member):
    fn = getattr(_m, 'ar_can_manage', None)
    try:
        if fn:
            return bool(fn(member))
    except Exception:
        pass
    return bool(member.guild_permissions.manage_guild)


def _humans(channel):
    return [x for x in channel.members if not x.bot]


# ---------------------------------------------------------------- ajustes por servidor --
_settings = {}


def _settings_file():
    return _m.DATA_DIR / 'music_settings.json'


def _guild_volume(gid):
    try:
        return max(0, min(int((_settings.get(str(gid)) or {}).get('volume', DEFAULT_VOLUME)), MAX_VOLUME))
    except (TypeError, ValueError):
        return DEFAULT_VOLUME


def _set_guild_volume(gid, vol):
    _settings.setdefault(str(gid), {})['volume'] = int(vol)
    _m.save_json(_settings_file(), _settings)


# ---------------------------------------------------------------- dependencias --
_yt = None
_deps_ok = False


def _load_ytdlp():
    global _yt
    if _yt is None:
        try:
            import yt_dlp
        except ImportError:
            raise _err('Me falta la libreria `yt-dlp` en el servidor del bot (`pip install -U "yt-dlp[default]"`).')
        _yt = yt_dlp
    return _yt


def _ffmpeg_exe():
    exe = shutil.which('ffmpeg')
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise _err('No encuentro FFmpeg en el servidor del bot. Instalalo en el host o agrega `imageio-ffmpeg` a requirements.txt.')


def _check_deps():
    global _deps_ok
    if _deps_ok:
        return
    if not getattr(getattr(discord, 'voice_client', None), 'has_nacl', False):
        raise _err('Me falta `PyNaCl` para conectarme a voz. Instala `discord.py[voice]`.')
    if tuple(discord.version_info)[:3] < (2, 7, 0):
        raise _err('Necesito discord.py 2.7 o superior: Discord exige el cifrado DAVE en voz desde marzo de 2026. '
                   'Actualiza con `pip install -U "discord.py[voice]"`.')
    try:
        import davey  # noqa: F401
    except ImportError:
        raise _err('Me falta el paquete `davey` (cifrado DAVE de voz). Instala `discord.py[voice]` y reinicia el bot.')
    _ffmpeg_exe()
    _load_ytdlp()
    _deps_ok = True


# ============================================================================
# yt-dlp
# ============================================================================
def _ydl_opts(flat=False, playlist=False, fmt='bestaudio/best'):
    opts = {
        'format': fmt,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': not playlist,
        'default_search': 'ytsearch',
        'socket_timeout': 15,
        'retries': 3,
        'extract_flat': 'in_playlist' if flat else False,
        'playlistend': MAX_PLAYLIST,
        'source_address': '0.0.0.0',      # fuerza IPv4 (el IPv6 de muchos hosts esta bloqueado)
    }
    if COOKIES and os.path.isfile(COOKIES):
        opts['cookiefile'] = COOKIES
    if PROXY:
        opts['proxy'] = PROXY
    if JS_RUNTIME:
        name, _, path = JS_RUNTIME.partition(':')
        opts['js_runtimes'] = {name.strip().lower(): ({'path': path.strip()} if path.strip() else {})}
    if REMOTE_COMPONENTS:
        opts['remote_components'] = REMOTE_COMPONENTS
    return opts


def _extract(query, flat=False, playlist=False, fmt='bestaudio/best'):
    yt = _load_ytdlp()
    with yt.YoutubeDL(_ydl_opts(flat, playlist, fmt)) as ydl:
        return ydl.extract_info(query, download=False)


async def _blocking(fn, *args, timeout=60):
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, fn, *args), timeout)
    except asyncio.TimeoutError:
        raise _err('Tardo demasiado en responder la busqueda. Intenta de nuevo en un momento.')


def _explain(e):
    """Traduce errores de yt-dlp a algo que el usuario entienda."""
    msg = re.sub(r'\x1b\[[0-9;]*m', '', str(e)).strip()
    low = msg.lower()
    if 'requested format' in low:
        return 'No encontre un formato de audio reproducible para eso.'
    if ('confirm' in low and 'bot' in low) or 'sign in to' in low:
        return ('YouTube esta bloqueando al servidor del bot ("confirma que no eres un bot"). '
                'Configura cookies con `JARVIS_YT_COOKIES` o cambia de host/IP.')
    if 'javascript runtime' in low or 'n challenge' in low or 'signature solving' in low:
        return 'Falta un runtime de JavaScript (Deno o Node 22+) en el servidor para que yt-dlp pueda leer YouTube.'
    if 'private video' in low:
        return 'Ese video es privado.'
    if 'age' in low and 'restrict' in low:
        return 'Ese video tiene restriccion de edad (hacen falta cookies de una cuenta).'
    if 'unsupported url' in low:
        return 'No se reproducir ese tipo de link.'
    if 'unavailable' in low or 'not available' in low:
        return 'Ese video no esta disponible (borrado, privado o bloqueado en la region del servidor).'
    return _trunc(msg.replace('ERROR: ', ''), 220)


# ============================================================================
# TRACKS
# ============================================================================
@dataclass
class Track:
    title: str
    url: str
    duration: Optional[int] = None
    requester_id: int = 0
    requester_name: str = ''
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None
    live: bool = False
    stream_url: Optional[str] = None
    resolved_at: float = 0.0
    retries: int = 0


def _page_url(info, flat):
    keys = ('url', 'webpage_url', 'original_url') if flat else ('webpage_url', 'original_url')
    for key in keys:
        v = info.get(key)
        if isinstance(v, str) and v.startswith('http'):
            return v
    vid = info.get('id')
    if vid and str(info.get('ie_key') or info.get('extractor_key') or '').lower().startswith('youtube'):
        return f'https://www.youtube.com/watch?v={vid}'
    return None


def _stream_url(info):
    u = info.get('url')
    if isinstance(u, str) and u.startswith('http'):
        return u
    for f in reversed(info.get('requested_formats') or info.get('formats') or []):
        if f.get('url') and f.get('acodec') != 'none':
            return f['url']
    return None


def _track_from_info(info, member, flat):
    if not isinstance(info, dict):
        return None
    page = _page_url(info, flat)
    if not page:
        return None
    thumb = info.get('thumbnail')
    if not thumb and isinstance(info.get('thumbnails'), list) and info['thumbnails']:
        thumb = (info['thumbnails'][-1] or {}).get('url')
    try:
        dur = int(info.get('duration')) if info.get('duration') else None
    except (TypeError, ValueError):
        dur = None
    t = Track(
        title=str(info.get('title') or 'Sin titulo'), url=page, duration=dur,
        requester_id=member.id, requester_name=member.display_name,
        thumbnail=thumb if isinstance(thumb, str) and thumb.startswith('http') else None,
        uploader=info.get('uploader') or info.get('channel'),
        live=bool(info.get('is_live')),
    )
    if not flat:
        su = _stream_url(info)
        if su:
            t.stream_url, t.resolved_at = su, time.time()
    return t


async def resolve_query(query, member):
    """Devuelve (lista_de_tracks, cuantas_se_saltaron_por_largas)."""
    q = query.strip()
    if re.search(r'open\.spotify\.com|spotify:', q, re.IGNORECASE):
        raise _err('Spotify no se puede reproducir directo (esta protegido). Pasame el nombre de la cancion o un link de YouTube/SoundCloud.')
    if BARE_DOMAIN_RE.match(q):
        q = 'https://' + q

    is_url = bool(URL_RE.match(q))
    low = q.lower()
    is_playlist = is_url and (
        ('list=' in low and 'watch?v=' not in low and 'youtu.be/' not in low)
        or '/playlist?' in low or '/sets/' in low
    )
    try:
        if is_playlist:
            info = await _blocking(_extract, q, True, True, timeout=90)
        else:
            target = q if is_url else f'ytsearch1:{q}'
            try:
                info = await _blocking(_extract, target, False, False)
            except _m.JarvisError:
                raise
            except Exception as e:
                if 'requested format' in str(e).lower():
                    info = await _blocking(_extract, target, False, False, 'best')
                else:
                    raise
    except _m.JarvisError:
        raise
    except Exception as e:
        raise _err(_explain(e))

    if not info:
        raise _err(f'No encontre nada para `{_trunc(q, 80)}`.')

    entries = [x for x in (info.get('entries') or []) if x] if ('entries' in info) else None
    if entries is not None and not is_playlist:
        entries = entries[:1]
    raw = entries if entries is not None else [info]
    flat = is_playlist
    tracks, skipped = [], 0
    for e in raw[:MAX_PLAYLIST if is_playlist else 1]:
        t = _track_from_info(e, member, flat)
        if t is None:
            continue
        if MAX_DURATION and t.duration and t.duration > MAX_DURATION:
            skipped += 1
            continue
        tracks.append(t)
    if not tracks:
        if skipped:
            raise _err(f'Esa cancion dura mas de {MAX_DURATION // 60} minutos y no la reproduzco.')
        raise _err(f'No encontre nada para `{_trunc(q, 80)}`.')
    return tracks, skipped


async def resolve_stream(track):
    """Obtiene (o renueva) el link de audio directo justo antes de reproducir."""
    if track.stream_url and time.time() - track.resolved_at < STREAM_TTL:
        return
    try:
        try:
            info = await _blocking(_extract, track.url, False, False)
        except _m.JarvisError:
            raise
        except Exception as e:
            if 'requested format' in str(e).lower():
                info = await _blocking(_extract, track.url, False, False, 'best')
            else:
                raise
    except _m.JarvisError:
        raise
    except Exception as e:
        raise _err(_explain(e))
    if info and info.get('entries'):
        info = next((x for x in info['entries'] if x), None)
    url = _stream_url(info or {})
    if not url:
        raise _err('No encontre un audio reproducible en ese enlace.')
    track.stream_url, track.resolved_at = url, time.time()
    track.live = bool(info.get('is_live'))
    if not track.duration and info.get('duration'):
        try:
            track.duration = int(info['duration'])
        except (TypeError, ValueError):
            pass


# ============================================================================
# REPRODUCTOR POR SERVIDOR
# ============================================================================
players = {}   # guildId -> Player


class Player:
    def __init__(self, guild):
        self.guild_id = guild.id
        self.queue = deque()
        self.current = None
        self.loop = 'off'                       # off | track | queue
        self.volume = _guild_volume(guild.id) / 100
        self.text_channel_id = None
        self.task = None
        self.wake = asyncio.Event()             # se agrego algo a la cola
        self.finished = asyncio.Event()         # termino la cancion actual
        self.skip_flag = False
        self.requeue_current = False            # me sacaron de voz (anti-disconnect): repetir la cancion al volver
        self.started_at = 0.0
        self.paused_at = None
        self.paused_total = 0.0
        self.np_message = None
        self.error_streak = 0
        self.alone_task = None
        self.closed = False

    # -- estado --
    def elapsed(self):
        if not self.started_at:
            return 0.0
        end = self.paused_at or time.time()
        return max(0.0, end - self.started_at - self.paused_total)

    def start(self):
        if self.task is None or self.task.done():
            self.task = _m.spawn(self._run())

    def add(self, tracks):
        self.queue.extend(tracks)
        self.wake.set()

    # -- mensajes --
    def _text_channel(self):
        guild = _m.client.get_guild(self.guild_id)
        if guild is None or not self.text_channel_id:
            return None
        return guild.get_channel_or_thread(self.text_channel_id)

    async def _say(self, text):
        ch = self._text_channel()
        if ch is None:
            return
        try:
            await ch.send(embed=_embed('Musica', text))
        except Exception as e:
            print(f'[Music] No pude escribir en el canal: {e}')

    async def _announce(self, track):
        ch = self._text_channel()
        if ch is None:
            return
        try:
            self.np_message = await ch.send(embed=np_embed(self, track), view=MusicControls(self.guild_id, track))
        except Exception as e:
            print(f'[Music] No pude enviar la tarjeta de reproduccion: {e}')

    async def _clear_np(self):
        msg, self.np_message = self.np_message, None
        if msg is not None:
            try:
                await msg.delete()
            except Exception:
                pass

    # -- bucle principal --
    async def _take_next(self):
        while not self.queue:
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), IDLE_LEAVE_SECS if not _afk_entry(self.guild_id) else None)
            except asyncio.TimeoutError:
                return None
            if self.closed:
                return None
        return self.queue.popleft()

    async def _wait_vc(self, guild):
        for _ in range(20):
            vc = guild.voice_client
            if vc is not None and vc.is_connected():
                return vc
            if not _afk_anti(guild.id):
                return None
            await asyncio.sleep(1)      # el anti-disconnect esta reconectando
        return None

    async def _play_track(self, track):
        guild = _m.client.get_guild(self.guild_id)
        if guild is None:
            self.closed = True
            return False
        vc = await self._wait_vc(guild)
        if vc is None:
            await self._say('Perdi la conexion de voz, asi que detengo la musica.')
            self.closed = True
            return False
        try:
            await resolve_stream(track)
        except _m.JarvisError as e:
            await self._say(f'No pude reproducir **{_clean(track.title)}**: {e}')
            return False
        src = discord.FFmpegPCMAudio(
            track.stream_url, executable=_ffmpeg_exe(),
            before_options=FFMPEG_BEFORE, options=FFMPEG_OPTIONS,
        )
        src = discord.PCMVolumeTransformer(src, volume=self.volume)
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        self.finished.clear()
        loop = asyncio.get_running_loop()

        def _after(err):
            loop.call_soon_threadsafe(self._source_done, err)

        vc.play(src, after=_after)
        self.started_at, self.paused_at, self.paused_total = time.time(), None, 0.0
        await self._announce(track)
        return True

    def _source_done(self, err):
        if err:
            print(f'[Music] Error de reproduccion: {err}')
        self.finished.set()

    def _finish_track(self, track, ok):
        if self.requeue_current and not self.skip_flag:
            self.queue.appendleft(track)
            return
        if not ok:
            return
        if self.loop == 'queue':
            self.queue.append(track)
        elif self.loop == 'track' and not self.skip_flag:
            self.queue.appendleft(track)

    async def _run(self):
        try:
            while not self.closed:
                track = await self._take_next()
                if track is None:
                    break
                self.current = track
                self.skip_flag = False
                self.requeue_current = False
                outcome = 'fail'
                try:
                    if await self._play_track(track):
                        outcome = 'ok'
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    traceback.print_exc()
                    await self._say(f'No pude reproducir **{_clean(track.title)}**: {_explain(e)}')
                if self.closed:
                    break

                if outcome == 'ok':
                    await self.finished.wait()
                    ran = time.time() - self.started_at - self.paused_total
                    if (not self.requeue_current and not self.skip_flag and ran < 3
                            and (track.duration or 99) > 10 and not track.live):
                        # el stream se corto al instante: reintento una vez con un link nuevo
                        if track.retries < 1:
                            track.retries += 1
                            track.stream_url = None
                            self.queue.appendleft(track)
                            outcome = 'retry'
                        else:
                            outcome = 'fail'
                            await self._say(f'**{_clean(track.title)}** se corto al instante (fallo el stream). Paso a la siguiente.')

                if outcome == 'ok':
                    self.error_streak = 0
                elif outcome == 'fail':
                    self.error_streak += 1
                    if self.error_streak >= 3 and self.queue:
                        self.queue.clear()
                        self.error_streak = 0
                        await self._say('Fallaron 3 canciones seguidas, asi que limpie la cola. Si pasa con todo, revisa '
                                        'yt-dlp, el runtime de JavaScript (Deno/Node) y las cookies de YouTube.')
                self._finish_track(track, outcome == 'ok')
                self.current = None
                await self._clear_np()
                if outcome == 'ok' and not self.queue and self.loop == 'off' and not self.requeue_current:
                    await self._say('Se acabo la cola. Pide mas con `/play`.'
                                    + ('' if _afk_entry(self.guild_id) else ' Me salgo en 3 min si no hay nada.'))
        finally:
            self.current = None
            await self.shutdown()

    async def shutdown(self, disconnect=True):
        if self.closed and players.get(self.guild_id) is not self:
            return
        self.closed = True
        if players.get(self.guild_id) is self:
            players.pop(self.guild_id, None)
        if self.alone_task is not None and not self.alone_task.done():
            self.alone_task.cancel()
        if self.task is not None and self.task is not asyncio.current_task() and not self.task.done():
            self.task.cancel()
        self.wake.set()
        guild = _m.client.get_guild(self.guild_id)
        vc = guild.voice_client if guild else None
        if vc is not None:
            try:
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
            except Exception:
                pass
            if disconnect and not _afk_entry(self.guild_id):
                try:
                    await vc.disconnect(force=True)
                except Exception as e:
                    print(f'[Music] No pude desconectar: {e}')
        await self._clear_np()


def get_player(guild):
    p = players.get(guild.id)
    if p is None or p.closed:
        p = Player(guild)
        players[guild.id] = p
    return p


# ============================================================================
# EMBEDS
# ============================================================================
def np_embed(player, track, with_bar=False):
    paused = player.paused_at is not None
    e = discord.Embed(title=_trunc(track.title, 250), color=COLOR)
    if str(track.url).startswith('http'):
        e.url = track.url
    e.set_author(name='En pausa' if paused else 'Reproduciendo ahora')
    if track.live:
        e.description = 'EN VIVO'
    elif track.duration:
        if with_bar:
            e.description = f'`{_fmt(player.elapsed())}` {_bar(player.elapsed(), track.duration)} `{_fmt(track.duration)}`'
        else:
            e.description = f'Duracion: `{_fmt(track.duration)}`'
    e.add_field(name='Pedida por', value=_trunc(_clean(track.requester_name), 100) or '?', inline=True)
    e.add_field(name='Volumen', value=f'{int(round(player.volume * 100))}%', inline=True)
    e.add_field(name='Repetir', value={'off': 'No', 'track': 'Cancion', 'queue': 'Cola'}[player.loop], inline=True)
    e.add_field(name='En cola', value=str(len(player.queue)), inline=True)
    if track.uploader:
        e.set_footer(text=_trunc(track.uploader, 100))
    if track.thumbnail:
        e.set_thumbnail(url=track.thumbnail)
    return e


# ============================================================================
# CONTROLES (botones en la tarjeta de "Reproduciendo ahora")
# ============================================================================
class MusicControls(discord.ui.View):
    def __init__(self, guild_id, track):
        super().__init__(timeout=min(max((track.duration or 3600) + 600, 900), 4 * 3600))
        self.guild_id = guild_id

    async def interaction_check(self, interaction):
        try:
            _require_control(interaction.guild, interaction.user)
        except _m.JarvisError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction):
        player = players.get(self.guild_id)
        if player is None or player.current is None:
            return await interaction.response.send_message('Ya no hay nada sonando.', ephemeral=True)
        await interaction.response.edit_message(embed=np_embed(player, player.current), view=self)

    async def _run(self, interaction, fn, refresh):
        try:
            result = await fn()
        except _m.JarvisError as e:
            return await interaction.response.send_message(str(e), ephemeral=True)
        except Exception as e:
            print(f'[Music] boton: {e}')
            return await interaction.response.send_message(f'Error: {_short(e)}', ephemeral=True)
        if refresh:
            return await self._refresh(interaction)
        await interaction.response.send_message(embed=result, ephemeral=True)

    @discord.ui.button(emoji='\u23EF\uFE0F', style=discord.ButtonStyle.primary)
    async def b_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        fn = do_resume if (vc and vc.is_paused()) else do_pause
        await self._run(interaction, lambda: fn(interaction.guild, interaction.user), True)

    @discord.ui.button(emoji='\u23ED\uFE0F', style=discord.ButtonStyle.secondary)
    async def b_skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run(interaction, lambda: do_skip(interaction.guild, interaction.user), False)

    @discord.ui.button(emoji='\u23F9\uFE0F', style=discord.ButtonStyle.danger)
    async def b_stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run(interaction, lambda: do_stop(interaction.guild, interaction.user), False)

    @discord.ui.button(emoji='\U0001F501', style=discord.ButtonStyle.secondary)
    async def b_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run(interaction, lambda: do_loop(interaction.guild, interaction.user, ''), True)

    @discord.ui.button(emoji='\U0001F500', style=discord.ButtonStyle.secondary)
    async def b_shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run(interaction, lambda: do_shuffle(interaction.guild, interaction.user), False)


# ============================================================================
# ACCIONES (las usan slash, prefijo y lenguaje natural; devuelven un Embed)
# ============================================================================
def _require_control(guild, member):
    """El bot tiene que estar en voz y quien pide tiene que estar en ese mismo canal (o ser DJ)."""
    vc = guild.voice_client
    if vc is None or not vc.is_connected() or vc.channel is None:
        raise _err('No estoy conectado a ningun canal de voz.')
    mc = _user_channel(member)
    if (mc is None or mc.id != vc.channel.id) and not _is_dj(member):
        raise _err(f'Tienes que estar en {vc.channel.mention} para controlar la musica.')
    return vc, players.get(guild.id)


def _need_playing(guild, member):
    vc, player = _require_control(guild, member)
    if player is None or player.current is None:
        raise _err('No hay nada sonando ahora mismo.')
    return vc, player


def _check_join(guild, ch):
    me = guild.me
    perms = ch.permissions_for(me)
    if not (perms.view_channel and perms.connect):
        raise _err(f'No tengo permiso para entrar a {ch.mention}.')
    if not isinstance(ch, discord.StageChannel) and not perms.speak:
        raise _err(f'No tengo permiso para hablar (Speak) en {ch.mention}.')
    if ch.user_limit and len(ch.members) >= ch.user_limit and me not in ch.members and not perms.move_members:
        raise _err(f'{ch.mention} esta lleno.')
    vc = guild.voice_client
    player = players.get(guild.id)
    busy = player is not None and (player.current is not None or bool(player.queue))
    if busy and vc is not None and vc.is_connected() and vc.channel and vc.channel.id != ch.id:
        raise _err(f'Ya estoy reproduciendo musica en {vc.channel.mention}. Entra ahi para pedir canciones.')
    afk = _afk_entry(guild.id)
    if afk and afk.get('anti') and int(afk['channel']) != ch.id:
        ac = guild.get_channel(int(afk['channel']))
        raise _err(f'Estoy en modo AFK con anti-disconnect en {ac.mention if ac else "otro canal"}. '
                   'Entra ahi para pedirme musica, o quita el modo AFK con `/disconnect`.')


async def _join(guild, ch):
    fn = getattr(_m, '_afk_join', None)     # reutiliza la conexion rapida del modulo AFK
    try:
        if fn:
            return await fn(guild, ch)
        vc = guild.voice_client
        if vc is not None and vc.is_connected():
            if vc.channel.id != ch.id:
                await vc.move_to(ch)
            return vc
        return await ch.connect(timeout=10, reconnect=True, self_deaf=True)
    except Exception as e:
        raise _err(f'No pude conectarme a {ch.mention}: {_short(e)}')


async def do_play(guild, member, query, text_channel):
    query = (query or '').strip().strip('<>').strip()
    if not query:
        raise _err('Dime que quieres escuchar: el nombre de una cancion o un link.')
    ch = _user_channel(member)
    if ch is None:
        raise _err('Entra primero a un canal de voz y vuelve a pedirmelo.')
    _check_deps()
    _check_join(guild, ch)

    tracks, skipped = await resolve_query(query, member)

    await _join(guild, ch)
    afk = _afk_entry(guild.id)
    if afk and not afk.get('anti') and int(afk.get('channel', 0)) != ch.id:
        afk['channel'] = ch.id              # el modo AFK sin anti-disconnect sigue al bot
        _m._afk_save()

    player = get_player(guild)
    room = MAX_QUEUE - len(player.queue)
    if room <= 0:
        raise _err(f'La cola esta llena ({MAX_QUEUE} canciones).')
    trimmed = len(tracks) > room
    tracks = tracks[:room]

    busy = player.current is not None or bool(player.queue)
    ahead = len(player.queue)
    eta = None
    if busy and player.current is not None:
        pend = [t.duration for t in player.queue]
        if player.current.duration and all(pend):
            eta = max(0, player.current.duration - player.elapsed()) + sum(pend)

    player.text_channel_id = text_channel.id
    player.add(tracks)
    player.start()

    notes = []
    if skipped:
        notes.append(f'{skipped} cancion(es) omitidas por durar mas de {MAX_DURATION // 60} min.')
    if trimmed:
        notes.append(f'La cola solo admite {MAX_QUEUE}; el resto no se agrego.')
    extra = ('\n' + ' '.join(notes)) if notes else ''

    if not busy:
        more = f'\n(+{len(tracks) - 1} mas en la cola)' if len(tracks) > 1 else ''
        return _embed('Cargando...', f'{_link(tracks[0])}{more}{extra}')
    if len(tracks) == 1:
        t = tracks[0]
        when = f'\nEmpieza en ~{_fmt(eta)}' if eta is not None else ''
        e = _embed('Agregada a la cola', f'{_link(t)}\nPosicion: **#{ahead + 1}**{when}{extra}')
        if t.thumbnail:
            e.set_thumbnail(url=t.thumbnail)
        return e
    return _embed('Playlist agregada', f'Se agregaron **{len(tracks)}** canciones a la cola (desde la posicion #{ahead + 1}).{extra}')


async def do_pause(guild, member):
    vc, player = _need_playing(guild, member)
    if vc.is_paused():
        raise _err('Ya esta en pausa.')
    if not vc.is_playing():
        raise _err('No hay nada sonando ahora mismo.')
    vc.pause()
    player.paused_at = time.time()
    return _embed('Pausado', f'{_link(player.current)}\nUsa `/resume` para continuar.')


async def do_resume(guild, member):
    vc, player = _need_playing(guild, member)
    if not vc.is_paused():
        raise _err('La musica no esta en pausa.')
    vc.resume()
    if player.paused_at is not None:
        player.paused_total += time.time() - player.paused_at
        player.paused_at = None
    return _embed('Reanudado', f'{_link(player.current)}')


async def do_skip(guild, member):
    vc, player = _need_playing(guild, member)
    title = player.current.title
    player.skip_flag = True
    vc.stop()
    nxt = f'\nSigue: {_link(player.queue[0])}' if player.queue else '\nNo hay mas canciones en la cola.'
    return _embed('Saltada', f'**{_clean(_trunc(title, 80))}**{nxt}')


async def do_stop(guild, member):
    vc, player = _require_control(guild, member)
    if player is None:
        raise _err('No estoy reproduciendo nada.')
    await player.shutdown(disconnect=True)
    stay = ' Me quedo en el canal porque el modo AFK esta activo.' if _afk_entry(guild.id) else ''
    return _embed('Musica detenida', f'Cola borrada.{stay}')


async def do_queue(guild, member):
    player = players.get(guild.id)
    if player is None or (player.current is None and not player.queue):
        return _embed('Cola de musica', 'No hay nada en la cola. Pide algo con `/play`.')
    lines = []
    if player.current is not None:
        c = player.current
        total = _fmt(c.duration) if c.duration else 'en vivo'
        lines.append(f'**Sonando:** {_link(c)} `{_fmt(player.elapsed())}/{total}`')
    items = list(player.queue)
    for i, t in enumerate(items[:10], 1):
        lines.append(f'`{i}.` {_link(t)} `{_fmt(t.duration) if t.duration else "?"}` - {_clean(_trunc(t.requester_name, 24))}')
    if len(items) > 10:
        lines.append(f'... y {len(items) - 10} mas')
    e = _embed(f'Cola de musica ({len(items)})', '\n'.join(lines)[:4000])
    known = [t.duration for t in items if t.duration]
    foot = [f'Repetir: {({"off": "no", "track": "cancion", "queue": "cola"})[player.loop]}',
            f'Volumen: {int(round(player.volume * 100))}%']
    if known:
        foot.append(f'Tiempo en cola: {_fmt(sum(known))}')
    e.set_footer(text=' | '.join(foot))
    return e


async def do_np(guild, member):
    player = players.get(guild.id)
    if player is None or player.current is None:
        raise _err('No hay nada sonando ahora mismo.')
    return np_embed(player, player.current, with_bar=True)


async def do_volume(guild, member, arg):
    a = (arg or '').replace('%', '').strip()
    if not a:
        return _embed('Volumen', f'El volumen esta en **{_guild_volume(guild.id)}%**.\nUsa `/volume 0-{MAX_VOLUME}` para cambiarlo.')
    vc, player = _require_control(guild, member)
    cur = int(round((player.volume if player else _guild_volume(guild.id) / 100) * 100))
    m = re.fullmatch(r'([+-])\s*(\d{1,3})', a)
    if m:
        vol = cur + int(m.group(2)) * (1 if m.group(1) == '+' else -1)
    elif a.isdigit():
        vol = int(a)
    else:
        raise _err(f'Dime un numero de 0 a {MAX_VOLUME}.')
    vol = max(0, min(vol, MAX_VOLUME))
    _set_guild_volume(guild.id, vol)
    if player is not None:
        player.volume = vol / 100
    src = vc.source
    if isinstance(src, discord.PCMVolumeTransformer):
        src.volume = vol / 100
    warn = '\nOjo: por encima de 100% puede distorsionar.' if vol > 100 else ''
    return _embed('Volumen', f'Volumen en **{vol}%**.{warn}')


_LOOP_ALIASES = {
    'off': 'off', 'no': 'off', 'apagar': 'off', 'apagado': 'off', 'desactivar': 'off', 'ninguno': 'off',
    'track': 'track', 'song': 'track', 'cancion': 'track', 'tema': 'track', 'una': 'track', 'uno': 'track', '1': 'track',
    'queue': 'queue', 'cola': 'queue', 'todo': 'queue', 'todas': 'queue', 'all': 'queue', 'lista': 'queue', 'playlist': 'queue',
}


async def do_loop(guild, member, mode):
    vc, player = _require_control(guild, member)
    if player is None:
        raise _err('No estoy reproduciendo nada.')
    key = _m.normalize_for_compare(mode or '')
    if key:
        new = _LOOP_ALIASES.get(key)
        if new is None:
            raise _err('Modos de repeticion: `off`, `cancion` o `cola`.')
    else:
        new = {'off': 'track', 'track': 'queue', 'queue': 'off'}[player.loop]
    player.loop = new
    label = {'off': 'Repeticion apagada.', 'track': 'Repitiendo la cancion actual.', 'queue': 'Repitiendo toda la cola.'}[new]
    return _embed('Repetir', label)


async def do_shuffle(guild, member):
    vc, player = _require_control(guild, member)
    if player is None or len(player.queue) < 2:
        raise _err('Necesito al menos 2 canciones en la cola para mezclarla.')
    items = list(player.queue)
    random.shuffle(items)
    player.queue = deque(items)
    return _embed('Cola mezclada', f'Mezcle **{len(items)}** canciones.')


async def do_remove(guild, member, arg):
    vc, player = _require_control(guild, member)
    a = (arg or '').strip().lstrip('#')
    if player is None or not player.queue:
        raise _err('La cola esta vacia.')
    if not a.isdigit() or not (1 <= int(a) <= len(player.queue)):
        raise _err(f'Dime una posicion entre 1 y {len(player.queue)} (mira `/queue`).')
    items = list(player.queue)
    t = items.pop(int(a) - 1)
    player.queue = deque(items)
    return _embed('Quitada de la cola', f'**{_clean(_trunc(t.title, 80))}**')


async def do_clear(guild, member):
    vc, player = _require_control(guild, member)
    if player is None or not player.queue:
        raise _err('La cola ya esta vacia.')
    n = len(player.queue)
    player.queue.clear()
    return _embed('Cola vaciada', f'Quite **{n}** cancion(es). La actual sigue sonando.')


def music_help_embed():
    e = _embed('Musica de Jarvis', 'Pide canciones por nombre o con link (YouTube, SoundCloud...). Tienes que estar en un canal de voz.')
    e.add_field(
        name='Slash',
        value='`/play` `/pause` `/resume` `/skip` `/stop` `/queue` `/nowplaying`\n`/volume` `/loop` `/shuffle` `/remove` `/clearqueue`',
        inline=False)
    e.add_field(
        name='Prefijo',
        value='`>>play <cancion o link>` (`>>p`) | `>>skip` | `>>pause` | `>>resume` | `>>stop` | `>>queue`\n'
              '`>>np` | `>>volume 70` | `>>loop [off|cancion|cola]` | `>>shuffle` | `>>remove 3` | `>>clearqueue`',
        inline=False)
    e.add_field(
        name='Hablando (cualquiera puede)',
        value='`jarvis pon despacito` | `jarvis toca <link>` | `jarvis pausa` | `jarvis continua` | `jarvis salta`\n'
              '`jarvis que suena` | `jarvis volumen 70` | `jarvis repite la cancion` | `jarvis mezcla la cola`\n'
              '`jarvis para la musica` | `jarvis borra la cancion 3`',
        inline=False)
    e.add_field(
        name='Notas',
        value='Los botones de la tarjeta "Reproduciendo ahora" tambien controlan la musica. '
              'Si el modo AFK esta activo, no me salgo del canal al terminar. Spotify no se puede reproducir directo.',
        inline=False)
    return e


async def do_help(guild, member):
    return music_help_embed()


# ============================================================================
# ENTRADA: lenguaje natural ("jarvis pon ...") y prefijo (">>play ...")
# ============================================================================
PLAY_VERBS = {'pon', 'pone', 'ponme', 'poneme', 'ponnos', 'ponla', 'ponlo',
              'toca', 'tocame', 'tocanos', 'reproduce', 'reproduceme', 'play', 'playea'}
AMBIGUOUS_VERBS = {'pon', 'pone', 'ponme', 'poneme', 'ponnos', 'ponla', 'ponlo'}   # chocan con "ponme el rol...", "pon slowmode..."
# si despues de "pon..." aparece algo de moderacion/gestion, NO es musica (lo atiende el resto de Jarvis)
EXCLUDE_PON = re.compile(
    r'\b(?:slowmode|modo\s*lento|timeout|quarantine|cuarentena|nick|apodo|nombre|rol|roles|whitelist|blacklist|anti|'
    r'lockdown|mute|silencio|advertencia|bloqueo|canal|canales|servidor|server|permiso|permisos|logs?|invitacion|'
    r'avatar|foto|color|nivel|xp|categoria|encuesta|sorteo|recordatorio|volumen|volume)\b')
FILLER_RE = re.compile(
    r'^(?:(?:la|el|una|un|esa|ese|esta|este)\s+)?(?:cancion|musica|tema|rola|video|cumbia|temazo|cancioncita)\s+'
    r'(?:de\s+|del\s+|llamada\s+|llamado\s+|que\s+se\s+llama\s+)?|^la\s+de\s+', re.IGNORECASE)
GENERIC_QUERIES = {'musica', 'cancion', 'una cancion', 'algo', 'algo de musica', 'un tema', 'tema', 'rola', 'una rola'}

NL_PATTERNS = [
    ('pause', re.compile(r'(?:pausa|pausar|pausala|pausalo|pause)(?:\s+(?:la\s+)?(?:musica|cancion|tema))?')),
    ('resume', re.compile(r'(?:continua|continuar|reanuda|reanudar|resume|sigue|despausa|despausar)(?:\s+(?:con\s+)?(?:la\s+)?(?:musica|cancion|tema))?')),
    ('skip', re.compile(r'(?:salta|saltar|saltala|saltalo|skip|siguiente|next|pasa\s+(?:a\s+)?la\s+siguiente|cambia\s+(?:de\s+)?cancion)'
                        r'(?:\s+(?:la\s+)?(?:musica|cancion|tema))?')),
    ('stop', re.compile(r'(?:(?:para|detene|deten|apaga|quita|termina|corta|stop)\s+(?:la\s+|el\s+)?(?:musica|reproductor|cancion|tema)|stop|detente|para\s+todo)')),
    ('queue', re.compile(r'(?:cola|la\s+cola|queue|que\s+hay\s+en\s+la\s+cola|ver\s+(?:la\s+)?cola|muestra\w*\s+(?:la\s+)?cola|lista\s+de\s+reproduccion)')),
    ('np', re.compile(r'(?:que\s+(?:suena|esta\s+sonando|estas\s+tocando|suena\s+ahora)|que\s+cancion\s+es(?:\s+esta)?|nowplaying|now\s+playing|cual\s+es\s+esta\s+cancion)')),
    ('shuffle', re.compile(r'(?:mezcla|mezclar|revuelve|shuffle|aleatorio)(?:\s+(?:la\s+)?(?:cola|lista|musica))?')),
    ('clear', re.compile(r'(?:limpia|vacia|borra)\s+(?:la\s+)?cola')),
    ('help', re.compile(r'(?:ayuda|comandos|help)\s+(?:de\s+)?(?:la\s+)?musica|que\s+puedes\s+hacer\s+con\s+(?:la\s+)?musica|comandos\s+de\s+musica')),
]
LOOP_OFF_RE = re.compile(r'no\s+repitas?|(?:desactiva|quita|apaga)\s+(?:el\s+)?(?:loop|bucle|repeticion)|loop\s+off|sin\s+repetir')
LOOP_QUEUE_RE = re.compile(r'(?:repite|repetir|loop|bucle)\s+(?:toda\s+)?(?:la\s+)?(?:cola|lista|playlist|todo)')
LOOP_TRACK_RE = re.compile(r'(?:repite|repetir|loop|bucle)(?:\s+(?:la\s+)?(?:cancion|tema|esta|musica))?')
VOL_SET_RE = re.compile(r'(?:pon(?:le)?\s+)?(?:el\s+)?volum(?:en|e)\s+(?:a\s+|al\s+)?(\d{1,3})(?:\s*por\s*ciento)?')
VOL_UP_RE = re.compile(r'(?:sube|subele|aumenta)\s+(?:el\s+)?volumen|mas\s+volumen|volumen\s+mas\s+alto')
VOL_DOWN_RE = re.compile(r'(?:baja|bajale|reduce|disminuye)\s+(?:el\s+)?volumen|menos\s+volumen|volumen\s+mas\s+bajo')
REMOVE_RE = re.compile(r'(?:quita|saca|elimina|borra|remove)\s+(?:la\s+)?(?:cancion|tema)\s+(?:numero\s+|n\s*)?(\d+)(?:\s+de\s+la\s+cola)?')


def parse_music_text(text):
    """Devuelve (accion, argumento) si la frase es una orden de musica; si no, None."""
    raw = (text or '').strip()
    if not raw:
        return None
    parts = raw.split(None, 1)
    verb = _m.normalize_for_compare(parts[0])
    rest = parts[1].strip() if len(parts) > 1 else ''

    if verb in PLAY_VERBS and rest:
        has_url = bool(re.search(r'https?://', rest))
        not_music = (not has_url and verb in AMBIGUOUS_VERBS
                     and (EXCLUDE_PON.search(_m.normalize_for_compare(rest)) or '<@' in rest or '<#' in rest))
        if not not_music:
            q = FILLER_RE.sub('', rest, count=1).strip()
            q = re.sub(r'\s+(?:por\s+favor|porfa|plis|please)\s*$', '', q, flags=re.IGNORECASE).strip(' ?!.')
            if _m.normalize_for_compare(q) in GENERIC_QUERIES:
                return ('play', '')
            return ('play', q) if q else ('play', '')
        # no era "pon <cancion>": se sigue evaluando (p. ej. "pon el volumen al 40")

    n = _m.normalize_for_compare(raw)
    if not n or len(n.split()) > 9:
        return None
    for action, pat in NL_PATTERNS:
        if pat.fullmatch(n):
            return (action, '')
    if LOOP_OFF_RE.fullmatch(n):
        return ('loop', 'off')
    if LOOP_QUEUE_RE.fullmatch(n):
        return ('loop', 'cola')
    if LOOP_TRACK_RE.fullmatch(n):
        return ('loop', 'cancion')
    m = VOL_SET_RE.fullmatch(n)
    if m:
        return ('volume', m.group(1))
    if VOL_UP_RE.fullmatch(n):
        return ('volume', '+15')
    if VOL_DOWN_RE.fullmatch(n):
        return ('volume', '-15')
    m = REMOVE_RE.fullmatch(n)
    if m:
        return ('remove', m.group(1))
    return None


PREFIX_ALIASES = {
    'play': 'play', 'p': 'play', 'tocar': 'play', 'reproducir': 'play',
    'pause': 'pause', 'pausa': 'pause',
    'resume': 'resume', 'reanudar': 'resume', 'continuar': 'resume',
    'skip': 'skip', 's': 'skip', 'saltar': 'skip', 'siguiente': 'skip', 'next': 'skip',
    'stop': 'stop', 'parar': 'stop', 'detener': 'stop',
    'queue': 'queue', 'q': 'queue', 'cola': 'queue',
    'nowplaying': 'np', 'np': 'np', 'now': 'np', 'sonando': 'np',
    'volume': 'volume', 'vol': 'volume', 'volumen': 'volume',
    'loop': 'loop', 'repetir': 'loop', 'repeat': 'loop',
    'shuffle': 'shuffle', 'mezclar': 'shuffle', 'aleatorio': 'shuffle',
    'remove': 'remove', 'rm': 'remove',
    'clearqueue': 'clear', 'limpiarcola': 'clear',
    'musica': 'help', 'musichelp': 'help',
}


async def dispatch(action, guild, member, channel, arg=''):
    if guild is None:
        raise _err('La musica solo funciona dentro de un servidor.')
    arg = (arg or '').strip()
    if action == 'play':
        return await do_play(guild, member, arg, channel)
    table = {
        'pause': lambda: do_pause(guild, member),
        'resume': lambda: do_resume(guild, member),
        'skip': lambda: do_skip(guild, member),
        'stop': lambda: do_stop(guild, member),
        'queue': lambda: do_queue(guild, member),
        'np': lambda: do_np(guild, member),
        'volume': lambda: do_volume(guild, member, arg),
        'loop': lambda: do_loop(guild, member, arg),
        'shuffle': lambda: do_shuffle(guild, member),
        'remove': lambda: do_remove(guild, member, arg),
        'clear': lambda: do_clear(guild, member),
        'help': lambda: do_help(guild, member),
    }
    return await table[action]()


async def _reply(message, result):
    kwargs = {'embed': result} if isinstance(result, discord.Embed) else {'content': str(result)[:1900]}
    try:
        return await message.reply(mention_author=False, **kwargs)
    except discord.HTTPException:
        return await message.channel.send(**kwargs)


async def _run_text(message, action, arg):
    try:
        async with message.channel.typing():
            result = await dispatch(action, message.guild, message.author, message.channel, arg)
        if result is not None:
            await _reply(message, result)
    except _m.JarvisError as e:
        await _reply(message, str(e))
    except Exception as e:
        traceback.print_exc()
        await _reply(message, f'Error con la musica: {_short(e)}')


# ============================================================================
# EVENTOS DE VOZ
# ============================================================================
def _check_alone(player, channel):
    if _afk_entry(player.guild_id):
        return
    if _humans(channel):
        if player.alone_task is not None and not player.alone_task.done():
            player.alone_task.cancel()
        player.alone_task = None
        return
    if player.alone_task is not None and not player.alone_task.done():
        return

    async def _wait():
        await asyncio.sleep(ALONE_LEAVE_SECS)
        guild = _m.client.get_guild(player.guild_id)
        vc = guild.voice_client if guild else None
        if players.get(player.guild_id) is player and vc is not None and vc.channel and not _humans(vc.channel):
            await player._say('Me quede solo en el canal, asi que me salgo.')
            await player.shutdown()

    player.alone_task = _m.spawn(_wait())


async def _on_voice_state(member, before, after):
    try:
        guild = member.guild
        player = players.get(guild.id)
        if player is None:
            return
        me = _m.client.user
        if me is not None and member.id == me.id:
            if after.channel is None:       # me sacaron / me desconectaron
                if _afk_anti(guild.id):
                    player.requeue_current = True       # el anti-disconnect me devuelve; retomo la cancion
                else:
                    await player.shutdown(disconnect=False)
            return
        vc = guild.voice_client
        if vc is not None and vc.channel is not None:
            cid = vc.channel.id
            if (before.channel and before.channel.id == cid) or (after.channel and after.channel.id == cid):
                _check_alone(player, vc.channel)
    except Exception as e:
        print(f'[Music] voice_state_update: {e}')


async def _on_guild_remove(guild):
    player = players.get(guild.id)
    if player is not None:
        await player.shutdown(disconnect=False)


# ============================================================================
# INSTALACION
# ============================================================================
def install(mod):
    global _m
    _m = mod
    client = mod.client
    tree = client.tree

    loaded = mod.load_json(_settings_file(), {})
    if isinstance(loaded, dict):
        _settings.update(loaded)

    # ------------------------------------------------------------ slash commands --
    try:
        async def _slash_send(interaction, **kw):
            if interaction.response.is_done():
                return await interaction.followup.send(**kw)
            return await interaction.response.send_message(**kw)

        async def _slash(interaction, action, arg='', defer=False):
            try:
                if defer:
                    await interaction.response.defer()
                result = await asyncio.wait_for(
                    dispatch(action, interaction.guild, interaction.user, interaction.channel, arg),
                    timeout=75)
            except asyncio.TimeoutError:
                return await _slash_send(interaction, content='Tardo demasiado (YouTube o la conexion de voz no respondieron). Intenta de nuevo.', ephemeral=True)
            except mod.JarvisError as e:
                return await _slash_send(interaction, content=str(e), ephemeral=True)
            except Exception as e:
                traceback.print_exc()
                return await _slash_send(interaction, content=f'Error con la musica: {_short(e)}', ephemeral=True)
            if isinstance(result, discord.Embed):
                await _slash_send(interaction, embed=result)

        @tree.command(name='play', description='Reproduce una cancion o playlist (nombre o link)')
        @app_commands.guild_only()
        @app_commands.describe(busqueda='Nombre de la cancion o link (YouTube, SoundCloud...)')
        async def slash_play(interaction: discord.Interaction, busqueda: str):
            await _slash(interaction, 'play', busqueda, defer=True)

        @tree.command(name='pause', description='Pausa la musica')
        @app_commands.guild_only()
        async def slash_pause(interaction: discord.Interaction):
            await _slash(interaction, 'pause')

        @tree.command(name='resume', description='Reanuda la musica')
        @app_commands.guild_only()
        async def slash_resume(interaction: discord.Interaction):
            await _slash(interaction, 'resume')

        @tree.command(name='skip', description='Salta a la siguiente cancion')
        @app_commands.guild_only()
        async def slash_skip(interaction: discord.Interaction):
            await _slash(interaction, 'skip')

        @tree.command(name='stop', description='Detiene la musica, borra la cola y sale del canal')
        @app_commands.guild_only()
        async def slash_stop(interaction: discord.Interaction):
            await _slash(interaction, 'stop')

        @tree.command(name='queue', description='Muestra la cola de musica')
        @app_commands.guild_only()
        async def slash_queue(interaction: discord.Interaction):
            await _slash(interaction, 'queue')

        @tree.command(name='nowplaying', description='Muestra la cancion que esta sonando')
        @app_commands.guild_only()
        async def slash_nowplaying(interaction: discord.Interaction):
            await _slash(interaction, 'np')

        @tree.command(name='volume', description='Ver o cambiar el volumen de la musica')
        @app_commands.guild_only()
        @app_commands.describe(nivel=f'Volumen de 0 a {MAX_VOLUME} (omitir = ver el actual)')
        async def slash_volume(interaction: discord.Interaction, nivel: app_commands.Range[int, 0, MAX_VOLUME] = None):
            await _slash(interaction, 'volume', '' if nivel is None else str(nivel))

        @tree.command(name='loop', description='Repetir la cancion actual o toda la cola')
        @app_commands.guild_only()
        @app_commands.describe(modo='Que repetir (omitir = alternar entre los modos)')
        @app_commands.choices(modo=[
            app_commands.Choice(name='Apagado', value='off'),
            app_commands.Choice(name='Cancion actual', value='track'),
            app_commands.Choice(name='Toda la cola', value='queue'),
        ])
        async def slash_loop(interaction: discord.Interaction, modo: app_commands.Choice[str] = None):
            await _slash(interaction, 'loop', modo.value if modo else '')

        @tree.command(name='shuffle', description='Mezcla la cola')
        @app_commands.guild_only()
        async def slash_shuffle(interaction: discord.Interaction):
            await _slash(interaction, 'shuffle')

        @tree.command(name='remove', description='Quita una cancion de la cola')
        @app_commands.guild_only()
        @app_commands.describe(posicion='Posicion en la cola (mira /queue)')
        async def slash_remove(interaction: discord.Interaction, posicion: app_commands.Range[int, 1, 1000]):
            await _slash(interaction, 'remove', str(posicion))

        @tree.command(name='clearqueue', description='Vacia la cola (la cancion actual sigue)')
        @app_commands.guild_only()
        async def slash_clearqueue(interaction: discord.Interaction):
            await _slash(interaction, 'clear')

        @tree.command(name='musica', description='Ayuda de los comandos de musica')
        @app_commands.guild_only()
        async def slash_musica(interaction: discord.Interaction):
            await _slash(interaction, 'help')

    except Exception as e:      # p. ej. otro modulo ya registro un comando con el mismo nombre
        print(f'[Music] No pude registrar los slash commands: {type(e).__name__}: {e} (siguen funcionando >>play y "jarvis pon ...")')

    # -------------------------------------------------- eventos / envoltorios --
    client.add_listener(_on_voice_state, 'on_voice_state_update')
    client.add_listener(_on_guild_remove, 'on_guild_remove')

    # "jarvis pon ..." lo puede usar cualquiera (no solo la whitelist); lo demas sigue igual
    orig_handle_jarvis = mod.handle_jarvis

    async def handle_jarvis(message):
        parsed = None
        try:
            if message.guild and not message.author.bot:
                m = mod.JARVIS_TRIGGER.match(message.content or '')
                if m:
                    parsed = parse_music_text(message.content[m.end():])
        except Exception as e:
            print(f'[Music] parse: {e}')
        if parsed:
            await _run_text(message, parsed[0], parsed[1])
            return True
        return await orig_handle_jarvis(message)

    mod.handle_jarvis = handle_jarvis

    orig_handle_command = mod.handle_command

    async def handle_command(message):
        if message.guild and not message.author.bot and (message.content or '').startswith(mod.PREFIX):
            parts = message.content[len(mod.PREFIX):].strip().split(None, 1)
            if parts:
                action = PREFIX_ALIASES.get(parts[0].lower())
                if action:
                    await _run_text(message, action, parts[1] if len(parts) > 1 else '')
                    return
        return await orig_handle_command(message)

    mod.handle_command = handle_command

    orig_help = mod.build_help_embed

    def build_help_embed():
        emb = orig_help()
        value = ('`/play` | `>>play` | `jarvis pon <cancion o link>` - pausa, salta, cola, volumen, repetir...\n'
                 '`jarvis ayuda de musica` o `/musica` para ver todo')
        if len(emb.fields) < 25 and len(emb) + len(value) + 20 < 5900:     # limite de Discord: 6000 caracteres
            emb.add_field(name='Musica', value=value, inline=False)
        return emb

    mod.build_help_embed = build_help_embed
    print('[Music] Modulo de musica cargado.')
