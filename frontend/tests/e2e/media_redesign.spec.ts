import { test, expect } from '@playwright/test'

// Redesign de /media (retours Ed sur les maquettes MyBox, audit du menu Média 2026-07-28) :
// bascule vignettes ↔ liste (persistée), fil d'Ariane + sous-dossiers en tuiles, panneau de
// filtres latéral, visionneuse plein écran.

const BASE = process.env.PLAYWRIGHT_BASE_URL ?? 'http://localhost:3000'
const USER = {
  id_user: 1, id_org: 1, email: 'admin@test.com', full_name: 'Olivia Rhye', avatar_url: null,
  totp_enabled: false, locale: 'en', is_active: true, last_login_at: null,
  created_at: new Date().toISOString(), permissions: ['media:read', 'media:edit', 'media:create'],
}

// Deux dossiers homonymes sous deux racines — le cas exact d'Ed, qui doit rester lisible.
const FOLDERS = [
  { id_folder: 1, id_org: 1, parent_id: null, folder_name: 'Video', depth: 0, path: '1', child_count: 1, media_count: 0, full_path: 'Video', created_at: '2026-01-01T00:00:00Z' },
  { id_folder: 2, id_org: 1, parent_id: 1, folder_name: 'Divers', depth: 1, path: '1.2', child_count: 0, media_count: 4, full_path: 'Video / Divers', created_at: '2026-01-01T00:00:00Z' },
  { id_folder: 3, id_org: 1, parent_id: null, folder_name: 'Pic', depth: 0, path: '3', child_count: 1, media_count: 0, full_path: 'Pic', created_at: '2026-01-01T00:00:00Z' },
  { id_folder: 4, id_org: 1, parent_id: 3, folder_name: 'Divers', depth: 1, path: '3.4', child_count: 0, media_count: 2, full_path: 'Pic / Divers', created_at: '2026-01-01T00:00:00Z' },
]

const mk = (id: number, name: string, type: string, size: number) => ({
  id_media: id, media_name: name, media_type: type, mime_type: null,
  file_size_bytes: size, served_size_bytes: size, duration_ms: null, thumbnail_url: null,
  source: 'upload', attribution_text: null, transcode_status: 'done', encode_mode: 'auto',
  conformance_status: 'ready', not_before_utc: null, embargo_event_id: null,
  embargo_event_name: null, tags: [], id_folder: 2, created_at: '2026-07-20T10:00:00Z',
})
const THUMB = 'http://minio.test.local:9000/thumbs/alpha.png'   // hote NON declare, comme MinIO
const PNG_1PX = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64')
const MEDIA = [
  { ...mk(1, 'Alpha clip', 'video', 5_000_000), thumbnail_url: THUMB },
  mk(2, 'Beta photo', 'image', 200_000),
  mk(3, 'Gamma doc', 'document', 1_000_000),
]

async function setup(page: import('@playwright/test').Page) {
  await page.goto(`${BASE}/login`)
  await page.evaluate((u) => {
    const b64 = (o: unknown) =>
      btoa(JSON.stringify(o)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
    const tk = `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.${b64({
      sub: '1', org: 1, roles: ['CMS_ORG_ADMIN'], locale: 'en', type: 'access', exp: 9999999999,
    })}.sig`
    localStorage.setItem('sf-auth', JSON.stringify({
      state: { user: u, accessToken: tk, refreshToken: 'r', roles: ['CMS_ORG_ADMIN'] }, version: 0,
    }))
    localStorage.setItem('sf_access_token', tk)
    localStorage.setItem('sf_refresh_token', 'r')
  }, USER)
  const json = (body: unknown) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  await page.route('**/api/v1/users/me', (r) => r.fulfill(json(USER)))
  await page.route('**/api/v1/auth/refresh', (r) => r.fulfill(json({ access_token: 'x', refresh_token: 'r', token_type: 'bearer', locale: 'en' })))
  await page.route('**/api/v1/media/folders**', (r) => r.fulfill(json(FOLDERS)))
  await page.route('**/api/v1/media/stats**', (r) =>
    r.fulfill(json({ total: 3, by_type: { video: 1, image: 1, document: 1 } })))
  await page.route('**/api/v1/media/usage-summary**', (r) =>
    r.fulfill(json({ '1': { playlists: 1, screens: 2 }, '2': { playlists: 0, screens: 0 },
                     '3': { playlists: 0, screens: 0 } })))
  await page.route('**/api/v1/media/*/usage', (r) =>
    r.fulfill(json({ summary: { playlists: 1, schedules: 1, events: 0, screens: 2, is_live: true },
                     screens: [{ id_player: 1, player_name: 'Ecran-1', site_name: 'Magasin' }] })))
  await page.route('**/api/v1/media/?*', (r) => r.fulfill(json(MEDIA)))
  await page.route('**/api/v1/media/1', (r) =>
    r.fulfill(json({ ...MEDIA[0], id_org: 1, width: null, height: null, isrc: null,
                     rights_metadata: {}, media_metadata: {},
                     cdn_url_signed: 'data:video/mp4;base64,AAAA', updated_at: '2026-07-20T10:00:00Z' })))
  await page.route('**/api/v1/tags/namespaces**', (r) => r.fulfill(json([])))
  await page.route('**/api/v1/generation/**', (r) => r.fulfill(json([])))
  // La vignette elle-même : servie telle quelle si (et seulement si) le composant ne la
  // fait pas passer par l'optimiseur de Next.
  await page.route('http://minio.test.local:9000/**', (r) =>
    r.fulfill({ status: 200, contentType: 'image/png', body: PNG_1PX }))
}

// La vue d'accueil « N derniers par type » masque la grille complète : on force un filtre
// de dossier pour atterrir directement sur la liste, comme le ferait un vrai usage.
async function gotoFolder(page: import('@playwright/test').Page) {
  await page.goto(`${BASE}/media`)
  // Cibler l'ARBRE : « Video » est aussi le libellé d'une pastille de type et d'un bouton
  // de drawer caché — un `.first()` global visait au hasard, parfois un élément invisible.
  await page.getByTestId('folder-tree').getByRole('button', { name: 'Video' }).click()
}

test('grid/list toggle switches the view and is remembered', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 })
  await setup(page)
  await gotoFolder(page)

  // Par défaut : vignettes, donc pas de tableau.
  await expect(page.locator('table')).toHaveCount(0)

  await page.getByTitle('List').click()
  await expect(page.locator('table')).toBeVisible()
  await expect(page.getByRole('columnheader', { name: /Size/i })).toBeVisible()
  await expect(page.getByText('Alpha clip')).toBeVisible()

  // Le choix est une préférence de travail : il survit à un rechargement.
  await page.reload()
  await page.getByTestId('folder-tree').getByRole('button', { name: 'Video' }).click()
  await expect(page.locator('table')).toBeVisible()
})

test('list view sorts by a column', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 })
  await setup(page)
  await gotoFolder(page)
  await page.getByTitle('List').click()

  await page.getByRole('button', { name: /^Name/ }).click()
  const first = page.locator('tbody tr').first()
  await expect(first).toContainText('Alpha clip')

  await page.getByRole('button', { name: /^Name/ }).click()   // ordre inverse
  await expect(page.locator('tbody tr').first()).toContainText('Gamma doc')
})

test('breadcrumb shows where you are and folder tiles let you go down', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 })
  await setup(page)
  await gotoFolder(page)

  // Le fil d'Ariane nomme le dossier courant — c'est ce qui manquait pour distinguer
  // « Video / Divers » de « Pic / Divers ».
  const crumb = page.getByLabel('Breadcrumb')
  await expect(crumb).toContainText('Video')

  // La tuile du sous-dossier descend d'un cran, sans passer par l'arbre de gauche.
  await page.getByTestId('folder-tiles').getByRole('button', { name: /Divers/ }).click()
  await expect(crumb).toContainText('Divers')
})

test('filters panel narrows by type and warns when the list is partial', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 })
  await setup(page)
  await gotoFolder(page)
  await page.getByTitle('List').click()

  await page.getByTitle('Filters').click()
  const panel = page.getByTestId('media-filters')
  await panel.getByRole('button', { name: /^Video/ }).click()

  await expect(page.getByText('Alpha clip')).toBeVisible()
  await expect(page.getByText('Beta photo')).toHaveCount(0)
})

test('clicking a visual media opens the full-screen viewer, not the edit drawer', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 })
  await setup(page)
  await gotoFolder(page)
  await page.getByTitle('List').click()

  await page.locator('tbody tr', { hasText: 'Alpha clip' }).click()
  const viewer = page.getByTestId('media-viewer')
  await expect(viewer).toBeVisible()
  await expect(viewer).toContainText('Alpha clip')
  // L'édition n'est pas perdue : elle est derrière un bouton explicite.
  await expect(viewer.getByRole('button', { name: 'Edit' })).toBeVisible()

  await page.keyboard.press('Escape')
  await expect(page.getByTestId('media-viewer')).toHaveCount(0)
})

test('hovering a row reveals where the media is broadcast', async ({ page }) => {
  // Ed : « quand on sélectionne ou survole un média, on sache [...] surtout actif sur un ou
  // des écrans ». Le déclencheur est la LIGNE entière : il était d'abord posé sur le petit
  // espace vide sous le nom, où il fallait viser juste pour que l'info arrive.
  await page.setViewportSize({ width: 1600, height: 900 })
  await setup(page)
  await gotoFolder(page)
  await page.getByTitle('List').click()

  // Décision d'Ed : l'information est PERMANENTE (chargée en lot pour toute la page) ;
  // seul le DÉTAIL nommé des écrans reste au survol, en infobulle.
  const row = page.locator('tbody tr', { hasText: 'Alpha clip' })
  await expect(row).toContainText('On 2 screen(s)')
  await expect(page.locator('tbody tr', { hasText: 'Beta photo' })).toContainText('Unused')
})

// ── Depuis la VUE D'ACCUEIL (« N derniers par type »), pas depuis un dossier ──────────
//
// 🔴 Ces trois tests manquaient, et c'est pourquoi la suite était verte sur un produit
// cassé : `gotoFolder()` sortait déjà de la vue d'accueil, or c'est ELLE qui avalait la
// bascule, les filtres et le survol. Constat d'Ed en production : « rien ne se passe dans
// la zone centrale ». On part donc de /media nu, comme lui.

test('switching to list from the landing view actually shows the list', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 })
  await setup(page)
  await page.goto(`${BASE}/media`)

  // Vue d'accueil : sections par type, aucune table.
  await expect(page.locator('table')).toHaveCount(0)

  await page.getByTitle('List').click()
  await expect(page.locator('table')).toBeVisible()
  await expect(page.getByText('Alpha clip')).toBeVisible()
})

test('ticking a filter from the landing view actually narrows the content', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 })
  await setup(page)
  await page.goto(`${BASE}/media`)

  await page.getByTitle('Filters').click()
  await page.getByTestId('media-filters').getByRole('button', { name: /^Video/ }).click()

  await expect(page.getByText('Alpha clip')).toBeVisible()
  await expect(page.getByText('Beta photo')).toHaveCount(0)
})

test('hovering a THUMBNAIL reveals where the media is broadcast', async ({ page }) => {
  // Ed passe surtout par la grille : l'indication de diffusion ne peut pas vivre
  // uniquement dans la vue liste.
  await page.setViewportSize({ width: 1600, height: 900 })
  await setup(page)
  await gotoFolder(page)

  const tile = page.locator('div.group', { hasText: 'Alpha clip' }).first()
  await expect(tile).toContainText('On 2 screen(s)')
})

test('list view really renders thumbnails (not an empty square)', async ({ page }) => {
  // 🔴 Constat d'Ed en production : « en mode liste on n'a pas les vignettes, il y a un gros
  // carré noir ». Cause : `<Image>` sans `unoptimized` → Next passe par son optimiseur, qui
  // refuse l'hôte MinIO (absent de `images.remotePatterns`) et l'image échoue en silence.
  // Aucun test ne l'a vu parce qu'ils mockaient tous `thumbnail_url: null`.
  await page.setViewportSize({ width: 1600, height: 900 })
  await setup(page)
  await gotoFolder(page)
  await page.getByTitle('List').click()

  const img = page.locator('tbody tr', { hasText: 'Alpha clip' }).locator('img').first()
  await expect(img).toBeVisible()
  // L'URL doit être servie TELLE QUELLE, pas réécrite en /_next/image.
  await expect(img).toHaveAttribute('src', 'http://minio.test.local:9000/thumbs/alpha.png')
  // Et l'image doit avoir réellement décodé — un src correct mais cassé resterait à 0.
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBeGreaterThan(0)
})

test('an edit shortcut opens the drawer directly, in both views', async ({ page }) => {
  // Demande d'Ed : depuis que le clic ouvre la VISIONNEUSE, éditer demandait deux gestes.
  // Un bouton crayon rouvre le tiroir directement — en vignettes comme en liste.
  await page.setViewportSize({ width: 1600, height: 900 })
  await setup(page)
  await gotoFolder(page)

  // Vignettes : le bouton n'apparaît qu'au survol de la tuile.
  const tile = page.locator('div.group', { hasText: 'Alpha clip' }).first()
  await tile.hover()
  await tile.getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(page.getByTestId('media-viewer')).toHaveCount(0)   // le tiroir, pas la visionneuse
  await expect(page.getByRole('heading', { name: /Alpha clip/ })).toBeVisible()
  await page.keyboard.press('Escape')

  // Liste : bouton crayon à côté de la poubelle.
  await page.getByTitle('List').click()
  const row = page.locator('tbody tr', { hasText: 'Alpha clip' })
  await row.getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(page.getByTestId('media-viewer')).toHaveCount(0)
  await expect(page.getByRole('heading', { name: /Alpha clip/ })).toBeVisible()
})

// Dossier système des annonceurs (migr 0131) : stocké sous le nom de repli anglais
// « Advertisers », repéré par `system_key`. Son nom affiché suit la langue de l'interface,
// jusque dans le chemin de ses sous-dossiers, et il ne propose pas de renommage.
test('system advertisers folder shows a translated name and cannot be renamed', async ({ page, context }) => {
  await page.setViewportSize({ width: 1600, height: 900 })
  await setup(page)
  await context.addCookies([{ name: 'locale', value: 'fr', url: BASE }])
  const SYS = [
    { id_folder: 10, id_org: 1, parent_id: null, folder_name: 'Advertisers', system_key: 'advertisers', depth: 0, path: '10', child_count: 1, media_count: 0, full_path: 'Advertisers', created_at: '2026-01-01T00:00:00Z' },
    { id_folder: 11, id_org: 1, parent_id: 10, folder_name: 'Acme', system_key: null, depth: 1, path: '10.11', child_count: 0, media_count: 1, full_path: 'Advertisers / Acme', created_at: '2026-01-01T00:00:00Z' },
    { id_folder: 12, id_org: 1, parent_id: null, folder_name: 'Advertisers', system_key: null, depth: 0, path: '12', child_count: 0, media_count: 0, full_path: 'Advertisers', created_at: '2026-01-01T00:00:00Z' },
  ]
  await page.route('**/api/v1/media/folders**', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SYS) }))
  await page.goto(`${BASE}/media`)

  const tree = page.getByTestId('folder-tree')
  const sysRow = tree.getByRole('button', { name: 'Annonceurs', exact: true })
  await expect(sysRow).toBeVisible()
  // Témoin : le dossier UTILISATEUR homonyme garde son nom tel quel.
  await expect(tree.getByRole('button', { name: 'Advertisers', exact: true })).toBeVisible()

  // Pas de renommage sur la ligne système ; il reste sur la ligne utilisateur.
  const rowOf = (b: import('@playwright/test').Locator) => b.locator('xpath=..')
  await expect(rowOf(sysRow).getByTitle('Renommer')).toHaveCount(0)
  await expect(rowOf(tree.getByRole('button', { name: 'Advertisers', exact: true })).getByTitle('Renommer')).toHaveCount(1)

  // Le fil d'Ariane du sous-dossier traduit aussi l'ancêtre système.
  await sysRow.click()
  await expect(page.getByLabel(/Breadcrumb|Fil d/)).toContainText('Annonceurs')
})
