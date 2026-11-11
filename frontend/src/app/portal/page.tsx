'use client'

// S7 B2 — accueil du portail annonceur, façon « Mission Control » orientée annonceur :
// compteurs (campagnes / players ciblés / PoP), État du parc + activité (SES campagnes),
// puis la liste de SES campagnes en 2 colonnes (visuelles | audio). Tout est scopé
// server-side à l'annonceur lié (B1 + /portal/overview).

import Link from 'next/link'
import { useTranslations } from 'next-intl'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, BadgeEuro, CalendarRange, Clapperboard,
  Megaphone, Monitor, MonitorOff, Music, PlayCircle, Plus,
  ListVideo, Trash2, Zap, ClipboardCheck, Upload,
} from 'lucide-react'
import Button from '@/components/ui/Button'
import SectionHeader from '@/components/ui/SectionHeader'
import StatusBadge from '@/components/ui/StatusBadge'
import { useCollapse } from '@/lib/useCollapse'
import FleetMap, { type GeoSite } from '@/components/dashboard/FleetMap'
import api from '@/lib/api'

interface PortalCampaign {
  id_campaign: number
  campaign_name: string
  status: string
  starts_on: string
  ends_on: string
  sov_percent: number
  campaign_kind?: string
  spots_per_hour?: number | null
  creative_count: number
  approval_request?: { id_request: number; status: string } | null
}

interface Overview {
  campaigns: { total: number; active: number; visual: number; audio: number }
  players: { total: number; online: number; offline: number; warning: number; maintenance: number }
  proof_of_play_24h: number
  sites: GeoSite[]
  activity: { id: number; action: string; resource_type: string; resource_id: string | null; user_name: string | null; created_at: string }[]
}

const BADGE: Record<string, 'online' | 'info' | 'pending' | 'warning' | 'done' | 'offline'> = {
  live: 'online', scheduled: 'info', draft: 'pending',
  paused: 'warning', completed: 'done', cancelled: 'offline',
}

function relTime(iso: string): string {
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h`
  return `${Math.floor(s / 86400)}d`
}

function prettyAction(action: string): string {
  const s = action.replace(/[_:]/g, ' ').trim()
  return s.charAt(0).toUpperCase() + s.slice(1)
}

export default function PortalHomePage() {
  const t = useTranslations('portal')
  const tc = useTranslations('campaigns')
  const qc = useQueryClient()

  // Suppression d'un BROUILLON par l'annonceur (demande d'Ed, 31/07). Le serveur reste
  // l'autorité : il refuse une campagne publiée, et une campagne soumise tant que le
  // retailer ne rend pas la main — on affiche alors son message tel quel.
  const deleteDraft = useMutation({
    mutationFn: (id: number) => api.delete(`/portal/campaigns/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['portal-campaigns'] })
      qc.invalidateQueries({ queryKey: ['portal-overview'] })
    },
    onError: (e: unknown) => {
      const d = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      alert(typeof d === 'string' ? d : t('delete_failed'))
    },
  })

  const { data: campaigns = [], isLoading } = useQuery<PortalCampaign[]>({
    queryKey: ['portal-campaigns'],
    queryFn: () => api.get<PortalCampaign[]>('/campaigns/').then((r) => r.data),
  })
  const { data: ov } = useQuery<Overview>({
    queryKey: ['portal-overview'],
    queryFn: () => api.get<Overview>('/portal/overview').then((r) => r.data),
    refetchInterval: 30000,
  })

  const [kpisOpen, toggleKpis] = useCollapse('sf-portal-kpis')
  const [fleetOpen, toggleFleet] = useCollapse('sf-portal-fleet')

  const visual = campaigns.filter((c) => (c.campaign_kind ?? 'visual') !== 'audio')
  const audio = campaigns.filter((c) => c.campaign_kind === 'audio')

  const kpis = [
    { label: t('kpi_campaigns'), value: ov?.campaigns.total ?? 0, rgb: '139 92 246', Icon: Megaphone,
      sub: t('kpi_campaigns_active', { n: ov?.campaigns.active ?? 0 }) },
    { label: t('kpi_online'), value: ov?.players.online ?? 0, rgb: '37 99 235', Icon: Monitor, sub: '' },
    { label: t('kpi_offline'), value: ov?.players.offline ?? 0, rgb: '239 68 68', Icon: MonitorOff, sub: '' },
    { label: t('kpi_warning'), value: ov?.players.warning ?? 0, rgb: '245 158 11', Icon: AlertTriangle, sub: '' },
    { label: t('kpi_pop'), value: ov?.proof_of_play_24h ?? 0, rgb: '16 185 129', Icon: PlayCircle, sub: '' },
  ] as const

  return (
    // Hauteur bornée par le layout : les colonnes de campagnes défilent en interne
    // (comme le dashboard CMS).
    //
    // ⚠️ `overflow-y-auto` ici est un GARDE-FOU, pas un retour au scroll de page :
    // les sections « Indicateurs » et « État du parc » sont `shrink-0`, donc sur
    // un écran court (1280×720 = portable courant) elles consomment TOUTE la
    // hauteur et le bloc campagnes tombait à 0 px. Ses cartes continuaient de
    // peindre par-dessus le pied de page, qui interceptait alors les clics :
    // l'annonceur ne pouvait plus ouvrir ses campagnes. Mesuré le 2026-07-20.
    // En hauteur suffisante, rien ne change (tout tient, aucun défilement).
    <div className="h-full flex flex-col min-h-0 gap-4 sf-stagger overflow-y-auto">
      {/* Hero + action (fixe) */}
      <div className="shrink-0 flex items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-gray-900">{t('campaigns_title')}</h1>
          <p className="text-sm text-gray-400">{t('campaigns_hint')}</p>
        </div>
        <Link href="/portal/campaigns/new">
          <Button size="sm"><Plus className="w-4 h-4 mr-1" />{t('new_campaign')}</Button>
        </Link>
      </div>

      {/* ── Section repliable : Indicateurs ───────────────────────────────────── */}
      <div className="shrink-0">
        <SectionHeader
          title={t('section_kpis')}
          open={kpisOpen}
          onToggle={toggleKpis}
          collapseLabel={t('section_collapse')}
          expandLabel={t('section_expand')}
        />
        {kpisOpen && (
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3 mt-3">
            {kpis.map((k) => (
              <div key={k.label} className="sf-panel sf-topline p-4" style={{ ['--sf-topline-rgb' as string]: k.rgb }}>
                <div className="flex items-start justify-between">
                  <p className="text-[11px] uppercase tracking-wide text-gray-400 font-medium">{k.label}</p>
                  <span className="w-7 h-7 rounded-lg flex items-center justify-center border shrink-0"
                        style={{ background: `rgb(${k.rgb} / 0.12)`, borderColor: `rgb(${k.rgb} / 0.25)`, color: `rgb(${k.rgb})` }}>
                    <k.Icon className="w-3.5 h-3.5" />
                  </span>
                </div>
                <p className="text-[2rem] font-semibold text-gray-900 mt-1.5 leading-none tabular-nums">{k.value.toLocaleString()}</p>
                {k.sub && <p className="text-[11px] text-gray-500 mt-2">{k.sub}</p>}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ── Section repliable : État du parc (ciblé) + activité ────────────────── */}
      <div className="shrink-0">
        <SectionHeader
          title={t('section_fleet_activity')}
          open={fleetOpen}
          onToggle={toggleFleet}
          collapseLabel={t('section_collapse')}
          expandLabel={t('section_expand')}
        />
        {fleetOpen && (
          <div className="grid grid-cols-1 xl:grid-cols-3 gap-5 mt-3">
            <div className="sf-panel p-5 xl:col-span-2 flex flex-col min-h-[320px]">
              <h3 className="text-sm font-semibold text-gray-900 mb-4">{t('fleet_title')}</h3>
              <FleetMap
                sites={ov?.sites ?? []}
                counts={{ online: ov?.players.online, warning: ov?.players.warning,
                          offline: ov?.players.offline, maintenance: ov?.players.maintenance }}
              />
            </div>

            <div className="sf-panel p-5 flex flex-col">
              <h3 className="text-sm font-semibold text-gray-900 mb-4">{t('activity_title')}</h3>
              <div className="space-y-3 overflow-y-auto max-h-[280px] pr-1">
                {(ov?.activity ?? []).map((it) => {
                  const err = /offline|error|fail|cancel|reject|delete/i.test(it.action)
                  const Icon = /approv|reject|submit/i.test(it.action) ? ClipboardCheck
                    : /creativ|upload/i.test(it.action) ? Upload
                      : /publish|start/i.test(it.action) ? Zap : ListVideo
                  return (
                    <div key={it.id} className="flex items-start gap-2.5">
                      <span className={`mt-0.5 shrink-0 w-6 h-6 rounded-md flex items-center justify-center ${err ? 'bg-red-500/15' : 'bg-white/5'}`}>
                        <Icon className={`w-3.5 h-3.5 ${err ? 'text-danger' : 'text-gray-400'}`} />
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="text-xs text-gray-800 leading-snug">
                          <span className="font-medium">{prettyAction(it.action)}</span>
                          {it.resource_id ? <span className="text-gray-500"> · #{it.resource_id}</span> : null}
                        </p>
                        <p className="text-[11px] text-gray-500 mt-0.5">
                          {relTime(it.created_at)}{it.user_name ? ` · ${it.user_name}` : ''}
                        </p>
                      </div>
                    </div>
                  )
                })}
                {(!ov || ov.activity.length === 0) && (
                  <p className="text-xs text-gray-500 text-center py-6">{t('activity_empty')}</p>
                )}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* ── Campagnes : remplit la hauteur restante, scroll INTERNE par colonne ──
          `min-h-[320px]` = plancher : sans lui, `flex-1` vaut 0 dès que les
          sections repliables au-dessus remplissent l'écran (cf. commentaire du
          conteneur racine). C'est la surface principale du portail : elle ne doit
          jamais disparaître. */}
      <div className="flex-1 min-h-[320px]">
        {isLoading ? (
          <p className="text-sm text-gray-400">…</p>
        ) : campaigns.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 gap-3 text-gray-400">
            <BadgeEuro className="w-10 h-10" />
            <p className="text-sm">{t('no_campaigns')}</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-5 h-full min-h-0">
            <CampaignColumn title={t('section_visual')} Icon={Clapperboard}
                            items={visual} empty={t('section_visual_empty')} t={t} tc={tc}
                            onDelete={(id) => deleteDraft.mutate(id)} />
            <CampaignColumn title={t('section_audio')} Icon={Music}
                            items={audio} empty={t('section_audio_empty')} t={t} tc={tc}
                            onDelete={(id) => deleteDraft.mutate(id)} />
          </div>
        )}
      </div>
    </div>
  )
}

function CampaignColumn({
  title, Icon, items, empty, t, tc, onDelete,
}: {
  title: string
  Icon: React.ComponentType<{ className?: string }>
  items: PortalCampaign[]
  empty: string
  t: ReturnType<typeof useTranslations>
  tc: ReturnType<typeof useTranslations>
  onDelete: (id: number) => void
}) {
  return (
    <div className="sf-panel p-4 flex flex-col min-h-0">
      <div className="flex items-center gap-2 mb-3 shrink-0">
        <Icon className="w-4 h-4 text-accent-hover" />
        <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
        <span className="text-xs text-gray-500">· {items.length}</span>
      </div>
      <div className="space-y-2.5 flex-1 min-h-0 overflow-y-auto pr-1">
        {items.map((c) => {
          const isAudio = c.campaign_kind === 'audio'
          return (
            <Link
              key={c.id_campaign}
              href={c.status === 'draft'
                ? `/portal/campaigns/${c.id_campaign}/edit`
                : `/portal/campaigns/${c.id_campaign}`}
              className="block bg-surface border border-white/5 rounded-lg p-3 transition-sf hover:border-accent"
            >
              <div className="flex items-start justify-between gap-2">
                <span className="text-sm font-medium text-gray-900 truncate" title={c.campaign_name}>
                  {c.campaign_name}
                </span>
                <span className="flex items-center gap-1.5 shrink-0">
                  {c.approval_request && (
                    <StatusBadge
                      status={c.approval_request.status === 'changes_requested' ? 'warning' : 'pending'}
                      label={t(c.approval_request.status === 'changes_requested'
                        ? 'st_changes_requested' : 'st_in_review')} />
                  )}
                  <StatusBadge status={BADGE[c.status] ?? 'pending'} label={tc(`st_${c.status}`)} />
                  {/* Suppression réservée aux BROUILLONS non soumis : dès qu'une demande
                      est en revue, la campagne est chez le valideur (le serveur refuse
                      de toute façon — l'icône ne fait que ne pas le promettre). */}
                  {c.status === 'draft' && !c.approval_request && (
                    <button
                      type="button"
                      data-testid={`portal-delete-${c.id_campaign}`}
                      title={t('delete_draft')}
                      onClick={(e) => {
                        e.preventDefault()   // la carte entière est un lien
                        e.stopPropagation()
                        if (confirm(t('delete_draft_confirm', { name: c.campaign_name }))) {
                          onDelete(c.id_campaign)
                        }
                      }}
                      className="p-1 text-gray-400 hover:text-danger transition-sf"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  )}
                </span>
              </div>
              <div className="flex items-center gap-3 mt-2 text-[11px] text-gray-500 flex-wrap">
                <span className="inline-flex items-center gap-1">
                  <CalendarRange className="w-3 h-3" />{c.starts_on} → {c.ends_on}
                </span>
                <span>{isAudio ? t('spots_per_hour_short', { n: c.spots_per_hour ?? 0 }) : `SOV ${c.sov_percent}%`}</span>
                <span className="inline-flex items-center gap-1">
                  <Clapperboard className="w-3 h-3" />{c.creative_count}
                </span>
              </div>
            </Link>
          )
        })}
        {items.length === 0 && (
          <p className="text-xs text-gray-500 text-center py-6">{empty}</p>
        )}
      </div>
    </div>
  )
}
