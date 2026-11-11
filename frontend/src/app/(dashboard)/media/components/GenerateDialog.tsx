'use client'

import { useEffect, useMemo, useState } from 'react'
import { useTranslations } from 'next-intl'
import { useServerLabels } from '@/lib/serverLabels'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Sparkles, Image as ImageIcon, Film, Music, AlertTriangle } from 'lucide-react'
import Drawer from '@/components/ui/Drawer'
import Button from '@/components/ui/Button'
import { useAuthStore } from '@/store/auth'
import api from '@/lib/api'
import { estimateCost, fmtCost, type GenerationCatalog, type ModelSpec } from '@/lib/generation'
import PromptAssistantPanel from '@/components/PromptAssistantPanel'
import ReferencePicker from './ReferencePicker'
import type { MediaRead } from '@/types/api'

type Tab = 'image' | 'video' | 'audio'

export default function GenerateDialog({
  open,
  onClose,
  onGenerated,
  onVideoQueued,
}: {
  open: boolean
  onClose: () => void
  onGenerated: (m: MediaRead) => void
  onVideoQueued: () => void
}) {
  const t = useTranslations()
  const { genModel } = useServerLabels()
  const orgId = useAuthStore((s) => s.user?.id_org)
  const [tab, setTab] = useState<Tab>('image')
  const [audioKind, setAudioKind] = useState<'music' | 'voice'>('music')
  const [audioProvider, setAudioProvider] = useState<string>('fal')
  const [prompt, setPrompt] = useState('')
  const [negativePrompt, setNegativePrompt] = useState('')
  const [name, setName] = useState('')
  const [mute, setMute] = useState(false)
  const [modelId, setModelId] = useState<string>('')
  const [params, setParams] = useState<Record<string, unknown>>({})
  const [refIds, setRefIds] = useState<number[]>([])
  const [count, setCount] = useState(1)   // variations : N images en un clic (t2i/i2i)
  const [err, setErr] = useState<string | null>(null)

  const { data: catalog } = useQuery<GenerationCatalog>({
    queryKey: ['generation-catalog', orgId],
    queryFn: () => api.get('/generation/catalog').then((r) => r.data),
    enabled: open && !!orgId,
    retry: false,
  })
  const models = useMemo(() => catalog?.models ?? [], [catalog])

  // Type catalogue effectif (audio → music|voice) + provider effectif.
  const effKind: ModelSpec['kind'] = tab === 'audio' ? audioKind : tab
  const audioProviders = useMemo<string[]>(
    () => Array.from(new Set(models.filter((m) => m.kind === audioKind).map((m) => m.provider as string))),
    [models, audioKind],
  )
  const effProvider = tab === 'audio'
    ? (audioProviders.includes(audioProvider) ? audioProvider : audioProviders[0])
    : undefined

  const availModels = useMemo(
    () => models.filter((m) => m.kind === effKind && (tab !== 'audio' || m.provider === effProvider)),
    [models, effKind, tab, effProvider],
  )
  const selected = availModels.find((m) => m.id === modelId) ?? availModels[0]

  // Onglets disponibles selon les clés présentes (image/vidéo = fal ; audio = fal ou elevenlabs).
  const availTabs: Tab[] = useMemo(() => {
    const k = new Set(models.map((m) => m.kind))
    const out: Tab[] = []
    if (k.has('image')) out.push('image')
    if (k.has('video')) out.push('video')
    if (k.has('music') || k.has('voice')) out.push('audio')
    return out
  }, [models])

  // Reset des params sur changement de modèle (valeurs par défaut du modèle).
  useEffect(() => {
    if (!selected) return
    if (modelId !== selected.id) setModelId(selected.id)
    const next: Record<string, unknown> = {}
    for (const p of selected.params) if (p.default !== null && p.default !== undefined) next[p.key] = p.default
    setParams(next)
    setRefIds([])
    setNegativePrompt('')
  }, [selected]) // eslint-disable-line react-hooks/exhaustive-deps

  const needsRef = selected?.input === 'image'
  const supportsNeg = !!selected?.supports_negative_prompt
  const isEdit = selected?.op === 'edit'                 // retouche (détourage / édition)
  const promptOptional = !!selected?.prompt_optional     // détourage = aucun prompt requis
  // Variations : seulement en génération d'image classique (pas en édition déterministe).
  const canVary = tab === 'image' && !isEdit
  const effCount = canVary ? count : 1

  const costStr = fmtCost(estimateCost(selected, params, prompt) * effCount)

  const onErr = (e: unknown) => {
    const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    setErr(typeof msg === 'string' ? msg : t('generation.error'))
  }
  const reset = () => { setPrompt(''); setNegativePrompt(''); setName(''); setMute(false); setErr(null); setRefIds([]); setCount(1) }
  const cleanName = () => name.trim() || undefined
  const aspect = (params.aspect_ratio as string) || '16:9'
  // Le negative prompt transite via params (pass-through fal) si le modèle le supporte.
  const withNegative = () =>
    supportsNeg && negativePrompt.trim() ? { ...params, negative_prompt: negativePrompt.trim() } : params

  const genImage = useMutation({
    // N variations en parallèle (effCount). Chaque image = un média ; on rafraîchit pour toutes.
    mutationFn: () =>
      Promise.all(
        Array.from({ length: effCount }, () =>
          api.post<MediaRead>('/generation/image', {
            prompt: prompt.trim(), model: selected?.id, aspect_ratio: aspect, params: withNegative(),
            reference_media_ids: refIds, name: cleanName(),
          }).then((r) => r.data),
        ),
      ),
    onSuccess: (list) => { reset(); list.forEach(onGenerated); onClose() },
    onError: onErr,
  })
  const genVideo = useMutation({
    mutationFn: () =>
      api.post('/generation/video', {
        prompt: prompt.trim(), model: selected?.id, aspect_ratio: aspect, params: withNegative(),
        reference_media_ids: refIds, name: cleanName(), mute,
      }),
    onSuccess: () => { reset(); onVideoQueued(); onClose() },
    onError: onErr,
  })
  const genAudio = useMutation({
    mutationFn: () =>
      api.post('/generation/audio', {
        provider: effProvider, kind: audioKind, prompt: prompt.trim(), model: selected?.id,
        params, name: cleanName(),
      }),
    onSuccess: () => { reset(); onVideoQueued(); onClose() },
    onError: onErr,
  })

  const busy = genImage.isPending || genVideo.isPending || genAudio.isPending
  const noModels = availTabs.length === 0
  const submit = () => {
    if (tab === 'image') genImage.mutate()
    else if (tab === 'video') genVideo.mutate()
    else genAudio.mutate()
  }
  const isVoice = tab === 'audio' && audioKind === 'voice'

  const optLabel = (o: { label?: string; label_literal?: string }) =>
    o.label_literal ?? (o.label ? t(`generation.opt_${o.label}`) : '')

  return (
    <Drawer open={open} onClose={onClose} title={t('generation.title')}>
      <div className="space-y-4" data-write-perm="media:create">
        {/* Onglets type */}
        <div className="flex gap-2">
          {([['image', ImageIcon], ['video', Film], ['audio', Music]] as const).map(([k, Icon]) => {
            const disabled = !noModels && !availTabs.includes(k)
            return (
              <button
                key={k}
                type="button"
                disabled={disabled}
                onClick={() => { setTab(k); setErr(null) }}
                className={`flex flex-1 items-center justify-center gap-1.5 rounded-lg border px-2 py-2 text-sm transition-sf ${
                  tab === k ? 'border-accent bg-accent/10 text-accent font-medium'
                    : disabled ? 'border-gray-200 text-gray-300 cursor-not-allowed'
                    : 'border-gray-200 text-gray-600 hover:bg-gray-50'
                }`}
              >
                <Icon className="h-4 w-4" />{t(`generation.kind_${k}`)}
              </button>
            )
          })}
        </div>

        <p className="text-xs text-gray-500">{t(`generation.intro_${tab}`)}</p>

        {noModels && (
          <p className="rounded-lg border border-amber-300 dark:border-amber-500/40 bg-amber-500/10 dark:bg-amber-500/15 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
            {t('generation.no_audio_key')}
          </p>
        )}

        {tab === 'audio' && !noModels && (
          <div className="flex gap-2">
            <div className="flex-1">
              <label className="mb-1 block text-xs font-medium text-gray-700">{t('generation.audio_kind')}</label>
              <select value={audioKind} onChange={(e) => setAudioKind(e.target.value as 'music' | 'voice')}
                className="w-full rounded border border-gray-200 bg-surface px-2 py-1.5 text-sm">
                <option value="music">{t('generation.audio_kind_music')}</option>
                <option value="voice">{t('generation.audio_kind_voice')}</option>
              </select>
            </div>
            {audioProviders.length > 1 && (
              <div className="flex-1">
                <label className="mb-1 block text-xs font-medium text-gray-700">{t('generation.audio_provider')}</label>
                <select value={effProvider} onChange={(e) => setAudioProvider(e.target.value)}
                  className="w-full rounded border border-gray-200 bg-surface px-2 py-1.5 text-sm">
                  {audioProviders.map((p) => <option key={p} value={p}>{t(`generation.provider_${p}`)}</option>)}
                </select>
              </div>
            )}
          </div>
        )}

        {/* Prompt / texte — masqué pour le détourage (transformation pure, aucun prompt) */}
        {!noModels && !promptOptional && (
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-700">
              {isVoice ? t('generation.text') : isEdit ? t('generation.edit_instruction') : t('generation.prompt')}
            </label>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={isEdit ? 2 : 4}
              placeholder={isVoice ? t('generation.text_ph') : isEdit ? t('generation.edit_instruction_ph') : t('generation.prompt_ph')}
              className="w-full resize-none rounded-lg border border-gray-200 px-2 py-1.5 text-sm focus:border-accent focus:outline-none"
            />
            {!isVoice && !isEdit && (
              <p className="mt-1 text-[11px] text-gray-400">{t('generation.english_warn')}</p>
            )}
          </div>
        )}

        {/* Assistant de rédaction de prompt (optionnel ; inutile pour le détourage) */}
        {!noModels && !promptOptional && (
          <PromptAssistantPanel
            kind={effKind}
            model={selected?.id}
            referenceMediaIds={refIds}
            onApply={(p, neg) => { setPrompt(p); if (neg !== null) setNegativePrompt(neg) }}
          />
        )}

        {/* Negative prompt (modèles qui le supportent) */}
        {!noModels && supportsNeg && (
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-700">
              {t('generation.negative_prompt')}
            </label>
            <textarea
              value={negativePrompt}
              onChange={(e) => setNegativePrompt(e.target.value)}
              rows={2}
              placeholder={t('generation.negative_prompt_ph')}
              className="w-full resize-none rounded-lg border border-gray-200 px-2 py-1.5 text-sm focus:border-accent focus:outline-none"
            />
          </div>
        )}

        {/* Modèle + badge de palier de coût */}
        {!noModels && availModels.length > 0 && (
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-700">{t('generation.model')}</label>
            <select value={selected?.id} onChange={(e) => setModelId(e.target.value)}
              className="w-full rounded border border-gray-200 bg-surface px-2 py-1.5 text-sm">
              {availModels.map((m) => (
                <option key={m.id} value={m.id}>{genModel(m)} · {t(`generation.tier_${m.tier}`)}</option>
              ))}
            </select>
            {selected?.tier === 'high' && (
              <p className="mt-1 flex items-center gap-1 text-[11px] text-amber-600 dark:text-amber-400">
                <AlertTriangle className="h-3 w-3" />{t('generation.tier_high_warn')}
              </p>
            )}
          </div>
        )}

        {/* Image(s) de référence (image→image / image→vidéo) */}
        {!noModels && needsRef && (
          <ReferencePicker value={refIds} onChange={setRefIds} maxRefs={selected?.max_refs ?? 1} />
        )}

        {/* Params avancés (dynamiques par modèle) */}
        {!noModels && selected && selected.params.length > 0 && (
          <div className="grid grid-cols-2 gap-2">
            {selected.params.map((p) => (
              <div key={p.key} className={p.control === 'text' ? 'col-span-2' : ''}>
                <label className="mb-1 block text-xs font-medium text-gray-700">
                  {t(`generation.param_${p.label}`)}{p.unit ? ` (${p.unit})` : ''}
                </label>
                {p.control === 'select' ? (
                  <select
                    value={String(params[p.key] ?? p.default ?? '')}
                    onChange={(e) => setParams((s) => ({ ...s, [p.key]: e.target.value }))}
                    className="w-full rounded border border-gray-200 bg-surface px-2 py-1.5 text-sm"
                  >
                    {p.options?.map((o) => <option key={o.value} value={o.value}>{optLabel(o)}</option>)}
                  </select>
                ) : (
                  <input
                    type={p.control === 'number' ? 'number' : 'text'}
                    value={params[p.key] === undefined || params[p.key] === null ? '' : String(params[p.key])}
                    min={p.min}
                    max={p.max}
                    step={p.step}
                    placeholder={p.optional ? t('generation.optional') : ''}
                    onChange={(e) => {
                      const v = e.target.value
                      setParams((s) => ({
                        ...s,
                        [p.key]: v === '' ? undefined : p.control === 'number' ? Number(v) : v,
                      }))
                    }}
                    className="w-full rounded-lg border border-gray-200 bg-surface px-2 py-1.5 text-sm focus:border-accent focus:outline-none"
                  />
                )}
              </div>
            ))}
          </div>
        )}

        {/* Variations : N images en un clic (génération image classique) */}
        {!noModels && canVary && (
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-700">{t('generation.variations')}</label>
            <select value={count} onChange={(e) => setCount(Number(e.target.value))}
              className="w-full rounded border border-gray-200 bg-surface px-2 py-1.5 text-sm">
              {[1, 2, 3, 4].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </div>
        )}

        {/* Nom du média (optionnel) */}
        {!noModels && selected && (
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-700">{t('generation.name')}</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t('generation.name_ph')}
              className="w-full rounded-lg border border-gray-200 bg-surface px-2 py-1.5 text-sm focus:border-accent focus:outline-none"
            />
          </div>
        )}

        {/* Sans bande audio (vidéo) */}
        {!noModels && tab === 'video' && (
          <label className="flex items-center gap-2 text-xs text-gray-700">
            <input type="checkbox" checked={mute} onChange={(e) => setMute(e.target.checked)}
              className="rounded-lg border-gray-300 text-accent focus:ring-accent/40" />
            {t('generation.mute')}
          </label>
        )}

        {/* Estimatif de coût */}
        {!noModels && selected && (
          <div className="flex items-center justify-between rounded-lg border border-gray-200 bg-gray-50 dark:bg-surface px-3 py-2 text-xs shadow-sf-card">
            <span className="text-gray-500">{t('generation.est_cost')}</span>
            <span className="font-semibold text-gray-800">≈ {costStr}</span>
          </div>
        )}

        {err && <p className="text-xs text-danger">{err}</p>}
        <Button
          size="sm" loading={busy}
          data-write-action
          disabled={(!prompt.trim() && !promptOptional) || noModels || !selected || (needsRef && refIds.length === 0)}
          onClick={submit}
        >
          <Sparkles className="w-3.5 h-3.5 mr-1" />{t('generation.generate')}
        </Button>
        <p className="text-[11px] text-gray-400">{t(`generation.note_${tab === 'image' ? 'image' : 'async'}`)}</p>
      </div>
    </Drawer>
  )
}
