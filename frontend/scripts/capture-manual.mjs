/**
 * Captures d'écran du manuel utilisateur (`/help`) — script relançable.
 *
 * POURQUOI UN SCRIPT ET PAS DES CAPTURES À LA MAIN. Les 34 images du manuel dataient de
 * juillet 2026 et l'interface a changé depuis (refonte « Mission Control », vue liste du
 * parc, page Serveur…). Les refaire à la main, c'est 34 manipulations à rejouer
 * intégralement à la prochaine évolution — donc, en pratique, des captures qui redeviennent
 * fausses. Ici on relance une commande.
 *
 * USAGE
 *   node scripts/capture-manual.mjs            # tout
 *   node scripts/capture-manual.mjs dashboard players   # une sélection
 *
 * VARIABLES
 *   SF_BASE   URL du CMS à capturer            (défaut : http://localhost:8080)
 *   SF_TOKEN  jeton d'accès org-admin          (obligatoire)
 *   SF_OUT    dossier de sortie                (défaut : public/help/img/<langue du compte>)
 *   SF_API    URL de l'API si elle n'est pas servie par SF_BASE (banc de dev : interface sur
 *             :3010, API sur :8010) — défaut : SF_BASE
 *
 * LANGUE. Les captures sont prises dans la langue du COMPTE (champ `locale`) et rangées dans
 * img/<langue>/ : img/en/ sert le README et les manuels en/de/es/pt, img/fr/ le manuel français.
 * Les données affichées viennent de `backend/scripts/seed_demo_content.py --lang <langue>`.
 *
 * ⚠️ Le jeton n'est JAMAIS écrit dans ce fichier ni journalisé : il est forgé côté serveur
 * et passé par l'environnement. Il expire en quelques minutes — c'est voulu.
 *
 * ⚠️ 1920 de large, comme les captures d'origine : le manuel les sert telles quelles, et un
 * changement de largeur se verrait d'une image à l'autre.
 */

import { chromium } from '@playwright/test'
import { mkdir, readFile, rm } from 'node:fs/promises'
import path from 'node:path'

// 🔴 CORRIGÉ LE 2026-08-10. Ce script écrivait des `.png` alors que le manuel sert des `.webp` :
// le rejouer ne mettait donc RIEN à jour — la promesse « les captures se rejouent par script »
// ne tenait pas, et personne ne s'en apercevait puisque les anciennes images restaient servies.
// On convertit désormais au format effectivement servi, aux réglages d'origine (1920 px, q86 —
// 16 Mo de PNG étaient devenus 1,3 Mo de WebP), et le PNG intermédiaire est supprimé.
//
// ⚠️ `sharp` n'est pas une dépendance déclarée : il arrive avec Next. Si une montée de version
// le retirait, ce script doit ÉCHOUER FRANCHEMENT plutôt que laisser des PNG que rien ne sert.
let sharp
try {
  ({ default: sharp } = await import('sharp'))
} catch {
  console.error('sharp introuvable — impossible de produire le WebP servi par le manuel.\n'
    + 'Installer : npm i -D sharp (dans frontend/).')
  process.exit(1)
}

const BASE = process.env.SF_BASE ?? 'http://localhost:8080'
const TOKEN = process.env.SF_TOKEN
const API = process.env.SF_API ?? BASE

if (!TOKEN) {
  console.error('SF_TOKEN manquant — forge un jeton org-admin et passe-le par l’environnement.')
  process.exit(1)
}

/**
 * Chaque entrée produit UNE image.
 *   name  : nom du fichier (sans extension) — doit correspondre au manuel
 *   url   : chemin à ouvrir
 *   ready : sélecteur attendu avant de déclencher (jamais un délai fixe : une attente
 *           arbitraire capture soit trop tôt, soit pour rien)
 *   act   : ouverture d'un tiroir ou d'un onglet, quand la capture le demande
 */
// Helper : ouvre le premier element d'une liste (carte ou ligne de tableau) pour faire
// apparaitre son tiroir. On vise le CONTENU (un lien/bouton portant un nom) plutot qu'une
// position : un selecteur positionnel casse des qu'une colonne bouge.
const ouvrirPremier = (sel) => async (page) => {
  await page.locator(sel).first().click({ timeout: 10000 })
  await page.waitForTimeout(1500)
}
const cliquerTexte = (nom) => async (page) => {
  await page.getByRole('button', { name: nom }).first().click({ timeout: 10000 })
  await page.waitForTimeout(1500)
}

// Ouvre le tiroir en cliquant le NOM du premier element, lu au serveur. On vise le
// contenu et jamais une position : une carte qui bouge, une colonne ajoutee, et un
// selecteur positionnel capture autre chose sans rien dire.
// ⛔ On ne clique JAMAIS une icone de suppression : elle ouvre une boite de dialogue
// native qui gele la page et ferait echouer tout le reste de la serie.
// ⚠️ Le slash final n'est PAS cosmetique : selon la route, il declenche un 307 dont la
// reponse est vide (`/vod/`) ou fait matcher une AUTRE route (`/campaigns/advertisers` est
// lu comme `/campaigns/{id}` et repond 422). Les chemins ci-dessous sont ceux qui rendent
// 200, verifies un par un.
const ouvrirParNom = (chemin, cle, filtre = () => true) => async (page) => {
  const r = await fetch(`${API}/api/v1${chemin}`, {
    headers: { Authorization: `Bearer ${TOKEN}` }, redirect: 'manual',
  })
  const j = await r.json()
  const liste = (Array.isArray(j) ? j : (j.items ?? [])).filter(filtre)
  if (!liste.length) throw new Error(`aucune donnee sur ${chemin}`)
  const nom = liste[0][cle]
  await page.getByText(nom, { exact: false }).first().click({ timeout: 10000 })
  await page.waitForTimeout(1500)
}
// Bouton identifie par son libelle d'accessibilite (title/aria-label), releve sur la page.
const cliquerTitre = (titre) => async (page) => {
  await page.getByTitle(titre, { exact: true }).first().click({ timeout: 10000 })
  await page.waitForTimeout(1500)
}

const SHOTS = [
  // ── Vue d'ensemble ────────────────────────────────────────────────────────────────
  { name: 'dashboard', url: '/dashboard' },

  // ── Parc ──────────────────────────────────────────────────────────────────────────
  { name: 'players', url: '/players' },
  // Un player EN LIGNE : la fiche d'un écran hors ligne n'a ni capture ni contenu en cours.
  { name: 'player-drawer', url: '/players',
    act: ouvrirParNom('/players/', 'player_name', (p) => p.status === 'online') },
  { name: 'players-provisioning', url: '/settings/players/provisioning' },
  { name: 'sites', url: '/sites' },
  { name: 'site-groups', url: '/sites', act: cliquerTexte(/groupe|group/i) },

  // ── Contenu ───────────────────────────────────────────────────────────────────────
  { name: 'media', url: '/media' },
  { name: 'media-drawer', url: '/media', act: cliquerTexte(/^(edit|modifier)$/i) },
  { name: 'media-upload', url: '/media', act: cliquerTexte(/upload|importer/i) },
  { name: 'media-web-page', url: '/url-display' },
  { name: 'media-generate-ai', url: '/media', act: cliquerTexte(/g[ée]n[ée]rer \(ia\)|generate \(ai\)/i) },
  // Générateur promo : le parcours en étapes, puis le mode série. Le module existait depuis le
  // 02/07 sans aucune capture — sa section de manuel était la seule sans image.
  // Le formulaire rempli (slogan + prix) : vide, l'aperçu n'est qu'une toile blanche — c'est
  // ce que montraient les captures du 09/11, prises pour un aperçu cassé.
  {
    name: 'promo',
    url: '/promo',
    act: async (page) => {
      const step = page.getByTestId('step-content')
      await step.locator('button').first().click({ timeout: 10000 })
      await step.locator('input').first().fill(ME.locale === 'fr' ? 'Croissants du matin' : 'Morning croissants')
      await step.getByPlaceholder('4,99').fill(ME.locale === 'fr' ? '3,90' : '3.90')
      await step.getByPlaceholder('6,99').fill(ME.locale === 'fr' ? '4,90' : '4.90')
      // La conformité est revalidée en différé : sans attendre, le bandeau « prix requis »
      // de l'état vide reste sur la capture.
      await page.getByTestId('promo-not-conform').waitFor({ state: 'detached', timeout: 10000 })
    },
  },
  {
    name: 'promo-serie',
    url: '/promo',
    // Une ligne VOLONTAIREMENT sans prix : c'est l'avertissement « écartée » qu'on veut montrer,
    // pas un tableau parfait qui ne dirait rien du comportement.
    act: async (page) => {
      await page.getByTestId('promo-mode-serie').click({ timeout: 10000 })
      await page.getByTestId('serie-paste-toggle').click()
      await page.getByTestId('serie-paste-area').fill((ME.locale === 'fr' ? [
        'Produit;Prix;Prix barré',
        'Tarte aux pommes;12,90;15,90',
        'Pain au levain 1 kg;5,40;6,20',
        'Coffret café moulu;18,00;22,00',
        'Confiture artisanale;;7,50',
        'Panier petit-déjeuner;24,00;29,00',
      ] : [
        'Product;Price;Was',
        'Apple tart;12.90;15.90',
        'Sourdough loaf 1 kg;5.40;6.20',
        'Ground coffee box;18.00;22.00',
        'Artisan jam;;7.50',
        'Breakfast basket;24.00;29.00',
      ]).join('\n'))
      await page.getByTestId('serie-paste-apply').click()
      await page.waitForTimeout(1500)
    },
  },
  { name: 'templates', url: '/templates' },
  { name: 'templates-editor', url: '__TEMPLATE__' },
  { name: 'playlists', url: '/playlists' },
  { name: 'playlist-editor', url: '__PLAYLIST__' },
  { name: 'playlist-add-media', url: '__PLAYLIST__', act: cliquerTexte(/add media|ajouter un m/i) },
  { name: 'playlist-add-playlist', url: '__PLAYLIST__', act: cliquerTexte(/ajouter une playlist|add a playlist/i) },
  { name: 'playlist-simulation', url: '__PLAYLIST__', act: cliquerTexte(/simulat/i) },
  { name: 'audio-baskets', url: '/audio-baskets' },
  { name: 'audio-baskets-edit', url: '/audio-baskets', act: cliquerTexte(/g[ée]rer|manage/i) },

  // ── Diffusion ─────────────────────────────────────────────────────────────────────
  { name: 'schedules', url: '/schedules' },
  { name: 'schedule-new', url: '/schedules', act: cliquerTexte(/new schedule|nouveau programme|new/i) },
  { name: 'schedule-simulation', url: '/schedules', act: cliquerTexte(/simuler une journ|simulate a day/i) },
  { name: 'governance', url: '/governance' },
  { name: 'events', url: '/events' },
  { name: 'event-edit', url: '/events', act: cliquerTexte(/^(edit|modifier)$/i) },
  { name: 'vod', url: '/vod' },
  { name: 'vod-edit', url: '/vod', act: ouvrirParNom('/vod', 'vod_name') },

  // ── Retail media ──────────────────────────────────────────────────────────────────
  { name: 'advertisers', url: '/campaigns/advertisers' },
  { name: 'advertiser-edit', url: '/campaigns/advertisers', act: ouvrirParNom('/advertisers/', 'advertiser_name') },
  { name: 'campaigns', url: '/campaigns' },
  { name: 'campaign-edit', url: '/campaigns', act: ouvrirParNom('/campaigns/', 'campaign_name') },
  { name: 'approvals', url: '/approvals' },
  { name: 'approval-review', url: '/approvals', act: ouvrirPremier('main [class*="cursor-pointer"], main tr') },

  // ── Exploitation (NOUVEAU — documente le 05/08) ────────────────────────────────────
  { name: 'server', url: '/settings/server' },
]

// L'application ne se contente pas du jeton : son magasin `sf-auth` doit porter
// l'utilisateur, sinon le garde de route renvoie vers /login. On recupere donc le compte
// avec le jeton AVANT d'ouvrir le navigateur — et si cet appel echoue, on s'arrete ici
// plutot que de capturer 16 fois la page de connexion.
const meRes = await fetch(`${API}/api/v1/users/me`, {
  headers: { Authorization: `Bearer ${TOKEN}` },
})
if (!meRes.ok) {
  console.error(`Jeton refuse par ${BASE} (HTTP ${meRes.status}) — forge-en un neuf, ils expirent vite.`)
  process.exit(1)
}
const ME = await meRes.json()
const OUT = process.env.SF_OUT
  ?? path.join(process.cwd(), 'public', 'help', 'img', ME.locale === 'fr' ? 'fr' : 'en')
console.log(`Session : ${ME.email} (org ${ME.id_org}), roles ${(ME.roles ?? []).join(', ')}`)

await mkdir(OUT, { recursive: true })

const browser = await chromium.launch({ channel: 'chrome' })
const LOCALE = ME.locale || 'en'
const context = await browser.newContext({ viewport: { width: 1920, height: 1080 }, locale: LOCALE })
// La langue de l'interface vient du cookie `locale` (sinon de la langue du NAVIGATEUR) : sans lui,
// un Chrome français capture une interface française quel que soit le compte.
await context.addCookies([{ name: 'locale', value: LOCALE, url: BASE }])

// Session : on dépose le jeton AVANT tout chargement d'application, sur la bonne origine.
// Clés des bulles d'aide, lues DANS le composant plutôt que recopiées : une liste figée
// ici raterait toute bulle ajoutée plus tard, et on ne s'en apercevrait qu'en regardant
// une capture ratée.
const TIP_KEYS = (await readFile('src/components/layout/DemoTips.tsx', 'utf8'))
  .split(/\r?\n/)
  .map((l) => l.match(/^\s*\['[^']+',\s*'([^']+)'\]/))   // 2e element = la CLE, pas la route
  .filter(Boolean)
  .map((m) => m[1])
console.log(`Bulles d'aide neutralisées : ${TIP_KEYS.length}`)

await context.addInitScript(([tk, me, TIP_KEYS]) => {
  localStorage.setItem('sf_access_token', tk)
  localStorage.setItem('sf_refresh_token', tk)
  localStorage.setItem('sf-auth', JSON.stringify({
    state: { user: me, accessToken: tk, refreshToken: tk, roles: me.roles ?? [] },
    version: 0,
  }))
  // Bulles d'aide : elles se posent en bas a droite et RECOUVRENT le contenu (c'est le
  // defaut deja corrige le 29/07 sur les tiroirs). Sur une capture de manuel elles n'ont
  // rien a faire — on les marque comme deja lues plutot que de les fermer page par page.
  for (const k of TIP_KEYS) localStorage.setItem(`sf-tip:${k}`, '1')
}, [TOKEN, ME, TIP_KEYS])

// Les ecrans a identifiant (editeur de playlist, de template) : on demande le premier id
// au serveur. Un id code en dur pourrit des que la donnee change, et la capture montrerait
// alors un ecran d'erreur — parfaitement net.
const premierId = async (chemin, cle) => {
  // ⚠️ `redirect: 'manual'` : un slash final de trop renvoie un 307 vers une URL qui PERD
  // le port (`http://localhost/...`), et la requete part alors sur le vhost TLS voisin
  // — l'erreur qu'on recoit parle alors de certificat auto-signe, ce qui envoie chercher a
  // 100 lieues du vrai probleme. On refuse de suivre, et on le DIT.
  const r = await fetch(`${API}/api/v1${chemin}`, {
    headers: { Authorization: `Bearer ${TOKEN}` },
    redirect: 'manual',
  })
  if (r.status >= 300 && r.status < 400) {
    console.warn(`  ${chemin} redirige (${r.status}) — slash final de trop ?`)
    return null
  }
  if (!r.ok) return null
  const j = await r.json()
  const l = Array.isArray(j) ? j : (j.items ?? [])
  return l.length ? l[0][cle] : null
}
const idPlaylist = await premierId('/playlists/', 'id_playlist')
const idTemplate = await premierId('/templates', 'id_template')
console.log(`Playlist ${idPlaylist ?? '(aucune)'} · template ${idTemplate ?? '(aucun)'}`)
for (const s of SHOTS) {
  if (s.url === '__PLAYLIST__') s.url = idPlaylist ? `/playlists/${idPlaylist}` : null
  if (s.url === '__TEMPLATE__') s.url = idTemplate ? `/templates/${idTemplate}/edit` : null
}

const page = await context.newPage()
const only = process.argv.slice(2)
const resultats = []

for (const shot of SHOTS) {
  if (only.length && !only.includes(shot.name)) continue
  if (!shot.url) { resultats.push([shot.name, 'ECHEC pas de donnee', '-']); continue }
  try {
    await page.goto(`${BASE}${shot.url}`, { waitUntil: 'networkidle', timeout: 30000 })
    // Le contenu arrive par React Query : on attend qu'une entête de page existe plutôt
    // qu'un délai fixe.
    await page.waitForSelector(shot.ready ?? 'main, [role="main"], h1, h2', { timeout: 15000 })
    await page.waitForTimeout(1200) // laisse les cartes/graphes finir leur animation d'entrée
    if (shot.act) await shot.act(page)
    // Une capture de la page de connexion est un ECHEC, pas une reussite. Sans cette garde,
    // une session perdue produit 16 images parfaitement nettes de l'ecran de login — et le
    // script annonce « tout va bien » (vecu au premier essai).
    if (page.url().includes('/login')) {
      resultats.push([shot.name, 'ECHEC session perdue', page.url()])
      continue
    }
    const png = path.join(OUT, `${shot.name}.png`)
    await page.screenshot({ path: png })
    // Le manuel sert du WebP : on produit CE format, pas un PNG que personne ne lit.
    await sharp(png).resize({ width: 1920, withoutEnlargement: true })
      .webp({ quality: 86 }).toFile(path.join(OUT, `${shot.name}.webp`))
    await rm(png)
    resultats.push([shot.name, 'OK', page.url()])
  } catch (err) {
    resultats.push([shot.name, `ÉCHEC ${String(err).slice(0, 80)}`, page.url()])
  }
}

console.log('\n--- résultat ---')
for (const [n, s, u] of resultats) console.log(`  ${s.padEnd(12)} ${n.padEnd(24)} ${u}`)
const ko = resultats.filter(([, s]) => s !== 'OK').length
console.log(`\n${resultats.length - ko} capture(s) réussie(s), ${ko} en échec.`)
// Sous Windows, la fermeture de Chrome peut ne jamais rendre la main (verrou sur ses fichiers
// temporaires) : le script restait bloqué APRÈS avoir tout produit. Le bilan est déjà affiché ;
// on laisse 10 s à la fermeture, puis on sort quoi qu'il arrive.
await Promise.race([browser.close(), new Promise((r) => setTimeout(r, 10000))])
process.exit(ko ? 1 : 0)
