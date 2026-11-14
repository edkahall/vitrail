// FREIN DE STOCKAGE — un player ne doit JAMAIS remplir son disque (defaut du 2026-08-06).
//
// Symptome, mesure en reel sur un stick Android TV de 4 Go :
//   · playlist assignee = 6 videos Ferrari, 1850 Mo ;
//   · Android 14 + Google TV occupaient deja l'essentiel de la partition ;
//   · le player a telecharge jusqu'a 100 % : 3,9 Go utilises, 0 octet libre.
// A partir de la, ce n'est plus notre app qui casse mais l'OS : SQLITE_FULL, keystore en
// echec, ENOSPC, applications systeme qui plantent. L'appareil n'a pu etre recupere qu'en
// DESINSTALLANT SignFlow — donc par un deplacement sur site chez un client.
//
// Cause : l'eviction (`enforceQuota`) raisonne en RATIO (85 % -> 70 %) et n'evince que le
// NON-epingle. Quand tout le cache est la playlist courante (= epingle), elle n'a rien a
// liberer : elle levait `pressure` pour prevenir le serveur... et le telechargement
// continuait quand meme. `pressure` etait un RAPPORT, pas un FREIN.
//
// Ce que ce test fige :
//   1. un plancher ABSOLU d'espace libre existe (les ratios ne suffisent pas) ;
//   2. sous ce plancher, aucun telechargement ne DEMARRE, et le job reste en file ;
//   3. le controle existe AUSSI pendant l'ecriture — la taille du fichier n'est pas connue
//      d'avance, donc un seul media peut a lui seul saturer le disque ;
//   4. une mesure impossible (statfs indisponible) ne freine RIEN : on ne bloque jamais la
//      diffusion sur une metrique qu'on ne sait pas prendre ;
//   5. les medias auxquels on renonce sont REMONTES au serveur (sinon l'ecran diffuse une
//      playlist tronquee sans que personne ne sache pourquoi) ;
//   6. on ne detruit jamais l'epingle pour faire de la place (regle inchangee).
//
// Le test lit les 3 fichiers Electron REELS : il verifie donc aussi qu'aucune des 3 copies
// n'a ete oubliee, et qu'elles n'ont pas diverge.
//
// Usage : node player-rpi/test/storage_brake.test.js

const fs = require('fs');
const path = require('path');
const os = require('os');
const Module = require('module');

const ROOT = path.resolve(__dirname, '..', '..');
const COPIES = [
  'player-rpi/src/content_store.js',
  'player-linux/src/content_store.js',
  'player-windows/src/content_store.js',
];

let failures = 0;
// La condition est evaluee ICI, protegee : sur un code d'origine ou la propriete n'existe
// pas encore, le test doit RAPPORTER la defaillance, jamais s'interrompre dessus (sinon
// l'A/B ne montre que la premiere).
function check(label, cond, detail) {
  try { cond = (typeof cond === 'function') ? cond() : cond; }
  catch (e) { cond = false; detail = (detail ? detail + ' ; ' : '') + e.message; }
  if (cond) { console.log(`  ok   ${label}`); }
  else { failures++; console.log(`  FAIL ${label}${detail ? ' — ' + detail : ''}`); }
}

// ── 1. Les 3 copies Electron sont identiques ────────────────────────────────────
console.log('Les 3 players Electron partagent le meme content_store');
const sources = COPIES.map((rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8'));
check('les 3 copies sont identiques', sources.every((s) => s === sources[0]),
  'une copie a diverge : le frein doit exister sur les 3');

// ── 2. Le plancher absolu existe, et il est distinct des watermarks ─────────────
console.log('Le frein repose sur un plancher ABSOLU, pas sur un ratio');
const src = sources[0];
check('MIN_FREE_BYTES est defini', /const\s+MIN_FREE_BYTES\s*=/.test(src));
check('il est exprime en octets, pas en pourcentage',
  /MIN_FREE_BYTES\s*=\s*\d+\s*\*\s*1024\s*\*\s*1024/.test(src));
check('les watermarks en ratio existent toujours (eviction inchangee)',
  /HIGH_WATERMARK\s*=\s*0\.85/.test(src) && /LOW_WATERMARK\s*=\s*0\.70/.test(src));

// ── 3. Chargement du vrai module, avec un statfs pilotable ─────────────────────
// On instrumente `fs.statfsSync` pour simuler un disque plein sans en remplir un vrai.
function loadStore(statfsImpl) {
  const real = fs.statfsSync;
  fs.statfsSync = statfsImpl;
  const p = path.join(ROOT, COPIES[0]);
  delete require.cache[require.resolve(p)];
  const mod = require(p);
  fs.statfsSync = real;
  return mod;
}

const BS = 4096;
// Disque plein : 0 bloc disponible.
const statfsFull = () => ({ bsize: BS, blocks: 1_000_000, bavail: 0, bfree: 0 });
// Disque confortable : 4 Go libres.
const statfsRoomy = () => ({ bsize: BS, blocks: 4_000_000, bavail: 1_000_000, bfree: 1_000_000 });
// Mesure impossible.
const statfsBroken = () => { throw new Error('statfs indisponible'); };

const StoreMod = loadStore(statfsRoomy);
const ContentStore = StoreMod.ContentStore || StoreMod;
check('le module expose ContentStore', typeof ContentStore === 'function',
  'export inattendu : ' + Object.keys(StoreMod || {}).join(','));

function makeStore(statfsImpl) {
  const real = fs.statfsSync;
  fs.statfsSync = statfsImpl;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sfstore-'));
  const s = new ContentStore(dir, () => {});
  s.__restore = () => { fs.statfsSync = real; };
  s.__statfs = statfsImpl;
  return s;
}

// ── 4. Sous le plancher : aucun demarrage, le job reste en file ────────────────
console.log('Sous le plancher, aucun telechargement ne DEMARRE');
{
  const real = fs.statfsSync;
  fs.statfsSync = statfsFull;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sfstore-'));
  const store = new ContentStore(dir, () => {});
  let downloaded = false;
  store._download = () => { downloaded = true; return Promise.resolve(true); };

  store.enqueue({
    cache_key: 'a.mp4', cache_checksum: null, mime: 'video/mp4',
    url: 'http://127.0.0.1:9/a.mp4', id_media: 42, priority: 1, isPrefetch: false,
  });

  check('aucun telechargement lance', downloaded === false);
  check('le job N EST PAS jete (il repartira si la place revient)', store.queue.length === 1,
    `queue=${store.queue.length}`);
  check('le media renonce est signale', !!store.blocked && store.blocked.has(42));
  check('la pression est levee pour le serveur', store._pressure === true);

  const st = store.getStorageStatus();
  check('blocked_ids est remonte au serveur', Array.isArray(st.blocked_ids) && st.blocked_ids.includes(42));
  check('free_bytes est remonte', st.free_bytes === 0);
  check('disk_total_bytes est remonte (point 4 : controle a l affectation)',
    st.disk_total_bytes === 1_000_000 * BS);
  fs.statfsSync = real;
}

// ── 5. Mesure impossible : on ne freine RIEN ───────────────────────────────────
console.log('Une mesure impossible ne doit jamais bloquer la diffusion');
{
  const real = fs.statfsSync;
  fs.statfsSync = statfsBroken;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sfstore-'));
  const store = new ContentStore(dir, () => {});
  let downloaded = false;
  store._download = () => { downloaded = true; return Promise.resolve(true); };

  store.enqueue({
    cache_key: 'b.mp4', cache_checksum: null, mime: 'video/mp4',
    url: 'http://127.0.0.1:9/b.mp4', id_media: 43, priority: 1, isPrefetch: false,
  });

  check('le telechargement demarre malgre statfs casse', downloaded === true);
  check('aucun media marque bloque', (store.blocked ? store.blocked.size : 0) === 0);
  fs.statfsSync = real;
}

// ── 6. Place disponible : comportement inchange ────────────────────────────────
console.log('Avec de la place, rien ne change');
{
  const real = fs.statfsSync;
  fs.statfsSync = statfsRoomy;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sfstore-'));
  const store = new ContentStore(dir, () => {});
  let downloaded = false;
  store._download = () => { downloaded = true; return Promise.resolve(true); };

  store.enqueue({
    cache_key: 'c.mp4', cache_checksum: null, mime: 'video/mp4',
    url: 'http://127.0.0.1:9/c.mp4', id_media: 44, priority: 1, isPrefetch: false,
  });

  check('le telechargement demarre', downloaded === true);
  check('aucune pression', store._pressure === false);
  fs.statfsSync = real;
}

// ── 7. Le frein existe AUSSI pendant l'ecriture ────────────────────────────────
// La taille du fichier n'est pas connue avant de commencer : sans ce controle, un seul
// media peut saturer le disque a lui seul. On le verifie sur la SOURCE (le chemin reseau
// n'est pas exercable ici sans serveur HTTP).
console.log('Le frein agit aussi PENDANT l ecriture');
check('un garde periodique est arme pendant le download', /setInterval\([\s\S]{0,400}MIN_FREE_BYTES/.test(src));
check('il abandonne le .part au lieu de le garder (sinon il immobilise la place manquante)',
  /insufficient-space/.test(src) && /fs\.unlink\(tmp/.test(src));
check('il coupe la requete HTTP', /req\.destroy\(\)/.test(src));

// ── 8. L'epingle n'est jamais detruit pour faire de la place ───────────────────
console.log('On ne sacrifie jamais la playlist courante');
check('l eviction ne porte que sur le NON-epingle',
  /filter\(\(\[k\]\) => !this\.pinned\.has\(k\)\)/.test(src));

// ── 9. ANDROID : la 4e surface du groupe A doit porter les MEMES garanties ─────
// Le stick Android est l'appareil qui a revele le defaut : il serait absurde qu'il soit le
// seul a ne pas etre protege. On lit le vrai ContentStore.kt (portage, pas copie : on
// verifie les GARANTIES, pas le texte).
console.log('Android porte les memes garanties (4e surface du groupe A)');
const KT = require('./_android_src').findAndroidSource('ContentStore.kt');
check('ContentStore.kt trouve (un seul) sous player-android', !!KT);
const kt = KT ? fs.readFileSync(KT, 'utf8') : '';
check('plancher absolu defini', /const\s+val\s+MIN_FREE_BYTES\s*=\s*512L/.test(kt));
check('frein AVANT demarrage (refus de pump)', /freeBytes\(\)\s*<\s*MIN_FREE_BYTES/.test(kt));
check('frein PENDANT l ecriture', /FREE_CHECK_EVERY_BYTES/.test(kt) && /insufficient-space/.test(kt));
check('le .part est abandonne, pas conserve', /tmp\.delete\(\)/.test(kt));
check('mesure impossible => AUCUN frein', /Long\.MAX_VALUE/.test(kt));
check('medias renonces remontes au serveur', /blocked_ids/.test(kt));
check('capacite absolue remontee (point 4)', /disk_total_bytes/.test(kt));
check('l eviction ne porte toujours que sur le NON-epingle',
  /filter\s*\{\s*!pinned\.contains/.test(kt));

// ── 10. GROUPE B : surfaces navigateur (ChromeOS, extension, BrightSign) ───────
// Elles ne peuvent PAS saturer la partition — le navigateur refuse l'ecriture au-dela du
// quota d'origine. Elles n'ont donc pas besoin du frein. Mais elles portaient
// `storage_pressure: false` EN DUR : elles ne pouvaient STRUCTURELLEMENT jamais signaler
// de pression, si bien qu'une alerte serveur branchee dessus serait restee muette a vie.
// Une garde inerte est pire que pas de garde : on se croit couvert.
console.log('Groupe B (navigateur) : la pression est REELLE, plus codee en dur');
const BROWSER = [
  'frontend/public/player-chromeos/signflow.js',
  'player-extension/player/signflow.js',
];
for (const rel of BROWSER) {
  const b = fs.readFileSync(path.join(ROOT, rel), 'utf8');
  const name = rel.split('/').slice(-2).join('/');
  check(`${name} : plus de storage_pressure code en dur`,
    !/storage_pressure:\s*false/.test(b));
  check(`${name} : le refus de quota est ATTRAPE`,
    /QuotaExceededError/.test(b));
  check(`${name} : le media renonce est remonte`,
    /blocked_ids/.test(b) && /_blockedMedia/.test(b));
  check(`${name} : capacite absolue remontee (point 4)`,
    /disk_total_bytes/.test(b));
  check(`${name} : jamais la capacite navigateur sur BrightSign (c'est la SD qui porte les medias)`,
    /!IS_BS\s*&&\s*_diskQuotaBytes/.test(b));
}

console.log(failures === 0 ? '\nTous verts.' : `\n${failures} ECHEC(S).`);
process.exit(failures === 0 ? 0 : 1);
