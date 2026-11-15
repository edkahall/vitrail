import { test, expect } from '@playwright/test'

// Mises à jour contrôlées de l'OS (issue vitrail#6) — tiroir « OS updates » de /settings/updates.
//
// Ce que ces tests figent :
// - l'état OS de chaque player Linux est lisible (en attente, sécurité, script root absent) ;
// - seul l'EXPLOITANT met à jour un banc et approuve un manifeste (l'écran ne propose pas aux
//   autres un geste voué au 403) ;
// - un manifeste non approuvé ne se déploie que sur UN player, la raison écrite sur la ligne ;
// - choisir un player ne montre que les manifestes de SA famille d'OS ;
// - un déploiement qui ne vise personne le dit.

const BASE = process.env.PLAYWRIGHT_BASE_URL ?? 'http://localhost:3000'

const USER = {
  id_user: 1, id_org: 1, email: 'admin@test.com', full_name: 'Olivia Rhye',
  avatar_url: null, totp_enabled: false, locale: 'en', is_active: true,
  last_login_at: null, created_at: new Date().toISOString(),
}

const osu = (family: string, pending: number, security: number, helper = true) => ({
  helper, family, pending, security, checked_at: '2026-11-15T15:44:52Z', last_upgrade: null,
  reboot_required: false, update_error: null, applied: null, run: null,
})
const row = (id: number, name: string, os: string, o: unknown) => ({
  id_player: id, player_name: name, player_os: os, status: 'online', current_version: '1.5.28',
  latest_version: '1.5.28', up_to_date: true, update_state: 'up_to_date', renderer_degraded: null,
  electron_version: '42.11.18', os_update: o, site: 'Site-01', groups: [],
})
const PARC = {
  players: [
    row(22, 'NUC-banc', 'linux-x64', osu('debian-12-amd64', 134, 91)),
    row(4, 'RPi-banc', 'linux-arm64', osu('raspios-13-arm64', 400, 84)),
    row(30, 'NUC-old', 'linux-x64', { helper: false }),
    row(9, 'BS-hall', 'brightsign', null),
  ],
  total: 4, outdated: 0, unknown: 0,
}
const man = (id: number, version: string, family: string, channel: string) => ({
  id_version: id, track: 'os', version, channel, model_family: family, download_url: null,
  release_notes: null, min_firmware_version: null, created_at: '2026-11-15T16:00:00Z',
  manifest: { packages: [{ name: 'libssl3', arch: 'amd64', version: '3.0.15-1~deb12u1' }], source_player: 22, reboot_required: true },
})
const MANIFESTS = [man(90, '20261115-160000', 'debian-12-amd64', 'beta'), man(91, '20261101-090000', 'raspios-13-arm64', 'stable')]

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
  const calls: { bench: unknown[]; deploy: unknown[]; promote: string[] } = { bench: [], deploy: [], promote: [] }
  // ⚠️ Playwright résout les routes de la DERNIÈRE à la première : le glob générique en premier.
  await page.route('**/api/v1/**', (r) => r.fulfill(json([])))
  await page.route('**/api/v1/users/me', (r) => r.fulfill(json(USER)))
  await page.route('**/api/v1/auth/refresh', (r) =>
    r.fulfill(json({ access_token: 'x', refresh_token: 'mock_refresh', token_type: 'bearer', locale: 'en' })))
  // Les deux noms selon la branche (exploitant du serveur / porteur de la licence).
  await page.route('**/api/v1/meta/operator**', (r) => r.fulfill(json({ is_operator: operator })))
  await page.route('**/api/v1/meta/licence**', (r) => r.fulfill(json({ is_holder: operator })))
  await page.route('**/api/v1/updates/parc**', (r) => r.fulfill(json(PARC)))
  await page.route((u) => u.pathname.endsWith('/updates/versions/') || u.pathname.endsWith('/updates/versions'),
    (r) => r.fulfill(json(new URL(r.request().url()).searchParams.get('track') === 'os' ? MANIFESTS : [])))
  await page.route('**/api/v1/updates/versions/*/promote', (r) => {
    calls.promote.push(r.request().url())
    return r.fulfill(json({ ...MANIFESTS[0], channel: 'stable' }))
  })
  await page.route('**/api/v1/updates/os/bench', (r) => {
    calls.bench.push(r.request().postDataJSON())
    return r.fulfill(json({ request_id: 'abc' }, 202))
  })
  await page.route('**/api/v1/updates/deployments', (r) => {
    if (r.request().method() === 'POST') {
      const b = r.request().postDataJSON()
      calls.deploy.push(b)
      return r.fulfill(json({ id_deployment: 50, total_targets: b.targets[0].target_type === 'player' ? 1 : 0 }, 201))
    }
    return r.fulfill(json([]))
  })
  await page.goto(`${BASE}/settings/updates`)
  await expect(page.getByRole('cell', { name: 'NUC-banc' })).toBeVisible()
  await page.getByTestId('os-btn').click()
  await expect(page.getByTestId('os-drawer')).toBeVisible()
  return calls
}

test("etat OS par player, et un admin d'org ne voit ni banc ni approbation", async ({ page }) => {
  const calls = await setup(page, { operator: false })
  const players = page.getByTestId('os-players')
  await expect(players.locator('li', { hasText: 'NUC-banc' })).toContainText('134 updates pending')
  await expect(players.locator('li', { hasText: 'NUC-banc' })).toContainText('91 security')
  await expect(players.locator('li', { hasText: 'NUC-old' })).toContainText('install-os-update.sh')
  await expect(players.locator('li', { hasText: 'BS-hall' })).toHaveCount(0)   // pas Linux
  await expect(page.getByTestId('os-not-operator')).toBeVisible()
  await expect(page.getByTestId('os-bench-btn')).toHaveCount(0)
  await expect(page.getByTestId('os-approve')).toHaveCount(0)

  // Bêta + toute l'organisation : bloqué, raison écrite sur la ligne.
  await page.getByTestId('os-target-type').selectOption('org')
  const beta = page.getByTestId('os-manifests').locator('li', { hasText: '20261115-160000' }).first()
  await expect(beta.getByTestId('os-deploy')).toBeDisabled()
  await expect(beta.getByTestId('os-row-reason')).toContainText('single player')
  // Approuvé + toute l'org : autorisé ; personne de cette famille ⇒ le dire.
  const stable = page.getByTestId('os-manifests').locator('li', { hasText: '20261101-090000' }).first()
  await stable.getByTestId('os-deploy').click()
  await expect(page.getByTestId('os-msg')).toContainText('No player of this target')
  // Un player choisi : seulement les manifestes de SA famille.
  await page.getByTestId('os-target-type').selectOption('player')
  await page.getByTestId('os-target-id').selectOption('22')
  await expect(page.getByTestId('os-manifests').locator('li', { hasText: '20261101-090000' })).toHaveCount(0)
  await beta.getByTestId('os-deploy').click()
  await expect(page.getByTestId('os-msg')).toContainText('1 player')
  // Vagues : 1 player à la fois par défaut, réglable.
  expect(calls.deploy.at(-1)).toMatchObject({ id_version: 90, targets: [{ target_type: 'player', target_id: 22 }],
    strategy_params: { wave_size: 1 } })
  await page.getByTestId('os-drawer').getByLabel('Players at a time').fill('3')
  await page.getByTestId('os-drawer').getByLabel('Players at a time').blur()
  await beta.getByTestId('os-deploy').click()
  await expect.poll(() => calls.deploy.length).toBe(3)
  expect(calls.deploy.at(-1)).toMatchObject({ strategy_params: { wave_size: 3 } })
})

test("l'exploitant met a jour un banc et approuve un manifeste", async ({ page }) => {
  const calls = await setup(page, { operator: true })
  await expect(page.getByTestId('os-not-operator')).toHaveCount(0)
  const sel = page.getByTestId('os-bench-player')
  await expect(sel.locator('option', { hasText: 'NUC-old' })).toHaveCount(0)   // sans script root
  await sel.selectOption('4')
  await page.getByTestId('os-bench-btn').click()
  await expect(page.getByTestId('os-msg')).toContainText('Bench update started')
  expect(calls.bench).toEqual([{ id_player: 4 }])
  await page.getByTestId('os-approve').first().click()
  await expect.poll(() => calls.promote.length).toBe(1)
  expect(calls.promote[0]).toContain('/updates/versions/90/promote')
})
