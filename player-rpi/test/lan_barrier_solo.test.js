// Barriere LAN — un player SEUL dans son groupe (2026-08-12, constat Ed du 11/08).
//
// SYMPTOME : le Minix, seul membre de son groupe de synchro LAN, demarrait son contenu avec
// des dizaines de secondes de retard sur le reste du parc, et sautait au milieu de la video
// (application de start_offset_ms au relachement). Sorti du groupe : +0 s, video entiere.
//
// CAUSE (lue dans le code, pas supposee) : `_electLeader()` n'etait appele QUE par le trafic
// des pairs — reception d'un message (_handleMessage) ou retrait d'un pair (_cleanupPeers).
// Un player seul n'a ni l'un ni l'autre : il ne devenait donc JAMAIS leader, `groupReady`
// n'etait meme jamais evalue, et la barriere ne s'ouvrait que par son filet anti-gel
// BARRIER_TIMEOUT_MS (25 s) — le filet devenu chemin nominal, a CHAQUE empreinte de contenu.
// La priorite n'y change rien : le maitre DESIGNE d'un groupe d'un membre est logé a la meme
// enseigne (verifie ici par le cas A, joue avec isMaster=true).
//
// CE QUE CE TEST FIGE — la frontiere, dans les DEUX sens :
//   A. seul + fenetre de decouverte ecoulee + pret        -> leader, barriere OUVERTE
//   B. seul + fenetre NON ecoulee                          -> reste slave, barriere FERMEE
//      (un player qui REDEMARRE dans un groupe existant ne doit pas se croire seul et partir
//       avant les autres : ce serait detruire la synchro LAN en croyant reparer le cas solo)
//   C. groupe de 2, pair PAS pret                          -> barriere FERMEE  (non-regression)
//   D. groupe de 2, pair pret sur la meme empreinte        -> barriere OUVERTE (nominal)
//   E. seul mais PAS pret                                  -> barriere FERMEE (attend son cache)
//
// Ce n'est PAS un portage : le test charge la VRAIE classe de chaque surface et la fait
// tourner (dgram simule), timer d'annonce compris — sans quoi le retrait du timer laisserait
// le test vert. La 4e surface (Android/Kotlin) n'est pas executable ici : elle est verifiee
// structurellement, faute de quoi un OS resterait en retard en silence.
//
// Usage : node player-rpi/test/lan_barrier_solo.test.js

const fs     = require('fs');
const path   = require('path');
const Module = require('module');

const ROOT = path.resolve(__dirname, '..', '..');

const SURFACES = [
  'player-rpi/src/sync_lan.js',
  'player-linux/src/sync_lan.js',
  'player-windows/src/sync_lan.js',
];
const KOTLIN = require('./_android_src').findAndroidSource('LanSyncController.kt');

let fail = 0;
function check(label, cond, detail) {
  console.log(`${cond ? 'OK   ' : 'ECHEC'} ${label}${cond ? '' : '  -> ' + JSON.stringify(detail)}`);
  if (!cond) fail++;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// --- dgram simule : aucun socket reel, mais on capture le handler de reception pour pouvoir
// livrer de vrais datagrammes au vrai `_handleMessage`.
const sockets = [];
const fakeDgram = {
  createSocket() {
    const s = {
      _onMessage: null,
      on(ev, cb) { if (ev === 'message') s._onMessage = cb; },
      bind(port, cb) { if (cb) cb(); },
      setBroadcast() {},
      send() {},
      close() {},
    };
    sockets.push(s);
    return s;
  },
};

const origLoad = Module._load;
Module._load = function (request) {
  if (request === 'dgram') return fakeDgram;
  return origLoad.apply(this, arguments);
};

const GROUP = 'g1';
const FP = 'fingerprint-abc123';

// Livre un datagramme au controleur comme le ferait le reseau.
function deliver(ctrl, msg) {
  const sock = ctrl.socket;
  if (!sock || !sock._onMessage) throw new Error('socket non demarre');
  sock._onMessage(Buffer.from(JSON.stringify(msg)), { address: '10.0.0.9' });
}

function newCase({ Ctl, idPlayer, isMaster = false, windowElapsed, ready, peer }) {
  const released = [];
  const ctrl = new Ctl(idPlayer, GROUP, () => {}, isMaster);
  ctrl.setBarrierReleaseHandler((fp) => released.push(fp));
  ctrl.start();
  // Antidate le debut d'ecoute pour ne pas attendre la fenetre en temps reel.
  if (windowElapsed) ctrl._startedAt = Date.now() - 60000;
  ctrl.setReadiness(FP, ready);
  // Le pair s'annonce (chemin reel : _handleMessage -> _setPeer -> _electLeader -> _checkBarrier).
  // id_player SUPERIEUR au notre pour que le noeud teste reste le leader legitime.
  if (peer) deliver(ctrl, { type: 'sync_announce', group: GROUP, id_player: idPlayer + 10, priority: 0, fp: FP, ready: peer.ready });
  return { ctrl, released };
}

(async () => {
  for (const rel of SURFACES) {
    const file = path.join(ROOT, rel);
    if (!fs.existsSync(file)) { check(`${rel} present`, false, 'fichier absent'); continue; }
    delete require.cache[require.resolve(file)];
    const { LanSyncController: Ctl } = require(file);

    console.log(`\n--- ${rel}`);

    // Les 5 cas tournent en parallele : chacun a son propre socket simule, ils ne se voient pas.
    const A = newCase({ Ctl, idPlayer: 1, isMaster: true,  windowElapsed: true,  ready: true });
    const B = newCase({ Ctl, idPlayer: 2, windowElapsed: false, ready: true });
    const C = newCase({ Ctl, idPlayer: 3, windowElapsed: true,  ready: true, peer: { ready: false } });
    const D = newCase({ Ctl, idPlayer: 4, windowElapsed: true,  ready: true, peer: { ready: true } });
    const E = newCase({ Ctl, idPlayer: 5, windowElapsed: true,  ready: false });

    // Un tick d'annonce (3 s) : c'est LUI qui doit reelire. Sans ce delai on ne prouverait que
    // le chemin declenche par message, celui qui marchait deja.
    await sleep(3400);

    check('A. seul + fenetre ecoulee -> devient leader',
      A.ctrl.isLeader === true, { isLeader: A.ctrl.isLeader });
    check('A. seul + fenetre ecoulee + pret -> barriere ouverte',
      A.released.length === 1 && A.released[0] === FP, { released: A.released });

    check('B. seul + fenetre NON ecoulee -> reste slave',
      B.ctrl.isLeader === false, { isLeader: B.ctrl.isLeader });
    check('B. seul + fenetre NON ecoulee -> barriere fermee',
      B.released.length === 0, { released: B.released });

    check('C. groupe de 2, pair pas pret -> barriere fermee',
      C.released.length === 0, { released: C.released, peers: C.ctrl.peers.size });

    check('D. groupe de 2, pair pret -> barriere ouverte',
      D.released.length === 1, { released: D.released, peers: D.ctrl.peers.size });

    check('E. seul mais pas pret -> barriere fermee',
      E.released.length === 0, { released: E.released });

    for (const c of [A, B, C, D, E]) c.ctrl.stop();
  }

  // --- Anti-divergence : les 3 surfaces JS doivent rester identiques.
  console.log('\n--- coherence des surfaces');
  const bodies = SURFACES.map((r) => fs.readFileSync(path.join(ROOT, r), 'utf8'));
  check('les 3 sync_lan.js sont identiques',
    bodies.every((b) => b === bodies[0]), { tailles: bodies.map((b) => b.length) });

  // --- Android/Kotlin : non executable ici, verifie structurellement. Un oubli laisserait le
  // Minix — le player qui a REVELE le defaut — avec le comportement fautif, sans un mot.
  check('kotlin: LanSyncController.kt trouve (un seul) sous player-android', !!KOTLIN, null);
  const kt = KOTLIN ? fs.readFileSync(KOTLIN, 'utf8') : '';
  check('kotlin: fenetre de decouverte definie',
    /DISCOVERY_WINDOW_MS\s*=\s*2\s*\*\s*ANNOUNCE_INTERVAL_MS/.test(kt), null);
  check('kotlin: garde de decouverte dans shouldBeLeader',
    /peers\.isEmpty\(\)\s*&&\s*\(System\.currentTimeMillis\(\)\s*-\s*startedAt\)\s*<\s*DISCOVERY_WINDOW_MS/.test(kt), null);
  const announceBlock = kt.match(/announceJob\s*=\s*scope\.launch\s*\{[\s\S]*?\n\s{12}\}/);
  check('kotlin: reelection dans la boucle d\'annonce',
    !!announceBlock && /electLeader\(\)/.test(announceBlock[0]) && /checkBarrier\(\)/.test(announceBlock[0]),
    announceBlock ? announceBlock[0].slice(0, 120) : 'bloc announceJob introuvable');
  check('kotlin: startedAt pose au demarrage',
    /startedAt\s*=\s*System\.currentTimeMillis\(\)/.test(kt), null);

  Module._load = origLoad;
  console.log(fail === 0 ? '\nTOUT OK' : `\n${fail} ECHEC(S)`);
  process.exit(fail === 0 ? 0 : 1);
})();
