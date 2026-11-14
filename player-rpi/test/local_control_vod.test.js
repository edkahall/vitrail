// Test fonctionnel du panneau de controle local — VOD groupees PAR CONFIG (2026-07-19).
//
// Aucun test ne couvrait ce panneau, et c'est precisement pour ca que les bugs rapportes par
// Ed sont passes : configs VOD fusionnees a l'affichage, liste de players du site entier au
// lieu de la cible reelle, et aucun controle du son.
//
// Usage :  node player-rpi/test/local_control_vod.test.js <chemin/local_control_server.js>
// Sortie : une ligne OK/ECHEC par assertion ; code de sortie 1 si un echec.
//         node player-rpi/test/local_control_vod.test.js     (sans argument : les 3 players
//         Electron, chacun dans son propre processus -- c'est ce que lance `node --test`)
const http = require('http');
const path = require('path');

if (!process.argv[2]) {
  const { spawnSync } = require('child_process');
  const ROOT = path.resolve(__dirname, '..', '..');
  let failed = 0;
  for (const p of ['player-rpi', 'player-linux', 'player-windows']) {
    const target = path.join(ROOT, p, 'src', 'local_control_server.js');
    console.log(`\n=== ${p} ===`);
    const r = spawnSync(process.execPath, [__filename, target], { stdio: 'inherit' });
    if (r.status !== 0) failed++;
  }
  console.log(failed ? `\n${failed} player(s) en ECHEC` : '\nTOUT VERT (3 players)');
  process.exit(failed ? 1 : 0);
}

// Chemin resolu en ABSOLU : require() resout sinon relativement a CE fichier, pas au cwd.
const { LocalControlServer } = require(path.resolve(process.cwd(), process.argv[2]));

const CONFIGS = [
  { id_vod: 1, vod_name: 'VOD_Domicile',
    medias: [{ id_media: 10, media_name: 'clip.mp4', media_type: 'video', has_audio: false }],
    players: [{ id_player: 2, name: 'AOpen' }, { id_player: 4, name: 'RPi' },
              { id_player: 22, name: 'NUC' }] },
  { id_vod: 3, vod_name: 'VOD_Twingo',
    medias: [{ id_media: 99, media_name: 'twingo.mp4', media_type: 'video', has_audio: true }],
    players: [{ id_player: 4, name: 'RPi' }, { id_player: 22, name: 'NUC' }] },
];

const actions = [];
const srv = new LocalControlServer({
  port: 18765,
  getProfile: () => ({ token: 'T'.repeat(24), role: 'manager', can_trigger_event: true }),
  getPlayerName: () => 'NUC',
  getVodConfigs: () => CONFIGS,
  getVodMedia: () => [{ id_media: 99, media_name: 'twingo.mp4', media_type: 'video' }],
  getSelfId: () => 22,
  onAction: (a) => actions.push(a),
});
srv.start();

function req(method, path, body, session) {
  return new Promise((resolve) => {
    const data = body ? JSON.stringify(body) : null;
    const headers = { 'Content-Type': 'application/json' };
    if (session) headers['Authorization'] = 'Bearer ' + session;
    const r = http.request({ host: '127.0.0.1', port: 18765, method, path, headers }, (res) => {
      let b = ''; res.on('data', (c) => b += c);
      res.on('end', () => { try { resolve({ code: res.statusCode, json: JSON.parse(b || '{}') }); }
                            catch { resolve({ code: res.statusCode, json: {} }); } });
    });
    r.on('error', () => resolve({ code: 0, json: {} }));
    if (data) r.write(data);
    r.end();
  });
}

let fail = 0;
function check(label, cond, detail) {
  console.log(`${cond ? 'OK  ' : 'ECHEC'} ${label}${cond ? '' : '  -> ' + JSON.stringify(detail)}`);
  if (!cond) fail++;
}

(async () => {
  const a = await req('POST', '/control/auth', { token: 'T'.repeat(24) });
  check('auth', a.code === 200 && a.json.session, a);
  const S = a.json.session;

  const v = await req('GET', '/control/vod', null, S);
  check('GET /control/vod = 200', v.code === 200, v.code);
  const cfgs = v.json.configs || [];
  check('2 configs renvoyees', cfgs.length === 2, cfgs.length);

  const twingo = cfgs.find((c) => c.vod_name === 'VOD_Twingo');
  check('VOD_Twingo ciblee 2 ecrans (pas 3)', twingo && twingo.players.length === 2,
        twingo && twingo.players);
  check('VOD_Twingo ne contient QUE son media',
        twingo && twingo.medias.length === 1 && twingo.medias[0].id_media === 99, twingo && twingo.medias);
  const dom = cfgs.find((c) => c.vod_name === 'VOD_Domicile');
  check('VOD_Domicile ciblee 3 ecrans', dom && dom.players.length === 3, dom && dom.players);
  check('has_audio remonte (media muet)', dom && dom.medias[0].has_audio === false, dom && dom.medias);

  // Son coche -> mode brut
  actions.length = 0;
  await req('POST', '/control/vod/play', { id_media: 99, mode: 'once', id_players: [4], with_sound: true }, S);
  check('avec son -> mode "once"', actions[0] && actions[0].mode === 'once', actions[0]);

  // Son decoche -> mode encode
  actions.length = 0;
  await req('POST', '/control/vod/play', { id_media: 99, mode: 'once', id_players: [4], with_sound: false }, S);
  check('sans son -> mode "once_muted"', actions[0] && actions[0].mode === 'once_muted', actions[0]);

  actions.length = 0;
  await req('POST', '/control/vod/play', { id_media: 99, mode: 'loop', id_players: [4], with_sound: false }, S);
  check('loop sans son -> "loop_muted"', actions[0] && actions[0].mode === 'loop_muted', actions[0]);

  // stop n'est jamais suffixe (pas de lecture -> pas de son)
  actions.length = 0;
  await req('POST', '/control/vod/play', { id_media: 99, mode: 'stop', id_players: [4], with_sound: false }, S);
  check('stop reste "stop"', actions[0] && actions[0].mode === 'stop', actions[0]);

  // Sans session -> refuse
  const noAuth = await req('GET', '/control/vod', null, null);
  check('sans session -> refuse', noAuth.code >= 400, noAuth.code);

  srv.stop ? srv.stop() : process.exit(fail ? 1 : 0);
  process.exit(fail ? 1 : 0);
})();
