'use client'

// Mises à jour contrôlées de l'OS (issue vitrail#6) : en attente par player → mise à jour du BANC
// (exploitant du serveur) qui produit un MANIFESTE → approbation (promotion, exploitant) →
// déploiement au parc, arrêté au premier échec. Le serveur refuse tout geste hors de ce circuit ;
// cet écran ne fait que le rendre lisible.
import { useMemo, useState } from 'react'
import { useTranslations } from 'next-intl'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, RefreshCw, Rocket, ShieldAlert, Trash2, FlaskConical } from 'lucide-react'
import Button from '@/components/ui/Button'
import Drawer from '@/components/ui/Drawer'
import NumberField from '@/components/ui/NumberField'
import api from '@/lib/api'

export interface OsUpdateInfo {
  helper: boolean
  family: string | null
  pending: number | null
  security: number | null
  checked_at: string | null
  last_upgrade: string | null
  reboot_required: boolean
  update_error: string | null
  applied: string | null
  run: { action: string; state: string; error: string | null; at: string | null; count: number; reboot_required?: boolean } | null
}
export interface OsPlayer {
  id_player: number; player_name: string; status: string; os_update: OsUpdateInfo | null
}
interface OsVersion {
  id_version: number; version: string; channel: string; model_family: string | null
  created_at: string
  manifest: { packages?: { name: string; arch: string; version: string }[]; source_player?: number; reboot_required?: boolean } | null
}

const inp = 'w-full rounded-lg border border-gray-200 bg-surface px-2 py-1.5 text-sm focus:border-accent focus:outline-none'
const sel = 'rounded-lg border border-gray-200 bg-surface px-2 py-1.5 text-sm text-gray-700 focus:border-accent focus:outline-none'

export default function OsUpdatesDrawer({ open, onClose, players, isOperator, groups }: {
  open: boolean; onClose: () => void; players: OsPlayer[]; isOperator: boolean
  groups: { id_group: number; group_name: string }[]
}) {
  const t = useTranslations('updates')
  const tc = useTranslations('common')
  const qc = useQueryClient()
  const errDetail = (e: unknown) =>
    (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || tc('error')
  const fmt = (iso: string | null) => iso
    ? new Date(iso).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' }) : '—'

  const { data: manifests = [] } = useQuery<OsVersion[]>({
    queryKey: ['ota-versions-os'], enabled: open, refetchInterval: open ? 10000 : false,
    queryFn: () => api.get('/updates/versions?track=os').then((r) => r.data),
  })
  // Seuls les players Linux qui remontent un état OS sont concernés (Windows : plus tard).
  const linux = useMemo(() => players.filter((p) => p.os_update), [players])
  const capable = useMemo(() => linux.filter((p) => p.os_update?.helper), [linux])
  const nameById = useMemo(() => new Map(players.map((p) => [p.id_player, p.player_name])), [players])

  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const check = useMutation({
    mutationFn: (id_player: number) => api.post('/updates/os/check', { id_player }),
    onSuccess: () => setMsg({ ok: true, text: t('os_check_sent') }),
    onError: (e) => setMsg({ ok: false, text: errDetail(e) }),
  })
  const [benchId, setBenchId] = useState<number | null>(null)
  const bench = useMutation({
    mutationFn: (id_player: number) => api.post('/updates/os/bench', { id_player }),
    onSuccess: () => setMsg({ ok: true, text: t('os_bench_sent') }),
    onError: (e) => setMsg({ ok: false, text: errDetail(e) }),
  })
  const approve = useMutation({
    mutationFn: (id: number) => api.post(`/updates/versions/${id}/promote`),
    onSuccess: () => { setMsg(null); qc.invalidateQueries({ queryKey: ['ota-versions-os'] }) },
    onError: (e) => setMsg({ ok: false, text: errDetail(e) }),
  })
  const remove = useMutation({
    mutationFn: (id: number) => api.delete(`/updates/versions/${id}`),
    onSuccess: () => { setMsg(null); qc.invalidateQueries({ queryKey: ['ota-versions-os'] }) },
    onError: (e) => setMsg({ ok: false, text: errDetail(e) }),
  })

  const [target, setTarget] = useState<{ type: string; id: number | null }>({ type: 'player', id: null })
  const [win, setWin] = useState('')
  const [force, setForce] = useState(false)
  const [wave, setWave] = useState(1)   // players à la fois (vagues)
  const targetReady = target.type === 'org' || target.id != null
  const tPlayer = target.type === 'player' && target.id != null
    ? linux.find((p) => p.id_player === target.id) ?? null : null
  const visible = tPlayer ? manifests.filter((m) => m.model_family === tPlayer.os_update?.family) : manifests
  const deploy = useMutation({
    mutationFn: (id_version: number) => {
      const sp: Record<string, unknown> = {}
      if (win.trim()) sp.install_window = win.trim()
      if (force) sp.force = true
      sp.wave_size = wave
      const targets = target.type === 'org' ? [{ target_type: 'org' }] : [{ target_type: target.type, target_id: target.id }]
      return api.post('/updates/deployments', {
        id_version, strategy: sp.install_window ? 'scheduled' : 'immediate', strategy_params: sp, targets,
      })
    },
    onSuccess: (r: { data: { total_targets: number } }) => {
      setMsg(r.data.total_targets > 0 ? { ok: true, text: t('os_deployed', { n: r.data.total_targets }) }
        : { ok: false, text: t('os_deployed_none') })
      qc.invalidateQueries({ queryKey: ['ota-deployments'] })
    },
    onError: (e) => setMsg({ ok: false, text: errDetail(e) }),
  })

  const runLabel = (o: OsUpdateInfo) => {
    if (!o.run) return '—'
    const what = t(`os_run_${o.run.action === 'bench' ? 'bench' : 'apply'}`)
    return `${what} · ${t(`os_state_${o.run.state}`)}${o.run.state === 'ok' ? ` · ${t('os_n_packages', { n: o.run.count })}` : ''}`
  }

  return (
    <Drawer open={open} onClose={onClose} title={t('os_title')}>
      <div className="space-y-4" data-testid="os-drawer">
        <p className="text-xs text-gray-400">{t('os_intro')}</p>
        {msg && <p className={`text-[11px] ${msg.ok ? 'text-success' : 'text-danger'}`} data-testid="os-msg">{msg.text}</p>}

        {/* 1. Ce qui est en attente, par player */}
        <section className="space-y-2">
          <div className="text-xs font-medium text-gray-600">{t('os_pending_title')}</div>
          {linux.length === 0 && <p className="text-xs text-gray-400" data-testid="os-no-player">{t('os_no_player')}</p>}
          <ul className="divide-y divide-gray-100" data-testid="os-players">
            {linux.map((p) => {
              const o = p.os_update as OsUpdateInfo
              return (
                <li key={p.id_player} className="space-y-0.5 py-2 text-xs">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-gray-800">{p.player_name}</span>
                    {o.family && <span className="font-mono text-[11px] text-gray-400">{o.family}</span>}
                    <span className="flex-1" />
                    {o.helper && <button type="button" className="text-accent hover:underline disabled:opacity-50"
                      disabled={p.status !== 'online' || check.isPending} onClick={() => check.mutate(p.id_player)}
                      title={t('os_check_now')}><RefreshCw className="inline h-3.5 w-3.5" /></button>}
                  </div>
                  {!o.helper ? (
                    <p className="text-amber-700 dark:text-amber-300">{t('os_no_helper')}</p>
                  ) : (
                    <p className="text-gray-500">
                      {o.pending == null ? t('os_not_checked') : t('os_pending', { n: o.pending })}
                      {!!o.security && <span className="ml-1 inline-flex items-center gap-0.5 text-danger">
                        <ShieldAlert className="h-3 w-3" />{t('os_security', { n: o.security })}</span>}
                      {o.reboot_required && <span className="ml-1 text-amber-700 dark:text-amber-300">· {t('os_reboot_pending')}</span>}
                      <span className="text-gray-400"> · {t('os_checked', { at: fmt(o.checked_at) })}</span>
                    </p>
                  )}
                  {o.helper && <p className="text-gray-400">{t('os_last_run')} : {runLabel(o)}
                    {o.run?.error && <span className="text-danger"> — {o.run.error}</span>}</p>}
                  {o.update_error && <p className="text-danger">{o.update_error}</p>}
                </li>
              )
            })}
          </ul>
        </section>

        {/* 2. Banc : réservé à l'exploitant du serveur */}
        <section className="space-y-2 rounded-lg border border-gray-100 p-3">
          <div className="flex items-center gap-1.5 text-xs font-medium text-gray-600">
            <FlaskConical className="h-3.5 w-3.5 text-accent" />{t('os_bench_title')}</div>
          <p className="text-[11px] text-gray-400">{t('os_bench_hint')}</p>
          {!isOperator ? (
            <p className="rounded bg-gray-50 px-2 py-1.5 text-[11px] text-gray-500 dark:bg-surface" data-testid="os-not-operator">{t('os_not_operator')}</p>
          ) : (
            <div className="flex gap-2">
              <select className={sel + ' min-w-0 flex-1'} value={benchId ?? ''} data-testid="os-bench-player"
                aria-label={t('os_bench_title')}
                onChange={(e) => setBenchId(e.target.value ? Number(e.target.value) : null)}>
                <option value="">{t('target_pick')}</option>
                {capable.map((p) => <option key={p.id_player} value={p.id_player} disabled={p.status !== 'online'}>
                  {p.player_name} · {p.os_update?.family} · {t('os_pending', { n: p.os_update?.pending ?? 0 })}</option>)}
              </select>
              <Button size="sm" disabled={benchId == null} loading={bench.isPending}
                onClick={() => benchId != null && bench.mutate(benchId)} data-testid="os-bench-btn">{t('os_bench_btn')}</Button>
            </div>
          )}
        </section>

        {/* 3. Manifestes : approbation (exploitant) puis déploiement */}
        <section className="space-y-2">
          <div className="text-xs font-medium text-gray-600">{t('os_manifests_title')}</div>
          <div className="space-y-2 rounded border border-gray-100 bg-gray-50 p-2 dark:bg-surface">
            <div className="text-[11px] font-medium text-gray-500">{t('engine_deploy_on')}</div>
            <div className="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-2">
              <select className={sel + ' w-full'} value={target.type} aria-label={t('target')} data-testid="os-target-type"
                onChange={(e) => setTarget({ type: e.target.value, id: null })}>
                <option value="player">{t('target_player')}</option>
                <option value="group">{t('target_group')}</option>
                <option value="org">{t('target_org')}</option>
              </select>
              {target.type !== 'org' && (
                <select className={sel + ' w-full'} value={target.id ?? ''} aria-label={t(`target_${target.type}`)} data-testid="os-target-id"
                  onChange={(e) => setTarget({ ...target, id: e.target.value ? Number(e.target.value) : null })}>
                  <option value="">{t('target_pick')}</option>
                  {target.type === 'player' && capable.map((p) => (
                    <option key={p.id_player} value={p.id_player}>{p.player_name} · {p.os_update?.family}</option>))}
                  {target.type === 'group' && groups.map((g) => (
                    <option key={g.id_group} value={g.id_group}>{g.group_name}</option>))}
                </select>
              )}
            </div>
            <div className="grid grid-cols-2 items-end gap-2">
              <label className="space-y-0.5 text-[11px] text-gray-500">
                <span>{t('install_window')}</span>
                <input className={inp} placeholder="HH:MM-HH:MM" value={win} onChange={(e) => setWin(e.target.value)} />
              </label>
              <label className="flex items-center gap-1.5 pb-2 text-[11px] text-gray-600">
                <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />{t('force')}
              </label>
              <label className="col-span-2 space-y-0.5 text-[11px] text-gray-500">
                <span>{t('wave_size')}</span>
                <NumberField className={inp} min={1} max={50} value={wave}
                  onCommit={(v) => setWave(v ?? 1)} title={t('wave_hint')} />
                <span className="block text-gray-400">{t('wave_hint')}</span>
              </label>
            </div>
          </div>
          <ul className="divide-y divide-gray-100" data-testid="os-manifests">
            {visible.map((m) => {
              const pk = m.manifest?.packages ?? []
              const beta = m.channel !== 'stable'
              // Même règle que le serveur : un manifeste non approuvé ne part que sur UN player.
              const betaBlocked = beta && !isOperator && target.type !== 'player'
              return (
                <li key={m.id_version} className="space-y-1 py-2 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-gray-800">{m.version}</span>
                    <span className="font-mono text-[11px] text-gray-400">{m.model_family}</span>
                    <span className={beta ? 'badge-warning' : 'badge-success'}>
                      {beta ? t('os_to_approve') : <><CheckCircle2 className="mr-0.5 inline h-3 w-3" />{t('os_approved')}</>}</span>
                    <span className="flex-1" />
                    {beta && isOperator && <Button size="sm" loading={approve.isPending}
                      onClick={() => approve.mutate(m.id_version)} data-testid="os-approve">{t('os_approve')}</Button>}
                    <Button size="sm" variant="secondary" disabled={!targetReady || betaBlocked || deploy.isPending}
                      title={!targetReady ? t('target_pick') : betaBlocked ? t('os_beta_one_player') : undefined}
                      onClick={() => deploy.mutate(m.id_version)} data-testid="os-deploy">
                      <Rocket className="mr-1 h-3.5 w-3.5" />{t('deploy')}</Button>
                    {isOperator && <button type="button" className="text-gray-400 hover:text-danger" title={tc('delete')}
                      onClick={() => remove.mutate(m.id_version)}><Trash2 className="h-3.5 w-3.5" /></button>}
                  </div>
                  {betaBlocked && <p className="text-right text-[11px] text-amber-700 dark:text-amber-300"
                    data-testid="os-row-reason">{t('os_beta_one_player')}</p>}
                  <details className="text-[11px] text-gray-500">
                    <summary className="cursor-pointer">
                      {t('os_n_packages', { n: pk.length })}
                      {m.manifest?.source_player != null && <> · {t('os_from_bench', { name: nameById.get(m.manifest.source_player) ?? `#${m.manifest.source_player}` })}</>}
                      {m.manifest?.reboot_required && <> · {t('os_needs_reboot')}</>}
                    </summary>
                    <ul className="mt-1 max-h-48 overflow-y-auto font-mono">
                      {pk.map((p) => <li key={`${p.name}:${p.arch}`}>{p.name}:{p.arch} {p.version}</li>)}
                    </ul>
                  </details>
                </li>
              )
            })}
            {visible.length === 0 && <li className="py-2 text-xs text-gray-400">{t('os_no_manifest')}</li>}
          </ul>
          <p className="text-[11px] text-gray-400">{t('os_deploy_hint')}</p>
        </section>
      </div>
    </Drawer>
  )
}
