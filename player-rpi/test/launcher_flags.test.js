// Options du lanceur RPi lues dans launcher-flags.conf (issue vitrail#4).
//
// Les options GPU/GL de Chromium dependent du moteur Electron (Electron 42 a supprime
// --use-gl=egl) et etaient gravees dans start-player.sh a l'installation : un player monte
// d'Electron restait sur les anciennes options, donc en rendu logiciel. start-player.sh les
// lit desormais dans un fichier, avec un repli integre.
//
// Ce test n'est PAS un portage : il fait generer start-player.sh par le VRAI heredoc
// d'install.sh (par bash), en extrait le bloc de lecture des options et l'EXECUTE sous bash
// contre des fichiers de cas.
//
// Usage : node player-rpi/test/launcher_flags.test.js
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..', '..');
const INSTALL = path.join(ROOT, 'player-rpi', 'install.sh');
const SHIPPED = path.join(ROOT, 'player-rpi', 'launcher-flags.conf');
const DEFAULTS = ['--enable-gpu', '--use-gl=angle', '--use-angle=gles', '--use-v4l2-codec'];

let fail = 0;
function check(label, cond, detail) {
  console.log(`${cond ? 'OK   ' : 'ECHEC'} ${label}${cond ? '' : '  -> ' + JSON.stringify(detail)}`);
  if (!cond) fail++;
}
const bash = (script, env = {}) =>
  spawnSync('bash', ['-c', script], { encoding: 'utf8', env: { ...process.env, ...env } });

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-launcher-'));
// Chemins passes a bash en barres obliques (Git Bash sous Windows les accepte ainsi).
const posix = (p) => p.replace(/\\/g, '/');

// ── 1. Generer start-player.sh avec le vrai heredoc d'install.sh ─────────────
const src = fs.readFileSync(INSTALL, 'utf8').replace(/\r\n/g, '\n');
const m = src.match(/^cat > "\$START_SCRIPT" <<EOF\n[\s\S]*?\nEOF$/m);
check('heredoc de start-player.sh trouve dans install.sh', !!m, null);
if (!m) process.exit(1);
const generated = path.join(tmp, 'start-player.sh');
const gen = bash(m[0].replace('"$START_SCRIPT"', '"$OUT"'), {
  OUT: posix(generated), PLAYER_DIR: '/opt/fake/player-rpi',
  LOG_FILE: '/tmp/x.log', WLR_OUTPUT: '',
});
check('generation par bash', gen.status === 0, gen.stderr);
const script = fs.readFileSync(generated, 'utf8');
check('start-player.sh genere est syntaxiquement valide',
  bash(`bash -n "${posix(generated)}"`).status === 0, null);

// L'appel Electron doit utiliser le tableau, et ne plus porter d'option GL en dur.
const call = script.match(/node_modules\/\.bin\/electron"[\s\S]*?>> "\$LOG" 2>&1/);
check('Electron recoit "${_engine_flags[@]}"', !!call && call[0].includes('"${_engine_flags[@]}"'),
  call && call[0]);
check('plus aucune option GL en dur sur la ligne Electron',
  !!call && !/--use-gl|--use-angle|--enable-gpu/.test(call[0]), call && call[0]);
check('options kiosque fixes toujours presentes',
  !!call && ['--ozone-platform=wayland', '--no-sandbox', '--start-fullscreen']
    .every((f) => call[0].includes(f)), call && call[0]);
check('chemin du fichier = racine du player (hors src/, donc hors OTA)',
  script.includes('_flags_file="/opt/fake/player-rpi/launcher-flags.conf"'), null);

// Le fichier est relu DANS la boucle (avant chaque relance d'Electron), pas une fois.
const loopStart = script.indexOf('\nwhile true; do');
check('lecture du fichier DANS la boucle de relance',
  loopStart > 0 && script.indexOf('_flags_file=') > loopStart, { loopStart });

// ── 2. Executer le bloc de lecture contre des cas ────────────────────────────
const block = script.match(/^ *_flags_file=.*\n[\s\S]*?Launcher flags \(.*\n/m);
check('bloc de lecture extrait', !!block, null);
if (!block) process.exit(1);

function run(content) {
  const file = path.join(tmp, `flags-${Math.random().toString(36).slice(2)}.conf`);
  const log = path.join(tmp, 'log.txt');
  fs.writeFileSync(log, '');
  if (content !== null) fs.writeFileSync(file, content);
  const body = block[0].replace(/^ *_flags_file=.*$/m, `_flags_file="${posix(file)}"`);
  const r = bash(`LOG="${posix(log)}"\n${body}\nprintf '%s\\n' "\${_engine_flags[@]}"`);
  return {
    flags: r.stdout.split('\n').filter((l) => l !== ''),
    log: fs.readFileSync(log, 'utf8'),
    status: r.status, err: r.stderr,
  };
}
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

let r = run(null);
check('fichier absent -> repli integre', same(r.flags, DEFAULTS), r);
check('fichier absent -> journal dit "built-in defaults"', /built-in defaults/.test(r.log), r.log);

r = run('# comment\n\n--use-gl=angle\n  --use-angle=gles  \n--enable-gpu\n');
check('fichier valide -> ses options, dans l\'ordre, espaces retires',
  same(r.flags, ['--use-gl=angle', '--use-angle=gles', '--enable-gpu']), r);
check('fichier valide -> journal nomme le fichier', /launcher-flags|flags-.*\.conf/.test(r.log), r.log);

r = run('--use-gl=angle\r\n--use-angle=gles\r\n');
check('CRLF (edite sous Windows) -> aucun \\r dans les options',
  same(r.flags, ['--use-gl=angle', '--use-angle=gles']), r);

r = run('--enable-gpu\nrm -rf /\nuse-gl=egl\n--\n--use-gl=angle\n');
check('lignes invalides ignorees, options valides gardees',
  same(r.flags, ['--enable-gpu', '--use-gl=angle']), r);
check('lignes invalides tracees au journal',
  /ignored line.*rm -rf/.test(r.log) && /ignored line.*use-gl=egl/.test(r.log), r.log);

r = run('# only comments\n\n');
check('aucune option valide -> repli integre', same(r.flags, DEFAULTS), r);

r = run('');
check('fichier vide -> repli integre', same(r.flags, DEFAULTS), r);

r = run('--enable-gpu\n--use-gl=angle');
check('derniere ligne sans fin de ligne -> lue quand meme',
  same(r.flags, ['--enable-gpu', '--use-gl=angle']), r);

r = run('--js-flags=--max-old-space-size=512 --expose-gc\n');
check('une valeur avec espaces reste UN seul argument',
  same(r.flags, ['--js-flags=--max-old-space-size=512 --expose-gc']), r);

r = run('--x=$(touch PWNED)\n--y=`id`\n');
check('aucune expansion du contenu (pas d\'eval)',
  same(r.flags, ['--x=$(touch PWNED)', '--y=`id`']) && !fs.existsSync('PWNED'), r);

// ── 3. Le fichier livre = les memes options que le repli ─────────────────────
const shipped = run(fs.readFileSync(SHIPPED, 'utf8'));
check('launcher-flags.conf livre = repli integre (meme comportement avec ou sans)',
  same(shipped.flags, DEFAULTS), shipped.flags);
check('ses commentaires ne sont PAS tracés comme lignes invalides (journal propre)',
  !/ignored line/.test(shipped.log), shipped.log);

fs.rmSync(tmp, { recursive: true, force: true });
console.log(fail === 0 ? '\nTOUT VERT' : `\n${fail} ECHEC(S)`);
process.exit(fail === 0 ? 0 : 1);
