// Jitter de reconnexion — reconnectDelayMs (P3.12, 2026-08-07).
//
// Defaut d'origine : le backoff etait exponentiel mais ENTIEREMENT DETERMINISTE
// (`Math.min(30000, 2000 * Math.pow(2, reconnectAttempts))`, identique sur les 6 surfaces).
// Apres une coupure de courant ou un redemarrage serveur, tous les ecrans repartent au meme
// instant, echouent au meme instant, et retentent a EXACTEMENT 2s, 4s, 8s... Leurs
// tentatives restent en phase : la rafale ne se disperse jamais, elle se rejoue a chaque
// palier. Mesure serveur du 02/08 : ~20 s de latence en pointe sur le premier player:sync
// a 400 reconnexions simultanees, un seul worker uvicorn serialisant les resolutions.
//
// Ce test n'est PAS un portage : il EXTRAIT la fonction reelle de chaque fichier et
// l'EXECUTE. Il verifie donc le comportement ET que les surfaces ne divergent pas -- en
// oublier une, c'est un OS qui reste synchronise en silence, donc une rafale intacte.
//
// Usage : node player-rpi/test/reconnect_jitter.test.js

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');

// Les surfaces qui tiennent une WebSocket vers le serveur. SD_card_BS/ est une copie de
// travail non suivie par git : on la verifie SI elle est presente (c'est ce qui part sur la
// carte SD des BrightSign).
const SURFACES = [
  'player-rpi/src/main.js',
  'player-linux/src/main.js',
  'player-windows/src/main.js',
  'player-extension/player/signflow.js',
  'frontend/public/player-chromeos/signflow.js',
  'SD_card_BS/sd_image_clean/sf_rnd_signflow.js',
];

let fail = 0;
function check(label, cond, detail) {
  console.log(`${cond ? 'OK  ' : 'ECHEC'} ${label}${cond ? '' : '  -> ' + JSON.stringify(detail)}`);
  if (!cond) fail++;
}

function extract(file) {
  const src = fs.readFileSync(file, 'utf8');
  const m = src.match(/function reconnectDelayMs\(attempts\) \{[\s\S]*?\n *\}/);
  return m ? m[0] : null;
}

const found = [];
for (const rel of SURFACES) {
  const file = path.join(ROOT, rel);
  if (!fs.existsSync(file)) {
    console.log(`--   ${rel} (absent, ignore)`);
    continue;
  }
  const code = extract(file);
  check(`${rel} : fonction presente`, !!code, rel);
  if (!code) continue;

  // eslint-disable-next-line no-new-func
  const fn = new Function(`${code}; return reconnectDelayMs;`)();
  found.push({ rel, fn });

  // 1. La borne haute reste celle d'avant : on etale, on ne rallonge pas l'attente.
  const samples = Array.from({ length: 400 }, () => fn(0));
  const max = Math.max(...samples);
  const min = Math.min(...samples);
  check(`${rel} : jamais au-dela de la borne (2000 ms a la 1re tentative)`, max <= 2000,
        { max });

  // 2. Le delai n'est jamais quasi nul : un jitter « plein » martelerait un serveur qui
  //    vient de repartir. On garde la moitie haute.
  check(`${rel} : jamais en dessous de la moitie de la borne`, min >= 1000, { min });

  // 3. LE POINT DU CORRECTIF : deux ecrans ne tombent pas sur le meme instant. Avec
  //    l'ancienne formule, `samples` ne contenait QU'UNE seule valeur distincte.
  const distinct = new Set(samples).size;
  check(`${rel} : les tentatives se dispersent (${distinct} valeurs distinctes / 400)`,
        distinct > 50, { distinct });

  // 4. Le backoff reste exponentiel : la borne double a chaque tentative, et plafonne.
  const ceilAt = (n) => Math.max(...Array.from({ length: 300 }, () => fn(n)));
  check(`${rel} : la borne croit avec les tentatives`,
        ceilAt(1) > ceilAt(0) && ceilAt(2) > ceilAt(1), { c0: ceilAt(0), c1: ceilAt(1) });
  check(`${rel} : plafonnee a 30 s`, ceilAt(10) <= 30000 && ceilAt(10) > 15000,
        { c10: ceilAt(10) });
}

// 5. Etalement d'entree (P3.12, decision Ed du 07/08). Le jitter ci-dessus ne joue QUE si
//    le serveur est injoignable : quand un site retrouve le courant et que le serveur, lui,
//    n'a jamais bouge, la premiere connexion reussit du premier coup et les ecrans arrivent
//    groupes. Ces assertions verifient que chaque surface tire un retard d'entree, UNE fois.
for (const rel of SURFACES) {
  const file = path.join(ROOT, rel);
  if (!fs.existsSync(file)) continue;
  const src = fs.readFileSync(file, 'utf8');

  check(`${rel} : fenetre d'etalement declaree`,
        /const STARTUP_SPREAD_MS = 5000;/.test(src), rel);
  // Tire au hasard : une valeur fixe regrouperait tout le parc sur le meme instant, ce qui
  // serait STRICTEMENT PIRE que pas d'etalement du tout.
  check(`${rel} : le retard d'entree est TIRE AU HASARD`,
        /Math\.random\(\) \* STARTUP_SPREAD_MS/.test(src), rel);
  // Desarme apres le premier passage, sinon chaque reconnexion paierait le retard.
  check(`${rel} : l'etalement ne s'applique qu'une fois`,
        /_startupSpreadDone = true;/.test(src) && /if \(!_startupSpreadDone\)/.test(src), rel);
}

// 6. Anti-divergence : toutes les surfaces doivent produire la MEME distribution. Une
//    surface laissee derriere ne se verrait pas autrement -- elle diffuserait normalement.
//    On fige Math.random sur des valeurs connues et on compare les sorties EXACTEMENT. La
//    version precedente comparait les min/max EMPIRIQUES de 400 tirages a +/-60 ms pres :
//    sur une fenetre de 8 s, l'ecart attendu entre extremes de deux surfaces depassait
//    regulierement 60 ms, d'ou un echec ~1 passage sur 3 sur du code juste.
if (found.length > 1) {
  const RANDOMS = [0, 0.001, 0.25, 0.5, 0.75, 0.999999];
  const realRandom = Math.random;
  let table;
  try {
    table = found.map(({ rel, fn }) => ({
      rel,
      out: RANDOMS.flatMap((r) => {
        Math.random = () => r;
        return Array.from({ length: 11 }, (_, n) => fn(n));
      }),
    }));
  } finally {
    Math.random = realRandom;
  }
  const ref = JSON.stringify(table[0].out);
  const diverging = table.filter((t) => JSON.stringify(t.out) !== ref).map((t) => t.rel);
  check(`les ${found.length} surfaces partagent la meme distribution`, diverging.length === 0,
        { reference: table[0].rel, diverging });
  // Temoin : la sortie depend bien du tirage (sinon la comparaison ci-dessus serait vacante).
  const first = table[0].out;
  check('temoin : Math.random fige change bien la sortie', first[3] !== first[3 + 11 * 5],
        { r0: first[3], rMax: first[3 + 11 * 5] });
}

console.log(fail === 0 ? `\nTOUT VERT (${found.length} surfaces)` : `\n${fail} ECHEC(S)`);
process.exit(fail === 0 ? 0 : 1);
