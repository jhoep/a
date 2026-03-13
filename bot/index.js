'use strict';

// ============================================================================
// IMPORTS
// ============================================================================
const {
  Client, GatewayIntentBits, EmbedBuilder, PermissionFlagsBits,
  ActionRowBuilder, ButtonBuilder, ButtonStyle, Collection,
} = require('discord.js');
const {
  joinVoiceChannel, createAudioPlayer, createAudioResource,
  AudioPlayerStatus, VoiceConnectionStatus, entersState,
  StreamType, getVoiceConnection,
} = require('@discordjs/voice');
const { spawn, execFile } = require('child_process');
const { promisify } = require('util');
const execFileAsync = promisify(execFile);
const path = require('path');
const fs = require('fs');
const http = require('http');
const fetch = require('node-fetch');
const ffmpegPath = require('ffmpeg-static');

// ============================================================================
// CONFIG
// ============================================================================
const DISCORD_TOKEN  = process.env.DISCORD_TOKEN;
const GROQ_API_KEY   = process.env.GROQ_API_KEY;
const OWNER_ID       = process.env.OWNER_ID || '596764844791824417';
const PREFIX         = '&';
const COOKIES_FILE   = path.join(__dirname, 'cookies.txt');

// Whitelist de usuarios que pueden usar Jarvis
const JARVIS_WHITELIST = new Set(
  (process.env.JARVIS_WHITELIST || OWNER_ID)
    .split(',').map(s => s.trim()).filter(Boolean)
);

// ============================================================================
// YT-DLP AUTO-UPDATE
// ============================================================================
async function autoUpdateYtdlp() {
  return new Promise(resolve => {
    console.log('[STARTUP] Actualizando yt-dlp...');
    const proc = spawn('pip', ['install', '-U', 'yt-dlp', '-q'], { stdio: 'inherit' });
    proc.on('close', code => {
      if (code === 0) console.log('[STARTUP] yt-dlp actualizado.');
      else console.log(`[STARTUP] yt-dlp update salió con código ${code} (puede ya estar al día).`);
      resolve();
    });
    proc.on('error', err => {
      console.log(`[STARTUP] No se pudo actualizar yt-dlp: ${err.message}`);
      resolve();
    });
  });
}

// ============================================================================
// YT-DLP HELPERS
// ============================================================================
const YTDLP_BASE_ARGS = [
  '--no-warnings',
  '--extractor-args', 'youtube:player_client=tv_embedded,ios',
  '--extractor-args', 'youtube:player_skip=webpage,js',
  '--socket-timeout', '30',
  '--retries', '5',
];

function cookieArgs() {
  return fs.existsSync(COOKIES_FILE) ? ['--cookies', COOKIES_FILE] : [];
}

function bestAudioFromFormats(formats = []) {
  const hasAudio = f => f.acodec && f.acodec !== 'none' && f.url;
  const audioOnly = formats.filter(f => f.vcodec === 'none' && hasAudio(f));
  if (audioOnly.length) {
    return audioOnly.sort((a, b) => (b.tbr || b.abr || 0) - (a.tbr || a.abr || 0))[0];
  }
  const withAudio = formats.filter(hasAudio);
  if (withAudio.length) {
    return withAudio.sort((a, b) => (b.tbr || b.abr || 0) - (a.tbr || a.abr || 0))[0];
  }
  return null;
}

async function ytdlpGetInfo(query, { flat = false, playlist = false } = {}) {
  const isUrl = /^https?:\/\//i.test(query);
  const target = isUrl ? query : `ytsearch1:${query}`;

  const args = [
    ...YTDLP_BASE_ARGS,
    ...cookieArgs(),
    '--dump-json',
    '--no-download',
  ];

  if (flat) args.push('--flat-playlist');
  if (!playlist) args.push('--no-playlist');

  args.push(target);

  const { stdout } = await execFileAsync('yt-dlp', args, { maxBuffer: 20 * 1024 * 1024 });

  const lines = stdout.trim().split('\n').filter(Boolean);
  if (!lines.length) throw new Error('yt-dlp no devolvió datos.');

  const items = lines.map(l => JSON.parse(l));
  return playlist ? items : items[0];
}

async function getAudioUrl(url) {
  const args = [
    ...YTDLP_BASE_ARGS,
    ...cookieArgs(),
    '--dump-json',
    '--no-download',
    '--no-playlist',
    url,
  ];

  const { stdout } = await execFileAsync('yt-dlp', args, { maxBuffer: 20 * 1024 * 1024 });
  const data = JSON.parse(stdout.trim().split('\n')[0]);

  let audioUrl = data.url;
  if (!audioUrl) {
    const best = bestAudioFromFormats(data.formats);
    if (!best) throw new Error('No se encontró formato de audio para este video.');
    audioUrl = best.url;
    console.log(`[YT-DLP] Formato: acodec=${best.acodec} tbr=${best.tbr}`);
  }
  return { ...data, url: audioUrl };
}

// ============================================================================
// FFMPEG STREAM
// ============================================================================
function createFFmpegResource(url, volume = 1.0) {
  const ffmpegArgs = [
    '-reconnect', '1',
    '-reconnect_streamed', '1',
    '-reconnect_delay_max', '5',
    '-reconnect_on_network_error', '1',
    '-i', url,
    '-analyzeduration', '0',
    '-loglevel', '0',
    '-f', 's16le',
    '-ar', '48000',
    '-ac', '2',
    'pipe:1',
  ];

  const proc = spawn(ffmpegPath, ffmpegArgs, { stdio: ['ignore', 'pipe', 'ignore'] });

  const resource = createAudioResource(proc.stdout, {
    inputType: StreamType.Raw,
    inlineVolume: true,
  });

  resource.volume?.setVolume(volume);
  resource._ffmpegProc = proc;
  return resource;
}

// ============================================================================
// MUSIC QUEUE
// ============================================================================
class Track {
  constructor({ url, title, duration, thumbnail, webpage_url, requester }) {
    this.url        = url;
    this.title      = title || 'Desconocido';
    this.duration   = duration || 0;
    this.thumbnail  = thumbnail || null;
    this.webpageUrl = webpage_url || url;
    this.requester  = requester;
  }

  formatDuration() {
    const s = Math.floor(this.duration);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
    return `${m}:${String(sec).padStart(2, '0')}`;
  }

  nowPlayingEmbed() {
    const embed = new EmbedBuilder()
      .setColor(0x1DB954)
      .setTitle('🎵 Reproduciendo ahora')
      .setDescription(`**[${this.title}](${this.webpageUrl})**`)
      .addFields(
        { name: '⏱ Duración', value: this.formatDuration(), inline: true },
        { name: '👤 Pedido por', value: `${this.requester}`, inline: true },
      );
    if (this.thumbnail) embed.setThumbnail(this.thumbnail);
    return embed;
  }
}

class MusicQueue {
  constructor(guildId) {
    this.guildId    = guildId;
    this.tracks     = [];
    this.current    = null;
    this.loop       = false;
    this.volume     = 1.0;
    this.player     = createAudioPlayer();
    this.connection = null;
    this.textChannel = null;
    this._playing   = false;

    this.player.on(AudioPlayerStatus.Idle, () => this._onIdle());
    this.player.on('error', err => {
      console.error(`[Player] Error: ${err.message}`);
      this._onIdle();
    });
  }

  subscribe(connection) {
    this.connection = connection;
    connection.subscribe(this.player);
  }

  enqueue(track) {
    this.tracks.push(track);
  }

  enqueueMany(tracks) {
    this.tracks.push(...tracks);
  }

  async startPlaying() {
    if (this._playing) return;
    await this._playNext();
  }

  async _playNext() {
    if (!this.connection) return;

    let track;
    if (this.loop && this.current) {
      track = this.current;
    } else if (this.tracks.length) {
      track = this.tracks.shift();
      this.current = track;
    } else {
      this.current = null;
      this._playing = false;
      this._scheduleDisconnect();
      return;
    }

    this._playing = true;

    try {
      const info = await getAudioUrl(track.webpageUrl || track.url);
      track.url = info.url;
      if (!track.title || track.title === 'Desconocido') track.title = info.title;
      if (!track.thumbnail) track.thumbnail = info.thumbnail;
      if (!track.duration) track.duration = info.duration || 0;

      const resource = createFFmpegResource(track.url, this.volume);
      this.player.play(resource);

      if (this.textChannel) {
        this.textChannel.send({ embeds: [track.nowPlayingEmbed()] }).catch(() => {});
      }
    } catch (err) {
      console.error(`[Music] Error cargando "${track.title}": ${err.message}`);
      if (this.textChannel) {
        this.textChannel.send(`❌ Error reproduciendo **${track.title}**: ${err.message}`).catch(() => {});
      }
      this._playing = false;
      await this._playNext();
    }
  }

  _onIdle() {
    this._playing = false;
    this._playNext();
  }

  pause() {
    return this.player.pause();
  }

  resume() {
    return this.player.unpause();
  }

  skip() {
    const wasLoop = this.loop;
    this.loop = false;
    this.player.stop();
    this.loop = wasLoop;
    return wasLoop;
  }

  stop() {
    this.tracks = [];
    this.current = null;
    this.loop = false;
    this._playing = false;
    this.player.stop(true);
    if (this.connection) {
      try { this.connection.destroy(); } catch (_) {}
      this.connection = null;
    }
  }

  toggleLoop() {
    if (!this.current) { this.loop = false; return false; }
    this.loop = !this.loop;
    return this.loop;
  }

  setVolume(vol) {
    this.volume = Math.max(0, Math.min(2, vol));
  }

  isPlaying() {
    return this.player.state.status === AudioPlayerStatus.Playing;
  }

  isPaused() {
    return this.player.state.status === AudioPlayerStatus.Paused;
  }

  _scheduleDisconnect() {
    setTimeout(() => {
      if (!this._playing && this.connection) {
        try { this.connection.destroy(); } catch (_) {}
        this.connection = null;
        queues.delete(this.guildId);
      }
    }, 300_000);
  }
}

const queues = new Map();

function getQueue(guildId) {
  if (!queues.has(guildId)) queues.set(guildId, new MusicQueue(guildId));
  return queues.get(guildId);
}

function destroyQueue(guildId) {
  const q = queues.get(guildId);
  if (q) { q.stop(); queues.delete(guildId); }
}

// ============================================================================
// BOT CLIENT
// ============================================================================
const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.GuildVoiceStates,
    GatewayIntentBits.GuildMembers,
    GatewayIntentBits.MessageContent,
  ],
});

// ============================================================================
// HELPERS
// ============================================================================
function parseDuration(str) {
  const map = { s: 1, m: 60, h: 3600, d: 86400 };
  const match = str.match(/^(\d+)([smhd])$/i);
  if (!match) return null;
  return parseInt(match[1]) * (map[match[2].toLowerCase()] || 1);
}

function requireVoice(message) {
  const channel = message.member?.voice?.channel;
  if (!channel) {
    message.reply('🚫 Debes estar en un canal de voz.');
    return null;
  }
  return channel;
}

async function connectToChannel(channel, guildId) {
  const existing = getVoiceConnection(guildId);
  if (existing) {
    try { existing.destroy(); } catch (_) {}
    await new Promise(r => setTimeout(r, 300));
  }
  const connection = joinVoiceChannel({
    channelId: channel.id,
    guildId,
    adapterCreator: channel.guild.voiceAdapterCreator,
    selfDeaf: true,
  });
  try {
    await entersState(connection, VoiceConnectionStatus.Ready, 30_000);
  } catch (err) {
    connection.destroy();
    throw new Error('No pude conectarme al canal de voz.');
  }
  return connection;
}

// ============================================================================
// GROQ / JARVIS
// ============================================================================
const JARVIS_TRIGGER = /^jarvis[,:]?\s*/i;

const SYSTEM_PROMPT = `Eres Jarvis, un asistente inteligente de Discord.
Tienes personalidad: eres amigable, cercano, con un toque de humor,
y hablas de forma natural como un amigo. Usas expresiones coloquiales
de vez en cuando (ej: 'pues mira', 'la verdad es que', 'te cuento', etc.).
Responde siempre en el mismo idioma del usuario (español o inglés).
Sé conciso pero completo. Máximo 1500 caracteres. No uses markdown excesivo.
Si no sabes algo, admítelo con honestidad y ofrece alternativas.`;

async function askGroq(prompt) {
  if (!GROQ_API_KEY) return 'No hay GROQ_API_KEY configurada.';
  try {
    const res = await fetch('https://api.groq.com/openai/v1/chat/completions', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${GROQ_API_KEY}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        model: 'llama-3.3-70b-versatile',
        messages: [
          { role: 'system', content: SYSTEM_PROMPT },
          { role: 'user', content: prompt },
        ],
        max_tokens: 600,
        temperature: 0.7,
      }),
    });
    if (res.status === 429) return 'Demasiadas peticiones. Intenta en unos segundos.';
    if (!res.ok) return `Error de Groq: ${res.status}`;
    const data = await res.json();
    return data.choices?.[0]?.message?.content?.trim() || 'Sin respuesta de Groq.';
  } catch (err) {
    return `Error contactando Groq: ${err.message}`;
  }
}

// ============================================================================
// COMMAND HANDLER
// ============================================================================
async function handleCommand(message) {
  if (!message.content.startsWith(PREFIX) || message.author.bot) return;

  const args = message.content.slice(PREFIX.length).trim().split(/\s+/);
  const cmd  = args.shift().toLowerCase();

  // ---- MUSIC COMMANDS ----

  if (['play', 'p', 'reproducir'].includes(cmd)) {
    const query = args.join(' ');
    if (!query) return message.reply('Debes especificar una URL o búsqueda. Ej: `&play Never Gonna Give You Up`');

    const voiceChannel = requireVoice(message);
    if (!voiceChannel) return;

    const guild  = message.guild;
    const queue  = getQueue(guild.id);
    const status = await message.channel.send(`🔍 Buscando: \`${query.slice(0, 100)}\``);

    try {
      // Limpiar URL: si tiene video + lista, usar solo el video
      let cleanedQuery = query.trim();
      const videoMatch = cleanedQuery.match(/(?:v=|youtu\.be\/)([a-zA-Z0-9_-]{11})/);
      const listMatch  = cleanedQuery.match(/[?&]list=([a-zA-Z0-9_-]+)/);
      let playlistNote = '';
      if (videoMatch && listMatch && cleanedQuery.includes('watch?')) {
        cleanedQuery = `https://www.youtube.com/watch?v=${videoMatch[1]}`;
        playlistNote = ' (solo el video; usa la URL de la playlist para reproducir completa)';
      }

      const isUrl = /^https?:\/\//i.test(cleanedQuery);

      // Conectar al canal de voz
      if (!queue.connection || queue.connection.state.status === VoiceConnectionStatus.Destroyed) {
        const conn = await connectToChannel(voiceChannel, guild.id);
        queue.subscribe(conn);
      } else if (queue.connection._state?.channel?.id !== voiceChannel.id) {
        await queue.connection.joinConfig.channelId;
      }
      queue.textChannel = message.channel;

      // Detectar playlist
      const isPlaylist = isUrl && /[?&]list=/.test(cleanedQuery) && !videoMatch;

      if (isPlaylist) {
        await status.edit('📋 Cargando playlist...');
        const items = await ytdlpGetInfo(cleanedQuery, { flat: true, playlist: true }).catch(() => null);
        if (!items || !items.length) {
          return status.edit('❌ No se encontraron canciones en la playlist.');
        }
        const tracks = items.map(item => new Track({
          url:         `https://www.youtube.com/watch?v=${item.id || item.url}`,
          title:       item.title,
          duration:    item.duration,
          thumbnail:   item.thumbnail,
          webpage_url: item.webpage_url || `https://www.youtube.com/watch?v=${item.id}`,
          requester:   message.author.tag,
        }));
        queue.enqueueMany(tracks);
        await status.edit(`✅ Añadidas **${tracks.length}** canciones de la playlist${playlistNote}`);
      } else {
        const info = await ytdlpGetInfo(cleanedQuery);
        const track = new Track({
          url:         info.url || cleanedQuery,
          title:       info.title,
          duration:    info.duration,
          thumbnail:   info.thumbnail,
          webpage_url: info.webpage_url || cleanedQuery,
          requester:   message.author.tag,
        });
        queue.enqueue(track);
        const pos = queue.tracks.length + (queue.current ? 1 : 0);
        await status.edit(`✅ Añadido (posición ${pos}): **${track.title}**${playlistNote}`);
      }

      if (!queue._playing) await queue.startPlaying();

    } catch (err) {
      console.error('[play]', err);
      const errMsg = err.message.toLowerCase();
      let reply = `❌ Error: ${err.message}`;
      if (errMsg.includes('age')) reply = '❌ Video con restricción de edad. Agrega `cookies.txt`.';
      else if (errMsg.includes('private')) reply = '❌ Video privado.';
      else if (errMsg.includes('unavailable')) reply = '❌ Video no disponible.';
      else if (errMsg.includes('sign in') || errMsg.includes('login')) reply = '❌ YouTube requiere login. Agrega `cookies.txt`.';
      await status.edit(reply.slice(0, 1990));
    }
    return;
  }

  if (['pause', 'pa'].includes(cmd)) {
    const q = queues.get(message.guild.id);
    if (!q) return message.reply('No hay nada reproduciéndose.');
    if (q.isPlaying()) { q.pause(); message.reply('⏸️ Pausado.'); }
    else if (q.isPaused()) message.reply('⚠️ Ya está pausado.');
    else message.reply('❌ No hay nada reproduciéndose.');
    return;
  }

  if (['resume', 'r', 'continue', 're', 'unpause'].includes(cmd)) {
    const q = queues.get(message.guild.id);
    if (!q) return message.reply('No hay nada pausado.');
    if (q.isPaused()) { q.resume(); message.reply('▶️ Reanudado.'); }
    else if (q.isPlaying()) message.reply('⚠️ Ya está reproduciéndose.');
    else message.reply('❌ Nada que reanudar.');
    return;
  }

  if (['stop', 'leave', 'disconnect', 'dc', 'salir', 'vete', 'fuckoff'].includes(cmd)) {
    destroyQueue(message.guild.id);
    message.reply('⏹️ Reproducción detenida y cola limpiada.');
    return;
  }

  if (['skip', 's', 'next', 'saltar'].includes(cmd)) {
    const q = queues.get(message.guild.id);
    if (!q || (!q.current && !q.tracks.length)) return message.reply('❌ No hay nada en cola.');

    const posArg = args[0];
    if (posArg && /^\d+$/.test(posArg)) {
      const pos = parseInt(posArg);
      if (pos < 1 || pos > q.tracks.length) return message.reply(`Posición inválida (1 - ${q.tracks.length}).`);
      q.tracks.splice(0, pos - 1);
      message.reply(`⏭️ Saltando a la posición **${pos}**...`);
    } else {
      const title = q.current?.title || 'canción actual';
      const wasLoop = q.skip();
      message.reply(`⏭️ Saltando **${title}**...` + (wasLoop ? ' (loop desactivado)' : ''));
    }
    return;
  }

  if (['queue', 'q', 'list', 'cola'].includes(cmd)) {
    const q = queues.get(message.guild.id);
    if (!q || (!q.current && !q.tracks.length)) return message.reply('La cola está vacía.');

    const lines = [];
    if (q.current) {
      lines.push(`**Reproduciendo ahora:**\n🎵 ${q.current.title} \`[${q.current.formatDuration()}]\` - *${q.current.requester}*${q.loop ? ' 🔁' : ''}`);
    }
    if (q.tracks.length) {
      lines.push('\n**Cola:**');
      q.tracks.slice(0, 20).forEach((t, i) => {
        lines.push(`${i + 1}. ${t.title} \`[${t.formatDuration()}]\` - *${t.requester}*`);
      });
      if (q.tracks.length > 20) lines.push(`...y ${q.tracks.length - 20} más.`);
    }

    const embed = new EmbedBuilder()
      .setColor(0x1DB954)
      .setTitle(`📋 Cola — ${message.guild.name}`)
      .setDescription(lines.join('\n').slice(0, 4096));
    message.reply({ embeds: [embed] });
    return;
  }

  if (['nowplaying', 'np', 'current', 'song', 'ahora'].includes(cmd)) {
    const q = queues.get(message.guild.id);
    if (!q?.current) return message.reply('No hay nada reproduciéndose ahora mismo.');
    message.reply({ embeds: [q.current.nowPlayingEmbed()] });
    return;
  }

  if (['volume', 'v', 'vol'].includes(cmd)) {
    const q = queues.get(message.guild.id);
    if (!q) return message.reply('No hay cola activa.');
    if (!args[0]) return message.reply(`🔊 Volumen actual: **${Math.round(q.volume * 100)}%**`);
    const vol = parseInt(args[0]);
    if (isNaN(vol) || vol < 0 || vol > 200) return message.reply('Volumen entre 0 y 200.');
    q.setVolume(vol / 100);
    message.reply(`🔊 Volumen: **${vol}%**`);
    return;
  }

  if (['loop', 'l', 'repeat', 'repetir'].includes(cmd)) {
    const q = queues.get(message.guild.id);
    if (!q?.current) return message.reply('No hay nada reproduciéndose.');
    const on = q.toggleLoop();
    message.reply(on ? '🔁 Loop activado.' : '▶️ Loop desactivado.');
    return;
  }

  if (['yt', 'ytsearch', 'youtube'].includes(cmd)) {
    const query = args.join(' ');
    if (!query) return message.reply('Especifica qué buscar.');
    const status = await message.channel.send(`🔍 Buscando en YouTube: \`${query}\``);
    try {
      const results = await ytdlpGetInfo(`ytsearch5:${query}`, { flat: true, playlist: true });
      const list = (Array.isArray(results) ? results : [results]).slice(0, 5);
      const embed = new EmbedBuilder()
        .setColor(0xFF0000)
        .setTitle(`🔍 Resultados para: ${query}`)
        .setDescription(
          list.map((r, i) => `**${i + 1}.** [${r.title}](https://www.youtube.com/watch?v=${r.id}) \`${r.duration ? Math.floor(r.duration / 60) + ':' + String(r.duration % 60).padStart(2,'0') : '?'}\``).join('\n')
        );
      await status.edit({ content: '', embeds: [embed] });
    } catch (err) {
      await status.edit(`❌ Error en búsqueda: ${err.message}`);
    }
    return;
  }

  // ---- MODERATION COMMANDS ----

  if (cmd === 'ban') {
    if (!message.member.permissions.has(PermissionFlagsBits.BanMembers))
      return message.reply('No tienes permisos para banear.');
    const target = message.mentions.members.first();
    if (!target) return message.reply('Menciona al usuario a banear.');
    const reason = args.slice(1).join(' ') || 'Sin razón especificada';
    try {
      await target.ban({ reason });
      message.reply(`✅ **${target.user.tag}** baneado. Razón: ${reason}`);
    } catch (err) {
      message.reply(`❌ No pude banear: ${err.message}`);
    }
    return;
  }

  if (cmd === 'unban') {
    if (!message.member.permissions.has(PermissionFlagsBits.BanMembers))
      return message.reply('No tienes permisos para desbanear.');
    const userId = args[0];
    if (!userId || !/^\d+$/.test(userId)) return message.reply('Proporciona el ID del usuario.');
    const reason = args.slice(1).join(' ') || 'Sin razón';
    try {
      await message.guild.members.unban(userId, reason);
      message.reply(`✅ Usuario \`${userId}\` desbaneado.`);
    } catch (err) {
      message.reply(`❌ No pude desbanear: ${err.message}`);
    }
    return;
  }

  if (['timeout', 'mute', 'silence'].includes(cmd)) {
    if (!message.member.permissions.has(PermissionFlagsBits.ModerateMembers))
      return message.reply('No tienes permisos para silenciar.');
    const target = message.mentions.members.first();
    if (!target) return message.reply('Menciona al usuario.');
    const durStr = args[1] || '10m';
    const secs   = parseDuration(durStr);
    if (!secs) return message.reply('Formato de duración inválido (ej: 10m, 2h, 1d).');
    const reason = args.slice(2).join(' ') || 'Sin razón';
    try {
      await target.timeout(secs * 1000, reason);
      message.reply(`✅ **${target.user.tag}** silenciado por ${durStr}. Razón: ${reason}`);
    } catch (err) {
      message.reply(`❌ No pude silenciar: ${err.message}`);
    }
    return;
  }

  if (['untimeout', 'unmute', 'removetimeout'].includes(cmd)) {
    if (!message.member.permissions.has(PermissionFlagsBits.ModerateMembers))
      return message.reply('No tienes permisos.');
    const target = message.mentions.members.first();
    if (!target) return message.reply('Menciona al usuario.');
    const reason = args.slice(1).join(' ') || 'Sin razón';
    try {
      await target.timeout(null, reason);
      message.reply(`✅ Silencio removido de **${target.user.tag}**.`);
    } catch (err) {
      message.reply(`❌ Error: ${err.message}`);
    }
    return;
  }

  if (cmd === 'purge') {
    if (!message.member.permissions.has(PermissionFlagsBits.ManageMessages))
      return message.reply('No tienes permisos para borrar mensajes.');
    const amount = parseInt(args[0]);
    if (!amount || amount < 1 || amount > 100) return message.reply('Indica un número entre 1 y 100.');
    try {
      const deleted = await message.channel.bulkDelete(amount, true);
      const conf = await message.channel.send(`✅ ${deleted.size} mensajes eliminados.`);
      setTimeout(() => conf.delete().catch(() => {}), 3000);
    } catch (err) {
      message.reply(`❌ Error: ${err.message}`);
    }
    return;
  }

  // ---- MISC COMMANDS ----

  if (cmd === 'ping') {
    const sent = await message.reply('Calculando...');
    const latency = sent.createdTimestamp - message.createdTimestamp;
    sent.edit(`🏓 Pong! Latencia: **${latency}ms** | API: **${Math.round(client.ws.ping)}ms**`);
    return;
  }

  if (cmd === 'server') {
    const g = message.guild;
    const embed = new EmbedBuilder()
      .setColor(0x5865F2)
      .setTitle(g.name)
      .setThumbnail(g.iconURL())
      .addFields(
        { name: 'Miembros', value: `${g.memberCount}`, inline: true },
        { name: 'Creado', value: `<t:${Math.floor(g.createdTimestamp / 1000)}:R>`, inline: true },
        { name: 'Propietario', value: `<@${g.ownerId}>`, inline: true },
        { name: 'Canales', value: `${g.channels.cache.size}`, inline: true },
        { name: 'Roles', value: `${g.roles.cache.size}`, inline: true },
        { name: 'Boosts', value: `${g.premiumSubscriptionCount || 0}`, inline: true },
      );
    message.reply({ embeds: [embed] });
    return;
  }

  if (['help', 'h', 'ayuda', 'commands', 'comandos'].includes(cmd)) {
    const embed = new EmbedBuilder()
      .setColor(0x5865F2)
      .setTitle('📖 Comandos')
      .setDescription(`Prefijo: \`${PREFIX}\``)
      .addFields(
        {
          name: '🎵 Música',
          value: [
            '`&play <url/búsqueda>` — Reproducir o añadir a la cola',
            '`&pause` — Pausar',
            '`&resume` — Reanudar',
            '`&skip [pos]` — Saltar canción',
            '`&stop` — Parar y desconectar',
            '`&queue` — Ver la cola',
            '`&nowplaying` — Canción actual',
            '`&volume <0-200>` — Volumen',
            '`&loop` — Activar/desactivar loop',
            '`&yt <búsqueda>` — Buscar en YouTube',
          ].join('\n'),
        },
        {
          name: '🔨 Moderación',
          value: [
            '`&ban @usuario [razón]` — Banear',
            '`&unban <id> [razón]` — Desbanear',
            '`&timeout @usuario <tiempo> [razón]` — Silenciar (ej: 10m, 2h)',
            '`&untimeout @usuario` — Quitar silencio',
            '`&purge <1-100>` — Borrar mensajes',
          ].join('\n'),
        },
        {
          name: '💬 General',
          value: [
            '`&ping` — Latencia del bot',
            '`&server` — Info del servidor',
            '`jarvis <pregunta>` — Asistente IA',
          ].join('\n'),
        },
      );
    message.reply({ embeds: [embed] });
    return;
  }
}

// ============================================================================
// JARVIS HANDLER
// ============================================================================
async function handleJarvis(message) {
  if (message.author.bot) return false;
  if (!JARVIS_TRIGGER.test(message.content)) return false;
  if (!JARVIS_WHITELIST.has(message.author.id)) return true;

  const text = message.content.replace(JARVIS_TRIGGER, '').trim();
  if (!text) {
    message.reply('¿En qué puedo ayudarte?');
    return true;
  }

  const typing = await message.channel.sendTyping().catch(() => {});
  const response = await askGroq(text);
  await message.reply(response.slice(0, 2000));
  return true;
}

// ============================================================================
// EVENTS
// ============================================================================
client.once('ready', () => {
  console.log(`✅ Bot listo: ${client.user.tag}`);
  console.log(`🔗 Conectado a ${client.guilds.cache.size} servidores`);
  console.log(`🍪 Cookies: ${fs.existsSync(COOKIES_FILE) ? 'Encontrado' : 'No encontrado'}`);
  client.user.setActivity(`${PREFIX}help | jarvis ayuda`, { type: 2 });
});

client.on('messageCreate', async message => {
  if (message.author.bot || !message.guild) return;
  try {
    const jarvisHandled = await handleJarvis(message);
    if (!jarvisHandled) await handleCommand(message);
  } catch (err) {
    console.error('[messageCreate]', err);
  }
});

client.on('voiceStateUpdate', (oldState, newState) => {
  const botId = client.user?.id;
  if (!botId) return;
  const guild = oldState.guild || newState.guild;

  // Si el bot es sacado del canal, destruir la cola
  if (oldState.id === botId && !newState.channelId) {
    destroyQueue(guild.id);
    return;
  }

  // Auto-disconnect si quedamos solos en el canal
  const q = queues.get(guild.id);
  if (!q?.connection) return;
  const channelId = q.connection.joinConfig?.channelId;
  if (!channelId) return;
  const channel = guild.channels.cache.get(channelId);
  if (!channel) return;
  const nonBotMembers = channel.members.filter(m => !m.user.bot);
  if (nonBotMembers.size === 0) {
    setTimeout(() => {
      const ch = guild.channels.cache.get(channelId);
      if (ch && ch.members.filter(m => !m.user.bot).size === 0) {
        destroyQueue(guild.id);
      }
    }, 60_000);
  }
});

client.on('error', err => console.error('[Client Error]', err));

// ============================================================================
// KEEPALIVE WEBSERVER (para Replit / hosting)
// ============================================================================
function keepAlive() {
  const server = http.createServer((req, res) => {
    res.writeHead(200, { 'Content-Type': 'text/plain' });
    res.end('Bot online');
  });
  const PORT = process.env.PORT || 3000;
  server.listen(PORT, () => console.log(`[Web] Keep-alive en puerto ${PORT}`));
}

// ============================================================================
// STARTUP
// ============================================================================
(async () => {
  await autoUpdateYtdlp();
  keepAlive();

  if (!DISCORD_TOKEN) {
    console.error('FATAL: DISCORD_TOKEN no configurado.');
    process.exit(1);
  }

  let attempt = 0;
  while (attempt < 10) {
    try {
      await client.login(DISCORD_TOKEN);
      break;
    } catch (err) {
      attempt++;
      const delay = Math.min(30 * 2 ** attempt, 900);
      console.error(`[Login] Error (intento ${attempt}): ${err.message}. Reintentando en ${delay}s...`);
      await new Promise(r => setTimeout(r, delay * 1000));
    }
  }
})();
