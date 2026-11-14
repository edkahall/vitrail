// Mise à jour du MOTEUR Electron depuis le serveur (issue vitrail#5) — côté player.
//
// IDENTIQUE sur les 3 players Electron (test anti-divergence). Tout ce qui dépend de la
// plateforme ou de main.js est INJECTÉ par le constructeur.
//
// Le serveur envoie une directive `engine_update` (clé séparée de `update`, qui ne porte que le
// code). Ce module la suit comme l'OTA du code suit la sienne : téléchargement en fenêtre avec
// reprise, vérification sha256, installation en fenêtre de maintenance. L'installation prépare
// `node_modules/electron/dist.next`, le vérifie, le force sur le disque, écrit l'essai puis
// confie la bascule au GARDE (`engine_guard.js`, lancé par le node système) et s'arrête : on ne
// remplace pas le moteur sous un Electron qui tourne (Windows le verrouille).
//
// Retour arrière (décision D2) : le nouveau moteur doit écrire `engine-ok` 60 s après le
// chargement de son renderer, faute de quoi le garde remet l'ancien. Le player revenu sur
// l'ancien moteur remonte alors `rolled_back` au serveur, qui suspend le déploiement.
'use strict';

// ⚠️ `original-fs` quand il existe (dans Electron) : le `fs` d'Electron présente les archives
// `.asar` comme des DOSSIERS — en parcourant un moteur extrait (resources/default_app.asar), on
// tentait d'ouvrir des fichiers DANS l'archive (ENOENT). Mesuré au 1er essai réel sur le RPi
// (14/11) ; invisible des tests, qui tournent sous Node. Sous le node système, `fs` suffit.
const fs = (() => { try { return require('original-fs'); } catch { return require('fs'); } })();
const path = require('path');
const crypto = require('crypto');
const { execFile, spawn, spawnSync } = require('child_process');
const guard = require('./engine_guard');

const OK_DELAY_MS = 60 * 1000;
const MAX_ATTEMPTS = 2;
const SPACE_FACTOR = 2.5;

// Sous Windows, le `tar.exe` du système (bsdtar, lit les .tar.gz, connaît --strip-components) —
// jamais un GNU tar d'outillage passé devant dans le PATH, qui prend « C:\… » pour un hôte distant.
function tarBin() {
  if (process.platform !== 'win32') return 'tar';
  const sys = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'tar.exe');
  return fs.existsSync(sys) ? sys : 'tar';
}

class EngineUpdater {
  /**
   * @param {object} o
   * @param {string} o.appRoot           racine du player (contient node_modules/electron)
   * @param {string} o.userData          dossier de données (téléchargement)
   * @param {string} o.platform          'rpi' | 'linux' | 'windows'
   * @param {string} o.electronVersion   process.versions.electron
   * @param {() => object} o.getConfig   config persistée du player
   * @param {() => void} o.saveConfig
   * @param {(id, status, error?) => boolean} o.sendStatus  update:status au serveur ; false si hors ligne
   * @param {(msg: string) => void} o.log
   * @param {() => void} o.quit          arrêt du player pour laisser le garde basculer
   * @param {(win: string|null) => boolean} o.inWindow
   */
  constructor(o) {
    Object.assign(this, {
      spawnFn: spawn, execFileFn: execFile, okDelayMs: OK_DELAY_MS, httpGet: null,
      nodePath: null, setTimeoutFn: setTimeout,
    }, o);
    this.busy = false;
    this.okTimer = null;
  }

  get state() { return this.getConfig().engine_ota || null; }
  _paths() {
    return {
      part: path.join(this.userData, 'engine_download.part'),
      staged: path.join(this.userData, 'engine_staged.tar.gz'),
      engine: guard.engineDir(this.appRoot),
    };
  }
  _clear() {
    const c = this.getConfig();
    const p = this._paths();
    for (const f of [p.part, p.staged]) { try { fs.unlinkSync(f); } catch {} }
    if (c.engine_ota || c.engine_attempt) { delete c.engine_ota; delete c.engine_attempt; this.saveConfig(); }
  }

  // ── Directive reçue au sync (null = déploiement terminé/annulé) ──────────────
  handle(d) {
    this.reportOutcome();
    if (!d || typeof d !== 'object' || !d.id_deployment || !d.version) {
      if (this.state) this._clear();
      return;
    }
    if (String(d.version) === String(this.electronVersion)) {   // déjà sur la cible : le serveur le constate
      this._clear();
      return;
    }
    const t = guard.readTrial(this.appRoot);
    if (t && t.id_deployment === d.id_deployment && (t.status === 'pending' || t.status === 'rolled_back')) {
      return;   // essai en cours, ou retour arrière à signaler : ne JAMAIS relancer le même essai
    }
    const prev = this.state;
    const staged = !!(prev && prev.version === d.version && prev.staged);
    this.getConfig().engine_ota = {
      id_deployment: d.id_deployment, version: String(d.version),
      url: d.download_url || '', sha256: d.sha256 || '',
      download_window: d.download_window || null, install_window: d.install_window || null,
      force: d.force === true, staged,
    };
    this.saveConfig();
    this.tick();
  }

  tick() {
    const s = this.state;
    if (!s || this.busy) return;
    if (s.staged && fs.existsSync(this._paths().staged)) {
      if (s.force || this.inWindow(s.install_window)) this.install();
    } else if (s.force || this.inWindow(s.download_window)) {
      this.download();
    }
  }

  _fail(msg, file) {
    const s = this.state;
    if (file) { try { fs.unlinkSync(file); } catch {} }
    this.log(`engine update failed: ${msg}`);
    if (s) this.sendStatus(s.id_deployment, 'failed', String(msg).slice(0, 500));
    this.busy = false;
  }

  // ── Téléchargement avec reprise (même logique que l'OTA du code) ─────────────
  download() {
    const s = this.state;
    const c = this.getConfig();
    this.busy = true;
    const url = /^https?:/i.test(s.url) ? s.url : `${String(c.backend_url || '').replace(/\/$/, '')}${s.url}`;
    const { part, staged } = this._paths();
    let offset = 0;
    try { if (fs.existsSync(part)) offset = fs.statSync(part).size; } catch {}
    const mod = this.httpGet ? { get: this.httpGet } : require(url.startsWith('https:') ? 'https' : 'http');
    const opts = url.startsWith('https:') ? { rejectUnauthorized: c.allow_insecure_tls !== true } : {};
    opts.headers = {};
    if (c.player_token) opts.headers.Authorization = `Bearer ${c.player_token}`;
    if (offset > 0) opts.headers.Range = `bytes=${offset}-`;
    this.sendStatus(s.id_deployment, 'downloading');
    const req = mod.get(url, opts, (res) => {
      if (res.statusCode !== 200 && res.statusCode !== 206) {
        res.resume();
        return this._fail(`download HTTP ${res.statusCode}`, part);
      }
      const append = res.statusCode === 206 && offset > 0;
      const out = fs.createWriteStream(part, { flags: append ? 'a' : 'w' });
      let stopped = false, lastCheck = Date.now();
      res.on('data', () => {
        if (!s.force && Date.now() - lastCheck > 3000) {
          lastCheck = Date.now();
          if (!this.inWindow(s.download_window)) { stopped = true; req.destroy(); out.end(); }
        }
      });
      res.pipe(out);
      out.on('finish', () => {
        if (stopped) { this.busy = false; this.log('engine download paused (outside window)'); return; }
        // Somme calculée en flux : l'archive fait ~120 Mo, pas question de la charger en mémoire.
        const h = crypto.createHash('sha256');
        fs.createReadStream(part).on('data', (b) => h.update(b)).on('error', (e) => this._fail(`hash: ${e.message}`, part))
          .on('end', () => {
            const got = h.digest('hex');
            if (s.sha256 && got.toLowerCase() !== String(s.sha256).toLowerCase()) {
              return this._fail(`sha256 mismatch (got ${got.slice(0, 12)}…)`, part);
            }
            try { fs.renameSync(part, staged); } catch (e) { return this._fail(`stage: ${e.message}`); }
            this.state.staged = true;
            this.saveConfig();
            this.sendStatus(s.id_deployment, 'staged');
            this.log(`engine ${s.version} downloaded and verified`);
            this.busy = false;
            this.tick();
          });
      });
    });
    req.on('error', (e) => { this.busy = false; this.log(`engine download error (retry next tick): ${e.message}`); });
    if (req.setTimeout) req.setTimeout(30 * 60 * 1000, () => { req.destroy(); this.busy = false; });
  }

  _findNode() {
    if (this.nodePath) return this.nodePath;
    const r = spawnSync('node', ['--version'], { encoding: 'utf8', windowsHide: true });
    return (r.status === 0 && /^v\d+/.test(r.stdout || '')) ? 'node' : null;
  }

  // ── Installation : dist.next vérifié + durable, essai écrit, garde lancé, arrêt ──
  install() {
    const s = this.state;
    const c = this.getConfig();
    this.busy = true;
    const prev = (c.engine_attempt && c.engine_attempt.version === s.version) ? (c.engine_attempt.count || 0) : 0;
    if (prev >= MAX_ATTEMPTS) return this._fail(`engine ${s.version} not reached after ${prev} attempts`);
    c.engine_attempt = { version: s.version, count: prev + 1 };
    this.saveConfig();
    const { staged, engine } = this._paths();
    const next = path.join(engine, 'dist.next');
    // Place : l'archive + le moteur extrait + l'ancien moteur conservé pour le retour.
    try {
      const need = fs.statSync(staged).size * SPACE_FACTOR;
      const st = fs.statfsSync(engine);
      if (st.bavail * st.bsize < need) return this._fail(`not enough disk space (${Math.round(need / 1048576)} MB needed)`);
    } catch (e) { this.log(`disk space check skipped: ${e.message}`); }
    const node = this._findNode();
    if (!node) return this._fail('system node not found: the engine guard cannot run');
    this.sendStatus(s.id_deployment, 'installing');
    this.log(`installing engine ${s.version} (attempt ${prev + 1})`);
    try { fs.rmSync(next, { recursive: true, force: true }); fs.mkdirSync(next, { recursive: true }); }
    catch (e) { return this._fail(`prepare: ${e.message}`); }
    this.execFileFn(tarBin(), ['-xzf', staged, '-C', next, '--strip-components=1'], { windowsHide: true }, (err, _o, se) => {
      if (err) { fs.rmSync(next, { recursive: true, force: true }); return this._fail(`extract: ${err.message} ${String(se || '').slice(-200)}`); }
      try {
        const v = fs.readFileSync(path.join(next, 'version'), 'utf8').trim().replace(/^v/, '');
        if (v !== s.version) throw new Error(`version file says ${v}, expected ${s.version}`);
        const bin = path.join(next, this.platform === 'windows' ? 'electron.exe' : 'electron');
        if (!fs.existsSync(bin)) throw new Error('engine binary missing');
        guard.fsyncTree(next);
        try { fs.unlinkSync(guard.okPath(this.appRoot)); } catch {}
        guard.writeTrial(this.appRoot, {
          id_deployment: s.id_deployment, from: this.electronVersion, to: s.version,
          status: 'pending', phase: 'staged', boots: 0, created_at: new Date().toISOString(),
        });
      } catch (e) {
        fs.rmSync(next, { recursive: true, force: true });
        return this._fail(`verify: ${e.message}`);
      }
      try { fs.unlinkSync(staged); } catch {}
      const args = [path.join(__dirname, 'engine_guard.js'), 'run', '--app-root', this.appRoot,
        '--platform', this.platform, '--pid', String(process.pid)];
      try {
        this.spawnFn(node, args, { detached: true, stdio: 'ignore', windowsHide: true }).unref();
      } catch (e) {
        try { fs.unlinkSync(guard.trialPath(this.appRoot)); } catch {}
        fs.rmSync(next, { recursive: true, force: true });
        return this._fail(`guard spawn: ${e.message}`);
      }
      this.log(`engine ${s.version} ready, handing over to the guard and stopping`);
      this.setTimeoutFn(() => this.quit(), 1000);
    });
  }

  // ── Démarrage : confirmer le rendu du nouveau moteur ─────────────────────────
  onRendererLoaded() {
    const t = guard.readTrial(this.appRoot);
    if (!t || t.status !== 'pending' || t.phase !== 'trial' || t.to !== this.electronVersion) return;
    if (this.okTimer) return;
    this.log(`engine ${t.to} on trial: confirming rendering in ${Math.round(this.okDelayMs / 1000)} s`);
    this.okTimer = this.setTimeoutFn(() => {
      const again = guard.readTrial(this.appRoot);
      if (again && again.status === 'pending' && again.to === this.electronVersion) {
        guard.writeDurable(guard.okPath(this.appRoot), new Date().toISOString());
        this.log(`engine ${t.to}: rendering confirmed`);
      }
    }, this.okDelayMs);
  }

  // ── Issue d'un essai à signaler au serveur (à chaque sync, donc connecté) ────
  reportOutcome() {
    const t = guard.readTrial(this.appRoot);
    if (!t) return;
    if (t.status === 'ok') {          // le succès, le serveur le constate par la version remontée
      try { fs.unlinkSync(guard.trialPath(this.appRoot)); } catch {}
      try { fs.unlinkSync(guard.okPath(this.appRoot)); } catch {}
      this._clear();
      return;
    }
    if (t.status === 'rolled_back' && !t.reported) {
      if (this.sendStatus(t.id_deployment, 'rolled_back', t.reason || 'engine rolled back')) {
        this.log(`engine ${t.to} rolled back (${t.reason}): reported`);
        guard.writeTrial(this.appRoot, { ...t, reported: true });
        this._clear();
      }
    }
  }
}

module.exports = { EngineUpdater, OK_DELAY_MS, MAX_ATTEMPTS, tarBin, _fs: fs };
