import { test, expect } from '@playwright/test'

// Version du moteur Electron dans la fiche du player (demande Ed 14/11) : depuis vitrail#5 le moteur
// se met à jour à part du code, la version du player ne dit plus rien du moteur.

const BASE = process.env.PLAYWRIGHT_BASE_URL ?? 'http://localhost:3000'
const MOCK_USER_ME = { id_user: 1, id_org: 1, email: 'admin@test.com', full_name: 'Admin', avatar_url: null, totp_enabled: false, locale: 'en', is_active: true, last_login_at: null, created_at: new Date().toISOString() }

const PLAYERS = [
  { id_player: 2, id_org: 1, player_name: 'Windows Aopen', player_os: 'windows', player_version: '1.6.25', status: 'online', id_site: null, capabilities: { electron_version: '42.11.18' }, alert_reasons: [] },
  { id_player: 13, id_org: 1, player_name: 'BrightSign HD1024', player_os: 'brightsign', player_version: '2.0.40', status: 'online', id_site: null, capabilities: { bs_model: 'HD1024' }, alert_reasons: [] },
]

async function mockAuthAndNavigate(page: import('@playwright/test').Page, url: string) {
  await page.goto(`${BASE}/login`)
  await page.evaluate(() => {
    const mockToken = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIiwib3JnIjoxLCJyb2xlcyI6WyJDTVNfT1JHX0FETUlOIl0sImxvY2FsZSI6ImVuIiwidHlwZSI6ImFjY2VzcyIsImV4cCI6OTk5OTk5OTk5OX0.signature'
    const store = {
      state: {
        user: { id_user: 1, id_org: 1, email: 'admin@test.com', full_name: 'Admin', avatar_url: null, totp_enabled: false, locale: 'en', is_active: true, last_login_at: null, created_at: new Date().toISOString() },
        accessToken: mockToken, refreshToken: 'mock_refresh', roles: ['CMS_ORG_ADMIN'],
      },
      version: 0,
    }
    localStorage.setItem('sf-auth', JSON.stringify(store))
    localStorage.setItem('sf_access_token', mockToken)
    localStorage.setItem('sf_refresh_token', 'mock_refresh')
  })
  await page.route('**/api/v1/users/me', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_USER_ME) })
  })
  await page.route('**/api/v1/auth/refresh', async (route) => {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ access_token: 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIiwib3JnIjoxLCJyb2xlcyI6WyJDTVNfT1JHX0FETUlOIl0sImxvY2FsZSI6ImVuIiwidHlwZSI6ImFjY2VzcyIsImV4cCI6OTk5OTk5OTk5OX0.signature', refresh_token: 'mock_refresh', token_type: 'bearer', locale: 'en' }),
    })
  })
  await page.goto(url)
}

async function open(page: import('@playwright/test').Page, name: string) {
  await page.route('**/api/v1/**', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: '[]' }))
  await page.route(/\/api\/v1\/players\/(\?.*)?$/, (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAYERS) }))
  await mockAuthAndNavigate(page, `${BASE}/players`)
  await page.getByText(name, { exact: true }).first().click()
}

test("la fiche d'un player Electron montre la version du moteur a cote de la sienne", async ({ page }) => {
  await open(page, 'Windows Aopen')
  // Une seule ligne : « v1.6.25 windows (Electron 42.11.18) », Renommer à droite, même hauteur.
  const line = page.getByTestId('player-version-line')
  await expect(line).toHaveText('v1.6.25 windows (Electron 42.11.18)')
  const rename = page.getByRole('button', { name: 'Rename' })
  const [a, b] = [await line.boundingBox(), await rename.boundingBox()]
  expect(a && b && Math.abs((a.y + a.height / 2) - (b.y + b.height / 2)) < 6).toBeTruthy()
})

test("un player non Electron (BrightSign) n'affiche aucune version de moteur", async ({ page }) => {
  await open(page, 'BrightSign HD1024')
  await expect(page.getByTestId('player-version-line')).toHaveText('v2.0.40 brightsign')
  await expect(page.getByTestId('player-electron-version')).toHaveCount(0)
})
