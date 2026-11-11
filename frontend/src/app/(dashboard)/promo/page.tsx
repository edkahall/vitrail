'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslations } from 'next-intl'
import { useServerLabels } from '@/lib/serverLabels'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Sparkles, AlertTriangle, CheckCircle2, Loader2, Image as ImageIcon, Scissors, Wand2, Link2, Layers, X, Upload, Palette, RefreshCw, Settings2 } from 'lucide-react'
import Link from 'next/link'
import Topbar from '@/components/layout/Topbar'
import Button from '@/components/ui/Button'
import Drawer from '@/components/ui/Drawer'
import ContentPickerDrawer from '@/components/content/ContentPickerDrawer'
import { useAuthStore } from '@/store/auth'
import api from '@/lib/api'
import type { MediaListItem, MediaRead } from '@/types/api'
import PromoPreview, {
  type PromoInputs, type PromoTemplate, type SlotStyle, type PromoLayer,
} from './PromoPreview'
import SlotStyleControls, { type FontItem } from './SlotStyleControls'
import OrgFontsManager from './OrgFontsManager'
import BrandKitManager, { COLOR_KEYS, type BrandKit } from './BrandKitManager'
import PromoCanvasEditor from './PromoCanvasEditor'
import { LayerToolbar, LayerProps } from './LayerControls'
import SavedTemplates from './SavedTemplates'
import StepCard from './StepCard'
import SerieTable, { newSerieLine, type SerieLine, type SerieState } from './SerieTable'

/** Ajuste une boîte au ratio donné pour qu'elle tienne (contain) dans l'espace dispo — le canvas
 *  occupe toute la hauteur laissée par la topbar/sidebar, laissant les contrôles se docker dessous. */
function FitBox({ ratio, children }: { ratio: number; children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  const [box, setBox] = useState({ w: 0, h: 0 })
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = () => {
      const { width, height } = el.getBoundingClientRect()
      const w = Math.max(0, Math.min(width, height * ratio))
      setBox({ w, h: w / ratio })
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [ratio])
  return (
    <div ref={ref} className="flex min-h-0 flex-1 items-center justify-center overflow-hidden">
      <div style={{ width: box.w || undefined, height: box.h || undefined }}>{children}</div>
    </div>
  )
}

interface TemplatesResponse { templates: PromoTemplate[]; resolutions: Record<string, string[]> }
interface Preset { id: string; label: string; text: string }
interface ConfError { field: string; message: string }
interface GenJob { id_job: number; kind: string; status: string; id_media: number | null; error_detail: string | null }

const CURRENCIES = ['EUR', 'USD', 'GBP']
/** Clés des étapes du parcours — l'ordre du tableau EST l'ordre affiché et la numérotation. */
const STEPS = ['who', 'product', 'content', 'layout', 'background', 'layers'] as const
type StepKey = typeof STEPS[number]

export default function PromoPage() {
  const t = useTranslations()
  const { promoTemplate, promoAmbiance } = useServerLabels()
  const qc = useQueryClient()
  const orgId = useAuthStore((s) => s.user?.id_org)

  const [templateId, setTemplateId] = useState<string>('')
  const [format, setFormat] = useState('16:9')
  const [resolution, setResolution] = useState('fhd')
  const [bgIds, setBgIds] = useState<number[]>([])
  const [productIds, setProductIds] = useState<number[]>([])
  const [name, setName] = useState('')
  const [inputs, setInputs] = useState<PromoInputs>({ currency: 'EUR' })
  const set = (k: keyof PromoInputs, v: string) => setInputs((s) => ({ ...s, [k]: v }))
  // Styling par slot (Phase A) : fusionne un patch dans inputs.style_overrides[slot].
  const setStyle = (slot: string, patch: SlotStyle) =>
    setInputs((s) => ({
      ...s,
      style_overrides: { ...s.style_overrides, [slot]: { ...s.style_overrides?.[slot], ...patch } },
    }))

  // ── Parcours en étapes (proposition A, 2026-08-10) ───────────────────────────
  // ACCORDÉON : ouvrir une étape referme les autres.
  //
  // 🔴 J'avais fait le choix INVERSE (étapes indépendantes) en argumentant qu'un accordéon
  // ferait un tunnel. La capture FHD a tranché contre moi : à 1080 px de haut, quatre étapes
  // ouvertes ⇒ **aucune n'est visible en entier** — « Texte et prix » coupée avant les mentions,
  // « Mise en page » réduite à une demi-vignette. Ed : « on n'arrive pas à voir la totalité des
  // champs d'une étape ». Un parcours dont on ne voit aucune étape complète est pire qu'un
  // parcours séquentiel : la hauteur d'écran est une contrainte réelle, la crainte du tunnel
  // était une hypothèse. Une étape reste ouvrable à tout moment et dans n'importe quel ordre —
  // c'est ça qui empêche le tunnel, pas le fait d'en garder plusieurs dépliées.
  const [openSteps, setOpenSteps] = useState<Record<string, boolean>>({ who: true })
  const toggleStep = (k: StepKey) => setOpenSteps((s) => (s[k] ? {} : { [k]: true }))
  const openStep = (k: StepKey) => setOpenSteps({ [k]: true })
  const stepNo = (k: StepKey) => STEPS.indexOf(k) + 1

  // ── Mode SÉRIE (planche E, 2026-08-10) ───────────────────────────────────────
  // « Un gabarit, N produits. » Le parcours de gauche continue de définir tout ce qui est
  // COMMUN (marque, mise en page, fond, mentions, calques) ; le tableau ne porte que ce qui
  // VARIE. C'est ce partage qui fait passer douze visuels de douze parcours à douze lignes.
  const [serieMode, setSerieMode] = useState(false)
  const [serieLines, setSerieLines] = useState<SerieLine[]>([])
  const [serieStatus, setSerieStatus] = useState<Record<string, SerieState>>({})
  const [serieBusy, setSerieBusy] = useState(false)
  const [pickerForLine, setPickerForLine] = useState<string | null>(null)

  // Calques libres (Phase B) — vivent dans inputs.layers (pass-through ; l'id est ignoré côté serveur).
  const layers = useMemo(() => inputs.layers ?? [], [inputs.layers])
  const [selectedLayerId, setSelectedLayerId] = useState<string | null>(null)
  const setLayers = (fn: (l: PromoLayer[]) => PromoLayer[]) =>
    setInputs((s) => ({ ...s, layers: fn(s.layers ?? []) }))
  const addLayer = (partial: Partial<PromoLayer> & { type: PromoLayer['type'] }) => {
    const id = `l${Date.now().toString(36)}${Math.floor(Math.random() * 1e4)}`
    const z = layers.reduce((m, l) => Math.max(m, l.z ?? 0), 0) + 1
    const base: PromoLayer = { id, x: 32, y: 38, w: 34, h: 18, rotation: 0, z, opacity: 1, ...partial }
    setLayers((l) => [...l, base]); setSelectedLayerId(id)
  }
  const updateLayer = (id: string, patch: Partial<PromoLayer>) =>
    setLayers((l) => l.map((x) => (x.id === id ? { ...x, ...patch } : x)))
  const deleteLayer = (id: string) => {
    setLayers((l) => l.filter((x) => x.id !== id))
    setSelectedLayerId((cur) => (cur === id ? null : cur))
  }
  const moveLayerZ = (id: string, dir: 1 | -1) =>
    setLayers((l) => l.map((x) => (x.id === id ? { ...x, z: Math.max(0, (x.z ?? 0) + dir) } : x)))
  /** Pose le logo de la charte comme calque image (P3.4) — déplaçable comme n'importe quel autre.
   *  Le logo est posé par le COMPOSITING : il n'est jamais demandé au modèle génératif. */
  const addLogoLayer = (idMedia: number) =>
    addLayer({ type: 'image', media_id: idMedia, fit: 'contain', x: 4, y: 4, w: 18, h: 12 })

  const { data: cat } = useQuery<TemplatesResponse>({
    queryKey: ['promo-templates'],
    queryFn: () => api.get<TemplatesResponse>('/promo/templates').then((r) => r.data),
    enabled: !!orgId,
  })
  const { data: presetData } = useQuery<{ presets: Preset[] }>({
    queryKey: ['promo-presets'],
    queryFn: () => api.get('/promo/presets').then((r) => r.data),
    enabled: !!orgId,
  })
  const { data: images = [] } = useQuery<MediaListItem[]>({
    queryKey: ['media', 'images-ref'],
    queryFn: () => api.get<MediaListItem[]>('/media/?media_type=image').then((r) => r.data),
    enabled: !!orgId,
    // Un fond/produit fraîchement généré n'a pas encore sa miniature (transcode async) :
    // on continue de rafraîchir tant qu'une sélection n'a pas de thumbnail_url, puis on s'arrête.
    refetchInterval: (query) => {
      const data = query.state.data ?? []
      const pending = [...bgIds, ...productIds].some((id) => {
        const m = data.find((x) => x.id_media === id)
        return !!m && !m.thumbnail_url
      })
      return pending ? 2000 : false
    },
  })

  const { data: fontData } = useQuery<{ fonts: FontItem[] }>({
    queryKey: ['promo-fonts'],
    queryFn: () => api.get('/promo/fonts').then((r) => r.data),
    enabled: !!orgId,
  })
  const promoFonts = useMemo(() => fontData?.fonts ?? [], [fontData])
  const fontFamilies = useMemo(
    () => Object.fromEntries(promoFonts.map((f) => [f.id, f.family])), [promoFonts])
  const customFonts = useMemo(() => promoFonts.filter((f) => f.category === 'custom'), [promoFonts])
  const isAdmin = useAuthStore((s) => s.isAdmin)()
  const mediaThumbs = useMemo(
    () => Object.fromEntries(images.filter((m) => m.thumbnail_url)
      .map((m) => [m.id_media, m.thumbnail_url as string])), [images])
  const mediaById = useMemo(
    () => new Map(images.map((m) => [m.id_media, m])), [images])
  const mediaNames = useMemo(
    () => Object.fromEntries(images.map((m) => [m.id_media, m.media_name])), [images])

  // Charge les polices custom de l'org dans le document (FontFace API) → aperçu fidèle. Les octets
  // sont récupérés via l'API authentifiée (même origine, pas de CORS) puis servis en blob URL.
  const loadedFonts = useRef<Set<string>>(new Set())
  useEffect(() => {
    if (typeof window === 'undefined' || !('fonts' in document)) return
    customFonts.forEach((f) => (f.files ?? []).forEach((file) => {
      const key = `${f.family}:${file.weight}`
      if (loadedFonts.current.has(key)) return
      loadedFonts.current.add(key)
      api.get(`/promo/fonts/file/${file.id_font}`, { responseType: 'blob' })
        .then((r) => {
          const url = URL.createObjectURL(r.data as Blob)
          const face = new FontFace(f.family, `url(${url})`, { weight: String(file.weight) })
          return face.load().then((loaded) => { document.fonts.add(loaded) })
        })
        .catch(() => loadedFonts.current.delete(key))
    }))
  }, [customFonts])

  // ── Charte de marque (P3.4) ──────────────────────────────────────────────────
  // L'annonceur visé décide de la charte appliquée ; `null` = visuel de l'enseigne.
  const [advertiserId, setAdvertiserId] = useState<number | null>(null)
  const { data: advertisers = [] } = useQuery<{ id_advertiser: number; advertiser_name: string }[]>({
    queryKey: ['advertisers-for-promo'],
    queryFn: () => api.get('/campaigns/advertisers').then((r) => r.data),
    enabled: !!orgId,
  })
  const { data: brandKit } = useQuery<BrandKit>({
    queryKey: ['brand-kit-resolved', advertiserId],
    queryFn: () => api.get('/promo/brand-kits/resolved', {
      params: advertiserId ? { advertiser_id: advertiserId } : undefined,
    }).then((r) => r.data),
    enabled: !!orgId,
  })
  const hasBrandKit = !!brandKit && Object.keys(brandKit).length > 0
  const advertiserName = advertisers.find((a) => a.id_advertiser === advertiserId)?.advertiser_name

  // Le catalogue est servi avec le thème du TEMPLATE : on lui applique la charte pour que
  // l'aperçu montre ce que le serveur rendra (mêmes règles : seules les clés déclarées).
  const rawTemplates = useMemo(() => cat?.templates ?? [], [cat])
  const templates = useMemo(() => {
    if (!hasBrandKit) return rawTemplates
    const overrides: Record<string, string> = {}
    for (const key of COLOR_KEYS) {
      const value = brandKit?.[key]
      if (value) overrides[key] = value
    }
    if (brandKit?.font_family) overrides.font = brandKit.font_family
    return rawTemplates.map((tpl) => ({ ...tpl, theme: { ...tpl.theme, ...overrides } }))
  }, [rawTemplates, brandKit, hasBrandKit])

  // Mentions légales : pré-remplies UNE fois, CÔTÉ ÉCRAN seulement. Le faire côté serveur ferait
  // revenir les mentions qu'un opérateur vient volontairement d'effacer.
  // ⚠️ Un SET des portées déjà pré-remplies, pas la dernière : avec une simple valeur, revenir sur
  // une portée déjà visitée (organisation → annonceur → organisation) ferait RÉAPPARAÎTRE des
  // mentions que l'opérateur vient d'effacer volontairement.
  const prefilled = useRef<Set<string>>(new Set())
  useEffect(() => {
    const key = String(advertiserId)
    if (!brandKit?.legal_mentions || prefilled.current.has(key)) return
    prefilled.current.add(key)
    setInputs((s) => (s.mentions ? s : { ...s, mentions: brandKit.legal_mentions ?? '' }))
  }, [brandKit, advertiserId])

  const template = templates.find((x) => x.id === templateId) ?? templates[0]
  useEffect(() => { if (!templateId && templates[0]) setTemplateId(templates[0].id) }, [templates, templateId])
  // Cale le format/résolution sur le template sélectionné.
  useEffect(() => {
    if (template && !template.formats.includes(format)) setFormat(template.formats[0])
  }, [template, format])
  const resolutions = useMemo(() => cat?.resolutions?.[format] ?? ['fhd'], [cat, format])
  useEffect(() => { if (!resolutions.includes(resolution)) setResolution(resolutions[0]) }, [resolutions, resolution])

  const urlOf = (ids: number[]) => mediaById.get(ids[0])?.thumbnail_url || undefined
  const bgUrl = urlOf(bgIds)
  const productUrl = urlOf(productIds)

  // ── Choix d'une image : le tiroir PARTAGÉ du produit ─────────────────────────
  // 🔴 Remplace `ReferencePicker`, qui chargeait `/media/?media_type=image` À PLAT — plafond
  // serveur de 500, aucun dossier, aucune recherche, dans une boîte de deux lignes de vignettes.
  // C'est la classe de défaut déjà corrigée le 2026-07-20 sur le tiroir « Ajouter un média » :
  // au-delà du plafond, une image est invisible ET introuvable. `ContentPickerDrawer` (arbre de
  // dossiers + recherche SERVEUR débouncée) est la façon unique de choisir un contenu depuis
  // l'audit du 2026-07-29 ; l'écran promo était le dernier à ne pas s'en servir.
  const [pickerFor, setPickerFor] = useState<'background' | 'product' | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const [uploadFor, setUploadFor] = useState<'background' | 'product'>('product')
  const upload = useMutation({
    mutationFn: (file: File) => {
      const fd = new FormData()
      fd.append('file', file)
      return api.post<MediaRead>('/media/upload', fd,
        { headers: { 'Content-Type': 'multipart/form-data' } }).then((r) => r.data)
    },
    onSuccess: (m) => {
      qc.invalidateQueries({ queryKey: ['media', 'images-ref'] })
      if (uploadFor === 'background') setBgIds([m.id_media]); else setProductIds([m.id_media])
    },
  })
  const askUpload = (which: 'background' | 'product') => { setUploadFor(which); fileRef.current?.click() }

  // Conformité live (débounce) — barrière serveur ré-appliquée à la génération.
  const [errors, setErrors] = useState<ConfError[]>([])
  useEffect(() => {
    if (!template) return
    const id = setTimeout(() => {
      api.post<{ valid: boolean; errors: ConfError[] }>('/promo/validate', {
        template_id: template.id, format, inputs,
      }).then((r) => setErrors(r.data.errors)).catch(() => setErrors([]))
    }, 400)
    return () => clearTimeout(id)
  }, [template, format, inputs])

  // Pré-remplissage depuis une URL produit (aide à la saisie, §8).
  interface Extracted { name?: string | null; brand?: string | null; price?: string | null; currency?: string | null; image_url?: string | null; description?: string | null }
  const [extractUrl, setExtractUrl] = useState('')
  const [extracted, setExtracted] = useState<Extracted | null>(null)
  const [extractErr, setExtractErr] = useState<string | null>(null)
  const extract = useMutation({
    mutationFn: () => api.post<Extracted>('/promo/extract-url', { url: extractUrl.trim() }).then((r) => r.data),
    onSuccess: (d) => {
      setExtractErr(null)
      setExtracted(d)
      setInputs((s) => ({
        ...s,
        slogan: d.name || s.slogan,
        price: d.price || s.price,
        currency: d.currency || s.currency,
      }))
    },
    onError: (e) => {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setExtractErr(typeof msg === 'string' ? msg : t('promo.extract_failed'))
    },
  })
  const importImg = useMutation({
    mutationFn: () => api.post<{ id_media: number }>('/promo/import-image',
      { url: extracted?.image_url, name: extracted?.name || undefined }).then((r) => r.data),
    onSuccess: (m) => { qc.invalidateQueries({ queryKey: ['media', 'images-ref'] }); setProductIds([m.id_media]) },
  })

  // Génération de fond IA (FLUX BYOK).
  const { data: ambData } = useQuery<{ ambiances: { id: string; label: string }[] }>({
    queryKey: ['promo-ambiances'],
    queryFn: () => api.get('/promo/ambiances').then((r) => r.data),
    enabled: !!orgId,
  })
  const [bgAmbiance, setBgAmbiance] = useState('')
  const [bgDesc, setBgDesc] = useState('')
  const [bgFocus, setBgFocus] = useState('')
  const [bgErr, setBgErr] = useState<string | null>(null)
  const bgGen = useMutation({
    mutationFn: () => api.post<{ id_media: number }>('/promo/background', {
      ambiance_id: bgAmbiance || null, description: bgDesc.trim() || null,
      focus: bgFocus || null, format,
    }).then((r) => r.data),
    onSuccess: (m) => {
      setBgErr(null)
      qc.invalidateQueries({ queryKey: ['media', 'images-ref'] })
      setBgIds([m.id_media])
    },
    onError: (e) => {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setBgErr(typeof msg === 'string' ? msg : t('promo.bg_failed'))
    },
  })

  // Détourage produit (retire le fond → PNG transparent, birefnet BYOK).
  const [cutoutErr, setCutoutErr] = useState<string | null>(null)
  const cutout = useMutation({
    mutationFn: () => api.post<{ id_media: number }>('/promo/cutout',
      { source_media_id: productIds[0] }).then((r) => r.data),
    onSuccess: (m) => {
      setCutoutErr(null)
      qc.invalidateQueries({ queryKey: ['media', 'images-ref'] })
      setProductIds([m.id_media])
    },
    onError: (e) => {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setCutoutErr(typeof msg === 'string' ? msg : t('promo.cutout_failed'))
    },
  })

  const [jobs, setJobs] = useState<GenJob[]>([])
  const [mediaMode, setMediaMode] = useState<'image' | 'video'>('image')
  const [vidDuration, setVidDuration] = useState(8)
  const [vidAnimation, setVidAnimation] = useState('zoom_in')
  const gen = useMutation({
    mutationFn: () => {
      const base = {
        template_id: template!.id, format, resolution, inputs,
        background_media_id: bgIds[0] ?? null, product_media_id: productIds[0] ?? null,
        name: name.trim() || null,
        // Décide QUELLE charte le serveur appliquera (annonceur > organisation).
        id_advertiser: advertiserId,
      }
      const path = mediaMode === 'video' ? '/promo/video' : '/promo/image'
      const payload = mediaMode === 'video'
        ? { ...base, duration_s: vidDuration, animation: vidAnimation }
        : base
      return api.post<GenJob>(path, payload).then((r) => r.data)
    },
    onSuccess: (j) => setJobs((s) => [j, ...s]),
  })

  // Poll des jobs promo actifs (tant qu'il en reste un en cours).
  const hasActive = jobs.some((j) => j.status === 'pending' || j.status === 'processing')
  const { data: polled } = useQuery<GenJob[]>({
    queryKey: ['promo-jobs-poll'],
    queryFn: () => api.get<GenJob[]>('/generation').then((r) => r.data),
    enabled: hasActive,
    refetchInterval: hasActive ? 3000 : false,
  })
  useEffect(() => {
    if (!polled) return
    setJobs((s) => s.map((j) => polled.find((a) => a.id_job === j.id_job) ?? j))
  }, [polled])

  const hasError = errors.length > 0
  const canGen = !!template && !hasError && !!inputs.price && !gen.isPending
  const selectedLayer = layers.find((l) => l.id === selectedLayerId) ?? null
  const lastReady = jobs.find((j) => j.status === 'ready')

  /** Le bouton grisé DOIT dire pourquoi (règle P3.3) — et surtout EMMENER À L'ÉTAPE.
   *
   * 🔴 CORRIGÉ APRÈS AVOIR REGARDÉ UNE CAPTURE DE LA PROD. J'avais ajouté un bandeau « il manque
   * le prix » : il ne s'affiche JAMAIS dans ce cas, parce que la conformité serveur signale déjà
   * « Prix requis (nombre valide, ex. 4,99) » ⇒ `hasError` est vrai et ce bandeau est écarté.
   * Mon test le voyait uniquement parce que le mock répondait `[]` à `/promo/validate` : **il
   * était vert pour la mauvaise raison**. Ce qui manquait n'était donc pas le message — le
   * serveur le disait — mais le **chemin vers l'endroit où corriger**. D'où le bouton posé sur
   * les DEUX bandeaux, et `blockedBy` réduit à ce que la conformité ne couvre pas. */
  const FIELD_STEP: Record<string, StepKey> = {
    price: 'content', original_price: 'content', discount_pct: 'content',
    slogan: 'content', mentions: 'content',
  }
  const errorStep: StepKey = FIELD_STEP[errors[0]?.field ?? ''] ?? 'content'
  const blockedBy: { message: string; step: StepKey } | null =
    hasError ? null
      : !template ? { message: t('promo.need_template'), step: 'layout' }
        : !inputs.price ? { message: t('promo.need_price'), step: 'content' }
          : null

  /** Refaire un visuel pour un AUTRE produit : on garde tout ce qui décrit la campagne (marque,
   *  mise en page, fond, mentions, calques) et on ne vide que ce qui appartient au produit.
   *  C'est le geste qui manquait : refaire douze visuels demandait douze fois tout le parcours. */
  const redoWithAnotherProduct = () => {
    setProductIds([])
    setInputs((s) => ({ ...s, slogan: undefined, price: undefined, original_price: undefined, discount_pct: undefined }))
    setName('')
    setExtracted(null); setExtractUrl('')
    setJobs([])
    setSelectedLayerId(null)
    setOpenSteps({ product: true })
  }

  /** Lance la série : UNE génération par ligne complète, via l'endpoint existant.
   *
   * 🔑 Aucun endpoint nouveau. Chaque ligne passe par le MÊME chemin qu'un visuel isolé, donc
   * hérite sans rien réécrire de la conformité serveur, de la résolution de charte et des
   * plafonds de dépense BYOK. Un endpoint « batch » aurait dupliqué ces trois gardes — et la
   * copie aurait fini par diverger.
   *
   * ⛔ Les lignes sans prix sont ÉCARTÉES, pas bloquantes : faire échouer six visuels pour un
   * prix manquant serait le contraire de la productivité recherchée. Le tableau les nomme.
   *
   * Séquentiel volontairement : les plafonds de dépense se réservent par appel, et vingt
   * requêtes simultanées transformeraient un refus de quota en course.
   */
  const genSerie = async () => {
    const valid = serieLines.filter((l) => (l.price ?? '').trim() !== '')
    if (!template || valid.length === 0) return
    setSerieBusy(true)
    setSerieStatus(Object.fromEntries(valid.map((l) => [l.id, 'queued' as SerieState])))
    for (const line of valid) {
      const base = {
        template_id: template.id, format, resolution,
        inputs: {
          ...inputs,
          slogan: line.slogan ?? inputs.slogan,
          price: line.price,
          original_price: line.original_price,
          discount_pct: line.discount_pct,
        },
        background_media_id: bgIds[0] ?? null,
        product_media_id: line.productMediaId ?? null,
        // Nommé d'après le produit : sans ça, N visuels arrivent dans la médiathèque sous le
        // même nom et deviennent indistinguables.
        name: (line.slogan ?? '').trim() || name.trim() || null,
        id_advertiser: advertiserId,
      }
      const payload = mediaMode === 'video'
        ? { ...base, duration_s: vidDuration, animation: vidAnimation }
        : base
      try {
        const { data } = await api.post<GenJob>(
          mediaMode === 'video' ? '/promo/video' : '/promo/image', payload)
        setJobs((j) => [data, ...j])
        setSerieStatus((st) => ({ ...st, [line.id]: 'sent' }))
      } catch {
        // Une ligne qui échoue n'arrête pas les suivantes — elle se marque.
        setSerieStatus((st) => ({ ...st, [line.id]: 'error' }))
      }
    }
    setSerieBusy(false)
  }
  const serieReady = serieLines.filter((l) => (l.price ?? '').trim() !== '').length

  // Modèles enregistrés (Phase C) : capture/restauration de l'état complet de l'éditeur.
  const currentConfig = (): Record<string, unknown> => ({
    template_id: template?.id, format, resolution, inputs,
    background_media_id: bgIds[0] ?? null, product_media_id: productIds[0] ?? null, name,
  })
  const loadConfig = (cfg: Record<string, unknown>) => {
    if (typeof cfg.template_id === 'string') setTemplateId(cfg.template_id)
    if (typeof cfg.format === 'string') setFormat(cfg.format)
    if (typeof cfg.resolution === 'string') setResolution(cfg.resolution)
    setInputs((cfg.inputs as PromoInputs) ?? { currency: 'EUR' })
    setBgIds(typeof cfg.background_media_id === 'number' ? [cfg.background_media_id] : [])
    setProductIds(typeof cfg.product_media_id === 'number' ? [cfg.product_media_id] : [])
    setName(typeof cfg.name === 'string' ? cfg.name : '')
    setSelectedLayerId(null)
  }

  const inputCls = 'w-full rounded-lg border border-gray-200 bg-surface px-2 py-1.5 text-sm focus:border-accent focus:outline-none'
  const inp = 'rounded border border-gray-200 bg-surface px-1.5 py-1 text-xs'
  const lbl = 'mb-1 block text-xs font-medium text-gray-700'
  const card = 'rounded-lg border border-gray-200 p-2 space-y-1.5'
  const cardTitle = 'flex items-center gap-1.5 text-xs font-semibold text-gray-700'
  const miniBtn = 'inline-flex items-center gap-1.5 rounded-lg border border-accent/40 px-2.5 py-1 text-xs text-accent transition-sf hover:bg-accent/10 disabled:opacity-50'
  const ghostBtn = 'inline-flex items-center gap-1.5 rounded-lg border border-gray-200 px-2.5 py-1 text-xs text-gray-600 transition-sf hover:border-accent hover:text-accent'

  /** Vignette + nom du média retenu — ce que le sélecteur à plat montrait, en une ligne. */
  const pickedMedia = (ids: number[], which: 'background' | 'product') => {
    const m = mediaById.get(ids[0])
    if (!m) return null
    return (
      <div className="flex items-center gap-2 rounded border border-gray-200 p-1.5">
        <div className="h-10 w-10 shrink-0 overflow-hidden rounded bg-gray-100">
          {m.thumbnail_url
            // eslint-disable-next-line @next/next/no-img-element
            ? <img src={m.thumbnail_url} alt="" className="h-full w-full object-contain" />
            : <ImageIcon className="m-auto mt-2.5 h-5 w-5 text-gray-400" />}
        </div>
        <span className="min-w-0 flex-1 truncate text-[11px] text-gray-700">{m.media_name}</span>
        <button type="button" aria-label={t('common.delete')}
          onClick={() => (which === 'background' ? setBgIds([]) : setProductIds([]))}
          className="shrink-0 text-gray-400 transition-sf hover:text-danger">
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    )
  }

  /** Boutons « Choisir / Importer » d'une étape image — même paire pour le fond et le produit. */
  const pickRow = (which: 'background' | 'product', has: boolean) => (
    <div className="flex flex-wrap gap-1.5">
      <button type="button" className={miniBtn} onClick={() => setPickerFor(which)}
        data-testid={`promo-pick-${which}`}>
        <ImageIcon className="h-3.5 w-3.5" />
        {has ? t('promo.replace_image') : t('promo.choose_image')}
      </button>
      <button type="button" className={ghostBtn} onClick={() => askUpload(which)}
        disabled={upload.isPending}>
        {upload.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Upload className="h-3.5 w-3.5" />}
        {t('promo.import_image')}
      </button>
    </div>
  )

  const [settingsOpen, setSettingsOpen] = useState(false)

  return (
    <>
      <Topbar title={t('promo.title')}>
        {/* Un visuel, ou une série. Le parcours de gauche ne change pas de rôle : il décrit
            toujours ce qui est COMMUN — c'est seulement le centre qui passe du canvas au tableau. */}
        <div className="flex gap-1" data-testid="promo-mode">
          {([false, true] as const).map((m) => (
            <button key={String(m)} type="button" onClick={() => setSerieMode(m)}
              data-testid={m ? 'promo-mode-serie' : 'promo-mode-single'}
              className={`rounded-lg border px-2.5 py-1 text-xs transition-sf ${
                serieMode === m ? 'border-accent bg-accent/10 font-medium text-accent'
                  : 'border-gray-200 text-gray-600 hover:bg-gray-50'}`}>
              {t(m ? 'promo.mode_serie' : 'promo.mode_single')}
            </button>
          ))}
        </div>
        {isAdmin && (
          <button type="button" onClick={() => setSettingsOpen(true)} className={ghostBtn}
            data-testid="promo-open-settings">
            <Settings2 className="h-3.5 w-3.5" />{t('promo.settings_title')}
          </button>
        )}
      </Topbar>

      {/* Une seule entrée de fichier pour les deux étapes image — `uploadFor` dit laquelle. */}
      <input ref={fileRef} type="file" accept="image/*" className="hidden"
        onChange={(e) => { const f = e.target.files?.[0]; if (f) upload.mutate(f); e.target.value = '' }} />

      <main className="flex-1 overflow-hidden">
        <div className="flex h-full gap-3 p-3">
          {/* ═══ RAIL GAUCHE — le parcours ═══ */}
          <aside data-testid="promo-rail"
            className="flex w-80 shrink-0 flex-col gap-2 overflow-y-auto pr-0.5">
            {/* Reprendre un préréglage : ce n'est pas une étape, c'est un raccourci vers toutes. */}
            <SavedTemplates currentConfig={currentConfig} onLoad={loadConfig} />

            {/* ① Pour qui ? — TOUJOURS affichée, même sans aucun annonceur (décision d'Ed du
                2026-08-10). La carte était masquée dans ce cas : un nouveau venu ne découvrait
                donc jamais la notion de charte, alors que c'est ici qu'elle s'explique. */}
            <StepCard n={stepNo('who')} label={t('promo.step_who')} done
              value={advertiserName ?? t('promo.brand_for_org')}
              open={!!openSteps.who} onToggle={() => toggleStep('who')} testId="step-who">
              <select value={advertiserId == null ? '' : String(advertiserId)} className={inp + ' w-full'}
                data-testid="promo-advertiser"
                onChange={(e) => setAdvertiserId(e.target.value ? Number(e.target.value) : null)}>
                <option value="">{t('promo.brand_for_org')}</option>
                {advertisers.map((a) => (
                  <option key={a.id_advertiser} value={a.id_advertiser}>{a.advertiser_name}</option>
                ))}
              </select>
              <p className="text-[11px] text-gray-400" data-testid="promo-brand-state">
                {hasBrandKit
                  ? t('promo.brand_in_use', { name: brandKit?.kit_name ?? '' })
                  : t('promo.brand_none')}
              </p>
              {/* La phrase d'explication n'apparaît QUE s'il n'y a pas de charte : c'est le seul
                  moment où elle apprend quelque chose. Avec une charte, elle répétait la ligne
                  d'état juste au-dessus — deux phrases voisines disant la même chose (vu sur la
                  capture FHD, pas dans le code). */}
              {!hasBrandKit && <p className="text-[11px] text-gray-400">{t('promo.who_hint')}</p>}
              {brandKit?.id_logo_media && (
                <button type="button" onClick={() => addLogoLayer(brandKit.id_logo_media!)}
                  data-testid="promo-add-logo" className={ghostBtn}>
                  <ImageIcon className="h-3 w-3" />{t('promo.brand_place_logo')}
                </button>
              )}
            </StepCard>

            {/* ② Le produit — en SÉRIE, il est défini ligne par ligne : l'étape disparaît
                plutôt que de proposer un réglage qui serait ignoré. */}
            {!serieMode && <StepCard n={stepNo('product')} label={t('promo.step_product')}
              done={productIds.length > 0}
              value={mediaById.get(productIds[0])?.media_name ?? t('promo.none_selected')}
              open={!!openSteps.product} onToggle={() => toggleStep('product')} testId="step-product">
              <div className="space-y-1.5">
                <div className={cardTitle}><Link2 className="h-3.5 w-3.5 text-accent" />{t('promo.from_url')}</div>
                <div className="flex gap-1.5">
                  <input value={extractUrl} onChange={(e) => setExtractUrl(e.target.value)}
                    placeholder={t('promo.from_url_ph')} className={inputCls} />
                  <Button size="sm" variant="secondary" loading={extract.isPending}
                    disabled={!extractUrl.trim()} onClick={() => extract.mutate()}>{t('promo.extract')}</Button>
                </div>
                <p className="text-[11px] text-gray-400">{t('promo.extract_hint')}</p>
                {extractErr && <p className="text-[11px] text-danger">{extractErr}</p>}
                {extracted && (
                  <div className="flex items-center gap-2 rounded border border-gray-100 bg-gray-50 p-2 dark:bg-surface">
                    {extracted.image_url && (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img src={extracted.image_url} alt="" className="h-10 w-10 rounded object-contain" />
                    )}
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-xs font-medium text-gray-700">{extracted.name || '—'}</p>
                      <p className="text-[11px] text-gray-400">{[extracted.price, extracted.currency].filter(Boolean).join(' ')}</p>
                    </div>
                    {extracted.image_url && (
                      <Button size="sm" variant="secondary" loading={importImg.isPending}
                        onClick={() => importImg.mutate()}>{t('promo.import_as_product')}</Button>
                    )}
                  </div>
                )}
              </div>

              {pickedMedia(productIds, 'product')}
              {pickRow('product', productIds.length > 0)}
              {productIds.length === 1 && (
                <button type="button" onClick={() => cutout.mutate()} disabled={cutout.isPending} className={miniBtn}>
                  {cutout.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Scissors className="h-3.5 w-3.5" />}
                  {t('promo.cutout')}
                </button>
              )}
              <p className="text-[11px] text-gray-400">{t('promo.cutout_hint')}</p>
              {cutoutErr && <p className="text-[11px] text-danger">{cutoutErr}</p>}
            </StepCard>}

            {/* ③ Texte et prix — en SÉRIE, seules les MENTIONS restent communes. */}
            {/* ⚠️ En SÉRIE, l'étape ne porte plus le prix : afficher « à remplir » y serait FAUX —
                le prix se saisit ligne par ligne. Vu sur la capture FHD. Elle montre alors ce
                qu'elle contient vraiment : la devise et, le cas échéant, les mentions. */}
            <StepCard n={stepNo('content')} label={t('promo.step_content')}
              done={serieMode ? true : !!inputs.price}
              value={serieMode
                ? [inputs.currency ?? 'EUR', inputs.mentions ? t('promo.mentions') : null]
                    .filter(Boolean).join(' · ')
                : inputs.price ? `${inputs.price} ${inputs.currency ?? ''}`.trim() : t('promo.to_fill')}
              open={!!openSteps.content} onToggle={() => toggleStep('content')} testId="step-content">
              {/* En SÉRIE, slogan et prix varient d'une ligne à l'autre : ils quittent le commun.
                  Mentions, devise, style et nom restent ici — ils s'appliquent à tous les visuels. */}
              {serieMode && (
                <p className="text-[11px] text-gray-400" data-testid="step-content-serie-note">
                  {t('promo.serie_varies_note')}
                </p>
              )}
              {!serieMode && <>
              <div>
                <label className={lbl}>{t('promo.slogan')}</label>
                <input value={inputs.slogan ?? ''} onChange={(e) => set('slogan', e.target.value)} className={inputCls} />
                <SlotStyleControls value={inputs.style_overrides?.slogan} fonts={promoFonts}
                  onChange={(p) => setStyle('slogan', p)} />
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <label className={lbl}>{t('promo.price')}</label>
                  <input value={inputs.price ?? ''} onChange={(e) => set('price', e.target.value)}
                    placeholder="4,99" className={inputCls} />
                  <SlotStyleControls value={inputs.style_overrides?.price} fonts={promoFonts}
                    onChange={(p) => setStyle('price', p)} />
                </div>
                <div>
                  <label className={lbl}>{t('promo.original_price')}</label>
                  <input value={inputs.original_price ?? ''} onChange={(e) => set('original_price', e.target.value)}
                    placeholder="6,99" className={inputCls} />
                  <SlotStyleControls value={inputs.style_overrides?.original_price} fonts={promoFonts}
                    onChange={(p) => setStyle('original_price', p)} />
                </div>
                <div>
                  <label className={lbl}>{t('promo.discount')}</label>
                  <input value={inputs.discount_pct ?? ''} onChange={(e) => set('discount_pct', e.target.value)}
                    placeholder="auto" className={inputCls} />
                </div>
                <div>
                  <label className={lbl}>{t('promo.currency')}</label>
                  <select value={inputs.currency ?? 'EUR'} onChange={(e) => set('currency', e.target.value)} className={inputCls}>
                    {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
              </div>
              </>}
              {serieMode && (
                <div>
                  {/* La devise s'applique à TOUTES les lignes : elle reste dans le commun,
                      sinon en série il n'y aurait plus aucun moyen de la choisir. */}
                  <label className={lbl}>{t('promo.currency')}</label>
                  <select value={inputs.currency ?? 'EUR'} onChange={(e) => set('currency', e.target.value)}
                    className={inputCls}>
                    {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
              )}
              <div>
                {/* ⚠️ Le libellé et le menu de préréglages étaient côte à côte : à 320 px de rail,
                    « Mentions légales » passait sur DEUX lignes et le menu se retrouvait écrasé
                    (vu sur la capture FHD). Le menu descend donc sous le champ, pleine largeur —
                    il agit sur le texte, sa place naturelle est en dessous, pas à côté du titre. */}
                <label className={lbl}>{t('promo.mentions')}</label>
                <textarea value={inputs.mentions ?? ''} rows={2} onChange={(e) => set('mentions', e.target.value)}
                  className={inputCls + ' resize-none'} />
                <select className={inp + ' mt-1 w-full text-gray-600'} value=""
                  onChange={(e) => {
                    const p = presetData?.presets.find((x) => x.id === e.target.value)
                    if (p) set('mentions', ((inputs.mentions ?? '') + (inputs.mentions ? ' ' : '') + p.text).trim())
                  }}>
                  <option value="">{t('promo.insert_preset')}</option>
                  {(presetData?.presets ?? []).map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
                </select>
                <SlotStyleControls value={inputs.style_overrides?.mentions} fonts={promoFonts}
                  onChange={(p) => setStyle('mentions', p)} />
              </div>
              <div>
                <label className={lbl}>{t('promo.name')}</label>
                <input value={name} onChange={(e) => setName(e.target.value)}
                  placeholder={t('promo.name_ph')} className={inputCls} />
              </div>
            </StepCard>

            {/* ④ Mise en page */}
            <StepCard n={stepNo('layout')} label={t('promo.step_layout')} done={!!template}
              value={template ? `${promoTemplate(template)} · ${format} · ${resolution.toUpperCase()}` : null}
              open={!!openSteps.layout} onToggle={() => toggleStep('layout')} testId="step-layout">
              <div className="grid grid-cols-2 gap-1.5">
                {templates.map((tpl) => (
                  <button key={tpl.id} type="button" onClick={() => setTemplateId(tpl.id)}
                    className={`rounded-lg border-2 p-1 text-left transition-sf ${
                      template?.id === tpl.id ? 'border-accent' : 'border-gray-200 hover:border-gray-300'}`}>
                    <div className="pointer-events-none">
                      <PromoPreview template={tpl} format={tpl.formats.includes(format) ? format : tpl.formats[0]}
                        inputs={inputs} backgroundUrl={bgUrl} productUrl={productUrl} fontFamilies={fontFamilies} />
                    </div>
                    <span className="mt-0.5 block truncate px-0.5 text-[11px] font-medium text-gray-700">{promoTemplate(tpl)}</span>
                  </button>
                ))}
              </div>
              <div className="flex items-center gap-2">
                <div className="flex gap-1">
                  {(template?.formats ?? []).map((f) => (
                    <button key={f} type="button" onClick={() => setFormat(f)}
                      className={`rounded border px-2 py-1 text-xs transition-sf ${
                        format === f ? 'border-accent bg-accent/10 font-medium text-accent'
                          : 'border-gray-200 text-gray-600 hover:bg-gray-50'}`}>{f}</button>
                  ))}
                </div>
                <select value={resolution} onChange={(e) => setResolution(e.target.value)}
                  className={inp + ' flex-1'}>
                  {resolutions.map((r) => <option key={r} value={r}>{r.toUpperCase()}</option>)}
                </select>
              </div>
            </StepCard>

            {/* ⑤ Fond */}
            <StepCard n={stepNo('background')} label={t('promo.step_background')}
              done={bgIds.length > 0}
              value={mediaById.get(bgIds[0])?.media_name ?? t('promo.none_selected')}
              open={!!openSteps.background} onToggle={() => toggleStep('background')} testId="step-background">
              {pickedMedia(bgIds, 'background')}
              {pickRow('background', bgIds.length > 0)}
              <div className="space-y-1.5 rounded-lg border border-gray-200 p-2">
                <p className="text-[11px] font-medium text-gray-600">{t('promo.bg_generate')}</p>
                <div className="grid grid-cols-2 gap-1.5">
                  <select value={bgAmbiance} onChange={(e) => setBgAmbiance(e.target.value)} className={inp}>
                    <option value="">{t('promo.bg_ambiance')}</option>
                    {(ambData?.ambiances ?? []).map((a) => <option key={a.id} value={a.id}>{promoAmbiance(a)}</option>)}
                  </select>
                  <select value={bgFocus} onChange={(e) => setBgFocus(e.target.value)} className={inp}>
                    <option value="">{t('promo.bg_focus_none')}</option>
                    {['left', 'right', 'top', 'bottom', 'center'].map((f) => (
                      <option key={f} value={f}>{t(`promo.bg_focus_${f}`)}</option>
                    ))}
                  </select>
                </div>
                <input value={bgDesc} onChange={(e) => setBgDesc(e.target.value)}
                  placeholder={t('promo.bg_desc_ph')} className={inp + ' w-full'} />
                <button type="button" onClick={() => bgGen.mutate()} disabled={bgGen.isPending} className={miniBtn}>
                  {bgGen.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wand2 className="h-3.5 w-3.5" />}
                  {t('promo.bg_generate_btn')}
                </button>
                {bgErr && <p className="text-[11px] text-danger">{bgErr}</p>}
              </div>
            </StepCard>

            {/* ⑥ Retouches libres */}
            <StepCard n={stepNo('layers')} label={t('promo.step_layers')} done={layers.length > 0}
              value={layers.length ? t('promo.layers_count', { n: layers.length }) : t('promo.none_selected')}
              open={!!openSteps.layers} onToggle={() => toggleStep('layers')} testId="step-layers">
              <LayerToolbar layers={layers} selectedId={selectedLayerId} onAdd={addLayer}
                onSelect={setSelectedLayerId} onDelete={deleteLayer} onMoveZ={moveLayerZ} />
            </StepCard>
          </aside>

          {/* ═══ CENTRE — Canvas héros + contrôles dockés dessous ═══ */}
          <section className="flex min-w-0 flex-1 flex-col gap-2">
            {serieMode ? (
              <SerieTable
                lines={serieLines} onChange={setSerieLines}
                template={template} format={format} inputs={inputs}
                backgroundUrl={bgUrl} fontFamilies={fontFamilies}
                mediaThumbs={mediaThumbs} mediaNames={mediaNames}
                onPickProduct={setPickerForLine}
                status={serieStatus} busy={serieBusy} />
            ) : (
              <FitBox ratio={format === '9:16' ? 9 / 16 : 16 / 9}>
                <PromoCanvasEditor template={template} format={format} inputs={inputs}
                  backgroundUrl={bgUrl} productUrl={productUrl} fontFamilies={fontFamilies}
                  layers={layers} mediaThumbs={mediaThumbs}
                  selectedId={selectedLayerId} onSelect={setSelectedLayerId} onChange={updateLayer} />
              </FitBox>
            )}

            {/* Barre d'action dockée sous le rendu */}
            <div className="shrink-0 space-y-2">
              {serieMode ? null : hasError ? (
                <div data-testid="promo-not-conform"
                  className="rounded-lg border border-amber-300 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:border-amber-500/40 dark:bg-amber-500/15 dark:text-amber-300">
                  <div className="mb-0.5 flex items-center gap-1.5 font-medium">
                    <AlertTriangle className="h-4 w-4" />{t('promo.not_conform')}
                    {/* Le message dit CE QUI ne va pas ; ce bouton dit OÙ le corriger. */}
                    <button type="button" onClick={() => openStep(errorStep)}
                      data-testid="promo-blocked-goto"
                      className="ml-auto shrink-0 rounded border border-current/40 px-2 py-0.5 text-xs font-medium transition-sf hover:bg-current/10">
                      {t('promo.go_to_step', { n: stepNo(errorStep) })}
                    </button>
                  </div>
                  <ul className="list-disc space-y-0.5 pl-5 text-xs">
                    {errors.map((e, i) => <li key={i}>{e.message}</li>)}
                  </ul>
                </div>
              ) : blockedBy ? (
                <div data-testid="promo-blocked"
                  className="flex items-center gap-2 rounded-lg border border-amber-300 bg-amber-500/10 px-3 py-1.5 text-sm text-amber-700 dark:border-amber-500/40 dark:bg-amber-500/15 dark:text-amber-300">
                  <AlertTriangle className="h-4 w-4 shrink-0" />
                  <span className="flex-1">{blockedBy.message}</span>
                  <button type="button" onClick={() => openStep(blockedBy.step)}
                    data-testid="promo-blocked-goto"
                    className="shrink-0 rounded border border-current/40 px-2 py-0.5 text-xs font-medium transition-sf hover:bg-current/10">
                    {t('promo.go_to_step', { n: stepNo(blockedBy.step) })}
                  </button>
                </div>
              ) : (
                <div className="flex items-center gap-1.5 rounded-lg border border-green-300 bg-green-50 px-3 py-1.5 text-sm text-green-700 dark:border-green-500/40 dark:bg-green-500/15 dark:text-green-300">
                  <CheckCircle2 className="h-4 w-4" />{t('promo.conform')}
                </div>
              )}

              <div className="flex items-center gap-2">
                <div className="flex gap-1">
                  {(['image', 'video'] as const).map((m) => (
                    <button key={m} type="button" onClick={() => setMediaMode(m)}
                      className={`rounded-lg border px-3 py-2 text-sm transition-sf ${
                        mediaMode === m ? 'border-accent bg-accent/10 font-medium text-accent'
                          : 'border-gray-200 text-gray-600 hover:bg-gray-50'}`}>
                      {t(`promo.mode_${m}`)}
                    </button>
                  ))}
                </div>
                {mediaMode === 'video' && (
                  <div className="flex flex-1 items-center gap-2 rounded-lg border border-gray-200 px-2 py-1">
                    <label className="flex items-center gap-1 text-xs text-gray-600">
                      {t('promo.vid_duration', { s: vidDuration })}
                      <input type="range" min={3} max={15} value={vidDuration}
                        onChange={(e) => setVidDuration(Number(e.target.value))} className="w-20" />
                    </label>
                    <select value={vidAnimation} onChange={(e) => setVidAnimation(e.target.value)} className={inp}>
                      {['zoom_in', 'zoom_out', 'static'].map((a) => (
                        <option key={a} value={a}>{t(`promo.anim_${a}`)}</option>
                      ))}
                    </select>
                  </div>
                )}
                <div className="ml-auto">
                  {serieMode ? (
                    <Button loading={serieBusy} disabled={!template || serieReady === 0 || serieBusy}
                      onClick={genSerie} data-testid="serie-generate">
                      <Sparkles className="mr-1.5 h-4 w-4" />
                      {t('promo.serie_generate', { n: serieReady })}
                    </Button>
                  ) : (
                    <Button loading={gen.isPending} disabled={!canGen} onClick={() => gen.mutate()}>
                      <Sparkles className="mr-1.5 h-4 w-4" />{t('promo.generate')}
                    </Button>
                  )}
                </div>
              </div>

              {/* La file des générations a migré dans le rail droit : elle s'accumule pendant la
                  séance, elle avait sa place là où l'espace était vide, pas entre le rendu et le
                  bas de l'écran. */}
            </div>
          </section>

          {/* ═══ RAIL DROIT — inspecteur ou charte, PUIS les visuels produits ═══════════════
              🔴 Ce rail ne portait qu'une petite carte en haut et un grand vide dessous (vu sur
              la capture FHD). ⛔ Le supprimer n'aurait rien réglé : le canvas est contraint par la
              HAUTEUR, pas par la largeur — on aurait déplacé le vide, pas gagné un pixel d'aperçu.
              On y met donc ce qui s'accumule pendant une séance et qui n'avait pas de place :
              la file des générations, jusque-là coincée entre le rendu et le bas de l'écran. */}
          <aside className="flex w-80 shrink-0 flex-col gap-3 overflow-y-auto pl-0.5">
            {selectedLayer ? (
              <div className={card}>
                <div className="flex items-center justify-between">
                  <span className={cardTitle}><Layers className="h-3.5 w-3.5 text-accent" />{t('promo.layer_props_title')}</span>
                  <button type="button" onClick={() => setSelectedLayerId(null)}
                    className="inline-flex items-center gap-0.5 text-[11px] text-gray-400 hover:text-gray-700">
                    <X className="h-3 w-3" />{t('promo.layer_deselect')}
                  </button>
                </div>
                <LayerProps layer={selectedLayer} fonts={promoFonts} onUpdate={updateLayer} />
              </div>
            ) : (
              /* La charte APPLIQUÉE, en permanence : c'est elle qui décide des couleurs, de la
                 police et des mentions — l'opérateur doit la voir sans la chercher. */
              <div className={card}>
                <div className={cardTitle}>
                  <Palette className="h-3.5 w-3.5 text-accent" />
                  {advertiserName ?? t('promo.brand_for_org')}
                </div>
                {/* ⛔ Pas de phrase d'état ici : elle est DÉJÀ dans l'étape 1, mot pour mot
                    (`promo-brand-state`). Vu sur la capture FHD — les deux s'affichaient en
                    même temps. Ici on montre ce que l'étape ne montre pas : les couleurs. */}
                {hasBrandKit ? (
                  <div className="flex gap-1">
                    {COLOR_KEYS.map((k) => brandKit?.[k] ? (
                      <span key={k} title={k} className="h-4 w-4 rounded border border-white/15"
                        style={{ background: brandKit[k] as string }} />
                    ) : null)}
                  </div>
                ) : (
                  <p className="text-[11px] text-gray-400">{t('promo.brand_none')}</p>
                )}
                <button type="button" onClick={() => openStep('who')} className={ghostBtn}>
                  {t('promo.brand_change')}
                </button>
              </div>
            )}

            {/* Les visuels produits pendant la séance — TOUJOURS visibles, y compris quand on
                retouche un calque : une génération en cours ne doit pas disparaître de l'écran
                parce qu'on a cliqué sur un texte. */}
            <div className={card + ' flex min-h-0 flex-1 flex-col'} data-testid="promo-results">
              <p className={cardTitle}><Sparkles className="h-3.5 w-3.5 text-accent" />{t('promo.results')}</p>
              {jobs.length === 0 ? (
                <p className="text-[11px] text-gray-400">{t('promo.results_empty')}</p>
              ) : (
                <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto">
                  {jobs.map((j) => {
                    const done = j.status === 'ready'
                    const failed = j.status === 'failed'
                    return (
                      <div key={j.id_job} className="flex items-center gap-2 rounded-lg border border-gray-200 px-2 py-1.5 text-xs">
                        {done ? <CheckCircle2 className="h-4 w-4 shrink-0 text-green-600" />
                          : failed ? <AlertTriangle className="h-4 w-4 shrink-0 text-danger" />
                          : <Loader2 className="h-4 w-4 shrink-0 animate-spin text-accent" />}
                        <span className="flex-1 truncate text-gray-600">
                          {done ? t('promo.job_ready') : failed ? (j.error_detail || t('promo.job_failed')) : t('promo.job_running')}
                        </span>
                      </div>
                    )
                  })}
                </div>
              )}

              {/* Ce qu'on fait du visuel une fois produit. « Refaire avec un autre produit » est
                  le geste qui manquait le plus : sans lui, douze visuels coûtaient douze fois
                  tout le parcours. */}
              {lastReady && (
                <div className="mt-1 space-y-1.5 border-t border-gray-200 pt-2" data-testid="promo-after-gen">
                  <Link href="/media" className={ghostBtn + ' w-full justify-center'}>
                    <ImageIcon className="h-3.5 w-3.5" />{t('promo.view_in_library')}
                  </Link>
                  <button type="button" onClick={redoWithAnotherProduct} className={miniBtn + ' w-full justify-center'}
                    data-testid="promo-redo">
                    <RefreshCw className="h-3.5 w-3.5" />{t('promo.redo_other_product')}
                  </button>
                  <p className="text-[11px] text-gray-400">{t('promo.redo_hint')}</p>
                </div>
              )}
            </div>
          </aside>
        </div>
      </main>

      {/* Tiroir de choix d'image — rendu AU NIVEAU DE LA PAGE (jamais dans un autre Drawer). */}
      <ContentPickerDrawer
        open={pickerFor !== null || pickerForLine !== null}
        onClose={() => { setPickerFor(null); setPickerForLine(null) }}
        mediaTypes={['image']}
        multiple={false}
        title={pickerFor === 'background' ? t('promo.picker_background') : t('promo.picker_product')}
        onPick={(pick) => {
          const id = pick.medias[0]
          if (id != null) {
            // Le MÊME tiroir sert au fond, au produit d'un visuel isolé et au produit d'une
            // ligne de série : trois entrées, une seule façon de choisir un contenu.
            if (pickerForLine) {
              setSerieLines((ls) => ls.map((l) => (l.id === pickerForLine ? { ...l, productMediaId: id } : l)))
            } else if (pickerFor === 'background') setBgIds([id])
            else setProductIds([id])
          }
          setPickerFor(null); setPickerForLine(null)
        }}
      />

      {/* Réglages SORTIS du flux de production : on les pose une fois par an, ils occupaient le
          bas du rail d'un écran utilisé tous les jours. */}
      {isAdmin && (
        <Drawer open={settingsOpen} onClose={() => setSettingsOpen(false)}
          title={t('promo.settings_title')} width="w-[560px]">
          <div className="space-y-3">
            <BrandKitManager fonts={promoFonts} />
            <OrgFontsManager customFonts={customFonts} />
          </div>
        </Drawer>
      )}
    </>
  )
}
