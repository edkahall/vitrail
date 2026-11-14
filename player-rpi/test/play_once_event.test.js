'use strict';

/**
 * Un event « joue une fois » ne doit PAS reboucler — les 6 surfaces de rendu.
 *
 * Constat d'Ed (2026-08-11) sur la prod et le VPS : un event configuré pour jouer
 * une vidéo une fois toutes les 30 min « recommence à la fin pour s'arrêter
 * quelques secondes après avoir redémarré ».
 *
 * Chaîne mesurée :
 *  · `duration_ms` renseigné = mode play-once CÔTÉ SERVEUR (`event_worker`) ;
 *  · mais le payload ne le disait pas au player, et `advanceFromItem` rebouclait
 *    TOUJOURS en fin de liste (`(currentIndex + 1) % playlist.length`) ;
 *  · le player avance sur un MINUTEUR réglé sur `item.duration_ms`, donc il
 *    relançait la vidéo à la seconde même où l'event expirait ;
 *  · `expire_events` ne tourne que toutes les 30 s → coupure 0-30 s plus tard.
 *
 * Ce test EXÉCUTE la vraie fonction `advanceFromItem` de chaque surface dans un
 * bac à sable, plutôt que de relire le code. Il vérifie les deux sens :
 *   ① play_once + fin de liste  → ne reboucle pas, et ne casse PAS la WebSocket ;
 *   ② programmation normale     → reboucle comme avant (non-régression).
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');

const SURFACES = [
  'frontend/public/player-chromeos/player.js',   // ChromeOS + BrightSign (servi)
  'player-android/app/src/main/assets/player/player.js',
  'player-extension/player/player.js',
  'player-linux/src/renderer/player.js',
  'player-rpi/src/renderer/player.js',
  'player-windows/src/renderer/player.js',
];

let fail = 0;
function check(label, cond, detail) {
  console.log(`${cond ? 'OK  ' : 'ECHEC'} ${label}${cond ? '' : '  -> ' + JSON.stringify(detail)}`);
  if (!cond) fail++;
}

function extract(file) {
  const src = fs.readFileSync(file, 'utf8');
  const m = src.match(/function advanceFromItem\(item\) \{[\s\S]*?\n\}/);
  if (!m) return null;
  // advanceFromItem appelle touchDocRun (feuilletage tactile, 14/08) : on extrait la VRAIE
  // fonction avec elle, sinon ReferenceError. touchInteracting, elle, depend de l'horloge et
  // des gestes : elle est bouchonnee dans le bac a sable (aucun geste = false).
  const dep = src.match(/function touchDocRun\(index\) \{[\s\S]*?\n\}/);
  return dep ? `${dep[0]}\n${m[0]}` : m[0];
}

/**
 * Bac à sable à VRAIES LIAISONS. ⚠️ Ne pas utiliser `with` + Proxy : un Proxy qui
 * répond à TOUT identifiant rend muettes les fonctions qu'on croit observer et
 * produit des verdicts incohérents sur du code juste (piège vécu les 06 et 07/08).
 */
function run(code, state) {
  const calls = { resync: [], played: 0, timers: [] };
  const sandbox = `
    let _pendingSync = ${JSON.stringify(state.pendingSync)};
    let _playOnce = ${state.playOnce};
    let currentIndex = ${state.currentIndex};
    let playlist = ${JSON.stringify(state.playlist)};
    let itemTimer = null;
    let _currentContentFP = null;
    function log() {}
    function clearTimeout() {}
    function setTimeout(fn, ms) { __calls.timers.push(ms); return 1; }
    function requestResyncDebounced(r) { __calls.resync.push(r); }
    function playCurrentItem() { __calls.played++; }
    function applyTransition(t, fn) { if (fn) fn(); }
    function saveSync() {}
    function clearPreload() {}
    function _getContentFP() { return 'fp'; }
    function touchInteracting() { return false; }
    ${code}
    advanceFromItem(${JSON.stringify(state.item)});
    return { currentIndex, playOnce: _playOnce };
  `;
  // eslint-disable-next-line no-new-func
  const out = new Function('__calls', sandbox)(calls);
  return { ...out, calls };
}

console.log('=== Event « joue une fois » : le player ne reboucle plus ===\n');

let surfaces = 0;
for (const rel of SURFACES) {
  const file = path.join(ROOT, rel);
  if (!fs.existsSync(file)) { console.log(`--   ${rel} (absent)`); continue; }
  const code = extract(file);
  check(`${rel} : advanceFromItem present`, !!code, rel);
  if (!code) continue;
  surfaces++;

  const base = {
    pendingSync: null, currentIndex: 0, item: { transition_type: 'fade' },
    playlist: [{ id_item: 1, transition_type: 'fade' }],
  };

  // ① play_once, dernier item : ne reboucle pas, ne touche pas au reseau, pose un filet.
  const once = run(code, { ...base, playOnce: true });
  check(`${rel} : play_once ne relance pas le media`, once.calls.played === 0, once.calls);
  // ⛔ Le contraire de ce que ce test verifiait au premier jet : `requestResync` ne
  // demande PAS une synchro, elle TUE LA WEBSOCKET (primitive ecrite pour une URL
  // MinIO expiree). L'appeler a chaque fin d'event = une coupure de WS toutes les
  // 30 min par ecran + les 0-5 s de jitter de P3.12, donc une derniere image figee
  // qui se lit comme une panne (constat d'Ed a l'ecran). C'est le SERVEUR qui reprend
  // la main, en programmant l'expiration a la seconde pres.
  check(`${rel} : play_once ne casse PAS la WebSocket`, once.calls.resync.length === 0, once.calls);
  check(`${rel} : play_once pose un filet anti-gel`,
    once.calls.timers.length === 1 && once.calls.timers[0] >= 10000
      && once.calls.timers[0] <= 30000, once.calls.timers);

  // ② Programmation normale : le comportement d'avant est INCHANGÉ.
  const loop = run(code, { ...base, playOnce: false });
  check(`${rel} : sans play_once, la liste reboucle`, loop.calls.played === 1, loop.calls);
  check(`${rel} : sans play_once, aucune synchro demandee`, loop.calls.resync.length === 0, loop.calls);

  // ③ Plusieurs items : play_once n'arrete QUE sur le dernier, pas au milieu.
  const mid = run(code, {
    ...base, playOnce: true, currentIndex: 0,
    playlist: [{ id_item: 1 }, { id_item: 2 }, { id_item: 3 }],
  });
  check(`${rel} : play_once avance normalement au MILIEU de la liste`,
    mid.calls.played === 1 && mid.currentIndex === 1, { played: mid.calls.played, i: mid.currentIndex });

  const last = run(code, {
    ...base, playOnce: true, currentIndex: 2,
    playlist: [{ id_item: 1 }, { id_item: 2 }, { id_item: 3 }],
  });
  check(`${rel} : play_once s'arrete sur le DERNIER item`,
    last.calls.played === 0 && last.calls.timers.length === 1, last.calls);
}

check('les 6 surfaces sont couvertes', surfaces === 6, surfaces);

console.log(`\n${fail === 0 ? 'TOUT VERT' : fail + ' ECHEC(S)'}`);
process.exit(fail === 0 ? 0 : 1);
