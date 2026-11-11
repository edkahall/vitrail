'use client'

import { useMemo, useState } from 'react'
import { useTranslations } from 'next-intl'
import { useQuery } from '@tanstack/react-query'
import { Search, Server } from 'lucide-react'
import Topbar from '@/components/layout/Topbar'
import BackButton from '@/components/ui/BackButton'
import StatusBadge from '@/components/ui/StatusBadge'
import api from '@/lib/api'

interface NocRow {
  id_player: number; player_name: string; site_name: string | null
  player_os: string | null; player_version: string | null; status: string
  last_seen_at: string | null; disk_usage: number | null; alert_reasons: string[]
}
interface NocResponse {
  total: number; online: number; warning: number; offline: number; maintenance: number
  rows: NocRow[]
  // Le parc dépasse le plafond de lignes détaillées : les compteurs restent justes,
  // la table non — il faut le dire (une table tronquée se lit comme un parc complet).
  rows_truncated?: boolean
}

const STATUS_MAP: Record<string, 'online' | 'offline' | 'warning'> = {
  online: 'online', warning: 'warning', offline: 'offline', critical: 'offline',
  maintenance: 'warning',
}

function relTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso).getTime()
  if (Number.isNaN(d)) return '—'
  const s = Math.floor((Date.now() - d) / 1000)
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h`
  return `${Math.floor(s / 86400)}d`
}

export default function NocDashboardPage() {
  const t = useTranslations('reports')
  const [search, setSearch] = useState('')
  const [statusF, setStatusF] = useState('')
  const [osF, setOsF] = useState('')

  const { data } = useQuery({
    queryKey: ['analytics-noc'],
    queryFn: async () => (await api.get<NocResponse>('/analytics/noc')).data,
    // Page d'exploitation « live » : poll 15 s même onglet en arrière-plan + refetch au focus
    // (staleTime 0 court-circuite le staleTime global 60 s → le retour d'un player passe au vert seul).
    refetchInterval: 15000,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
    staleTime: 0,
  })

  const osList = useMemo(
    () => Array.from(new Set((data?.rows ?? []).map((r) => r.player_os).filter(Boolean))) as string[],
    [data])

  const rows = useMemo(() => {
    let r = data?.rows ?? []
    if (search) {
      const q = search.toLowerCase()
      r = r.filter((x) => x.player_name.toLowerCase().includes(q)
        || (x.site_name ?? '').toLowerCase().includes(q))
    }
    if (statusF) r = r.filter((x) => (statusF === 'offline'
      ? x.status === 'offline' || x.status === 'critical' : x.status === statusF))
    if (osF) r = r.filter((x) => x.player_os === osF)
    return r
  }, [data, search, statusF, osF])

  const cards = [
    { key: 'total', value: data?.total ?? 0, cls: 'text-gray-900' },
    { key: 'online', value: data?.online ?? 0, cls: 'text-success' },
    { key: 'warning', value: data?.warning ?? 0, cls: 'text-warning' },
    { key: 'offline', value: data?.offline ?? 0, cls: 'text-danger' },
    { key: 'maintenance', value: data?.maintenance ?? 0, cls: 'text-gray-500' },
  ]

  return (
    <>
      <Topbar title={t('noc_title')}>
        <BackButton />
      </Topbar>
      <main className="flex-1 overflow-y-auto">
        <div className="w-full px-6 py-8 space-y-6">
          <p className="text-sm text-gray-400">{t('noc_hint')}</p>

          <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
            {cards.map((c) => (
              <div key={c.key} className="bg-surface rounded-lg border border-gray-200 p-4 shadow-sf-card">
                <p className="text-xs text-gray-500 mb-1">{t(c.key === 'total' ? 'noc_total' : c.key)}</p>
                <p className={`text-2xl font-semibold ${c.cls}`}>{c.value}</p>
              </div>
            ))}
          </div>

          {/* Parc au-delà du plafond de lignes : les compteurs ci-dessus restent justes,
              la table ne montre qu'une partie — le taire ferait lire un parc complet. */}
          {data?.rows_truncated && (
            <p className="text-xs text-warning bg-warning/10 border border-warning/25 rounded-lg px-3 py-2">
              {t('noc_truncated', { shown: data.rows.length, total: data.total })}
            </p>
          )}

          {/* Filtres */}
          <div className="flex flex-wrap items-center gap-3">
            <div className="relative flex-1 min-w-[200px] max-w-sm">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none" />
              <input value={search} onChange={(e) => setSearch(e.target.value)}
                placeholder={t('noc_search')} className="w-full pl-9 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-accent/40 bg-surface" />
            </div>
            <select value={statusF} onChange={(e) => setStatusF(e.target.value)} className={selCls}>
              <option value="">{t('noc_all_status')}</option>
              <option value="online">{t('online')}</option>
              <option value="warning">{t('warning')}</option>
              <option value="offline">{t('offline')}</option>
              <option value="maintenance">{t('maintenance')}</option>
            </select>
            <select value={osF} onChange={(e) => setOsF(e.target.value)} className={selCls}>
              <option value="">{t('noc_all_os')}</option>
              {osList.map((os) => <option key={os} value={os}>{os}</option>)}
            </select>
          </div>

          <div className="bg-surface rounded-lg border border-gray-200 overflow-y-auto max-h-[60vh] shadow-sf-card">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
                <tr>
                  {['noc_col_player', 'noc_col_site', 'noc_col_os', 'noc_col_version',
                    'noc_col_status', 'noc_col_seen', 'noc_col_disk', 'noc_col_alerts'].map((k) => (
                    <th key={k} className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wide">
                      {t(k)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {rows.length === 0 && (
                  <tr><td colSpan={8} className="px-4 py-10 text-center text-gray-400">
                    <Server className="w-8 h-8 mx-auto mb-2 opacity-50" />{t('noc_empty')}</td></tr>
                )}
                {rows.map((r) => (
                  <tr key={r.id_player} className="hover:bg-gray-50/50">
                    <td className="px-4 py-2.5 font-medium text-gray-900">{r.player_name}</td>
                    <td className="px-4 py-2.5 text-gray-600">{r.site_name ?? '—'}</td>
                    <td className="px-4 py-2.5 text-gray-500 font-mono text-xs">{r.player_os ?? '—'}</td>
                    <td className="px-4 py-2.5 text-gray-500 font-mono text-xs">{r.player_version ?? '—'}</td>
                    <td className="px-4 py-2.5">
                      <StatusBadge status={STATUS_MAP[r.status] ?? 'offline'} label={t(r.status === 'critical' ? 'offline' : r.status)} />
                    </td>
                    <td className="px-4 py-2.5 text-gray-500">{relTime(r.last_seen_at)}</td>
                    <td className="px-4 py-2.5 text-gray-500">
                      {r.disk_usage != null ? `${Math.round(r.disk_usage)}%` : '—'}
                    </td>
                    <td className="px-4 py-2.5">
                      {r.alert_reasons.length === 0
                        ? <span className="text-gray-300">—</span>
                        : <span className="inline-flex flex-wrap gap-1">
                            {r.alert_reasons.map((a) => (
                              <span key={a} className="px-1.5 py-0.5 rounded bg-warning/10 text-warning text-[10px]">{a}</span>
                            ))}
                          </span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </main>
    </>
  )
}

const selCls = 'px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-accent/40 bg-surface text-gray-700'
