// Mise à jour du moteur Electron depuis le serveur (issue vitrail#5) — garde + module player.
//
// Ce test fait tourner le VRAI garde (engine_guard.js) et le VRAI module (engine_update.js) sur
// des arborescences temporaires, avec une vraie archive tar.gz. Seuls les effets système (arrêt
// d'Electron, relance, horloge, lancement du garde) sont simulés.
//
// Usage : node player-rpi/test/engine_update.test.js
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { spawnSync } = require('child_process');
const { Readable } = require('stream');

const ROOT = path.resolve(__dirname, '..', '..');
const PLAYERS = ['player-rpi', 'player-linux', 'player-windows'];
const guard = require(path.join(ROOT, 'player-rpi', 'src', 'engine_guard.js'));
const { EngineUpdater, tarBin } = require(path.join(ROOT, 'player-rpi', 'src', 'engine_update.js'));

let fail = 0;
function check(label, cond, detail) {
  console.log(`${cond ? 'OK   ' : 'ECHEC'} ${label}${cond ? '' : '  -> ' + JSON.stringify(detail)}`);
  if (!cond) fail++;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const read = (p) => { try { return fs.readFileSync(p, 'utf8').trim(); } catch { return null; } };

function tree(trial = {}) {
  const appRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-engine-'));
  const eng = guard.engineDir(appRoot);
  for (const [d, v] of [['dist', '1.0.0'], ['dist.next', '2.0.0']]) {
    fs.mkdirSync(path.join(eng, d, 'resources'), { recursive: true });
    fs.writeFileSync(path.join(eng, d, 'version'), v);
    fs.writeFileSync(path.join(eng, d, 'electron'), `bin ${v}`);
  }
  guard.writeTrial(appRoot, { id_deployment: 7, from: '1.0.0', to: '2.0.0', status: 'pending',
    phase: 'staged', boots: 0, ...trial });
  return { appRoot, eng, v: (d) => read(path.join(eng, d, 'version')) };
}

function fx(over = {}) {
  let t = 0;
  const calls = { kill: 0, relaunch: 0, logs: [] };
  const f = {
    calls,
    killElectron: () => { calls.kill++; },
    relaunch: () => { calls.relaunch++; if (f.onRelaunch) f.onRelaunch(); },
    pidAlive: () => false,
    sleep: async (ms) => { t += ms; await sleep(1); },     // horloge simulée, avance au sommeil
    monoMs: () => t,
    log: (m) => calls.logs.push(m),
    ...over,
  };
  return f;
}

(async () => {
  // ── 1. Anti-divergence + branchement dans les 3 main.js ─────────────────────
  console.log('--- les 3 players portent le meme mecanisme');
  for (const mod of ['engine_update.js', 'engine_guard.js']) {
    const bodies = PLAYERS.map((p) => fs.readFileSync(path.join(ROOT, p, 'src', mod), 'utf8'));
    check(`${mod} identique sur les 3 players`, bodies.every((b) => b === bodies[0]), mod);
  }
  for (const p of PLAYERS) {
    const m = fs.readFileSync(path.join(ROOT, p, 'src', 'main.js'), 'utf8');
    const plat = p.replace('player-', '');
    check(`${p}: directive lue dans la cle engine_update`,
      m.includes("if ('engine_update' in msg) getEngineUpdater().handle(msg.engine_update);"), null);
    check(`${p}: version d'Electron remontee dans les capabilities`,
      m.includes('caps.electron_version = process.versions.electron;'), null);
    check(`${p}: confirmation du rendu apres chargement du renderer`,
      /did-finish-load', \(\) => \{\s*\n\s*\/\/[^\n]*\n\s*try \{ getEngineUpdater\(\)\.onRendererLoaded\(\)/.test(m), null);
    check(`${p}: plateforme declaree '${plat}'`, m.includes(`platform: '${plat}',`), null);
  }
  // Lanceurs : boot-check AVANT le premier Electron ; moteur donné au compte du player (D7).
  const rpiInst = fs.readFileSync(path.join(ROOT, 'player-rpi', 'install.sh'), 'utf8');
  const rpiBoot = rpiInst.indexOf('engine_guard.js" boot-check --app-root "$PLAYER_DIR" --platform rpi');
  check('rpi: boot-check avant la boucle de relance', rpiBoot > 0
    && rpiBoot < rpiInst.indexOf('\nwhile true; do\n  # Auto-detect output'), rpiBoot);
  const linInst = fs.readFileSync(path.join(ROOT, 'player-linux', 'install.sh'), 'utf8');
  const linBoot = linInst.indexOf('engine_guard.js boot-check --app-root $PLAYER_DIR --platform linux');
  check('linux: boot-check (synchrone) avant Electron dans l\'autostart', linBoot > 0
    && linBoot < linInst.indexOf('echo "$ELECTRON_BIN $PLAYER_DIR >> $LOG_FILE 2>&1 &"')
    && !/boot-check[^\n]*&" >>/.test(linInst), linBoot);
  const winInst = fs.readFileSync(path.join(ROOT, 'player-windows', 'install.ps1'), 'utf8');
  const winBoot = winInst.indexOf('node src\\engine_guard.js boot-check');
  check('windows: boot-check avant Electron dans la tache', winBoot > 0
    && winBoot < winInst.indexOf('node_modules\\.bin\\electron.cmd'), winBoot);
  // La garde de résolution du lanceur RPi tuait le garde moteur (pkill -f 'SignFlow/player-rpi',
  // 14/11, en plein essai). Son motif doit toucher Electron, JAMAIS le garde.
  {
    const m = rpiInst.match(/pkill -f '([^']+)'/);
    const pat = m ? m[1].replace('$PLAYER_DIR', '/home/signflow/SignFlow/player-rpi') : null;
    const re = pat ? new RegExp(pat) : null;
    const electronCmd = '/home/signflow/SignFlow/player-rpi/node_modules/electron/dist/electron --ozone-platform=wayland --no-sandbox /home/signflow/SignFlow/player-rpi';
    const guardCmd = 'node /home/signflow/SignFlow/player-rpi/src/engine_guard.js run --app-root /home/signflow/SignFlow/player-rpi --platform rpi --pid 42';
    check('rpi: la garde de resolution relance bien Electron', !!re && re.test(electronCmd), pat);
    check('rpi: ... sans jamais toucher le garde moteur', !!re && !re.test(guardCmd), pat);
  }
  for (const [n, src] of [['rpi', rpiInst], ['linux', linInst]]) {
    const npm = src.indexOf('npm install --omit=dev');
    const own = src.indexOf('chown -R "$SERVICE_USER:$SERVICE_USER" "$PLAYER_DIR/node_modules"');
    check(`${n}: moteur au compte du player apres npm install`, npm > 0 && own > npm, { npm, own });
  }

  // Dans Electron, `fs` voit les .asar comme des dossiers : les 2 modules doivent prendre
  // `original-fs` quand il existe (1er essai réel sur le RPi, 14/11 : ENOENT dans default_app.asar).
  {
    const Module = require('module');
    const realLoad = Module._load;
    const sentinel = { ...fs, __original: true };
    const fresh = (rel) => {
      const p = path.join(ROOT, 'player-rpi', 'src', rel);
      for (const k of Object.keys(require.cache)) if (k.includes(`${path.sep}src${path.sep}engine_`)) delete require.cache[k];
      return require(p);
    };
    Module._load = function (req, ...a) { return req === 'original-fs' ? sentinel : realLoad.call(this, req, ...a); };
    try {
      check('dans Electron : engine_guard utilise original-fs', fresh('engine_guard.js')._fs === sentinel, null);
      check('dans Electron : engine_update utilise original-fs', fresh('engine_update.js')._fs === sentinel, null);
    } finally {
      Module._load = realLoad;
    }
    check('sous node : repli sur fs', fresh('engine_guard.js')._fs === fs && fresh('engine_update.js')._fs === fs, null);
  }

  const gsrc = fs.readFileSync(path.join(ROOT, 'player-rpi', 'src', 'engine_guard.js'), 'utf8');
  check('garde : aucun delai sur l\'heure murale (RPi sans pile)', !/Date\.now\(\)/.test(gsrc), null);
  check('garde : sans aucune API Electron', !/require\(['"]electron['"]\)/.test(gsrc), null);

  // ── 2. Garde : essai confirme ───────────────────────────────────────────────
  console.log('\n--- garde : bascule puis confirmation');
  let T = tree();
  let F = fx();
  F.onRelaunch = () => guard.writeDurable(guard.okPath(T.appRoot), 'x');   // le nouveau player confirme
  let r = await guard.run(T.appRoot, 'linux', { fx: F, trialMs: 60000, pollMs: 50 });
  check('resultat ok', r === 'ok', r);
  check('dist = nouveau moteur', T.v('dist') === '2.0.0', T.v('dist'));
  check('dist.prev = ancien moteur conserve', T.v('dist.prev') === '1.0.0', T.v('dist.prev'));
  check('essai marque ok', guard.readTrial(T.appRoot).status === 'ok', guard.readTrial(T.appRoot));
  check('relance une fois, aucun arret force', F.calls.relaunch === 1 && F.calls.kill === 0, F.calls);

  // ── 3. Garde : pas de confirmation => retour arriere ────────────────────────
  console.log('\n--- garde : pas de confirmation dans le delai');
  T = tree();
  F = fx();
  r = await guard.run(T.appRoot, 'rpi', { fx: F, trialMs: 300000, pollMs: 2000 });
  const tr = guard.readTrial(T.appRoot);
  check('resultat rolled_back', r === 'rolled_back', r);
  check('dist = ancien moteur remis', T.v('dist') === '1.0.0', T.v('dist'));
  check('dist.bad = moteur rejete (diagnostic)', T.v('dist.bad') === '2.0.0', T.v('dist.bad'));
  check('essai rolled_back, raison, a signaler', tr.status === 'rolled_back' && /300 s/.test(tr.reason)
    && tr.reported === false, tr);
  check('Electron arrete puis relance', F.calls.kill === 1 && F.calls.relaunch === 2, F.calls);

  // ── 4. Garde : attend la fin du player avant de basculer ────────────────────
  console.log('\n--- garde : attend la fin du player');
  T = tree();
  let alive = 5, swappedWhileAlive = false;
  F = fx({ pidAlive: () => { if (alive > 0 && T.v('dist') === '2.0.0') swappedWhileAlive = true; return alive-- > 0; } });
  F.onRelaunch = () => guard.writeDurable(guard.okPath(T.appRoot), 'x');
  await guard.run(T.appRoot, 'linux', { pid: 1234, fx: F, trialMs: 60000, pollMs: 50 });
  check('aucune bascule tant que le player tourne', !swappedWhileAlive && T.v('dist') === '2.0.0', T.v('dist'));
  check('le player s\'est arrete de lui-meme (aucun arret force)', F.calls.kill === 0, F.calls);

  // ── 5. Garde : un echec de bascule ne laisse jamais sans moteur ─────────────
  console.log('\n--- garde : dist.next absent');
  T = tree();
  fs.rmSync(path.join(T.eng, 'dist.next'), { recursive: true });
  F = fx();
  r = await guard.run(T.appRoot, 'linux', { fx: F, trialMs: 60000, pollMs: 50 });
  check('echec signale rolled_back, moteur intact', r === 'rolled_back' && T.v('dist') === '1.0.0'
    && guard.readTrial(T.appRoot).status === 'rolled_back', { r, d: T.v('dist') });

  // ── 6. Demarrage (boot-check) ───────────────────────────────────────────────
  console.log('\n--- boot-check');
  T = tree();
  check('aucun essai -> rien', await guard.bootCheck(fs.mkdtempSync(path.join(os.tmpdir(), 'sf-e-')), 'rpi', { fx: fx() }) === 'none', null);
  T = tree({ phase: 'trial', boots: 0 });
  fs.renameSync(path.join(T.eng, 'dist'), path.join(T.eng, 'dist.prev'));
  fs.renameSync(path.join(T.eng, 'dist.next'), path.join(T.eng, 'dist'));
  let armed = 0;
  r = await guard.bootCheck(T.appRoot, 'rpi', { fx: fx(), spawnGuard: () => { armed++; } });
  check('essai interrompu -> garde rearme, demarrage compte', r === 'rearmed' && armed === 1
    && guard.readTrial(T.appRoot).boots === 1, { r, armed, t: guard.readTrial(T.appRoot) });
  await guard.bootCheck(T.appRoot, 'rpi', { fx: fx(), spawnGuard: () => { armed++; } });
  r = await guard.bootCheck(T.appRoot, 'rpi', { fx: fx(), spawnGuard: () => { armed++; } });
  check(`au ${guard.MAX_BOOTS}e demarrage sans confirmation -> retour arriere`, r === 'rolled_back'
    && T.v('dist') === '1.0.0' && armed === 2, { r, armed, d: T.v('dist') });

  T = tree({ phase: 'swapping' });
  fs.renameSync(path.join(T.eng, 'dist'), path.join(T.eng, 'dist.prev'));   // coupure entre les 2 renommages
  r = await guard.bootCheck(T.appRoot, 'rpi', { fx: fx(), spawnGuard: () => {} });
  check('coupure ENTRE les renommages -> ancien moteur remis', r === 'repaired' && T.v('dist') === '1.0.0', { r, d: T.v('dist') });

  T = tree({ phase: 'staged' });
  r = await guard.bootCheck(T.appRoot, 'rpi', { fx: fx(), spawnGuard: () => {} });
  check('coupure AVANT la bascule -> essai abandonne, moteur intact', r === 'cleared'
    && T.v('dist') === '1.0.0' && !fs.existsSync(path.join(T.eng, 'dist.next'))
    && !guard.readTrial(T.appRoot), { r });

  T = tree({ status: 'ok' });
  check('essai deja conclu -> rien', await guard.bootCheck(T.appRoot, 'rpi', { fx: fx() }) === 'none', null);

  // Le VRAI programme, tel que l'appellent les lanceurs (node système, arguments, code de sortie).
  const guardCli = path.join(ROOT, 'player-rpi', 'src', 'engine_guard.js');
  T = tree({ phase: 'staged' });
  let cli = spawnSync(process.execPath, [guardCli, 'boot-check', '--app-root', T.appRoot, '--platform', 'rpi'], { encoding: 'utf8' });
  check('CLI boot-check : code 0, essai interrompu nettoye', cli.status === 0 && /boot-check: cleared/.test(cli.stdout)
    && !guard.readTrial(T.appRoot), { status: cli.status, out: cli.stdout, err: cli.stderr });
  cli = spawnSync(process.execPath, [guardCli, 'boot-check', '--app-root', path.join(T.appRoot, 'absent'), '--platform', 'rpi'], { encoding: 'utf8' });
  check('CLI boot-check : rien a faire -> code 0', cli.status === 0 && /boot-check: none/.test(cli.stdout), cli.stdout);
  // Journal FICHIER (le garde tourne détaché, sortie ignorée : c'est sa seule trace).
  T = tree({ phase: 'staged' });
  spawnSync(process.execPath, [guardCli, 'boot-check', '--app-root', T.appRoot, '--platform', 'rpi'], { encoding: 'utf8' });
  const glog = read(path.join(T.eng, 'engine-guard.log')) || '';
  check('le garde ecrit son journal dans engine-guard.log', /boot-check start/.test(glog) && /boot-check: cleared/.test(glog), glog);

  // Un lancement qui ÉCHOUE ne doit pas tuer le garde (AOpen 14/11 : garde mort en plein essai).
  // Sous node, un `error` de spawn sans écouteur arrête le processus entier.
  const survive = spawnSync(process.execPath, ['-e',
    `const g = require(${JSON.stringify(guardCli)});
     g._spawnSafe(${JSON.stringify(path.join(os.tmpdir(), 'absent-' + Date.now(), 'electron.exe'))}, [], { detached: true, stdio: 'ignore' });
     setTimeout(() => console.log('ALIVE'), 400);`], { encoding: 'utf8' });
  check('un lancement qui echoue ne tue pas le garde', /ALIVE/.test(survive.stdout), { out: survive.stdout, err: survive.stderr.slice(0, 200) });

  // Pendant un essai, le garde survit à SIGTERM/SIGHUP (seul SIGKILL l'arrête). Unix seulement.
  if (process.platform !== 'win32') {
    const { spawn: spawnChild } = require('child_process');
    const child = spawnChild(process.execPath, ['-e',
      `require(${JSON.stringify(guardCli)})._shieldSignals(); console.log('READY'); setTimeout(() => console.log('ALIVE'), 800);`]);
    let outp = '';
    child.stdout.on('data', (b) => { outp += b; if (/READY/.test(outp) && !child._sent) { child._sent = true; child.kill('SIGTERM'); } });
    await new Promise((r) => child.on('exit', r));
    check('le garde survit a SIGTERM pendant un essai', /ALIVE/.test(outp), outp);
  } else {
    check('protection contre les signaux exportee', typeof guard._shieldSignals === 'function', null);
  }

  cli = spawnSync(process.execPath, [guardCli, 'nimporte'], { encoding: 'utf8' });
  check('CLI : mode inconnu -> code 0 quand meme (ne bloque jamais le lanceur)', cli.status === 0, cli.status);

  // La VRAIE relance (pas un bouchon) : un faux moteur qui écrit un témoin quand il démarre.
  // Windows : lancer dist\electron.exe directement (14/11 : `schtasks /run` depuis le garde ne
  // démarrait rien sur l'AOpen). Linux : node_modules/.bin/electron.
  {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-relaunch-'));
    const marker = path.join(root, 'started.txt');
    fs.writeFileSync(path.join(root, 'index.js'),
      `require('fs').writeFileSync(${JSON.stringify(marker)}, String(process.env.ELECTRON_RUN_AS_NODE || 'unset') + ' ' + process.argv[1])`);
    const plat = process.platform === 'win32' ? 'windows' : 'linux';
    if (plat === 'windows') {
      fs.mkdirSync(path.join(guard.engineDir(root), 'dist'), { recursive: true });
      fs.copyFileSync(process.execPath, path.join(guard.engineDir(root), 'dist', 'electron.exe'));
    } else {
      fs.mkdirSync(path.join(root, 'node_modules', '.bin'), { recursive: true });
      const bin = path.join(root, 'node_modules', '.bin', 'electron');
      fs.writeFileSync(bin, `#!/bin/sh\nexec "${process.execPath}" "$@"\n`);
      fs.chmodSync(bin, 0o755);
    }
    const saved = process.env.ELECTRON_RUN_AS_NODE;
    process.env.ELECTRON_RUN_AS_NODE = '1';     // hérité d'un garde lancé en mode node : à retirer
    try { guard._relaunch(root, plat); } finally {
      if (saved === undefined) delete process.env.ELECTRON_RUN_AS_NODE; else process.env.ELECTRON_RUN_AS_NODE = saved;
    }
    for (let i = 0; i < 100 && !fs.existsSync(marker); i++) await sleep(50);
    const got = read(marker);
    check(`relance ${plat} : le moteur demarre vraiment, sur la racine du player`, got !== null && got.slice(got.indexOf(' ') + 1).startsWith(root), got);
    check(`relance ${plat} : ELECTRON_RUN_AS_NODE retire (sinon Electron demarrerait en simple node)`,
      got !== null && got.startsWith('unset'), got);
    const gs = fs.readFileSync(path.join(ROOT, 'player-rpi', 'src', 'engine_guard.js'), 'utf8');
    check('windows : plus de relance par schtasks', !/spawn\('schtasks'/.test(gs), null);
  }

  // ── 7. Durabilite (lecon du lanceur a 0 octet) ──────────────────────────────
  console.log('\n--- ecritures forcees sur le disque');
  T = tree();
  const synced = new Set();
  const realFsync = fs.fsyncSync, realOpen = fs.openSync;
  const fdPath = new Map();
  fs.openSync = function (p, ...a) { const fd = realOpen.call(fs, p, ...a); fdPath.set(fd, path.resolve(String(p))); return fd; };
  fs.fsyncSync = function (fd) { synced.add(fdPath.get(fd)); return realFsync.call(fs, fd); };
  try { guard.fsyncTree(path.join(T.eng, 'dist.next')); } finally { fs.fsyncSync = realFsync; fs.openSync = realOpen; }
  const want = ['version', 'electron'].map((f) => path.resolve(T.eng, 'dist.next', f));
  check('chaque fichier du nouveau moteur est fsync', want.every((w) => synced.has(w)), [...synced]);
  if (process.platform !== 'win32') {
    check('ses dossiers aussi', synced.has(path.resolve(T.eng, 'dist.next'))
      && synced.has(path.resolve(T.eng, 'dist.next', 'resources')), [...synced]);
  }
  guard.writeDurable(path.join(T.eng, 'f.json'), '{"a":1}');
  check('ecriture durable : contenu juste, pas de .tmp laisse', read(path.join(T.eng, 'f.json')) === '{"a":1}'
    && !fs.existsSync(path.join(T.eng, 'f.json.tmp')), null);

  // ── 8. Module player ────────────────────────────────────────────────────────
  console.log('\n--- module player');
  function updater(appRoot, over = {}) {
    const cfg = { backend_url: 'http://srv', player_token: 'T' };
    const sent = [];
    const u = new EngineUpdater({
      appRoot, userData: fs.mkdtempSync(path.join(os.tmpdir(), 'sf-ud-')), platform: 'linux',
      electronVersion: '1.0.0', getConfig: () => cfg, saveConfig: () => {},
      sendStatus: (id, st, err) => { sent.push([id, st, err]); return true; },
      log: () => {}, quit: () => { u.quitted = true; }, inWindow: () => true,
      nodePath: 'node', spawnFn: (cmd, args) => { u.spawned = [cmd, args]; return { unref() {} }; },
      setTimeoutFn: (fn) => { fn(); return 1; }, okDelayMs: 0, ...over,
    });
    u.cfg = cfg; u.sent = sent;
    return u;
  }
  const dir = { id_deployment: 9, version: '2.0.0', download_url: '/api/v1/updates/engines/linux-x64/2.0.0', sha256: '' };

  T = tree({ status: 'ok' });
  let U = updater(T.appRoot);
  U.handle({ ...dir, version: '1.0.0' });
  check('deja sur la version cible -> rien a faire', !U.cfg.engine_ota, U.cfg.engine_ota);

  T = tree({ id_deployment: 9, status: 'rolled_back', reason: 'no render confirmation within 300 s', reported: false });
  U = updater(T.appRoot);
  U.handle(dir);
  check('retour arriere signale au serveur', U.sent.some(([id, st]) => id === 9 && st === 'rolled_back'), U.sent);
  check('le meme essai n\'est PAS relance', !U.cfg.engine_ota, U.cfg.engine_ota);
  U.handle(dir);
  check('signale une seule fois', U.sent.filter(([, st]) => st === 'rolled_back').length === 1, U.sent);

  // Installation avec une VRAIE archive.
  function mkTar(version, withBin = true) {
    const src = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-tar-'));
    fs.mkdirSync(path.join(src, 'dist', 'resources'), { recursive: true });
    fs.writeFileSync(path.join(src, 'dist', 'version'), version);
    if (withBin) fs.writeFileSync(path.join(src, 'dist', 'electron'), 'bin');
    const out = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'sf-tgz-')), 'e.tar.gz');
    const res = spawnSync(tarBin(), ['-czf', out, '-C', src, 'dist']);
    if (res.status !== 0) throw new Error(`tar: ${res.stderr}`);
    return out;
  }
  async function installWith(tarPath, cfgOver = {}) {
    const t = tree();
    fs.rmSync(path.join(t.eng, 'dist.next'), { recursive: true });
    fs.unlinkSync(guard.trialPath(t.appRoot));
    const u = updater(t.appRoot);
    Object.assign(u.cfg, { engine_ota: { id_deployment: 9, version: '2.0.0', staged: true, force: true } }, cfgOver);
    fs.copyFileSync(tarPath, path.join(u.userData, 'engine_staged.tar.gz'));
    u.install();
    for (let i = 0; i < 100 && u.busy && !u.quitted; i++) await sleep(50);
    return { t, u };
  }
  let I = await installWith(mkTar('2.0.0'));
  const trial = guard.readTrial(I.t.appRoot);
  check('dist.next extrait et verifie', I.t.v('dist.next') === '2.0.0', I.t.v('dist.next'));
  check('ancien moteur intact avant la bascule', I.t.v('dist') === '1.0.0', I.t.v('dist'));
  check('essai ecrit (pending, staged, 1.0.0 -> 2.0.0)', trial && trial.status === 'pending'
    && trial.phase === 'staged' && trial.from === '1.0.0' && trial.to === '2.0.0', trial);
  check('garde lance par le node systeme avec le pid du player', I.u.spawned && I.u.spawned[0] === 'node'
    && I.u.spawned[1].includes('run') && I.u.spawned[1].includes(String(process.pid))
    && /engine_guard\.js$/.test(I.u.spawned[1][0]), I.u.spawned);
  check('le player s\'arrete pour laisser basculer', I.u.quitted === true, null);
  check('statut installing remonte', I.u.sent.some(([, st]) => st === 'installing'), I.u.sent);

  I = await installWith(mkTar('2.0.1'));
  check('archive d\'une autre version -> refus, rien de prepare', !I.u.quitted
    && !fs.existsSync(path.join(I.t.eng, 'dist.next')) && !guard.readTrial(I.t.appRoot)
    && I.u.sent.some(([, st, e]) => st === 'failed' && /version file/.test(e)), I.u.sent);
  I = await installWith(mkTar('2.0.0', false));
  check('binaire absent -> refus', !I.u.quitted && I.u.sent.some(([, st, e]) => st === 'failed' && /binary/.test(e)), I.u.sent);
  I = await installWith(mkTar('2.0.0'), { engine_attempt: { version: '2.0.0', count: 2 } });
  check('2 tentatives deja faites -> echec sans extraire', !I.u.quitted
    && !fs.existsSync(path.join(I.t.eng, 'dist.next')) && I.u.sent.some(([, st]) => st === 'failed'), I.u.sent);

  // Confirmation du rendu par le nouveau moteur.
  T = tree({ phase: 'trial' });
  U = updater(T.appRoot, { electronVersion: '2.0.0' });
  U.onRendererLoaded();
  check('nouveau moteur en essai -> engine-ok ecrit', fs.existsSync(guard.okPath(T.appRoot)), null);
  T = tree({ phase: 'trial' });
  U = updater(T.appRoot, { electronVersion: '1.0.0' });
  U.onRendererLoaded();
  check('ancien moteur -> ne confirme JAMAIS a la place du nouveau', !fs.existsSync(guard.okPath(T.appRoot)), null);

  // Téléchargement : somme vérifiée.
  async function downloadWith(body, sha) {
    const t = tree({ status: 'ok' });
    const u = updater(t.appRoot);
    let asked;
    u.httpGet = (url, opts, cb) => {
      asked = { url, auth: opts.headers.Authorization };
      const res = Readable.from([body]); res.statusCode = 200;
      setImmediate(() => cb(res));
      return { on() {}, destroy() {}, setTimeout() {} };
    };
    u.cfg.engine_ota = { id_deployment: 9, version: '2.0.0', url: dir.download_url, sha256: sha, force: true, staged: false };
    u.download();
    for (let i = 0; i < 100 && u.busy; i++) await sleep(20);
    return { u, asked };
  }
  const body = Buffer.from('engine-bytes');
  const goodSha = crypto.createHash('sha256').update(body).digest('hex');
  let D = await downloadWith(body, goodSha);
  check('telechargement depuis LE serveur, jeton machine presente',
    D.asked.url === 'http://srv/api/v1/updates/engines/linux-x64/2.0.0' && D.asked.auth === 'Bearer T', D.asked);
  check('somme juste -> staged', D.u.cfg.engine_ota.staged === true
    && fs.existsSync(path.join(D.u.userData, 'engine_staged.tar.gz')), D.u.cfg.engine_ota);
  D = await downloadWith(body, '0'.repeat(64));
  check('somme fausse -> echec, rien de garde', D.u.cfg.engine_ota.staged === false
    && !fs.existsSync(path.join(D.u.userData, 'engine_staged.tar.gz'))
    && D.u.sent.some(([, st, e]) => st === 'failed' && /sha256/.test(e)), D.u.sent);

  console.log(`\n${fail === 0 ? 'TOUT VERT' : fail + ' ECHEC(S)'}`);
  process.exit(fail === 0 ? 0 : 1);
})().catch((e) => { console.error(e); process.exit(1); });
