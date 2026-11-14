// Garde du moteur Electron (issue vitrail#5) — bascule, essai, retour arrière automatique.
//
// ⚠️ Ce fichier est lancé par le NODE SYSTÈME (`node engine_guard.js …`), jamais par Electron :
// il doit fonctionner quand le nouveau moteur ne démarre pas du tout. Aucune dépendance npm,
// aucune API Electron. IDENTIQUE sur les 3 players Electron (test anti-divergence).
//
// Cycle (décision D2 du 14/11 : 5 min, sur le RENDU LOCAL, la connexion serveur n'y entre pas) :
//   1. le player a extrait et vérifié le nouveau moteur dans `dist.next`, écrit `engine-trial.json`
//      (phase `staged`), lancé `engine_guard.js run --pid <lui>` puis s'est arrêté ;
//   2. le garde attend la fin du player, bascule `dist` → `dist.prev` et `dist.next` → `dist`
//      (fsync à chaque étape), passe en phase `trial` et relance le player ;
//   3. le nouveau player écrit `engine-ok` 60 s après le chargement de son renderer ;
//   4. pas de `engine-ok` avant l'échéance ⇒ on tue Electron, on remet `dist.prev` en place,
//      l'essai passe `rolled_back` (le player, revenu sur l'ancien moteur, le signale au serveur).
//
// Coupure de courant pendant l'essai : le garde meurt avec la machine. Les lanceurs appellent donc
// `engine_guard.js boot-check` avant Electron : il répare une bascule interrompue et RÉARME un garde
// pour 5 nouvelles minutes (l'appareil ne sait pas si c'est le moteur ou la prise qui a coupé) ;
// au 3e démarrage sans confirmation, retour arrière immédiat.
//
// ⚠️ Délais mesurés sur une horloge MONOTONE (process.hrtime) : le RPi n'a pas de pile, son heure
// murale peut sauter de plusieurs mois à la synchro NTP — en plein essai, elle ferait expirer le
// délai d'un coup.
'use strict';

// ⚠️ `original-fs` quand il existe (dans Electron) : le `fs` d'Electron présente les archives
// `.asar` comme des DOSSIERS — en parcourant un moteur extrait (resources/default_app.asar), on
// tentait d'ouvrir des fichiers DANS l'archive (ENOENT). Mesuré au 1er essai réel sur le RPi
// (14/11) ; invisible des tests, qui tournent sous Node. Sous le node système, `fs` suffit.
const fs = (() => { try { return require('original-fs'); } catch { return require('fs'); } })();
const path = require('path');
const { spawn, spawnSync } = require('child_process');

const TRIAL_MS = 5 * 60 * 1000;
const MAX_BOOTS = 3;
const TRIAL_FILE = 'engine-trial.json';
const OK_FILE = 'engine-ok';

function engineDir(appRoot) { return path.join(appRoot, 'node_modules', 'electron'); }
function trialPath(appRoot) { return path.join(engineDir(appRoot), TRIAL_FILE); }
function okPath(appRoot) { return path.join(engineDir(appRoot), OK_FILE); }
function monoMs() { return Number(process.hrtime.bigint() / 1000000n); }
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// ── Écritures durables ─────────────────────────────────────────────────────────
// Leçon du 14/11 : un fichier écrit puis renommé sans fsync, et une coupure de courant moins
// d'une minute après, a laissé un lanceur de 0 octet. Ici, chaque fichier et chaque dossier
// touché est forcé sur le disque avant d'être considéré comme acquis.

function fsyncPath(p) {
  const isDir = fs.statSync(p).isDirectory();
  // Windows : on n'ouvre pas un DOSSIER (NTFS journalise ses métadonnées), et vider le cache
  // d'un FICHIER exige un accès en écriture (FlushFileBuffers) — ouvert en lecture seule,
  // fsync échoue en EPERM (mesuré : le garde aurait refusé chaque installation sur Windows).
  if (isDir && process.platform === 'win32') return;
  const fd = fs.openSync(p, isDir ? 'r' : 'r+');
  try {
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
}

function fsyncTree(dir) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) fsyncTree(p);
    else fsyncPath(p);
  }
  fsyncPath(dir);
}

function writeDurable(file, content) {
  const tmp = `${file}.tmp`;
  const fd = fs.openSync(tmp, 'w');
  try {
    fs.writeSync(fd, content);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(tmp, file);
  fsyncPath(path.dirname(file));
}

function readTrial(appRoot) {
  try { return JSON.parse(fs.readFileSync(trialPath(appRoot), 'utf8')); } catch { return null; }
}

function writeTrial(appRoot, trial) {
  writeDurable(trialPath(appRoot), JSON.stringify(trial, null, 2));
}

// Renommage avec réessais : sous Windows, un dossier dont l'exécutable vient de s'arrêter reste
// verrouillé quelques instants.
async function renameRetry(from, to, tries = 30) {
  for (let i = 0; ; i++) {
    try { fs.renameSync(from, to); return; } catch (e) {
      if (i >= tries) throw e;
      await sleep(1000);
    }
  }
}

function rmrf(p) { fs.rmSync(p, { recursive: true, force: true }); }

// ── Journal FICHIER (engine-guard.log, à côté du moteur) ───────────────────────
// Le garde est lancé détaché, sortie ignorée : sans fichier, sa mort restait inexplicable
// (AOpen, 14/11 : garde disparu en plein essai, aucune trace). Borné à 256 Ko.
let LOG_FILE = null;
function setLogFile(appRoot) { LOG_FILE = path.join(engineDir(appRoot), 'engine-guard.log'); }
function logLine(m) {
  const line = `${new Date().toISOString()} [engine-guard] ${m}`;
  console.log(line);
  if (!LOG_FILE) return;
  try {
    try { if (fs.statSync(LOG_FILE).size > 256 * 1024) fs.renameSync(LOG_FILE, `${LOG_FILE}.1`); } catch {}
    fs.appendFileSync(LOG_FILE, line + '\n');
  } catch {}
}

// Lancement détaché qui ne peut PAS tuer le garde : un échec de spawn est émis en événement
// `error`, et un `error` sans écouteur ARRÊTE le processus node. Toute relance passe par ici.
function spawnSafe(cmd, args, opts) {
  try {
    const child = spawn(cmd, args, opts);
    child.on('error', (e) => logLine(`spawn ${path.basename(cmd)} failed: ${e.message}`));
    child.unref();
    return true;
  } catch (e) {
    logLine(`spawn ${path.basename(cmd)} threw: ${e.message}`);
    return false;
  }
}

// ── Bascule et retour ──────────────────────────────────────────────────────────

async function swapIn(appRoot) {
  const dir = engineDir(appRoot);
  const dist = path.join(dir, 'dist'), next = path.join(dir, 'dist.next'), prev = path.join(dir, 'dist.prev');
  if (!fs.existsSync(next)) throw new Error('dist.next is missing');
  const trial = readTrial(appRoot);
  writeTrial(appRoot, { ...trial, phase: 'swapping' });
  rmrf(prev);                       // un seul ancien moteur conservé
  await renameRetry(dist, prev);
  await renameRetry(next, dist);
  fsyncPath(dir);
  writeTrial(appRoot, { ...trial, phase: 'trial', boots: 0 });
}

async function rollBack(appRoot, reason) {
  const dir = engineDir(appRoot);
  const dist = path.join(dir, 'dist'), prev = path.join(dir, 'dist.prev'), bad = path.join(dir, 'dist.bad');
  if (!fs.existsSync(prev)) throw new Error('dist.prev is missing: cannot roll back');
  rmrf(bad);
  if (fs.existsSync(dist)) await renameRetry(dist, bad);
  await renameRetry(prev, dist);
  fsyncPath(dir);
  try { fs.unlinkSync(okPath(appRoot)); } catch {}
  const trial = readTrial(appRoot) || {};
  writeTrial(appRoot, { ...trial, status: 'rolled_back', reason, reported: false });
}

// ── Effets système (injectables pour les tests) ────────────────────────────────

function killElectron(appRoot, platform) {
  if (platform === 'windows') {
    spawnSync('taskkill', ['/F', '/T', '/IM', 'electron.exe'], { windowsHide: true });
  } else {
    spawnSync('pkill', ['-f', path.join(engineDir(appRoot), 'dist', 'electron')]);
  }
}

function relaunch(appRoot, platform) {
  if (platform === 'rpi') return;   // la boucle de start-player.sh relance Electron toute seule
  if (platform === 'windows') {
    // Electron lancé DIRECTEMENT (comme `app.relaunch()` de l'OTA du code), jamais par
    // `schtasks /run` : mesuré le 14/11 sur l'AOpen, la relance par le planificateur, demandée
    // depuis le garde, ne démarrait rien (ni après la bascule ni après le retour arrière) —
    // l'écran restait éteint jusqu'à une relance manuelle. Le garde tourne dans la session
    // interactive (lancé par Electron) : la fenêtre s'ouvre donc bien sur le bureau.
    // .exe et non electron.cmd : Node refuse de lancer un .cmd sans shell (EINVAL).
    const env = { ...process.env };
    delete env.ELECTRON_RUN_AS_NODE;
    let out = 'ignore';
    try { out = fs.openSync(path.join(process.env.USERPROFILE || appRoot, 'signflow-player.log'), 'a'); } catch {}
    logLine(`relaunch: ${path.join(engineDir(appRoot), 'dist', 'electron.exe')}`);
    spawnSafe(path.join(engineDir(appRoot), 'dist', 'electron.exe'), [appRoot],
      { detached: true, stdio: ['ignore', out, out], env, cwd: appRoot, windowsHide: false });
    return;
  }
  // linux (openbox) : relance détachée, avec l'environnement hérité du player (DISPLAY…).
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;
  let out = 'ignore';
  try { out = fs.openSync('/var/log/signflow-player.log', 'a'); } catch {}
  logLine('relaunch: node_modules/.bin/electron');
  spawnSafe(path.join(appRoot, 'node_modules', '.bin', 'electron'), [appRoot],
    { detached: true, stdio: ['ignore', out, out], env, cwd: appRoot });
}

function pidAlive(pid) {
  if (!pid) return false;
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; }
}

const defaultFx = { killElectron, relaunch, pidAlive, sleep, monoMs, log: logLine };

// ── Modes ──────────────────────────────────────────────────────────────────────

// Surveille l'essai jusqu'à `engine-ok` ou l'échéance. Renvoie 'ok' | 'rolled_back' | 'gone'.
async function watch(appRoot, platform, fx, trialMs, pollMs) {
  const deadline = fx.monoMs() + trialMs;
  while (fx.monoMs() < deadline) {
    const t = readTrial(appRoot);
    if (!t || t.status !== 'pending') return 'gone';
    if (fs.existsSync(okPath(appRoot))) {
      writeTrial(appRoot, { ...t, status: 'ok' });
      fx.log(`engine ${t.to} confirmed rendering`);
      return 'ok';
    }
    await fx.sleep(pollMs);
  }
  const t = readTrial(appRoot) || {};
  const reason = `no render confirmation within ${Math.round(trialMs / 1000)} s`;
  fx.log(`engine ${t.to}: ${reason} -> rolling back to ${t.from}`);
  fx.killElectron(appRoot, platform);
  await fx.sleep(3000);
  await rollBack(appRoot, reason);
  fx.relaunch(appRoot, platform);
  return 'rolled_back';
}

async function run(appRoot, platform, { pid = 0, noRelaunch = false, fx = defaultFx,
  trialMs = TRIAL_MS, pollMs = 2000 } = {}) {
  // 1. Attendre la fin du player qui nous a lancés (60 s au plus, puis on l'arrête). Sondage
  //    serré : sur le RPi la boucle du lanceur relance Electron 3 s après son arrêt, la bascule
  //    doit être faite avant (sinon il repartirait sur l'ancien moteur et ne confirmerait jamais).
  for (let i = 0; i < 300 && fx.pidAlive(pid); i++) await fx.sleep(200);
  if (fx.pidAlive(pid)) { fx.killElectron(appRoot, platform); await fx.sleep(3000); }
  const trial = readTrial(appRoot);
  if (!trial || trial.status !== 'pending') return 'nothing';
  // 2. Bascule (si pas déjà faite : un garde réarmé au démarrage arrive en phase `trial`).
  if (trial.phase === 'staged') {
    try {
      await swapIn(appRoot);
    } catch (e) {
      fx.log(`switch failed: ${e.message}`);
      if (fs.existsSync(path.join(engineDir(appRoot), 'dist.prev'))
          && !fs.existsSync(path.join(engineDir(appRoot), 'dist'))) {
        await rollBack(appRoot, `switch failed: ${e.message}`);
      } else {
        writeTrial(appRoot, { ...trial, status: 'rolled_back', reason: `switch failed: ${e.message}`, reported: false });
      }
      fx.relaunch(appRoot, platform);
      return 'rolled_back';
    }
    fx.log(`switched engine ${trial.from} -> ${trial.to}, trial started`);
    if (!noRelaunch) fx.relaunch(appRoot, platform);
  }
  // 3. Essai.
  return watch(appRoot, platform, fx, trialMs, pollMs);
}

// Appelé par le lanceur AVANT Electron. Synchrone et rapide : ne retarde jamais l'écran.
// Renvoie l'action prise : 'none' | 'repaired' | 'rolled_back' | 'rearmed' | 'cleared'.
async function bootCheck(appRoot, platform, { fx = defaultFx, spawnGuard } = {}) {
  const t = readTrial(appRoot);
  if (!t || t.status !== 'pending') return 'none';
  const dir = engineDir(appRoot);
  const has = (n) => fs.existsSync(path.join(dir, n));
  if (t.phase === 'staged') {
    // Coupure entre l'extraction et la bascule : rien n'a changé, on abandonne proprement.
    // Le player reprendra l'installation à la prochaine directive (compteur de tentatives).
    rmrf(path.join(dir, 'dist.next'));
    try { fs.unlinkSync(trialPath(appRoot)); } catch {}
    fsyncPath(dir);
    fx.log('interrupted before switch: trial cleared');
    return 'cleared';
  }
  if (t.phase === 'swapping') {
    // Coupure ENTRE les deux renommages : sans `dist`, Electron ne démarrerait pas.
    if (!has('dist') && has('dist.prev')) {
      await rollBack(appRoot, 'interrupted during the switch');
      fx.log('interrupted during switch: previous engine restored');
      return 'repaired';
    }
    writeTrial(appRoot, { ...t, phase: 'trial', boots: 0 });
  }
  const cur = readTrial(appRoot);
  const boots = (cur.boots || 0) + 1;
  if (boots >= MAX_BOOTS) {
    await rollBack(appRoot, `no render confirmation over ${MAX_BOOTS} starts`);
    fx.log(`engine ${cur.to}: ${MAX_BOOTS} starts without confirmation -> rolled back`);
    return 'rolled_back';
  }
  writeTrial(appRoot, { ...cur, boots });
  (spawnGuard || spawnDetachedGuard)(appRoot, platform);
  fx.log(`trial of ${cur.to} interrupted (start ${boots}/${MAX_BOOTS}): guard re-armed`);
  return 'rearmed';
}

function shieldSignals() {
  for (const sig of ['SIGTERM', 'SIGHUP', 'SIGINT']) {
    try { process.on(sig, () => logLine(`${sig} received during a trial: ignored`)); } catch {}
  }
}

function spawnDetachedGuard(appRoot, platform, pid = 0, noRelaunch = true) {
  const args = [__filename, 'run', '--app-root', appRoot, '--platform', platform, '--pid', String(pid)];
  if (noRelaunch) args.push('--no-relaunch');
  spawnSafe(process.execPath, args, { detached: true, stdio: 'ignore', windowsHide: true });
}

module.exports = {
  _fs: fs,   // pour les tests : l'implémentation réellement utilisée
  TRIAL_MS, MAX_BOOTS, TRIAL_FILE, OK_FILE,
  engineDir, trialPath, okPath, fsyncPath, fsyncTree, writeDurable, readTrial, writeTrial,
  swapIn, rollBack, run, bootCheck, watch, spawnDetachedGuard,
  _relaunch: relaunch,   // pour les tests : la vraie relance
  _spawnSafe: spawnSafe, _setLogFile: setLogFile, _shieldSignals: shieldSignals,
};

if (require.main === module) {
  const argv = process.argv.slice(2);
  const opt = (k) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : undefined; };
  const mode = argv[0];
  const appRoot = opt('--app-root') || path.join(__dirname, '..');
  const platform = opt('--platform') || (process.platform === 'win32' ? 'windows' : 'linux');
  try { setLogFile(appRoot); } catch {}
  process.on('uncaughtException', (e) => { logLine(`uncaught: ${e && e.stack || e}`); process.exit(0); });
  process.on('unhandledRejection', (e) => { logLine(`unhandled rejection: ${e && e.stack || e}`); });
  logLine(`${mode} start (pid ${process.pid}, platform ${platform})`);
  const done = (r) => { defaultFx.log(`${mode}: ${r}`); process.exit(0); };
  const fail = (e) => { defaultFx.log(`${mode} error: ${e && e.stack || e}`); process.exit(0); };
  // exit 0 même en erreur : le lanceur ne doit JAMAIS être empêché de démarrer Electron.
  if (mode === 'run') {
    // Un essai ne doit pas mourir d'un `pkill` trop large ni d'une fin de session : sur le RPi,
    // la garde de résolution de start-player.sh tuait le garde (pkill -f 'SignFlow/player-rpi',
    // 14/11) et l'essai restait en suspens. SIGKILL reste le seul arrêt possible.
    shieldSignals();
    run(appRoot, platform, { pid: parseInt(opt('--pid') || '0', 10), noRelaunch: argv.includes('--no-relaunch') })
      .then(done, fail);
  } else if (mode === 'boot-check') {
    bootCheck(appRoot, platform).then(done, fail);
  } else {
    fail(`unknown mode ${mode}`);
  }
}
