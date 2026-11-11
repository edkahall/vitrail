import { test, expect, type Page } from '@playwright/test'

// Générateur promo → le parcours en étapes (proposition A, 2026-08-10).
//
// Ce que ces tests verrouillent, et pourquoi :
//   · UN point d'entrée au chargement — le constat d'Ed était « je ne savais pas où cliquer » ;
//   · une étape repliée AFFICHE sa valeur — sans ça, replier reviendrait à cacher, et l'écran
//     serait moins lisible qu'avant, pas plus ;
//   · le bouton grisé DIT pourquoi et propose le geste (règle P3.3) — il ne restait qu'un bouton
//     éteint, et sans prix aucun bandeau ne s'affichait non plus ;
//   · « Pour qui ? » existe même sans aucun annonceur (décision d'Ed) — la carte était masquée
//     dans ce cas, donc un nouveau venu ne découvrait jamais la notion de charte ;
//   · ouvrir une étape REFERME les autres — j'avais choisi l'inverse ; la capture FHD a montré
//     qu'à quatre étapes ouvertes aucune n'est visible en entier, et Ed a tranché.

const BASE = process.env.PLAYWRIGHT_BASE_URL ?? 'http://localhost:3000'

const USER = {
  id_user: 1, id_org: 1, email: 'admin@test.com', full_name: 'Olivia Rhye',
  avatar_url: null, totp_enabled: false, locale: 'fr', is_active: true,
  last_login_at: null, created_at: new Date().toISOString(),
}

const TEMPLATES = {
  templates: [{
    id: 'fresh',
    // Le VRAI libellé du serveur (anglais) : l'interface française doit le traduire par l'id.
    label: 'Fresh / Food',
    formats: ['16:9'],
    theme: { accent: '#3F8F4F', ink: '#1D2B3A', paper: '#F8F7F5', badge_bg: '#E4572E', badge_ink: '#FFFFFF', font: 'Inter' },
    layouts: {
      '16:9': {
        product: { left: 55, top: 12, width: 40, height: 76 },
        slogan: { left: 6, top: 12, width: 46, height: 32, align: 'left', font_vh: 8.5, min_vh: 4.5, color: 'ink', weight: 800, max_chars: 42 },
        price: { left: 6, top: 52, width: 40, align: 'left', font_vh: 16, min_vh: 9, color: 'accent', weight: 900, max_chars: 10 },
        mentions: { left: 6, top: 90, width: 88, height: 7, align: 'left', font_vh: 2.4, min_vh: 2, color: 'ink', weight: 500, max_chars: 220 },
      },
    },
  }],
  resolutions: { '16:9': ['fhd', '4k'] },
}

async function setup(page: Page, {
  advertisers = [{ id_advertiser: 7, advertiser_name: 'Brightwell' }],
  templates = TEMPLATES,
} = {}) {
  await page.goto(`${BASE}/login`)
  await page.evaluate((u) => {
    const b64url = (o: unknown) =>
      btoa(JSON.stringify(o)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
    const roles = ['CMS_ORG_ADMIN']
    const tk = [
      b64url({ alg: 'HS256', typ: 'JWT' }),
      b64url({ sub: '1', org: 1, roles, locale: 'fr', type: 'access', exp: 9999999999 }),
      'signature',
    ].join('.')
    localStorage.setItem('sf-auth', JSON.stringify({
      state: { user: u, accessToken: tk, refreshToken: 'mock_refresh', roles }, version: 0,
    }))
    localStorage.setItem('sf_access_token', tk)
    localStorage.setItem('sf_refresh_token', 'mock_refresh')
    document.cookie = 'locale=fr; path=/'
  }, USER)

  const json = (body: unknown) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  // ⚠️ Playwright résout les routes de la DERNIÈRE à la première : le glob générique d'abord.
  await page.route('**/api/v1/**', (r) => r.fulfill(json([])))
  await page.route('**/api/v1/users/me', (r) => r.fulfill(json(USER)))
  await page.route('**/api/v1/promo/templates**', (r) => r.fulfill(json(templates)))
  await page.route('**/api/v1/promo/fonts**', (r) => r.fulfill(json({ fonts: [] })))
  await page.route('**/api/v1/promo/brand-kits/resolved**', (r) => r.fulfill(json({})))
  await page.route('**/api/v1/campaigns/advertisers**', (r) => r.fulfill(json(advertisers)))
  // 🔴 La conformité doit se comporter comme le VRAI serveur. Mon premier mock répondait `[]`
  // en toutes circonstances : le test croyait donc voir mon bandeau « il manque le prix » alors
  // qu'en production la conformité signale déjà « Prix requis » et l'écarte. **Le test était
  // vert pour la mauvaise raison** — découvert en regardant une capture de la prod, pas un test.
  await page.route('**/api/v1/promo/validate', (r) => {
    const body = r.request().postDataJSON() as { inputs?: { price?: string } } | null
    const hasPrice = !!body?.inputs?.price
    return r.fulfill(json({
      valid: hasPrice,
      errors: hasPrice ? [] : [{ field: 'price', message: 'Prix requis (nombre valide, ex. 4,99).' }],
    }))
  })

  await page.goto(`${BASE}/promo`)
  await expect(page.getByTestId('step-who')).toBeVisible()
}

const step = (page: Page, key: string) => page.getByTestId(`step-${key}`)
const header = (page: Page, key: string) => step(page, key).getByRole('button').first()

test('au chargement, une seule étape est dépliée — le point d entrée', async ({ page }) => {
  await setup(page)
  await expect(step(page, 'who')).toHaveAttribute('data-open', 'true')
  for (const k of ['product', 'content', 'layout', 'background', 'layers']) {
    await expect(step(page, k)).toHaveAttribute('data-open', 'false')
  }
})

test('une étape repliée affiche sa valeur, pas un tiroir vide', async ({ page }) => {
  await setup(page)
  // 🔑 C'est CE comportement qui rend le repli acceptable : sans la valeur, on aurait caché
  // vingt contrôles derrière six libellés et rendu l'écran moins lisible qu'avant.
  await expect(step(page, 'layout').getByTestId('step-layout-value')).toHaveText('Frais / Alimentaire · 16:9 · FHD')
  await expect(step(page, 'content').getByTestId('step-content-value')).toHaveText('à remplir')
  await expect(step(page, 'product').getByTestId('step-product-value')).toHaveText('Aucun')
})

test('le bouton grisé dit pourquoi et emmène au bon endroit', async ({ page }) => {
  await setup(page)
  // Sans prix, le serveur signale déjà « Prix requis » : ce qui manquait n'était pas le message
  // mais le CHEMIN vers l'endroit où corriger. Le bouton est donc posé sur ce bandeau-là.
  await expect(page.getByRole('button', { name: /Générer/ })).toBeDisabled()
  const banner = page.getByTestId('promo-not-conform')
  await expect(banner).toBeVisible()
  await expect(banner).toContainText('Prix requis')
  await expect(banner.getByTestId('promo-blocked-goto')).toContainText('3')   // l'étape « Texte et prix »

  await page.getByTestId('promo-blocked-goto').click()
  await expect(step(page, 'content')).toHaveAttribute('data-open', 'true')

  // …et une fois le prix saisi, le bandeau disparaît et le bouton s'active.
  await page.getByPlaceholder('4,99').fill('499,00')
  await expect(page.getByTestId('promo-not-conform')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /Générer/ })).toBeEnabled()
})

test('« Pour qui ? » s affiche même sans aucun annonceur', async ({ page }) => {
  // Décision d'Ed (2026-08-10) : la carte était masquée quand l'organisation n'avait aucun
  // annonceur — c'est précisément le cas où il faut EXPLIQUER la notion de charte, pas la cacher.
  await setup(page, { advertisers: [] })
  await expect(step(page, 'who')).toBeVisible()
  await expect(page.getByTestId('promo-advertiser')).toBeVisible()
  await expect(step(page, 'who').getByTestId('step-who-value')).toHaveCount(0)   // dépliée
  await expect(page.getByTestId('promo-brand-state')).toBeVisible()
})

test('ouvrir une étape referme les autres — sinon aucune n est visible en entier', async ({ page }) => {
  // 🔴 CE TEST AFFIRMAIT L'INVERSE. J'avais choisi des étapes indépendantes en craignant le
  // tunnel ; la capture FHD a montré qu'à quatre étapes ouvertes, AUCUNE n'est visible en
  // entier (« Texte et prix » coupée avant les mentions). Ed a tranché : les autres se
  // replient. La hauteur d'écran est une contrainte mesurée, le tunnel était une hypothèse.
  await setup(page)
  await header(page, 'content').click()
  await expect(step(page, 'content')).toHaveAttribute('data-open', 'true')
  await expect(step(page, 'who')).toHaveAttribute('data-open', 'false')

  // Une seule étape ouverte à la fois, quel que soit le chemin emprunté.
  await header(page, 'layout').click()
  for (const k of ['who', 'product', 'content', 'background', 'layers']) {
    await expect(step(page, k)).toHaveAttribute('data-open', 'false')
  }
  await expect(step(page, 'layout')).toHaveAttribute('data-open', 'true')

  // …et elle reste refermable : le parcours n'enferme pas dans une étape.
  await header(page, 'layout').click()
  await expect(step(page, 'layout')).toHaveAttribute('data-open', 'false')
})

test('choisir une image passe par le tiroir de médiathèque, pas par une grille à plat', async ({ page }) => {
  // 🔴 Le sélecteur d'avant chargeait /media/?media_type=image à plat (plafond 500, ni dossier
  // ni recherche) : au-delà, une image était invisible ET introuvable.
  await setup(page)
  await header(page, 'product').click()
  await page.getByTestId('promo-pick-product').click()
  await expect(page.getByPlaceholder(/Rechercher/i).first()).toBeVisible()
})

test('un modèle que l interface ne connaît pas garde le libellé du serveur', async ({ page }) => {
  // Témoin du repli : un modèle ajouté côté serveur ne doit jamais s'afficher en clé brute.
  await setup(page, {
    templates: { ...TEMPLATES, templates: [{ ...TEMPLATES.templates[0], id: 'bakery_2027', label: 'Bakery 2027' }] },
  })
  await expect(step(page, 'layout').getByTestId('step-layout-value')).toHaveText('Bakery 2027 · 16:9 · FHD')
})
