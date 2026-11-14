'use client'

import { useEffect, useState } from 'react'
import { AlertTriangle, Check, ChevronDown, ChevronRight, RefreshCw, Server } from 'lucide-react'

import Button from '@/components/ui/Button'
import type { PlayerRead } from '@/types/api'

type T = (key: string, values?: Record<string, string | number>) => string

// OS dont le player sait appliquer `set_timezone` : Electron (timedatectl / tzutil),
// Android Device Owner (dpm.setTimeZone, relance app), BrightSign (roSystemTime + reboot).
// ChromeOS/extension : TZ pilotée par le compte/la politique Google — non modifiable ici.
const TZ_CAPABLE_OS = new Set(['linux', 'linux-x64', 'linux-arm64', 'rpi', 'windows', 'brightsign'])
const tzCapable = (os: string | null) => !!os && (TZ_CAPABLE_OS.has(os) || os.startsWith('android'))

// Liste IANA complète du navigateur (Chrome ≥ 99). Indisponible → édition masquée.
function browserTimezones(): string[] {
  try {
    const intl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] }
    if (typeof intl.supportedValuesOf === 'function') return intl.supportedValuesOf('timeZone')
  } catch { /* navigateur ancien */ }
  return []
}

// Windows ne connaît que ~140 zones larges : `tzutil` applique la zone Windows englobante,
// puis l'OS remonte son représentant CANONIQUE (ex. demande Europe/Monaco → applique
// « W. Europe Standard Time » → remonte Europe/Berlin — heure identique, nom différent :
// surprise vécue par Ed sur l'AOpen). Pour éviter ça, un player Windows ne propose QUE les
// zones canoniques (une par zone Windows de la table du player) → remontée = demande.
// BrightSign : le BOS n'accepte QUE ses libellés natifs (roSystemTime.SetTimeZone —
// liste doc BrightSign ; IANA refusé, constaté HW HD1024 avec Europe/Paris). Le select
// propose donc ces libellés tels quels. GMT±N = offset fixe SANS heure d'été.
// ⚠️ Liste VALIDÉE HW seulement : CET et GMT confirmés (reboot + apply) sur BOS 9.1.132 ;
// les variantes « GMT+N » de la doc ont été REFUSÉES par le BOS (test Ed GMT+2, pas de
// reboot) → retirées tant qu'on n'a pas le format exact. Les autres libellés nommés
// viennent de la doc officielle (même famille que CET/GMT — plausibles, à confirmer HW).
const BRIGHTSIGN_ZONES = [
  'CET', 'EET', 'WET', 'GMTBST', 'GMT', 'MSK',
  'EST', 'CST', 'MST', 'PST', 'AKST', 'HST',
  'IST', 'JST', 'AWST', 'ACST', 'AEST', 'NZST',
]
// Équivalences libellé BrightSign ↔ zones IANA (badge mismatch : un BS en « CET » sur un
// site « Europe/Paris » est ALIGNÉ). Volontairement les zones plausibles côté sites.
const BS_TZ_EQUIV: Record<string, string[]> = {
  CET: ['Europe/Paris', 'Europe/Berlin', 'Europe/Brussels', 'Europe/Amsterdam', 'Europe/Madrid',
        'Europe/Rome', 'Europe/Vienna', 'Europe/Zurich', 'Europe/Stockholm', 'Europe/Oslo',
        'Europe/Copenhagen', 'Europe/Warsaw', 'Europe/Prague', 'Europe/Budapest', 'Europe/Monaco',
        'Europe/Luxembourg'],
  EET: ['Europe/Athens', 'Europe/Bucharest', 'Europe/Helsinki', 'Europe/Kyiv', 'Europe/Riga',
        'Europe/Vilnius'],
  WET: ['Europe/Lisbon'],
  GMTBST: ['Europe/London', 'Europe/Dublin'],
  MSK: ['Europe/Moscow'],
  EST: ['America/New_York', 'America/Toronto'],
  CST: ['America/Chicago'],
  MST: ['America/Denver'],
  PST: ['America/Los_Angeles', 'America/Vancouver'],
  JST: ['Asia/Tokyo'],
  IST: ['Asia/Kolkata'],
  AEST: ['Australia/Sydney', 'Australia/Melbourne'],
  AWST: ['Australia/Perth'],
  NZST: ['Pacific/Auckland'],
  GMT: ['UTC', 'Etc/UTC'],
}

const WINDOWS_CANONICAL_ZONES = [
  'UTC',
  'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Europe/Warsaw', 'Europe/Prague',
  'Europe/Athens', 'Europe/Helsinki', 'Europe/Istanbul', 'Europe/Moscow',
  'Africa/Casablanca', 'Africa/Cairo', 'Africa/Lagos', 'Africa/Nairobi', 'Africa/Johannesburg',
  'Asia/Dubai', 'Asia/Riyadh', 'Asia/Jerusalem', 'Asia/Karachi', 'Asia/Kolkata', 'Asia/Dhaka',
  'Asia/Bangkok', 'Asia/Singapore', 'Asia/Shanghai', 'Asia/Taipei', 'Asia/Tokyo', 'Asia/Seoul',
  'Australia/Perth', 'Australia/Adelaide', 'Australia/Brisbane', 'Australia/Sydney',
  'Pacific/Auckland', 'Pacific/Honolulu',
  'America/New_York', 'America/Chicago', 'America/Denver', 'America/Phoenix',
  'America/Los_Angeles', 'America/Anchorage', 'America/Mexico_City', 'America/Bogota',
  'America/Santiago', 'America/Sao_Paulo', 'America/Argentina/Buenos_Aires',
]

function Row({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-gray-400">{label}</dt>
      <dd className="font-medium text-gray-700 dark:text-gray-300 break-all">{value}</dd>
    </>
  )
}

/**
 * Section « Appareil » du drawer player (P1.3). Infos device : OS / IP / MAC pour tous,
 * modèle / firmware / série / mode vidéo pour BrightSign (servis par l'autorun), et le
 * FUSEAU HORAIRE OS réel remonté au heartbeat (capabilities.timezone) — avec détection
 * de mismatch vs le fuseau du site (le pilotage offline auto-power suit l'horloge du
 * device : un clone mal fuselé serait décalé hors connexion) + modification à distance
 * (commande `set_timezone`, le player relance son app pour appliquer).
 */
export default function DeviceInfoSection({ player, t, siteTimezone, onSetTimezone }: {
  player: PlayerRead
  t: T
  siteTimezone?: string | null
  onSetTimezone?: (tz: string) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [tzDraft, setTzDraft] = useState('')
  // Feedback visuel du set_timezone (retour Ed) : fuseau demandé en attente de confirmation.
  // Confirmé quand le player (redémarré) remonte ce fuseau au heartbeat (player = donnée LIVE,
  // refetch 10 s côté parent) ; abandon silencieux après 2 min (commande perdue/refusée).
  const [sentTz, setSentTz] = useState<string | null>(null)
  const caps = player.capabilities
  const isBs = (player.player_os === 'brightsign') || !!caps?.bs_model || caps?.platform === 'brightsign'

  const deviceTz = caps?.timezone ?? null
  // Aligné = identique, OU libellé BrightSign équivalent à la zone IANA du site (CET ≡ Europe/Paris).
  const tzEquivalent = !!(deviceTz && siteTimezone
    && (deviceTz === siteTimezone || (BS_TZ_EQUIV[deviceTz] ?? []).includes(siteTimezone)))
  const tzMismatch = !!(deviceTz && siteTimezone && !tzEquivalent)
  const zones = player.player_os === 'windows' ? WINDOWS_CANONICAL_ZONES
    : player.player_os === 'brightsign' ? BRIGHTSIGN_ZONES
    : browserTimezones()
  const canSetTz = !!onSetTimezone && tzCapable(player.player_os) && zones.length > 0
  // Pré-sélection : le fuseau du site si mismatch (cas d'usage n°1 : réaligner), sinon l'actuel.
  const chosen = tzDraft || (tzMismatch ? siteTimezone ?? '' : deviceTz ?? '')

  const tzPending = !!sentTz && deviceTz !== sentTz
  const tzConfirmed = !!sentTz && deviceTz === sentTz
  useEffect(() => {
    if (!sentTz) return
    // Confirmé → laisser la coche 5 s puis nettoyer ; jamais confirmé → abandon après 2 min.
    const id = setTimeout(() => setSentTz(null), deviceTz === sentTz ? 5000 : 120000)
    return () => clearTimeout(id)
  }, [sentTz, deviceTz])

  return (
    <div className="pt-2 border-t border-gray-100">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center justify-between gap-2 text-left"
      >
        <span className="text-xs font-medium text-gray-500 uppercase tracking-wide flex items-center gap-1.5">
          {expanded ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
          <Server className="w-3.5 h-3.5 text-gray-400" />
          {t('players.device.title')}
          {!expanded && tzMismatch && <AlertTriangle className="w-3.5 h-3.5 text-amber-500" />}
        </span>
        {isBs && caps?.bs_model && (
          <span className="text-xs text-gray-400">{caps.bs_model}</span>
        )}
      </button>

      {expanded && (
        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs text-gray-600">
          {player.player_os && <Row label={t('players.device.os')} value={player.player_os} />}
          {player.player_version && <Row label={t('players.device.version')} value={`v${player.player_version}`} />}
          {caps?.electron_version && <Row label={t('players.device.electron')} value={caps.electron_version} />}
          {player.ip_address && <Row label={t('players.device.ip')} value={player.ip_address} />}
          {player.mac_address && <Row label={t('players.device.mac')} value={player.mac_address} />}
          {deviceTz && (
            <>
              <dt className="text-gray-400">{t('players.device.timezone')}</dt>
              <dd className="font-medium text-gray-700 dark:text-gray-300 break-all">
                <span className="inline-flex items-center gap-1.5 flex-wrap">
                  {deviceTz}
                  {tzMismatch && (
                    <span className="inline-flex items-center gap-1 text-amber-600">
                      <AlertTriangle className="w-3 h-3" />
                      {t('players.device.tz_mismatch', { site: siteTimezone ?? '' })}
                    </span>
                  )}
                  {tzEquivalent && (
                    <Check className="w-3 h-3 text-green-600" aria-label={t('players.device.tz_ok')} />
                  )}
                </span>
              </dd>
            </>
          )}
          {isBs && caps?.bs_model && <Row label={t('players.device.model')} value={caps.bs_model} />}
          {isBs && caps?.bs_bos && <Row label={t('players.device.firmware')} value={caps.bs_bos} />}
          {isBs && caps?.bs_serial && <Row label={t('players.device.serial')} value={caps.bs_serial} />}
          {isBs && caps?.bs_video_mode && <Row label={t('players.device.video_mode')} value={caps.bs_video_mode} />}
          {isBs && caps?.bs_best_mode && <Row label={t('players.device.best_mode')} value={caps.bs_best_mode} />}
          {isBs && caps?.bs_autorun_ver && <Row label={t('players.device.autorun_ver')} value={caps.bs_autorun_ver} />}
        </dl>
      )}

      {expanded && canSetTz && (
        <div className="mt-2">
          <div className="flex items-center gap-2">
            <select
              value={chosen}
              onChange={(e) => setTzDraft(e.target.value)}
              disabled={tzPending}
              className="flex-1 min-w-0 text-xs border border-gray-200 rounded-md px-2 py-1.5 bg-surface text-gray-700 disabled:opacity-60"
              aria-label={t('players.device.timezone')}
            >
              {/* Valeur courante hors liste (ex. Etc/Unknown, libellé exotique) : l'injecter
                  en option explicite — sinon le navigateur AFFICHE silencieusement la 1re
                  option de la liste (artefacts « Africa/Abidjan » / « CET » vus par Ed). */}
              {chosen && !zones.includes(chosen) && (
                <option value={chosen}>{chosen}</option>
              )}
              {zones.map((z) => (
                <option key={z} value={z}>{z}</option>
              ))}
            </select>
            <Button
              size="sm"
              loading={tzPending}
              disabled={tzPending || !chosen || chosen === deviceTz}
              onClick={() => { onSetTimezone?.(chosen); setSentTz(chosen) }}
            >
              {t('players.device.tz_apply')}
            </Button>
          </div>
          {tzPending && (
            <p className="mt-1 text-[11px] text-amber-600 inline-flex items-center gap-1">
              <RefreshCw className="w-3 h-3 animate-spin" />
              {t('players.device.tz_pending', { tz: sentTz ?? '' })}
            </p>
          )}
          {tzConfirmed && (
            <p className="mt-1 text-[11px] text-green-600 inline-flex items-center gap-1">
              <Check className="w-3 h-3" />
              {t('players.device.tz_applied', { tz: sentTz ?? '' })}
            </p>
          )}
          {!tzPending && !tzConfirmed && (
            <p className="mt-1 text-[11px] text-gray-400">{t('players.device.tz_hint')}</p>
          )}
        </div>
      )}
    </div>
  )
}
