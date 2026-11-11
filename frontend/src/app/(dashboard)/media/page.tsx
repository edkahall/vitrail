'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslations } from 'next-intl'
import { useInfiniteQuery, useQueries, useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Lock, X, AlertTriangle, ExternalLink, CheckSquare, Tag, FolderInput, Globe, Sparkles, MonitorPlay, Folder, Inbox, LayoutGrid, List, SlidersHorizontal } from 'lucide-react'
import Topbar from '@/components/layout/Topbar'
import Can from '@/components/auth/Can'
import Button from '@/components/ui/Button'
import Drawer from '@/components/ui/Drawer'
import SearchInput from '@/components/ui/SearchInput'
import api from '@/lib/api'
import { useCreateShortcut } from '@/lib/useCreateShortcut'
import { useSearchPrefill } from '@/lib/useSearchPrefill'
import MediaGrid from './components/MediaGrid'
import MediaUploader from './components/MediaUploader'
import GenerateDialog from './components/GenerateDialog'
import GenerationJobsBanner from './components/GenerationJobsBanner'
import StockSearch from './components/StockSearch'
import MediaDrawer from './components/MediaDrawer'
import UrlDisplayDrawer from './components/UrlDisplayDrawer'
import FeedDrawer from './components/FeedDrawer'
import EmbargoEditor from './components/EmbargoEditor'
import DeleteMediaDialog from './components/DeleteMediaDialog'
import FolderTree, { type FolderFilter } from './components/FolderTree'
import FolderBreadcrumb from './components/FolderBreadcrumb'
import MediaListView, { type SortKey, type SortDir } from './components/MediaListView'
import MediaViewer, { isViewable } from './components/MediaViewer'
import MediaFilters, {
  EMPTY_FILTERS, applyFilters, countActive, type FilterState,
} from './components/MediaFilters'
import { useCollapse } from '@/lib/useCollapse'
import { useFolderLabel } from '@/lib/folderLabel'
import { TagPicker, useTagNamespaces } from '@/components/tags/TagControls'
import type { AffectedPlaylist, FolderRead, MediaEmbargoResult, MediaListItem, MediaRead } from '@/types/api'

type Tab = 'library' | 'stock'
type MediaType = 'all' | 'image' | 'video' | 'audio' | 'document' | 'html_package' | 'url_display' | 'feed_display' | 'hdmi_input'

// Ordre d'affichage des onglets de type (les types absents de la médiathèque sont masqués)
const TYPE_ORDER: Exclude<MediaType, 'all'>[] = ['image', 'video', 'audio', 'document', 'html_package', 'url_display', 'feed_display', 'hdmi_input']

// Dernière largeur mesurée (vignettes par ligne), gardée entre deux visites de la page
// (navigation interne) : la vue d'accueil repart directement du bon nombre.
let lastPerRow: number | null = null

export default function MediaPage() {
  const t = useTranslations()
  const qc = useQueryClient()
  const [tab, setTab] = useState<Tab>('library')
  // Vue grille ↔ liste (retour Ed : « l'option mode liste des contenus est intéressante »).
  // Le choix est PERSISTÉ : c'est une préférence de travail, pas un état d'écran — la
  // retrouver au retour évite de la reposer à chaque visite.
  const [gridView, toggleView] = useCollapse('sf-media-view', true)
  const viewMode: 'grid' | 'list' = gridView ? 'grid' : 'list'
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({ key: 'date', dir: 'desc' })
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [filters, setFilters] = useState<FilterState>(EMPTY_FILTERS)
  // Visionneuse plein écran (contenus visuels uniquement — cf. `isViewable`).
  const [viewerItem, setViewerItem] = useState<MediaListItem | null>(null)
  const [typeFilter, setTypeFilter] = useState<MediaType>('all')
  const [folderFilter, setFolderFilter] = useState<FolderFilter>('all')
  const [search, setSearch] = useState('')
  const [showAll, setShowAll] = useState(false) // landing « N derniers » vs grille complète
  // Recherche globale → on remet la médiathèque à plat (onglet, type, dossier) pour que le
  // terme cherché ne soit pas masqué par un filtre resté actif, et on force la grille complète.
  useSearchPrefill((q) => {
    setTab('library'); setTypeFilter('all'); setFolderFilter('all')
    setSearch(q); setShowAll(true)
  })
  // Nb de vignettes tenant sur une ligne à la largeur courante (landing dynamique).
  const [perRow, setPerRow] = useState<number | null>(() => lastPerRow)
  const [showUploader, setShowUploader] = useState(false)
  useCreateShortcut(() => setShowUploader(true))
  const [showGenerate, setShowGenerate] = useState(false)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  // Page Web (url_display = média sans fichier) : création / édition in-place dans la médiathèque.
  const [urlDrawer, setUrlDrawer] = useState<{ open: boolean; editId: number | null }>({ open: false, editId: null })
  // Feed (feed_display = média pointeur) : édition in-place dans la médiathèque.
  const [feedDrawer, setFeedDrawer] = useState<{ open: boolean; editId: number | null }>({ open: false, editId: null })
  // Entrée HDMI (hdmi_input = média pointeur) : bascule le player BrightSign XT/4K/XD sur son
  // entrée HDMI physique pendant la durée de l'item ; les players sans entrée ignorent l'item.
  const [showHdmiCreate, setShowHdmiCreate] = useState(false)
  const [hdmiName, setHdmiName] = useState('')

  // Embargo editor state
  const [embargoItem, setEmbargoItem] = useState<MediaListItem | null>(null)
  const [deleteItem, setDeleteItem] = useState<MediaListItem | null>(null)
  const [embargoWarnings, setEmbargoWarnings] = useState<AffectedPlaylist[]>([])

  // Bulk tagging state
  const [selectMode, setSelectMode] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [bulkTagOpen, setBulkTagOpen] = useState(false)
  const [bulkTags, setBulkTags] = useState<Set<number>>(new Set())
  const [bulkMode, setBulkMode] = useState<'add' | 'replace'>('add')
  const { data: namespaces = [] } = useTagNamespaces()

  // Déplacement vers dossier (mode sélection)
  const [moveOpen, setMoveOpen] = useState(false)
  const { data: folders = [] } = useQuery<FolderRead[]>({
    queryKey: ['media-folders'],
    queryFn: () => api.get<FolderRead[]>('/media/folders').then((r) => r.data),
  })
  const { label: folderLabel, path: folderPath } = useFolderLabel()
  const foldersById = useMemo(() => new Map(folders.map((f) => [f.id_folder, f])), [folders])
  const moveMutation = useMutation({
    mutationFn: (id_folder: number | null) =>
      api.post('/media/folders/move-media', { id_media: [...selectedIds], id_folder }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['media'] })
      qc.invalidateQueries({ queryKey: ['media-folders'] })
      setMoveOpen(false)
      setSelectedIds(new Set())
      setSelectMode(false)
    },
  })

  // ── Pagination server-side (fin du « charge tout en limit:2000 ») ────────────
  // Type + recherche + dossier sont filtrés par l'API ; la grille charge des pages de
  // PAGE_SIZE ajoutées via « Afficher plus ». Les compteurs viennent de /media/stats
  // (exacts sur toute la bibliothèque, indépendants de la pagination). Les query keys
  // restent préfixées ['media'] → toutes les invalidations existantes (upload, drawers
  // feed/url/hdmi, suppressions, tags…) continuent de tout rafraîchir.
  const PAGE_SIZE = 100

  // Recherche débouncée : server-side désormais → pas une requête par frappe.
  const [debouncedSearch, setDebouncedSearch] = useState('')
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search.trim()), 300)
    return () => clearTimeout(timer)
  }, [search])

  function folderParams(): Record<string, string | number | boolean> {
    if (folderFilter === 'unfiled') return { unfiled: true }
    if (folderFilter !== 'all') return { id_folder: folderFilter }
    return {}
  }

  const { data: stats } = useQuery<{ total: number; by_type: Record<string, number> }>({
    queryKey: ['media', 'stats', folderFilter],
    queryFn: () => api.get('/media/stats', { params: folderParams() }).then((r) => r.data),
  })
  const typeCounts = stats?.by_type ?? {}
  const totalCount = stats?.total ?? 0

  const {
    data: mediaPages, isLoading, fetchNextPage, hasNextPage, isFetchingNextPage,
  } = useInfiniteQuery<MediaListItem[]>({
    queryKey: ['media', 'list', folderFilter, typeFilter, debouncedSearch],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => {
      const params: Record<string, string | number | boolean> = {
        ...folderParams(), limit: PAGE_SIZE, offset: pageParam as number,
      }
      if (typeFilter !== 'all') params.media_type = typeFilter
      if (debouncedSearch) params.q = debouncedSearch
      return api.get<MediaListItem[]>('/media/', { params }).then((r) => r.data)
    },
    // Page pleine = il en reste probablement (une page exactement pleine en fin de liste
    // coûte juste un « Afficher plus » qui revient vide — acceptable, zéro requête count).
    getNextPageParam: (lastPage, pages) =>
      lastPage.length === PAGE_SIZE ? pages.length * PAGE_SIZE : undefined,
    refetchInterval: (query) => {
      const items = (query.state.data?.pages ?? []).flat()
      return items.some((m) => m.transcode_status === 'processing' || m.transcode_status === 'pending')
        ? 5000
        : false
    },
  })
  const mediaItems = useMemo(() => (mediaPages?.pages ?? []).flat(), [mediaPages])

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.delete(`/media/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['media'] })
      setDeleteItem(null)
    },
  })

  const bulkTagMutation = useMutation({
    mutationFn: (body: { id_media: number[]; id_tags: number[]; mode: 'add' | 'replace' }) =>
      api.post('/media/bulk-tag', body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['media'] })
      setBulkTagOpen(false)
      setBulkTags(new Set())
      setSelectedIds(new Set())
      setSelectMode(false)
    },
  })

  function exitSelectMode() {
    setSelectMode(false)
    setSelectedIds(new Set())
  }

  // Ouvre l'item dans le bon éditeur : url_display → drawer Page Web, feed_display → drawer
  // Feed (édition source+modes), sinon le drawer média générique.
  function openMediaItem(item: MediaListItem) {
    if (item.media_type === 'url_display') setUrlDrawer({ open: true, editId: item.id_media })
    else if (item.media_type === 'feed_display' && item.id_feed)
      setFeedDrawer({ open: true, editId: item.id_feed })
    else setSelectedId(item.id_media)
  }

  function toggleSelect(id: number) {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  const embargoMutation = useMutation({
    mutationFn: (body: { id: number; not_before_utc: string | null; embargo_event_id: number | null }) =>
      api.patch<MediaEmbargoResult>(`/media/${body.id}/embargo`, {
        not_before_utc: body.not_before_utc,
        embargo_event_id: body.embargo_event_id,
      }).then((r) => r.data),
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ['media'] })
      setEmbargoItem(null)
      if (result.affected_playlists.length > 0) {
        setEmbargoWarnings(result.affected_playlists)
      }
    },
  })

  function handleUploaded(media: MediaRead) {
    qc.invalidateQueries({ queryKey: ['media'] })
    setSelectedId(media.id_media)
    setTab('library')
  }

  // Création du média pointeur « Entrée HDMI » (aucun fichier — juste un nom).
  const createHdmi = useMutation({
    mutationFn: async () =>
      (await api.post<MediaRead>('/media/hdmi-input', { name: hdmiName.trim() })).data,
    onSuccess: (d) => {
      setShowHdmiCreate(false)
      setHdmiName('')
      handleUploaded(d)
    },
  })

  function handleImported(media: MediaRead) {
    qc.invalidateQueries({ queryKey: ['media'] })
    setSelectedId(media.id_media)
    setTab('library')
  }

  // Le serveur filtre type + recherche + dossier. Le panneau latéral (multi-types, état)
  // affine ENSUITE, côté client, sur la page chargée — l'API ne prend qu'un `media_type`.
  // C'est assumé et DIT à l'écran quand la liste est paginée (cf. `partial`).
  const filteredItems = useMemo(() => {
    const base = applyFilters(mediaItems, filters)
    const dir = sort.dir === 'asc' ? 1 : -1
    const val = (m: MediaListItem) => {
      if (sort.key === 'name') return m.media_name.toLowerCase()
      if (sort.key === 'type') return m.media_type
      if (sort.key === 'size') return m.served_size_bytes ?? m.file_size_bytes ?? 0
      return m.created_at ?? ''
    }
    // Tri LOCAL, donc sur la page chargée seulement — cohérent avec le filtrage ci-dessus.
    return [...base].sort((a, b) => {
      const va = val(a), vb = val(b)
      return va < vb ? -dir : va > vb ? dir : 0
    })
  }, [mediaItems, filters, sort])

  function onSort(key: SortKey) {
    setSort((s) => s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'asc' })
  }

  // Ouvre le bon écran : un contenu VISUEL se regarde d'abord (visionneuse), un média
  // POINTEUR (page web / flux / entrée HDMI) n'a rien à montrer → droit à son éditeur.
  function activateItem(item: MediaListItem) {
    if (isViewable(item.media_type)) setViewerItem(item)
    else openMediaItem(item)
  }

  const viewerIndex = viewerItem ? filteredItems.findIndex((m) => m.id_media === viewerItem.id_media) : -1
  const gotoOffset = (d: number) => {
    if (viewerIndex < 0) return
    const next = filteredItems[viewerIndex + d]
    if (next && isViewable(next.media_type)) setViewerItem(next)
  }

  // Onglets de type : « Tous » + un onglet par type effectivement présent (comptes /media/stats)
  const typeTabs = [
    { key: 'all', label: t('media.type_all'), count: totalCount },
    ...TYPE_ORDER.filter((ty) => typeCounts[ty]).map((ty) => ({
      key: ty,
      label: t(`media.type_${ty}`),
      count: typeCounts[ty],
    })),
  ]

  // Si le type filtré n'a plus d'éléments (ex. dernier média supprimé), revenir à « Tous »
  useEffect(() => {
    if (stats && typeFilter !== 'all' && !typeCounts[typeFilter]) setTypeFilter('all')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [typeFilter, stats])

  // Mesure la largeur du conteneur → nb de vignettes par ligne (MediaGrid = minmax(180px) + gap 16px).
  //
  // ⚠️ CALLBACK REF, et surtout PAS `useRef` + `useEffect([])` : au tout premier
  // affichage, la zone de défilement n'est pas encore montée quand l'effet se
  // joue (la page rend d'abord un écran de chargement), donc `ref.current` vaut
  // `null`, l'effet sort — et ne se rejoue jamais. `perRow` restait alors à sa
  // valeur initiale de 5 quelle que soit la largeur réelle. En revenant sur la
  // page, React Query sert le cache : la grille est montée dès le 1er rendu,
  // l'effet mesurait, et le nombre devenait correct — d'où le comportement
  // « juste seulement à partir de la 2e visite » (régression vue par Ed le
  // 2026-07-20). Une callback ref se déclenche à CHAQUE attachement du nœud.
  const roRef = useRef<ResizeObserver | null>(null)
  const contentRef = useCallback((el: HTMLDivElement | null) => {
    roRef.current?.disconnect()
    roRef.current = null
    if (!el) return
    const TILE = 180, GAP = 16
    const compute = () => {
      lastPerRow = Math.max(1, Math.floor((el.clientWidth + GAP) / (TILE + GAP)))
      setPerRow(lastPerRow)
    }
    compute()
    const ro = new ResizeObserver(compute)
    ro.observe(el)
    roRef.current = ro
  }, [])

  // Vue d'accueil « N derniers uploadés par type » : tant qu'aucun filtre n'est
  // actif et qu'on n'a pas demandé la grille complète (évite le mur de contenu).
  // N = nombre exact de vignettes tenant sur UNE ligne à la largeur courante
  // (dynamique : ~5 en FHD, ~14 en ultrawide, plus en 4K). Mesuré sur le conteneur.
  const RECENT_PER_TYPE = perRow ?? 5
  //
  // 🔴 Défaut trouvé par Ed EN PRODUCTION (2026-07-28) : cette vue rend ses PROPRES grilles
  // par type et ignore `viewMode`, `filteredItems` et le survol. Elle avalait donc les trois
  // gestes du redesign — basculer en liste, cocher un filtre, survoler une ligne — sans le
  // moindre signe : « rien ne se passe dans la zone centrale ». UNE cause, trois symptômes.
  // Un mode d'affichage choisi ou un filtre coché SONT des intentions de voir la liste
  // complète : ils doivent sortir de la vue d'accueil, exactement comme le fait déjà un
  // filtre de type ou de dossier.
  const isLanding =
    !showAll && typeFilter === 'all' && folderFilter === 'all' && !search && !selectMode
    && viewMode === 'grid' && countActive(filters) === 0 && totalCount > 0
  // Vue d'accueil paginée : une petite requête PAR TYPE (limit = 1 ligne de vignettes) —
  // les N derniers d'un type ne sont plus dérivables de la liste (elle est paginée).
  const landingTypes = TYPE_ORDER.filter((ty) => typeCounts[ty])
  const recentQueries = useQueries({
    queries: landingTypes.map((ty) => ({
      queryKey: ['media', 'recent', ty, RECENT_PER_TYPE],
      queryFn: () =>
        api.get<MediaListItem[]>('/media/', { params: { media_type: ty, limit: RECENT_PER_TYPE } })
          .then((r) => r.data),
      // Pas de requête avant la MESURE : sinon elles partaient avec le repli de 5, puis
      // repartaient une fois la largeur connue — une requête inutile par type à chaque
      // chargement, et une ligne trop courte le temps de la seconde (vu en CI 2026-11-09).
      enabled: isLanding && perRow !== null,
      // Même polling que la liste paginée : sur la vue d'accueil un média fraîchement
      // uploadé restait « en cours » jusqu'à un rechargement manuel (cette vue n'avait
      // aucun refetchInterval, contrairement à la liste). On poll tant qu'un item de ce
      // type transcode, puis on s'arrête (retour Ed 2026-07-21).
      refetchInterval: (query: { state: { data?: MediaListItem[] } }) => {
        const items = query.state.data ?? []
        return items.some((m) => m.transcode_status === 'processing' || m.transcode_status === 'pending')
          ? 5000
          : false
      },
    })),
  })
  const recentItems = useMemo(
    () => recentQueries.flatMap((q) => q.data ?? []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [recentQueries.map((q) => q.dataUpdatedAt).join(',')],
  )
  // « Diffusé sur N écrans » AFFICHÉ EN PERMANENCE (demande d'Ed) : une SEULE requête pour
  // toute la page, jamais une par vignette. Le serveur résout les cibles une fois par
  // programme, pas une fois par média — c'est ce qui rend le permanent tenable.
  const visibleIds = useMemo(
    () => [...new Set([...mediaItems, ...recentItems].map((m) => m.id_media))].sort((a, b) => a - b),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [mediaItems, recentItems],
  )
  const { data: usageSummary } = useQuery<Record<string, { playlists: number; screens: number }>>({
    queryKey: ['media-usage-summary', visibleIds],
    queryFn: () =>
      api.get(`/media/usage-summary?ids=${visibleIds.join(',')}`).then((r) => r.data),
    enabled: visibleIds.length > 0,
    staleTime: 60_000,
  })

  const recentByType = landingTypes.map((ty, i) => ({
    type: ty,
    total: typeCounts[ty] ?? 0,
    items: recentQueries[i]?.data ?? [],
  }))

  const tabs = [
    { key: 'library', label: t('media.library_tab'), count: totalCount },
    { key: 'stock',   label: t('media.stock_tab') },
  ]

  return (
    <>
      <Topbar title={t('media.title')}>
        {tab === 'library' && (
          // Toutes ces actions creent un media -> `media:create` cote serveur.
          <Can perm="media:create">
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              onClick={() => setUrlDrawer({ open: true, editId: null })}
            >
              <Globe className="w-3.5 h-3.5 mr-1" />{t('url_display.new')}
            </Button>
            <Button
              size="sm"
              onClick={() => setShowHdmiCreate(true)}
            >
              <MonitorPlay className="w-3.5 h-3.5 mr-1" />{t('media.hdmi_new')}
            </Button>
            <Button
              size="sm"
              onClick={() => setShowGenerate(true)}
            >
              <Sparkles className="w-3.5 h-3.5 mr-1" />{t('generation.generate_btn')}
            </Button>
            <Button
              size="sm"
              onClick={() => setShowUploader((v) => !v)}
              variant={showUploader ? 'secondary' : 'primary'}
            >
              {showUploader ? t('common.close') : t('media.upload')}
            </Button>
          </div>
          </Can>
        )}
      </Topbar>
      {/*
        Page verrouillée au viewport. Trois zones qui ne s'influencent plus :
        bandeaux + navigation de gauche (fixes), arbre des dossiers (défile seul),
        barre de filtres/recherche (fixe) et grille de vignettes (défile seule).
      */}
      <main className="flex-1 flex flex-col min-h-0 px-6 pt-5">
        <div className="shrink-0">

          <GenerationJobsBanner />

          {/* Embargo warnings banner */}
          {embargoWarnings.length > 0 && (
            <div className="mb-4 bg-orange-500/15 dark:bg-orange-500/15 border border-orange-300 dark:border-orange-500/40 rounded-lg p-4">
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-start gap-2">
                  <AlertTriangle className="w-4 h-4 text-orange-600 dark:text-orange-300 mt-0.5 shrink-0" />
                  <div>
                    <p className="text-sm font-medium text-orange-900 dark:text-orange-200">
                      {t('media.embargo_warning_title')}
                    </p>
                    <p className="text-xs text-orange-700 dark:text-orange-300 mt-0.5">
                      {t('media.embargo_warning_body')}
                    </p>
                    <ul className="mt-2 space-y-1">
                      {embargoWarnings.map((pl) => (
                        <li key={pl.id_playlist} className="flex items-center gap-1.5">
                          <ExternalLink className="w-3 h-3 text-orange-500" />
                          <a
                            href={`/playlists/${pl.id_playlist}`}
                            className="text-xs text-orange-700 dark:text-orange-300 underline hover:text-orange-900"
                          >
                            {pl.playlist_name}
                          </a>
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
                <button
                  onClick={() => setEmbargoWarnings([])}
                  className="text-orange-400 hover:text-orange-300 shrink-0"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            </div>
          )}

        </div>

          <div className="flex-1 min-h-0 flex gap-6">
            {/* Navigation à gauche : source (Bibliothèque / Photos libres) + dossiers */}
            <aside className="w-56 shrink-0 flex flex-col min-h-0 gap-4">
              <div className="space-y-1 shrink-0">
                {tabs.map((tb) => {
                  const active = tab === tb.key
                  return (
                    <button
                      key={tb.key}
                      onClick={() => setTab(tb.key as Tab)}
                      className={`w-full flex items-center justify-between px-3 py-2 rounded-lg text-sm font-medium transition-sf ${
                        active ? 'bg-accent text-white' : 'text-gray-600 hover:bg-gray-100'
                      }`}
                    >
                      <span>{tb.label}</span>
                      {'count' in tb && tb.count != null && (
                        <span className={`text-xs ${active ? 'text-white/80' : 'text-gray-400'}`}>{tb.count}</span>
                      )}
                    </button>
                  )
                })}
              </div>
              {tab === 'library' && (
                <div className="flex-1 min-h-0" data-write-perm="media:edit">
                  <FolderTree selected={folderFilter} onSelect={setFolderFilter} fill />
                </div>
              )}
            </aside>

            {/* Zone principale */}
            <div className="flex-1 min-w-0 flex flex-col min-h-0">
              {tab === 'stock' ? (
                <StockSearch onImported={handleImported} />
              ) : isLoading ? (
                <div className="flex justify-center py-16">
                  <span className="w-6 h-6 border-2 border-accent border-t-transparent rounded-full animate-spin" />
                </div>
              ) : (
                <>
              {/*
                En-tête RÉELLEMENT fixe : filtres par type + recherche + actions.
                Auparavant `sticky top-0`, ce qui le laissait remonter de quelques
                pixels avant de se coller — d'où l'impression de flottement.
              */}
              <div className="shrink-0 pb-3">
              {/* Filtre par type de média */}
              {typeTabs.length > 1 && (
                <div className="flex flex-wrap items-center gap-1.5 mb-4">
                  {typeTabs.map((tt) => (
                    <button
                      key={tt.key}
                      onClick={() => setTypeFilter(tt.key as MediaType)}
                      className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium transition-sf ${
                        typeFilter === tt.key
                          ? 'bg-accent text-white'
                          : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                      }`}
                    >
                      {tt.label}
                      <span
                        className={`text-xs ${
                          typeFilter === tt.key ? 'text-white/80' : 'text-gray-400'
                        }`}
                      >
                        {tt.count}
                      </span>
                    </button>
                  ))}
                </div>
              )}

              <div className="flex items-center gap-2">
                <SearchInput
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder={t('common.search')}
                />
                {isLanding && (
                  <Button size="sm" variant="secondary" onClick={() => setShowAll(true)}>
                    {t('media.show_all_media')}
                  </Button>
                )}
                {mediaItems.length > 0 && (
                  selectMode ? (
                    <Button size="sm" variant="secondary" onClick={exitSelectMode}>
                      {t('common.cancel')}
                    </Button>
                  ) : (
                    <Button size="sm" variant="secondary" onClick={() => setSelectMode(true)}>
                      <CheckSquare className="w-3.5 h-3.5" />
                      {t('media.select')}
                    </Button>
                  )
                )}

                {/* Bascule grille ↔ liste + panneau de filtres, poussés à droite. */}
                <div className="ml-auto flex items-center gap-2">
                  <button
                    onClick={() => setFiltersOpen((v) => !v)}
                    title={t('media.filters')}
                    className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition-sf ${
                      filtersOpen || countActive(filters) > 0
                        ? 'bg-accent/15 border-accent/40 text-accent-hover'
                        : 'bg-surface border-white/5 text-gray-500 hover:text-gray-800'
                    }`}
                  >
                    <SlidersHorizontal className="w-3.5 h-3.5" />
                    {countActive(filters) > 0 && (
                      <span className="tabular-nums">{countActive(filters)}</span>
                    )}
                  </button>
                  <div className="inline-flex rounded-lg border border-white/5 overflow-hidden">
                    <button
                      onClick={() => { if (viewMode !== 'grid') toggleView() }}
                      title={t('media.view_grid')}
                      aria-pressed={viewMode === 'grid'}
                      className={`px-2.5 py-1.5 transition-sf ${
                        viewMode === 'grid' ? 'bg-accent text-white' : 'text-gray-500 hover:text-gray-800'
                      }`}
                    >
                      <LayoutGrid className="w-3.5 h-3.5" />
                    </button>
                    <button
                      onClick={() => { if (viewMode !== 'list') toggleView() }}
                      title={t('media.view_list')}
                      aria-pressed={viewMode === 'list'}
                      className={`px-2.5 py-1.5 transition-sf ${
                        viewMode === 'list' ? 'bg-accent text-white' : 'text-gray-500 hover:text-gray-800'
                      }`}
                    >
                      <List className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
              </div>
              {/* Fil d'Ariane + sous-dossiers en tuiles : la zone de contenu ne disait
                  NULLE PART où l'on se trouve. Masqué en mode sélection (le geste est
                  alors « cocher », pas « naviguer ») et sur la vue d'accueil. */}
              {!selectMode && !isLanding && (
                <div className="mt-1">
                  <FolderBreadcrumb
                    folders={folders}
                    selected={folderFilter}
                    onSelect={setFolderFilter}
                  />
                </div>
              )}
              </div>
              {/* fin en-tête fixe */}

              {/* Seule zone défilante : la grille de vignettes.
                  `contentRef` est posé ICI (et non sur la colonne) pour que la
                  mesure du nombre de vignettes par ligne tienne compte de la
                  barre de défilement. */}
              <div ref={contentRef} className="flex-1 min-h-0 overflow-y-auto pb-6">

              {/* Bulk action bar */}
              {selectMode && (
                <div className="flex items-center justify-between gap-3 mb-4 px-4 py-2.5 bg-accent/5 border border-accent/20 rounded-lg">
                  <span className="text-sm text-gray-700">
                    {t('media.selected_count', { count: selectedIds.size })}
                  </span>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => setSelectedIds(new Set(filteredItems.map((m) => m.id_media)))}
                      className="text-xs text-accent hover:underline"
                    >
                      {t('media.select_all')}
                    </button>
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={selectedIds.size === 0}
                      onClick={() => setMoveOpen(true)}
                    >
                      <FolderInput className="w-3.5 h-3.5" />
                      {t('media.move_to_folder')}
                    </Button>
                    <Button
                      size="sm"
                      disabled={selectedIds.size === 0}
                      onClick={() => { setBulkTags(new Set()); setBulkMode('add'); setBulkTagOpen(true) }}
                    >
                      <Tag className="w-3.5 h-3.5" />
                      {t('media.bulk_tag')}
                    </Button>
                  </div>
                </div>
              )}

              {isLanding ? (
                <div className="space-y-8">
                  {recentByType.map(({ type, total, items }) => (
                    <section key={type}>
                      <div className="flex items-center gap-3 mb-3">
                        <h3 className="text-sm font-semibold text-gray-800">
                          {t(`media.type_${type}`)}
                          <span className="ml-2 text-xs font-normal text-gray-400">{total}</span>
                        </h3>
                        {total > items.length && (
                          <button
                            onClick={() => setTypeFilter(type)}
                            className="text-xs text-accent hover:underline"
                          >
                            {t('media.see_all')}
                          </button>
                        )}
                      </div>
                      <MediaGrid
                        items={items}
                        onSelect={activateItem}
                        onEdit={openMediaItem}
                        usage={usageSummary}
                        onDelete={(item) => setDeleteItem(item)}
                        onEditEmbargo={(item) => { setEmbargoWarnings([]); setEmbargoItem(item) }}
                      />
                    </section>
                  ))}
                </div>
              ) : (
                <>
                  {viewMode === 'list' ? (
                    <MediaListView
                      items={filteredItems}
                      folders={folders}
                      sort={sort}
                      onSort={onSort}
                      selectMode={selectMode}
                      selectedIds={selectedIds}
                      onSelect={(item) => selectMode ? toggleSelect(item.id_media) : activateItem(item)}
                      onEdit={openMediaItem}
                      onDelete={(item) => setDeleteItem(item)}
                      usage={usageSummary}
                    />
                  ) : (
                    <MediaGrid
                      items={filteredItems}
                      selectMode={selectMode}
                      selectedIds={selectedIds}
                      onSelect={(item) => selectMode ? toggleSelect(item.id_media) : activateItem(item)}
                      onEdit={openMediaItem}
                      onDelete={(item) => setDeleteItem(item)}
                      onEditEmbargo={(item) => { setEmbargoWarnings([]); setEmbargoItem(item) }}
                      usage={usageSummary}
                    />
                  )}
                  {/* Pagination : pages de PAGE_SIZE ajoutées à la demande (server-side) */}
                  {hasNextPage && (
                    <div className="flex flex-col items-center gap-1 mt-6">
                      <Button size="sm" variant="secondary" loading={isFetchingNextPage}
                        onClick={() => fetchNextPage()}>
                        {t('media.load_more')}
                      </Button>
                      <span className="text-xs text-gray-400">
                        {t('media.shown_count', { count: filteredItems.length })}
                      </span>
                    </div>
                  )}
                </>
              )}
              </div>
                </>
              )}
            </div>

            {tab === 'library' && (
              <MediaFilters
                open={filtersOpen}
                onClose={() => setFiltersOpen(false)}
                value={filters}
                onChange={setFilters}
                availableTypes={TYPE_ORDER.filter((ty) => typeCounts[ty]).map((ty) => ({
                  key: ty, label: t(`media.type_${ty}`), count: typeCounts[ty],
                }))}
                partial={!!hasNextPage}
              />
            )}
          </div>
      </main>

      {/* Visionneuse plein écran — contenus visuels seulement (cf. isViewable). */}
      <MediaViewer
        item={viewerItem}
        onClose={() => setViewerItem(null)}
        onEdit={() => { if (viewerItem) { const it = viewerItem; setViewerItem(null); openMediaItem(it) } }}
        onPrev={viewerIndex > 0 ? () => gotoOffset(-1) : undefined}
        onNext={viewerIndex >= 0 && viewerIndex < filteredItems.length - 1 ? () => gotoOffset(1) : undefined}
      />

      {/* Upload drawer */}
      <Drawer
        open={showUploader}
        onClose={() => setShowUploader(false)}
        title={t('media.upload')}
      >
        <MediaUploader onUploaded={handleUploaded} />
      </Drawer>

      <GenerateDialog
        open={showGenerate}
        onClose={() => setShowGenerate(false)}
        onGenerated={handleUploaded}
        onVideoQueued={() => qc.invalidateQueries({ queryKey: ['generation-jobs'] })}
      />

      {/* Page Web (url_display) — création / édition in-place */}
      <UrlDisplayDrawer
        open={urlDrawer.open}
        editId={urlDrawer.editId}
        onClose={() => setUrlDrawer({ open: false, editId: null })}
      />

      {/* Feed (feed_display) — édition in-place depuis la médiathèque */}
      <FeedDrawer
        open={feedDrawer.open}
        editId={feedDrawer.editId}
        onClose={() => setFeedDrawer({ open: false, editId: null })}
      />

      {/* Entrée HDMI (hdmi_input) — média pointeur, juste un nom */}
      <Drawer
        open={showHdmiCreate}
        onClose={() => setShowHdmiCreate(false)}
        title={t('media.hdmi_title')}
      >
        <div className="space-y-4">
          <p className="text-xs text-gray-400">{t('media.hdmi_hint')}</p>
          <div>
            <label className="block text-xs text-gray-500 mb-1">{t('media.hdmi_name_label')}</label>
            <input
              value={hdmiName}
              onChange={(e) => setHdmiName(e.target.value)}
              className="w-full px-3 py-2 text-sm rounded-lg border border-gray-200 focus:outline-none focus:ring-2 focus:ring-accent/40 bg-surface"
              placeholder={t('media.hdmi_name_ph')}
              onKeyDown={(e) => { if (e.key === 'Enter' && hdmiName.trim()) createHdmi.mutate() }}
            />
          </div>
          <Button
            className="w-full"
            disabled={!hdmiName.trim()}
            loading={createHdmi.isPending}
            onClick={() => createHdmi.mutate()}
          >
            {t('common.create')}
          </Button>
          {createHdmi.isError && <p className="text-xs text-danger">{t('media.hdmi_error')}</p>}
        </div>
      </Drawer>

      {/* Embargo editor drawer */}
      <Drawer
        open={embargoItem !== null}
        onClose={() => setEmbargoItem(null)}
        title={t('media.embargo_edit_title')}
      >
        {embargoItem && (
          <EmbargoEditor
            item={embargoItem}
            isPending={embargoMutation.isPending}
            onSave={(not_before_utc, embargo_event_id) =>
              embargoMutation.mutate({ id: embargoItem.id_media, not_before_utc, embargo_event_id })
            }
            onCancel={() => setEmbargoItem(null)}
          />
        )}
      </Drawer>

      {/* Delete confirmation (impact analysis) */}
      <Drawer
        open={deleteItem !== null}
        onClose={() => setDeleteItem(null)}
        title={t('media.delete_title')}
      >
        {deleteItem && (
          <DeleteMediaDialog
            name={deleteItem.media_name}
            impactUrl={`/media/${deleteItem.id_media}/deletion-impact`}
            queryKey={['media-deletion-impact', deleteItem.id_media]}
            isPending={deleteMutation.isPending}
            onConfirm={() => deleteMutation.mutate(deleteItem.id_media)}
            onCancel={() => setDeleteItem(null)}
          />
        )}
      </Drawer>

      {/* Bulk tag drawer */}
      <Drawer
        open={bulkTagOpen}
        onClose={() => setBulkTagOpen(false)}
        title={t('media.bulk_tag_title', { count: selectedIds.size })}
      >
        <div className="space-y-4">
          <p className="text-sm text-gray-500">{t('media.bulk_tag_hint')}</p>

          <div className="flex flex-col gap-1">
            <span className="text-sm font-medium text-gray-800">{t('media.bulk_mode')}</span>
            <div className="flex gap-2">
              {(['add', 'replace'] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setBulkMode(m)}
                  className={`flex-1 px-3 py-2 rounded border text-sm transition-sf ${
                    bulkMode === m
                      ? 'border-accent bg-accent/10 text-accent font-medium'
                      : 'border-gray-200 text-gray-600 hover:bg-gray-50'
                  }`}
                >
                  {t(`media.bulk_mode_${m}`)}
                </button>
              ))}
            </div>
            <p className="text-xs text-gray-400">{t(`media.bulk_mode_${bulkMode}_hint`)}</p>
          </div>

          <div>
            <span className="text-sm font-medium text-gray-800">{t('media.bulk_pick_tags')}</span>
            <div className="mt-2">
              <TagPicker
                namespaces={namespaces}
                selected={bulkTags}
                onToggle={(id) =>
                  setBulkTags((prev) => {
                    const next = new Set(prev)
                    next.has(id) ? next.delete(id) : next.add(id)
                    return next
                  })
                }
              />
            </div>
          </div>

          <div className="pt-2">
            <Button
              loading={bulkTagMutation.isPending}
              disabled={selectedIds.size === 0 || (bulkMode === 'add' && bulkTags.size === 0)}
              onClick={() =>
                bulkTagMutation.mutate({
                  id_media: [...selectedIds],
                  id_tags: [...bulkTags],
                  mode: bulkMode,
                })
              }
            >
              {t('media.bulk_apply')}
            </Button>
          </div>
        </div>
      </Drawer>

      {/* Move-to-folder drawer */}
      <Drawer
        open={moveOpen}
        onClose={() => setMoveOpen(false)}
        title={t('media.move_to_folder_title', { count: selectedIds.size })}
      >
        <div className="space-y-2">
          <p className="text-sm text-gray-500">{t('media.move_hint')}</p>
          <button
            onClick={() => moveMutation.mutate(null)}
            className="w-full flex items-center gap-2 px-3 py-2 rounded text-sm text-gray-600 hover:bg-gray-50 transition-sf"
          >
            <Inbox className="w-4 h-4 shrink-0 text-gray-400" />
            <span className="truncate">{t('media.folder_unfiled')}</span>
          </button>
          {/* Le nom seul ne suffit pas : deux dossiers homonymes sous deux parents
              differents donnaient ici deux lignes IDENTIQUES a la meme indentation
              (cas fréquent en pratique). On affiche donc le chemin complet du serveur sous
              le nom des qu'il y a un parent — c'est ce qui permet de choisir. */}
          {folders.map((f) => (
            <button
              key={f.id_folder}
              onClick={() => moveMutation.mutate(f.id_folder)}
              className="w-full flex items-start gap-2 px-3 py-2 rounded text-left text-sm text-gray-700 hover:bg-gray-50 transition-sf"
              style={{ paddingLeft: `${12 + f.depth * 14}px` }}
            >
              <Folder className="w-4 h-4 shrink-0 mt-0.5 text-gray-400" />
              <span className="min-w-0 flex-1">
                <span className="block truncate">{folderLabel(f)}</span>
                {f.parent_id !== null && (
                  <span className="block truncate text-[11px] text-gray-500">{folderPath(f, foldersById)}</span>
                )}
              </span>
              {f.media_count > 0 && (
                <span className="shrink-0 text-[11px] text-gray-500 tabular-nums mt-0.5">{f.media_count}</span>
              )}
            </button>
          ))}
          {folders.length === 0 && (
            <p className="text-sm text-gray-400 py-4 text-center">{t('media.folder_empty')}</p>
          )}
        </div>
      </Drawer>

      <MediaDrawer mediaId={selectedId} onClose={() => setSelectedId(null)} />
    </>
  )
}
