export interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
  locale: string
}

export interface AccessTokenResponse {
  access_token: string
  token_type: string
}

export interface UserRead {
  id_user: number
  id_org: number
  email: string
  full_name: string | null
  avatar_url: string | null
  totp_enabled: boolean
  locale: string
  theme?: string
  is_active: boolean
  is_demo?: boolean
  // Portail annonceur (S7 B2) : non-null = compte externe → UI /portal, pas le CMS
  id_advertiser?: number | null
  last_login_at: string | null
  created_at: string
  org_name?: string | null
  /** Peuplés par /users/me seulement — droits effectifs pour le masquage UI. */
  roles?: string[]
  permissions?: string[]
}

/** Entrée du catalogue de permissions atomiques (§II.1), pour l'éditeur de rôles. */
export interface PermissionRead {
  id_permission: number
  module: string
  operation: string
  description?: string | null
  /** false = fonctionnalité spécifiée mais non développée (`spf:*`) → non cochable. */
  available: boolean
}

export interface MediaListItem {
  id_media: number
  media_name: string
  media_type: string
  mime_type: string | null
  file_size_bytes: number | null
  /** Taille du fichier réellement SERVI (après re-encode) — null si pas encore transcodé */
  served_size_bytes?: number | null
  duration_ms: number | null
  thumbnail_url: string | null
  source: string
  attribution_text: string | null
  /** `awaiting_decision` : l'étalonnage a jugé l'encodage trop long, rien ne se lance
   *  tant qu'un humain n'a pas tranché (décision Ed — le média reste visible et compté). */
  transcode_status: 'pending' | 'processing' | 'done' | 'error' | 'awaiting_decision'
  /** Renseigné uniquement en `awaiting_decision` : ce que l'étalonnage a mesuré. */
  encode_decision?: EncodeDecision | null
  /** Passthrough / conformité (§XVI.2) */
  encode_mode: 'auto' | 'passthrough'
  conformance_status: 'ready' | 'playable' | 'nonconform' | null
  /** Embargo — null si pas d'embargo */
  not_before_utc: string | null
  embargo_event_id: number | null
  embargo_event_name: string | null
  /** id_tags (§XVII) — pour les badges de la Media Library */
  tags: number[]
  /** Dossier de classement (null = racine / non classé) */
  id_folder: number | null
  /** Quarantaine / kill-switch (§ Q5) — bloqué = jamais diffusé */
  is_blocked?: boolean
  block_reason?: string | null
  /** Renditions adaptatives (« artefacts » vidéo) présentes sur ce parent */
  has_renditions?: boolean
  /** Pour un média pointeur feed_display → id du feed (ouvre l'éditeur de feed). */
  id_feed?: number | null
  /** Document multipage converti (PDF/PPT/PPTX) : nb de pages — badge « N pages » */
  page_count?: number | null
  /** Gouvernance §XVIII contenu (D9) : nœud propriétaire (null = média d'organisation). */
  id_governance_node?: number | null
  /** Gouverné par un nœud non éditable → toute mutation est refusée (grisage front). */
  governed_readonly?: boolean
  /** Date d'ajout — colonne triable de la vue liste. */
  created_at: string
  /** Avancement de l'encodage EN COURS (barre de progression). Absent dès que l'encodage
   *  est terminé — ou que le worker est mort : dans les deux cas on retombe sur le statut
   *  seul, sans chiffre trompeur. Jamais persisté côté serveur (Redis + TTL). */
  encode_progress?: EncodeProgress | null
}

/** Verdict de l'étalonnage quand l'encodage dépasse une bande (cf. `awaiting_decision`). */
export interface EncodeDecision {
  /** `confirm` = 20 min–4 h, l'utilisateur choisit · `refuse` = > 4 h, org-admin seulement. */
  band: 'confirm' | 'refuse'
  /** Durée estimée de l'encodage complet, MESURÉE sur une tranche de la vraie source. */
  estimate_s: number
  /** Vitesse constatée pendant la mesure (1 = temps réel). */
  measured_speed?: number | null
  width?: number | null
  height?: number | null
  encoder?: string
  /** Replis proposés, avec leur propre estimation — ce qui transforme un refus en choix. */
  downscale: { profile: string; box: number; estimate_s: number }[]
  resolved?: boolean
}

/** Avancement publié par le worker pendant un encodage ffmpeg. */
export interface EncodeProgress {
  /** download = récupération du fichier depuis le stockage · encode = ffmpeg en cours
   *  · finalize = upload + conformité après l'encodage · rendition = artefact adaptatif */
  phase: 'download' | 'encode' | 'finalize' | 'rendition'
  /** Absent quand la durée de la source est inconnue : on montre une activité, pas un chiffre. */
  pct?: number
  /** Secondes restantes estimées à la vitesse constatée à l'instant. */
  eta_s?: number
  /** Vitesse ffmpeg (1 = temps réel). Chute quand plusieurs encodages se disputent la machine. */
  speed?: number
  /** Profil de l'artefact en cours (phase `rendition`). */
  profile?: string
  at: number
}

/** Comptes par type sur toute la bibliothèque (GET /media/stats — indépendant de la pagination). */
export interface MediaStats {
  total: number
  by_type: Record<string, number>
}

/** État de diffusion d'une playlist/programme dans l'analyse d'impact de suppression. */
export type BroadcastState = 'active' | 'scheduled' | 'expired' | 'inactive' | 'none'

/** Arbre d'impact de la suppression d'un média (GET /media/{id}/deletion-impact). */
export interface MediaDeletionImpact {
  media: { id_media: number; media_name: string; media_type: string }
  playlists: {
    id_playlist: number
    playlist_name: string
    items_using: number
    remaining_items: number
    will_be_deleted: boolean
    broadcast: BroadcastState
  }[]
  schedules: {
    id_schedule: number
    schedule_name: string
    broadcast: BroadcastState
    will_be_deleted: boolean
  }[]
  events: {
    id_event: number
    event_name: string
    status: string
    reason: 'media' | 'playlist'
    will_be_cancelled: boolean
  }[]
  baskets: { id_basket: number; basket_name: string; items_using: number }[]
  summary: {
    playlists_total: number
    playlists_deleted: number
    schedules_deleted: number
    events_cancelled: number
    baskets_affected: number
    has_live_broadcast: boolean
  }
}

/** Artefact (rendition adaptative) d'un média parent. */
export interface RenditionRead {
  id_rendition: number
  id_parent: number
  profile: string // fhd | hd720 | uhd
  width: number | null
  height: number | null
  codec: string | null
  file_size_bytes: number | null
  conformance_status: 'ready' | 'playable' | 'nonconform' | null
  status: 'pending' | 'processing' | 'ready' | 'failed'
  error_detail: string | null
  created_at: string
  updated_at: string
}

/** Dossier de classement des médias (arborescence path matérialisé). */
export interface FolderRead {
  id_folder: number
  id_org: number
  parent_id: number | null
  folder_name: string
  depth: number
  path: string
  child_count: number
  media_count: number
  /** Chemin lisible ancêtres compris (« Video / Divers ») — lève l'ambiguïté des homonymes. */
  full_path: string
  /** System folder (`advertisers`): visible name translated by `useFolderLabel`, cannot be renamed or moved. */
  system_key?: string | null
  created_at: string
}

export interface MediaRead extends MediaListItem {
  id_org: number
  duration_ms: number | null
  width: number | null
  height: number | null
  isrc: string | null
  rights_metadata: Record<string, unknown>
  /** Métadonnées techniques (§6) : {container, size_bytes, video, audio, subtitles} */
  media_metadata: Record<string, unknown>
  cdn_url_signed: string | null
  updated_at: string
}

export interface StockSearchResult {
  provider: string
  provider_id: string
  title: string | null
  description: string | null
  preview_url: string
  full_url: string
  attribution_text: string
  photographer: string
  photographer_url: string | null
  width: number | null
  height: number | null
}

export interface GroupRead {
  id_group: number
  id_org: number
  group_name: string
  description: string | null
  scope_type: string
  scope_id: number | null
  is_active: boolean
  created_at: string
}

export interface RoleRead {
  id_role: number
  id_org: number | null
  role_key: string
  label: string
  description: string | null
  is_system: boolean
  is_active: boolean
}

export interface TemplateRead {
  id_template: number
  id_org: number
  template_name: string
  description: string | null
  layout: {
    layout_type: string
    canvas_width?: number
    canvas_height?: number
    zones: { index: number; x: number; y: number; w: number; h: number; label: string }[]
  }
  preview_url: string | null
  /**
   * P3.6 (migr 0124) — ou s'affiche un spot de campagne sur un ecran MULTI-ZONES.
   * `fullscreen` = le spot recouvre tout l'ecran (seul mode coherent avec la tarification
   * au SOV) · `zone` = il s'insere dans la zone principale, les autres continuent.
   */
  campaign_placement?: 'fullscreen' | 'zone'
  /** Gouvernance §XVIII (migr 0112) : noeud proprietaire. null = template d'organisation. */
  id_governance_node?: number | null
  /** Gouverné par un nœud non administré → toute mutation part en 403 (grisage front). */
  governed_readonly?: boolean
  created_at: string
  updated_at: string
}

export interface PlaylistItemRead {
  id_item: number
  id_playlist: number
  id_media: number | null
  /** media | sub_playlist (playlist imbriquée — rotation) */
  item_type?: 'media' | 'sub_playlist'
  id_child_playlist?: number | null
  /** Enrichissement sub_playlist : nom + nb d'items + réglages rotation de l'enfant. */
  child_name?: string | null
  child_item_count?: number | null
  child_rotation_mode?: string | null
  child_items_per_turn?: number | null
  sort_order: number
  duration_ms: number | null
  zone_index: number
  transition_type: string
  created_at: string
  updated_at: string
  media_name: string | null
  media_type: string | null
  /** Conformité du média (§XVI.2) */
  conformance_status?: 'ready' | 'playable' | 'nonconform' | null
  /** True si l'item porte ≥1 règle de diffusion (item time-boxé) ; false = perpétuel. */
  has_rules?: boolean
  /** Documents multipages : sélection "2-5,8" (null = toutes) — duration_ms = durée PAR PAGE */
  doc_pages?: string | null
  /** Nb de pages du média document converti (null = pas un document / pas converti) */
  media_page_count?: number | null
  /** Durée NATIVE du média (extraite à l'upload, vidéo/audio) — affichée quand
   *  duration_ms (override manuel) est null. */
  media_duration_ms?: number | null
  /** Document multipage : le visiteur peut feuilleter au doigt (migr 0129). Sans effet hors
   *  document et sur un écran qui ne remonte aucun point de contact. */
  touch_navigation?: boolean
  /** Délai sans interaction avant de quitter le document (null = défaut du player, 60 s). */
  touch_resume_after_s?: number | null
}

/** Page convertie d'un document multipage (GET /media/{id}/pages). */
export interface DocumentPageRead {
  page: number
  url: string
  width: number | null
  height: number | null
}

export interface DocumentPagesResult {
  id_media: number
  page_count: number
  truncated: boolean
  pages: DocumentPageRead[]
}

/** Règle de diffusion d'un item (heure locale). Miroir de ScheduleRuleRead + scope. */
export interface ItemRule {
  rule_type: 'always' | 'weekly' | 'date_range'
  days_of_week: number[] | null
  time_start: string | null
  time_end: string | null
  date_from: string | null
  date_to: string | null
  scope: 'default' | 'player' | 'player_group'
  target_id: number | null
}

export interface ItemRuleRead extends ItemRule {
  id_item_rule: number
  id_item: number
}

export interface PlaylistRead {
  id_playlist: number
  id_org: number
  playlist_name: string
  description: string | null
  id_template: number | null
  sync_mode: string
  total_duration_ms: number | null
  /** Nombre d'items (sous-playlists comptées 1) — enrichi par l'endpoint liste. */
  item_count?: number | null
  /** Synchronisable LAN/wall-clock : sync_mode==wall_clock + tous les items ont une durée résolue. */
  sync_ready: boolean
  /** True si ≥1 item porte une règle de diffusion (badge « Programmé »). */
  has_item_rules?: boolean
  /** « Synchronisable à fenêtres » : sync_ready + règles toutes scope=default
   *  (jeu d'items player-uniforme, re-sync graceful aux frontières). */
  sync_windowed?: boolean
  /** Staging (#1) : true si la playlist a un brouillon d'édition non publié. */
  has_draft_changes?: boolean
  /** Rotation (sous-playlist « N items par tour ») : all | one_sequential | one_random. */
  rotation_mode?: 'all' | 'one_sequential' | 'one_random'
  items_per_turn?: number
  /** « Pas de publicité entre les contenus » (effet en sous-playlist uniquement). */
  no_ads_inside?: boolean
  /** True si référencée comme sous-playlist par ≥1 parent (suppression bloquée). */
  used_as_child?: boolean
  /** Gouvernance §XVIII contenu : nœud propriétaire (null = playlist d'organisation). */
  id_governance_node?: number | null
  /** Gouvernée par un nœud que l'utilisateur ne peut PAS éditer (visible, D6) → grisage. */
  governed_readonly?: boolean
  /** Le nœud exige la modération : publication via §XXI (« Soumettre à l'approbation »). */
  requires_approval?: boolean
  created_at: string
  updated_at: string
}

export interface PlaylistDetail extends PlaylistRead {
  items: PlaylistItemRead[]
  /** Modération §XVIII/§XXI : demande d'approbation ACTIVE (null = aucune). */
  approval_status?: 'pending_review' | 'changes_requested' | 'approved' | null
  approval_request_id?: number | null
  /** Durée max par contenu imposée par la chaîne de gouvernance (secondes), ou null. */
  governance_max_item_sec?: number | null
  /** Contenus de la playlist qui la dépassent, NOMMÉS — avertissement au chargement de
   *  l'éditeur (décision Ed 2026-07-26) ; l'exclusion réelle reste à la compilation. */
  governance_too_long?: string[]
}

export interface ScheduleRuleRead {
  id_rule: number
  id_schedule: number
  rule_type: string
  days_of_week: number[] | null
  time_start: string | null
  time_end: string | null
  date_from: string | null
  date_to: string | null
}

export interface ScheduleTargetRead {
  id_target: number
  id_schedule: number
  target_type: string
  target_id: number
}

export interface ScheduleRead {
  id_schedule: number
  id_org: number
  schedule_name: string
  schedule_kind: 'content' | 'audio'
  id_playlist: number | null
  /** Nom résolu par le serveur — le client ne doit plus le déduire d'une liste paginée. */
  playlist_name: string | null
  /** Nœud de gouvernance propriétaire (§XVIII). null = pas de contrainte. */
  id_governance_node: number | null
  priority: number
  is_active: boolean
  /** Plage de validité (dates début/fin, heure locale). null = borne ouverte. */
  valid_from: string | null
  valid_until: string | null
  created_at: string
  updated_at: string
  /** Règles de récurrence (always / weekly) — peuplées par la liste pour la vue calendrier. */
  rules: ScheduleRuleRead[]
}

export interface ScheduleBasket {
  id_basket: number
  mix_ratio: number
  basket_name?: string | null
  basket_type?: string | null
}

export interface ScheduleDetail extends ScheduleRead {
  rules: ScheduleRuleRead[]
  targets: ScheduleTargetRead[]
  baskets: ScheduleBasket[]
}

// Simulation « journée » d'un player (programmation complète : programmes + events + audio)
export interface DaySimVideoSegment {
  start: string            // "HH:MM"
  end: string
  source: 'schedule' | 'event'
  name: string
  id_playlist: number | null
  playlist_name: string | null
  priority: number
}
export interface DaySimBasket {
  id_basket: number
  name: string
  basket_type: string
  ratio: number
  source: 'schedule' | 'event'
  /** Cadence des jingles : 'tracks' | 'minutes' (null pour music/ambiance) */
  trigger_mode?: string | null
  trigger_value?: number | null
}
export interface DaySimAudioSegment {
  start: string
  end: string
  baskets: DaySimBasket[]
}
export interface PlayerDaySimResult {
  id_player: number
  date: string
  /** Fuseau dans lequel les horaires ci-dessous se lisent = celui du site de l'ecran. */
  timezone: string
  video: DaySimVideoSegment[]
  audio: DaySimAudioSegment[]
}

// ── Sites & auto-power écrans ────────────────────────────────────────────────
export interface SiteHoursItem {
  weekday: number            // 0=Lun … 6=Dim
  is_closed: boolean
  open_time: string | null   // 'HH:MM[:SS]'
  close_time: string | null
}

export interface SiteException {
  id_exception: number
  id_site: number
  date_from: string          // 'YYYY-MM-DD'
  date_to: string
  kind: 'closed' | 'hours' | 'open_24h'
  open_time: string | null
  close_time: string | null
  label: string | null
}

export interface SitePlayer {
  id_player: number
  player_name: string
  player_os: string | null
  status: string
  last_power_state: boolean | null
  power_mode: string         // follow_site | always_on | custom | manual
}

export interface Site {
  id_site: number
  id_org: number
  site_name: string
  address: string | null
  country: string | null
  site_number: string | null
  latitude: number | null
  longitude: number | null
  timezone: string | null
  ntp_server: string | null
  auto_power_enabled: boolean
  power_lead_min: number
  power_lag_min: number
  respect_holidays: boolean
  holiday_region: string
  /** Inventaire v2 (0082) : plafond campagnes (%) des players du site — null = pas de plafond */
  campaign_max_occupancy_pct?: number | null
  /** Inventaire v2 (0082) : tarif SOV du site (€ / 1 % / jour) — null = défaut org */
  campaign_sov_daily_rate?: number | null
  /** Exclusion concurrentielle (0087) : catégories d'industrie exclues du site */
  campaign_excluded_categories?: number[]
  created_at: string
  player_count: number
  hours: SiteHoursItem[]
}

export interface SiteGroupRead {
  id_site_group: number
  id_org: number
  group_name: string
  description: string | null
  created_at: string
  updated_at: string
  site_count: number
  player_count: number
  /** Sites membres — peuplé par GET /site-groups/ (agrégat, pas une colonne). */
  site_ids: number[]
}

export interface CurrentScheduleResponse {
  id_player: number
  id_schedule: number | null
  id_playlist: number | null
  playlist_name: string | null
  schedule_name: string | null
  priority: number | null
  resolved_at: string
  /** Fuseau dans lequel `resolved_at` se lit = celui du site de l'ecran. */
  timezone: string
}

/** Etape de la sequence de demarrage de l'ecran (executee par le player localement). */
export interface DisplaySequenceStep {
  /** Commande a envoyer (power_on, set_input, set_brightness, mute_off, etc.) */
  command: string
  /** Parametres de la commande (source, level...) */
  params?: Record<string, unknown>
  /** Delai en secondes avant d'executer cette etape (0 = immediat) */
  delay_before_s?: number
}

/**
 * Programmation horaire d'allumage/extinction de l'ecran physique.
 * Stockee dans display_config.schedule, executee localement par le player.
 * Fonctionne meme si le player est hors ligne (config persistee sur disque).
 */
export interface DisplaySchedule {
  /** Heure d'allumage au format HH:MM (24h). Absent = pas d'allumage programme. */
  power_on?: string | null
  /** Heure d'extinction au format HH:MM (24h). Absent = pas d'extinction programmee. */
  power_off?: string | null
  /** Jours actifs ISO : 1=Lun...7=Dim. Absent ou vide = tous les jours. */
  days?: number[] | null
  /** false = pulse desactive manuellement. undefined/true = actif si horaires configures. */
  pulse_enabled?: boolean
  /** Intervalle en secondes entre deux commandes pulse (0 ou absent = pas de pulse). */
  pulse_interval_s?: number | null
}

/** Configuration de l'ecran physique connecte au player (RS232 ou IP). */
/** Mur d'écrans (video wall) — stocké par player dans display_config.wall. Compensation de
 *  bezel calculée serveur (services/video_wall.py). Mode B (overscan) = défaut ; A (crop). */
export interface WallConfig {
  mode?: 'overscan' | 'crop'
  rows: number
  cols: number
  /** Cellule (0-indexée) occupée par CE player dans la grille. */
  cell: { row: number; col: number }
  /** Mode A (crop) : toutes les cellules du groupe (row/col + dalle/bezels) — le serveur en
   *  déduit le sous-rectangle source de CE player. Inutile en mode B (overscan). */
  cells?: Array<{
    row: number
    col: number
    panel_w_mm?: number
    panel_h_mm?: number
    bezel_left_mm?: number
    bezel_right_mm?: number
    bezel_top_mm?: number
    bezel_bottom_mm?: number
  }>
  panel_w_mm?: number
  panel_h_mm?: number
  bezel_left_mm?: number
  bezel_right_mm?: number
  bezel_top_mm?: number
  bezel_bottom_mm?: number
  /** Multiplicateur de compensation (fine-tune ; 1.0 = géométrique exact). */
  gap_scale?: number
}

export interface DisplayConfig {
  type: 'rs232' | 'ip' | 'cec' | null
  /** RS232 : chemin du port serie (ex: /dev/ttyUSB0, COM3) */
  serial_port?: string | null
  /** RS232 : vitesse de communication */
  baud_rate?: number | null
  /** IP : adresse de l'ecran */
  host?: string | null
  /** IP : port TCP (Samsung MDC = 1515, LG Telnet = 9761) */
  tcp_port?: number | null
  /** CEC : peripherique kernel CEC (linux/rpi, defaut /dev/cec0). BrightSign = roCecInterface. */
  cec_device?: string | null
  /** Ecran NON pilotable (pas de RS232/IP/CEC) : power_on/power_off coupent/retablissent la
   *  SORTIE VIDEO du player lui-meme (DPMS / wlr-randr / SC_MONITORPOWER / veille Android /
   *  SetPowerSaveMode BrightSign). Opt-in : requis pour l'auto-power heures d'ouverture. */
  output_power_fallback?: boolean | null
  /** Page locale /control (BrightSign a entree HDMI) : false = desactivee. Defaut = active. */
  local_control?: boolean | null
  /** PIN optionnel (4-6 chiffres) exige par la page locale /control (valide dans l'autorun). */
  local_control_pin?: string | null
  /** Protocole constructeur : samsung_mdc | lg_rs232c | generic */
  model?: string | null
  /** Adresse ecran (mode multi-ecrans en daisy chain, 1-99) */
  address?: number | null
  /** Active la synchronisation inter-players via UDP multicast */
  lan_sync_enabled?: boolean
  /** Nom du groupe LAN (pour isoler des groupes de videowalls) */
  lan_sync_group?: string | null
  /** Leader designe : ce player est la reference de sync (machine la plus puissante) */
  lan_sync_master?: boolean
  /** Resolution forcee appliquee au demarrage et a chaque sync (wlr-randr/xrandr/PS). Ex: "1920x1080@60" */
  screen_resolution?: string | null
  /** BrightSign : mode de sortie video pilote par le CMS (toggle 4K par-player). "best" = meilleur
   *  mode EDID (4K/8K selon le panneau), "fhd" = 1920x1080x60p, ou un mode explicite "WxHxRp".
   *  Absent = pas d'avis (defaut config.json de la SD). L'autorun applique au boot via un sidecar. */
  bs_video_mode?: 'best' | 'fhd' | string | null
  /** Rotation de l'ecran (xrandr --rotate). normal | left | right | inverted. Pour l'affichage portrait. */
  screen_rotation?: 'normal' | 'left' | 'right' | 'inverted' | null
  /** Acceleration materielle GPU. false = rendu logiciel (utile si pilote GPU instable).
   *  Defaut (absent/true) = GPU actif. Changement = relaunch du player (Windows). */
  gpu_enabled?: boolean | null
  /** Moteur de rendu video (Intel/Linux). 'mpv' = decodage HW VAAPI via mpv (au lieu du
   *  <video> Chromium software). Defaut (absent/'auto'/'chromium') = Chromium. Propose
   *  seulement si capabilities.mpv_vaapi. */
  video_renderer?: 'auto' | 'chromium' | 'mpv' | 'exo' | null
  /** Mur d'ecrans (video wall) : grille + cellule + bezels → compensation calculee serveur. */
  wall?: WallConfig | null
  /** Role NTP (chrony, linux/rpi) : 'server' = relais NTP du groupe LAN sync ; 'client' = se cale sur ntp_server. */
  ntp_role?: 'server' | 'client' | null
  /** IP du relais NTP du groupe (pour les clients). Rempli auto par le CMS depuis l'ip_address du relais. */
  ntp_server?: string | null
  /** Programmation horaire d'allumage/extinction (executee localement, offline-safe) */
  schedule?: DisplaySchedule | null
  /** Sequence de demarrage : liste de commandes avec delais (executee par le player). */
  startup_sequence?: DisplaySequenceStep[] | null
  /** Store Control Panel LAN (§19.3) : active le serveur HTTP local du player (opt-in, OFF par defaut). */
  local_control_enabled?: boolean
  /** Volume audio in-store (0-100), modifiable depuis le Store Control Panel. */
  audio_volume?: number | null
  /** Sortie audio des paniers : auto | hdmi | jack | usb */
  audio_basket_output?: 'auto' | 'hdmi' | 'jack' | 'usb'
  /** Son des contenus standards : on | muted */
  content_sound?: 'on' | 'muted'
  /** BrightSign : sortie audio matérielle du player (route le PCM du widget : tout le son sort là).
   *  hdmi | analog | spdif | usb | none. Absent = défaut système (HDMI). Changement = reboot du player
   *  (l'autorun applique au boot via pcm_audio_outputs ; le re-route runtime est deprecate). */
  audio_output?: 'hdmi' | 'analog' | 'spdif' | 'usb' | 'none' | null
  /** BrightSign : mute de la sortie audio matérielle (roAudioOutput.SetMute, appliqué à chaud). */
  audio_mute?: boolean | null
  /** Streaming auto depuis le serveur quand un média n'est pas encore en cache local. OFF par
   *  défaut = download-then-play STRICT (le player ne streame jamais ; il garde le programme
   *  courant / l'idle jusqu'à ce que TOUTES les vidéos du nouveau contenu soient cachées). ON =
   *  streame en attendant le cache (pour des players à très bonne bande passante). Anti-écroulement
   *  de la bande passante serveur à l'échelle (1000+ players). */
  allow_streaming?: boolean
  /** Sous-titres (§7) : langue préférée du pays du player (code ISO, ex. "fr") */
  subtitle_lang?: string
  /** Si la langue est absente : 'none' (rien) | 'default' (langue de repli) */
  subtitle_fallback?: 'none' | 'default'
  /** Langue de repli si subtitle_fallback='default' (ex. "en") */
  subtitle_default_lang?: string
  /** Intervalle de capture d'écran automatique en secondes (0 = désactivé, défaut 3600 = 1h). */
  screenshot_interval?: number | null
  /** Offline-first (Q3) : plafond de bande passante download en kbit/s (0/null = illimité). */
  download_bandwidth_kbps?: number | null
  /** Look-ahead (Phase 2) : fenêtres horaires autorisées pour le PREFETCH (contenu futur).
   *  La playlist courante télécharge hors fenêtre aussi. Vide/null = toujours autorisé. */
  download_windows?: { start: string; end: string }[] | null
  /** Look-ahead (Phase 3) : mesurer passivement le débit réel pendant les downloads et le
   *  remonter → l'ETA de readiness l'utilise (en repli du plafond manuel). Opt-in. */
  measure_bandwidth?: boolean
  /** Proof of Play : active la signature Ed25519 des preuves par le renderer web
   *  (BrightSign/ChromeOS). Les players Electron signent nativement en permanence, sans ce flag.
   *  Clé privée = seed 32o persistée localement, ne quitte jamais l'appareil ; clé publique figée
   *  côté serveur au 1er lot (TOFU). Activation en cours de route sans danger. */
  pop_sign?: boolean
  /** Auto-power écran : mode d'alimentation du player (cf. heures d'ouverture du site). */
  power_schedule?: {
    mode: 'follow_site' | 'always_on' | 'manual' | 'custom'
    on_time?: string   // 'HH:MM' (mode custom)
    off_time?: string
    days?: number[]    // 0=Lun … 6=Dim ; absent = tous les jours
  } | null
}

/** Profil d'acces au Store Control Panel LAN d'un player (§19.3). */
export interface LocalControlProfile {
  id_profile: number
  id_player: number
  role: string
  allowed_basket_ids: number[] | null
  can_edit_ratio: boolean
  can_set_volume: boolean
  can_trigger_event: boolean
  expires_at: string | null
  revoked: boolean
  created_at: string
  token: string | null
  lan_url: string | null
}

export interface PlayerRead {
  id_player: number
  id_org: number
  id_site: number | null
  player_name: string
  player_os: string | null
  player_version: string | null
  /** Rôle audio/contenu (§XIX) : content (défaut) | audio | mixed */
  player_role: 'content' | 'audio' | 'mixed'
  /** Retail Media §2.8 : exclu des campagnes (écran caisse…) — jamais de créatif */
  campaigns_optout?: boolean
  /** Inventaire v2 (0082) : plafond d'occupation campagnes (%) — null = héritage site/100 */
  campaign_max_occupancy_pct?: number | null
  /** Inventaire v2 (0082) : tarif SOV (€ / 1 % / jour) — null = héritage site/org */
  campaign_sov_daily_rate?: number | null
  status: 'online' | 'offline' | 'warning' | 'maintenance' | 'critical'
  ip_address: string | null
  wan_ip: string | null
  mac_address: string | null
  last_seen_at: string | null
  cpu_usage: number | null
  ram_usage: number | null
  disk_usage: number | null
  current_playlist_id: number | null
  token_expires_at: string | null
  /** Dernière capture d'écran : horodatage + URL signée MinIO (null si aucune) */
  last_screenshot_at: string | null
  screenshot_url: string | null
  /** Filtres de réception par tags (id_tag) — pastilles sur la carte */
  tag_filters?: number[]
  display_config: DisplayConfig | null
  /** Aptitude LAN Sync (calculé backend, source de vérité) : block = mpv (décodage HW,
   *  incompatible), warn = Android (hétérogène, déconseillé écrans proches), ok = Chromium/RPi/
   *  Windows. Pilote le gating de l'UI (drawer + onglet LAN Sync). */
  lan_sync_eligibility?: { level: 'ok' | 'warn' | 'block'; reason: string | null }
  /** Capacités matérielles auto-détectées (décodage vidéo HW), remontées par le player */
  capabilities: PlayerCapabilities | null
  /** Alertes actives posées par check_player_health (Celery 5 min) */
  alert_reasons: string[]
  created_at: string
  updated_at: string
}

/** Capacités HW remontées par le player (écrites par install.sh/ps1 dans capabilities.json). */
export interface PlayerCapabilities {
  gpu_vendor?: string | null
  gpu_model?: string | null
  /** vaapi | v4l2 | d3d11 | mediacodec | none */
  hw_decode?: string | null
  hw_decode_active?: boolean | null
  codecs?: string[]
  gl_backend?: string | null
  /** auto | on | off */
  sf_hw_decode?: string | null
  /** Voie mpv dispo (Intel/Linux) : mpv installé + VAAPI decode → le CMS propose
   *  display_config.video_renderer='mpv' (décodage HW alternatif au <video> Chromium). */
  mpv_vaapi?: boolean | null
  chromium_flags?: string[]
  detection_source?: string | null
  detected_at?: string | null
  /** Modes d'affichage supportés au format "WxH@Hz" (remontés par le player). */
  display_modes?: string[] | null
  /** Offline-first (Q4) : état du store local remonté au heartbeat. */
  store?: {
    disk_used_ratio?: number       // 0..1 (disque entier)
    storage_pressure?: boolean     // true = l'épinglé ne tient pas (alerte)
    cached_count?: number          // nb de médias en cache local
    throughput_kbps?: number       // débit mesuré (opt-in measure_bandwidth), kbit/s
  } | null
  /** Taille physique de la dalle en mm (lue via EDID/xrandr) → pré-remplit la dalle dans
   *  l'éditeur de mur (panel_w_mm/panel_h_mm) au lieu des défauts. */
  panel_mm?: { w: number; h: number } | null
  /** BrightSign : infos device natives servies par l'autorun (/device.json) → drawer enrichi
   *  (P1.3). model/famille/firmware BOS/série/mode vidéo courant/meilleur mode écran (EDID). */
  bs_model?: string | null
  bs_family?: string | null
  bs_bos?: string | null
  bs_serial?: string | null
  bs_video_mode?: string | null
  bs_best_mode?: string | null
  bs_autorun_ver?: string | null
  platform?: string | null
  /** Fuseau horaire OS réel du device (IANA, remonté au heartbeat) — détection de
   *  mismatch vs le fuseau du site : le pilotage offline auto-power suit CETTE horloge. */
  timezone?: string | null
}

export interface ProvisioningTokenRead {
  id_key: number
  id_org: number
  key_prefix: string
  label: string | null
  expires_at: string | null
  max_uses: number | null
  use_count: number
  is_active: boolean
  created_at: string
  token_value?: string
}

export interface PlayerGroupRead {
  id_group: number
  id_org: number
  group_name: string
  group_type: 'content' | 'audio' | 'mixed'
  description: string | null
  created_at: string
  updated_at: string
  /** Players membres — peuplé par GET /player-groups/ (agrégat, pas une colonne). */
  player_ids: number[]
}

export interface PlayerStatsRead {
  total: number
  online: number
  offline: number
  warning: number
}

export interface PopDayStats {
  date: string
  count: number
  duration_ms: number
}

export interface PopTopItem {
  id: number
  name: string
  count: number
}

export interface PopAnalyticsResponse {
  period_days: number
  total_plays: number
  total_duration_ms: number
  plays_by_day: PopDayStats[]
  top_media: PopTopItem[]
}

export interface PopTypeGroup {
  media_type: string
  items: PopTopItem[]
}

/** Dashboard « Mission Control » — carte KPI (GET /dashboard/summary). */
export interface KpiCard {
  key: string
  value: number
  /** Évolution % vs il y a 7 jours — null = base 0 (pas de badge). */
  delta_pct: number | null
  /** Série chronologique 8 points (ancien → aujourd'hui) pour la sparkline. */
  spark: number[]
}

export interface DashboardSummary {
  generated_at: string
  players_online: KpiCard
  players_offline: KpiCard
  players_warning: KpiCard
  active_campaigns: KpiCard
  proof_of_play: KpiCard
}

/** Fil d'activité récente (GET /dashboard/activity — depuis cms_audit_log). */
export interface ActivityItem {
  id: number
  action: string
  resource_type: string
  resource_id: string | null
  resource_name: string | null
  /** La ressource n'existe plus : le nom vient du journal d'audit, pas de la base. */
  resource_gone?: boolean
  // `detail` de l'audit n'est PAS exposé par l'API (dict libre, aucun écran ne le lit).
  user_name: string | null
  source: string
  created_at: string
}

export interface ActivityFeed {
  items: ActivityItem[]
}

/** Panneau média latéral (GET /dashboard/media) — par type : total + 3 récents. */
export interface RecentMediaItem {
  id_media: number
  media_name: string
  media_type: string
  thumbnail_url: string | null
}
export interface MediaTypeGroup {
  media_type: string
  total: number
  items: RecentMediaItem[]
}
export interface DashboardMedia {
  groups: MediaTypeGroup[]
}

/** Recherche globale (GET /search?q=). */
export interface SearchHit {
  id: number
  label: string
  sublabel: string | null
  href: string
}
export interface SearchGroup {
  kind: string
  items: SearchHit[]
}
export interface SearchResponse {
  query: string
  groups: SearchGroup[]
}

export interface PopTopByTypeResponse {
  period_days: number
  groups: PopTypeGroup[]
}

export interface OrgPlayerAlertSettings {
  offline_critical_min: number
  cpu_warn_pct: number
  ram_warn_pct: number
  disk_warn_pct: number
  token_warn_days: number
  min_player_version: string | null
}

export interface AudioBasket {
  id_basket: number
  id_org: number
  id_node: number | null
  basket_name: string
  basket_type: string  // music | message | jingle | ambiance | emergency
  basket_scope: string // national | regional | local | style
  description: string | null
  is_mandatory: boolean
  min_ratio: number
  max_ratio: number
  ducking_config: {
    enabled?: boolean
    target_pct?: number
    fade_in_ms?: number
    fade_out_ms?: number
  }
  /** Récurrence jingle : 'tracks' (tous les N morceaux) | 'minutes' (toutes les N min) */
  trigger_mode?: 'tracks' | 'minutes'
  trigger_value?: number
  /** Lecture en ordre aléatoire (paniers Music) */
  shuffle?: boolean
  is_active: boolean
  item_count: number
  /** Durée totale des pistes (somme des durées natives connues ; null si aucune). */
  total_duration_ms?: number | null
  created_by: number | null
  created_at: string
  updated_at: string
}

export interface AudioBasketCreate {
  basket_name: string
  basket_type: string
  basket_scope?: string
  description?: string | null
  is_mandatory?: boolean
  min_ratio?: number
  max_ratio?: number
  ducking_config?: Record<string, unknown>
}

export interface BasketItem {
  id_item: number
  id_basket: number
  id_media: number
  media_name: string | null
  media_type: string | null
  duration_ms: number | null
  weight: number
  valid_from: string | null
  valid_until: string | null
  /** Règle de diffusion récurrente (heure locale) : jours (0=Lun..6=Dim) + créneau */
  rule_days: number[] | null
  rule_time_start: string | null
  rule_time_end: string | null
  is_active: boolean
  sort_order: number
}

export interface PlayerBasketConfig {
  id_player: number
  id_basket: number
  basket_name: string | null
  basket_type: string | null
  mix_ratio: number
  is_active: boolean
  activated_at: string
}

export interface AffectedPlaylist {
  id_playlist: number
  playlist_name: string
}

export interface MediaEmbargoResult {
  media: MediaListItem
  affected_playlists: AffectedPlaylist[]
}

/** Lookup MusicBrainz par ISRC (§XIX.6). */
export interface IsrcLookupResult {
  found: boolean
  isrc: string
  title: string | null
  composer: string | null
  publisher: string | null
  album: string | null
  year: number | null
}

/** Ligne du rapport de droits SACEM/SOCAN (§XIX.6). */
export interface RightsReportRow {
  id_media: number
  isrc: string | null
  title: string
  composer: string | null
  publisher: string | null
  rights_org: string | null
  rights_cleared: boolean
  plays: number
  total_sec: number
  players: number
  sites: number
}

export interface RightsReportResponse {
  from_date: string
  until_date: string
  rights_org: string | null
  total_plays: number
  total_sec: number
  rows: RightsReportRow[]
}

// ── Gouvernance §XVIII ──────────────────────────────────────────────────────
export interface GovernanceNode {
  id_node: number
  id_org: number
  parent_id: number | null
  node_name: string
  node_level: number
  node_type: string | null
  path: string
  created_at: string
  child_count: number
  /** D7 : le contenu rattaché à ce nœud passe-t-il par une approbation (§XXI) ? */
  requires_approval?: boolean
  /** D6/D9 : l'utilisateur courant peut-il écrire sur ce nœud ? (false = ancêtre en lecture seule) */
  can_edit?: boolean
  /** Restrictions posées SUR CE NŒUD, par famille (migr 0110). Clé absente = restreinte. */
  rules_locked?: Partial<Record<RuleFamily, boolean>>
  /** Plafonds que ce nœud impose à ses descendants (null = aucun). */
  max_for_children?: number | null
  max_item_sec?: number | null
}

/** Les 4 familles de règles = les 4 familles de restriction (migr 0110). */
export type RuleFamily = 'item_count' | 'time_budget' | 'time_window' | 'preemptive'
export const RULE_FAMILIES: RuleFamily[] = [
  'item_count', 'time_budget', 'time_window', 'preemptive',
]

export interface GovernanceAssignment {
  /** `site_group` ajouté le 2026-07-26 (P1) : un groupe de sites peut ancrer un nœud. */
  target_type: 'player' | 'group' | 'site' | 'site_group'
  target_id: number
  target_name: string | null
}

export interface DelegationRule {
  id_rule: number
  id_node: number
  rule_type: string
  params: Record<string, unknown>
  soft_cut: boolean
  is_active: boolean
  created_at: string
}

export interface NodeAdmin {
  id_user: number
  email: string
  full_name: string | null
}

export interface GovernanceNodeDetail extends GovernanceNode {
  children: GovernanceNode[]
  rules: DelegationRule[]
  assignments: GovernanceAssignment[]
  admins: NodeAdmin[]
  /** D7 : décocher « sous modération » est réservé à un admin d'ancêtre / org-admin. */
  can_disable_moderation?: boolean
  /** Restriction EFFECTIVE par famille (OU sur le nœud et ses ancêtres) : c'est CETTE
   *  valeur que l'écran montre, pas la case brute — décocher ici ne sert à rien si un
   *  ancêtre a posé la restriction. */
  rules_locked_effective?: Partial<Record<RuleFamily, boolean>>
  /** Familles restreintes PLUS HAUT : la case locale est alors sans effet et doit le dire. */
  rules_locked_above?: Partial<Record<RuleFamily, boolean>>
  /** Peut-on écrire la règle de cette famille ? (grise au lieu de tomber en 403) */
  can_edit_rule_types?: Partial<Record<RuleFamily, boolean>>
  can_set_rules_delegation?: boolean
  /** Plafonds imposés par les ancêtres — bornent la saisie au lieu d'encaisser un 422. */
  inherited_max_per_turn?: number | null
  inherited_max_item_sec?: number | null
}

// ── Programme compilé §XVIII Phase 2 (lot 2c) ───────────────────────────────
export interface CompiledLayer {
  id_node: number
  node_name: string
  node_level: number
  id_schedule: number
  priority: number
  status: 'active' | 'preempted' | 'outside_time_window'
  item_count: number
  sec: number
  budget_trimmed: boolean
  budget_overflow: boolean
  /** Contenus écartés parce que trop longs (plafond de durée par contenu) — nommés. */
  dropped_too_long?: string[]
}

export interface CompiledItem {
  id_media: number
  media_name: string
  media_type: string
  duration_ms: number
  id_node: number
  id_schedule: number
}

/** Taux de passage d'un contenu sur le cycle compilé (ce que l'admin doit voir avant de valider). */
export interface ContentRate {
  id_media: number
  media_name: string
  id_node: number
  node_name: string
  plays_per_cycle: number
  interval_ms: number
  plays_per_day: number
  airtime_pct: number
}

export interface CompiledSchedule {
  id_player: number
  resolved_at: string
  cycle_duration_ms: number
  /** Nombre de tours du cycle (1 tour = 1 boucle à l'écran). */
  turns?: number
  /** Cycle réduit par le garde-fou : des contenus passent moins souvent qu'annoncé. */
  truncated?: boolean
  open_hours_per_day?: number
  contents?: ContentRate[]
  layers: CompiledLayer[]
  items: CompiledItem[]
  /** R10 : quand `layers` est vide, la cause réelle (et non « aucune couche active »). */
  empty_reason?: 'no_node' | 'no_schedule' | 'schedule_inactive' | 'no_playlist' | 'no_rule' | 'outside_window' | null
  /** Nom du programme concerné, quand la cause en désigne un. */
  empty_detail?: string | null
}

// ── Tags & filtrage §XVII ───────────────────────────────────────────────────
export interface TagValue {
  id_tag: number
  id_namespace: number
  tag_value: string
  label: string | null
  color: string | null
  sort_order: number
}

export interface TagNamespace {
  id_namespace: number
  id_org: number
  namespace_key: string
  label: string | null
  match_mode: string
  applies_to: string[]
  is_system: boolean
  is_active: boolean
  sort_order: number
  values: TagValue[]
}

export interface PlaylistItemTags {
  tags: number[]
  ns_overrides: number[]
  effective: Record<string, string[]>
}

export interface SimulateItem {
  id_item: number
  id_media: number
  media_name: string | null
  matched: boolean
}

export interface SimulatePlayerResult {
  id_player: number
  player_name: string | null
  items: SimulateItem[]
}

/** Simulation v2 — timeline déterministe d'une journée (honorant les règles). */
export interface TimelineSegment {
  id_item: number
  id_media: number | null
  media_name: string | null
  media_type: string | null
  start: string   // "HH:MM:SS"
  end: string
  duration_ms: number
  kind?: 'item' | 'event'        // 'event' = fenêtre où un event programmé prend la main
  event_name?: string | null
  content_name?: string | null
  priority?: number | null
}

export interface SimulateTimelineResult {
  sync_mode: string
  segments: TimelineSegment[]
  truncated: boolean
}

export interface FallbackConfig {
  fallback_audio_id: number | null
  fallback_video_message: string | null
  fallback_video_config: Record<string, unknown>
}

export interface EventRead {
  id_event: number
  id_org: number
  event_name: string
  event_type: string
  /** content (playlist/media visuel) | audio (panier §XIX) */
  event_kind: 'content' | 'audio'
  /** Playlist associee (null si event utilise id_media) */
  id_playlist: number | null
  playlist_name: string | null
  /** Contenu unique associe (null si event utilise id_playlist) */
  id_media: number | null
  media_name: string | null
  /** Panier audio associe (event_kind=audio) + ratio de mix */
  id_basket: number | null
  basket_name: string | null
  mix_ratio: number | null
  priority: number
  /** Gouvernance §XVIII (migr 0112) : noeud proprietaire. null = evenement d'organisation. */
  id_governance_node?: number | null
  duration_ms: number | null
  revert_on_end: boolean
  /** Plage de validite — NULL = pas de contrainte de date */
  valid_from: string | null
  valid_until: string | null
  /** Si true, repasse en inactive apres expiration (répétition automatique) */
  is_recurring: boolean
  /** Si true : coupe à l'heure exacte. Si false : attend la fin de l'item en cours. */
  is_preemptive: boolean
  /** Si true : garde les écrans des players ciblés allumés tant que l'event est actif */
  keep_screens_on: boolean
  /** Embargo UTC : l'event ne peut s'activer avant cette date/heure (null = pas d'embargo) */
  not_before_utc: string | null
  /** Fuseau de reference des heures du declenchement — toujours servi resolu (P3.13). */
  timezone: string
  status: string
  trigger_config: Record<string, unknown> | null
  target_config: Record<string, unknown>
  activated_at: string | null
  expires_at: string | null
  created_by: number | null
  created_at: string
  updated_at: string
}

export interface EventCreate {
  event_name: string
  event_type: string
  /** Soit id_playlist, soit id_media est obligatoire */
  id_playlist?: number | null
  id_media?: number | null
  priority?: number
  duration_ms?: number | null
  revert_on_end?: boolean
  valid_from?: string | null
  valid_until?: string | null
  is_recurring?: boolean
  /** Si true : coupe le contenu à l'heure exacte. Si false : attend la fin de l'item en cours. */
  is_preemptive?: boolean
  /** Si true : garde les écrans des players ciblés allumés tant que l'event est actif */
  keep_screens_on?: boolean
  /** Embargo UTC : plancher absolu de diffusion, indépendant de SIGNFLOW_TZ */
  not_before_utc?: string | null
  trigger_config?: Record<string, unknown> | null
  target_config: Record<string, unknown>
}

export interface EventStackItem {
  id_event: number
  event_name: string
  event_type: string
  id_playlist: number | null
  id_media: number | null
  priority: number
  status: string
  activated_at: string | null
  expires_at: string | null
}

export interface PlayerCurrentState {
  resolved_at: string
  override_source: 'event' | 'schedule' | 'none'
  active_event: {
    id_event: number
    event_name: string
    priority: number
    expires_at: string | null
  } | null
  active_schedule: {
    id_schedule: number
    schedule_name: string
    priority: number
  } | null
  active_playlist: {
    id_playlist: number
    playlist_name: string
    sync_mode: string
    item_count: number
    total_duration_ms: number | null
  } | null
  current_item: {
    item_index: number
    media_name: string
    offset_ms: number
    duration_ms: number
  } | null
  player_role?: 'content' | 'audio' | 'mixed'
  active_audio?: {
    id_basket: number
    basket_name: string
    basket_type: string
    mix_ratio: number | null
    item_count: number
  }[] | null
}

export interface ApiError {
  // `enrolment_token` : jeton à portée réduite (n'ouvre que /auth/totp/setup et /verify),
  // délivré avec le 403 « 2FA obligatoire » pour qu'un administrateur non enrôlé puisse
  // s'enrôler. Sans lui, la connexion et l'enrôlement se bloquaient mutuellement.
  detail: string | { message: string; force_2fa_setup?: boolean; enrolment_token?: string }
}

/** VOD à la demande (Feature B) — config nommée : médias visuels pré-cachés sur des
 *  players du même site, déclenchables à la demande. */
export interface VodMediaItem {
  id_media: number
  media_name: string | null
  media_type: string | null
  sort_order: number
}

export interface VodPlayerItem {
  id_player: number
  player_name: string | null
  player_status: string | null
  id_site: number | null
}

/** Mode de ciblage d'une config VOD (migr 0096) : liste figée de players, ou site ENTIER
 *  (cible dynamique — un player ajouté au site hérite du pré-cache sans rouvrir la config). */
export type VodTargetMode = 'players' | 'site'

export interface VodConfig {
  id_vod: number
  id_org: number
  id_site: number | null
  site_name: string | null
  target_mode: VodTargetMode
  vod_name: string
  description: string | null
  /** Gouvernance §XVIII (migr 0112) : noeud proprietaire. null = config d'organisation. */
  id_governance_node?: number | null
  medias: VodMediaItem[]
  /** Players RÉSOLUS (liste explicite, ou tous ceux du site en ciblage `site`). */
  players: VodPlayerItem[]
  created_by: number | null
  created_at: string
  updated_at: string
}

// URL Display (§XXV.2) — « Site Web » comme contenu schedulable (média sans fichier)
export interface UrlDisplayConfig {
  render_mode: 'webview' | 'screenshot_server'
  refresh_interval_s: number
  zoom: number
  scroll_mode: 'none' | 'auto' | 'infinite'
  scroll_speed_px_s: number
  interaction_blocked: boolean
  custom_css: string
  hide_selectors: string[]
  reachability_enabled: boolean
  reachability_timeout_s: number
}

export interface UrlDisplayRead {
  id_media: number
  name: string
  url: string
  config: UrlDisplayConfig
  created_at: string
  updated_at: string
}

// ── Feed Display (§XXV.3) — RSS/Atom ──
export type FeedType =
  | 'rss' | 'atom' | 'google_news' | 'youtube_channel'
  | 'instagram' | 'facebook_page' | 'twitter_x' | 'linkedin' | 'tiktok'
  | 'crypto' | 'financial' | 'weather_widget'
export type FeedDisplayMode =
  | 'ticker' | 'card_carousel' | 'card_grid' | 'full_item' | 'split_layout' | 'compact_list'
  | 'video_feed' | 'social_wall' | 'data_chart'

export interface FeedDisplayConfig {
  item_duration_s: number
  transition: 'slide' | 'fade'
  show_image: boolean
  show_date: boolean
  show_description: boolean
  grid_columns: number
  grid_rows: number
  split_side: 'left' | 'right'
  scroll_speed_px_s: number
  separator: string
  show_play_icon?: boolean
  show_source: boolean
  caption_max_chars?: number
  chart_type?: 'bars' | 'line' | 'gauge'
  show_change?: boolean
}

export interface FeedRead {
  id_feed: number
  id_media: number | null
  feed_name: string
  feed_type: FeedType
  url: string | null
  config: Record<string, unknown>
  polling_interval_s: number
  display_mode: FeedDisplayMode | null
  display_config: FeedDisplayConfig
  max_items: number
  moderation_enabled: boolean
  ai_summary_enabled: boolean
  status: string
  last_fetched_at: string | null
  last_error: string | null
  item_count: number
  created_at: string
  updated_at: string
}

export interface FeedItemRead {
  id_item: number
  title: string | null
  description: string | null
  summary_ai: string | null
  image_url: string | null
  link_url: string | null
  author: string | null
  value_num: number | null
  value_unit: string | null
  change_pct: number | null
  published_at: string | null
  moderation_status: string
}
