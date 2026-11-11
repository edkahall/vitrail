'use client'

import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslations } from 'next-intl'
import { useFolderLabel } from '@/lib/folderLabel'
import {
  Folder, FolderPlus, Layers, Inbox, Pencil, Trash2, Plus, ChevronRight, ChevronDown,
} from 'lucide-react'
import api from '@/lib/api'
import type { FolderRead } from '@/types/api'

export type FolderFilter = number | 'all' | 'unfiled'

export default function FolderTree({
  selected, onSelect, fill = false,
}: {
  selected: FolderFilter
  onSelect: (s: FolderFilter) => void
  /**
   * `true` : le composant occupe toute la hauteur que lui donne son parent, en
   * gardant l'en-tête (titre, « Tous les médias », « Non classés ») FIXE et en
   * ne faisant défiler que l'arbre des dossiers.
   *
   * Défaut `false` = hauteur naturelle, comportement historique — les appelants
   * qui l'enveloppent déjà dans leur propre conteneur à `max-h` (pickers des
   * drawers) ne changent pas de rendu.
   */
  fill?: boolean
}) {
  const t = useTranslations()
  const { label: folderLabel } = useFolderLabel()
  const qc = useQueryClient()

  const { data: folders = [] } = useQuery<FolderRead[]>({
    queryKey: ['media-folders'],
    queryFn: () => api.get<FolderRead[]>('/media/folders').then((r) => r.data),
  })

  // Arbre replié par défaut : seuls les dossiers racine sont visibles tant qu'on
  // n'a pas déplié leur parent.
  const [expanded, setExpanded] = useState<Set<number>>(new Set())
  const toggle = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const childrenOf = useMemo(() => {
    const map = new Map<number | null, FolderRead[]>()
    for (const f of folders) {
      const arr = map.get(f.parent_id) ?? []
      arr.push(f)
      map.set(f.parent_id, arr)
    }
    return map
  }, [folders])

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['media-folders'] })
    qc.invalidateQueries({ queryKey: ['media'] })
  }
  const createM = useMutation({
    mutationFn: (b: { folder_name: string; parent_id: number | null }) => api.post('/media/folders', b),
    onSuccess: invalidate,
  })
  const renameM = useMutation({
    mutationFn: (b: { id: number; folder_name: string }) =>
      api.put(`/media/folders/${b.id}`, { folder_name: b.folder_name }),
    onSuccess: invalidate,
  })
  const deleteM = useMutation({
    mutationFn: (id: number) => api.delete(`/media/folders/${id}`),
    onSuccess: invalidate,
    onError: () => alert(t('media.folder_delete_error')),
  })

  const addRoot = () => {
    const n = prompt(t('media.folder_name_prompt'))?.trim()
    if (n) createM.mutate({ folder_name: n, parent_id: null })
  }
  const addChild = (pid: number) => {
    const n = prompt(t('media.folder_name_prompt'))?.trim()
    if (n) createM.mutate({ folder_name: n, parent_id: pid })
  }
  const rename = (f: FolderRead) => {
    const n = prompt(t('media.folder_rename_prompt'), f.folder_name)?.trim()
    if (n && n !== f.folder_name) renameM.mutate({ id: f.id_folder, folder_name: n })
  }
  const del = (f: FolderRead) => {
    if (confirm(t('media.folder_delete_confirm', { name: folderLabel(f) }))) deleteM.mutate(f.id_folder)
  }

  const rowCls = (active: boolean) =>
    `group flex items-center gap-2 w-full px-2 py-1.5 rounded text-sm transition-sf ${
      active ? 'bg-accent/10 text-accent font-medium' : 'text-gray-600 hover:bg-gray-50'
    }`

  const renderNodes = (parentId: number | null): React.ReactNode =>
    (childrenOf.get(parentId) ?? []).map((f) => {
      const hasChildren = (childrenOf.get(f.id_folder)?.length ?? 0) > 0
      const isOpen = expanded.has(f.id_folder)
      return (
        <div key={f.id_folder}>
          <div className={rowCls(selected === f.id_folder)} style={{ paddingLeft: `${8 + f.depth * 14}px` }}>
            {hasChildren ? (
              <button
                onClick={() => toggle(f.id_folder)}
                className="text-gray-400 hover:text-accent shrink-0"
                title={isOpen ? t('media.folder_collapse') : t('media.folder_expand')}
              >
                {isOpen ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
              </button>
            ) : (
              <span className="w-3.5 shrink-0" />
            )}
            <button className="flex items-center gap-2 flex-1 min-w-0 text-left" onClick={() => onSelect(f.id_folder)}>
              <Folder className="w-4 h-4 shrink-0" />
              <span className="truncate">{folderLabel(f)}</span>
              {f.media_count > 0 && <span className="text-[10px] text-gray-400">{f.media_count}</span>}
            </button>
            <span className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-sf shrink-0">
              <button data-write-action onClick={() => addChild(f.id_folder)} title={t('media.folder_new_sub')} className="text-gray-300 hover:text-accent"><Plus className="w-3.5 h-3.5" /></button>
              {!f.system_key && (
                <button data-write-action onClick={() => rename(f)} title={t('common.rename')} className="text-gray-300 hover:text-accent"><Pencil className="w-3.5 h-3.5" /></button>
              )}
              <button data-write-action onClick={() => del(f)} title={t('common.delete')} className="text-gray-300 hover:text-danger"><Trash2 className="w-3.5 h-3.5" /></button>
            </span>
          </div>
          {hasChildren && isOpen && renderNodes(f.id_folder)}
        </div>
      )
    })

  return (
    <div data-testid="folder-tree" className={`w-full space-y-0.5 ${fill ? 'flex flex-col h-full min-h-0' : ''}`}>
      <div className={`flex items-center justify-between px-2 mb-1 ${fill ? 'shrink-0' : ''}`}>
        <span className="text-xs font-semibold text-gray-400 uppercase tracking-wide">{t('media.folders')}</span>
        <button data-write-action onClick={addRoot} className="text-gray-400 hover:text-accent transition-sf" title={t('media.folder_new')}>
          <FolderPlus className="w-4 h-4" />
        </button>
      </div>

      <button className={`${rowCls(selected === 'all')} ${fill ? 'shrink-0' : ''}`} onClick={() => onSelect('all')}>
        <Layers className="w-4 h-4 shrink-0" />
        <span className="truncate">{t('media.folder_all')}</span>
      </button>
      <button className={`${rowCls(selected === 'unfiled')} ${fill ? 'shrink-0' : ''}`} onClick={() => onSelect('unfiled')}>
        <Inbox className="w-4 h-4 shrink-0" />
        <span className="truncate">{t('media.folder_unfiled')}</span>
      </button>

      {/* Seule partie défilante en mode `fill` : l'arbre des dossiers de classement. */}
      <div className={`pt-1 ${fill ? 'flex-1 min-h-0 overflow-y-auto' : ''}`}>
        {renderNodes(null)}
        {folders.length === 0 && (
          <p className="px-2 py-2 text-xs text-gray-400">{t('media.folder_empty')}</p>
        )}
      </div>
    </div>
  )
}
