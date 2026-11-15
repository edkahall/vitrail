// Mises à jour contrôlées de l'OS (issue vitrail#6) — côté player Linux / Raspberry Pi.
//
// IDENTIQUE sur player-linux et player-rpi (test anti-divergence). Le player ne touche jamais à
// apt lui-même : il dépose une demande dans /var/lib/signflow/os-update/request et démarre l'unité
// `signflow-os-update.service` (autorisée par polkit, insensible à NoNewPrivs), qui lance le
// script root `signflow-os-update`. Ce script valide chaque ligne et refuse tout le reste ; il
// écrit ses résultats dans SON dossier (/var/lib/signflow-os-update, lecture seule pour le player).
//
// Trois usages :
//  - RELEVÉ (check) : au démarrage puis toutes les 6 h, ce qui est en attente → capabilities ;
//  - BANC (commande `os_bench` de l'exploitant) : tout installer, remonter la liste exacte de ce
//    qui a changé (le MANIFESTE) par un message `os:manifest` ;
//  - PARC (directive `os_update` du sync) : installer le manifeste approuvé, et rien d'autre, dans
//    la fenêtre de maintenance ; `success` seulement une fois revenu (après redémarrage s'il en
//    fallait un) ; `failed` sinon, avec la raison (« obsolete » = version disparue de l'archive).
'use strict';

const fs = require('fs');
const path = require('path');
const { execFile } = require('child_process');

const STATE_DIR = '/var/lib/signflow-os-update';   // sorties du script root (lecture seule)
const INBOX = '/var/lib/signflow/os-update';        // demande déposée par le player
const HELPER = '/usr/local/bin/signflow-os-update';
const UNIT = 'signflow-os-update.service';
const CHECK_EVERY_MS = 6 * 3600 * 1000;
const FIRST_CHECK_MS = 2 * 60 * 1000;
const START_GRACE_MS = 10 * 60 * 1000;    // le script n'a pas écrit de résultat : il a refusé
const BENCH_MAX_MS = 6 * 3600 * 1000;     // même durée de vie que la demande côté serveur
const SUCCESS_RESENDS = 30;
const PKG_RE = /^[a-z0-9][a-z0-9+.-]*:[a-z0-9-]+=[A-Za-z0-9.+~:-]+$/;

class OsUpdater {
  /**
   * @param {object} o
   * @param {() => object} o.getConfig
   * @param {() => void} o.saveConfig
   * @param {(msg: object) => boolean} o.send             message WS ; false si hors ligne
   * @param {(id, status, error?) => boolean} o.sendStatus update:status ; false si hors ligne
   * @param {(msg: string) => void} o.log
   * @param {(win: string|null) => boolean} o.inWindow
   */
  constructor(o) {
    Object.assign(this, {
      stateDir: STATE_DIR, inboxDir: INBOX, helperPath: HELPER, execFileFn: execFile, now: () => Date.now(),
      bootIdFn: () => { try { return fs.readFileSync('/proc/sys/kernel/random/boot_id', 'utf8').trim(); } catch { return ''; } },
    }, o);
    this.startedAt = this.now();
    this.lastCheck = 0;
    this.successSent = 0;
  }

  available() { return fs.existsSync(this.helperPath) && fs.existsSync(this.inboxDir); }
  _read(name) {
    try { return JSON.parse(fs.readFileSync(path.join(this.stateDir, name), 'utf8')); } catch { return null; }
  }
  _cfg() { return this.getConfig(); }

  // ── Démarrer le script root avec une demande ─────────────────────────────────
  // Une seule demande à la fois (revue du 15/11) : deux départs rapprochés (relevé périodique +
  // installation, ou « relever maintenant ») pouvaient s'écraser dans la boîte de dépôt, et l'une
  // des deux se perdait. Occupé = un départ en cours ICI, le script qui tourne, ou une demande
  // déposée que le script n'a pas encore prise.
  _start(lines, cb) {
    const done = (e) => { this.starting = false; if (cb) cb(e); };
    if (this.starting) return cb && cb(new Error('busy'));
    this.starting = true;
    const req = path.join(this.inboxDir, 'request');
    this.execFileFn('systemctl', ['is-active', UNIT], { timeout: 10000 }, (_e, out) => {
      if (String(out || '').trim() === 'activating' || fs.existsSync(req)) return done(new Error('busy'));
      try {
        fs.writeFileSync(`${req}.tmp`, lines.join('\n') + '\n');
        fs.renameSync(`${req}.tmp`, req);
      } catch (e) { return done(e); }
      this.execFileFn('systemctl', ['start', '--no-block', UNIT], { timeout: 15000 },
        (err, _o, se) => done(err ? new Error(`${err.message} ${String(se || '').trim()}`) : null));
    });
  }

  // Le script a-t-il cessé de tourner sans écrire son résultat ? (arrêt manuel, délai systemd,
  // manque de mémoire). Sans ce contrôle, le player attendait indéfiniment (revue du 15/11).
  _ifHelperDead(fn) {
    this.execFileFn('systemctl', ['is-active', UNIT], { timeout: 10000 }, (_e, out) => {
      if (String(out || '').trim() === 'activating') return;
      const again = this._read('result.json');
      if (again && again.state === 'running') fn();
    });
  }

  check() {
    if (!this.available()) return;
    this.lastCheck = this.now();
    this._start(['action=check'], (e) => { if (e && e.message !== 'busy') this.log(`os check not started: ${e.message}`); });
  }

  // ── Banc : commande de l'exploitant ──────────────────────────────────────────
  bench(requestId) {
    if (!this.available() || !/^[A-Za-z0-9_-]{1,64}$/.test(String(requestId || ''))) {
      this.log('os bench refused: helper missing or invalid request');
      return;
    }
    const c = this._cfg();
    if (c.os_ota) { this.log('os bench refused: an OS update is in progress'); return; }
    // Un banc déjà demandé n'est JAMAIS remplacé : son relevé serait perdu (revue du 15/11).
    if (c.os_bench) { this.log(`os bench ${requestId} refused: bench ${c.os_bench.request_id} not finished`); return; }
    // Mémorisée AVANT de démarrer : si le script est occupé (un relevé tourne — c'est le cas deux
    // minutes après chaque démarrage), le banc ATTEND et repart à la minute suivante. Vécu sur le
    // NUC le 15/11 : refusé « busy » juste après un redémarrage, l'exploitant n'avait rien vu venir.
    c.os_bench = { request_id: requestId, started_at: this.now(), started: false };
    this.saveConfig();
    this._startBench(c);
  }

  _startBench(c) {
    const b = c.os_bench;
    if (!b || b.started || this.benchStarting) return;
    this.benchStarting = true;
    this._start(['action=bench', `id=${b.request_id}`], (e) => {
      this.benchStarting = false;
      if (e && e.message === 'busy') {
        if (!b.waiting) { b.waiting = true; this.saveConfig(); this.log(`os bench ${b.request_id} waiting: the helper is busy`); }
        return;
      }
      if (e) {
        this.log(`os bench not started: ${e.message}`);
        this.send({ type: 'os:manifest', request_id: b.request_id, state: 'failed', error: `not started: ${e.message}`.slice(0, 300) });
        delete c.os_bench; this.saveConfig();
        return;
      }
      b.started = true;
      this.saveConfig();
      this.log(`os bench ${b.request_id} started`);
    });
  }

  // ── Parc : directive du sync (null = rien à faire / déploiement terminé ou annulé) ──
  handle(d) {
    const c = this._cfg();
    if (!d || typeof d !== 'object' || !d.id_deployment || !d.version || !Array.isArray(d.packages)) {
      if (c.os_ota) { delete c.os_ota; this.saveConfig(); }
      this.successSent = 0;
      return;
    }
    if (!c.os_ota || c.os_ota.id_deployment !== d.id_deployment) {
      const packages = d.packages.map(String).filter((p) => PKG_RE.test(p));
      c.os_ota = {
        id_deployment: d.id_deployment, version: String(d.version), packages,
        install_window: d.install_window || null, force: d.force === true,
        started: false, started_at: 0, reported: false,
      };
      this.successSent = 0;
      this.saveConfig();
    }
    this.tick();
  }

  // ── Toutes les minutes (et à chaque sync) ────────────────────────────────────
  tick() {
    if (!this.available()) return;
    const c = this._cfg();
    const result = this._read('result.json');
    const boot = this.bootIdFn();
    this._tickBench(c, result, boot);
    this._tickApply(c, result, boot);
    const busy = !!(c.os_ota && c.os_ota.started) || !!c.os_bench;
    const due = this.lastCheck === 0 ? this.now() - this.startedAt >= FIRST_CHECK_MS
      : this.now() - this.lastCheck >= CHECK_EVERY_MS;
    if (!busy && due) this.check();
  }

  _tickBench(c, result, boot) {
    const b = c.os_bench;
    if (!b) return;
    if (b.started === false) {   // en attente d'un script libre (ancien format sans le champ = démarré)
      if (this.now() - (b.started_at || 0) > BENCH_MAX_MS) { delete c.os_bench; this.saveConfig(); return; }
      this._startBench(c);
      return;
    }
    const mine = result && result.action === 'bench' && result.id === b.request_id;
    let msg = null;
    if (mine && result.state === 'running' && result.boot_id && result.boot_id !== boot) {
      msg = { state: 'failed', error: 'interrupted (power cut or reboot during the update)' };
    } else if (mine && result.state === 'running') {
      this._ifHelperDead(() => {
        if (this.send({ type: 'os:manifest', request_id: b.request_id, state: 'failed', error: 'the OS helper stopped before the end' })) {
          delete c.os_bench; this.saveConfig();
        }
      });
      return;
    } else if (mine && result.state !== 'running') {
      msg = {
        state: result.state, error: result.error || undefined, family: result.family,
        packages: Array.isArray(result.packages) ? result.packages : [],
        reboot_required: result.reboot_required === true,
      };
    } else if (this.now() - (b.started_at || 0) > BENCH_MAX_MS) {
      delete c.os_bench; this.saveConfig();
      return;
    }
    if (!msg) return;
    if (this.send({ type: 'os:manifest', request_id: b.request_id, ...msg })) {
      this.log(`os bench ${b.request_id}: ${msg.state}, ${(msg.packages || []).length} package(s) reported`);
      delete c.os_bench; this.saveConfig();
    }
  }

  _tickApply(c, result, boot) {
    const s = c.os_ota;
    if (!s) return;
    const id = `d${s.id_deployment}`;
    const mine = result && result.action === 'apply' && result.id === id;
    if (mine) {
      if (result.state === 'running') {
        if (result.boot_id && result.boot_id !== boot) this._failApply(s, 'interrupted (power cut or reboot during the update)');
        else this._ifHelperDead(() => this._failApply(s, 'the OS helper stopped before the end'));
        return;
      }
      if (result.state === 'ok') {
        // Redémarrage nécessaire et pas encore fait : il est programmé, on attend d'être revenu.
        if (result.reboot_required && result.boot_id === boot) return;
        if (c.os_applied !== s.version) { c.os_applied = s.version; this.saveConfig(); }
        if (this.successSent < SUCCESS_RESENDS && this.sendStatus(s.id_deployment, 'success')) {
          if (this.successSent === 0) this.log(`os manifest ${s.version} applied`);
          this.successSent += 1;
        }
        return;
      }
      return this._failApply(s, `${result.state}: ${result.error || 'no detail'}`);
    }
    if (s.reported) return;
    if (s.started) {
      if (this.now() - (s.started_at || 0) > START_GRACE_MS) {
        this._failApply(s, 'the OS helper did not run (journalctl -u signflow-os-update)');
      }
      return;
    }
    if (!s.packages.length) return this._failApply(s, 'empty manifest');
    if (!(s.force || this.inWindow(s.install_window))) return;
    s.started = true;
    s.started_at = this.now();
    this.saveConfig();
    const lines = ['action=apply', `id=${id}`, `manifest=${s.version}`, ...s.packages.map((p) => `pkg=${p}`)];
    this._start(lines, (e) => {
      if (e && e.message === 'busy') { s.started = false; this.saveConfig(); return; }   // un relevé tourne : minute suivante
      if (e) return this._failApply(s, `not started: ${e.message}`);
      this.sendStatus(s.id_deployment, 'installing');
      this.log(`os manifest ${s.version}: installing ${s.packages.length} package(s)`);
    });
  }

  _failApply(s, why) {
    if (s.reported) return;
    const msg = String(why).slice(0, 500);
    this.log(`os manifest ${s.version} failed: ${msg}`);
    if (this.sendStatus(s.id_deployment, 'failed', msg)) { s.reported = true; this.saveConfig(); }
  }

  // ── Ce que le serveur voit (capabilities.os_update) ──────────────────────────
  capabilities() {
    if (!this.available()) return { helper: false };
    const st = this._read('status.json') || {};
    const r = this._read('result.json');
    return {
      helper: true,
      helper_version: st.helper_version || null,
      family: st.family || (r && r.family) || null,
      pending: Number.isFinite(st.pending) ? st.pending : null,
      security: Number.isFinite(st.security) ? st.security : null,
      checked_at: st.checked_at || null,
      last_upgrade: st.last_upgrade || null,
      reboot_required: st.reboot_required === true,
      update_error: st.update_error || null,
      applied: this._cfg().os_applied || null,
      run: r ? {
        action: r.action, state: r.state, error: r.error || null, at: r.finished_at || null,
        count: Array.isArray(r.packages) ? r.packages.length : 0,
        reboot_required: r.reboot_required === true,
      } : null,
    };
  }
}

module.exports = { OsUpdater, PKG_RE, STATE_DIR, INBOX, HELPER, UNIT };
