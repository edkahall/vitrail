'use strict';

const { app, BrowserWindow, globalShortcut, powerSaveBlocker, ipcMain, protocol } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
const crypto = require('crypto');
const { execSync, execFile, spawn } = require('child_process');
const WebSocket = require('ws');

const { createDisplayController } = require('./display_control');
const { LanSyncController }       = require('./sync_lan');
const { LocalControlServer }      = require('./local_control_server');
const { ContentStore }            = require('./content_store');
const { VodLanController }        = require('./vod_lan');

// Schéma signflow:// pour servir les octets du store local au renderer (offline-first
// P0). `stream:true` = lecture média avec support Range (seek vidéo pour la sync LAN).
// Doit être déclaré AVANT app.ready.
protocol.registerSchemesAsPrivileged([
  { scheme: 'signflow', privileges: { standard: true, secure: true, stream: true, supportFetchAPI: true, bypassCSP: true } },
]);

let contentStore = null;  // Local Content Store (initialisé à app.ready)

const PLAYER_VERSION      = app.getVersion();
const CONFIG_PATH         = path.join(app.getPath('userData'), 'config.json');
const DISPLAY_CONFIG_PATH = path.join(app.getPath('userData'), 'display_config.json');
const CAPABILITIES_PATH   = path.join(app.getPath('userData'), 'capabilities.json');
let playerCapabilities = null;  // capacités HW (capabilities.json) remontées au serveur
const LOCAL_CONTROL_PORT  = parseInt(process.env.LOCAL_CONTROL_PORT || '8765', 10);

// ── Forward des logs main + renderer vers fichier rotatif (debug terrain) ───────
// Les logs du renderer (le `log()` du player) ne sortent que dans la console
// Chromium, invisible sur le terrain. On les capte via console-message + on tee
// les logs du process principal → <userData>/logs/player.log (+ .1 après rotation).
const LOG_PATH      = path.join(app.getPath('userData'), 'logs', 'player.log');
const LOG_MAX_BYTES = 2 * 1024 * 1024;   // 2 Mo → rotation vers player.log.1
// Proof of Play (§XXIV) : file de preuves de diffusion PERSISTÉE (survit reboot/coupure) →
// flushée vers POST /pop/batch. Donnée de conformité → on ne la perd pas (≠ events volatils).
const POP_QUEUE_PATH      = path.join(app.getPath('userData'), 'pop_queue.json');
const POP_QUEUE_MAX       = 5000;    // borne anti-gonflement (offline long) → drop des plus anciens
const POP_BATCH_MAX       = 200;     // taille max d'un POST /pop/batch
const POP_FLUSH_INTERVAL  = 60000;   // tentative de flush toutes les 60 s
// P2 — clé device Ed25519 : signe chaque preuve (preuve INFALSIFIABLE émanant du player). La
// privée ne quitte JAMAIS l'appareil ; la publique est enregistrée côté serveur (TOFU).
const POP_KEY_PATH        = path.join(app.getPath('userData'), 'pop_key.json');
let _logStream = null;
let _logBytes  = 0;

function _writeLog(tag, msg) {
  if (!_logStream) return;
  const line = `${new Date().toISOString()} [${tag}] ${msg}\n`;
  try {
    _logStream.write(line);
    _logBytes += Buffer.byteLength(line);
    if (_logBytes > LOG_MAX_BYTES) _rotateLog();
  } catch {}
}

function _rotateLog() {
  try { _logStream.end(); fs.renameSync(LOG_PATH, LOG_PATH + '.1'); } catch {}
  try { _logStream = fs.createWriteStream(LOG_PATH, { flags: 'a' }); _logBytes = 0; }
  catch { _logStream = null; }
}

function initFileLog() {
  try {
    fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true });
    if (fs.existsSync(LOG_PATH) && fs.statSync(LOG_PATH).size > LOG_MAX_BYTES) {
      try { fs.renameSync(LOG_PATH, LOG_PATH + '.1'); } catch {}
    }
    _logStream = fs.createWriteStream(LOG_PATH, { flags: 'a' });
    try { _logBytes = fs.statSync(LOG_PATH).size; } catch { _logBytes = 0; }
    // Tee des logs du process principal (console.log/warn/error) vers le fichier.
    for (const level of ['log', 'warn', 'error']) {
      const orig = console[level].bind(console);
      console[level] = (...args) => {
        orig(...args);
        _writeLog(level === 'log' ? 'main' : `main:${level}`,
          args.map((a) => (typeof a === 'string' ? a : String(a))).join(' '));
      };
    }
    _writeLog('main', `=== Player ${PLAYER_VERSION} log start ===`);
  } catch {
    _logStream = null;  // jamais bloquant
  }
}

let mainWindow        = null;
let ws                = null;
let heartbeatTimer    = null;
let reconnectTimer    = null;
let config            = {};

// ── Display & LAN sync (initialises depuis display_config recu dans player:sync) ─
let displayController  = null;
let lanSync            = null;
let _appliedLanConfig  = null;   // "group|master" du contrôleur LAN actif (idempotence : ne
                                 // recrée le contrôleur QUE si la config LAN change réellement)
let serverDisplayConfig = null;  // display_config recu du serveur dans player:sync
// Dernières valeurs d'affichage RÉELLEMENT appliquées — garde d'idempotence : initDisplayController
// tourne à CHAQUE player:sync (force_sync 30s + activité barrière/store). Sans cette garde,
// `xrandr --mode` / `wlr-randr --transform` étaient relancés à chaque fois → reset de la sortie
// → flash noir + la vidéo redémarre (surtout visible en chargeant des 4K).
let _appliedResolution = null;
let _appliedRotation   = null;
// ── Pilote auto-power OFFLINE (autonomie sans serveur) ─────────────────────────
// Fenêtres d'allumage pré-calculées par le serveur (display_config.offline_power,
// persisté dans display_config.json) : si la WS est down depuis > grace, le player
// allume/éteint SEUL son écran (contrôleur ou repli sortie vidéo). Online, le serveur
// reste la source de vérité (worker apply_site_power + re-assertion à la reconnexion).
let _wsDownSince         = Date.now();  // boot = « down » jusqu'au 1er open WS
let _offlineAppliedPower = null;        // dernier état appliqué par le pilote offline
let _offlinePowerTimer   = null;
const OFFLINE_POWER_GRACE_MS = 3 * 60 * 1000;  // > backoff reconnect max (30 s)
// Barriere de readiness LAN (Q1) : empreinte du contenu courant + cache_keys epingles
// + fps deja relaches (le mur ne demarre une empreinte que quand le groupe l'a en cache).
let _currentFp         = null;
let _pinnedKeys        = [];
const _releasedFps     = new Set();
// Download-then-play STRICT (anti-stream auto serveur) : `allow_streaming` (display_config) OFF
// par défaut → jamais de streaming d'une vidéo non cachée. Le nouveau contenu est RETENU
// (_downloadGate) tant que toutes ses vidéos ne sont pas cachées ; le programme courant continue.
let _allowStreaming    = false;
let _downloadGate      = null;
// Repli offline : cache_keys (servis) satisfaits via le parent caché ce sync → comptent
// comme « prêts » pour la barrière LAN même si la variante servie n'est pas (encore) là.
let _fallbackReady     = new Set();

// ── Store Control Panel LAN (§19.3) ──────────────────────────────────────────
let localControlServer  = null;  // instance LocalControlServer (creee une fois sur ready)
let localControlProfile = null;  // profil recu dans player:sync (token + permissions)
let _vodMedia          = [];     // medias VOD a la demande (Feature B) du player — panneau LAN-local
// Configs VOD PILOTABLES depuis le panneau local (toutes celles du SITE, chacune avec SA
// cible resolue). A ne pas confondre avec _vodMedia = ce que CE player peut JOUER.
let _vodConfigs        = [];
let _vodLan            = null;    // { secret, self_id, peers:[{id_player,name}] } — trigger VOD multi-cible LAN
let vodLanController   = null;    // instance VodLanController (broadcast/reception UDP 7779)
let lastAudioStatus     = {};    // dernier etat audio remonte par le renderer (cache pour /control/status)
let lastSyncState      = null;   // { id_playlist, currentIndex, currentOffsetMs, syncedAt }
let _scheduleTimer     = null;   // setTimeout handle pour le power schedule
let _pulseTimer        = null;   // setInterval handle pour le pulse power
let _sequenceRunning   = false;  // guard contre les sequences concurrentes

/**
 * Decode l'id_player depuis le JWT player (payload.sub).
 * Retourne null si le token est absent ou mal forme.
 */
function getPlayerId() {
  try {
    if (!config.player_token) return null;
    const parts = config.player_token.split('.');
    if (parts.length < 2) return null;
    const payload = JSON.parse(Buffer.from(parts[1], 'base64').toString());
    const id = parseInt(payload.sub, 10);
    return isNaN(id) ? null : id;
  } catch { return null; }
}

/**
 * Etat courant de lecture (utilise par LAN sync leader pour diffuser la position).
 * L'offset est calcule a partir de la derniere sync recue + le temps ecoule.
 */
// Position de lecture VIDEO reelle, remontee par le renderer toutes les ~1s.
// Bien plus precise que l'estimation Date.now() (qui ignore la latence de decodage).
let lastPlaybackPos = null;   // { id_playlist, currentIndex, offsetMs, reportedAt }

function getLanSyncState() {
  // Prefere la position video reelle (interpolee depuis le dernier report)
  if (lastPlaybackPos) {
    // Borne l'interpolation : le renderer reporte toutes les 1000ms. S'il cesse de reporter
    // (vidéo finie, jank), un elapsed NON borné fait courir currentOffsetMs avec l'horloge murale
    // (65000ms pour une vidéo de 22s) → le leader DIFFUSE cette valeur folle en start_offset_ms →
    // seeks hors-borne sur les slaves = écran noir. Cap à 1500ms.
    const elapsed = Math.min(Date.now() - lastPlaybackPos.reportedAt, 1500);
    return {
      id_playlist:     lastPlaybackPos.id_playlist,
      currentIndex:    lastPlaybackPos.currentIndex,
      currentOffsetMs: lastPlaybackPos.offsetMs + elapsed,
      switch_at:       lastPlaybackPos.switchAt ?? null,   // #2b : absolu, pas d'interpolation
      next_index:      lastPlaybackPos.nextIndex ?? null,
    };
  }
  if (!lastSyncState) return null;
  const elapsed = Date.now() - lastSyncState.syncedAt;
  return {
    id_playlist:    lastSyncState.id_playlist,
    currentIndex:   lastSyncState.currentIndex,
    currentOffsetMs: (lastSyncState.currentOffsetMs ?? 0) + elapsed,
  };
}

// ── Display config persistence (pour le power schedule offline) ───────────────

/**
 * Sauvegarde display_config sur disque — permet au scheduler de fonctionner
 * meme si le player demarre sans connexion au backend.
 */
function saveDisplayConfig(cfg) {
  try {
    if (cfg) {
      fs.writeFileSync(DISPLAY_CONFIG_PATH, JSON.stringify(cfg, null, 2));
    } else {
      try { fs.unlinkSync(DISPLAY_CONFIG_PATH); } catch {}
    }
  } catch (e) {
    console.error('[Display] Failed to save config:', e.message);
  }
}

function loadDisplayConfig() {
  try {
    const raw = fs.readFileSync(DISPLAY_CONFIG_PATH, 'utf8');
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

/**
 * Scheduler d'allumage/extinction de l'ecran physique.
 *
 * - S'aligne sur les frontieres de minutes (evite la derive et ne rate jamais
 *   un horaire meme si le player vient de demarrer).
 * - Lit serverDisplayConfig a chaque tick : un changement de config est pris
 *   en compte sans redemarrage.
 * - Fonctionne hors ligne grace a la persistance dans display_config.json.
 *
 * Format schedule : { power_on: "HH:MM", power_off: "HH:MM", days: [1..7] }
 * days ISO : 1=Lun...7=Dim. Absent ou vide = tous les jours.
 */
function startDisplayScheduler() {
  if (_scheduleTimer) { clearTimeout(_scheduleTimer); _scheduleTimer = null; }

  const sched = serverDisplayConfig?.schedule;
  if (!sched || (!sched.power_on && !sched.power_off)) return;
  if (!displayController) return;

  console.log(
    `[Schedule] Started: ON=${sched.power_on ?? '--'} OFF=${sched.power_off ?? '--'}` +
    ` days=${JSON.stringify(sched.days ?? 'all')}`
  );

  function tick() {
    const schedule = serverDisplayConfig?.schedule;
    if (schedule && displayController) {
      const now  = new Date();
      const dow  = now.getDay() || 7;           // JS: 0=Sun -> 7; ISO: 1=Lun...7=Dim
      const days = schedule.days;

      if (!days || days.length === 0 || days.includes(dow)) {
        const hhmm  = String(now.getHours()).padStart(2, '0') + ':' +
                      String(now.getMinutes()).padStart(2, '0');
        const cfg   = serverDisplayConfig ?? {};
        const model = cfg.model ?? 'samsung_mdc';
        const addr  = cfg.address ?? 1;

        if (schedule.power_on && hhmm === schedule.power_on) {
          console.log(`[Schedule] power_on at ${hhmm}`);
          if ((serverDisplayConfig?.startup_sequence?.length ?? 0) > 0) {
            runStartupSequence();
          } else {
            displayController.command(model, 'power_on', addr, {})
              .catch((err) => console.error('[Schedule] power_on error:', err.message));
          }
        }
        if (schedule.power_off && hhmm === schedule.power_off) {
          console.log(`[Schedule] power_off at ${hhmm}`);
          displayController.command(model, 'power_off', addr, {})
            .catch((err) => console.error('[Schedule] power_off error:', err.message));
        }
      }
    }

    // Prochain tick a la frontiere de la minute suivante (+50ms de marge)
    const n = new Date();
    _scheduleTimer = setTimeout(tick, (60 - n.getSeconds()) * 1000 - n.getMilliseconds() + 50);
  }

  // Premier tick a la prochaine frontiere de minute
  const n = new Date();
  _scheduleTimer = setTimeout(tick, (60 - n.getSeconds()) * 1000 - n.getMilliseconds() + 50);
}

/**
 * Retourne true si l'heure actuelle est dans la plage d'ouverture (power_on <= now < power_off).
 * Plages sur la meme journee uniquement.
 */
function isInOpenHours(schedule) {
  if (!schedule?.power_on || !schedule?.power_off) return false;
  const now  = new Date();
  const dow  = now.getDay() || 7;
  const days = schedule.days;
  if (days && days.length > 0 && !days.includes(dow)) return false;
  const hhmm = String(now.getHours()).padStart(2, '0') + ':' +
               String(now.getMinutes()).padStart(2, '0');
  return hhmm >= schedule.power_on && hhmm < schedule.power_off;
}

/**
 * Demarre le scheduler de pulse power.
 * Envoie power_on (dans la plage) ou power_off (hors plage) a intervalle regulier.
 * Conditions : controleur configure + horaires definis + pulse_enabled != false + intervalle > 0.
 */
function startPulseScheduler() {
  if (_pulseTimer) { clearInterval(_pulseTimer); _pulseTimer = null; }
  const sched = serverDisplayConfig?.schedule;
  if (!sched?.power_on || !sched?.power_off) return;
  if (sched.pulse_enabled === false) return;
  const intervalMs = (sched.pulse_interval_s ?? 0) * 1000;
  if (intervalMs <= 0 || !displayController) return;

  console.log(`[Schedule] Pulse started: every ${sched.pulse_interval_s}s`);
  _pulseTimer = setInterval(() => {
    const cfg = serverDisplayConfig;
    if (!cfg?.schedule || !displayController) return;
    if (cfg.schedule.pulse_enabled === false) return;
    const cmd = isInOpenHours(cfg.schedule) ? 'power_on' : 'power_off';
    displayController.command(cfg.model ?? 'samsung_mdc', cmd, cfg.address ?? 1, {})
      .catch((err) => console.error(`[Schedule] pulse ${cmd} error:`, err.message));
  }, intervalMs);
}

/**
 * Execute la sequence de demarrage configuree dans display_config.startup_sequence.
 * Chaque step est execute apres son delai (delay_before_s, defaut 0).
 */
async function runStartupSequence() {
  if (_sequenceRunning) { console.log('[Display] Sequence already running, ignored'); return; }
  const cfg = serverDisplayConfig;
  if (!cfg?.startup_sequence?.length || !displayController) return;
  _sequenceRunning = true;
  const model = cfg.model ?? 'samsung_mdc';
  const addr  = cfg.address ?? 1;
  console.log(`[Display] Startup sequence: ${cfg.startup_sequence.length} step(s)`);
  try {
    for (const step of cfg.startup_sequence) {
      const delay = (step.delay_before_s ?? 0) * 1000;
      if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay));
      if (!displayController) break;
      console.log(`[Display] Seq step: ${step.command} (after ${step.delay_before_s ?? 0}s)`);
      await displayController.command(model, step.command, addr, step.params ?? {})
        .catch((err) => console.error(`[Display] Seq step error (${step.command}):`, err.message));
    }
    console.log('[Display] Startup sequence complete');
  } finally {
    _sequenceRunning = false;
  }
}

// ── Pilote auto-power offline ──────────────────────────────────────────────────

/**
 * Applique un etat d'alimentation ecran par le meme chemin que display_command :
 * controleur reel (rs232/ip/cec) si configure, sinon repli coupure sortie video
 * (si output_power_fallback). No-op si aucun des deux.
 */
function applyDisplayPower(on, source) {
  const cfg = serverDisplayConfig ?? {};
  const cmd = on ? 'power_on' : 'power_off';
  if (displayController) {
    displayController.command(cfg.model ?? 'samsung_mdc', cmd, cfg.address ?? 1, {})
      .catch((err) => console.error(`[Power] ${source} ${cmd} error:`, err.message));
  } else if (cfg.output_power_fallback) {
    try { setVideoOutputPower(on); } catch (err) {
      console.error(`[Power] ${source} ${cmd} fallback error:`, err.message);
    }
  }
}

/**
 * Etat d'alimentation voulu d'apres display_config.offline_power, ou null = ne pas piloter.
 * offline_power = { mode, intervals: [["YYYY-MM-DDTHH:MM:SS","YYYY-MM-DDTHH:MM:SS"], ...] }
 * Datetimes SANS timezone = heure LOCALE (meme hypothese que le wall-clock scheduling :
 * TZ du player = TZ du site). Horizon serveur = 14 jours ; au-dela on projette le motif
 * hebdomadaire en reculant de k x 7 jours dans la fenetre connue.
 */
function offlineDesiredPower(op, now) {
  if (!op || op.mode === 'manual') return null;
  if (op.mode === 'always_on') return true;
  const iv = Array.isArray(op.intervals) ? op.intervals : [];
  if (iv.length === 0) return false;
  let t = now.getTime();
  const lastEnd = new Date(iv[iv.length - 1][1]).getTime();
  if (Number.isFinite(lastEnd) && t > lastEnd) {
    const WEEK_MS = 7 * 24 * 3600 * 1000;
    t -= Math.ceil((t - lastEnd) / WEEK_MS) * WEEK_MS;
  }
  for (const pair of iv) {
    const a = new Date(pair[0]).getTime();
    const b = new Date(pair[1]).getTime();
    if (Number.isFinite(a) && Number.isFinite(b) && a <= t && t < b) return true;
  }
  return false;
}

/**
 * Tick minute (aligne sur les frontieres de minute, comme startDisplayScheduler) :
 * n'agit QUE si la WS est down depuis plus de OFFLINE_POWER_GRACE_MS (online, le
 * worker serveur pilote — jamais de double commande contradictoire). Edge-triggered
 * (_offlineAppliedPower) + baseline a l'entree en mode offline (null -> etat courant),
 * ce qui couvre aussi le boot sans serveur au milieu d'une fenetre d'ouverture.
 */
function startOfflinePowerScheduler() {
  if (_offlinePowerTimer) { clearTimeout(_offlinePowerTimer); _offlinePowerTimer = null; }

  function tick() {
    try {
      const op = serverDisplayConfig?.offline_power;
      const wsUp = ws && ws.readyState === WebSocket.OPEN;
      if (!wsUp && op && _wsDownSince
          && (Date.now() - _wsDownSince) >= OFFLINE_POWER_GRACE_MS) {
        const desired = offlineDesiredPower(op, new Date());
        if (desired !== null && desired !== _offlineAppliedPower) {
          const downMin = Math.round((Date.now() - _wsDownSince) / 60000);
          console.log(`[Power] Offline pilot: ${desired ? 'power_on' : 'power_off'} (no server for ${downMin} min)`);
          applyDisplayPower(desired, 'offline');
          _offlineAppliedPower = desired;
        }
      }
    } catch (e) {
      console.error('[Power] offline tick error:', e.message);
    }
    const n = new Date();
    _offlinePowerTimer = setTimeout(tick, (60 - n.getSeconds()) * 1000 - n.getMilliseconds() + 50);
  }

  const n = new Date();
  _offlinePowerTimer = setTimeout(tick, (60 - n.getSeconds()) * 1000 - n.getMilliseconds() + 50);
}

/**
 * Initialise (ou reinitialise) le displayController et le LAN sync
 * a partir de display_config recu dans player:sync.
 */
// Apply screen resolution via xrandr (X11).
// Auto-detects the first connected output.
function applyScreenResolution(resolution) {
  if (!resolution) return;
  try {
    const output = execSync("xrandr | awk '/ connected/{print $1}' | head -1", { encoding: 'utf8', timeout: 5000 }).trim();
    if (!output) { console.warn('[Resolution] No X11 output detected'); return; }
    const mode = resolution.split('@')[0]; // strip @hz for xrandr --mode
    execSync(`xrandr --output "${output}" --mode "${mode}" 2>/dev/null`, { timeout: 5000 });
    console.log(`[Resolution] ${resolution} applied on ${output} (X11)`);
  } catch (e) {
    console.error('[Resolution] xrandr error:', e.message);
  }
}

// Apply screen rotation. Tente wlr-randr (cage/Wayland) puis xrandr (X11) en repli.
// Affichage portrait piloté depuis le CMS (display_config.screen_rotation).
function applyScreenRotation(rotation) {
  const valid = ['normal', 'left', 'right', 'inverted'];
  if (!valid.includes(rotation)) return;
  // Mapping xrandr → wlr-randr transform (rotation horaire de la sortie)
  const wlrTransform = { normal: 'normal', right: '90', inverted: '180', left: '270' }[rotation];
  try {
    const wlrOut = execSync("wlr-randr 2>/dev/null | awk 'NR==1{print $1}'", { encoding: 'utf8', timeout: 5000 }).trim();
    if (wlrOut) {
      execSync(`wlr-randr --output "${wlrOut}" --transform ${wlrTransform} 2>/dev/null`, { timeout: 5000 });
      console.log(`[Rotation] ${rotation} (transform ${wlrTransform}) applied on ${wlrOut} (wlr-randr)`);
      return;
    }
  } catch (e) { /* pas de Wayland → repli xrandr */ }
  try {
    const output = execSync("xrandr | awk '/ connected/{print $1}' | head -1", { encoding: 'utf8', timeout: 5000 }).trim();
    if (!output) { console.warn('[Rotation] No output detected'); return; }
    execSync(`xrandr --output "${output}" --rotate ${rotation} 2>/dev/null`, { timeout: 5000 });
    console.log(`[Rotation] ${rotation} applied on ${output} (X11)`);
  } catch (e) {
    console.error('[Rotation] error:', e.message);
  }
}

// ── Coupure de la sortie video (ecran NON pilotable RS232/IP/CEC) ───────────────
// power_on/power_off sans controleur d'ecran : on coupe le signal HDMI du player
// lui-meme via wlr-randr --off (cage/Wayland desactive la sortie -> plus de signal).
// La plupart des ecrans passent alors en veille ; le reveil au retour du signal
// depend du reglage de l'ecran (« wake on signal » sur les ecrans pro).
// Opt-in CMS : display_config.output_power_fallback. Nom d'output memorise AVANT
// extinction (on ne depend pas du parsing d'une sortie desactivee pour rallumer).
let _wlrPowerOutput = null;
// Etat REELLEMENT applique (null = inconnu au demarrage) — cf. defaut du 2026-07-27 :
// le serveur remet `last_power_state = NULL` a chaque (re)connexion WS, donc un `power_on`
// redondant arrive apres CHAQUE redemarrage backend. Reappliquer un `--on` sur une sortie
// deja allumee provoque un modeset (perte puis reacquisition du signal cote ecran).
let _videoOutputOn = null;
function setVideoOutputPower(on) {
  if (!_wlrPowerOutput) {
    const out = execSync("wlr-randr 2>/dev/null | awk 'NR==1{print $1}'", { encoding: 'utf8', timeout: 5000 }).trim();
    if (!out) throw new Error('no Wayland output detected (wlr-randr)');
    _wlrPowerOutput = out;
  }
  if (_videoOutputOn === on) {
    console.log(`[Display] video output already ${on ? 'ON' : 'OFF'} — no-op (idempotent)`);
    return;
  }
  execSync(`wlr-randr --output "${_wlrPowerOutput}" --${on ? 'on' : 'off'}`, { timeout: 5000 });
  console.log(`[Display] video output ${on ? 'ON' : 'OFF'} (wlr-randr ${_wlrPowerOutput})`);
  _videoOutputOn = on;
}

function initDisplayController(displayConfig) {
  // Fermer les instances precedentes
  if (displayController) {
    try { displayController.close(); } catch {}
    displayController = null;
  }
  // NB : le contrôleur LAN sync n'est PLUS arrêté ici — il est géré par manageLanSync()
  // de façon idempotente (recréé seulement si la config LAN change), sinon il était
  // détruit/recréé à CHAQUE player:sync (dont le force_sync 30s des fenêtres horaires)
  // → peers jamais stables, barrière réinitialisée, micro-désync toutes les 30s.
  if (_scheduleTimer) { clearTimeout(_scheduleTimer); _scheduleTimer = null; }
  if (_pulseTimer)    { clearInterval(_pulseTimer);   _pulseTimer    = null; }

  serverDisplayConfig = displayConfig ?? null;
  saveDisplayConfig(displayConfig ?? null);   // Persist pour offline
  applyScreenshotSchedule(displayConfig);     // capture periodique (defaut 1h)
  // QoS download (Q3) + fenêtres look-ahead (Phase 2) : plafond bande passante + plages
  // horaires de prefetch autorisées par player (display_config).
  if (contentStore) contentStore.setPolicy({
    bandwidth_kbps: displayConfig?.download_bandwidth_kbps,
    windows: displayConfig?.download_windows,
    measure_bandwidth: displayConfig?.measure_bandwidth,
  });
  manageLanSync(displayConfig);               // idempotent (recrée seulement si la config LAN change)
  if (!displayConfig) return;

  if (displayConfig.screen_resolution && displayConfig.screen_resolution !== _appliedResolution) {
    applyScreenResolution(displayConfig.screen_resolution);
    _appliedResolution = displayConfig.screen_resolution;
  }
  if (displayConfig.screen_rotation && displayConfig.screen_rotation !== _appliedRotation) {
    applyScreenRotation(displayConfig.screen_rotation);
    _appliedRotation = displayConfig.screen_rotation;
  }

  // Controller ecran (RS232 ou IP)
  displayController = createDisplayController(displayConfig);
  if (displayController) {
    console.log(
      `[Display] Controller init: type=${displayConfig.type} ` +
      `model=${displayConfig.model ?? 'samsung_mdc'}`
    );
    startDisplayScheduler();
    startPulseScheduler();
  }

}

// Gère le contrôleur LAN sync de façon IDEMPOTENTE : ne le (re)crée que si la config LAN
// (enabled/group/master) a réellement changé. Sinon on le laisse tourner — sinon chaque
// player:sync (dont le force_sync 30s des fenêtres horaires) le détruisait/recréait, et
// les peers ne se stabilisaient jamais (élection en boucle, barrière réinitialisée).
function manageLanSync(displayConfig) {
  const wanted = displayConfig?.lan_sync_enabled
    ? `${displayConfig.lan_sync_group ?? 'default'}|${displayConfig.lan_sync_master === true}`
    : null;
  if (wanted === _appliedLanConfig && (wanted === null || lanSync)) return;  // aucun changement

  if (lanSync) { try { lanSync.stop(); } catch {} lanSync = null; }
  _appliedLanConfig = wanted;
  if (!wanted) return;

  const idPlayer = getPlayerId();
  if (!idPlayer) {
    console.warn('[LAN Sync] Cannot start: id_player unknown (no JWT yet)');
    _appliedLanConfig = null;   // re-essai au prochain sync (quand le JWT sera là)
    return;
  }
  lanSync = new LanSyncController(
    idPlayer,
    displayConfig.lan_sync_group ?? 'default',
    (syncPayload) => {
      // Piste B : nudge fin via playbackRate (canal dedie, contourne le fingerprint)
      mainWindow?.webContents.send('player:lan-adjust', {
        id_playlist:     syncPayload.id_playlist,
        start_index:     syncPayload.start_index,
        start_offset_ms: syncPayload.start_offset_ms,
      });
    },
    displayConfig.lan_sync_master === true   // leader designe (machine la plus puissante)
  );
  lanSync.setStateGetter(getLanSyncState);
  // #2b : le maître annonce sa prochaine bascule → les slaves s'y alignent
  lanSync.setSwitchHintHandler((hint) => {
    mainWindow?.webContents.send('player:lan-switch', hint);
  });
  // Barrière de readiness (Q1) : la barrière s'ouvre (groupe prêt ou timeout) → le
  // renderer démarre l'empreinte courante.
  lanSync.setBarrierReleaseHandler((fp) => {
    _releasedFps.add(fp);
    if (fp === _currentFp) mainWindow?.webContents.send('player:lan-release');
  });
  lanSync.start();
  updateLanReadiness();  // déclare l'état courant dès le démarrage du contrôleur
}

// ── Store Control Panel LAN (§19.3) ──────────────────────────────────────────

/**
 * Lecture VOD ad-hoc takeover LOCALE (ce player). Résolue offline-first (cache prioritaire,
 * repli URL signée) puis poussée au renderer — même chemin que le trigger cloud play_now.
 * Partagé par : le panneau LAN-local (cible = soi) ET la réception d'une commande UDP d'un pair.
 */
function triggerLocalVod(id_media, mode) {
  // Le son du takeover voyage ENCODE dans le mode (`once_muted`/`loop_muted`) : la chaine
  // signee du paquet LAN (vod_play|id_player|id_media|mode|ts|nonce) reste ainsi inchangee,
  // donc zero rupture de compatibilite pendant un deploiement partiel. Un player ancien
  // recevant `once_muted` retombe sur `once` (avec son) — degradation sure, jamais un echec.
  const withSound = !String(mode || '').endsWith('_muted');
  mode = String(mode || '').replace(/_muted$/, '');
  if (mode === 'stop') {
    mainWindow?.webContents.send('player:play_now', { mode: 'stop' });
    return;
  }
  const m = (_vodMedia || []).find((x) => x.id_media === id_media);
  if (!m) return;
  let src = m.url || null;
  if (contentStore && m.cache_key) {
    const fk = contentStore.keyFor(m.cache_key);
    if (contentStore.verify(fk)) src = contentStore.localUrl(fk);
    else if (m.url) contentStore.enqueue({ cache_key: m.cache_key, cache_checksum: m.cache_checksum, mime: m.mime_type, url: m.url, id_media: m.id_media });
  }
  if (!src) return;
  mainWindow?.webContents.send('player:play_now', {
    id_media: m.id_media, media_type: m.media_type || 'video',
    mode: mode === 'loop' ? 'loop' : 'once', src, duration_ms: m.duration_ms || null,
    // Trigger LOCAL/LAN : le son vient du panneau (encode dans le mode), pas du serveur.
    with_sound: withSound, has_audio: m.has_audio,
  });
}

/**
 * Démarre/arrête/re-clé le contrôleur UDP de trigger VOD multi-cible (port 7779) selon la
 * présence d'un bloc `vod_lan` (secret partagé) dans le sync. Actif dès que le player est dans
 * une config VOD — indépendamment du LAN sync (le trigger doit marcher hors groupe de synchro).
 */
function manageVodLan() {
  const secret = _vodLan && _vodLan.secret ? _vodLan.secret : null;
  const selfId = getPlayerId();
  if (secret && selfId != null) {
    if (!vodLanController) {
      vodLanController = new VodLanController(selfId, secret, ({ id_media, mode }) => triggerLocalVod(id_media, mode));
      vodLanController.start();
    } else {
      vodLanController.updateSecret(secret);
    }
  } else if (vodLanController) {
    vodLanController.stop();
    vodLanController = null;
  }
}

/**
 * Cree (une fois) l'instance du serveur de controle local. Le demarrage/arret
 * effectif est pilote par manageLocalControl() selon le flag display_config.
 */
function ensureLocalControlServer() {
  if (localControlServer) return;
  localControlServer = new LocalControlServer({
    port:          LOCAL_CONTROL_PORT,
    getPlayerName: () => os.hostname(),
    getProfile:    () => localControlProfile,
    getStatus:     () => lastAudioStatus,
    getVodMedia:   () => _vodMedia,
    getVodConfigs: () => _vodConfigs,
    getVodPeers:   () => (_vodLan && Array.isArray(_vodLan.peers) ? _vodLan.peers : []),
    getSelfId:     () => getPlayerId(),
    onAction:      (action) => {
      // VOD à la demande LAN-local (Feature B Phase 3) : lecture ad-hoc takeover MULTI-CIBLE,
      // AUTONOME (sans serveur). Le panneau choisit des players cibles (id_players) ; pour soi =
      // lecture locale directe, pour les autres = broadcast UDP signé (le pair résout offline-first).
      if (action.action === 'play_vod') {
        const selfId = getPlayerId();
        // Cibles : sous-ensemble demandé, repli = tous les pairs (ou soi seul si pas de groupe LAN).
        const peerIds = (_vodLan && Array.isArray(_vodLan.peers)) ? _vodLan.peers.map((p) => p.id_player) : [];
        let targets = Array.isArray(action.id_players) && action.id_players.length
          ? action.id_players
          : (peerIds.length ? peerIds : (selfId != null ? [selfId] : []));
        for (const t of targets) {
          if (t === selfId) {
            triggerLocalVod(action.id_media, action.mode);
          } else if (vodLanController) {
            vodLanController.broadcast({ id_player: t, id_media: action.id_media, mode: action.mode });
          }
        }
        if (ws?.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'local_control:action', ...action, id_players: targets }));
        }
        return;
      }
      // Applique localement (reactif) ...
      mainWindow?.webContents.send('control:apply', action);
      // Routage sortie audio = niveau OS (PipeWire), pas Chromium
      if (action.action === 'set_output' && action.basket_output) {
        applyAudioSink(action.basket_output);
      }
      // ... et transmet au backend (persistance + audit source=local_control)
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'local_control:action', ...action }));
      }
    },
  });
}

/**
 * Demarre ou arrete le serveur de controle local selon le flag serveur
 * display_config.local_control_enabled (opt-in par player, OFF par defaut).
 */
function manageLocalControl(displayConfig) {
  const enabled = displayConfig?.local_control_enabled === true;
  if (enabled) {
    ensureLocalControlServer();
    localControlServer.start();
  } else if (localControlServer) {
    localControlServer.stop();
  }
}

// ── Sortie audio (routage OS via PipeWire wpctl, fiable sur RPi) ───────────────
let _appliedSink = null;

function applyAudioSink(role) {
  role = role || 'auto';
  if (role === 'auto' || role === _appliedSink) return;
  try {
    // Backend de routage : PipeWire (wpctl) en priorité, sinon PulseAudio (pactl).
    // Les Debian J4025 du parc n'ont PAS wpctl → le fallback pactl est indispensable
    // (sans lui, la sortie reste collée au sink par défaut, souvent le HDMI/écran).
    let backend = null;
    const sinks = [];   // [{ id, name }] — id numérique (wpctl) ou nom (pactl)
    try {
      const out = execSync('wpctl status', { encoding: 'utf8', timeout: 4000 });
      let inAudio = false, inSinks = false;
      for (const ln of out.split('\n')) {
        if (/^Audio\b/.test(ln)) { inAudio = true; inSinks = false; continue; }
        if (/^(Video|Settings)\b/.test(ln)) { inAudio = false; inSinks = false; continue; }
        if (!inAudio) continue;
        if (/Sinks:/.test(ln)) { inSinks = true; continue; }
        if (/(Sources|Filters|Streams|Devices):/.test(ln)) { inSinks = false; continue; }
        if (inSinks) {
          const m = ln.match(/(\d+)\.\s+(.+?)\s+\[vol/);
          if (m) sinks.push({ id: parseInt(m[1], 10), name: m[2] });
        }
      }
      backend = 'wpctl';
    } catch {
      // wpctl absent → PulseAudio. `pactl list short sinks` : <id> <name> <module> ...
      const out = execSync('pactl list short sinks', { encoding: 'utf8', timeout: 4000 });
      for (const ln of out.split('\n')) {
        const cols = ln.trim().split(/\s+/);
        if (cols.length >= 2 && /^\d+$/.test(cols[0])) sinks.push({ id: cols[0], name: cols[1] });
      }
      backend = 'pactl';
    }

    const want = (n) => {
      const l = n.toLowerCase();
      if (role === 'hdmi') return l.includes('hdmi');
      if (role === 'usb')  return l.includes('usb');
      // jack = sortie analogique INTÉGRÉE → exclure la carte USB, dont le nom contient
      // souvent 'analog' ET 'usb' (ex. C-Media ...USB_Audio_Device...analog-stereo).
      if (role === 'jack') return /analog|headphone|headset/.test(l) && !l.includes('usb');
      return false;
    };
    const sink = sinks.find((s) => want(s.name));
    if (!sink) {
      console.warn(`[AudioSink] aucun sink pour role=${role} via ${backend} (dispo: ${sinks.map((s) => s.name).join(' | ')})`);
      return;
    }

    if (backend === 'wpctl') {
      execSync(`wpctl set-default ${sink.id}`, { timeout: 4000 });
    } else {
      // PulseAudio : set-default-sink (par nom) + déplacer les flux DÉJÀ en cours vers
      // la nouvelle sortie (sinon ils restent collés à l'ancien sink, ex. HDMI).
      execSync(`pactl set-default-sink ${sink.name}`, { timeout: 4000 });
      try {
        const ins = execSync('pactl list short sink-inputs', { encoding: 'utf8', timeout: 4000 });
        for (const ln of ins.split('\n')) {
          const idx = ln.trim().split(/\s+/)[0];
          if (/^\d+$/.test(idx)) { try { execSync(`pactl move-sink-input ${idx} ${sink.name}`, { timeout: 4000 }); } catch {} }
        }
      } catch {}
    }
    _appliedSink = role;
    console.log(`[AudioSink] sortie → ${sink.name} (${backend}) pour role=${role}`);
    mainWindow?.webContents.send('audio:restart');
  } catch (e) {
    console.error('[AudioSink] erreur:', e.message);
  }
}

// ── Config ───────────────────────────────────────────────────────────────────

function loadConfig() {
  try {
    config = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
  } catch {
    config = { backend_url: '', player_token: '' };
    saveConfig();
  }
}

function saveConfig() {
  fs.mkdirSync(path.dirname(CONFIG_PATH), { recursive: true });
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2));
}

// ── Window ────────────────────────────────────────────────────────────────────

function createWindow() {
  mainWindow = new BrowserWindow({
    fullscreen: true,
    frame: false,
    backgroundColor: '#1C1E2A',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      plugins: true,   // viewer PDF Chromium intégré (rendu des médias 'document' en iframe)
    },
  });

  // Autorise l'accès média → enumerateDevices() expose les labels des sorties audio
  // (nécessaire pour router les paniers vers HDMI/Jack/USB via setSinkId).
  try {
    const ses = mainWindow.webContents.session;
    ses.setPermissionRequestHandler((_wc, _perm, cb) => cb(true));
    ses.setPermissionCheckHandler(() => true);
    // url_display (§XXV.2) : retire les protections d'embarquement (X-Frame-Options + CSP
    // frame-ancestors) pour qu'un site externe s'affiche dans une <iframe> (sinon « page noire » :
    // la plupart des sites refusent d'être framés). Acceptable sur un player kiosk qui n'affiche
    // que des URLs configurées par l'admin.
    ses.webRequest.onHeadersReceived((details, cb) => {
      const headers = details.responseHeaders || {};
      for (const k of Object.keys(headers)) {
        const lk = k.toLowerCase();
        if (lk === 'x-frame-options') { delete headers[k]; continue; }
        if (lk === 'content-security-policy') {
          const vals = (Array.isArray(headers[k]) ? headers[k] : [headers[k]])
            .map((v) => String(v).replace(/frame-ancestors[^;]*;?/gi, '').trim())
            .filter((v) => v.length > 0);
          if (vals.length) headers[k] = vals; else delete headers[k];
        }
      }
      cb({ responseHeaders: headers });
    });
  } catch {}

  // Forward des logs du renderer vers le fichier (debug terrain)
  // Electron >= 35 porte niveau et message sur l'événement (level = 'info'|'warning'|'error'|
  // 'debug') et déclare obsolètes les arguments positionnels d'Electron 30 (level 0-3, message).
  // Les DEUX formes doivent marcher : un OTA ne livre que src/ + package.json, jamais le moteur,
  // donc un parc reste en Electron 30 tant que chaque appareil n'a pas été réinstallé.
  // `...args` (et non 3 paramètres nommés) : le listener ne réclame que l'événement.
  mainWindow.webContents.on('console-message', (e, ...args) => {
    const modern = e && typeof e.message === 'string';
    const lvl = modern ? (e.level || 'info')
      : (['verbose', 'info', 'warning', 'error'][args[0]] || 'info');
    _writeLog(`renderer:${lvl}`, modern ? e.message : args[1]);
  });

  // Crash du process renderer en RÉGIME STABLE : sans handler, la fenêtre reste figée sur
  // un écran noir (le compositeur cage ne sort pas → la boucle start-player.sh ne relance
  // pas). On relance proprement l'app (la boucle start-player.sh la rouvre). 'clean-exit'
  // = sortie normale → ignoré. ⚠️ Garde anti-boucle OBLIGATOIRE sur RPi : l'init GPU
  // egl/Passthrough (Mesa/V3D) peut FATAL au boot et entraîner des render-process-gone en
  // rafale ; relancer dessus ferait une boucle de redémarrage. On ne relance donc QUE si
  // l'app tourne depuis >60s (crash en fonctionnement = écran figé à récupérer) ; un crash
  // précoce est laissé à la boucle start-player.sh (qui temporise 3s) + repli software.
  mainWindow.webContents.on('render-process-gone', (_e, details) => {
    const reason = details && details.reason;
    if (reason === 'clean-exit') return;
    if (process.uptime() < 60) {
      _writeLog('renderer:crash', `render-process-gone reason=${reason} uptime=${process.uptime().toFixed(0)}s <60s → pas de relaunch (anti-boucle GPU)`);
      return;
    }
    _writeLog('renderer:crash', `render-process-gone reason=${reason} → relaunch`);
    try { app.relaunch(); } catch {}
    app.exit(0);
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

// ── WebSocket ─────────────────────────────────────────────────────────────────

let lastPongAt = 0;
let reconnectAttempts = 0;

// Delai avant la prochaine tentative : backoff exponentiel 2s/4s/8s/16s/30s, tire au
// hasard dans la MOITIE HAUTE de sa borne (« equal jitter »).
//
// Sans ce tirage, le backoff est entierement deterministe : apres une coupure de courant
// ou un redemarrage serveur, tous les ecrans repartent au meme instant, echouent au meme
// instant, et retentent a exactement 2s, 4s, 8s... Leurs tentatives restent EN PHASE, et
// la rafale se rejoue a chaque palier au lieu de se disperser. Mesure serveur : ~20 s de
// latence en pointe sur le premier player:sync a 400 reconnexions simultanees.
//
// La moitie basse est ecartee volontairement : un jitter « plein » (0 a la borne) peut
// rendre un delai quasi nul et faire marteler un serveur qui vient a peine de repartir.
// Fenetre d'etalement de la TOUTE PREMIERE connexion apres le demarrage (P3.12).
// Le jitter du backoff ne sert que si le serveur est injoignable ; quand un site retrouve
// le courant et que le serveur, lui, n'a jamais bouge, la premiere connexion reussit du
// premier coup et les ecrans arrivent GROUPES -- d'autant plus qu'un parc homogene boote
// en meme temps. On tire ici un retard par ecran pour disperser ce pic.
// Cout assume : quelques secondes avant le retour a l'antenne d'un ecran redemarre seul.
const STARTUP_SPREAD_MS = 5000;
let _startupSpreadDone = false;
function reconnectDelayMs(attempts) {
  const ceiling = Math.min(30000, 2000 * Math.pow(2, attempts));
  return Math.round(ceiling / 2 + Math.random() * (ceiling / 2));
}
const PONG_TIMEOUT_MS = 25000;

async function registerPlayer() {
  try {
    console.log('[WS] Provisioning token — registering player...');
    const res = await fetch(`${config.backend_url}/api/v1/players/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        player_name: os.hostname(),
        player_os:   `linux-${os.arch()}`,
        player_version: PLAYER_VERSION,
        org_token:   config.player_token,
      }),
    });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    const data = await res.json();
    config.player_token = data.player_token;
    saveConfig();
    console.log('[WS] Registration OK — connecting with JWT');
    connectWebSocket();
  } catch (e) {
    console.error('[WS] Registration error:', e.message);
    const delay = reconnectDelayMs(reconnectAttempts);
    reconnectAttempts++;
    reconnectTimer = setTimeout(connectWebSocket, delay);
  }
}

// File d'événements sortants (player → serveur), ex. alerte url_display inaccessible (§XXV.2).
// Si le serveur est joignable on envoie tout de suite ; sinon on met en file et on rejoue à la
// reconnexion (flushEventQueue dans ws.on('open')). Bornée pour ne pas gonfler hors-ligne.
let _eventQueue = [];
function reportPlayerEvent(ev) {
  if (ws && ws.readyState === 1 /* OPEN */) {
    try { ws.send(JSON.stringify({ type: 'player:event', ...ev })); return; } catch (e) {}
  }
  _eventQueue.push(ev);
  if (_eventQueue.length > 100) _eventQueue.shift();
}
function flushEventQueue() {
  const q = _eventQueue.splice(0);
  for (const ev of q) reportPlayerEvent(ev);
}

// ── Proof of Play (§XXIV) — file persistée + flush HTTP vers /pop/batch ─────────
let _popQueue = [];
let _popFlushing = false;
let _popPrivateKey = null;     // clé privée Ed25519 (KeyObject) — ne quitte jamais l'appareil
let _popPublicKeyPem = null;   // clé publique PEM (envoyée au serveur, TOFU)
let _popPlayerId = null;       // id_player décodé du JWT (cache, pour le message signé)

// Charge ou génère la paire Ed25519 device (persistée). La privée signe les preuves.
function ensurePopKey() {
  try {
    if (fs.existsSync(POP_KEY_PATH)) {
      const j = JSON.parse(fs.readFileSync(POP_KEY_PATH, 'utf8'));
      _popPrivateKey = crypto.createPrivateKey(j.private_pem);
      _popPublicKeyPem = j.public_pem;
      return;
    }
    const { publicKey, privateKey } = crypto.generateKeyPairSync('ed25519');
    _popPublicKeyPem = publicKey.export({ type: 'spki', format: 'pem' }).toString();
    const privPem = privateKey.export({ type: 'pkcs8', format: 'pem' }).toString();
    _popPrivateKey = privateKey;
    fs.writeFileSync(POP_KEY_PATH, JSON.stringify({ public_pem: _popPublicKeyPem, private_pem: privPem }));
    console.log('[PoP] clé device Ed25519 générée');
  } catch (e) { console.error('[PoP] keygen échouée:', e.message); }
}

function _popDecodePlayerId() {
  if (_popPlayerId != null) return _popPlayerId;
  try {
    const parts = (config.player_token || '').split('.');
    if (parts.length < 2) return null;
    const sub = JSON.parse(Buffer.from(parts[1], 'base64').toString()).sub;
    _popPlayerId = sub != null ? Number(sub) : null;
  } catch { _popPlayerId = null; }
  return _popPlayerId;
}

// Signature base64 d'une preuve (message canonique IDENTIQUE au serveur, played_at en ms epoch).
function _signPop(o) {
  if (!_popPrivateKey) return null;
  const idPlayer = _popDecodePlayerId();
  if (idPlayer == null) return null;
  try {
    const ms = Date.parse(o.played_at) || Date.now();
    const msg = `${idPlayer}|${o.id_media ?? ''}|${o.id_playlist ?? ''}|${ms}|${o.duration_ms}`;
    return crypto.sign(null, Buffer.from(msg), _popPrivateKey).toString('base64');
  } catch { return null; }
}

function loadPopQueue() {
  try {
    const raw = fs.readFileSync(POP_QUEUE_PATH, 'utf8');
    const arr = JSON.parse(raw);
    _popQueue = Array.isArray(arr) ? arr : [];
  } catch { _popQueue = []; }
}

function persistPopQueue() {
  try { fs.writeFileSync(POP_QUEUE_PATH, JSON.stringify(_popQueue)); }
  catch (e) { console.error('[PoP] persist échouée:', e.message); }
}

// Enregistre une lecture remontée par le renderer. Persiste tout de suite (anti-perte au reboot).
function recordPlay(entry) {
  if (!entry || typeof entry !== 'object') return;
  if (entry.id_media == null && entry.id_playlist == null) return;
  const o = {
    id_media: entry.id_media ?? null,
    id_playlist: entry.id_playlist ?? null,
    duration_ms: Math.max(0, Math.round(entry.duration_ms || 0)),
    played_at: entry.played_at || new Date().toISOString(),
    // Zone recouverte par un overlay plein ecran au demarrage du media (migr 0125) :
    // spot de campagne P3.6 ou takeover VOD. Voyage jusqu'au serveur, qui conserve la
    // preuve mais ne la compte jamais comme de l'antenne. HORS du message signe (le
    // player est deja la racine de confiance de l'existence meme des preuves).
    covered: !!entry.covered,
  };
  o.signature = _signPop(o);   // P2 : signe la preuve (null si pas de clé/token → stockée non signée)
  _popQueue.push(o);
  if (_popQueue.length > POP_QUEUE_MAX) {
    const drop = _popQueue.length - POP_QUEUE_MAX;
    _popQueue.splice(0, drop);
    console.log(`[PoP] file pleine — ${drop} preuve(s) la/les plus ancienne(s) abandonnée(s)`);
  }
  persistPopQueue();
}

// Flush best-effort : envoie un lot, ne retire de la file QUE si le serveur a accepté (offline → on garde).
async function flushPopQueue() {
  if (_popFlushing || !_popQueue.length) return;
  if (!config.backend_url || !config.player_token) return;
  if (config.player_token.startsWith('sfp_')) return;  // pas encore enregistré (token de provisioning)
  _popFlushing = true;
  try {
    const batch = _popQueue.slice(0, POP_BATCH_MAX);
    const res = await fetch(`${config.backend_url}/api/v1/pop/batch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${config.player_token}` },
      body: JSON.stringify({ logs: batch, pop_public_key: _popPublicKeyPem }),  // P2 : clé publique (TOFU)
      signal: AbortSignal.timeout(15000),
    });
    if (res.ok) {
      _popQueue.splice(0, batch.length);   // retire UNIQUEMENT le lot accepté
      persistPopQueue();
      console.log(`[PoP] ${batch.length} preuve(s) envoyée(s), ${_popQueue.length} en attente`);
    } else {
      console.log(`[PoP] flush refusé (HTTP ${res.status}) — ${_popQueue.length} en attente`);
    }
  } catch {
    // hors-ligne / serveur down → on garde la file pour le prochain tick
  } finally {
    _popFlushing = false;
  }
}

function connectWebSocket() {
  // Etalement d'entree : une seule fois, au premier appel apres le demarrage. Les
  // reconnexions suivantes passent par reconnectDelayMs(), qui a son propre jitter.
  if (!_startupSpreadDone) {
    _startupSpreadDone = true;
    const spread = Math.floor(Math.random() * STARTUP_SPREAD_MS);
    if (spread > 0) {
      console.log('[WS] Startup spread: ' + spread + 'ms');
      reconnectTimer = setTimeout(connectWebSocket, spread);
      return;
    }
  }
  if (!config.backend_url || !config.player_token) {
    mainWindow?.webContents.send('player:setup-required');
    return;
  }

  if (config.player_token.startsWith('sfp_')) {
    registerPlayer();
    return;
  }

  const wsUrl = `${config.backend_url.replace(/^http/, 'ws')}/api/v1/ws/player?token=${config.player_token}`;

  // Vérification TLS du WebSocket ACTIVÉE par défaut (anti-MITM en prod = vrai cert LE).
  // Opt-out explicite par player via config.json `allow_insecure_tls: true` (endpoint self-signed/dev
  // uniquement). Un backend_url en http:// (ws://, pas de TLS) n'est pas concerné par cette option.
  // handshakeTimeout : sans lui, si le serveur tombe brutalement (pas de FIN TCP) et
  // qu'on tente de reconnecter pendant son redémarrage, le TCP peut s'établir mais
  // l'upgrade WS ne jamais répondre → la connexion PEND indéfiniment (ni open/error/close,
  // heartbeat jamais démarré, aucun reconnect programmé) → player figé jusqu'au reboot.
  // Avec le timeout, un handshake qui traîne → error → close → reconnect (backoff).
  ws = new WebSocket(wsUrl, { rejectUnauthorized: config.allow_insecure_tls !== true, handshakeTimeout: 15000 });

  ws.on('open', () => {
    console.log('[WS] Connected to backend');
    lastPongAt = Date.now();
    reconnectAttempts = 0;
    clearTimeout(reconnectTimer);
    // Le serveur reprend le pilotage ecran (re-assertion via reset last_power_state
    // au connect) → le pilote offline s'efface et oublie son dernier etat applique.
    _wsDownSince = null;
    _offlineAppliedPower = null;
    mainWindow?.webContents.send('player:connected');
    startHeartbeat();
    flushEventQueue();   // rejoue les alertes (ex. url_display inaccessible) accumulées offline
  });

  ws.on('pong', () => {
    lastPongAt = Date.now();
  });

  ws.on('message', (data) => {
    try {
      handleServerMessage(JSON.parse(data.toString()));
    } catch (e) {
      console.error('[WS] Invalid message:', e.message);
    }
  });

  ws.on('close', (code, reason) => {
    console.log(`[WS] Disconnected (${code})`);
    stopHeartbeat();
    if (!_wsDownSince) _wsDownSince = Date.now();  // debut de la fenetre offline (pilote ecran)
    mainWindow?.webContents.send('player:disconnected');
    // Exponential backoff: 2s, 4s, 8s, 16s, 30s max
    const delay = reconnectDelayMs(reconnectAttempts);
    reconnectAttempts++;
    reconnectTimer = setTimeout(connectWebSocket, delay);
  });

  ws.on('error', (err) => {
    console.error('[WS] Error:', err.message);
  });
}

// NTP piloté depuis l'UI (relais NTP par groupe LAN sync). Applique role/server via le
// helper root (écrit le bloc chrony + restart). Linux/RPi only.
// Chemin 1 (NNP-immune) : requête fichier + `systemctl start signflow-ntp-apply.service`
// (unité oneshot autorisée par polkit, D-Bus) — après un app.relaunch() OTA, sudo est
// BLOQUÉ (NoNewPrivs hérité) mais pas D-Bus (même pattern que le fuseau/timedated).
// Repli : helper sudo historique (parc sans l'unité/la règle polkit, sessions pré-relaunch).
let _appliedNtp = null;
function applyNtpConfig(dc) {
  const role   = (dc && dc.ntp_role) || 'none';
  const server = (dc && dc.ntp_server) || '';
  const sig = `${role}|${server}`;
  if (sig === _appliedNtp) return;   // déjà appliqué → pas de restart chrony inutile
  _appliedNtp = sig;
  const legacySudo = () => {
    try {
      execFile('sudo', ['/usr/local/bin/signflow-ntp-apply', role, server], { timeout: 15000 },
        (err, _stdout, stderr) => {
          if (err) console.error('[NTP] apply error:', err.message, (stderr || '').trim());
          else console.log(`[NTP] applied role=${role} server=${server || '-'} (sudo)`);
        });
    } catch (e) {
      console.error('[NTP] apply failed:', e.message);
    }
  };
  try {
    fs.writeFileSync('/var/lib/signflow/ntp_request', `${role} ${server}\n`);
    execFile('systemctl', ['start', 'signflow-ntp-apply.service'], { timeout: 15000 },
      (err, _stdout, stderr) => {
        if (err) {
          console.error('[NTP] systemd path failed:', err.message, (stderr || '').trim());
          legacySudo();
        } else {
          console.log(`[NTP] applied role=${role} server=${server || '-'} (systemd/polkit)`);
        }
      });
  } catch (e) {   // /var/lib/signflow absent (install pré-polkit) → repli sudo
    legacySudo();
  }
}

// Réécrit le payload player:sync pour servir depuis le store local (signflow://) les
// médias vidéo/image déjà en cache, et met en file ceux qui manquent (téléchargés en
// fond). Les médias non cachés gardent leur URL MinIO → repli streaming gracieux.
function rewriteSyncForStore(msg) {
  if (!contentStore) return;
  const pinned = [];   // tout le contenu épinglé (vidéo + image + audio) → jamais évincé
  const fpKeys = [];   // empreinte barrière LAN = vidéo/image seulement
  _fallbackReady = new Set();   // recalculé à chaque sync (repli parent → prêt)

  const handle = (m, isVisual) => {
    if (!m || !m.cache_key || !m.url) return;
    const srcUrl = m.url;   // URL d'origine (MinIO) — source du téléchargement, à préserver
    pinned.push(m.cache_key);
    if (isVisual) fpKeys.push(m.cache_key);
    const filekey = contentStore.keyFor(m.cache_key);
    if (contentStore.verify(filekey)) {   // garde runtime : fichier réellement présent+intègre
      m.url = contentStore.localUrl(filekey);
      return;
    }
    // Variante servie pas (encore) en cache. Repli : si un parent de repli est caché
    // (ex. 4K d'une config antérieure), le jouer plutôt qu'un écran noir (typiquement
    // serveur down + variante non téléchargée). On met quand même la variante en file.
    if (m.cache_key_fallback) {
      const fbk = contentStore.keyFor(m.cache_key_fallback);
      if (contentStore.verify(fbk)) {
        m.url = contentStore.localUrl(fbk);
        _fallbackReady.add(m.cache_key);   // compte comme prêt pour la barrière LAN
      }
    }
    contentStore.enqueue({
      cache_key: m.cache_key, cache_checksum: m.cache_checksum,
      mime: m.mime_type, url: srcUrl, id_media: m.id_media,
    });
  };

  // Traite le média d'un item : vidéo/image/document → cache direct ; feed_display → vignettes.
  const handleItem = (item, isVisual) => {
    const m = item && item.media;
    if (!m) return;
    // Visuel cachable offline-first : vidéo, image ET document (PDF). Le document fait partie
    // de l'empreinte barrière LAN comme une image (tous les écrans du mur doivent l'avoir).
    if (m.media_type === 'video' || m.media_type === 'image' || m.media_type === 'document') handle(m, isVisual);
    // Feed Display : chaque item porte une vignette re-hébergée (image_cache_key) → cache
    // offline-first (HORS empreinte LAN) ; on bascule image_url sur signflow:// si présente.
    else if (m.media_type === 'feed_display' && m.feed_config && Array.isArray(m.feed_config.items)) {
      for (const fi of m.feed_config.items) {
        if (!fi || !fi.image_cache_key || !fi.image_url) continue;
        const proxy = { cache_key: fi.image_cache_key, url: fi.image_url, mime_type: 'image/jpeg', id_media: m.id_media };
        handle(proxy, false);
        fi.image_url = proxy.url;   // signflow:// si caché, sinon URL signée MinIO conservée
      }
    }
  };

  for (const item of (msg.items || [])) handleItem(item, true);
  // §25.4 multi-zones : patcher AUSSI les items du bloc template. Après sérialisation JSON ce
  // sont des objets DISTINCTS de msg.items, et le renderer multi-zones lit template.zones[].items
  // (pas msg.items) → sans ça une zone vidéo DÉJÀ cachée garde l'URL MinIO et streame (écran noir
  // serveur coupé). Même cache_key que msg.items (déjà épinglé/enfilé) → isVisual=false : la
  // barrière LAN reste pilotée par msg.items, le multi-zones a son propre moteur de sync.
  if (msg.template && Array.isArray(msg.template.zones)) {
    for (const z of msg.template.zones) {
      for (const item of (z.items || [])) handleItem(item, false);
    }
  }
  for (const b of (msg.audio_baskets || [])) {
    for (const it of (b.items || [])) handle(it, false);   // audio des paniers (§XIX)
  }
  // VOD à la demande (Feature B) : pré-cache des médias VOD (variante navigateur-safe) — pin +
  // enqueue, HORS empreinte LAN (isVisual=false) → lecture instantanée locale au déclenchement.
  for (const m of (msg.vod_media || [])) handle(m, false);

  contentStore.setPinned(pinned);  // épingle (vidéo+image+audio) + évince l'ancien si disque tendu
  // Look-ahead (Phase 1) : pré-télécharge le contenu programmé futur (priorisé par date de
  // diffusion). Après setPinned → les items courants restent non-prefetch (DL prioritaire).
  contentStore.setPrefetch(msg.prefetch || []);
  _pinnedKeys = fpKeys;            // barrière LAN = visuel uniquement (l'audio ne bloque pas)
  _currentFp = fpKeys.length
    ? crypto.createHash('sha1').update(fpKeys.slice().sort().join('|')).digest('hex')
    : null;
  updateLanReadiness();
}

// Recalcule et déclare au LAN si j'ai TOUT le contenu de l'empreinte courante en cache.
// Appelé à chaque sync ET à chaque download terminé (la readiness passe à true quand le
// dernier octet arrive → le leader rouvre la barrière).
function updateLanReadiness() {
  if (!lanSync || !contentStore) return;
  const ready = _pinnedKeys.every((ck) =>
    contentStore.verify(contentStore.keyFor(ck)) || _fallbackReady.has(ck));
  lanSync.setReadiness(_currentFp, ready);
}

// Toutes les VIDÉOS du payload sont-elles cachées ? (après rewriteSyncForStore, une vidéo cachée
// a une url signflow://). Non-vidéos ignorées : le gate ne porte QUE sur les vidéos.
function _localVideosCached(msg) {
  if (!msg || !Array.isArray(msg.items)) return true;
  return msg.items.every((it) =>
    !it.media || it.media.media_type !== 'video' ||
    String(it.media.url || '').startsWith('signflow://'));
}
function _downloadGateActive(msg) {
  return !_allowStreaming && Array.isArray(msg.items) && msg.items.length > 0 && !_localVideosCached(msg);
}
function _emitSyncToRenderer(msg) {
  lastSyncState = {
    id_playlist:     msg.id_playlist ?? (msg.id_event != null ? -msg.id_event : null),
    items:           msg.items ?? [],
    currentIndex:    msg.start_index ?? 0,
    currentOffsetMs: msg.start_offset_ms ?? 0,
    syncedAt:        Date.now(),
  };
  if (lanSync) lanSync.forceSyncNow();
  if (lanSync && _currentFp && !_releasedFps.has(_currentFp)) msg.lan_hold = true;
  mainWindow?.webContents.send('player:sync', msg);
}

// ── OTA §XXX slice 2 + 2c : MAJ pilotée par le player (fenêtres + install programmée) ─────────
// Le serveur joint un bloc `update` au player:sync tant que la cible est non terminale. Le player
// PILOTE une machine à états : (1) DOWNLOAD de l'artefact dans `download_window` (repris via HTTP
// Range si interrompu — survit aux coupures/liens lents), (2) vérif sha256 → `staged`, (3) INSTALL
// (extract/relaunch) UNIQUEMENT dans `install_window` (évite un reboot en pleine journée). `force`
// = ignorer les fenêtres (tout de suite). Sans download_url = git self-update (rien à stager →
// direct à l'install en fenêtre). État persisté dans `config.ota` (survit aux relaunch). Timer de
// ré-évaluation (60s) pour ne pas dépendre de la cadence des syncs. Backward-safe : inerte sans bloc.
let _otaInProgress = false;
let _otaTimer = null;

function _sendOtaStatus(id_deployment, status, error) {
  try {
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'update:status', id_deployment, status,
        version: PLAYER_VERSION, error: error || undefined,
      }));
    }
  } catch {}
}

function _semverGte(a, b) {
  const p = (v) => String(v || '0').split('.').map((n) => parseInt(n, 10) || 0);
  const av = p(a), bv = p(b);
  for (let i = 0; i < 3; i++) {
    if ((av[i] || 0) !== (bv[i] || 0)) return (av[i] || 0) > (bv[i] || 0);
  }
  return true;  // égales
}

// Fenêtre "HH:MM-HH:MM" en HEURE LOCALE du site (= horloge du player). null/invalide = toujours
// autorisé. Gère le passage minuit (start > end, ex. 23:00-04:00).
function _inWindow(win) {
  if (!win) return true;
  const m = /^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$/.exec(String(win));
  if (!m) return true;
  const now = new Date();
  const cur = now.getHours() * 60 + now.getMinutes();
  const s = (+m[1]) * 60 + (+m[2]);
  const e = (+m[3]) * 60 + (+m[4]);
  return s <= e ? (cur >= s && cur < e) : (cur >= s || cur < e);
}

function _otaPartPath() { return path.join(app.getPath('userData'), 'ota_download.part'); }
function _otaStagedPath() { return path.join(app.getPath('userData'), 'ota_staged.bin'); }

function _otaCleanup() {
  for (const p of [_otaPartPath(), _otaStagedPath()]) {
    try { if (fs.existsSync(p)) fs.unlinkSync(p); } catch {}
  }
}

function handleUpdateDirective(update) {
  if (!_otaTimer) _otaTimer = setInterval(_otaTick, 60 * 1000);  // ré-évalue les fenêtres
  if (!update || typeof update !== 'object' || !update.id_deployment || !update.version) {
    // Plus de directive = déploiement terminé/annulé → abandonne un éventuel update en attente.
    if (config.ota) { _otaCleanup(); delete config.ota; delete config.ota_attempt; saveConfig(); }
    return;
  }
  if (_semverGte(PLAYER_VERSION, update.version)) {
    if (config.ota || config.ota_attempt) {
      _otaCleanup(); delete config.ota; delete config.ota_attempt; saveConfig();
    }
    return;
  }
  // Persiste/rafraîchit l'update en attente (préserve l'état `staged` si la version est inchangée).
  const prevStaged = (config.ota && config.ota.version === update.version) ? !!config.ota.staged : false;
  config.ota = {
    id_deployment: update.id_deployment, version: update.version,
    url: update.download_url || '', sha256: update.sha256 || '',
    download_window: update.download_window || null, install_window: update.install_window || null,
    force: update.force === true, staged: prevStaged,
  };
  saveConfig();
  _otaTick();
}

// Machine à états, ré-évaluée à chaque sync ET toutes les 60s.
function _otaTick() {
  const ota = config.ota;
  if (!ota || !ota.version) return;
  if (_semverGte(PLAYER_VERSION, ota.version)) {
    _otaCleanup(); delete config.ota; delete config.ota_attempt; saveConfig(); return;
  }
  if (_otaInProgress) return;
  const isGit = !ota.url;  // pas d'artefact → git self-update (rien à télécharger/stager)
  const staged = isGit || (ota.staged && fs.existsSync(_otaStagedPath()));
  if (staged) {
    if (ota.force || _inWindow(ota.install_window)) _otaDoInstall(ota, isGit);
  } else if (ota.force || _inWindow(ota.download_window)) {
    _otaDoDownload(ota);
  }
}

// DOWNLOAD resumable (HTTP Range) + windowé : reprend le .part si présent ; s'interrompt proprement
// si la fenêtre se ferme (reprise à la prochaine fenêtre) ; à la fin vérifie sha256 → `staged`.
function _otaDoDownload(ota) {
  _otaInProgress = true;
  // download_url RELATIF (ex "/api/v1/updates/bundles/rpi/2.0.0") = bundle servi par LE serveur
  // du player → résoudre contre backend_url (portable, jamais GitHub). Une URL absolue passe telle quelle.
  const url = /^https?:/i.test(ota.url) ? ota.url
    : `${String(config.backend_url || '').replace(/\/$/, '')}${ota.url}`;
  const part = _otaPartPath();
  let offset = 0;
  try { if (fs.existsSync(part)) offset = fs.statSync(part).size; } catch {}
  let mod;
  try { mod = url.startsWith('https:') ? require('https') : require('http'); }
  catch (e) { _otaInProgress = false; return; }
  const opts = url.startsWith('https:') ? { rejectUnauthorized: config.allow_insecure_tls !== true } : {};
  opts.headers = opts.headers || {};
  // Bundle servi par un endpoint authentifié-player → présenter le token machine (ignoré par MinIO/redirects).
  if (config.player_token) opts.headers.Authorization = `Bearer ${config.player_token}`;
  if (offset > 0) opts.headers.Range = `bytes=${offset}-`;
  _sendOtaStatus(ota.id_deployment, 'downloading');
  const req = mod.get(url, opts, (res) => {
    if ([301, 302, 303, 307, 308].includes(res.statusCode) && res.headers.location) {
      config.ota.url = res.headers.location; saveConfig(); _otaInProgress = false; return;  // suit au prochain tick
    }
    if (res.statusCode !== 200 && res.statusCode !== 206) {
      try { fs.unlinkSync(part); } catch {}  // 416/erreur → repart de zéro au prochain tick
      _otaFail(ota.id_deployment, `download HTTP ${res.statusCode}`); return;
    }
    // Le serveur ignore le Range (200 alors qu'on demandait la suite) → on repart de zéro.
    const append = res.statusCode === 206 && offset > 0;
    if (!append && offset > 0) { try { fs.unlinkSync(part); } catch {} }
    const out = fs.createWriteStream(part, { flags: append ? 'a' : 'w' });
    let stopped = false, lastCheck = Date.now();
    res.on('data', () => {
      if (!ota.force && Date.now() - lastCheck > 3000) {  // fenêtre fermée en cours → arrêt propre
        lastCheck = Date.now();
        if (!_inWindow(ota.download_window)) { stopped = true; req.destroy(); out.end(); }
      }
    });
    res.pipe(out);
    out.on('finish', () => {
      if (stopped) { _otaInProgress = false; _writeLog('ota', 'download hors fenêtre → pause (reprise en fenêtre)'); return; }
      if (ota.sha256) {
        try {
          const h = require('crypto').createHash('sha256').update(fs.readFileSync(part)).digest('hex');
          if (h.toLowerCase() !== String(ota.sha256).toLowerCase()) {
            _otaFail(ota.id_deployment, `sha256 invalide (calculé ${h.slice(0, 12)}…)`, part); return;
          }
        } catch (e) { _otaFail(ota.id_deployment, `hash: ${e.message}`, part); return; }
      }
      try { fs.renameSync(part, _otaStagedPath()); }
      catch (e) { _otaFail(ota.id_deployment, `stage: ${e.message}`); return; }
      config.ota.staged = true; saveConfig();
      _sendOtaStatus(ota.id_deployment, 'staged');
      _writeLog('ota', `téléchargé + vérifié → staged (v${ota.version})`);
      _otaInProgress = false;
      _otaTick();  // peut installer tout de suite si on est dans la fenêtre d'install
    });
  });
  req.on('error', (e) => { _otaInProgress = false; _writeLog('ota', `download err (reprise au tick): ${e.message}`); });
  req.setTimeout(30 * 60 * 1000, () => { req.destroy(); _otaInProgress = false; });
}

// INSTALL (dans la fenêtre de maintenance) : anti-boucle, puis git self-update OU extract du bundle
// staged, enfin finalize (relaunch/reboot). Le succès = version remontée au heartbeat.
function _otaDoInstall(ota, isGit) {
  _otaInProgress = true;
  const prev = (config.ota_attempt && config.ota_attempt.version === ota.version)
    ? (config.ota_attempt.count || 0) : 0;
  if (prev >= 2) {
    _sendOtaStatus(ota.id_deployment, 'failed',
      `version ${ota.version} non atteinte après ${prev} tentatives`);
    _otaInProgress = false; return;
  }
  config.ota_attempt = { version: ota.version, count: prev + 1 };
  saveConfig();
  _sendOtaStatus(ota.id_deployment, 'installing');
  _writeLog('ota', `install v${ota.version} (deployment ${ota.id_deployment}) — tentative ${prev + 1}`);
  if (isGit) _runGitApply(ota.id_deployment, ota.version);
  else _runBundleInstall(ota.id_deployment, _otaStagedPath(), ota.version);
}

function _runGitApply(idDep, version) {
  const repoDir = config.update_repo_dir || path.join(__dirname, '..', '..');
  const cmd = String(config.update_command || 'git -C {dir} pull --ff-only')
    .replace(/\{dir\}/g, repoDir).replace(/\{version\}/g, version);
  execFile('/bin/sh', ['-c', cmd], { timeout: 5 * 60 * 1000 }, (err, stdout, stderr) => {
    if (err) return _otaFail(idDep, `git: ${err.message} ${String(stderr).slice(-300)}`);
    _writeLog('ota', `git apply OK (v${version}), finalize`);
    _finalizeUpdate();
  });
}

function _runBundleInstall(idDep, stagedFile, version) {
  const os = require('os');
  const appRoot = config.update_app_dir || path.join(__dirname, '..');
  const backup = path.join(os.tmpdir(), `sf_ota_backup_${process.pid}.tar.gz`);
  execFile('tar', ['-czf', backup, '-C', appRoot, 'package.json', 'src'], (berr) => {
    if (berr) return _otaFail(idDep, `backup: ${berr.message}`);
    execFile('tar', ['-xzf', stagedFile, '-C', appRoot], (xerr, so, se) => {
      if (xerr) {
        execFile('tar', ['-xzf', backup, '-C', appRoot], () => {});  // rollback best-effort
        return _otaFail(idDep, `extract: ${xerr.message} ${String(se).slice(-200)}`);
      }
      try { fs.unlinkSync(backup); } catch {}
      try { fs.unlinkSync(stagedFile); } catch {}
      _writeLog('ota', `bundle v${version} installé, finalize`);
      _finalizeUpdate();
    });
  });
}

// Relaunch pour charger le nouveau code (linux : openbox relance l'app).
function _finalizeUpdate() {
  // RPi : reboot OS (app.relaunch crashe le GPU sous cage) → cage relance le player au boot.
  try {
    const proc = spawn('dbus-send', [
      '--system', '--print-reply', '--dest=org.freedesktop.login1',
      '/org/freedesktop/login1', 'org.freedesktop.login1.Manager.Reboot', 'boolean:false',
    ], { detached: true, stdio: 'ignore' });
    proc.unref();
  } catch (e) { _writeLog('ota', `reboot spawn err: ${e.message}`); }
}

// Échec d'apply : nettoie le temp, journalise, report failed, libère le verrou (pas de finalize).
function _otaFail(id_deployment, msg, tmp) {
  if (tmp) { try { fs.unlinkSync(tmp); } catch {} }
  _writeLog('ota', `échec: ${msg}`);
  _sendOtaStatus(id_deployment, 'failed', String(msg).slice(0, 500));
  _otaInProgress = false;
}

function handleServerMessage(msg) {
  switch (msg.type) {
    case 'player:sync':
      // Store local (offline-first P0) : bascule les médias cachés sur signflow://
      // et met en file les manquants — AVANT de forwarder / mémoriser le payload.
      rewriteSyncForStore(msg);
      // Extraire display_config avant de forwarder au renderer
      if (msg.display_config !== undefined) {
        initDisplayController(msg.display_config);
        manageLocalControl(msg.display_config);  // start/stop serveur controle local (§19.3)
        applyAudioSink(msg.display_config?.audio_basket_output);  // routage sortie audio OS
        applyNtpConfig(msg.display_config);       // NTP relais/client piloté depuis l'UI
        _allowStreaming = msg.display_config?.allow_streaming === true;  // download-then-play strict par défaut
      }
      // Profil Store Control Panel LAN (token + permissions) — None si desactive
      if (msg.local_control !== undefined) {
        localControlProfile = msg.local_control;
      }
      // Médias VOD à la demande (Feature B Phase 3) — exposés au panneau LAN-local
      if (msg.vod_media !== undefined) {
        _vodMedia = Array.isArray(msg.vod_media) ? msg.vod_media : [];
        _vodConfigs = Array.isArray(msg.vod_configs) ? msg.vod_configs : [];
      }
      // Trigger VOD multi-cible LAN (secret partagé + pairs) — pilote le contrôleur UDP 7779
      if (msg.vod_lan !== undefined) {
        _vodLan = (msg.vod_lan && typeof msg.vod_lan === 'object') ? msg.vod_lan : null;
        manageVodLan();
      }
      // OTA §XXX slice 2 : directive de mise à jour (indépendante du download gate ci-dessous).
      if (msg.update) handleUpdateDirective(msg.update);

      // Download-then-play STRICT (anti-stream auto) : retenir tant que les vidéos ne sont pas
      // toutes cachées — le renderer garde le programme courant ; contentStore.onReady forwarde
      // quand tout est prêt (bascule propre).
      if (_downloadGateActive(msg)) {
        _downloadGate = msg;
        console.log('[DownloadGate] nouveau contenu retenu (vidéo non cachée, no-stream) — garde le programme courant');
        break;
      }
      _downloadGate = null;
      _emitSyncToRenderer(msg);
      break;
    case 'player:command':
      handleCommand(msg.command, msg.params || {});
      break;
    case 'player:purge_media':
      // Kill-switch (§ Q5) : suppression réelle des octets du store local + purge du
      // cache HTTP Chromium (médias encore streamés / non basculés sur le store).
      try {
        const removed = contentStore ? contentStore.purgeMedia(msg.id_media) : 0;
        console.log(`[Purge] store: ${removed} fichier(s) supprimé(s) pour média ${msg.id_media}`);
        const ses = mainWindow?.webContents?.session;
        if (ses) ses.clearCache().then(() => console.log('[Purge] HTTP cache cleared for media', msg.id_media));
      } catch (e) { console.log('[Purge] error:', e.message); }
      break;
    case 'token:refresh':
      // Rotation du token machine (« Option B » serveur) : le serveur a renouvelé notre token
      // (expiration approchant / legacy 100 ans à migrer) → on le persiste. La connexion WS
      // courante reste valide ; le nouveau token sert au prochain reconnect ET aux requêtes
      // HTTP (qui relisent config.player_token à chaque appel). Aucune coupure d'écran.
      if (msg.token && typeof msg.token === 'string' && msg.token !== config.player_token) {
        config.player_token = msg.token;
        saveConfig();
        console.log('[Token] renouvelé par le serveur et persisté');
      }
      break;
    default:
      console.log('[WS] Unknown type:', msg.type);
  }
}

function handleCommand(command, params) {
  switch (command) {
    case 'restart':
      app.relaunch();
      app.exit(0);
      break;
    case 'reboot_os': {
      // sudo is blocked after app.relaunch() due to PR_SET_NO_NEW_PRIVS inheritance.
      // Use D-Bus to request reboot from logind (no privilege escalation needed).
      const proc = spawn('dbus-send', [
        '--system', '--print-reply',
        '--dest=org.freedesktop.login1',
        '/org/freedesktop/login1',
        'org.freedesktop.login1.Manager.Reboot',
        'boolean:false',
      ], { detached: true, stdio: 'ignore' });
      proc.unref();
      break;
    }
    case 'reset_config':
      fs.writeFileSync(CONFIG_PATH, JSON.stringify({ backend_url: '', player_token: '' }, null, 2));
      app.relaunch();
      app.exit(0);
      break;
    case 'quit_app':
      app.quit();
      break;
    case 'set_timezone': {
      // Change le fuseau OS (timedatectl ; regle sudoers NOPASSWD posee par install.sh)
      // puis relance l'app : Node/V8 fige la TZ au demarrage — sans relance, le pilote
      // offline auto-power et tout le wall-clock local resteraient sur l'ancien fuseau.
      const tz = params && params.timezone;
      if (!tz || !/^[A-Za-z0-9_+\-/]+$/.test(tz)) { console.error('[TZ] invalid timezone:', tz); break; }
      try {
        try { execSync(`timedatectl set-timezone "${tz}"`, { timeout: 10000 }); }
        catch { execSync(`sudo -n timedatectl set-timezone "${tz}"`, { timeout: 10000 }); }
        console.log(`[TZ] system timezone set to ${tz} — relaunching`);
        app.relaunch();
        app.exit(0);
      } catch (e) {
        console.error('[TZ] set-timezone failed (sudoers rule missing?):', e.message);
      }
      break;
    }
    case 'maintenance':
      mainWindow?.webContents.send('player:maintenance', params);
      break;
    case 'screenshot':
      captureScreenshot(params.request_id);
      break;
    case 'toggle_overlay':
      // Overlay de maintenance : envoie les infos device au renderer qui toggle l'affichage.
      mainWindow?.webContents.send('player:overlay', { device: buildOverlayInfo() });
      break;
    case 'play_now': {
      // VOD à la demande (Feature B) : lecture ad-hoc takeover. On résout le src offline-first
      // (cache local prioritaire — le média a été pré-caché en Phase 1), repli URL signée.
      const p = params || {};
      if (p.mode === 'stop' || !p.id_media) {
        mainWindow?.webContents.send('player:play_now', { mode: 'stop' });
        break;
      }
      let src = p.url || null;
      if (contentStore && p.cache_key) {
        const fk = contentStore.keyFor(p.cache_key);
        if (contentStore.verify(fk)) {
          src = contentStore.localUrl(fk);   // lecture locale instantanée
        } else if (p.url) {
          contentStore.enqueue({ cache_key: p.cache_key, cache_checksum: p.cache_checksum, mime: p.mime_type, url: p.url, id_media: p.id_media });
        }
      }
      if (!src) { console.log('[play_now] aucun src résolu pour média', p.id_media); break; }
      mainWindow?.webContents.send('player:play_now', {
        id_media: p.id_media, media_type: p.media_type || 'video',
        mode: p.mode === 'loop' ? 'loop' : 'once', src, duration_ms: p.duration_ms || null,
        // Relayes tels quels : le defaut (`!== false`) vit dans le renderer.
        with_sound: p.with_sound, has_audio: p.has_audio,
      });
      break;
    }
    case 'detect_capabilities': {
      // Relance la detection HW (ecrit capabilities.json) puis recharge la variable.
      // Le prochain heartbeat (<=10s) remonte les nouvelles capacites au serveur.
      const script = path.join(__dirname, '..', 'scripts', 'detect-capabilities.sh');
      try {
        const proc = spawn('bash', [script, CAPABILITIES_PATH], { stdio: 'ignore' });
        proc.on('close', () => {
          loadCapabilities();
          console.log('[hw-decode] re-detection terminee:', playerCapabilities ? playerCapabilities.hw_decode : 'vide');
        });
        proc.on('error', (e) => { console.warn('[hw-decode] detect script error:', e.message); loadCapabilities(); });
      } catch (e) {
        console.warn('[hw-decode] detect spawn failed:', e.message);
      }
      break;
    }
    case 'display_command': {
      // Commande ecran physique (RS232 ou IP)
      const { display_cmd, ...cmdParams } = params;
      if (display_cmd === 'run_startup_sequence') {
        runStartupSequence();
        break;
      }
      if (!displayController) {
        // Repli « ecran non pilotable » : power_on/power_off coupent/retablissent la
        // sortie video du player lui-meme (l'ecran passe en veille sur perte de signal).
        if (display_cmd === 'power_on' || display_cmd === 'power_off') {
          try {
            setVideoOutputPower(display_cmd === 'power_on');
            if (ws?.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({
                type: 'display:status', command: display_cmd, result: 'ok',
              }));
            }
          } catch (err) {
            console.error(`[Display] video output fallback error:`, err.message);
            if (ws?.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({
                type: 'display:status', command: display_cmd,
                result: 'error', error: err.message,
              }));
            }
          }
          break;
        }
        console.warn('[Display] display_command received but no controller configured');
        if (ws?.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({
            type: 'display:status', command: display_cmd,
            result: 'error', error: 'No display controller configured',
          }));
        }
        break;
      }
      const cfg = serverDisplayConfig ?? {};
      displayController.command(
        cfg.model ?? 'samsung_mdc',
        display_cmd,
        cfg.address ?? 1,
        cmdParams,
      )
        .then(() => {
          console.log(`[Display] ${display_cmd} OK`);
          if (ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
              type: 'display:status', command: display_cmd, result: 'ok',
            }));
          }
        })
        .catch((err) => {
          console.error(`[Display] ${display_cmd} error:`, err.message);
          if (ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
              type: 'display:status', command: display_cmd,
              result: 'error', error: err.message,
            }));
          }
        });
      break;
    }
    default:
      console.log('[WS] Unknown command:', command);
  }
}

// Capture periodique pilotee par display_config.screenshot_interval (secondes).
// Defaut 3600 (1h) ; 0 ou absent<60 => desactivee.
let _screenshotTimer = null;
let _screenshotIntervalSec = null;  // intervalle actif du timer en cours
function applyScreenshotSchedule(dc) {
  const raw = dc?.screenshot_interval;
  const sec = raw === undefined || raw === null ? 3600 : Number(raw);
  const effective = (!sec || sec < 60) ? 0 : sec;  // 0 = desactive
  // NE RIEN relancer si l'intervalle est inchange : sinon chaque player:sync
  // (heartbeat, reapply_time_windows toutes les 30s, events...) remettrait le timer
  // a zero et la capture periodique n'arriverait JAMAIS a echeance.
  if (effective === _screenshotIntervalSec) return;
  _screenshotIntervalSec = effective;
  if (_screenshotTimer) { clearInterval(_screenshotTimer); _screenshotTimer = null; }
  if (!effective) { console.log('[Screenshot] periodic disabled'); return; }
  _screenshotTimer = setInterval(() => captureScreenshot('periodic'), effective * 1000);
  console.log(`[Screenshot] periodic every ${effective}s`);
}

async function captureScreenshot(requestId) {
  if (!mainWindow || !config.backend_url || !config.player_token) return;
  try {
    const image = await mainWindow.webContents.capturePage();
    const pngBuffer = image.toPNG();

    // Decode id_player from JWT payload
    const parts = config.player_token.split('.');
    if (parts.length < 2) return;
    const jwtPayload = JSON.parse(Buffer.from(parts[1], 'base64').toString());
    const idPlayer = jwtPayload.sub;
    if (!idPlayer) return;

    await fetch(`${config.backend_url}/api/v1/players/${idPlayer}/screenshot`, {
      method: 'POST',
      headers: {
        'Content-Type': 'image/png',
        'Authorization': `Bearer ${config.player_token}`,
      },
      body: pngBuffer,
    });
    console.log(`[Screenshot] Uploaded ${pngBuffer.length} bytes (request_id=${requestId})`);
  } catch (e) {
    console.error('[Screenshot]', e.message);
  }
}

// ── Heartbeat ─────────────────────────────────────────────────────────────────

let _prevCpuInfo = null;

function getCpuUsage() {
  const cpus = os.cpus();
  const totals = cpus.map((cpu) => {
    const times = cpu.times;
    return { total: Object.values(times).reduce((a, b) => a + b, 0), idle: times.idle };
  });

  if (!_prevCpuInfo) {
    _prevCpuInfo = totals;
    return 0;
  }

  let totalDiff = 0;
  let idleDiff = 0;
  for (let i = 0; i < totals.length; i++) {
    totalDiff += totals[i].total - _prevCpuInfo[i].total;
    idleDiff += totals[i].idle - _prevCpuInfo[i].idle;
  }
  _prevCpuInfo = totals;

  return totalDiff > 0 ? Math.round((1 - idleDiff / totalDiff) * 1000) / 10 : 0;
}

function getRamUsage() {
  const total = os.totalmem();
  const free = os.freemem();
  return Math.round(((total - free) / total) * 1000) / 10;
}

function getDiskUsage() {
  try {
    const out = execSync("df -P / | awk 'NR==2 {print $5}'", { encoding: 'utf8', timeout: 3000 }).trim();
    const pct = parseInt(out.replace('%', ''), 10);
    return isNaN(pct) ? null : pct;
  } catch {
    return null;
  }
}

function getLanIp() {
  const nets = os.networkInterfaces();
  for (const name of Object.keys(nets)) {
    for (const net of (nets[name] ?? [])) {
      if (net.family === 'IPv4' && !net.internal) return net.address;
    }
  }
  return null;
}

function getMacAddress() {
  const nets = os.networkInterfaces();
  for (const name of Object.keys(nets)) {
    for (const net of (nets[name] ?? [])) {
      if (net.family === 'IPv4' && !net.internal && net.mac !== '00:00:00:00:00:00') {
        return net.mac;
      }
    }
  }
  return null;
}

let _cachedWanIp = null;
let _wanIpFetchedAt = 0;
const WAN_IP_TTL_MS = 5 * 60 * 1000;

async function getWanIp() {
  if (_cachedWanIp && Date.now() - _wanIpFetchedAt < WAN_IP_TTL_MS) return _cachedWanIp;
  try {
    const res = await fetch('https://api.ipify.org?format=json', { signal: AbortSignal.timeout(5000) });
    const data = await res.json();
    _cachedWanIp = data.ip;
    _wanIpFetchedAt = Date.now();
    return _cachedWanIp;
  } catch {
    return _cachedWanIp;
  }
}

// Modes d'affichage réellement supportés (format "WxH@Hz") — remontés au serveur pour que
// le drawer CMS ne propose que des résolutions activables. Parsé via wlr-randr (cage/Wayland)
// avec repli xrandr. Sur RPi, le 4K est éliminé (saccade) → on plafonne à 1920x1080.
let _panelMmCache = null;
let _panelMmTries = 0;
// Taille physique de la dalle (mm), issue de l'EDID. wlr-randr (cage/Wayland) l'expose en
// "Physical size: 530x300 mm" ; repli xrandr ("... 527mm x 296mm"). Remontée dans
// capabilities.panel_mm → pré-remplit la dalle dans l'éditeur de mur. Caché (retente ≤3×).
function getPanelMm() {
  if (_panelMmCache || _panelMmTries >= 3) return _panelMmCache;
  _panelMmTries++;
  try {
    const out = execSync('wlr-randr', { encoding: 'utf8', timeout: 5000 });
    const m = out.match(/Physical size:\s*(\d+)\s*x\s*(\d+)\s*mm/i);
    if (m) {
      const w = parseInt(m[1], 10), h = parseInt(m[2], 10);
      if (w > 0 && h > 0) _panelMmCache = { w, h };
    }
  } catch (e) { /* wlr-randr absent → xrandr */ }
  if (!_panelMmCache) {
    try {
      const out = execSync('xrandr', { encoding: 'utf8', timeout: 5000 });
      for (const ln of out.split('\n')) {
        if (!/\bconnected\b/.test(ln)) continue;
        const m = ln.match(/(\d+)mm\s*x\s*(\d+)mm/);
        if (m) {
          const w = parseInt(m[1], 10), h = parseInt(m[2], 10);
          if (w > 0 && h > 0) { _panelMmCache = { w, h }; break; }
        }
      }
    } catch (e2) { console.warn('[PanelMm] error:', e2.message); }
  }
  if (_panelMmCache) console.log(`[PanelMm] dalle ${_panelMmCache.w}x${_panelMmCache.h} mm (EDID)`);
  return _panelMmCache;
}

let _displayModesCache = null;
let _displayModesTries = 0;
function getDisplayModes() {
  // Retente si vide (écran pas prêt au boot) mais abandonne après 3 essais → repli liste figée.
  if (_displayModesCache && (_displayModesCache.length || _displayModesTries >= 3)) return _displayModesCache;
  _displayModesTries++;
  const modes = [];
  try {
    const out = execSync('wlr-randr', { encoding: 'utf8', timeout: 5000 });
    for (const ln of out.split('\n')) {
      // "    1920x1080 px, 60.000000 Hz (preferred, current)"
      const m = ln.match(/(\d{3,5})x(\d{3,5})\s*px,\s*([\d.]+)\s*Hz/);
      if (m) modes.push(`${m[1]}x${m[2]}@${Math.round(parseFloat(m[3]))}`);
    }
  } catch (e) { /* wlr-randr absent → xrandr */ }
  if (modes.length === 0) {
    try {
      const out = execSync('xrandr', { encoding: 'utf8', timeout: 5000 });
      for (const ln of out.split('\n')) {
        const m = ln.match(/^\s+(\d{3,5})x(\d{3,5})\s+(.+)$/);
        if (!m) continue;
        for (const r of (m[3].match(/\d+\.\d+/g) || [])) modes.push(`${m[1]}x${m[2]}@${Math.round(parseFloat(r))}`);
      }
    } catch (e2) { console.warn('[DisplayModes] error:', e2.message); }
  }
  // RPi : pas de 4K (forçage FHD dans start-player.sh) → ne pas proposer >1920x1080.
  const capped = [...new Set(modes)].filter((s) => {
    const d = s.match(/^(\d+)x(\d+)@/);
    return d && Number(d[1]) <= 1920 && Number(d[2]) <= 1080;
  });
  _displayModesCache = capped;
  console.log(`[DisplayModes] ${capped.length} mode(s) (<=FHD): ${capped.join(', ')}`);
  return _displayModesCache;
}

function buildCapabilities() {
  const modes = getDisplayModes();
  const caps = { ...(playerCapabilities || {}) };
  // Fuseau horaire OS reel (drawer CMS : detection de mismatch avec le site — le
  // pilotage offline auto-power compare ses fenetres locales a CETTE horloge).
  try { caps.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch {}
  if (modes.length > 0) caps.display_modes = modes;
  const panelMm = getPanelMm();
  if (panelMm) caps.panel_mm = panelMm;       // EDID → pré-remplissage dalle dans l'éditeur de mur
  if (contentStore) caps.store = contentStore.getStorageStatus();
  return Object.keys(caps).length > 0 ? caps : undefined;
}

// Infos device pour l'overlay de maintenance (envoyées au renderer au toggle).
function buildOverlayInfo() {
  const caps = playerCapabilities || {};
  const decode = caps.hw_decode
    ? (caps.hw_decode_active ? `HW ${caps.hw_decode}` : 'logiciel')
    : '—';
  const dc = serverDisplayConfig || {};
  return {
    id_player: getPlayerId(),
    hostname: os.hostname(),
    os: process.platform,
    version: PLAYER_VERSION,
    ip: getLanIp(),
    mac: getMacAddress(),
    wan_ip: _cachedWanIp,
    decode,
    lan: {
      enabled: !!dc.lan_sync_enabled,
      group: dc.lan_sync_group || null,
      master: !!dc.lan_sync_master,
    },
  };
}

function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(async () => {
    if (ws?.readyState === WebSocket.OPEN) {
      if (Date.now() - lastPongAt > PONG_TIMEOUT_MS) {
        console.log('[WS] Pong timeout — network lost, terminating');
        ws.terminate();
        return;
      }
      ws.ping();
      ws.send(JSON.stringify({
        type: 'player:status',
        status: 'online',
        cpu: getCpuUsage(),
        ram: getRamUsage(),
        disk: getDiskUsage(),
        ip_address: getLanIp(),
        mac_address: getMacAddress(),
        wan_ip: await getWanIp(),
        player_version: PLAYER_VERSION,
        capabilities: buildCapabilities(),
      }));
    }
  }, 10000);
}

function stopHeartbeat() {
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

// ── Lifecycle ─────────────────────────────────────────────────────────────────

// Allow audio autoplay without a prior user gesture (required in Electron kiosk mode)
// Décodage vidéo matériel — lit capabilities.json (écrit par install.sh/ps1),
// applique les flags Chromium détectés (gardé, réversible) et garde le bloc pour
// le remonter au serveur dans player:status. Sur RPi les flags V4L2 sont déjà sur
// la CLI cage → chromium_flags est vide ici (pas de double application).
function loadCapabilities() {
  try {
    playerCapabilities = JSON.parse(fs.readFileSync(CAPABILITIES_PATH, 'utf8'));
  } catch {
    playerCapabilities = null;
  }
  return playerCapabilities;
}

function applyDecodeFlags(caps) {
  if (!caps || caps.sf_hw_decode === 'off') return;
  const flags = Array.isArray(caps.chromium_flags) ? caps.chromium_flags : [];
  for (const f of flags) {
    if (typeof f !== 'string' || !f.startsWith('--')) continue;
    const eq = f.indexOf('=');
    try {
      if (eq === -1) app.commandLine.appendSwitch(f.slice(2));
      else app.commandLine.appendSwitch(f.slice(2, eq), f.slice(eq + 1));
    } catch (e) {
      console.warn('[hw-decode] flag ignore', f, e.message);
    }
  }
  if (flags.length) console.log(`[hw-decode] ${flags.length} flag(s) de decodage appliques`);
}

app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required');
try {
  applyDecodeFlags(loadCapabilities());
} catch (e) {
  console.warn('[hw-decode] detection echouee:', e.message);
}

// Verrou single-instance : empêche qu'un 2e process player (donc un 2e runtime audio)
// coexiste avec celui qui tourne déjà (cf. « Redémarrer l'app »).
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
}

// ── Watchdog mémoire (F3) : auto-relance avant OOM ──────────────────────────────
// Un player isolé (ex. RPi 4 Go) peut voir sa RAM dériver sur une longue session (offline
// prolongé, fuite/churn Chromium). Sans filet il gèle DUR → SSH mort → power-cycle physique
// (impossible sur un parc mondial). On surveille la mémoire DISPONIBLE et on relance
// proprement AVANT le gel ; on logge la mémoire (1/min) pour diagnostiquer la fuite a posteriori.
let _memLowTicks = 0;
function _availableMemMb() {
  // Linux : os.freemem() = MemFree (≈0 même sain car le cache compte comme utilisé). On lit
  // MemAvailable (mémoire réellement récupérable) — sinon faux positifs permanents.
  if (process.platform === 'linux') {
    try {
      const m = fs.readFileSync('/proc/meminfo', 'utf8').match(/^MemAvailable:\s+(\d+)\s*kB/m);
      if (m) return Math.round(parseInt(m[1], 10) / 1024);
    } catch {}
  }
  return Math.round(os.freemem() / 1048576);
}
function _appMemMb() {
  // Somme des working sets de TOUS les process de l'app (main + renderer + GPU) = là où
  // vit la fuite (le renderer Chromium, pas le main) → utile au diagnostic.
  try { return Math.round(app.getAppMetrics().reduce((s, p) => s + (p.memory?.workingSetSize || 0), 0) / 1024); }
  catch { return 0; }
}
function startMemoryWatchdog() {
  const totalMb = Math.round(os.totalmem() / 1048576);
  const envMin = parseInt(process.env.SF_MEM_MIN_MB || '', 10);
  const minMb = envMin > 0 ? envMin : Math.max(250, Math.round(totalMb * 0.06));
  let ticks = 0;
  console.log(`[MemWatchdog] total=${totalMb}MB seuil_relance=${minMb}MB (avail<seuil x3)`);
  setInterval(() => {
    const avail = _availableMemMb();
    ticks++;
    if (ticks % 2 === 0) console.log(`[Mem] avail=${avail}MB/${totalMb}MB app=${_appMemMb()}MB`);  // ~1/min
    if (avail < minMb) {
      _memLowTicks++;
      console.log(`[MemWatchdog] mémoire basse ${avail}MB < ${minMb}MB (${_memLowTicks}/3)`);
      if (_memLowTicks >= 3) {
        console.log('[MemWatchdog] seuil critique persistant → relance préventive (anti-OOM)');
        app.relaunch();
        app.exit(0);
      }
    } else {
      _memLowTicks = 0;
    }
  }, 30000);
}

app.whenReady().then(() => {
  initFileLog();
  powerSaveBlocker.start('prevent-display-sleep');
  startMemoryWatchdog();   // filet anti-OOM + log mémoire diagnostic

  // ── Local Content Store (offline-first P0) ───────────────────────────────────
  // Sert les octets locaux via signflow:// (Range natif = seek vidéo). Le renderer
  // bascule sur le local quand un média est prêt ; les téléchargements finis
  // patchent les items en place (canal player:store-update), sans coupure.
  contentStore = new ContentStore(app.getPath('userData'), (m) => console.log(`[Store] ${m}`));
  protocol.registerFileProtocol('signflow', (request, callback) => {
    try {
      const filekey = new URL(request.url).pathname.replace(/^\/+/, '');
      const f = contentStore.fileFor(filekey);
      if (f) callback({ path: f.path, headers: { 'Content-Type': f.mime } });
      else callback({ statusCode: 404 });
    } catch { callback({ statusCode: 400 }); }
  });
  contentStore.onReady = ({ cache_key, url }) => {
    // Un média vient d'arriver en cache → patcher les items du renderer en place.
    mainWindow?.webContents.send('player:store-update', { cache_key, url });
    updateLanReadiness();  // barrière LAN : le dernier octet peut rendre le groupe prêt
    // Download-then-play : si un contenu est retenu, re-vérifier — quand TOUTES ses vidéos sont
    // cachées, on forwarde (bascule propre du programme courant vers le nouveau).
    if (_downloadGate) {
      rewriteSyncForStore(_downloadGate);
      if (_localVideosCached(_downloadGate)) {
        const m = _downloadGate;
        _downloadGate = null;
        console.log('[DownloadGate] toutes les vidéos cachées → bascule vers le nouveau programme');
        _emitSyncToRenderer(m);
      }
    }
  };

  loadConfig();
  ensurePopKey();                                  // Proof of Play : clé device Ed25519 (P2)
  loadPopQueue();                                  // Proof of Play : reprend la file persistée
  setInterval(flushPopQueue, POP_FLUSH_INTERVAL);  // flush périodique best-effort (offline-first)
  startOfflinePowerScheduler();  // pilote auto-power offline (lit serverDisplayConfig a chaque tick)
  createWindow();
  mainWindow.webContents.once('did-finish-load', () => {
    // Charger la config ecran persistee pour le power schedule offline
    const cachedCfg = loadDisplayConfig();
    if (cachedCfg) {
      initDisplayController(cachedCfg);
      manageLocalControl(cachedCfg);  // serveur controle local dispo des le boot offline (§19.3)
      applyAudioSink(cachedCfg.audio_basket_output);  // routage sortie audio au boot
      manageLanSync(cachedCfg);       // sync LAN inter-players dès le boot offline (pair-à-pair, sans serveur)
    }
    connectWebSocket();
  });
  globalShortcut.register('CommandOrControl+Shift+D', () => {
    mainWindow?.webContents.toggleDevTools();
  });

  // Position de lecture video remontee par le renderer (pour le LAN sync precis)
  ipcMain.on('player:playback-pos', (_event, pos) => {
    lastPlaybackPos = { ...pos, reportedAt: Date.now() };
  });

  // Etat audio remonte par le renderer (cache pour le Store Control Panel §19.3)
  ipcMain.on('control:status', (_event, status) => {
    lastAudioStatus = status || {};
  });

  // Evenement remonte par le renderer (ex. url_display inaccessible §XXV.2) → serveur (ou file).
  ipcMain.on('player:report-event', (_event, ev) => {
    if (ev && typeof ev === 'object') reportPlayerEvent(ev);
  });

  // Proof of Play (§XXIV) : lecture remontée par le renderer → file persistée + flush différé.
  ipcMain.on('player:report-play', (_event, entry) => recordPlay(entry));

  // Re-sync demande par le renderer (erreur media = URL MinIO signee expiree).
  // Force une reconnexion WS → le serveur renvoie un player:sync avec URLs fraiches.
  ipcMain.on('player:request-resync', () => {
    // Le resync (URLs MinIO signées expirées) n'a de sens que si le WS est VIVANT : on le coupe
    // pour forcer un player:sync avec des URLs fraîches. Si le WS est DÉJÀ down / en reconnexion
    // (serveur coupé, ou « à moitié coupé » backend down + nginx up → handshake 502), réinitialiser
    // reconnectAttempts=0 + reconnecter tout de suite DÉFAIT le backoff exponentiel → on martèle la
    // reconnexion à chaque erreur média de frontière (variante non cachée + serveur down) → jank de
    // l'event-loop → perte des broadcasts du master LAN → flip-flop d'élection (oscillation au début
    // de chaque vidéo). Offline : on ignore — le backoff en cours ramènera des URLs fraîches au
    // retour serveur.
    if (ws?.readyState !== WebSocket.OPEN) {
      console.log('[WS] Resync ignoré (WS non ouvert) — backoff de reconnexion déjà en cours');
      return;
    }
    console.log('[WS] Resync requested by renderer (media error) — reconnecting');
    if (ws) { ws.removeAllListeners('close'); ws.terminate(); ws = null; }
    reconnectAttempts = 0;
    clearTimeout(reconnectTimer);
    connectWebSocket();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  clearTimeout(reconnectTimer);
  stopHeartbeat();
  if (_offlinePowerTimer) { clearTimeout(_offlinePowerTimer); _offlinePowerTimer = null; }
  if (_scheduleTimer)    { clearTimeout(_scheduleTimer);  _scheduleTimer = null; }
  if (_pulseTimer)       { clearInterval(_pulseTimer);    _pulseTimer    = null; }
  if (displayController) { try { displayController.close(); } catch {} }
  if (lanSync)           { try { lanSync.stop(); } catch {} }
  if (localControlServer){ try { localControlServer.stop(); } catch {} }
  if (ws) {
    ws.removeAllListeners('close');
    ws.close();
  }
});
