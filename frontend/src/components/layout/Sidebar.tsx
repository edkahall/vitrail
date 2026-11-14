'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { clsx } from 'clsx'
import {
  LayoutDashboard,
  Image,
  Monitor,
  ListVideo,
  BarChart2,
  Users,
  Shield,
  UserCircle,
  LayoutTemplate,
  Calendar,
  KeyRound,
  Zap,
  Music,
  FileText,
  Network,
  Tags,
  Building2,
  PlayCircle,
  Globe,
  Rss,
  MessageSquare,
  Bot,
  Sparkles,
  ShieldCheck,
  ChevronDown,
  HardDriveDownload,
  Megaphone,
  RefreshCw,
  Server,
  MapPin,
  Bell,
  BadgeEuro,
  Handshake,
  ClipboardCheck,
  Send,
  BookOpen,
} from 'lucide-react'
import { useTranslations } from 'next-intl'
import { useAuthStore } from '@/store/auth'
import api from '@/lib/api'

interface NavItem {
  href: string
  icon: React.ComponentType<{ className?: string }>
  labelKey: string
  disabled?: boolean
  superAdminOnly?: boolean
  /**
   * Entree pertinente pour un compte PLATEFORME (super-admin). Le menu Reglages du
   * super-admin etait filtre par une liste de chemins CODEE EN DUR : toute nouvelle
   * page plateforme y etait donc invisible sans que rien ne le signale -- c'est ce
   * qui est arrive a une page plateforme (la page existait mais aucun menu n'y
   * menait). Un drapeau porte par l'entree elle-meme
   * ne peut pas se desynchroniser.
   */
  platform?: boolean
  /**
   * Droit `module:operation` requis pour VOIR l'entrée, aligné sur ce que le
   * serveur exige RÉELLEMENT (relevé endpoint par endpoint le 2026-07-20).
   * Absent = visible par tout utilisateur authentifié (profil, manuel).
   */
  perm?: string
  /**
   * Visible si l'utilisateur détient AU MOINS UNE de ces permissions. Sert quand le serveur
   * lui-même accepte plusieurs profils : la file de validation est ouverte au valideur
   * **OU** au soumetteur (`_actor_caps`, approvals.py) — la gater sur le seul
   * `approvals:review` masquait le menu à un admin de nœud qui soumet, donc l'empêchait de
   * suivre SES propres demandes (constaté sur la gouvernance §XVIII).
   */
  anyPerm?: string[]
  /**
   * Entrée réservée aux org-admins. Nécessaire parce que les modules
   * `users`/`groups`/`org_settings`/`api_keys` et une partie de players,
   * campaigns et analytics sont gatés par le booléen `require_org_admin` et non
   * par une permission granulaire — leurs permissions seedées ne sont vérifiées
   * nulle part (27 permissions mortes, audit du 2026-07-20). À faire disparaître
   * quand le RBAC granulaire couvrira ces modules.
   */
  adminOnly?: boolean
  exact?: boolean        // état actif = égalité stricte (cas parent href == href d'un enfant)
  // Destination du CLIC, quand elle diffère du préfixe servant au surlignage. Cas « Rapports » :
  // `href` reste `/reports` pour que toute la section reste surlignée sur ses sous-pages, mais le
  // clic mène au tableau de bord exécutif plutôt qu'aux droits musique (demande Ed 2026-07-19).
  linkHref?: string
  children?: NavItem[]
}

const mainNav: NavItem[] = [
  { href: '/dashboard',  icon: LayoutDashboard, labelKey: 'nav.dashboard', adminOnly: true },
  {
    href: '/media',
    icon: Image,
    labelKey: 'nav.media',
    perm: 'media:read',
    children: [
      { href: '/studio',      icon: Sparkles, labelKey: 'nav.studio', perm: 'media:create' },
      { href: '/promo',       icon: Megaphone, labelKey: 'nav.promo', perm: 'media:create' },
      { href: '/media/tags',  icon: Tags,  labelKey: 'nav.tags', perm: 'media:read' },
      { href: '/url-display', icon: Globe, labelKey: 'nav.url_display', perm: 'media:read' },
      { href: '/feeds',       icon: Rss,   labelKey: 'nav.feeds', perm: 'media:read' },
    ],
  },
  { href: '/templates',  icon: LayoutTemplate,  labelKey: 'nav.templates', perm: 'templates:read' },
  { href: '/playlists',  icon: ListVideo,        labelKey: 'nav.playlists', perm: 'playlists:read' },
  { href: '/audio-baskets', icon: Music,         labelKey: 'nav.audio_baskets', perm: 'audio_baskets:read' },
  { href: '/schedules',  icon: Calendar,         labelKey: 'nav.schedules', perm: 'schedules:read' },
  { href: '/events',     icon: Zap,              labelKey: 'nav.events', perm: 'events:read' },
  { href: '/vod',        icon: PlayCircle,       labelKey: 'nav.vod', perm: 'vod:read' },
  {
    href: '/campaigns',
    icon: BadgeEuro,
    labelKey: 'nav.campaigns',
    perm: 'campaigns:read',
    exact: true,
    children: [
      { href: '/campaigns/advertisers', icon: Handshake, labelKey: 'nav.advertisers', adminOnly: true },
    ],
  },
  // La file de validation ne sert plus seulement les campagnes : depuis la modération du
  // contenu gouverné (§XVIII, D7), une playlist de magasin y passe aussi. La laisser sous
  // « Campagnes » la rendait introuvable pour un responsable de magasin.
  {
    href: '/approvals',
    icon: ClipboardCheck,
    labelKey: 'nav.approvals',
    anyPerm: ['approvals:review', 'approvals:approve', 'approvals:reject', 'approvals:submit'],
  },
  {
    href: '/players',
    icon: Monitor,
    labelKey: 'nav.players',
    perm: 'players:read',
    children: [
      { href: '/sites', icon: Building2, labelKey: 'nav.sites', perm: 'players:read' },
      {
        href: '/settings/players/provisioning',
        icon: KeyRound,
        labelKey: 'nav.provisioning',
        adminOnly: true,
      },
    ],
  },
  { href: '/governance', icon: Network,          labelKey: 'nav.governance', perm: 'governance:read' },
  {
    href: '/reports',
    linkHref: '/reports/executive',
    icon: FileText,
    labelKey: 'nav.reports',
    perm: 'reports:read',
    children: [
      { href: '/reports/executive', icon: BarChart2, labelKey: 'nav.executive_dashboard', perm: 'reports:read' },
      { href: '/reports/noc', icon: Server, labelKey: 'nav.noc', perm: 'reports:read' },
      { href: '/reports/geo', icon: MapPin, labelKey: 'nav.geo', perm: 'reports:read' },
      { href: '/reports/content', icon: PlayCircle, labelKey: 'nav.content_perf', perm: 'reports:read' },
      { href: '/reports/audience', icon: Users, labelKey: 'nav.audience', perm: 'reports:read' },
      { href: '/reports', icon: Music, labelKey: 'nav.rights_usage', exact: true, perm: 'reports:read' },
      { href: '/reports/proof-of-play',  icon: ShieldCheck,  labelKey: 'nav.proof_of_play', perm: 'reports:read' },
    ],
  },
]

const settingsNav: NavItem[] = [
  { href: '/settings/orgs',     icon: Building2,    labelKey: 'nav.organizations', superAdminOnly: true, platform: true },
  // adminOnly : le gate réel est l'EXPLOITANT du serveur, filtré côté serveur
  // (`require_server_operator`) — sur une install autonome l'org-admin EST l'exploitant.
  { href: '/settings/firmware', icon: HardDriveDownload, labelKey: 'nav.firmware', adminOnly: true, platform: true },
  { href: '/settings/feedback', icon: MessageSquare, labelKey: 'nav.feedback', superAdminOnly: true, platform: true },
  { href: '/settings/iam',      icon: Users,        labelKey: 'nav.iam', adminOnly: true },
  // platform : le catalogue des moteurs Electron (récupérer, promouvoir, supprimer) est un geste
  // de l'exploitant du serveur, comme le firmware (14/11 : le super-admin ne trouvait pas la page).
  { href: '/settings/updates',  icon: RefreshCw,    labelKey: 'nav.updates', adminOnly: true, platform: true },
  // Meme gate que Firmware : l'EXPLOITANT est filtre cote serveur
  // (`require_server_operator`). Sur un serveur mutualise, un org-admin d'organisation voit
  // l'entree mais recoit 403 — la page le dit, plutot que de masquer sans expliquer.
  { href: '/settings/server',   icon: Server,       labelKey: 'nav.server', adminOnly: true, platform: true },
  { href: '/settings/notifications', icon: Bell,    labelKey: 'nav.notifications', adminOnly: true },
  { href: '/settings/messaging', icon: Send,        labelKey: 'nav.messaging', adminOnly: true },
  { href: '/settings/ai-keys',  icon: Bot,          labelKey: 'nav.ai_keys', adminOnly: true },
  { href: '/settings/security', icon: Shield,       labelKey: 'nav.security', adminOnly: true },
  { href: '/settings/profile',  icon: UserCircle,   labelKey: 'nav.profile', platform: true },
  { href: '/help',              icon: BookOpen,     labelKey: 'nav.help' },
]

function isHrefActive(pathname: string, href: string) {
  return pathname === href || pathname.startsWith(href + '/')
}

function NavLink({ item, sub = false }: { item: NavItem; sub?: boolean }) {
  const t = useTranslations()
  const pathname = usePathname()
  const isActive = item.exact ? pathname === item.href : isHrefActive(pathname, item.href)
  const Icon = item.icon

  if (item.disabled) {
    return (
      <span className="flex items-center gap-3 px-3 py-2 rounded text-sm text-gray-400 cursor-not-allowed opacity-50">
        <Icon className="w-4 h-4 shrink-0" />
        {t(item.labelKey)}
      </span>
    )
  }

  if (sub) {
    return (
      <Link
        href={item.href}
        className={clsx(
          'flex items-center gap-2 ml-6 px-2 py-1.5 rounded text-xs font-medium transition-sf border-l-2',
          isActive
            ? 'border-accent-hover bg-accent/10 text-white'
            : 'border-transparent text-gray-400 hover:bg-white/5 hover:text-gray-800'
        )}
      >
        <Icon className={clsx('w-3.5 h-3.5 shrink-0', isActive && 'text-accent-hover')} />
        {t(item.labelKey)}
      </Link>
    )
  }

  return (
    <Link
      href={item.href}
      className={clsx(
        'flex items-center gap-3 px-3 py-2 text-sm font-medium transition-sf',
        isActive
          ? 'sf-nav-active rounded-r-lg'
          : 'rounded-lg border-l-[3px] border-transparent text-gray-400 hover:bg-white/5 hover:text-white'
      )}
    >
      <Icon className={clsx('w-4 h-4 shrink-0', isActive && 'text-accent-hover')} />
      {t(item.labelKey)}
    </Link>
  )
}

function NavGroup({ item }: { item: NavItem }) {
  const t = useTranslations()
  const pathname = usePathname()
  const Icon = item.icon
  const isActive = isHrefActive(pathname, item.href)
  const children = item.children ?? []
  const childActive = children.some((c) => isHrefActive(pathname, c.href))

  // Plié par défaut ; on déplie le groupe qui contient la page courante afin que
  // le sous-item actif reste visible (sans rester replié à froid sur une sous-page).
  const [expanded, setExpanded] = useState(childActive)
  useEffect(() => {
    if (childActive) setExpanded(true)
  }, [childActive])

  return (
    <div>
      <div
        className={clsx(
          'flex items-center transition-sf',
          isActive
            ? 'sf-nav-active rounded-r-lg'
            : 'rounded-lg border-l-[3px] border-transparent text-gray-400 hover:bg-white/5 hover:text-white'
        )}
      >
        <Link
          href={item.linkHref ?? item.href}
          className="flex flex-1 items-center gap-3 px-3 py-2 text-sm font-medium min-w-0"
        >
          <Icon className={clsx('w-4 h-4 shrink-0', isActive && 'text-accent-hover')} />
          <span className="truncate">{t(item.labelKey)}</span>
        </Link>
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-label={t(item.labelKey)}
          aria-expanded={expanded}
          className={clsx(
            'shrink-0 px-2 py-2 hover:text-white',
            isActive ? 'text-white/70' : 'text-gray-400'
          )}
        >
          <ChevronDown
            className={clsx(
              'w-4 h-4 transition-transform',
              expanded ? 'rotate-0' : '-rotate-90'
            )}
          />
        </button>
      </div>
      {expanded && (
        <div className="mt-0.5 space-y-0.5">
          {children.map((child) => (
            <NavLink key={child.href} item={child} sub />
          ))}
        </div>
      )}
    </div>
  )
}

export default function Sidebar() {
  const t = useTranslations()
  const isSuperAdmin = useAuthStore((s) => s.hasRole('CMS_SUPER_ADMIN'))
  const isAdmin = useAuthStore((s) => s.isAdmin())
  // S'abonner aux permissions rend la sidebar réactive : sans cette ligne, elle
  // ne se recalculerait pas quand /users/me répond après le rendu initial.
  const permissions = useAuthStore((s) => s.user?.permissions)

  // Version de la release servie par le serveur (endpoint public /meta). Purement
  // informatif — un échec de fetch n'affiche simplement rien (pas de placeholder).
  const [serverVersion, setServerVersion] = useState<string | null>(null)
  useEffect(() => {
    let alive = true
    api
      .get<{ version: string }>('/meta')
      .then((r) => alive && setServerVersion(r.data.version))
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  /**
   * Une entrée n'est affichée que si l'utilisateur peut réellement s'en servir.
   *
   * ⚠️ Masquage = CONFORT, pas sécurité : le serveur reste seul juge (403). On
   * masque parce qu'un menu qui mène systématiquement à un écran vide ou à une
   * erreur est un défaut de produit — constat Ed 2026-07-20, où un Approbateur
   * voyait l'intégralité du CMS et pouvait remplir des formulaires sans effet.
   */
  const visible = (item: NavItem): boolean => {
    if (item.adminOnly && !isAdmin) return false
    if (isAdmin) return true
    const held = permissions ?? []
    if (item.anyPerm) return item.anyPerm.some((p) => held.includes(p))
    if (!item.perm) return true
    return held.includes(item.perm)
  }

  /**
   * Filtre l'arbre de navigation.
   *
   * Cas subtil : un parent INACCESSIBLE dont un enfant l'est (un Approbateur n'a
   * pas `campaigns:read` mais a bien `approvals:review`). Garder le groupe
   * afficherait un en-tête « Campagnes » cliquable menant droit à un 403. On
   * REMONTE donc l'enfant au premier niveau — l'utilisateur ne voit que ce qu'il
   * peut réellement ouvrir.
   */
  const filterTree = (items: NavItem[]): NavItem[] =>
    items.flatMap((item) => {
      const children = item.children?.filter(visible)
      if (visible(item)) return [{ ...item, children }]
      return (children ?? []).map((c) => ({ ...c, children: undefined }))
    })

  // Le super-admin est un compte de provisioning plateforme : pas de contenu, pas
  // d'IAM d'org — uniquement la gestion des organisations + son propre profil.
  const mainItems = isSuperAdmin ? [] : filterTree(mainNav)
  const settings = isSuperAdmin
    ? settingsNav.filter((i) => i.platform)
    : settingsNav.filter((i) => !i.superAdminOnly && visible(i))

  return (
    <nav
      className="w-60 shrink-0 h-screen flex flex-col bg-sidebar text-white overflow-y-auto border-r border-white/5 shadow-[10px_0_30px_rgba(0,0,0,0.5)] relative z-20"
      aria-label="Main navigation"
    >
      {/* Marque : logo Vitrail (panneaux de vitrail = zones d'un écran) + nom. */}
      <div className="flex items-center gap-3 px-5 h-16 shrink-0 border-b border-white/5">
        {/* eslint-disable-next-line @next/next/no-img-element -- SVG statique, rien à optimiser */}
        <img src="/brand/vitrail-logo.svg" alt="" aria-hidden width={32} height={32} className="shrink-0" />
        <span className="font-semibold text-white tracking-wide">Vitrail</span>
      </div>

      {/* Main nav */}
      <div className="flex-1 px-3 py-4 space-y-0.5">
        {mainItems.map((item) =>
          item.children ? (
            <NavGroup key={item.href} item={item} />
          ) : (
            <NavLink key={item.href} item={item} />
          )
        )}
      </div>

      {/* Settings nav */}
      <div className="px-3 pb-4 border-t border-white/5 pt-3 space-y-0.5">
        <p className="px-3 py-1 text-[10px] font-semibold text-gray-400/70 uppercase tracking-wider">
          {t('nav.settings')}
        </p>
        {settings.map((item) => (
          <NavLink key={item.href} item={item} />
        ))}
        {serverVersion && (
          <p
            className="px-3 pt-2 text-center text-[10px] text-gray-400/60 tracking-wider select-none"
            title={t('nav.serverVersionTitle')}
          >
            {t('nav.serverVersion', { version: serverVersion })}
          </p>
        )}
      </div>
    </nav>
  )
}
