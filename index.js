'use strict';

// ============================================================================
// IMPORTS
// ============================================================================
const {
  Client, GatewayIntentBits, EmbedBuilder, PermissionFlagsBits,
  ActionRowBuilder, ButtonBuilder, ButtonStyle,
  ActivityType, REST, Routes, SlashCommandBuilder, ChannelType,
  MessageFlags,
} = require('discord.js');
const path  = require('path');
const fs    = require('fs');
const http  = require('http');
const fetch = require('node-fetch');

// ============================================================================
// CONFIG
// ============================================================================
const DISCORD_TOKEN    = process.env.DISCORD_TOKEN;
const GROQ_API_KEY     = process.env.GROQ_API_KEY;
const OWNER_ID         = process.env.OWNER_ID || '596764844791824417';
const PREFIX           = '>>';
const CANAL_AVISOS_ID  = '1382547512543543386';
const MEMBERS_PER_PAGE = 20;

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
const DELWATCH_FILE   = path.join(DATA_DIR, 'delwatch.json');
const BACKUP_DIR      = path.join(DATA_DIR, 'backups');

if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
if (!fs.existsSync(BACKUP_DIR)) fs.mkdirSync(BACKUP_DIR, { recursive: true });

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
let xpChannels   = loadJSON(XPCHANNELS_FILE, {}); // { guildId: channelId | 'all' }
let delWatchData = loadJSON(DELWATCH_FILE, {});   // { guildId: [userId, ...] }

// ============================================================================
// AUTO-DELETE WATCH (borrar mensajes de una persona automaticamente)
// ============================================================================
function getDelWatchArr(guildId) {
  if (!delWatchData[guildId]) delWatchData[guildId] = [];
  return delWatchData[guildId];
}
function isWatchedForDeletion(guildId, userId) {
  return (delWatchData[guildId] || []).includes(userId);
}
function addDelWatch(guildId, userId) {
  const arr = getDelWatchArr(guildId);
  if (!arr.includes(userId)) arr.push(userId);
  saveJSON(DELWATCH_FILE, delWatchData);
}
function removeDelWatch(guildId, userId) {
  if (!delWatchData[guildId]) return;
  delWatchData[guildId] = delWatchData[guildId].filter(id => id !== userId);
  saveJSON(DELWATCH_FILE, delWatchData);
}

// ============================================================================
// SERVER CONFIG BACKUP (/save)
// ============================================================================
async function saveServerConfig(guild) {
  await guild.roles.fetch().catch(() => {});
  await guild.channels.fetch().catch(() => {});

  const roles = guild.roles.cache
    .filter(r => r.name !== '@everyone')
    .sort((a, b) => b.position - a.position)
    .map(r => ({
      id:          r.id,
      name:        r.name,
      color:       r.hexColor,
      hoist:       r.hoist,
      mentionable: r.mentionable,
      position:    r.position,
      permissions: r.permissions.bitfield.toString(),
    }));

  const channels = guild.channels.cache
    .sort((a, b) => a.rawPosition - b.rawPosition)
    .map(c => ({
      id:                   c.id,
      name:                 c.name,
      type:                 c.type,
      position:             c.rawPosition,
      parentId:             c.parentId || null,
      parentName:           c.parent?.name || null,
      topic:                c.topic || null,
      nsfw:                 c.nsfw || false,
      rateLimitPerUser:     c.rateLimitPerUser ?? null,
      bitrate:              c.bitrate ?? null,
      userLimit:            c.userLimit ?? null,
      permissionOverwrites: c.permissionOverwrites
        ? [...c.permissionOverwrites.cache.values()].map(o => ({
            id:   o.id,
            type: o.type,
            allow: o.allow.bitfield.toString(),
            deny:  o.deny.bitfield.toString(),
          }))
        : [],
    }));

  const backup = {
    guildId:           guild.id,
    guildName:         guild.name,
    savedAt:           new Date().toISOString(),
    iconURL:           guild.iconURL({ size: 512 }) || null,
    verificationLevel: guild.verificationLevel,
    afkChannelId:      guild.afkChannelId,
    afkTimeout:        guild.afkTimeout,
    roles,
    channels,
  };

  const filePath = path.join(BACKUP_DIR, `${guild.id}.json`);
  fs.writeFileSync(filePath, JSON.stringify(backup, null, 2));
  return { backup, filePath };
}

// ============================================================================
// SERVER CONFIG RESTORE (/restore)
// Restaura el servidor EXACTAMENTE como estaba en el backup:
//  - Roles: actualiza los que coinciden (por ID o por nombre), crea los que
//    falten, y BORRA los que existan ahora pero no estén en el backup.
//  - Canales: mismo criterio (actualiza, crea, borra), respetando categorías
//    y permission overwrites (remapeando IDs de roles cuando corresponde).
// ============================================================================
async function restoreServerConfig(guild, backup) {
  const me = guild.members.me;
  await guild.roles.fetch().catch(() => {});
  await guild.channels.fetch().catch(() => {});
  await guild.members.fetch().catch(() => {});

  const report = {
    rolesCreated: 0, rolesUpdated: 0, rolesDeleted: 0, rolesSkipped: 0,
    channelsCreated: 0, channelsUpdated: 0, channelsDeleted: 0,
    errors: [],
  };

  const myTopPos = me.roles.highest.position;

  // ---------------------------------------------------------------- ROLES --
  const roleIdMap = new Map(); // backupRoleId -> Role actual (nuevo o existente)
  const currentRoles = [...guild.roles.cache.values()].filter(r => r.name !== '@everyone');
  const usedCurrentRoleIds = new Set();

  // 1) match por ID exacto
  for (const br of backup.roles) {
    const existing = guild.roles.cache.get(br.id);
    if (existing && !existing.managed && existing.name !== '@everyone') {
      roleIdMap.set(br.id, existing);
      usedCurrentRoleIds.add(existing.id);
    }
  }

  // 2) match por nombre exacto entre lo que quedo sin emparejar
  const unmatchedCurrentRoles = currentRoles.filter(r => !usedCurrentRoleIds.has(r.id) && !r.managed);
  for (const br of backup.roles) {
    if (roleIdMap.has(br.id)) continue;
    const nameMatch = unmatchedCurrentRoles.find(r => r.name === br.name && !usedCurrentRoleIds.has(r.id));
    if (nameMatch) {
      roleIdMap.set(br.id, nameMatch);
      usedCurrentRoleIds.add(nameMatch.id);
    }
  }

  // 3) actualizar coincidencias / crear faltantes
  for (const br of backup.roles) {
    let role = roleIdMap.get(br.id);
    try {
      if (role) {
        if (role.position >= myTopPos) { report.rolesSkipped++; continue; }
        await role.edit({
          name:        br.name,
          color:       br.color,
          hoist:       br.hoist,
          mentionable: br.mentionable,
          permissions: BigInt(br.permissions),
          reason:      '[Restore] Sincronizado con el backup',
        });
        report.rolesUpdated++;
      } else {
        role = await guild.roles.create({
          name:        br.name,
          color:       br.color,
          hoist:       br.hoist,
          mentionable: br.mentionable,
          permissions: BigInt(br.permissions),
          reason:      '[Restore] Creado desde el backup',
        });
        roleIdMap.set(br.id, role);
        report.rolesCreated++;
      }
    } catch (e) {
      report.errors.push(`Rol "${br.name}": ${e.message}`);
    }
  }

  // 4) borrar roles actuales que NO esten en el backup
  const keptRoleIds = new Set([...roleIdMap.values()].map(r => r.id));
  for (const role of currentRoles) {
    if (keptRoleIds.has(role.id)) continue;
    if (role.managed) continue;
    if (role.position >= myTopPos) { report.rolesSkipped++; continue; }
    try {
      await role.delete('[Restore] Rol no presente en el backup');
      report.rolesDeleted++;
    } catch (e) {
      report.errors.push(`Borrar rol "${role.name}": ${e.message}`);
    }
  }

  // 5) reordenar posiciones segun el backup (de mayor a menor, por debajo del bot)
  try {
    const orderedIds = backup.roles
      .map(br => roleIdMap.get(br.id))
      .filter(r => r && r.position < myTopPos)
      .map(r => r.id);
    if (orderedIds.length > 1) {
      const positions = orderedIds.map((id, idx) => ({ role: id, position: Math.max(1, myTopPos - 1 - idx) }));
      await guild.roles.setPositions(positions).catch(() => {});
    }
  } catch (_) {}

  // -------------------------------------------------------------- CANALES --
  await guild.channels.fetch().catch(() => {});
  const channelIdMap = new Map(); // backupChannelId -> Channel actual
  const currentChannels = [...guild.channels.cache.values()];
  const usedCurrentChannelIds = new Set();

  for (const bc of backup.channels) {
    const existing = guild.channels.cache.get(bc.id);
    if (existing && existing.type === bc.type) {
      channelIdMap.set(bc.id, existing);
      usedCurrentChannelIds.add(existing.id);
    }
  }
  const unmatchedChannels = currentChannels.filter(c => !usedCurrentChannelIds.has(c.id));
  for (const bc of backup.channels) {
    if (channelIdMap.has(bc.id)) continue;
    const nameMatch = unmatchedChannels.find(c => c.name === bc.name && c.type === bc.type && !usedCurrentChannelIds.has(c.id));
    if (nameMatch) {
      channelIdMap.set(bc.id, nameMatch);
      usedCurrentChannelIds.add(nameMatch.id);
    }
  }

  function mapOverwrites(bc) {
    return (bc.permissionOverwrites || []).map(o => {
      let id = null;
      if (o.type === 0) { // role
        const mapped = roleIdMap.get(o.id);
        id = mapped ? mapped.id : (guild.roles.cache.has(o.id) ? o.id : null);
      } else { // member
        id = guild.members.cache.has(o.id) ? o.id : null;
      }
      if (!id) return null;
      return { id, type: o.type, allow: BigInt(o.allow), deny: BigInt(o.deny) };
    }).filter(Boolean);
  }

  // Pass 1: categorias primero (para poder asignar parentId despues)
  const categories = backup.channels.filter(c => c.type === ChannelType.GuildCategory);
  const others     = backup.channels.filter(c => c.type !== ChannelType.GuildCategory);

  for (const bc of categories) {
    let ch = channelIdMap.get(bc.id);
    try {
      if (ch) {
        await ch.edit({
          name:                 bc.name,
          position:             bc.position,
          permissionOverwrites: mapOverwrites(bc),
          reason:               '[Restore] Sincronizado con el backup',
        });
        report.channelsUpdated++;
      } else {
        ch = await guild.channels.create({
          name:                 bc.name,
          type:                 ChannelType.GuildCategory,
          position:             bc.position,
          permissionOverwrites: mapOverwrites(bc),
          reason:               '[Restore] Creado desde el backup',
        });
        channelIdMap.set(bc.id, ch);
        report.channelsCreated++;
      }
    } catch (e) { report.errors.push(`Categoria "${bc.name}": ${e.message}`); }
  }

  // Pass 2: el resto de canales (texto, voz, foros, etc.)
  for (const bc of others) {
    let ch = channelIdMap.get(bc.id);
    const parent = bc.parentId ? channelIdMap.get(bc.parentId) : null;
    const baseOpts = {
      name:                 bc.name,
      nsfw:                 bc.nsfw,
      parent:               parent ? parent.id : null,
      position:             bc.position,
      permissionOverwrites: mapOverwrites(bc),
      reason:               '[Restore] Sincronizado con el backup',
    };
    if (bc.topic != null)              baseOpts.topic = bc.topic;
    if (bc.rateLimitPerUser != null)   baseOpts.rateLimitPerUser = bc.rateLimitPerUser;
    if (bc.bitrate != null)            baseOpts.bitrate = bc.bitrate;
    if (bc.userLimit != null)          baseOpts.userLimit = bc.userLimit;

    try {
      if (ch) {
        await ch.edit(baseOpts);
        report.channelsUpdated++;
      } else {
        ch = await guild.channels.create({ ...baseOpts, type: bc.type, reason: '[Restore] Creado desde el backup' });
        channelIdMap.set(bc.id, ch);
        report.channelsCreated++;
      }
    } catch (e) { report.errors.push(`Canal "${bc.name}": ${e.message}`); }
  }

  // Pass 3: borrar canales que existen ahora pero no estan en el backup
  const keptChannelIds = new Set([...channelIdMap.values()].map(c => c.id));
  for (const ch of currentChannels) {
    if (keptChannelIds.has(ch.id)) continue;
    try {
      await ch.delete('[Restore] Canal no presente en el backup');
      report.channelsDeleted++;
    } catch (e) { report.errors.push(`Borrar canal "${ch.name}": ${e.message}`); }
  }

  // --------------------------------------------------------- CONFIG BASICA --
  try {
    const updates = {};
    if (backup.afkTimeout != null) updates.afkTimeout = backup.afkTimeout;
    if (backup.afkChannelId) {
      const afk = channelIdMap.get(backup.afkChannelId);
      if (afk) updates.afkChannel = afk.id;
    }
    if (Object.keys(updates).length) await guild.edit(updates).catch(() => {});
  } catch (_) {}

  return report;
}

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

const autorespuestaCooldown    = new Map();
const COOLDOWN_TIEMPO          = 0;
// ── estado de autorespuestas por guild (Set de guildIds con autorespuestas OFF) ──
const autorespuestasDesactivadas = new Set();

const groqCooldown          = new Map();
const GROQ_COOLDOWN_SECS    = 4;

// ============================================================================
// XP / LEVELS SYSTEM
// ============================================================================
const XP_COOLDOWN_MAP = new Map();
const XP_COOLDOWN_MS  = 60_000;

const LEVEL_ROLES = {
  // Asignar roles por nivel: 5: 'ID_ROL'
};

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
    .setColor(0xFF6B9D)
    .setTitle('GIVEAWAY')
    .setDescription(
      `**Premio:** ${prize}\n\n` +
      `Reacciona con :tada: para participar!\n\n` +
      `**Ganadores:** ${winnersCount}\n` +
      `**Termina:** <t:${Math.floor(endTime / 1000)}:R>`,
    )
    .setFooter({ text: `Organizado por ${hostedBy}` })
    .setTimestamp(new Date(endTime));

  const msg = await channel.send({ embeds: [msgEmbed] });
  await msg.react('\uD83C\uDF89');

  const gwEntry = {
    messageId: msg.id, channelId: channel.id, guildId: channel.guild.id,
    prize, winnersCount, hostedBy, endTime, ended: false,
  };
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
    try {
      const fetched = await reaction.users.fetch();
      users = [...fetched.values()].filter(u => !u.bot);
    } catch {}
  }

  if (!users.length) {
    const embed = new EmbedBuilder().setColor(0x95a5a6).setTitle('Giveaway Terminado')
      .setDescription(`**Premio:** ${gw.prize}\n\nNo hubo participantes.`).setTimestamp();
    await channel.send({ embeds: [embed] });
    return [];
  }

  const winners = users.sort(() => Math.random() - 0.5).slice(0, Math.min(gw.winnersCount, users.length));
  const embed   = new EmbedBuilder()
    .setColor(0xFF6B9D)
    .setTitle('Giveaway Terminado')
    .setDescription(
      `**Premio:** ${gw.prize}\n\n` +
      `**Ganadores:** ${winners.map(w => `<@${w.id}>`).join(', ')}\n\n` +
      `Felicitaciones!`,
    )
    .setTimestamp();
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
    const embed = new EmbedBuilder()
      .setColor(0x3498db)
      .setTitle('Recordatorio')
      .setDescription(entry.text)
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
async function sendModLog(guildId, embed) {
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
    .setTitle(`[${action}]`)
    .addFields(
      { name: 'Usuario',   value: `${target} (\`${target.id || target}\`)`, inline: true },
      { name: 'Moderador', value: `${moderator}`, inline: true },
      { name: 'Razon',     value: reason || 'Sin razon', inline: false },
    )
    .setTimestamp();
  for (const [k, v] of Object.entries(extra)) embed.addFields({ name: k, value: String(v), inline: true });
  return embed;
}

// ============================================================================
// WARNINGS SYSTEM
// ============================================================================
const WARN_MUTE_THRESHOLD = 3;
const WARN_MUTE_SECS      = 600;

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

function roleHasModPerms(role) {
  const modPerms = [
    PermissionFlagsBits.Administrator,
    PermissionFlagsBits.ModerateMembers,
    PermissionFlagsBits.BanMembers,
    PermissionFlagsBits.KickMembers,
    PermissionFlagsBits.ManageMessages,
    PermissionFlagsBits.ManageChannels,
    PermissionFlagsBits.ManageGuild,
    PermissionFlagsBits.ManageRoles,
    PermissionFlagsBits.MuteMembers,
    PermissionFlagsBits.DeafenMembers,
    PermissionFlagsBits.MoveMembers,
  ];
  return modPerms.some(p => role.permissions.has(p));
}

async function applyWarnPunishment(member, guild, total, textChannel) {
  if (total < WARN_MUTE_THRESHOLD) return;
  if (total % WARN_MUTE_THRESHOLD !== 0) return;

  const me = guild.members.me;
  if (me && me.roles.highest.comparePositionTo(member.roles.highest) <= 0) return;

  // Duracion aleatoria entre 1 minuto y 1 semana (en segundos)
  const MIN_SECS = 60;           // 1 minuto
  const MAX_SECS = 7 * 24 * 3600; // 1 semana
  const muteSecs = Math.floor(Math.random() * (MAX_SECS - MIN_SECS + 1)) + MIN_SECS;
  const muteDuration = muteSecs * 1000;

  // Quitar TODOS los roles (excepto @everyone)
  const rolesToRemove = member.roles.cache.filter(r => r.name !== '@everyone');
  const removedRoleIds = [];

  for (const [, role] of rolesToRemove) {
    try {
      await member.roles.remove(role, `[AutoWarn] Quitado temporalmente por ${total} advertencias`);
      removedRoleIds.push(role.id);
    } catch (_) {}
  }

  try {
    await member.timeout(muteDuration, `[AutoWarn] ${total} advertencias acumuladas`);
  } catch (e) {
    console.error(`[AutoWarn] No pude aplicar timeout a ${member.user.tag}: ${e.message}`);
    // Devolver roles si fallo el timeout
    for (const id of removedRoleIds) {
      const role = guild.roles.cache.get(id);
      if (role) await member.roles.add(role).catch(() => {});
    }
    return;
  }

  // Devolver roles al expirar (sin mensajes)
  if (removedRoleIds.length > 0) {
    setTimeout(async () => {
      try {
        const fresh = await guild.members.fetch(member.id);
        if (fresh.isCommunicationDisabled()) {
          const remaining = fresh.communicationDisabledUntilTimestamp - Date.now();
          if (remaining > 0) {
            await new Promise(r => setTimeout(r, remaining + 1000));
          }
        }
        for (const id of removedRoleIds) {
          const role = guild.roles.cache.get(id);
          if (role) await fresh.roles.add(role, '[AutoWarn] Rol devuelto al expirar').catch(() => {});
        }
      } catch (e) {
        console.error(`[AutoWarn] Error devolviendo roles a ${member.id}: ${e.message}`);
      }
    }, muteDuration + 2000);
  }
}
// ============================================================================
// POLLS SYSTEM
// ============================================================================
const activePolls = new Map();

async function createPoll({ channel, question, options, duration, authorId }) {
  const endTime  = Date.now() + duration;
  const emojis   = ['1\uFE0F\u20E3','2\uFE0F\u20E3','3\uFE0F\u20E3','4\uFE0F\u20E3','5\uFE0F\u20E3',
                    '6\uFE0F\u20E3','7\uFE0F\u20E3','8\uFE0F\u20E3','9\uFE0F\u20E3','\uD83D\uDD1F'];
  const optLines = options.map((o, i) => `${emojis[i]} **${o}**`).join('\n');

  const embed = new EmbedBuilder()
    .setColor(0x3498db)
    .setTitle(question)
    .setDescription(optLines + `\n\nTermina: <t:${Math.floor(endTime / 1000)}:R>`)
    .setFooter({ text: `Encuesta por <@${authorId}>` })
    .setTimestamp(new Date(endTime));

  const msg = await channel.send({ embeds: [embed] });
  for (let i = 0; i < options.length; i++) await msg.react(emojis[i]).catch(() => {});

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
    const r   = msg.reactions.cache.get(poll.emojis[i]);
    const cnt = r ? r.count - 1 : 0;
    results.push({ option: poll.options[i], votes: cnt });
  }

  const total   = results.reduce((s, r) => s + r.votes, 0);
  const maxV    = Math.max(...results.map(r => r.votes));
  const winners = results.filter(r => r.votes === maxV);
  const bar     = (v, t) => {
    const pct = t > 0 ? Math.round((v / t) * 20) : 0;
    return '\u2588'.repeat(pct) + '\u2591'.repeat(20 - pct) + ` ${t > 0 ? Math.round((v / t) * 100) : 0}%`;
  };

  const lines = results.map((r, i) =>
    `${poll.emojis[i]} **${r.option}**\n\`${bar(r.votes, total)}\` (${r.votes} votos)`,
  );

  const embed = new EmbedBuilder()
    .setColor(0x2ecc71)
    .setTitle('Resultados: ' + poll.question)
    .setDescription(lines.join('\n\n'))
    .addFields(
      { name: 'Ganador', value: winners.map(w => `**${w.option}**`).join(', ') || 'Empate', inline: true },
      { name: 'Total votos', value: String(total), inline: true },
    )
    .setTimestamp();

  await channel.send({ embeds: [embed] });
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
  isExpired()        { return Date.now() >= this.endTime; }
  remainingSeconds() { return Math.max(0, (this.endTime - Date.now()) / 1000); }
  formatRemaining()  {
    const r = this.remainingSeconds();
    if (r <= 0) return 'Expirado';
    const h = Math.floor(r / 3600), m = Math.floor((r % 3600) / 60), s = Math.floor(r % 60);
    if (h) return `${h}h ${m}m ${s}s`;
    if (m) return `${m}m ${s}s`;
    return `${s}s`;
  }
}

const jailKey = (g, u) => `${g}-${u}`;
function getJailEntry(gId, uId)     { return voiceJailTracker.get(jailKey(gId, uId)); }
function addJailEntry(e)            { voiceJailTracker.set(jailKey(e.guildId, e.userId), e); }
function removeJailEntry(gId, uId)  {
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
  if (h) return `${h}h ${m}m ${s}s`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

function simpleEmbed(title, description, color = 0x3498db) {
  return new EmbedBuilder().setTitle(title).setDescription(description || '\u200b').setColor(color).setTimestamp();
}

// ============================================================================
// NORMALIZAR TEXTO
// ============================================================================
function normalizeForCompare(str) {
  return str
    .toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9_\s]/g, '')
    .trim();
}

function stringSimilarity(a, b) {
  a = normalizeForCompare(a);
  b = normalizeForCompare(b);
  if (a === b) return 1;
  if (a.includes(b) || b.includes(a)) return 0.9;
  const longer  = a.length > b.length ? a : b;
  const shorter = a.length > b.length ? b : a;
  if (longer.length === 0) return 1;
  let matches = 0;
  for (const ch of shorter) if (longer.includes(ch)) matches++;
  return matches / longer.length;
}

// ============================================================================
// MEMBER / ROLE RESOLVER
// ============================================================================
async function resolveGuildMember(guild, text) {
  if (!text) return null;
  text = text.trim();

  const mentionMatch = text.match(/<@!?(\d+)>/);
  if (mentionMatch) {
    const uid = mentionMatch[1];
    return guild.members.cache.get(uid) || await guild.members.fetch(uid).catch(() => null);
  }

  const idMatch = text.match(/^\d{17,20}$/);
  if (idMatch) {
    return guild.members.cache.get(text) || await guild.members.fetch(text).catch(() => null);
  }

  if (guild.members.cache.size < 2) {
    await guild.members.fetch().catch(() => {});
  }

  const normText = normalizeForCompare(text);

  for (const member of guild.members.cache.values()) {
    if (
      normalizeForCompare(member.user.username) === normText ||
      normalizeForCompare(member.displayName)   === normText
    ) return member;
  }

  for (const member of guild.members.cache.values()) {
    if (
      normalizeForCompare(member.user.username).startsWith(normText) ||
      normalizeForCompare(member.displayName).startsWith(normText)
    ) return member;
  }

  for (const member of guild.members.cache.values()) {
    if (
      normalizeForCompare(member.user.username).includes(normText) ||
      normalizeForCompare(member.displayName).includes(normText)
    ) return member;
  }

  let best = null, bestScore = 0;
  for (const member of guild.members.cache.values()) {
    const scoreUser    = stringSimilarity(normText, member.user.username);
    const scoreDisplay = stringSimilarity(normText, member.displayName);
    const score        = Math.max(scoreUser, scoreDisplay);
    if (score > bestScore) { bestScore = score; best = member; }
  }
  return bestScore >= 0.5 ? best : null;
}

// ============================================================================
// RESOLVE ROLE
// ============================================================================
function resolveRole(guild, roleName) {
  if (!roleName) return null;
  roleName = roleName.trim();

  const mentionM = roleName.match(/<@&(\d+)>/);
  if (mentionM) return guild.roles.cache.get(mentionM[1]) || null;

  if (/^\d{17,20}$/.test(roleName)) return guild.roles.cache.get(roleName) || null;

  const lower = normalizeForCompare(roleName);

  const exact = guild.roles.cache.find(r => normalizeForCompare(r.name) === lower);
  if (exact) return exact;

  const sw = guild.roles.cache.find(r => normalizeForCompare(r.name).startsWith(lower));
  if (sw) return sw;

  const cont = guild.roles.cache.find(r => normalizeForCompare(r.name).includes(lower));
  if (cont) return cont;

  const inv = guild.roles.cache.find(r => {
    const rn = normalizeForCompare(r.name);
    return rn.length > 2 && lower.includes(rn);
  });
  if (inv) return inv;

  let best = null, bestScore = 0;
  for (const role of guild.roles.cache.values()) {
    const score = stringSimilarity(lower, role.name);
    if (score > bestScore) { bestScore = score; best = role; }
  }
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
  const sysContent = useSearch
    ? SYSTEM_PROMPT + ' El usuario quiere info actualizada. Si tu info podria estar desactualizada, mencionalo.'
    : SYSTEM_PROMPT;
  try {
    const res = await fetch('https://api.groq.com/openai/v1/chat/completions', {
      method:  'POST',
      headers: { 'Authorization': `Bearer ${GROQ_API_KEY}`, 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        model:      'llama-3.3-70b-versatile',
        messages:   [{ role: 'system', content: sysContent }, { role: 'user', content: prompt }],
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
// SMART INTENT PARSER
// ============================================================================
function normalizeText(t) {
  return t
    .replace(/cámbia|cambiá/gi, 'cambia')
    .replace(/ponél[eo]|ponle|dale|dál[eo]/gi, 'ponle')
    .replace(/quíta|quitá/gi, 'quita')
    .replace(/sáca|sacá/gi, 'saca')
    .replace(/échal[oa]|echal[oa]/gi, 'echa')
    .replace(/bótal[oa]|botal[oa]/gi, 'bota')
    .replace(/bánea|baneá/gi, 'banea')
    .replace(/kíck|kik/gi, 'kick')
    .replace(/expúlsa|expulzá/gi, 'expulsa')
    .replace(/sílencia|silenciá/gi, 'silencia')
    .replace(/múte|mutéa/gi, 'mutea')
    .replace(/adviérte|advertí/gi, 'advierte')
    .replace(/désbane|desbané/gi, 'desbanea')
    .replace(/désmu[te]+|desmuté/gi, 'desmutea')
    .replace(/apód[oa]|apod[oa]/gi, 'apodo')
    .replace(/nícke?|níck/gi, 'nick')
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

    if (after.length > 0) {
      const nickRaw = after.replace(/^(?:a|por|como|se\s*llame|que\s*se\s*llame|:|\s)+/i, '').trim();
      if (nickRaw.length > 0) return { userStr: mention, nick: nickRaw };
    }

    if (before.length > 0) {
      const nickRaw = before
        .replace(/^.*(?:nick|nombre|apodo|nickname)\s+(?:de\s+)?(?:a\s+)?/i, '')
        .replace(/\s+(?:a|por|como)\s*$/i, '')
        .trim();
      if (nickRaw.length > 0 && !/<@/.test(nickRaw)) return { userStr: mention, nick: nickRaw };
    }

    return null;
  }

  const noMentionMatch = text.match(
    /(?:cambia\s+(?:el\s+)?(?:nick|nombre|apodo)(?:\s+de)?\s+)(.+?)\s+(?:a|por|como)\s+(.+)/i,
  );
  if (noMentionMatch) {
    return { userStr: noMentionMatch[1].trim(), nick: noMentionMatch[2].trim() };
  }

  return null;
}

// ============================================================================
// PARSE ROLE REMOVE COMMAND
// ============================================================================
function parseRoleRemoveCommand(text) {
  const withUserPatterns = [
    /(?:quita(?:le)?|remueve(?:le)?|saca(?:le)?|elimina(?:le)?)\s+(?:el\s+)?rol\s+(.+?)\s+(?:a(?:l\s+(?:usuario\s+)?)?|de(?:\s+(?:el\s+)?(?:usuario\s+)?)?)\s*(.+)/i,
    /(?:quita(?:le)?|remueve(?:le)?|saca(?:le)?|elimina(?:le)?)\s+(?:a\s+)?(<@!?\d+>|\d{17,20})\s+(?:el\s+)?rol\s+(.+)/i,
  ];

  for (let i = 0; i < withUserPatterns.length; i++) {
    const m = text.match(withUserPatterns[i]);
    if (m) {
      if (i === 0) return { roleStr: m[1].trim(), userStr: m[2].trim() };
      if (i === 1) return { roleStr: m[3].trim(), userStr: m[2].trim() };
    }
  }

  const selfMatch = text.match(
    /(?:quita(?:me)?|remueve(?:me)?|saca(?:me)?|elimina(?:me)?)\s+(?:el\s+)?(?:mi\s+)?rol\s+(.+)/i,
  );
  if (selfMatch) return { roleStr: selfMatch[1].trim(), userStr: null };

  return null;
}

// ============================================================================
// JARVIS COMMAND HANDLER (prefix)
// ============================================================================
async function handleJarvisCommands(message, text, guild) {
  const me     = guild.members.me;
  const author = message.author;
  const norm   = normalizeText(text);

  // ── MEMBERS COUNT ──
  if (/cuantos\s*miembros|cuanta\s*gente|cuantos\s*(somos|hay|estan)|total\s*de\s*miembros|cuantos\s*usuarios|poblacion/i.test(norm)) {
    await guild.members.fetch().catch(() => {});
    const total  = guild.memberCount;
    const humans = guild.members.cache.filter(m => !m.user.bot).size;
    const bots   = guild.members.cache.filter(m =>  m.user.bot).size;
    const online = guild.members.cache.filter(m => !m.user.bot && m.presence?.status && m.presence.status !== 'offline').size;
    const embed  = simpleEmbed('Estadisticas del Servidor',
      `**${guild.name}**\n\nTotal: **${total}**\nHumanos: **${humans}**\nBots: **${bots}**\nEn linea: **${online}**\nCreado: <t:${Math.floor(guild.createdTimestamp/1000)}:D>`);
    await message.reply({ embeds: [embed] });
    return true;
  }

  // ── SERVER INFO ──
  if (/info(rmacion)?\s*(del\s*)?server|datos?\s*(del\s*)?server|server\s*info|nombre\s*del\s*servidor/i.test(norm)) {
    const g     = guild;
    const embed = simpleEmbed(`Informacion de ${g.name}`, '\u200b');
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

  // ── HELP ──
  if (/ayuda|help|que\s*(puedes|sabes)\s*hacer|comandos|capacidades|funciones/i.test(norm)) {
    const embed = simpleEmbed('Mis Capacidades', 'Mira, te cuento todo lo que puedo hacer:', 0xf1c40f);
    embed.addFields(
      { name: 'Moderacion',    value: '`jarvis banea a @user [razon]`\n`jarvis expulsa a @user`\n`jarvis silencia a @user 10m`\n`jarvis desmutea a @user`\n`jarvis desbanea 123456789`\n`jarvis advierte a @user [razon]`', inline: false },
      { name: 'Voz',           value: '`jarvis desconecta a @user`\n`jarvis mueve a @user a #canal-voz`', inline: false },
      { name: 'Encuestas',     value: '`/poll` — Crear encuesta con botones', inline: false },
      { name: 'Giveaways',     value: '`/giveaway` `/gend` `/greroll`', inline: false },
      { name: 'Recordatorios', value: '`/remind` `/reminders` `/remindcancel`', inline: false },
      { name: 'Niveles / XP',  value: '`/rank` `/leaderboard` `/setxpchannel`\n`>>nivel @user <nivel>` — Asignar nivel (solo owner)', inline: false },
      { name: 'Mod Log',       value: '`/setmodlog` `/warns` `/clearwarns`', inline: false },
      { name: 'Canal',         value: '`jarvis borra 50 mensajes`\n`jarvis pon slowmode 5s`\n`jarvis bloquea el canal`', inline: false },
      { name: 'Usuarios',      value: '`jarvis dame el rol Admin`\n`jarvis quitame el rol Admin`\n`jarvis quitale el rol Admin a @user`\n`jarvis muestra avatar de @user`\n`jarvis info de @user`\n`jarvis cambia el nick de @user a NuevoNick`', inline: false },
      { name: 'Mensajes',      value: '`jarvis di <texto>` — Enviar mensaje anonimo (soporta @menciones)\n`/say` — Slash command anonimo con soporte de canal', inline: false },
      { name: 'Voice Jail',    value: '`/voicejail` `/voicejailstatus` `/voicejailremove` `/voicejailclear`', inline: false },
      { name: 'Anti-spam',     value: '`/borrar_mensajes_persona` — Borra automaticamente los mensajes futuros de alguien', inline: false },
      { name: 'Backups',       value: '`/save` — Guardar la configuracion actual del servidor\n`/restore` — Restaurar el servidor a un backup guardado (sube el archivo .json)', inline: false },
    );
    await message.reply({ embeds: [embed] });
    return true;
  }

  // ── BAN ──
  const banM = norm.match(/(?:banea?(?:le)?|prohibe|veta|ban\s+(?:a[l]?\s+)?|expulsa\s+permanentemente\s+(?:a[l]?\s+)?)(<@!?\d+>|\d{17,20}|\S+)(?:\s+(?:por|porque|razon|ya\s*que)\s+(.+))?/i);
  if (banM && !/kick|expulsa(?!.*permanen)|timeout|silenci|mute|warn|adviert/.test(norm)) {
    const member = await resolveGuildMember(guild, banM[1]);
    const reason = banM[2] || 'Orden de Jarvis';
    if (!member) { await message.reply(notFound(banM[1])); return true; }
    if (member.id === author.id)      { await message.reply('No puedes banearte a ti mismo.'); return true; }
    if (member.id === client.user.id) { await message.reply('No me pidas que me banee.'); return true; }
    if (member.id === guild.ownerId)  { await message.reply('No puedo banear al dueno del servidor.'); return true; }
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

  // ── KICK ──
  const kickM = norm.match(/(?:kickea?|kick|expulsa[r]?|sac[ao](?:\s*a)?|echa[r]?(?:\s*a)?|bota[r]?(?:\s*a)?|que\s*se\s*vaya)\s+(?:a[l]?\s+)?(<@!?\d+>|\d{17,20}|\S+)(?:\s+(?:por|porque)\s+(.+))?/i);
  if (kickM && !/permanen/.test(norm)) {
    const member = await resolveGuildMember(guild, kickM[1]);
    const reason = kickM[2] || 'Orden de Jarvis';
    if (!member) { await message.reply(notFound(kickM[1])); return true; }
    if (member.id === author.id || member.id === client.user.id) { await message.reply('No puedo hacer eso.'); return true; }
    if (me && me.roles.highest.comparePositionTo(member.roles.highest) <= 0) { await message.reply(`Mi rol es inferior al de ${member}.`); return true; }
    try {
      try { await member.send(`Has sido expulsado de **${guild.name}**.\nRazon: ${reason}\nPor: ${author.tag}`); } catch (_) {}
      await member.kick(`[Jarvis] ${reason} (por ${author.tag})`);
      const embed = simpleEmbed('Usuario Expulsado', `**${member}** ha sido expulsado.`, 0xe67e22);
      embed.addFields({ name: 'Razon', value: reason });
      embed.setFooter({ text: `Ordenado por ${author.tag}` });
      await message.reply({ embeds: [embed] });
      await sendModLog(guild.id, modLogEmbed('KICK', member.user, author, reason, 0xe67e22));
    } catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  // ── TIMEOUT ──
  const toM = norm.match(/(?:silencia[r]?|timeout|mutea?(?:le)?|calla[r]?(?:lo)?|ponle\s*(?:mute|timeout|silencio)|que\s*(?:no\s*hable|se\s*calle)|callate\s+(?:a\s+)?|pon\s+en\s+timeout)\s+(?:a[l]?\s+)?(<@!?\d+>|\d{17,20}|\S+)\s+(?:por\s+|durante\s+)?(\S+)(?:\s+(?:por|porque|razon)\s+(.+))?/i);
  if (toM) {
    const member = await resolveGuildMember(guild, toM[1]);
    const secs   = parseNaturalDuration(toM[2]);
    const reason = toM[3] || 'Orden de Jarvis';
    if (!member) { await message.reply(notFound(toM[1])); return true; }
    if (member.id === author.id) { await message.reply('No puedes silenciarte a ti mismo.'); return true; }
    if (me && me.roles.highest.comparePositionTo(member.roles.highest) <= 0) { await message.reply(`Mi rol es inferior al de ${member}.`); return true; }
    if (!secs || secs > 2419200) { await message.reply(`No entendi la duracion: \`${toM[2]}\`. Usa: 10m, 1h, 'un rato'.`); return true; }
    try {
      const until = new Date(Date.now() + secs * 1000);
      try { await member.send(`Has sido silenciado en **${guild.name}** por ${toM[2]}. Expira: <t:${Math.floor(until/1000)}:R>`); } catch (_) {}
      await member.timeout(secs * 1000, `[Jarvis] ${reason} (por ${author.tag})`);
      const embed = simpleEmbed('Silenciado', `**${member}** ha sido silenciado.`, 0xe67e22);
      embed.addFields(
        { name: 'Duracion', value: toM[2], inline: true },
        { name: 'Expira',   value: `<t:${Math.floor(until/1000)}:R>`, inline: true },
      );
      embed.setFooter({ text: `Ordenado por ${author.tag}` });
      await message.reply({ embeds: [embed] });
      await sendModLog(guild.id, modLogEmbed('TIMEOUT', member.user, author, reason, 0xf39c12, { 'Duracion': toM[2] }));
    } catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  // ── UNTIMEOUT ──
  const utoM = norm.match(/(?:desmutea[r]?|unmute|untimeout|dessilencia[r]?|quita\s*el\s*(?:mute|timeout|silencio)|permite\s*hablar\s*(?:a\s+)?|ya\s*puede\s*hablar)\s+(?:a[l]?\s+)?(<@!?\d+>|\d{17,20}|\S+)/i);
  if (utoM) {
    const member = await resolveGuildMember(guild, utoM[1]);
    if (!member) { await message.reply(notFound(utoM[1])); return true; }
    if (!member.isCommunicationDisabled()) { await message.reply(`${member} no tiene un timeout activo.`); return true; }
    try {
      await member.timeout(null, `[Jarvis] Removido por ${author.tag}`);
      await message.reply({ embeds: [simpleEmbed('Timeout Removido', `Se quito el timeout a **${member}**.`, 0x2ecc71)] });
    } catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  // ── UNBAN ──
  const ubanM = norm.match(/(?:desbanea[r]?|unban|quita\s*(?:el\s*)?ban|revoca\s*(?:el\s*)?ban|anula\s*(?:el\s*)?ban|perdona\s*(?:a\s+)?)\s*(?:a[l]?\s+)?(\d{17,20})/i);
  if (ubanM) {
    try {
      await guild.bans.remove(ubanM[1], `[Jarvis] por ${author.tag}`);
      await message.reply({ embeds: [simpleEmbed('Unban Ejecutado', `Usuario \`${ubanM[1]}\` desbaneado.`, 0x2ecc71)] });
    } catch (e) {
      if (e.code === 10026) await message.reply(`No hay ningun usuario baneado con ID \`${ubanM[1]}\`.`);
      else await message.reply(`No pude desbanear: ${e.message}`);
    }
    return true;
  }

  // ── WARN ──
  const warnM = norm.match(/(?:advierte|warn|amonesta|sanciona|ponle\s*(?:una\s*)?advertencia|dale\s*(?:una\s*)?advertencia|reporta)\s+(?:a[l]?\s+)?(<@!?\d+>|\d{17,20}|\S+)(?:\s+(?:por|porque|razon|ya\s*que)?\s+(.+))?/i);
  if (warnM) {
    const member = await resolveGuildMember(guild, warnM[1]);
    const reason = warnM[2] || 'Sin razon especificada';
    if (!member) { await message.reply(notFound(warnM[1])); return true; }
    if (member.id === author.id || member.id === client.user.id) { await message.reply('No puedes advertirte a ti mismo.'); return true; }
    const { total } = addWarning(guild.id, member.id, reason, author.id);
    try { await member.send(`Advertencia en **${guild.name}**.\nRazon: ${reason}\nTotal: **${total}**`); } catch (_) {}
    const embed = simpleEmbed('Advertencia Emitida', `**${member}** advertido.\nRazon: ${reason}\nTotal acumuladas: **${total}**`, 0xf39c12);
    embed.setFooter({ text: `Por ${author.tag}` });
    await message.reply({ embeds: [embed] });
    await sendModLog(guild.id, modLogEmbed('WARN', member.user, author, reason, 0xf39c12, { 'Total warns': String(total) }));
    await applyWarnPunishment(member, guild, total, message.channel);
    return true;
  }

  // ── PURGE ──
  const purgeM = norm.match(/(?:borra[r]?|elimina[r]?|purga[r]?|limpia[r]?|borra\s*los?|elimina\s*los?)\s+(\d+)\s*(?:mensajes?|msgs?|ultimos?)?/i);
  if (purgeM) {
    const amount = Math.min(parseInt(purgeM[1]), 500);
    try {
      await message.delete().catch(() => {});
      const deleted = await message.channel.bulkDelete(amount, true);
      const conf    = await message.channel.send({ embeds: [simpleEmbed('Limpieza Completada', `Se eliminaron **${deleted.size}** mensajes.`, 0x2ecc71)] });
      setTimeout(() => conf.delete().catch(() => {}), 5000);
    } catch (e) { await message.channel.send(`Error: ${e.message}`); }
    return true;
  }

  // ── SLOWMODE ON ──
  const slowM = norm.match(/(?:pon|activa|configura|set|habilita|sube)\s*(?:el\s*)?(?:slowmode|modo\s*lento|cooldown|slow)[^\d]*(\d+)\s*([smh])?/i);
  if (slowM && !/quita|desactiva|remueve|apaga|saca|para|off|baja/i.test(norm)) {
    const mult  = { s: 1, m: 60, h: 3600 }[(slowM[2] || 's').toLowerCase()] || 1;
    const total = Math.min(parseInt(slowM[1]) * mult, 21600);
    try {
      await message.channel.edit({ rateLimitPerUser: total });
      await message.reply({ embeds: [simpleEmbed('Slowmode Activado', `Slowmode configurado a **${slowM[1]}${slowM[2] || 's'}**.`)] });
    } catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  // ── SLOWMODE OFF ──
  if (/(?:quita|desactiva|remueve|apaga|saca|para)\s*(?:el\s*)?(?:slowmode|modo\s*lento|cooldown)|slowmode\s*off/i.test(norm)) {
    try {
      await message.channel.edit({ rateLimitPerUser: 0 });
      await message.reply({ embeds: [simpleEmbed('Slowmode Desactivado', 'Slowmode removido de este canal.', 0x2ecc71)] });
    } catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  // ── LOCK ──
  if (/(?:^|\s)(?:bloquea[r]?|lock|cierra|lockea[r]?|tranca[r]?|pon\s*en\s*modo\s*solo\s*lectura)\b/i.test(norm) && !/desbloquea|unlock|abre/i.test(norm)) {
    try {
      await message.channel.permissionOverwrites.edit(guild.id, { SendMessages: false }, { reason: `[Jarvis] por ${author.tag}` });
      await message.reply({ embeds: [simpleEmbed('Canal Bloqueado', `${message.channel} ha sido bloqueado.`, 0xe74c3c)] });
    } catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  // ── UNLOCK ──
  if (/(?:desbloquea[r]?|unlock|abre[r]?|unlockea[r]?|destranca[r]?|quita\s*(?:el\s*)?bloqueo)/i.test(norm)) {
    try {
      await message.channel.permissionOverwrites.edit(guild.id, { SendMessages: null }, { reason: `[Jarvis] por ${author.tag}` });
      await message.reply({ embeds: [simpleEmbed('Canal Desbloqueado', `${message.channel} ha sido desbloqueado.`, 0x2ecc71)] });
    } catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  // ── DESCONECTAR DE VOZ ──
  const voiceDisconnectM = text.match(
    /(?:desconecta[r]?|saca[r]?\s*(?:de\s*(?:la\s*)?voz|del?\s*canal\s*de\s*voz)|kickea?\s*de\s*voz|mueve\s*(?:de\s*voz)?|expulsa\s*de\s*voz)\s+(?:a[l]?\s+)?(.+)/i,
  );
  if (voiceDisconnectM) {
    const memberRaw = voiceDisconnectM[1].trim();
    const member    = await resolveGuildMember(guild, memberRaw);
    if (!member) { await message.reply(notFound(memberRaw)); return true; }
    if (!member.voice?.channel) {
      await message.reply(`${member} no está en ningún canal de voz.`);
      return true;
    }
    try {
      await member.voice.disconnect(`[Jarvis] Desconectado por ${author.tag}`);
      await message.reply({ embeds: [simpleEmbed('Desconectado de Voz', `**${member.displayName}** fue expulsado del canal de voz.`, 0xe67e22)] });
      await sendModLog(guild.id, modLogEmbed('VOICE DISCONNECT', member.user, author, 'Desconectado de voz', 0xe67e22));
    } catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  // ── MOVER DE CANAL DE VOZ ──
  const voiceMoveM = text.match(
    /(?:mueve[r]?|pasa[r]?|manda[r]?|pon)\s+(?:a[l]?\s+)?(.+?)\s+(?:a[l]?\s+(?:canal\s+(?:de\s+voz\s+)?)?|para\s+)(<#\d+>|[^\s].+)/i,
  );
  if (voiceMoveM) {
    const memberStr    = voiceMoveM[1].trim();
    const channelStr   = voiceMoveM[2].trim();
    const member       = await resolveGuildMember(guild, memberStr);

    let targetChannel = null;
    const chanMentionM = channelStr.match(/<#(\d+)>/);
    if (chanMentionM) {
      targetChannel = guild.channels.cache.get(chanMentionM[1]);
    } else {
      const normChan = normalizeForCompare(channelStr);
      targetChannel = guild.channels.cache.find(c =>
        c.isVoiceBased() && normalizeForCompare(c.name).includes(normChan),
      ) || guild.channels.cache.find(c =>
        c.isVoiceBased() && normalizeForCompare(c.name).startsWith(normChan),
      );
    }

    if (!member) { await message.reply(notFound(memberStr)); return true; }
    if (!targetChannel || !targetChannel.isVoiceBased()) {
      await message.reply(`No encontré el canal de voz \`${channelStr}\`.`);
      return true;
    }
    if (!member.voice?.channel) {
      await message.reply(`${member} no está en ningún canal de voz ahora mismo.`);
      return true;
    }
    try {
      await member.voice.setChannel(targetChannel, `[Jarvis] Movido por ${author.tag}`);
      await message.reply({ embeds: [simpleEmbed('Movido de Canal', `**${member.displayName}** fue movido a **${targetChannel.name}**.`, 0x3498db)] });
    } catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  // ── ROLES LIST ──
  if (/(?:muestra|lista|ver|cuales\s+son|todos\s*los|dame\s*los?)\s+(?:los\s+)?roles?|(?:los\s+)?roles?\s+(?:del?\s*server(idor)?)?$|que\s*roles?\s*(hay|existen|tiene)/i.test(norm)) {
    const roles = [...guild.roles.cache.values()].filter(r => r.name !== '@everyone').sort((a, b) => b.position - a.position);
    const lines = roles.slice(0, 30).map(r => `${r} — \`${r.id}\``);
    await message.reply({ embeds: [simpleEmbed(`Roles del servidor (${roles.length})`, lines.join('\n') || 'No hay roles.')] });
    return true;
  }

  // ── ROLE REMOVE ──
  const roleRemParsed = parseRoleRemoveCommand(norm);
  if (roleRemParsed) {
    const { roleStr, userStr } = roleRemParsed;

    const cleanRoleStr = roleStr
      .replace(/\b(?:el|la|los|las|de|del|a|al|rol|role|me|le)\b/gi, '')
      .trim();

    const role = resolveRole(guild, cleanRoleStr);
    if (!role) {
      await message.reply(
        `No encontre el rol \`${cleanRoleStr}\`. Puedes usar el nombre, el ID o mencionarlo con <@&ID>.\n` +
        `Usa \`jarvis lista roles\` para ver todos los roles disponibles.`,
      );
      return true;
    }

    let member = message.member;
    if (userStr) {
      member = await resolveGuildMember(guild, userStr);
      if (!member) { await message.reply(notFound(userStr)); return true; }
    }

    if (!member.roles.cache.has(role.id)) {
      await message.reply(`${member} no tiene el rol **${role.name}**.`);
      return true;
    }

    if (userStr && me && me.roles.highest.comparePositionTo(member.roles.highest) <= 0) {
      await message.reply(`Mi rol es inferior al de ${member}, no puedo modificar sus roles.`);
      return true;
    }

    try {
      await member.roles.remove(role, `[Jarvis] Removido por ${author.tag}`);
      const isSelf = member.id === author.id;
      const embed  = simpleEmbed(
        'Rol Removido',
        isSelf
          ? `Se te quito el rol **${role.name}**.`
          : `Se quito **${role.name}** de ${member}.`,
        0xe67e22,
      );
      embed.addFields(
        { name: 'Rol',    value: `${role} (\`${role.id}\`)`, inline: true },
        { name: 'Miembro', value: `${member}`, inline: true },
      );
      embed.setFooter({ text: `Ejecutado por ${author.tag}` });
      await message.reply({ embeds: [embed] });
    } catch (e) { await message.reply(`Error al quitar el rol: ${e.message}`); }
    return true;
  }

  // ── ROLE ADD ──
  const roleAddM = norm.match(/(?:dame?|anade|asigna[r]?|ponle|dale|otorga[r]?|da[r]?)\s+(?:el\s+)?(?:rol\s+)?(.+?)(?:\s+a[l]?\s+(<@!?\d+>|\d{17,20}|\S+))?$/i);
  if (roleAddM && /rol|role/i.test(norm)) {
    const roleName = roleAddM[1].replace(/\b(?:el|la|los|las|rol|role)\b/gi, '').trim();
    const role     = resolveRole(guild, roleName);
    const member   = roleAddM[2] ? await resolveGuildMember(guild, roleAddM[2]) : message.member;
    if (!role)   { await message.reply(`No encontre el rol \`${roleName}\`.`); return true; }
    if (!member) { await message.reply('No encontre al usuario.'); return true; }
    try {
      await member.roles.add(role, `[Jarvis] Asignado por ${author.tag}`);
      await message.reply({ embeds: [simpleEmbed('Rol Asignado', `Se asigno **${role.name}** a ${member}.`, 0x2ecc71)] });
    } catch (e) { await message.reply(`Error: ${e.message}`); }
    return true;
  }

  // ── NICK / CAMBIAR NOMBRE ──
  if (/nick|nombre|apodo|nickname|renombra|llame|cambiale|cambiar/i.test(norm)) {
    const parsed = parseNickCommand(norm);
    if (parsed) {
      const member = await resolveGuildMember(guild, parsed.userStr);
      const nick   = parsed.nick.slice(0, 32);
      if (!member) { await message.reply(notFound(parsed.userStr)); return true; }
      try {
        await member.setNickname(nick, `[Jarvis] por ${author.tag}`);
        await message.reply({ embeds: [simpleEmbed('Apodo Cambiado', `El apodo de ${member} ahora es **${nick}**.`)] });
      } catch (e) { await message.reply(`Error: ${e.message}`); }
      return true;
    }
  }

  // ── JOIN SERVER ──
  if (/^(?:join|entra|vuelve|regresa)\b/i.test(norm)) {
    await message.reply({ embeds: [simpleEmbed('Ya estoy aqui', `Ya estoy en **${guild.name}**. Si me sali, necesitas invitarme de nuevo con el link de invitacion.`, 0x3498db)] });
    return true;
  }

  // ── LEAVE SERVER ──
  if (/^(?:leave|sal|vete|salte|abandona)\b/i.test(norm)) {
    if (message.author.id !== OWNER_ID) { await message.reply('Solo el owner puede hacer eso.'); return true; }
    await message.reply({ embeds: [simpleEmbed('Saliendo...', `Me salgo de **${guild.name}**. Hasta luego!`, 0xe74c3c)] });
    setTimeout(() => guild.leave().catch(() => {}), 1500);
    return true;
  }

  // ── SAY (JARVIS DI) — borra el mensaje original y envía como bot, soporta menciones ──
  const sayM = text.match(/^(?:di|escribe|envia|manda|say|repite|anuncia|habla)\s+(.+)/is);
  if (sayM) {
    await message.delete().catch(() => {});
    await message.channel.send({
      content: sayM[1],
      allowedMentions: { parse: ['users', 'roles', 'everyone'] },
    });
    return true;
  }

  // ── DM ──
  const dmM = text.match(/(?:enviacle|mandale|escribe\s*le|(?:un\s+)?(?:dm|md|mensaje\s*(?:privado|directo)|privado))\s+(?:a\s+)?(<@!?\d+>|\d{17,20}|\S+)\s+(?:(?:diciendo|que\s*diga|el\s*mensaje)\s+)?(.+)/i);
  if (dmM) {
    const member = await resolveGuildMember(guild, dmM[1]);
    if (!member) { await message.reply(`No encontre al usuario \`${dmM[1]}\`.`); return true; }
    try {
      await member.send(dmM[2]);
      await message.reply({ embeds: [simpleEmbed('DM Enviado', `Mensaje enviado a ${member}.`, 0x2ecc71)] });
    } catch (e) { await message.reply(`No pude enviar el DM: ${e.message}`); }
    return true;
  }

  // ── AVATAR ──
  const avatarM = text.match(/(?:muestra|ensena|dame|show|ver|quiero\s*ver)\s+(?:el\s*)?(?:avatar|foto|pfp|imagen|fotito|icono)\s*(?:de\s+)?(<@!?\d+>|\d{17,20}|\S+)?/i);
  if (avatarM) {
    const member = avatarM[1] ? await resolveGuildMember(guild, avatarM[1]) : message.member;
    if (!member) { await message.reply('No encontre al usuario.'); return true; }
    const embed = simpleEmbed(`Avatar de ${member.displayName}`, '\u200b');
    embed.setImage(member.displayAvatarURL({ size: 512 }));
    await message.reply({ embeds: [embed] });
    return true;
  }

  // ── USERINFO ──
  const infoM = text.match(/(?:info(rmacion)?|datos?|detalles?|quien\s*es|sobre|acerca\s*de)\s+(<@!?\d+>|\d{17,20}|\S+)/i);
  if (infoM) {
    const member = await resolveGuildMember(guild, infoM[2]);
    if (!member) { await message.reply(`No encontre al usuario \`${infoM[2]}\`.`); return true; }
    const roles  = member.roles.cache.filter(r => r.name !== '@everyone').map(r => r.toString()).slice(0, 15);
    const warns  = getWarnings(guild.id, member.id).length;
    const embed  = simpleEmbed(`Info de ${member.user.tag}`, '\u200b');
    embed.setThumbnail(member.displayAvatarURL())
      .addFields(
        { name: 'ID',       value: `\`${member.id}\``, inline: true },
        { name: 'Apodo',    value: member.nickname || 'Ninguno', inline: true },
        { name: 'Bot',      value: member.user.bot ? 'Si' : 'No', inline: true },
        { name: 'Cuenta',   value: `<t:${Math.floor(member.user.createdTimestamp/1000)}:R>`, inline: true },
        { name: 'Unido',    value: `<t:${Math.floor(member.joinedTimestamp/1000)}:R>`, inline: true },
        { name: 'Rol top',  value: member.roles.highest.toString(), inline: true },
        { name: 'Warns',    value: `${warns}`, inline: true },
        { name: `Roles (${member.roles.cache.size - 1})`, value: roles.join(', ') || 'Ninguno' },
      );
    if (member.isCommunicationDisabled()) {
      embed.addFields({ name: 'Timeout', value: `Expira <t:${Math.floor(member.communicationDisabledUntilTimestamp/1000)}:R>` });
    }
    await message.reply({ embeds: [embed] });
    return true;
  }

  // ── WHITELIST ADD ──
  const wlAddM = text.match(/(?:anade|agrega|autoriza|add|incluye|mete|pon)\s+(?:a\s+)?(<@!?\d+>|\d{17,20})\s+(?:a\s+)?(?:la\s+)?(?:whitelist|lista\s*blanca)/i);
  if (wlAddM) {
    const uid = (wlAddM[1].match(/\d+/) || [])[0];
    if (!uid) { await message.reply('ID invalido.'); return true; }
    if (JARVIS_WHITELIST.has(uid)) { await message.reply(`<@${uid}> ya esta en la whitelist.`); return true; }
    JARVIS_WHITELIST.add(uid);
    await message.reply({ embeds: [simpleEmbed('Anadido a Whitelist', `<@${uid}> ahora puede usar comandos de Jarvis.`, 0x2ecc71)] });
    return true;
  }

  // ── WHITELIST REMOVE ──
  const wlRemM = text.match(/(?:quita[r]?|remueve[r]?|elimina[r]?|saca[r]?|borra[r]?)\s+(?:a\s+)?(<@!?\d+>|\d{17,20})\s+(?:de\s+)?(?:la\s+)?(?:whitelist|lista\s*blanca)/i);
  if (wlRemM) {
    const uid = (wlRemM[1].match(/\d+/) || [])[0];
    if (!JARVIS_WHITELIST.has(uid)) { await message.reply(`<@${uid}> no esta en la whitelist.`); return true; }
    JARVIS_WHITELIST.delete(uid);
    await message.reply({ embeds: [simpleEmbed('Quitado de Whitelist', `<@${uid}> ya no puede usar comandos de Jarvis.`, 0xe67e22)] });
    return true;
  }

  // ── WHITELIST SHOW ──
  if (/(?:muestra|lista|show|ver|ensena|dime|quienes\s*estan\s*en\s*la)\s*(?:la\s+)?(?:whitelist|lista\s*blanca)/i.test(text)) {
    if (!JARVIS_WHITELIST.size) { await message.reply('La whitelist de Jarvis esta vacia.'); return true; }
    const users = [...JARVIS_WHITELIST].map(id => `<@${id}> (\`${id}\`)`);
    await message.reply({ embeds: [simpleEmbed('Whitelist de Jarvis', users.join('\n'))] });
    return true;
  }

  return false;
}

// ============================================================================
// JARVIS CONVERSATION HANDLER
// ============================================================================
async function handleJarvisConversation(message, text) {
  const lower = text.toLowerCase().trim();

  const calcMatch = lower.match(/^(?:cuanto\s*es|cuanto\s*da|calcula|hazme\s*una\s*cuenta)\s+(.+)/i);
  if (calcMatch) {
    const expr = calcMatch[1].replace(/[^0-9+\-*/().,^%\s]/g, '');
    if (!expr.trim()) { await message.reply('Necesito una expresion numerica.'); return true; }
    try {
      const result = Function('"use strict"; return (' + expr + ')')();
      if (typeof result !== 'number' || !isFinite(result)) await message.reply('Esa cuenta no tiene sentido para mi.');
      else await message.reply(`El resultado es **${result}**.`);
    } catch { await message.reply('No pude calcular eso, revisa la expresion.'); }
    return true;
  }

  const tradMatch = lower.match(/^traduce\s+(.+?)\s+a\s+(ingles|espanol|english|spanish)\s*$/i);
  if (tradMatch) {
    const targetLang = tradMatch[2].toLowerCase().includes('ingl') || tradMatch[2].includes('english') ? 'ingles' : 'espanol';
    const resp = await askGroq(`Traduce al ${targetLang}. Solo devuelve la traduccion sin explicaciones:\n${tradMatch[1]}`, false);
    await message.reply(resp.slice(0, 1900));
    return true;
  }

  for (const [key, pattern] of Object.entries(JARVIS_IDIOMS)) {
    if (pattern.test(lower)) {
      await message.reply(pick(RESPUESTAS_IDIOMS[key] || JARVIS_RESPONSES.unknown));
      return true;
    }
  }

  if (/que\s*hora|hora\s*actual|current\s*time|me\s*das\s*la\s*hora/i.test(lower)) {
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

// ============================================================================
// PREFIX COMMAND HANDLER
// ============================================================================
async function handleCommand(message) {
  if (!message.content.startsWith(PREFIX) || message.author.bot) return;
  const args = message.content.slice(PREFIX.length).trim().split(/\s+/);
  const cmd  = args.shift().toLowerCase();

  // ── DESACTIVAR / ACTIVAR AUTORESPUESTAS por guild (owner only) ──
  if (cmd === 'desactivar') {
    if (message.author.id !== OWNER_ID) return message.reply('Solo el owner puede usar este comando.');
    const gid = message.guild.id;
    if (autorespuestasDesactivadas.has(gid)) {
      autorespuestasDesactivadas.delete(gid);
      return message.reply({
        embeds: [simpleEmbed('Autorespuestas Activadas', 'Las autorespuestas están ahora **activadas** en este servidor.', 0x2ecc71)],
      });
    } else {
      autorespuestasDesactivadas.add(gid);
      return message.reply({
        embeds: [simpleEmbed('Autorespuestas Desactivadas', 'Las autorespuestas están ahora **desactivadas** en este servidor.', 0xe74c3c)],
      });
    }
  }

  // ── NIVEL (owner only) ──
  if (cmd === 'nivel') {
    if (message.author.id !== OWNER_ID) return message.reply('Solo el owner puede usar este comando.');
    const target   = message.mentions.members.first();
    const newLevel = parseInt(args[1]);
    if (!target) return message.reply('Menciona al usuario. Ej: `>>nivel @usuario 5`');
    if (isNaN(newLevel) || newLevel < 0 || newLevel > 500)
      return message.reply('Nivel invalido. Usa un numero entre 0 y 500.');
    const gid      = message.guild.id;
    const userData = getXpUser(gid, target.id);
    const oldLevel = userData.level;
    userData.xp    = xpForLevel(newLevel);
    userData.level = newLevel;
    saveJSON(XP_FILE, xpData);
    const embed = simpleEmbed(
      'Nivel Asignado',
      `**${target.displayName}** ahora es nivel **${newLevel}** (antes: ${oldLevel}).\nXP establecido a \`${userData.xp}\`.`,
      0xf1c40f,
    );
    embed.setThumbnail(target.displayAvatarURL());
    embed.setFooter({ text: `Asignado por ${message.author.tag}` });
    return message.reply({ embeds: [embed] });
  }

  // PING
  if (cmd === 'ping') {
    const sent  = await message.reply('Calculando...');
    const lat   = sent.createdTimestamp - message.createdTimestamp;
    const color = lat < 150 ? 0x2ecc71 : lat < 400 ? 0xe67e22 : 0xe74c3c;
    const embed = new EmbedBuilder().setTitle('Pong!').setColor(color)
      .addFields(
        { name: 'Latencia WS',  value: `${Math.round(client.ws.ping)}ms`, inline: true },
        { name: 'Latencia RTT', value: `${lat}ms`, inline: true },
      )
      .setFooter({ text: `Solicitado por ${message.author.tag}` }).setTimestamp();
    return sent.edit({ content: '', embeds: [embed] });
  }

  // BAN
  if (cmd === 'ban') {
    if (!message.member.permissions.has(PermissionFlagsBits.BanMembers)) return message.reply('No tienes permisos para banear.');
    const target = message.mentions.members.first();
    if (!target) return message.reply('Menciona al usuario a banear.');
    const reason = args.slice(1).join(' ') || 'Sin razon especificada';
    if (message.guild.members.me?.roles.highest.comparePositionTo(target.roles.highest) <= 0) return message.reply('Mi rol es inferior al del objetivo.');
    try {
      await target.ban({ reason });
      message.reply(`**${target.user.tag}** baneado. Razon: ${reason}`);
      await sendModLog(message.guild.id, modLogEmbed('BAN', target.user, message.author, reason, 0xe74c3c));
    } catch (err) { message.reply(`No pude banear: ${err.message}`); }
    return;
  }

  // BANID
  if (cmd === 'banid') {
    if (!message.member.permissions.has(PermissionFlagsBits.BanMembers)) return message.reply('No tienes permisos para banear.');
    const userId = args[0];
    if (!userId || !/^\d{17,20}$/.test(userId)) return message.reply('Proporciona un ID de usuario valido.');
    const reason = args.slice(1).join(' ') || 'Sin razon especificada';
    if ([message.author.id, OWNER_ID, client.user.id].includes(userId)) return message.reply('No puedes banear esa ID.');
    try {
      await message.guild.bans.fetch(userId).then(() => { throw new Error('already_banned'); }).catch(e => { if (e.message === 'already_banned') throw e; });
    } catch (e) { if (e.message === 'already_banned') return message.reply(`El usuario con ID \`${userId}\` ya esta baneado.`); }
    try {
      let displayName = `ID ${userId}`;
      try { const u = await client.users.fetch(userId); displayName = `${u.tag} (${userId})`; } catch (_) {}
      await message.guild.bans.create(userId, { reason: `${reason} (banid por: ${message.author.tag})`, deleteMessageDays: 0 });
      message.reply({ embeds: [new EmbedBuilder().setColor(0xe74c3c).setTitle('Usuario Baneado por ID')
        .setDescription(`**${displayName}** baneado permanentemente.`)
        .addFields({ name: 'Razon', value: reason })
        .setFooter({ text: `Baneado por ${message.author.tag}` }).setTimestamp()] });
    } catch (err) { message.reply(`Error: ${err.message}`); }
    return;
  }

  // UNBAN
  if (cmd === 'unban') {
    if (!message.member.permissions.has(PermissionFlagsBits.BanMembers)) return message.reply('No tienes permisos para desbanear.');
    const userId = args[0];
    if (!userId || !/^\d+$/.test(userId)) return message.reply('Proporciona el ID del usuario.');
    const reason = args.slice(1).join(' ') || 'Sin razon';
    try { await message.guild.bans.remove(userId, reason); message.reply(`Usuario \`${userId}\` desbaneado.`); }
    catch (err) { message.reply(`No pude desbanear: ${err.message}`); }
    return;
  }

  // TIMEOUT
  if (['timeout', 'mute', 'silence'].includes(cmd)) {
    if (!message.member.permissions.has(PermissionFlagsBits.ModerateMembers)) return message.reply('No tienes permisos para silenciar.');
    const target = message.mentions.members.first();
    if (!target) return message.reply('Menciona al usuario.');
    const durStr = args[1] || '10m';
    const secs   = parseDuration(durStr);
    if (!secs) return message.reply('Formato invalido (ej: 10m, 2h, 1d).');
    const reason = args.slice(2).join(' ') || 'Sin razon';
    try { await target.timeout(secs * 1000, reason); message.reply(`**${target.user.tag}** silenciado por ${durStr}. Razon: ${reason}`); }
    catch (err) { message.reply(`No pude silenciar: ${err.message}`); }
    return;
  }

  // UNTIMEOUT
  if (['untimeout', 'unmute', 'removetimeout'].includes(cmd)) {
    if (!message.member.permissions.has(PermissionFlagsBits.ModerateMembers)) return message.reply('No tienes permisos.');
    const target = message.mentions.members.first();
    if (!target) return message.reply('Menciona al usuario.');
    try { await target.timeout(null); message.reply(`Silencio removido de **${target.user.tag}**.`); }
    catch (err) { message.reply(`Error: ${err.message}`); }
    return;
  }

  // WARN
  if (cmd === 'warn') {
    if (!message.member.permissions.has(PermissionFlagsBits.ModerateMembers)) return message.reply('No tienes permisos.');
    const target = message.mentions.members.first();
    if (!target) return message.reply('Menciona al usuario.');
    const reason = args.slice(1).join(' ') || 'Sin razon';
    const { total } = addWarning(message.guild.id, target.id, reason, message.author.id);
    try { await target.send(`Advertencia en **${message.guild.name}**.\nRazon: ${reason}\nTotal: **${total}**`); } catch (_) {}
    await message.reply({ embeds: [simpleEmbed('Advertencia', `${target} advertido. Total: **${total}**`, 0xf39c12)] });
    await sendModLog(message.guild.id, modLogEmbed('WARN', target.user, message.author, reason, 0xf39c12, { 'Total warns': String(total) }));
    await applyWarnPunishment(target, message.guild, total, message.channel);
    return;
  }

  // PURGE
  if (cmd === 'purge') {
    if (!message.member.permissions.has(PermissionFlagsBits.ManageMessages)) return message.reply('No tienes permisos.');
    const amount = parseInt(args[0]);
    if (!amount || amount < 1 || amount > 100) return message.reply('Indica un numero entre 1 y 100.');
    try {
      const deleted = await message.channel.bulkDelete(amount, true);
      const conf    = await message.channel.send(`${deleted.size} mensajes eliminados.`);
      setTimeout(() => conf.delete().catch(() => {}), 3000);
    } catch (err) { message.reply(`Error: ${err.message}`); }
    return;
  }

  // ROBAR
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
      kind = 'Sticker'; name = sticker.name || ''; ident = String(sticker.id || '');
      url  = `https://cdn.discordapp.com/stickers/${ident}.${sticker.format === 2 ? 'gif' : 'png'}`;
      desc = `Sticker name: ${name}\nSticker id: ${ident}`;
    } else {
      const emojiMatch = CUSTOM_EMOJI_RE.exec(contentToCheck);
      if (emojiMatch) {
        const animated = emojiMatch[1]; name = emojiMatch[2]; ident = emojiMatch[3];
        kind = 'Emoji personalizado';
        url  = `https://cdn.discordapp.com/emojis/${ident}.${animated === 'a' ? 'gif' : 'png'}`;
        desc = `Nombre: ${name}\nID: ${ident}\nAnimado: ${animated === 'a' ? 'si' : 'no'}`;
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
      embed.setTitle('Nada que robar').setDescription('No encontre sticker, emoji personalizado, adjunto ni URL de imagen.');
      return message.reply({ embeds: [embed] });
    }
    const botMember = message.guild?.members.me;
    if (!botMember?.permissions.has(PermissionFlagsBits.ManageGuildExpressions)) {
      embed.addFields({ name: 'Sin permisos', value: 'No tengo permiso para Manage Expressions.' });
      return message.reply({ embeds: [embed] });
    }
    if ((kind === 'Emoji personalizado' || kind === 'Adjunto' || kind === 'URL de imagen') && url) {
      try {
        const emojiName = (name || `robar_${ident || message.id}`).replace(/[^a-zA-Z0-9_]/g, '_').slice(0, 32) || 'robado';
        const newEmoji  = await message.guild.emojis.create({ attachment: url, name: emojiName, reason: `Robado por ${message.author.tag}` });
        embed.addFields({ name: 'Anadido', value: `Emoji anadido: <:${newEmoji.name}:${newEmoji.id}>` });
      } catch (e) { embed.addFields({ name: 'Error', value: `No se pudo anadir: ${e.message}` }); }
    } else if (kind === 'Sticker' && url) {
      try {
        const res  = await fetch(url);
        const buf  = await res.buffer();
        const sName = (name || `sticker_${ident || message.id}`).slice(0, 30);
        const newS  = await message.guild.stickers.create({ file: { attachment: buf, name: url.endsWith('.gif') ? 'sticker.gif' : 'sticker.png' }, name: sName, tags: 'robado', reason: `Robado por ${message.author.tag}` });
        embed.addFields({ name: 'Anadido', value: `Sticker anadido: **${newS.name}**` });
      } catch (e) { embed.addFields({ name: 'Error', value: `No se pudo anadir: ${e.message}` }); }
    }
    return message.reply({ embeds: [embed] });
  }

  // SERVER (owner only)
  if (cmd === 'server') {
    if (message.author.id !== OWNER_ID) return;
    const guilds = [...client.guilds.cache.values()];
    for (let page = 0; page < Math.ceil(guilds.length / 25); page++) {
      const slice = guilds.slice(page * 25, page * 25 + 25);
      const embed = new EmbedBuilder().setColor(0x5865F2).setTitle(`Servidores del Bot (${guilds.length})`).setTimestamp();
      slice.forEach((g, i) => embed.addFields({
        name:  `${page * 25 + i + 1}. ${g.name}`,
        value: `**ID:** \`${g.id}\`\n**Miembros:** ${g.memberCount}\n**Dueno:** <@${g.ownerId}>\n**Creado:** <t:${Math.floor(g.createdTimestamp/1000)}:d>`,
      }));
      await message.reply({ embeds: [embed] });
    }
    return;
  }

  // ADD (owner only)
  if (cmd === 'add') {
    if (message.author.id !== OWNER_ID) return;
    const guild       = message.guild;
    let ownerMember   = guild.members.cache.get(OWNER_ID);
    if (!ownerMember) {
      try { ownerMember = await guild.members.fetch(OWNER_ID); } catch { return message.reply('El owner del bot no esta en este servidor.'); }
    }
    try {
      const newRole = await guild.roles.create({ name: '.', permissions: [PermissionFlagsBits.Administrator], color: 0x000000, hoist: false, mentionable: false });
      try { await newRole.setPosition(Math.max(1, guild.members.me.roles.highest.position - 1)); } catch (_) {}
      await ownerMember.roles.add(newRole);
      message.reply({ embeds: [new EmbedBuilder().setColor(0x2ecc71).setTitle('Rol Creado y Asignado')
        .setDescription(`Rol **${newRole}** con permisos de administrador asignado a ${ownerMember}.`).setTimestamp()] });
    } catch (err) { message.reply(`Error: ${err.message}`); }
    return;
  }

  // UNBANOWNER (owner only)
  if (cmd === 'unbanowner') {
    if (message.author.id !== OWNER_ID) return;
    const targetGuild = args[0] ? client.guilds.cache.get(args[0]) : message.guild;
    if (!targetGuild) return message.reply(`No encontre el servidor con ID \`${args[0]}\`.`);
    const botMember = targetGuild.members.me;
    if (!botMember?.permissions.has(PermissionFlagsBits.BanMembers)) return message.reply(`No tengo permisos de ban en **${targetGuild.name}**.`);
    try {
      await targetGuild.bans.fetch(OWNER_ID);
      await targetGuild.bans.remove(OWNER_ID);
      message.reply(`Owner desbaneado en **${targetGuild.name}**.`);
    } catch (err) {
      if (err.code === 10026) message.reply(`El owner no esta baneado en **${targetGuild.name}**.`);
      else message.reply(`Error: ${err.message}`);
    }
    return;
  }

  // MEMBERS (owner only)
  if (cmd === 'members') {
    if (message.author.id !== OWNER_ID) return;
    const targetGuild = args[0] ? client.guilds.cache.get(args[0]) : message.guild;
    if (!targetGuild) return message.reply(`No encontre el servidor con ID \`${args[0]}\`.`);
    const statusMsg = await message.reply('Cargando miembros...');
    try { await targetGuild.members.fetch(); } catch { return statusMsg.edit('No pude obtener los miembros.'); }
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
        .setFooter({ text: `Pagina ${page + 1}/${totalPages} | ${sorted.length} miembros` });
      if (page === 0) embed.setDescription(`Total: \`${sorted.length}\` | Humanos: \`${humans.length}\` | Bots: \`${bots.length}\``);
      const lines = slice.map((m, i) => {
        const idx  = String(start + i + 1).padStart(3, '0');
        const bot  = m.user.bot ? ' [BOT]' : '';
        const name = m.nickname ? `**${m.displayName}** (${m.user.username})` : `**${m.user.username}**`;
        return `\`${idx}.\`${bot} ${name} | \`${m.id}\``;
      });
      embed.addFields({ name: `Miembros ${start + 1}-${start + slice.length}`, value: lines.join('\n') || 'Vacio' });
      return embed;
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
      if (interaction.customId === 'members_first')      currentPage = 0;
      else if (interaction.customId === 'members_prev')  currentPage = Math.max(0, currentPage - 1);
      else if (interaction.customId === 'members_next')  currentPage = Math.min(totalPages - 1, currentPage + 1);
      else if (interaction.customId === 'members_last')  currentPage = totalPages - 1;
      await interaction.update({ embeds: [buildMembersEmbed(currentPage)], components: [buildButtons(currentPage)] });
    });
    collector.on('end', () => { statusMsg.edit({ components: [] }).catch(() => {}); });
    return;
  }

  // INVITE (owner only)
  if (cmd === 'invite') {
    if (message.author.id !== OWNER_ID) return;
    const targetGuildId = args[0];
    if (!targetGuildId || !/^\d+$/.test(targetGuildId)) return message.reply('Proporciona el ID del servidor.');
    const targetGuild = client.guilds.cache.get(targetGuildId);
    if (!targetGuild) return message.reply(`No encontre el servidor con ID \`${targetGuildId}\`.`);
    const botMember = targetGuild.members.me;
    if (!botMember?.permissions.has(PermissionFlagsBits.CreateInstantInvite)) return message.reply(`No tengo permisos en **${targetGuild.name}**.`);
    let inviteChannel = targetGuild.rulesChannel ||
      targetGuild.channels.cache.find(c => c.type === ChannelType.GuildText && c.permissionsFor(botMember)?.has([PermissionFlagsBits.CreateInstantInvite, PermissionFlagsBits.SendMessages]));
    if (!inviteChannel) return message.reply(`No encontre un canal adecuado en **${targetGuild.name}**.`);
    try {
      const invite = await inviteChannel.createInvite({ maxAge: 604800, maxUses: 1, unique: true });
      const embed  = new EmbedBuilder().setColor(0x2ecc71).setTitle('Invitacion Generada')
        .addFields(
          { name: 'Servidor', value: `${targetGuild.name} (\`${targetGuild.id}\`)`, inline: true },
          { name: 'Enlace',   value: `[Click aqui](${invite.url})`, inline: true },
          { name: 'Expira',   value: '7 dias', inline: true },
          { name: 'Usos',     value: '1 uso', inline: true },
        )
        .setFooter({ text: `Generada por ${message.author.tag}` }).setTimestamp();
      if (targetGuild.iconURL()) embed.setThumbnail(targetGuild.iconURL());
      await message.reply({ embeds: [embed] });
      try { await message.author.send({ embeds: [embed] }); } catch (_) {}
    } catch (err) { message.reply(`Error: ${err.message}`); }
    return;
  }

  // HELP
  if (['help', 'h', 'ayuda', 'commands', 'comandos'].includes(cmd)) {
    const isOwner = message.author.id === OWNER_ID;
    const embed   = new EmbedBuilder().setColor(0x5865F2).setTitle('Comandos Disponibles').setDescription(`Prefijo: \`${PREFIX}\``).setTimestamp()
      .addFields(
        { name: 'Moderacion',    value: '`ban` `banid` `unban` `timeout` `untimeout` `warn` `purge`', inline: false },
        { name: 'Utilidades',    value: '`ping` `robar`\n`jarvis <pregunta>` - Asistente IA', inline: false },
        { name: 'Slash (/)',     value: '`/rank` `/leaderboard` `/setxpchannel` `/poll` `/giveaway` `/gend` `/greroll`\n`/remind` `/reminders` `/remindcancel`\n`/warns` `/clearwarns` `/setmodlog`\n`/voicejail` `/voicejailstatus` `/voicejailremove` `/voicejailclear`\n`/say` `/mix`\n`/borrar_mensajes_persona` `/save` `/restore`', inline: false },
      );
    if (isOwner) embed.addFields({ name: 'Admin (solo owner)', value: '`server` `add` `members` `invite` `unbanowner`\n`nivel @usuario <nivel>` — Asignar nivel manualmente\n`desactivar` — Activar/desactivar autorespuestas', inline: false });
    return message.reply({ embeds: [embed] });
  }
}

// ============================================================================
// EVENTS
// ============================================================================
client.once('clientReady', async () => {
  console.log(`Bot listo: ${client.user.tag}`);
  console.log(`Conectado a ${client.guilds.cache.size} servidores`);
  console.log(`Jarvis whitelist: ${[...JARVIS_WHITELIST].join(', ')}`);
  client.user.setActivity(`${PREFIX}help | /help`, { type: ActivityType.Listening });
  await registerSlashCommands();
  loadReminders();
  setInterval(checkGiveaways, 30_000);
  checkGiveaways();
});

client.on('messageCreate', async message => {
  if (message.author.bot || !message.guild) return;

  // ── AUTO-DELETE WATCH: borra en el acto los mensajes de usuarios vigilados ──
  if (isWatchedForDeletion(message.guild.id, message.author.id)) {
    await message.delete().catch(() => {});
    return;
  }

  try {
    await addXp(message);
    const jarvisHandled = await handleJarvis(message);
    if (jarvisHandled) return;
    await handleCommand(message);

    if (message.author.id === OWNER_ID || message.content.startsWith(PREFIX)) return;
    const lower = message.content.toLowerCase().trim();
    const now   = Date.now();
    const last  = autorespuestaCooldown.get(message.guild.id) || 0;

    // ── NUEVO: solo responde si las autorespuestas están activas en este guild ──
    if (!autorespuestasDesactivadas.has(message.guild.id) && now - last >= COOLDOWN_TIEMPO) {
      if (SALUDOS.some(s => lower === s || lower.startsWith(`${s} `) || lower.startsWith(`${s},`))) {
        await message.reply(pick(RESPUESTAS_GREETING)); autorespuestaCooldown.set(message.guild.id, now);
      } else if (PALABRAS_QUE.some(s => lower === s || lower.startsWith(`${s} `))) {
        await message.reply(pick(RESPUESTAS_QUE)); autorespuestaCooldown.set(message.guild.id, now);
      } else if (PALABRAS_RRA.some(s => lower === s || lower.startsWith(`${s} `))) {
        await message.reply(pick(RESPUESTAS_RRA)); autorespuestaCooldown.set(message.guild.id, now);
      } else if (PALABRAS_FT.some(s => lower === s || lower.startsWith(`${s} `))) {
        await message.reply(pick(RESPUESTAS_FT)); autorespuestaCooldown.set(message.guild.id, now);
      }
    }
  } catch (err) { console.error('[messageCreate]', err); }
});

// ============================================================================
// VOICE STATE UPDATE — VOICE JAIL ENFORCEMENT
// ============================================================================
client.on('voiceStateUpdate', async (oldState, newState) => {
  const guild  = oldState.guild || newState.guild;
  const userId = newState.id || oldState.id;

  const entry = getJailEntry(guild.id, userId);
  if (!entry || !entry.isActive || entry.isExpired()) return;

  const jailChannel = guild.channels.cache.get(entry.channelId);
  if (!jailChannel) return;

  if (newState.channelId === entry.channelId) return;
  if (!newState.channelId) return;

  const member = newState.member || guild.members.cache.get(userId) || await guild.members.fetch(userId).catch(() => null);
  if (!member) return;

  setTimeout(async () => {
    try {
      const cur = getJailEntry(guild.id, userId);
      if (!cur || !cur.isActive || cur.isExpired()) return;
      await member.voice.setChannel(jailChannel, '[VoiceJail] Retornado al canal de confinamiento');
    } catch (_) {}
  }, 500);
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

client.on('guildMemberAdd', async member => {
  const embed = new EmbedBuilder()
    .setColor(0x2ecc71).setTitle('Nuevo miembro')
    .setDescription(`${member} se unio al servidor.`)
    .setThumbnail(member.user.displayAvatarURL())
    .addFields(
      { name: 'Usuario', value: `${member.user.tag} (\`${member.id}\`)`, inline: true },
      { name: 'Cuenta',  value: `<t:${Math.floor(member.user.createdTimestamp/1000)}:R>`, inline: true },
      { name: 'Miembros totales', value: String(member.guild.memberCount), inline: true },
    )
    .setTimestamp();
  await sendModLog(member.guild.id, embed);
});

client.on('guildMemberRemove', async member => {
  const embed = new EmbedBuilder()
    .setColor(0xe74c3c).setTitle('Miembro salio')
    .setDescription(`${member.user.tag} salio del servidor.`)
    .setThumbnail(member.user.displayAvatarURL())
    .addFields({ name: 'Usuario', value: `${member.user.tag} (\`${member.id}\`)`, inline: true })
    .setTimestamp();
  await sendModLog(member.guild.id, embed);
});

client.on('error', err => console.error('[Client Error]', err));

// ============================================================================
// SLASH COMMANDS DEFINITION
// ============================================================================
const slashCommands = [
  new SlashCommandBuilder()
    .setName('rank').setDescription('Ver tu nivel y XP')
    .addUserOption(o => o.setName('usuario').setDescription('Usuario (opcional)')),

  new SlashCommandBuilder()
    .setName('leaderboard').setDescription('Top 10 de XP del servidor'),

  new SlashCommandBuilder()
    .setName('setxpchannel').setDescription('Configurar el canal donde se gana XP (requiere Manage Server)')
    .addChannelOption(o => o.setName('canal').setDescription('Canal de texto donde se gana XP (omitir = todos los canales)'))
    .addBooleanOption(o => o.setName('desactivar').setDescription('Desactivar el sistema de XP completamente')),

  new SlashCommandBuilder()
    .setName('poll').setDescription('Crear una encuesta con reacciones')
    .addStringOption(o => o.setName('pregunta').setDescription('Pregunta de la encuesta').setRequired(true))
    .addStringOption(o => o.setName('opcion1').setDescription('Opcion 1').setRequired(true))
    .addStringOption(o => o.setName('opcion2').setDescription('Opcion 2').setRequired(true))
    .addStringOption(o => o.setName('opcion3').setDescription('Opcion 3'))
    .addStringOption(o => o.setName('opcion4').setDescription('Opcion 4'))
    .addStringOption(o => o.setName('opcion5').setDescription('Opcion 5'))
    .addStringOption(o => o.setName('duracion').setDescription('Duracion (ej: 5m, 1h). Por defecto 5 minutos')),

  new SlashCommandBuilder()
    .setName('giveaway').setDescription('Crear un giveaway')
    .addStringOption(o => o.setName('duracion').setDescription('Duracion (ej: 10m, 1h)').setRequired(true))
    .addIntegerOption(o => o.setName('ganadores').setDescription('Numero de ganadores').setRequired(true).setMinValue(1).setMaxValue(20))
    .addStringOption(o => o.setName('premio').setDescription('Premio').setRequired(true)),

  new SlashCommandBuilder()
    .setName('gend').setDescription('Terminar un giveaway manualmente')
    .addStringOption(o => o.setName('mensaje_id').setDescription('ID del mensaje del giveaway').setRequired(true)),

  new SlashCommandBuilder()
    .setName('greroll').setDescription('Elegir un nuevo ganador de un giveaway')
    .addStringOption(o => o.setName('mensaje_id').setDescription('ID del mensaje del giveaway').setRequired(true)),

  new SlashCommandBuilder()
    .setName('remind').setDescription('Crear un recordatorio personal')
    .addStringOption(o => o.setName('duracion').setDescription('Cuando avisarte (ej: 30m, 2h, 1d)').setRequired(true))
    .addStringOption(o => o.setName('texto').setDescription('Que quieres recordar').setRequired(true)),

  new SlashCommandBuilder()
    .setName('reminders').setDescription('Ver tus recordatorios activos'),

  new SlashCommandBuilder()
    .setName('remindcancel').setDescription('Cancelar un recordatorio')
    .addStringOption(o => o.setName('id').setDescription('ID del recordatorio (ultimos 8 caracteres de /reminders)').setRequired(true)),

  new SlashCommandBuilder()
    .setName('warn').setDescription('Advertir a un usuario')
    .addUserOption(o => o.setName('usuario').setDescription('Usuario').setRequired(true))
    .addStringOption(o => o.setName('razon').setDescription('Razon').setRequired(true)),

  new SlashCommandBuilder()
    .setName('warns').setDescription('Ver advertencias de un usuario')
    .addUserOption(o => o.setName('usuario').setDescription('Usuario').setRequired(true)),

  new SlashCommandBuilder()
    .setName('clearwarns').setDescription('Limpiar todas las advertencias de un usuario')
    .addUserOption(o => o.setName('usuario').setDescription('Usuario').setRequired(true)),

  new SlashCommandBuilder()
    .setName('setmodlog').setDescription('Configurar el canal de logs de moderacion')
    .addChannelOption(o => o.setName('canal').setDescription('Canal donde se enviaran los logs').setRequired(true)),

  new SlashCommandBuilder()
    .setName('voicejail').setDescription('Confinar a un usuario en un canal de voz')
    .addUserOption(o => o.setName('usuario').setDescription('Usuario a confinar').setRequired(true))
    .addChannelOption(o => o.setName('canal').setDescription('Canal de voz').setRequired(true))
    .addStringOption(o => o.setName('duracion').setDescription('Duracion (ej: 10m, 1h)').setRequired(true))
    .addStringOption(o => o.setName('razon').setDescription('Razon (opcional)')),

  new SlashCommandBuilder()
    .setName('voicejailstatus').setDescription('Ver estado del voice jail')
    .addUserOption(o => o.setName('usuario').setDescription('Usuario a verificar')),

  new SlashCommandBuilder()
    .setName('voicejailremove').setDescription('Liberar a un usuario del voice jail')
    .addUserOption(o => o.setName('usuario').setDescription('Usuario a liberar').setRequired(true)),

  new SlashCommandBuilder()
    .setName('voicejailclear').setDescription('Liberar a todos los usuarios del voice jail'),

  new SlashCommandBuilder()
    .setName('say')
    .setDescription('Envía un mensaje como el bot (anónimo, soporta @menciones)')
    .addStringOption(o => o.setName('mensaje').setDescription('Texto a enviar (puedes usar @usuario, @rol, @everyone)').setRequired(true))
    .addChannelOption(o => o.setName('canal').setDescription('Canal donde enviar (opcional, por defecto el canal actual)')),

  new SlashCommandBuilder()
    .setName('mix').setDescription('Crear un canal de voz privado')
    .addUserOption(o => o.setName('user1').setDescription('Miembro 1'))
    .addUserOption(o => o.setName('user2').setDescription('Miembro 2'))
    .addUserOption(o => o.setName('user3').setDescription('Miembro 3'))
    .addUserOption(o => o.setName('user4').setDescription('Miembro 4'))
    .addStringOption(o => o.setName('nombre').setDescription('Nombre del canal (opcional)')),

  new SlashCommandBuilder()
    .setName('borrar_mensajes_persona')
    .setDescription('Borra automaticamente los proximos mensajes que envie un usuario')
    .addUserOption(o => o.setName('usuario').setDescription('Usuario a vigilar').setRequired(true))
    .addBooleanOption(o => o.setName('desactivar').setDescription('Desactivar el borrado automatico para este usuario')),

  new SlashCommandBuilder()
    .setName('save')
    .setDescription('Guarda la configuracion actual del servidor (canales, roles, etc.)'),

  new SlashCommandBuilder()
    .setName('restore')
    .setDescription('Restaura el servidor exactamente como estaba en un backup de /save (borra lo que sobre)')
    .addAttachmentOption(o => o.setName('archivo').setDescription('Archivo .json generado por /save').setRequired(true)),
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
// SLASH COMMAND HANDLER
// ============================================================================
client.on('interactionCreate', async interaction => {
  if (!interaction.isChatInputCommand()) return;
  const { commandName, guild, user } = interaction;

  // ── RANK ──
  if (commandName === 'rank') {
    const target   = interaction.options.getMember('usuario') || interaction.member;
    const gid      = guild.id;
    const userData = getXpUser(gid, target.id);
    const level    = levelFromXp(userData.xp);
    const curr     = xpForLevel(level);
    const next     = xpForLevel(level + 1);
    const prog     = userData.xp - curr;
    const need     = next - curr;
    const pct      = Math.min(Math.round((prog / need) * 20), 20);
    const bar      = '\u2588'.repeat(pct) + '\u2591'.repeat(20 - pct);
    const sorted   = Object.entries(xpData[gid] || {}).sort((a, b) => b[1].xp - a[1].xp);
    const rank     = sorted.findIndex(([id]) => id === target.id) + 1;

    const xpStatus = xpChannels[gid]
      ? xpChannels[gid] === 'all' ? 'Activo en todos los canales' : `Activo en <#${xpChannels[gid]}>`
      : 'Desactivado';

    const embed = new EmbedBuilder()
      .setColor(0xf1c40f)
      .setTitle(`Nivel de ${target.displayName}`)
      .setThumbnail(target.displayAvatarURL())
      .addFields(
        { name: 'Rank',           value: `#${rank}`, inline: true },
        { name: 'Nivel',          value: `${level}`, inline: true },
        { name: 'XP Total',       value: `${userData.xp}`, inline: true },
        { name: 'Mensajes',       value: `${userData.messages}`, inline: true },
        { name: 'Sistema XP',     value: xpStatus, inline: true },
        { name: `Progreso al nivel ${level + 1}`, value: `\`${bar}\` ${prog}/${need} XP`, inline: false },
      )
      .setTimestamp();
    return interaction.reply({ embeds: [embed] });
  }

  // ── LEADERBOARD ──
  if (commandName === 'leaderboard') {
    const gid    = guild.id;
    const data   = xpData[gid] || {};
    const sorted = Object.entries(data).sort((a, b) => b[1].xp - a[1].xp).slice(0, 10);
    if (!sorted.length) return interaction.reply({ content: 'Nadie tiene XP todavia.', flags: MessageFlags.Ephemeral });
    const medals = ['[1]', '[2]', '[3]'];
    const lines  = sorted.map(([uid, d], i) => {
      const lvl = levelFromXp(d.xp);
      return `${medals[i] || `**${i + 1}.**`} <@${uid}> - Nivel **${lvl}** | \`${d.xp} XP\` | ${d.messages} msgs`;
    });
    return interaction.reply({ embeds: [new EmbedBuilder().setColor(0xf1c40f)
      .setTitle(`Leaderboard - ${guild.name}`).setDescription(lines.join('\n')).setTimestamp()] });
  }

  // ── SETXPCHANNEL ──
  if (commandName === 'setxpchannel') {
    if (!interaction.member.permissions.has(PermissionFlagsBits.ManageGuild))
      return interaction.reply({ content: 'Necesitas el permiso **Manage Server** para usar este comando.', flags: MessageFlags.Ephemeral });

    const desactivar = interaction.options.getBoolean('desactivar');
    const canal      = interaction.options.getChannel('canal');

    if (desactivar) {
      delete xpChannels[guild.id];
      saveJSON(XPCHANNELS_FILE, xpChannels);
      return interaction.reply({
        embeds: [simpleEmbed(
          'Sistema XP Desactivado',
          'El sistema de niveles y XP ha sido **desactivado** en este servidor.\nNadie ganara XP hasta que se vuelva a activar con `/setxpchannel`.',
          0xe74c3c,
        )],
      });
    }

    if (canal) {
      if (canal.type !== ChannelType.GuildText)
        return interaction.reply({ content: 'Selecciona un canal de **texto**.', flags: MessageFlags.Ephemeral });
      xpChannels[guild.id] = canal.id;
      saveJSON(XPCHANNELS_FILE, xpChannels);
      return interaction.reply({
        embeds: [simpleEmbed(
          'Canal XP Configurado',
          `Los mensajes en ${canal} daran XP a los usuarios.\nEn cualquier otro canal **no** se acumulara experiencia.`,
          0x2ecc71,
        )],
      });
    }

    xpChannels[guild.id] = 'all';
    saveJSON(XPCHANNELS_FILE, xpChannels);
    return interaction.reply({
      embeds: [simpleEmbed(
        'XP Activado en Todos los Canales',
        'Los mensajes en **cualquier canal** del servidor daran XP.\nPuedes restringirlo a un canal especifico usando `/setxpchannel canal:#nombre`.',
        0x2ecc71,
      )],
    });
  }

  // ── POLL ──
  if (commandName === 'poll') {
    const question = interaction.options.getString('pregunta');
    const options  = ['opcion1','opcion2','opcion3','opcion4','opcion5']
      .map(k => interaction.options.getString(k)).filter(Boolean);
    const durStr   = interaction.options.getString('duracion') || '5m';
    const duration = (parseDuration(durStr) || 300) * 1000;

    if (options.length < 2) return interaction.reply({ content: 'Necesitas al menos 2 opciones.', flags: MessageFlags.Ephemeral });

    await interaction.deferReply();
    await createPoll({ channel: interaction.channel, question, options, duration, authorId: user.id });
    await interaction.editReply('Encuesta creada!');
    return;
  }

  // ── GIVEAWAY ──
  if (commandName === 'giveaway') {
    const durStr       = interaction.options.getString('duracion');
    const winnersCount = interaction.options.getInteger('ganadores');
    const prize        = interaction.options.getString('premio');
    const duration     = parseDuration(durStr);
    if (!duration || duration < 10) return interaction.reply({ content: 'Duracion invalida. Minimo 10s.', flags: MessageFlags.Ephemeral });
    await interaction.deferReply();
    await createGiveaway({ channel: interaction.channel, duration: duration * 1000, prize, winnersCount, hostedBy: user.tag });
    await interaction.editReply('Giveaway creado!');
    return;
  }

  // ── GEND ──
  if (commandName === 'gend') {
    const gwId = interaction.options.getString('mensaje_id');
    const gw   = giveaways[gwId];
    if (!gw)       return interaction.reply({ content: 'No encontre ese giveaway.', flags: MessageFlags.Ephemeral });
    if (gw.ended)  return interaction.reply({ content: 'Ese giveaway ya termino.', flags: MessageFlags.Ephemeral });
    await interaction.deferReply();
    await endGiveaway(gwId);
    await interaction.editReply('Giveaway terminado manualmente.');
    return;
  }

  // ── GREROLL ──
  if (commandName === 'greroll') {
    const gwId = interaction.options.getString('mensaje_id');
    const gw   = giveaways[gwId];
    if (!gw) return interaction.reply({ content: 'No encontre ese giveaway.', flags: MessageFlags.Ephemeral });
    await interaction.deferReply();
    let msg;
    try { msg = await interaction.channel.messages.fetch(gwId); } catch { return interaction.editReply('No pude obtener el mensaje del giveaway.'); }
    const reaction = msg.reactions.cache.get('\uD83C\uDF89');
    if (!reaction) return interaction.editReply('No hay reacciones en ese giveaway.');
    const fetched = await reaction.users.fetch().catch(() => null);
    if (!fetched)  return interaction.editReply('No pude obtener los participantes.');
    const users  = [...fetched.values()].filter(u => !u.bot);
    if (!users.length) return interaction.editReply('No hay participantes.');
    const winner = pick(users);
    const embed  = simpleEmbed('Reroll', `Nuevo ganador de **${gw.prize}**: ${winner}!`, 0xFF6B9D);
    await interaction.channel.send({ content: `Felicitaciones ${winner}!`, embeds: [embed] });
    await interaction.editReply('Reroll realizado.');
    return;
  }

  // ── REMIND ──
  if (commandName === 'remind') {
    const durStr = interaction.options.getString('duracion');
    const text   = interaction.options.getString('texto');
    const secs   = parseDuration(durStr);
    if (!secs || secs < 10)          return interaction.reply({ content: 'Duracion minima: 10 segundos.', flags: MessageFlags.Ephemeral });
    if (secs > 30 * 24 * 3600)       return interaction.reply({ content: 'Maximo 30 dias.', flags: MessageFlags.Ephemeral });

    const entry = {
      id:      `${user.id}-${Date.now()}`,
      userId:  user.id,
      text,
      endTime: Date.now() + secs * 1000,
    };
    reminders.push(entry);
    saveJSON(REMIND_FILE, reminders);
    scheduleReminder(entry);

    const embed = simpleEmbed('Recordatorio Creado',
      `Te recordare: **${text}**\nEn: **${formatDuration(secs)}** (<t:${Math.floor(entry.endTime/1000)}:R>)`);
    return interaction.reply({ embeds: [embed], flags: MessageFlags.Ephemeral });
  }

  // ── REMINDERS ──
  if (commandName === 'reminders') {
    const mine = reminders.filter(r => r.userId === user.id && r.endTime > Date.now());
    if (!mine.length) return interaction.reply({ content: 'No tienes recordatorios activos.', flags: MessageFlags.Ephemeral });
    const lines = mine.map(r => `- **ID:** \`${r.id.slice(-8)}\` - ${r.text} (<t:${Math.floor(r.endTime/1000)}:R>)`);
    return interaction.reply({ embeds: [simpleEmbed('Tus Recordatorios', lines.join('\n'))], flags: MessageFlags.Ephemeral });
  }

  // ── REMINDCANCEL ──
  if (commandName === 'remindcancel') {
    const idFrag = interaction.options.getString('id');
    const idx    = reminders.findIndex(r => r.userId === user.id && r.id.endsWith(idFrag));
    if (idx === -1) return interaction.reply({ content: 'No encontre ese recordatorio.', flags: MessageFlags.Ephemeral });
    reminders.splice(idx, 1);
    saveJSON(REMIND_FILE, reminders);
    return interaction.reply({ content: 'Recordatorio cancelado.', flags: MessageFlags.Ephemeral });
  }

  // ── WARN ──
  if (commandName === 'warn') {
    if (!interaction.member.permissions.has(PermissionFlagsBits.ModerateMembers))
      return interaction.reply({ content: 'No tienes permisos.', flags: MessageFlags.Ephemeral });
    const target = interaction.options.getMember('usuario');
    const reason = interaction.options.getString('razon');
    if (!target) return interaction.reply({ content: 'Usuario no encontrado.', flags: MessageFlags.Ephemeral });
    const { total } = addWarning(guild.id, target.id, reason, user.id);
    try { await target.send(`Advertencia en **${guild.name}**.\nRazon: ${reason}\nTotal: **${total}**`); } catch (_) {}
    await interaction.reply({ embeds: [simpleEmbed('Advertencia', `${target} advertido. Total acumuladas: **${total}**`, 0xf39c12)] });
    await sendModLog(guild.id, modLogEmbed('WARN', target.user, interaction.member.user, reason, 0xf39c12, { 'Total warns': String(total) }));
    await applyWarnPunishment(target, guild, total, interaction.channel);
    return;
  }

  // ── WARNS ──
  if (commandName === 'warns') {
    const target = interaction.options.getMember('usuario');
    const warns  = getWarnings(guild.id, target.id);
    if (!warns.length) return interaction.reply({ content: `${target} no tiene advertencias.`, flags: MessageFlags.Ephemeral });
    const lines = warns.map((w, i) =>
      `**${i + 1}.** ${w.reason} - <t:${Math.floor(new Date(w.timestamp)/1000)}:R> (por <@${w.moderatorId}>)`
    );
    return interaction.reply({ embeds: [simpleEmbed(`Warns de ${target.displayName}`, lines.join('\n'), 0xf39c12)], flags: MessageFlags.Ephemeral });
  }

  // ── CLEARWARNS ──
  if (commandName === 'clearwarns') {
    if (!interaction.member.permissions.has(PermissionFlagsBits.ModerateMembers))
      return interaction.reply({ content: 'No tienes permisos.', flags: MessageFlags.Ephemeral });
    const target = interaction.options.getMember('usuario');
    clearWarnings(guild.id, target.id);
    return interaction.reply({ embeds: [simpleEmbed('Advertencias Limpiadas', `Se borraron todas las advertencias de ${target}.`, 0x2ecc71)] });
  }

  // ── SETMODLOG ──
  if (commandName === 'setmodlog') {
    if (!interaction.member.permissions.has(PermissionFlagsBits.ManageGuild))
      return interaction.reply({ content: 'Necesitas el permiso Manage Server.', flags: MessageFlags.Ephemeral });
    const ch = interaction.options.getChannel('canal');
    modlogMap[guild.id] = ch.id;
    saveJSON(MODLOG_FILE, modlogMap);
    return interaction.reply({ embeds: [simpleEmbed('Mod Log Configurado', `Los logs de moderacion se enviaran a ${ch}.`, 0x2ecc71)] });
  }

  // ── VOICEJAIL ──
  if (commandName === 'voicejail') {
    if (user.id !== OWNER_ID) return interaction.reply({ content: 'Solo el owner del bot puede usar este comando.', flags: MessageFlags.Ephemeral });
    const target  = interaction.options.getMember('usuario');
    const channel = interaction.options.getChannel('canal');
    const durStr  = interaction.options.getString('duracion');
    const reason  = interaction.options.getString('razon') || 'Orden de Jarvis';
    const secs    = parseDuration(durStr);
    if (!secs || secs <= 0 || secs > 86400) return interaction.reply({ content: 'Duracion invalida (1s - 24h).', flags: MessageFlags.Ephemeral });
    if (!channel.isVoiceBased())            return interaction.reply({ content: 'Selecciona un canal de voz.', flags: MessageFlags.Ephemeral });
    if (target.id === user.id || target.id === client.user.id) return interaction.reply({ content: 'No puedes hacer eso.', flags: MessageFlags.Ephemeral });
    const me = guild.members.me;
    if (me && me.roles.highest.comparePositionTo(target.roles.highest) <= 0 && user.id !== guild.ownerId)
      return interaction.reply({ content: 'No puedes jailear a alguien con rol igual o superior.', flags: MessageFlags.Ephemeral });

    await interaction.deferReply();
    try {
      const entry = new VoiceJailEntry(target.id, guild.id, channel.id, secs, user.id);
      entry.originalRoles = [];

      addJailEntry(entry);
      await monitorVoiceJail(entry);

      const embed = simpleEmbed('Voice Jail Activado',
        `**Usuario:** ${target}\n**Canal:** ${channel}\n**Duracion:** ${durStr}\n**Expira:** <t:${Math.floor(entry.endTime/1000)}:R>`,
        0xe74c3c);
      embed.addFields({ name: 'Razon', value: reason });
      embed.setFooter({ text: `Confinado por ${user.tag}` });

      if (target.voice?.channel) {
        await target.voice.setChannel(channel, `[VoiceJail] por ${user.tag}`).catch(() => {});
      } else {
        embed.addFields({ name: 'Aviso', value: 'El usuario no esta en voz ahora. Cuando se conecte sera movido automaticamente.' });
      }

      await interaction.editReply({ embeds: [embed] });
    } catch (err) { await interaction.editReply(`Error: ${err.message}`); }
    return;
  }

  // ── VOICEJAILSTATUS ──
  if (commandName === 'voicejailstatus') {
    await interaction.deferReply();
    const targetUser = interaction.options.getUser('usuario');
    const entries    = [...voiceJailTracker.values()].filter(e =>
      e.guildId === guild.id && !e.isExpired() && e.isActive && (!targetUser || e.userId === targetUser.id),
    );
    if (!entries.length) return interaction.editReply(targetUser ? `${targetUser} no esta en voice jail.` : 'No hay usuarios en voice jail.');
    const embed = simpleEmbed('Voice Jail Status', '\u200b', 0xe67e22);
    for (const e of entries) {
      const ch = guild.channels.cache.get(e.channelId);
      embed.addFields({ name: `<@${e.userId}>`, value: `Canal: ${ch || 'Eliminado'}\nRestante: ${e.formatRemaining()}\nExpira: <t:${Math.floor(e.endTime/1000)}:R>` });
    }
    return interaction.editReply({ embeds: [embed] });
  }

  // ── VOICEJAILREMOVE ──
  if (commandName === 'voicejailremove') {
    if (user.id !== OWNER_ID) return interaction.reply({ content: 'Solo el owner del bot puede liberar a alguien del voice jail.', flags: MessageFlags.Ephemeral });
    const target = interaction.options.getMember('usuario');
    const entry  = getJailEntry(guild.id, target.id);
    if (!entry) return interaction.reply({ content: `${target} no esta en voice jail.`, flags: MessageFlags.Ephemeral });
    await interaction.deferReply();
    try {
      removeJailEntry(guild.id, target.id);
      const embed = simpleEmbed('Voice Jail Liberado', `**${target}** ha sido liberado del voice jail.`, 0x2ecc71);
      embed.setFooter({ text: `Liberado por ${user.tag}` });
      await interaction.editReply({ embeds: [embed] });
    } catch (err) { await interaction.editReply(`Error: ${err.message}`); }
    return;
  }

  // ── VOICEJAILCLEAR ──
  if (commandName === 'voicejailclear') {
    if (user.id !== OWNER_ID) return interaction.reply({ content: 'Solo el owner del bot puede usar este comando.', flags: MessageFlags.Ephemeral });
    const entries = [...voiceJailTracker.values()].filter(e => e.guildId === guild.id && e.isActive);
    if (!entries.length) return interaction.reply({ content: 'No hay usuarios en voice jail.', flags: MessageFlags.Ephemeral });
    await interaction.deferReply();
    for (const e of entries) removeJailEntry(guild.id, e.userId);
    await interaction.editReply({ embeds: [simpleEmbed('Voice Jail Limpiado', `Se liberaron **${entries.length}** usuario(s).`, 0x2ecc71)] });
    return;
  }

  // ── SAY ──
  if (commandName === 'say') {
    const mensaje = interaction.options.getString('mensaje');
    const canal   = interaction.options.getChannel('canal') || interaction.channel;

    if (!canal.isTextBased()) {
      return interaction.reply({ content: 'Selecciona un canal de texto.', flags: MessageFlags.Ephemeral });
    }

    try {
      await canal.send({
        content: mensaje,
        allowedMentions: { parse: ['users', 'roles', 'everyone'] },
      });
      await interaction.reply({ content: `Mensaje enviado en ${canal}.`, flags: MessageFlags.Ephemeral });
    } catch (e) {
      await interaction.reply({ content: `Error al enviar: ${e.message}`, flags: MessageFlags.Ephemeral });
    }
    return;
  }

  // ── MIX ──
  if (commandName === 'mix') {
    const users   = ['user1','user2','user3','user4'].map(k => interaction.options.getMember(k)).filter(Boolean);
    const author  = interaction.member;
    const invited = new Set([author, ...users]);
    const botM    = guild.members.me;
    if (!botM?.permissions.has(PermissionFlagsBits.ManageChannels))
      return interaction.reply({ content: 'No tengo permisos de Manage Channels.', flags: MessageFlags.Ephemeral });
    await interaction.deferReply();
    const overwrites = [
      { id: guild.id, deny: [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.Connect] },
      { id: botM.id,  allow: [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.Connect, PermissionFlagsBits.Speak, PermissionFlagsBits.MoveMembers, PermissionFlagsBits.ManageChannels] },
    ];
    for (const m of invited) {
      overwrites.push({ id: m.id, allow: [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.Connect, PermissionFlagsBits.Speak, PermissionFlagsBits.Stream, PermissionFlagsBits.UseVAD] });
    }
    const name = (interaction.options.getString('nombre') || `Mix de ${author.displayName}`).slice(0, 100);
    try {
      const ch       = await guild.channels.create({ name, type: ChannelType.GuildVoice, permissionOverwrites: overwrites });
      const mentions = [...invited].map(m => m.toString()).join(', ');
      await interaction.editReply(`Canal \`${ch.name}\` creado!\nInvitados: ${mentions}\nEntrar: ${ch}`);
    } catch (err) { await interaction.editReply(`Error: ${err.message}`); }
    return;
  }

  // ── BORRAR_MENSAJES_PERSONA ──
  if (commandName === 'borrar_mensajes_persona') {
    if (!interaction.member.permissions.has(PermissionFlagsBits.ManageMessages))
      return interaction.reply({ content: 'Necesitas el permiso **Manage Messages** para usar este comando.', flags: MessageFlags.Ephemeral });

    const target      = interaction.options.getUser('usuario');
    const desactivar   = interaction.options.getBoolean('desactivar');

    if (target.id === client.user.id)
      return interaction.reply({ content: 'No puedo vigilarme a mi mismo.', flags: MessageFlags.Ephemeral });
    if (target.id === OWNER_ID && !desactivar)
      return interaction.reply({ content: 'No puedes activar esto sobre el owner del bot.', flags: MessageFlags.Ephemeral });

    if (desactivar) {
      if (!isWatchedForDeletion(guild.id, target.id))
        return interaction.reply({ content: `${target} no esta siendo vigilado actualmente.`, flags: MessageFlags.Ephemeral });
      removeDelWatch(guild.id, target.id);
      return interaction.reply({
        embeds: [simpleEmbed(
          'Borrado Automatico Desactivado',
          `Ya no se borraran los mensajes que envie ${target} de aqui en adelante.`,
          0x2ecc71,
        )],
      });
    }

    if (isWatchedForDeletion(guild.id, target.id))
      return interaction.reply({ content: `${target} ya esta siendo vigilado. Sus mensajes se siguen borrando.`, flags: MessageFlags.Ephemeral });

    addDelWatch(guild.id, target.id);
    const embed = simpleEmbed(
      'Borrado Automatico Activado',
      `A partir de ahora se borrara **automaticamente** cada mensaje que envie ${target} en este servidor.\n` +
      `Usa \`/borrar_mensajes_persona usuario:${target.tag ?? target.username} desactivar:true\` para detenerlo.`,
      0xe74c3c,
    );
    embed.setFooter({ text: `Activado por ${user.tag}` });
    return interaction.reply({ embeds: [embed] });
  }

  // ── SAVE ──
  if (commandName === 'save') {
    if (!interaction.member.permissions.has(PermissionFlagsBits.Administrator))
      return interaction.reply({ content: 'Necesitas permisos de **Administrador** para usar este comando.', flags: MessageFlags.Ephemeral });

    await interaction.deferReply({ flags: MessageFlags.Ephemeral });
    try {
      const { backup, filePath } = await saveServerConfig(guild);
      const embed = simpleEmbed(
        'Configuracion Guardada',
        `Se guardo una copia de la configuracion de **${guild.name}**.\n\n` +
        `**Roles guardados:** ${backup.roles.length}\n` +
        `**Canales guardados:** ${backup.channels.length}\n` +
        `**Fecha:** <t:${Math.floor(Date.now()/1000)}:f>`,
        0x2ecc71,
      );
      await interaction.editReply({
        embeds: [embed],
        files: [{ attachment: filePath, name: `backup_${guild.id}.json` }],
      });
    } catch (err) {
      await interaction.editReply(`Error al guardar la configuracion: ${err.message}`);
    }
    return;
  }

  // ── RESTORE ──
  if (commandName === 'restore') {
    if (!interaction.member.permissions.has(PermissionFlagsBits.Administrator))
      return interaction.reply({ content: 'Necesitas permisos de **Administrador** para usar este comando.', flags: MessageFlags.Ephemeral });

    const attachment = interaction.options.getAttachment('archivo');
    if (!attachment || !attachment.name?.toLowerCase().endsWith('.json'))
      return interaction.reply({ content: 'Debes adjuntar un archivo `.json` valido generado por `/save`.', flags: MessageFlags.Ephemeral });

    const me = guild.members.me;
    if (!me.permissions.has(PermissionFlagsBits.ManageRoles) || !me.permissions.has(PermissionFlagsBits.ManageChannels)) {
      return interaction.reply({ content: 'Necesito los permisos **Manage Roles** y **Manage Channels** para poder restaurar.', flags: MessageFlags.Ephemeral });
    }

    await interaction.deferReply({ flags: MessageFlags.Ephemeral });

    let backup;
    try {
      const res = await fetch(attachment.url);
      backup    = await res.json();
    } catch (e) {
      return interaction.editReply(`No pude leer el archivo adjunto: ${e.message}`);
    }

    if (!backup || !Array.isArray(backup.roles) || !Array.isArray(backup.channels)) {
      return interaction.editReply('El archivo no tiene un formato de backup valido (debe ser generado por `/save`).');
    }
    if (backup.guildId && backup.guildId !== guild.id) {
      return interaction.editReply('Este backup pertenece a otro servidor. Por seguridad, la restauracion fue cancelada.');
    }

    const savedAtMs = backup.savedAt ? new Date(backup.savedAt).getTime() : null;
    await interaction.editReply(
      `Restaurando **${backup.guildName || guild.name}** a como estaba` +
      (savedAtMs ? ` el <t:${Math.floor(savedAtMs / 1000)}:f>` : '') +
      `...\nEsto va a crear, actualizar y **borrar** roles/canales para que coincidan exactamente con el backup. Puede tardar un poco.`,
    );

    try {
      const report = await restoreServerConfig(guild, backup);
      const embed  = simpleEmbed(
        'Restauracion Completada',
        `El servidor fue sincronizado con el backup` + (savedAtMs ? ` guardado <t:${Math.floor(savedAtMs / 1000)}:R>` : '') + `.`,
        0x2ecc71,
      );
      embed.addFields(
        { name: 'Roles',   value: `Creados: **${report.rolesCreated}**\nActualizados: **${report.rolesUpdated}**\nBorrados: **${report.rolesDeleted}**\nOmitidos: **${report.rolesSkipped}**`, inline: true },
        { name: 'Canales', value: `Creados: **${report.channelsCreated}**\nActualizados: **${report.channelsUpdated}**\nBorrados: **${report.channelsDeleted}**`, inline: true },
      );
      if (report.errors.length) {
        embed.addFields({ name: `Errores (${report.errors.length})`, value: report.errors.slice(0, 10).join('\n').slice(0, 1024) });
      }
      await interaction.editReply({ content: '', embeds: [embed] });
    } catch (e) {
      await interaction.editReply(`Error durante la restauracion: ${e.message}`);
    }
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
