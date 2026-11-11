'use client'

import { clsx } from 'clsx'
import { useTranslations } from 'next-intl'
import { Monitor, Trash2 } from 'lucide-react'
import StatusBadge from '@/components/ui/StatusBadge'
import Can from '@/components/auth/Can'
import type { PlayerRead } from '@/types/api'

/**
 * Vue LISTE du parc (demande d'Ed, audit du menu Players 2026-07-31 : « j'apprécierai
 * d'avoir une vue liste et un filtre à l'image de ce que l'on a fait pour Media »).
 *
 * Ce que les cartes ne peuvent pas donner : COMPARER et TRIER. Une vignette de capture
 * d'écran montre bien ce qui est à l'antenne, mais retrouver « les trois écrans en 1.5.14
 * qui n'ont pas répondu depuis hier » dans 200 tuiles est impossible. La liste expose donc
 * les colonnes sur lesquelles on cherche vraiment — nom, site, OS, version, statut, dernier
 * contact — et les rend triables.
 *
 * Les cellules restent SOBRES (une seule colonne d'état) : sinon une ligne redevient aussi
 * haute qu'une carte et on perd la densité qui fait tout l'intérêt de cette vue.
 */

export type SortKey = 'name' | 'site' | 'os' | 'version' | 'status' | 'seen'
export type SortDir = 'asc' | 'desc'

interface Props {
  players: PlayerRead[]
  sites: { id_site: number; site_name: string }[]
  sort: { key: SortKey; dir: SortDir }
  onSort: (key: SortKey) => void
  onSelect: (p: PlayerRead) => void
  onDelete: (id: number) => void
  emptyLabel: string
}

/** Écoulé depuis le dernier contact — l'unité avec laquelle on raisonne en exploitation. */
export function sinceLabel(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const s = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h`
  return `${Math.floor(s / 86400)}d`
}

/** Statut → variante de badge. `critical` se lit comme `offline` (bucketing NOC/dashboard). */
export function badgeStatus(status: string): 'online' | 'warning' | 'offline' | 'pending' {
  if (status === 'online') return 'online'
  if (status === 'warning') return 'warning'
  if (status === 'maintenance') return 'pending'
  return 'offline'
}

export default function PlayerListView({
  players, sites, sort, onSort, onSelect, onDelete, emptyLabel,
}: Props) {
  const t = useTranslations('players')
  const siteName = new Map(sites.map((s) => [s.id_site, s.site_name]))

  if (players.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 gap-3 text-gray-400">
        <Monitor className="w-10 h-10" />
        <p className="text-sm">{emptyLabel}</p>
      </div>
    )
  }

  const COLS: { key: SortKey | null; label: string; cls: string }[] = [
    { key: 'name', label: t('col_name'), cls: '' },
    { key: 'site', label: t('col_site'), cls: 'hidden md:table-cell' },
    { key: 'os', label: t('col_os'), cls: 'hidden lg:table-cell' },
    { key: 'version', label: t('col_version'), cls: 'hidden xl:table-cell' },
    { key: null, label: t('col_ip'), cls: 'hidden xl:table-cell' },
    { key: 'seen', label: t('col_seen'), cls: 'hidden lg:table-cell' },
    { key: 'status', label: t('col_status'), cls: '' },
  ]

  return (
    <table className="w-full text-sm" data-testid="players-list">
      <thead className="sticky top-0 z-10 bg-surface">
        <tr className="border-y border-white/5">
          {COLS.map((c) => (
            <th key={c.label} className={`text-left font-medium px-4 py-2.5 ${c.cls}`}>
              {c.key ? (
                <button
                  data-testid={`players-sort-${c.key}`}
                  onClick={() => onSort(c.key!)}
                  className="inline-flex items-center gap-1 hover:text-accent transition-sf"
                >
                  {c.label}
                  {sort.key === c.key && (
                    <span className="text-[10px]">{sort.dir === 'asc' ? '▲' : '▼'}</span>
                  )}
                </button>
              ) : c.label}
            </th>
          ))}
          <th className="w-12" />
        </tr>
      </thead>
      <tbody>
        {players.map((p) => (
          <tr
            key={p.id_player}
            data-testid={`player-row-${p.id_player}`}
            onClick={() => onSelect(p)}
            className="border-b border-white/5 last:border-0 cursor-pointer hover:bg-surface-hover/60 transition-sf"
          >
            <td className="px-4 py-2">
              <span className="block truncate text-gray-900" title={p.player_name}>
                {p.player_name}
              </span>
            </td>
            <td className="px-4 py-2 hidden md:table-cell text-gray-500 truncate">
              {p.id_site != null ? (siteName.get(p.id_site) ?? `#${p.id_site}`) : '—'}
            </td>
            <td className="px-4 py-2 hidden lg:table-cell text-gray-500">{p.player_os ?? '—'}</td>
            <td className="px-4 py-2 hidden xl:table-cell text-gray-500 tabular-nums">
              {p.player_version ?? '—'}
            </td>
            <td className="px-4 py-2 hidden xl:table-cell text-gray-500 tabular-nums">
              {p.ip_address ?? '—'}
            </td>
            <td className="px-4 py-2 hidden lg:table-cell text-gray-500 tabular-nums">
              {sinceLabel(p.last_seen_at)}
            </td>
            <td className="px-4 py-2">
              <StatusBadge status={badgeStatus(p.status)} label={t(`status_${p.status}`)} />
            </td>
            <td className="px-4 py-2 text-right">
              <Can admin>
                <button
                  onClick={(e) => { e.stopPropagation(); onDelete(p.id_player) }}
                  className="p-1 text-gray-300 hover:text-danger transition-sf"
                  title={t('delete')}
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </Can>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** Tri appliqué à la liste — exporté pour que la page reste la seule source de vérité. */
export function sortPlayers(
  players: PlayerRead[], sort: { key: SortKey; dir: SortDir },
  siteName: Map<number, string>,
): PlayerRead[] {
  const dir = sort.dir === 'asc' ? 1 : -1
  const val = (p: PlayerRead): string | number => {
    switch (sort.key) {
      case 'site': return p.id_site != null ? (siteName.get(p.id_site) ?? '') : ''
      case 'os': return (p.player_os ?? '').toLowerCase()
      case 'version': return p.player_version ?? ''
      case 'status': return p.status
      // Dernier contact : on trie sur l'INSTANT, pas sur le libellé « 3h » (qui trierait
      // en ordre alphabétique — 10m avant 3h). Jamais vu = le plus ancien possible.
      case 'seen': return p.last_seen_at ? new Date(p.last_seen_at).getTime() : 0
      default: return p.player_name.toLowerCase()
    }
  }
  return [...players].sort((a, b) => {
    const va = val(a); const vb = val(b)
    if (va < vb) return -dir
    if (va > vb) return dir
    return a.player_name.localeCompare(b.player_name)
  })
}
