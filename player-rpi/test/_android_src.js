// Trouve un fichier source Kotlin du player Android PAR SON NOM, ou qu'il soit sous
// player-android/app/src/main/java/.
//
// Les tests lisaient un chemin code en dur (app/signflow/player/...) : le renommage du
// paquet en app.vitrail.player les a casses en silence (ENOENT, hors CI). Chercher le
// fichier plutot que coder son paquet survit au prochain renommage.
//
// Renvoie le chemin absolu, ou null si aucun fichier (ou plusieurs) ne porte ce nom --
// l'appelant doit en faire un ECHEC visible, jamais un saut silencieux.
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const JAVA_ROOT = path.join(ROOT, 'player-android', 'app', 'src', 'main', 'java');

function findAndroidSource(fileName) {
  const hits = [];
  const walk = (dir) => {
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) walk(p);
      else if (e.name === fileName) hits.push(p);
    }
  };
  walk(JAVA_ROOT);
  return hits.length === 1 ? hits[0] : null;
}

module.exports = { findAndroidSource };
