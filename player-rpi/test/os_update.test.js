// Mises à jour contrôlées de l'OS (issue vitrail#6) — module player + script root.
//
// Fait tourner le VRAI module (os_update.js) sur un dossier d'état temporaire ; seuls systemctl
// et l'identifiant de démarrage sont simulés. Le script root est exercé par bash (lecture de la
// sortie d'apt, refus des demandes invalides) quand bash est disponible.
//
// Usage : node player-rpi/test/os_update.test.js
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..', '..');
const { OsUpdater } = require(path.join(ROOT, 'player-rpi', 'src', 'os_update.js'));

let fail = 0;
function check(label, cond, detail) {
  console.log(`${cond ? 'OK   ' : 'ECHEC'} ${label}${cond ? '' : '  -> ' + JSON.stringify(detail)}`);
  if (!cond) fail++;
}
const read = (p) => { try { return fs.readFileSync(p, 'utf8'); } catch { return null; } };

function rig(over = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-os-'));
  const helper = path.join(dir, 'helper');
  fs.writeFileSync(helper, '#!/bin/sh\n');
  const config = {};
  const sent = [], statuses = [], calls = [];
  let t = 1_000_000;
  const env = { active: 'inactive', online: true, boot: 'boot-A', consume: true, last: null };
  const u = new OsUpdater({
    stateDir: dir, inboxDir: dir, helperPath: helper,
    getConfig: () => config, saveConfig: () => {},
    send: (m) => { if (!env.online) return false; sent.push(m); return true; },
    sendStatus: (id, st, err) => { if (!env.online) return false; statuses.push([id, st, err]); return true; },
    log: () => {},
    inWindow: (w) => !w || w === 'open',
    now: () => t,
    bootIdFn: () => env.boot,
    execFileFn: (cmd, args, _o, cb) => {
      calls.push([cmd, ...args]);
      if (args[0] === 'is-active') return cb(null, env.active + '\n', '');
      // Comme le vrai script : la demande est PRISE (retirée de la boîte) au démarrage.
      if (args[0] === 'start' && env.consume) {
        const f = path.join(dir, 'request');
        try { env.last = fs.readFileSync(f, 'utf8'); fs.unlinkSync(f); } catch {}
      }
      return cb(null, '', '');
    },
    ...over,
  });
  return {
    u, dir, config, sent, statuses, calls, env,
    advance: (ms) => { t += ms; },
    request: () => read(path.join(dir, 'request')) ?? env.last,
    result: (r) => fs.writeFileSync(path.join(dir, 'result.json'), JSON.stringify(r)),
    starts: () => calls.filter((c) => c[1] === 'start').length,
  };
}

const DIRECTIVE = {
  id_deployment: 5, version: '20261115-100000', track: 'os', force: true,
  packages: ['libssl3:amd64=3.0.15-1~deb12u1', 'tzdata:all=2025b-0+deb12u1', 'evil; rm -rf /=1'],
};

// ── 1. Anti-divergence + branchement ─────────────────────────────────────────
console.log('--- linux et rpi portent le meme mecanisme');
for (const f of ['src/os_update.js', 'scripts/signflow-os-update', 'scripts/install-os-update.sh']) {
  const a = read(path.join(ROOT, 'player-linux', f));
  const b = read(path.join(ROOT, 'player-rpi', f));
  check(`${f} identique linux/rpi`, a && a === b, { linux: !!a, rpi: !!b });
}
for (const p of ['player-linux', 'player-rpi']) {
  const m = read(path.join(ROOT, p, 'src', 'main.js'));
  check(`${p}: require + directive + commandes + capabilities`,
    m.includes("require('./os_update')") && m.includes("if ('os_update' in msg) getOsUpdater().handle(msg.os_update)")
    && m.includes("case 'os_bench':") && m.includes("case 'os_check':") && m.includes('caps.os_update = getOsUpdater().capabilities()'));
  const inst = read(path.join(ROOT, p, 'install.sh'));
  check(`${p}: install.sh pose le script root`, inst.includes('scripts/install-os-update.sh'));
}

(async () => {
  // ── 2. Parc : directive → demande au script root ────────────────────────────
  console.log('--- parc');
  {
    const r = rig();
    r.u.handle(DIRECTIVE);
    const req = r.request() || '';
    check('demande apply ecrite', req.startsWith('action=apply\nid=d5\nmanifest=20261115-100000\n'), req);
    check('paquets valides seulement', req.includes('pkg=libssl3:amd64=3.0.15-1~deb12u1')
      && req.includes('pkg=tzdata:all=2025b-0+deb12u1') && !req.includes('evil'), req);
    check('unite demarree sans attendre', r.calls.some((c) => c.join(' ') === 'systemctl start --no-block signflow-os-update.service'));
    check('installing signale', r.statuses.some((s) => s[1] === 'installing'));
    // Re-sync du même déploiement : pas de second lancement.
    r.u.handle(DIRECTIVE);
    check('pas relance au sync suivant', r.starts() === 1, r.starts());

    // Résultat OK avec redémarrage nécessaire, même démarrage : on attend.
    r.result({ action: 'apply', id: 'd5', state: 'ok', reboot_required: true, boot_id: 'boot-A', packages: [] });
    r.u.tick();
    check('attend le redemarrage', !r.statuses.some((s) => s[1] === 'success') && !r.config.os_applied);
    // Après redémarrage : appliqué + succès.
    r.env.boot = 'boot-B';
    r.u.tick();
    check('applique remonte apres redemarrage', r.config.os_applied === '20261115-100000', r.config);
    check('success signale', r.statuses.some((s) => s[0] === 5 && s[1] === 'success'));
    check('capabilities: applied', r.u.capabilities().applied === '20261115-100000');
    r.u.handle(null);
    check('directive retiree => etat efface', !r.config.os_ota);
  }
  {
    const r = rig();
    r.u.handle({ ...DIRECTIVE, force: false, install_window: 'closed' });
    check('hors fenetre: rien lance', r.request() === null && r.starts() === 0);
    r.u.handle(null);
    r.u.handle({ ...DIRECTIVE, id_deployment: 6, force: false, install_window: 'open' });
    check('dans la fenetre: lance', (r.request() || '').includes('id=d6'));
  }
  {
    const r = rig();
    r.env.active = 'activating';   // un relevé tourne
    r.u.handle(DIRECTIVE);
    check('script occupe: rien ecrit, retente', r.request() === null && r.config.os_ota.started === false);
    r.env.active = 'inactive';
    r.u.tick();
    check('minute suivante: lance', (r.request() || '').includes('id=d5'));
  }
  {
    const r = rig();
    r.u.handle(DIRECTIVE);
    r.result({ action: 'apply', id: 'd5', state: 'obsolete', error: "Version '1' for 'x' was not found", boot_id: 'boot-A' });
    r.u.tick(); r.u.tick();
    const f = r.statuses.filter((s) => s[1] === 'failed');
    check('perime => failed une seule fois', f.length === 1 && f[0][2].startsWith('obsolete:'), f);
  }
  {
    const r = rig();
    r.u.handle(DIRECTIVE);
    r.result({ action: 'apply', id: 'd5', state: 'running', boot_id: 'boot-A' });
    r.env.active = 'activating';
    r.u.tick();
    check('en cours: rien', !r.statuses.some((s) => s[1] === 'failed'));
    r.env.boot = 'boot-B';    // coupure pendant apt
    r.u.tick();
    check('coupure pendant apt => failed', r.statuses.some((s) => s[1] === 'failed' && /interrupted/.test(s[2])));
  }
  {
    const r = rig();
    r.u.handle(DIRECTIVE);
    r.advance(11 * 60 * 1000);   // le script n'a jamais écrit de résultat (demande refusée)
    r.u.tick();
    check('script muet => failed nomme', r.statuses.some((s) => s[1] === 'failed' && /did not run/.test(s[2])));
  }
  {
    const r = rig();
    r.u.handle({ ...DIRECTIVE, packages: ['bad one'] });
    check('manifeste vide apres filtre => failed', r.statuses.some((s) => s[1] === 'failed' && /empty/.test(s[2])) && r.starts() === 0);
  }

  // ── 2 bis. Revue du 15/11 : départs simultanés, demande non prise, script mort ──
  console.log('--- revue du 15/11');
  {
    // systemctl répond plus tard (comme en vrai) : deux départs dans la même fenêtre.
    const pending = [];
    const r = rig({ execFileFn: (cmd, args, _o, cb) => { pending.push([args, cb]); } });
    r.u.check();
    r.u.check();
    check('deux departs simultanes: un seul interroge systemctl', pending.length === 1, pending.length);
  }
  {
    const r = rig();
    r.env.consume = false;   // le script n'a pas encore pris la demande
    r.u.check();
    r.u.handle(DIRECTIVE);
    check('demande non prise: pas ecrasee', (r.request() || '') === 'action=check\n' && r.config.os_ota.started === false, r.request());
  }
  {
    const r = rig();
    r.u.handle(DIRECTIVE);
    r.result({ action: 'apply', id: 'd5', state: 'running', boot_id: 'boot-A' });
    r.env.active = 'inactive';   // le script s'est arrêté sans écrire son résultat
    r.u.tick();
    check('script mort => failed nomme', r.statuses.some((s) => s[1] === 'failed' && /stopped/.test(s[2])), r.statuses);
  }
  {
    const r = rig();
    r.u.bench('first');
    r.u.bench('second');
    check('second banc refuse, le premier garde', r.config.os_bench.request_id === 'first' && r.starts() === 1);
    r.result({ action: 'bench', id: 'first', state: 'running', boot_id: 'boot-A' });
    r.env.active = 'inactive';
    r.u.tick();
    const m = r.sent.find((x) => x.type === 'os:manifest');
    check('banc dont le script meurt => echec remonte', m && m.state === 'failed' && /stopped/.test(m.error) && !r.config.os_bench, m);
  }

  // ── 3. Banc ─────────────────────────────────────────────────────────────────
  console.log('--- banc');
  {
    const r = rig();
    r.u.bench('abc123');
    check('demande bench ecrite', r.request() === 'action=bench\nid=abc123\n', r.request());
    r.u.bench('bad id!');
    check('identifiant invalide refuse', r.starts() === 1);
    const pk = [{ name: 'libssl3', arch: 'amd64', version: '3.0.15-1~deb12u1' }];
    r.result({ action: 'bench', id: 'abc123', state: 'ok', family: 'debian-12-amd64', packages: pk, reboot_required: true, boot_id: 'boot-A' });
    r.env.online = false;
    r.u.tick();
    check('hors ligne: manifeste garde', !!r.config.os_bench && r.sent.length === 0);
    r.env.online = true;
    r.u.tick();
    const m = r.sent.find((x) => x.type === 'os:manifest');
    check('manifeste envoye', m && m.request_id === 'abc123' && m.state === 'ok' && m.family === 'debian-12-amd64'
      && m.packages.length === 1 && m.reboot_required === true, m);
    check('banc efface apres envoi', !r.config.os_bench);
  }
  {
    // Vécu sur le NUC le 15/11 : banc demandé pendant le relevé d'après démarrage → refusé « busy ».
    const r = rig();
    r.env.active = 'activating';
    r.u.bench('wait1');
    check('script occupe: banc en attente, pas d echec', r.request() === null && r.config.os_bench
      && r.config.os_bench.started === false && !r.sent.some((m) => m.type === 'os:manifest'), r.sent);
    r.u.tick();
    check('toujours occupe: toujours en attente', r.request() === null);
    r.env.active = 'inactive';
    r.u.tick();
    check('script libre: banc demarre', r.request() === 'action=bench\nid=wait1\n' && r.config.os_bench.started === true);
    r.u.tick();
    check('pas relance une fois demarre', r.starts() === 1, r.starts());
  }
  {
    const r = rig();
    r.config.os_ota = { id_deployment: 1, version: 'x', packages: ['a:all=1'], started: true, started_at: 0 };
    r.u.bench('abc');
    check('pas de banc pendant une MAJ du parc', r.request() === null);
  }

  // ── 4. Relevé périodique + capabilities ─────────────────────────────────────
  console.log('--- releve');
  {
    const r = rig();
    r.u.tick();
    check('pas de releve avant 2 min', r.starts() === 0);
    r.advance(2 * 60 * 1000);
    r.u.tick();
    check('releve apres 2 min', r.request() === 'action=check\n');
    fs.writeFileSync(path.join(r.dir, 'status.json'), JSON.stringify({
      helper_version: 1, family: 'raspios-13-arm64', pending: 12, security: 3, checked_at: 'T', reboot_required: false }));
    const c = r.u.capabilities();
    check('capabilities lues', c.helper && c.family === 'raspios-13-arm64' && c.pending === 12 && c.security === 3, c);
    r.u.tick();
    check('pas de second releve avant 6 h', r.starts() === 1);
    r.advance(6 * 3600 * 1000);
    r.u.tick();
    check('releve suivant a 6 h', r.starts() === 2);
  }
  {
    const r = rig({ helperPath: '/nonexistent/signflow-os-update' });
    check('sans script root: helper=false', JSON.stringify(r.u.capabilities()) === '{"helper":false}');
    r.u.handle(DIRECTIVE);
    check('sans script root: rien ecrit', r.request() === null);
  }

  // ── 5. Le script root lui-même (bash) ───────────────────────────────────────
  const bash = spawnSync('bash', ['-c', 'echo ok'], { encoding: 'utf8' });
  if (bash.status !== 0) {
    console.log('(bash absent : script root non exerce)');
  } else {
    console.log('--- script root');
    const script = path.join(ROOT, 'player-rpi', 'scripts', 'signflow-os-update').replace(/\\/g, '/');
    check('syntaxe bash', spawnSync('bash', ['-n', script]).status === 0);
    const sim = [
      'Inst libssl3 [3.0.11-1~deb12u2] (3.0.15-1~deb12u1 Debian-Security:12/stable-security [amd64])',
      'Inst tzdata [2024a-0+deb12u1] (2025b-0+deb12u1 Debian:12.12/stable [all])',
      'Inst linux-image-6.1.0-40-amd64 (6.1.153-1 Debian-Security:12/stable-security [amd64])',
      'Conf libssl3 (3.0.15-1~deb12u1 Debian-Security:12/stable-security [amd64])',
      'Inst libfoo:i386 [1] (2 Debian:12/stable [i386]) []',
      'Remv bar [1.0]',
    ].join('\n');
    const extract = `source <(sed -n '/^json_str()/,/^needs_reboot()/p' "${script}" | sed '$d'); parse_sim`;
    const out = spawnSync('bash', ['-c', extract], { input: sim + '\n', encoding: 'utf8' }).stdout.trim().split('\n');
    check('lecture de la sortie apt', JSON.stringify(out) === JSON.stringify([
      'libssl3:amd64=3.0.15-1~deb12u1|1', 'tzdata:all=2025b-0+deb12u1|0',
      'linux-image-6.1.0-40-amd64:amd64=6.1.153-1|1', 'libfoo:i386=2|0', 'REMOVE bar']), out);
    // Lecture STRICTE (revue du 15/11) : une ligne Inst illisible ou une purge ne passent jamais.
    const odd = spawnSync('bash', ['-c', extract], { input: 'Inst weird-format-line\nPurg foo [1]\n', encoding: 'utf8' }).stdout.trim().split('\n');
    check('ligne Inst illisible => UNPARSED, purge => REMOVE', JSON.stringify(odd) === JSON.stringify(['UNPARSED weird-format-line', 'REMOVE foo']), odd);
    const needs = (lines) => spawnSync('bash', ['-c',
      `source <(grep '^REBOOT_RE=' "${script}"); source <(sed -n '/^json_str()/,/^write_result()/p' "${script}" | sed '$d'); needs_reboot && echo yes || echo no`],
    { input: lines, encoding: 'utf8' }).stdout.trim();
    check('noyau => redemarrage', needs('linux-image-6.1.0-40-amd64:amd64=6.1.153-1\n') === 'yes');
    check('tzdata seul => pas de redemarrage', needs('tzdata:all=2025b\n') === 'no' || fs.existsSync('/run/reboot-required'));

    // Demandes invalides : refusées AVANT tout apt (code 2), et consommées.
    for (const [label, body] of [
      ['action inconnue', 'action=shell\n'],
      ['ligne inconnue', 'action=check\ncmd=rm -rf /\n'],
      ['paquet invalide', 'action=apply\nid=d1\nmanifest=m\npkg=libssl3;reboot:amd64=1\n'],
      ['paquet sans arch', 'action=apply\nid=d1\nmanifest=m\npkg=libssl3=1\n'],
      ['identifiant invalide', 'action=bench\nid=../../x\n'],
      ['apply sans paquet', 'action=apply\nid=d1\nmanifest=m\n'],
    ]) {
      const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-osh-'));
      const inbox = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-osi-'));
      fs.writeFileSync(path.join(inbox, 'request'), body);
      const r = spawnSync('bash', [script], { encoding: 'utf8', env: { ...process.env,
        SIGNFLOW_OS_STATE_DIR: dir.replace(/\\/g, '/'), SIGNFLOW_OS_INBOX: inbox.replace(/\\/g, '/') } });
      check(`refus: ${label}`, r.status === 2 && !fs.existsSync(path.join(inbox, 'request')), { status: r.status, err: r.stderr });
    }
  }

  if (bash.status === 0) {
    // Lien symbolique déposé à la place de la demande : refusé, la cible n'est jamais lue.
    const script = path.join(ROOT, 'player-rpi', 'scripts', 'signflow-os-update').replace(/\\/g, '/');
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-osh-'));
    const inbox = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-osi-'));
    const target = path.join(inbox, 'secret');
    fs.writeFileSync(target, 'pkg=leak:amd64=1\n');
    let linked = false;
    try {
      fs.symlinkSync(target, path.join(inbox, 'request'));
      linked = fs.lstatSync(path.join(inbox, 'request')).isSymbolicLink();
    } catch {}
    if (!linked) {
      console.log('(liens symboliques indisponibles ici : test du lien non exerce)');
    } else {
      const r = spawnSync('bash', [script], { encoding: 'utf8', env: { ...process.env,
        SIGNFLOW_OS_STATE_DIR: dir.replace(/\\/g, '/'), SIGNFLOW_OS_INBOX: inbox.replace(/\\/g, '/') } });
      check('demande en lien symbolique refusee', r.status === 2 && /not a plain file/.test(r.stderr)
        && fs.readFileSync(target, 'utf8') === 'pkg=leak:amd64=1\n', { status: r.status, err: r.stderr });
      // La BOÎTE elle-même remplacée par un lien vers un autre dossier : refusée, rien n'est pris.
      const elsewhere = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-ose-'));
      fs.writeFileSync(path.join(elsewhere, 'request'), 'action=check\n');
      const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-osp-'));
      const box = path.join(parent, 'os-update');
      fs.symlinkSync(elsewhere, box);
      const r2 = spawnSync('bash', [script], { encoding: 'utf8', env: { ...process.env,
        SIGNFLOW_OS_STATE_DIR: dir.replace(/\\/g, '/'), SIGNFLOW_OS_INBOX: box.replace(/\\/g, '/') } });
      check('boite de depot en lien symbolique refusee', r2.status === 2 && /inbox is not a plain directory/.test(r2.stderr)
        && fs.existsSync(path.join(elsewhere, 'request')), { status: r2.status, err: r2.stderr });
    }
  }

  console.log(fail ? `\n${fail} ECHEC(S)` : '\nTOUT OK');
  process.exit(fail ? 1 : 0);
})();
