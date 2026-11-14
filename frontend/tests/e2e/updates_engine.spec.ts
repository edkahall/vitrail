import { test, expect } from '@playwright/test'

// Moteur Electron des players (issue vitrail#5) — tiroir « Moteur Electron » de /settings/updates.
//
// Ce que ces tests figent :
// - le bouton n'existe que pour l'EXPLOITANT du serveur (le serveur refuse les autres, l'écran ne
//   doit pas leur proposer un geste voué au 403) ;
// - « Récupérer » envoie la version et les plateformes cochées, puis suit chaque tâche jusqu'au bout ;
// - un refus du serveur s'affiche (jamais un bouton qui ne fait rien) ;
// - le déploiement d'un moteur passe par le circuit commun (cible des options de la page) ;
// - le parc montre la version d'Electron de chaque écran.

const BASE = process.env.PLAYWRIGHT_BASE_URL ?? 'http://localhost:3000'

const USER = {
  id_user: 1, id_org: 1, email: 'admin@test.com', full_name: 'Olivia Rhye',
  avatar_url: null, totp_enabled: false, locale: 'en', is_active: true,
  last_login_at: null, created_at: new Date().toISOString(),
}

const PARC = {
  players: [
    { id_player: 4, player_name: 'RPi-banc', player_os: 'linux-arm64', status: 'online',
      current_version: '1.6.22', latest_version: '1.6.22', up_to_date: true, update_state: 'up_to_date',
      renderer_degraded: null, electron_version: '42.11.18', site: 'Site-01', groups: [] },
    { id_player: 9, player_name: 'BS-hall', player_os: 'brightsign', status: 'online',
      current_version: '2.0.40', latest_version: '2.0.40', up_to_date: true, update_state: 'up_to_date',
      renderer_degraded: null, electron_version: null, site: 'Site-01', groups: [] },
  ],
  total: 2, outdated: 0, unknown: 0,
}

const eng = (id: number, version: string, family: string) => ({ id_version: id, track: 'engine', version,
  channel: 'beta', download_url: `/api/v1/updates/engines/${family}/${version}`, model_family: family,
  release_notes: null, min_firmware_version: null })
const ENGINE = [eng(77, '42.11.17', 'linux-arm64'), eng(78, '42.11.18', 'linux-arm64'), eng(79, '42.11.16', 'windows')]

type Page = import('@playwright/test').Page
const json = (body: unknown, status = 200) =>
  ({ status, contentType: 'application/json', body: JSON.stringify(body) })

async function setup(page: Page, { operator }: { operator: boolean }) {
  await page.goto(`${BASE}/login`)
  await page.evaluate(({ u }) => {
    const b64url = (o: unknown) =>
      btoa(JSON.stringify(o)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
    const tk = [
      b64url({ alg: 'HS256', typ: 'JWT' }),
      b64url({ sub: '1', org: 1, roles: ['CMS_SUPER_ADMIN'], locale: 'en', type: 'access', exp: 9999999999 }),
      'signature',
    ].join('.')
    localStorage.setItem('sf-auth', JSON.stringify({
      state: { user: u, accessToken: tk, refreshToken: 'mock_refresh', roles: ['CMS_SUPER_ADMIN'] }, version: 0,
    }))
    localStorage.setItem('sf_access_token', tk)
    localStorage.setItem('sf_refresh_token', 'mock_refresh')
  }, { u: USER })
  const calls: { fetch: unknown[]; deploy: unknown[]; jobs: number } = { fetch: [], deploy: [], jobs: 0 }
  // ⚠️ Playwright résout les routes de la DERNIÈRE à la première : le glob générique en premier.
  await page.route('**/api/v1/**', (r) => r.fulfill(json([])))
  await page.route('**/api/v1/users/me', (r) => r.fulfill(json(USER)))
  await page.route('**/api/v1/auth/refresh', (r) =>
    r.fulfill(json({ access_token: 'x', refresh_token: 'mock_refresh', token_type: 'bearer', locale: 'en' })))
  await page.route('**/api/v1/meta/operator**', (r) => r.fulfill(json({ is_operator: operator })))
  await page.route('**/api/v1/updates/parc**', (r) => r.fulfill(json(PARC)))
  await page.route((u) => u.pathname.endsWith('/updates/versions/') || u.pathname.endsWith('/updates/versions'),
    (r) => r.fulfill(json(new URL(r.request().url()).searchParams.get('track') === 'engine' ? ENGINE : [])))
  await page.route('**/api/v1/updates/engines/fetch', async (r) => {
    const body = r.request().postDataJSON()
    calls.fetch.push(body)
    if (body.version === '1.2') return r.fulfill(json({ detail: 'Invalid Electron version (expected MAJOR.MINOR.PATCH).' }, 400))
    return r.fulfill(json(body.families.map((f: string, i: number) =>
      ({ job_id: `${'a'.repeat(31)}${i}`, family: f, version: body.version })), 202))
  })
  await page.route('**/api/v1/updates/engines/jobs/**', (r) => {
    calls.jobs++
    return r.fulfill(json({ state: 'done', created: true }))
  })
  await page.route('**/api/v1/updates/deployments', (r) => {
    if (r.request().method() === 'POST') {
      const b = r.request().postDataJSON()
      calls.deploy.push(b)
      // Un player = 1 cible ; « toute l'org » simule un déploiement qui ne vise personne.
      return r.fulfill(json({ id_deployment: 41 + calls.deploy.length, total_targets: b.targets[0].target_type === 'player' ? 1 : 0 }, 201))
    }
    return r.fulfill(json([]))
  })
  await page.goto(`${BASE}/settings/updates`)
  await expect(page.getByRole('cell', { name: 'RPi-banc' })).toBeVisible()
  return calls
}

test("un admin d'org deploie sur ses ecrans : beta sur un seul player, catalogue a l'exploitant", async ({ page }) => {
  const calls = await setup(page, { operator: false })
  await page.getByRole('button', { name: 'Electron engine' }).click()
  // Catalogue = exploitant : ni récupération, ni promotion, ni suppression.
  await expect(page.getByTestId('engine-not-operator')).toBeVisible()
  await expect(page.getByTestId('engine-version')).toHaveCount(0)
  const row = page.getByTestId('engine-versions').locator('li', { hasText: '42.11.17' })
  await expect(row.getByRole('button', { name: /Promote/ })).toHaveCount(0)
  await expect(row.getByTitle('Delete')).toHaveCount(0)
  // La cible se choisit DANS le tiroir (retour Ed 14/11). Bêta + toute l'organisation : bloqué.
  await page.getByTestId('engine-target-type').selectOption('org')
  await expect(row.getByRole('button', { name: 'Deploy' })).toBeDisabled()
  // La raison est écrite SUR la ligne (retour Ed 14/11 : tout semblait grisé sans raison).
  await expect(row.getByTestId('engine-row-reason')).toContainText('try it on a single player')
  // Un seul player : autorisé. La liste ne propose que les players Electron.
  await page.getByTestId('engine-target-type').selectOption('player')
  const ids = page.getByTestId('engine-target-id')
  await expect(ids.locator('option', { hasText: 'RPi-banc' })).toHaveCount(1)
  await expect(ids.locator('option', { hasText: 'BS-hall' })).toHaveCount(0)
  await expect(row.getByRole('button', { name: 'Deploy' })).toBeDisabled()   // aucun player choisi
  await ids.selectOption({ label: 'RPi-banc · linux-arm64 · Electron 42.11.18' })
  // Player choisi : seulement les versions de SA plateforme (retour Ed 14/11).
  const list = page.getByTestId('engine-versions')
  await expect(list.locator('li', { hasText: 'windows' })).toHaveCount(0)
  await expect(list.locator('li')).toHaveCount(2)
  // La version déjà installée sur ce player est grisée, avec la raison.
  const installed = list.locator('li', { hasText: '42.11.18' })
  await expect(installed.getByRole('button', { name: 'Deploy' })).toBeDisabled()
  await expect(installed.getByTestId('engine-row-reason')).toContainText('Already installed')
  await row.getByRole('button', { name: 'Deploy' }).click()
  await expect.poll(() => calls.deploy.length).toBe(1)
  expect(calls.deploy[0]).toMatchObject({ id_version: 77, targets: [{ target_type: 'player', target_id: 4 }] })
  // Retour visible (retour Ed 14/11 : rien ne s'affichait, 4 clics = 4 déploiements).
  await expect(page.getByTestId('deploy-msg').first()).toContainText('Deployment #42 started on 1 player(s)')
})

test('le parc montre la version Electron de chaque ecran', async ({ page }) => {
  await setup(page, { operator: true })
  await expect(page.locator('th', { hasText: 'Electron' })).toBeVisible()
  await expect(page.locator('tr', { hasText: 'RPi-banc' })).toContainText('42.11.18')
  await expect(page.locator('tr', { hasText: 'BS-hall' })).toContainText('—')
})

test('recuperer un moteur : version + plateformes cochees, tache suivie jusqu au bout', async ({ page }) => {
  const calls = await setup(page, { operator: true })
  await page.getByRole('button', { name: 'Electron engine' }).click()
  await page.getByTestId('engine-version').fill('42.11.17')
  await page.getByRole('checkbox', { name: 'Windows', exact: true }).uncheck()
  await page.getByRole('button', { name: 'Get', exact: true }).click()
  await expect.poll(() => calls.fetch.length).toBe(1)
  expect(calls.fetch[0]).toEqual({ version: '42.11.17', families: ['linux-arm64', 'linux-x64'] })
  const jobs = page.getByTestId('engine-jobs')
  await expect(jobs).toContainText('42.11.17 · linux-arm64')
  await expect(jobs.getByText('Added to the catalogue (beta)')).toHaveCount(2)
  expect(calls.jobs).toBeGreaterThanOrEqual(2)
})

test('un refus du serveur est affiche', async ({ page }) => {
  await setup(page, { operator: true })
  await page.getByRole('button', { name: 'Electron engine' }).click()
  const get = page.getByRole('button', { name: 'Get', exact: true })
  await page.getByTestId('engine-version').fill('1.2')
  await expect(get).toBeDisabled()          // format refusé dès la saisie
  await page.getByTestId('engine-version').fill('42.11.17')
  await page.route('**/api/v1/updates/engines/fetch', (r) =>
    r.fulfill(json({ detail: 'Restricted to the server operator' }, 403)))
  await get.click()
  await expect(page.getByText('Restricted to the server operator')).toBeVisible()
})

test('deployer un moteur : cible et fenetres choisies dans le tiroir', async ({ page }) => {
  const calls = await setup(page, { operator: true })
  await page.getByRole('button', { name: 'Electron engine' }).click()
  const row = page.getByTestId('engine-versions').locator('li', { hasText: '42.11.17' })
  await expect(row).toContainText('beta · linux-arm64')
  // Le seul écran linux-arm64 est en 42.11.18 : aucun n'est encore sur la 42.11.17.
  await expect(row).toContainText('0 player(s)')
  const form = page.getByTestId('engine-deploy-form')
  await page.getByTestId('engine-target-type').selectOption('org')
  await form.getByPlaceholder('HH:MM-HH:MM').first().fill('02:00-05:00')
  await row.getByRole('button', { name: 'Deploy' }).click()
  await expect.poll(() => calls.deploy.length).toBe(1)
  expect(calls.deploy[0]).toMatchObject({ id_version: 77, strategy: 'scheduled',
    strategy_params: { download_window: '02:00-05:00' }, targets: [{ target_type: 'org' }] })
  // Un déploiement qui ne vise personne n'est plus muet.
  await expect(page.getByTestId('deploy-msg').first()).toContainText('targets no player')
})

test("le super-admin (exploitant) trouve Mises a jour dans sa barre laterale", async ({ page }) => {
  // 14/11 : le super-admin ne voit que les réglages « plateforme » ; Mises à jour n'en faisait pas
  // partie, il ne trouvait donc pas le catalogue des moteurs, qui est pourtant son geste.
  await setup(page, { operator: true })
  await expect(page.getByRole('navigation', { name: 'Main navigation' }).locator('a[href="/settings/updates"]')).toBeVisible()
})
