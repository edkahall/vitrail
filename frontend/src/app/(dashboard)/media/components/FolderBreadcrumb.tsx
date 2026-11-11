'use client'

import { useTranslations } from 'next-intl'
import { useFolderLabel } from '@/lib/folderLabel'
import { ChevronRight, Folder, Inbox, Layers } from 'lucide-react'
import type { FolderRead } from '@/types/api'
import type { FolderFilter } from './FolderTree'

/**
 * Fil d'Ariane + sous-dossiers en TUILES (retour Ed sur les maquettes MyBox, audit du menu
 * Média 2026-07-28).
 *
 * Pourquoi ça compte au-delà du confort : l'arbre de gauche montre la hiérarchie, mais
 * la zone de contenu, elle, ne disait NULLE PART où l'on se trouvait ni ce que le dossier
 * courant contient comme sous-dossiers. C'est précisément ce qui a rendu invisible le bug
 * des dossiers homonymes — avec un chemin affiché, « Video / Divers » et « Pic / Divers »
 * ne se seraient jamais confondus.
 *
 * Les tuiles ne dupliquent pas l'arbre : l'arbre sert à SAUTER n'importe où, les tuiles à
 * DESCENDRE d'un cran dans le dossier ouvert, sans quitter la zone de contenu.
 */

interface Props {
  folders: FolderRead[]
  selected: FolderFilter
  onSelect: (f: FolderFilter) => void
}

export default function FolderBreadcrumb({ folders, selected, onSelect }: Props) {
  const t = useTranslations('media')
  const { label: folderLabel } = useFolderLabel()
  const byId = new Map(folders.map((f) => [f.id_folder, f]))

  // Chaîne des ancêtres du dossier courant (racine → courant).
  const trail: FolderRead[] = []
  if (typeof selected === 'number') {
    let cur = byId.get(selected)
    let guard = 0
    while (cur && guard++ < 32) {
      trail.unshift(cur)
      cur = cur.parent_id != null ? byId.get(cur.parent_id) : undefined
    }
  }

  // Sous-dossiers DIRECTS du niveau courant (racine si « Tous »).
  const parentId = typeof selected === 'number' ? selected : null
  const children = selected === 'unfiled' ? [] : folders.filter((f) => f.parent_id === parentId)

  return (
    <div className="shrink-0">
      <nav className="flex items-center gap-1 text-sm flex-wrap" aria-label={t('breadcrumb')}>
        <button
          onClick={() => onSelect('all')}
          className={`inline-flex items-center gap-1.5 px-2 py-1 rounded transition-sf ${
            selected === 'all' ? 'text-gray-900 font-medium' : 'text-gray-500 hover:text-accent'
          }`}
        >
          <Layers className="w-3.5 h-3.5" />
          {t('folder_all')}
        </button>
        {selected === 'unfiled' && (
          <>
            <ChevronRight className="w-3.5 h-3.5 text-gray-500" />
            <span className="inline-flex items-center gap-1.5 px-2 py-1 text-gray-900 font-medium">
              <Inbox className="w-3.5 h-3.5" />
              {t('folder_unfiled')}
            </span>
          </>
        )}
        {trail.map((f, i) => (
          <span key={f.id_folder} className="inline-flex items-center gap-1">
            <ChevronRight className="w-3.5 h-3.5 text-gray-500" />
            <button
              onClick={() => onSelect(f.id_folder)}
              className={`px-2 py-1 rounded transition-sf ${
                i === trail.length - 1
                  ? 'text-gray-900 font-medium'
                  : 'text-gray-500 hover:text-accent'
              }`}
            >
              {folderLabel(f)}
            </button>
          </span>
        ))}
      </nav>

      {children.length > 0 && (
        <div className="mt-3" data-testid="folder-tiles">
          <p className="text-[11px] uppercase tracking-wide text-gray-400 mb-2">{t('folders')}</p>
          <div className="grid grid-cols-[repeat(auto-fill,minmax(170px,1fr))] gap-2">
            {children.map((f) => (
              <button
                key={f.id_folder}
                onClick={() => onSelect(f.id_folder)}
                className="flex items-center gap-2.5 px-3 py-2.5 rounded-lg border border-white/5 bg-surface hover:bg-surface-hover hover:border-accent/30 transition-sf text-left"
              >
                <Folder className="w-4 h-4 shrink-0 text-accent-hover" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm text-gray-900">{folderLabel(f)}</span>
                  <span className="block text-[11px] text-gray-500">
                    {t('folder_counts', { media: f.media_count, sub: f.child_count })}
                  </span>
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
