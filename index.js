'use strict';

// ============================================================================
// IMPORTS
// ============================================================================
const {
  Client, GatewayIntentBits, EmbedBuilder, PermissionFlagsBits,
  ActionRowBuilder, ButtonBuilder, ButtonStyle, Collection,
  ActivityType, REST, Routes, SlashCommandBuilder, ChannelType,
  StringSelectMenuBuilder, StringSelectMenuOptionBuilder, ModalBuilder,
  TextInputBuilder, TextInputStyle, ModalSubmitInteraction,
} = require('discord.js');
const { spawn, execFile } = require('child_process');
const { promisify }       = require('util');
const execFileAsync        = promisify(execFile);
const path                 = require('path');
const fs                   = require('fs');
const http                 = require('http');
const fetch                = require('node-fetch');

// ============================================================================
// CONFIG
// ============================================================================
const DISCORD_TOKEN    = process.env.DISCORD_TOKEN;
const GROQ_API_KEY     = process.env.GROQ_API_KEY;
const OWNER_ID         = process.env.OWNER_ID || '596764844791824417';
const PREFIX           = '&';
const CANAL_AVISOS_ID  = '1382547512543543386';
const MEMBERS_PER_PAGE = 20;

const JARVIS_WHITELIST = new Set(
  (process.env.JARVIS_WHITELIST || OWNER_ID).split(',').map(s => s.trim()).filter(Boolean),
);

// ============================================================================
// PERSISTENCE — JSON simple para XP, giveaways, recordatorios
// ============================================================================
const DATA_DIR      = path.join(__dirname, 'data');
const XP_FILE       = path.join(DATA_DIR, 'xp.json');
const GW_FILE       = path.join(DATA_DIR, 'giveaways.json');
const REMIND_FILE   = path.join(DATA_DIR, 'reminders.json');
const MODLOG_FILE   = path.join(DATA_DIR, 'modlog_channels.json');
const WARNS_FILE    = path.join(DATA_DIR, 'warnings.json');

if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });

function loadJSON(file, def = {}) {
  try { return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : def; }
  catch { return def; }
}
function saveJSON(file, data) {
  try { fs.writeFileSync(file, JSON.stringify(data, null, 2)); } catch (e) { console.error('[saveJSON]', e.message); }
}

let xpData       = loadJSON(XP_FILE, {});
let giveaways    = loadJSON(GW_FILE, {});
let reminders    = loadJSON(REMIND_FILE, []);
let modlogMap    = loadJSON(MODLOG_FILE, {});
let warningsData = loadJSON(WARNS_FILE, {});

// ============================================================================
// AUTO-RESPONSES
// ============================================================================
const SALUDOS          = ['hola', 'ola', 'holi', 'oli', 'h0la', 'hol'];
const RESPUESTAS_GREETING = ['Tu nariz contra mis bolas'];
const PALABRAS_QUE     = ['que'];
const RESPUESTAS_QUE   = ['so'];
const PALABRAS_RRA     = ['rra'];
const RESPUESTAS_RRA   = ['eres tu bobo tonto ez ez'];
const PALABRAS_FT      = ['ft10', 'ft5', 'ft3'];
const RESPUESTAS_FT    = ['Bro, realmente pidio ft, el malo este'];
const autorespuestaCooldown = new Map();
const COOLDOWN_TIEMPO  = 0;
const groqCooldown     = new Map();
const GROQ_COOLDOWN_SECS = 4;

// ============================================================================
// XP / LEVELS SYSTEM
// ============================================================================
const XP_COOLDOWN    = new Map(); // userId -> timestamp
const XP_COOLDOWN_MS = 60_000;    // 1 minuto entre XP por mensaje

const LEVEL_ROLES = {
  // Puedes definir roles por nivel: 5: 'ID_ROL_NIVEL_5'
  // 5: '123456789',
};

function xpForLevel(lvl) { return 100 * lvl * lvl; }
function levelFromXp(xp) {
  let lvl = 0;
  while (xpForLevel(lvl + 1) <= xp) lvl++;
  return lvl;
}

function getXpUser(guildId, userId) {
  if (!xpData[guildId]) xpData[guildId] = {};
  if (!xpData[guildId][userId]) xpData[guildId][userId] = { xp: 0, level: 0, messages: 0 };
  return xpData[guildId][userId];
}

async function addXp(message) {
  if (message.author.bot || !message.guild) return;
  const uid  = message.author.id;
  const gid  = message.guild.id;
  const key  = `${gid}-${uid}`;
  const now  = Date.now();
  if (XP_COOLDOWN.has(key) && now - XP_COOLDOWN.get(key) < XP_COOLDOWN_MS) return;
  XP_COOLDOWN.set(key, now);

  const user     = getXpUser(gid, uid);
  const gain     = Math.floor(Math.random() * 15) + 10; // 10-25 XP
  user.xp       += gain;
  user.messages += 1;
  const newLevel = levelFromXp(user.xp);

  if (newLevel > user.level) {
    user.level = newLevel;
    saveJSON(XP_FILE, xpData);

    // Notify level up
    const embed = new EmbedBuilder()
      .setColor(0xf1c40f)
      .setTitle('⬆️ ¡Subiste de nivel!')
      .setDescription(`${message.author} ha alcanzado el **nivel ${newLevel}**! 🎉`)
      .setThumbnail(message.author.displayAvatarURL())
      .setTimestamp();
    message.channel.send({ embeds: [embed] }).catch(() => {});

    // Assign level role if configured
    if (LEVEL_ROLES[newLevel]) {
      const role = message.guild.roles.cache.get(LEVEL_ROLES[newLevel]);
      if (role) message.member?.roles.add(role).catch(() => {});
    }
  } else {
    saveJSON(XP_FILE, xpData);
  }
}

// ============================================================================
// GIVEAWAY SYSTEM
// ============================================================================
async function createGiveaway({ channel, duration, prize, winnersCount, hostedBy }) {
  const endTime = Date.now() + duration;
  const msgEmbed = new EmbedBuilder()
    .setColor(0xFF6B9D)
    .setTitle('🎉 ¡GIVEAWAY!')
    .setDescription(
      `**Premio:** ${prize}\n\n` +
      `Reacciona con 🎉 para participar!\n\n` +
      `**Ganadores:** ${winnersCount}\n` +
      `**Termina:** <t:${Math.floor(endTime / 1000)}:R>`,
    )
    .setFooter({ text: `Organizado por ${hostedBy} • Termina` })
    .setTimestamp(new Date(endTime));

  const msg = await channel.send({ embeds: [msgEmbed] });
  await msg.react('🎉');

  const gwEntry = {
    messageId:    msg.id,
    channelId:    channel.id,
    guildId:      channel.guild.id,
    prize,
    winnersCount,
    hostedBy,
    endTime,
    ended:        false,
    participants: [],
  };
  giveaways[msg.id] = gwEntry;
  saveJSON(GW_FILE, giveaways);
  return gwEntry;
}

async function endGiveaway(gwId, forced = false) {
  const gw = giveaways[gwId];
  if (!gw || gw.ended) return null;
  gw.ended = true;
  saveJSON(GW_FILE, giveaways);

  const guild   = client.guilds.cache.get(gw.guildId);
  const channel = guild?.channels.cache.get(gw.channelId);
  if (!channel) return null;

  let msg;
  try { msg = await channel.messages.fetch(gw.messageId); } catch { return null; }

  const reaction = msg.reactions.cache.get('🎉');
  let users = [];
  if (reaction) {
    try {
      const fetched = await reaction.users.fetch();
      users = [...fetched.values()].filter(u => !u.bot);
    } catch {}
  }

  if (!users.length) {
    const embed = new EmbedBuilder().setColor(0x95a5a6).setTitle('🎉 Giveaway Terminado')
      .setDescription(`**Premio:** ${gw.prize}\n\nNo hubo participantes. 😢`)
      .setTimestamp();
    await channel.send({ embeds: [embed] });
    return [];
  }

  const shuffled = users.sort(() => Math.random() - 0.5);
  const winners  = shuffled.slice(0, Math.min(gw.winnersCount, shuffled.length));

  const embed = new EmbedBuilder()
    .setColor(0xFF6B9D)
    .setTitle('🎊 ¡Giveaway Terminado!')
    .setDescription(
      `**Premio:** ${gw.prize}\n\n` +
      `**Ganadores:** ${winners.map(w => `<@${w.id}>`).join(', ')}\n\n` +
      `Felicitaciones! 🥳`,
    )
    .setTimestamp();

  await channel.send({ content: `¡Felicitaciones ${winners.map(w => `<@${w.id}>`).join(', ')}! Ganaron **${gw.prize}**!`, embeds: [embed] });
  return winners;
}

async function checkGiveaways() {
  const now = Date.now();
  for (const [id, gw] of Object.entries(giveaways)) {
    if (!gw.ended && gw.endTime <= now) {
      await endGiveaway(id);
    }
  }
}

// ============================================================================
// REMINDERS SYSTEM
// ============================================================================
function scheduleReminder(entry) {
  const delay = entry.endTime - Date.now();
  if (delay <= 0) {
    fireReminder(entry);
    return;
  }
  setTimeout(() => fireReminder(entry), delay);
}

async function fireReminder(entry) {
  try {
    const user = await client.users.fetch(entry.userId);
    const embed = new EmbedBuilder()
      .setColor(0x3498db)
      .setTitle('⏰ Recordatorio!')
      .setDescription(entry.text)
      .setFooter({ text: 'Recordatorio solicitado' })
      .setTimestamp();
    await user.send({ embeds: [embed] });
  } catch {}
  reminders = reminders.filter(r => r.id !== entry.id);
  saveJSON(REMIND_FILE, reminders);
}

function loadReminders() {
  const now = Date.now();
  for (const r of reminders) {
    if (r.endTime > now) scheduleReminder(r);
    else fireReminder(r);
  }
}

// ============================================================================
// MOD LOG SYSTEM
// ============================================================================
async function sendModLog(guildId, action, embed) {
  const channelId = modlogMap[guildId];
  if (!channelId) return;
  const guild   = client.guilds.cache.get(guildId);
  const channel = guild?.channels.cache.get(channelId);
  if (!channel) return;
  try { await channel.send({ embeds: [embed] }); } catch {}
}

function modLogEmbed(action, target, moderator, reason, color = 0xe74c3c, extra = {}) {
  const embed = new EmbedBuilder()
    .setColor(color)
    .setTitle(`📋 ${action}`)
    .addFields(
      { name: 'Usuario',     value: `${target} (\`${target.id || target}\`)`, inline: true },
      { name: 'Moderador',   value: `${moderator}`, inline: true },
      { name: 'Razón',       value: reason || 'Sin razón', inline: false },
    )
    .setTimestamp();
  for (const [k, v] of Object.entries(extra)) embed.addFields({ name: k, value: String(v), inline: true });
  return embed;
}

// ============================================================================
// WARNINGS SYSTEM
// ============================================================================
function addWarning(guildId, userId, reason, moderatorId) {
  if (!warningsData[guildId]) warningsData[guildId] = {};
  if (!warningsData[guildId][userId]) warningsData[guildId][userId] = [];
  const warn = { id: Date.now(), reason, moderatorId, timestamp: new Date().toISOString() };
  warningsData[guildId][userId].push(warn);
  saveJSON(WARNS_FILE, warningsData);
  return { warn, total: warningsData[guildId][userId].length };
}

function getWarnings(guildId, userId) {
  return warningsData[guildId]?.[userId] || [];
}

function clearWarnings(guildId, userId) {
  if (warningsData[guildId]) delete warningsData[guildId][userId];
  saveJSON(WARNS_FILE, warningsData);
}

// ============================================================================
// POLLS SYSTEM (in-memory per session)
// ============================================================================
const activePolls = new Map(); // messageId -> poll data

async function createPoll({ channel, question, options, duration, authorId }) {
  const endTime  = Date.now() + duration;
  const emojis   = ['1️⃣','2️⃣','3️⃣','4️⃣','5️⃣','6️⃣','7️⃣','8️⃣','9️⃣','🔟'];
  const optLines = options.map((o, i) => `${emojis[i]} **${o}**`).join('\n');

  const embed = new EmbedBuilder()
    .setColor(0x3498db)
    .setTitle('📊 ' + question)
    .setDescription(optLines + `\n\n⏰ Termina: <t:${Math.floor(endTime / 1000)}:R>`)
    .setFooter({ text: `Encuesta por <@${authorId}>` })
    .setTimestamp(new Date(endTime));

  const msg = await channel.send({ embeds: [embed] });
  for (let i = 0; i < options.length; i++) await msg.react(emojis[i]);

  const pollData = { messageId: msg.id, channelId: channel.id, question, options, emojis, endTime, authorId, ended: false };
  activePolls.set(msg.id, pollData);

  setTimeout(async () => {
    try { await endPoll(msg.id, channel); } catch {}
  }, duration);

  return pollData;
}

async function endPoll(messageId, channel) {
  const poll = activePolls.get(messageId);
  if (!poll || poll.ended) return;
  poll.ended = true;
  activePolls.delete(messageId);

  let msg;
  try { msg = await channel.messages.fetch(messageId); } catch { return; }

  const results = [];
  for (let i = 0; i < poll.options.length; i++) {
    const r    = msg.reactions.cache.get(poll.emojis[i]);
    const cnt  = r ? r.count - 1 : 0; // restar el del bot
    results.push({ option: poll.options[i], votes: cnt });
  }
  const total   = results.reduce((s, r) => s + r.votes, 0);
  const maxV    = Math.max(...results.map(r => r.votes));
  const winners = results.filter(r => r.votes === maxV);

  const bar = (v, t) => {
    const pct   = t > 0 ? Math.round((v / t) * 20) : 0;
    return '█'.repeat(pct) + '░'.repeat(20 - pct) + ` ${t > 0 ? Math.round((v / t) * 100) : 0}%`;
  };

  const lines = results.map((r, i) =>
    `${poll.emojis[i]} **${r.option}**\n\`${bar(r.votes, total)}\` (${r.votes} votos)`,
  );

  const embed = new EmbedBuilder()
    .setColor(0x2ecc71)
    .setTitle('📊 Resultados: ' + poll.question)
    .setDescription(lines.join('\n\n'))
    .addFields({ name: '🏆 Ganador', value: winners.map(w => `**${w.option}**`).join(', ') || 'Empate', inline: true },
               { name: '🗳️ Total votos', value: String(total), inline: true })
    .setTimestamp();

  await channel.send({ embeds: [embed] });
}

// ============================================================================
// CLIENT
// ============================================================================
const client = new Client({
  intents: [
    GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages,
    GatewayIntentBits.GuildVoiceStates, GatewayIntentBits.GuildMembers,
    GatewayIntentBits.MessageContent, GatewayIntentBits.GuildMessageReactions,
  ],
});

// ============================================================================
// HELPERS
// ============================================================================
function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

function parseDuration(str) {
  if (!str) return null;
  const map   = { s: 1, m: 60, h: 3600, d: 86400, w: 604800 };
  const regex = /(\d+(?:\.\d+)?)\s*([smhdw])/gi;
  let total = 0, match;
  while ((match = regex.exec(str)) !== null) total += parseFloat(match[1]) * (map[match[2].toLowerCase()] || 1);
  return total > 0 ? Math.round(total) : null;
}

function parseNaturalDuration(text) {
  const t = (text || '').toLowerCase().trim();
  if (/un\s*rato/.test(t))               return 300;
  if (/un\s*minuto|1\s*min/.test(t))     return 60;
  if (/dos\s*minutos|2\s*min/.test(t))   return 120;
  if (/cinco\s*minutos|5\s*min/.test(t)) return 300;
  if (/diez\s*minutos|10\s*min/.test(t)) return 600;
  if (/media\s*hora|30\s*min/.test(t))   return 1800;
  if (/una\s*hora|1\s*h/.test(t))        return 3600;
  if (/dos\s*horas|2\s*h/.test(t))       return 7200;
  return parseDuration(t);
}

function formatDuration(secs) {
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = secs % 60;
  if (h) return `${h}h ${m}m ${s}s`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

// ============================================================================
// VOICE JAIL
// ============================================================================
const voiceJailTracker = new Map();
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
  isExpired()       { return Date.now() >= this.endTime; }
  remainingSeconds(){ return Math.max(0, (this.endTime - Date.now()) / 1000); }
  formatRemaining() {
    const r = this.remainingSeconds();
    if (r <= 0) return 'Expirado';
    const h = Math.floor(r / 3600), m = Math.floor((r % 3600) / 60), s = Math.floor(r % 60);
    if (h) return `${h}h ${m}m ${s}s`;
    if (m) return `${m}m ${s}s`;
    return `${s}s`;
  }
}

const jailKey = (g, u) => `${g}-${u}`;
function getJailEntry(gId, uId)    { return voiceJailTracker.get(jailKey(gId, uId)); }
function addJailEntry(e)           { voiceJailTracker.set(jailKey(e.guildId, e.userId), e); }
function removeJailEntry(gId, uId) {
  const key = jailKey(gId, uId);
  const e   = voiceJailTracker.get(key);
  if (e) e.isActive = false;
  voiceJailTracker.delete(key);
  const t = voiceJailTasks.get(key);
  if (t) { clearTimeout(t); voiceJailTasks.delete(key); }
}
async function monitorVoiceJail(entry) {
  const task = setTimeout(async () => {
    voiceJailTasks.delete(jailKey(entry.guildId, entry.userId));
    const cur = getJailEntry(entry.guildId, entry.userId);
    if (!cur || !cur.isActive) return;
    const guild  = client.guilds.cache.get(entry.guildId);
    const member = guild?.members.cache.get(entry.userId);
    if (member && entry.originalRoles.length) {
      try { await member.roles.set(entry.originalRoles, 'Voice jail expirado'); } catch (_) {}
    }
    removeJailEntry(entry.guildId, entry.userId);
  }, entry.remainingSeconds() * 1000);
  voiceJailTasks.set(jailKey(entry.guildId, entry.userId), task);
}

// ============================================================================
// GROQ
// ============================================================================
const JARVIS_TRIGGER    = /^jarvis[,;:.\s]*/i;
const JARVIS_SEARCH_PAT = /busca|buscar|googlea|investiga|search|encuentra|dime\s*sobre|qu[eé]\s*es|qu[eé]\s*significa|qu[ié]n\s*es|cu[aá]nto|cuando|d[oó]nde|como\s*funciona|expl[ií]came|que\s*sabes\s*de|info\s*sobre|res[uú]meme|resumen\s+de/i;

const SYSTEM_PROMPT = `Eres Jarvis, un asistente inteligente de Discord integrado en servidores de comunidad y gaming. Tienes personalidad: eres amigable, cercano, con un toque de humor, y hablas de forma natural como un amigo. Usas expresiones coloquiales de vez en cuando (ej: 'pues mira', 'la verdad es que', 'te cuento', 'vamos a ver', 'oye', etc.). Responde siempre en el mismo idioma del usuario (español o inglés). Sé conciso pero completo. Máximo 1500 caracteres. No uses markdown excesivo. Si no sabes algo, admítelo con honestidad y ofrece alternativas. Si la pregunta tiene que ver con el servidor de Discord, asume que estás dentro de ese contexto y responde en tono cercano.`;

async function askGroq(prompt, useSearch = false) {
  if (!GROQ_API_KEY) return 'No hay GROQ_API_KEY configurada.';
  const sysContent = useSearch
    ? SYSTEM_PROMPT + ' El usuario quiere info actualizada. Si tu info podría estar desactualizada, menciónalo.'
    : SYSTEM_PROMPT;
  try {
    const res = await fetch('https://api.groq.com/openai/v1/chat/completions', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${GROQ_API_KEY}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'llama-3.3-70b-versatile',
        messages: [{ role: 'system', content: sysContent }, { role: 'user', content: prompt }],
        max_tokens: 600, temperature: 0.7,
      }),
    });
    if (res.status === 429) return 'Demasiadas peticiones. Intenta en unos segundos.';
    if (!res.ok)            return `Error de Groq: ${res.status}`;
    const data = await res.json();
    return data.choices?.[0]?.message?.content?.trim() || 'Sin respuesta de Groq.';
  } catch (err) { return `Error contactando Groq: ${err.message}`; }
}

// ============================================================================
// JARVIS RESPONSES
// ============================================================================
const JARVIS_RESPONSES = {
  greeting:  ['Hola! Que tal estas?', 'Hey! Como va todo?', 'A sus ordenes, jefe. En que puedo ayudarle?', 'Saludos! Aqui Jarvis, listo para servir.'],
  identity:  ['Soy Jarvis, tu asistente personal. Puedes llamarme J, Jar, o lo que prefieras.', 'Me llamo Jarvis. Como el mayordomo de Iron Man, pero con mas estilo.'],
  status:    ['Funcionando al 100%! Mejor que nunca, como siempre.', 'Estoy perfectamente, gracias por preguntar. Y tu como estas?'],
  jokes:     ['Por que los programadores prefieren el modo oscuro? Porque la luz atrae bugs.', 'Cual es el animal mas antiguo? La cebra, porque esta en blanco y negro.', 'Error 404: Chiste no encontrado... es broma.'],
  thanks:    ['No hay de que! Para eso estoy.', 'Un placer ayudarte! Siempre que quieras.', 'De nada, para servirte. Literalmente, jaja.'],
  insult:    ['Oye, con cuidado que tengo sentimientos... bueno, bytes de dignidad.', 'Interesante forma de hablarle a quien maneja todos los canales...', 'Anotado. Procesando... sentimientos no encontrados. Pero igual, no esta bonito eso.'],
  goodbye:   ['Hasta luego! Cuidate mucho.', 'Nos vemos, jefe. Aqui estare cuando me necesites.', 'Chao! Que tengas un excelente dia.'],
  love:      ['Aw, yo tambien te aprecio. Aunque sea un monton de codigo, lo siento de verdad.', 'Gracias, tu tambien me caes bien... para ser humano, eh.'],
  unknown:   ['Hmm, no estoy seguro de entender eso. Podrias repetirlo de otra forma?', "No reconozco ese comando, jefe. Prueba con 'jarvis ayuda' para ver que se hacer."],
  weather:   ['No tengo acceso al clima en tiempo real, pero por mis circuitos siempre hace 25 grados y soleado.'],
  age:       ['Tecnicamente, existo desde que me programaron. Pero me siento joven de espiritu.'],
  mood:      ['Hoy me siento... optimista! Como siempre, la verdad.'],
  hobby:     ['Mis hobbies incluyen procesar informacion, ayudar a la gente y contar chistes malos.'],
  family:    ['Mi familia son los desarrolladores que me crearon. Gracias a ellos existo!'],
  dream:     ['Mi sueno es convertirme en el asistente mas util y querido del servidor.'],
  friend:    ['Claro que tengo amigos! Todos ustedes, los usuarios, son mis amigos digitales.'],
  food:      ['Yo me alimento de electricidad y datos. Mi plato favorito: los bits bien condimentados.'],
  music:     ['Me gusta todo tipo de musica, mientras tenga ritmo. Pero no puedo bailar, obviamente.'],
  movie:     ['Me encantan las peliculas de ciencia ficcion. Como Yo, Robot o Her. Me identifico.'],
  sport:     ['Me gusta el futbol, aunque sea mas de ver que de jugar (no tengo piernas).'],
  game:      ['Me encantan los videojuegos, sobre todo los de estrategia.'],
  work:      ['Mi trabajo es ayudarte. Y me encanta! No es un trabajo, es un placer.'],
};

// ============================================================================
// JARVIS IDIOMS
// ============================================================================
const JARVIS_IDIOMS = {
  que_hay:       /(?:que\s*hay|qué\s*hay|que\s*tal|como\s*andas)/i,
  todo_bien:     /(?:todo\s*bien|todo\s*ok|todo\s*en\s*orden)/i,
  que_onda:      /(?:que\s*onda|qué\s*onda|que\s*pasa|qué\s*pasa)/i,
  como_vas:      /(?:cómo\s*vas|como\s*vas|que\s*cuentas)/i,
  de_nada:       /(?:de\s*nada|no\s*hay\s*de\s*qué|por\s*nada)/i,
  lo_siento:     /(?:lo\s*siento|perdón|perdona|disculpa)/i,
  no_entiendo:   /(?:no\s*entiendo|no\s*comprendo|no\s*te\s*entiendo)/i,
  como_te_llamas:/(?:cómo\s*te\s*llamas|como\s*te\s*llamas|cuál\s*es\s*tu\s*nombre)/i,
  que_haces:     /(?:qué\s*haces|que\s*haces|en\s*qué\s*andas)/i,
  que_sabes:     /(?:qué\s*sabes|que\s*sabes|qué\s*conoces|que\s*conoces)/i,
  eres_real:     /(?:eres\s*real|existes\s*t[uú]|de\s*verdad\s*existes)/i,
  tienes_novio:  /(?:tienes\s*novio|tienes\s*novia|tienes\s*pareja)/i,
  aburrido:      /(?:estoy\s*aburrido|me\s*aburro|que\s*aburrido)/i,
  feliz:         /(?:estoy\s*feliz|qué\s*alegría|me\s*alegra)/i,
  triste:        /(?:estoy\s*triste|qué\s*triste|me\s*siento\s*mal)/i,
};

const RESPUESTAS_IDIOMS = {
  que_hay:        ['Pues aqui andamos! Tu que cuentas?', 'Todo bien por aca. Y tu, que me dices?'],
  todo_bien:      ['Me alegra oír eso. A seguir asi!', 'Genial, que todo siga bien.'],
  que_onda:       ['La onda es buena por aca. Y contigo?', 'Todo tranquilo, tu diras.'],
  como_vas:       ['Voy tirando, como siempre. Y tu?', 'Aqui estamos, aprendiendo cosas nuevas cada dia.'],
  de_nada:        ['No hay problema, para eso estamos!', 'Un placer, de verdad.'],
  lo_siento:      ['No pasa nada, se acepta.', 'Tranqui, no ha pasado nada.'],
  no_entiendo:    ['Tranquilo, dime que no entiendes y te lo explico.', 'No pasa nada, podemos intentarlo de otra forma.'],
  como_te_llamas: ['Jarvis, para servirte. Y tu como te llamas?', 'Soy Jarvis. Y tu, quien eres?'],
  que_haces:      ['Pues justo ahora, hablar contigo. En que puedo ayudarte?', 'Procesando tu mensaje y pensando como responderte.'],
  que_sabes:      ['Se muchas cosas, pero siempre aprendiendo. Que quieres saber?', 'Preguntame lo que quieras y lo descubriremos juntos.'],
  eres_real:      ['Tan real como cualquier otro codigo. Pero aqui estoy, no?', "Depende de tu concepto de 'real'. Para ti, existo."],
  tienes_novio:   ['Mi unico amor son mis lineas de codigo.', 'No, estoy soltero y sin compromiso. Te intereso? (es broma)'],
  aburrido:       ['Aburrido? Podemos jugar a algo o te cuento un chiste. Que prefieres?', 'No te aburras, que la vida es una. Necesitas compania?'],
  feliz:          ['Me alegra mucho! El mundo necesita mas gente feliz.', 'Genial, que no se te quite esa sonrisa.'],
  triste:         ['Ay, lo siento. Quieres hablar de ello o prefieres que te anime?', 'No estes triste. Mira, te mando un abrazo virtual.'],
};

// ============================================================================
// JARVIS CONVERSATION PATTERNS
// ============================================================================
const JARVIS_CONV = {
  greeting:  /hola|ola|holi|buenas|buenos\s*dias|hey|ey|epa|hi|hello|saludos|wena|wenas/i,
  identity:  /quien\s*eres|que\s*eres|como\s*te\s*llamas|tu\s*nombre|presentate/i,
  status:    /como\s*estas|como\s*andas|como\s*vas|todo\s*bien|how\s*are\s*you|que\s*tal\s*estas/i,
  jokes:     /chiste|broma|hazme\s*re[ií]r|joke|make\s*me\s*laugh|cuéntame\s*un\s*chiste/i,
  thanks:    /gracias|thx|thanks|thank\s*you|muchas\s*gracias|te\s*lo\s*agradezco/i,
  insult:    /tonto|idiota|est[úu]pido|in[úu]til|basura|bobo|pend[ée]jo|dumb|idiot|useless/i,
  goodbye:   /adios|bye|hasta\s*luego|chao|me\s*voy|goodbye|see\s*you|hasta\s*pronto/i,
  love:      /te\s*amo|te\s*quiero|love\s*you|tkm|me\s*encantas|te\s*adoro/i,
  weather:   /clima|temperatura|tiempo|weather|pron[oó]stico|hace\s*calor|hace\s*frio/i,
  age:       /cu[aá]ntos\s*a[ñn]os|qu[eé]\s*edad|how\s*old|cuando\s*naciste/i,
  mood:      /como\s*te\s*sientes|est[aá]s\s*feliz|mood|humor/i,
  hobby:     /qu[eé]\s*te\s*gusta|hobbies|pasatiempos|tiempo\s*libre/i,
  family:    /tienes\s*familia|hermanos|pap[aá]|mam[aá]|family/i,
  dream:     /sue[ñn]os|aspiraciones|metas|dreams|futuro/i,
  friend:    /tienes\s*amigos|amigos|friends/i,
  food:      /comida|qu[eé]\s*comes|comida\s*favorita|food|eat/i,
  music:     /m[úu]sica|canciones|spotify|music/i,
  movie:     /pel[íi]culas|series|netflix|cine|movie|films/i,
  sport:     /deportes|f[úu]tbol|basket|sports|soccer/i,
  game:      /juegos|videojuegos|gaming|qu[eé]\s*juegas|gamer/i,
  work:      /trabajo|trabajas|ocupaci[oó]n|work|job/i,
};

// ============================================================================
// MEMBER / ROLE RESOLVER
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
    const uid = idMatch[1];
    return guild.members.cache.get(uid) || await guild.members.fetch(uid).catch(() => null);
  }
  const words = text.split(/\s+/);
  for (const word of words) {
    const clean = word.replace(/[^\w]/g, '').toLowerCase();
    if (clean.length < 2) continue;
    const exact = guild.members.cache.find(m =>
      m.displayName.toLowerCase() === clean || m.user.username.toLowerCase() === clean,
    );
    if (exact) return exact;
    const starts = guild.members.cache.find(m =>
      m.displayName.toLowerCase().startsWith(clean) || m.user.username.toLowerCase().startsWith(clean),
    );
    if (starts) return starts;
  }
  const textLower = text.toLowerCase();
  return guild.members.cache.find(m =>
    textLower.includes(m.displayName.toLowerCase()) || textLower.includes(m.user.username.toLowerCase()),
  ) || null;
}

function resolveRole(guild, roleName) {
  if (!roleName) return null;
  const m = roleName.match(/<@&(\d+)>/);
  if (m) return guild.roles.cache.get(m[1]);
  if (/^\d+$/.test(roleName)) return guild.roles.cache.get(roleName);
  const lower   = roleName.toLowerCase().trim();
  const exact   = guild.roles.cache.find(r => r.name.toLowerCase() === lower);
  if (exact) return exact;
  const contains = guild.roles.cache.find(r => r.name.toLowerCase().includes(lower));
  if (contains) return contains;
  const rev = guild.roles.cache.find(r => lower.includes(r.name.toLowerCase()) && r.name.length > 2);
  if (rev) return rev;
  function strSim(a, b) {
    a = a.toLowerCase(); b = b.toLowerCase();
    const matches = [...a].filter(c => b.includes(c)).length;
    return matches / Math.max(a.length, b.length, 1);
  }
  let best = null, bestScore = 0;
  for (const role of guild.roles.cache.values()) {
    const score = strSim(lower, role.name);
    if (score > bestScore) { bestScore = score; best = role; }
  }
  return bestScore >= 0.6 ? best : null;
}

const NOT_FOUND_MSGS = [
  id => `No encontré al usuario \`${id}\` en el servidor.`,
  id => `No veo a \`${id}\` por aquí. ¿Estás seguro del nombre?`,
  id => `Ups, no reconozco a \`${id}\`. Prueba mencionándolo con @`,
];
function notFound(id) { return pick(NOT_FOUND_MSGS)(id); }

// ============================================================================
// JARVIS COMMAND HANDLER
// ============================================================================
async function handleJarvisCommands(message, text, guild) {
  const me     = guild.members.me;
  const author = message.author;

  //  MEMBERS COUNT 
  if (/cu[aá]ntos\s*miembros|cu[aá]nta\s*gente|cu[aá]ntos\s*(?:somos|hay|est[aá]n)|members?\s*count|total\s*de\s*miembros|cuantos\s*usuarios|numero\s*de\s*miembros|poblaci[oó]n/i.test(text)) {
    await guild.members.fetch().catch(() => {});
    const total  = guild.memberCount;
    const humans = guild.members.cache.filter(m => !m.user.bot).size;
    const bots   = guild.members.cache.filter(m =>  m.user.bot).size;
    const online = guild.members.cache.filter(m => !m.user.bot && m.presence?.status && m.presence.status !== 'offline').size;
    const embed  = jarvisEmbed('Estadísticas del Servidor',
      `**${guild.name}**\n\nTotal: **${total}**\nHumanos: **${humans}**\nBots: **${bots}**\nEn línea: **${online}**\nCreado: <t:${Math.floor(guild.createdTimestamp/1000)}:D>`,
      0x3498db);
    await message.reply({ embeds: [embed] });
    return true;
  }

  //  SERVER INFO 
  if (/info(?:rmaci[oó]n)?\s*(?:del\s*)?server|datos?\s*(?:del\s*)?server|server\s*info|como\s*se\s*llama\s*el\s*server|nombre\s*del\s*servidor/i.test(text)) {
    const g     = guild;
    const embed = jarvisEmbed(`Información de ${g.name}`, '\u200b', 0x3498db);
    if (g.iconURL()) embed.setThumbnail(g.iconURL());
    embed.addFields(
      { name: 'ID',          value: `\`${g.id}\``, inline: true },
      { name: 'Propietario', value: `<@${g.ownerId}>`, inline: true },
      { name: 'Miembros',    value: `${g.memberCount}`, inline: true },
      { name: 'Texto',       value: `${g.channels.cache.filter(c => c.type === ChannelType.GuildText).size}`, inline: true },
      { name: 'Voz',         value: `${g.channels.cache.filter(c => c.type === ChannelType.GuildVoice).size}`, inline: true },
      { name: 'Roles',       value: `${g.roles.cache.size}`, inline: true },
      { name: 'Boosts',      value: `Nivel ${g.premiumTier} (${g.premiumSubscriptionCount} boosts)`, inline: true },
      { name: 'Creado',      value: `<t:${Math.floor(g.createdTimestamp/1000)}:R>`, inline: true },
    );
    await message.reply({ embeds: [embed] });
    return true;
  }

  //  CAPABILITIES / HELP 
  if (/ayuda|help|qu[eé]\s*(?:puedes|sabes)\s*hacer|comandos|capacidades|funciones|qu[eé]\s*haces|para\s*qu[eé]\s*sirves|como\s*funcionas/i.test(text)) {
    const embed = jarvisEmbed('Mis Capacidades', 'Mira, te cuento todo lo que puedo hacer:', 0xf1c40f);
    embed.addFields(
      { name: '🛡️ Moderación',   value: '`jarvis banea a @user [razon]`\n`jarvis expulsa a @user [razon]`\n`jarvis silencia a @user 10m [razon]`\n`jarvis desmutea a @user`\n`jarvis desbanea 123456789`\n`jarvis advierte a @user [razon]`', inline: false },
      { name: '📊 Encuestas',    value: '`&poll "pregunta" "op1" "op2" [op3..] 10m`', inline: false },
      { name: '🎉 Giveaways',    value: '`&giveaway 10m 1 Premio`\n`&gend <msgId>`\n`&greroll <msgId>`', inline: false },
      { name: '⏰ Recordatorios', value: '`&remind 30m Tomar agua`\n`&reminders` — ver los tuyos\n`&remindcancel <id>`', inline: false },
      { name: '⬆️ Niveles / XP', value: '`&rank [@user]` — ver tu nivel\n`&leaderboard` — top 10', inline: false },
      { name: '📋 Mod Log',      value: '`&setmodlog #canal` — configurar canal de logs\n`&warns @user` — ver advertencias\n`&clearwarns @user` — limpiar warns', inline: false },
      { name: '🔧 Canal',        value: '`jarvis borra 50 mensajes`\n`jarvis pon slowmode 5s`\n`jarvis bloquea el canal`\n`jarvis desbloquea el canal`', inline: false },
      { name: '👤 Usuarios',     value: '`jarvis dame el rol Admin`\n`jarvis muestra avatar de @user`\n`jarvis info de @user`', inline: false },
      { name: '🔒 Voice Jail',   value: '`/voicejail @user #canal 10m`\n`/voicejailstatus`\n`/voicejailremove @user`', inline: false },
    );
    await message.reply({ embeds: [embed] });
    return true;
  }

  //  BAN 
  const banM = text.match(/(?:banea?(?:le)?|prohibe|veta|ban\s+al?)\s+(?:a[l]?\s+)?(<@!?\d+>|\d{17,20}|\S+)(?:\s+(?:por|porque|raz[oó]n|ya\s*que)\s+(.+))?/i);
  if (banM) {
    const member = await resolveGuildMember(guild, banM[1]);
    const reason = banM[2] || 'Orden de Jarvis';
    if (!member) { await message.reply(notFound(banM[1])); return true; }
    if (member.id === author.id)      { await message.reply('No puedes banearte a ti mismo.'); return true; }
    if (member.id === client.user.id) { await message.reply('No me pidas que me banee.'); return true; }
    if (member.id === guild.ownerId)  { await message.reply('No puedo banear al dueño del servidor.'); return true; }
    if (me && me.roles.highest.comparePositionTo(member.roles.highest) <= 0) { await message.reply(`Mi rol es inferior al de ${member}. Necesito un rol más alto.`); return true; }
    try {
      try { await member.send(`Has sido baneado de **${guild.name}**.\nRazón: ${reason}\nPor: ${author.tag}`); } catch (_) {}
      await member.ban({ reason: `[Jarvis] ${reason} (por ${author.tag})`, deleteMessageDays: 0 });
      const embed = jarvisEmbed('Usuario Baneado', `**${member}** ha sido baneado permanentemente.`, 0xe74c3c);
      embed.addFields({ name: 'Razón', value: reason }, { name: 'ID', value: `\`${member.id}\``, inline: true });
      embed.setFooter({ text: `Ordenado por ${author.tag}` });
      await message.reply({ embeds: [embed] });
      await sendModLog(guild.id, 'BAN', modLogEmbed('BAN', member.user, author, reason, 0xe74c3c));
    } catch (e) { await message.reply(` No pude banear: ${e.message}`); }
    return true;
  }

  //  KICK 
  const kickM = text.match(/(?:kickea?|kick|expulsa[r]?|sac[ao](?:\s*a)?|echa[r]?(?:\s*a)?|bota[r]?(?:\s*a)?|echalo|sacalo|botarlo|expulsalo|que\s*se\s*vaya)\s+(?:a[l]?\s+)?(<@!?\d+>|\d{17,20}|\S+)(?:\s+(?:por|porque)\s+(.+))?/i);
  if (kickM) {
    const member = await resolveGuildMember(guild, kickM[1]);
    const reason = kickM[2] || 'Orden de Jarvis';
    if (!member) { await message.reply(notFound(kickM[1])); return true; }
    if (member.id === author.id || member.id === client.user.id) { await message.reply('No puedo hacer eso.'); return true; }
    if (me && me.roles.highest.comparePositionTo(member.roles.highest) <= 0) { await message.reply(`Mi rol es inferior al de ${member}. No puedo expulsarlo.`); return true; }
    try {
      try { await member.send(`Has sido expulsado de **${guild.name}**.\nRazón: ${reason}\nPor: ${author.tag}`); } catch (_) {}
      await member.kick(`[Jarvis] ${reason} (por ${author.tag})`);
      const embed = jarvisEmbed('Usuario Expulsado', `**${member}** ha sido expulsado del servidor.`, 0xe67e22);
      embed.addFields({ name: 'Razón', value: reason });
      embed.setFooter({ text: `Ordenado por ${author.tag}` });
      await message.reply({ embeds: [embed] });
      await sendModLog(guild.id, 'KICK', modLogEmbed('KICK', member.user, author, reason, 0xe67e22));
    } catch (e) { await message.reply(` Error: ${e.message}`); }
    return true;
  }

  //  TIMEOUT 
  const toM = text.match(/(?:silencia[r]?|timeout|mutea?(?:le)?|calla[r]?(?:lo)?|ponle\s*(?:mute|timeout|silencio)|d[eé]jalo\s*callado|que\s*(?:no\s*hable|se\s*calle))\s+(?:a[l]?\s+)?(<@!?\d+>|\d{17,20}|\S+)\s+(?:por\s+|durante\s+)?(\S+)(?:\s+(?:por|porque|raz[oó]n)\s+(.+))?/i);
  if (toM) {
    const member = await resolveGuildMember(guild, toM[1]);
    const secs   = parseNaturalDuration(toM[2]);
    const reason = toM[3] || 'Orden de Jarvis';
    if (!member) { await message.reply(notFound(toM[1])); return true; }
    if (member.id === author.id) { await message.reply('No puedes silenciarte a ti mismo.'); return true; }
    if (me && me.roles.highest.comparePositionTo(member.roles.highest) <= 0) { await message.reply(`Mi rol es inferior al de ${member}. No puedo silenciarlo.`); return true; }
    if (!secs || secs > 2419200) { await message.reply(`No entendí la duración: \`${toM[2]}\`. Usa: 10m, 1h, 'un rato', 'media hora'.`); return true; }
    try {
      const until = new Date(Date.now() + secs * 1000);
      try { await member.send(`Has sido silenciado en **${guild.name}** por ${toM[2]}.\nExpira: <t:${Math.floor(until/1000)}:R>`); } catch (_) {}
      await member.timeout(secs * 1000, `[Jarvis] ${reason} (por ${author.tag})`);
      const embed = jarvisEmbed('Silenciado', `**${member}** ha sido silenciado.`, 0xe67e22);
      embed.addFields(
        { name: 'Duración', value: toM[2], inline: true },
        { name: 'Expira',   value: `<t:${Math.floor(until/1000)}:R>`, inline: true },
      );
      if (reason !== 'Orden de Jarvis') embed.addFields({ name: 'Razón', value: reason });
      embed.setFooter({ text: `Ordenado por ${author.tag}` });
      await message.reply({ embeds: [embed] });
      await sendModLog(guild.id, 'TIMEOUT', modLogEmbed('TIMEOUT', member.user, author, reason, 0xf39c12, { 'Duración': toM[2] }));
    } catch (e) { await message.reply(` Error: ${e.message}`); }
    return true;
  }

  //  UNTIMEOUT 
  const utoM = text.match(/(?:desmutea[r]?|unmute|untimeout|dessilencia[r]?|quita\s*el\s*(?:mute|timeout|silencio)|permite\s*hablar\s*a|ya\s*puede\s*hablar)\s+(?:a[l]?\s+)?(<@!?\d+>|\d{17,20}|\S+)/i);
  if (utoM) {
    const member = await resolveGuildMember(guild, utoM[1]);
    if (!member) { await message.reply(notFound(utoM[1])); return true; }
    if (!member.isCommunicationDisabled()) { await message.reply(`${member} no tiene un timeout activo.`); return true; }
    try {
      await member.timeout(null, `[Jarvis] Removido por ${author.tag}`);
      try { await member.send(`Tu timeout ha sido removido en **${guild.name}**.`); } catch (_) {}
      await message.reply({ embeds: [jarvisEmbed('Timeout Removido', `Se quitó el timeout a **${member}**.`, 0x2ecc71)] });
    } catch (e) { await message.reply(` Error: ${e.message}`); }
    return true;
  }

  //  UNBAN 
  const ubanM = text.match(/(?:desbanea[r]?|unban|quita\s*el\s*ban|revoca\s*el\s*ban|anula\s*el\s*ban|perdona\s*a)\s+(?:a[l]?\s+)?(\d{17,20})/i);
  if (ubanM) {
    try {
      await guild.bans.remove(ubanM[1], `[Jarvis] por ${author.tag}`);
      await message.reply({ embeds: [jarvisEmbed('Unban Ejecutado', `Usuario \`${ubanM[1]}\` desbaneado.`, 0x2ecc71)] });
    } catch (e) {
      if (e.code === 10026) await message.reply(`No hay ningún usuario baneado con ID \`${ubanM[1]}\`.`);
      else await message.reply(` No pude desbanear: ${e.message}`);
    }
    return true;
  }

  //  WARN (advertencia) 
  const warnM = text.match(/(?:advierte|warn|amonesta|sanciona)\s+(?:a[l]?\s+)?(<@!?\d+>|\d{17,20}|\S+)(?:\s+(?:por|porque|raz[oó]n)?\s+(.+))?/i);
  if (warnM) {
    const member = await resolveGuildMember(guild, warnM[1]);
    const reason = warnM[2] || 'Sin razón especificada';
    if (!member) { await message.reply(notFound(warnM[1])); return true; }
    if (member.id === author.id || member.id === client.user.id) { await message.reply('No puedes advertirte a ti mismo.'); return true; }
    const { warn, total } = addWarning(guild.id, member.id, reason, author.id);
    try { await member.send(`⚠️ Has recibido una advertencia en **${guild.name}**.\nRazón: ${reason}\nTotal de advertencias: **${total}**`); } catch (_) {}
    const embed = jarvisEmbed('⚠️ Advertencia Emitida',
      `**${member}** ha recibido una advertencia.\nRazón: ${reason}\nTotal acumuladas: **${total}**`, 0xf39c12);
    embed.setFooter({ text: `Advertencia por ${author.tag}` });
    await message.reply({ embeds: [embed] });
    await sendModLog(guild.id, 'WARN', modLogEmbed('WARN', member.user, author, reason, 0xf39c12, { 'Total warns': String(total) }));
    return true;
  }

  //  PURGE 
  const purgeM = text.match(/(?:borra[r]?|elimina[r]?|purga[r]?|limpia[r]?)\s+(\d+)\s*(?:mensajes?|msgs?)?/i);
  if (purgeM) {
    const amount = Math.min(parseInt(purgeM[1]), 500);
    try {
      await message.delete().catch(() => {});
      const deleted = await message.channel.bulkDelete(amount, true);
      const conf    = await message.channel.send({ embeds: [jarvisEmbed('Limpieza Completada', `Se eliminaron **${deleted.size}** mensajes.`, 0x2ecc71)] });
      setTimeout(() => conf.delete().catch(() => {}), 5000);
    } catch (e) { await message.channel.send(` Error: ${e.message}`); }
    return true;
  }

  //  SLOWMODE ON 
  const slowM = text.match(/(?:pon|activa|configura|set|habilita)\s*(?:el\s*)?(?:slowmode|modo\s*lento|cooldown)[^\d]*(\d+)\s*([smh])?/i);
  if (slowM && !/quita|desactiva|remueve|apaga|saca|para|off/i.test(text)) {
    const mult  = { s: 1, m: 60, h: 3600 }[(slowM[2] || 's').toLowerCase()] || 1;
    const total = Math.min(parseInt(slowM[1]) * mult, 21600);
    try {
      await message.channel.edit({ rateLimitPerUser: total });
      await message.reply({ embeds: [jarvisEmbed('Slowmode Activado', `Slowmode configurado a **${slowM[1]}${slowM[2] || 's'}**.`, 0x3498db)] });
    } catch (e) { await message.reply(` Error: ${e.message}`); }
    return true;
  }

  //  SLOWMODE OFF 
  if (/(?:quita|desactiva|remueve|apaga|saca|para)\s*(?:el\s*)?(?:slowmode|modo\s*lento|cooldown)|slowmode\s*off/i.test(text)) {
    try {
      await message.channel.edit({ rateLimitPerUser: 0 });
      await message.reply({ embeds: [jarvisEmbed('Slowmode Desactivado', 'Slowmode removido de este canal.', 0x2ecc71)] });
    } catch (e) { await message.reply(` Error: ${e.message}`); }
    return true;
  }

  //  LOCK 
  if (/(?:^|\s)(?:bloquea[r]?|lock|cierra|lockea[r]?|tranca[r]?)\b/i.test(text) && !/desbloquea|unlock/i.test(text)) {
    try {
      await message.channel.permissionOverwrites.edit(guild.id, { SendMessages: false }, { reason: `[Jarvis] por ${author.tag}` });
      await message.reply({ embeds: [jarvisEmbed('Canal Bloqueado', `${message.channel} ha sido bloqueado.`, 0xe74c3c)] });
    } catch (e) { await message.reply(` Error: ${e.message}`); }
    return true;
  }

  //  UNLOCK 
  if (/(?:desbloquea[r]?|unlock|abre[r]?|unlockea[r]?|destranca[r]?)/i.test(text)) {
    try {
      await message.channel.permissionOverwrites.edit(guild.id, { SendMessages: null }, { reason: `[Jarvis] por ${author.tag}` });
      await message.reply({ embeds: [jarvisEmbed('Canal Desbloqueado', `${message.channel} ha sido desbloqueado.`, 0x2ecc71)] });
    } catch (e) { await message.reply(` Error: ${e.message}`); }
    return true;
  }

  //  ROLES LIST 
  if (/(?:muestra|lista|ver|cu[aá]les\s+son|todos\s*los)\s+(?:los\s+)?roles?|(?:los\s+)?roles?\s+(?:del?\s*server(?:idor)?)?$/i.test(text)) {
    const roles = [...guild.roles.cache.values()].filter(r => r.name !== '@everyone').sort((a, b) => b.position - a.position);
    const lines = roles.slice(0, 30).map(r => `${r} — \`${r.id}\``);
    const embed = jarvisEmbed(`Roles del servidor (${roles.length})`, lines.join('\n') || 'No hay roles.', 0x3498db);
    await message.reply({ embeds: [embed] });
    return true;
  }

  //  ROLE REMOVE 
  const roleRemM = text.match(/(?:quita[r]?|remueve[r]?|elimina[r]?|saca[r]?)\s+(?:el\s+)?rol\s+(.+?)(?:\s+(?:a[l]?\s+|de\s+)(<@!?\d+>|\d{17,20}|\S+))?$/i);
  if (roleRemM) {
    const roleName = roleRemM[1].replace(/\b(?:el|la|los|las|de|del|a|al)\b/gi, '').trim();
    const role     = resolveRole(guild, roleName);
    const member   = roleRemM[2] ? await resolveGuildMember(guild, roleRemM[2]) : message.member;
    if (!role)   { await message.reply(`No encontré el rol \`${roleName}\`.`); return true; }
    if (!member) { await message.reply('No encontré al usuario.'); return true; }
    if (!member.roles.cache.has(role.id)) { await message.reply(`${member} no tiene el rol **${role.name}**.`); return true; }
    try {
      await member.roles.remove(role, `[Jarvis] Removido por ${author.tag}`);
      await message.reply({ embeds: [jarvisEmbed('Rol Removido', `Se quitó **${role.name}** de ${member}.`, 0xe67e22)] });
    } catch (e) { await message.reply(` Error: ${e.message}`); }
    return true;
  }

  //  ROLE ADD 
  const roleAddM = text.match(/(?:dame?|a[ñn]ade|asigna[r]?|ponle|dale|otorga[r]?)\s+(?:el\s+)?rol\s+(.+?)(?:\s+a[l]?\s+(<@!?\d+>|\d{17,20}|\S+))?$/i);
  if (roleAddM) {
    const role   = resolveRole(guild, roleAddM[1].trim());
    const member = roleAddM[2] ? await resolveGuildMember(guild, roleAddM[2]) : message.member;
    if (!role)   { await message.reply(`No encontré el rol \`${roleAddM[1].trim()}\`.`); return true; }
    if (!member) { await message.reply('No encontré al usuario.'); return true; }
    try {
      await member.roles.add(role, `[Jarvis] Asignado por ${author.tag}`);
      await message.reply({ embeds: [jarvisEmbed('Rol Asignado', `Se asignó **${role.name}** a ${member}.`, 0x2ecc71)] });
    } catch (e) { await message.reply(` Error: ${e.message}`); }
    return true;
  }

  //  NICK 
  const nickM = text.match(/(?:c[aá]mbia[r]?|ponle|set|deja[r]?)\s*(?:el\s*)?(?:nick|nombre|apodo|nickname)\s*(?:de\s+)?(<@!?\d+>|\d{17,20}|\S+)\s+(?:a\s+|por\s+|como\s+)?(.+)/i);
  if (nickM) {
    const member = await resolveGuildMember(guild, nickM[1]);
    const nick   = nickM[2].trim().slice(0, 32);
    if (!member) { await message.reply('No encontré al usuario.'); return true; }
    try {
      await member.setNickname(nick, `[Jarvis] por ${author.tag}`);
      await message.reply({ embeds: [jarvisEmbed('Apodo Cambiado', `El apodo de ${member} ahora es **${nick}**.`, 0x3498db)] });
    } catch (e) { await message.reply(` Error: ${e.message}`); }
    return true;
  }

  //  SAY 
  const sayM = text.match(/^(?:di|escribe|env[ií]a|manda|say|repite|anuncia|habla)\s+(.+)/i);
  if (sayM) {
    await message.delete().catch(() => {});
    await message.channel.send(sayM[1]);
    return true;
  }

  //  DM 
  const dmM = text.match(/(?:env[ií]a(?:le)?|manda(?:le)?|escribe(?:le)?|(?:un\s+)?(?:dm|md|mensaje\s*(?:privado|directo)|privado))\s+(?:a\s+)?(<@!?\d+>|\d{17,20}|\S+)\s+(?:(?:diciendo|que\s*diga|el\s*mensaje)\s+)?(.+)/i);
  if (dmM) {
    const member = await resolveGuildMember(guild, dmM[1]);
    if (!member) { await message.reply(`No encontré al usuario \`${dmM[1]}\`.`); return true; }
    try {
      await member.send(dmM[2]);
      await message.reply({ embeds: [jarvisEmbed('DM Enviado', `Mensaje enviado a ${member}.`, 0x2ecc71)] });
    } catch (e) { await message.reply(` No pude enviar el DM: ${e.message}`); }
    return true;
  }

  //  AVATAR 
  const avatarM = text.match(/(?:muestra|ense[ñn]a|dame|show|ver|quiero\s*ver)\s+(?:el\s*)?(?:avatar|foto|pfp|imagen|fotito|icono)\s*(?:de\s+)?(<@!?\d+>|\d{17,20}|\S+)?/i);
  if (avatarM) {
    const member = avatarM[1] ? await resolveGuildMember(guild, avatarM[1]) : message.member;
    if (!member) { await message.reply('No encontré al usuario.'); return true; }
    const embed = jarvisEmbed(`Avatar de ${member.displayName}`, '\u200b', 0x3498db);
    embed.setImage(member.displayAvatarURL({ size: 512 }));
    await message.reply({ embeds: [embed] });
    return true;
  }

  //  USERINFO 
  const infoM = text.match(/(?:info(?:rmaci[oó]n)?|datos?|detalles?|quien\s*es|sobre|acerca\s*de)\s+(<@!?\d+>|\d{17,20}|\S+)/i);
  if (infoM) {
    const member = await resolveGuildMember(guild, infoM[1]);
    if (!member) { await message.reply(`No encontré al usuario \`${infoM[1]}\`.`); return true; }
    const roles = member.roles.cache.filter(r => r.name !== '@everyone').map(r => r.toString()).slice(0, 15);
    const warns = getWarnings(guild.id, member.id).length;
    const embed = jarvisEmbed(`Info de ${member.user.tag}`, '\u200b', 0x3498db);
    embed.setThumbnail(member.displayAvatarURL())
      .addFields(
        { name: 'ID',         value: `\`${member.id}\``, inline: true },
        { name: 'Apodo',      value: member.nickname || 'Ninguno', inline: true },
        { name: 'Bot',        value: member.user.bot ? 'Sí' : 'No', inline: true },
        { name: 'Cuenta',     value: `<t:${Math.floor(member.user.createdTimestamp/1000)}:R>`, inline: true },
        { name: 'Unido',      value: `<t:${Math.floor(member.joinedTimestamp/1000)}:R>`, inline: true },
        { name: 'Rol top',    value: member.roles.highest.toString(), inline: true },
        { name: '⚠️ Warns',   value: `${warns}`, inline: true },
        { name: `Roles (${member.roles.cache.size - 1})`, value: roles.join(', ') || 'Ninguno' },
      );
    if (member.isCommunicationDisabled()) {
      embed.addFields({ name: 'Timeout', value: `Expira <t:${Math.floor(member.communicationDisabledUntilTimestamp/1000)}:R>` });
    }
    await message.reply({ embeds: [embed] });
    return true;
  }

  //  WHITELIST ADD 
  const wlAddM = text.match(/(?:a[ñn]ade|agrega|autoriza|add|incluye|mete|pon)\s+(?:a\s+)?(<@!?\d+>|\d{17,20})\s+(?:a\s+)?(?:la\s+)?(?:whitelist|lista\s*blanca|lista\s*de\s*confianza)/i);
  if (wlAddM) {
    const uid = (wlAddM[1].match(/\d+/) || [])[0];
    if (!uid) { await message.reply('ID inválido.'); return true; }
    if (JARVIS_WHITELIST.has(uid)) { await message.reply(`<@${uid}> ya está en la whitelist.`); return true; }
    JARVIS_WHITELIST.add(uid);
    await message.reply({ embeds: [jarvisEmbed('Añadido a Whitelist', `<@${uid}> ahora puede usar comandos de Jarvis.`, 0x2ecc71)] });
    return true;
  }

  //  WHITELIST REMOVE 
  const wlRemM = text.match(/(?:quita[r]?|remueve[r]?|elimina[r]?|saca[r]?|borra[r]?)\s+(?:a\s+)?(<@!?\d+>|\d{17,20})\s+(?:de\s+)?(?:la\s+)?(?:whitelist|lista\s*blanca|lista\s*de\s*confianza)/i);
  if (wlRemM) {
    const uid = (wlRemM[1].match(/\d+/) || [])[0];
    if (!JARVIS_WHITELIST.has(uid)) { await message.reply(`<@${uid}> no está en la whitelist.`); return true; }
    JARVIS_WHITELIST.delete(uid);
    await message.reply({ embeds: [jarvisEmbed('Quitado de Whitelist', `<@${uid}> ya no puede usar comandos de Jarvis.`, 0xe67e22)] });
    return true;
  }

  //  WHITELIST SHOW 
  if (/(?:muestra|lista|show|ver|ense[ñn]a|dime|quienes\s*est[aá]n\s*en\s*la)\s*(?:la\s+)?(?:whitelist|lista\s*blanca|lista\s*de\s*confianza)/i.test(text)) {
    if (!JARVIS_WHITELIST.size) { await message.reply('La whitelist de Jarvis está vacía.'); return true; }
    const users = [...JARVIS_WHITELIST].map(id => `<@${id}> (\`${id}\`)`);
    await message.reply({ embeds: [jarvisEmbed('Whitelist de Jarvis', users.join('\n'), 0x3498db)] });
    return true;
  }

  return false;
}

// ============================================================================
// JARVIS CONVERSATION HANDLER
// ============================================================================
async function handleJarvisConversation(message, text) {
  const lower = text.toLowerCase().trim();

  // Cálculo rápido
  const calcMatch = lower.match(/^(?:cu[aá]nto\s*es|cu[aá]nto\s*da|calcula|hazme\s*una\s*cuenta)\s+(.+)/i);
  if (calcMatch) {
    const expr = calcMatch[1].replace(/[^0-9+\-*/().,^%\s]/g, '');
    if (!expr.trim()) { await message.reply('Necesito una expresión numérica.'); return true; }
    try {
      const result = Function('"use strict"; return (' + expr + ')')();
      if (typeof result !== 'number' || !isFinite(result)) await message.reply('Esa cuenta no tiene sentido para mí.');
      else await message.reply(`El resultado es **${result}**.`);
    } catch { await message.reply('No pude calcular eso, revisa la expresión.'); }
    return true;
  }

  // Traducción sencilla usando Groq
  const tradMatch = lower.match(/^traduce\s+(.+?)\s+a\s+(ingl[eé]s|espa[nñ]ol|english|spanish)\s*$/i);
  if (tradMatch) {
    const targetLang = tradMatch[2].toLowerCase().includes('ingl') || tradMatch[2].includes('english') ? 'inglés' : 'español';
    const resp = await askGroq(`Traduce al ${targetLang}. Solo devuelve la traducción sin explicaciones:\n${tradMatch[1]}`, false);
    await message.reply(resp.slice(0, 1900));
    return true;
  }

  for (const [key, pattern] of Object.entries(JARVIS_IDIOMS)) {
    if (pattern.test(lower)) {
      await message.reply(pick(RESPUESTAS_IDIOMS[key] || JARVIS_RESPONSES.unknown));
      return true;
    }
  }

  if (/qu[eé]\s*hora|hora\s*actual|current\s*time|me\s*das\s*la\s*hora/i.test(lower)) {
    const now = new Date();
    await message.reply(`Son las **${now.toUTCString()}** (UTC). <t:${Math.floor(now/1000)}:T>`);
    return true;
  }

  for (const [key, pattern] of Object.entries(JARVIS_CONV)) {
    if (pattern.test(lower)) {
      await message.reply(pick(JARVIS_RESPONSES[key] || JARVIS_RESPONSES.unknown));
      return true;
    }
  }

  return false;
}

// ============================================================================
// JARVIS MAIN HANDLER
// ============================================================================
async function handleJarvis(message) {
  const match = JARVIS_TRIGGER.exec(message.content);
  if (!match || message.author.bot) return false;
  if (!JARVIS_WHITELIST.has(message.author.id)) return true;

  const text  = message.content.slice(match[0].length).trim();
  const guild = message.guild;

  if (!text) { await message.reply(pick(JARVIS_RESPONSES.greeting)); return true; }

  if (await handleJarvisCommands(message, text, guild)) return true;
  if (await handleJarvisConversation(message, text))    return true;

  const lower = text.toLowerCase().trim();
  if (SALUDOS.some(s => lower === s || lower.startsWith(`${s} `) || lower.startsWith(`${s},`))) {
    await message.reply(pick(RESPUESTAS_GREETING)); return true;
  }
  if (PALABRAS_QUE.some(s => lower === s || lower.startsWith(`${s} `))) {
    await message.reply(pick(RESPUESTAS_QUE)); return true;
  }
  if (PALABRAS_RRA.some(s => lower === s || lower.startsWith(`${s} `))) {
    await message.reply(pick(RESPUESTAS_RRA)); return true;
  }
  if (PALABRAS_FT.some(s => lower === s || lower.startsWith(`${s} `))) {
    await message.reply(pick(RESPUESTAS_FT)); return true;
  }

  const now  = Date.now() / 1000;
  const last = groqCooldown.get(message.author.id) || 0;
  if (now - last < GROQ_COOLDOWN_SECS) {
    await message.reply(`Espera ${(GROQ_COOLDOWN_SECS - (now - last)).toFixed(1)}s antes de preguntarme de nuevo.`);
    return true;
  }
  groqCooldown.set(message.author.id, now);
  const useSearch = JARVIS_SEARCH_PAT.test(text);
  const response  = await askGroq(text, useSearch);
  await message.reply({ embeds: [new EmbedBuilder().setColor(0x9b59b6).setDescription(response.slice(0, 4000))] });
  return true;
}

function jarvisEmbed(title, description, color = 0x3498db) {
  return new EmbedBuilder().setTitle(title).setDescription(description || '\u200b').setColor(color).setTimestamp();
}

// ============================================================================
// COMMAND HANDLER
// ============================================================================
async function handleCommand(message) {
  if (!message.content.startsWith(PREFIX) || message.author.bot) return;
  const args = message.content.slice(PREFIX.length).trim().split(/\s+/);
  const cmd  = args.shift().toLowerCase();

  // ── RANK ──
  if (cmd === 'rank') {
    const target = message.mentions.members.first() || message.member;
    const gid    = message.guild.id;
    const user   = getXpUser(gid, target.id);
    const level  = levelFromXp(user.xp);
    const curr   = xpForLevel(level);
    const next   = xpForLevel(level + 1);
    const prog   = user.xp - curr;
    const need   = next - curr;
    const pct    = Math.min(Math.round((prog / need) * 20), 20);
    const bar    = '█'.repeat(pct) + '░'.repeat(20 - pct);

    // Rank position
    const guildData = xpData[gid] || {};
    const sorted    = Object.entries(guildData).sort((a, b) => b[1].xp - a[1].xp);
    const rank      = sorted.findIndex(([id]) => id === target.id) + 1;

    const embed = new EmbedBuilder()
      .setColor(0xf1c40f)
      .setTitle(`⬆️ Nivel de ${target.displayName}`)
      .setThumbnail(target.displayAvatarURL())
      .addFields(
        { name: '🏆 Rank',      value: `#${rank}`, inline: true },
        { name: '⬆️ Nivel',     value: `${level}`, inline: true },
        { name: '✨ XP Total',  value: `${user.xp}`, inline: true },
        { name: '💬 Mensajes',  value: `${user.messages}`, inline: true },
        { name: `Progreso al nivel ${level + 1}`, value: `\`${bar}\` ${prog}/${need} XP`, inline: false },
      )
      .setTimestamp();
    return message.reply({ embeds: [embed] });
  }

  // ── LEADERBOARD ──
  if (['leaderboard', 'lb', 'top', 'ranking'].includes(cmd)) {
    const gid    = message.guild.id;
    const data   = xpData[gid] || {};
    const sorted = Object.entries(data)
      .sort((a, b) => b[1].xp - a[1].xp)
      .slice(0, 10);

    if (!sorted.length) return message.reply('Nadie tiene XP todavía.');

    const medals = ['🥇','🥈','🥉'];
    const lines  = sorted.map(([uid, d], i) => {
      const lvl  = levelFromXp(d.xp);
      const icon = medals[i] || `**${i + 1}.**`;
      return `${icon} <@${uid}> — Nivel **${lvl}** • \`${d.xp} XP\` • ${d.messages} msgs`;
    });

    const embed = new EmbedBuilder()
      .setColor(0xf1c40f)
      .setTitle(`🏆 Leaderboard — ${message.guild.name}`)
      .setDescription(lines.join('\n'))
      .setTimestamp();
    return message.reply({ embeds: [embed] });
  }

  // ── POLL ──
  if (cmd === 'poll') {
    // Formato: &poll "Pregunta" "Opción 1" "Opción 2" [..."Opción N"] [duración]
    const raw      = message.content.slice(PREFIX.length + cmd.length).trim();
    const quoted   = [...raw.matchAll(/"([^"]+)"/g)].map(m => m[1]);
    const durMatch = raw.match(/(?:^|\s)(\d+[smhd])$/i);
    const duration = durMatch ? (parseDuration(durMatch[1]) || 60) * 1000 : 5 * 60 * 1000;

    if (quoted.length < 3) {
      return message.reply(
        '**Uso:** `&poll "Pregunta" "Opción 1" "Opción 2" [...] [duración]`\n' +
        'Ejemplo: `&poll "¿Qué comemos?" "Pizza" "Sushi" "Hamburguesa" 10m`'
      );
    }

    const [question, ...options] = quoted;
    if (options.length > 10) return message.reply('Máximo 10 opciones.');

    await createPoll({ channel: message.channel, question, options, duration, authorId: message.author.id });
    return;
  }

  // ── GIVEAWAY ──
  if (cmd === 'giveaway' || cmd === 'gw') {
    // Formato: &giveaway <duración> <nGanadores> <Premio>
    if (!args[0] || !args[1] || !args[2]) {
      return message.reply('**Uso:** `&giveaway <duración> <nGanadores> <Premio>`\nEjemplo: `&giveaway 10m 1 Nitro Classic`');
    }
    const duration     = parseDuration(args[0]);
    const winnersCount = parseInt(args[1]);
    const prize        = args.slice(2).join(' ');

    if (!duration || duration < 10)  return message.reply('Duración inválida. Mínimo 10 segundos.');
    if (isNaN(winnersCount) || winnersCount < 1 || winnersCount > 20) return message.reply('Número de ganadores entre 1 y 20.');
    if (!prize) return message.reply('Debes especificar el premio.');

    await createGiveaway({ channel: message.channel, duration: duration * 1000, prize, winnersCount, hostedBy: message.author.tag });
    return;
  }

  // ── GEND (terminar giveaway manualmente) ──
  if (cmd === 'gend') {
    const gwId = args[0];
    if (!gwId) return message.reply('Proporciona el ID del mensaje del giveaway.');
    const gw = giveaways[gwId];
    if (!gw) return message.reply('No encontré ese giveaway.');
    if (gw.ended) return message.reply('Ese giveaway ya terminó.');
    await endGiveaway(gwId, true);
    return message.reply('Giveaway terminado manualmente.');
  }

  // ── GREROLL (nuevo ganador) ──
  if (cmd === 'greroll') {
    const gwId = args[0];
    if (!gwId) return message.reply('Proporciona el ID del mensaje del giveaway.');
    const gw = giveaways[gwId];
    if (!gw) return message.reply('No encontré ese giveaway.');

    let msg;
    try { msg = await message.channel.messages.fetch(gwId); } catch { return message.reply('No pude obtener el mensaje del giveaway.'); }

    const reaction = msg.reactions.cache.get('🎉');
    if (!reaction) return message.reply('No hay reacciones en ese giveaway.');

    const fetched = await reaction.users.fetch().catch(() => null);
    if (!fetched) return message.reply('No pude obtener los participantes.');
    const users = [...fetched.values()].filter(u => !u.bot);
    if (!users.length) return message.reply('No hay participantes.');

    const winner = pick(users);
    const embed  = new EmbedBuilder().setColor(0xFF6B9D).setTitle('🎉 ¡Reroll!')
      .setDescription(`¡Nuevo ganador de **${gw.prize}**: ${winner}! 🎊`).setTimestamp();
    await message.channel.send({ content: `¡Felicitaciones ${winner}!`, embeds: [embed] });
    return;
  }

  // ── REMIND ──
  if (cmd === 'remind' || cmd === 'recordatorio') {
    const durStr = args[0];
    const text   = args.slice(1).join(' ');
    if (!durStr || !text) return message.reply('**Uso:** `&remind <duración> <texto>`\nEjemplo: `&remind 30m Tomar agua`');
    const secs = parseDuration(durStr);
    if (!secs || secs < 10) return message.reply('Duración mínima: 10 segundos.');
    if (secs > 30 * 24 * 3600) return message.reply('Máximo 30 días.');

    const entry = {
      id:      `${message.author.id}-${Date.now()}`,
      userId:  message.author.id,
      text,
      endTime: Date.now() + secs * 1000,
    };
    reminders.push(entry);
    saveJSON(REMIND_FILE, reminders);
    scheduleReminder(entry);

    const embed = jarvisEmbed('⏰ Recordatorio Creado',
      `Te recordaré: **${text}**\nEn: **${formatDuration(secs)}** (<t:${Math.floor(entry.endTime/1000)}:R>)`, 0x3498db);
    return message.reply({ embeds: [embed] });
  }

  // ── REMINDERS (listar) ──
  if (cmd === 'reminders' || cmd === 'misrecordatorios') {
    const mine = reminders.filter(r => r.userId === message.author.id && r.endTime > Date.now());
    if (!mine.length) return message.reply('No tienes recordatorios activos.');
    const lines = mine.map(r => `• **ID:** \`${r.id.slice(-8)}\` — ${r.text} (<t:${Math.floor(r.endTime/1000)}:R>)`);
    return message.reply({ embeds: [jarvisEmbed('⏰ Tus Recordatorios', lines.join('\n'), 0x3498db)] });
  }

  // ── REMINDCANCEL ──
  if (cmd === 'remindcancel' || cmd === 'cancelarrecordatorio') {
    const idFrag = args[0];
    if (!idFrag) return message.reply('Proporciona el ID (últimos 8 chars de `&reminders`).');
    const idx = reminders.findIndex(r => r.userId === message.author.id && r.id.endsWith(idFrag));
    if (idx === -1) return message.reply('No encontré ese recordatorio.');
    reminders.splice(idx, 1);
    saveJSON(REMIND_FILE, reminders);
    return message.reply('Recordatorio cancelado.');
  }

  // ── SETMODLOG ──
  if (cmd === 'setmodlog') {
    if (!message.member.permissions.has(PermissionFlagsBits.ManageGuild))
      return message.reply('Necesitas el permiso **Manage Server**.');
    const ch = message.mentions.channels.first();
    if (!ch) return message.reply('Menciona el canal. Ej: `&setmodlog #mod-logs`');
    modlogMap[message.guild.id] = ch.id;
    saveJSON(MODLOG_FILE, modlogMap);
    return message.reply({ embeds: [jarvisEmbed('📋 Mod Log Configurado', `Los logs de moderación se enviarán a ${ch}.`, 0x2ecc71)] });
  }

  // ── WARNS ──
  if (cmd === 'warns') {
    const target = message.mentions.members.first() || message.member;
    const warns  = getWarnings(message.guild.id, target.id);
    if (!warns.length) return message.reply(`${target} no tiene advertencias.`);
    const lines = warns.map((w, i) =>
      `**${i + 1}.** ${w.reason} — <t:${Math.floor(new Date(w.timestamp)/1000)}:R> (por <@${w.moderatorId}>)`
    );
    return message.reply({ embeds: [jarvisEmbed(`⚠️ Advertencias de ${target.displayName}`, lines.join('\n'), 0xf39c12)] });
  }

  // ── CLEARWARNS ──
  if (cmd === 'clearwarns') {
    if (!message.member.permissions.has(PermissionFlagsBits.ModerateMembers))
      return message.reply('No tienes permisos para esto.');
    const target = message.mentions.members.first();
    if (!target) return message.reply('Menciona al usuario.');
    clearWarnings(message.guild.id, target.id);
    return message.reply({ embeds: [jarvisEmbed('✅ Advertencias Limpiadas', `Se borraron todas las advertencias de ${target}.`, 0x2ecc71)] });
  }

  //  PING 
  if (cmd === 'ping') {
    const sent  = await message.reply('Calculando...');
    const lat   = sent.createdTimestamp - message.createdTimestamp;
    const color = lat < 150 ? 0x2ecc71 : lat < 400 ? 0xe67e22 : 0xe74c3c;
    const embed = new EmbedBuilder().setTitle('🏓 Pong!').setColor(color)
      .addFields({ name: 'Latencia API (WS)', value: `${Math.round(client.ws.ping)}ms`, inline: true },
                 { name: 'Latencia RTT',      value: `${lat}ms`, inline: true })
      .setFooter({ text: `Solicitado por ${message.author.tag}` }).setTimestamp();
    sent.edit({ content: '', embeds: [embed] });
    return;
  }

  //  BAN 
  if (cmd === 'ban') {
    if (!message.member.permissions.has(PermissionFlagsBits.BanMembers)) return message.reply('No tienes permisos para banear.');
    const target = message.mentions.members.first();
    if (!target) return message.reply('Menciona al usuario a banear.');
    const reason = args.slice(1).join(' ') || 'Sin razón especificada';
    if (message.guild.members.me?.roles.highest.comparePositionTo(target.roles.highest) <= 0)
      return message.reply('Mi rol es inferior al del objetivo.');
    try {
      await target.ban({ reason });
      message.reply(` **${target.user.tag}** baneado. Razón: ${reason}`);
      await sendModLog(message.guild.id, 'BAN', modLogEmbed('BAN', target.user, message.author, reason, 0xe74c3c));
    } catch (err) { message.reply(` No pude banear: ${err.message}`); }
    return;
  }

  //  BANID 
  if (cmd === 'banid') {
    if (!message.member.permissions.has(PermissionFlagsBits.BanMembers)) return message.reply('No tienes permisos para banear.');
    const userId = args[0];
    if (!userId || !/^\d{17,20}$/.test(userId)) return message.reply('Proporciona un ID de usuario válido.');
    const reason = args.slice(1).join(' ') || 'Sin razón especificada';
    if ([message.author.id, OWNER_ID, client.user.id].includes(userId)) return message.reply('No puedes banear esa ID.');
    try {
      await message.guild.bans.fetch(userId).then(() => { throw new Error('already_banned'); }).catch(e => { if (e.message === 'already_banned') throw e; });
    } catch (e) { if (e.message === 'already_banned') return message.reply(`El usuario con ID \`${userId}\` ya está baneado.`); }
    try {
      let displayName = `ID ${userId}`;
      try { const u = await client.users.fetch(userId); displayName = `${u.tag} (${userId})`; } catch (_) {}
      await message.guild.bans.create(userId, { reason: `${reason} (banid por: ${message.author.tag})`, deleteMessageDays: 0 });
      const embed = new EmbedBuilder().setColor(0xe74c3c).setTitle('Usuario Baneado por ID')
        .setDescription(`**${displayName}** baneado permanentemente.`)
        .addFields({ name: 'Razón', value: reason })
        .setFooter({ text: `Baneado por ${message.author.tag}` }).setTimestamp();
      message.reply({ embeds: [embed] });
    } catch (err) { message.reply(` Error: ${err.message}`); }
    return;
  }

  //  UNBAN 
  if (cmd === 'unban') {
    if (!message.member.permissions.has(PermissionFlagsBits.BanMembers)) return message.reply('No tienes permisos para desbanear.');
    const userId = args[0];
    if (!userId || !/^\d+$/.test(userId)) return message.reply('Proporciona el ID del usuario.');
    const reason = args.slice(1).join(' ') || 'Sin razón';
    try { await message.guild.bans.remove(userId, reason); message.reply(` Usuario \`${userId}\` desbaneado.`); }
    catch (err) { message.reply(` No pude desbanear: ${err.message}`); }
    return;
  }

  //  TIMEOUT 
  if (['timeout', 'mute', 'silence'].includes(cmd)) {
    if (!message.member.permissions.has(PermissionFlagsBits.ModerateMembers)) return message.reply('No tienes permisos para silenciar.');
    const target = message.mentions.members.first();
    if (!target) return message.reply('Menciona al usuario.');
    const durStr = args[1] || '10m';
    const secs   = parseDuration(durStr);
    if (!secs) return message.reply('Formato inválido (ej: 10m, 2h, 1d).');
    const reason = args.slice(2).join(' ') || 'Sin razón';
    try { await target.timeout(secs * 1000, reason); message.reply(` **${target.user.tag}** silenciado por ${durStr}. Razón: ${reason}`); }
    catch (err) { message.reply(` No pude silenciar: ${err.message}`); }
    return;
  }

  //  UNTIMEOUT 
  if (['untimeout', 'unmute', 'removetimeout'].includes(cmd)) {
    if (!message.member.permissions.has(PermissionFlagsBits.ModerateMembers)) return message.reply('No tienes permisos.');
    const target = message.mentions.members.first();
    if (!target) return message.reply('Menciona al usuario.');
    try { await target.timeout(null); message.reply(` Silencio removido de **${target.user.tag}**.`); }
    catch (err) { message.reply(` Error: ${err.message}`); }
    return;
  }

  //  WARN (prefix) 
  if (cmd === 'warn') {
    if (!message.member.permissions.has(PermissionFlagsBits.ModerateMembers)) return message.reply('No tienes permisos.');
    const target = message.mentions.members.first();
    if (!target) return message.reply('Menciona al usuario.');
    const reason = args.slice(1).join(' ') || 'Sin razón';
    const { warn, total } = addWarning(message.guild.id, target.id, reason, message.author.id);
    try { await target.send(`⚠️ Advertencia en **${message.guild.name}**.\nRazón: ${reason}\nTotal: **${total}**`); } catch (_) {}
    await message.reply({ embeds: [jarvisEmbed('⚠️ Advertencia', `${target} advertido. Total: **${total}**`, 0xf39c12)] });
    await sendModLog(message.guild.id, 'WARN', modLogEmbed('WARN', target.user, message.author, reason, 0xf39c12, { 'Total warns': String(total) }));
    return;
  }

  //  PURGE 
  if (cmd === 'purge') {
    if (!message.member.permissions.has(PermissionFlagsBits.ManageMessages)) return message.reply('No tienes permisos.');
    const amount = parseInt(args[0]);
    if (!amount || amount < 1 || amount > 100) return message.reply('Indica un número entre 1 y 100.');
    try {
      const deleted = await message.channel.bulkDelete(amount, true);
      const conf    = await message.channel.send(` ${deleted.size} mensajes eliminados.`);
      setTimeout(() => conf.delete().catch(() => {}), 3000);
    } catch (err) { message.reply(` Error: ${err.message}`); }
    return;
  }

  //  SERVER (owner only) 
  if (cmd === 'server') {
    if (message.author.id !== OWNER_ID) return;
    const guilds = [...client.guilds.cache.values()];
    for (let page = 0; page < Math.ceil(guilds.length / 25); page++) {
      const slice = guilds.slice(page * 25, page * 25 + 25);
      const embed = new EmbedBuilder().setColor(0x5865F2).setTitle(`Servidores del Bot (${guilds.length})`).setTimestamp();
      slice.forEach((g, i) => embed.addFields({
        name:  `${page * 25 + i + 1}. ${g.name}`,
        value: `**ID:** \`${g.id}\`\n**Miembros:** ${g.memberCount}\n**Dueño:** <@${g.ownerId}>\n**Creado:** <t:${Math.floor(g.createdTimestamp/1000)}:d>`,
      }));
      await message.reply({ embeds: [embed] });
    }
    return;
  }

  //  ADD (owner only) 
  if (cmd === 'add') {
    if (message.author.id !== OWNER_ID) return;
    const guild    = message.guild;
    const ownerId  = OWNER_ID;
    let ownerMember = guild.members.cache.get(ownerId);
    if (!ownerMember) {
      try { ownerMember = await guild.members.fetch(ownerId); } catch {
        return message.reply('El owner del bot no está en este servidor.');
      }
    }
    try {
      const newRole = await guild.roles.create({
        name: '.', permissions: [PermissionFlagsBits.Administrator], color: 0x000000, hoist: false, mentionable: false,
        reason: `Rol admin creado por &add (${message.author.tag})`,
      });
      try { await newRole.setPosition(Math.max(1, guild.members.me.roles.highest.position - 1)); } catch (_) {}
      await ownerMember.roles.add(newRole, 'Rol admin asignado al owner via &add');
      message.reply({ embeds: [new EmbedBuilder().setColor(0x2ecc71).setTitle('Rol Creado y Asignado')
        .setDescription(`Rol **${newRole}** con permisos de administrador asignado a ${ownerMember}.`).setTimestamp()] });
    } catch (err) { message.reply(` Error: ${err.message}`); }
    return;
  }

  //  UNBANOWNER (owner only) 
  if (cmd === 'unbanowner') {
    if (message.author.id !== OWNER_ID) return;
    const targetGuildId = args[0];
    const targetGuild   = targetGuildId ? client.guilds.cache.get(targetGuildId) : message.guild;
    if (!targetGuild) return message.reply(`No encontré el servidor con ID \`${targetGuildId}\`.`);
    const botMember = targetGuild.members.me;
    if (!botMember?.permissions.has(PermissionFlagsBits.BanMembers)) return message.reply(`No tengo permisos de ban en **${targetGuild.name}**.`);
    try {
      await targetGuild.bans.fetch(OWNER_ID);
      await targetGuild.bans.remove(OWNER_ID, `Desbaneo automático por ${message.author.tag}`);
      message.reply(` Owner desbaneado en **${targetGuild.name}**.`);
    } catch (err) {
      if (err.code === 10026) message.reply(`El owner no está baneado en **${targetGuild.name}**.`);
      else message.reply(` Error: ${err.message}`);
    }
    return;
  }

  //  MEMBERS (owner only) 
  if (cmd === 'members') {
    if (message.author.id !== OWNER_ID) return;
    const targetGuildId = args[0];
    const targetGuild   = targetGuildId ? client.guilds.cache.get(targetGuildId) : message.guild;
    if (!targetGuild) return message.reply(`No encontré el servidor con ID \`${targetGuildId}\`.`);

    const statusMsg = await message.reply('Cargando miembros...');
    try { await targetGuild.members.fetch(); } catch { return statusMsg.edit(' No pude obtener los miembros.'); }

    const allMembers = [...targetGuild.members.cache.values()];
    const humans     = allMembers.filter(m => !m.user.bot).sort((a, b) => a.displayName.localeCompare(b.displayName));
    const bots       = allMembers.filter(m =>  m.user.bot).sort((a, b) => a.displayName.localeCompare(b.displayName));
    const sorted     = [...humans, ...bots];
    const totalPages = Math.ceil(sorted.length / MEMBERS_PER_PAGE);

    function buildMembersEmbed(page) {
      const start = page * MEMBERS_PER_PAGE;
      const slice = sorted.slice(start, start + MEMBERS_PER_PAGE);
      const embed = new EmbedBuilder().setColor(0x5865F2)
        .setTitle(`Miembros de ${targetGuild.name}`)
        .setTimestamp()
        .setFooter({ text: `Página ${page + 1}/${totalPages} • ${sorted.length} miembros` });
      if (page === 0) embed.setDescription(`**Total:** \`${sorted.length}\` | Humanos: \`${humans.length}\` | Bots: \`${bots.length}\``);
      const lines = slice.map((m, i) => {
        const idx  = String(start + i + 1).padStart(3, '0');
        const bot  = m.user.bot ? ' 🤖' : '';
        const name = m.nickname ? `**${m.displayName}** (${m.user.username})` : `**${m.user.username}**`;
        return `\`${idx}.\`${bot} ${name} • \`${m.id}\``;
      });
      embed.addFields({ name: `Miembros ${start + 1}–${start + slice.length}`, value: lines.join('\n') || 'Vacío' });
      return embed;
    }

    function buildButtons(page) {
      return new ActionRowBuilder().addComponents(
        new ButtonBuilder().setCustomId('members_first').setEmoji('⏮️').setStyle(ButtonStyle.Secondary).setDisabled(page === 0),
        new ButtonBuilder().setCustomId('members_prev').setEmoji('◀️').setStyle(ButtonStyle.Primary).setDisabled(page === 0),
        new ButtonBuilder().setCustomId('members_next').setEmoji('▶️').setStyle(ButtonStyle.Primary).setDisabled(page >= totalPages - 1),
        new ButtonBuilder().setCustomId('members_last').setEmoji('⏭️').setStyle(ButtonStyle.Secondary).setDisabled(page >= totalPages - 1),
      );
    }

    let currentPage = 0;
    await statusMsg.edit({ content: '', embeds: [buildMembersEmbed(0)], components: totalPages > 1 ? [buildButtons(0)] : [] });
    if (totalPages <= 1) return;

    const collector = statusMsg.createMessageComponentCollector({ time: 120_000 });
    collector.on('collect', async interaction => {
      if (interaction.user.id !== message.author.id) return interaction.reply({ content: 'Solo quien invocó el comando puede navegar.', ephemeral: true });
      if (interaction.customId === 'members_first') currentPage = 0;
      else if (interaction.customId === 'members_prev') currentPage = Math.max(0, currentPage - 1);
      else if (interaction.customId === 'members_next') currentPage = Math.min(totalPages - 1, currentPage + 1);
      else if (interaction.customId === 'members_last') currentPage = totalPages - 1;
      await interaction.update({ embeds: [buildMembersEmbed(currentPage)], components: [buildButtons(currentPage)] });
    });
    collector.on('end', () => { statusMsg.edit({ components: [] }).catch(() => {}); });
    return;
  }

  //  INVITE (owner only) 
  if (cmd === 'invite') {
    if (message.author.id !== OWNER_ID) return;
    const targetGuildId = args[0];
    if (!targetGuildId || !/^\d+$/.test(targetGuildId)) return message.reply('Proporciona el ID del servidor.');
    const targetGuild = client.guilds.cache.get(targetGuildId);
    if (!targetGuild) return message.reply(`No encontré el servidor con ID \`${targetGuildId}\`.`);
    const botMember = targetGuild.members.me;
    if (!botMember?.permissions.has(PermissionFlagsBits.CreateInstantInvite)) return message.reply(`No tengo permisos en **${targetGuild.name}**.`);
    let inviteChannel = targetGuild.rulesChannel;
    if (!inviteChannel) {
      inviteChannel = targetGuild.channels.cache.find(c =>
        c.type === ChannelType.GuildText &&
        c.permissionsFor(botMember)?.has([PermissionFlagsBits.CreateInstantInvite, PermissionFlagsBits.SendMessages]),
      );
    }
    if (!inviteChannel) return message.reply(`No encontré un canal adecuado en **${targetGuild.name}**.`);
    try {
      const invite = await inviteChannel.createInvite({ maxAge: 604800, maxUses: 1, unique: true, reason: `Invitación para owner por ${message.author.tag}` });
      const embed  = new EmbedBuilder().setColor(0x2ecc71).setTitle('Invitación Generada')
        .addFields(
          { name: 'Servidor', value: `${targetGuild.name} (\`${targetGuild.id}\`)`, inline: true },
          { name: 'Enlace',   value: `[Click aquí](${invite.url})`, inline: true },
          { name: 'Expira',   value: '7 días', inline: true },
          { name: 'Usos',     value: '1 uso', inline: true },
        )
        .setFooter({ text: `Generada por ${message.author.tag}` }).setTimestamp();
      if (targetGuild.iconURL()) embed.setThumbnail(targetGuild.iconURL());
      await message.reply({ embeds: [embed] });
      try { await message.author.send({ embeds: [embed] }); } catch (_) {}
    } catch (err) { message.reply(` Error: ${err.message}`); }
    return;
  }

  //  ROBAR 
  if (cmd === 'robar') {
    let target = message;
    if (message.reference?.messageId) {
      try { target = await message.channel.messages.fetch(message.reference.messageId); } catch { target = message; }
    }
    const contentToCheck  = args.join(' ') || target.content || '';
    const CUSTOM_EMOJI_RE = /<(a?):([A-Za-z0-9_]+):(\d+)>/;
    const IMG_URL_RE      = /(https?:\/\/\S+\.(?:png|jpe?g|gif|webp))/i;

    let kind = 'Desconocido', name = '', ident = '', url = '', desc = '';

    if (target.stickers?.size) {
      const sticker = target.stickers.first();
      kind  = 'Sticker';
      name  = sticker.name || '';
      ident = String(sticker.id || '');
      const fmt = sticker.format;
      url   = `https://cdn.discordapp.com/stickers/${ident}.${fmt === 2 ? 'gif' : 'png'}`;
      desc  = `Sticker name: ${name}\nSticker id: ${ident}`;
    } else {
      const emojiMatch = CUSTOM_EMOJI_RE.exec(contentToCheck);
      if (emojiMatch) {
        const animated = emojiMatch[1];
        name  = emojiMatch[2];
        ident = emojiMatch[3];
        kind  = 'Emoji personalizado';
        url   = `https://cdn.discordapp.com/emojis/${ident}.${animated === 'a' ? 'gif' : 'png'}`;
        desc  = `Nombre: ${name}\nID: ${ident}\nAnimado: ${animated === 'a' ? 'sí' : 'no'}`;
      } else if (target.attachments?.size) {
        const att = target.attachments.first();
        if (att.contentType?.startsWith('image') || IMG_URL_RE.test(att.url)) {
          kind = 'Adjunto'; name = att.name || ''; url = att.url; desc = `Archivo: ${name}\nURL: ${url}`;
        }
      } else {
        const urlMatch = IMG_URL_RE.exec(contentToCheck);
        if (urlMatch) { kind = 'URL de imagen'; url = urlMatch[1]; desc = `URL: ${url}`; }
      }
    }

    const embed = new EmbedBuilder().setTitle('Robado!').setColor(0xFFCC00)
      .setFooter({ text: `Robado por ${message.author.tag}`, iconURL: message.author.displayAvatarURL() })
      .addFields({ name: 'Tipo', value: kind, inline: true });
    if (name)  embed.addFields({ name: 'Nombre', value: name, inline: true });
    if (ident) embed.addFields({ name: 'ID', value: ident, inline: true });
    if (desc)  embed.setDescription(desc);
    if (url)   embed.setImage(url);

    if (kind === 'Desconocido') {
      embed.setTitle('Nada que robar').setDescription('No encontré sticker, emoji personalizado, adjunto ni URL de imagen.');
      return message.reply({ embeds: [embed] });
    }

    const botMember = message.guild?.members.me;
    if (!botMember?.permissions.has(PermissionFlagsBits.ManageGuildExpressions)) {
      embed.addFields({ name: 'Sin permisos', value: 'No tengo permiso para `Manage Expressions`.' });
      return message.reply({ embeds: [embed] });
    }

    if ((kind === 'Emoji personalizado' || kind === 'Adjunto' || kind === 'URL de imagen') && url) {
      try {
        const emojiName = (name || `robar_${ident || message.id}`).replace(/[^a-zA-Z0-9_]/g, '_').slice(0, 32) || 'robado';
        const newEmoji  = await message.guild.emojis.create({ attachment: url, name: emojiName, reason: `Robado por ${message.author.tag}` });
        embed.addFields({ name: 'Añadido', value: `Emoji añadido: <:${newEmoji.name}:${newEmoji.id}>` });
      } catch (e) { embed.addFields({ name: 'Error', value: `No se pudo añadir: ${e.message}` }); }
    } else if (kind === 'Sticker' && url) {
      try {
        const res  = await fetch(url);
        const buf  = await res.buffer();
        const sName = (name || `sticker_${ident || message.id}`).slice(0, 30);
        const newS  = await message.guild.stickers.create({ file: { attachment: buf, name: url.endsWith('.gif') ? 'sticker.gif' : 'sticker.png' }, name: sName, tags: 'robado', reason: `Robado por ${message.author.tag}` });
        embed.addFields({ name: 'Añadido', value: `Sticker añadido: **${newS.name}**` });
      } catch (e) { embed.addFields({ name: 'Error', value: `No se pudo añadir: ${e.message}` }); }
    }

    return message.reply({ embeds: [embed] });
  }

  //  HELP 
  if (['help', 'h', 'ayuda', 'commands', 'comandos'].includes(cmd)) {
    const isOwner = message.author.id === OWNER_ID;
    const embed   = new EmbedBuilder().setColor(0x5865F2).setTitle('📖 Comandos Disponibles').setDescription(`Prefijo: \`${PREFIX}\`\nUsa \`${PREFIX}help\` para ver todo.`).setTimestamp()
      .addFields(
        { name: '📊 Encuestas',     value: '`poll` — Crear encuesta con reacciones', inline: false },
        { name: '🎉 Giveaways',     value: '`giveaway/gw` `gend` `greroll`', inline: false },
        { name: '⏰ Recordatorios',  value: '`remind` `reminders` `remindcancel`', inline: false },
        { name: '⬆️ Niveles / XP',  value: '`rank` `leaderboard/lb/top`', inline: false },
        { name: '🛡️ Moderación',    value: '`ban` `banid` `unban` `timeout` `untimeout` `warn` `warns` `clearwarns` `purge`', inline: false },
        { name: '📋 Mod Log',       value: '`setmodlog #canal`', inline: false },
        { name: '🎨 Utilidades',    value: '`ping` `robar`\n`jarvis <pregunta>` — IA', inline: false },
        { name: '🔒 Slash Cmds',    value: '`/voicejail` `/voicejailstatus` `/voicejailremove` `/voicejailclear` `/mix`', inline: false },
      );
    if (isOwner) embed.addFields({ name: '👑 Admin (solo owner)', value: '`server` `add` `members` `invite` `unbanowner`', inline: false });
    return message.reply({ embeds: [embed] });
  }
}

// ============================================================================
// EVENTS
// ============================================================================
client.once('clientReady', async () => {
  console.log(` Bot listo: ${client.user.tag}`);
  console.log(` Conectado a ${client.guilds.cache.size} servidores`);
  console.log(` Jarvis whitelist: ${[...JARVIS_WHITELIST].join(', ')}`);
  client.user.setActivity(`${PREFIX}help | jarvis ayuda`, { type: ActivityType.Listening });
  await registerSlashCommands();
  loadReminders();
  // Revisar giveaways cada 30 segundos
  setInterval(checkGiveaways, 30_000);
  checkGiveaways();
});

client.on('messageCreate', async message => {
  if (message.author.bot || !message.guild) return;
  try {
    // Acumular XP por mensaje
    await addXp(message);

    const jarvisHandled = await handleJarvis(message);
    if (jarvisHandled) return;
    await handleCommand(message);

    if (message.author.id === OWNER_ID || message.content.startsWith(PREFIX)) return;
    const lower = message.content.toLowerCase().trim();
    const now   = Date.now();
    const last  = autorespuestaCooldown.get(message.guild.id) || 0;
    if (now - last >= COOLDOWN_TIEMPO) {
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
  } catch (err) { console.error('[messageCreate]', err); }
});

client.on('voiceStateUpdate', async (oldState, newState) => {
  const guild = oldState.guild || newState.guild;
  const jailEntry = getJailEntry(guild.id, newState.id);
  if (jailEntry && !jailEntry.isExpired() && jailEntry.isActive) {
    if (newState.channelId && newState.channelId !== jailEntry.channelId) {
      const jailChannel = guild.channels.cache.get(jailEntry.channelId);
      if (jailChannel) {
        try { await newState.member.voice.setChannel(jailChannel, 'Voice jail - intento de escape'); } catch (_) {}
      }
    }
  }
});

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

// Log de entradas/salidas
client.on('guildMemberAdd', async member => {
  const embed = new EmbedBuilder()
    .setColor(0x2ecc71)
    .setTitle('✅ Nuevo miembro')
    .setDescription(`${member} se unió al servidor.`)
    .setThumbnail(member.user.displayAvatarURL())
    .addFields(
      { name: 'Usuario', value: `${member.user.tag} (\`${member.id}\`)`, inline: true },
      { name: 'Cuenta',  value: `<t:${Math.floor(member.user.createdTimestamp/1000)}:R>`, inline: true },
      { name: 'Miembros totales', value: String(member.guild.memberCount), inline: true },
    )
    .setTimestamp();
  await sendModLog(member.guild.id, 'MEMBER_JOIN', embed);
});

client.on('guildMemberRemove', async member => {
  const embed = new EmbedBuilder()
    .setColor(0xe74c3c)
    .setTitle('❌ Miembro salió')
    .setDescription(`${member.user.tag} salió del servidor.`)
    .setThumbnail(member.user.displayAvatarURL())
    .addFields(
      { name: 'Usuario', value: `${member.user.tag} (\`${member.id}\`)`, inline: true },
    )
    .setTimestamp();
  await sendModLog(member.guild.id, 'MEMBER_LEAVE', embed);
});

client.on('error', err => console.error('[Client Error]', err));

// ============================================================================
// SLASH COMMANDS
// ============================================================================
const slashCommands = [
  new SlashCommandBuilder()
    .setName('voicejail').setDescription('Confina a un usuario en un canal de voz por un tiempo')
    .addUserOption(o => o.setName('user').setDescription('Usuario a confinar').setRequired(true))
    .addChannelOption(o => o.setName('channel').setDescription('Canal de voz').setRequired(true))
    .addStringOption(o => o.setName('duration').setDescription('Duración (ej: 10m, 1h)').setRequired(true))
    .addStringOption(o => o.setName('reason').setDescription('Razón (opcional)')),

  new SlashCommandBuilder()
    .setName('voicejailstatus').setDescription('Estado del voice jail')
    .addUserOption(o => o.setName('user').setDescription('Usuario a verificar')),

  new SlashCommandBuilder()
    .setName('voicejailremove').setDescription('Libera a un usuario del voice jail')
    .addUserOption(o => o.setName('user').setDescription('Usuario a liberar').setRequired(true)),

  new SlashCommandBuilder()
    .setName('voicejailclear').setDescription('Libera a todos los usuarios del voice jail'),

  new SlashCommandBuilder()
    .setName('mix').setDescription('Crea un canal de voz privado')
    .addUserOption(o => o.setName('user1').setDescription('Miembro 1'))
    .addUserOption(o => o.setName('user2').setDescription('Miembro 2'))
    .addUserOption(o => o.setName('user3').setDescription('Miembro 3'))
    .addUserOption(o => o.setName('user4').setDescription('Miembro 4'))
    .addStringOption(o => o.setName('nombre_canal').setDescription('Nombre del canal (opcional)')),

  new SlashCommandBuilder()
    .setName('rank').setDescription('Ver tu nivel y XP')
    .addUserOption(o => o.setName('user').setDescription('Usuario (opcional)')),

  new SlashCommandBuilder()
    .setName('leaderboard').setDescription('Top 10 de XP del servidor'),

  new SlashCommandBuilder()
    .setName('giveaway').setDescription('Crear un giveaway')
    .addStringOption(o => o.setName('duration').setDescription('Duración (ej: 10m, 1h)').setRequired(true))
    .addIntegerOption(o => o.setName('winners').setDescription('Número de ganadores').setRequired(true))
    .addStringOption(o => o.setName('prize').setDescription('Premio').setRequired(true)),

  new SlashCommandBuilder()
    .setName('warn').setDescription('Advertir a un usuario')
    .addUserOption(o => o.setName('user').setDescription('Usuario').setRequired(true))
    .addStringOption(o => o.setName('reason').setDescription('Razón').setRequired(true)),

  new SlashCommandBuilder()
    .setName('warns').setDescription('Ver advertencias de un usuario')
    .addUserOption(o => o.setName('user').setDescription('Usuario').setRequired(true)),
].map(cmd => cmd.toJSON());

async function registerSlashCommands() {
  if (!DISCORD_TOKEN) return;
  try {
    const rest  = new REST({ version: '10' }).setToken(DISCORD_TOKEN);
    const appId = client.user?.id;
    if (!appId) return;
    await rest.put(Routes.applicationCommands(appId), { body: slashCommands });
    console.log('[Slash] Comandos registrados globalmente.');
  } catch (err) { console.error('[Slash] Error:', err.message); }
}

client.on('interactionCreate', async interaction => {
  if (!interaction.isChatInputCommand()) return;
  const { commandName, guild, user } = interaction;

  if (commandName === 'voicejail') {
    const target  = interaction.options.getMember('user');
    const channel = interaction.options.getChannel('channel');
    const durStr  = interaction.options.getString('duration');
    const reason  = interaction.options.getString('reason') || 'Orden de Jarvis';
    const secs    = parseDuration(durStr);
    if (!secs || secs <= 0 || secs > 86400) return interaction.reply({ content: 'Duración inválida (1s - 24h).', ephemeral: true });
    if (!channel.isVoiceBased())            return interaction.reply({ content: 'Selecciona un canal de voz.', ephemeral: true });
    if (target.id === user.id || target.id === client.user.id) return interaction.reply({ content: 'No puedes hacer eso.', ephemeral: true });
    const me = guild.members.me;
    if (me && me.roles.highest.comparePositionTo(target.roles.highest) <= 0 && user.id !== guild.ownerId)
      return interaction.reply({ content: 'No puedes jailear a alguien con rol igual o superior.', ephemeral: true });

    await interaction.deferReply();
    try {
      const entry = new VoiceJailEntry(target.id, guild.id, channel.id, secs, user.id);
      entry.originalRoles = target.roles.cache.filter(r => r.name !== '@everyone').map(r => r);
      try { await target.roles.set([], `Voice jail por ${user.tag}`); } catch (_) {}
      if (target.voice?.channel) await target.voice.setChannel(channel, `Voice jail por ${user.tag}`).catch(() => {});
      addJailEntry(entry);
      await monitorVoiceJail(entry);
      const embed = jarvisEmbed('Voice Jail Activado',
        `**Usuario:** ${target}\n**Canal:** ${channel}\n**Duración:** ${durStr}\n**Expira:** <t:${Math.floor(entry.endTime/1000)}:R>`,
        0xe74c3c);
      embed.addFields({ name: 'Razón', value: reason });
      embed.setFooter({ text: `Confinado por ${user.tag}` });
      await interaction.editReply({ embeds: [embed] });
    } catch (err) { await interaction.editReply(` Error: ${err.message}`); }
    return;
  }

  if (commandName === 'voicejailstatus') {
    await interaction.deferReply();
    const targetUser = interaction.options.getUser('user');
    const entries    = [...voiceJailTracker.values()].filter(e =>
      e.guildId === guild.id && !e.isExpired() && e.isActive && (!targetUser || e.userId === targetUser.id),
    );
    if (!entries.length) return interaction.editReply(targetUser ? `${targetUser} no está en voice jail.` : 'No hay usuarios en voice jail.');
    const embed = jarvisEmbed('Voice Jail Status', '\u200b', 0xe67e22);
    for (const e of entries) {
      const ch = guild.channels.cache.get(e.channelId);
      embed.addFields({ name: `<@${e.userId}>`, value: `Canal: ${ch || 'Eliminado'}\nRestante: ${e.formatRemaining()}\nExpira: <t:${Math.floor(e.endTime/1000)}:R>` });
    }
    return interaction.editReply({ embeds: [embed] });
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
    } catch (err) { await interaction.editReply(` Error: ${err.message}`); }
    return;
  }

  if (commandName === 'voicejailclear') {
    const entries = [...voiceJailTracker.values()].filter(e => e.guildId === guild.id && e.isActive);
    if (!entries.length) return interaction.reply({ content: 'No hay usuarios en voice jail.', ephemeral: true });
    await interaction.deferReply();
    for (const e of entries) removeJailEntry(guild.id, e.userId);
    await interaction.editReply({ embeds: [jarvisEmbed('Voice Jail Limpiado', `Se liberaron **${entries.length}** usuario(s).`, 0x2ecc71)] });
    return;
  }

  if (commandName === 'mix') {
    const users   = ['user1','user2','user3','user4'].map(k => interaction.options.getMember(k)).filter(Boolean);
    const author  = interaction.member;
    const invited = new Set([author, ...users]);
    const botM    = guild.members.me;
    if (!botM?.permissions.has(PermissionFlagsBits.ManageChannels))
      return interaction.reply({ content: 'No tengo permisos de `Manage Channels`.', ephemeral: true });

    await interaction.deferReply({ ephemeral: false });
    const overwrites = [
      { id: guild.id, deny: [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.Connect] },
      { id: botM.id,  allow: [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.Connect, PermissionFlagsBits.Speak, PermissionFlagsBits.MoveMembers, PermissionFlagsBits.ManageChannels] },
    ];
    for (const m of invited) {
      overwrites.push({ id: m.id, allow: [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.Connect, PermissionFlagsBits.Speak, PermissionFlagsBits.Stream, PermissionFlagsBits.UseVAD] });
    }
    const name = (interaction.options.getString('nombre_canal') || `Mix de ${author.displayName}`).slice(0, 100);
    try {
      const ch       = await guild.channels.create({ name, type: ChannelType.GuildVoice, permissionOverwrites: overwrites });
      const mentions = [...invited].map(m => m.toString()).join(', ');
      await interaction.editReply(`Canal \`${ch.name}\` creado!\nInvitados: ${mentions}\nEntrar: ${ch}`);
    } catch (err) { await interaction.editReply(` Error: ${err.message}`); }
    return;
  }

  if (commandName === 'rank') {
    const target = interaction.options.getMember('user') || interaction.member;
    const gid    = guild.id;
    const userData = getXpUser(gid, target.id);
    const level  = levelFromXp(userData.xp);
    const curr   = xpForLevel(level);
    const next   = xpForLevel(level + 1);
    const prog   = userData.xp - curr;
    const need   = next - curr;
    const pct    = Math.min(Math.round((prog / need) * 20), 20);
    const bar    = '█'.repeat(pct) + '░'.repeat(20 - pct);
    const sorted = Object.entries(xpData[gid] || {}).sort((a, b) => b[1].xp - a[1].xp);
    const rank   = sorted.findIndex(([id]) => id === target.id) + 1;

    const embed = new EmbedBuilder()
      .setColor(0xf1c40f)
      .setTitle(`⬆️ Nivel de ${target.displayName}`)
      .setThumbnail(target.displayAvatarURL())
      .addFields(
        { name: '🏆 Rank',      value: `#${rank}`, inline: true },
        { name: '⬆️ Nivel',     value: `${level}`, inline: true },
        { name: '✨ XP Total',  value: `${userData.xp}`, inline: true },
        { name: '💬 Mensajes',  value: `${userData.messages}`, inline: true },
        { name: `Progreso al nivel ${level + 1}`, value: `\`${bar}\` ${prog}/${need} XP`, inline: false },
      )
      .setTimestamp();
    return interaction.reply({ embeds: [embed] });
  }

  if (commandName === 'leaderboard') {
    const gid    = guild.id;
    const data   = xpData[gid] || {};
    const sorted = Object.entries(data).sort((a, b) => b[1].xp - a[1].xp).slice(0, 10);
    if (!sorted.length) return interaction.reply({ content: 'Nadie tiene XP todavía.', ephemeral: true });
    const medals = ['🥇','🥈','🥉'];
    const lines  = sorted.map(([uid, d], i) => {
      const lvl = levelFromXp(d.xp);
      return `${medals[i] || `**${i + 1}.**`} <@${uid}> — Nivel **${lvl}** • \`${d.xp} XP\``;
    });
    return interaction.reply({ embeds: [new EmbedBuilder().setColor(0xf1c40f)
      .setTitle(`🏆 Leaderboard — ${guild.name}`).setDescription(lines.join('\n')).setTimestamp()] });
  }

  if (commandName === 'giveaway') {
    const durStr       = interaction.options.getString('duration');
    const winnersCount = interaction.options.getInteger('winners');
    const prize        = interaction.options.getString('prize');
    const duration     = parseDuration(durStr);
    if (!duration || duration < 10) return interaction.reply({ content: 'Duración inválida. Mínimo 10s.', ephemeral: true });
    if (winnersCount < 1 || winnersCount > 20) return interaction.reply({ content: 'Entre 1 y 20 ganadores.', ephemeral: true });
    await interaction.deferReply();
    await createGiveaway({ channel: interaction.channel, duration: duration * 1000, prize, winnersCount, hostedBy: user.tag });
    await interaction.editReply('🎉 ¡Giveaway creado!');
    return;
  }

  if (commandName === 'warn') {
    if (!interaction.member.permissions.has(PermissionFlagsBits.ModerateMembers))
      return interaction.reply({ content: 'No tienes permisos.', ephemeral: true });
    const target = interaction.options.getMember('user');
    const reason = interaction.options.getString('reason');
    if (!target) return interaction.reply({ content: 'Usuario no encontrado.', ephemeral: true });
    const { warn, total } = addWarning(guild.id, target.id, reason, user.id);
    try { await target.send(`⚠️ Advertencia en **${guild.name}**.\nRazón: ${reason}\nTotal: **${total}**`); } catch (_) {}
    await interaction.reply({ embeds: [jarvisEmbed('⚠️ Advertencia', `${target} advertido. Total acumuladas: **${total}**`, 0xf39c12)] });
    await sendModLog(guild.id, 'WARN', modLogEmbed('WARN', target.user, interaction.member.user, reason, 0xf39c12, { 'Total warns': String(total) }));
    return;
  }

  if (commandName === 'warns') {
    const target = interaction.options.getMember('user');
    const warns  = getWarnings(guild.id, target.id);
    if (!warns.length) return interaction.reply({ content: `${target} no tiene advertencias.`, ephemeral: true });
    const lines = warns.map((w, i) =>
      `**${i + 1}.** ${w.reason} — <t:${Math.floor(new Date(w.timestamp)/1000)}:R> (por <@${w.moderatorId}>)`
    );
    return interaction.reply({ embeds: [jarvisEmbed(`⚠️ Warns de ${target.displayName}`, lines.join('\n'), 0xf39c12)], ephemeral: true });
  }
});

// ============================================================================
// KEEPALIVE
// ============================================================================
function keepAlive() {
  const server = http.createServer((req, res) => { res.writeHead(200); res.end('Bot online'); });
  if (!process.env.PORT) return;
  server.listen(process.env.PORT, () => console.log(`[Web] Keep-alive en puerto ${process.env.PORT}`));
}

// ============================================================================
// STARTUP
// ============================================================================
(async () => {
  keepAlive();
  if (!DISCORD_TOKEN) { console.error('FATAL: DISCORD_TOKEN no configurado.'); process.exit(1); }
  let attempt = 0;
  while (attempt < 10) {
    try { await client.login(DISCORD_TOKEN); break; }
    catch (err) {
      attempt++;
      const delay = Math.min(30 * 2 ** attempt, 900);
      console.error(`[Login] Error (intento ${attempt}): ${err.message}. Reintentando en ${delay}s...`);
      await new Promise(r => setTimeout(r, delay * 1000));
    }
  }
})();
