// Amorce ChromeOS + autorun BS — decision 3 « canal unique » (2026-07-28).
//
// L'amorce (index.html) doit charger la version EPINGLEE du renderer depuis, dans l'ordre :
//   1. le chemin statique v/<ver>/ (sous le scope du service worker -> survit offline) ;
//   2. la route CATALOGUE /api/v1/updates/renderers/chromeos/<ver>/ (version IMPORTEE en zip,
//      extraite dans MinIO — elle n'existe QUE la) ;
//   3. le chemin plat historique (fail-safe, jamais d'ecran blanc).
//
// Ce test n'est PAS un portage : il EXECUTE le VRAI script embarque de index.html dans un
// bac a sable (fetch/document/localStorage stubes) — un `node --check` ne voit pas le JS
// embarque (lecon local_control 19/07), et un portage divergerait en silence.
// Il verifie aussi que les TROIS copies de l'autorun BS (source, servie, image SD) sont
// identiques et portent v53 — le piege des copies multiples (lecon triple-copy BrightSign).
//
// Usage : node player-rpi/test/renderer_loader.test.js

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');

let fail = 0;
function check(label, cond, detail) {
  console.log(`${cond ? 'OK  ' : 'ECHEC'} ${label}${cond ? '' : '  -> ' + JSON.stringify(detail)}`);
  if (!cond) fail++;
}

// ── 1. Extraction du script reel de l'amorce ────────────────────────────────
const html = fs.readFileSync(
  path.join(ROOT, 'frontend/public/player-chromeos/index.html'), 'utf8');
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const loader = blocks[blocks.length - 1];
check('amorce : script embarque trouve', !!loader && loader.includes('renderer-version'), null);

// ── 2. Bac a sable : rejoue l'amorce avec un fetch scripte ──────────────────
// `routes` : [motif d'URL, reponse] — HEAD sur signflow.js = sondes ; GET renderer-version = pin.
async function runLoader({ token, routes, backend = 'http://bk' }) {
  const scripts = [];          // src des <script> injectes par loadFrom (dans l'ordre)
  const fetched = [];          // URLs demandees (sondes comprises)
  const storage = {};
  const sandbox = {
    URLSearchParams,
    location: { search: backend ? `?backend=${backend}&token=${token}` : `?token=${token}` },
    localStorage: {
      getItem: (k) => (k in storage ? storage[k] : null),
      setItem: (k, v) => { storage[k] = String(v); },
      removeItem: (k) => { delete storage[k]; },
    },
    document: {
      head: { appendChild: (s) => scripts.push(s.src) },
      createElement: () => ({}),
      getElementById: () => ({
        value: '', classList: { add() {}, remove() {} }, addEventListener() {},
      }),
    },
    window: {},                              // pas de bridge -> wireSetup ne s'accroche jamais
    setTimeout: () => 0,                     // jamais declenche (ni offline-timer ni retry)
    clearTimeout: () => {},
    fetch: (url, opts) => {
      fetched.push(url);
      for (const [pattern, resp] of routes) {
        if (url.includes(pattern)) {
          if (resp === 'reject') return Promise.reject(new Error('offline'));
          return Promise.resolve({ ok: resp.ok !== false, json: () => Promise.resolve(resp.body || null) });
        }
      }
      return Promise.resolve({ ok: false, json: () => Promise.resolve(null) });
    },
    console,
  };
  vm.runInNewContext(loader, sandbox);
  // Chaines de .then imbriquees -> plusieurs tours de micro/macro-taches.
  for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
  return { scripts, fetched };
}

const JWT = 'eyJhbGciOi.fake.jwt';
const API_BASE = 'http://bk/api/v1/updates/renderers/chromeos/9.9.9/';

(async () => {
  // A. Statique presente -> chargee (le cache SW prime : survit offline).
  let r = await runLoader({
    token: JWT,
    routes: [
      ['renderer-version', { body: { version: '9.9.9' } }],
      ['v/9.9.9/signflow.js', { ok: true }],
    ],
  });
  check('A statique presente -> v/<ver>/',
    r.scripts.length === 4 && r.scripts.every((s) => s.startsWith('v/9.9.9/')), r.scripts);

  // B. Statique absente, catalogue present -> route API (version importee en zip).
  r = await runLoader({
    token: JWT,
    routes: [
      ['renderer-version', { body: { version: '9.9.9' } }],
      ['v/9.9.9/signflow.js', { ok: false }],
      [API_BASE + 'signflow.js', { ok: true }],
    ],
  });
  check('B statique absente -> catalogue',
    r.scripts.length === 4 && r.scripts.every((s) => s.startsWith(API_BASE)), r.scripts);
  check('B ordre des sondes : statique AVANT catalogue',
    r.fetched.findIndex((u) => u.includes('v/9.9.9/')) <
    r.fetched.findIndex((u) => u.includes('/updates/renderers/')), r.fetched);

  // C. Version epinglee servie nulle part -> plat (fail-safe, jamais d'ecran blanc).
  r = await runLoader({
    token: JWT,
    routes: [
      ['renderer-version', { body: { version: '9.9.9' } }],
      ['v/9.9.9/signflow.js', { ok: false }],
      [API_BASE + 'signflow.js', { ok: false }],
    ],
  });
  check('C nulle part -> plat',
    JSON.stringify(r.scripts) === JSON.stringify(['ed25519.js', 'signflow.js', 'audio_player.js', 'player.js']),
    r.scripts);

  // D. Sonde catalogue en ECHEC RESEAU (pas 404) -> plat aussi (le .catch ne casse rien).
  r = await runLoader({
    token: JWT,
    routes: [
      ['renderer-version', { body: { version: '9.9.9' } }],
      ['v/9.9.9/signflow.js', { ok: false }],
      [API_BASE + 'signflow.js', 'reject'],
    ],
  });
  check('D catalogue injoignable -> plat', JSON.stringify(r.scripts) ===
    JSON.stringify(['ed25519.js', 'signflow.js', 'audio_player.js', 'player.js']), r.scripts);

  // F. Kiosk SAME-ORIGIN (backend VIDE — signflow.js resout location.origin sans remplir
  // LS_BACKEND, mesure sur le Chromebox au canary) : la sonde catalogue doit partir en chemin
  // ABSOLU '/api/...' et non s'auto-annuler. C'est le bug qui a fait echouer les deploiements
  // 103->107 sans une seule requete visible cote serveur.
  r = await runLoader({
    token: JWT, backend: '',
    routes: [
      ['renderer-version', { body: { version: '9.9.9' } }],
      ['v/9.9.9/signflow.js', { ok: false }],
      ['/api/v1/updates/renderers/chromeos/9.9.9/signflow.js', { ok: true }],
    ],
  });
  check('F backend vide -> sonde catalogue en chemin absolu',
    r.scripts.length === 4 && r.scripts.every((s) => s.startsWith('/api/v1/updates/renderers/chromeos/9.9.9/')),
    r.scripts);

  // E. Token de provisioning sfp_ -> plat direct, AUCUN appel reseau (comportement historique).
  r = await runLoader({ token: 'sfp_abc', routes: [] });
  check('E sfp_ -> plat sans reseau',
    r.scripts.length === 4 && r.scripts[0] === 'ed25519.js' && r.fetched.length === 0,
    { scripts: r.scripts, fetched: r.fetched });

  // ── 3. Autorun BS : les copies SUIVIES identiques + marqueurs presents ─────
  // La version attendue est LUE dans la source (player-brightsign/autorun.brs) : un numero
  // code en dur ici cassait le test a chaque montee d'autorun (v53 -> v54), sans rien
  // apprendre. Ce qui compte : les copies servies ne divergent pas de la source.
  const TRACKED = [
    'player-brightsign/autorun.brs',
    'frontend/public/player-chromeos/brightsign/autorun.brs',
  ];
  // Copie de travail NON suivie par git (ce qui part sur la carte SD) : absente en CI,
  // signalee ici si elle a pris du retard, mais non assertee (cf. multizone_recovery).
  const SD_COPY = 'SD_card_BS/sd_image_clean/autorun.brs';
  const bodies = TRACKED
    .map((f) => path.join(ROOT, f))
    .filter((f) => fs.existsSync(f))
    .map((f) => [f, fs.readFileSync(f, 'utf8')]);
  check('autorun : les 2 copies suivies sont presentes', bodies.length === 2, bodies.map(([f]) => f));
  const verMatch = bodies.length ? bodies[0][1].match(/autorun_ver = "(\d+)"/) : null;
  check('autorun : version lisible dans la source', !!verMatch, null);
  const ver = verMatch ? verMatch[1] : '?';
  for (const [f, body] of bodies) {
    check(`autorun v${ver} (${path.basename(path.dirname(f))})`,
      body.includes(`autorun_ver = "${ver}"`)
      && body.includes('/api/v1/updates/renderers/brightsign/')
      && body.includes('Function ProbeUrl('), null);
  }
  const uniqueBodies = new Set(bodies.map(([, b]) => b));
  check('autorun : copies suivies strictement identiques', uniqueBodies.size === 1, bodies.map(([f]) => f));
  const sdFile = path.join(ROOT, SD_COPY);
  if (fs.existsSync(sdFile)) {
    const same = bodies.length && fs.readFileSync(sdFile, 'utf8') === bodies[0][1];
    console.log(`--   copie SD locale (${SD_COPY}) : ${same ? 'a jour' : 'EN RETARD sur la source, a recopier avant de graver une carte'} (informatif)`);
  }

  console.log(fail === 0 ? 'TOUS VERTS' : `${fail} ECHEC(S)`);
  process.exit(fail === 0 ? 0 : 1);
})();
