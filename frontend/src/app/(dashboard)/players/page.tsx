'use client'

import React, { useState, useEffect } from 'react'
import { useTranslations } from 'next-intl'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Monitor, Plus, Pencil, Trash2, RefreshCw, Camera, Power, RotateCcw, Search, Zap, Square, Tv2, Sun, Plug, Activity, Volume2, VolumeX, SlidersHorizontal, Bell, AlertTriangle, Wifi, Crown, Clock, Info, HardDrive, Wrench, LayoutGrid, List } from 'lucide-react'
import Topbar from '@/components/layout/Topbar'
import Can from '@/components/auth/Can'
import StatusBadge from '@/components/ui/StatusBadge'
import Collapsible from '@/components/ui/Collapsible'
import MarqueeText from '@/components/ui/MarqueeText'
import Button from '@/components/ui/Button'
import Drawer from '@/components/ui/Drawer'
import Input from '@/components/ui/Input'
import NumberField from '@/components/ui/NumberField'
import TabBar from '@/components/ui/TabBar'
import { CardGrid } from '@/components/ui/CardGrid'
import api from '@/lib/api'
import { useSearchPrefill } from '@/lib/useSearchPrefill'
import { useOpenShortcut } from '@/lib/useOpenShortcut'
import type { PlayerRead, PlayerGroupRead, EventRead, DisplayConfig, PlayerCurrentState, OrgPlayerAlertSettings, Site } from '@/types/api'
import BulkCommandDrawer from '@/components/players/BulkCommandDrawer'
import { useCollapse } from '@/lib/useCollapse'
import PlayerListView, { sortPlayers, type SortKey, type SortDir } from './PlayerListView'
import PlayerFilters, {
  applyPlayerFilters, countActivePlayerFilters, EMPTY_PLAYER_FILTERS,
  type PlayerFilterState,
} from './PlayerFilters'
import LocalControlSection from './LocalControlSection'
import AudioOutputSection from './AudioOutputSection'
import ScreenPowerSection from './ScreenPowerSection'
import PopSignSection from './PopSignSection'
import SubtitleSection from './SubtitleSection'
import TagFilterSection from './TagFilterSection'
import { TagBadges, useTagNamespaces } from '@/components/tags/TagControls'
import DecodeSection, { DecodeBadge } from './DecodeSection'
import DeviceInfoSection from './DeviceInfoSection'
import FirmwareSection from './FirmwareSection'
import InventorySection from './InventorySection'
import ReadinessSection from './ReadinessSection'
import WallEditor, { type WallSave } from './WallEditor'
import { useAuthStore } from '@/store/auth'

/** Modèles BrightSign à entrée HDMI (Video In) : gamme XT (XT1143/XT1144/XT1145/XT2145…),
 *  4K1142/4K1242, XD1230. Les gammes HD (HD1024…) et LS n'ont PAS d'entrée HDMI → on masque les
 *  boutons « Entrée HDMI » pour ces modèles (bs_model = di.GetModel(), ex. "XT1144"). */
function bsHasHdmiInput(model?: string | null): boolean {
  const m = (model || '').toUpperCase()
  return /^XT\d/.test(m) || /^4K1[12]\d\d/.test(m) || m === 'XD1230'
}

function relativeTime(iso: string | null, tNever: string): string {
  if (!iso) return tNever
  const ms = Date.now() - new Date(iso).getTime()
  if (ms < 30000) return '< 30s'
  if (ms < 3600000) return `${Math.floor(ms / 60000)}m`
  if (ms < 86400000) return `${Math.floor(ms / 3600000)}h`
  return `${Math.floor(ms / 86400000)}d`
}

/** Débit lisible : Mbps quand c'est grand, sinon kbps. */
function fmtKbps(kbps: number): string {
  if (kbps >= 1000) {
    const mbps = kbps / 1000
    return `${mbps >= 10 ? Math.round(mbps) : mbps.toFixed(1)} Mbps`
  }
  return `${Math.round(kbps)} kbps`
}

function ResourceBar({ value, label }: { value: number | null; label: string }) {
  if (value === null) return <span className="text-xs text-gray-400">—</span>
  const pct = Math.min(100, Math.round(value))
  const color = pct > 90 ? 'bg-danger' : pct > 70 ? 'bg-warning' : 'bg-accent'
  return (
    <div className="space-y-0.5">
      <div className="flex justify-between text-xs text-gray-500">
        <span>{label}</span>
        <span>{pct}%</span>
      </div>
      <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
        <div className={`h-full rounded-full transition-all ${color}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

/** Compatibilité rôle player ↔ type de groupe (§XIX). Un player 'mixed' va partout ;
 *  un groupe 'mixed' n'accepte que des players 'mixed' (les autres n'ont pas les deux usages). */
function roleFitsGroup(
  role: PlayerRead['player_role'],
  groupType: PlayerGroupRead['group_type'],
): boolean {
  if (role === 'mixed') return true
  if (groupType === 'content') return role === 'content'
  if (groupType === 'audio') return role === 'audio'
  return false // groupe mixed → players mixed uniquement
}

function playerStatusLabel(status: PlayerRead['status'], t: ReturnType<typeof useTranslations>): string {
  const map: Record<PlayerRead['status'], string> = {
    online:      t('players.status_online'),
    offline:     t('players.status_offline'),
    warning:     t('players.status_warning'),
    maintenance: t('players.status_maintenance'),
    critical:    t('players.status_critical'),
  }
  return map[status] ?? status
}

const OS_LABELS: Record<string, string> = {
  'linux-arm64': 'Raspberry Pi',
  'linux-x64':   'Linux',
  'linux':       'Linux',
  'windows':     'Windows',
  'darwin':      'macOS',
  'android-arm64-v8a':  'Android TV',
  'android-armeabi-v7a': 'Android TV',
  'android-x86_64':     'Android TV',
  'android':            'Android TV',
  'chromeos':           'ChromeOS',
  'chrome_extension':   'Chrome Extension',
}

function OsChip({ os }: { os: string | null }) {
  if (!os) return <span className="text-gray-400">—</span>
  const osKey = os.toLowerCase()
  const label = OS_LABELS[osKey]
    ?? (osKey.startsWith('android') ? 'Android TV' : (os.charAt(0).toUpperCase() + os.slice(1)))
  return (
    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-600">
      {label}
    </span>
  )
}

// ── Event override helpers ─────────────────────────────────────────────────────

function computeOverriddenIds(events: EventRead[], allPlayerIds: number[]): Set<number> {
  const result = new Set<number>()
  for (const ev of events) {
    const tType = ev.target_config.target_type as string
    const tIds = (ev.target_config.target_ids as number[]) ?? []
    if (tType === 'org') {
      allPlayerIds.forEach((id) => result.add(id))
    } else if (tType === 'player') {
      tIds.forEach((id) => result.add(id))
    }
  }
  return result
}

function playerActiveEvents(events: EventRead[], player: PlayerRead, allPlayerIds: number[]): EventRead[] {
  return events.filter((ev) => {
    const tType = ev.target_config.target_type as string
    const tIds = (ev.target_config.target_ids as number[]) ?? []
    return (
      tType === 'org' ||
      (tType === 'player' && tIds.includes(player.id_player))
    )
  })
}

// ── Groups tab ─────────────────────────────────────────────────────────────────

function GroupsTab({
  players,
  t,
  onSelectPlayer,
}: {
  players: PlayerRead[]
  t: ReturnType<typeof useTranslations>
  onSelectPlayer: (p: PlayerRead) => void
}) {
  const [createOpen, setCreateOpen] = useState(false)
  const [newGroupName, setNewGroupName] = useState('')
  const [newGroupType, setNewGroupType] = useState<'content' | 'audio' | 'mixed'>('content')
  const [editGroup, setEditGroup] = useState<PlayerGroupRead | null>(null)
  const [editName, setEditName] = useState('')
  const [editType, setEditType] = useState<'content' | 'audio' | 'mixed'>('content')

  const { data: groups = [], refetch } = useQuery<PlayerGroupRead[]>({
    queryKey: ['player-groups'],
    queryFn: () => api.get<PlayerGroupRead[]>('/player-groups/').then((r) => r.data),
  })

  const handleCreate = async () => {
    if (!newGroupName.trim()) return
    await api.post('/player-groups/', { group_name: newGroupName.trim(), group_type: newGroupType })
    setNewGroupName('')
    setNewGroupType('content')
    setCreateOpen(false)
    refetch()
  }

  const handleUpdate = async () => {
    if (!editGroup || !editName.trim()) return
    await api.put(`/player-groups/${editGroup.id_group}`, { group_name: editName.trim(), group_type: editType })
    setEditGroup(null)
    refetch()
  }

  const handleDelete = async (id: number) => {
    if (!confirm(t('players.delete_group_confirm'))) return
    await api.delete(`/player-groups/${id}`)
    refetch()
  }

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <Can perm="players:edit">
          <button
            onClick={() => setCreateOpen(true)}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-accent text-white rounded-md hover:bg-accent/90 transition-sf"
          >
            <Plus className="w-4 h-4" />
            {t('players.new_group')}
          </button>
        </Can>
      </div>

      {createOpen && (
        <div className="bg-surface border border-gray-200 rounded-lg p-4 flex items-center gap-3 shadow-sf-card">
          <input
            type="text"
            value={newGroupName}
            onChange={(e) => setNewGroupName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
            placeholder={t('players.group_name_placeholder')}
            className="flex-1 border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40"
            autoFocus
          />
          <select
            value={newGroupType}
            onChange={(e) => setNewGroupType(e.target.value as 'content' | 'audio' | 'mixed')}
            className="border border-gray-300 rounded-md px-2 py-1.5 text-sm bg-surface focus:outline-none focus:ring-2 focus:ring-accent/40"
          >
            <option value="content">{t('players.group_type_content')}</option>
            <option value="audio">{t('players.group_type_audio')}</option>
            <option value="mixed">{t('players.group_type_mixed')}</option>
          </select>
          <button onClick={handleCreate} className="px-3 py-1.5 bg-accent text-white text-sm rounded-md hover:bg-accent/90">
            {t('common.create')}
          </button>
          <button onClick={() => setCreateOpen(false)} className="px-3 py-1.5 text-sm text-gray-600 hover:text-gray-900">
            {t('common.cancel')}
          </button>
        </div>
      )}

      {groups.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 gap-3 text-gray-400">
          <Monitor className="w-10 h-10" />
          <p className="text-sm">{t('players.no_groups')}</p>
        </div>
      ) : (
        <div className="space-y-3">
          {groups.map((g) => (
            <GroupCard
              key={g.id_group}
              group={g}
              allPlayers={players}
              t={t}
              onSelectPlayer={onSelectPlayer}
              onEdit={() => { setEditGroup(g); setEditName(g.group_name); setEditType(g.group_type) }}
              onDelete={() => handleDelete(g.id_group)}
            />
          ))}
        </div>
      )}

      {editGroup && (
        <Drawer open onClose={() => setEditGroup(null)} title={t('players.edit_group')}>
          <div className="space-y-4" data-write-perm="players:edit">
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">{t('players.group_name_placeholder')}</label>
              <input
                type="text"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleUpdate()}
                className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40"
                autoFocus
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">{t('players.group_type_label')}</label>
              <select
                value={editType}
                onChange={(e) => setEditType(e.target.value as 'content' | 'audio' | 'mixed')}
                className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm bg-surface focus:outline-none focus:ring-2 focus:ring-accent/40"
              >
                <option value="content">{t('players.group_type_content')}</option>
                <option value="audio">{t('players.group_type_audio')}</option>
                <option value="mixed">{t('players.group_type_mixed')}</option>
              </select>
            </div>
            <button data-write-action onClick={handleUpdate} className="w-full py-2 bg-accent text-white text-sm rounded-md hover:bg-accent/90">
              {t('common.save')}
            </button>
          </div>
        </Drawer>
      )}
    </div>
  )
}

function GroupCard({
  group, allPlayers, t, onSelectPlayer, onEdit, onDelete,
}: {
  group: PlayerGroupRead
  allPlayers: PlayerRead[]
  t: ReturnType<typeof useTranslations>
  onSelectPlayer: (p: PlayerRead) => void
  onEdit: () => void
  onDelete: () => void
}) {
  const { data: memberIds = [], refetch: refetchMembers } = useQuery<number[]>({
    queryKey: ['group-players', group.id_group],
    queryFn: () =>
      api.get<PlayerRead[]>(`/player-groups/${group.id_group}/players`)
        .then((r) => r.data.map((p) => p.id_player)),
  })

  const members = allPlayers.filter((p) => memberIds.includes(p.id_player))
  // Filtrage par rôle (§XIX) : un groupe ne propose que des players compatibles avec son type.
  //  - content → players content ou mixed ; audio → players audio ou mixed ; mixed → players mixed.
  const nonMembers = allPlayers.filter(
    (p) => !memberIds.includes(p.id_player) && roleFitsGroup(p.player_role, group.group_type),
  )

  const addPlayer = async (id: number) => {
    await api.post(`/player-groups/${group.id_group}/players`, { id_player: id })
    refetchMembers()
  }

  const removePlayer = async (id: number) => {
    await api.delete(`/player-groups/${group.id_group}/players/${id}`)
    refetchMembers()
  }

  return (
    <div className="bg-surface border border-gray-200 rounded-lg overflow-hidden shadow-sf-card">
      <div className="flex items-center justify-between px-4 py-3 bg-gray-50 border-b border-gray-200">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-gray-900">{group.group_name}</span>
          <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${
            group.group_type === 'audio' ? 'bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-300'
            : group.group_type === 'mixed' ? 'bg-teal-100 dark:bg-teal-500/20 text-teal-700 dark:text-teal-300'
            : 'bg-accent/10 dark:bg-blue-500/15 text-blue-700 dark:text-blue-300'
          }`}>
            {t(`players.group_type_${group.group_type}`)}
          </span>
          <span className="text-xs text-gray-400">{members.length} player{members.length !== 1 ? 's' : ''}</span>
        </div>
        <div className="flex items-center gap-1">
          <Can perm="players:edit">
            <button onClick={onEdit} className="p-1.5 text-gray-400 hover:text-gray-700 rounded transition-sf">
              <Pencil className="w-3.5 h-3.5" />
            </button>
          </Can>
          <Can perm="players:edit">
            <button onClick={onDelete} className="p-1.5 text-gray-400 hover:text-danger rounded transition-sf">
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </Can>
        </div>
      </div>

      {members.length > 0 && (
        <div className="divide-y divide-gray-50">
          {members.map((p) => (
            <div key={p.id_player} className="flex items-center justify-between px-4 py-2.5">
              <div
                className="flex items-center gap-3 flex-1 cursor-pointer hover:text-accent"
                onClick={() => onSelectPlayer(p)}
              >
                <StatusBadge status={p.status} label="" />
                <span className="text-sm text-gray-800">{p.player_name}</span>
                <OsChip os={p.player_os} />
              </div>
              <button
                onClick={() => removePlayer(p.id_player)}
                className="p-1.5 text-gray-300 hover:text-danger rounded transition-sf"
              >
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}

      {nonMembers.length > 0 && (
        <div className="px-4 py-2 border-t border-dashed border-gray-200">
          <p className="text-xs text-gray-400 mb-2">{t('players.add_to_group')}</p>
          <div className="flex flex-wrap gap-1.5">
            {nonMembers.map((p) => (
              <button
                key={p.id_player}
                onClick={() => addPlayer(p.id_player)}
                className="inline-flex items-center gap-1 px-2 py-1 text-xs bg-gray-100 text-gray-600 rounded hover:bg-accent hover:text-white transition-sf"
              >
                <Plus className="w-3 h-3" />
                {p.player_name}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ── LAN Sync tab (#1a même-LAN + #4 regroupement par lan_sync_group) ────────────

/** Sous-réseau /24 d'une IPv4 (3 premiers octets), null si non-IPv4. */
function subnet24(ip: string | null): string | null {
  if (!ip) return null
  const m = ip.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.\d{1,3}$/)
  return m ? `${m[1]}.${m[2]}.${m[3]}` : null
}

/** Player pouvant être RELAIS NTP (chrony) : linux/rpi uniquement (Windows ne sert pas bien le NTP). */
function isChrony(os: string | null): boolean {
  return /^(linux|rpi|debian)/i.test(os ?? '')
}

/** Player Windows : pilotable comme CLIENT NTP (w32time tuné via signflow-ntp-apply.ps1). */
function isWindows(os: string | null): boolean {
  return /^win/i.test(os ?? '')
}

/** Players dont on pilote l'horloge depuis l'UI (relais chrony + clients chrony/Windows). */
function isNtpManaged(os: string | null): boolean {
  return isChrony(os) || isWindows(os)
}

/** Player ChromeOS PWA : sandbox navigateur → on masque les contrôles inopérants du drawer
 *  (arrêt d'app, reboot OS, CPU/disque système, routage de sortie audio = setSinkId non porté). */
function isChromeOs(os: string | null): boolean {
  return /chromeos|chrome_?ext/i.test(os ?? '')
}

function LanSyncTab({
  players, t, onSelectPlayer,
}: {
  players: PlayerRead[]
  t: ReturnType<typeof useTranslations>
  onSelectPlayer: (p: PlayerRead) => void
}) {
  const qc = useQueryClient()

  // Désigne un relais NTP (ou désactive si relayId=null) sur les membres pilotables d'un groupe :
  // relais (chrony) → ntp_role=server ; les autres (chrony + Windows) → ntp_role=client + ip relais.
  const ntpMutation = useMutation({
    mutationFn: async ({ members, relayId }: { members: PlayerRead[]; relayId: number | null }) => {
      const relay = relayId ? members.find((m) => m.id_player === relayId) ?? null : null
      await Promise.all(members.map((m) => {
        const dc: Partial<DisplayConfig> = { ...(m.display_config ?? {}) }
        if (relay && m.id_player === relay.id_player) {
          dc.ntp_role = 'server'; dc.ntp_server = null
        } else if (relay) {
          dc.ntp_role = 'client'; dc.ntp_server = relay.ip_address ?? null
        } else {
          dc.ntp_role = null; dc.ntp_server = null
        }
        return api.put(`/players/${m.id_player}`, { display_config: dc })
      }))
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['players'] }),
  })

  // Video Wall : écrit display_config.wall sur chaque membre (PUT → force_sync serveur).
  const wallMutation = useMutation({
    mutationFn: async ({ members, cfg }: { members: PlayerRead[]; cfg: WallSave }) => {
      // Mode A (crop) : le serveur calcule le sous-rectangle de chaque player à partir de la
      // grille COMPLÈTE → on embarque toutes les cellules (dalle/bezels homogènes) dans chaque
      // bloc wall. Inutile en mode B, mais inoffensif (quelques entrées) et garde les blocs cohérents.
      const cells = members.map((mm) => {
        const cc = cfg.assignments[mm.id_player] ?? { row: 0, col: 0 }
        return {
          row: cc.row, col: cc.col,
          panel_w_mm: cfg.panel_w_mm, panel_h_mm: cfg.panel_h_mm,
          bezel_left_mm: cfg.bezel_left_mm, bezel_right_mm: cfg.bezel_right_mm,
          bezel_top_mm: cfg.bezel_top_mm, bezel_bottom_mm: cfg.bezel_bottom_mm,
        }
      })
      await Promise.all(members.map((m) => {
        const dc: Partial<DisplayConfig> = { ...(m.display_config ?? {}) }
        const cell = cfg.assignments[m.id_player] ?? { row: 0, col: 0 }
        dc.wall = {
          mode: cfg.mode, rows: cfg.rows, cols: cfg.cols, cell, cells,
          panel_w_mm: cfg.panel_w_mm, panel_h_mm: cfg.panel_h_mm,
          bezel_left_mm: cfg.bezel_left_mm, bezel_right_mm: cfg.bezel_right_mm,
          bezel_top_mm: cfg.bezel_top_mm, bezel_bottom_mm: cfg.bezel_bottom_mm,
          gap_scale: cfg.gap_scale,
        }
        return api.put(`/players/${m.id_player}`, { display_config: dc })
      }))
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['players'] }),
  })

  const wallDisableMutation = useMutation({
    mutationFn: async (members: PlayerRead[]) => {
      await Promise.all(members.map((m) => {
        const dc: Partial<DisplayConfig> = { ...(m.display_config ?? {}) }
        delete dc.wall
        return api.put(`/players/${m.id_player}`, { display_config: dc })
      }))
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['players'] }),
  })

  const lanPlayers = players.filter((p) => p.display_config?.lan_sync_enabled)

  if (lanPlayers.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 gap-3 text-gray-400">
        <Wifi className="w-10 h-10" />
        <p className="text-sm text-center max-w-sm">{t('players.lansync_empty')}</p>
      </div>
    )
  }

  // Regroupement par nom de groupe LAN (vide/absent = groupe par défaut).
  const groups = new Map<string, PlayerRead[]>()
  for (const p of lanPlayers) {
    const key = (p.display_config?.lan_sync_group ?? '').trim()
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key)!.push(p)
  }
  const groupKeys = Array.from(groups.keys()).sort()

  return (
    <div className="space-y-4">
      <p className="text-xs text-gray-400">{t('players.lansync_hint')}</p>
      {groupKeys.map((key) => {
        const members = groups.get(key)!
        const masters = members.filter((p) => p.display_config?.lan_sync_master)
        const chronyMembers = members.filter((p) => isChrony(p.player_os))
        // Clients pilotables = chrony + Windows (le relais reste chrony) ; sync au relais.
        const ntpManaged = members.filter((p) => isNtpManaged(p.player_os))
        const ntpRelay = chronyMembers.find((p) => p.display_config?.ntp_role === 'server') ?? null
        // Diagnostic même-LAN : sous-réseaux /24 + WAN IP distincts parmi les membres.
        const subnets = new Set(members.map((p) => subnet24(p.ip_address)).filter(Boolean))
        const wans = new Set(members.map((p) => p.wan_ip).filter(Boolean))
        const subnetDivergent = members.length > 1 && (subnets.size > 1 || wans.size > 1)
        const offline = members.some((p) => p.status === 'offline')

        return (
          <div key={key || '__default__'} className="bg-surface border border-gray-200 rounded-lg overflow-hidden shadow-sf-card">
            <div className="flex items-center justify-between px-4 py-3 bg-gray-50 border-b border-gray-200">
              <div className="flex items-center gap-2">
                <Wifi className="w-4 h-4 text-accent" />
                <span className="text-sm font-medium text-gray-900">
                  {key || <span className="italic text-gray-500">{t('players.lansync_group_default')}</span>}
                </span>
                <span className="text-xs text-gray-400">{t('players.lansync_members', { count: members.length })}</span>
              </div>
            </div>

            {/* Diagnostics */}
            {(masters.length !== 1 || subnetDivergent || offline || members.some((p) => p.lan_sync_eligibility?.level === 'warn')) && (
              <div className="px-4 py-2 space-y-1.5 border-b border-gray-100">
                {members.some((p) => p.lan_sync_eligibility?.level === 'warn') && (
                  <p className="flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-300">
                    <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />{t('players.lansync_heterogeneous_warn')}
                  </p>
                )}
                {masters.length === 0 && (
                  <p className="flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-300">
                    <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />{t('players.lansync_no_master')}
                  </p>
                )}
                {masters.length > 1 && (
                  <p className="flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-300">
                    <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />{t('players.lansync_multi_master')}
                  </p>
                )}
                {subnetDivergent && (
                  <p className="flex items-start gap-1.5 text-xs text-danger">
                    <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />{t('players.lansync_subnet_warn')}
                  </p>
                )}
                {offline && (
                  <p className="flex items-start gap-1.5 text-xs text-gray-500">
                    <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />{t('players.lansync_offline_warn')}
                  </p>
                )}
              </div>
            )}

            {/* Relais NTP (chrony, linux/rpi) — pilote la source d'horloge commune du groupe */}
            {chronyMembers.length > 0 && (
              <div className="flex items-center justify-between gap-2 px-4 py-2 border-b border-gray-100 bg-accent/5">
                <div className="flex items-center gap-1.5 text-xs text-gray-600">
                  <Clock className="w-3.5 h-3.5 text-accent shrink-0" />
                  {ntpRelay
                    ? <span>{t('players.ntp_relay')}: <span className="font-medium text-gray-800">{ntpRelay.player_name}</span></span>
                    : <span className="text-gray-400">{t('players.ntp_none')}</span>}
                </div>
                {ntpRelay && (
                  <Can perm="players:edit">
                    <button
                      onClick={() => ntpMutation.mutate({ members: ntpManaged, relayId: null })}
                      disabled={ntpMutation.isPending}
                      className="text-xs text-gray-400 hover:text-danger disabled:opacity-50"
                    >
                      {t('players.ntp_disable')}
                    </button>
                  </Can>
                )}
              </div>
            )}

            <div className="divide-y divide-gray-50">
              {members.map((p) => {
                const ntpR = p.display_config?.ntp_role
                const canRelay = isChrony(p.player_os)
                return (
                <div
                  key={p.id_player}
                  className="flex items-center justify-between px-4 py-2.5 cursor-pointer hover:bg-gray-50 transition-sf"
                  onClick={() => onSelectPlayer(p)}
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <StatusBadge status={p.status} label="" />
                    <span className="text-sm text-gray-800 truncate">{p.player_name}</span>
                    {p.display_config?.lan_sync_master && (
                      <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-medium bg-accent/10 text-accent">
                        <Crown className="w-3 h-3" />{t('players.lansync_master_badge')}
                      </span>
                    )}
                    {ntpR === 'server' && (
                      <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-medium bg-emerald-500/10 dark:bg-emerald-500/15 text-emerald-700 dark:text-emerald-300">
                        <Clock className="w-3 h-3" />{t('players.ntp_relay_badge')}
                      </span>
                    )}
                    {ntpR === 'client' && (
                      <span className="text-[10px] text-gray-400">{t('players.ntp_client_badge')}</span>
                    )}
                    <OsChip os={p.player_os} />
                  </div>
                  <div className="flex items-center gap-3 shrink-0 text-xs text-gray-400 font-mono">
                    {canRelay && ntpR !== 'server' && (
                      <Can perm="players:edit">
                        <button
                          onClick={(e) => { e.stopPropagation(); ntpMutation.mutate({ members: ntpManaged, relayId: p.id_player }) }}
                          disabled={ntpMutation.isPending}
                          className="font-sans text-accent hover:underline disabled:opacity-50"
                        >
                          {t('players.ntp_set_relay')}
                        </button>
                      </Can>
                    )}
                    <span>{p.ip_address ?? '—'}</span>
                    {p.wan_ip && <span className="text-gray-300">· {p.wan_ip}</span>}
                  </div>
                </div>
                )
              })}
            </div>
            {members.length >= 2 && (
              <Can perm="players:edit">
                <WallEditor
                  members={members}
                  onSave={(cfg) => wallMutation.mutate({ members, cfg })}
                  onDisable={() => wallDisableMutation.mutate(members)}
                  isSaving={wallMutation.isPending || wallDisableMutation.isPending}
                  t={t}
                />
              </Can>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ── Command button — icon-only + color + tooltip ──────────────────────────────

function CmdButton({
  icon, label, colorClass, onClick, disabled,
}: {
  icon: React.ReactNode
  label: string
  colorClass: string
  onClick: () => void
  disabled?: boolean
}) {
  return (
    <div className="relative group">
      <button
        onClick={onClick}
        disabled={disabled}
        className={`p-2 rounded-lg text-white transition-sf disabled:opacity-40 ${colorClass}`}
      >
        {icon}
      </button>
      <div className="pointer-events-none absolute bottom-full left-1/2 mb-2 -translate-x-1/2 whitespace-nowrap rounded bg-canvas-dark px-2 py-1 text-xs font-medium text-white opacity-0 transition-opacity group-hover:opacity-100 z-20">
        {label}
        <span className="absolute left-1/2 top-full -translate-x-1/2 border-4 border-transparent border-t-gray-900" />
      </div>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

/** Chips d'alerte colorées pour le drawer player */
const ALERT_CHIP_CONFIG: Record<string, { label: string; color: string }> = {
  offline_critical: { label: 'Offline critique',     color: 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-300' },
  offline_scheduled: { label: 'Veille programmée',   color: 'bg-slate-100 dark:bg-slate-500/20 text-slate-600 dark:text-slate-300' },
  cpu_high:         { label: 'CPU élevé',            color: 'bg-orange-100 dark:bg-orange-500/20 text-orange-700 dark:text-orange-300' },
  ram_high:         { label: 'RAM élevée',           color: 'bg-orange-100 dark:bg-orange-500/20 text-orange-700 dark:text-orange-300' },
  disk_high:        { label: 'Disque élevé',         color: 'bg-orange-100 dark:bg-orange-500/20 text-orange-700 dark:text-orange-300' },
  token_3d:         { label: 'Token expire < 3j',    color: 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-300' },
  token_7d:         { label: 'Token expire < 7j',    color: 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-300' },
  version_outdated: { label: 'Version obsolète',     color: 'bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-300' },
}

export default function PlayersPage() {
  const t = useTranslations()
  const { user } = useAuthStore()
  const qc = useQueryClient()
  const [selected, setSelected] = useState<PlayerRead | null>(null)
  const [editPlayerName, setEditPlayerName] = useState('')
  const [renameOpen, setRenameOpen] = useState(false)   // champ de renommage révélé à la demande (gain de place)
  const [activeTab, setActiveTab] = useState('all')
  const [search, setSearch] = useState('')
  // Vue cartes ↔ liste (demande d'Ed, 31/07 — même patron que la médiathèque). Le choix
  // est PERSISTÉ : c'est une préférence de travail, pas un état d'écran.
  const [gridView, toggleView] = useCollapse('sf-players-view', true)
  const viewMode: 'grid' | 'list' = gridView ? 'grid' : 'list'
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({ key: 'name', dir: 'asc' })
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [filters, setFilters] = useState<PlayerFilterState>(EMPTY_PLAYER_FILTERS)
  useSearchPrefill(setSearch)
  // Recherche globale -> `/players?open=<id>` : on ouvre la FICHE plutot qu'une liste filtree
  // a un element (retour Ed 2026-07-19). Declare ici, applique plus bas quand `players` est
  // charge : au montage la liste est vide, l'ouverture doit donc attendre les donnees.
  const [pendingOpen, setPendingOpen] = useState<number | null>(null)
  useOpenShortcut(setPendingOpen)
  const [screenshotUrl, setScreenshotUrl] = useState<string | null>(null)
  const [screenshotLoading, setScreenshotLoading] = useState(false)
  const [commandNotice, setCommandNotice] = useState<string | null>(null)

  // ── Alert settings drawer ──────────────────────────────────────────────────
  const [showAlertSettings, setShowAlertSettings] = useState(false)
  // Commandes en masse (migr 0097) — drawer dédié, cf. components/players/BulkCommandDrawer
  const [bulkOpen, setBulkOpen] = useState(false)
  const [alertForm, setAlertForm] = useState<OrgPlayerAlertSettings>({
    offline_critical_min: 10, cpu_warn_pct: 90, ram_warn_pct: 90,
    disk_warn_pct: 85, token_warn_days: 7, min_player_version: null,
  })

  const { data: alertSettings } = useQuery<OrgPlayerAlertSettings>({
    queryKey: ['player-alert-settings'],
    queryFn: () => api.get<OrgPlayerAlertSettings>(`/orgs/${user!.id_org}/player-alert-settings`).then((r) => r.data),
    enabled: !!user,
  })

  // Sites de l'org — pour le fuseau horaire du site du player sélectionné (section
  // Appareil : détection de mismatch device vs site, le pilotage offline suit l'horloge device).
  const { data: sites = [] } = useQuery<Site[]>({
    queryKey: ['sites'],
    queryFn: () => api.get<Site[]>('/sites').then((r) => r.data),
  })

  const alertSettingsMutation = useMutation({
    mutationFn: (body: OrgPlayerAlertSettings) =>
      api.put<OrgPlayerAlertSettings>(`/orgs/${user!.id_org}/player-alert-settings`, body).then((r) => r.data),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['player-alert-settings'] }); setShowAlertSettings(false) },
  })

  // ── Display config edit state ──────────────────────────────────────────────
  const [displayConfigOpen, setDisplayConfigOpen] = useState(false)
  const [displayConfigForm, setDisplayConfigForm] = useState<Partial<DisplayConfig>>({})

  function showNotice(msg: string) {
    setCommandNotice(msg)
    setTimeout(() => setCommandNotice(null), 3000)
  }

  const commandMutation = useMutation({
    mutationFn: ({ id, command, params }: { id: number; command: string; params?: Record<string, unknown> }) =>
      api.post(`/players/${id}/command`, { command, params }).then((r) => r.data),
    onSuccess: (_, { command, id }) => {
      showNotice(t('players.command_sent'))
      if (command === 'screenshot') {
        setScreenshotLoading(true)
        setScreenshotUrl(null)
        // L'upload est asynchrone (commande Redis → capture → upload). On poll le
        // player jusqu'à ce que last_screenshot_at devienne récent (≥ début du poll),
        // puis on affiche l'URL signée fraîche et on rafraîchit la liste (vignette carte).
        const startedAt = Date.now()
        let attempts = 0
        const poll = async () => {
          attempts++
          try {
            const p = await api.get<PlayerRead>(`/players/${id}`).then((r) => r.data)
            const ts = p.last_screenshot_at ? new Date(p.last_screenshot_at).getTime() : 0
            if (ts >= startedAt - 5000 && p.screenshot_url) {
              setScreenshotUrl(p.screenshot_url)
              setScreenshotLoading(false)
              refetchPlayers()  // met à jour la vignette de la carte
              return
            }
          } catch { /* retry */ }
          if (attempts >= 12) { setScreenshotLoading(false); return }  // ~24s max
          setTimeout(poll, 2000)
        }
        setTimeout(poll, 2000)
      }
    },
  })

  /** Envoie une commande ecran (RS232/IP) via le nouvel endpoint display/command. */
  const displayCmdMutation = useMutation({
    mutationFn: ({
      id, command, params,
    }: { id: number; command: string; params?: Record<string, unknown> }) =>
      api.post(`/players/${id}/display/command`, { command, params }).then((r) => r.data),
    onSuccess: (_, { command }) => showNotice(t('players.display_command_sent', { cmd: command })),
  })

  /** Mode maintenance manuel : pose/retire le statut « collant » `maintenance` (alertes
   *  suspendues, distinct dans les dashboards). Le heartbeat ne réveille pas un player en
   *  maintenance (garde-fou serveur) ; la sortie ré-évalue le statut au heartbeat suivant. */
  const maintenanceMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      api.post<PlayerRead>(`/players/${id}/maintenance`, { enabled }).then((r) => r.data),
    onSuccess: (updated: PlayerRead) => {
      setSelected(updated)
      refetchPlayers()
      showNotice(t(updated.status === 'maintenance' ? 'players.maintenance_on_done' : 'players.maintenance_off_done'))
    },
  })

  /** Change le rôle audio/contenu du player (§XIX) via PUT /players/{id}. */
  const roleMutation = useMutation({
    mutationFn: ({ id, role }: { id: number; role: PlayerRead['player_role'] }) =>
      api.put<PlayerRead>(`/players/${id}`, { player_role: role }).then((r) => r.data),
    onSuccess: (updated: PlayerRead) => {
      setSelected(updated)
      refetchPlayers()
    },
  })

  /** Exclut/ré-inclut le player des campagnes Retail Media via PUT /players/{id}. */
  const optoutMutation = useMutation({
    mutationFn: ({ id, optout }: { id: number; optout: boolean }) =>
      api.put<PlayerRead>(`/players/${id}`, { campaigns_optout: optout }).then((r) => r.data),
    onSuccess: (updated: PlayerRead) => {
      setSelected(updated)
      refetchPlayers()
    },
  })

  /** Inventaire v2 : plafond d'occupation / tarif SOV du player (null = héritage). */
  const campaignInvMutation = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Record<string, number | null> }) =>
      api.put<PlayerRead>(`/players/${id}`, body).then((r) => r.data),
    onSuccess: (updated: PlayerRead) => {
      setSelected(updated)
      refetchPlayers()
    },
  })

  /** Renomme le player via PUT /players/{id}. */
  const nameMutation = useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) =>
      api.put<PlayerRead>(`/players/${id}`, { player_name: name }).then((r) => r.data),
    onSuccess: (updated: PlayerRead) => {
      setSelected(updated)
      setEditPlayerName(updated.player_name)
      setRenameOpen(false)   // champ masqué après renommage réussi
      showNotice(t('players.name_saved'))
      refetchPlayers()
    },
  })

  /** Sauvegarde la configuration ecran via PUT /players/{id}. */
  const displayConfigMutation = useMutation({
    mutationFn: ({ id, cfg, forceChromium }: { id: number; cfg: DisplayConfig | null; forceChromium?: boolean }) =>
      api
        .put(`/players/${id}`, { display_config: cfg, ...(forceChromium ? { force_chromium: true } : {}) })
        .then((r) => r.data),
    onSuccess: (updated: PlayerRead) => {
      setSelected(updated)
      setDisplayConfigOpen(false)
      showNotice(t('players.display_config_saved'))
      refetchPlayers()
    },
    onError: (err: unknown, vars: { id: number; cfg: DisplayConfig | null; forceChromium?: boolean }) => {
      // Règle d'homogénéité : un groupe LAN avec décodage HW (mpv/exo) doit être homogène.
      // Le backend renvoie 409 LAN_SYNC_HETEROGENEOUS → on propose de repasser ce player en
      // Chromium (force_chromium) pour réduire l'hétérogénéité, sinon on signale l'erreur.
      const code = (err as { response?: { data?: { detail?: { code?: string } } } })
        ?.response?.data?.detail?.code
      if (code === 'LAN_SYNC_HETEROGENEOUS') {
        if (!vars.forceChromium && window.confirm(t('players.lan_heterogeneous_force_confirm'))) {
          displayConfigMutation.mutate({ ...vars, forceChromium: true })
        } else {
          showNotice(t('players.lan_sync_heterogeneous_error'))
        }
      } else {
        showNotice(t('players.display_config_save_error'))
      }
    },
  })

  function handleSelectPlayer(p: PlayerRead) {
    setSelected(p)
    setEditPlayerName(p.player_name)
    setRenameOpen(false)
    setScreenshotUrl(null)
    setScreenshotLoading(false)
    setCommandNotice(null)
    setDisplayConfigOpen(false)
    setDisplayConfigForm(p.display_config ?? {})
  }

  const { data: players = [], isLoading, refetch: refetchPlayers } = useQuery<PlayerRead[]>({
    queryKey: ['players'],
    queryFn: () => api.get<PlayerRead[]>('/players/').then((r) => r.data),
    refetchInterval: 10000,
  })

  // Ouverture differee : le hook peut arriver avant le chargement de la liste.
  useEffect(() => {
    if (pendingOpen === null || players.length === 0) return
    const target = players.find((p) => p.id_player === pendingOpen)
    if (target) handleSelectPlayer(target)
    setPendingOpen(null)   // trouve ou non, on ne re-essaie pas en boucle
  }, [pendingOpen, players])   // eslint-disable-line react-hooks/exhaustive-deps

  const { data: activeEvents = [] } = useQuery<EventRead[]>({
    queryKey: ['events-active'],
    queryFn: () => api.get<EventRead[]>('/events/active').then((r) => r.data),
    refetchInterval: 15000,
  })

  const allPlayerIds = players.map((p) => p.id_player)
  const overriddenPlayerIds = computeOverriddenIds(activeEvents, allPlayerIds)

  const { data: currentState, refetch: refetchState } = useQuery<PlayerCurrentState>({
    queryKey: ['player-current-state', selected?.id_player],
    queryFn: () => api.get<PlayerCurrentState>(`/players/${selected!.id_player}/current-state`).then((r) => r.data),
    enabled: selected !== null,
    refetchInterval: 15000,
  })

  // Suppression d'un player → modale avec option « supprimer aussi les preuves PoP ».
  const [deleteId, setDeleteId] = useState<number | null>(null)
  const [deletePop, setDeletePop] = useState(false)
  const handleDeletePlayer = (id: number) => { setDeletePop(false); setDeleteId(id) }
  const deletePlayerMut = useMutation({
    mutationFn: ({ id, pop }: { id: number; pop: boolean }) =>
      api.delete(`/players/${id}`, { params: pop ? { delete_pop: true } : {} }),
    onSuccess: (_r, { id }) => {
      refetchPlayers()
      if (selected?.id_player === id) setSelected(null)
      setDeleteId(null)
    },
  })
  const deleteName = players.find((p) => p.id_player === deleteId)?.player_name ?? ''

  const online  = players.filter((p) => p.status === 'online').length
  const offline = players.filter((p) => p.status === 'offline').length
  const warning = players.filter((p) => ['warning', 'critical'].includes(p.status)).length
  const alertPlayers = players.filter((p) => ['warning', 'critical', 'offline'].includes(p.status))

  const q = search.toLowerCase()
  const filteredPlayers = q
    ? players.filter((p) =>
        p.player_name.toLowerCase().includes(q) ||
        (p.player_os ?? '').toLowerCase().includes(q) ||
        (p.ip_address ?? '').toLowerCase().includes(q) ||
        (p.wan_ip ?? '').toLowerCase().includes(q) ||
        (p.mac_address ?? '').toLowerCase().includes(q)
      )
    : players
  const filteredAlerts = q
    ? alertPlayers.filter((p) =>
        p.player_name.toLowerCase().includes(q) ||
        (p.player_os ?? '').toLowerCase().includes(q) ||
        (p.ip_address ?? '').toLowerCase().includes(q) ||
        (p.wan_ip ?? '').toLowerCase().includes(q) ||
        (p.mac_address ?? '').toLowerCase().includes(q)
      )
    : alertPlayers

  // Panneau de filtres (demande d'Ed) : il s'APPLIQUE PAR-DESSUS la recherche, dans la vue
  // courante. Le filtrage est exhaustif — `GET /players/` rend tout le parc, rien n'est caché.
  const siteNameById = new Map(sites.map((s) => [s.id_site, s.site_name]))
  const shownPlayers = sortPlayers(
    applyPlayerFilters(activeTab === 'alerts' ? filteredAlerts : filteredPlayers, filters),
    sort, siteNameById)
  const activeFilterCount = countActivePlayerFilters(filters)
  const onSort = (key: SortKey) => setSort((s0) =>
    s0.key === key ? { key, dir: s0.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'asc' })

  const lanSyncCount = players.filter((p) => p.display_config?.lan_sync_enabled).length
  const tabs = [
    { key: 'all',     label: t('players.tab_all'),     count: players.length },
    { key: 'groups',  label: t('players.tab_groups') },
    { key: 'lansync', label: t('players.tab_lansync'),  count: lanSyncCount },
    { key: 'alerts',  label: t('players.tab_alerts'),  count: alertPlayers.length },
  ]

  // Pré-remplir alertForm quand les settings sont chargés
  React.useEffect(() => {
    if (alertSettings) setAlertForm(alertSettings)
  }, [alertSettings])

  return (
    <>
      <Topbar title={t('players.title')}>
        {/* Les deux sont gatees org-admin cote serveur. Le bouton « seuils
            d'alerte » etait le cas signale par Ed le 2026-07-20 : un Approbateur
            pouvait remplir le formulaire, l'API refusait en 403 sans le dire. */}
        <Can admin>
          <Button size="sm" onClick={() => setBulkOpen(true)}>
            <Zap className="w-3.5 h-3.5 mr-1.5" />
            {t('players.bulk_title')}
          </Button>
          <Button size="sm" onClick={() => setShowAlertSettings(true)}>
            <Bell className="w-3.5 h-3.5 mr-1.5" />
            {t('players.alert_settings')}
          </Button>
        </Can>
      </Topbar>

      <BulkCommandDrawer
        open={bulkOpen}
        onClose={() => setBulkOpen(false)}
        players={players}
        onSent={(count, command) => {
          showNotice(t('players.bulk_sent', { count, command }))
          refetchPlayers()
        }}
      />

      {/* Page VERROUILLÉE AU VIEWPORT (règle du design system, appliquée ici le 31/07 —
          cette page défilait encore en entier). `DashboardShell` fournit déjà
          `h-screen overflow-hidden` : il suffit d'une colonne flex dont EXACTEMENT UN
          enfant porte le défilement. Gain concret : les compteurs et les onglets restent
          visibles pendant qu'on parcourt un parc de plusieurs centaines d'écrans, et
          l'en-tête `sticky` de la vue liste a enfin un conteneur auquel se coller. */}
      <main className="flex-1 flex flex-col min-h-0">
        <div className="flex-1 min-h-0 flex flex-col px-6 py-8 gap-6">

          {/* KPI row — zone fixe */}
          <div className="shrink-0 grid grid-cols-3 gap-4">
            {[
              { label: t('players.kpi_online'),  value: online,  color: 'text-success' },
              { label: t('players.kpi_offline'), value: offline, color: 'text-danger' },
              { label: t('players.kpi_warning'), value: warning, color: 'text-warning' },
            ].map(({ label, value, color }) => (
              <div key={label} className="bg-surface rounded-lg border border-gray-200 px-5 py-4 shadow-sf-card">
                <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">{label}</p>
                <p className={`text-3xl font-semibold mt-1 ${color}`}>{value}</p>
              </div>
            ))}
          </div>

          {/* TabBar + contenu : le panneau prend la hauteur restante, seul l'intérieur défile */}
          <div className="flex-1 min-h-0 flex flex-col bg-surface rounded-lg border border-gray-200 overflow-hidden shadow-sf-card">
            <TabBar tabs={tabs} active={activeTab} onChange={(k) => { setActiveTab(k); setSearch('') }} className="shrink-0 px-2 pt-2" />

            <div className="flex-1 min-h-0 flex flex-col p-4">
              {activeTab !== 'groups' && activeTab !== 'lansync' && (
                <div className="shrink-0 flex items-center gap-2 mb-4">
                  <div className="relative flex-1">
                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none" />
                    <input
                      type="search"
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                      placeholder={t('common.search')}
                      className="w-full pl-9 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-accent/40 bg-surface"
                    />
                  </div>
                  {/* Bascule cartes ↔ liste (demande d'Ed) : les cartes montrent CE QUI EST
                      À L'ANTENNE (capture d'écran), la liste sert à comparer et à trier. */}
                  <div className="inline-flex rounded-lg border border-gray-200 overflow-hidden shrink-0">
                    <button
                      data-testid="players-view-grid"
                      onClick={() => { if (viewMode !== 'grid') toggleView() }}
                      title={t('players.view_grid')}
                      aria-pressed={viewMode === 'grid'}
                      className={`px-2.5 py-2 transition-sf ${
                        viewMode === 'grid' ? 'bg-accent text-white' : 'text-gray-500 hover:text-gray-800'
                      }`}
                    >
                      <LayoutGrid className="w-3.5 h-3.5" />
                    </button>
                    <button
                      data-testid="players-view-list"
                      onClick={() => { if (viewMode !== 'list') toggleView() }}
                      title={t('players.view_list')}
                      aria-pressed={viewMode === 'list'}
                      className={`px-2.5 py-2 transition-sf ${
                        viewMode === 'list' ? 'bg-accent text-white' : 'text-gray-500 hover:text-gray-800'
                      }`}
                    >
                      <List className="w-3.5 h-3.5" />
                    </button>
                  </div>
                  <button
                    data-testid="players-filters-toggle"
                    onClick={() => setFiltersOpen((o) => !o)}
                    title={t('media.filters')}
                    className={`inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border text-xs font-medium transition-sf shrink-0 ${
                      activeFilterCount > 0
                        ? 'bg-accent/10 text-accent border-accent/40'
                        : 'bg-surface text-gray-600 border-gray-200 hover:border-accent'
                    }`}
                  >
                    <SlidersHorizontal className="w-3.5 h-3.5" />
                    {activeFilterCount > 0 && <span className="tabular-nums">{activeFilterCount}</span>}
                  </button>
                </div>
              )}
              {(activeTab === 'all' || activeTab === 'alerts') && (
                <div className="flex-1 min-h-0 flex items-stretch gap-4">
                  {/* Un SEUL conteneur de défilement dans cette colonne (règle du design
                      system) : l'en-tête `sticky` de la vue liste s'y colle. */}
                  <div className="flex-1 min-w-0 overflow-y-auto">
                    {viewMode === 'list' ? (
                      <PlayerListView
                        players={shownPlayers}
                        sites={sites}
                        sort={sort}
                        onSort={onSort}
                        onSelect={handleSelectPlayer}
                        onDelete={handleDeletePlayer}
                        emptyLabel={t(activeTab === 'alerts' ? 'players.no_alerts' : 'players.no_players')}
                      />
                    ) : (
                      <PlayerTable
                        players={shownPlayers}
                        isLoading={isLoading}
                        t={t}
                        onSelect={handleSelectPlayer}
                        onDelete={handleDeletePlayer}
                        overriddenPlayerIds={overriddenPlayerIds}
                        emptyKey={activeTab === 'alerts' ? 'no_alerts' : 'no_players'}
                      />
                    )}
                  </div>
                  <PlayerFilters
                    open={filtersOpen}
                    onClose={() => setFiltersOpen(false)}
                    value={filters}
                    onChange={setFilters}
                    players={players}
                    sites={sites}
                  />
                </div>
              )}
              {activeTab === 'groups' && (
                <div className="flex-1 min-h-0 overflow-y-auto">
                  <GroupsTab players={players} t={t} onSelectPlayer={handleSelectPlayer} />
                </div>
              )}
              {activeTab === 'lansync' && (
                <div className="flex-1 min-h-0 overflow-y-auto">
                  <LanSyncTab players={players} t={t} onSelectPlayer={handleSelectPlayer} />
                </div>
              )}
            </div>
          </div>
        </div>
      </main>

      {/* Player detail drawer */}
      <Drawer
        open={selected !== null}
        onClose={() => {
          setSelected(null)
          setScreenshotUrl(null)
          setCommandNotice(null)
        }}
        title={selected?.player_name ?? ''}
        width="w-[440px]"
      >
        {selected && (
          <div className="flex flex-col gap-6" data-write-perm="players:edit">
            {/* En-tête : statut + version + OS + bouton Renommer (le nom est déjà le titre du drawer) */}
            {/* Une seule ligne (demande Ed 14/11) : « v1.6.22 linux-arm64 (Electron 42.11.18) » puis
                Renommer à droite ; si la place manque, c'est la ligne d'infos qui se tronque. */}
            <div className="flex items-center gap-x-3">
              <StatusBadge
                status={selected.status}
                label={playerStatusLabel(selected.status, t)}
              />
              <span className="min-w-0 truncate whitespace-nowrap text-[11px] text-gray-400" data-testid="player-version-line">
                {selected.player_version && <>v{selected.player_version} </>}
                {selected.player_os}
                {/* Moteur Electron (players Electron seulement) : depuis vitrail#5 il se met à jour à
                    part du code, la version du player ne dit plus rien du moteur. */}
                {selected.capabilities?.electron_version && (
                  <span data-testid="player-electron-version"> (Electron {selected.capabilities.electron_version})</span>
                )}
              </span>
              <button
                onClick={() => { setEditPlayerName(selected.player_name); setRenameOpen((v) => !v) }}
                className="ml-auto inline-flex shrink-0 items-center gap-1 text-xs text-accent hover:underline"
              >
                <Pencil className="w-3.5 h-3.5" />
                {t('common.rename')}
              </button>
            </div>

            {/* Champ de renommage — révélé à la demande (masqué par défaut = gain de place) */}
            {renameOpen && (
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={editPlayerName}
                  onChange={(e) => setEditPlayerName(e.target.value)}
                  autoFocus
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && editPlayerName.trim() && editPlayerName.trim() !== selected.player_name)
                      nameMutation.mutate({ id: selected.id_player, name: editPlayerName.trim() })
                    if (e.key === 'Escape') setRenameOpen(false)
                  }}
                  className="flex-1 px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-accent/40 bg-surface"
                  placeholder={selected.player_name}
                />
                <button
                  onClick={() => nameMutation.mutate({ id: selected.id_player, name: editPlayerName.trim() })}
                  disabled={
                    nameMutation.isPending ||
                    !editPlayerName.trim() ||
                    editPlayerName.trim() === selected.player_name
                  }
                  className="px-3 py-1.5 text-sm rounded bg-accent text-white hover:bg-accent/80 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  {t('common.save')}
                </button>
                <button
                  onClick={() => setRenameOpen(false)}
                  className="px-3 py-1.5 text-sm rounded border border-gray-200 text-gray-600 hover:bg-gray-50"
                >
                  {t('common.cancel')}
                </button>
              </div>
            )}

            {/* Commands player — pilotage + réinitialisation isolée à droite */}
            <div>
              <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">
                {t('players.section_player_controls')}
              </p>
              <div className="flex items-center justify-between gap-2" data-write-action>
                <div className="flex items-center gap-2">
                  {/* Stop app : pas d'arrêt d'app en PWA (le navigateur ne se ferme pas tout seul) → masqué ChromeOS */}
                  {!isChromeOs(selected.player_os) && (
                    <CmdButton
                      icon={<Square className="w-4 h-4" />}
                      label={t('players.quit_app')}
                      colorClass="bg-gray-500 hover:bg-gray-600"
                      onClick={() => { if (confirm(t('players.quit_app_confirm'))) commandMutation.mutate({ id: selected.id_player, command: 'quit_app' }) }}
                      disabled={commandMutation.isPending}
                    />
                  )}
                  <CmdButton
                    icon={<RefreshCw className="w-4 h-4" />}
                    label={t('players.restart')}
                    colorClass="bg-accent hover:bg-accent/80"
                    onClick={() => { if (confirm(t('players.restart_confirm'))) commandMutation.mutate({ id: selected.id_player, command: 'restart' }) }}
                    disabled={commandMutation.isPending}
                  />
                  {/* Reboot OS : pas d'accès OS en PWA (repli reload, redondant avec Restart) → masqué ChromeOS */}
                  {!isChromeOs(selected.player_os) && (
                    <CmdButton
                      icon={<Power className="w-4 h-4" />}
                      label={t('players.reboot_os')}
                      colorClass="bg-orange-500 hover:bg-orange-600"
                      onClick={() => { if (confirm(t('players.reboot_os_confirm'))) commandMutation.mutate({ id: selected.id_player, command: 'reboot_os' }) }}
                      disabled={commandMutation.isPending}
                    />
                  )}
                  <CmdButton
                    icon={<Camera className="w-4 h-4" />}
                    label={t('players.screenshot')}
                    colorClass="bg-accent hover:bg-accent/80"
                    onClick={() => { if (confirm(t('players.screenshot_confirm'))) commandMutation.mutate({ id: selected.id_player, command: 'screenshot' }) }}
                    disabled={commandMutation.isPending || screenshotLoading}
                  />
                  <CmdButton
                    icon={<Info className="w-4 h-4" />}
                    label={t('players.toggle_overlay')}
                    colorClass="bg-gray-500 hover:bg-gray-600"
                    onClick={() => commandMutation.mutate({ id: selected.id_player, command: 'toggle_overlay' })}
                    disabled={commandMutation.isPending}
                  />
                  {/* Mode maintenance manuel : statut « collant » (alertes suspendues) — pas une
                      commande Redis mais un statut serveur. Bascule selon l'état courant. */}
                  <CmdButton
                    icon={<Wrench className="w-4 h-4" />}
                    label={selected.status === 'maintenance' ? t('players.maintenance_off') : t('players.maintenance_on')}
                    colorClass={selected.status === 'maintenance' ? 'bg-success hover:bg-success/80' : 'bg-blue-500 hover:bg-blue-600'}
                    onClick={() => maintenanceMutation.mutate({ id: selected.id_player, enabled: selected.status !== 'maintenance' })}
                    disabled={maintenanceMutation.isPending}
                  />
                  {/* Entrée HDMI live (point AV 3/3) : prise de contrôle plein écran. Affiché UNIQUEMENT
                      pour les modèles BrightSign à entrée HDMI (gamme XT/4K/XD) — masqué sur HD/LS. */}
                  {selected.player_os === 'brightsign' && bsHasHdmiInput(selected.capabilities?.bs_model) && (
                    <>
                      <CmdButton
                        icon={<Tv2 className="w-4 h-4" />}
                        label={t('players.hdmi_in_start')}
                        colorClass="bg-gray-500 hover:bg-gray-600"
                        onClick={() => commandMutation.mutate({ id: selected.id_player, command: 'hdmi_input', params: { mode: 'start', source: 'drawer' } })}
                        disabled={commandMutation.isPending}
                      />
                      <CmdButton
                        icon={<Square className="w-4 h-4" />}
                        label={t('players.hdmi_in_stop')}
                        colorClass="bg-gray-500 hover:bg-gray-600"
                        onClick={() => commandMutation.mutate({ id: selected.id_player, command: 'hdmi_input', params: { mode: 'stop', source: 'drawer' } })}
                        disabled={commandMutation.isPending}
                      />
                    </>
                  )}
                </div>
                <CmdButton
                  icon={<RotateCcw className="w-4 h-4" />}
                  label={t('players.reset_config')}
                  colorClass="bg-red-500 hover:bg-red-600"
                  onClick={() => { if (confirm(t('players.reset_config_confirm'))) commandMutation.mutate({ id: selected.id_player, command: 'reset_config' }) }}
                  disabled={commandMutation.isPending}
                />
              </div>
            </div>

            {/* Pilotage écran — boutons remontés ici (sous les contrôles player) quand un contrôleur RS232/IP est configuré ; la (re)configuration reste dans « Écran & audio ». */}
            {selected.display_config?.type && !displayConfigOpen && (
              <div>
                <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2 flex items-center gap-1.5">
                  <Tv2 className="w-3.5 h-3.5" />
                  {t('players.display_control_title')}
                </p>
                <div className="flex flex-wrap items-center gap-2" data-write-action>
                  <CmdButton icon={<Power className="w-4 h-4" />} label={t('players.display_power_on')} colorClass="bg-success hover:bg-success/80" onClick={() => displayCmdMutation.mutate({ id: selected.id_player, command: 'power_on' })} disabled={displayCmdMutation.isPending} />
                  {(selected.display_config?.startup_sequence?.length ?? 0) > 0 && (
                    <CmdButton icon={<Power className="w-4 h-4" />} label={t('players.display_power_sequence')} colorClass="bg-success/70 hover:bg-success/90" onClick={() => displayCmdMutation.mutate({ id: selected.id_player, command: 'run_startup_sequence' })} disabled={displayCmdMutation.isPending} />
                  )}
                  <CmdButton icon={<Power className="w-4 h-4" />} label={t('players.display_power_off')} colorClass="bg-gray-500 hover:bg-gray-600" onClick={() => { if (confirm(t('players.display_power_off_confirm'))) displayCmdMutation.mutate({ id: selected.id_player, command: 'power_off' }) }} disabled={displayCmdMutation.isPending} />
                  {/* CEC = veille/réveil TV uniquement (pas de luminosité/volume/entrée arbitraire) */}
                  {selected.display_config?.type !== 'cec' && (
                    <>
                  <CmdButton icon={<Sun className="w-4 h-4" />} label={t('players.display_brightness')} colorClass="bg-amber-500 hover:bg-amber-600" onClick={() => { const lvl = prompt(t('players.display_brightness_prompt'), '50'); const level = parseInt(lvl ?? '', 10); if (!isNaN(level) && level >= 0 && level <= 100) displayCmdMutation.mutate({ id: selected.id_player, command: 'set_brightness', params: { level } }) }} disabled={displayCmdMutation.isPending} />
                  <CmdButton icon={<SlidersHorizontal className="w-4 h-4" />} label={t('players.display_contrast')} colorClass="bg-gray-500 hover:bg-gray-600" onClick={() => { const lvl = prompt(t('players.display_contrast_prompt'), '50'); const level = parseInt(lvl ?? '', 10); if (!isNaN(level) && level >= 0 && level <= 100) displayCmdMutation.mutate({ id: selected.id_player, command: 'set_contrast', params: { level } }) }} disabled={displayCmdMutation.isPending} />
                  <CmdButton icon={<Volume2 className="w-4 h-4" />} label={t('players.display_volume')} colorClass="bg-purple-500 hover:bg-purple-600" onClick={() => { const lvl = prompt(t('players.display_volume_prompt'), '50'); const level = parseInt(lvl ?? '', 10); if (!isNaN(level) && level >= 0 && level <= 100) displayCmdMutation.mutate({ id: selected.id_player, command: 'set_volume', params: { level } }) }} disabled={displayCmdMutation.isPending} />
                  <CmdButton icon={<VolumeX className="w-4 h-4" />} label={t('players.display_mute_on')} colorClass="bg-orange-500 hover:bg-orange-600" onClick={() => displayCmdMutation.mutate({ id: selected.id_player, command: 'mute_on' })} disabled={displayCmdMutation.isPending} />
                  <CmdButton icon={<Volume2 className="w-4 h-4" />} label={t('players.display_mute_off')} colorClass="bg-green-500 hover:bg-green-600" onClick={() => displayCmdMutation.mutate({ id: selected.id_player, command: 'mute_off' })} disabled={displayCmdMutation.isPending} />
                    </>
                  )}
                  <CmdButton icon={<Plug className="w-4 h-4" />} label={t('players.display_set_input')} colorClass="bg-blue-500 hover:bg-blue-600" onClick={() => { if (selected.display_config?.type === 'cec') { displayCmdMutation.mutate({ id: selected.id_player, command: 'set_input', params: { source: 'self' } }); return } const src = prompt(t('players.display_input_prompt'), 'hdmi1'); if (src) displayCmdMutation.mutate({ id: selected.id_player, command: 'set_input', params: { source: src.trim() } }) }} disabled={displayCmdMutation.isPending} />
                </div>
              </div>
            )}

            {/* Écran non pilotable : repli « coupure de la sortie vidéo du player » (opt-in). */}
            {!selected.display_config?.type && selected.display_config?.output_power_fallback && !displayConfigOpen && (
              <div>
                <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2 flex items-center gap-1.5">
                  <Tv2 className="w-3.5 h-3.5" />
                  {t('players.video_output_title')}
                </p>
                <div className="flex flex-wrap items-center gap-2" data-write-action>
                  <CmdButton icon={<Power className="w-4 h-4" />} label={t('players.video_output_on')} colorClass="bg-success hover:bg-success/80" onClick={() => displayCmdMutation.mutate({ id: selected.id_player, command: 'power_on' })} disabled={displayCmdMutation.isPending} />
                  <CmdButton icon={<Power className="w-4 h-4" />} label={t('players.video_output_off')} colorClass="bg-gray-500 hover:bg-gray-600" onClick={() => { if (confirm(t('players.video_output_off_confirm'))) displayCmdMutation.mutate({ id: selected.id_player, command: 'power_off' }) }} disabled={displayCmdMutation.isPending} />
                </div>
                <p className="text-[11px] text-gray-400 mt-1">{t('players.video_output_hint')}</p>
              </div>
            )}

            {/* Command notice */}
            {commandNotice && (
              <p className="text-xs text-success font-medium">{commandNotice}</p>
            )}
            {screenshotLoading && (
              <div className="flex items-center gap-2 text-xs text-gray-400">
                <div className="w-3.5 h-3.5 border-2 border-accent border-t-transparent rounded-full animate-spin" />
                {t('players.screenshot_loading')}
              </div>
            )}
            {screenshotUrl ? (
              <div className="rounded-lg overflow-hidden border border-gray-200">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={screenshotUrl} alt="Screenshot" className="w-full object-contain max-h-48 bg-canvas-dark" onError={() => setScreenshotUrl(null)} />
              </div>
            ) : selected.screenshot_url ? (
              <div className="rounded-lg overflow-hidden border border-gray-200">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={selected.screenshot_url} alt="Screenshot" className="w-full object-contain max-h-48 bg-canvas-dark" />
                <p className="text-[11px] text-gray-400 px-2 py-1">
                  {t('players.screenshot_captured', { when: relativeTime(selected.last_screenshot_at, t('players.never')) })}
                </p>
              </div>
            ) : null}

            <Collapsible title={t('players.group_screen_audio')} icon={<Tv2 className="w-3.5 h-3.5" />} active={!!selected.display_config?.type} className="order-3">
            {/* DISPLAY CONTROL */}
            <div className="space-y-3 pt-2 border-t border-gray-100">
              <div className="flex items-center justify-between">
                <p className="text-xs font-medium text-gray-500 uppercase tracking-wide flex items-center gap-1.5">
                  <Tv2 className="w-3.5 h-3.5" />
                  {t('players.display_title')}
                </p>
                <button
                  onClick={() => {
                    setDisplayConfigForm(selected.display_config ?? {})
                    setDisplayConfigOpen((v) => !v)
                  }}
                  className="text-xs text-accent hover:underline"
                >
                  {t('players.display_configure')}
                </button>
              </div>

              {/* Pas configuré + formulaire fermé */}
              {!selected.display_config?.type && !displayConfigOpen && (
                <p className="text-xs text-gray-400 italic">{t('players.display_not_configured')}</p>
              )}

              {/* Boutons de pilotage écran remontés au niveau des contrôles player (cf. plus haut) quand configuré. */}
              {selected.display_config?.type && !displayConfigOpen && (
                <p className="text-xs text-gray-400 italic">{t('players.display_buttons_moved')}</p>
              )}

              {/* Formulaire Configure */}
              {displayConfigOpen && (
                <DisplayConfigForm
                  initial={displayConfigForm}
                  onChange={setDisplayConfigForm}
                  playerOs={selected.player_os}
                  displayModes={selected.capabilities?.display_modes}
                  mpvVaapi={selected.capabilities?.mpv_vaapi ?? false}
                  hevcCapable={selected.capabilities?.codecs?.includes('hevc') ?? false}
                  bsBestMode={selected.capabilities?.bs_best_mode ?? null}
                  bsHdmiIn={selected.player_os === 'brightsign' && bsHasHdmiInput(selected.capabilities?.bs_model)}
                  onSave={() =>
                    displayConfigMutation.mutate({
                      id: selected.id_player,
                      // On persiste le display_config dès qu'AU MOINS un champ a une
                      // valeur significative — pas seulement type/résolution. Sinon régler
                      // uniquement la rotation (ou le volume, l'auto-power…) sur un player
                      // sans contrôleur RS232/IP effaçait tout le config (cfg=null).
                      cfg: Object.values(displayConfigForm).some(
                        (v) => v !== null && v !== undefined && v !== '',
                      )
                        ? (displayConfigForm as DisplayConfig)
                        : null,
                    })
                  }
                  onCancel={() => setDisplayConfigOpen(false)}
                  isSaving={displayConfigMutation.isPending}
                  t={t}
                />
              )}
            </div>

            {/* Rôle audio/contenu du player (§XIX) — contraint les choix de sortie audio */}
            <div className="space-y-1.5 pt-2 border-t border-gray-100">
              <label className="flex items-center justify-between gap-2 text-xs font-medium text-gray-500 uppercase tracking-wide">
                {t('players.player_role.title')}
                <select
                  value={selected.player_role ?? 'content'}
                  onChange={(e) => roleMutation.mutate({ id: selected.id_player, role: e.target.value as PlayerRead['player_role'] })}
                  disabled={roleMutation.isPending}
                  className="rounded border border-gray-200 px-2 py-1 bg-surface font-normal normal-case text-gray-700"
                >
                  <option value="content">{t('players.player_role.content')}</option>
                  <option value="audio">{t('players.player_role.audio')}</option>
                  <option value="mixed">{t('players.player_role.mixed')}</option>
                </select>
              </label>
              <p className="text-[11px] text-gray-400">{t(`players.player_role.hint_${selected.player_role ?? 'content'}`)}</p>
            </div>

            {/* Retail Media §2.8 — exclusion des campagnes (écran caisse, back-office…) */}
            <div className="space-y-1.5 pt-2 border-t border-gray-100">
              <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                <input
                  type="checkbox"
                  checked={selected.campaigns_optout ?? false}
                  disabled={optoutMutation.isPending}
                  onChange={(e) => optoutMutation.mutate({ id: selected.id_player, optout: e.target.checked })}
                />
                {t('players.campaigns_optout')}
              </label>
              <p className="text-[11px] text-gray-400">{t('players.campaigns_optout_hint')}</p>
              {/* Inventaire v2 (migr 0082) : plafond d'occupation + tarif SOV — vide =
                  héritage site puis org (enregistrés au blur) */}
              {!(selected.campaigns_optout ?? false) && (
                <div className="grid grid-cols-2 gap-3 pt-1">
                  <div>
                    <label className="block text-[11px] text-gray-400 mb-1">
                      {t('players.campaign_cap')}
                    </label>
                    <NumberField
                      key={`cap-${selected.id_player}`}
                      value={selected.campaign_max_occupancy_pct ?? null}
                      onCommit={(v) => campaignInvMutation.mutate({
                        id: selected.id_player,
                        body: { campaign_max_occupancy_pct: v },
                      })}
                      allowEmpty min={0} max={100}
                      placeholder={t('players.campaign_inherited')}
                      disabled={campaignInvMutation.isPending}
                      className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-lg bg-surface focus:outline-none focus:ring-2 focus:ring-accent/40"
                    />
                  </div>
                  <div>
                    <label className="block text-[11px] text-gray-400 mb-1">
                      {t('players.campaign_rate')}
                    </label>
                    <NumberField
                      key={`rate-${selected.id_player}`}
                      value={selected.campaign_sov_daily_rate ?? null}
                      onCommit={(v) => campaignInvMutation.mutate({
                        id: selected.id_player,
                        body: { campaign_sov_daily_rate: v },
                      })}
                      allowEmpty min={0} step={0.01}
                      placeholder={t('players.campaign_inherited')}
                      disabled={campaignInvMutation.isPending}
                      className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-lg bg-surface focus:outline-none focus:ring-2 focus:ring-accent/40"
                    />
                  </div>
                  <p className="text-[11px] text-gray-400 col-span-2 -mt-1">
                    {t('players.campaign_inv_hint')}
                  </p>
                </div>
              )}
            </div>

            {/* Sortie audio — tous les players sauf ChromeOS (routage setSinkId non porté en PWA) */}
            {!isChromeOs(selected.player_os) && (
              <AudioOutputSection player={selected} t={t} onPlayerUpdate={setSelected} />
            )}

            {/* Alimentation écran (auto-power §heures d'ouverture) */}
            <ScreenPowerSection player={selected} t={t} onPlayerUpdate={setSelected} />

            {/* Sous-titres multi-langues par pays du player (§7) */}
            <SubtitleSection player={selected} t={t} onPlayerUpdate={setSelected} />
            </Collapsible>

            <Collapsible title={t('players.group_device_diag')} icon={<Info className="w-3.5 h-3.5" />} className="order-3">
            {/* Infos appareil (OS/IP/MAC + modèle/firmware/série BrightSign) + fuseau horaire
                device (mismatch vs site + set_timezone à distance). ⚠️ On passe le player LIVE
                (liste refetch 10 s), pas le snapshot `selected` : après un set_timezone, le
                player redémarre et remonte son nouveau fuseau — la section doit le refléter
                sans re-sélectionner (retour Ed : « on ne sait pas si ça a été pris en compte »). */}
            <DeviceInfoSection
              player={players.find((p) => p.id_player === selected.id_player) ?? selected}
              t={t}
              siteTimezone={sites.find((s) => s.id_site === selected.id_site)?.timezone ?? null}
              onSetTimezone={(tz) => {
                commandMutation.mutate({ id: selected.id_player, command: 'set_timezone', params: { timezone: tz } })
              }}
            />

            {/* MAJ firmware BrightSign (P1.1) — version courante + MAJ vers la version épinglée */}
            <FirmwareSection player={selected} t={t} />

            {/* Signature Proof of Play (Ed25519) — toggle pour BrightSign/ChromeOS, natif Electron */}
            <PopSignSection player={selected} t={t} onPlayerUpdate={setSelected} />

            {/* Décodage vidéo matériel auto-détecté (capabilities) — read-only */}
            <DecodeSection
              player={selected}
              t={t}
              onDetect={() => { commandMutation.mutate({ id: selected.id_player, command: 'detect_capabilities' }); showNotice(t('players.decode.run_detect_sent')) }}
            />

            {/* Débit mesuré (opt-in measure_bandwidth) — dernière mesure remontée par le player */}
            {selected.capabilities?.store?.throughput_kbps != null && (
              <p className="pt-2 border-t border-gray-100 text-xs text-gray-500 flex items-center gap-1.5">
                <Wifi className="w-3.5 h-3.5 text-gray-400" />
                {t('players.measured_throughput', { value: fmtKbps(selected.capabilities.store.throughput_kbps) })}
              </p>
            )}

            {/* Inventaire média : programme planifié vs cache réel (cached/downloading/missing) */}
            <InventorySection player={selected} t={t} />

            {/* Disponibilité (look-ahead Phase 3) : ETA de téléchargement du contenu à venir */}
            <ReadinessSection player={selected} t={t} />

            {/* Diffusion — filtres de contenu par tags + fallback (§XVII) */}
            <TagFilterSection player={selected} t={t} />

            {/* Store Control Panel LAN (§19.3) — linux/rpi/windows (serveur HTTP local embarqué).
                L'assignation des paniers audio est sur la page Paniers audio
                (onglet « Diffusion »), pas ici. */}
            {/^(linux|windows)/.test((selected.player_os ?? '').toLowerCase()) && (
              <LocalControlSection player={selected} t={t} onPlayerUpdate={setSelected} />
            )}
            </Collapsible>

            {/* Active event overrides */}
            {playerActiveEvents(activeEvents, selected, allPlayerIds).length > 0 && (
              <div className="order-3 space-y-2 pt-2 border-t border-gray-100">
                <p className="text-xs font-medium text-gray-500 uppercase tracking-wide flex items-center gap-1.5">
                  <Zap className="w-3.5 h-3.5 text-amber-500" />
                  {t('players.active_events')}
                </p>
                {playerActiveEvents(activeEvents, selected, allPlayerIds).map((ev) => (
                  <div key={ev.id_event} className="flex items-start justify-between gap-2 bg-amber-500/10 dark:bg-amber-500/15 border border-amber-400/20 dark:border-amber-500/30 rounded-md px-3 py-2">
                    <div>
                      <p className="text-xs font-medium text-amber-900 dark:text-amber-200">{ev.event_name}</p>
                      <p className="text-xs text-amber-700 dark:text-amber-300 mt-0.5">{ev.playlist_name ?? ev.media_name ?? '—'}</p>
                    </div>
                    <div className="text-right shrink-0">
                      <p className="text-xs text-amber-600 dark:text-amber-300">P{ev.priority}</p>
                      {ev.expires_at && (
                        <p className="text-xs text-amber-500 mt-0.5">
                          {new Date(ev.expires_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                        </p>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}

            <Collapsible title={t('players.group_identity')} icon={<Info className="w-3.5 h-3.5" />} className="order-2">
            {/* Info grid */}
            <div className="space-y-2 pt-0">
              {[
                { label: 'ID',                          value: `#${selected.id_player}` },
                { label: t('players.drawer_ip'),        value: selected.ip_address ?? '—' },
                { label: t('players.drawer_wan_ip'),    value: selected.wan_ip ?? '—' },
                { label: t('players.drawer_mac'),       value: selected.mac_address ?? '—' },
                {
                  label: t('players.drawer_token_exp'),
                  value: selected.token_expires_at
                    ? new Date(selected.token_expires_at).toLocaleDateString()
                    : '—',
                },
              ].map(({ label, value }) => (
                <div key={label} className="flex justify-between text-sm">
                  <span className="text-gray-500">{label}</span>
                  <span className="text-gray-900 font-medium">{value}</span>
                </div>
              ))}
            </div>
            </Collapsible>

            {/* Alert reasons */}
            {(selected.alert_reasons?.length ?? 0) > 0 && (
              <div className="order-3 pt-2 border-t border-gray-100 space-y-2">
                <p className="text-xs font-medium text-gray-500 uppercase tracking-wide flex items-center gap-1.5">
                  <AlertTriangle className="w-3.5 h-3.5 text-orange-500" />
                  {t('players.active_alerts')}
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {selected.alert_reasons.map((code) => {
                    const cfg = ALERT_CHIP_CONFIG[code] ?? { label: code, color: 'bg-gray-100 text-gray-600' }
                    return (
                      <span key={code} className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${cfg.color}`}>
                        {t(`players.alert_${code}` as any, { defaultValue: cfg.label })}
                      </span>
                    )
                  })}
                </div>
              </div>
            )}

            {/* Resources — ChromeOS : seule la RAM (approx. deviceMemory) ; CPU/disque système
                indisponibles en navigateur → barres masquées plutôt que vides. */}
            {(selected.cpu_usage !== null || selected.ram_usage !== null || selected.disk_usage !== null) && (
              <div className="order-1 space-y-3 pt-2 border-t border-gray-100">
                <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">
                  {t('players.drawer_resources')}
                </p>
                {!isChromeOs(selected.player_os) && <ResourceBar value={selected.cpu_usage} label="CPU" />}
                <ResourceBar value={selected.ram_usage} label="RAM" />
                {!isChromeOs(selected.player_os) && <ResourceBar value={selected.disk_usage} label={t('players.drawer_disk')} />}
              </div>
            )}

            {/* Current state */}
            {currentState && (
              <div className="order-1 space-y-2 pt-2 border-t border-gray-100">
                <p className="text-xs font-medium text-gray-500 uppercase tracking-wide flex items-center gap-1.5">
                  <Activity className="w-3.5 h-3.5" />
                  {t('players.current_state')}
                </p>
                {currentState.active_event && (
                  <div className="flex items-start justify-between gap-2 bg-amber-500/10 dark:bg-amber-500/15 border border-amber-400/20 dark:border-amber-500/30 rounded-md px-3 py-2">
                    <div className="min-w-0 flex-1">
                      <p className="text-xs text-amber-600 dark:text-amber-300 font-medium uppercase tracking-wide">{t('players.state_event')}</p>
                      <MarqueeText text={currentState.active_event.event_name} className="text-xs font-medium text-amber-900 dark:text-amber-200 mt-0.5" />
                    </div>
                    <div className="text-right shrink-0">
                      <p className="text-xs text-amber-600 dark:text-amber-300">P{currentState.active_event.priority}</p>
                      {currentState.active_event.expires_at && (
                        <p className="text-xs text-amber-500 mt-0.5">
                          → {new Date(currentState.active_event.expires_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                        </p>
                      )}
                    </div>
                  </div>
                )}
                {currentState.active_schedule && (
                  <div className="flex items-center justify-between gap-2 bg-accent/10 dark:bg-blue-500/15 border border-blue-200 dark:border-blue-500/30 rounded-md px-3 py-2">
                    <div className="min-w-0 flex-1">
                      <p className="text-xs text-blue-600 dark:text-blue-300 font-medium uppercase tracking-wide">{t('players.state_schedule')}</p>
                      <MarqueeText text={currentState.active_schedule.schedule_name} className="text-xs font-medium text-blue-900 dark:text-blue-200 mt-0.5" />
                    </div>
                    <p className="text-xs text-blue-600 dark:text-blue-300 shrink-0">P{currentState.active_schedule.priority}</p>
                  </div>
                )}
                {/* Bloc visuel (content/mixed — masqué pour les players audio purs) */}
                {currentState.player_role !== 'audio' && (
                  currentState.active_playlist ? (
                  <div className="bg-gray-50 border border-gray-200 rounded-md px-3 py-2 space-y-0.5">
                    <p className="text-xs text-gray-500 font-medium uppercase tracking-wide">{t('players.state_playlist')}</p>
                    <MarqueeText text={currentState.active_playlist.playlist_name} className="text-xs font-medium text-gray-800" />
                    <p className="text-xs text-gray-500">
                      {currentState.active_playlist.item_count} {t('players.state_items')}
                      {currentState.active_playlist.total_duration_ms && (
                        <> · {Math.round(currentState.active_playlist.total_duration_ms / 1000)}s</>
                      )}
                      {' · '}{currentState.active_playlist.sync_mode === 'wall_clock' ? '⏱ sync' : '🔁 loop'}
                    </p>
                    {currentState.current_item && (
                      <div className="flex items-center justify-between gap-2 pt-1 border-t border-gray-200 mt-1">
                        <span className="text-xs text-gray-400 shrink-0">{t('players.state_now_playing')}</span>
                        <div className="min-w-0 flex-1 text-right">
                          <MarqueeText text={currentState.current_item.media_name} className="text-xs font-medium text-gray-700 text-right" />
                          <p className="text-xs text-gray-400">
                            {currentState.current_item.item_index + 1}/{currentState.active_playlist.item_count}
                            {' · '}{Math.round(currentState.current_item.offset_ms / 1000)}s / {Math.round(currentState.current_item.duration_ms / 1000)}s
                          </p>
                        </div>
                      </div>
                    )}
                  </div>
                  ) : (
                    <p className="text-xs text-gray-400 italic">{t('players.state_none')}</p>
                  )
                )}

                {/* Bloc audio (audio/mixed) — paniers actifs résolus */}
                {(currentState.player_role === 'audio' || currentState.player_role === 'mixed') && (
                  (currentState.active_audio && currentState.active_audio.length > 0) ? (
                    <div className="bg-teal-50 dark:bg-teal-500/15 border border-teal-200 dark:border-teal-500/30 rounded-md px-3 py-2 space-y-1">
                      <p className="text-xs text-teal-700 dark:text-teal-300 font-medium uppercase tracking-wide flex items-center gap-1.5">
                        <Volume2 className="w-3.5 h-3.5" />
                        {t('players.state_audio')}
                      </p>
                      {currentState.active_audio.map((b) => (
                        <div key={b.id_basket} className="flex items-center justify-between gap-2 text-xs">
                          <span className="text-teal-900 dark:text-teal-200 truncate">{b.basket_name}</span>
                          <span className="text-teal-600 dark:text-teal-300 shrink-0">
                            {t(`ab.type_${b.basket_type}`)}
                            {b.mix_ratio != null && <> · {b.mix_ratio}%</>}
                            {' · '}{b.item_count} {t('players.state_tracks')}
                          </span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-xs text-gray-400 italic">{t('players.state_no_audio')}</p>
                  )
                )}
              </div>
            )}
          </div>
        )}
      </Drawer>

      {/* Alert settings drawer */}
      <Drawer
        open={showAlertSettings}
        onClose={() => setShowAlertSettings(false)}
        title={t('players.alert_settings')}
      >
        <div className="space-y-4 text-sm">
          <p className="text-xs text-gray-500">{t('players.alert_settings_hint')}</p>

          {(
            [
              { key: 'offline_critical_min', label: t('players.alert_offline_min'), unit: t('players.unit_minutes'), min: 1, max: 60 },
              { key: 'token_warn_days',       label: t('players.alert_token_days'),   unit: t('players.unit_days'),    min: 1, max: 30 },
              { key: 'cpu_warn_pct',          label: t('players.alert_cpu_pct'),      unit: '%',                       min: 50, max: 100 },
              { key: 'ram_warn_pct',          label: t('players.alert_ram_pct'),      unit: '%',                       min: 50, max: 100 },
              { key: 'disk_warn_pct',         label: t('players.alert_disk_pct'),     unit: '%',                       min: 50, max: 100 },
            ] as const
          ).map(({ key, label, unit, min, max }) => (
            <div key={key} className="flex items-center justify-between gap-3">
              <label className="text-xs text-gray-600 flex-1">{label}</label>
              <div className="flex items-center gap-1.5 shrink-0">
                <NumberField
                  min={min}
                  max={max}
                  value={alertForm[key] as number}
                  onCommit={(v) => setAlertForm((f) => ({ ...f, [key]: v ?? min }))}
                  className="w-16 border border-gray-300 rounded-lg px-2 py-1 text-xs text-right focus:outline-none focus:ring-2 focus:ring-accent/40"
                />
                <span className="text-xs text-gray-400 w-12">{unit}</span>
              </div>
            </div>
          ))}

          <div className="flex items-center justify-between gap-3">
            <label className="text-xs text-gray-600 flex-1">{t('players.alert_min_version')}</label>
            <input
              type="text"
              placeholder="e.g. 1.5.0"
              value={alertForm.min_player_version ?? ''}
              onChange={(e) => setAlertForm((f) => ({ ...f, min_player_version: e.target.value || null }))}
              className="w-24 border border-gray-300 rounded-lg px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-accent/40"
            />
          </div>

          <div className="flex gap-2 pt-2 border-t border-gray-100">
            <Button
              className="flex-1"
              onClick={() => alertSettingsMutation.mutate(alertForm)}
              disabled={alertSettingsMutation.isPending}
            >
              {alertSettingsMutation.isPending ? t('common.loading') : t('common.save')}
            </Button>
            <Button variant="ghost" onClick={() => setShowAlertSettings(false)}>
              {t('common.cancel')}
            </Button>
          </div>
        </div>
      </Drawer>

      {/* Suppression d'un player — confirmation + option « supprimer aussi les preuves PoP » */}
      {deleteId !== null && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          onClick={() => !deletePlayerMut.isPending && setDeleteId(null)}
        >
          <div
            className="bg-surface rounded-lg border border-gray-200 w-full max-w-md p-5 space-y-4 shadow-sf-card"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2 text-danger">
              <AlertTriangle className="w-5 h-5" />
              <h3 className="font-semibold text-gray-800">{t('players.delete_title', { name: deleteName })}</h3>
            </div>
            <p className="text-sm text-gray-600">{t('players.delete_confirm')}</p>
            <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
              <input
                type="checkbox"
                checked={deletePop}
                onChange={(e) => setDeletePop(e.target.checked)}
                className="accent-accent"
              />
              {t('players.delete_also_pop')}
            </label>
            {deletePlayerMut.isError && (
              <p className="text-xs text-danger">
                Échec de la suppression —{' '}
                {(deletePlayerMut.error as { response?: { data?: { detail?: string } } })?.response?.data?.detail
                  || "le player est peut-être référencé ailleurs. Réessaie."}
              </p>
            )}
            <div className="flex gap-2 justify-end pt-2">
              <Button variant="ghost" onClick={() => setDeleteId(null)} disabled={deletePlayerMut.isPending}>
                {t('common.cancel')}
              </Button>
              <Button
                variant="danger"
                onClick={() => deletePlayerMut.mutate({ id: deleteId, pop: deletePop })}
                disabled={deletePlayerMut.isPending}
              >
                {deletePlayerMut.isPending ? t('common.loading') : t('common.delete')}
              </Button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

// ── Display Config Form ────────────────────────────────────────────────────────

function DisplayConfigForm({
  initial, onChange, onSave, onCancel, isSaving, t, playerOs, displayModes, mpvVaapi, hevcCapable, bsBestMode, bsHdmiIn,
}: {
  initial: Partial<DisplayConfig>
  onChange: (cfg: Partial<DisplayConfig>) => void
  onSave: () => void
  onCancel: () => void
  isSaving: boolean
  t: ReturnType<typeof useTranslations>
  playerOs?: string | null
  /** Modes supportés remontés par le player (format "WxH@Hz") — si présents, le sélecteur
   *  propose ces modes-là au lieu de la liste figée. */
  displayModes?: string[] | null
  /** Voie mpv dispo (capabilities.mpv_vaapi) → propose le rendu vidéo HW VAAPI via mpv. */
  mpvVaapi?: boolean
  /** Décodeur HEVC matériel présent (capabilities.codecs ⊇ 'hevc') → sur Android, propose
   *  le rendu vidéo natif ExoPlayer (HEVC + 4K@60 hors WebView). */
  hevcCapable?: boolean
  /** BrightSign : meilleur mode EDID (capabilities.bs_best_mode, ex. "3840x2160x60p") → libellé du toggle 4K. */
  bsBestMode?: string | null
  /** BrightSign à entrée HDMI physique (gamme XT/4K/XD1230) → réglages page locale /control. */
  bsHdmiIn?: boolean
}) {
  const type = initial.type ?? null
  const isRpi = playerOs?.startsWith('linux-arm') ?? false
  // mpv = décodage HW VAAPI alternatif au <video> Chromium. Proposé uniquement sur un
  // player Intel/Linux x64 qui a remonté la capacité (mpv installé + VAAPI decode).
  // NB : player_os est figé à l'enregistrement — les players enregistrés avant l'ajout du
  // suffixe d'archi remontent 'linux' (pas 'linux-x64'). On accepte donc tout 'linux*' SAUF
  // le RPi (linux-arm) ; mpv_vaapi (jamais vrai sur arm/RPi) reste le vrai garde-fou matériel.
  const showMpv = (mpvVaapi ?? false) && (playerOs?.startsWith('linux') ?? false) && !isRpi
  const isAndroid = playerOs?.startsWith('android') ?? false
  const isExo = (initial.video_renderer ?? 'auto') === 'exo'
  // exo = rendu vidéo natif ExoPlayer (HEVC + 4K@60 hors WebView Chromium). Proposé sur un
  // player Android dont le décodeur HEVC matériel est remonté (capabilities.codecs ⊇ 'hevc'),
  // ou déjà en exo (évite un sélecteur vide si la capacité n'est pas remontée à cet instant).
  const showExo = isAndroid && ((hevcCapable ?? false) || isExo)
  // Règle d'homogénéité (player_compat) : mpv/exo (décodage HW natif) en LAN Sync exigent un
  // groupe homogène ; Android = hétérogène avec d'autres types (warn).
  const isMpv = (initial.video_renderer ?? 'auto') === 'mpv'
  // L'extension Chrome ne peut PAS cacher en local (Service Worker indisponible sur
  // chrome-extension://) → elle ne fait que streamer. Le streaming serveur est donc
  // OBLIGATOIRE : sans lui, le player resterait bloqué (attente d'un cache qui ne se remplit jamais).
  const isExtension = playerOs === 'chrome_extension'
  const isBrightSign = playerOs === 'brightsign'

  function set<K extends keyof DisplayConfig>(key: K, value: DisplayConfig[K]) {
    onChange({ ...initial, [key]: value })
  }

  // Force le streaming pour l'extension Chrome (case verrouillée + valeur persistée à true).
  useEffect(() => {
    if (isExtension && initial.allow_streaming !== true) set('allow_streaming', true)
  }, [isExtension, initial.allow_streaming]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="bg-gray-50 border border-gray-200 rounded-lg p-4 space-y-3 text-sm">
      {/* Screen resolution — non applicable aux BrightSign : leur sortie est pilotée par
          bs_video_mode (selecteur ci-dessous), pas par wlr-randr/xrandr/PS. On le masque. */}
      {!isBrightSign && (
      <div>
        <label className="block text-xs font-medium text-gray-500 mb-1">
          {t('players.screen_resolution')}
        </label>
        <select
          value={initial.screen_resolution ?? ''}
          onChange={(e) => set('screen_resolution', e.target.value || null)}
          className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40 bg-surface"
        >
          <option value="">{t('players.resolution_unchanged')}</option>
          {displayModes && displayModes.length > 0 ? (
            // Modes réels remontés par le player (ex. MINIX : 3840x2160@60), triés du plus
            // grand au plus petit (résolution puis fréquence) pour un drawer lisible.
            [...displayModes]
              .sort((a, b) => {
                const p = (s: string) => {
                  const m = s.match(/^(\d+)x(\d+)@(\d+)$/)
                  return m ? [+m[1], +m[2], +m[3]] : [0, 0, 0]
                }
                const A = p(a), B = p(b)
                return B[0] - A[0] || B[1] - A[1] || B[2] - A[2]
              })
              .map((m) => {
                const [dim, hz] = m.split('@')
                return (
                  <option key={m} value={m}>
                    {dim.replace('x', '×')}{hz ? ` ${hz}Hz` : ''}
                  </option>
                )
              })
          ) : (
            <>
              <option value="1920x1080@60">1920×1080 60Hz (FHD)</option>
              <option value="1280x720@60">1280×720 60Hz (HD)</option>
              <option value="1024x768@60">1024×768 60Hz (XGA)</option>
              {!isRpi && <option value="3840x2160@30">3840×2160 30Hz (4K)</option>}
            </>
          )}
        </select>
        <p className="text-xs text-gray-400 mt-0.5">{t('players.resolution_hint')}</p>
      </div>
      )}

      {/* BrightSign : sortie 4K par-player. L'autorun applique le mode au boot (sidecar sf_videomode.txt
          + reboot). "best" = meilleur mode EDID (4K/8K selon le panneau), "fhd" = 1080. Changement = 1 reboot. */}
      {isBrightSign && (
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">
            {t('players.bs_output')}
          </label>
          <select
            value={(initial.bs_video_mode as string) ?? ''}
            onChange={(e) => set('bs_video_mode', (e.target.value || null) as DisplayConfig['bs_video_mode'])}
            className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40 bg-surface"
          >
            <option value="">{t('players.bs_output_default')}</option>
            <option value="best">{t('players.bs_output_4k')}{bsBestMode ? ` — ${bsBestMode}` : ''}</option>
            <option value="fhd">{t('players.bs_output_fhd')}</option>
          </select>
          <p className="text-xs text-gray-400 mt-0.5">{t('players.bs_output_hint')}</p>
        </div>
      )}

      {/* Rotation écran — affichage portrait (xrandr --rotate ; Windows = à venir) */}
      <div>
        <label className="block text-xs font-medium text-gray-500 mb-1">
          {t('players.rotation')}
        </label>
        <select
          value={initial.screen_rotation ?? ''}
          onChange={(e) => set('screen_rotation', (e.target.value || null) as DisplayConfig['screen_rotation'])}
          className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40 bg-surface"
        >
          <option value="">{t('players.rotation_unchanged')}</option>
          <option value="normal">{t('players.rotation_normal')}</option>
          <option value="right">{t('players.rotation_right')}</option>
          <option value="left">{t('players.rotation_left')}</option>
          <option value="inverted">{t('players.rotation_inverted')}</option>
        </select>
        <p className="text-xs text-gray-400 mt-0.5">{t('players.rotation_hint')}</p>
      </div>

      {/* Accélération matérielle GPU — Windows uniquement (Linux/RPi : flags start-player.sh).
          Relaunch du player au changement. */}
      {(playerOs?.startsWith('windows') ?? false) && (
        <div>
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={initial.gpu_enabled !== false}
              onChange={(e) => set('gpu_enabled', e.target.checked)}
              className="rounded-lg border-gray-300 text-accent focus:ring-accent/40"
            />
            <span className="text-xs font-medium text-gray-600">{t('players.gpu_enabled')}</span>
          </label>
          <p className="text-xs text-gray-400 mt-0.5 ml-6">{t('players.gpu_enabled_hint')}</p>
        </div>
      )}

      {/* Moteur de rendu vidéo — alternatif au <video> Chromium quand un décodage HW natif
          est dispo : mpv+VAAPI (Intel/Linux) ou ExoPlayer (Android, HEVC + 4K@60). Proposé
          seulement si la capacité matérielle est remontée. */}
      {(showMpv || showExo) && (
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">
            {t('players.video_renderer')}
          </label>
          <select
            value={initial.video_renderer ?? 'auto'}
            onChange={(e) => {
              const v = e.target.value as DisplayConfig['video_renderer']
              // Règle d'homogénéité (cf. player_compat) : mpv/exo + LAN Sync sont permis si le
              // groupe est homogène. Le backend refuse (409) à la sauvegarde si le groupe devient
              // hétérogène.
              set('video_renderer', v)
            }}
            className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40 bg-surface"
          >
            <option value="auto">{t('players.video_renderer_auto')}</option>
            {showMpv && <option value="mpv">{t('players.video_renderer_mpv')}</option>}
            {showExo && <option value="exo">{t('players.video_renderer_exo')}</option>}
          </select>
          <p className="text-xs text-gray-400 mt-0.5">
            {t(showExo ? 'players.video_renderer_exo_hint' : 'players.video_renderer_hint')}
          </p>
        </div>
      )}

      {/* Plafond de bande passante download (offline-first Q3) */}
      <div>
        <label className="block text-xs font-medium text-gray-500 mb-1">
          {t('players.download_bandwidth')}
        </label>
        <NumberField
          min={0}
          allowEmpty
          value={(initial.download_bandwidth_kbps as number | null) ?? null}
          onCommit={(v) => set('download_bandwidth_kbps', v)}
          placeholder={t('players.download_bandwidth_unlimited')}
          className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40 bg-surface"
        />
        <p className="text-xs text-gray-400 mt-0.5">{t('players.download_bandwidth_hint')}</p>
      </div>

      {/* Streaming auto serveur — OFF par défaut = download-then-play STRICT (anti-écroulement BP).
          OBLIGATOIRE et verrouillé pour l'extension Chrome (streaming-only, pas de cache local). */}
      <div>
        <label className={`flex items-center gap-2 text-sm text-gray-700 ${isExtension ? 'cursor-not-allowed opacity-90' : 'cursor-pointer'}`}>
          <input
            type="checkbox"
            checked={isExtension ? true : initial.allow_streaming === true}
            disabled={isExtension}
            onChange={(e) => set('allow_streaming', e.target.checked)}
            className="rounded-lg border-gray-300 text-accent focus:ring-accent/40 disabled:opacity-60"
          />
          {t('players.allow_streaming')}
        </label>
        <p className="text-xs text-gray-400 mt-0.5">
          {isExtension ? t('players.allow_streaming_ext') : t('players.allow_streaming_hint')}
        </p>
      </div>

      {/* Fenêtre horaire de prefetch (look-ahead Phase 2) — créneau unique start→end. */}
      <div>
        <label className="block text-xs font-medium text-gray-500 mb-1">
          {t('players.download_window')}
        </label>
        <div className="flex items-center gap-2">
          <input
            type="time"
            value={initial.download_windows?.[0]?.start ?? ''}
            onChange={(e) => {
              const start = e.target.value
              const end = initial.download_windows?.[0]?.end ?? ''
              set('download_windows', start && end ? [{ start, end }] : null)
            }}
            className="flex-1 border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40 bg-surface"
          />
          <span className="text-gray-400">→</span>
          <input
            type="time"
            value={initial.download_windows?.[0]?.end ?? ''}
            onChange={(e) => {
              const end = e.target.value
              const start = initial.download_windows?.[0]?.start ?? ''
              set('download_windows', start && end ? [{ start, end }] : null)
            }}
            className="flex-1 border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40 bg-surface"
          />
        </div>
        <p className="text-xs text-gray-400 mt-0.5">{t('players.download_window_hint')}</p>
      </div>

      {/* Mesure passive du débit (look-ahead Phase 3) → affine l'ETA de readiness */}
      <div>
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={!!initial.measure_bandwidth}
            onChange={(e) => set('measure_bandwidth', e.target.checked)}
            className="rounded-lg border-gray-300 text-accent focus:ring-accent/40"
          />
          <span className="text-xs font-medium text-gray-600">{t('players.measure_bandwidth')}</span>
        </label>
        <p className="text-xs text-gray-400 mt-0.5 ml-6">{t('players.measure_bandwidth_hint')}</p>
      </div>

      {/* Capture d'écran automatique (intervalle) */}
      <div>
        <label className="block text-xs font-medium text-gray-500 mb-1">
          {t('players.screenshot_interval')}
        </label>
        <select
          value={initial.screenshot_interval ?? 3600}
          onChange={(e) => set('screenshot_interval', Number(e.target.value))}
          className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40 bg-surface"
        >
          <option value={0}>{t('players.screenshot_interval_off')}</option>
          <option value={900}>{t('players.screenshot_interval_15m')}</option>
          <option value={1800}>{t('players.screenshot_interval_30m')}</option>
          <option value={3600}>{t('players.screenshot_interval_1h')}</option>
          <option value={21600}>{t('players.screenshot_interval_6h')}</option>
          <option value={86400}>{t('players.screenshot_interval_24h')}</option>
        </select>
        <p className="text-xs text-gray-400 mt-0.5">{t('players.screenshot_interval_hint')}</p>
      </div>

      <hr className="border-gray-200" />

      {/* Type */}
      <div>
        <label className="block text-xs font-medium text-gray-500 mb-1">
          {t('players.display_type')}
        </label>
        <select
          value={type ?? ''}
          onChange={(e) => {
            // Fusionner avec le reste du config (set) — sinon on efface résolution/rotation/etc.
            const v = e.target.value as DisplayConfig['type']
            set('type', v || null)
          }}
          className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40 bg-surface"
        >
          <option value="">{t('players.display_type_none')}</option>
          <option value="rs232">RS232 (serial)</option>
          <option value="ip">IP (TCP)</option>
          <option value="cec">HDMI-CEC</option>
        </select>
      </div>

      {/* Page locale /control (BrightSign à entrée HDMI) : activation + PIN optionnel.
          Validé DANS l'autorun (jamais servi en HTTP) ; ne concerne que le canal local. */}
      {isBrightSign && bsHdmiIn && (
        <>
          <div>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={initial.local_control !== false}
                onChange={(e) => set('local_control', e.target.checked ? null : false)}
                className="rounded-lg border-gray-300 text-accent focus:ring-accent/40"
              />
              <span className="text-xs font-medium text-gray-600">{t('players.local_control_page')}</span>
            </label>
            <p className="text-xs text-gray-400 mt-0.5 ml-6">{t('players.local_control_hint')}</p>
          </div>
          {initial.local_control !== false && (
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">
                {t('players.local_control_pin')}
              </label>
              <input
                type="text"
                inputMode="numeric"
                maxLength={6}
                value={initial.local_control_pin ?? ''}
                onChange={(e) => {
                  const v = e.target.value.replace(/\D/g, '').slice(0, 6)
                  set('local_control_pin', v || null)
                }}
                placeholder="—"
                className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40"
              />
              <p className="text-xs text-gray-400 mt-0.5">{t('players.local_control_pin_hint')}</p>
            </div>
          )}
          <hr className="border-gray-200" />
        </>
      )}

      {/* Écran non pilotable : repli « couper la sortie vidéo du player » (opt-in).
          Utilisé par l'auto-power heures d'ouverture + les boutons Allumer/Éteindre. */}
      {!type && (
        <div>
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={!!initial.output_power_fallback}
              onChange={(e) => set('output_power_fallback', e.target.checked ? true : null)}
              className="rounded-lg border-gray-300 text-accent focus:ring-accent/40"
            />
            <span className="text-xs font-medium text-gray-600">{t('players.output_fallback')}</span>
          </label>
          <p className="text-xs text-gray-400 mt-0.5 ml-6">{t('players.output_fallback_hint')}</p>
        </div>
      )}

      {type === 'rs232' && (
        <>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">
              {t('players.display_serial_port')}
            </label>
            <input
              type="text"
              value={initial.serial_port ?? ''}
              onChange={(e) => set('serial_port', e.target.value || null)}
              placeholder="/dev/ttyUSB0"
              className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">
              {t('players.display_baud_rate')}
            </label>
            <select
              value={initial.baud_rate ?? 9600}
              onChange={(e) => set('baud_rate', Number(e.target.value))}
              className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40 bg-surface"
            >
              {[9600, 19200, 38400, 57600, 115200].map((b) => (
                <option key={b} value={b}>{b}</option>
              ))}
            </select>
          </div>
        </>
      )}

      {type === 'ip' && (
        <>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">
              {t('players.display_host')}
            </label>
            <input
              type="text"
              value={initial.host ?? ''}
              onChange={(e) => set('host', e.target.value || null)}
              placeholder="192.168.1.50"
              className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">
              {t('players.display_tcp_port')}
            </label>
            <NumberField
              min={1} max={65535}
              value={(initial.tcp_port as number | null) ?? 1515}
              onCommit={(v) => set('tcp_port', v ?? 1515)}
              className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40"
            />
          </div>
        </>
      )}

      {type === 'cec' && (
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">
            {t('players.display_cec_device')}
          </label>
          <input
            type="text"
            value={initial.cec_device ?? ''}
            onChange={(e) => set('cec_device', e.target.value || null)}
            placeholder="/dev/cec0"
            className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
          <p className="text-xs text-gray-400 mt-0.5">{t('players.display_cec_hint')}</p>
        </div>
      )}

      {type && (
        <>
          {/* CEC : pas de protocole constructeur ni d'adresse (la TV = adresse logique 0) */}
          {type !== 'cec' && (
            <>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">
              {t('players.display_model')}
            </label>
            <select
              value={initial.model ?? 'samsung_mdc'}
              onChange={(e) => set('model', e.target.value || null)}
              className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40 bg-surface"
            >
              <option value="samsung_mdc">Samsung MDC</option>
              <option value="lg_rs232c">LG RS232C</option>
              <option value="generic">{t('players.display_model_generic')}</option>
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">
              {t('players.display_address')}
            </label>
            <NumberField
              min={1} max={99}
              value={(initial.address as number | null) ?? 1}
              onCommit={(v) => set('address', v ?? 1)}
              className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40"
            />
          </div>
            </>
          )}

          {/* ── Power schedule ──────────────────────────────────────────── */}
          <div className="pt-2 border-t border-gray-200 space-y-3">
            <div>
              <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">
                {t('players.display_schedule_title')}
              </p>
              <p className="text-xs text-gray-400 mt-0.5">{t('players.display_schedule_hint')}</p>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="block text-xs font-medium text-gray-500 mb-1">{t('players.display_schedule_on')}</label>
                <input type="time" value={initial.schedule?.power_on ?? ''} onChange={(e) => set('schedule', { ...(initial.schedule ?? {}), power_on: e.target.value || null })} className="w-full border border-gray-300 rounded-md px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40" />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-500 mb-1">{t('players.display_schedule_off')}</label>
                <input type="time" value={initial.schedule?.power_off ?? ''} onChange={(e) => set('schedule', { ...(initial.schedule ?? {}), power_off: e.target.value || null })} className="w-full border border-gray-300 rounded-md px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40" />
              </div>
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">{t('players.display_schedule_days')}</label>
              <div className="flex gap-1">
                {([
                  [1, t('players.display_sched_mo')],
                  [2, t('players.display_sched_tu')],
                  [3, t('players.display_sched_we')],
                  [4, t('players.display_sched_th')],
                  [5, t('players.display_sched_fr')],
                  [6, t('players.display_sched_sa')],
                  [7, t('players.display_sched_su')],
                ] as [number, string][]).map(([day, label]) => {
                  const activeDays = initial.schedule?.days ?? [1, 2, 3, 4, 5, 6, 7]
                  const isActive = activeDays.includes(day)
                  return (
                    <button key={day} type="button" onClick={() => { const current = initial.schedule?.days ?? [1,2,3,4,5,6,7]; const next = isActive ? current.filter((d) => d !== day) : [...current, day].sort((a,b)=>a-b); set('schedule', { ...(initial.schedule ?? {}), days: next }) }} className={`w-7 h-7 rounded text-xs font-medium transition-sf ${isActive ? 'bg-accent text-white' : 'bg-gray-100 text-gray-500 hover:bg-gray-200'}`}>{label}</button>
                  )
                })}
              </div>
            </div>
          </div>

          {/* ── Pulse power ──────────────────────────────────────────────── */}
          {initial.schedule?.power_on && initial.schedule?.power_off && (
            <div className="space-y-2 pt-2 border-t border-gray-200">
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={initial.schedule?.pulse_enabled !== false} onChange={(e) => set('schedule', { ...(initial.schedule ?? {}), pulse_enabled: e.target.checked ? undefined : false })} className="accent-accent" />
                <span className="text-xs font-medium text-gray-700">{t('players.display_pulse_enabled')}</span>
              </label>
              {initial.schedule?.pulse_enabled !== false && (
                <div className="flex items-center gap-2 ml-5">
                  <NumberField min={1} allowEmpty value={initial.schedule?.pulse_interval_s ? Math.round((initial.schedule.pulse_interval_s as number) / 60) : null} onCommit={(v) => set('schedule', { ...(initial.schedule ?? {}), pulse_interval_s: v === null ? null : v * 60 })} placeholder="30" className="w-16 border border-gray-300 rounded-md px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40" />
                  <span className="text-xs text-gray-500">{t('players.display_pulse_minutes')}</span>
                </div>
              )}
            </div>
          )}
        </>
      )}

      {/* ── Sequence de demarrage ─────────────────────────────────────── */}
      {initial.type && (
        <div className="pt-2 border-t border-gray-200 space-y-3">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">
            {t('players.display_sequence_title')}
          </p>
          <p className="text-xs text-gray-400">{t('players.display_sequence_hint')}</p>

          {(initial.startup_sequence ?? []).map((step, idx) => (
            <div key={idx} className="flex items-start gap-2 bg-gray-50 rounded-md p-2">
              <div className="flex flex-col gap-1 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-xs text-gray-400 w-4">{idx + 1}</span>
                  <select
                    value={step.command}
                    onChange={(e) => {
                      const seq = [...(initial.startup_sequence ?? [])];
                      seq[idx] = { ...seq[idx], command: e.target.value, params: undefined };
                      set('startup_sequence', seq);
                    }}
                    className="flex-1 border border-gray-300 rounded-lg px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-accent/40"
                  >
                    {['power_on','power_off','set_input','set_brightness','set_contrast','set_volume','mute_on','mute_off'].map((cmd) => (
                      <option key={cmd} value={cmd}>{cmd}</option>
                    ))}
                  </select>
                  <div className="flex items-center gap-1">
                    <NumberField
                      min={0}
                      value={step.delay_before_s ?? 0}
                      onCommit={(v) => {
                        const seq = [...(initial.startup_sequence ?? [])];
                        seq[idx] = { ...seq[idx], delay_before_s: v ?? 0 };
                        set('startup_sequence', seq);
                      }}
                      className="w-12 border border-gray-300 rounded px-1 py-1 text-xs text-center"
                    />
                    <span className="text-xs text-gray-400">s</span>
                  </div>
                </div>
                {['set_input'].includes(step.command) && (
                  <select
                    value={(step.params?.source as string) ?? 'hdmi1'}
                    onChange={(e) => {
                      const seq = [...(initial.startup_sequence ?? [])];
                      seq[idx] = { ...seq[idx], params: { source: e.target.value } };
                      set('startup_sequence', seq);
                    }}
                    className="ml-6 border border-gray-300 rounded px-2 py-1 text-xs"
                  >
                    {['hdmi1','hdmi2','hdmi3','dp','pc','av','dvi'].map((s) => (
                      <option key={s} value={s}>{s}</option>
                    ))}
                  </select>
                )}
                {['set_brightness','set_contrast','set_volume'].includes(step.command) && (
                  <div className="ml-6 flex items-center gap-1">
                    <NumberField
                      min={0}
                      max={100}
                      value={(step.params?.level as number) ?? 50}
                      onCommit={(v) => {
                        const seq = [...(initial.startup_sequence ?? [])];
                        seq[idx] = { ...seq[idx], params: { level: v ?? 0 } };
                        set('startup_sequence', seq);
                      }}
                      className="w-14 border border-gray-300 rounded px-2 py-1 text-xs"
                    />
                    <span className="text-xs text-gray-400">0-100</span>
                  </div>
                )}
              </div>
              <button
                type="button"
                onClick={() => {
                  const seq = (initial.startup_sequence ?? []).filter((_, i) => i !== idx);
                  set('startup_sequence', seq.length ? seq : null);
                }}
                className="text-gray-400 hover:text-red-500 mt-0.5"
              >
                ×
              </button>
            </div>
          ))}

          <button
            type="button"
            onClick={() => {
              const seq = [...(initial.startup_sequence ?? []), { command: 'power_on', delay_before_s: 0 }];
              set('startup_sequence', seq);
            }}
            className="w-full py-1.5 text-xs text-accent border border-accent/30 rounded-md hover:bg-accent/5"
          >
            + {t('players.display_sequence_add')}
          </button>
        </div>
      )}

      {/* ── LAN Sync — visible même sans écran RS232/IP. MASQUÉ pour ChromeOS (PWA) : la
          plateforme navigateur ne fait pas de synchro LAN (UDP broadcast hors-périmètre PWA)
          → pas de case d'activation (retour Ed). ── */}
      {!isChromeOs(playerOs ?? null) && (
      <div className="pt-2 border-t border-gray-200 space-y-2">
        <div className="flex items-center gap-2">
          <input
            id="lan-sync"
            type="checkbox"
            checked={initial.lan_sync_enabled ?? false}
            onChange={(e) => set('lan_sync_enabled', e.target.checked)}
            className="rounded-lg border-gray-300 text-accent focus:ring-accent/40"
          />
          <label htmlFor="lan-sync" className="text-xs text-gray-700 font-medium">
            {t('players.display_lan_sync')}
          </label>
        </div>
        {/* Règle d'homogénéité : un groupe LAN avec décodage HW (mpv/exo) doit être homogène
            (mêmes OS/renderer/matériel) sinon flip-flop. Indication quand mpv + LAN activés. */}
        {isMpv && (initial.lan_sync_enabled ?? false) && (
          <p className="text-xs text-amber-600 dark:text-amber-300">{t('players.lan_sync_homogeneous_hint')}</p>
        )}
        {/* Android = matériel/horloge hétérogènes : à grouper seulement avec des players identiques. */}
        {!isMpv && isAndroid && (initial.lan_sync_enabled ?? false) && (
          <p className="text-xs text-amber-600 dark:text-amber-300">{t('players.lan_sync_homogeneous_hint')}</p>
        )}
        {(initial.lan_sync_enabled ?? false) && (
          <>
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">
                {t('players.display_lan_group')}
              </label>
              <input
                type="text"
                value={initial.lan_sync_group ?? ''}
                onChange={(e) => set('lan_sync_group', e.target.value || null)}
                placeholder="videowall-1"
                className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/40"
              />
            </div>
            <div className="flex items-start gap-2">
              <input
                id="lan-master"
                type="checkbox"
                checked={initial.lan_sync_master ?? false}
                onChange={(e) => set('lan_sync_master', e.target.checked)}
                className="mt-0.5 rounded-lg border-gray-300 text-accent focus:ring-accent/40"
              />
              <label htmlFor="lan-master" className="text-xs text-gray-700">
                {t('players.display_lan_master')}
                <span className="block text-gray-400">{t('players.display_lan_master_hint')}</span>
              </label>
            </div>
          </>
        )}
      </div>
      )}

      <div className="flex gap-2 pt-1">
        <button
          data-write-action
          onClick={onSave}
          disabled={isSaving}
          className="flex-1 py-1.5 bg-accent text-white text-sm rounded-md hover:bg-accent/90 disabled:opacity-50"
        >
          {isSaving ? t('common.loading') : t('common.save')}
        </button>
        <button
          onClick={onCancel}
          className="px-3 py-1.5 text-sm text-gray-600 hover:text-gray-900 border border-gray-300 rounded-md"
        >
          {t('common.cancel')}
        </button>
      </div>
    </div>
  )
}

// ── Player table (extracted) ───────────────────────────────────────────────────

function PlayerTable({
  players,
  isLoading,
  t,
  onSelect,
  onDelete,
  overriddenPlayerIds = new Set(),
  emptyKey = 'no_players',
}: {
  players: PlayerRead[]
  isLoading: boolean
  t: ReturnType<typeof useTranslations>
  onSelect: (p: PlayerRead) => void
  onDelete: (id: number) => void
  overriddenPlayerIds?: Set<number>
  emptyKey?: string
}) {
  const { data: namespaces = [] } = useTagNamespaces()
  if (isLoading) {
    return (
      <div className="flex justify-center py-12">
        <div className="w-6 h-6 border-2 border-accent border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  if (players.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 gap-3 text-gray-400">
        <Monitor className="w-10 h-10" />
        <p className="text-sm">{t(`players.${emptyKey}`)}</p>
      </div>
    )
  }

  return (
    // Cartes compactes : ~4/ligne en largeur standard, le nombre s'adapte à la
    // résolution horizontale (auto-fill minmax 260px).
    <CardGrid minItemWidth={260}>
      {players.map((p) => (
        <div
          key={p.id_player}
          onClick={() => onSelect(p)}
          className="bg-surface rounded-lg border border-gray-200 overflow-hidden hover:border-accent hover:shadow-sm transition-sf cursor-pointer"
        >
          {/* Zone screenshot (capture player réelle si dispo, sinon placeholder) + overlays */}
          <div className="aspect-video bg-gray-100 relative">
            {p.screenshot_url ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={p.screenshot_url} alt={p.player_name} className="absolute inset-0 w-full h-full object-cover" />
            ) : (
              <div className="absolute inset-0 flex items-center justify-center text-gray-300">
                <Monitor className="w-10 h-10" />
              </div>
            )}
            {/* Nom (haut gauche) */}
            <span className="absolute top-2 left-2 max-w-[62%] truncate px-2 py-0.5 rounded bg-black/55 text-white text-xs font-medium" title={p.player_name}>
              {p.player_name}
            </span>
            {/* Statut (haut droite) */}
            <span className="absolute top-2 right-2 flex items-center gap-1">
              <StatusBadge status={p.status} label={playerStatusLabel(p.status, t)} />
              {(p.alert_reasons?.length ?? 0) > 0 && (
                <span className="inline-flex items-center justify-center w-4 h-4 rounded-full bg-orange-500 text-white text-[9px] font-bold shrink-0">
                  {p.alert_reasons.length}
                </span>
              )}
            </span>
            {/* OS (bas gauche) */}
            <span className="absolute bottom-2 left-2"><OsChip os={p.player_os} /></span>
            {/* Dernière connexion (bas droite) */}
            <span className="absolute bottom-2 right-2 px-1.5 py-0.5 rounded bg-black/55 text-white text-[10px]">
              {relativeTime(p.last_seen_at, t('players.never'))}
            </span>
          </div>

          {/* Sous l'image : décodage + override, ressources CPU/RAM, suppression */}
          <div className="p-3 space-y-2">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-1.5 min-w-0">
                <DecodeBadge player={p} t={t} compact />
                {overriddenPlayerIds.has(p.id_player) && (
                  <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-medium bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-300 shrink-0">
                    <Zap className="w-3 h-3" />
                    {t('players.event_override')}
                  </span>
                )}
              </div>
              <button
                onClick={(e) => { e.stopPropagation(); onDelete(p.id_player) }}
                className="p-1.5 text-gray-400 hover:text-danger rounded transition-sf shrink-0"
                title={t('common.delete')}
              >
                <Trash2 className="w-4 h-4" />
              </button>
            </div>
            {p.cpu_usage !== null ? (
              <div className="space-y-1">
                <ResourceBar value={p.cpu_usage} label="CPU" />
                <ResourceBar value={p.ram_usage} label="RAM" />
              </div>
            ) : (
              <span className="text-gray-400 text-xs">—</span>
            )}
            {/* Inventaire store local (offline-first Q4) */}
            {p.capabilities?.store && (
              <div className="flex items-center gap-1.5 text-xs pt-0.5">
                <HardDrive className="w-3 h-3 text-gray-400 shrink-0" />
                <span className="text-gray-500">
                  {Math.round((p.capabilities.store.disk_used_ratio ?? 0) * 100)}% · {p.capabilities.store.cached_count ?? 0} {t('players.cache_items')}
                </span>
                {p.capabilities.store.storage_pressure && (
                  <span className="inline-flex items-center gap-0.5 text-amber-700 dark:text-amber-300 bg-amber-100 dark:bg-amber-500/20 rounded px-1 py-0.5 text-[10px] font-medium">
                    <AlertTriangle className="w-2.5 h-2.5" /> {t('players.cache_pressure')}
                  </span>
                )}
              </div>
            )}
            {p.tag_filters && p.tag_filters.length > 0 && (
              <div className="pt-0.5">
                <TagBadges ids={p.tag_filters} namespaces={namespaces} max={4} />
              </div>
            )}
          </div>
        </div>
      ))}
    </CardGrid>
  )
}
