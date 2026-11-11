'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import { useTranslations } from 'next-intl'
import { useQuery } from '@tanstack/react-query'
import {
  Monitor,
  MonitorOff,
  AlertTriangle,
  Megaphone,
  PlayCircle,
  TrendingUp,
  TrendingDown,
  ChevronRight,
  ListVideo,
  Film,
  FileVideo,
  FileImage,
  FileText,
  AudioLines,
  Globe,
  Rss,
  MonitorPlay,
  Calendar,
  Zap,
  ClipboardCheck,
  Upload,
  Activity as ActivityIcon,
  Image as ImageIcon,
} from 'lucide-react'
import Topbar from '@/components/layout/Topbar'
import Sparkline from '@/components/ui/Sparkline'
import SectionHeader from '@/components/ui/SectionHeader'
import StatusBadge from '@/components/ui/StatusBadge'
import { useCollapse } from '@/lib/useCollapse'
import FleetMap, { type GeoSite } from '@/components/dashboard/FleetMap'
import { useAuthStore } from '@/store/auth'
import api from '@/lib/api'
import { landingPath } from '@/lib/landingPath'
import type {
  DashboardSummary,
  KpiCard,
  ActivityFeed,
  DashboardMedia,
} from '@/types/api'

// ── NOC (players enrichis site/os/statut) — réutilisé pour Fleet Status + table ──
interface NocRow {
  id_player: number
  player_name: string
  id_site: number | null
  site_name: string | null
  player_os: string | null
  player_version: string | null
  status: string
  last_seen_at: string | null
}
interface NocResponse {
  total: number
  online: number
  warning: number
  offline: number
  maintenance: number
  rows: NocRow[]
  // Parc au-delà du plafond de lignes détaillées : compteurs justes, table partielle.
  rows_truncated?: boolean
}

type Bucket = 'online' | 'offline' | 'warning' | 'maintenance'

// Regroupement des statuts pour filtres + carte, ALIGNÉ sur le NOC/summary :
// `critical` = escalade d'offline → compté offline (jamais warning).
function statusBucket(s: string): Bucket {
  if (s === 'online') return 'online'
  if (s === 'warning') return 'warning'
  if (s === 'maintenance') return 'maintenance'
  return 'offline' // offline + critical
}

const MEDIA_TILE: Record<string, string> = {
  video: '#3B82F6', image: '#10B981', audio: '#8B5CF6', document: '#F59E0B',
  html_package: '#F43F5E', feed_display: '#F97316', url_display: '#06B6D4',
  hdmi_input: '#06B6D4',
}
// Icône par type de média (cohérent avec /media MediaGrid).
const MEDIA_ICON: Record<string, React.ComponentType<{ className?: string; style?: React.CSSProperties }>> = {
  video: FileVideo, image: FileImage, audio: AudioLines, document: FileText,
  html_package: Globe, url_display: Globe, feed_display: Rss, hdmi_input: MonitorPlay,
}

function relTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso).getTime()
  if (Number.isNaN(d)) return '—'
  const s = Math.max(0, Math.floor((Date.now() - d) / 1000))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h`
  return `${Math.floor(s / 86400)}d`
}

export default function DashboardPage() {
  const t = useTranslations()
  const { user } = useAuthStore()
  const router = useRouter()
  const isAdmin = useAuthStore((s) => s.isAdmin())
  const isSuperAdmin = useAuthStore((s) => s.hasRole('CMS_SUPER_ADMIN'))

  // Le tableau de bord lit des endpoints gatés org-admin. Pour un rôle métier,
  // ils repondent 403 et la page affiche des compteurs vides ET des phrases
  // FAUSSES (« aucun player », « aucun média ») : l'absence de données y est
  // indistinguable d'un parc vide. On redirige donc vers sa première surface
  // utilisable plutôt que de lui montrer un écran qui ment (constat Ed 20/07).
  // L'entrée est déjà masquée du menu — ceci couvre l'accès par URL directe.
  //
  // ⚠️ Le SUPER-ADMIN passe `isAdmin()` (qui couvre les deux rôles d'administration) : sans
  // le second test il restait ici, sur un tableau de bord scopé à `id_org`. Or son
  // rattachement à une organisation n'est qu'une formalité de schéma — il y lisait donc
  // « 0 écran / 0 média » pendant que la plateforme en gère plusieurs. Exactement le
  // mensonge d'affichage que `landingPath` évite au moment de la connexion, resté ouvert
  // à la navigation directe (audit Dashboard 2026-07-28). `landingPath` l'envoie sur sa
  // vraie surface : la console de provisionnement.
  useEffect(() => {
    if (!isAdmin || isSuperAdmin) {
      router.replace(landingPath({ isAdmin, isSuperAdmin, permissions: user?.permissions }))
    }
  }, [isAdmin, isSuperAdmin, user?.permissions, router])

  const { data: summary } = useQuery<DashboardSummary>({
    queryKey: ['dashboard-summary'],
    queryFn: () => api.get<DashboardSummary>('/dashboard/summary').then((r) => r.data),
    refetchInterval: 30000,
  })

  const { data: feed } = useQuery<ActivityFeed>({
    queryKey: ['dashboard-activity'],
    queryFn: () => api.get<ActivityFeed>('/dashboard/activity?limit=10').then((r) => r.data),
    refetchInterval: 30000,
  })

  const { data: noc } = useQuery<NocResponse>({
    queryKey: ['dashboard-noc'],
    queryFn: () => api.get<NocResponse>('/analytics/noc').then((r) => r.data),
    refetchInterval: 15000,
  })

  const { data: geo } = useQuery<{ sites: GeoSite[]; unplaced: number }>({
    queryKey: ['dashboard-geo'],
    queryFn: () => api.get<{ sites: GeoSite[]; unplaced: number }>('/analytics/geo').then((r) => r.data),
    refetchInterval: 30000,
  })

  const { data: mediaPanel } = useQuery<DashboardMedia>({
    queryKey: ['dashboard-media'],
    queryFn: () => api.get<DashboardMedia>('/dashboard/media').then((r) => r.data),
    refetchInterval: 60000,
  })

  const [kpisOpen, toggleKpis] = useCollapse('sf-dash-kpis')
  const [fleetOpen, toggleFleet] = useCollapse('sf-dash-fleet')

  // Filtre de statut de la table Players + centrage carte au clic sur un site.
  const [statusFilter, setStatusFilter] = useState<Bucket | 'all'>('all')
  const [focus, setFocus] = useState<{ id_site: number; nonce: number } | null>(null)
  const placedIds = useMemo(
    () => new Set((geo?.sites ?? []).map((s) => s.id_site)),
    [geo],
  )

  const greeting = useMemo(() => {
    const h = new Date().getHours()
    return h < 12 ? t('dashboard.greeting_morning')
      : h < 18 ? t('dashboard.greeting_afternoon')
      : t('dashboard.greeting_evening')
  }, [t])

  const firstName = (user?.full_name ?? user?.email ?? '').split(' ')[0]

  // ── Config des 5 KPI cards (label, couleur, icône, sens « bon ») ──────────────
  const kpis = summary
    ? ([
        { card: summary.players_online, label: t('dashboard.kpi_players_online'), rgb: '37 99 235', color: 'rgb(59 130 246)', Icon: Monitor, up_good: true, sub: t('dashboard.kpi_vs_last_week') },
        { card: summary.players_offline, label: t('dashboard.kpi_players_offline'), rgb: '239 68 68', color: 'rgb(239 68 68)', Icon: MonitorOff, up_good: false, sub: t('dashboard.kpi_vs_last_week') },
        { card: summary.players_warning, label: t('dashboard.kpi_players_warning'), rgb: '245 158 11', color: 'rgb(245 158 11)', Icon: AlertTriangle, up_good: false, sub: t('dashboard.kpi_vs_last_week') },
        { card: summary.active_campaigns, label: t('dashboard.kpi_active_campaigns'), rgb: '139 92 246', color: 'rgb(139 92 246)', Icon: Megaphone, up_good: true, sub: t('dashboard.kpi_vs_last_week') },
        { card: summary.proof_of_play, label: t('dashboard.kpi_proof_of_play'), rgb: '16 185 129', color: 'rgb(16 185 129)', Icon: PlayCircle, up_good: true, sub: t('dashboard.kpi_pop_24h') },
      ] as const)
    : []

  const rows = noc?.rows ?? []
  const filteredRows = statusFilter === 'all'
    ? rows
    : rows.filter((r) => statusBucket(r.status) === statusFilter)

  // Clic sur un site dans la table → déplie l'État du parc si besoin puis centre la carte.
  function focusSite(id: number | null) {
    if (id == null || !placedIds.has(id)) return
    if (!fleetOpen) toggleFleet()
    setFocus((f) => ({ id_site: id, nonce: (f?.nonce ?? 0) + 1 }))
  }

  const STATUS_FILTERS: { k: Bucket | 'all'; label: string; n: number }[] = [
    { k: 'all', label: t('dashboard.filter_all'), n: noc?.total ?? 0 },
    { k: 'online', label: t('players.status_online'), n: noc?.online ?? 0 },
    { k: 'offline', label: t('players.status_offline'), n: noc?.offline ?? 0 },
    { k: 'warning', label: t('players.status_warning'), n: noc?.warning ?? 0 },
    { k: 'maintenance', label: t('players.status_maintenance'), n: noc?.maintenance ?? 0 },
  ]

  return (
    <>
      <Topbar title={t('nav.dashboard')} />
      <div className="flex-1 flex min-h-0 overflow-hidden">
        {/* Colonne centrale : hero fixe + sections repliables + table Players extensible.
            PAS de scroll de FENÊTRE. La colonne, elle, peut défiler — et c'est délibéré :
            🔑 mesuré à 1920x950 (un navigateur plein écran sur un écran FHD, barre d'adresse et
            favoris déduits), hero + KPI + carte 340 px consommaient tout et la table des écrans
            tombait à 61 px pour 413 px de contenu, soit UNE ligne. Un plancher sur la table + un
            défilement de colonne en dernier recours valent mieux qu'un tableau réduit à rien :
            une barre de défilement est honnête, un tableau d'une ligne ment sur ce qu'il montre. */}
        <main className="flex-1 flex flex-col min-h-0 overflow-y-auto p-6 gap-5 relative sf-stagger">
          {/* ── Hero (fixe, ne bouge jamais) ─────────────────────────────────── */}
          <div className="shrink-0">
            <h2 className="text-2xl font-bold tracking-tight text-white">
              {greeting}{firstName ? `, ${firstName}` : ''}
            </h2>
            <p className="text-sm text-gray-400 mt-1">{t('dashboard.hero_sub')}</p>
          </div>

          {/* ── Section repliable : Indicateurs clés (KPI) ───────────────────── */}
          <div className="shrink-0">
            <SectionHeader
              title={t('dashboard.section_kpis')}
              open={kpisOpen}
              onToggle={toggleKpis}
              collapseLabel={t('dashboard.section_collapse')}
              expandLabel={t('dashboard.section_expand')}
            />
            {kpisOpen && (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-5 gap-4 mt-3">
                {kpis.map(({ card, label, rgb, color, Icon, up_good, sub }) => (
                  <KpiCardView key={card.key} card={card} label={label} rgb={rgb} color={color} Icon={Icon} upGood={up_good} sub={sub} t={t} />
                ))}
                {kpis.length === 0 &&
                  Array.from({ length: 5 }).map((_, i) => (
                    <div key={i} className="sf-panel sf-topline p-4 h-[130px] animate-pulse" />
                  ))}
              </div>
            )}
          </div>

          {/* ── Section repliable : État du parc + Activité récente ───────────── */}
          <div className="shrink-0">
            <SectionHeader
              title={t('dashboard.section_fleet')}
              open={fleetOpen}
              onToggle={toggleFleet}
              collapseLabel={t('dashboard.section_collapse')}
              expandLabel={t('dashboard.section_expand')}
            />
            {fleetOpen && (
              <div className="grid grid-cols-1 xl:grid-cols-3 gap-6 mt-3">
                {/* Fleet Status (2/3) — carte monde Leaflet interactive */}
                {/* 260 et non 340 : la carte était le plus gros bloc FIXE de la colonne et
                    mangeait à elle seule la place de la table. Elle reste lisible. */}
                <div className="sf-panel p-5 xl:col-span-2 flex flex-col min-h-[260px]">
                  <div className="flex items-center justify-between mb-4">
                    <h3 className="text-sm font-semibold text-gray-900">{t('dashboard.fleet_title')}</h3>
                    <Link href="/reports/geo" className="text-xs text-accent-hover hover:underline flex items-center gap-0.5">
                      {t('dashboard.view_all')} <ChevronRight className="w-3 h-3" />
                    </Link>
                  </div>
                  <FleetMap
                    sites={geo?.sites ?? []}
                    counts={{ online: noc?.online, warning: noc?.warning, offline: noc?.offline, maintenance: noc?.maintenance }}
                    focus={focus}
                  />
                </div>

                {/* Recent Activity (1/3) */}
                <div className="sf-panel p-5 flex flex-col">
                  <div className="flex items-center justify-between mb-4">
                    <h3 className="text-sm font-semibold text-gray-900">{t('dashboard.activity_title')}</h3>
                  </div>
                  <div className="space-y-3 overflow-y-auto max-h-[300px] pr-1">
                    {(feed?.items ?? []).map((it) => {
                      const meta = activityMeta(it.resource_type, it.action)
                      return (
                        <div key={it.id} className="flex items-start gap-2.5">
                          <span className={`mt-0.5 shrink-0 w-6 h-6 rounded-md flex items-center justify-center ${meta.err ? 'bg-red-500/15' : 'bg-white/5'}`}>
                            <meta.Icon className={`w-3.5 h-3.5 ${meta.err ? 'text-danger' : 'text-gray-400'}`} />
                          </span>
                          <div className="min-w-0 flex-1">
                            <ActivityText dep={`${it.id}:${it.resource_name ?? ''}`}>
                              <span className="font-medium">{prettyAction(it.action)}</span>
                              {(it.resource_name || it.resource_id) ? (
                                <span className="text-gray-500"> · {it.resource_name ?? `${it.resource_type} #${it.resource_id}`}</span>
                              ) : null}
                              {/* Nom venu du JOURNAL : la ressource n'existe plus. On le dit,
                                  sinon le nom laisse croire qu'elle est encore ouvrable. */}
                              {it.resource_gone && (
                                <span className="text-gray-500 italic"> ({t('dashboard.activity_deleted')})</span>
                              )}
                            </ActivityText>
                            <p className="text-[11px] text-gray-500 mt-0.5">
                              {relTime(it.created_at)}{it.user_name ? ` · ${it.user_name}` : ''}
                            </p>
                          </div>
                        </div>
                      )
                    })}
                    {(!feed || feed.items.length === 0) && (
                      <p className="text-xs text-gray-500 text-center py-6">{t('dashboard.activity_empty')}</p>
                    )}
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* ── Players : prend la hauteur restante, scroll INTERNE, PLANCHER 15 rem ──
              Sans plancher, `flex-1 min-h-0` accepte de tomber à 61 px : « prend le reste »
              n'est une promesse que s'il reste quelque chose. */}
          <div className="sf-panel flex flex-col flex-1 min-h-[15rem] overflow-hidden">
            <div className="flex items-center justify-between gap-3 px-5 py-4 shrink-0 flex-wrap">
              <div className="flex items-center gap-3 flex-wrap">
                <h3 className="text-sm font-semibold text-gray-900">{t('nav.players')}</h3>
                {/* Filtres de statut (Q5) */}
                <div className="flex items-center gap-1.5 flex-wrap">
                  {STATUS_FILTERS.map(({ k, label, n }) => {
                    const active = statusFilter === k
                    return (
                      <button
                        key={k}
                        onClick={() => setStatusFilter(k)}
                        className={`inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium border transition-sf ${
                          active
                            ? 'bg-accent/15 border-accent/40 text-accent-hover'
                            : 'bg-surface border-white/5 text-gray-500 hover:text-gray-800 hover:bg-surface-hover'
                        }`}
                      >
                        {label}
                        <span className="tabular-nums text-gray-500">{n}</span>
                      </button>
                    )
                  })}
                </div>
              </div>
              <Link href="/players" className="text-xs text-accent-hover hover:underline flex items-center gap-0.5">
                {t('dashboard.view_all')} <ChevronRight className="w-3 h-3" />
              </Link>
            </div>

            {/* Parc au-delà du plafond : les compteurs des pastilles restent justes,
                la table ne montre qu'une partie — on le dit plutôt que de laisser
                lire un parc complet. */}
            {noc?.rows_truncated && (
              <p className="mx-5 mb-3 shrink-0 text-xs text-warning bg-warning/10 border border-warning/25 rounded-lg px-3 py-2">
                {t('dashboard.players_truncated', { shown: rows.length, total: noc.total })}
              </p>
            )}

            {/* Corps scrollable : le nombre de lignes visibles suit la hauteur dispo,
                l'excédent scrolle sans bouger le reste de la page (Q5/Q6). */}
            <div className="flex-1 min-h-0 overflow-y-auto">
              <table className="w-full text-sm">
                <thead className="sticky top-0 z-10 bg-surface">
                  <tr className="border-y border-white/5">
                    <th className="text-left font-medium px-5 py-2.5">{t('dashboard.col_player')}</th>
                    <th className="text-left font-medium px-5 py-2.5">{t('dashboard.col_os')}</th>
                    <th className="text-left font-medium px-5 py-2.5">{t('dashboard.col_site')}</th>
                    <th className="text-left font-medium px-5 py-2.5">{t('dashboard.col_status')}</th>
                    <th className="text-left font-medium px-5 py-2.5">{t('dashboard.col_last_seen')}</th>
                    <th className="text-left font-medium px-5 py-2.5">{t('dashboard.col_version')}</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredRows.map((r) => {
                    const clickable = r.id_site != null && placedIds.has(r.id_site)
                    return (
                      <tr key={r.id_player} className="border-b border-white/5 last:border-0 hover:bg-surface-hover/60 transition-sf">
                        <td className="px-5 py-3 text-gray-900 font-medium">{r.player_name}</td>
                        <td className="px-5 py-3 text-gray-500">{r.player_os ?? '—'}</td>
                        <td className="px-5 py-3">
                          {r.site_name ? (
                            clickable ? (
                              <button
                                onClick={() => focusSite(r.id_site)}
                                title={t('dashboard.site_focus')}
                                className="text-accent-hover hover:underline"
                              >
                                {r.site_name}
                              </button>
                            ) : (
                              <span className="text-gray-600">{r.site_name}</span>
                            )
                          ) : (
                            <span className="text-gray-600">—</span>
                          )}
                        </td>
                        <td className="px-5 py-3">
                          <StatusBadge status={r.status as 'online' | 'offline' | 'warning' | 'maintenance' | 'critical'} label={t(`players.status_${r.status}`)} />
                        </td>
                        <td className="px-5 py-3 text-gray-500 tabular-nums">{relTime(r.last_seen_at)}</td>
                        <td className="px-5 py-3 text-gray-500 tabular-nums">{r.player_version ?? '—'}</td>
                      </tr>
                    )
                  })}
                  {filteredRows.length === 0 && (
                    <tr><td colSpan={6} className="px-5 py-8 text-center text-xs text-gray-500">{t('dashboard.players_empty')}</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </main>

        {/* ── Panneau latéral Média ────────────────────────────────────────── */}
        <aside className="hidden 2xl:flex w-80 shrink-0 flex-col border-l border-white/5 bg-surface/30 overflow-y-auto">
          <div className="px-5 py-4 border-b border-white/5 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <ImageIcon className="w-4 h-4 text-accent-hover" />
              <h3 className="text-sm font-semibold text-gray-900">{t('nav.media')}</h3>
            </div>
            <Link href="/media" className="text-xs text-accent-hover hover:underline flex items-center gap-0.5">
              {t('dashboard.view_all')} <ChevronRight className="w-3 h-3" />
            </Link>
          </div>

          {/* Par type : compteur total puis les 3 contenus récents (nom à droite, défilant si long) */}
          <div className="p-3 space-y-4">
            {(mediaPanel?.groups ?? []).map((g) => {
              const tile = MEDIA_TILE[g.media_type] ?? '#8B949E'
              return (
                <div key={g.media_type}>
                  <div className="flex items-center gap-2 px-2 mb-1.5">
                    <span className="w-1.5 h-4 rounded-full shrink-0" style={{ background: tile }} />
                    <span className="text-sm font-semibold text-gray-900">{t(`media.type_${g.media_type}`)}</span>
                    <span className="text-xs text-gray-500">· {g.total} {t('dashboard.media_total')}</span>
                  </div>
                  <div className="space-y-1">
                    {g.items.map((m) => (
                      <Link
                        key={m.id_media}
                        href="/media"
                        className="flex items-center gap-2.5 rounded-lg px-2 py-1.5 hover:bg-surface-hover transition-sf"
                        title={m.media_name}
                      >
                        <MediaThumb thumbnailUrl={m.thumbnail_url} type={g.media_type} tile={tile} />
                        <MarqueeName text={m.media_name} />
                      </Link>
                    ))}
                  </div>
                </div>
              )
            })}
            {(!mediaPanel || mediaPanel.groups.length === 0) && (
              <p className="text-xs text-gray-500 text-center py-6">{t('dashboard.media_empty')}</p>
            )}
          </div>
        </aside>
      </div>
    </>
  )
}

// ── Sous-composants ───────────────────────────────────────────────────────────

// Vignette média : image si dispo, sinon icône colorée par type (comme /media).
function MediaThumb({ thumbnailUrl, type, tile }: { thumbnailUrl: string | null; type: string; tile: string }) {
  const Icon = MEDIA_ICON[type] ?? Film
  return (
    <span className="w-9 h-9 rounded-md overflow-hidden bg-surface border border-white/5 shrink-0 flex items-center justify-center">
      {thumbnailUrl ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={thumbnailUrl} alt="" className="w-full h-full object-cover" />
      ) : (
        <Icon className="w-4 h-4" style={{ color: tile }} />
      )}
    </span>
  )
}

function KpiCardView({ card, label, rgb, color, Icon, upGood, sub, t }: {
  card: KpiCard; label: string; rgb: string; color: string
  Icon: React.ComponentType<{ className?: string }>; upGood: boolean; sub: string
  t: ReturnType<typeof useTranslations>
}) {
  const d = card.delta_pct
  const hasDelta = d !== null && d !== undefined
  const isUp = (d ?? 0) >= 0
  const good = hasDelta ? (isUp ? upGood : !upGood) : true
  const DeltaIcon = isUp ? TrendingUp : TrendingDown

  return (
    <div className="sf-panel sf-topline p-4" style={{ ['--sf-topline-rgb' as string]: rgb }}>
      <div className="flex items-start justify-between">
        <p className="text-[11px] uppercase tracking-wide text-gray-400 font-medium">{label}</p>
        <span className="w-7 h-7 rounded-lg flex items-center justify-center border" style={{ background: `rgb(${rgb} / 0.12)`, borderColor: `rgb(${rgb} / 0.25)`, color: `rgb(${rgb})` }}>
          <Icon className="w-3.5 h-3.5" />
        </span>
      </div>
      <p className="text-[2rem] font-semibold text-gray-900 mt-1.5 leading-none tabular-nums">{card.value.toLocaleString()}</p>
      <div className="flex items-end justify-between mt-3">
        {hasDelta ? (
          <span className={`inline-flex items-center gap-0.5 text-[11px] font-medium px-1.5 py-0.5 rounded ${good ? 'text-success bg-green-500/10' : 'text-danger bg-red-500/10'}`}>
            <DeltaIcon className="w-3 h-3" />
            {isUp ? '+' : ''}{d}%
          </span>
        ) : (
          <span className="text-[11px] text-gray-500">{t('dashboard.kpi_no_history')}</span>
        )}
        <Sparkline data={card.spark} color={color} width={84} height={30} />
      </div>
      <p className="text-[11px] text-gray-500 mt-2">{sub}</p>
    </div>
  )
}

// Ligne d'activité : tronquée (une ligne), défile au survol si elle déborde. Même mécanique que
// MarqueeName mais accepte des enfants (action en gras + ressource) au lieu d'un simple texte.
function ActivityText({ children, dep }: { children: React.ReactNode; dep: string }) {
  const hostRef = useRef<HTMLSpanElement>(null)
  const innerRef = useRef<HTMLSpanElement>(null)
  useEffect(() => {
    const host = hostRef.current, inner = innerRef.current
    if (!host || !inner) return
    const overflow = inner.scrollWidth - host.clientWidth
    if (overflow > 4) {
      host.style.setProperty('--sf-marquee-x', `-${overflow + 8}px`)
      host.style.setProperty('--sf-marquee-dur', `${Math.max(3, overflow / 35)}s`)
    } else {
      host.style.removeProperty('--sf-marquee-x')
    }
  }, [dep])
  return (
    <span ref={hostRef} className="sf-scroll-host block text-xs text-gray-800 leading-snug">
      <span ref={innerRef} className="sf-scroll-inner">{children}</span>
    </span>
  )
}

// Nom de média qui défile au survol UNIQUEMENT s'il déborde (pose --sf-marquee-x mesuré).
function MarqueeName({ text }: { text: string }) {
  const hostRef = useRef<HTMLSpanElement>(null)
  const innerRef = useRef<HTMLSpanElement>(null)
  useEffect(() => {
    const host = hostRef.current, inner = innerRef.current
    if (!host || !inner) return
    const overflow = inner.scrollWidth - host.clientWidth
    if (overflow > 4) {
      host.style.setProperty('--sf-marquee-x', `-${overflow + 8}px`)
      host.style.setProperty('--sf-marquee-dur', `${Math.max(3, overflow / 35)}s`)
    } else {
      host.style.removeProperty('--sf-marquee-x')
    }
  }, [text])
  return (
    <span ref={hostRef} className="sf-scroll-host flex-1 min-w-0 text-xs text-gray-800">
      <span ref={innerRef} className="sf-scroll-inner">{text}</span>
    </span>
  )
}

// Icône + drapeau erreur d'une ligne d'activité selon le type de ressource / l'action.
function activityMeta(resourceType: string, action: string): { Icon: React.ComponentType<{ className?: string }>; err: boolean } {
  const err = /offline|error|fail|cancel|reject|delete|critical/i.test(action)
  const map: Record<string, React.ComponentType<{ className?: string }>> = {
    playlist: ListVideo, campaign: Megaphone, player: Monitor, media: Film,
    schedule: Calendar, event: Zap, approval: ClipboardCheck, upload: Upload,
  }
  return { Icon: map[resourceType] ?? ActivityIcon, err }
}

// Humanise une action d'audit : "assistant_add_playlist_item" → "Assistant add playlist item".
function prettyAction(action: string): string {
  const s = action.replace(/^local_control:/, '').replace(/[_:]/g, ' ').trim()
  return s.charAt(0).toUpperCase() + s.slice(1)
}
