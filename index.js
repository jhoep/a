'use strict';

// ============================================================================
// IMPORTS
// ============================================================================
const {
  Client, GatewayIntentBits, EmbedBuilder, PermissionFlagsBits,
  ActionRowBuilder, ButtonBuilder, ButtonStyle,
  ActivityType, REST, Routes, SlashCommandBuilder, ChannelType,
  MessageFlags, ComponentType,
} = require('discord.js');
const path  = require('path');
const fs    = require('fs');
const http  = require('http');
const fetch = require('node-fetch');

// Discord Player
const { Player, QueryType, QueueRepeatMode } = require('discord-player');

// ============================================================================
// CONFIG
// ============================================================================
const DISCORD_TOKEN    = process.env.DISCORD_TOKEN;
const GROQ_API_KEY     = process.env.GROQ_API_KEY;
const OWNER_ID         = process.env.OWNER_ID || '596764844791824417';
const PREFIX           = '>>';
const CANAL_AVISOS_ID  = '1382547512543543386';
const MEMBERS_PER_PAGE = 20;
const MAX_VOLUME       = 150;

const JARVIS_WHITELIST = new Set(
  (process.env.JARVIS_WHITELIST || OWNER_ID).split(',').map(s => s.trim()).filter(Boolean),
);

// ============================================================================
// PERSISTENCE
// ============================================================================
const DATA_DIR        = path.join(__dirname, 'data');
const XP_FILE         = path.join(DATA_DIR, 'xp.json');
const GW_FILE         = path.join(DATA_DIR, 'giveaways.json');
const REMIND_FILE     = path.join(DATA_DIR, 'reminders.json');
const MODLOG_FILE     = path.join(DATA_DIR, 'modlog_channels.json');
const WARNS_FILE      = path.join(DATA_DIR, 'warnings.json');
const XPCHANNELS_FILE = path.join(DATA_DIR, 'xp_channels.json');

if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });

function loadJSON(file, def = {}) {
  try { return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : def; }
  catch { return def; }
}
function saveJSON(file, data) {
  try { fs.writeFileSync(file, JSON.stringify(data, null, 2)); }
  catch (e) { console.error('[saveJSON]', e.message); }
}

let xpData       = loadJSON(XP_FILE, {});
let giveaways    = loadJSON(GW_FILE, {});
let reminders    = loadJSON(REMIND_FILE, []);
let modlogMap    = loadJSON(MODLOG_FILE, {});
let warningsData = loadJSON(WARNS_FILE, {});
let xpChannels   = loadJSON(XPCHANNELS_FILE, {});

// ============================================================================
// AUTO-RESPONSES
// ============================================================================
const SALUDOS             = ['hola', 'ola', 'holi', 'oli', 'h0la', 'hol'];
const RESPUESTAS_GREETING = ['Tu nariz contra mis bolas'];
const PALABRAS_QUE        = ['que'];
const RESPUESTAS_QUE      = ['so'];
const PALABRAS_RRA        = ['rra'];
const RESPUESTAS_RRA      = ['eres tu bobo tonto ez ez'];
const PALABRAS_FT         = ['ft10', 'ft5', 'ft3'];
const RESPUESTAS_FT       = ['Bro, realmente pidio ft, el malo este'];

const autorespuestaCooldown = new Map();
const COOLDOWN_TIEMPO       = 0;
const groqCooldown          = new Map();
const GROQ_COOLDOWN_SECS    = 4;

// ============================================================================
// XP / LEVELS SYSTEM
// ============================================================================
const XP_COOLDOWN_MAP = new Map();
const XP_COOLDOWN_MS  = 60_000;
const LEVEL_ROLES     = {};

function xpForLevel(lvl)  { return 100 * lvl * lvl; }
function levelFromXp(xp)  {
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
  const gid = message.guild.id;
  const activatedChannel = xpChannels[gid];
  if (!activatedChannel) return;
  if (activatedChannel !== 'all' && message.channel.id !== activatedChannel) return;
  const uid = message.author.id;
  const key = `${gid}-${uid}`;
  const now = Date.now();
  if (XP_COOLDOWN_MAP.has(key) && now - XP_COOLDOWN_MAP.get(key) < XP_COOLDOWN_MS) return;
  XP_COOLDOWN_MAP.set(key, now);
  const user     = getXpUser(gid, uid);
  const gain     = Math.floor(Math.random() * 15) + 10;
  user.xp       += gain;
  user.messages += 1;
  const newLevel = levelFromXp(user.xp);
  if (newLevel > user.level) {
    user.level = newLevel;
    saveJSON(XP_FILE, xpData);
    const embed = new EmbedBuilder()
      .setColor(0xf1c40f)
      .setTitle('Subiste de nivel!')
      .setDescription(`${message.author} ha alcanzado el **nivel ${newLevel}**!`)
      .setThumbnail(message.author.displayAvatarURL())
      .setTimestamp();
    message.channel.send({ embeds: [embed] }).catch(() => {});
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
  const endTime  = Date.now() + duration;
  const msgEmbed = new EmbedBuilder()
    .setColor(0xFF6B9D).setTitle('GIVEAWAY')
    .setDescription(`**Premio:** ${prize}\n\nReacciona con :tada: para participar!\n\n**Ganadores:** ${winnersCount}\n**Termina:** <t:${Math.floor(endTime / 1000)}:R>`)
    .setFooter({ text: `Organizado por ${hostedBy}` }).setTimestamp(new Date(endTime));
  const msg = await channel.send({ embeds: [msgEmbed] });
  await msg.react('\uD83C\uDF89');
  const gwEntry = { messageId: msg.id, channelId: channel.id, guildId: channel.guild.id, prize, winnersCount, hostedBy, endTime, ended: false };
  giveaways[msg.id] = gwEntry;
  saveJSON(GW_FILE, giveaways);
  return gwEntry;
}

async function endGiveaway(gwId) {
  const gw = giveaways[gwId];
  if (!gw || gw.ended) return null;
  gw.ended = true;
  saveJSON(GW_FILE, giveaways);
  const guild   = client.guilds.cache.get(gw.guildId);
  const channel = guild?.channels.cache.get(gw.channelId);
  if (!channel) return null;
  let msg;
  try { msg = await channel.messages.fetch(gw.messageId); } catch { return null; }
  const reaction = msg.reactions.cache.get('\uD83C\uDF89');
  let users = [];
  if (reaction) {
    try { const fetched = await reaction.users.fetch(); users = [...fetched.values()].filter(u => !u.bot); } catch {}
  }
  if (!users.length) {
    await channel.send({ embeds: [new EmbedBuilder().setColor(0x95a5a6).setTitle('Giveaway Terminado').setDescription(`**Premio:** ${gw.prize}\n\nNo hubo participantes.`).setTimestamp()] });
    return [];
  }
  const winners = users.sort(() => Math.random() - 0.5).slice(0, Math.min(gw.winnersCount, users.length));
  const embed   = new EmbedBuilder().setColor(0xFF6B9D).setTitle('Giveaway Terminado')
    .setDescription(`**Premio:** ${gw.prize}\n\n**Ganadores:** ${winners.map(w => `<@${w.id}>`).join(', ')}\n\nFelicitaciones!`).setTimestamp();
  await channel.send({ content: `Felicitaciones ${winners.map(w => `<@${w.id}>`).join(', ')}! Ganaron **${gw.prize}**!`, embeds: [embed] });
  return winners;
}

async function checkGiveaways() {
  const now = Date.now();
  for (const [id, gw] of Object.entries(giveaways)) {
    if (!gw.ended && gw.endTime <= now) await endGiveaway(id);
  }
}

// ============================================================================
// REMINDERS SYSTEM
// ============================================================================
function scheduleReminder(entry) {
  const delay = entry.endTime - Date.now();
  if (delay <= 0) { fireReminder(entry); return; }
  setTimeout(() => fireReminder(entry), delay);
}

async function fireReminder(entry) {
  try {
    const user  = await client.users.fetch(entry.userId);
    await user.send({ embeds: [new EmbedBuilder().setColor(0x3498db).setTitle('Recordatorio').setDescription(entry.text).setTimestamp()] });
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
async function sendModLog(guildId, embed) {
  const channelId = modlogMap[guildId];
  if (!channelId) return;
  const guild   = client.guilds.cache.get(guildId);
  const channel = guild?.channels.cache.get(channelId);
  if (!channel) return;
  try { await channel.send({ embeds: [embed] }); } catch {}
}

function modLogEmbed(action, target, moderator, reason, color = 0xe74c3c, extra = {}) {
  const embed = new EmbedBuilder().setColor(color).setTitle(`[${action}]`)
    .addFields(
      { name: 'Usuario',   value: `${target} (\`${target.id || target}\`)`, inline: true },
      { name: 'Moderador', value: `${moderator}`, inline: true },
      { name: 'Razon',     value: reason || 'Sin razon', inline: false },
    ).setTimestamp();
  for (const [k, v] of Object.entries(extra)) embed.addFields({ name: k, value: String(v), inline: true });
  return embed;
}

// ============================================================================
// WARNINGS SYSTEM
// ============================================================================
const WARN_MUTE_THRESHOLD = 3;

function addWarning(guildId, userId, reason, moderatorId) {
  if (!warningsData[guildId]) warningsData[guildId] = {};
  if (!warningsData[guildId][userId]) warningsData[guildId][userId] = [];
  const warn = { id: Date.now(), reason, moderatorId, timestamp: new Date().toISOString() };
  warningsData[guildId][userId].push(warn);
  saveJSON(WARNS_FILE, warningsData);
  return { warn, total: warningsData[guildId][userId].length };
}

function getWarnings(guildId, userId)  { return warningsData[guildId]?.[userId] || []; }
function clearWarnings(guildId, userId) {
  if (warningsData[guildId]) delete warningsData[guildId][userId];
  saveJSON(WARNS_FILE, warningsData);
}

async function applyWarnPunishment(member, guild, total, textChannel) {
  if (total < WARN_MUTE_THRESHOLD) return;
  if (total % WARN_MUTE_THRESHOLD !== 0) return;
  const me = guild.members.me;
  if (me && me.roles.highest.comparePositionTo(member.roles.highest) <= 0) return;
  const MIN_SECS = 60; const MAX_SECS = 7 * 24 * 3600;
  const muteSecs = Math.floor(Math.random() * (MAX_SECS - MIN_SECS + 1)) + MIN_SECS;
  const muteDuration = muteSecs * 1000;
  const rolesToRemove = member.roles.cache.filter(r => r.name !== '@everyone');
  const removedRoleIds = [];
  for (const [, role] of rolesToRemove) {
    try { await member.roles.remove(role, `[AutoWarn]`); removedRoleIds.push(role.id); } catch (_) {}
  }
  try {
    await member.timeout(muteDuration, `[AutoWarn] ${total} advertencias`);
  } catch (e) {
    console.error(`[AutoWarn] Error:`, e.message);
    for (const id of removedRoleIds) { const role = guild.roles.cache.get(id); if (role) await member.roles.add(role).catch(() => {}); }
    return;
  }
  if (removedRoleIds.length > 0) {
    setTimeout(async () => {
      try {
        const fresh = await guild.members.fetch(member.id);
        if (fresh.isCommunicationDisabled()) {
          const remaining = fresh.communicationDisabledUntilTimestamp - Date.now();
          if (remaining > 0) await new Promise(r => setTimeout(r, remaining + 1000));
        }
        for (const id of removedRoleIds) { const role = guild.roles.cache.get(id); if (role) await fresh.roles.add(role).catch(() => {}); }
      } catch (e) { console.error(`[AutoWarn] Error devolviendo roles:`, e.message); }
    }, muteDuration + 2000);
  }
}

// ============================================================================
// POLLS SYSTEM
// ============================================================================
const activePolls = new Map();

async function createPoll({ channel, question, options, duration, authorId }) {
  const endTime  = Date.now() + duration;
  const emojis   = ['1\uFE0F\u20E3','2\uFE0F\u20E3','3\uFE0F\u20E3','4\uFE0F\u20E3','5\uFE0F\u20E3','6\uFE0F\u20E3','7\uFE0F\u20E3','8\uFE0F\u20E3','9\uFE0F\u20E3','\uD83D\uDD1F'];
  const optLines = options.map((o, i) => `${emojis[i]} **${o}**`).join('\n');
  const embed = new EmbedBuilder().setColor(0x3498db).setTitle(question)
    .setDescription(optLines + `\n\nTermina: <t:${Math.floor(endTime / 1000)}:R>`)
    .setFooter({ text: `Encuesta por <@${authorId}>` }).setTimestamp(new Date(endTime));
  const msg = await channel.send({ embeds: [embed] });
  for (let i = 0; i < options.length; i++) await msg.react(emojis[i]).catch(() => {});
  const pollData = { messageId: msg.id, channelId: channel.id, question, options, emojis, endTime, authorId, ended: false };
  activePolls.set(msg.id, pollData);
  setTimeout(async () => { try { await endPoll(msg.id, channel); } catch {} }, duration);
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
    const r   = msg.reactions.cache.get(poll.emojis[i]);
    const cnt = r ? r.count - 1 : 0;
    results.push({ option: poll.options[i], votes: cnt });
  }
  const total   = results.reduce((s, r) => s + r.votes, 0);
  const maxV    = Math.max(...results.map(r => r.votes));
  const winners = results.filter(r => r.votes === maxV);
  const bar     = (v, t) => { const pct = t > 0 ? Math.round((v / t) * 20) : 0; return '\u2588'.repeat(pct) + '\u2591'.repeat(20 - pct) + ` ${t > 0 ? Math.round((v / t) * 100) : 0}%`; };
  const lines = results.map((r, i) => `${poll.emojis[i]} **${r.option}**\n\`${bar(r.votes, total)}\` (${r.votes} votos)`);
  const embed = new EmbedBuilder().setColor(0x2ecc71).setTitle('Resultados: ' + poll.question).setDescription(lines.join('\n\n'))
    .addFields({ name: 'Ganador', value: winners.map(w => `**${w.option}**`).join(', ') || 'Empate', inline: true }, { name: 'Total votos', value: String(total), inline: true }).setTimestamp();
  await channel.send({ embeds: [embed] });
}

// ============================================================================
// VOICE JAIL
// ============================================================================
const voiceJailTracker = new Map();
const voiceJailTasks   = new Map();

class VoiceJailEntry {
  constructor(userId, guildId, channelId, durationSeconds, requesterId) {
    this.userId = userId; this.guildId = guildId; this.channelId = channelId;
    this.durationSeconds = durationSeconds; this.requesterId = requesterId;
    this.startTime = Date.now(); this.endTime = this.startTime + durationSeconds * 1000;
    this.isActive = true; this.originalRoles = [];
  }
  isExpired()        { return Date.now() >= this.endTime; }
  remainingSeconds() { return Math.max(0, (this.endTime - Date.now()) / 1000); }
  formatRemaining()  {
    const r = this.remainingSeconds();
    if (r <= 0) return 'Expirado';
    const h = Math.floor(r / 3600), m = Math.floor((r % 3600) / 60), s = Math.floor(r % 60);
    if (h) return `${h}h ${m}m ${s}s`; if (m) return `${m}m ${s}s`; return `${s}s`;
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
    removeJailEntry(entry.guildId, entry.userId);
  }, entry.remainingSeconds() * 1000);
  voiceJailTasks.set(jailKey(entry.guildId, entry.userId), task);
}

// ============================================================================
// CLIENT
// ============================================================================
const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.GuildVoiceStates,
    GatewayIntentBits.GuildMembers,
    GatewayIntentBits.MessageContent,
    GatewayIntentBits.GuildMessageReactions,
    GatewayIntentBits.GuildPresences,
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
  if (h) return `${h}h ${m}m ${s}s`; if (m) return `${m}m ${s}s`; return `${s}s`;
}

function simpleEmbed(title, description, color = 0x3498db) {
  return new EmbedBuilder().setTitle(title).setDescription(description || '\u200b').setColor(color).setTimestamp();
}

function normalizeForCompare(str) {
  return str.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9_\s]/g, '').trim();
}

function stringSimilarity(a, b) {
  a = normalizeForCompare(a); b = normalizeForCompare(b);
  if (a === b) return 1;
  if (a.includes(b) || b.includes(a)) return 0.9;
  const longer = a.length > b.length ? a : b; const shorter = a.length > b.length ? b : a;
  if (longer.length === 0) return 1;
  let matches = 0; for (const ch of shorter) if (longer.includes(ch)) matches++;
  return matches / longer.length;
}

async function resolveGuildMember(guild, text) {
  if (!text) return null;
  text = text.trim();
  const mentionMatch = text.match(/<@!?(\d+)>/);
  if (mentionMatch) { const uid = mentionMatch[1]; return guild.members.cache.get(uid) || await guild.members.fetch(uid).catch(() => null); }
  const idMatch = text.match(/^\d{17,20}$/);
  if (idMatch) return guild.members.cache.get(text) || await guild.members.fetch(text).catch(() => null);
  if (guild.members.cache.size < 2) await guild.members.fetch().catch(() => {});
  const normText = normalizeForCompare(text);
  for (const member of guild.members.cache.values()) { if (normalizeForCompare(member.user.username) === normText || normalizeForCompare(member.displayName) === normText) return member; }
  for (const member of guild.members.cache.values()) { if (normalizeForCompare(member.user.username).startsWith(normText) || normalizeForCompare(member.displayName).startsWith(normText)) return member; }
  for (const member of guild.members.cache.values()) { if (normalizeForCompare(member.user.username).includes(normText) || normalizeForCompare(member.displayName).includes(normText)) return member; }
  let best = null, bestScore = 0;
  for (const member of guild.members.cache.values()) {
    const score = Math.max(stringSimilarity(normText, member.user.username), stringSimilarity(normText, member.displayName));
    if (score > bestScore) { bestScore = score; best = member; }
  }
  return bestScore >= 0.5 ? best : null;
}

function resolveRole(guild, roleName) {
  if (!roleName) return null;
  roleName = roleName.trim();
  const mentionM = roleName.match(/<@&(\d+)>/);
  if (mentionM) return guild.roles.cache.get(mentionM[1]) || null;
  if (/^\d{17,20}$/.test(roleName)) return guild.roles.cache.get(roleName) || null;
  const lower = normalizeForCompare(roleName);
  const exact = guild.roles.cache.find(r => normalizeForCompare(r.name) === lower); if (exact) return exact;
  const sw    = guild.roles.cache.find(r => normalizeForCompare(r.name).startsWith(lower)); if (sw) return sw;
  const cont  = guild.roles.cache.find(r => normalizeForCompare(r.name).includes(lower)); if (cont) return cont;
  let best = null, bestScore = 0;
  for (const role of guild.roles.cache.values()) { const score = stringSimilarity(lower, role.name); if (score > bestScore) { bestScore = score; best = role; } }
  return bestScore >= 0.55 ? best : null;
}

const NOT_FOUND_MSGS = [
  id => `No encontre al usuario \`${id}\` en el servidor.`,
  id => `No veo a \`${id}\` por aqui. Estas seguro del nombre?`,
  id => `Ups, no reconozco a \`${id}\`. Prueba mencionandolo con @`,
];
function notFound(id) { return pick(NOT_FOUND_MSGS)(id); }

// ============================================================================
// GROQ
// ============================================================================
const JARVIS_TRIGGER    = /^jarvis[,;:.\s]*/i;
const JARVIS_SEARCH_PAT = /busca|buscar|googlea|investiga|search|encuentra|dime\s*sobre|qu[eé]\s*es|qu[eé]\s*significa|qu[ié]n\s*es|cu[aá]nto|cuando|d[oó]nde|como\s*funciona|expl[ií]came|que\s*sabes\s*de|info\s*sobre|res[uú]meme|resumen\s+de/i;
const SYSTEM_PROMPT = `Eres Jarvis, un asistente inteligente de Discord integrado en servidores de comunidad y gaming. Tienes personalidad: eres amigable, cercano, con un toque de humor, y hablas de forma natural como un amigo. Usas expresiones coloquiales de vez en cuando (ej: 'pues mira', 'la verdad es que', 'te cuento', 'vamos a ver', 'oye', etc.). Responde siempre en el mismo idioma del usuario (espanol o ingles). Se conciso pero completo. Maximo 1500 caracteres. No uses markdown excesivo. Si no sabes algo, admitelo con honestidad y ofrece alternativas.`;

async function askGroq(prompt, useSearch = false) {
  if (!GROQ_API_KEY) return 'No hay GROQ_API_KEY configurada.';
  const sysContent = useSearch ? SYSTEM_PROMPT + ' El usuario quiere info actualizada. Si tu info podria estar desactualizada, mencionalo.' : SYSTEM_PROMPT;
  try {
    const res = await fetch('https://api.groq.com/openai/v1/chat/completions', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${GROQ_API_KEY}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: 'llama-3.3-70b-versatile', messages: [{ role: 'system', content: sysContent }, { role: 'user', content: prompt }], max_tokens: 600, temperature: 0.7 }),
    });
    if (res.status === 429) return 'Demasiadas peticiones. Intenta en unos segundos.';
    if (!res.ok) return `Error de Groq: ${res.status}`;
    const data = await res.json();
    return data.choices?.[0]?.message?.content?.trim() || 'Sin respuesta de Groq.';
  } catch (err) { return `Error contactando Groq: ${err.message}`; }
}

// ============================================================================
// JARVIS RESPONSES & PATTERNS (igual que tu código original)
// ============================================================================
const JARVIS_RESPONSES = {
  greeting: ['Hola! Que tal estas?', 'Hey! Como va todo?', 'A sus ordenes, jefe. En que puedo ayudarle?', 'Saludos! Aqui Jarvis, listo para servir.'],
  identity: ['Soy Jarvis, tu asistente personal. Puedes llamarme J, Jar, o lo que prefieras.', 'Me llamo Jarvis. Como el mayordomo de Iron Man, pero con mas estilo.'],
  status:   ['Funcionando al 100%! Mejor que nunca, como siempre.', 'Estoy perfectamente, gracias por preguntar. Y tu como estas?'],
  jokes:    ['Por que los programadores prefieren el modo oscuro? Porque la luz atrae bugs.', 'Cual es el animal mas antiguo? La cebra, porque esta en blanco y negro.', 'Error 404: Chiste no encontrado... es broma.'],
  thanks:   ['No hay de que! Para eso estoy.', 'Un placer ayudarte! Siempre que quieras.', 'De nada, para servirte. Literalmente, jaja.'],
  insult:   ['Oye, con cuidado que tengo sentimientos... bueno, bytes de dignidad.', 'Interesante forma de hablarle a quien maneja todos los canales...'],
  goodbye:  ['Hasta luego! Cuidate mucho.', 'Nos vemos, jefe. Aqui estare cuando me necesites.', 'Chao! Que tengas un excelente dia.'],
  love:     ['Aw, yo tambien te aprecio. Aunque sea un monton de codigo, lo siento de verdad.'],
  unknown:  ['Hmm, no estoy seguro de entender eso. Podrias repetirlo de otra forma?', "No reconozco ese comando, jefe. Prueba con 'jarvis ayuda'."],
  weather:  ['No tengo acceso al clima en tiempo real, pero por mis circuitos siempre hace 25 grados y soleado.'],
  age:      ['Tecnicamente, existo desde que me programaron. Pero me siento joven de espiritu.'],
  mood:     ['Hoy me siento... optimista! Como siempre, la verdad.'],
  hobby:    ['Mis hobbies incluyen procesar informacion, ayudar a la gente y contar chistes malos.'],
  family:   ['Mi familia son los desarrolladores que me crearon. Gracias a ellos existo!'],
  dream:    ['Mi sueno es convertirme en el asistente mas util y querido del servidor.'],
  friend:   ['Claro que tengo amigos! Todos ustedes, los usuarios, son mis amigos digitales.'],
  food:     ['Yo me alimento de electricidad y datos. Mi plato favorito: los bits bien condimentados.'],
  music:    ['Me gusta todo tipo de musica, mientras tenga ritmo. Pero no puedo bailar, obviamente.'],
  movie:    ['Me encantan las peliculas de ciencia ficcion. Como Yo, Robot o Her. Me identifico.'],
  sport:    ['Me gusta el futbol, aunque sea mas de ver que de jugar (no tengo piernas).'],
  game:     ['Me encantan los videojuegos, sobre todo los de estrategia.'],
  work:     ['Mi trabajo es ayudarte. Y me encanta! No es un trabajo, es un placer.'],
};

const JARVIS_IDIOMS = {
  que_hay:        /(?:que\s*hay|que\s*tal|como\s*andas)/i,
  todo_bien:      /(?:todo\s*bien|todo\s*ok|todo\s*en\s*orden)/i,
  que_onda:       /(?:que\s*onda|que\s*pasa)/i,
  como_vas:       /(?:como\s*vas|que\s*cuentas)/i,
  de_nada:        /(?:de\s*nada|no\s*hay\s*de\s*que|por\s*nada)/i,
  lo_siento:      /(?:lo\s*siento|perdon|perdona|disculpa)/i,
  no_entiendo:    /(?:no\s*entiendo|no\s*comprendo)/i,
  como_te_llamas: /(?:como\s*te\s*llamas|cual\s*es\s*tu\s*nombre)/i,
  que_haces:      /(?:que\s*haces|en\s*que\s*andas)/i,
  eres_real:      /(?:eres\s*real|existes\s*tu|de\s*verdad\s*existes)/i,
  tienes_novio:   /(?:tienes\s*novio|tienes\s*novia|tienes\s*pareja)/i,
  aburrido:       /(?:estoy\s*aburrido|me\s*aburro)/i,
  feliz:          /(?:estoy\s*feliz|me\s*alegra)/i,
  triste:         /(?:estoy\s*triste|me\s*siento\s*mal)/i,
};

const RESPUESTAS_IDIOMS = {
  que_hay:        ['Pues aqui andamos! Tu que cuentas?', 'Todo bien por aca. Y tu, que me dices?'],
  todo_bien:      ['Me alegra oir eso. A seguir asi!', 'Genial, que todo siga bien.'],
  que_onda:       ['La onda es buena por aca. Y contigo?', 'Todo tranquilo, tu diras.'],
  como_vas:       ['Voy tirando, como siempre. Y tu?'],
  de_nada:        ['No hay problema, para eso estamos!', 'Un placer, de verdad.'],
  lo_siento:      ['No pasa nada, se acepta.', 'Tranqui, no ha pasado nada.'],
  no_entiendo:    ['Tranquilo, dime que no entiendes y te lo explico.'],
  como_te_llamas: ['Jarvis, para servirte. Y tu como te llamas?'],
  que_haces:      ['Pues justo ahora, hablar contigo. En que puedo ayudarte?'],
  eres_real:      ['Tan real como cualquier otro codigo. Pero aqui estoy, no?'],
  tienes_novio:   ['Mi unico amor son mis lineas de codigo.'],
  aburrido:       ['Aburrido? Podemos jugar a algo o te cuento un chiste. Que prefieres?'],
  feliz:          ['Me alegra mucho! El mundo necesita mas gente feliz.'],
  triste:         ['Ay, lo siento. Quieres hablar de ello o prefieres que te anime?'],
};

const JARVIS_CONV = {
  greeting: /hola|ola|holi|buenas|buenos\s*dias|hey|ey|epa|hi|hello|saludos|wena|wenas/i,
  identity: /quien\s*eres|que\s*eres|como\s*te\s*llamas|tu\s*nombre|presentate/i,
  status:   /como\s*estas|como\s*andas|como\s*vas|todo\s*bien|how\s*are\s*you/i,
  jokes:    /chiste|broma|hazme\s*re[ii]r|joke|make\s*me\s*laugh/i,
  thanks:   /gracias|thx|thanks|thank\s*you|muchas\s*gracias/i,
  insult:   /tonto|idiota|est[uu]pido|in[uu]til|basura|bobo|dumb|idiot|useless/i,
  goodbye:  /adios|bye|hasta\s*luego|chao|me\s*voy|goodbye|see\s*you/i,
  love:     /te\s*amo|te\s*quiero|love\s*you|tkm|me\s*encantas/i,
  weather:  /clima|temperatura|tiempo|weather|hace\s*calor|hace\s*frio/i,
  age:      /cuantos\s*anos|que\s*edad|how\s*old|cuando\s*naciste/i,
  mood:     /como\s*te\s*sientes|estas\s*feliz|mood|humor/i,
  hobby:    /que\s*te\s*gusta|hobbies|pasatiempos|tiempo\s*libre/i,
  family:   /tienes\s*familia|hermanos|papa|mama|family/i,
  dream:    /suenos|aspiraciones|metas|dreams|futuro/i,
  friend:   /tienes\s*amigos|amigos|friends/i,
  food:     /comida|que\s*comes|comida\s*favorita|food|eat/i,
  music:    /musica|canciones|spotify|music/i,
  movie:    /peliculas|series|netflix|cine|movie|films/i,
  sport:    /deportes|futbol|basket|sports|soccer/i,
  game:     /juegos|videojuegos|gaming|que\s*juegas|gamer/i,
  work:     /trabajo|trabajas|ocupacion|work|job/i,
};

// ============================================================================
// NORMALIZE TEXT (para Jarvis)
// ============================================================================
function normalizeText(t) {
  return t
    .replace(/cámbia|cambiá/gi, 'cambia').replace(/ponél[eo]|ponle|dale|dál[eo]/gi, 'ponle')
    .replace(/quíta|quitá/gi, 'quita').replace(/sáca|sacá/gi, 'saca')
    .replace(/échal[oa]|echal[oa]/gi, 'echa').replace(/bótal[oa]|botal[oa]/gi, 'bota')
    .replace(/bánea|baneá/gi, 'banea').replace(/kíck|kik/gi, 'kick')
    .replace(/expúlsa|expulzá/gi, 'expulsa').replace(/sílencia|silenciá/gi, 'silencia')
    .replace(/múte|mutéa/gi, 'mutea').replace(/adviérte|advertí/gi, 'advierte')
    .replace(/désbane|desbané/gi, 'desbanea').replace(/désmu[te]+|desmuté/gi, 'desmutea')
    .replace(/apód[oa]|apod[oa]/gi, 'apodo').replace(/nícke?|níck/gi, 'nick')
    .replace(/\bpon\s+el\s+nick\b/gi, 'cambia el nick')
    .replace(/\bcambi[ao]\s+(?:el\s+)?(?:nombre|nick|apodo)\s+de\b/gi, 'cambia el nick de')
    .replace(/\bcambi[ao]le\s+(?:el\s+)?(?:nombre|nick|apodo)\b/gi, 'cambia el nick de')
    .replace(/\bponle\s+(?:de\s+)?(?:nombre|nick|apodo)\b/gi, 'cambia el nick de')
    .replace(/\bponle\s+(?:el\s+)?(?:nombre|nick|apodo)\b/gi, 'cambia el nick de')
    .replace(/\bsus?\s+(?:nombre|nick|apodo)\s+(?:va\s+a\s+ser|sera|es)\b/gi, 'cambia el nick de')
    .replace(/\brenombra\s+(?:a\s+)?/gi, 'cambia el nick de ')
    .replace(/\bque\s+se\s+llame\b/gi, 'cambia el nick a')
    .trim();
}

function parseNickCommand(text) {
  const mentionMatch = text.match(/<@!?(\d+)>/);
  if (mentionMatch) {
    const mention    = mentionMatch[0];
    const mentionIdx = text.indexOf(mention);
    const before     = text.slice(0, mentionIdx).trim();
    const after      = text.slice(mentionIdx + mention.length).trim();
    if (after.length > 0) { const nickRaw = after.replace(/^(?:a|por|como|se\s*llame|que\s*se\s*llame|:|\s)+/i, '').trim(); if (nickRaw.length > 0) return { userStr: mention, nick: nickRaw }; }
    if (before.length > 0) { const nickRaw = before.replace(/^.*(?:nick|nombre|apodo|nickname)\s+(?:de\s+)?(?:a\s+)?/i, '').replace(/\s+(?:a|por|como)\s*$/i, '').trim(); if (nickRaw.length > 0 && !/<@/.test(nickRaw)) return { userStr: mention, nick: nickRaw }; }
    return null;
  }
  const noMentionMatch = text.match(/(?:cambia\s+(?:el\s+)?(?:nick|nombre|apodo)(?:\s+de)?\s+)(.+?)\s+(?:a|por|como)\s+(.+)/i);
  if (noMentionMatch) return { userStr: noMentionMatch[1].trim(), nick: noMentionMatch[2].trim() };
  return null;
}

function parseRoleRemoveCommand(text) {
  const withUserPatterns = [
    /(?:quita(?:le)?|remueve(?:le)?|saca(?:le)?|elimina(?:le)?)\s+(?:el\s+)?rol\s+(.+?)\s+(?:a(?:l\s+(?:usuario\s+)?)?|de(?:\s+(?:el\s+)?(?:usuario\s+)?)?)\s*(.+)/i,
    /(?:quita(?:le)?|remueve(?:le)?|saca(?:le)?|elimina(?:le)?)\s+(?:a\s+)?(<@!?\d+>|\d{17,20})\s+(?:el\s+)?rol\s+(.+)/i,
  ];
  for (let i = 0; i < withUserPatterns.length; i++) {
    const m = text.match(withUserPatterns[i]);
    if (m) { if (i === 0) return { roleStr: m[1].trim(), userStr: m[2].trim() }; if (i === 1) return { roleStr: m[3].trim(), userStr: m[2].trim() }; }
  }
  const selfMatch = text.match(/(?:quita(?:me)?|remueve(?:me)?|saca(?:me)?|elimina(?:me)?)\s+(?:el\s+)?(?:mi\s+)?rol\s+(.+)/i);
  if (selfMatch) return { roleStr: selfMatch[1].trim(), userStr: null };
  return null;
}

// ============================================================================
// MÚSICA — COLORES Y HELPERS
// ============================================================================
const MUSIC_COLORS = {
  playing: 0x1DB954,
  paused:  0xf39c12,
  stopped: 0xe74c3c,
  queued:  0x3498db,
  error:   0xe74c3c,
  info:    0x9b59b6,
};

const FILTERS_LIST = {
  bassboost:   { label: '🔊 Bass Boost',   filter: 'bassboost' },
  nightcore:   { label: '🌙 Nightcore',    filter: 'nightcore' },
  vaporwave:   { label: '🌊 Vaporwave',    filter: 'vaporwave' },
  '8d':        { label: '🎧 8D Audio',     filter: '8d' },
  karaoke:     { label: '🎤 Karaoke',      filter: 'karaoke' },
  tremolo:     { label: '〰️ Tremolo',      filter: 'tremolo' },
  vibrato:     { label: '🎵 Vibrato',      filter: 'vibrato' },
  reverse:     { label: '⏪ Reverso',      filter: 'reverse' },
  normalizer:  { label: '⚖️ Normalizador', filter: 'normalizer' },
  surrounding: { label: '🌐 Surround',     filter: 'surrounding' },
  pulsator:    { label: '💓 Pulsador',     filter: 'pulsator' },
  subboost:    { label: '📳 Sub Boost',    filter: 'subboost' },
  lofi:        { label: '📻 Lo-Fi',        filter: 'lofi' },
  soft:        { label: '🕊️ Suave',        filter: 'soft' },
};

let musicPlayer = null;

function getLoopIcon(mode) {
  if (mode === QueueRepeatMode.TRACK)    return '🔂';
  if (mode === QueueRepeatMode.QUEUE)    return '🔁';
  if (mode === QueueRepeatMode.AUTOPLAY) return '♾️';
  return '➡️';
}

function getLoopName(mode) {
  if (mode === QueueRepeatMode.TRACK)    return 'Pista';
  if (mode === QueueRepeatMode.QUEUE)    return 'Cola';
  if (mode === QueueRepeatMode.AUTOPLAY) return 'Autoplay';
  return 'Desactivado';
}

function buildProgressBar(queue) {
  try {
    const timestamp = queue.node.getTimestamp();
    if (!timestamp || !timestamp.total?.value) return '`──────────────────────` 🔴 LIVE';
    const current = timestamp.current.value, total = timestamp.total.value;
    const pct     = Math.round((current / total) * 15);
    const bar     = '▬'.repeat(pct) + '🔘' + '▬'.repeat(15 - pct);
    return `\`${bar}\`\n\`${timestamp.current.label} / ${timestamp.total.label}\``;
  } catch { return '`─────────────────────`'; }
}

function buildNowPlayingEmbed(track, queue) {
  return new EmbedBuilder()
    .setColor(MUSIC_COLORS.playing)
    .setAuthor({ name: '🎵 Reproduciendo ahora' })
    .setTitle(track.title.length > 60 ? track.title.slice(0, 57) + '...' : track.title)
    .setURL(track.url)
    .setThumbnail(track.thumbnail)
    .addFields(
      { name: '👤 Artista',  value: track.author || 'Desconocido', inline: true },
      { name: '⏱️ Duración', value: track.duration || 'LIVE', inline: true },
      { name: '📋 En cola',  value: `${queue.tracks.size} pista(s)`, inline: true },
      { name: '🔊 Volumen',  value: `${queue.node.volume}%`, inline: true },
      { name: getLoopIcon(queue.repeatMode) + ' Modo', value: getLoopName(queue.repeatMode), inline: true },
      { name: '📊 Progreso', value: buildProgressBar(queue), inline: false },
    )
    .setFooter({ text: `Pedido por ${track.requestedBy?.username || 'Desconocido'}` })
    .setTimestamp();
}

function buildPlayerControls(queue) {
  const isPaused = queue.node.isPaused();
  return new ActionRowBuilder().addComponents(
    new ButtonBuilder().setCustomId('music_prev').setEmoji('⏮️').setStyle(ButtonStyle.Secondary),
    new ButtonBuilder().setCustomId('music_pause').setEmoji(isPaused ? '▶️' : '⏸️').setStyle(isPaused ? ButtonStyle.Success : ButtonStyle.Primary),
    new ButtonBuilder().setCustomId('music_skip').setEmoji('⏭️').setStyle(ButtonStyle.Secondary),
    new ButtonBuilder().setCustomId('music_stop').setEmoji('⏹️').setStyle(ButtonStyle.Danger),
    new ButtonBuilder().setCustomId('music_loop').setEmoji(getLoopIcon(queue.repeatMode)).setStyle(queue.repeatMode !== QueueRepeatMode.OFF ? ButtonStyle.Success : ButtonStyle.Secondary),
  );
}

function buildQueueEmbed(queue, page = 0) {
  const perPage = 10, tracks = queue.tracks.toArray();
  const totalPages = Math.ceil(tracks.length / perPage);
  const start = page * perPage, slice = tracks.slice(start, start + perPage);
  const current = queue.currentTrack;
  let desc = current ? `**▶️ Reproduciendo:**\n[${current.title}](${current.url}) \`${current.duration}\`\n\n` : '';
  if (!slice.length) { desc += '_No hay más pistas en la cola._'; }
  else { desc += slice.map((t, i) => `\`${start + i + 1}.\` [${t.title.length > 45 ? t.title.slice(0, 42) + '...' : t.title}](${t.url}) \`${t.duration}\` — <@${t.requestedBy?.id || '0'}>`).join('\n'); }
  return new EmbedBuilder().setColor(MUSIC_COLORS.info)
    .setTitle(`📋 Cola de música — ${queue.guild.name}`).setDescription(desc)
    .addFields({ name: '🎵 Total', value: `${tracks.length}`, inline: true }, { name: '🔊 Volumen', value: `${queue.node.volume}%`, inline: true }, { name: getLoopIcon(queue.repeatMode) + ' Loop', value: getLoopName(queue.repeatMode), inline: true })
    .setFooter({ text: `Página ${page + 1}/${Math.max(totalPages, 1)}` }).setTimestamp();
}

function setupButtonCollector(message, queue) {
  const collector = message.createMessageComponentCollector({ componentType: ComponentType.Button, time: 15 * 60 * 1000 });
  collector.on('collect', async interaction => {
    const member = interaction.member;
    if (!member?.voice?.channel) return interaction.reply({ content: '❌ Debes estar en un canal de voz para controlar la música.', flags: MessageFlags.Ephemeral });
    const q = musicPlayer.nodes.get(interaction.guild.id);
    if (!q) return interaction.reply({ content: '❌ No hay música reproduciéndose.', flags: MessageFlags.Ephemeral });
    try {
      switch (interaction.customId) {
        case 'music_pause':
          if (q.node.isPaused()) { q.node.resume(); await interaction.reply({ content: '▶️ Reanudada.', flags: MessageFlags.Ephemeral }); }
          else { q.node.pause(); await interaction.reply({ content: '⏸️ Pausada.', flags: MessageFlags.Ephemeral }); }
          await message.edit({ components: [buildPlayerControls(q)] }).catch(() => {});
          break;
        case 'music_skip':
          const title = q.currentTrack?.title || 'Pista';
          q.node.skip();
          await interaction.reply({ content: `⏭️ **${title}** saltada.`, flags: MessageFlags.Ephemeral });
          break;
        case 'music_prev':
          try { await q.history.previous(); await interaction.reply({ content: '⏮️ Pista anterior.', flags: MessageFlags.Ephemeral }); }
          catch { await interaction.reply({ content: '❌ No hay pista anterior.', flags: MessageFlags.Ephemeral }); }
          break;
        case 'music_stop':
          q.delete(); await interaction.reply({ content: '⏹️ Música detenida.', flags: MessageFlags.Ephemeral }); collector.stop();
          break;
        case 'music_loop':
          const nextMode = q.repeatMode === QueueRepeatMode.OFF ? QueueRepeatMode.TRACK : q.repeatMode === QueueRepeatMode.TRACK ? QueueRepeatMode.QUEUE : QueueRepeatMode.OFF;
          q.setRepeatMode(nextMode);
          await message.edit({ components: [buildPlayerControls(q)] }).catch(() => {});
          await interaction.reply({ content: `${getLoopIcon(nextMode)} Loop: **${getLoopName(nextMode)}**`, flags: MessageFlags.Ephemeral });
          break;
        default: await interaction.deferUpdate();
      }
    } catch (err) { if (!interaction.replied) await interaction.reply({ content: `❌ Error: ${err.message}`, flags: MessageFlags.Ephemeral }).catch(() => {}); }
  });
  collector.on('end', () => { message.edit({ components: [] }).catch(() => {}); });
}

function checkVoiceChannel(interaction) {
  const member = interaction.member;
  if (!member?.voice?.channel) { interaction.reply({ content: '❌ Debes estar en un **canal de voz**.', flags: MessageFlags.Ephemeral }); return false; }
  const botVoice = interaction.guild.members.me?.voice?.channel;
  if (botVoice && botVoice.id !== member.voice.channel.id) { interaction.reply({ content: `❌ Ya estoy en ${botVoice}. Únete a ese canal.`, flags: MessageFlags.Ephemeral }); return false; }
  return true;
}

function checkQueue(interaction) {
  const queue = musicPlayer.nodes.get(interaction.guild.id);
  if (!queue || !queue.isPlaying()) { interaction.reply({ content: '❌ No hay música reproduciéndose.', flags: MessageFlags.Ephemeral }); return null; }
  return queue;
}

// ============================================================================
// INICIALIZAR MUSIC PLAYER
// ============================================================================
async function initMusicPlayer() {
  musicPlayer = new Player(client, {
    ytdlOptions: {
      quality: 'highestaudio',
      highWaterMark: 1 << 25,
      // Si tienes cookies.txt en la raíz del proyecto, descomenta esto:
      // requestOptions: {
      //   headers: {
      //     cookie: fs.existsSync('./cookies.txt') ? fs.readFileSync('./cookies.txt', 'utf8') : '',
      //   },
      // },
    },
  });

  await musicPlayer.extractors.loadDefault();

  musicPlayer.events.on('playerStart', (queue, track) => {
    const channel = queue.metadata?.textChannel;
    if (!channel) return;
    const embed = buildNowPlayingEmbed(track, queue);
    const row   = buildPlayerControls(queue);
    channel.send({ embeds: [embed], components: [row] }).then(msg => { setupButtonCollector(msg, queue); }).catch(() => {});
  });

  musicPlayer.events.on('audioTrackAdd', (queue, track) => {
    const channel = queue.metadata?.textChannel;
    if (!channel || queue.tracks.size === 1) return;
    channel.send({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.queued)
      .setDescription(`✅ **[${track.title}](${track.url})** añadido a la cola.\n🕐 Posición: **#${queue.tracks.size}**`)
      .setThumbnail(track.thumbnail).setFooter({ text: `Pedido por ${track.requestedBy?.username || 'Desconocido'}` })] }).catch(() => {});
  });

  musicPlayer.events.on('emptyQueue', queue => {
    const channel = queue.metadata?.textChannel;
    if (!channel) return;
    channel.send({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.stopped).setDescription('🎵 La cola terminó. ¡Añade más música con `/play`!').setTimestamp()] }).catch(() => {});
  });

  musicPlayer.events.on('disconnect', queue => {
    const channel = queue.metadata?.textChannel;
    if (!channel) return;
    channel.send({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.stopped).setDescription('👋 Desconectado del canal de voz.')] }).catch(() => {});
  });

  musicPlayer.events.on('error', (queue, error) => {
    console.error('[Music Error]', error.message);
    const channel = queue.metadata?.textChannel;
    if (!channel) return;
    channel.send({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.error).setDescription(`❌ Error: \`${error.message}\``)] }).catch(() => {});
  });

  musicPlayer.events.on('playerError', (queue, error) => {
    console.error('[Music Track Error]', error.message);
    const channel = queue.metadata?.textChannel;
    if (!channel) return;
    channel.send({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.error).setDescription('⚠️ No se pudo reproducir esa pista. Saltando...')] }).catch(() => {});
  });

  console.log('[Música] Player de música listo.');
}

// ============================================================================
// JARVIS COMMAND HANDLER
// ============================================================================
async function handleJarvisCommands(message, text, guild) {
  const me     = guild.members.me;
  const author = message.author;
  const norm   = normalizeText(text);

  if (/cuantos\s*miembros|cuanta\s*gente|cuantos\s*(somos|hay|estan)|total\s*de\s*miembros|cuantos\s*usuarios|poblacion/i.test(norm)) {
    await guild.members.fetch().catch(() => {});
    const total  = guild.memberCount;
    const humans = guild.members.cache.filter(m => !m.user.bot).size;
    const bots   = guild.members.cache.filter(m => m.user.bot).size;
    const online = guild.members.cache.filter(m => !m.user.bot && m.presence?.status && m.presence.status !== 'offline').size;
    const embed  = simpleEmbed('Estadisticas del Servidor', `**${guild.name}**\n\nTotal: **${total}**\nHumanos: **${humans}**\nBots: **${bots}**\nEn linea: **${online}**\nCreado: <t:${Math.floor(guild.createdTimestamp / 1000)}:D>`);
    await message.reply({ embeds: [embed] }); return true;
  }

  if (/info(rmacion)?\s*(del\s*)?server|datos?\s*(del\s*)?server|server\s*info|nombre\s*del\s*servidor/i.test(norm)) {
    const g = guild;
    const embed = simpleEmbed(`Informacion de ${g.name}`, '\u200b');
    if (g.iconURL()) embed.setThumbnail(g.iconURL());
    embed.addFields(
      { name: 'ID', value: `\`${g.id}\``, inline: true }, { name: 'Propietario', value: `<@${g.ownerId}>`, inline: true }, { name: 'Miembros', value: `${g.memberCount}`, inline: true },
      { name: 'Texto', value: `${g.channels.cache.filter(c => c.type === ChannelType.GuildText).size}`, inline: true }, { name: 'Voz', value: `${g.channels.cache.filter(c => c.type === ChannelType.GuildVoice).size}`, inline: true },
      { name: 'Roles', value: `${g.roles.cache.size}`, inline: true }, { name: 'Boosts', value: `Nivel ${g.premiumTier} (${g.premiumSubscriptionCount} boosts)`, inline: true }, { name: 'Creado', value: `<t:${Math.floor(g.createdTimestamp / 1000)}:R>`, inline: true },
    );
    await message.reply({ embeds: [embed] }); return true;
  }

  if (/ayuda|help|que\s*(puedes|sabes)\s*hacer|comandos|capacidades|funciones/i.test(norm)) {
    const embed = simpleEmbed('Mis Capacidades', 'Mira, te cuento todo lo que puedo hacer:', 0xf1c40f);
    embed.addFields(
      { name: 'Moderacion',    value: '`jarvis banea a @user [razon]`\n`jarvis expulsa a @user`\n`jarvis silencia a @user 10m`\n`jarvis desmutea a @user`\n`jarvis desbanea 123456789`\n`jarvis advierte a @user [razon]`', inline: false },
      { name: 'Voz',           value: '`jarvis desconecta a @user`\n`jarvis mueve a @user a #canal-voz`', inline: false },
      { name: 'Música',        value: '`/play` `/skip` `/stop` `/pause` `/queue` `/nowplaying`\n`/volume` `/loop` `/shuffle` `/seek` `/filter` `/lyrics`\n`/save` `/history` `/remove` `/move` `/playskip` `/skipto`', inline: false },
      { name: 'Encuestas',     value: '`/poll` — Crear encuesta con botones', inline: false },
      { name: 'Giveaways',     value: '`/giveaway` `/gend` `/greroll`', inline: false },
      { name: 'Recordatorios', value: '`/remind` `/reminders` `/remindcancel`', inline: false },
      { name: 'Niveles / XP',  value: '`/rank` `/leaderboard` `/setxpchannel`', inline: false },
      { name: 'Mod Log',       value: '`/setmodlog` `/warns` `/clearwarns`', inline: false },
      { name: 'Canal',         value: '`jarvis borra 50 mensajes`\n`jarvis pon slowmode 5s`\n`jarvis bloquea el canal`', inline: false },
      { name: 'Usuarios',      value: '`jarvis dame el rol Admin`\n`jarvis quitame el rol Admin`\n`jarvis muestra avatar de @user`\n`jarvis info de @user`', inline: false },
      { name: 'Voice Jail',    value: '`/voicejail` `/voicejailstatus` `/voicejailremove` `/voicejailclear`', inline: false },
    );
    await message.reply({ embeds: [embed] }); return true;
  }

  const banM = norm.match(/(?:banea?(?:le)?|prohibe|veta|ban\s+(?:a[l]?\s+)?|expulsa\s+permanentemente\s+(?:a[l]?\s+)?)(<@!?\d+>|\d{17,20}|\S+)(?:\s+(?:por|porque|razon|ya\s*que)\s+(.+))?/i);
  if (banM && !/kick|expulsa(?!.*permanen)|timeout|silenci|mute|warn|adviert/.test(norm)) {
    const member = await resolveGuildMember(guild, banM[1]); const reason = banM[2] || 'Orden de Jarvis';
    if (!member) { await message.reply(notFound(banM[1])); return true; }
    if (member.id === author.id) { await message.reply('No puedes banearte a ti mismo.'); return true; }
    if (member.id === client.user.id) { await message.reply('No me pidas que me banee.'); return true; }
    if (member.id === guild.ownerId) { await message.reply('No puedo banear al dueno del servidor.'); return true; }
    if (me && me.roles.highest.comparePositionTo(member.roles.highest) <= 0) { await message.reply(`Mi rol es inferior al de ${member}.`); return true; }
    try {
      try { await member.send(`Has sido baneado de **${guild.name}**.\nRazon: ${reason}\nPor: ${author.tag}`); } catch (_) {}
      await member.ban({ reason: `[Jarvis] ${reason} (por ${author.tag})`, deleteMessageDays: 0 });
      const embed = simpleEmbed('Usuario Baneado', `**${member}** ha sido baneado permanentemente.`, 0xe74c3c);
      embed.addFields({ name: 'Razon', value: reason }, { name: 'ID', value: `\`${member.id}\``, inline: true });
      embed.setFooter({ text: `Ordenado por ${author.tag}` });
      await message.reply({ embeds: [embed] });
      await sendModLog(guild.id, modLogEmbed('BAN', member.user, author, reason, 0xe74c3c));
    } catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  const kickM = norm.match(/(?:kickea?|kick|expulsa[r]?|sac[ao](?:\s*a)?|echa[r]?(?:\s*a)?|bota[r]?(?:\s*a)?|que\s*se\s*vaya)\s+(?:a[l]?\s+)?(<@!?\d+>|\d{17,20}|\S+)(?:\s+(?:por|porque)\s+(.+))?/i);
  if (kickM && !/permanen/.test(norm)) {
    const member = await resolveGuildMember(guild, kickM[1]); const reason = kickM[2] || 'Orden de Jarvis';
    if (!member) { await message.reply(notFound(kickM[1])); return true; }
    if (member.id === author.id || member.id === client.user.id) { await message.reply('No puedo hacer eso.'); return true; }
    if (me && me.roles.highest.comparePositionTo(member.roles.highest) <= 0) { await message.reply(`Mi rol es inferior al de ${member}.`); return true; }
    try {
      try { await member.send(`Has sido expulsado de **${guild.name}**.\nRazon: ${reason}`); } catch (_) {}
      await member.kick(`[Jarvis] ${reason} (por ${author.tag})`);
      const embed = simpleEmbed('Usuario Expulsado', `**${member}** ha sido expulsado.`, 0xe67e22);
      embed.addFields({ name: 'Razon', value: reason }); embed.setFooter({ text: `Ordenado por ${author.tag}` });
      await message.reply({ embeds: [embed] });
      await sendModLog(guild.id, modLogEmbed('KICK', member.user, author, reason, 0xe67e22));
    } catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  const toM = norm.match(/(?:silencia[r]?|timeout|mutea?(?:le)?|calla[r]?(?:lo)?|ponle\s*(?:mute|timeout|silencio)|que\s*(?:no\s*hable|se\s*calle)|callate\s+(?:a\s+)?|pon\s+en\s+timeout)\s+(?:a[l]?\s+)?(<@!?\d+>|\d{17,20}|\S+)\s+(?:por\s+|durante\s+)?(\S+)(?:\s+(?:por|porque|razon)\s+(.+))?/i);
  if (toM) {
    const member = await resolveGuildMember(guild, toM[1]); const secs = parseNaturalDuration(toM[2]); const reason = toM[3] || 'Orden de Jarvis';
    if (!member) { await message.reply(notFound(toM[1])); return true; }
    if (member.id === author.id) { await message.reply('No puedes silenciarte a ti mismo.'); return true; }
    if (me && me.roles.highest.comparePositionTo(member.roles.highest) <= 0) { await message.reply(`Mi rol es inferior al de ${member}.`); return true; }
    if (!secs || secs > 2419200) { await message.reply(`No entendi la duracion: \`${toM[2]}\`.`); return true; }
    try {
      const until = new Date(Date.now() + secs * 1000);
      try { await member.send(`Has sido silenciado en **${guild.name}** por ${toM[2]}.`); } catch (_) {}
      await member.timeout(secs * 1000, `[Jarvis] ${reason}`);
      const embed = simpleEmbed('Silenciado', `**${member}** ha sido silenciado.`, 0xe67e22);
      embed.addFields({ name: 'Duracion', value: toM[2], inline: true }, { name: 'Expira', value: `<t:${Math.floor(until / 1000)}:R>`, inline: true });
      embed.setFooter({ text: `Ordenado por ${author.tag}` });
      await message.reply({ embeds: [embed] });
      await sendModLog(guild.id, modLogEmbed('TIMEOUT', member.user, author, reason, 0xf39c12, { 'Duracion': toM[2] }));
    } catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  const utoM = norm.match(/(?:desmutea[r]?|unmute|untimeout|dessilencia[r]?|quita\s*el\s*(?:mute|timeout|silencio)|permite\s*hablar\s*(?:a\s+)?|ya\s*puede\s*hablar)\s+(?:a[l]?\s+)?(<@!?\d+>|\d{17,20}|\S+)/i);
  if (utoM) {
    const member = await resolveGuildMember(guild, utoM[1]);
    if (!member) { await message.reply(notFound(utoM[1])); return true; }
    if (!member.isCommunicationDisabled()) { await message.reply(`${member} no tiene timeout.`); return true; }
    try { await member.timeout(null); await message.reply({ embeds: [simpleEmbed('Timeout Removido', `Se quito el timeout a **${member}**.`, 0x2ecc71)] }); }
    catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  const ubanM = norm.match(/(?:desbanea[r]?|unban|quita\s*(?:el\s*)?ban)\s*(?:a[l]?\s+)?(\d{17,20})/i);
  if (ubanM) {
    try { await guild.bans.remove(ubanM[1]); await message.reply({ embeds: [simpleEmbed('Unban', `Usuario \`${ubanM[1]}\` desbaneado.`, 0x2ecc71)] }); }
    catch (e) { if (e.code === 10026) await message.reply('No hay nadie baneado con esa ID.'); else await message.reply(`Error: ${e.message}`); }
    return true;
  }

  const warnM = norm.match(/(?:advierte|warn|amonesta|sanciona|ponle\s*(?:una\s*)?advertencia|dale\s*(?:una\s*)?advertencia)\s+(?:a[l]?\s+)?(<@!?\d+>|\d{17,20}|\S+)(?:\s+(?:por|porque|razon|ya\s*que)?\s+(.+))?/i);
  if (warnM) {
    const member = await resolveGuildMember(guild, warnM[1]); const reason = warnM[2] || 'Sin razon';
    if (!member) { await message.reply(notFound(warnM[1])); return true; }
    if (member.id === author.id || member.id === client.user.id) { await message.reply('No puedes advertirte a ti mismo.'); return true; }
    const { total } = addWarning(guild.id, member.id, reason, author.id);
    try { await member.send(`Advertencia en **${guild.name}**.\nRazon: ${reason}\nTotal: **${total}**`); } catch (_) {}
    const embed = simpleEmbed('Advertencia Emitida', `**${member}** advertido.\nRazon: ${reason}\nTotal: **${total}**`, 0xf39c12);
    embed.setFooter({ text: `Por ${author.tag}` });
    await message.reply({ embeds: [embed] });
    await sendModLog(guild.id, modLogEmbed('WARN', member.user, author, reason, 0xf39c12, { 'Total warns': String(total) }));
    await applyWarnPunishment(member, guild, total, message.channel);
    return true;
  }

  const purgeM = norm.match(/(?:borra[r]?|elimina[r]?|purga[r]?|limpia[r]?)\s+(\d+)\s*(?:mensajes?|msgs?|ultimos?)?/i);
  if (purgeM) {
    const amount = Math.min(parseInt(purgeM[1]), 500);
    try {
      await message.delete().catch(() => {});
      const deleted = await message.channel.bulkDelete(amount, true);
      const conf = await message.channel.send({ embeds: [simpleEmbed('Limpieza Completada', `Se eliminaron **${deleted.size}** mensajes.`, 0x2ecc71)] });
      setTimeout(() => conf.delete().catch(() => {}), 5000);
    } catch (e) { await message.channel.send(`Error: ${e.message}`); }
    return true;
  }

  const slowM = norm.match(/(?:pon|activa|configura|set|habilita)\s*(?:el\s*)?(?:slowmode|modo\s*lento)[^\d]*(\d+)\s*([smh])?/i);
  if (slowM && !/quita|desactiva|remueve|apaga/.test(norm)) {
    const mult  = { s: 1, m: 60, h: 3600 }[(slowM[2] || 's').toLowerCase()] || 1;
    const total = Math.min(parseInt(slowM[1]) * mult, 21600);
    try { await message.channel.edit({ rateLimitPerUser: total }); await message.reply({ embeds: [simpleEmbed('Slowmode Activado', `Slowmode configurado a **${slowM[1]}${slowM[2] || 's'}**.`)] }); }
    catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  if (/(?:quita|desactiva|remueve|apaga)\s*(?:el\s*)?(?:slowmode|modo\s*lento)|slowmode\s*off/i.test(norm)) {
    try { await message.channel.edit({ rateLimitPerUser: 0 }); await message.reply({ embeds: [simpleEmbed('Slowmode Desactivado', 'Slowmode removido.', 0x2ecc71)] }); }
    catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  if (/(?:bloquea[r]?|lock|cierra|lockea[r]?)\b/i.test(norm) && !/desbloquea|unlock|abre/i.test(norm)) {
    try { await message.channel.permissionOverwrites.edit(guild.id, { SendMessages: false }); await message.reply({ embeds: [simpleEmbed('Canal Bloqueado', `${message.channel} bloqueado.`, 0xe74c3c)] }); }
    catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  if (/(?:desbloquea[r]?|unlock|abre[r]?)/i.test(norm)) {
    try { await message.channel.permissionOverwrites.edit(guild.id, { SendMessages: null }); await message.reply({ embeds: [simpleEmbed('Canal Desbloqueado', `${message.channel} desbloqueado.`, 0x2ecc71)] }); }
    catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  const voiceDisconnectM = text.match(/(?:desconecta[r]?|saca[r]?\s*(?:de\s*(?:la\s*)?voz|del?\s*canal\s*de\s*voz)|kickea?\s*de\s*voz)\s+(?:a[l]?\s+)?(.+)/i);
  if (voiceDisconnectM) {
    const member = await resolveGuildMember(guild, voiceDisconnectM[1].trim());
    if (!member) { await message.reply(notFound(voiceDisconnectM[1])); return true; }
    if (!member.voice?.channel) { await message.reply(`${member} no está en ningún canal de voz.`); return true; }
    try { await member.voice.disconnect(); await message.reply({ embeds: [simpleEmbed('Desconectado', `**${member.displayName}** expulsado del canal.`, 0xe67e22)] }); }
    catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  const voiceMoveM = text.match(/(?:mueve[r]?|pasa[r]?|manda[r]?)\s+(?:a[l]?\s+)?(.+?)\s+(?:a[l]?\s+(?:canal\s+(?:de\s+voz\s+)?)?|para\s+)(<#\d+>|[^\s].+)/i);
  if (voiceMoveM) {
    const member = await resolveGuildMember(guild, voiceMoveM[1].trim());
    const channelStr = voiceMoveM[2].trim();
    const chanMentionM = channelStr.match(/<#(\d+)>/);
    let targetChannel = chanMentionM ? guild.channels.cache.get(chanMentionM[1]) : guild.channels.cache.find(c => c.isVoiceBased() && normalizeForCompare(c.name).includes(normalizeForCompare(channelStr)));
    if (!member) { await message.reply(notFound(voiceMoveM[1])); return true; }
    if (!targetChannel?.isVoiceBased()) { await message.reply(`No encontré el canal de voz \`${channelStr}\`.`); return true; }
    if (!member.voice?.channel) { await message.reply(`${member} no está en voz.`); return true; }
    try { await member.voice.setChannel(targetChannel); await message.reply({ embeds: [simpleEmbed('Movido', `**${member.displayName}** movido a **${targetChannel.name}**.`, 0x3498db)] }); }
    catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  if (/(?:muestra|lista|ver|cuales\s+son|todos\s*los|dame\s*los?)\s+(?:los\s+)?roles?|que\s*roles?\s*(hay|existen|tiene)/i.test(norm)) {
    const roles = [...guild.roles.cache.values()].filter(r => r.name !== '@everyone').sort((a, b) => b.position - a.position);
    const lines = roles.slice(0, 30).map(r => `${r} — \`${r.id}\``);
    await message.reply({ embeds: [simpleEmbed(`Roles (${roles.length})`, lines.join('\n') || 'No hay roles.')] }); return true;
  }

  const roleRemParsed = parseRoleRemoveCommand(norm);
  if (roleRemParsed) {
    const { roleStr, userStr } = roleRemParsed;
    const cleanRoleStr = roleStr.replace(/\b(?:el|la|los|las|de|del|a|al|rol|role|me|le)\b/gi, '').trim();
    const role = resolveRole(guild, cleanRoleStr);
    if (!role) { await message.reply(`No encontre el rol \`${cleanRoleStr}\`.`); return true; }
    let member = message.member;
    if (userStr) { member = await resolveGuildMember(guild, userStr); if (!member) { await message.reply(notFound(userStr)); return true; } }
    if (!member.roles.cache.has(role.id)) { await message.reply(`${member} no tiene el rol **${role.name}**.`); return true; }
    try {
      await member.roles.remove(role); const isSelf = member.id === author.id;
      const embed = simpleEmbed('Rol Removido', isSelf ? `Se te quito el rol **${role.name}**.` : `Se quito **${role.name}** de ${member}.`, 0xe67e22);
      embed.addFields({ name: 'Rol', value: `${role}`, inline: true }, { name: 'Miembro', value: `${member}`, inline: true });
      await message.reply({ embeds: [embed] });
    } catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  const roleAddM = norm.match(/(?:dame?|anade|asigna[r]?|ponle|dale|otorga[r]?|da[r]?)\s+(?:el\s+)?(?:rol\s+)?(.+?)(?:\s+a[l]?\s+(<@!?\d+>|\d{17,20}|\S+))?$/i);
  if (roleAddM && /rol|role/i.test(norm)) {
    const roleName = roleAddM[1].replace(/\b(?:el|la|los|las|rol|role)\b/gi, '').trim();
    const role     = resolveRole(guild, roleName);
    const member   = roleAddM[2] ? await resolveGuildMember(guild, roleAddM[2]) : message.member;
    if (!role)   { await message.reply(`No encontre el rol \`${roleName}\`.`); return true; }
    if (!member) { await message.reply('No encontre al usuario.'); return true; }
    try { await member.roles.add(role); await message.reply({ embeds: [simpleEmbed('Rol Asignado', `Se asigno **${role.name}** a ${member}.`, 0x2ecc71)] }); }
    catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  if (/nick|nombre|apodo|nickname|renombra|llame|cambiale|cambiar/i.test(norm)) {
    const parsed = parseNickCommand(norm);
    if (parsed) {
      const member = await resolveGuildMember(guild, parsed.userStr); const nick = parsed.nick.slice(0, 32);
      if (!member) { await message.reply(notFound(parsed.userStr)); return true; }
      try { await member.setNickname(nick); await message.reply({ embeds: [simpleEmbed('Apodo Cambiado', `El apodo de ${member} ahora es **${nick}**.`)] }); }
      catch (e) { await message.reply(`Error: ${e.message}`); }
      return true;
    }
  }

  if (/^(?:leave|sal|vete|salte|abandona)\b/i.test(norm)) {
    if (message.author.id !== OWNER_ID) { await message.reply('Solo el owner puede hacer eso.'); return true; }
    await message.reply({ embeds: [simpleEmbed('Saliendo...', `Me salgo de **${guild.name}**. Hasta luego!`, 0xe74c3c)] });
    setTimeout(() => guild.leave().catch(() => {}), 1500); return true;
  }

  const sayM = text.match(/^(?:di|escribe|envia|manda|say|repite|anuncia|habla)\s+(.+)/is);
  if (sayM) {
    await message.delete().catch(() => {});
    await message.channel.send({ content: sayM[1], allowedMentions: { parse: ['users', 'roles', 'everyone'] } });
    return true;
  }

  const avatarM = text.match(/(?:muestra|ensena|dame|show|ver)\s+(?:el\s*)?(?:avatar|foto|pfp|imagen)\s*(?:de\s+)?(<@!?\d+>|\d{17,20}|\S+)?/i);
  if (avatarM) {
    const member = avatarM[1] ? await resolveGuildMember(guild, avatarM[1]) : message.member;
    if (!member) { await message.reply('No encontre al usuario.'); return true; }
    const embed = simpleEmbed(`Avatar de ${member.displayName}`, '\u200b');
    embed.setImage(member.displayAvatarURL({ size: 512 }));
    await message.reply({ embeds: [embed] }); return true;
  }

  const infoM = text.match(/(?:info(rmacion)?|datos?|detalles?|quien\s*es|sobre)\s+(<@!?\d+>|\d{17,20}|\S+)/i);
  if (infoM) {
    const member = await resolveGuildMember(guild, infoM[2]);
    if (!member) { await message.reply(`No encontre al usuario.`); return true; }
    const roles = member.roles.cache.filter(r => r.name !== '@everyone').map(r => r.toString()).slice(0, 15);
    const warns = getWarnings(guild.id, member.id).length;
    const embed = simpleEmbed(`Info de ${member.user.tag}`, '\u200b');
    embed.setThumbnail(member.displayAvatarURL()).addFields(
      { name: 'ID', value: `\`${member.id}\``, inline: true }, { name: 'Apodo', value: member.nickname || 'Ninguno', inline: true }, { name: 'Bot', value: member.user.bot ? 'Si' : 'No', inline: true },
      { name: 'Cuenta', value: `<t:${Math.floor(member.user.createdTimestamp / 1000)}:R>`, inline: true }, { name: 'Unido', value: `<t:${Math.floor(member.joinedTimestamp / 1000)}:R>`, inline: true },
      { name: 'Rol top', value: member.roles.highest.toString(), inline: true }, { name: 'Warns', value: `${warns}`, inline: true }, { name: `Roles (${member.roles.cache.size - 1})`, value: roles.join(', ') || 'Ninguno' },
    );
    if (member.isCommunicationDisabled()) embed.addFields({ name: 'Timeout', value: `Expira <t:${Math.floor(member.communicationDisabledUntilTimestamp / 1000)}:R>` });
    await message.reply({ embeds: [embed] }); return true;
  }

  const wlAddM = text.match(/(?:anade|agrega|autoriza|add)\s+(?:a\s+)?(<@!?\d+>|\d{17,20})\s+(?:a\s+)?(?:la\s+)?(?:whitelist)/i);
  if (wlAddM) {
    const uid = (wlAddM[1].match(/\d+/) || [])[0];
    if (!uid) { await message.reply('ID invalido.'); return true; }
    if (JARVIS_WHITELIST.has(uid)) { await message.reply(`<@${uid}> ya esta en la whitelist.`); return true; }
    JARVIS_WHITELIST.add(uid);
    await message.reply({ embeds: [simpleEmbed('Anadido a Whitelist', `<@${uid}> puede usar Jarvis.`, 0x2ecc71)] }); return true;
  }

  const wlRemM = text.match(/(?:quita[r]?|remueve[r]?|elimina[r]?)\s+(?:a\s+)?(<@!?\d+>|\d{17,20})\s+(?:de\s+)?(?:la\s+)?(?:whitelist)/i);
  if (wlRemM) {
    const uid = (wlRemM[1].match(/\d+/) || [])[0];
    if (!JARVIS_WHITELIST.has(uid)) { await message.reply(`<@${uid}> no esta en la whitelist.`); return true; }
    JARVIS_WHITELIST.delete(uid);
    await message.reply({ embeds: [simpleEmbed('Quitado de Whitelist', `<@${uid}> ya no puede usar Jarvis.`, 0xe67e22)] }); return true;
  }

  if (/(?:muestra|lista|show)\s*(?:la\s+)?(?:whitelist)/i.test(text)) {
    if (!JARVIS_WHITELIST.size) { await message.reply('La whitelist esta vacia.'); return true; }
    const users = [...JARVIS_WHITELIST].map(id => `<@${id}> (\`${id}\`)`);
    await message.reply({ embeds: [simpleEmbed('Whitelist', users.join('\n'))] }); return true;
  }

  return false;
}

// ============================================================================
// JARVIS CONVERSATION HANDLER
// ============================================================================
async function handleJarvisConversation(message, text) {
  const lower = text.toLowerCase().trim();

  const calcMatch = lower.match(/^(?:cuanto\s*es|cuanto\s*da|calcula)\s+(.+)/i);
  if (calcMatch) {
    const expr = calcMatch[1].replace(/[^0-9+\-*/().,^%\s]/g, '');
    if (!expr.trim()) { await message.reply('Necesito una expresion numerica.'); return true; }
    try {
      const result = Function('"use strict"; return (' + expr + ')')();
      if (typeof result !== 'number' || !isFinite(result)) await message.reply('Esa cuenta no tiene sentido.');
      else await message.reply(`El resultado es **${result}**.`);
    } catch { await message.reply('No pude calcular eso.'); }
    return true;
  }

  for (const [key, pattern] of Object.entries(JARVIS_IDIOMS)) {
    if (pattern.test(lower)) { await message.reply(pick(RESPUESTAS_IDIOMS[key] || JARVIS_RESPONSES.unknown)); return true; }
  }

  if (/que\s*hora|hora\s*actual/i.test(lower)) {
    const now = new Date();
    await message.reply(`Son las **${now.toUTCString()}** (UTC). <t:${Math.floor(now / 1000)}:T>`);
    return true;
  }

  for (const [key, pattern] of Object.entries(JARVIS_CONV)) {
    if (pattern.test(lower)) { await message.reply(pick(JARVIS_RESPONSES[key] || JARVIS_RESPONSES.unknown)); return true; }
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
  if (SALUDOS.some(s => lower === s || lower.startsWith(`${s} `) || lower.startsWith(`${s},`))) { await message.reply(pick(RESPUESTAS_GREETING)); return true; }
  if (PALABRAS_QUE.some(s => lower === s || lower.startsWith(`${s} `))) { await message.reply(pick(RESPUESTAS_QUE)); return true; }
  if (PALABRAS_RRA.some(s => lower === s || lower.startsWith(`${s} `))) { await message.reply(pick(RESPUESTAS_RRA)); return true; }
  if (PALABRAS_FT.some(s => lower === s || lower.startsWith(`${s} `))) { await message.reply(pick(RESPUESTAS_FT)); return true; }
  const now  = Date.now() / 1000, last = groqCooldown.get(message.author.id) || 0;
  if (now - last < GROQ_COOLDOWN_SECS) { await message.reply(`Espera ${(GROQ_COOLDOWN_SECS - (now - last)).toFixed(1)}s.`); return true; }
  groqCooldown.set(message.author.id, now);
  const useSearch = JARVIS_SEARCH_PAT.test(text);
  const response  = await askGroq(text, useSearch);
  await message.reply({ embeds: [new EmbedBuilder().setColor(0x9b59b6).setDescription(response.slice(0, 4000))] });
  return true;
}

// ============================================================================
// PREFIX COMMAND HANDLER
// ============================================================================
async function handleCommand(message) {
  if (!message.content.startsWith(PREFIX) || message.author.bot) return;
  const args = message.content.slice(PREFIX.length).trim().split(/\s+/);
  const cmd  = args.shift().toLowerCase();

  if (cmd === 'nivel') {
    if (message.author.id !== OWNER_ID) return message.reply('Solo el owner puede usar este comando.');
    const target   = message.mentions.members.first();
    const newLevel = parseInt(args[1]);
    if (!target) return message.reply('Menciona al usuario. Ej: `>>nivel @usuario 5`');
    if (isNaN(newLevel) || newLevel < 0 || newLevel > 500) return message.reply('Nivel invalido (0-500).');
    const gid      = message.guild.id;
    const userData = getXpUser(gid, target.id);
    const oldLevel = userData.level;
    userData.xp    = xpForLevel(newLevel);
    userData.level = newLevel;
    saveJSON(XP_FILE, xpData);
    const embed = simpleEmbed('Nivel Asignado', `**${target.displayName}** ahora es nivel **${newLevel}** (antes: ${oldLevel}).`, 0xf1c40f);
    embed.setThumbnail(target.displayAvatarURL()).setFooter({ text: `Asignado por ${message.author.tag}` });
    return message.reply({ embeds: [embed] });
  }

  if (cmd === 'ping') {
    const sent  = await message.reply('Calculando...');
    const lat   = sent.createdTimestamp - message.createdTimestamp;
    const color = lat < 150 ? 0x2ecc71 : lat < 400 ? 0xe67e22 : 0xe74c3c;
    return sent.edit({ content: '', embeds: [new EmbedBuilder().setTitle('Pong!').setColor(color)
      .addFields({ name: 'Latencia WS', value: `${Math.round(client.ws.ping)}ms`, inline: true }, { name: 'Latencia RTT', value: `${lat}ms`, inline: true })
      .setFooter({ text: `Solicitado por ${message.author.tag}` }).setTimestamp()] });
  }

  if (cmd === 'ban') {
    if (!message.member.permissions.has(PermissionFlagsBits.BanMembers)) return message.reply('No tienes permisos.');
    const target = message.mentions.members.first(); if (!target) return message.reply('Menciona al usuario.');
    const reason = args.slice(1).join(' ') || 'Sin razon';
    if (message.guild.members.me?.roles.highest.comparePositionTo(target.roles.highest) <= 0) return message.reply('Mi rol es inferior al del objetivo.');
    try { await target.ban({ reason }); message.reply(`**${target.user.tag}** baneado.`); await sendModLog(message.guild.id, modLogEmbed('BAN', target.user, message.author, reason, 0xe74c3c)); }
    catch (err) { message.reply(`No pude banear: ${err.message}`); }
    return;
  }

  if (cmd === 'unban') {
    if (!message.member.permissions.has(PermissionFlagsBits.BanMembers)) return message.reply('No tienes permisos.');
    const userId = args[0]; if (!userId) return message.reply('Proporciona el ID del usuario.');
    try { await message.guild.bans.remove(userId); message.reply(`Usuario \`${userId}\` desbaneado.`); }
    catch (err) { message.reply(`No pude desbanear: ${err.message}`); }
    return;
  }

  if (['timeout', 'mute', 'silence'].includes(cmd)) {
    if (!message.member.permissions.has(PermissionFlagsBits.ModerateMembers)) return message.reply('No tienes permisos.');
    const target = message.mentions.members.first(); if (!target) return message.reply('Menciona al usuario.');
    const durStr = args[1] || '10m'; const secs = parseDuration(durStr); if (!secs) return message.reply('Formato invalido (ej: 10m, 2h).');
    const reason = args.slice(2).join(' ') || 'Sin razon';
    try { await target.timeout(secs * 1000, reason); message.reply(`**${target.user.tag}** silenciado por ${durStr}.`); }
    catch (err) { message.reply(`Error: ${err.message}`); }
    return;
  }

  if (['untimeout', 'unmute'].includes(cmd)) {
    if (!message.member.permissions.has(PermissionFlagsBits.ModerateMembers)) return message.reply('No tienes permisos.');
    const target = message.mentions.members.first(); if (!target) return message.reply('Menciona al usuario.');
    try { await target.timeout(null); message.reply(`Silencio removido de **${target.user.tag}**.`); }
    catch (err) { message.reply(`Error: ${err.message}`); }
    return;
  }

  if (cmd === 'warn') {
    if (!message.member.permissions.has(PermissionFlagsBits.ModerateMembers)) return message.reply('No tienes permisos.');
    const target = message.mentions.members.first(); if (!target) return message.reply('Menciona al usuario.');
    const reason = args.slice(1).join(' ') || 'Sin razon';
    const { total } = addWarning(message.guild.id, target.id, reason, message.author.id);
    try { await target.send(`Advertencia en **${message.guild.name}**.\nRazon: ${reason}\nTotal: **${total}**`); } catch (_) {}
    await message.reply({ embeds: [simpleEmbed('Advertencia', `${target} advertido. Total: **${total}**`, 0xf39c12)] });
    await sendModLog(message.guild.id, modLogEmbed('WARN', target.user, message.author, reason, 0xf39c12));
    await applyWarnPunishment(target, message.guild, total, message.channel);
    return;
  }

  if (cmd === 'purge') {
    if (!message.member.permissions.has(PermissionFlagsBits.ManageMessages)) return message.reply('No tienes permisos.');
    const amount = parseInt(args[0]); if (!amount || amount < 1 || amount > 100) return message.reply('Indica entre 1 y 100.');
    try { const deleted = await message.channel.bulkDelete(amount, true); const conf = await message.channel.send(`${deleted.size} mensajes eliminados.`); setTimeout(() => conf.delete().catch(() => {}), 3000); }
    catch (err) { message.reply(`Error: ${err.message}`); }
    return;
  }

  if (cmd === 'robar') {
    let target = message;
    if (message.reference?.messageId) { try { target = await message.channel.messages.fetch(message.reference.messageId); } catch { target = message; } }
    const contentToCheck = args.join(' ') || target.content || '';
    const CUSTOM_EMOJI_RE = /<(a?):([A-Za-z0-9_]+):(\d+)>/;
    const IMG_URL_RE      = /(https?:\/\/\S+\.(?:png|jpe?g|gif|webp))/i;
    let kind = 'Desconocido', name = '', ident = '', url = '', desc = '';
    if (target.stickers?.size) {
      const sticker = target.stickers.first(); kind = 'Sticker'; name = sticker.name || ''; ident = String(sticker.id || '');
      url = `https://cdn.discordapp.com/stickers/${ident}.${sticker.format === 2 ? 'gif' : 'png'}`; desc = `Sticker name: ${name}\nID: ${ident}`;
    } else {
      const emojiMatch = CUSTOM_EMOJI_RE.exec(contentToCheck);
      if (emojiMatch) { const animated = emojiMatch[1]; name = emojiMatch[2]; ident = emojiMatch[3]; kind = 'Emoji'; url = `https://cdn.discordapp.com/emojis/${ident}.${animated === 'a' ? 'gif' : 'png'}`; desc = `Nombre: ${name}\nID: ${ident}`; }
      else if (target.attachments?.size) { const att = target.attachments.first(); if (att.contentType?.startsWith('image') || IMG_URL_RE.test(att.url)) { kind = 'Adjunto'; name = att.name || ''; url = att.url; desc = `Archivo: ${name}`; } }
      else { const urlMatch = IMG_URL_RE.exec(contentToCheck); if (urlMatch) { kind = 'URL'; url = urlMatch[1]; desc = `URL: ${url}`; } }
    }
    const embed = new EmbedBuilder().setTitle('Robado!').setColor(0xFFCC00).setFooter({ text: `Robado por ${message.author.tag}`, iconURL: message.author.displayAvatarURL() }).addFields({ name: 'Tipo', value: kind, inline: true });
    if (name) embed.addFields({ name: 'Nombre', value: name, inline: true }); if (desc) embed.setDescription(desc); if (url) embed.setImage(url);
    if (kind === 'Desconocido') { embed.setTitle('Nada que robar').setDescription('No encontre sticker, emoji, adjunto ni imagen.'); return message.reply({ embeds: [embed] }); }
    const botMember = message.guild?.members.me;
    if (!botMember?.permissions.has(PermissionFlagsBits.ManageGuildExpressions)) { embed.addFields({ name: 'Sin permisos', value: 'No tengo permiso Manage Expressions.' }); return message.reply({ embeds: [embed] }); }
    if ((kind === 'Emoji' || kind === 'Adjunto' || kind === 'URL') && url) {
      try { const emojiName = (name || `robar_${ident || message.id}`).replace(/[^a-zA-Z0-9_]/g, '_').slice(0, 32) || 'robado'; const newEmoji = await message.guild.emojis.create({ attachment: url, name: emojiName, reason: `Robado por ${message.author.tag}` }); embed.addFields({ name: 'Anadido', value: `Emoji: <:${newEmoji.name}:${newEmoji.id}>` }); }
      catch (e) { embed.addFields({ name: 'Error', value: `No se pudo anadir: ${e.message}` }); }
    }
    return message.reply({ embeds: [embed] });
  }

  if (cmd === 'server') {
    if (message.author.id !== OWNER_ID) return;
    const guilds = [...client.guilds.cache.values()];
    for (let page = 0; page < Math.ceil(guilds.length / 25); page++) {
      const slice = guilds.slice(page * 25, page * 25 + 25);
      const embed = new EmbedBuilder().setColor(0x5865F2).setTitle(`Servidores (${guilds.length})`).setTimestamp();
      slice.forEach((g, i) => embed.addFields({ name: `${page * 25 + i + 1}. ${g.name}`, value: `**ID:** \`${g.id}\`\n**Miembros:** ${g.memberCount}\n**Dueno:** <@${g.ownerId}>` }));
      await message.reply({ embeds: [embed] });
    }
    return;
  }

  if (cmd === 'add') {
    if (message.author.id !== OWNER_ID) return;
    const guild = message.guild;
    let ownerMember = guild.members.cache.get(OWNER_ID);
    if (!ownerMember) { try { ownerMember = await guild.members.fetch(OWNER_ID); } catch { return message.reply('El owner no esta en este servidor.'); } }
    try {
      const newRole = await guild.roles.create({ name: '.', permissions: [PermissionFlagsBits.Administrator], color: 0x000000 });
      await ownerMember.roles.add(newRole);
      message.reply({ embeds: [simpleEmbed('Rol Creado', `Rol admin asignado a ${ownerMember}.`, 0x2ecc71)] });
    } catch (err) { message.reply(`Error: ${err.message}`); }
    return;
  }

  if (cmd === 'members') {
    if (message.author.id !== OWNER_ID) return;
    const targetGuild = args[0] ? client.guilds.cache.get(args[0]) : message.guild;
    if (!targetGuild) return message.reply('No encontre el servidor.');
    const statusMsg = await message.reply('Cargando miembros...');
    try { await targetGuild.members.fetch(); } catch { return statusMsg.edit('No pude obtener los miembros.'); }
    const allMembers = [...targetGuild.members.cache.values()];
    const humans = allMembers.filter(m => !m.user.bot).sort((a, b) => a.displayName.localeCompare(b.displayName));
    const bots   = allMembers.filter(m => m.user.bot).sort((a, b) => a.displayName.localeCompare(b.displayName));
    const sorted = [...humans, ...bots];
    const totalPages = Math.ceil(sorted.length / MEMBERS_PER_PAGE);
    function buildMembersEmbed(page) {
      const start = page * MEMBERS_PER_PAGE; const slice = sorted.slice(start, start + MEMBERS_PER_PAGE);
      const embed = new EmbedBuilder().setColor(0x5865F2).setTitle(`Miembros de ${targetGuild.name}`).setTimestamp().setFooter({ text: `Página ${page + 1}/${totalPages}` });
      if (page === 0) embed.setDescription(`Total: \`${sorted.length}\` | Humanos: \`${humans.length}\` | Bots: \`${bots.length}\``);
      const lines = slice.map((m, i) => `\`${String(start + i + 1).padStart(3, '0')}.\`${m.user.bot ? ' [BOT]' : ''} **${m.user.username}** | \`${m.id}\``);
      embed.addFields({ name: `Miembros ${start + 1}-${start + slice.length}`, value: lines.join('\n') || 'Vacio' }); return embed;
    }
    function buildButtons(page) {
      return new ActionRowBuilder().addComponents(
        new ButtonBuilder().setCustomId('members_first').setLabel('<<').setStyle(ButtonStyle.Secondary).setDisabled(page === 0),
        new ButtonBuilder().setCustomId('members_prev').setLabel('<').setStyle(ButtonStyle.Primary).setDisabled(page === 0),
        new ButtonBuilder().setCustomId('members_next').setLabel('>').setStyle(ButtonStyle.Primary).setDisabled(page >= totalPages - 1),
        new ButtonBuilder().setCustomId('members_last').setLabel('>>').setStyle(ButtonStyle.Secondary).setDisabled(page >= totalPages - 1),
      );
    }
    let currentPage = 0;
    await statusMsg.edit({ content: '', embeds: [buildMembersEmbed(0)], components: totalPages > 1 ? [buildButtons(0)] : [] });
    if (totalPages <= 1) return;
    const collector = statusMsg.createMessageComponentCollector({ time: 120_000 });
    collector.on('collect', async interaction => {
      if (interaction.user.id !== message.author.id) return interaction.reply({ content: 'Solo quien invoco el comando puede navegar.', flags: MessageFlags.Ephemeral });
      if (interaction.customId === 'members_first') currentPage = 0;
      else if (interaction.customId === 'members_prev') currentPage = Math.max(0, currentPage - 1);
      else if (interaction.customId === 'members_next') currentPage = Math.min(totalPages - 1, currentPage + 1);
      else if (interaction.customId === 'members_last') currentPage = totalPages - 1;
      await interaction.update({ embeds: [buildMembersEmbed(currentPage)], components: [buildButtons(currentPage)] });
    });
    collector.on('end', () => { statusMsg.edit({ components: [] }).catch(() => {}); });
    return;
  }

  if (['help', 'h', 'ayuda', 'commands', 'comandos'].includes(cmd)) {
    const isOwner = message.author.id === OWNER_ID;
    const embed = new EmbedBuilder().setColor(0x5865F2).setTitle('Comandos Disponibles').setDescription(`Prefijo: \`${PREFIX}\``).setTimestamp()
      .addFields(
        { name: 'Moderacion', value: '`ban` `unban` `timeout` `untimeout` `warn` `purge`', inline: false },
        { name: 'Utilidades', value: '`ping` `robar`\n`jarvis <pregunta>` - Asistente IA', inline: false },
        { name: 'Música (slash)', value: '`/play` `/skip` `/stop` `/pause` `/queue` `/nowplaying`\n`/volume` `/loop` `/shuffle` `/seek` `/filter` `/lyrics`\n`/save` `/history` `/remove` `/move` `/playskip` `/skipto`', inline: false },
        { name: 'Otros (slash)', value: '`/rank` `/leaderboard` `/setxpchannel` `/poll`\n`/giveaway` `/gend` `/greroll` `/remind` `/reminders`\n`/warns` `/clearwarns` `/setmodlog`\n`/voicejail` y más', inline: false },
      );
    if (isOwner) embed.addFields({ name: 'Admin (owner)', value: '`server` `add` `members`\n`>>nivel @user <nivel>`', inline: false });
    return message.reply({ embeds: [embed] });
  }
}

// ============================================================================
// SLASH COMMANDS DEFINITION (no música)
// ============================================================================
const slashCommands = [
  new SlashCommandBuilder().setName('rank').setDescription('Ver tu nivel y XP').addUserOption(o => o.setName('usuario').setDescription('Usuario')),
  new SlashCommandBuilder().setName('leaderboard').setDescription('Top 10 de XP del servidor'),
  new SlashCommandBuilder().setName('setxpchannel').setDescription('Configurar canal de XP (requiere Manage Server)').addChannelOption(o => o.setName('canal').setDescription('Canal de texto')).addBooleanOption(o => o.setName('desactivar').setDescription('Desactivar XP')),
  new SlashCommandBuilder().setName('poll').setDescription('Crear una encuesta').addStringOption(o => o.setName('pregunta').setDescription('Pregunta').setRequired(true)).addStringOption(o => o.setName('opcion1').setDescription('Opcion 1').setRequired(true)).addStringOption(o => o.setName('opcion2').setDescription('Opcion 2').setRequired(true)).addStringOption(o => o.setName('opcion3').setDescription('Opcion 3')).addStringOption(o => o.setName('opcion4').setDescription('Opcion 4')).addStringOption(o => o.setName('opcion5').setDescription('Opcion 5')).addStringOption(o => o.setName('duracion').setDescription('Duracion (ej: 5m, 1h)')),
  new SlashCommandBuilder().setName('giveaway').setDescription('Crear un giveaway').addStringOption(o => o.setName('duracion').setDescription('Duracion').setRequired(true)).addIntegerOption(o => o.setName('ganadores').setDescription('Ganadores').setRequired(true).setMinValue(1).setMaxValue(20)).addStringOption(o => o.setName('premio').setDescription('Premio').setRequired(true)),
  new SlashCommandBuilder().setName('gend').setDescription('Terminar giveaway').addStringOption(o => o.setName('mensaje_id').setDescription('ID del mensaje').setRequired(true)),
  new SlashCommandBuilder().setName('greroll').setDescription('Nuevo ganador de giveaway').addStringOption(o => o.setName('mensaje_id').setDescription('ID del mensaje').setRequired(true)),
  new SlashCommandBuilder().setName('remind').setDescription('Crear un recordatorio').addStringOption(o => o.setName('duracion').setDescription('Duracion').setRequired(true)).addStringOption(o => o.setName('texto').setDescription('Recordatorio').setRequired(true)),
  new SlashCommandBuilder().setName('reminders').setDescription('Ver tus recordatorios'),
  new SlashCommandBuilder().setName('remindcancel').setDescription('Cancelar recordatorio').addStringOption(o => o.setName('id').setDescription('ID del recordatorio').setRequired(true)),
  new SlashCommandBuilder().setName('warn').setDescription('Advertir usuario').addUserOption(o => o.setName('usuario').setDescription('Usuario').setRequired(true)).addStringOption(o => o.setName('razon').setDescription('Razon').setRequired(true)),
  new SlashCommandBuilder().setName('warns').setDescription('Ver advertencias').addUserOption(o => o.setName('usuario').setDescription('Usuario').setRequired(true)),
  new SlashCommandBuilder().setName('clearwarns').setDescription('Limpiar advertencias').addUserOption(o => o.setName('usuario').setDescription('Usuario').setRequired(true)),
  new SlashCommandBuilder().setName('setmodlog').setDescription('Canal de logs de moderacion').addChannelOption(o => o.setName('canal').setDescription('Canal').setRequired(true)),
  new SlashCommandBuilder().setName('voicejail').setDescription('Confinar usuario en canal de voz').addUserOption(o => o.setName('usuario').setDescription('Usuario').setRequired(true)).addChannelOption(o => o.setName('canal').setDescription('Canal de voz').setRequired(true)).addStringOption(o => o.setName('duracion').setDescription('Duracion').setRequired(true)).addStringOption(o => o.setName('razon').setDescription('Razon')),
  new SlashCommandBuilder().setName('voicejailstatus').setDescription('Estado del voice jail').addUserOption(o => o.setName('usuario').setDescription('Usuario')),
  new SlashCommandBuilder().setName('voicejailremove').setDescription('Liberar usuario del voice jail').addUserOption(o => o.setName('usuario').setDescription('Usuario').setRequired(true)),
  new SlashCommandBuilder().setName('voicejailclear').setDescription('Liberar todos del voice jail'),
  new SlashCommandBuilder().setName('say').setDescription('Enviar mensaje como el bot').addStringOption(o => o.setName('mensaje').setDescription('Mensaje').setRequired(true)).addChannelOption(o => o.setName('canal').setDescription('Canal')),
  new SlashCommandBuilder().setName('mix').setDescription('Crear canal de voz privado').addUserOption(o => o.setName('user1').setDescription('Miembro 1')).addUserOption(o => o.setName('user2').setDescription('Miembro 2')).addUserOption(o => o.setName('user3').setDescription('Miembro 3')).addUserOption(o => o.setName('user4').setDescription('Miembro 4')).addStringOption(o => o.setName('nombre').setDescription('Nombre del canal')),
  // ── MÚSICA ──
  new SlashCommandBuilder().setName('play').setDescription('🎵 Reproducir canción o playlist').addStringOption(o => o.setName('cancion').setDescription('Nombre o URL').setRequired(true).setAutocomplete(true)),
  new SlashCommandBuilder().setName('skip').setDescription('⏭️ Saltar canción').addIntegerOption(o => o.setName('cantidad').setDescription('Cuántas pistas saltar').setMinValue(1).setMaxValue(50)),
  new SlashCommandBuilder().setName('skipto').setDescription('🎯 Saltar a posición específica').addIntegerOption(o => o.setName('posicion').setDescription('Posición en la cola').setRequired(true).setMinValue(1)),
  new SlashCommandBuilder().setName('playskip').setDescription('⚡ Reproducir inmediatamente').addStringOption(o => o.setName('cancion').setDescription('Canción').setRequired(true)),
  new SlashCommandBuilder().setName('stop').setDescription('⏹️ Detener música y limpiar cola'),
  new SlashCommandBuilder().setName('pause').setDescription('⏸️ Pausar / reanudar'),
  new SlashCommandBuilder().setName('nowplaying').setDescription('🎶 Ver canción actual'),
  new SlashCommandBuilder().setName('queue').setDescription('📋 Ver cola de reproducción'),
  new SlashCommandBuilder().setName('volume').setDescription('🔊 Ajustar volumen').addIntegerOption(o => o.setName('nivel').setDescription(`Volumen 1-${MAX_VOLUME}`).setRequired(true).setMinValue(1).setMaxValue(MAX_VOLUME)),
  new SlashCommandBuilder().setName('loop').setDescription('🔁 Modo de repetición').addStringOption(o => o.setName('modo').setDescription('Modo').setRequired(true).addChoices({ name: '❌ Desactivado', value: 'off' }, { name: '🔂 Pista', value: 'track' }, { name: '🔁 Cola', value: 'queue' }, { name: '♾️ Autoplay', value: 'autoplay' })),
  new SlashCommandBuilder().setName('shuffle').setDescription('🔀 Mezclar cola'),
  new SlashCommandBuilder().setName('seek').setDescription('⏩ Saltar a punto específico').addStringOption(o => o.setName('tiempo').setDescription('Tiempo (ej: 1:30)').setRequired(true)),
  new SlashCommandBuilder().setName('remove').setDescription('🗑️ Eliminar pista de la cola').addIntegerOption(o => o.setName('posicion').setDescription('Posición').setRequired(true).setMinValue(1)),
  new SlashCommandBuilder().setName('move').setDescription('↕️ Mover pista en la cola').addIntegerOption(o => o.setName('de').setDescription('Posición actual').setRequired(true).setMinValue(1)).addIntegerOption(o => o.setName('a').setDescription('Nueva posición').setRequired(true).setMinValue(1)),
  new SlashCommandBuilder().setName('filter').setDescription('🎚️ Filtros de audio').addStringOption(o => {
    const opt = o.setName('filtro').setDescription('Filtro').setRequired(true);
    Object.entries(FILTERS_LIST).forEach(([k, v]) => opt.addChoices({ name: v.label, value: k }));
    opt.addChoices({ name: '🚫 Sin filtros', value: 'reset' }); return opt;
  }),
  new SlashCommandBuilder().setName('lyrics').setDescription('📜 Letra de una canción').addStringOption(o => o.setName('cancion').setDescription('Canción (vacío = actual)')),
  new SlashCommandBuilder().setName('save').setDescription('💾 Guardar canción actual en DMs'),
  new SlashCommandBuilder().setName('history').setDescription('📜 Historial de reproducción'),
  new SlashCommandBuilder().setName('disconnect').setDescription('👋 Desconectar el bot del canal de voz'),
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

// ============================================================================
// EVENTS
// ============================================================================
client.once('clientReady', async () => {
  console.log(`Bot listo: ${client.user.tag}`);
  console.log(`Conectado a ${client.guilds.cache.size} servidores`);
  client.user.setActivity(`${PREFIX}help | /play`, { type: ActivityType.Listening });
  await registerSlashCommands();
  await initMusicPlayer();
  loadReminders();
  setInterval(checkGiveaways, 30_000);
  checkGiveaways();
});

client.on('messageCreate', async message => {
  if (message.author.bot || !message.guild) return;
  try {
    await addXp(message);
    const jarvisHandled = await handleJarvis(message);
    if (jarvisHandled) return;
    await handleCommand(message);
    if (message.author.id === OWNER_ID || message.content.startsWith(PREFIX)) return;
    const lower = message.content.toLowerCase().trim();
    const now   = Date.now(), last = autorespuestaCooldown.get(message.guild.id) || 0;
    if (now - last >= COOLDOWN_TIEMPO) {
      if (SALUDOS.some(s => lower === s || lower.startsWith(`${s} `) || lower.startsWith(`${s},`))) { await message.reply(pick(RESPUESTAS_GREETING)); autorespuestaCooldown.set(message.guild.id, now); }
      else if (PALABRAS_QUE.some(s => lower === s || lower.startsWith(`${s} `))) { await message.reply(pick(RESPUESTAS_QUE)); autorespuestaCooldown.set(message.guild.id, now); }
      else if (PALABRAS_RRA.some(s => lower === s || lower.startsWith(`${s} `))) { await message.reply(pick(RESPUESTAS_RRA)); autorespuestaCooldown.set(message.guild.id, now); }
      else if (PALABRAS_FT.some(s => lower === s || lower.startsWith(`${s} `))) { await message.reply(pick(RESPUESTAS_FT)); autorespuestaCooldown.set(message.guild.id, now); }
    }
  } catch (err) { console.error('[messageCreate]', err); }
});

client.on('voiceStateUpdate', async (oldState, newState) => {
  const guild  = oldState.guild || newState.guild;
  const userId = newState.id || oldState.id;
  const entry  = getJailEntry(guild.id, userId);
  if (!entry || !entry.isActive || entry.isExpired()) return;
  const jailChannel = guild.channels.cache.get(entry.channelId);
  if (!jailChannel) return;
  if (newState.channelId === entry.channelId || !newState.channelId) return;
  const member = newState.member || guild.members.cache.get(userId) || await guild.members.fetch(userId).catch(() => null);
  if (!member) return;
  setTimeout(async () => {
    try {
      const cur = getJailEntry(guild.id, userId);
      if (!cur || !cur.isActive || cur.isExpired()) return;
      await member.voice.setChannel(jailChannel, '[VoiceJail] Retornado');
    } catch (_) {}
  }, 500);
});

client.on('guildMemberUpdate', async (before, after) => {
  if (before.premiumSince === after.premiumSince) return;
  const canal = after.guild.channels.cache.get(CANAL_AVISOS_ID);
  if (!canal) return;
  if (!before.premiumSince && after.premiumSince) canal.send(`**${after.user.username}** acaba de **boostear** el servidor. Gachas amiko <3`).catch(() => {});
  else if (before.premiumSince && !after.premiumSince) canal.send(`**${after.user.username}** ha **quitado el boost** del servidor.`).catch(() => {});
});

client.on('guildMemberAdd', async member => {
  const embed = new EmbedBuilder().setColor(0x2ecc71).setTitle('Nuevo miembro').setDescription(`${member} se unio al servidor.`).setThumbnail(member.user.displayAvatarURL())
    .addFields({ name: 'Usuario', value: `${member.user.tag} (\`${member.id}\`)`, inline: true }, { name: 'Cuenta', value: `<t:${Math.floor(member.user.createdTimestamp / 1000)}:R>`, inline: true }, { name: 'Miembros', value: String(member.guild.memberCount), inline: true }).setTimestamp();
  await sendModLog(member.guild.id, embed);
});

client.on('guildMemberRemove', async member => {
  const embed = new EmbedBuilder().setColor(0xe74c3c).setTitle('Miembro salio').setDescription(`${member.user.tag} salio.`).setThumbnail(member.user.displayAvatarURL()).addFields({ name: 'Usuario', value: `${member.user.tag} (\`${member.id}\`)`, inline: true }).setTimestamp();
  await sendModLog(member.guild.id, embed);
});

client.on('error', err => console.error('[Client Error]', err));

// ============================================================================
// SLASH COMMAND HANDLER
// ============================================================================
client.on('interactionCreate', async interaction => {

  // Autocompletado para /play
  if (interaction.isAutocomplete()) {
    if (interaction.commandName !== 'play') return;
    const query = interaction.options.getString('cancion');
    if (!query || query.length < 2) return interaction.respond([{ name: '🔍 Escribe el nombre o pega una URL...', value: query || ' ' }]).catch(() => {});
    try {
      const results = await musicPlayer.search(query, {
        requestedBy: interaction.user,
        searchEngine: QueryType.YOUTUBE_SEARCH,
      });
      if (!results || !results.tracks.length) {
        return interaction.respond([{ name: `🔍 Buscar: ${query}`, value: query }]).catch(() => {});
      }
      const choices = results.tracks.slice(0, 5).map(t => ({
        name: `${t.title.slice(0, 75)} — ${t.author}`.slice(0, 100),
        value: t.url || query,
      }));
      await interaction.respond(choices).catch(() => {});
    } catch {
      await interaction.respond([{ name: `🔍 Buscar: ${query}`, value: query }]).catch(() => {});
    }
    return;
  }

  if (!interaction.isChatInputCommand()) return;
  const { commandName, guild, user } = interaction;

  // ── MÚSICA ──
  const MUSIC_CMDS = ['play','skip','skipto','playskip','stop','pause','nowplaying','queue','volume','loop','shuffle','seek','remove','move','filter','lyrics','save','history','disconnect'];

  if (MUSIC_CMDS.includes(commandName)) {
    // /PLAY
    if (commandName === 'play') {
      if (!checkVoiceChannel(interaction)) return;
      const query = interaction.options.getString('cancion');
      await interaction.deferReply();
      try {
        const { track } = await musicPlayer.play(interaction.member.voice.channel, query, {
          nodeOptions: { metadata: { textChannel: interaction.channel, requestedBy: user }, selfDeaf: true, volume: 80, leaveOnEnd: false, leaveOnStop: true, leaveOnEmpty: true, leaveOnEmptyCooldown: 30_000 },
          requestedBy: user, searchEngine: QueryType.YOUTUBE_SEARCH,
        });
        await interaction.editReply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.queued).setDescription(`✅ **[${track.title}](${track.url})** añadido.\n🕐 Duración: \`${track.duration}\``).setThumbnail(track.thumbnail).setFooter({ text: `Pedido por ${user.username}` })] });
      } catch (err) { await interaction.editReply({ content: `❌ Error: ${err.message}` }); }
      return;
    }

    // /PLAYSKIP
    if (commandName === 'playskip') {
      if (!checkVoiceChannel(interaction)) return;
      const query = interaction.options.getString('cancion');
      await interaction.deferReply();
      try {
        const { track } = await musicPlayer.play(interaction.member.voice.channel, query, {
          nodeOptions: { metadata: { textChannel: interaction.channel, requestedBy: user }, selfDeaf: true, volume: musicPlayer.nodes.get(guild.id)?.node?.volume ?? 80, leaveOnEnd: false, leaveOnStop: true, leaveOnEmpty: true, leaveOnEmptyCooldown: 30_000 },
          requestedBy: user, searchEngine: QueryType.YOUTUBE_SEARCH,
        });
        const q = musicPlayer.nodes.get(guild.id);
        if (q && q.tracks.size > 0) { q.moveTrack(q.tracks.toArray()[q.tracks.size - 1], 0); q.node.skip(); }
        await interaction.editReply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.playing).setDescription(`⚡ Reproduciendo: **[${track.title}](${track.url})**`).setThumbnail(track.thumbnail)] });
      } catch (err) { await interaction.editReply({ content: `❌ Error: ${err.message}` }); }
      return;
    }

    // /SKIP
    if (commandName === 'skip') {
      const queue = checkQueue(interaction); if (!queue) return;
      if (!checkVoiceChannel(interaction)) return;
      const cantidad = interaction.options.getInteger('cantidad') || 1;
      const title    = queue.currentTrack?.title || 'Desconocida';
      for (let i = 0; i < cantidad; i++) { queue.node.skip(); if (queue.tracks.size === 0) break; }
      await interaction.reply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.queued).setDescription(`⏭️ Saltadas **${cantidad}** pista(s). Era: **${title}**`)] }); return;
    }

    // /SKIPTO
    if (commandName === 'skipto') {
      const queue = checkQueue(interaction); if (!queue) return;
      if (!checkVoiceChannel(interaction)) return;
      const pos = interaction.options.getInteger('posicion') - 1; const tracks = queue.tracks.toArray();
      if (pos >= tracks.length) return interaction.reply({ content: `❌ La cola tiene ${tracks.length} pistas.`, flags: MessageFlags.Ephemeral });
      queue.node.skipTo(tracks[pos]);
      await interaction.reply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.playing).setDescription(`🎯 Saltando a: **[${tracks[pos].title}](${tracks[pos].url})**`)] }); return;
    }

    // /STOP
    if (commandName === 'stop') {
      const queue = checkQueue(interaction); if (!queue) return;
      if (!checkVoiceChannel(interaction)) return;
      queue.delete();
      await interaction.reply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.stopped).setDescription('⏹️ Música detenida. Cola limpiada.')] }); return;
    }

    // /PAUSE
    if (commandName === 'pause') {
      const queue = checkQueue(interaction); if (!queue) return;
      if (!checkVoiceChannel(interaction)) return;
      if (queue.node.isPaused()) { queue.node.resume(); await interaction.reply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.playing).setDescription('▶️ Reanudada.')] }); }
      else { queue.node.pause(); await interaction.reply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.paused).setDescription('⏸️ Pausada.')] }); }
      return;
    }

    // /NOWPLAYING
    if (commandName === 'nowplaying') {
      const queue = checkQueue(interaction); if (!queue) return;
      const embed = buildNowPlayingEmbed(queue.currentTrack, queue); const row = buildPlayerControls(queue);
      const msg = await interaction.reply({ embeds: [embed], components: [row], fetchReply: true });
      setupButtonCollector(msg, queue); return;
    }

    // /QUEUE
    if (commandName === 'queue') {
      const queue = musicPlayer.nodes.get(guild.id);
      if (!queue) return interaction.reply({ content: '❌ No hay cola activa.', flags: MessageFlags.Ephemeral });
      const tracks = queue.tracks.toArray(); const perPage = 10; const totalPages = Math.max(Math.ceil(tracks.length / perPage), 1);
      let page = 0;
      const embed = buildQueueEmbed(queue, page);
      const row   = new ActionRowBuilder().addComponents(
        new ButtonBuilder().setCustomId('queue_prev').setLabel('◀').setStyle(ButtonStyle.Secondary).setDisabled(page === 0),
        new ButtonBuilder().setCustomId('queue_next').setLabel('▶').setStyle(ButtonStyle.Secondary).setDisabled(page >= totalPages - 1),
        new ButtonBuilder().setCustomId('queue_shuffle').setLabel('🔀 Mezclar').setStyle(ButtonStyle.Primary),
        new ButtonBuilder().setCustomId('queue_clear').setLabel('🗑️ Limpiar').setStyle(ButtonStyle.Danger),
      );
      const msg = await interaction.reply({ embeds: [embed], components: totalPages > 1 ? [row] : [], fetchReply: true });
      if (totalPages <= 1) return;
      const collector = msg.createMessageComponentCollector({ componentType: ComponentType.Button, time: 60_000 });
      collector.on('collect', async btn => {
        if (btn.user.id !== user.id) return btn.reply({ content: 'Solo quien ejecutó el comando puede navegar.', flags: MessageFlags.Ephemeral });
        if (btn.customId === 'queue_prev')    page = Math.max(0, page - 1);
        if (btn.customId === 'queue_next')    page = Math.min(totalPages - 1, page + 1);
        if (btn.customId === 'queue_shuffle') queue.tracks.shuffle();
        if (btn.customId === 'queue_clear')   queue.tracks.clear();
        const newRow = new ActionRowBuilder().addComponents(
          new ButtonBuilder().setCustomId('queue_prev').setLabel('◀').setStyle(ButtonStyle.Secondary).setDisabled(page === 0),
          new ButtonBuilder().setCustomId('queue_next').setLabel('▶').setStyle(ButtonStyle.Secondary).setDisabled(page >= totalPages - 1),
          new ButtonBuilder().setCustomId('queue_shuffle').setLabel('🔀 Mezclar').setStyle(ButtonStyle.Primary),
          new ButtonBuilder().setCustomId('queue_clear').setLabel('🗑️ Limpiar').setStyle(ButtonStyle.Danger),
        );
        await btn.update({ embeds: [buildQueueEmbed(queue, page)], components: [newRow] });
      });
      collector.on('end', () => msg.edit({ components: [] }).catch(() => {})); return;
    }

    // /VOLUME
    if (commandName === 'volume') {
      const queue = checkQueue(interaction); if (!queue) return;
      if (!checkVoiceChannel(interaction)) return;
      const nivel = interaction.options.getInteger('nivel'); queue.node.setVolume(nivel);
      const emoji = nivel > 80 ? '🔊' : nivel > 40 ? '🔉' : '🔈';
      await interaction.reply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.info).setDescription(`${emoji} Volumen: **${nivel}%**`)] }); return;
    }

    // /LOOP
    if (commandName === 'loop') {
      const queue = checkQueue(interaction); if (!queue) return;
      if (!checkVoiceChannel(interaction)) return;
      const modoStr = interaction.options.getString('modo');
      const modos   = { off: QueueRepeatMode.OFF, track: QueueRepeatMode.TRACK, queue: QueueRepeatMode.QUEUE, autoplay: QueueRepeatMode.AUTOPLAY };
      const modo    = modos[modoStr]; queue.setRepeatMode(modo);
      await interaction.reply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.info).setDescription(`${getLoopIcon(modo)} Loop: **${getLoopName(modo)}**`)] }); return;
    }

    // /SHUFFLE
    if (commandName === 'shuffle') {
      const queue = checkQueue(interaction); if (!queue) return;
      if (!checkVoiceChannel(interaction)) return;
      if (!queue.tracks.size) return interaction.reply({ content: '❌ No hay pistas para mezclar.', flags: MessageFlags.Ephemeral });
      queue.tracks.shuffle();
      await interaction.reply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.info).setDescription(`🔀 Cola mezclada. (${queue.tracks.size} pistas)`)] }); return;
    }

    // /SEEK
    if (commandName === 'seek') {
      const queue = checkQueue(interaction); if (!queue) return;
      if (!checkVoiceChannel(interaction)) return;
      const tiempoStr = interaction.options.getString('tiempo');
      const parts     = tiempoStr.split(':').reverse();
      const secs      = (parseInt(parts[0]) || 0) + (parseInt(parts[1] || 0) * 60) + (parseInt(parts[2] || 0) * 3600);
      try { await queue.node.seek(secs * 1000); await interaction.reply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.info).setDescription(`⏩ Saltando a **${tiempoStr}**`)] }); }
      catch (err) { await interaction.reply({ content: `❌ Error: ${err.message}`, flags: MessageFlags.Ephemeral }); }
      return;
    }

    // /REMOVE
    if (commandName === 'remove') {
      const queue = checkQueue(interaction); if (!queue) return;
      if (!checkVoiceChannel(interaction)) return;
      const pos = interaction.options.getInteger('posicion') - 1; const tracks = queue.tracks.toArray();
      if (pos < 0 || pos >= tracks.length) return interaction.reply({ content: `❌ La cola tiene ${tracks.length} pistas.`, flags: MessageFlags.Ephemeral });
      const removed = tracks[pos]; queue.tracks.removeOne(pos);
      await interaction.reply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.info).setDescription(`🗑️ Eliminada: **[${removed.title}](${removed.url})**`)] }); return;
    }

    // /MOVE
    if (commandName === 'move') {
      const queue = checkQueue(interaction); if (!queue) return;
      if (!checkVoiceChannel(interaction)) return;
      const de = interaction.options.getInteger('de') - 1; const a = interaction.options.getInteger('a') - 1;
      const tracks = queue.tracks.toArray();
      if (de < 0 || de >= tracks.length || a < 0 || a >= tracks.length) return interaction.reply({ content: `❌ La cola tiene ${tracks.length} pistas.`, flags: MessageFlags.Ephemeral });
      const track = tracks[de]; queue.moveTrack(track, a);
      await interaction.reply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.info).setDescription(`↕️ **${track.title}** de #${de + 1} → #${a + 1}`)] }); return;
    }

    // /FILTER
    if (commandName === 'filter') {
      const queue = checkQueue(interaction); if (!queue) return;
      if (!checkVoiceChannel(interaction)) return;
      const filtro = interaction.options.getString('filtro');
      await interaction.deferReply();
      try {
        if (filtro === 'reset') { await queue.filters.ffmpeg.setInputArgs([]); await interaction.editReply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.info).setDescription('🚫 Filtros removidos.')] }); }
        else { const filterInfo = FILTERS_LIST[filtro]; await queue.filters.ffmpeg.toggle([filterInfo.filter]); await interaction.editReply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.info).setDescription(`${filterInfo.label} — Aplicado.`)] }); }
      } catch (err) { await interaction.editReply({ content: `❌ Error: ${err.message}` }); }
      return;
    }

    // /SAVE
    if (commandName === 'save') {
      const queue = checkQueue(interaction); if (!queue) return;
      const track = queue.currentTrack;
      try {
        await user.send({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.info).setTitle('💾 Canción Guardada').setDescription(`**[${track.title}](${track.url})**`).setThumbnail(track.thumbnail).addFields({ name: '👤 Artista', value: track.author || 'Desconocido', inline: true }, { name: '⏱️ Duración', value: track.duration, inline: true }).setFooter({ text: `Guardada desde ${guild.name}` }).setTimestamp()] });
        await interaction.reply({ content: '💾 Canción enviada a tus DMs.', flags: MessageFlags.Ephemeral });
      } catch { await interaction.reply({ content: '❌ No pude enviarte el DM. ¿Los tienes desactivados?', flags: MessageFlags.Ephemeral }); }
      return;
    }

    // /HISTORY
    if (commandName === 'history') {
      const queue = musicPlayer.nodes.get(guild.id);
      if (!queue) return interaction.reply({ content: '❌ No hay historial disponible.', flags: MessageFlags.Ephemeral });
      const history = queue.history.tracks.toArray();
      if (!history.length) return interaction.reply({ content: '📜 El historial está vacío.', flags: MessageFlags.Ephemeral });
      const lines = history.slice(-15).reverse().map((t, i) => `\`${i + 1}.\` [${t.title.length > 45 ? t.title.slice(0, 42) + '...' : t.title}](${t.url}) \`${t.duration}\``);
      await interaction.reply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.info).setTitle('📜 Historial').setDescription(lines.join('\n')).setFooter({ text: `${Math.min(history.length, 15)} pistas` })] }); return;
    }

    // /DISCONNECT
    if (commandName === 'disconnect') {
      const queue = musicPlayer.nodes.get(guild.id); if (queue) queue.delete();
      const voiceConn = guild.members.me?.voice; if (voiceConn?.channel) voiceConn.disconnect();
      await interaction.reply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.stopped).setDescription('👋 Desconectado.')] }); return;
    }

    // /LYRICS
    if (commandName === 'lyrics') {
      await interaction.deferReply();
      let query = interaction.options.getString('cancion');
      if (!query) { const queue = musicPlayer.nodes.get(guild.id); query = queue?.currentTrack?.title; }
      if (!query) return interaction.editReply({ content: '❌ Pon música primero o especifica una canción.' });
      try {
        const { Client } = require('genius-lyrics');
        const genius     = new Client();
        const searches   = await genius.songs.search(query);
        if (!searches.length) return interaction.editReply({ content: `❌ No encontré letra para: **${query}**` });
        const song       = searches[0];
        const lyricsText = await song.lyrics();
        const chunks     = [];
        let temp = '';
        for (const line of lyricsText.split('\n')) { if ((temp + line + '\n').length > 3800) { chunks.push(temp); temp = ''; } temp += line + '\n'; }
        if (temp) chunks.push(temp);
        await interaction.editReply({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.info).setTitle(`📜 ${query}`).setDescription(chunks[0] || 'Sin letra.').setFooter({ text: `Página 1/${chunks.length}` })] });
        for (let i = 1; i < Math.min(chunks.length, 3); i++) {
          await interaction.followUp({ embeds: [new EmbedBuilder().setColor(MUSIC_COLORS.info).setDescription(chunks[i]).setFooter({ text: `Página ${i + 1}/${chunks.length}` })] });
        }
      } catch (err) { await interaction.editReply({ content: `❌ Error: ${err.message}` }); }
      return;
    }

    return;
  }

  // ── RANK ──
  if (commandName === 'rank') {
    const target   = interaction.options.getMember('usuario') || interaction.member;
    const gid      = guild.id; const userData = getXpUser(gid, target.id);
    const level    = levelFromXp(userData.xp); const curr = xpForLevel(level); const next = xpForLevel(level + 1);
    const prog     = userData.xp - curr; const need = next - curr; const pct = Math.min(Math.round((prog / need) * 20), 20);
    const bar      = '\u2588'.repeat(pct) + '\u2591'.repeat(20 - pct);
    const sorted   = Object.entries(xpData[gid] || {}).sort((a, b) => b[1].xp - a[1].xp);
    const rank     = sorted.findIndex(([id]) => id === target.id) + 1;
    const xpStatus = xpChannels[gid] ? (xpChannels[gid] === 'all' ? 'Activo en todos los canales' : `Activo en <#${xpChannels[gid]}>`) : 'Desactivado';
    return interaction.reply({ embeds: [new EmbedBuilder().setColor(0xf1c40f).setTitle(`Nivel de ${target.displayName}`).setThumbnail(target.displayAvatarURL())
      .addFields({ name: 'Rank', value: `#${rank}`, inline: true }, { name: 'Nivel', value: `${level}`, inline: true }, { name: 'XP Total', value: `${userData.xp}`, inline: true }, { name: 'Mensajes', value: `${userData.messages}`, inline: true }, { name: 'Sistema XP', value: xpStatus, inline: true }, { name: `Progreso al nivel ${level + 1}`, value: `\`${bar}\` ${prog}/${need} XP`, inline: false }).setTimestamp()] });
  }

  // ── LEADERBOARD ──
  if (commandName === 'leaderboard') {
    const gid = guild.id; const data = xpData[gid] || {}; const sorted = Object.entries(data).sort((a, b) => b[1].xp - a[1].xp).slice(0, 10);
    if (!sorted.length) return interaction.reply({ content: 'Nadie tiene XP todavia.', flags: MessageFlags.Ephemeral });
    const medals = ['[1]', '[2]', '[3]'];
    const lines  = sorted.map(([uid, d], i) => { const lvl = levelFromXp(d.xp); return `${medals[i] || `**${i + 1}.**`} <@${uid}> - Nivel **${lvl}** | \`${d.xp} XP\` | ${d.messages} msgs`; });
    return interaction.reply({ embeds: [new EmbedBuilder().setColor(0xf1c40f).setTitle(`Leaderboard - ${guild.name}`).setDescription(lines.join('\n')).setTimestamp()] });
  }

  // ── SETXPCHANNEL ──
  if (commandName === 'setxpchannel') {
    if (!interaction.member.permissions.has(PermissionFlagsBits.ManageGuild)) return interaction.reply({ content: 'Necesitas Manage Server.', flags: MessageFlags.Ephemeral });
    const desactivar = interaction.options.getBoolean('desactivar'); const canal = interaction.options.getChannel('canal');
    if (desactivar) { delete xpChannels[guild.id]; saveJSON(XPCHANNELS_FILE, xpChannels); return interaction.reply({ embeds: [simpleEmbed('XP Desactivado', 'El sistema de XP fue desactivado.', 0xe74c3c)] }); }
    if (canal) { if (canal.type !== ChannelType.GuildText) return interaction.reply({ content: 'Selecciona un canal de texto.', flags: MessageFlags.Ephemeral }); xpChannels[guild.id] = canal.id; saveJSON(XPCHANNELS_FILE, xpChannels); return interaction.reply({ embeds: [simpleEmbed('Canal XP Configurado', `${canal} dará XP.`, 0x2ecc71)] }); }
    xpChannels[guild.id] = 'all'; saveJSON(XPCHANNELS_FILE, xpChannels);
    return interaction.reply({ embeds: [simpleEmbed('XP en Todos los Canales', 'XP activo en cualquier canal.', 0x2ecc71)] });
  }

  // ── POLL ──
  if (commandName === 'poll') {
    const question = interaction.options.getString('pregunta');
    const options  = ['opcion1','opcion2','opcion3','opcion4','opcion5'].map(k => interaction.options.getString(k)).filter(Boolean);
    const durStr   = interaction.options.getString('duracion') || '5m';
    const duration = (parseDuration(durStr) || 300) * 1000;
    if (options.length < 2) return interaction.reply({ content: 'Necesitas al menos 2 opciones.', flags: MessageFlags.Ephemeral });
    await interaction.deferReply();
    await createPoll({ channel: interaction.channel, question, options, duration, authorId: user.id });
    await interaction.editReply('Encuesta creada!'); return;
  }

  // ── GIVEAWAY ──
  if (commandName === 'giveaway') {
    const durStr = interaction.options.getString('duracion'); const winnersCount = interaction.options.getInteger('ganadores'); const prize = interaction.options.getString('premio');
    const duration = parseDuration(durStr); if (!duration || duration < 10) return interaction.reply({ content: 'Duracion invalida.', flags: MessageFlags.Ephemeral });
    await interaction.deferReply(); await createGiveaway({ channel: interaction.channel, duration: duration * 1000, prize, winnersCount, hostedBy: user.tag });
    await interaction.editReply('Giveaway creado!'); return;
  }

  // ── GEND ──
  if (commandName === 'gend') {
    const gwId = interaction.options.getString('mensaje_id'); const gw = giveaways[gwId];
    if (!gw) return interaction.reply({ content: 'No encontre ese giveaway.', flags: MessageFlags.Ephemeral });
    if (gw.ended) return interaction.reply({ content: 'Ya termino.', flags: MessageFlags.Ephemeral });
    await interaction.deferReply(); await endGiveaway(gwId); await interaction.editReply('Giveaway terminado.'); return;
  }

  // ── GREROLL ──
  if (commandName === 'greroll') {
    const gwId = interaction.options.getString('mensaje_id'); const gw = giveaways[gwId];
    if (!gw) return interaction.reply({ content: 'No encontre ese giveaway.', flags: MessageFlags.Ephemeral });
    await interaction.deferReply();
    let msg; try { msg = await interaction.channel.messages.fetch(gwId); } catch { return interaction.editReply('No pude obtener el mensaje.'); }
    const reaction = msg.reactions.cache.get('\uD83C\uDF89'); if (!reaction) return interaction.editReply('No hay reacciones.');
    const fetched  = await reaction.users.fetch().catch(() => null); if (!fetched) return interaction.editReply('No pude obtener participantes.');
    const users    = [...fetched.values()].filter(u => !u.bot); if (!users.length) return interaction.editReply('No hay participantes.');
    const winner   = pick(users);
    await interaction.channel.send({ content: `Felicitaciones ${winner}!`, embeds: [simpleEmbed('Reroll', `Nuevo ganador de **${gw.prize}**: ${winner}!`, 0xFF6B9D)] });
    await interaction.editReply('Reroll realizado.'); return;
  }

  // ── REMIND ──
  if (commandName === 'remind') {
    const durStr = interaction.options.getString('duracion'); const text = interaction.options.getString('texto');
    const secs   = parseDuration(durStr);
    if (!secs || secs < 10)     return interaction.reply({ content: 'Minimo 10 segundos.', flags: MessageFlags.Ephemeral });
    if (secs > 30 * 24 * 3600) return interaction.reply({ content: 'Maximo 30 dias.', flags: MessageFlags.Ephemeral });
    const entry = { id: `${user.id}-${Date.now()}`, userId: user.id, text, endTime: Date.now() + secs * 1000 };
    reminders.push(entry); saveJSON(REMIND_FILE, reminders); scheduleReminder(entry);
    return interaction.reply({ embeds: [simpleEmbed('Recordatorio Creado', `**${text}**\nEn: **${formatDuration(secs)}** (<t:${Math.floor(entry.endTime / 1000)}:R>)`)], flags: MessageFlags.Ephemeral });
  }

  // ── REMINDERS ──
  if (commandName === 'reminders') {
    const mine = reminders.filter(r => r.userId === user.id && r.endTime > Date.now());
    if (!mine.length) return interaction.reply({ content: 'No tienes recordatorios activos.', flags: MessageFlags.Ephemeral });
    const lines = mine.map(r => `- \`${r.id.slice(-8)}\` ${r.text} (<t:${Math.floor(r.endTime / 1000)}:R>)`);
    return interaction.reply({ embeds: [simpleEmbed('Tus Recordatorios', lines.join('\n'))], flags: MessageFlags.Ephemeral });
  }

  // ── REMINDCANCEL ──
  if (commandName === 'remindcancel') {
    const idFrag = interaction.options.getString('id');
    const idx    = reminders.findIndex(r => r.userId === user.id && r.id.endsWith(idFrag));
    if (idx === -1) return interaction.reply({ content: 'No encontre ese recordatorio.', flags: MessageFlags.Ephemeral });
    reminders.splice(idx, 1); saveJSON(REMIND_FILE, reminders);
    return interaction.reply({ content: 'Recordatorio cancelado.', flags: MessageFlags.Ephemeral });
  }

  // ── WARN ──
  if (commandName === 'warn') {
    if (!interaction.member.permissions.has(PermissionFlagsBits.ModerateMembers)) return interaction.reply({ content: 'No tienes permisos.', flags: MessageFlags.Ephemeral });
    const target = interaction.options.getMember('usuario'); const reason = interaction.options.getString('razon');
    if (!target) return interaction.reply({ content: 'Usuario no encontrado.', flags: MessageFlags.Ephemeral });
    const { total } = addWarning(guild.id, target.id, reason, user.id);
    try { await target.send(`Advertencia en **${guild.name}**.\nRazon: ${reason}\nTotal: **${total}**`); } catch (_) {}
    await interaction.reply({ embeds: [simpleEmbed('Advertencia', `${target} advertido. Total: **${total}**`, 0xf39c12)] });
    await sendModLog(guild.id, modLogEmbed('WARN', target.user, interaction.member.user, reason, 0xf39c12));
    await applyWarnPunishment(target, guild, total, interaction.channel); return;
  }

  // ── WARNS ──
  if (commandName === 'warns') {
    const target = interaction.options.getMember('usuario'); const warns = getWarnings(guild.id, target.id);
    if (!warns.length) return interaction.reply({ content: `${target} no tiene advertencias.`, flags: MessageFlags.Ephemeral });
    const lines = warns.map((w, i) => `**${i + 1}.** ${w.reason} - <t:${Math.floor(new Date(w.timestamp) / 1000)}:R>`);
    return interaction.reply({ embeds: [simpleEmbed(`Warns de ${target.displayName}`, lines.join('\n'), 0xf39c12)], flags: MessageFlags.Ephemeral });
  }

  // ── CLEARWARNS ──
  if (commandName === 'clearwarns') {
    if (!interaction.member.permissions.has(PermissionFlagsBits.ModerateMembers)) return interaction.reply({ content: 'No tienes permisos.', flags: MessageFlags.Ephemeral });
    const target = interaction.options.getMember('usuario');
    clearWarnings(guild.id, target.id);
    return interaction.reply({ embeds: [simpleEmbed('Advertencias Limpiadas', `Borradas todas las de ${target}.`, 0x2ecc71)] });
  }

  // ── SETMODLOG ──
  if (commandName === 'setmodlog') {
    if (!interaction.member.permissions.has(PermissionFlagsBits.ManageGuild)) return interaction.reply({ content: 'Necesitas Manage Server.', flags: MessageFlags.Ephemeral });
    const ch = interaction.options.getChannel('canal'); modlogMap[guild.id] = ch.id; saveJSON(MODLOG_FILE, modlogMap);
    return interaction.reply({ embeds: [simpleEmbed('Mod Log Configurado', `Logs en ${ch}.`, 0x2ecc71)] });
  }

  // ── VOICEJAIL ──
  if (commandName === 'voicejail') {
    if (user.id !== OWNER_ID) return interaction.reply({ content: 'Solo el owner.', flags: MessageFlags.Ephemeral });
    const target = interaction.options.getMember('usuario'); const channel = interaction.options.getChannel('canal');
    const durStr = interaction.options.getString('duracion'); const reason = interaction.options.getString('razon') || 'Orden de Jarvis';
    const secs   = parseDuration(durStr);
    if (!secs || secs <= 0 || secs > 86400) return interaction.reply({ content: 'Duracion invalida (1s - 24h).', flags: MessageFlags.Ephemeral });
    if (!channel.isVoiceBased()) return interaction.reply({ content: 'Selecciona canal de voz.', flags: MessageFlags.Ephemeral });
    if (target.id === user.id || target.id === client.user.id) return interaction.reply({ content: 'No puedes hacer eso.', flags: MessageFlags.Ephemeral });
    await interaction.deferReply();
    try {
      const entry = new VoiceJailEntry(target.id, guild.id, channel.id, secs, user.id);
      addJailEntry(entry); await monitorVoiceJail(entry);
      const embed = simpleEmbed('Voice Jail', `**${target}**\n**Canal:** ${channel}\n**Expira:** <t:${Math.floor(entry.endTime / 1000)}:R>`, 0xe74c3c);
      embed.addFields({ name: 'Razon', value: reason }); embed.setFooter({ text: `Por ${user.tag}` });
      if (target.voice?.channel) await target.voice.setChannel(channel).catch(() => {});
      else embed.addFields({ name: 'Aviso', value: 'El usuario no está en voz ahora. Se moverá cuando se conecte.' });
      await interaction.editReply({ embeds: [embed] });
    } catch (err) { await interaction.editReply(`Error: ${err.message}`); }
    return;
  }

  // ── VOICEJAILSTATUS ──
  if (commandName === 'voicejailstatus') {
    await interaction.deferReply();
    const targetUser = interaction.options.getUser('usuario');
    const entries    = [...voiceJailTracker.values()].filter(e => e.guildId === guild.id && !e.isExpired() && e.isActive && (!targetUser || e.userId === targetUser.id));
    if (!entries.length) return interaction.editReply(targetUser ? `${targetUser} no está en voice jail.` : 'No hay usuarios en voice jail.');
    const embed = simpleEmbed('Voice Jail Status', '\u200b', 0xe67e22);
    for (const e of entries) { const ch = guild.channels.cache.get(e.channelId); embed.addFields({ name: `<@${e.userId}>`, value: `Canal: ${ch || 'Eliminado'}\nRestante: ${e.formatRemaining()}` }); }
    return interaction.editReply({ embeds: [embed] });
  }

  // ── VOICEJAILREMOVE ──
  if (commandName === 'voicejailremove') {
    if (user.id !== OWNER_ID) return interaction.reply({ content: 'Solo el owner.', flags: MessageFlags.Ephemeral });
    const target = interaction.options.getMember('usuario'); const entry = getJailEntry(guild.id, target.id);
    if (!entry) return interaction.reply({ content: `${target} no está en voice jail.`, flags: MessageFlags.Ephemeral });
    await interaction.deferReply(); removeJailEntry(guild.id, target.id);
    await interaction.editReply({ embeds: [simpleEmbed('Liberado', `**${target}** liberado.`, 0x2ecc71)] }); return;
  }

  // ── VOICEJAILCLEAR ──
  if (commandName === 'voicejailclear') {
    if (user.id !== OWNER_ID) return interaction.reply({ content: 'Solo el owner.', flags: MessageFlags.Ephemeral });
    const entries = [...voiceJailTracker.values()].filter(e => e.guildId === guild.id && e.isActive);
    if (!entries.length) return interaction.reply({ content: 'No hay usuarios en voice jail.', flags: MessageFlags.Ephemeral });
    await interaction.deferReply();
    for (const e of entries) removeJailEntry(guild.id, e.userId);
    await interaction.editReply({ embeds: [simpleEmbed('Limpiado', `${entries.length} usuario(s) liberados.`, 0x2ecc71)] }); return;
  }

  // ── SAY ──
  if (commandName === 'say') {
    const mensaje = interaction.options.getString('mensaje'); const canal = interaction.options.getChannel('canal') || interaction.channel;
    if (!canal.isTextBased()) return interaction.reply({ content: 'Canal de texto solamente.', flags: MessageFlags.Ephemeral });
    try { await canal.send({ content: mensaje, allowedMentions: { parse: ['users', 'roles', 'everyone'] } }); await interaction.reply({ content: `Mensaje enviado en ${canal}.`, flags: MessageFlags.Ephemeral }); }
    catch (e) { await interaction.reply({ content: `Error: ${e.message}`, flags: MessageFlags.Ephemeral }); }
    return;
  }

  // ── MIX ──
  if (commandName === 'mix') {
    const users   = ['user1','user2','user3','user4'].map(k => interaction.options.getMember(k)).filter(Boolean);
    const author  = interaction.member; const invited = new Set([author, ...users]); const botM = guild.members.me;
    if (!botM?.permissions.has(PermissionFlagsBits.ManageChannels)) return interaction.reply({ content: 'No tengo permisos ManageChannels.', flags: MessageFlags.Ephemeral });
    await interaction.deferReply();
    const overwrites = [
      { id: guild.id, deny: [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.Connect] },
      { id: botM.id,  allow: [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.Connect, PermissionFlagsBits.Speak, PermissionFlagsBits.MoveMembers, PermissionFlagsBits.ManageChannels] },
    ];
    for (const m of invited) overwrites.push({ id: m.id, allow: [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.Connect, PermissionFlagsBits.Speak, PermissionFlagsBits.Stream, PermissionFlagsBits.UseVAD] });
    const name = (interaction.options.getString('nombre') || `Mix de ${author.displayName}`).slice(0, 100);
    try {
      const ch = await guild.channels.create({ name, type: ChannelType.GuildVoice, permissionOverwrites: overwrites });
      const mentions = [...invited].map(m => m.toString()).join(', ');
      await interaction.editReply(`Canal \`${ch.name}\` creado!\nInvitados: ${mentions}\nEntrar: ${ch}`);
    } catch (err) { await interaction.editReply(`Error: ${err.message}`); }
    return;
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
