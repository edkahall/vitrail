'use client'

import Image from 'next/image'
import { clsx } from 'clsx'
import { useState } from 'react'
import { useTranslations } from 'next-intl'
import { useFolderLabel } from '@/lib/folderLabel'
import { Check, Lock, Pencil, ShieldAlert, Trash2 } from 'lucide-react'
import type { MediaListItem, FolderRead } from '@/types/api'
import { GovernedBadge } from '@/components/governance/NodeSelect'
import { MediaPlaceholder, TranscodeStatusBadge, formatSize } from './MediaGrid'
import MediaUsageHint from './MediaUsageHint'
import EncodeProgressBar from './EncodeProgressBar'

/**
 * Vue LISTE de la médiathèque (retour Ed, audit du menu Média 2026-07-28 : « l'option
 * mode liste des contenus est intéressante »).
 *
 * Ce que la grille ne peut pas donner : comparer et TRIER. Une vignette montre bien un
 * visuel, mais retrouver « le PDF de 40 Mo uploadé la semaine dernière » dans 300 tuiles
 * est impossible. La liste expose donc les colonnes sur lesquelles on cherche vraiment —
 * type, taille, dossier, date — et les rend triables.
 *
 * Les cellules restent SOBRES : les statuts et badges vivent dans une seule colonne, sinon
 * une ligne redevient aussi haute qu'une carte et on perd la densité qui fait tout l'intérêt.
 */

export type SortKey = 'name' | 'type' | 'size' | 'date'
export type SortDir = 'asc' | 'desc'

interface Props {
  items: MediaListItem[]
  folders: FolderRead[]
  sort: { key: SortKey; dir: SortDir }
  onSort: (key: SortKey) => void
  onSelect: (item: MediaListItem) => void
  /** Raccourci d'édition (demande d'Ed) — le clic sur la ligne ouvre la visionneuse. */
  onEdit: (item: MediaListItem) => void
  onDelete: (item: MediaListItem) => void
  /** Résumé de diffusion chargé en lot par la page (une requête pour tout l'écran). */
  usage?: Record<string, { playlists: number; screens: number }>
  selectMode?: boolean
  selectedIds?: Set<number>
}

function relDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleDateString(undefined, { day: '2-digit', month: '2-digit', year: 'numeric' })
}

export default function MediaListView({
  items, folders, sort, onSort, onSelect, onEdit, onDelete, usage, selectMode = false, selectedIds,
}: Props) {
  const t = useTranslations('media')
  const [hovered, setHovered] = useState<number | null>(null)
  const { path: folderPath } = useFolderLabel()
  const foldersById = new Map(folders.map((f) => [f.id_folder, f]))
  const folderName = new Map(folders.map((f) => [f.id_folder, folderPath(f, foldersById)]))

  if (items.length === 0) {
    return <p className="text-sm text-gray-400 text-center py-16">{t('no_media')}</p>
  }

  const COLS: { key: SortKey | null; label: string; cls: string }[] = [
    { key: 'name', label: t('col_name'), cls: '' },
    { key: 'type', label: t('col_type'), cls: 'hidden md:table-cell' },
    { key: 'size', label: t('col_size'), cls: 'hidden lg:table-cell text-right' },
    { key: null, label: t('col_folder'), cls: 'hidden xl:table-cell' },
    { key: 'date', label: t('col_added'), cls: 'hidden lg:table-cell' },
    { key: null, label: t('col_state'), cls: '' },
  ]

  return (
    <table className="w-full text-sm">
      <thead className="sticky top-0 z-10 bg-surface">
        <tr className="border-y border-white/5">
          {COLS.map((c) => (
            <th key={c.label} className={`text-left font-medium px-4 py-2.5 ${c.cls}`}>
              {c.key ? (
                <button
                  onClick={() => onSort(c.key!)}
                  className="inline-flex items-center gap-1 hover:text-accent transition-sf"
                >
                  {c.label}
                  {sort.key === c.key && <span className="text-[10px]">{sort.dir === 'asc' ? '▲' : '▼'}</span>}
                </button>
              ) : c.label}
            </th>
          ))}
          <th className="w-20" />
        </tr>
      </thead>
      <tbody>
        {items.map((item) => {
          const selected = selectedIds?.has(item.id_media) ?? false
          return (
            <tr
              key={item.id_media}
              onClick={() => onSelect(item)}
              onMouseEnter={() => setHovered(item.id_media)}
              onMouseLeave={() => setHovered((h) => (h === item.id_media ? null : h))}
              className={clsx(
                'border-b border-white/5 last:border-0 cursor-pointer transition-sf',
                selected ? 'bg-accent/10' : 'hover:bg-surface-hover/60',
              )}
            >
              <td className="px-4 py-2">
                <div className="flex items-center gap-3 min-w-0">
                  {selectMode && (
                    <span className={clsx(
                      'w-4 h-4 rounded border shrink-0 flex items-center justify-center',
                      selected ? 'bg-accent border-accent' : 'border-gray-400',
                    )}>
                      {selected && <Check className="w-3 h-3 text-white" />}
                    </span>
                  )}
                  <span className="relative w-10 h-10 rounded overflow-hidden bg-surface border border-white/5 shrink-0 flex items-center justify-center">
                    {item.thumbnail_url ? (
                      <Image
                        src={item.thumbnail_url}
                        alt=""
                        fill
                        sizes="40px"
                        className="object-cover"
                        /* `unoptimized` comme dans la grille : les vignettes vivent sur
                           MinIO, qui n'est pas déclaré dans `images.remotePatterns` de
                           `next.config`. Sans ce drapeau, Next les fait passer par son
                           optimiseur, qui refuse l'hôte — la vignette échoue en silence et
                           laisse un carré vide (constat d'Ed en production, 2026-07-28). */
                        unoptimized
                      />
                    ) : (
                      <MediaPlaceholder type={item.media_type} />
                    )}
                  </span>
                  <span className="min-w-0">
                    <span className="block truncate text-gray-900">{item.media_name}</span>
                    {/* L'usage se charge au SURVOL, pas au rendu : sinon afficher 100 lignes
                        déclencherait 100 analyses de diffusion. */}
                    <MediaUsageHint
                      idMedia={item.id_media}
                      hovered={hovered === item.id_media}
                      summary={usage?.[String(item.id_media)]}
                    />
                  </span>
                </div>
              </td>
              <td className="px-4 py-2 text-gray-500 hidden md:table-cell">{t(`type_${item.media_type}`)}</td>
              <td className="px-4 py-2 text-gray-500 tabular-nums text-right hidden lg:table-cell">
                {formatSize(item.served_size_bytes ?? item.file_size_bytes) ?? '—'}
              </td>
              <td className="px-4 py-2 text-gray-500 truncate hidden xl:table-cell">
                {item.id_folder ? (folderName.get(item.id_folder) ?? '—') : t('folder_unfiled')}
              </td>
              <td className="px-4 py-2 text-gray-500 tabular-nums hidden lg:table-cell">
                {relDate(item.created_at)}
              </td>
              <td className="px-4 py-2">
                <div className="flex items-center gap-1.5 flex-wrap">
                  <TranscodeStatusBadge status={item.transcode_status} />
                  {/* Barre + pourcentage + temps restant : la vue liste est celle qu'on
                      regarde quand on attend la fin d'un lot d'uploads. */}
                  {item.encode_progress && (
                    <EncodeProgressBar progress={item.encode_progress} className="w-32" />
                  )}
                  {item.is_blocked && (
                    <span title={item.block_reason ?? ''} className="text-danger"><ShieldAlert className="w-3.5 h-3.5" /></span>
                  )}
                  {item.not_before_utc && (
                    <span title={t('embargo')} className="text-warning"><Lock className="w-3.5 h-3.5" /></span>
                  )}
                  {item.id_governance_node != null && (
                    <GovernedBadge idNode={item.id_governance_node} readonly={item.governed_readonly} />
                  )}
                </div>
              </td>
              <td className="px-4 py-2">
                {!selectMode && !item.governed_readonly && (
                  <div className="flex items-center justify-end gap-0.5">
                  <button
                    onClick={(e) => { e.stopPropagation(); onEdit(item) }}
                    title={t('viewer_edit')}
                    aria-label={t('viewer_edit')}
                    className="p-1.5 rounded text-gray-400 hover:text-accent hover:bg-accent/10 transition-sf"
                  >
                    <Pencil className="w-3.5 h-3.5" />
                  </button>
                  <button
                    data-write-action
                    onClick={(e) => { e.stopPropagation(); onDelete(item) }}
                    title={t('delete')}
                    className="p-1.5 rounded text-gray-400 hover:text-danger hover:bg-red-500/10 transition-sf"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                  </div>
                )}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
