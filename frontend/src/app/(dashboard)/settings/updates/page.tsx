'use client'

import { useMemo, useState } from 'react'
import { useTranslations } from 'next-intl'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, CheckCircle2, AlertTriangle, HelpCircle, Loader2, Trash2, Rocket, Plus, Search, Clock, Pause, Play, Upload, Cpu } from 'lucide-react'
import Topbar from '@/components/layout/Topbar'
import Button from '@/components/ui/Button'
import Drawer from '@/components/ui/Drawer'
import api from '@/lib/api'

interface ParcRow {
  id_player: number; player_name: string; player_os: string; status: string
  current_version: string | null; latest_version: string | null; up_to_date: boolean
  // 'unknown' = aucune version publiée pour cette plateforme, donc AUCUNE comparaison possible.
  // Distinct de 'up_to_date' : le confondre annonçait un parc sain qu'on n'avait comparé à rien.
  update_state: 'up_to_date' | 'outdated' | 'unknown'
  // Version épinglée que le player n'a PAS pu charger : il exécute un renderer de repli, donc du
  // code potentiellement très ancien. À distinguer d'un simple retard — un retard attend un
  // déploiement, un RECUL est arrivé tout seul et repartira tout seul (mesuré le 2026-08-12 :
  // trois semaines en 2.0.8 alors que le serveur épinglait 2.0.17).
  renderer_degraded: string | null
  // Moteur Electron courant (vitrail#5) ; null pour un player non Electron.
  electron_version: string | null
  site: string | null; groups: string[]
}
interface Version {
  id_version: number; track: string; version: string; channel: string
  download_url: string | null; model_family: string | null; release_notes: string | null
  min_firmware_version: string | null
}
interface Deployment {
  id_deployment: number; id_version: number; strategy: string; status: string
  total_targets: number; success_count: number; failed_count: number; created_at: string
}
interface DeployTarget {
  id_target: number; id_player: number; status: string; previous_version: string | null
  error_message: string | null
}
interface BundleImportResult {
  dry_run: boolean; platform: string; version: string; size_bytes: number; sha256: string
  already_registered: boolean; artifact_present: boolean; id_version: number | null
  download_url: string | null
  files_extracted: number | null
}

// Moteur Electron (vitrail#5) : plateformes = player_os exact des players Electron.
const ENGINE_FAMILIES = ['linux-arm64', 'linux-x64', 'windows'] as const
interface EngineJob { job_id: string; family: string; version: string; state?: string; error?: string; created?: boolean }

const inp = 'w-full rounded-lg border border-gray-200 bg-surface px-2 py-1.5 text-sm focus:border-accent focus:outline-none'
const sel = 'rounded-lg border border-gray-200 bg-surface px-2 py-1.5 text-sm text-gray-700 focus:border-accent focus:outline-none'

export default function UpdatesPage() {
  const t = useTranslations()
  const qc = useQueryClient()

  const { data: parc } = useQuery<{ players: ParcRow[]; total: number; outdated: number; unknown: number }>({
    queryKey: ['ota-parc'], queryFn: () => api.get('/updates/parc').then((r) => r.data),
    refetchInterval: 10000,
  })
  const { data: versions = [] } = useQuery<Version[]>({
    queryKey: ['ota-versions'], queryFn: () => api.get('/updates/versions?track=player').then((r) => r.data),
  })
  // Versions FIRMWARE (BrightSign) : chargées pour libeller les déploiements firmware (sinon la
  // liste des déploiements affichait un `#id_version` brut faute de nom — track=player seul).
  const { data: fwVersions = [] } = useQuery<Version[]>({
    queryKey: ['ota-versions-fw'], queryFn: () => api.get('/updates/versions?track=firmware').then((r) => r.data),
  })
  // Moteur Electron (vitrail#5) : versions `engine` + droit d'exploitant du serveur (seul à les
  // récupérer, promouvoir, déployer ou supprimer — le serveur refuse les autres en 403).
  const { data: engineVersions = [] } = useQuery<Version[]>({
    queryKey: ['ota-versions-engine'], queryFn: () => api.get('/updates/versions?track=engine').then((r) => r.data),
  })
  const { data: operator } = useQuery<{ is_operator: boolean }>({
    queryKey: ['meta-operator'], queryFn: () => api.get('/meta/operator').then((r) => r.data),
  })
  const isOperator = operator?.is_operator === true
  const { data: deployments = [] } = useQuery<Deployment[]>({
    queryKey: ['ota-deployments'], queryFn: () => api.get('/updates/deployments').then((r) => r.data),
    refetchInterval: 10000,
  })
  // Ciblage des déploiements : groupes + sites (les players viennent du parc).
  const { data: groups = [] } = useQuery<{ id_group: number; group_name: string }[]>({
    queryKey: ['player-groups'], queryFn: () => api.get('/player-groups/').then((r) => r.data),
  })
  const { data: sites = [] } = useQuery<{ id_site: number; site_name: string }[]>({
    queryKey: ['sites'], queryFn: () => api.get('/sites').then((r) => r.data),
  })
  const { data: siteGroups = [] } = useQuery<{ id_site_group: number; group_name: string }[]>({
    queryKey: ['site-groups'], queryFn: () => api.get('/site-groups/').then((r) => r.data),
  })

  const players = useMemo(() => parc?.players ?? [], [parc])
  // Options de filtre (valeurs distinctes triées) dérivées de l'inventaire.
  const uniq = (arr: (string | null)[]) => [...new Set(arr.filter((x): x is string => !!x))].sort()
  const osOpts = useMemo(() => uniq(players.map((p) => p.player_os)), [players])
  const verOpts = useMemo(() => uniq(players.map((p) => p.current_version)), [players])
  const siteOpts = useMemo(() => uniq(players.map((p) => p.site)), [players])
  const groupOpts = useMemo(() => uniq(players.flatMap((p) => p.groups)), [players])

  const [fOs, setFOs] = useState('')
  const [fVer, setFVer] = useState('')
  const [fState, setFState] = useState('')   // '' | 'up_to_date' | 'outdated' | 'unknown'
  const [fSite, setFSite] = useState('')
  const [fGroup, setFGroup] = useState('')
  const [q, setQ] = useState('')

  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase()
    return players.filter((p) =>
      (!fOs || p.player_os === fOs) &&
      (!fVer || p.current_version === fVer) &&
      (!fState || p.update_state === fState) &&
      (!fSite || p.site === fSite) &&
      (!fGroup || p.groups.includes(fGroup)) &&
      (!s || p.player_name.toLowerCase().includes(s) ||
        (p.current_version || '').toLowerCase().includes(s) ||
        (p.site || '').toLowerCase().includes(s) ||
        p.groups.some((g) => g.toLowerCase().includes(s))),
    )
  }, [players, fOs, fVer, fState, fSite, fGroup, q])

  // Enregistrer une version (canal beta = version de TEST, invisible du « latest » du parc)
  const [form, setForm] = useState({ version: '', download_url: '', release_notes: '', model_family: '', channel: 'stable' })
  const addVersion = useMutation({
    mutationFn: () => api.post('/updates/versions', { track: 'player', ...form }),
    onSuccess: () => { setForm({ version: '', download_url: '', release_notes: '', model_family: '', channel: 'stable' }); qc.invalidateQueries({ queryKey: ['ota-versions'] }); qc.invalidateQueries({ queryKey: ['ota-parc'] }) },
  })

  // Import d'une version player installée sur l'appareil (linux/rpi/windows/android) : URL ou fichier.
  const [importOpen, setImportOpen] = useState(false)
  const [impMode, setImpMode] = useState<'url' | 'file'>('url')
  const [impUrl, setImpUrl] = useState('')
  const [impFile, setImpFile] = useState<File | null>(null)
  const [impPlatform, setImpPlatform] = useState('')   // '' = détection auto
  const [impPreview, setImpPreview] = useState<BundleImportResult | null>(null)
  const [impError, setImpError] = useState('')
  const impReset = () => { setImpUrl(''); setImpFile(null); setImpPlatform(''); setImpPreview(null); setImpError('') }
  const doImport = useMutation({
    mutationFn: async (dry: boolean) => {
      const fd = new FormData()
      if (impMode === 'file' && impFile) fd.append('file', impFile)
      if (impMode === 'url' && impUrl.trim()) fd.append('url', impUrl.trim())
      if (impPlatform) fd.append('platform', impPlatform)
      if (dry) fd.append('dry_run', 'true')
      return (await api.post('/updates/versions/import', fd)).data as BundleImportResult
    },
    onSuccess: (data) => {
      setImpError('')
      if (data.dry_run) { setImpPreview(data); return }
      impReset(); setImportOpen(false)
      qc.invalidateQueries({ queryKey: ['ota-versions'] })
      qc.invalidateQueries({ queryKey: ['ota-parc'] })
    },
    onError: (e) => setImpError(
      (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || t('common.error')),
  })
  const impCanSubmit = impMode === 'url' ? impUrl.trim().length > 0 : impFile != null
  // Workflow canary : promotion beta → stable une fois validée sur le groupe de test.
  const promote = useMutation({
    mutationFn: (id: number) => api.post(`/updates/versions/${id}/promote`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['ota-versions'] }); qc.invalidateQueries({ queryKey: ['ota-parc'] }) },
  })
  const [verErr, setVerErr] = useState('')
  // Retour visible d'un déploiement créé (retour Ed 14/11 : le bouton ne montrait rien, 4 clics =
  // 4 déploiements). total = 0 ⇒ avertissement : un déploiement qui ne vise personne était muet.
  const [depMsg, setDepMsg] = useState<{ id: number; total: number } | null>(null)
  const onDeployed = (r: { data: { id_deployment: number; total_targets: number } }) => {
    setVerErr('')
    setDepMsg({ id: r.data.id_deployment, total: r.data.total_targets })
    qc.invalidateQueries({ queryKey: ['ota-deployments'] })
  }
  const depMsgEl = depMsg && (
    <p data-testid="deploy-msg" className={`text-[11px] ${depMsg.total > 0 ? 'text-green-700 dark:text-green-300' : 'text-amber-700 dark:text-amber-300'}`}>
      {depMsg.total > 0 ? t('updates.deploy_started', { id: depMsg.id, n: depMsg.total })
        : t('updates.deploy_no_target', { id: depMsg.id })}</p>
  )
  // Decision 2 « canal unique » : BOS minimum d'un renderer BrightSign, editable EN LIGNE
  // (le flux reel cree la ligne par served-versions/register, sans minimum).
  const [fwEdit, setFwEdit] = useState<{ id: number; value: string } | null>(null)
  const setMinFw = useMutation({
    mutationFn: ({ id, value }: { id: number; value: string | null }) =>
      api.patch(`/updates/versions/${id}`, { min_firmware_version: value }),
    onSuccess: () => { setVerErr(''); setFwEdit(null); qc.invalidateQueries({ queryKey: ['ota-versions'] }) },
    onError: (e) => setVerErr(
      (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      || t('common.error'),
    ),
  })
  const delVersion = useMutation({
    mutationFn: (id: number) => api.delete(`/updates/versions/${id}`),
    onSuccess: () => { setVerErr(''); qc.invalidateQueries({ queryKey: ['ota-versions'] }) },
    onError: (e) => setVerErr(
      (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      || t('updates.delete_error'),
    ),
  })
  // Slice 2c — options de déploiement : fenêtres horaires (heure locale du site) + forçage
  // + CIBLE (org entière / site / groupe / player — workflow canary).
  const [depOpts, setDepOpts] = useState({ download_window: '', install_window: '', force: false })
  const [depTarget, setDepTarget] = useState<{ type: string; id: number | null }>({ type: 'org', id: null })
  const targetReady = depTarget.type === 'org' || depTarget.id != null
  // Corps d'un déploiement, partagé par le catalogue de la page et le tiroir moteur.
  const postDeployment = (id_version: number, target: { type: string; id: number | null },
    opts: { download_window: string; install_window: string; force: boolean }) => {
    const sp: Record<string, unknown> = {}
    if (opts.download_window.trim()) sp.download_window = opts.download_window.trim()
    if (opts.install_window.trim()) sp.install_window = opts.install_window.trim()
    if (opts.force) sp.force = true
    const strategy = (sp.download_window || sp.install_window) ? 'scheduled' : 'immediate'
    const targets = target.type === 'org'
      ? [{ target_type: 'org' }]
      : [{ target_type: target.type, target_id: target.id }]
    return api.post('/updates/deployments', { id_version, strategy, strategy_params: sp, targets })
  }
  const deploy = useMutation({
    mutationFn: (id_version: number) => postDeployment(id_version, depTarget, depOpts),
    onMutate: () => setDepMsg(null),
    // 422 = FirmwareRequired (decision 2 : importer le .bsfw puis rejouer) — un deploiement
    // refuse SANS message laisserait croire que rien ne s'est passe.
    onSuccess: onDeployed,
    onError: (e) => setVerErr(
      (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      || t('common.error'),
    ),
  })
  const cancel = useMutation({
    mutationFn: (id: number) => api.post(`/updates/deployments/${id}/cancel`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ota-deployments'] }),
  })
  const [depErr, setDepErr] = useState('')
  const pause = useMutation({
    mutationFn: (id: number) => api.post(`/updates/deployments/${id}/pause`),
    onSuccess: () => { setDepErr(''); qc.invalidateQueries({ queryKey: ['ota-deployments'] }) },
    onError: () => setDepErr(t('common.error')),
  })
  const resume = useMutation({
    mutationFn: (id: number) => api.post(`/updates/deployments/${id}/resume`),
    onSuccess: () => { setDepErr(''); qc.invalidateQueries({ queryKey: ['ota-deployments'] }) },
    onError: () => setDepErr(t('common.error')),
  })
  const delDeployment = useMutation({
    mutationFn: (id: number) => api.delete(`/updates/deployments/${id}`),
    onSuccess: () => { setDepErr(''); qc.invalidateQueries({ queryKey: ['ota-deployments'] }) },
    onError: () => setDepErr(t('updates.dep_delete_error')),
  })

  // Catalogue : recherche + filtre plateforme + nb de players actuellement sur chaque version.
  const [vq, setVq] = useState('')
  const [vOs, setVOs] = useState('')   // '' = toutes | 'generic' | model_family
  // ⚠️ Options dérivées du CATALOGUE (model_family : linux, rpi…), pas du parc
  // (player_os : linux-x64…) — les deux vocabulaires ne se recoupent jamais, un
  // filtre bâti sur le parc ne matchait aucune ligne (défaut vu par Ed, 27/07).
  const famOpts = useMemo(
    () => uniq(versions.map((v) => v.model_family).filter((f): f is string => !!f)),
    [versions])
  const filteredVersions = useMemo(() => {
    const s = vq.trim().toLowerCase()
    return versions.filter((v) =>
      (!vOs || (vOs === 'generic' ? !v.model_family : v.model_family === vOs)) &&
      (!s || v.version.toLowerCase().includes(s) || v.channel.toLowerCase().includes(s) ||
        (v.model_family || '').toLowerCase().includes(s) ||
        (v.release_notes || '').toLowerCase().includes(s)),
    )
  }, [versions, vq, vOs])
  const playersOnVersion = useMemo(() => {
    const m = new Map<number, number>()
    for (const v of versions) {
      m.set(v.id_version, players.filter((p) =>
        p.current_version === v.version &&
        (!v.model_family || p.player_os === v.model_family)).length)
    }
    return m
  }, [versions, players])

  // Libellés des lignes de déploiement : version déployée + nom du player cible.
  // Libellés des déploiements : player + firmware (le firmware porte model_family → « 9.1.140 · malibu »).
  const verById = useMemo(() => new Map(
    [...versions, ...fwVersions, ...engineVersions].map((v) => [v.id_version,
      v.track === 'engine' ? `Electron ${v.version} · ${v.model_family}`
        : v.model_family && v.track === 'firmware' ? `${v.version} · ${v.model_family}` : v.version]),
  ), [versions, fwVersions, engineVersions])
  const playerById = useMemo(() => new Map(players.map((p) => [p.id_player, p.player_name])), [players])
  const fmtDate = (iso: string) =>
    new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })

  const [openDep, setOpenDep] = useState<number | null>(null)
  const { data: detail } = useQuery<Deployment & { targets: DeployTarget[] }>({
    queryKey: ['ota-deployment', openDep], enabled: openDep != null,
    queryFn: () => api.get(`/updates/deployments/${openDep}`).then((r) => r.data),
    refetchInterval: openDep != null ? 5000 : false,
  })

  // ── Moteur Electron (vitrail#5) ─────────────────────────────────────────────
  const [engineOpen, setEngineOpen] = useState(false)
  const [engVersion, setEngVersion] = useState('')
  const [engFamilies, setEngFamilies] = useState<string[]>([...ENGINE_FAMILIES])
  const [engJobs, setEngJobs] = useState<EngineJob[]>([])
  const [engErr, setEngErr] = useState('')
  const errDetail = (e: unknown) =>
    (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || t('common.error')
  const fetchEngine = useMutation({
    mutationFn: () => api.post('/updates/engines/fetch', { version: engVersion.trim(), families: engFamilies })
      .then((r) => r.data as EngineJob[]),
    onSuccess: (jobs) => { setEngErr(''); setEngJobs(jobs) },
    onError: (e) => setEngErr(errDetail(e)),
  })
  const jobsRunning = engJobs.some((j) => j.state !== 'done' && j.state !== 'error')
  useQuery({
    queryKey: ['engine-jobs', engJobs.map((j) => j.job_id).join(',')],
    enabled: engineOpen && engJobs.length > 0 && jobsRunning,
    refetchInterval: 3000,
    queryFn: async () => {
      const next = await Promise.all(engJobs.map((j) => (j.state === 'done' || j.state === 'error')
        ? Promise.resolve(j)
        : api.get(`/updates/engines/jobs/${j.job_id}`).then((r) => ({ ...j, ...r.data }), () => j)))
      setEngJobs(next)
      if (next.some((j) => j.state === 'done')) qc.invalidateQueries({ queryKey: ['ota-versions-engine'] })
      return next
    },
  })
  // Cible + fenêtres DU TIROIR (retour Ed 14/11 : choisir la cible dans le panneau de la page puis
  // ouvrir le tiroir pour déployer n'était « pas pratique du tout »). Défaut = un player : c'est la
  // forme d'un essai sur banc, la seule ouverte à une bêta pour un admin d'org.
  const [engTarget, setEngTarget] = useState<{ type: string; id: number | null }>({ type: 'player', id: null })
  const [engOpts, setEngOpts] = useState({ download_window: '', install_window: '', force: false })
  const engTargetReady = engTarget.type === 'org' || engTarget.id != null
  // Seuls les players Electron remontent une version de moteur : ce sont les seuls concernés.
  const electronPlayers = useMemo(() => players.filter((p) => !!p.electron_version), [players])
  // Player choisi ⇒ seulement les versions de SA plateforme (retour Ed 14/11 : le RPi se voyait
  // proposer les moteurs Windows et x64). Groupe/site/org : toutes — le serveur ne cible, pour
  // chaque version, que les players de sa plateforme.
  const engPlayer = engTarget.type === 'player' && engTarget.id != null
    ? players.find((p) => p.id_player === engTarget.id) ?? null : null
  const visibleEngineVersions = engPlayer
    ? engineVersions.filter((v) => v.model_family === engPlayer.player_os) : engineVersions
  const deployEngine = useMutation({
    mutationFn: (id_version: number) => postDeployment(id_version, engTarget, engOpts),
    onMutate: () => setDepMsg(null),
    onSuccess: onDeployed,
    onError: (e) => setVerErr(
      (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || t('common.error')),
  })
  const engVersionOk = /^\d+\.\d+\.\d+$/.test(engVersion.trim())
  const playersOnEngine = (v: Version) =>
    players.filter((p) => p.electron_version === v.version && p.player_os === v.model_family).length

  const card = 'rounded-lg border border-gray-200 bg-surface p-4 space-y-3 shadow-sf-card'
  const h2 = 'flex items-center gap-2 text-sm font-semibold text-gray-900'
  const th = 'py-1 pr-3 font-medium'

  return (
    <>
      <Topbar title={t('nav.updates')}>
        {/* Visible de tout admin d'org : il déploie sur SES écrans (décision du 14/11). Récupérer,
            promouvoir et supprimer restent à l'exploitant du serveur (catalogue commun). */}
        <Button size="sm" onClick={() => { setEngErr(''); setEngineOpen(true) }}>
          <Cpu className="mr-1 h-4 w-4" />{t('updates.engine_btn')}
        </Button>
        <Button size="sm" onClick={() => { impReset(); setImportOpen(true) }}>
          <Upload className="mr-1 h-4 w-4" />{t('updates.import_title')}
        </Button>
      </Topbar>
      {/* ≥ xl (FHD+) : page verrouillée au viewport — ZÉRO scroll fenêtre, chaque section scrolle
          en interne et se partage la hauteur dynamiquement. < xl : repli scroll fenêtre classique. */}
      <main className="flex-1 overflow-y-auto xl:min-h-0 xl:overflow-hidden">
        <div className="flex w-full flex-col gap-4 px-4 py-4 xl:h-full xl:min-h-0">
          <p className="flex-none text-sm text-gray-500">{t('updates.intro')}</p>
          {/* Panneau « versions servies » RETIRÉ (décision 3, 2026-07-28) : les renderers
              BS/ChromeOS entrent au catalogue par l'IMPORT (zip) ou le colis OCI — le manifeste
              statique n'est plus qu'un repli de résolution, pas une source à déclarer. */}
          {/* Inventaire du parc (héros, pleine largeur).
              🔑 `flex-none` + plafond, PAS `flex-1` : le partage était FIXE (1 pour le parc,
              1,15 pour le bas) quel que soit le contenu ⇒ un parc de 2 écrans gardait ~350 px de
              vide pendant que la liste des versions, sous 365 px de formulaire incompressible,
              tombait à 74 px (une ligne pour 121 versions). Le parc prend donc la hauteur de son
              contenu, plafonnée à 45 % de la page, et REND le reste au bloc du bas. */}
          <div className={card + ' xl:flex xl:min-h-0 xl:max-h-[45%] xl:flex-none xl:flex-col'}>
            <div className={h2}>
              <RefreshCw className="h-4 w-4 text-accent" />{t('updates.parc_title')}
              {parc && <span className="text-xs font-normal text-gray-400">
                · {filtered.length}/{parc.total} · {parc.outdated} {t('updates.outdated')}
                {/* Les « inconnus » ne sont annoncés QUE s'il y en a : sur un serveur au catalogue
                    complet la ligne reste telle quelle. Les taire les ferait passer pour à jour. */}
                {parc.unknown > 0 && <span className="text-amber-600 dark:text-amber-300">
                  {' · '}{parc.unknown} {t('updates.st_unknown')}</span>}</span>}
            </div>

            {/* Barre de filtres combinés */}
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative">
                <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-gray-400" />
                <input className={inp + ' w-56 pl-7'} placeholder={t('updates.search_ph')}
                  value={q} onChange={(e) => setQ(e.target.value)} />
              </div>
              <select className={sel} value={fOs} onChange={(e) => setFOs(e.target.value)}>
                <option value="">OS : {t('updates.f_all')}</option>
                {osOpts.map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
              <select className={sel} value={fVer} onChange={(e) => setFVer(e.target.value)}>
                <option value="">{t('updates.col_version')} : {t('updates.f_all')}</option>
                {verOpts.map((v) => <option key={v} value={v}>{v}</option>)}
              </select>
              <select className={sel} value={fState} onChange={(e) => setFState(e.target.value)}>
                <option value="">{t('updates.col_state')} : {t('updates.f_all')}</option>
                <option value="up_to_date">{t('updates.up_to_date')}</option>
                <option value="outdated">{t('updates.behind')}</option>
                <option value="unknown">{t('updates.st_unknown')}</option>
              </select>
              <select className={sel} value={fSite} onChange={(e) => setFSite(e.target.value)}>
                <option value="">{t('updates.col_site')} : {t('updates.f_all')}</option>
                {siteOpts.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
              <select className={sel} value={fGroup} onChange={(e) => setFGroup(e.target.value)}>
                <option value="">{t('updates.col_groups')} : {t('updates.f_all')}</option>
                {groupOpts.map((g) => <option key={g} value={g}>{g}</option>)}
              </select>
              {(fOs || fVer || fState || fSite || fGroup || q) && (
                <button type="button" onClick={() => { setFOs(''); setFVer(''); setFState(''); setFSite(''); setFGroup(''); setQ('') }}
                  className="text-xs text-accent hover:underline">{t('updates.reset_filters')}</button>
              )}
            </div>

            <div className="max-h-[42vh] overflow-auto pr-2 xl:max-h-none xl:min-h-0 xl:flex-1">
              <table className="w-full text-sm">
                <thead className="sticky top-0 z-10 bg-surface"><tr className="border-b border-gray-100 text-left text-xs text-gray-400">
                  <th className={th}>{t('updates.col_player')}</th><th className={th}>OS</th>
                  <th className={th}>{t('updates.col_site')}</th><th className={th}>{t('updates.col_groups')}</th>
                  <th className={th}>{t('updates.col_version')}</th><th className={th}>{t('updates.col_latest')}</th>
                  <th className={th}>{t('updates.col_electron')}</th>
                  <th className={th}>{t('updates.col_state')}</th>
                </tr></thead>
                <tbody>
                  {filtered.map((p) => (
                    <tr key={p.id_player} className="border-t border-gray-100">
                      <td className="py-1.5 pr-3 text-gray-800">{p.player_name}</td>
                      <td className="pr-3 text-gray-500">{p.player_os}</td>
                      <td className="pr-3 text-gray-500">{p.site || '—'}</td>
                      <td className="pr-3">
                        {p.groups.length === 0 ? <span className="text-gray-400">—</span>
                          : <span className="flex flex-wrap gap-1">
                              {p.groups.map((g) => <span key={g} className="rounded bg-gray-100 dark:bg-surface px-1.5 py-0.5 text-[11px] text-gray-600">{g}</span>)}
                            </span>}
                      </td>
                      <td className="pr-3 text-gray-700">{p.current_version || '—'}</td>
                      <td className="pr-3 text-gray-500">{p.latest_version || '—'}</td>
                      <td className="pr-3 font-mono text-xs text-gray-500">{p.electron_version || '—'}</td>
                      <td>{p.renderer_degraded
                        ? <span title={t('updates.st_degraded_hint', { version: p.renderer_degraded })} className="inline-flex items-center gap-1 text-xs text-red-700 dark:text-red-300"><AlertTriangle className="h-3.5 w-3.5" />{t('updates.st_degraded')}</span>
                        : p.update_state === 'up_to_date'
                        ? <span className="inline-flex items-center gap-1 text-xs text-green-700 dark:text-green-300"><CheckCircle2 className="h-3.5 w-3.5" />{t('updates.up_to_date')}</span>
                        : p.update_state === 'outdated'
                          ? <span className="inline-flex items-center gap-1 text-xs text-amber-700 dark:text-amber-300"><AlertTriangle className="h-3.5 w-3.5" />{t('updates.behind')}</span>
                          : <span title={t('updates.st_unknown_hint')} className="inline-flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400"><HelpCircle className="h-3.5 w-3.5" />{t('updates.st_unknown')}</span>}
                      </td>
                    </tr>
                  ))}
                  {filtered.length === 0 && (
                    <tr><td colSpan={8} className="py-3 text-center text-xs text-gray-400">{t('common.no_results')}</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          <div className="grid grid-cols-1 items-start gap-4 xl:min-h-0 xl:flex-1 xl:grid-cols-2 xl:items-stretch">
            {/* Catalogue de versions. `overflow-y-auto` sur la CARTE = filet : si la fenêtre est
                trop courte pour le formulaire + le plancher de la liste, la carte défile au lieu
                de couper du contenu sans barre (le mode de panne de `/events`, 20/07). */}
            <div className={card + ' xl:flex xl:min-h-0 xl:max-h-full xl:flex-col xl:overflow-y-auto'}>
              <div className={h2}>{t('updates.versions_title')}</div>
              {/* Champs titrés + bouton Add sur la MÊME ligne (retour Ed 2026-07-06) */}
              <div className="grid grid-cols-1 items-end gap-2 sm:grid-cols-[90px_minmax(0,1fr)_130px_110px_auto]">
                <label className="space-y-0.5 text-[11px] text-gray-500">
                  <span>{t('updates.col_version')}</span>
                  <input className={inp} placeholder={t('updates.version_ph')} value={form.version}
                    onChange={(e) => setForm({ ...form, version: e.target.value })} />
                </label>
                <label className="space-y-0.5 text-[11px] text-gray-500">
                  <span>{t('updates.lbl_url')}</span>
                  <input className={inp} placeholder={t('updates.url_ph')} value={form.download_url}
                    onChange={(e) => setForm({ ...form, download_url: e.target.value })} />
                </label>
                <label className="space-y-0.5 text-[11px] text-gray-500">
                  <span>{t('updates.platform')}</span>
                  <select className={sel + ' w-full'} value={form.model_family}
                    onChange={(e) => setForm({ ...form, model_family: e.target.value })}>
                    <option value="">{t('updates.platform_generic')}</option>
                    {osOpts.map((o) => <option key={o} value={o}>{o}</option>)}
                  </select>
                </label>
                <label className="space-y-0.5 text-[11px] text-gray-500">
                  <span>{t('updates.lbl_channel')}</span>
                  <select className={sel + ' w-full'} value={form.channel}
                    onChange={(e) => setForm({ ...form, channel: e.target.value })}>
                    <option value="stable">{t('updates.ch_stable')}</option>
                    <option value="beta">{t('updates.ch_beta')}</option>
                  </select>
                </label>
                <Button size="sm" loading={addVersion.isPending} disabled={!/^\d+\.\d+\.\d+/.test(form.version)}
                  onClick={() => addVersion.mutate()}><Plus className="mr-1 h-4 w-4" />{t('updates.add_version')}</Button>
              </div>
              {addVersion.isError && <p className="text-xs text-danger">{t('common.error')}</p>}

              {/* Slice 2c — options appliquées aux déploiements ci-dessous (fenêtres heure locale du site)
                  + cible (org / site / groupe / player — canary) */}
              <div className="rounded border border-gray-100 bg-gray-50 p-2 dark:bg-surface">
                <div className="mb-1.5 text-[11px] font-medium text-gray-500">{t('updates.deploy_options')}</div>
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                  <label className="space-y-0.5 text-[11px] text-gray-500">
                    <span>{t('updates.download_window')}</span>
                    <input className={inp} placeholder="HH:MM-HH:MM" value={depOpts.download_window}
                      onChange={(e) => setDepOpts({ ...depOpts, download_window: e.target.value })} />
                  </label>
                  <label className="space-y-0.5 text-[11px] text-gray-500">
                    <span>{t('updates.install_window')}</span>
                    <input className={inp} placeholder="HH:MM-HH:MM" value={depOpts.install_window}
                      onChange={(e) => setDepOpts({ ...depOpts, install_window: e.target.value })} />
                  </label>
                  <label className="flex items-end gap-1.5 pb-1.5 text-xs text-gray-600">
                    <input type="checkbox" checked={depOpts.force}
                      onChange={(e) => setDepOpts({ ...depOpts, force: e.target.checked })} />
                    {t('updates.force')}
                  </label>
                </div>
                <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
                  <label className="space-y-0.5 text-[11px] text-gray-500">
                    <span>{t('updates.target')}</span>
                    <select className={sel + ' w-full'} value={depTarget.type}
                      onChange={(e) => setDepTarget({ type: e.target.value, id: null })}>
                      <option value="org">{t('updates.target_org')}</option>
                      <option value="site">{t('updates.target_site')}</option>
                      <option value="site_group">{t('updates.target_site_group')}</option>
                      <option value="group">{t('updates.target_group')}</option>
                      <option value="player">{t('updates.target_player')}</option>
                    </select>
                  </label>
                  {depTarget.type !== 'org' && (
                    <label className="space-y-0.5 text-[11px] text-gray-500">
                      <span>{t(`updates.target_${depTarget.type}`)}</span>
                      <select className={sel + ' w-full'} value={depTarget.id ?? ''}
                        onChange={(e) => setDepTarget({ ...depTarget, id: e.target.value ? Number(e.target.value) : null })}>
                        <option value="">{t('updates.target_pick')}</option>
                        {depTarget.type === 'site' && sites.map((s) => (
                          <option key={s.id_site} value={s.id_site}>{s.site_name}</option>))}
                        {depTarget.type === 'site_group' && siteGroups.map((g) => (
                          <option key={g.id_site_group} value={g.id_site_group}>{g.group_name}</option>))}
                        {depTarget.type === 'group' && groups.map((g) => (
                          <option key={g.id_group} value={g.id_group}>{g.group_name}</option>))}
                        {depTarget.type === 'player' && players.map((p) => (
                          <option key={p.id_player} value={p.id_player}>{p.player_name} · {p.player_os}</option>))}
                      </select>
                    </label>
                  )}
                </div>
                <p className="mt-1 text-[11px] text-gray-400">{t('updates.windows_hint')}</p>
              </div>
              {/* Recherche + filtre plateforme du catalogue */}
              <div className="flex flex-wrap items-center gap-2">
                <div className="relative">
                  <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-gray-400" />
                  <input className={inp + ' w-44 pl-7'} placeholder={t('updates.search_versions_ph')}
                    value={vq} onChange={(e) => setVq(e.target.value)} />
                </div>
                <select className={sel} value={vOs} onChange={(e) => setVOs(e.target.value)}>
                  <option value="">{t('updates.platform')} : {t('updates.f_all')}</option>
                  <option value="generic">{t('updates.platform_generic')}</option>
                  {famOpts.map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
                {(vq || vOs) && (
                  <button type="button" onClick={() => { setVq(''); setVOs('') }}
                    className="text-xs text-accent hover:underline">{t('updates.reset_filters')}</button>
                )}
                <span className="ml-auto text-xs text-gray-400">{filteredVersions.length}/{versions.length}</span>
              </div>
              {/* pr-2 : la scrollbar ne recouvre plus les poubelles de suppression */}
              {/* Plancher : sous 8 rem la liste ne montre plus qu'une ligne et n'a plus d'usage.
                  Elle peut donc pousser la carte à défiler — c'est le filet ci-dessus. */}
              <ul className="max-h-[34vh] divide-y divide-gray-100 overflow-y-auto pr-2 xl:max-h-none xl:min-h-[8rem] xl:flex-1">
                {filteredVersions.map((v) => (
                  <li key={v.id_version} className="flex items-center gap-2 py-1.5 text-sm">
                    <span className="font-medium text-gray-800">{v.version}</span>
                    <span className={`text-xs ${v.channel === 'beta'
                      ? 'rounded bg-amber-100 px-1.5 py-0.5 font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-200'
                      : 'text-gray-400'}`}>
                      {v.channel}{v.model_family ? ` · ${v.model_family}` : ''}
                    </span>
                    <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-600 dark:bg-surface"
                      title={t('updates.players_on_hint')}>
                      {t('updates.players_on', { n: playersOnVersion.get(v.id_version) ?? 0 })}
                    </span>
                    {v.model_family === 'brightsign' && (fwEdit?.id === v.id_version ? (
                      <span className="flex items-center gap-1">
                        <input className={inp + ' w-24'} placeholder="9.1.132" autoFocus
                          value={fwEdit.value}
                          onChange={(e) => setFwEdit({ id: v.id_version, value: e.target.value })}
                          onKeyDown={(e) => { if (e.key === 'Enter') setMinFw.mutate({ id: v.id_version, value: fwEdit.value.trim() || null }) }} />
                        <button type="button" className="text-[11px] text-accent hover:underline"
                          onClick={() => setMinFw.mutate({ id: v.id_version, value: fwEdit.value.trim() || null })}>
                          {t('common.save')}</button>
                        <button type="button" className="text-[11px] text-gray-400 hover:underline"
                          onClick={() => setFwEdit(null)}>{t('common.cancel')}</button>
                      </span>
                    ) : (
                      <button type="button" title={t('updates.min_fw_hint')}
                        onClick={() => setFwEdit({ id: v.id_version, value: v.min_firmware_version ?? '' })}
                        className={v.min_firmware_version
                          ? 'rounded border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[11px] text-accent'
                          : 'rounded border border-dashed border-gray-300 px-1.5 py-0.5 text-[11px] text-gray-400 hover:text-gray-600'}>
                        {v.min_firmware_version
                          ? t('updates.min_fw_badge', { v: v.min_firmware_version })
                          : t('updates.min_fw_set')}
                      </button>
                    ))}
                    <span className="flex-1" />
                    {v.channel === 'beta' && (
                      <Button size="sm" variant="secondary" loading={promote.isPending}
                        title={t('updates.promote_hint')}
                        onClick={() => promote.mutate(v.id_version)}>
                        <CheckCircle2 className="mr-1 h-3.5 w-3.5" />{t('updates.promote')}</Button>
                    )}
                    <Button size="sm" variant="secondary" loading={deploy.isPending} disabled={!targetReady}
                      title={targetReady ? undefined : t('updates.target_pick')}
                      onClick={() => deploy.mutate(v.id_version)}><Rocket className="mr-1 h-3.5 w-3.5" />{t('updates.deploy')}</Button>
                    <button type="button" onClick={() => delVersion.mutate(v.id_version)} title={t('common.delete')}
                      className="text-gray-400 hover:text-danger"><Trash2 className="h-4 w-4" /></button>
                  </li>
                ))}
                {filteredVersions.length === 0 && <li className="py-2 text-xs text-gray-400">
                  {versions.length === 0 ? t('updates.no_version') : t('common.no_results')}</li>}
              </ul>
              {verErr && <p className="text-[11px] text-danger">{verErr}</p>}
              {depMsgEl}
              <p className="text-[11px] text-gray-400">{t('updates.deploy_hint')}</p>
            </div>

            {/* Déploiements */}
            <div className={card + ' xl:flex xl:min-h-0 xl:max-h-full xl:flex-col'}>
              <div className={h2}>{t('updates.deployments_title')}</div>
              {deployments.length === 0 ? (
                <p className="text-xs text-gray-400">{t('updates.no_deployment')}</p>
              ) : (
                <div className="max-h-[42vh] space-y-2 overflow-y-auto pr-2 xl:max-h-none xl:min-h-0 xl:flex-1">
                {deployments.map((d) => (
                <div key={d.id_deployment} className="rounded-lg border border-gray-100">
                  <div className="flex items-center gap-2 px-3 py-2 text-sm">
                    <ProgressRing done={d.success_count} total={d.total_targets}
                      label={t('updates.progress_title', { done: d.success_count, total: d.total_targets })} />
                    <StatusBadge s={d.status} />
                    <span className="font-medium text-gray-800">
                      {verById.get(d.id_version) ?? `#${d.id_version}`}
                    </span>
                    <span className="text-gray-600">{t('updates.targets_n', { n: d.total_targets })} · {d.strategy}</span>
                    <span className="text-xs text-gray-400">{fmtDate(d.created_at)}</span>
                    <span className="text-xs text-green-600">{d.success_count} ✓</span>
                    {d.failed_count > 0 && <span className="text-xs text-danger">{d.failed_count} ✗</span>}
                    <span className="flex-1" />
                    <button type="button" onClick={() => setOpenDep(openDep === d.id_deployment ? null : d.id_deployment)}
                      className="text-xs text-accent hover:underline">{t('updates.details')}</button>
                    {['pending', 'in_progress'].includes(d.status) && (
                      <button type="button" onClick={() => pause.mutate(d.id_deployment)}
                        title={t('updates.pause')}
                        className="inline-flex items-center gap-1 text-xs text-gray-500 hover:text-accent">
                        <Pause className="h-3.5 w-3.5" />{t('updates.pause')}</button>
                    )}
                    {d.status === 'paused' && (
                      <button type="button" onClick={() => resume.mutate(d.id_deployment)}
                        title={t('updates.resume')}
                        className="inline-flex items-center gap-1 text-xs text-accent hover:underline">
                        <Play className="h-3.5 w-3.5" />{t('updates.resume')}</button>
                    )}
                    {['pending', 'in_progress', 'paused'].includes(d.status) && (
                      <button type="button" onClick={() => cancel.mutate(d.id_deployment)}
                        className="text-xs text-gray-400 hover:text-danger">{t('updates.cancel')}</button>
                    )}
                    {['cancelled', 'paused', 'completed', 'failed'].includes(d.status) && (
                      <button type="button" onClick={() => delDeployment.mutate(d.id_deployment)}
                        title={t('common.delete')}
                        className="text-gray-400 hover:text-danger"><Trash2 className="h-4 w-4" /></button>
                    )}
                  </div>
                  {openDep === d.id_deployment && detail && (
                    <ul className="border-t border-gray-100 px-3 py-1.5 text-xs">
                      {detail.targets.map((tg) => (
                        <li key={tg.id_target} className="flex items-center gap-2 py-0.5">
                          <StatusBadge s={tg.status} small />
                          <span className="text-gray-600">
                            {playerById.get(tg.id_player) ?? `#${tg.id_player}`}
                          </span>
                          <span className="text-gray-400">{tg.previous_version || '—'}</span>
                          {tg.error_message && <span className="text-danger">{tg.error_message}</span>}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
                </div>
              )}
              {depErr && <p className="text-[11px] text-danger">{depErr}</p>}
            </div>
          </div>
        </div>
      </main>

      <Drawer open={engineOpen} onClose={() => setEngineOpen(false)} title={t('updates.engine_title')}>
        <div className="space-y-4">
          <p className="text-xs text-gray-400">{t('updates.engine_intro')}</p>
          {!isOperator && <p className="rounded bg-gray-50 px-2 py-1.5 text-[11px] text-gray-500 dark:bg-surface" data-testid="engine-not-operator">{t('updates.engine_not_operator')}</p>}
          {isOperator && <div className="space-y-2 rounded-lg border border-gray-100 p-3">
            <div className="text-xs font-medium text-gray-600">{t('updates.engine_fetch_title')}</div>
            <label className="block space-y-0.5 text-[11px] text-gray-500">
              <span>{t('updates.engine_version')}</span>
              <input className={inp} placeholder="42.11.18" value={engVersion} data-testid="engine-version"
                onChange={(e) => setEngVersion(e.target.value)} />
            </label>
            <div className="flex flex-wrap gap-3">
              {ENGINE_FAMILIES.map((f) => (
                <label key={f} className="flex items-center gap-1.5 text-xs text-gray-600">
                  <input type="checkbox" checked={engFamilies.includes(f)}
                    onChange={(e) => setEngFamilies(e.target.checked ? [...engFamilies, f] : engFamilies.filter((x) => x !== f))} />
                  {t(`updates.engine_family_${f.replace('-', '_')}`)}
                </label>
              ))}
            </div>
            <Button size="sm" loading={fetchEngine.isPending} disabled={!engVersionOk || engFamilies.length === 0 || jobsRunning}
              onClick={() => fetchEngine.mutate()}>{t('updates.engine_fetch')}</Button>
            {engJobs.length > 0 && (
              <ul className="space-y-1 text-xs" data-testid="engine-jobs">
                {engJobs.map((j) => (
                  <li key={j.job_id} className="flex items-center gap-2">
                    {j.state === 'done' ? <CheckCircle2 className="h-3.5 w-3.5 text-success" />
                      : j.state === 'error' ? <AlertTriangle className="h-3.5 w-3.5 text-danger" />
                        : <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" />}
                    <span className="text-gray-700">{j.version} · {j.family}</span>
                    <span className={j.state === 'error' ? 'text-danger' : 'text-gray-500'}>
                      {j.state === 'error' ? (j.error || t('common.error'))
                        : j.state === 'done' && j.created === false ? t('updates.engine_job_already')
                          : t(`updates.engine_job_${j.state || 'queued'}`)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {engErr && <p className="text-[11px] text-danger">{engErr}</p>}
          </div>}

          <div className="space-y-2">
            <div className="text-xs font-medium text-gray-600">{t('updates.engine_versions_title')}</div>
            <div className="space-y-2 rounded border border-gray-100 bg-gray-50 p-2 dark:bg-surface" data-testid="engine-deploy-form">
              <div className="text-[11px] font-medium text-gray-500">{t('updates.engine_deploy_on')}</div>
              <div className="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-2">
                <select className={sel + ' w-full'} value={engTarget.type} data-testid="engine-target-type"
                  aria-label={t('updates.target')}
                  onChange={(e) => { setDepMsg(null); setEngTarget({ type: e.target.value, id: null }) }}>
                  <option value="player">{t('updates.target_player')}</option>
                  <option value="group">{t('updates.target_group')}</option>
                  <option value="site">{t('updates.target_site')}</option>
                  <option value="site_group">{t('updates.target_site_group')}</option>
                  <option value="org">{t('updates.target_org')}</option>
                </select>
                {engTarget.type !== 'org' && (
                  <select className={sel + ' w-full'} value={engTarget.id ?? ''} data-testid="engine-target-id"
                    aria-label={t(`updates.target_${engTarget.type}`)}
                    onChange={(e) => setEngTarget({ ...engTarget, id: e.target.value ? Number(e.target.value) : null })}>
                    <option value="">{t('updates.target_pick')}</option>
                    {engTarget.type === 'player' && electronPlayers.map((p) => (
                      <option key={p.id_player} value={p.id_player}>{p.player_name} · {p.player_os} · Electron {p.electron_version}</option>))}
                    {engTarget.type === 'group' && groups.map((g) => (
                      <option key={g.id_group} value={g.id_group}>{g.group_name}</option>))}
                    {engTarget.type === 'site' && sites.map((x) => (
                      <option key={x.id_site} value={x.id_site}>{x.site_name}</option>))}
                    {engTarget.type === 'site_group' && siteGroups.map((g) => (
                      <option key={g.id_site_group} value={g.id_site_group}>{g.group_name}</option>))}
                  </select>
                )}
              </div>
              <div className="grid grid-cols-2 items-end gap-2">
                <label className="space-y-0.5 text-[11px] text-gray-500">
                  <span>{t('updates.download_window')}</span>
                  <input className={inp} placeholder="HH:MM-HH:MM" value={engOpts.download_window}
                    onChange={(e) => setEngOpts({ ...engOpts, download_window: e.target.value })} />
                </label>
                <label className="space-y-0.5 text-[11px] text-gray-500">
                  <span>{t('updates.install_window')}</span>
                  <input className={inp} placeholder="HH:MM-HH:MM" value={engOpts.install_window}
                    onChange={(e) => setEngOpts({ ...engOpts, install_window: e.target.value })} />
                </label>
                <label className="col-span-2 flex items-center gap-1.5 text-[11px] text-gray-600">
                  <input type="checkbox" checked={engOpts.force}
                    onChange={(e) => setEngOpts({ ...engOpts, force: e.target.checked })} />
                  {t('updates.force')}
                </label>
              </div>
            </div>
            <ul className="divide-y divide-gray-100" data-testid="engine-versions">
              {visibleEngineVersions.map((v) => (
                <li key={v.id_version} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                  <span className="font-medium text-gray-800">{v.version}</span>
                  <span className={`whitespace-nowrap text-xs ${v.channel === 'beta'
                    ? 'rounded bg-amber-100 px-1.5 py-0.5 font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-200'
                    : 'text-gray-400'}`}>{v.channel} · {v.model_family}</span>
                  <span className="whitespace-nowrap rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-600 dark:bg-surface">
                    {t('updates.players_on', { n: playersOnEngine(v) })}</span>
                  {/* Actions sur leur propre ligne : le tiroir est étroit (capture du 14/11). */}
                  <span className="flex w-full items-center justify-end gap-2 whitespace-nowrap">
                  {v.channel === 'beta' && isOperator && (
                    <Button size="sm" variant="secondary" loading={promote.isPending} title={t('updates.promote_hint')}
                      onClick={() => promote.mutate(v.id_version, { onSuccess: () => qc.invalidateQueries({ queryKey: ['ota-versions-engine'] }) })}>
                      <CheckCircle2 className="mr-1 h-3.5 w-3.5" />{t('updates.promote')}</Button>
                  )}
                  {(() => {
                    // Bêta = banc : un seul player tant que l'exploitant ne l'a pas promue.
                    const betaBlocked = v.channel === 'beta' && !isOperator && engTarget.type !== 'player'
                    const installed = engPlayer?.electron_version === v.version
                    return (
                      <Button size="sm" variant="secondary" loading={deployEngine.isPending}
                        disabled={!engTargetReady || betaBlocked || installed}
                        title={installed ? t('updates.engine_installed') : betaBlocked ? t('updates.engine_beta_one_player')
                          : engTargetReady ? undefined : t('updates.target_pick')}
                        onClick={() => deployEngine.mutate(v.id_version)}><Rocket className="mr-1 h-3.5 w-3.5" />{t('updates.deploy')}</Button>
                    )
                  })()}
                  {isOperator && (
                    <button type="button" title={t('common.delete')} className="text-gray-400 hover:text-danger"
                      onClick={() => delVersion.mutate(v.id_version, { onSuccess: () => qc.invalidateQueries({ queryKey: ['ota-versions-engine'] }) })}>
                      <Trash2 className="h-4 w-4" /></button>
                  )}
                  </span>
                  {/* La raison d'un bouton grisé, SUR la ligne (retour Ed 14/11 : un seul message en
                      bas du tiroir passait inaperçu, tout semblait grisé sans raison). */}
                  {engPlayer?.electron_version === v.version ? (
                    <span className="w-full text-right text-[11px] text-gray-500" data-testid="engine-row-reason">{t('updates.engine_installed')}</span>
                  ) : v.channel === 'beta' && !isOperator && engTarget.type !== 'player' ? (
                    <span className="w-full text-right text-[11px] text-amber-700 dark:text-amber-300" data-testid="engine-row-reason">{t('updates.engine_beta_row')}</span>
                  ) : null}
                </li>
              ))}
              {visibleEngineVersions.length === 0 && <li className="py-2 text-xs text-gray-400">
                {engineVersions.length === 0 ? t('updates.engine_no_version')
                  : t('updates.engine_no_version_for', { os: engPlayer?.player_os ?? '' })}</li>}
            </ul>
            {verErr && <p className="text-[11px] text-danger">{verErr}</p>}
              {depMsgEl}
            <p className="text-[11px] text-gray-400">{t('updates.engine_deploy_hint')}</p>
          </div>
        </div>
      </Drawer>

      <Drawer open={importOpen} onClose={() => setImportOpen(false)} title={t('updates.import_title')}>
        <div className="space-y-4">
          <p className="text-xs text-gray-400">{t('updates.import_hint')}</p>
          <div className="flex gap-1 rounded-lg bg-gray-50 p-1 dark:bg-surface">
            {(['url', 'file'] as const).map((m) => (
              <button key={m} type="button"
                onClick={() => { setImpMode(m); setImpPreview(null); setImpError('') }}
                className={`flex-1 rounded-md px-3 py-1.5 text-sm ${impMode === m ? 'bg-accent text-white' : 'text-gray-600 hover:text-gray-900'}`}>
                {t(m === 'url' ? 'updates.import_by_url' : 'updates.import_by_file')}
              </button>
            ))}
          </div>
          {impMode === 'url' ? (
            <label className="block space-y-1 text-[11px] text-gray-500">
              <span>{t('updates.import_url_label')}</span>
              <input className={inp} placeholder="https://…/signflow-player-linux-1.5.15.tar.gz"
                value={impUrl} onChange={(e) => { setImpUrl(e.target.value); setImpPreview(null) }} />
            </label>
          ) : (
            <label className="block space-y-1 text-[11px] text-gray-500">
              <span>{t('updates.import_file_label')}</span>
              <input type="file" accept=".tar.gz,.tgz,.apk,application/gzip,application/vnd.android.package-archive"
                onChange={(e) => { setImpFile(e.target.files?.[0] ?? null); setImpPreview(null) }}
                className="block w-full text-sm text-gray-600 file:mr-3 file:rounded-md file:border-0 file:bg-accent file:px-3 file:py-1.5 file:text-white" />
            </label>
          )}
          <label className="block space-y-1 text-[11px] text-gray-500">
            <span>{t('updates.platform')}</span>
            <select className={sel + ' w-full'} value={impPlatform}
              onChange={(e) => { setImpPlatform(e.target.value); setImpPreview(null) }}>
              <option value="">{t('updates.import_platform_auto')}</option>
              {['linux', 'rpi', 'windows', 'android', 'chromeos', 'brightsign'].map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          </label>
          {impPreview && (
            <div className="rounded-lg border border-gray-100 bg-gray-50 p-3 text-sm dark:bg-surface">
              <div className="flex items-center gap-2">
                <span className="font-medium text-gray-800">{impPreview.platform}</span>
                <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-600 dark:bg-[#1A1D2B]">{impPreview.version}</span>
                <span className="text-xs text-gray-400">{Math.max(1, Math.round(impPreview.size_bytes / 1024))} Ko</span>
                {impPreview.files_extracted != null && (
                  <span className="text-xs text-gray-400">
                    · {t('updates.import_renderer_files', { n: impPreview.files_extracted })}</span>
                )}
              </div>
              {impPreview.already_registered
                ? <p className="mt-1 inline-flex items-center gap-1 text-xs text-amber-700 dark:text-amber-300"><AlertTriangle className="h-3.5 w-3.5" />{t('updates.import_already')}</p>
                : <p className="mt-1 text-xs text-gray-400">{t('updates.import_new')}</p>}
            </div>
          )}
          {impError && <p className="text-xs text-danger">{impError}</p>}
          <div className="flex gap-2 pt-1">
            <Button variant="secondary" size="sm" disabled={!impCanSubmit}
              loading={doImport.isPending && doImport.variables === true}
              onClick={() => doImport.mutate(true)}>{t('updates.import_preview')}</Button>
            <Button size="sm" disabled={!impCanSubmit || (impPreview?.already_registered ?? false)}
              loading={doImport.isPending && doImport.variables === false}
              onClick={() => doImport.mutate(false)}><Upload className="mr-1 h-4 w-4" />{t('updates.import_do')}</Button>
          </div>
        </div>
      </Drawer>
    </>
  )
}

/** Anneau de progression d'un déploiement : vert = players à jour, rouge = restant (% au centre).
    Jamais couleur seule : % en texte + badge statut + compteurs ✓/✗ dans la même ligne. */
function ProgressRing({ done, total, label }: { done: number; total: number; label: string }) {
  const R = 15.5
  const C = 2 * Math.PI * R
  const frac = total > 0 ? Math.min(done / total, 1) : 0
  return (
    <svg viewBox="0 0 36 36" className="h-8 w-8 flex-none" role="img" aria-label={label}>
      <title>{label}</title>
      {/* Fond = restant (rouge) ; gris neutre si aucune cible */}
      <circle cx="18" cy="18" r={R} fill="none" strokeWidth="3.5" stroke="currentColor"
        className={total > 0 ? 'text-danger' : 'text-gray-200 dark:text-gray-600'} />
      {/* Arc = fait (vert). Cercle plein si 100 % (un arc à angle début=fin dégénère). */}
      {frac >= 0.9999 ? (
        <circle cx="18" cy="18" r={R} fill="none" strokeWidth="3.5" stroke="currentColor"
          className="text-green-600 dark:text-green-400" />
      ) : frac > 0 ? (
        <circle cx="18" cy="18" r={R} fill="none" strokeWidth="3.5" stroke="currentColor"
          strokeLinecap="round" strokeDasharray={`${frac * C} ${C}`}
          transform="rotate(-90 18 18)" className="text-green-600 dark:text-green-400" />
      ) : null}
      <text x="18" y="18" textAnchor="middle" dominantBaseline="central"
        className="fill-current text-gray-700 dark:text-gray-200" fontSize="9" fontWeight="600">
        {total > 0 ? `${Math.round(frac * 100)}%` : '—'}
      </text>
    </svg>
  )
}

function StatusBadge({ s, small }: { s: string; small?: boolean }) {
  const t = useTranslations('updates')
  const map: Record<string, string> = {
    success: 'text-green-700 dark:text-green-300', failed: 'text-danger', rolled_back: 'text-danger',
    cancelled: 'text-gray-400', paused: 'text-gray-500', pending: 'text-amber-700 dark:text-amber-300',
    queued: 'text-amber-700 dark:text-amber-300', downloading: 'text-accent',
    staged: 'text-indigo-600 dark:text-indigo-300',
    installing: 'text-accent', in_progress: 'text-accent', completed: 'text-green-700 dark:text-green-300',
  }
  // Statut inconnu (nouveau backend, front pas à jour) → libellé brut, pas d'erreur i18n.
  const label = s in map ? t(`st_${s}`) : s
  const cls = `inline-flex items-center gap-1 ${small ? 'text-[11px]' : 'text-xs'} font-medium ${map[s] || 'text-gray-500'}`
  const Icon = s === 'success' || s === 'completed' ? CheckCircle2
    : s === 'failed' || s === 'rolled_back' ? AlertTriangle
    : s === 'staged' ? Clock
    : s === 'paused' ? Pause
    : ['downloading', 'installing', 'in_progress'].includes(s) ? Loader2 : RefreshCw
  return <span className={cls}><Icon className={`h-3 w-3 ${Icon === Loader2 ? 'animate-spin' : ''}`} />{label}</span>
}
