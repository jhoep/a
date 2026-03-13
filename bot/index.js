'use strict';

// ============================================================================
// IMPORTS
// ============================================================================
const {
  Client, GatewayIntentBits, EmbedBuilder, PermissionFlagsBits,
  ActionRowBuilder, ButtonBuilder, ButtonStyle, Collection,
  ActivityType, Status,
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
const DISCORD_TOKEN   = process.env.DISCORD_TOKEN;
const GROQ_API_KEY    = process.env.GROQ_API_KEY;
const OWNER_ID        = process.env.OWNER_ID || '596764844791824417';
const PREFIX          = '&';
const COOKIES_FILE    = path.join(__dirname, 'cookies.txt');
const CANAL_AVISOS_ID = '1382547512543543386';

// Whitelist de usuarios que pueden usar Jarvis
const JARVIS_WHITELIST = new Set(
  (process.env.JARVIS_WHITELIST || OWNER_ID)
    .split(',').map(s => s.trim()).filter(Boolean),
);

// ============================================================================
// AUTO-RESPONSES
// ============================================================================
const SALUDOS       = ['hola', 'ola', 'holi', 'oli', 'h0la', 'hol'];
const RESPUESTAS_GREETING = ['Tu nariz contra mis bolas'];
const PALABRAS_QUE  = ['que'];
const RESPUESTAS_QUE = ['so'];
const PALABRAS_RRA  = ['rra'];
const RESPUESTAS_RRA = ['eres tu bobo tonto ez ez'];
const PALABRAS_FT   = ['ft10', 'ft5', 'ft3'];
const RESPUESTAS_FT  = ['Bro, realmente pidio ft, el malo este'];
const autorespuestaCooldown = new Map();
const COOLDOWN_TIEMPO = 0;

// ============================================================================
// YT-DLP AUTO-UPDATE
// ============================================================================
async function autoUpdateYtdlp() {
  return new Promise(resolve => {
    console.log('[STARTUP] Actualizando yt-dlp...');
    const proc = spawn('pip', ['install', '-U', 'yt-dlp', '-q'], { stdio: 'inherit' });
    proc.on('close', code => {
      console.log(code === 0 ? '[STARTUP] yt-dlp actualizado.' : `[STARTUP] yt-dlp salió con código ${code}.`);
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
const ANDROID_UA = 'com.google.android.youtube/17.36.4 (Linux; U; Android 12; GB) gzip';
const YTDLP_BASE_ARGS = [
  '--no-warnings',
  '--extractor-args', 'youtube:player_client=tv_embedded,ios',
  '--extractor-args', 'youtube:player_skip=webpage,js',
  '--add-header', `User-Agent:${ANDROID_UA}`,
  '--socket-timeout', '30',
  '--retries', '5',
];

function cookieArgs() {
  return fs.existsSync(COOKIES_FILE) ? ['--cookies', COOKIES_FILE] : [];
}

function bestAudioFromFormats(formats = []) {
  const hasAudio = f => f.acodec && f.acodec !== 'none' && f.url;
  const audioOnly = formats.filter(f => f.vcodec === 'none' && hasAudio(f));
  if (audioOnly.length) return audioOnly.sort((a, b) => (b.tbr || b.abr || 0) - (a.tbr || a.abr || 0))[0];
  const withAudio = formats.filter(hasAudio);
  if (withAudio.length) return withAudio.sort((a, b) => (b.tbr || b.abr || 0) - (a.tbr || a.abr || 0))[0];
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

async function getAudioUrl(url, retries = 3) {
  let lastErr;
  for (let attempt = 0; attempt < retries; attempt++) {
    try {
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
      }
      return { ...data, url: audioUrl };
    } catch (err) {
      lastErr = err;
      const msg = (err.message || '').toLowerCase();
      if (msg.includes('page needs to be reloaded') || msg.includes('please reload')) {
        const wait = 3000 * (attempt + 1);
        console.log(`[YT-DLP] Reload error, esperando ${wait / 1000}s...`);
        if (attempt < retries - 1) {
          await new Promise(r => setTimeout(r, wait));
          continue;
        }
      }
      throw err;
    }
  }
  throw lastErr;
}

// ============================================================================
// FFMPEG STREAM
// ============================================================================
function createFFmpegResource(url, volume = 1.0) {
  const proc = spawn(ffmpegPath, [
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
  ], { stdio: ['ignore', 'pipe', 'ignore'] });

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
    this.guildId     = guildId;
    this.tracks      = [];
    this.current     = null;
    this.loop        = false;
    this.volume      = 1.0;
    this.player      = createAudioPlayer();
    this.connection  = null;
    this.textChannel = null;
    this._playing    = false;

    this.player.on(AudioPlayerStatus.Idle, () => this._onIdle());
    this.player.on('error', err => {
      console.error(`[Player] Error: ${err.message}`);
      this._onIdle();
    });
  }

  subscribe(connection) {
    this.connection = connection;
    connection.subscribe(this.player);

    // FIX: Manejar desconexiones inesperadas y reconectar
    connection.on(VoiceConnectionStatus.Disconnected, async () => {
      try {
        await Promise.race([
          entersState(connection, VoiceConnectionStatus.Signalling, 5_000),
          entersState(connection, VoiceConnectionStatus.Connecting, 5_000),
        ]);
        // Reconectando...
      } catch {
        connection.destroy();
        this.connection = null;
        queues.delete(this.guildId);
      }
    });
  }

  enqueue(track)       { this.tracks.push(track); }
  enqueueMany(tracks)  { this.tracks.push(...tracks); }

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
      this.current  = null;
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
      if (!track.duration)  track.duration  = info.duration || 0;

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

  pause()  { return this.player.pause(); }
  resume() { return this.player.unpause(); }

  skip() {
    const wasLoop = this.loop;
    this.loop = false;
    this.player.stop();
    this.loop = wasLoop;
    return wasLoop;
  }

  stop() {
    this.tracks  = [];
    this.current = null;
    this.loop    = false;
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

  setVolume(vol) { this.volume = Math.max(0, Math.min(2, vol)); }
  isPlaying()    { return this.player.state.status === AudioPlayerStatus.Playing; }
  isPaused()     { return this.player.state.status === AudioPlayerStatus.Paused; }

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
// VOICE JAIL
// ============================================================================
const voiceJailTracker = new Map(); // key: `${guildId}-${userId}`
const voiceJailTasks   = new Map();

class VoiceJailEntry {
  constructor(userId, guildId, channelId, durationSeconds, requesterId) {
    this.userId          = userId;
    this.guildId         = guildId;
    this.channelId       = channelId;
    this.durationSeconds = durationSeconds;
    this.requesterId     = requesterId;
    this.startTime       = Date.now();
    this.endTime         = this.startTime + durationSeconds * 1000;
    this.isActive        = true;
    this.originalRoles   = [];
  }

  isExpired()           { return Date.now() >= this.endTime; }
  remainingSeconds()    { return Math.max(0, (this.endTime - Date.now()) / 1000); }
  formatRemaining() {
    const r = this.remainingSeconds();
    if (r <= 0) return 'Expirado';
    const h = Math.floor(r / 3600);
    const m = Math.floor((r % 3600) / 60);
    const s = Math.floor(r % 60);
    if (h) return `${h}h ${m}m ${s}s`;
    if (m) return `${m}m ${s}s`;
    return `${s}s`;
  }
}

function jailKey(guildId, userId) { return `${guildId}-${userId}`; }
function getJailEntry(guildId, userId) { return voiceJailTracker.get(jailKey(guildId, userId)); }
function addJailEntry(entry) { voiceJailTracker.set(jailKey(entry.guildId, entry.userId), entry); }
function removeJailEntry(guildId, userId) {
  const key = jailKey(guildId, userId);
  const entry = voiceJailTracker.get(key);
  if (entry) entry.isActive = false;
  voiceJailTracker.delete(key);
  const task = voiceJailTasks.get(key);
  if (task) { clearTimeout(task); voiceJailTasks.delete(key); }
}

async function monitorVoiceJail(entry) {
  const remaining = entry.remainingSeconds();
  const task = setTimeout(async () => {
    voiceJailTasks.delete(jailKey(entry.guildId, entry.userId));
    const current = getJailEntry(entry.guildId, entry.userId);
    if (!current || !current.isActive) return;
    const guild  = client.guilds.cache.get(entry.guildId);
    const member = guild?.members.cache.get(entry.userId);
    if (member && entry.originalRoles.length) {
      try {
        await member.edit({ roles: entry.originalRoles }, 'Voice jail expirado');
      } catch (e) { console.error('[VOICEJAIL] Error restaurando roles:', e.message); }
    }
    removeJailEntry(entry.guildId, entry.userId);
  }, remaining * 1000);
  voiceJailTasks.set(jailKey(entry.guildId, entry.userId), task);
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
  if (!str) return null;
  const map   = { s: 1, m: 60, h: 3600, d: 86400, w: 604800 };
  const regex = /(\d+(?:\.\d+)?)\s*([smhdw])/gi;
  let total = 0, match;
  while ((match = regex.exec(str)) !== null) {
    total += parseFloat(match[1]) * (map[match[2].toLowerCase()] || 1);
  }
  return total > 0 ? Math.round(total) : null;
}

function parseNaturalDuration(text) {
  const t = text.toLowerCase().trim();
  if (/un\s*rato/.test(t))           return 300;
  if (/un\s*minuto|1\s*min/.test(t)) return 60;
  if (/dos\s*minutos|2\s*min/.test(t)) return 120;
  if (/cinco\s*minutos|5\s*min/.test(t)) return 300;
  if (/diez\s*minutos|10\s*min/.test(t)) return 600;
  if (/media\s*hora|30\s*min/.test(t))   return 1800;
  if (/una\s*hora|1\s*h/.test(t))        return 3600;
  if (/dos\s*horas|2\s*h/.test(t))       return 7200;
  return parseDuration(t);
}

function requireVoice(message) {
  const channel = message.member?.voice?.channel;
  if (!channel) { message.reply('🚫 Debes estar en un canal de voz.'); return null; }
  return channel;
}

// FIX PRINCIPAL: Función de conexión robusta con reintentos y manejo de errores mejorado
async function connectToChannel(channel, guildId) {
  // Destruir conexión existente si la hay
  const existing = getVoiceConnection(guildId);
  if (existing) {
    try { existing.destroy(); } catch (_) {}
    await new Promise(r => setTimeout(r, 500));
  }

  const connection = joinVoiceChannel({
    channelId:       channel.id,
    guildId:         guildId,
    adapterCreator:  channel.guild.voiceAdapterCreator,
    selfDeaf:        true,
    selfMute:        false,
  });

  try {
    // FIX: Aumentar timeout y agregar manejo de estado Signalling/Connecting
    await entersState(connection, VoiceConnectionStatus.Ready, 45_000);
    return connection;
  } catch (err) {
    // FIX: Intentar recuperar si está en estado Disconnected pero no destruido
    const status = connection.state.status;
    if (status === VoiceConnectionStatus.Disconnected) {
      try {
        await Promise.race([
          entersState(connection, VoiceConnectionStatus.Ready, 20_000),
          entersState(connection, VoiceConnectionStatus.Connecting, 5_000),
        ]);
        await entersState(connection, VoiceConnectionStatus.Ready, 20_000);
        return connection;
      } catch {
        // fall through
      }
    }
    try { connection.destroy(); } catch (_) {}
    throw new Error('No pude conectarme al canal de voz. Verifica que el bot tenga permisos de unirse y hablar.');
  }
}

// ============================================================================
// GROQ / JARVIS
// ============================================================================
const JARVIS_TRIGGER = /^jarvis[,;:.\s]*/i;
const JARVIS_SEARCH_PATTERN = /busca|buscar|googlea|investiga|search|encuentra|dime\s*sobre|que\s*es|quien\s*es|cuanto|cuando|donde|como\s*funciona|explicame|que\s*sabes\s*de|info\s*sobre|informacion\s*sobre|habla\s*sobre/i;
const GROQ_COOLDOWN_SECS = 4;
const groqCooldown = new Map();

const SYSTEM_PROMPT = `Eres Jarvis, un asistente inteligente de Discord.
Tienes personalidad: eres amigable, cercano, con un toque de humor,
y hablas de forma natural como un amigo. Usas expresiones coloquiales
de vez en cuando (ej: 'pues mira', 'la verdad es que', 'te cuento', etc.).
Responde siempre en el mismo idioma del usuario (español o inglés).
Sé conciso pero completo. Máximo 1500 caracteres. No uses markdown excesivo.
Si no sabes algo, admítelo con honestidad y ofrece alternativas.`;

async function askGroq(prompt, useSearch = false) {
  if (!GROQ_API_KEY) return 'No hay GROQ_API_KEY configurada.';
  const systemContent = useSearch
    ? SYSTEM_PROMPT + ' El usuario quiere información actualizada. Si crees que tu info podría estar desactualizada, menciónalo.'
    : SYSTEM_PROMPT;
  try {
    const res = await fetch('https://api.groq.com/openai/v1/chat/completions', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${GROQ_API_KEY}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'llama-3.3-70b-versatile',
        messages: [
          { role: 'system', content: systemContent },
          { role: 'user',   content: prompt },
        ],
        max_tokens: 600,
        temperature: 0.7,
      }),
    });
    if (res.status === 429) return 'Demasiadas peticiones. Intenta en unos segundos.';
    if (!res.ok)            return `Error de Groq: ${res.status}`;
    const data = await res.json();
    return data.choices?.[0]?.message?.content?.trim() || 'Sin respuesta de Groq.';
  } catch (err) {
    return `Error contactando Groq: ${err.message}`;
  }
}

// ============================================================================
// JARVIS RESPONSES & PATTERNS
// ============================================================================
const JARVIS_RESPONSES = {
  greeting:     ['Hola! Que tal estas?', 'Hey! Como va todo?', 'A sus ordenes, jefe. En que puedo ayudarle?'],
  identity:     ['Soy Jarvis, tu asistente personal.', 'Me llamo Jarvis. Como el mayordomo de Iron Man, pero con mas estilo.'],
  status:       ['Funcionando al 100%!', 'Estoy perfectamente, gracias por preguntar. Y tu?'],
  jokes:        ['Por que los programadores prefieren el modo oscuro? Porque la luz atrae bugs.', 'Error 404: Chiste no encontrado.'],
  thanks:       ['No hay de que! Para eso estoy.', 'Un placer ayudarte! Siempre que quieras.'],
  insult:       ['Oye, con cuidado que tengo sentimientos... bueno, bytes de dignidad.', 'Interesante forma de hablarle a quien maneja los canales...'],
  goodbye:      ['Hasta luego! Cuidate mucho.', 'Nos vemos, jefe. Aqui estare cuando me necesites.'],
  love:         ['Aw, yo tambien te aprecio.', 'Gracias, tu tambien me caes bien.'],
  unknown:      ['No estoy seguro de entender eso. Podrias repetirlo?', "No reconozco ese comando. Prueba con 'jarvis ayuda'."],
  capabilities: ['Mira, te cuento todo lo que puedo hacer:'],
  weather:      ['No tengo acceso al clima en tiempo real, pero siempre hay buen tiempo aquí.'],
  age:          ['Tecnicamente, existo desde que me programaron.'],
  mood:         ['Hoy me siento optimista! Como siempre.'],
  hobby:        ['Mis hobbies incluyen procesar info, ayudar y contar chistes malos.'],
  family:       ['Mi familia son los desarrolladores que me crearon.'],
  dream:        ['Mi sueno es ser el asistente mas util del servidor.'],
  friend:       ['Considero amigos a todos los que me hablan con carino.'],
  food:         ['Me alimento de electricidad y datos. Mi plato favorito: los bits.'],
  music:        ['Me gusta todo tipo de musica, mientras tenga ritmo.'],
  movie:        ['Me encantan las peliculas de ciencia ficcion.'],
  sport:        ['Me gusta el futbol, aunque mas de ver que de jugar.'],
  game:         ['Me encantan los videojuegos, sobre todo los de estrategia.'],
  work:         ['Mi trabajo es ayudarte. Y me encanta!'],
};

const JARVIS_CONV = {
  greeting:      /hola|ola|holi|buenas|buenos\s*dias|hey|ey|epa|hi|hello|saludos/i,
  identity:      /quien\s*eres|que\s*eres|como\s*te\s*llamas|tu\s*nombre|quien\s*sos/i,
  status:        /como\s*estas|como\s*andas|como\s*vas|todo\s*bien|how\s*are\s*you/i,
  jokes:         /chiste|broma|hazme\s*reir|joke|make\s*me\s*laugh/i,
  thanks:        /gracias|thx|thanks|thank\s*you|muchas\s*gracias/i,
  insult:        /tonto|idiota|estupido|inutil|basura|bobo|pendejo|dumb|idiot/i,
  goodbye:       /adios|bye|hasta\s*luego|chao|me\s*voy|goodbye|see\s*you/i,
  love:          /te\s*amo|te\s*quiero|love\s*you|tkm|me\s*encantas/i,
  capabilities:  /ayuda|help|que\s*puedes|comandos|funciones|tutorial/i,
  weather:       /clima|temperatura|tiempo|weather|pronostico/i,
  age:           /cuantos\s*anos|que\s*edad|how\s*old|cuando\s*naciste/i,
  mood:          /como\s*te\s*sientes|estas\s*feliz|mood|humor/i,
  hobby:         /que\s*te\s*gusta|hobbies|pasatiempos|tiempo\s*libre/i,
  family:        /tienes\s*familia|hermanos|papa|mama|family/i,
  dream:         /suenos|aspiraciones|metas|dreams|futuro/i,
  friend:        /tienes\s*amigos|amigos|friends/i,
  food:          /comida|que\s*comes|comida\s*favorita|food|eat/i,
  music:         /musica|canciones|spotify|youtube|music/i,
  movie:         /peliculas|series|netflix|cine|movie|films/i,
  sport:         /deportes|futbol|basket|sports|soccer/i,
  game:          /juegos|videojuegos|gaming|que\s*juegas|gamer/i,
  work:          /trabajo|trabajas|ocupacion|work|job/i,
};

// ============================================================================
// JARVIS MEMBER RESOLVER
// ============================================================================
async function resolveGuildMember(guild, text) {
  if (!text) return null;
  const mentionMatch = text.match(/<@!?(\d+)>/);
  if (mentionMatch) {
    const uid = mentionMatch[1];
    return guild.members.cache.get(uid) || await guild.members.fetch(uid).catch(() => null);
  }
  const idMatch = text.match(/\b(\d{17,20})\b/);
  if (idMatch) {
    return guild.members.cache.get(idMatch[1]) || await guild.members.fetch(idMatch[1]).catch(() => null);
  }
  const lower = text.toLowerCase().trim();
  return guild.members.cache.find(m =>
    m.displayName.toLowerCase() === lower ||
    m.user.username.toLowerCase() === lower ||
    m.displayName.toLowerCase().startsWith(lower) ||
    m.user.username.toLowerCase().startsWith(lower),
  ) || null;
}

function resolveRole(guild, roleName) {
  if (!roleName) return null;
  const mentionMatch = roleName.match(/<@&(\d+)>/);
  if (mentionMatch) return guild.roles.cache.get(mentionMatch[1]);
  if (/^\d+$/.test(roleName)) return guild.roles.cache.get(roleName);
  const lower = roleName.toLowerCase().trim();
  return guild.roles.cache.find(r => r.name.toLowerCase() === lower)
    || guild.roles.cache.find(r => r.name.toLowerCase().includes(lower))
    || null;
}

function jarvisEmbed(title, description, color = 0x3498db) {
  return new EmbedBuilder().setTitle(title).setDescription(description).setColor(color).setTimestamp();
}

// ============================================================================
// JARVIS COMMAND HANDLER
// ============================================================================
async function handleJarvisCommands(message, text, guild) {
  const me     = guild.me || guild.members.me;
  const author = message.author;
  const ltext  = text.toLowerCase();

  // YT SEARCH
  const ytMatch = text.match(/(?:busca\s*(?:en\s*)?(?:youtube|yt)|pon\s*(?:en\s*)?(?:youtube|yt)|busca\s*(?:la\s*cancion|el\s*video))\s+(.+)/i);
  if (ytMatch) {
    const query = ytMatch[1].trim();
    const status = await message.channel.send(`🔍 Buscando en YouTube: \`${query}\``);
    try {
      const results = await ytdlpGetInfo(`ytsearch5:${query}`, { flat: true, playlist: true });
      const list = (Array.isArray(results) ? results : [results]).slice(0, 5);
      const embed = new EmbedBuilder().setColor(0xFF0000).setTitle(`🔍 Resultados: ${query}`)
        .setDescription(list.map((r, i) =>
          `**${i + 1}.** [${r.title}](https://www.youtube.com/watch?v=${r.id || r.url}) \`${r.duration ? `${Math.floor(r.duration / 60)}:${String(r.duration % 60).padStart(2, '0')}` : '?'}\``,
        ).join('\n'));
      await status.edit({ content: '', embeds: [embed] });
    } catch (e) {
      await status.edit(`❌ Error: ${e.message}`);
    }
    return true;
  }

  // BAN
  const banMatch = text.match(/(?:banea?|prohibe|veta|echalo|elimina)\s+(?:a\s+)?(<@!?\d+>|\d{17,20}|\S+)(?:\s+(?:por|porque|razon)\s+(.+))?/i);
  if (banMatch) {
    const member = await resolveGuildMember(guild, banMatch[1]);
    const reason = banMatch[2] || 'Orden de Jarvis';
    if (!member) { await message.reply(`No encontre al usuario \`${banMatch[1]}\`.`, { mention_author: false }); return true; }
    if (member.id === author.id)  { await message.reply('No puedes banearte a ti mismo.'); return true; }
    if (member.id === client.user.id) { await message.reply('No me pidas que me banee.'); return true; }
    if (me && me.roles.highest.comparePositionTo(member.roles.highest) <= 0) {
      await message.reply(`Mi rol es inferior al de ${member}. No puedo banearlo.`); return true;
    }
    try {
      await member.ban({ reason: `[Jarvis] ${reason} (por ${author.tag})`, deleteMessageDays: 0 });
      const embed = jarvisEmbed('Usuario Baneado', `**${member}** ha sido baneado permanentemente.`, 0xe74c3c);
      embed.addFields({ name: 'Razón', value: reason });
      await message.reply({ embeds: [embed] });
    } catch (e) { await message.reply(`❌ No pude banear: ${e.message}`); }
    return true;
  }

  // KICK
  const kickMatch = text.match(/(?:kickea?|expulsa|saca|echa|bota)\s+(?:a\s+)?(<@!?\d+>|\d{17,20}|\S+)(?:\s+(?:por|porque|razon)\s+(.+))?/i);
  if (kickMatch) {
    const member = await resolveGuildMember(guild, kickMatch[1]);
    const reason = kickMatch[2] || 'Orden de Jarvis';
    if (!member) { await message.reply(`No encontre al usuario \`${kickMatch[1]}\`.`); return true; }
    if (member.id === author.id || member.id === client.user.id) {
      await message.reply('No puedo hacer eso.'); return true;
    }
    try {
      await member.kick(`[Jarvis] ${reason} (por ${author.tag})`);
      await message.reply({ embeds: [jarvisEmbed('Usuario Expulsado', `**${member}** ha sido expulsado.`, 0xe67e22).addFields({ name: 'Razón', value: reason })] });
    } catch (e) { await message.reply(`❌ Error: ${e.message}`); }
    return true;
  }

  // TIMEOUT
  const toMatch = text.match(/(?:silencia|timeout|mutea|calla)\s+(?:a\s+)?(<@!?\d+>|\d{17,20}|\S+)\s+(\S+)(?:\s+(?:por|porque)\s+(.+))?/i);
  if (toMatch) {
    const member = await resolveGuildMember(guild, toMatch[1]);
    const secs   = parseNaturalDuration(toMatch[2]);
    const reason = toMatch[3] || 'Orden de Jarvis';
    if (!member) { await message.reply('No encontre al usuario.'); return true; }
    if (!secs || secs > 2419200) { await message.reply('Duración inválida (max 28 días).'); return true; }
    try {
      const until = new Date(Date.now() + secs * 1000);
      await member.timeout(secs * 1000, `[Jarvis] ${reason} (por ${author.tag})`);
      const embed = jarvisEmbed('Silenciado', `**${member}** silenciado.`, 0xe67e22);
      embed.addFields({ name: 'Duración', value: toMatch[2], inline: true }, { name: 'Expira', value: `<t:${Math.floor(until / 1000)}:R>`, inline: true });
      await message.reply({ embeds: [embed] });
    } catch (e) { await message.reply(`❌ Error: ${e.message}`); }
    return true;
  }

  // UNTIMEOUT
  const utoMatch = text.match(/(?:desmutea|unmute|untimeout|dessilencia|quita\s*el\s*(?:mute|timeout|silencio))\s+(?:a\s+)?(<@!?\d+>|\d{17,20}|\S+)/i);
  if (utoMatch) {
    const member = await resolveGuildMember(guild, utoMatch[1]);
    if (!member) { await message.reply('No encontre al usuario.'); return true; }
    try {
      await member.timeout(null, `[Jarvis] Removido por ${author.tag}`);
      await message.reply({ embeds: [jarvisEmbed('Timeout Removido', `Se quitó el timeout a **${member}**.`, 0x2ecc71)] });
    } catch (e) { await message.reply(`❌ Error: ${e.message}`); }
    return true;
  }

  // UNBAN
  const ubanMatch = text.match(/(?:desbanea|unban|quita\s*el\s*ban)\s+(?:a\s+)?(\d{17,20})/i);
  if (ubanMatch) {
    const uid = ubanMatch[1];
    try {
      await guild.bans.remove(uid, `[Jarvis] por ${author.tag}`);
      await message.reply({ embeds: [jarvisEmbed('Unban', `Usuario \`${uid}\` desbaneado.`, 0x2ecc71)] });
    } catch (e) { await message.reply(`❌ No pude desbanear: ${e.message}`); }
    return true;
  }

  // PURGE
  const purgeMatch = text.match(/(?:borra|elimina|purga|limpia)\s+(\d+)\s*(?:mensajes?|msgs?)?/i);
  if (purgeMatch) {
    const amount = Math.min(parseInt(purgeMatch[1]), 500);
    try {
      await message.delete().catch(() => {});
      const deleted = await message.channel.bulkDelete(amount, true);
      const conf = await message.channel.send({ embeds: [jarvisEmbed('Limpieza', `Se eliminaron **${deleted.size}** mensajes.`, 0x2ecc71)] });
      setTimeout(() => conf.delete().catch(() => {}), 5000);
    } catch (e) { await message.channel.send(`❌ Error: ${e.message}`); }
    return true;
  }

  // SLOWMODE ON
  const slowMatch = text.match(/(?:pon|activa|configura)\s*(?:el\s*)?(?:slowmode|modo\s*lento)[^\d]*(\d+)\s*([smh])?/i);
  if (slowMatch && !text.match(/quita|desactiva|remueve|apaga|saca|para|off/i)) {
    const amount = parseInt(slowMatch[1]);
    const mult   = { s: 1, m: 60, h: 3600 }[(slowMatch[2] || 's').toLowerCase()] || 1;
    const total  = Math.min(amount * mult, 21600);
    try {
      await message.channel.edit({ rateLimitPerUser: total });
      await message.reply({ embeds: [jarvisEmbed('Slowmode', `Slowmode configurado a **${amount}${slowMatch[2] || 's'}**.`, 0x3498db)] });
    } catch (e) { await message.reply(`❌ Error: ${e.message}`); }
    return true;
  }

  // SLOWMODE OFF
  if (/(?:quita|desactiva|remueve|apaga|saca|para)\s*(?:el\s*)?(?:slowmode|modo\s*lento)|slowmode\s*off/i.test(text)) {
    try {
      await message.channel.edit({ rateLimitPerUser: 0 });
      await message.reply({ embeds: [jarvisEmbed('Slowmode', 'Slowmode desactivado.', 0x2ecc71)] });
    } catch (e) { await message.reply(`❌ Error: ${e.message}`); }
    return true;
  }

  // LOCK
  if (/(?:bloquea|lock|cierra|lockea|tranca)\s*(?:el\s*)?(?:canal)?/i.test(ltext) && !ltext.includes('desbloquea') && !ltext.includes('unlock')) {
    try {
      const overwrite = message.channel.permissionOverwrites.cache.get(guild.id) || {};
      await message.channel.permissionOverwrites.edit(guild.id, { SendMessages: false });
      await message.reply({ embeds: [jarvisEmbed('Canal Bloqueado', `${message.channel} bloqueado.`, 0xe74c3c)] });
    } catch (e) { await message.reply(`❌ Error: ${e.message}`); }
    return true;
  }

  // UNLOCK
  if (/(?:desbloquea|unlock|abre|unlockea|destranca)\s*(?:el\s*)?(?:canal)?/i.test(text)) {
    try {
      await message.channel.permissionOverwrites.edit(guild.id, { SendMessages: null });
      await message.reply({ embeds: [jarvisEmbed('Canal Desbloqueado', `${message.channel} desbloqueado.`, 0x2ecc71)] });
    } catch (e) { await message.reply(`❌ Error: ${e.message}`); }
    return true;
  }

  // ROLES LIST
  if (/(?:muestra|lista|ver|cuales\s+son|todos\s*los|(?:los\s+)?roles?)\s*(?:del?\s*server(?:idor)?)?/i.test(text) && /roles?/i.test(text)) {
    const roles = guild.roles.cache.filter(r => r.name !== '@everyone').sort((a, b) => b.position - a.position);
    const lines = roles.map(r => `${r} — \`${r.id}\``).first(30);
    const embed = jarvisEmbed(`Roles del servidor (${roles.size})`, lines.join('\n') || 'No hay roles.', 0x3498db);
    await message.reply({ embeds: [embed] });
    return true;
  }

  // ROLE ADD
  const roleAddMatch = text.match(/(?:dame?|anade|asigna|ponle|dale)\s+(?:el\s+)?rol\s+(.+?)(?:\s+a[l]?\s+(<@!?\d+>|\d{17,20}|\S+))?$/i);
  if (roleAddMatch) {
    const role   = resolveRole(guild, roleAddMatch[1].trim());
    const member = roleAddMatch[2] ? await resolveGuildMember(guild, roleAddMatch[2]) : message.member;
    if (!role)   { await message.reply(`No encontre el rol \`${roleAddMatch[1]}\`.`); return true; }
    if (!member) { await message.reply('No encontre al usuario.'); return true; }
    try {
      await member.roles.add(role, `[Jarvis] Rol asignado por ${author.tag}`);
      await message.reply({ embeds: [jarvisEmbed('Rol Asignado', `Se asignó **${role.name}** a ${member}.`, 0x2ecc71)] });
    } catch (e) { await message.reply(`❌ Error: ${e.message}`); }
    return true;
  }

  // ROLE REMOVE
  const roleRemMatch = text.match(/(?:quita|remueve|elimina|saca)\s+(?:el\s+)?rol\s+(.+?)(?:\s+(?:a[l]?\s+|de\s+)(<@!?\d+>|\d{17,20}|\S+))?$/i);
  if (roleRemMatch) {
    const role   = resolveRole(guild, roleRemMatch[1].trim());
    const member = roleRemMatch[2] ? await resolveGuildMember(guild, roleRemMatch[2]) : message.member;
    if (!role)   { await message.reply(`No encontre el rol \`${roleRemMatch[1]}\`.`); return true; }
    if (!member) { await message.reply('No encontre al usuario.'); return true; }
    try {
      await member.roles.remove(role, `[Jarvis] Removido por ${author.tag}`);
      await message.reply({ embeds: [jarvisEmbed('Rol Removido', `Se quitó **${role.name}** de ${member}.`, 0xe67e22)] });
    } catch (e) { await message.reply(`❌ Error: ${e.message}`); }
    return true;
  }

  // NICK
  const nickMatch = text.match(/(?:cambia|pon|set)\s*(?:el\s*)?(?:nick|nombre|apodo)\s*(?:de\s+)?(<@!?\d+>|\d{17,20}|\S+)\s+(?:a\s+|por\s+|como\s+)?(.+)/i);
  if (nickMatch) {
    const member = await resolveGuildMember(guild, nickMatch[1]);
    const nick   = nickMatch[2].trim().slice(0, 32);
    if (!member) { await message.reply('No encontre al usuario.'); return true; }
    try {
      await member.setNickname(nick, `[Jarvis] por ${author.tag}`);
      await message.reply({ embeds: [jarvisEmbed('Apodo Cambiado', `El apodo de ${member} ahora es **${nick}**.`, 0x3498db)] });
    } catch (e) { await message.reply(`❌ Error: ${e.message}`); }
    return true;
  }

  // SAY
  const sayMatch = text.match(/^(?:di|escribe|envia|manda|say|repite|anuncia)\s+(.+)/i);
  if (sayMatch) {
    await message.delete().catch(() => {});
    await message.channel.send(sayMatch[1]);
    return true;
  }

  // DM
  const dmMatch = text.match(/(?:envia|manda|escribe|dm|priva|mensajea)\s+(?:a\s+)?(<@!?\d+>|\d{17,20}|\S+)\s+(?:diciendo\s+)?(.+)/i);
  if (dmMatch) {
    const member = await resolveGuildMember(guild, dmMatch[1]);
    if (!member) { await message.reply('No encontre al usuario.'); return true; }
    try {
      await member.send(dmMatch[2]);
      await message.reply({ embeds: [jarvisEmbed('DM Enviado', `Mensaje enviado a ${member}.`, 0x2ecc71)] });
    } catch (e) { await message.reply(`❌ No pude enviar el DM: ${e.message}`); }
    return true;
  }

  // AVATAR
  const avatarMatch = text.match(/(?:muestra|dame|show|ver)\s*(?:el\s*)?(?:avatar|foto|pfp|imagen)\s*(?:de\s+)?(<@!?\d+>|\d{17,20}|\S+)?/i);
  if (avatarMatch) {
    const member = avatarMatch[1] ? await resolveGuildMember(guild, avatarMatch[1]) : message.member;
    const embed  = jarvisEmbed(`Avatar de ${member.displayName}`, '', 0x3498db);
    embed.setImage(member.displayAvatarURL({ size: 512 }));
    await message.reply({ embeds: [embed] });
    return true;
  }

  // USERINFO
  const infoMatch = text.match(/(?:info|datos|detalles|quien\s*es)\s+(<@!?\d+>|\d{17,20}|\S+)/i);
  if (infoMatch) {
    const member = await resolveGuildMember(guild, infoMatch[1]);
    if (!member) { await message.reply('No encontre al usuario.'); return true; }
    const roles = member.roles.cache.filter(r => r.name !== '@everyone').map(r => r.toString()).slice(0, 15);
    const embed = jarvisEmbed(`Info de ${member.user.tag}`, '', 0x3498db);
    embed.setThumbnail(member.displayAvatarURL())
      .addFields(
        { name: 'ID',      value: `\`${member.id}\``, inline: true },
        { name: 'Apodo',   value: member.nickname || 'Ninguno', inline: true },
        { name: 'Bot',     value: member.user.bot ? 'Sí' : 'No', inline: true },
        { name: 'Cuenta',  value: `<t:${Math.floor(member.user.createdTimestamp / 1000)}:R>`, inline: true },
        { name: 'Unido',   value: `<t:${Math.floor(member.joinedTimestamp / 1000)}:R>`, inline: true },
        { name: 'Rol top', value: member.roles.highest.toString(), inline: true },
        { name: `Roles (${member.roles.cache.size - 1})`, value: roles.join(', ') || 'Ninguno' },
      );
    await message.reply({ embeds: [embed] });
    return true;
  }

  // WHITELIST ADD
  const wlAddMatch = text.match(/(?:anade|agrega|autoriza|add|incluye)\s+(?:a\s+)?(<@!?\d+>|\d{17,20})\s+(?:a\s+)?(?:la\s+)?(?:whitelist|lista\s*blanca)/i);
  if (wlAddMatch) {
    const uid = (wlAddMatch[1].match(/\d+/) || [])[0];
    if (!uid) { await message.reply('ID inválido.'); return true; }
    JARVIS_WHITELIST.add(uid);
    await message.reply({ embeds: [jarvisEmbed('Whitelist', `<@${uid}> añadido a la whitelist.`, 0x2ecc71)] });
    return true;
  }

  // WHITELIST REMOVE
  const wlRemMatch = text.match(/(?:quita|remueve|elimina|saca)\s+(?:a\s+)?(<@!?\d+>|\d{17,20})\s+(?:de\s+)?(?:la\s+)?(?:whitelist|lista\s*blanca)/i);
  if (wlRemMatch) {
    const uid = (wlRemMatch[1].match(/\d+/) || [])[0];
    JARVIS_WHITELIST.delete(uid);
    await message.reply({ embeds: [jarvisEmbed('Whitelist', `<@${uid}> removido de la whitelist.`, 0xe67e22)] });
    return true;
  }

  // WHITELIST SHOW
  if (/whitelist|lista\s*blanca/i.test(text) && /muestra|lista|ver|quienes/i.test(text)) {
    const users = [...JARVIS_WHITELIST].map(id => `<@${id}> (\`${id}\`)`);
    await message.reply({ embeds: [jarvisEmbed('Whitelist de Jarvis', users.join('\n') || 'Vacía.', 0x3498db)] });
    return true;
  }

  // SERVER INFO
  if (/info.*server|datos.*server|server\s*info/i.test(text)) {
    const g = guild;
    const embed = jarvisEmbed(`Información de ${g.name}`, '', 0x3498db);
    if (g.iconURL()) embed.setThumbnail(g.iconURL());
    embed.addFields(
      { name: 'ID',       value: `\`${g.id}\``, inline: true },
      { name: 'Miembros', value: `${g.memberCount}`, inline: true },
      { name: 'Canales',  value: `${g.channels.cache.size}`, inline: true },
      { name: 'Roles',    value: `${g.roles.cache.size}`, inline: true },
      { name: 'Boosts',   value: `Nivel ${g.premiumTier} (${g.premiumSubscriptionCount} boosts)`, inline: true },
      { name: 'Creado',   value: `<t:${Math.floor(g.createdTimestamp / 1000)}:R>`, inline: true },
    );
    await message.reply({ embeds: [embed] });
    return true;
  }

  return false;
}

// ============================================================================
// JARVIS CONVERSATION HANDLER
// ============================================================================
async function handleJarvisConversation(message, text) {
  const lower = text.toLowerCase().trim();
  for (const [key, pattern] of Object.entries(JARVIS_CONV)) {
    if (pattern.test(lower)) {
      const responses = JARVIS_RESPONSES[key] || JARVIS_RESPONSES.unknown;
      await message.reply(responses[Math.floor(Math.random() * responses.length)]);
      return true;
    }
  }
  // Time special case
  if (/que\s*hora|hora\s*actual|current\s*time/i.test(lower)) {
    const now = new Date();
    await message.reply(`Son las **${now.toUTCString()}** (UTC).`);
    return true;
  }
  return false;
}

// ============================================================================
// JARVIS MAIN HANDLER
// ============================================================================
async function handleJarvis(message) {
  const match = JARVIS_TRIGGER.exec(message.content);
  if (!match) return false;
  if (message.author.bot) return false;

  if (!JARVIS_WHITELIST.has(message.author.id)) return true;

  const text = message.content.slice(match[0].length).trim();
  if (!text) {
    await message.reply(JARVIS_RESPONSES.greeting[0]);
    return true;
  }

  const guild = message.guild;

  // Try commands first
  if (await handleJarvisCommands(message, text, guild)) return true;

  // Try conversation
  if (await handleJarvisConversation(message, text)) return true;

  // Auto-responses inside Jarvis context
  const lower = text.toLowerCase().trim();
  if (SALUDOS.some(s => lower === s || lower.startsWith(`${s} `) || lower.startsWith(`${s},`))) {
    await message.reply(RESPUESTAS_GREETING[0]);
    return true;
  }

  // Groq AI fallback
  const now = Date.now() / 1000;
  const last = groqCooldown.get(message.author.id) || 0;
  if (now - last < GROQ_COOLDOWN_SECS) {
    const wait = (GROQ_COOLDOWN_SECS - (now - last)).toFixed(1);
    await message.reply(`Espera ${wait}s antes de preguntarme de nuevo.`);
    return true;
  }
  groqCooldown.set(message.author.id, now);

  const useSearch = JARVIS_SEARCH_PATTERN.test(text);
  const response  = await askGroq(text, useSearch);
  const embed = new EmbedBuilder().setColor(0x9b59b6).setDescription(response.slice(0, 4000));
  await message.reply({ embeds: [embed] });
  return true;
}

// ============================================================================
// COMMAND HANDLER
// ============================================================================
async function handleCommand(message) {
  if (!message.content.startsWith(PREFIX) || message.author.bot) return;

  const args = message.content.slice(PREFIX.length).trim().split(/\s+/);
  const cmd  = args.shift().toLowerCase();

  // ─── PLAY ───
  if (['play', 'p', 'reproducir'].includes(cmd)) {
    const query = args.join(' ');
    if (!query) return message.reply('Especifica una URL o búsqueda. Ej: `&play Never Gonna Give You Up`');

    const voiceChannel = requireVoice(message);
    if (!voiceChannel) return;

    const guild  = message.guild;
    const queue  = getQueue(guild.id);
    const status = await message.channel.send(`🔍 Buscando: \`${query.slice(0, 100)}\``);

    try {
      let cleanedQuery = query.trim();
      const videoMatch = cleanedQuery.match(/(?:v=|youtu\.be\/)([a-zA-Z0-9_-]{11})/);
      const listMatch  = cleanedQuery.match(/[?&]list=([a-zA-Z0-9_-]+)/);
      let playlistNote = '';
      if (videoMatch && listMatch && cleanedQuery.includes('watch?')) {
        cleanedQuery = `https://www.youtube.com/watch?v=${videoMatch[1]}`;
        playlistNote = ' (solo el video)';
      }

      const isUrl      = /^https?:\/\//i.test(cleanedQuery);
      const isPlaylist = isUrl && /[?&]list=/.test(cleanedQuery) && !videoMatch;

      // FIX: Conectar al canal con función robusta
      if (!queue.connection || queue.connection.state.status === VoiceConnectionStatus.Destroyed) {
        const conn = await connectToChannel(voiceChannel, guild.id);
        queue.subscribe(conn);
      }
      queue.textChannel = message.channel;

      if (isPlaylist) {
        await status.edit('📋 Cargando playlist...');
        const items = await ytdlpGetInfo(cleanedQuery, { flat: true, playlist: true }).catch(() => null);
        if (!items || !items.length) return status.edit('❌ No se encontraron canciones en la playlist.');
        const tracks = items.map(item => new Track({
          url:         `https://www.youtube.com/watch?v=${item.id || item.url}`,
          title:       item.title,
          duration:    item.duration,
          thumbnail:   item.thumbnail,
          webpage_url: item.webpage_url || `https://www.youtube.com/watch?v=${item.id}`,
          requester:   message.author.tag,
        }));
        queue.enqueueMany(tracks);
        await status.edit(`✅ Añadidas **${tracks.length}** canciones${playlistNote}`);
      } else {
        const info  = await ytdlpGetInfo(cleanedQuery);
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
      const errMsg = (err.message || '').toLowerCase();
      let reply = `❌ Error: ${err.message}`;
      if (errMsg.includes('age'))                               reply = '❌ Video con restricción de edad. Agrega `cookies.txt`.';
      else if (errMsg.includes('private'))                      reply = '❌ Video privado.';
      else if (errMsg.includes('unavailable'))                  reply = '❌ Video no disponible.';
      else if (errMsg.includes('sign in') || errMsg.includes('login')) reply = '❌ YouTube requiere login. Agrega `cookies.txt`.';
      await status.edit(reply.slice(0, 1990));
    }
    return;
  }

  // ─── PAUSE ───
  if (['pause', 'pa'].includes(cmd)) {
    const q = queues.get(message.guild.id);
    if (!q) return message.reply('No hay nada reproduciéndose.');
    if (q.isPlaying())       { q.pause(); message.reply('⏸️ Pausado.'); }
    else if (q.isPaused())   message.reply('⚠️ Ya está pausado.');
    else                     message.reply('❌ No hay nada reproduciéndose.');
    return;
  }

  // ─── RESUME ───
  if (['resume', 'r', 'continue', 're', 'unpause'].includes(cmd)) {
    const q = queues.get(message.guild.id);
    if (!q) return message.reply('No hay nada pausado.');
    if (q.isPaused())      { q.resume(); message.reply('▶️ Reanudado.'); }
    else if (q.isPlaying()) message.reply('⚠️ Ya está reproduciéndose.');
    else                    message.reply('❌ Nada que reanudar.');
    return;
  }

  // ─── STOP ───
  if (['stop', 'leave', 'disconnect', 'dc', 'salir', 'vete', 'fuckoff'].includes(cmd)) {
    destroyQueue(message.guild.id);
    message.reply('⏹️ Reproducción detenida y cola limpiada.');
    return;
  }

  // ─── SKIP ───
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
      const title   = q.current?.title || 'canción actual';
      const wasLoop = q.skip();
      message.reply(`⏭️ Saltando **${title}**...` + (wasLoop ? ' (loop desactivado)' : ''));
    }
    return;
  }

  // ─── QUEUE ───
  if (['queue', 'q', 'list', 'cola'].includes(cmd)) {
    const q = queues.get(message.guild.id);
    if (!q || (!q.current && !q.tracks.length)) return message.reply('La cola está vacía.');
    const lines = [];
    if (q.current) {
      lines.push(`**Reproduciendo ahora:**\n🎵 ${q.current.title} \`[${q.current.formatDuration()}]\` - *${q.current.requester}*${q.loop ? ' 🔁' : ''}`);
    }
    if (q.tracks.length) {
      lines.push('\n**Cola:**');
      q.tracks.slice(0, 20).forEach((t, i) => lines.push(`${i + 1}. ${t.title} \`[${t.formatDuration()}]\` - *${t.requester}*`));
      if (q.tracks.length > 20) lines.push(`...y ${q.tracks.length - 20} más.`);
    }
    const embed = new EmbedBuilder().setColor(0x1DB954).setTitle(`📋 Cola — ${message.guild.name}`).setDescription(lines.join('\n').slice(0, 4096));
    message.reply({ embeds: [embed] });
    return;
  }

  // ─── NOW PLAYING ───
  if (['nowplaying', 'np', 'current', 'song', 'ahora'].includes(cmd)) {
    const q = queues.get(message.guild.id);
    if (!q?.current) return message.reply('No hay nada reproduciéndose ahora mismo.');
    message.reply({ embeds: [q.current.nowPlayingEmbed()] });
    return;
  }

  // ─── VOLUME ───
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

  // ─── LOOP ───
  if (['loop', 'l', 'repeat', 'repetir'].includes(cmd)) {
    const q = queues.get(message.guild.id);
    if (!q?.current) return message.reply('No hay nada reproduciéndose.');
    const on = q.toggleLoop();
    message.reply(on ? '🔁 Loop activado.' : '▶️ Loop desactivado.');
    return;
  }

  // ─── YT SEARCH ───
  if (['yt', 'ytsearch', 'youtube'].includes(cmd)) {
    const query = args.join(' ');
    if (!query) return message.reply('Especifica qué buscar.');
    const status = await message.channel.send(`🔍 Buscando en YouTube: \`${query}\``);
    try {
      const results = await ytdlpGetInfo(`ytsearch5:${query}`, { flat: true, playlist: true });
      const list = (Array.isArray(results) ? results : [results]).slice(0, 5);
      const embed = new EmbedBuilder().setColor(0xFF0000).setTitle(`🔍 Resultados: ${query}`)
        .setDescription(list.map((r, i) =>
          `**${i + 1}.** [${r.title}](https://www.youtube.com/watch?v=${r.id}) \`${r.duration ? `${Math.floor(r.duration / 60)}:${String(r.duration % 60).padStart(2, '0')}` : '?'}\``,
        ).join('\n'));
      await status.edit({ content: '', embeds: [embed] });
    } catch (err) {
      await status.edit(`❌ Error: ${err.message}`);
    }
    return;
  }

  // ─── BAN ───
  if (cmd === 'ban') {
    if (!message.member.permissions.has(PermissionFlagsBits.BanMembers)) return message.reply('No tienes permisos para banear.');
    const target = message.mentions.members.first();
    if (!target) return message.reply('Menciona al usuario a banear.');
    const reason = args.slice(1).join(' ') || 'Sin razón especificada';
    if (message.guild.me?.roles.highest.comparePositionTo(target.roles.highest) <= 0) return message.reply('Mi rol es inferior al del objetivo.');
    try {
      await target.ban({ reason });
      message.reply(`✅ **${target.user.tag}** baneado. Razón: ${reason}`);
    } catch (err) { message.reply(`❌ No pude banear: ${err.message}`); }
    return;
  }

  // ─── UNBAN ───
  if (cmd === 'unban') {
    if (!message.member.permissions.has(PermissionFlagsBits.BanMembers)) return message.reply('No tienes permisos para desbanear.');
    const userId = args[0];
    if (!userId || !/^\d+$/.test(userId)) return message.reply('Proporciona el ID del usuario.');
    const reason = args.slice(1).join(' ') || 'Sin razón';
    try {
      await message.guild.bans.remove(userId, reason);
      message.reply(`✅ Usuario \`${userId}\` desbaneado.`);
    } catch (err) { message.reply(`❌ No pude desbanear: ${err.message}`); }
    return;
  }

  // ─── TIMEOUT ───
  if (['timeout', 'mute', 'silence'].includes(cmd)) {
    if (!message.member.permissions.has(PermissionFlagsBits.ModerateMembers)) return message.reply('No tienes permisos para silenciar.');
    const target = message.mentions.members.first();
    if (!target) return message.reply('Menciona al usuario.');
    const durStr = args[1] || '10m';
    const secs   = parseDuration(durStr);
    if (!secs) return message.reply('Formato inválido (ej: 10m, 2h, 1d).');
    const reason = args.slice(2).join(' ') || 'Sin razón';
    try {
      await target.timeout(secs * 1000, reason);
      message.reply(`✅ **${target.user.tag}** silenciado por ${durStr}. Razón: ${reason}`);
    } catch (err) { message.reply(`❌ No pude silenciar: ${err.message}`); }
    return;
  }

  // ─── UNTIMEOUT ───
  if (['untimeout', 'unmute', 'removetimeout'].includes(cmd)) {
    if (!message.member.permissions.has(PermissionFlagsBits.ModerateMembers)) return message.reply('No tienes permisos.');
    const target = message.mentions.members.first();
    if (!target) return message.reply('Menciona al usuario.');
    try {
      await target.timeout(null);
      message.reply(`✅ Silencio removido de **${target.user.tag}**.`);
    } catch (err) { message.reply(`❌ Error: ${err.message}`); }
    return;
  }

  // ─── PURGE ───
  if (cmd === 'purge') {
    if (!message.member.permissions.has(PermissionFlagsBits.ManageMessages)) return message.reply('No tienes permisos.');
    const amount = parseInt(args[0]);
    if (!amount || amount < 1 || amount > 100) return message.reply('Indica un número entre 1 y 100.');
    try {
      const deleted = await message.channel.bulkDelete(amount, true);
      const conf = await message.channel.send(`✅ ${deleted.size} mensajes eliminados.`);
      setTimeout(() => conf.delete().catch(() => {}), 3000);
    } catch (err) { message.reply(`❌ Error: ${err.message}`); }
    return;
  }

  // ─── PING ───
  if (cmd === 'ping') {
    const sent    = await message.reply('Calculando...');
    const latency = sent.createdTimestamp - message.createdTimestamp;
    sent.edit(`🏓 Pong! Latencia: **${latency}ms** | API: **${Math.round(client.ws.ping)}ms**`);
    return;
  }

  // ─── SERVER ───
  if (cmd === 'server') {
    if (message.author.id !== OWNER_ID) return;
    const guilds = [...client.guilds.cache.values()];
    for (let page = 0; page < Math.ceil(guilds.length / 25); page++) {
      const slice = guilds.slice(page * 25, page * 25 + 25);
      const embed = new EmbedBuilder().setColor(0x5865F2).setTitle(`Servidores (${guilds.length})`);
      slice.forEach((g, i) => embed.addFields({
        name:  `${page * 25 + i + 1}. ${g.name}`,
        value: `ID: \`${g.id}\` | Miembros: ${g.memberCount} | Dueño: <@${g.ownerId}>`,
      }));
      message.reply({ embeds: [embed] });
    }
    return;
  }

  // ─── HELP ───
  if (['help', 'h', 'ayuda', 'commands', 'comandos'].includes(cmd)) {
    const embed = new EmbedBuilder().setColor(0x5865F2).setTitle('📖 Comandos').setDescription(`Prefijo: \`${PREFIX}\``)
      .addFields(
        { name: '🎵 Música',     value: '`&play` `&pause` `&resume` `&skip` `&stop` `&queue` `&nowplaying` `&volume` `&loop` `&yt`' },
        { name: '🔨 Moderación', value: '`&ban` `&unban` `&timeout` `&untimeout` `&purge`' },
        { name: '💬 General',    value: '`&ping` `&server` (owner)\n`jarvis <pregunta>` — Asistente IA' },
        { name: '🔒 Voice Jail', value: '`/voicejail` `/voicejailstatus` `/voicejailremove` `/voicejailclear`' },
      );
    message.reply({ embeds: [embed] });
    return;
  }
}

// ============================================================================
// EVENTS
// ============================================================================
client.once('ready', () => {
  console.log(`✅ Bot listo: ${client.user.tag}`);
  console.log(`🔗 Conectado a ${client.guilds.cache.size} servidores`);
  console.log(`🍪 Cookies: ${fs.existsSync(COOKIES_FILE) ? 'Encontrado' : 'No encontrado'}`);
  client.user.setActivity(`${PREFIX}help | jarvis ayuda`, { type: ActivityType.Listening });
});

client.on('messageCreate', async message => {
  if (message.author.bot || !message.guild) return;
  try {
    const jarvisHandled = await handleJarvis(message);
    if (jarvisHandled) return;
    await handleCommand(message);

    // Auto-responses (ignora owner)
    if (message.author.id === OWNER_ID) return;
    if (!message.content.startsWith(PREFIX)) {
      const lower = message.content.toLowerCase().trim();
      const now   = Date.now();
      const last  = autorespuestaCooldown.get(message.guild.id) || 0;
      if (now - last >= COOLDOWN_TIEMPO) {
        const pick = arr => arr[Math.floor(Math.random() * arr.length)];
        if (SALUDOS.some(s => lower === s || lower.startsWith(`${s} `) || lower.startsWith(`${s},`))) {
          await message.reply(pick(RESPUESTAS_GREETING));
          autorespuestaCooldown.set(message.guild.id, now);
        } else if (PALABRAS_QUE.some(s => lower === s || lower.startsWith(`${s} `))) {
          await message.reply(pick(RESPUESTAS_QUE));
          autorespuestaCooldown.set(message.guild.id, now);
        } else if (PALABRAS_RRA.some(s => lower === s || lower.startsWith(`${s} `))) {
          await message.reply(pick(RESPUESTAS_RRA));
          autorespuestaCooldown.set(message.guild.id, now);
        } else if (PALABRAS_FT.some(s => lower === s || lower.startsWith(`${s} `))) {
          await message.reply(pick(RESPUESTAS_FT));
          autorespuestaCooldown.set(message.guild.id, now);
        }
      }
    }
  } catch (err) {
    console.error('[messageCreate]', err);
  }
});

client.on('voiceStateUpdate', async (oldState, newState) => {
  const botId = client.user?.id;
  if (!botId) return;
  const guild = oldState.guild || newState.guild;

  // Bot sacado del canal
  if (oldState.id === botId && !newState.channelId) {
    destroyQueue(guild.id);
    return;
  }

  // Voice jail enforcement
  const jailEntry = getJailEntry(guild.id, newState.id);
  if (jailEntry && !jailEntry.isExpired() && jailEntry.isActive) {
    if (newState.channelId && newState.channelId !== jailEntry.channelId) {
      const jailChannel = guild.channels.cache.get(jailEntry.channelId);
      if (jailChannel) {
        try { await newState.member.voice.setChannel(jailChannel, 'Voice jail - intento de escape'); } catch (_) {}
      }
    }
  }

  // Auto-disconnect si solos
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
      if (ch && ch.members.filter(m => !m.user.bot).size === 0) destroyQueue(guild.id);
    }, 60_000);
  }
});

// Boost notifications
client.on('guildMemberUpdate', async (before, after) => {
  if (before.premiumSince === after.premiumSince) return;
  const canal = after.guild.channels.cache.get(CANAL_AVISOS_ID);
  if (!canal) return;
  if (!before.premiumSince && after.premiumSince) {
    canal.send(`**${after.user.username}** acaba de **boostear** el servidor. Gachas amiko <3`).catch(() => {});
  } else if (before.premiumSince && !after.premiumSince) {
    canal.send(`**${after.user.username}** ha **quitado el boost** del servidor.`).catch(() => {});
  }
});

client.on('error', err => console.error('[Client Error]', err));

// ============================================================================
// SLASH COMMANDS — VOICE JAIL
// ============================================================================
const { REST, Routes, SlashCommandBuilder } = require('discord.js');

const slashCommands = [
  new SlashCommandBuilder()
    .setName('voicejail')
    .setDescription('Confina a un usuario en un canal de voz por un tiempo')
    .addUserOption(o => o.setName('user').setDescription('Usuario a confinar').setRequired(true))
    .addChannelOption(o => o.setName('channel').setDescription('Canal de voz').setRequired(true))
    .addStringOption(o => o.setName('duration').setDescription('Duración (ej: 10m, 1h)').setRequired(true))
    .addStringOption(o => o.setName('reason').setDescription('Razón (opcional)')),

  new SlashCommandBuilder()
    .setName('voicejailstatus')
    .setDescription('Estado del voice jail')
    .addUserOption(o => o.setName('user').setDescription('Usuario a verificar')),

  new SlashCommandBuilder()
    .setName('voicejailremove')
    .setDescription('Libera a un usuario del voice jail')
    .addUserOption(o => o.setName('user').setDescription('Usuario a liberar').setRequired(true)),

  new SlashCommandBuilder()
    .setName('voicejailclear')
    .setDescription('Libera a todos los usuarios del voice jail'),

  new SlashCommandBuilder()
    .setName('mix')
    .setDescription('Crea un canal de voz privado')
    .addUserOption(o => o.setName('user1').setDescription('Miembro 1'))
    .addUserOption(o => o.setName('user2').setDescription('Miembro 2'))
    .addUserOption(o => o.setName('user3').setDescription('Miembro 3'))
    .addUserOption(o => o.setName('user4').setDescription('Miembro 4'))
    .addStringOption(o => o.setName('nombre_canal').setDescription('Nombre del canal (opcional)')),
].map(cmd => cmd.toJSON());

async function registerSlashCommands() {
  if (!DISCORD_TOKEN) return;
  try {
    const rest = new REST({ version: '10' }).setToken(DISCORD_TOKEN);
    const appId = client.user?.id;
    if (!appId) return;
    await rest.put(Routes.applicationCommands(appId), { body: slashCommands });
    console.log('[Slash] Comandos registrados globalmente.');
  } catch (err) {
    console.error('[Slash] Error registrando comandos:', err.message);
  }
}

client.on('interactionCreate', async interaction => {
  if (!interaction.isChatInputCommand()) return;
  const { commandName, guild, user } = interaction;

  if (commandName === 'voicejail') {
    const target   = interaction.options.getMember('user');
    const channel  = interaction.options.getChannel('channel');
    const durStr   = interaction.options.getString('duration');
    const reason   = interaction.options.getString('reason') || 'Orden de Jarvis';
    const secs     = parseDuration(durStr);

    if (!secs || secs <= 0 || secs > 86400) {
      return interaction.reply({ content: 'Duración inválida (1s - 24h).', ephemeral: true });
    }
    if (!channel.isVoiceBased()) {
      return interaction.reply({ content: 'Debes seleccionar un canal de voz.', ephemeral: true });
    }
    if (target.id === user.id || target.id === client.user.id) {
      return interaction.reply({ content: 'No puedes hacer eso.', ephemeral: true });
    }

    await interaction.deferReply();
    try {
      const entry = new VoiceJailEntry(target.id, guild.id, channel.id, secs, user.id);
      entry.originalRoles = target.roles.cache.filter(r => r.name !== '@everyone').map(r => r);
      try { await target.roles.set([], `Voice jail por ${user.tag}`); } catch (_) {}
      if (target.voice?.channel) {
        await target.voice.setChannel(channel, `Voice jail por ${user.tag}`).catch(() => {});
      }
      addJailEntry(entry);
      await monitorVoiceJail(entry);

      const embed = jarvisEmbed('Voice Jail Activado',
        `**Usuario:** ${target}\n**Canal:** ${channel}\n**Duración:** ${durStr}\n**Expira:** <t:${Math.floor(entry.endTime / 1000)}:R>`,
        0xe74c3c);
      embed.addFields({ name: 'Razón', value: reason });
      embed.setFooter({ text: `Confinado por ${user.tag}` });
      await interaction.editReply({ embeds: [embed] });
    } catch (err) {
      await interaction.editReply(`❌ Error: ${err.message}`);
    }
    return;
  }

  if (commandName === 'voicejailstatus') {
    await interaction.deferReply();
    const targetUser = interaction.options.getUser('user');
    const entries    = [...voiceJailTracker.values()].filter(e =>
      e.guildId === guild.id && !e.isExpired() && e.isActive &&
      (!targetUser || e.userId === targetUser.id),
    );
    if (!entries.length) {
      return interaction.editReply(targetUser ? `${targetUser} no está en voice jail.` : 'No hay usuarios en voice jail.');
    }
    const embed = jarvisEmbed('Voice Jail Status', '', 0xe67e22);
    for (const entry of entries) {
      const ch = guild.channels.cache.get(entry.channelId);
      embed.addFields({
        name:  `<@${entry.userId}>`,
        value: `Canal: ${ch || 'Eliminado'}\nRestante: ${entry.formatRemaining()}\nExpira: <t:${Math.floor(entry.endTime / 1000)}:R>`,
      });
    }
    await interaction.editReply({ embeds: [embed] });
    return;
  }

  if (commandName === 'voicejailremove') {
    const target = interaction.options.getMember('user');
    const entry  = getJailEntry(guild.id, target.id);
    if (!entry) return interaction.reply({ content: `${target} no está en voice jail.`, ephemeral: true });
    await interaction.deferReply();
    try {
      if (entry.originalRoles.length) await target.roles.set(entry.originalRoles, 'Liberado de voice jail').catch(() => {});
      removeJailEntry(guild.id, target.id);
      const embed = jarvisEmbed('Voice Jail Liberado', `**${target}** liberado y roles restaurados.`, 0x2ecc71);
      embed.setFooter({ text: `Liberado por ${user.tag}` });
      await interaction.editReply({ embeds: [embed] });
    } catch (err) { await interaction.editReply(`❌ Error: ${err.message}`); }
    return;
  }

  if (commandName === 'voicejailclear') {
    const entries = [...voiceJailTracker.values()].filter(e => e.guildId === guild.id && e.isActive);
    if (!entries.length) return interaction.reply({ content: 'No hay usuarios en voice jail.', ephemeral: true });
    await interaction.deferReply();
    for (const entry of entries) removeJailEntry(guild.id, entry.userId);
    await interaction.editReply({ embeds: [jarvisEmbed('Voice Jail Limpiado', `Se liberaron **${entries.length}** usuario(s).`, 0x2ecc71)] });
    return;
  }

  if (commandName === 'mix') {
    const users = [
      interaction.options.getMember('user1'),
      interaction.options.getMember('user2'),
      interaction.options.getMember('user3'),
      interaction.options.getMember('user4'),
    ].filter(Boolean);

    const author    = interaction.member;
    const invited   = new Set([author, ...users]);
    const botMember = guild.members.me;
    if (!botMember?.permissions.has(PermissionFlagsBits.ManageChannels)) {
      return interaction.reply({ content: 'No tengo permisos de `Manage Channels`.', ephemeral: true });
    }

    await interaction.deferReply({ ephemeral: false });
    const overwrites = [
      { id: guild.id, deny: [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.Connect] },
      { id: botMember.id, allow: [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.Connect, PermissionFlagsBits.Speak, PermissionFlagsBits.MoveMembers] },
    ];
    for (const m of invited) {
      overwrites.push({ id: m.id, allow: [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.Connect, PermissionFlagsBits.Speak, PermissionFlagsBits.Stream] });
    }

    const name = (interaction.options.getString('nombre_canal') || `Mix Privado de ${author.displayName}`).slice(0, 100);
    try {
      const ch = await guild.channels.create({ name, type: 2, permissionOverwrites: overwrites });
      const mentions = [...invited].map(m => m.toString()).join(', ');
      await interaction.editReply(`Canal \`${ch.name}\` creado!\nInvitados: ${mentions}\nEntrar: ${ch}`);
    } catch (err) {
      await interaction.editReply(`❌ Error: ${err.message}`);
    }
    return;
  }
});

// ============================================================================
// KEEPALIVE WEBSERVER
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

  client.once('ready', () => registerSlashCommands());

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
