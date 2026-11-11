'use client'

import { useMemo, useState } from 'react'
import { useTranslations } from 'next-intl'
import { useServerLabels } from '@/lib/serverLabels'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Sparkles, Wand2, AlertTriangle, CheckCircle2, Loader2, Image as ImageIcon,
  Film, Music, Mic, ArrowLeft, SlidersHorizontal, Check, RotateCcw,
} from 'lucide-react'
import Link from 'next/link'
import Topbar from '@/components/layout/Topbar'
import Button from '@/components/ui/Button'
import Drawer from '@/components/ui/Drawer'
import { useAuthStore } from '@/store/auth'
import api from '@/lib/api'
import { estimateCost, fmtCost, type GenerationCatalog, type ModelSpec } from '@/lib/generation'
import NumberField from '@/components/ui/NumberField'
import PromptAssistantPanel from '@/components/PromptAssistantPanel'
import ReferencePicker from '../media/components/ReferencePicker'

const RATIOS = ['16:9', '9:16', '1:1', '4:3', '3:4'] as const
type CType = 'image' | 'video' | 'voice' | 'music'
const TYPE_META: { key: CType; kind: ModelSpec['kind']; icon: typeof ImageIcon }[] = [
  { key: 'image', kind: 'image', icon: ImageIcon },
  { key: 'video', kind: 'video', icon: Film },
  { key: 'voice', kind: 'voice', icon: Mic },
  { key: 'music', kind: 'music', icon: Music },
]
const MAX_ASSETS = 40

type Plan = {
  concepts: { title: string; prompt: string }[]
  video_prompt: string | null
  voiceover: string | null
  music_prompt: string | null
}
type CampaignRead = {
  id_campaign: number; brief: string; ratios: string[]; status: string
  total_cost_usd: number | null; jobs_total: number; jobs_ready: number; jobs_failed: number
}

/** Les trois temps RÉELS de l'écran : deux appels serveur séparés par une décision humaine. */
type Phase = 'brief' | 'plan' | 'run'

/**
 * Signature de ce dont le plan DÉPEND (ce que `/generation/campaign/plan` reçoit).
 * Sert à détecter un plan périmé — cf. `stale` plus bas.
 */
function planSignature(brief: string, types: Set<CType>, nConcepts: number) {
  return JSON.stringify({ b: brief.trim(), t: [...types].sort(), n: nConcepts })
}

export default function StudioPage() {
  const t = useTranslations()
  const { genModel } = useServerLabels()
  const qc = useQueryClient()
  const orgId = useAuthStore((s) => s.user?.id_org)

  const [phase, setPhase] = useState<Phase>('brief')
  const [settingsOpen, setSettingsOpen] = useState(false)

  const [brief, setBrief] = useState('')
  const [nConcepts, setNConcepts] = useState(3)
  const [types, setTypes] = useState<Set<CType>>(new Set(['image']))
  const [ratios, setRatios] = useState<string[]>(['16:9', '9:16'])      // formats IMAGE
  const [imageRefIds, setImageRefIds] = useState<number[]>([])
  const [videoRatios, setVideoRatios] = useState<string[]>(['16:9'])    // formats VIDÉO
  const [videoRefIds, setVideoRefIds] = useState<number[]>([])
  const [modelByKind, setModelByKind] = useState<Record<string, string>>({})
  const [plan, setPlan] = useState<Plan | null>(null)
  const [planFrom, setPlanFrom] = useState<string | null>(null)
  const [videoNeg, setVideoNeg] = useState('')
  const [videoMute, setVideoMute] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const { data: catalog } = useQuery<GenerationCatalog>({
    queryKey: ['generation-catalog', orgId],
    queryFn: () => api.get('/generation/catalog').then((r) => r.data),
    enabled: !!orgId, retry: false,
  })
  const models = useMemo(() => catalog?.models ?? [], [catalog])
  const getModel = (id: string | undefined) => models.find((m) => m.id === id)
  const modelsOf = (kind: ModelSpec['kind']) => models.filter((m) => m.kind === kind)
  const chosen = (kind: ModelSpec['kind']) => getModel(modelByKind[kind]) ?? modelsOf(kind)[0]
  const hasFal = (catalog?.providers ?? []).includes('fal')
  const availTypes = TYPE_META.filter((tm) => modelsOf(tm.kind).length > 0)

  const onErr = (e: unknown) => {
    const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    setErr(typeof msg === 'string' ? msg : t('studio.error'))
  }
  const toggleType = (k: CType) =>
    setTypes((s) => { const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n })
  const setModel = (kind: string, id: string) => setModelByKind((s) => ({ ...s, [kind]: id }))

  const imgI2i = types.has('image') && chosen('image')?.input === 'image'
  const vidI2v = types.has('video') && chosen('video')?.input === 'image'

  /**
   * 🔑 PLAN PÉRIMÉ. Le plan est produit À PARTIR du brief, des types et du nombre de
   * concepts ; rien ne le régénère quand l'un des trois change. Avant, `toggleType` ne
   * touchait pas à `plan` : on proposait avec « image » seul (donc `video_prompt: null`),
   * on cochait « vidéo », et « Générer » partait quand même — une vidéo PAYANTE lancée
   * sans prompt. On ne se contente pas de recalculer en douce : l'écran le DIT et refuse,
   * parce que reproposer coûte un appel au modèle et écrase les corrections déjà faites.
   */
  const stale = plan !== null && planFrom !== planSignature(brief, types, nConcepts)

  // Coût total estimé — recalcul direct.
  const nC = plan && !stale ? plan.concepts.length : nConcepts
  const cost = useMemo(() => {
    let total = 0
    if (types.has('image')) {
      const m = chosen('image')
      total += nC * ratios.reduce((s, r) => s + estimateCost(m, { aspect_ratio: r }, ''), 0)
    }
    if (types.has('video')) {
      const m = chosen('video')
      total += videoRatios.length * estimateCost(m, {}, '')
    }
    if (types.has('voice')) total += estimateCost(chosen('voice'), {}, plan?.voiceover ?? brief)
    if (types.has('music')) total += estimateCost(chosen('music'), {}, '')
    return total
  }, [types, nC, ratios, videoRatios, plan, brief, modelByKind, models]) // eslint-disable-line react-hooks/exhaustive-deps

  const planMut = useMutation({
    mutationFn: () =>
      api.post<Plan>('/generation/campaign/plan', {
        brief: brief.trim(), n_concepts: nConcepts, types: [...types],
      }).then((r) => r.data),
    onSuccess: (d) => {
      setErr(null); setPlan(d)
      setPlanFrom(planSignature(brief, types, nConcepts))
      setPhase('plan')
    },
    onError: onErr,
  })

  const execMut = useMutation({
    mutationFn: () => {
      const voice = chosen('voice'); const music = chosen('music')
      return api.post<CampaignRead>('/generation/campaign', {
        brief: brief.trim(), types: [...types],
        concepts: plan?.concepts ?? [],
        ratios, reference_media_ids: imageRefIds, model: chosen('image')?.id,
        video_model: chosen('video')?.id, video_ratios: videoRatios,
        video_reference_media_ids: videoRefIds, video_prompt: plan?.video_prompt,
        video_negative_prompt: videoNeg.trim() || null, video_mute: videoMute,
        voice_provider: voice?.provider, voice_model: voice?.id, voiceover: plan?.voiceover,
        music_provider: music?.provider, music_model: music?.id, music_prompt: plan?.music_prompt,
      })
    },
    onSuccess: () => {
      setErr(null)
      setPhase('run')
      qc.invalidateQueries({ queryKey: ['campaigns'] })
    },
    onError: onErr,
  })

  const { data: campaigns = [] } = useQuery<CampaignRead[]>({
    queryKey: ['campaigns', orgId],
    queryFn: () => api.get('/generation/campaigns').then((r) => r.data),
    enabled: !!orgId,
    refetchInterval: (q) =>
      (q.state.data as CampaignRead[] | undefined)?.some(
        (c) => c.status === 'pending' || c.status === 'processing') ? 4000 : false,
  })

  const busy = planMut.isPending || execMut.isPending
  const totalAssets = (types.has('image') ? nC * ratios.length : 0)
    + (types.has('video') ? videoRatios.length : 0)
    + (types.has('voice') ? 1 : 0) + (types.has('music') ? 1 : 0)
  const refsOk = (!imgI2i || imageRefIds.length > 0) && (!vidI2v || videoRefIds.length > 0)

  /**
   * Règle P3.3 : un bouton grisé DIT sa raison. On renvoie la 1re qui bloque — un
   * message unique et actionnable vaut mieux qu'une liste de conditions.
   */
  const proposeBlocker: string | null =
    !hasFal ? t('studio.no_key')
      : !brief.trim() ? t('studio.why_no_brief')
        : types.size === 0 ? t('studio.why_no_type')
          : null
  const generateBlocker: string | null =
    stale ? t('studio.why_stale')
      : totalAssets === 0 ? t('studio.why_no_assets')
        : totalAssets > MAX_ASSETS ? t('studio.too_many', { max: MAX_ASSETS })
          : !refsOk ? t('studio.why_refs')
            : null

  const canPlan = proposeBlocker === null && !busy
  const canExec = !!plan && generateBlocker === null && !busy

  function restart() {
    setPlan(null); setPlanFrom(null); setBrief(''); setVideoNeg(''); setVideoMute(false)
    setErr(null); setPhase('brief')
  }

  // Fonctions JSX (PAS des composants) → inlinées dans le parent, aucun remount à la frappe.
  const modelSelect = (kind: ModelSpec['kind'], label: string) => (
    <div>
      <label className="mb-1 block text-xs font-medium text-gray-700">{label}</label>
      <select value={chosen(kind)?.id ?? ''} onChange={(e) => setModel(kind, e.target.value)}
        className="w-full rounded border border-gray-200 bg-surface px-2 py-1.5 text-sm">
        {modelsOf(kind).map((m) => (
          <option key={m.id} value={m.id}>{genModel(m)} · {t(`generation.tier_${m.tier}`)}</option>
        ))}
      </select>
    </div>
  )

  const formats = (value: string[], onChange: (v: string[]) => void) => (
    <div>
      <label className="mb-1 block text-xs font-medium text-gray-700">{t('studio.formats')}</label>
      <div className="flex flex-wrap gap-2">
        {RATIOS.map((r) => (
          <button key={r} type="button"
            onClick={() => onChange(value.includes(r) ? value.filter((x) => x !== r) : [...value, r])}
            className={`rounded-lg border px-3 py-1.5 text-sm transition-sf ${
              value.includes(r) ? 'border-accent bg-accent/10 text-accent font-medium'
                : 'border-gray-200 text-gray-600 hover:bg-gray-50'}`}>
            {t(`generation.opt_ratio_${r.replace(':', '_')}`)}
          </button>
        ))}
      </div>
    </div>
  )

  const block = 'rounded-lg border border-gray-200 bg-surface p-4 space-y-3 shadow-sf-card'
  const blockHead = (Icon: typeof ImageIcon, label: string) => (
    <div className="flex items-center gap-1.5 text-sm font-semibold text-gray-900">
      <Icon className="h-4 w-4 text-accent" />{label}
    </div>
  )

  /** Rappel compact des réglages en vigueur — sans lui, « masquer » vaudrait « cacher ». */
  const settingsSummary = [
    types.has('image') && t('studio.sum_concepts', { n: nConcepts }),
    types.has('image') && ratios.length > 0 && ratios.join(' · '),
    types.has('image') && chosen('image') && genModel(chosen('image')!),
    types.has('video') && chosen('video') && genModel(chosen('video')!),
  ].filter(Boolean).join(' · ')

  const STEPS: { key: Phase; label: string }[] = [
    { key: 'brief', label: t('studio.phase_brief') },
    { key: 'plan', label: t('studio.phase_plan') },
    { key: 'run', label: t('studio.phase_run') },
  ]
  const stepIndex = STEPS.findIndex((s) => s.key === phase)

  return (
    <>
      <Topbar title={t('studio.title')}>
        <div className="flex items-center gap-2">
          {phase !== 'run' && totalAssets > 0 && types.size > 0 && (
            <span className="hidden sm:inline-flex items-center gap-1.5 rounded-lg border border-gray-200 px-2.5 py-1 text-xs text-gray-600">
              {t('studio.total_cost', { assets: totalAssets })}
              <span className="font-semibold text-gray-800">≈ {fmtCost(cost)}</span>
            </span>
          )}
          <Button size="sm" variant="secondary" onClick={() => setSettingsOpen(true)}>
            <SlidersHorizontal className="w-3.5 h-3.5 mr-1" />{t('studio.advanced')}
          </Button>
        </div>
      </Topbar>

      {/* Page VERROUILLÉE AU VIEWPORT : bandeau fixe, une seule zone de défilement. */}
      <main className="flex-1 flex flex-col min-h-0">

        {/* Bandeau des trois temps */}
        <div className="shrink-0 flex items-center gap-1 border-b border-gray-200 bg-surface px-6 py-2.5">
          {STEPS.map((s, i) => (
            <div key={s.key} className="flex items-center gap-1">
              {i > 0 && <span className="mx-2 h-px w-8 bg-gray-200" aria-hidden />}
              <span data-step={s.key} data-state={i === stepIndex ? 'current' : i < stepIndex ? 'done' : 'todo'}
                className={`inline-flex items-center gap-1.5 text-xs ${
                  i === stepIndex ? 'text-gray-900 font-semibold'
                    : i < stepIndex ? 'text-gray-500' : 'text-gray-400'}`}>
                <span className={`flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold ${
                  i === stepIndex ? 'bg-accent text-white'
                    : i < stepIndex ? 'bg-green-500/20 text-green-600' : 'bg-gray-100 text-gray-400'}`}>
                  {i < stepIndex ? <Check className="h-3 w-3" /> : i + 1}
                </span>
                {s.label}
              </span>
            </div>
          ))}
        </div>

        <div className={`flex-1 min-h-0 overflow-y-auto px-6 py-6 ${
          phase === 'brief' ? 'flex flex-col justify-center' : ''}`}>

          {!hasFal && (
            <p className="mb-4 rounded-lg border border-amber-300 dark:border-amber-500/40 bg-amber-500/10 dark:bg-amber-500/15 px-3 py-2 text-sm text-amber-700 dark:text-amber-300">
              {t('studio.no_key')}
            </p>
          )}

          {/* ─────────────────────────── TEMPS 1 — LE BRIEF ─────────────────────────
              Entrée épurée : une seule question à l'écran, les réglages en retrait. */}
          {phase === 'brief' && (
            <div className="mx-auto w-full max-w-2xl space-y-4">
              <div className="text-center">
                <h2 className="text-base font-semibold text-gray-900">{t('studio.brief_question')}</h2>
                <p className="mt-1 text-sm text-gray-500">{t('studio.intro')}</p>
              </div>

              <textarea value={brief} onChange={(e) => setBrief(e.target.value)} rows={5}
                placeholder={t('studio.brief_ph')} aria-label={t('studio.brief')} data-testid="brief-input"
                className="w-full resize-none rounded-lg border border-gray-200 bg-surface px-3 py-2.5 text-sm focus:border-accent focus:outline-none" />

              <div className="flex flex-wrap justify-center gap-2">
                {availTypes.map(({ key, icon: Icon }) => (
                  <button key={key} type="button" onClick={() => toggleType(key)}
                    aria-pressed={types.has(key)} data-type={key}
                    className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm transition-sf ${
                      types.has(key) ? 'border-accent bg-accent/10 text-accent font-medium'
                        : 'border-gray-200 text-gray-600 hover:bg-gray-50'}`}>
                    <Icon className="h-4 w-4" />{t(`studio.type_${key}`)}
                  </button>
                ))}
              </div>

              <div className="flex flex-col items-center gap-2">
                <div className="flex flex-wrap items-center justify-center gap-2">
                  <Button loading={planMut.isPending} disabled={!canPlan}
                    data-testid="propose" onClick={() => planMut.mutate()}>
                    <Wand2 className="w-4 h-4 mr-1.5" />
                    {plan ? t('studio.replan') : t('studio.propose')}
                  </Button>
                  {/* Revenir au plan SANS rappeler le modèle : les corrections déjà
                      faites sur les concepts sont du travail, on ne les jette pas
                      parce que l'utilisateur a voulu relire son brief. */}
                  {plan && (
                    <Button variant="secondary" data-testid="back-to-plan"
                      onClick={() => setPhase('plan')}>
                      {t('studio.back_to_plan')}
                    </Button>
                  )}
                </div>
                {/* Le bouton grisé DIT pourquoi (P3.3). */}
                {proposeBlocker && !busy && (
                  <p data-testid="propose-blocker" className="text-xs text-amber-600 dark:text-amber-400">
                    {proposeBlocker}
                  </p>
                )}
                {plan && stale && !busy && (
                  <p data-testid="brief-stale" className="text-xs text-amber-600 dark:text-amber-400">
                    {t('studio.stale_warning')}
                  </p>
                )}
              </div>

              {/* Réglages en vigueur, énoncés — masquer sans dire serait cacher. */}
              {settingsSummary && (
                <p className="text-center text-xs text-gray-400">
                  {settingsSummary}
                  {' — '}
                  <button type="button" onClick={() => setSettingsOpen(true)}
                    className="text-accent hover:underline">{t('studio.modify')}</button>
                </p>
              )}
            </div>
          )}

          {/* ────────────────────── TEMPS 2 — LE PLAN PROPOSÉ ───────────────────────
              Le moment qui compte : on relit ce que l'IA a compris avant de dépenser. */}
          {phase === 'plan' && plan && (
            <div className="mx-auto w-full max-w-6xl space-y-4">
              <div className="flex flex-wrap items-baseline gap-3">
                <h2 className="text-base font-semibold text-gray-900">{t('studio.plan_review')}</h2>
                <span className="text-xs text-gray-500">{t('studio.plan_hint')}</span>
                <Button size="sm" variant="secondary" className="ml-auto"
                  data-testid="back-to-brief" onClick={() => setPhase('brief')}>
                  <ArrowLeft className="w-3.5 h-3.5 mr-1" />{t('studio.back_to_brief')}
                </Button>
              </div>

              {stale && (
                <div data-testid="stale-plan"
                  className="flex flex-wrap items-center gap-3 rounded-lg border border-amber-300 dark:border-amber-500/40 bg-amber-500/10 dark:bg-amber-500/15 px-3 py-2.5 text-sm text-amber-700 dark:text-amber-300">
                  <AlertTriangle className="h-4 w-4 shrink-0" />
                  <span className="flex-1">{t('studio.stale_warning')}</span>
                  <Button size="sm" variant="secondary" loading={planMut.isPending}
                    data-testid="replan" disabled={!canPlan} onClick={() => planMut.mutate()}>
                    <RotateCcw className="w-3.5 h-3.5 mr-1" />{t('studio.replan')}
                  </Button>
                </div>
              )}

              {types.has('image') && (
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                  {plan.concepts.map((c, i) => (
                    <div key={i} className={block}>
                      <input value={c.title} aria-label={t('studio.concept_title', { n: i + 1 })}
                        onChange={(e) => setPlan((p) => p && ({ ...p, concepts: p.concepts.map((x, j) => j === i ? { ...x, title: e.target.value } : x) }))}
                        className="w-full bg-transparent text-sm font-semibold text-gray-900 focus:outline-none" />
                      <textarea value={c.prompt} rows={4}
                        aria-label={t('studio.concept_prompt', { n: i + 1 })}
                        onChange={(e) => setPlan((p) => p && ({ ...p, concepts: p.concepts.map((x, j) => j === i ? { ...x, prompt: e.target.value } : x) }))}
                        className="w-full resize-none rounded-lg border border-gray-200 px-2 py-1.5 text-xs text-gray-600 focus:border-accent focus:outline-none" />
                    </div>
                  ))}
                </div>
              )}

              {types.has('video') && (
                <div className={block}>
                  {blockHead(Film, t('studio.type_video'))}
                  <PromptField label={t('generation.prompt')} value={plan.video_prompt ?? ''}
                    onChange={(v) => setPlan((p) => p && ({ ...p, video_prompt: v }))} />
                  <PromptAssistantPanel
                    kind="video"
                    model={chosen('video')?.id}
                    referenceMediaIds={videoRefIds}
                    onApply={(pr, neg) => {
                      setPlan((p) => p && ({ ...p, video_prompt: pr }))
                      if (neg !== null) setVideoNeg(neg)
                    }}
                  />
                  {chosen('video')?.supports_negative_prompt && (
                    <PromptField label={t('generation.negative_prompt')} value={videoNeg}
                      onChange={setVideoNeg} placeholder={t('generation.negative_prompt_ph')} />
                  )}
                  <label className="flex items-center gap-2 text-xs text-gray-700">
                    <input type="checkbox" checked={videoMute} onChange={(e) => setVideoMute(e.target.checked)}
                      className="rounded-lg border-gray-300 text-accent focus:ring-accent/40" />
                    {t('generation.mute')}
                  </label>
                </div>
              )}

              {types.has('voice') && (
                <div className={block}>
                  {blockHead(Mic, t('studio.type_voice'))}
                  <PromptField label={t('generation.prompt')} value={plan.voiceover ?? ''}
                    onChange={(v) => setPlan((p) => p && ({ ...p, voiceover: v }))} />
                </div>
              )}

              {types.has('music') && (
                <div className={block}>
                  {blockHead(Music, t('studio.type_music'))}
                  <PromptField label={t('generation.prompt')} value={plan.music_prompt ?? ''}
                    onChange={(v) => setPlan((p) => p && ({ ...p, music_prompt: v }))} />
                </div>
              )}

              <div className="flex flex-wrap items-center gap-3 border-t border-gray-200 pt-4">
                <Button loading={execMut.isPending} disabled={!canExec}
                  data-testid="generate" onClick={() => execMut.mutate()}>
                  <Sparkles className="w-4 h-4 mr-1.5" />
                  {t('studio.generate_n', { n: totalAssets })} · ≈ {fmtCost(cost)}
                </Button>
                {generateBlocker && !busy ? (
                  <p data-testid="generate-blocker" className="text-xs text-amber-600 dark:text-amber-400">
                    {generateBlocker}
                  </p>
                ) : (
                  <p className="text-xs text-gray-400">{t('studio.nothing_yet')}</p>
                )}
              </div>
            </div>
          )}

          {/* ───────────────────── TEMPS 3 — LA GÉNÉRATION ────────────────────────── */}
          {phase === 'run' && (
            <div className="mx-auto w-full max-w-4xl space-y-4">
              <div className="flex flex-wrap items-baseline gap-3">
                <h2 className="text-base font-semibold text-gray-900">{t('studio.recent')}</h2>
                <Button size="sm" className="ml-auto" onClick={restart}>
                  <Wand2 className="w-3.5 h-3.5 mr-1" />{t('studio.new_campaign')}
                </Button>
              </div>

              {campaigns.length === 0 ? (
                <p className="text-sm text-gray-500">{t('studio.run_empty')}</p>
              ) : (
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  {campaigns.map((c) => {
                    const done = c.status === 'ready' || c.status === 'failed'
                    const pct = c.jobs_total > 0 ? Math.round((c.jobs_ready / c.jobs_total) * 100) : 0
                    return (
                      <div key={c.id_campaign} className={block}>
                        <div className="flex items-start justify-between gap-2">
                          <span className="line-clamp-2 text-sm font-medium text-gray-900" title={c.brief}>{c.brief}</span>
                          {done ? <CheckCircle2 className="h-4 w-4 shrink-0 text-green-600" />
                            : <Loader2 className="h-4 w-4 shrink-0 animate-spin text-accent" />}
                        </div>
                        <div className="h-1.5 overflow-hidden rounded-full bg-gray-100">
                          <div className="h-full rounded-full bg-accent transition-sf" style={{ width: `${pct}%` }} />
                        </div>
                        <div className="flex items-center gap-2 text-xs text-gray-500">
                          <span>{t('studio.progress', { ready: c.jobs_ready, total: c.jobs_total })}</span>
                          {c.jobs_failed > 0 && <span className="text-danger">· {t('studio.failed', { n: c.jobs_failed })}</span>}
                          {c.total_cost_usd != null && <span className="ml-auto font-medium text-gray-700">≈ {fmtCost(c.total_cost_usd)}</span>}
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}

              <Link href="/media" className="inline-flex items-center gap-1 text-xs text-accent hover:underline">
                <ImageIcon className="h-3.5 w-3.5" />{t('studio.view_in_library')}
              </Link>
            </div>
          )}

          {err && <p className="mt-4 text-sm text-danger">{err}</p>}
        </div>
      </main>

      {/* ── Réglages avancés — hors du flux, comme sur le générateur promo ─────────── */}
      <Drawer open={settingsOpen} onClose={() => setSettingsOpen(false)}
        title={t('studio.advanced')} width="w-[540px]">
        <div className="space-y-4">
          <p className="text-xs text-gray-500">{t('studio.advanced_hint')}</p>

          {types.has('image') && (
            <div className={block}>
              {blockHead(ImageIcon, t('studio.type_image'))}
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div className="sm:col-span-2">{modelSelect('image', t('studio.model'))}</div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-700">{t('studio.concepts')}</label>
                  <NumberField min={1} max={6} value={nConcepts}
                    onCommit={(v) => setNConcepts(v ?? 1)}
                    className="w-full rounded border border-gray-200 bg-surface px-2 py-1.5 text-sm" />
                </div>
              </div>
              {formats(ratios, setRatios)}
              <ReferencePicker value={imageRefIds} onChange={setImageRefIds} maxRefs={chosen('image')?.max_refs ?? 4} />
              {imageRefIds.length > 0 && !imgI2i && (
                <p className="flex items-center gap-1 text-[11px] text-amber-600 dark:text-amber-400">
                  <AlertTriangle className="h-3 w-3" />{t('studio.ref_pick_i2i')}
                </p>
              )}
            </div>
          )}

          {types.has('video') && (
            <div className={block}>
              {blockHead(Film, t('studio.type_video'))}
              {modelSelect('video', t('studio.video_model'))}
              {formats(videoRatios, setVideoRatios)}
              <p className="text-[11px] text-gray-400">{t('studio.video_one_per_format')}</p>
              {vidI2v && (
                <ReferencePicker value={videoRefIds} onChange={setVideoRefIds} maxRefs={chosen('video')?.max_refs ?? 1} />
              )}
            </div>
          )}

          {types.has('voice') && (
            <div className={block}>
              {blockHead(Mic, t('studio.type_voice'))}
              {modelSelect('voice', t('studio.voice_model'))}
            </div>
          )}

          {types.has('music') && (
            <div className={block}>
              {blockHead(Music, t('studio.type_music'))}
              {modelSelect('music', t('studio.music_model'))}
            </div>
          )}

          {types.size === 0 && <p className="text-sm text-gray-500">{t('studio.why_no_type')}</p>}
        </div>
      </Drawer>
    </>
  )
}

function PromptField({ label, value, onChange, placeholder }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string
}) {
  return (
    <div className="space-y-1">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500">{label}</h3>
      <textarea value={value} rows={2} onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder} aria-label={label}
        className="w-full resize-none rounded-lg border border-gray-200 px-2 py-1.5 text-xs text-gray-600 focus:border-accent focus:outline-none" />
    </div>
  )
}
