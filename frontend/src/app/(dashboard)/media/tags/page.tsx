'use client'

import { useState } from 'react'
import { useTranslations } from 'next-intl'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Pencil, Trash2, Tags as TagsIcon, Lock } from 'lucide-react'
import Topbar from '@/components/layout/Topbar'
import Can, { useCan } from '@/components/auth/Can'
import Button from '@/components/ui/Button'
import Input from '@/components/ui/Input'
import Drawer from '@/components/ui/Drawer'
import NumberField from '@/components/ui/NumberField'
import api from '@/lib/api'
import type { TagNamespace, TagValue } from '@/types/api'

const APPLIES_OPTIONS = ['image', 'video', 'audio', 'document', 'html_package', 'url_display']
const MATCH_MODES = ['any', 'all']
const DEFAULT_COLOR = '#4875B4'

// Onglets par type de contenu (« Tous » + un par type) — exploite applies_to.
type TypeTab = 'all' | 'image' | 'video' | 'audio' | 'document' | 'html_package' | 'url_display'
const TYPE_TABS: TypeTab[] = ['all', 'image', 'video', 'audio', 'document', 'html_package', 'url_display']

type NsForm = {
  id_namespace?: number
  namespace_key: string
  label: string
  match_mode: string
  applies_to: string[]
  is_active: boolean
  is_system?: boolean
}

type ValForm = {
  id_tag?: number
  id_namespace: number
  tag_value: string
  label: string
  color: string
  sort_order: number
}

type DrawerState =
  | { kind: 'ns'; data: NsForm }
  | { kind: 'value'; data: ValForm }
  | null

function apiError(e: unknown): string {
  const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
  return typeof detail === 'string' ? detail : 'Error'
}

function chipStyle(color: string | null): React.CSSProperties {
  if (!color) return {}
  return { backgroundColor: `${color}1A`, color, borderColor: `${color}55` }
}

export default function TagsPage() {
  const t = useTranslations('tags')
  const tMedia = useTranslations('media')
  const qc = useQueryClient()
  const [drawer, setDrawer] = useState<DrawerState>(null)
  const [err, setErr] = useState<string | null>(null)
  const [typeTab, setTypeTab] = useState<TypeTab>('all')
  // Groupe ouvert en detail : le tableau ne montre qu'un apercu des valeurs, la liste
  // complete (et son edition) vit dans le tiroir — c'est ce qui garde les lignes a
  // hauteur constante quel que soit le nombre de valeurs.
  const [detailNs, setDetailNs] = useState<number | null>(null)
  // Les tags s'éditent avec media:edit. Un rôle en lecture (ex. Approbateur) VOIT
  // les tags mais n'a aucun bouton d'action — masqués plutôt que menant à un 403.
  const canEdit = useCan('media:edit')

  const { data: namespaces = [], isLoading } = useQuery<TagNamespace[]>({
    queryKey: ['tag-namespaces'],
    queryFn: () => api.get<TagNamespace[]>('/tags/namespaces').then((r) => r.data),
  })

  const invalidate = () => qc.invalidateQueries({ queryKey: ['tag-namespaces'] })

  const saveNs = useMutation({
    mutationFn: (f: NsForm): Promise<unknown> =>
      f.id_namespace
        ? api.put(`/tags/namespaces/${f.id_namespace}`, {
            label: f.label || null,
            match_mode: f.match_mode,
            applies_to: f.applies_to,
            is_active: f.is_active,
          })
        : api.post('/tags/namespaces', {
            namespace_key: f.namespace_key,
            label: f.label || null,
            match_mode: f.match_mode,
            applies_to: f.applies_to,
          }),
    onSuccess: () => { invalidate(); setDrawer(null); setErr(null) },
    onError: (e) => setErr(apiError(e)),
  })

  const deleteNs = useMutation({
    mutationFn: (id: number) => api.delete(`/tags/namespaces/${id}`),
    onSuccess: invalidate,
    onError: (e) => alert(apiError(e)),
  })

  const saveVal = useMutation({
    mutationFn: (f: ValForm): Promise<unknown> =>
      f.id_tag
        ? api.put(`/tags/values/${f.id_tag}`, {
            label: f.label || null,
            color: f.color || null,
            sort_order: f.sort_order,
          })
        : api.post(`/tags/namespaces/${f.id_namespace}/values`, {
            tag_value: f.tag_value,
            label: f.label || null,
            color: f.color || null,
            sort_order: f.sort_order,
          }),
    onSuccess: () => { invalidate(); setDrawer(null); setErr(null) },
    onError: (e) => setErr(apiError(e)),
  })

  const deleteVal = useMutation({
    mutationFn: (id: number) => api.delete(`/tags/values/${id}`),
    onSuccess: invalidate,
    onError: (e) => alert(apiError(e)),
  })

  const openNewNs = () =>
    setDrawer({
      kind: 'ns',
      data: {
        namespace_key: '', label: '', match_mode: 'any',
        // Pré-remplit le type courant pour créer un tag spécifique au type.
        applies_to: typeTab === 'all' ? [] : [typeTab],
        is_active: true,
      },
    })

  // Un namespace est visible sous un onglet type si applies_to est vide (= tous) ou le contient.
  const shownNamespaces = typeTab === 'all'
    ? namespaces
    : namespaces.filter((ns) => ns.applies_to.length === 0 || ns.applies_to.includes(typeTab))
  const typeLabel = (ty: TypeTab) => (ty === 'all' ? tMedia('type_all') : tMedia(`type_${ty}`))

  const openEditNs = (ns: TagNamespace) =>
    setDrawer({
      kind: 'ns',
      data: {
        id_namespace: ns.id_namespace,
        namespace_key: ns.namespace_key,
        label: ns.label ?? '',
        match_mode: ns.match_mode,
        applies_to: ns.applies_to,
        is_active: ns.is_active,
        is_system: ns.is_system,
      },
    })

  const openDetail = (ns: TagNamespace) => setDetailNs(ns.id_namespace)

  const openNewVal = (ns: TagNamespace) =>
    setDrawer({
      kind: 'value',
      data: { id_namespace: ns.id_namespace, tag_value: '', label: '', color: DEFAULT_COLOR, sort_order: ns.values.length },
    })

  const openEditVal = (ns: TagNamespace, v: TagValue) =>
    setDrawer({
      kind: 'value',
      data: {
        id_tag: v.id_tag,
        id_namespace: ns.id_namespace,
        tag_value: v.tag_value,
        label: v.label ?? '',
        color: v.color ?? '',
        sort_order: v.sort_order,
      },
    })

  const detailGroup = namespaces.find((n) => n.id_namespace === detailNs) ?? null

  return (
    <>
      <Topbar title={t('title')}>
        <Can perm="media:edit">
          <Button size="sm" onClick={openNewNs}>
            <Plus className="w-3.5 h-3.5" />
            {t('new_namespace')}
          </Button>
        </Can>
      </Topbar>
      <main className="flex-1 overflow-y-auto">
        <div className="w-full px-6 py-5">
          <p className="text-sm text-gray-500 mb-4">{t('subtitle')}</p>

          {/* Onglets par type de contenu */}
          <div className="flex flex-wrap items-center gap-1.5 mb-4">
            {TYPE_TABS.map((ty) => (
              <button
                key={ty}
                onClick={() => setTypeTab(ty)}
                className={`px-3 py-1.5 rounded-full text-sm font-medium transition-sf ${
                  typeTab === ty ? 'bg-accent text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                }`}
              >
                {typeLabel(ty)}
              </button>
            ))}
          </div>

          {isLoading ? (
            <p className="text-sm text-gray-400">…</p>
          ) : namespaces.length === 0 ? (
            <div className="flex flex-col items-center gap-2 py-16 text-gray-400">
              <TagsIcon className="w-8 h-8" />
              <p className="text-sm">{t('empty')}</p>
            </div>
          ) : shownNamespaces.length === 0 ? (
            <div className="flex flex-col items-center gap-2 py-16 text-gray-400">
              <TagsIcon className="w-8 h-8" />
              <p className="text-sm">{t('empty_for_type')}</p>
              <Can perm="media:edit">
                <Button size="sm" variant="secondary" onClick={openNewNs}>
                  <Plus className="w-3.5 h-3.5" />
                  {t('new_namespace')}
                </Button>
              </Can>
            </div>
          ) : (
            /* Tableau : une ligne par groupe. Les cartes precedentes prenaient la hauteur
               de leur contenu — un groupe a 3 valeurs et un groupe a 21 n'avaient pas la
               meme taille, d'ou une grille en escalier illisible (retour Ed 2026-07-28).
               Une ligne de tableau a une hauteur constante PAR CONSTRUCTION, et les
               colonnes rendent les groupes comparables d'un coup d'oeil. */
            <div className="rounded-lg border border-gray-200 bg-surface shadow-sf-card overflow-hidden">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-200">
                    <th className="text-left font-medium px-4 py-2.5">{t('col_group')}</th>
                    <th className="text-left font-medium px-4 py-2.5 hidden md:table-cell">{t('col_match')}</th>
                    <th className="text-left font-medium px-4 py-2.5 hidden lg:table-cell">{t('col_applies')}</th>
                    <th className="text-right font-medium px-4 py-2.5">{t('col_values')}</th>
                    <th className="text-left font-medium px-4 py-2.5">{t('col_preview')}</th>
                    <th className="w-20" />
                  </tr>
                </thead>
                <tbody>
                  {shownNamespaces.map((ns) => (
                    <tr
                      key={ns.id_namespace}
                      onClick={() => openDetail(ns)}
                      className="border-b border-gray-100 last:border-0 hover:bg-surface-hover/60 transition-sf cursor-pointer"
                    >
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-medium text-gray-900">{ns.label ?? ns.namespace_key}</span>
                          {ns.is_system && (
                            <span title={t('system')} className="text-gray-400"><Lock className="w-3 h-3" /></span>
                          )}
                          {!ns.is_active && (
                            <span className="text-[10px] font-medium text-amber-700 dark:text-amber-300 bg-amber-500/10 border border-amber-400/20 rounded px-1.5 py-0.5">
                              {t('inactive')}
                            </span>
                          )}
                        </div>
                        <code className="text-[11px] text-gray-500">{ns.namespace_key}</code>
                      </td>
                      <td className="px-4 py-3 text-gray-600 hidden md:table-cell">{t(`match_${ns.match_mode}`)}</td>
                      <td className="px-4 py-3 text-gray-500 hidden lg:table-cell">
                        {ns.applies_to.length > 0 ? ns.applies_to.join(', ') : '—'}
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-gray-900 font-medium">{ns.values.length}</td>
                      <td className="px-4 py-3">
                        {/* Apercu borne a 4 pastilles + un reste compte : la colonne garde
                            sa largeur quel que soit le nombre de valeurs. */}
                        <div className="flex items-center gap-1.5 flex-nowrap overflow-hidden">
                          {ns.values.slice(0, 4).map((v) => (
                            <span
                              key={v.id_tag}
                              className="inline-flex items-center rounded border text-[11px] font-medium px-1.5 py-0.5 bg-gray-100 text-gray-600 border-gray-200 whitespace-nowrap"
                              style={chipStyle(v.color)}
                            >
                              {v.label ?? v.tag_value}
                            </span>
                          ))}
                          {ns.values.length > 4 && (
                            <span className="text-[11px] text-gray-500 whitespace-nowrap">
                              +{ns.values.length - 4}
                            </span>
                          )}
                          {ns.values.length === 0 && (
                            <span className="text-[11px] text-gray-400">{t('no_value')}</span>
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        {canEdit && (
                          <div className="flex items-center justify-end gap-1">
                            <button
                              onClick={(e) => { e.stopPropagation(); openEditNs(ns) }}
                              className="p-1.5 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-700 transition-sf"
                              title={t('edit_namespace')}
                            >
                              <Pencil className="w-3.5 h-3.5" />
                            </button>
                            {!ns.is_system && (
                              <button
                                onClick={(e) => {
                                  e.stopPropagation()
                                  if (confirm(t('delete_ns_confirm'))) deleteNs.mutate(ns.id_namespace)
                                }}
                                className="p-1.5 rounded hover:bg-red-500/10 text-gray-400 hover:text-danger transition-sf"
                                title={t('delete_namespace')}
                              >
                                <Trash2 className="w-3.5 h-3.5" />
                              </button>
                            )}
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </main>

      {/* Detail d'un groupe : toutes ses valeurs, ajout et edition */}
      <Drawer
        open={detailNs !== null && drawer === null}
        onClose={() => setDetailNs(null)}
        title={detailGroup ? (detailGroup.label ?? detailGroup.namespace_key) : ''}
      >
        {detailGroup && (
          <div className="space-y-4">
            <div className="flex items-center gap-2 flex-wrap text-xs text-gray-500">
              <code className="bg-gray-100 px-1.5 py-0.5 rounded text-gray-600">{detailGroup.namespace_key}</code>
              <span>{t('match')}: <span className="text-gray-700">{t(`match_${detailGroup.match_mode}`)}</span></span>
              {detailGroup.applies_to.length > 0 && (
                <span>· {t('applies')}: <span className="text-gray-700">{detailGroup.applies_to.join(', ')}</span></span>
              )}
            </div>

            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-gray-800">
                {t('col_values')} ({detailGroup.values.length})
              </span>
              {canEdit && (
                <Button size="sm" variant="secondary" onClick={() => openNewVal(detailGroup)}>
                  <Plus className="w-3.5 h-3.5" />
                  {t('add_value')}
                </Button>
              )}
            </div>

            <div className="flex flex-wrap items-center gap-1.5">
              {detailGroup.values.map((v) => (
                <span
                  key={v.id_tag}
                  className="group inline-flex items-center gap-1 rounded border text-xs font-medium pl-2 pr-1 py-1 bg-gray-100 text-gray-600 border-gray-200"
                  style={chipStyle(v.color)}
                >
                  {canEdit ? (
                    <button onClick={() => openEditVal(detailGroup, v)} title={t('edit_value')}>
                      {v.label ?? v.tag_value}
                    </button>
                  ) : (
                    <span>{v.label ?? v.tag_value}</span>
                  )}
                  {canEdit && (
                    <button
                      onClick={() => { if (confirm(t('delete_value_confirm'))) deleteVal.mutate(v.id_tag) }}
                      className="opacity-40 hover:opacity-100 transition-sf"
                      title={t('delete_value')}
                    >
                      <Trash2 className="w-3 h-3" />
                    </button>
                  )}
                </span>
              ))}
              {detailGroup.values.length === 0 && (
                <p className="text-xs text-gray-400 py-2">{t('no_value')}</p>
              )}
            </div>
          </div>
        )}
      </Drawer>

      {/* Namespace drawer */}
      <Drawer
        open={drawer?.kind === 'ns'}
        onClose={() => { setDrawer(null); setErr(null) }}
        title={drawer?.kind === 'ns' && drawer.data.id_namespace ? t('edit_namespace') : t('new_namespace')}
      >
        {drawer?.kind === 'ns' && (
          <NamespaceForm
            data={drawer.data}
            error={err}
            saving={saveNs.isPending}
            onChange={(d) => setDrawer({ kind: 'ns', data: d })}
            onSubmit={() => saveNs.mutate(drawer.data)}
          />
        )}
      </Drawer>

      {/* Value drawer */}
      <Drawer
        open={drawer?.kind === 'value'}
        onClose={() => { setDrawer(null); setErr(null) }}
        title={drawer?.kind === 'value' && drawer.data.id_tag ? t('edit_value') : t('add_value')}
      >
        {drawer?.kind === 'value' && (
          <ValueForm
            data={drawer.data}
            error={err}
            saving={saveVal.isPending}
            onChange={(d) => setDrawer({ kind: 'value', data: d })}
            onSubmit={() => saveVal.mutate(drawer.data)}
          />
        )}
      </Drawer>
    </>
  )
}

function NamespaceForm({
  data, error, saving, onChange, onSubmit,
}: {
  data: NsForm
  error: string | null
  saving: boolean
  onChange: (d: NsForm) => void
  onSubmit: () => void
}) {
  const t = useTranslations('tags')
  const tMedia = useTranslations('media')
  const isEdit = !!data.id_namespace
  const keyValid = isEdit || /^[a-z0-9_]+$/.test(data.namespace_key)

  return (
    <form
      className="space-y-4"
      onSubmit={(e) => { e.preventDefault(); if (keyValid && data.namespace_key) onSubmit() }}
    >
      <Input
        label={t('field_key')}
        value={data.namespace_key}
        disabled={isEdit}
        onChange={(e) => onChange({ ...data, namespace_key: e.target.value })}
        hint={isEdit ? t('key_immutable') : t('key_hint')}
        error={data.namespace_key && !keyValid ? t('key_invalid') : undefined}
        placeholder="orientation"
      />
      <Input
        label={t('field_label')}
        value={data.label}
        onChange={(e) => onChange({ ...data, label: e.target.value })}
        placeholder={t('field_label_ph')}
      />

      <div className="flex flex-col gap-1">
        <span className="text-sm font-medium text-gray-800">{t('field_match')}</span>
        <div className="flex gap-2">
          {MATCH_MODES.map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => onChange({ ...data, match_mode: m })}
              className={`flex-1 px-3 py-2 rounded border text-sm transition-sf ${
                data.match_mode === m
                  ? 'border-accent bg-accent/10 text-accent font-medium'
                  : 'border-gray-200 text-gray-600 hover:bg-gray-50'
              }`}
            >
              {t(`match_${m}`)}
            </button>
          ))}
        </div>
        <p className="text-xs text-gray-400">{t(`match_${data.match_mode}_hint`)}</p>
      </div>

      <div className="flex flex-col gap-1">
        <span className="text-sm font-medium text-gray-800">{t('field_applies')}</span>
        <div className="flex flex-wrap gap-1.5">
          {APPLIES_OPTIONS.map((opt) => {
            const on = data.applies_to.includes(opt)
            return (
              <button
                key={opt}
                type="button"
                onClick={() =>
                  onChange({
                    ...data,
                    applies_to: on ? data.applies_to.filter((x) => x !== opt) : [...data.applies_to, opt],
                  })
                }
                className={`px-2.5 py-1 rounded border text-xs font-medium transition-sf ${
                  on ? 'border-accent bg-accent/10 text-accent' : 'border-gray-200 text-gray-600 hover:bg-gray-50'
                }`}
              >
                {tMedia(`type_${opt}`)}
              </button>
            )
          })}
        </div>
        <p className="text-xs text-gray-400">{t('applies_hint')}</p>
      </div>

      {isEdit && (
        <label className="flex items-center gap-2 text-sm text-gray-700">
          <input
            type="checkbox"
            checked={data.is_active}
            onChange={(e) => onChange({ ...data, is_active: e.target.checked })}
            className="rounded-lg border-gray-300 text-accent focus:ring-accent/40"
          />
          {t('field_active')}
        </label>
      )}

      {error && <p className="text-xs text-danger">{error}</p>}

      <div className="pt-2">
        <Button type="submit" loading={saving} disabled={!data.namespace_key || !keyValid}>
          {t('save')}
        </Button>
      </div>
    </form>
  )
}

function ValueForm({
  data, error, saving, onChange, onSubmit,
}: {
  data: ValForm
  error: string | null
  saving: boolean
  onChange: (d: ValForm) => void
  onSubmit: () => void
}) {
  const t = useTranslations('tags')
  const isEdit = !!data.id_tag

  return (
    <form
      className="space-y-4"
      onSubmit={(e) => { e.preventDefault(); if (data.tag_value) onSubmit() }}
    >
      <Input
        label={t('field_value')}
        value={data.tag_value}
        disabled={isEdit}
        onChange={(e) => onChange({ ...data, tag_value: e.target.value })}
        hint={isEdit ? t('value_immutable') : undefined}
        placeholder="portrait"
      />
      <Input
        label={t('field_label')}
        value={data.label}
        onChange={(e) => onChange({ ...data, label: e.target.value })}
        placeholder={t('field_label_ph')}
      />

      <div className="flex flex-col gap-1">
        <span className="text-sm font-medium text-gray-800">{t('field_color')}</span>
        <div className="flex items-center gap-2">
          <input
            type="color"
            value={data.color || DEFAULT_COLOR}
            onChange={(e) => onChange({ ...data, color: e.target.value })}
            className="h-9 w-12 rounded border border-gray-200 cursor-pointer bg-surface"
          />
          <input
            type="text"
            value={data.color}
            onChange={(e) => onChange({ ...data, color: e.target.value })}
            placeholder="#4875B4"
            className="w-28 px-3 py-2 text-sm rounded-lg border border-gray-200 bg-surface text-gray-900 focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
          {data.color && (
            <button
              type="button"
              onClick={() => onChange({ ...data, color: '' })}
              className="text-xs text-gray-400 hover:text-gray-600"
            >
              {t('clear_color')}
            </button>
          )}
        </div>
      </div>

      <NumberField
        label={t('field_sort')}
        value={data.sort_order}
        onCommit={(v) => onChange({ ...data, sort_order: v ?? 0 })}
      />

      {error && <p className="text-xs text-danger">{error}</p>}

      <div className="pt-2">
        <Button type="submit" loading={saving} disabled={!data.tag_value}>
          {t('save')}
        </Button>
      </div>
    </form>
  )
}
