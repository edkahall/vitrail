import { useCallback } from 'react'
import { useTranslations } from 'next-intl'
import type { FolderRead } from '@/types/api'

/**
 * Display name of a media folder. System folders (`system_key`, migr 0131) carry an
 * English fallback name in the database; their visible name is translated here, never
 * stored per language — the server finds them by key, not by name.
 */
export function useFolderLabel() {
  const t = useTranslations('media')
  const label = useCallback(
    (f: Pick<FolderRead, 'folder_name' | 'system_key'>) =>
      f.system_key === 'advertisers' ? t('system_folder_advertisers') : f.folder_name,
    [t],
  )
  /** Readable path ("Advertisers / Acme") rebuilt from the materialised `path`, so that a
   *  system ancestor is translated too. Falls back to the server's `full_path`. */
  const path = useCallback(
    (f: FolderRead, byId: Map<number, FolderRead>) => {
      const chain = f.path.split('.').map((id) => byId.get(Number(id)))
      if (chain.length === 0 || chain.some((c) => c === undefined)) return f.full_path || label(f)
      return (chain as FolderRead[]).map(label).join(' / ')
    },
    [label],
  )
  return { label, path }
}
