' ============================================================================
' SignFlow — autorun.brs (BrightSign / cache local NATIF — option B)
' ----------------------------------------------------------------------------
' roHttpServer (127.0.0.1:8888) :
'   GET  /                  -> bootstrap.html (SD)  [page en ORIGINE LOCALE http]
'   GET  /media/<cache_key> -> fichier SD (vidéo lue par le <video>)
'   POST /queue             -> le renderer POSTe {items:[{cache_key,url}]} -> roUrlTransfer
'   GET  /cached.json       -> { keys: [...] } des médias dispos en local
'
' ⚠️ ÉTAT PARTAGÉ : BrightScript NE PARTAGE PAS `m` entre fonctions distinctes →
'    on utilise GetGlobalAA() au début de CHAQUE fonction qui touche l'état partagé.
'    (Le crash-loop précédent venait de sous-fonctions de setup avec un `m` vide.)
'
' Itération 2 : pas de re-scan SD au boot (persistance offline = plus tard), noms de
' fichiers APLATIS (cache_key "/"→"~" → pas de sous-dossier, pas de CreateDirectory par
' fichier), pas de réponse manuelle au POST (le renderer poll /cached.json pour l'état).
' VERIFY restants : CreateDirectory(cacheDir), GetRequestBodyString, AsyncGetToFile.
' ============================================================================

Sub Main()
    m = GetGlobalAA()
    ' Securite : telnet (console BrightScript) DESACTIVE par defaut (prod). Reactive PLUS BAS
    ' seulement si config.json a "debug": true (poste de debug au domicile). Delete inoffensif
    ' si la cle n'existe pas.
    tr = CreateObject("roRegistrySection", "networking")
    tr.Delete("telnet")
    tr.Flush()
    m.port = CreateObject("roMessagePort")
    m.prefix = "sfc_"          ' préfixe des fichiers de cache, à la RACINE SD (pas de sous-dossier)
    m.cached = {}
    m.dlQueue = CreateObject("roList")
    m.dling = invalid
    m.dlKey = ""
    m.dlTmp = ""
    m.dlDest = ""
    ' P2.7 : file de PREFETCH look-ahead (contenu futur) — BASSE priorite : drainee seulement quand
    ' m.dlQueue (items courants) est vide ET la fenetre horaire (P2.8) l'autorise. m.prefetchWindows =
    ' download_windows (HH:MM) pousse par le renderer ; [] = aucune restriction (download tout le temps).
    m.prefetchQueue = CreateObject("roList")
    m.prefetchWindows = []
    ' P2.3 : etat de la machine screenshot non-bloquante (0 idle | 1 snapshot en vol | 2 upload en vol).
    m.ssState = 0
    m.ssXfer = invalid
    m.ssLocal = ""
    m.ssPlayerId = ""
    m.ssPending = false
    m.ssPendingId = ""

    ' ── Config depuis la SD ─────────────────────────────────────────────────
    cfg = {}
    raw = ReadAsciiFile("config.json")
    if raw <> invalid and raw <> "" then
        parsed = ParseJson(raw)
        if type(parsed) = "roAssociativeArray" then cfg = parsed
    end if
    backend = "" : token = ""
    if cfg.backend_url <> invalid then backend = cfg.backend_url
    if cfg.token <> invalid then token = cfg.token
    ' Correctif A : le JWT rote est persiste sur la SD (sf_token.txt, ecrit par la cmd persist_token)
    ' -> SURVIT au wipe du profil Chromium (saut de firmware = localStorage efface). PRIORITE sur le
    ' token de config.json (qui peut etre un sfp_ a usage unique deja consomme). Plus de boucle 409.
    savedTok = ReadAsciiFile("sf_token.txt")
    if savedTok <> invalid and savedTok <> "" then token = savedTok
    m.backend = backend
    m.token = token            ' Bearer pour l'upload du screenshot (cmd "screenshot" -> DWS -> CMS)
    ' P2.9 : fuseau du device depuis config.json (provisioning zero-touch). roSystemTime.SetTimeZone
    ' (API BrightSign COEUR ; accepte "CET"/"EST"/"GMT+1"/POSIX) -> fenetres horaires P2.8 + horodatage
    ' logs/screenshots a l'heure locale correcte SANS config manuelle sous BrightSign OS. GUARDE
    ' (n'agit QUE si cfg.timezone present) -> no-op total sur le parc existant. ⚠️ a valider HW.
    if cfg.timezone <> invalid and cfg.timezone <> "" then
        st = CreateObject("roSystemTime")
        if st <> invalid then st.SetTimeZone(cfg.timezone)
    end if
    w = 1920 : h = 1080
    if cfg.width <> invalid then w = cfg.width
    if cfg.height <> invalid then h = cfg.height
    m.scrW = w : m.scrH = h                       ' memorise la resolution (rectangle HDMI-in plein ecran)
    ' P1.2 : auto-update de l'autorun depuis prod — OPT-IN (feature a risque de brick, cf.
    ' RefreshAutorun). Une fois ce gate a true sur une SD, les futurs autoruns prod se propagent
    ' tout seuls ; le 1er deploiement de l'autorun auto-update-capable reste manuel (cette SD-ci).
    m.autoUpdate = (cfg.autorun_autoupdate = true)

    ' P2.5 : watchdog memoire. La VRAIE voie BrightSign pour la memoire = roDeviceInfo.getLoadStatistics
    ' ({item:"meminfo"}) MAIS la methode n'existait pas sur BOS 8.5.70 -> "Member function not found" =
    ' crash non-catchable (pas de try/catch BrightScript classique) = LED rouge 10x = sortie SD. On NE
    ' l'utilise donc PAS. A la place, canal BRICK-PROOF : l'autorun interroge son PROPRE DWS local
    ' (LDWS GET /api/v1/legacy/meminfo, securite "informational", deja prouve via curl LAN) en roUrlTransfer
    ' + Basic auth (m.dwsPw, MEME pattern que le screenshot) -> ParseJson -> MemAvailable/MemTotal. Aucune
    ' methode BrightScript non-prouvee dans le chemin. Le ratio est ecrit dans sf_mem.json (servi /mem.json
    ' pour validation curl). OBSERVE-ONLY par defaut (mem_watchdog absent/false -> ne reboote JAMAIS, ecrit
    ' juste /mem.json) ; le reboot de recuperation n'est arme que par "mem_watchdog":true ET un creux
    ' SOUTENU (mem_low_count lectures consecutives < mem_low_ratio) -> anti faux-positif sur pic transitoire.
    m.memWatchdog = (cfg.mem_watchdog = true)
    m.memLowRatio = 0.08
    if type(cfg.mem_low_ratio) = "Float" or type(cfg.mem_low_ratio) = "Double" or type(cfg.mem_low_ratio) = "Integer" then
        if cfg.mem_low_ratio > 0 then m.memLowRatio = cfg.mem_low_ratio
    end if
    m.memLowMax = 5
    if type(cfg.mem_low_count) = "Integer" and cfg.mem_low_count > 0 then m.memLowMax = cfg.mem_low_count
    m.memIntervalMs = 60000        ' frequence min entre 2 mesures (time-gate, cf. boucle d'evenements)
    m.memLowCount = 0
    m.memRatio = -1.0              ' -1 = pas encore mesure
    ' v49 : charge CPU via LDWS /api/v1/legacy/loadavg (meme canal brick-proof que meminfo — la sonde
    ' LAN 2026-07-14 a prouve loadavg ET stat sur BOS 9.1.132). ncpu compte une fois depuis legacy/stat
    ' (lignes cpuN) -> cpu_ratio = load1/ncpu (cap 1.0) publie dans sf_mem.json -> renderer -> heartbeat.
    m.nCpu = 0                     ' 0 = pas encore compte (1re MemCheck reussie le fixe)

    ' P2.2 : config reseau depuis config.json (OPT-IN). Defaut = pas de bloc "network" -> rien (DHCP,
    ' comportement actuel). Applique AVANT le DWS/device.json (pour binder sur l'IP configuree).
    ApplyNetworkConfig(cfg.network)

    ' Debug : reactive la console telnet (port 23) pour diagnostiquer en dev (config "debug": true).
    if cfg.debug = true then
        tr.Write("telnet", "23")
        tr.Flush()
    end if

    ' Screenshot : active le Diagnostic Web Server (DWS) -> l'endpoint LDWS /api/v1/snapshot/ capture
    ' le framebuffer COMPOSITE (plan video HW inclus) que le canvas ne peut pas (video hwz hors canvas).
    ' L'autorun le requetera en LOCAL (127.0.0.1:80, digest admin/m.dwsPw). Effectif au reboot SUIVANT.
    ' AUTH : username DWS toujours "admin". BOS 9.x : le DWS LOCAL est DESACTIVE par defaut + la cle
    ' registre http_auth n'active plus le LDWS. La voie moderne = roNetworkConfiguration.SetupDWS({
    ' port, open}) : active le LDWS sur le port 80 avec un mot de passe (open = mdp en clair). Effectif
    ' apres reboot (renvoie true si restart requis). Le mdp ne vit que dans cet autorun (SD), jamais servi.
    ' P0.3 — mdp DWS DERIVE PAR DEVICE (fini le mdp fixe partage par tout le parc). On combine un SEL
    ' secret (present uniquement dans ce repo PRIVE) au n° de serie du device. Proprietes : un attaquant
    ' LAN connait le serie (/device.json l'expose) mais PAS le sel ; un lecteur du repo connait le sel
    ' mais PAS le serie de CHAQUE device -> il faut les DEUX -> compromettre 1 device ne donne pas le parc.
    ' Concatenation pure (aucun builtin string a risque de compile : roDeviceInfo est deja prouve sur
    ' device.json plus bas). Repli sur l'ancien mdp fixe si le serie est indisponible (parc historique).
    serialForPw = ""
    diPw = CreateObject("roDeviceInfo")
    if diPw <> invalid then serialForPw = diPw.GetDeviceUniqueId()
    if type(serialForPw) <> "String" or serialForPw = "" then
        m.dwsPw = "SfDws2026x9!"
    else
        m.dwsPw = "Sf9Kqr" + serialForPw + "Dx7!"
    end if
    ' Serie memorise (gate de la sonde LAN sync Phase 1 : n'execute le socket UDP non prouve que
    ' sur UN device -> rayon de brick limite a 1 machine tant que roDatagramSocket n'est pas valide HW).
    m.serial = ""
    if type(serialForPw) = "String" then m.serial = serialForPw
    if cfg.dws = true or cfg.debug = true then
        tr.Write("dwse", "on")
        tr.Flush()
        ncDws = CreateObject("roNetworkConfiguration", 0)
        if ncDws <> invalid then
            ' basic:true -> auth BASIC (identifiants des la 1re requete) au lieu de digest. Le digest
            ' fait 2 requetes (challenge 401 -> renvoi) et roUrlTransfer NE PEUT PAS rembobiner le corps
            ' du POST pour le renvoi ("necessary data rewind wasn't possible"). Basic = 1 requete = OK.
            ' Acceptable car l'autorun appelle le DWS en LOCALHOST (identifiants pas exposes au reseau).
            ncDws.SetupDWS({ port: 80, open: m.dwsPw, basic: true })
        end if
    end if

    ' ── P0.2 : autorun auto-portable (dependance fichier implicite ss_body.json) ──
    ' Le screenshot POST le corps {width,height} au snapshot DWS depuis CE fichier (cf. HandleScreenshot,
    ' ReadAsciiFile("ss_body.json")). Une SD provisionnee sans ss_body.json = screenshot KO EN SILENCE
    ' (corps vide -> l'endpoint renvoie 500). On le (re)cree au boot s'il manque, de sorte qu'une SD
    ' creee depuis l'UI / un template ancien marche sans fichier ajoute a la main. FormatJson est deja
    ' prouve a la compile + au runtime sur ce firmware (device.json plus bas) et evite toute quote
    ' echappee dans le .brs (refus de compile sur ce BOS) ; pas besoin de Chr(). Ecrit a la RACINE SD.
    ssBody = ReadAsciiFile("ss_body.json")
    if type(ssBody) <> "String" or ssBody = "" then
        WriteAsciiFile("ss_body.json", FormatJson({ width: 1280, height: 720 }))
    end if

    ' ── Registre (BOS 8.2+) : clés avec ESPACES ─────────────────────────────
    reg = CreateObject("roRegistrySection", "html")
    if reg <> invalid then
        reg.Write("disable web security", "1")
        if cfg.debug = true then reg.Write("enable web inspector", "1")
        reg.Flush()
    end if

    ' ── Plan graphique DEVANT le plan vidéo + mode vidéo ────────────────────
    ' hwz="on" met le <video> sur le PLAN VIDÉO MATÉRIEL (décodage HW, 4K). Par défaut ce plan
    ' passe DEVANT le HTML → l'overlay de maintenance est masqué. SetGraphicsZOrder("front") met
    ' le plan graphique AU-DESSUS ; le <video hwz> reste visible via le « trou » transparent qu'il
    ' perce dans le plan graphique → vidéo HW + overlay HTML par-dessus. cf. BrightSign div-over-video.
    vm = CreateObject("roVideoMode")
    if vm <> invalid then
        ' Mode video : defaut config.json, OVERRIDE CMS (toggle 4K par-player) via le sidecar
        ' sf_videomode.txt (ecrit par la cmd set_video_mode + reboot). Mots-cles : "best" ->
        ' GetBestMode (4K/8K selon le panneau EDID), "fhd" -> 1920x1080x60p. Permet de piloter la
        ' resolution depuis l'UI sans editer la SD. Primitives toutes prouvees (Read/Write + SetMode).
        vmode = ""
        if cfg.video_mode <> invalid then vmode = cfg.video_mode
        ov = ReadAsciiFile("sf_videomode.txt")
        if ov <> invalid and ov <> "" then vmode = ov
        if vmode = "best" then vmode = vm.GetBestMode("hdmi")
        if vmode = "fhd" then vmode = "1920x1080x60p"
        if vmode <> "" and vmode <> invalid then vm.SetMode(vmode)
        vm.SetGraphicsZOrder("front")
    end if

    ' ── Serveur HTTP local ──────────────────────────────────────────────────
    m.server = CreateObject("roHttpServer", { port: 8888 })
    m.server.SetPort(m.port)
    m.server.AddGetFromFile({ url_path: "/",              content_type: "text/html; charset=utf-8", filename: "bootstrap.html" })
    m.server.AddGetFromFile({ url_path: "/bootstrap.html", content_type: "text/html; charset=utf-8", filename: "bootstrap.html" })
    ' Renderer servi en LOCAL depuis la SD (sf_rnd_*) → le bootstrap le charge même HORS-LIGNE.
    ' Sans ça il chargeait le renderer depuis prod → écran noir en coupure (offline-cold-boot KO).
    m.server.AddGetFromFile({ url_path: "/player-chromeos/ed25519.js",      content_type: "application/javascript", filename: "sf_rnd_ed25519.js" })
    m.server.AddGetFromFile({ url_path: "/player-chromeos/signflow.js",     content_type: "application/javascript", filename: "sf_rnd_signflow.js" })
    m.server.AddGetFromFile({ url_path: "/player-chromeos/audio_player.js", content_type: "application/javascript", filename: "sf_rnd_audio_player.js" })
    m.server.AddGetFromFile({ url_path: "/player-chromeos/player.js",       content_type: "application/javascript", filename: "sf_rnd_player.js" })
    m.server.AddGetFromFile({ url_path: "/player-chromeos/style.css",       content_type: "text/css", filename: "sf_rnd_style.css" })
    ' UN SEUL endpoint POST fiable sur ce firmware (un 2e endpoint POST n'est pas routé -> POST /cmd
    ' renvoyait 000 sans atteindre HandleHttpEvent). Le canal de commande passe donc PAR /queue :
    ' HandleHttpEvent discrimine par le CORPS ({cmd:...} vs {keep/purge/items}).
    m.server.AddPostToString({ url_path: "/queue" })
    ' telecharge les sf_rnd_* manquants depuis backend (SD vierge -> auto-provision). Echec (reseau pas
    ' encore la) -> m.rndMissing : la boucle reessaie toutes les 30 s puis recharge la page (v54).
    m.rndMissing = not EnsureRenderer()
    ScanCache()                          ' re-decouvre les sfc_* deja sur SD (persistance reboot) + pose /cached.json

    ' ── Infos device natives → /device.json ─────────────────────────────────
    ' Le JS @brightsign/* (require) est DÉSACTIVÉ → on récupère ces infos en BrightScript natif
    ' et on les sert en local ; le renderer les lit (/device.json) → heartbeat. Donne : la VRAIE
    ' IP LAN (le drawer montrait la gateway), la MAC, le MODÈLE (→ capacité 4K), la version BOS.
    dev = {}
    di = CreateObject("roDeviceInfo")
    if di <> invalid then
        dev.model   = di.GetModel()
        dev.family  = di.GetFamily()
        dev.bos     = di.GetVersion()
        ' Tier 1.3 drawer : n° de série natif (idée Ed : remonter au CMS). Best-effort selon firmware.
        dev.serial  = di.GetDeviceUniqueId()
    end if
    ' P1.2 : version de CET autorun → remontée au CMS (capabilities.bs_autorun_ver) via /device.json.
    ' Rend l'auto-update OBSERVABLE en base (après application, la version change) + visibilité de parc.
    ' ⚠️ À BUMPER à chaque publish d'autorun (sync avec frontend/public/player-chromeos/brightsign/).
    ' v3 = + handler firmware_update (P1.1) ; v4 = + occupation disque SD (P2.1) ; v5 = + config reseau
    ' (P2.2) ; v6 = fix chemin roStorageInfo "SD:" (sonde HW : "/storage/sd"=invalid, "SD:"=60475 Mo OK) ;
    ' v7 = + watchdog memoire P2.5 (LDWS meminfo, observe-only par defaut, /mem.json) ; v8 = screenshot
    ' P2.3 NON-BLOQUANT (machine a etats async sur m.port, exclusion mutuelle avec les DL) ; v9 = prefetch
    ' look-ahead P2.7 (file basse priorite) + fenetres horaires P2.8 (download_windows, roDateTime local) ;
    ' v10 = + toggle 4K par-player P2.4/P3.1 (cmd set_video_mode -> sidecar sf_videomode.txt -> SetMode au boot) ;
    ' v11 = + Correctif A : JWT rote persiste sur SD (cmd persist_token -> sf_token.txt, lu au boot, survit au wipe firmware) ;
    ' v12 = + P2.9 fuseau device depuis config.json (roSystemTime.SetTimeZone, guarde, zero-touch provisioning) ;
    ' v13 = + contrôle écran RS232 (cmd display_command -> roSerialPort ; trame protocole construite par le renderer, A VALIDER HW) ;
    ' v14 = + sortie audio (pcm_audio_outputs au boot + cmds audio_output[reboot]/audio_volume/audio_mute[live] -> roAudioOutput, A VALIDER HW) ;
    ' v15 = + entree HDMI live (cmd hdmi_input start/stop -> roVideoPlayer.PlayFile(roVideoInput) plein ecran + z-order, XT1144 only, A VALIDER HW) ;
    ' v16 = 🐞 FIX boucle reboot sortie audio (SetAudioOutput ne reboote QUE si la sortie change vs sidecar ; + dev.audio_out dans device.json pour comparaison etat reel cote renderer) ;
    ' v17 = 🐞 RETRAIT roAudioOutput (incompatible audio HTML5 widget -> cassait le son non-recuperable) : volume/mute -> cote renderer (video.volume/.muted) ; ApplyAudioVolMute = nettoyage sidecars seulement ;
    ' v18 = 🐞 FIX HDMI muet (analog->hdmi gardait le routage analog) : pcm_audio_outputs TOUJOURS pose explicitement au boot (hdmi inclus), SetAudioOutput ecrit toujours le sidecar ;
    ' v19 = sonde RS232 (/rs232.json) : HandleDisplayCommand trace chaque etape (parse/open/send) -> debug pilotage ecran via le LAN sans telnet ;
    ' v20 = 🐞 FIX crash RS232 : roSerialPort.SendByteArray N'EXISTE PAS -> SendBlock(roByteArray) ; sonde survit au reboot (idle seulement si fichier absent) ;
    ' v21 = 🐞 FIX crash RS232 #2 : le crash etait dans le parse hex (Val(s,16)/Mid non fiables) -> le renderer envoie payload.bytes (tableau d'entiers), l'autorun les pousse via for-each (zero parsing).
    ' v22 = sonde HDMI-in (/hdmi.json) : StartHdmiInput trace chaque etape (roVideoInput valide ? roVideoPlayer valide ? rectangle ? PlayFile resultat ? z-order ?) -> debug "l'entree HDMI ne s'affiche pas" via le LAN sans cycle SD.
    ' v23 = HDMI-in : retry PlayFile(roVideoInput) jusqu'a 6x espacees de 500ms (le verrouillage du signal HDMI prend un instant -> PlayFile=false au 1er essai ; pattern documente BrightSign). diag.attempts dans /hdmi.json.
    ' v24 = PHASE 1 LAN SYNC — sonde reception UDP (slave-only) : roDatagramSocket bind :7778 -> capte le broadcast sync_play du NUC maitre -> fil d'Ariane /lan.json. GATE serie (HD1024) = 1 seul device execute le socket (non prouve HW) -> rayon de brick limite. API confirmee doc BrightSign (bindToLocalPort/setPort/roDatagramEvent.getString). VALIDE HW (count>500, sync_play du NUC id 1 capte).
    ' v25 = PHASE 1->2 LAN SYNC : le recepteur elit le meilleur leader (priorite puis id) et EXPOSE le dernier sync_play BRUT de ce leader dans /lan.json (0 re-serialisation = pas de perte de precision sur switch_at/sent_at). Le renderer poll /lan.json -> onLanSwitch(switch_at absolu)/onLanAdjust(offset extrapole) = unison de contenu. Slave-only (BrightSign ne broadcast pas). VALIDE HW HD1024 (suit le NUC maitre, seek attendu sur groupe heterogene).
    ' v26 = gate socket etendu au XT1144 (D7E9AA003555) : les 2 BrightSign recoivent+suivent le maitre LAN. API roDatagramSocket prouvee HW sur HD1024 -> etendue (reseau BrightScript = OS-level). (Emission broadcast pour qu'un BrightSign soit MAITRE = Phase 2b, sendTo a prouver HW.)
    ' v27 = PHASE 2b Step A : sonde EMISSION broadcast (LanSendProbe) : sendTo({type:"bs_probe"}) -> 255.255.255.255:7778 toutes les 3s, GATE serie HD1024. Prouve que l'emission broadcast marche (prealable au BrightSign MAITRE). Resultat rc/fail -> /landiag.json. Paquet benin (sync_lan.js l'ignore : type inconnu, pas d'id_player).
    ' v28 = Step A suite : rc=0/fail="" du sendTo AMBIGU -> compteur de bs_probe RECUS (/lanrecv.json, recv+IP source). Si le XT1144 voit les bs_probe du HD1024 -> emission broadcast PROUVEE end-to-end (via le chemin de reception deja valide). => EMISSION BROADCAST PROUVEE HW (XT1144 recoit .150).
    ' v29 = PHASE 2b : BrightSign MAITRE. Le renderer POST {lan_report:{id,group,master,play}} -> HandleLanReport : announce (3s) + relais du sync_play BRUT si lan_sync_master. HandleLanDatagram = filtre de GROUPE (2 groupes sur le LAN) + self-guard (loopback). Groupe pur-BS "BS Sync LAN" : XT1144 maitre diffuse, HD1024 slave suit. bs_probe retire.
    ' v30 = FAILOVER (item 1) : election du leader effectif VIVANT (m.lanPeers horodates via m.lanClock roTimespan ; TTL peer 20s = mort -> sort de l'election ; hysterese promotion 35s > TTL = anti flip-flop, aligne sync_lan.js). Un maitre mort est demote -> un slave survivant (tie-break d'id) est PROMU et se met a diffuser SA position (le renderer envoie TOUJOURS son play, l'autorun gate la diffusion sur m.lanEffMaster). Election re-jouee a chaque paquet ET toutes les 3s (detecte un silence). HandleLanDatagram suit m.lanEffLeaderId (pas la best monotone), le maitre effectif suit le SERVEUR (ne s'ecrit pas /lan.json). Etat d'election -> /lanelect.json (ecrit aux CHANGEMENTS seulement). Renderer signflow.js : play construit meme en slave, priority = designee (1/0). VALIDE HW les 2 sens.
    ' v31 = USURE FLASH SD (item 2) : les fichiers LAN a forte frequence (sf_lan.json ~1Hz slave, sf_landiag.json ~1Hz maitre = ~86k ecritures/j) sont ecrits sur le volume RAM tmp: (tmpfs) au lieu de la SD -> 0 ecriture SD pour la sync LAN. Sonde au boot (WriteAsciiFile/ReadAsciiFile "tmp:/sf_probe.txt") : si tmp: absent/KO -> repli SD (m.ramPrefix="") = brick-proof, degrade gracieux. Prefixe m.ramPrefix applique a l'ecriture ET a AddGetFromFile (m.lanFile/m.lanDiagFile/m.lanElFile). Le champ "ram" de /lan.json expose le prefixe effectif ("tmp:/" ou "").
    ' v32/v33 = BARRIERE READINESS (item 3) + polish late-joiner -> RETIREE en v34 (regression chemin de rendu : ecran noir sur un player qui reboote avec le nouveau contenu 4K PAS ENCORE en cache ; la barrire de groupe empilee sur le download-then-play retenait tout sans repli visible ; validation initiale faite sur les endpoints LAN /lanbarrier.json seulement, PAS sur l'ecran). A reprendre pour un vrai MUR HOMOGENE avec validation VISUELLE.
    ' v34 = ROLLBACK item 3 : retour au download-then-play LOCAL pur (comportement d'avant, sur). Items 1 (failover) + 2 (usure flash tmp:) GARDES + valides HW. Renderer signflow.js restaure a l'etat item-1 (sans hold barriere, sans poll /lanbarrier.json). Autorun sans code barriere (LanCheckBarrier/lanReleased/rel_fp/lanbarrier retires).
    ' v35 = ROTATION ECRAN NATIVE : le transform CSS du renderer ne tourne QUE la couche HTML, pas le plan video HW (<video hwz>) -> rotation cassee sur BrightSign (portrait 90 h/anti-h identiques, 180 = paysage). Fix = cle "transform" du roHtmlWidget (rot90/rot180/rot270) posee a la creation du widget depuis le sidecar sf_rotation.txt (cmd set_rotation + reboot, comme audio/video_mode ; anti-boucle via dev.rotation dans /device.json). Renderer : bsApplyRotation mappe screen_rotation (normal/left/right/inverted) -> transform et desactive l'applyRotation CSS sur BrightSign. A VALIDER VISUELLEMENT (le transform du widget tourne-t-il bien AUSSI le plan video ? + sens rot90=horaire a confirmer).
    ' v36 = FIX rotation "toujours paysage" : bsFetchDevice (renderer) ne copiait PAS d.rotation dans _bsDevice -> _bsDevice.rotation undefined -> le garde `if(!cur) return` de bsApplyRotation bloquait TOUTE demande de rotation (meme piege que audio_out, documente). Ajout de la copie. Autorun inchange fonctionnellement (bump pour forcer RefreshAutorun a re-tirer le renderer corrige en 1 passe).
    ' v37 = ROTATION VALIDEE HW + neutralisation du 180 : 90° (rot90/rot270) tournent le plan video HW (valide snapshot DWS : voiture tournee, sens OK, remplit en 9:16). 180° (rot180) NON honore par BrightSign sur le plan video (valide snapshot : voiture reste a l'endroit ; le 180 ne change ni orientation ni dimensions -> rotation HW non enclenchee). Renderer : map inverted -> identity (au lieu de rot180) pour eviter une demi-rotation moche (overlay HTML seul). 180 = non supporte BrightSign, documente. Autorun inchange (bump pour re-tirer le renderer).
    ' v38 = OTA §XXX slice 2 (apply BrightSign) : nouvelle cmd "ota_apply" (DoOtaApply) = RefreshRenderer + RefreshAutorunForced + reboot, declenchee par le renderer (bsHandleOta) quand un deploiement APPROUVE l'autorise (fenetre install / force). RefreshAutorun scinde en gate config (boot) + RefreshAutorunForced (inconditionnel, pour l'OTA). Au boot suivant, signflow.js reporte BS_PLAYER_VERSION -> le serveur constate la montee de version = succes. Renderer plafonne les tentatives (anti-boucle reboot).
    ' v39 = ALIMENTATION ECRAN sans RS232 : cmd "video_output" (SetVideoOutputPower : roVideoMode.SetPowerSaveMode coupe/retablit le sync HDMI -> ecran en veille sur perte de signal, runtime sans reboot) + cmd "cec_power" (DoCecPower : roCecInterface.SendRawMessage Image View On &h04 / Standby &h36 vers la TV, adresse logique 0) = repli "ecran non pilotable" (auto-power heures d'ouverture / bouton CMS) + 3e type de pilotage display_config.type="cec". Hors chemin de boot (aucun risque de brick). A VALIDER HW (SetPowerSaveMode runtime + reception CEC selon TV).
    ' v40 = FUSEAU HORAIRE : cmd "set_timezone" (SetSystemTimeZone : roSystemTime.SetTimeZone puis
    ' reboot — seul moyen fiable de propager la TZ au roHtmlWidget/renderer). Le pilote auto-power
    ' OFFLINE (fenetres pre-calculees offline_power) vit dans le RENDERER signflow.js (tick minute,
    ' WS down > 3 min, applique via video_output/cec_power/rs232) — l'autorun ne change pas pour ca.
    ' Hors chemin de boot (aucun risque de brick).
    ' v41 = PULSE HDMI au rallumage (SetVideoOutputPower on) : SetMode(mode courant) apres
    ' SetPowerSaveMode(false) -> hotplug/re-handshake EDID pour les dalles sans wake-on-signal
    ' (constate HW : dalles multi-entrees ne re-scannant pas une entree dont le sync revient).
    ' v42 = NTP DU SITE : cmd "set_time_server" (SetSystemTimeServer : roNetworkConfiguration.
    ' SetTimeServer + Apply — runtime, persiste au reboot). Pousse par le renderer quand le sync
    ' merge cms_sites.ntp_server (migr 0071). Hors chemin de boot (aucun risque de brick).
    ' v43 = FIX NTP : SetTimeServer runtime SANS EFFET (constate HW, registre inchange) -> ecriture
    ' DIRECTE du registre networking/ts (voie validee HW via DWS) + runtime best-effort ensuite.
    ' v44 = ENTREE HDMI x3 : (a) STATUT (/hdmiin.json) GetHdmiInputStatus au boot + roHdmiInputChanged
    ' (vm.SetPort) -> device_present servi au renderer (ecran « No Signal Detected » AUTORITAIRE —
    ' les compteurs Chromium ne voient pas la perte de signal, constate HW 07-08) ; (b) page LOCALE
    ' /control (magasin, switch HDMI sans CMS) -> POST /queue {cmd:hdmi_input} ; (c) HandleHdmiInput
    ' REPURPOSE en relais renderer (sf_hdmicmd.json {mode,seq}, poll 2s signflow.js) — l'ancienne voie
    ' native roVideoPlayer n'est plus appelee (sur-allocation decodeur, v22/v23). GATE MODELE XT/4K/
    ' XD1230 partout (anti-brick : methode absente = crash non-catchable) -> HD/LS n'appellent rien.
    ' v45 = FIX statut HDMI-in PERIME : l'event roHdmiInputChanged ne tire PAS sur ce BOS (constate
    ' HW 07-08 : re-branchement jamais reflete -> le renderer re-masquait une image revenue) ->
    ' POLLING GetHdmiInputStatus time-gate 3 s dans la boucle (reveillee par le poll 1 s du renderer
    ' pendant un item hdmi_input). Meme lecon que SetTimeServer v43 (API/event runtime inerte).
    ' v46 = GATE de la page locale /control : cmd "hdmi_local_cfg" {enabled,pin} poussee par le
    ' renderer (display_config.local_control[_pin]) -> RAM only (PIN jamais ecrit/servi) ;
    ' HandleHdmiInput ignore la commande si page desactivee ou PIN incorrect (chemins CMS non
    ' concernes) ; page /control = champ PIN optionnel.
    ' v47 = UX PIN de /control (demande Ed 07-09) : 2 boutons toujours visibles, PIN demande par
    ' MODAL a la 1re action seulement (garde en JS-RAM de la page, jamais persiste) ; nouveau
    ' /controlst.json {enabled, pin_required, ack, nack} (AUCUN secret) = la page sait si un PIN
    ' est requis ET si la commande a ete acceptee/refusee (fin du refus silencieux : mauvais PIN
    ' -> compteur nack -> message "Wrong PIN" + re-invite).
    ' v48 = nom du player sur /control (demande Ed 07-09) : le renderer pousse player_name (sync)
    ' dans hdmi_local_cfg -> expose dans /controlst.json {name} -> sous-titre de la page.
    ' v49 = CPU/RAM au CMS (demande Ed 07-14) : MemCheck etend sf_mem.json (/mem.json) avec
    ' cpu_load1/cpu_ratio/ncpu via LDWS legacy/loadavg + legacy/stat (sonde LAN concluante BOS
    ' 9.1.132 ; meme canal brick-proof que meminfo). Le renderer 2.0.18 lit /mem.json -> heartbeat
    ' cpu/ram (usage %) -> cms_players.cpu_usage/ram_usage (fin du ram=deviceMemory Go trompeur).
    ' v50 = (a) VOD LAN : socket UDP :7779 (roDatagramSocket, API PROUVEE HW v24/v26) -> les
    ' broadcasts vod_play du Store Control Panel (§19.3, vod_lan.js Electron) sont RELAYES BRUTS
    ' au renderer via sf_vodcmd.json {seq,packet} (poll /vodcmd.json 2 s) ; TOUTE la validation
    ' (cible, fraicheur, nonce, HMAC-SHA256 via crypto.subtle — origine http://127.0.0.1 = secure
    ' context) vit dans le RENDERER : AUCUNE nouvelle API BrightScript risquee (roHMAC non prouve
    ' sur ce BOS = risque crash-loop non-catchable). (b) FIX ecran noir post-reboot au retour de
    ' HDMI-in (constate HW 16/07, XT1144) : le power_on de re-assertion (repousse par le serveur
    ' a CHAQUE reconnexion WS — et le stop HDMI reconnecte via requestResync) re-pulsait SetMode
    ' alors que la sortie etait DEJA allumee -> le pulse reinitialise le plan video HW et tue le
    ' contenu fraichement repris. SetVideoOutputPower ne pulse plus que sur une VRAIE transition
    ' off->on (etat suivi en RAM m.videoOutOff, defaut false = sortie allumee au boot).
    ' v51 = VOD LAN : buffer ROULANT des 8 derniers paquets dans sf_vodcmd.json {seq,packets[]}
    ' (retro-compat lue par renderer >= 2.0.21). Le slot unique v50 + rate-limit 250 ms JETAIENT
    ' les paquets d'une rafale multi-cibles (1 paquet UDP par player cible, dos a dos) -> un BS
    ' au milieu de la rafale ne voyait jamais SON paquet (vu HW par Ed 17/07 : VOD site entier =
    ' BS inertes, cible unique = OK). Anti-flood par COMPTEUR (16 paquets/s) au lieu du drop.
    ' v52 = Step B (#4) : RefreshRenderer tire le renderer EPINGLE (/player-chromeos/v/<ver>/) via
    ' ResolveRendererPin (GET /updates/renderer-version) -> une MAJ serveur ne met plus le parc a jour
    ' au reboot ; l'admin epingle la version par DEPLOIEMENT (bac a sables). Backward-safe (pin non
    ' resolu / vieux serveur -> chemin plat historique). Aucune primitive BrightScript nouvelle.
    ' v53 = decision 3 "canal unique" (#4) : la version epinglee peut desormais etre servie par le
    ' CATALOGUE (zip importe -> extraction MinIO, route /api/v1/updates/renderers/brightsign/<ver>/)
    ' et plus seulement par le chemin statique v/<ver>/. RefreshRenderer sonde UNE fois par boot
    ' (catalogue d'abord, statique ensuite) ; les deux absents -> copies SD GARDEES telles quelles
    ' (jamais de repli plat quand un pin existe = pas de MAJ involontaire). Anti-brick inchange :
    ' echec HTTP = fichier SD intact ; primitives deja utilisees partout (roUrlTransfer/GetToFile).
    ' v54 = renderer absent au boot (carte faite a la main, reseau lent) : EnsureRenderer en TEMP +
    ' code 200, reessai toutes les 30 s dans la boucle puis m.html.SetUrl (rechargement). Issue #2.
    dev.autorun_ver = "54"
    ' P2.1 : occupation disque de la SD (le renderer ne peut pas la lire — objets JS désactivés).
    ' roStorageInfo = API BrightScript STANDARD (≠ les API JS-bridge qui crashaient). GUARDÉ : si
    ' l'objet/la mesure échoue, on n'expose simplement pas le champ (pas de crash). Snapshot au boot
    ' (après ScanCache → reflète le cache présent) ; mise à jour live = raffinement futur. ⚠️ Le chemin
    ' est le VOLUME BrightScript "SD:" (le POSIX "/storage/sd" renvoie invalid sur ce firmware — sonde HW).
    si = CreateObject("roStorageInfo", "SD:")
    if si <> invalid then
        totMb = si.GetSizeInMegabytes()
        freMb = si.GetFreeInMegabytes()
        if totMb <> invalid and freMb <> invalid and totMb > 0 then
            dev.disk_ratio    = (totMb - freMb) / totMb
            dev.disk_total_mb = totMb
        end if
    end if
    nc = CreateObject("roNetworkConfiguration", 0)
    if nc <> invalid then
        cur = nc.GetCurrentConfig()
        if type(cur) = "roAssociativeArray" then
            if cur.ip4_address  <> invalid then
                dev.ip  = cur.ip4_address
                m.lanIp = cur.ip4_address     ' le DWS 9.x bind sur l'IP LAN (pas 127.0.0.1) -> screenshot l'utilise
            end if
            if cur.ethernet_mac <> invalid then dev.mac = cur.ethernet_mac
        end if
    end if
    ' Résolutions : mode courant + meilleur mode supporté par l'ÉCRAN (EDID) → le serveur sait si
    ' le player fait du 4K et sert la bonne rendition (idée Ed : interroger le player, pas un
    ' mapping modèle codé en dur). best ex. "3840x2160x60p" sur un écran 4K.
    if vm <> invalid then
        dev.video_mode = vm.GetMode()
        dev.best_mode  = vm.GetBestMode("hdmi")
    end if
    dev.audio_out = AudioOutName()                ' sortie audio courante (sidecar) -> le renderer compare l'état RÉEL (anti-boucle)
    dev.rotation  = RotationTransform()           ' rotation ecran courante (sidecar) -> anti-boucle renderer (idem audio_out)
    m.server.AddGetFromString({ url_path: "/device.json", content_type: "application/json", body: FormatJson(dev) })
    ' Sonde RS232 : HandleDisplayCommand écrit son fil d'Ariane (étape par étape) dans sf_rs232.json,
    ' interrogeable via le LAN (/rs232.json) -> debug du pilotage écran sans telnet (désactivé en prod).
    ' On NE réinitialise PAS à "idle" si le fichier existe déjà -> le dernier step SURVIT à un reboot
    ' (ex. crash sur une commande) → on peut lire la ligne fautive après coup.
    if ReadAsciiFile("sf_rs232.json") = "" then WriteAsciiFile("sf_rs232.json", FormatJson({ step: "idle" }))
    m.server.AddGetFromFile({ url_path: "/rs232.json", content_type: "application/json", filename: "sf_rs232.json" })
    ' Sonde HDMI-in : StartHdmiInput écrit son fil d'Ariane dans sf_hdmi.json (interrogeable via /hdmi.json)
    ' -> on voit EXACTEMENT où ça bloque quand "l'entree HDMI ne s'affiche pas" : modele sans entree
    ' (roVideoInput invalid), PlayFile qui echoue, ou PlayFile=true mais rien a l'ecran (=> z-order /
    ' transparence / contention decodeur). SURVIT au reboot (idle seulement si fichier absent).
    if ReadAsciiFile("sf_hdmi.json") = "" then WriteAsciiFile("sf_hdmi.json", FormatJson({ step: "idle" }))
    m.server.AddGetFromFile({ url_path: "/hdmi.json", content_type: "application/json", filename: "sf_hdmi.json" })

    ' P2.5 : /mem.json sert le DERNIER releve memoire (ecrit par MemCheck dans sf_mem.json). Route FICHIER
    ' (relue a chaque GET) -> reflete le live sans re-enregistrer de route. Placeholder au boot (avant la
    ' 1re mesure) pour ne pas servir un 404. Le renderer pourra le lire plus tard (parite disque) ; sert
    ' surtout a valider le watchdog par curl LAN sans armer le reboot.
    WriteAsciiFile("sf_mem.json", FormatJson({ avail_ratio: -1, measured: false }))
    m.server.AddGetFromFile({ url_path: "/mem.json", content_type: "application/json", filename: "sf_mem.json" })

    ' ── PHASE 1 LAN SYNC — sonde reception UDP (slave-only) ──────────────────────
    ' Le groupe "LAN Sync" mixe des players UDP : NUC (maitre, broadcast :7778) + RPi/AOpen (Electron)
    ' + Minix (Android LanSyncController.kt). Les BrightSign y sont muets. ETAPE 1 = PROUVER qu'on
    ' RECOIT le broadcast sync_play du NUC sur ce firmware. roDatagramSocket : API confirmee doc
    ' BrightSign (bindToLocalPort/setPort ; paquets = roDatagramEvent.getString/getSourceHost) MAIS
    ' JAMAIS utilisee dans notre autorun -> non prouvee HW = risque de brick (methode absente au runtime
    ' = non-catchable). D'ou le GATE sur le SERIE : seul le HD1024 cree le socket ; l'autre BrightSign
    ' telecharge le meme autorun, il COMPILE (syntaxe OK) mais saute le socket -> brick limite a 1 machine.
    ' Aussi activable par cfg.lan_sync=true. Le fil d'Ariane /lan.json = ce qu'on capte (validable curl LAN).
    m.lanSock = invalid
    m.lanCount = 0
    m.lanBound = false
    ' Election du meilleur leader VIVANT (parmi self+peers) : priorite superieure, sinon id_player
    ' plus petit (meme tie-break que sync_lan.js Electron). Le NUC maitre = priorite 1 -> gagne
    ' toujours tant qu'il vit. FAILOVER (item 1) : chaque peer est horodate (m.lanClock, monotone) ;
    ' un peer silencieux > 20s est considere MORT et sort de l'election -> un survivant est promu
    ' (tie-break d'id parmi les pri=0). Hysterese de promotion 35s (> TTL) = anti flip-flop : on ne
    ' s'auto-promeut que si un noeud nous surpassant n'a pas parle depuis 35s (trou transitoire tolere).
    m.lanPeers = {}              ' id-str -> { id, pri, seen (ms m.lanClock) } : liveness du groupe
    m.lanSelfPri = 0            ' priorite d'election de CE player (1 si maitre DESIGNE, 0 sinon)
    m.lanSelfPlay = ""         ' dernier sync_play BRUT du renderer (diffuse SEULEMENT si eff. maitre)
    m.lanLastOutrankMs = 0     ' derniere fois qu'un noeud nous surpassant a parle (hysterese promo)
    m.lanEffLeaderId = 0       ' leader VIVANT elu (self ou peer) -> filtre les sync_play a suivre
    m.lanEffMaster = false     ' CE player est-il le leader effectif ? -> diffuse son play, ne suit personne
    m.lanPrevLeaderId = -1     ' journalisation des CHANGEMENTS d'election (evite le spam SD)
    m.lanPrevMaster = false
    m.lanClock = CreateObject("roTimespan")   ' horloge monotone (ms) pour la liveness des peers
    m.lanClock.Mark()
    ' ── Item 2 (USURE FLASH SD) : fichiers LAN a forte frequence en RAM (volume tmp:) ────────────
    ' sf_lan.json (~1Hz cote slave qui suit le maitre) + sf_landiag.json (~1Hz cote maitre qui diffuse)
    ' tapaient la SD a chaque tick (~86k ecritures/j). On les ecrit sur le volume RAM tmp: (tmpfs) ->
    ' 0 ecriture SD pour la sync LAN. SONDE au boot : ecrit/relit un fichier test ; si tmp: absent/KO ->
    ' repli SD (m.ramPrefix="") -> brick-proof, degrade gracieux (comportement d'avant). Le prefixe est
    ' applique a l'ECRITURE ET a l'enregistrement AddGetFromFile (le roHttpServer sert depuis tmp:).
    m.ramPrefix = ""
    WriteAsciiFile("tmp:/sf_probe.txt", "ok")
    if ReadAsciiFile("tmp:/sf_probe.txt") = "ok" then m.ramPrefix = "tmp:/"
    DeleteFile("tmp:/sf_probe.txt")
    m.lanFile     = m.ramPrefix + "sf_lan.json"      ' churn slave -> RAM si dispo
    m.lanDiagFile = m.ramPrefix + "sf_landiag.json"  ' churn maitre -> RAM si dispo
    m.lanElFile   = m.ramPrefix + "sf_lanel.json"    ' faible churn (changements) -> memes regles
    ' API roDatagramSocket PROUVEE HW sur le HD1024 (pagani/HD4) -> etendue au XT1144 (malibu/XT4,
    ' meme BOS 9.1.132 ; l'API reseau BrightScript est OS-level, pas SoC-specifique). Les 2 seuls
    ' BrightSign du parc, tous deux dans un groupe LAN. cfg.lan_sync = echappatoire generique.
    lanEnable = (m.serial = "TKF242002313") or (m.serial = "D7E9AA003555") or (cfg.lan_sync = true)
    ' Placeholder "waiting" (pas de type=sync_play) -> le renderer l'ignore tant qu'aucun sync_play
    ' du leader n'a ete capte. Ensuite HandleLanDatagram ecrit le paquet BRUT du leader (renderer le parse).
    WriteAsciiFile(m.lanFile, FormatJson({ probe: "waiting", enabled: lanEnable, serial: m.serial }))
    m.server.AddGetFromFile({ url_path: "/lan.json", content_type: "application/json", filename: m.lanFile })
    if lanEnable then
        m.lanSock = CreateObject("roDatagramSocket")
        if m.lanSock <> invalid then
            m.lanSock.SetPort(m.port)
            m.lanBound = m.lanSock.BindToLocalPort(7778)
            fail = ""
            if not m.lanBound then fail = m.lanSock.GetFailureReason()
            WriteAsciiFile(m.lanFile, FormatJson({ probe: "bound", enabled: true, bound: m.lanBound, fail: fail, serial: m.serial, count: 0, ram: m.ramPrefix }))
        else
            WriteAsciiFile(m.lanFile, FormatJson({ probe: "no_socket", enabled: true, serial: m.serial }))
        end if
    end if

    ' ── v50 : VOD LAN (Store Control Panel §19.3) — relais UDP :7779 -> renderer ────────────────
    ' Le panneau local d'un player Electron declenche la VOD des pairs par broadcast UDP :7779
    ' signe HMAC (vod_lan.js). L'autorun RELAIE le paquet BRUT au renderer (sf_vodcmd.json, poll
    ' /vodcmd.json 2 s) — la validation complete (cible/fraicheur/nonce/HMAC) vit dans le renderer
    ' (crypto.subtle, origine localhost = secure context). Socket TOUJOURS cree (roDatagramSocket
    ' = API prouvee HW v24/v26 sur les 2 modeles du parc) ; rate-limit d'ecriture 250 ms (un
    ' flood LAN ne churne pas la RAM/CPU). Fichier en RAM (m.ramPrefix, pattern v31).
    m.vodCmdSeq = 0
    m.vodCmdFile = m.ramPrefix + "sf_vodcmd.json"
    WriteAsciiFile(m.vodCmdFile, FormatJson({ seq: 0, packets: [] }))
    m.server.AddGetFromFile({ url_path: "/vodcmd.json", content_type: "application/json", filename: m.vodCmdFile })
    ' v51 : buffer roulant (8 paquets) + anti-flood compteur — cf. changelog v51.
    m.vodPackets = []
    m.vodBurst = 0
    m.vodSpan = CreateObject("roTimespan")
    m.vodSpan.Mark()
    m.vodSock = CreateObject("roDatagramSocket")
    m.vodBound = false
    if m.vodSock <> invalid then
        m.vodSock.SetPort(m.port)
        m.vodBound = m.vodSock.BindToLocalPort(7779)
    end if

    ' ── v44 : STATUT D'ENTREE HDMI -> /hdmiin.json (ecran « No Signal » du renderer) ─────────────
    ' Source AUTORITAIRE de presence du signal : les compteurs Chromium ne voient PAS la perte
    ' (constate HW 07-08 : source debranchee -> videoWidth reste verrouille -> ecran noir sans
    ' message). roVideoMode.GetHdmiInputStatus() -> device_present/width/height (invalid = pas de
    ' source) + event roHdmiInputChanged (hotplug) via vm.SetPort -> re-ecriture du fichier.
    ' GATE MODELE (anti-brick : une methode absente = crash NON-catchable, lecon v7) : gamme XT /
    ' 4K114x-4K124x / XD1230 uniquement (miroir bsHasHdmiInput) -> le HD1024 ne l'appelle JAMAIS.
    ' Fichier en RAM (m.ramPrefix, pattern v31) : re-ecrit a chaque hotplug seulement (0 churn SD).
    m.vm = vm
    m.hdmiInCapable = false
    mdl = ""
    if dev.model <> invalid then mdl = UCase(dev.model)
    if Left(mdl, 2) = "XT" or Left(mdl, 4) = "4K11" or Left(mdl, 4) = "4K12" or mdl = "XD1230" then m.hdmiInCapable = true
    if m.hdmiInCapable and vm <> invalid then
        vm.SetPort(m.port)   ' roHdmiInputChanged -> boucle principale (types inconnus ignores)
        m.hdmiInFile = m.ramPrefix + "sf_hdmiin.json"
        m.server.AddGetFromFile({ url_path: "/hdmiin.json", content_type: "application/json", filename: m.hdmiInFile })
        WriteHdmiInStatus()   ' etat initial (couvre le debranchement PERMANENT : item sans source)
        ' Page LOCALE de controle (demande Ed : le magasin n'a RIEN en local pour switcher — le
        ' Store Control Panel ne tourne pas sur BS). /control = 2 boutons (switch HDMI / playlist)
        ' -> POST /queue {cmd:"hdmi_input"} -> HandleHdmiInput ecrit sf_hdmicmd.json {mode,seq}
        ' -> le RENDERER poll /hdmicmd.json (2s) et execute la voie <video tv:...> (la seule qui
        ' marche — la voie native roVideoPlayer sur-alloue le decodeur, prouve HW v22/v23).
        m.hdmiCmdSeq = 0
        m.hdmiCmdFile = m.ramPrefix + "sf_hdmicmd.json"
        WriteAsciiFile(m.hdmiCmdFile, FormatJson({ mode: "", seq: 0 }))
        m.server.AddGetFromFile({ url_path: "/hdmicmd.json", content_type: "application/json", filename: m.hdmiCmdFile })
        m.server.AddGetFromString({ url_path: "/control", content_type: "text/html; charset=utf-8", body: HdmiControlPage() })
        ' v45 : time-gate du POLLING de statut (l'event roHdmiInputChanged ne tire PAS sur ce BOS —
        ' constate HW 07-08 : re-branchement jamais reflete dans /hdmiin.json. Meme lecon que
        ' SetTimeServer v43 : API/event runtime inerte). Le polling est la source fiable, l'event
        ' reste en bonus. Cree UNIQUEMENT si capable (le time-gate de la boucle est imbrique).
        m.hdmiSpan = CreateObject("roTimespan")
        m.hdmiSpan.Mark()
        ' v46 : gate du controle LOCAL (/control). Le renderer pousse display_config.local_control
        ' + local_control_pin via cmd "hdmi_local_cfg" (RAM uniquement : le PIN n'est JAMAIS ecrit
        ' dans un fichier servi en HTTP — un curieux du LAN ne peut pas le lire). Defauts = page
        ' active sans PIN (retro-compat + boot avant le 1er push du renderer).
        m.hdmiLocalEnabled = true
        m.hdmiLocalPin = ""
        m.hdmiLocalName = ""
        ' v47 : etat du controle local servi a la page (AUCUN secret : pin_required = booleen
        ' seulement) + compteurs ack/nack = feedback accepte/refuse (fin du refus silencieux).
        m.hdmiLocalAck = 0
        m.hdmiLocalNack = 0
        m.hdmiCtlFile = m.ramPrefix + "sf_controlst.json"
        WriteControlState()
        m.server.AddGetFromFile({ url_path: "/controlst.json", content_type: "application/json", filename: m.hdmiCtlFile })
    end if

    ' ── PHASE 2b — EMISSION (BrightSign MAITRE d'un groupe) ──────────────────────
    ' Emission broadcast PROUVEE HW (Step A : sendTo->255.255.255.255 recu par l'autre BrightSign). Le
    ' renderer POST son etat (POST /queue {lan_report:{id,group,master,play}}) -> l'autorun diffuse
    ' sync_announce (presence, 3s) + relaie le sync_play du renderer si lan_sync_master (0 re-serialisation
    ' -> switch_at/sent_at construits en JS = precision pleine). Slave : n'emet QUE l'announce (priorite 0)
    ' et SUIT via la reception (Phase 2). Piloté par display_config.lan_sync_master (pas de gate serie).
    m.lanMyId = 0             ' id_player (rapporte par le renderer) -> announce + tie-break, self-guard
    m.lanReportGroup = ""     ' groupe LAN (du renderer) -> filtre reception + announce
    m.lanSendCount = 0
    m.lanAnnSpan = CreateObject("roTimespan")
    m.lanAnnSpan.Mark()
    WriteAsciiFile(m.lanDiagFile, FormatJson({ send: "init" }))
    m.server.AddGetFromFile({ url_path: "/landiag.json", content_type: "application/json", filename: m.lanDiagFile })
    ' Etat d'election (validation curl LAN sans cycle SD : qui est le leader effectif ?). Ecrit
    ' seulement aux CHANGEMENTS d'election (LanElect) -> pas de churn SD.
    WriteAsciiFile(m.lanElFile, FormatJson({ elect: "init" }))
    m.server.AddGetFromFile({ url_path: "/lanelect.json", content_type: "application/json", filename: m.lanElFile })

    ' ── roHtmlWidget : page en ORIGINE LOCALE ───────────────────────────────
    url = "http://127.0.0.1:8888/?backend=" + backend + "&token=" + token
    r = CreateObject("roRectangle", 0, 0, w, h)
    config = {
        url: url
        javascript_enabled: true
        brightsign_js_objects_enabled: true
        storage_path: "SD:/sf_html_storage"
        security_params: { websecurity: false }
    }
    if cfg.debug = true then config.inspector_server = { ip_addr: "0.0.0.0", port: 2999 }
    ' ── Sortie audio (point AV 2/3) : route le PCM du <video> du widget vers une sortie nommee ──
    ' pcm_audio_outputs = cle de config DOCUMENTEE (non-deprecated) du roHtmlWidget : array de noms de
    ' sortie (cf. roAudioOutput : "hdmi"/"analog"/"spdif"/"usb"). Sidecar sf_audioout.txt ecrit par la cmd
    ' audio_output (+ reboot, comme set_video_mode car le re-route runtime est deprecate). VALIDE contre
    ' une whitelist -> un sidecar absent/corrompu ne pose PAS la cle = comportement actuel inchange (defaut
    ' systeme HDMI) -> 0 impact sur le parc, seul un player explicitement configure change de sortie.
    ' TOUJOURS poser pcm_audio_outputs EXPLICITEMENT (y compris "hdmi") : sinon, en passant de analog→hdmi,
    ' BrightSign GARDE le routage analog precedent (le retrait de la cle ne re-route pas vers HDMI). On force
    ' donc la sortie a chaque boot. AudioOutName() = sidecar valide ou "hdmi" par defaut → toujours une valeur
    ' valide (jamais de cle corrompue → pas de risque boot).
    config.pcm_audio_outputs = [AudioOutName()]
    ' ── Rotation ecran (NATIVE) : le transform CSS du renderer ne tourne QUE la couche HTML, PAS le plan
    ' video HW (<video hwz>) -> rotation cassee sur BrightSign. La cle DOCUMENTEE "transform" du roHtmlWidget
    ' ("rot90"/"rot180"/"rot270") tourne TOUT le rendu du widget (graphique + trou video) au niveau OS.
    ' Fixee a la CREATION du widget -> changer de rotation = sidecar sf_rotation.txt + reboot (comme audio/
    ' video_mode). RotationTransform() = sidecar valide (whitelist) ou "identity" -> jamais de cle corrompue.
    rt = RotationTransform()
    if rt <> "identity" then config.transform = rt
    m.htmlUrl = url                              ' v54 : rechargement une fois le renderer recupere
    m.html = CreateObject("roHtmlWidget", r, config)
    m.html.SetPort(m.port)
    m.html.AllowJavaScriptUrls({ all: "*" })

    sleep(4000)
    m.html.Show()
    ApplyAudioVolMute()                          ' re-applique volume/mute persistes (sidecars) au boot

    ' Tier 1.5 : MAJ des renderers (sf_rnd_*) qui ont changé côté prod. SYNCHRONE -> GetToFile ne
    ' poste PAS sur le port -> AUCUN routage roUrlEvent (= ce qui crashait l'ancienne 1.5 async).
    ' Lancé APRÈS Show() (contenu déjà peint depuis la SD) -> n'allonge pas l'offline-cold-boot.
    RefreshRenderer()
    ' P1.2 : auto-update de l'autorun lui-même (gate config "autorun_autoupdate":true). MÊME timing
    ' (post-Show, synchrone) -> n'allonge pas l'offline-cold-boot. Effectif au prochain boot.
    RefreshAutorun()

    ' ── Boucle d'événements ─────────────────────────────────────────────────
    ' P2.5 : watchdog memoire RE-ARME mais via un canal BRICK-PROOF (LDWS HTTP, pas getLoadStatistics).
    ' L'ancien 1.4 crashait car `roDeviceInfo.GetGeneralMemoryLevel()` (API Roku, PAS BrightSign) +
    ' `getLoadStatistics()` n'existaient pas -> "Member function not found" non-catchable -> LED rouge 10x.
    ' Ici MemCheck() ne touche AUCUNE methode BrightScript non-prouvee (roUrlTransfer + ParseJson seulement).
    ' TIME-GATE : la boucle se reveille a CHAQUE requete HTTP (le renderer poll /cached.json ~15s) -> on ne
    ' peut pas se fier au seul timeout de wait(). On mesure roTimespan a chaque tour et on ne lance MemCheck
    ' que si memIntervalMs ecoule. wait(20000) garantit un reveil meme en silence total (parc idle).
    m.memSpan = CreateObject("roTimespan")
    m.memSpan.Mark()
    m.rndSpan = CreateObject("roTimespan")
    m.rndSpan.Mark()
    while true
        msg = wait(20000, m.port)
        t = type(msg)
        if t = "roHttpEvent" then
            HandleHttpEvent(msg)
        else if t = "roUrlEvent" then
            HandleUrlEvent(msg)
        else if t = "roHtmlWidgetEvent" then
            data = msg.GetData()
            if type(data) = "roAssociativeArray" and data.sf_cmd = "reboot" then RebootSystem()
        else if t = "roDatagramEvent" then
            HandleLanDatagram(msg)
        else if t = "roHdmiInputChanged" then
            WriteHdmiInStatus()   ' v44 : hotplug entree HDMI -> maj /hdmiin.json (renderer No Signal)
        end if
        ' Time-gate (s'execute apres un evenement OU un timeout) : mesure memoire periodique +
        ' relance PumpQueue (P2.8 : reprend le prefetch quand une fenetre horaire s'ouvre en idle ;
        ' no-op si un DL est deja en vol ou les files sont vides).
        ' v54 : renderer absent de la SD au boot (carte faite a la main + reseau lent) -> nouvel essai
        ' toutes les 30 s ; complet -> la page est rechargee (sinon "Connecting..." a vie). ifs
        ' IMBRIQUES (BrightScript ne court-circuite pas).
        if m.rndMissing = true then
            if m.rndSpan.TotalMilliseconds() >= 30000 then
                m.rndSpan.Mark()
                if EnsureRenderer() then
                    m.rndMissing = false
                    print "renderer complet -> rechargement de la page"
                    m.html.SetUrl(m.htmlUrl)
                end if
            end if
        end if
        if m.memSpan.TotalMilliseconds() >= m.memIntervalMs then
            m.memSpan.Mark()
            MemCheck()
            PumpQueue()
        end if
        ' v45 : statut HDMI-in par POLLING time-gate 3s (l'event roHdmiInputChanged ne tire pas
        ' sur ce BOS, constate HW). La boucle se reveille a chaque poll HTTP du renderer (1 s
        ' pendant un item hdmi_input) -> latence effective ~3 s ; ifs IMBRIQUES (pas de and :
        ' BrightScript ne court-circuite pas, m.hdmiSpan n'existe que si capable).
        if m.hdmiInCapable = true then
            if m.hdmiSpan.TotalMilliseconds() >= 3000 then
                m.hdmiSpan.Mark()
                WriteHdmiInStatus()
            end if
        end if
        ' PHASE 2b + FAILOVER : toutes les 3s, re-election (detecte un peer devenu silencieux meme SANS
        ' paquet entrant -> promotion d'un survivant) + emission de presence (sync_announce). Le sync_play
        ' (si maitre effectif) est relaye a chaque report du renderer (HandleLanReport).
        if m.lanMyId > 0 and m.lanSock <> invalid and m.lanAnnSpan.TotalMilliseconds() >= 3000 then
            m.lanAnnSpan.Mark()
            LanElect()
            LanBroadcastAnnounce()
        end if
    end while
End Sub

' ── v44 : ecrit le statut de l'entree HDMI (RAM) -> servi /hdmiin.json ───────────────────────
' Appele au boot + a chaque roHdmiInputChanged (hotplug). GARDES : jamais appele si le modele
' n'a pas d'entree (gate m.hdmiInCapable, boot) ; GetHdmiInputStatus() renvoie invalid quand
' aucune source n'est branchee -> device_present:false. Le renderer poll /hdmiin.json pendant
' un item/override hdmi_input (ecran « No Signal Detected »).
Sub WriteHdmiInStatus()
    m = GetGlobalAA()
    if m.hdmiInCapable <> true or m.vm = invalid then return
    st = { device_present: false }
    aa = m.vm.GetHdmiInputStatus()
    if type(aa) = "roAssociativeArray" then
        if aa.device_present <> invalid then st.device_present = aa.device_present
        if aa.width <> invalid then st.width = aa.width
        if aa.height <> invalid then st.height = aa.height
    end if
    WriteAsciiFile(m.hdmiInFile, FormatJson(st))
End Sub

' ── v44 : page LOCALE /control (magasin) — switch HDMI-in sans CMS ───────────────────────────
' Servie par le roHttpServer :8888 du player (LAN). 2 boutons -> POST /queue {cmd:"hdmi_input",
' params:{mode}} (fire-and-forget, comme le renderer : pas de reponse HTTP fiable sur ce
' firmware). Anglais (regle : tout ce qui tourne SUR le device). AUCUNE quote double dans le
' HTML/JS (attributs et chaines en quotes simples) -> concatenation BrightScript sans escaping.
Function HdmiControlPage() As String
    h = "<!doctype html><html><head><meta charset='utf-8'>"
    h = h + "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    h = h + "<title>Vitrail HDMI Control</title><style>"
    h = h + "body{margin:0;font-family:system-ui,sans-serif;background:#1C1E2A;color:#eee;display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;gap:20px}"
    h = h + "h2{font-weight:600;margin:0 0 8px}"
    h = h + "button{width:300px;padding:22px;font-size:18px;font-weight:600;border:0;border-radius:12px;cursor:pointer}"
    h = h + "#on{background:#4875B4;color:#fff}#off{background:#39445a;color:#fff}"
    h = h + "#st{font-size:14px;color:#9ca3af;min-height:20px}"
    h = h + "</style></head><body>"
    h = h + "<h2>HDMI Source Control</h2>"
    h = h + "<p id='pname' style='color:#9ca3af;font-size:15px;margin:-14px 0 0'></p>"
    h = h + "<button id='on'>Switch to HDMI input</button>"
    h = h + "<button id='off'>Back to playlist</button>"
    h = h + "<p id='st'></p>"
    ' v47 : PIN demande par MODAL a la 1re action seulement (si pin_required via /controlst.json).
    ' Le code saisi vit dans une variable JS de la page (jamais persiste) ; mauvais code = le
    ' compteur nack bouge -> message Wrong PIN + re-invite. Aucun secret ne transite en GET.
    h = h + "<div id='ov' style='display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);align-items:center;justify-content:center'>"
    h = h + "<div style='background:#252938;padding:28px;border-radius:14px;display:flex;flex-direction:column;gap:14px;align-items:center;width:320px'>"
    h = h + "<b>Enter PIN</b>"
    h = h + "<input id='pin' type='password' inputmode='numeric' maxlength='6' autocomplete='off' style='width:200px;padding:12px;font-size:20px;letter-spacing:8px;border:0;border-radius:10px;text-align:center;background:#2a3040;color:#eee'>"
    h = h + "<p id='perr' style='color:#f87171;font-size:13px;min-height:16px;margin:0'></p>"
    h = h + "<div style='display:flex;gap:10px'>"
    h = h + "<button id='ok' style='width:120px;padding:12px;background:#4875B4;color:#fff'>OK</button>"
    h = h + "<button id='cancel' style='width:120px;padding:12px;background:#39445a;color:#fff'>Cancel</button>"
    h = h + "</div></div></div>"
    h = h + "<script>"
    h = h + "var pin='';var pend='';var cfg={enabled:true,pin_required:false,ack:0,nack:0};"
    h = h + "function refresh(cb){fetch('/controlst.json').then(function(r){return r.json()}).then(function(j){cfg=j;if(cfg.name){document.getElementById('pname').textContent=cfg.name;document.title=cfg.name+' — HDMI Control';}if(cfg.enabled===false){document.getElementById('st').textContent='Local control is disabled on this player.';}if(cb)cb();}).catch(function(){if(cb)cb();});}"
    h = h + "function openModal(mo,err){pend=mo;document.getElementById('perr').textContent=err||'';document.getElementById('pin').value='';document.getElementById('ov').style.display='flex';document.getElementById('pin').focus();}"
    h = h + "function closeModal(){document.getElementById('ov').style.display='none';}"
    h = h + "function doSend(mo){var a0=cfg.ack||0,n0=cfg.nack||0;"
    h = h + "fetch('/queue',{method:'POST',body:JSON.stringify({cmd:'hdmi_input',params:{mode:mo,pin:pin}})}).catch(function(){});"
    h = h + "document.getElementById('st').textContent='Sending...';"
    h = h + "setTimeout(function(){refresh(function(){"
    h = h + "if((cfg.nack||0)>n0){pin='';openModal(mo,'Wrong PIN');document.getElementById('st').textContent='';}"
    h = h + "else if((cfg.ack||0)>a0){document.getElementById('st').textContent=(mo==='stop'?'Back to playlist':'Switching to HDMI input')+' — accepted at '+new Date().toLocaleTimeString();}"
    h = h + "else{document.getElementById('st').textContent='No response — is local control enabled?';}"
    h = h + "});},900);}"
    h = h + "function act(mo){if(cfg.pin_required&&!pin){openModal(mo,'');}else{doSend(mo);}}"
    h = h + "document.getElementById('on').onclick=function(){act('start')};"
    h = h + "document.getElementById('off').onclick=function(){act('stop')};"
    h = h + "document.getElementById('ok').onclick=function(){pin=document.getElementById('pin').value||'';closeModal();doSend(pend);};"
    h = h + "document.getElementById('cancel').onclick=closeModal;"
    h = h + "document.getElementById('pin').addEventListener('keydown',function(e){if(e.key==='Enter'){document.getElementById('ok').onclick();}});"
    h = h + "refresh();"
    h = h + "</script></body></html>"
    return h
End Function

' ── v51 : relais des paquets VOD LAN (:7779) vers le renderer ─────────────────────────────────
' Buffer ROULANT des 8 derniers paquets BRUTS dans sf_vodcmd.json {seq, packets:[{seq,packet}]}
' (RAM). Le renderer (>= 2.0.21) poll /vodcmd.json (2 s) et traite TOUS les paquets de seq
' superieur au dernier vu, apres validation complete (cible/fraicheur/nonce/HMAC) — une rafale
' multi-cibles (1 paquet UDP par player) n'ecrase plus le paquet qui cible CE BS (bug v50 vu HW
' 17/07). Anti-flood : 16 paquets max par fenetre glissante de 1 s (une rafale legitime = 1
' paquet/cible), le drop temporel 250 ms qui jetait la rafale est supprime. AUCUN parsing
' au-dela du ParseJson deja fait par l'appelant -> zero API nouvelle, zero risque boot.
Sub HandleVodDatagram(raw As String)
    m = GetGlobalAA()
    if m.vodCmdFile = invalid then return
    if m.vodPackets = invalid then return
    if m.vodSpan <> invalid then
        if m.vodSpan.TotalMilliseconds() >= 1000 then
            m.vodSpan.Mark()
            m.vodBurst = 0
        end if
    end if
    if m.vodBurst >= 16 then return
    m.vodBurst = m.vodBurst + 1
    m.vodCmdSeq = m.vodCmdSeq + 1
    m.vodPackets.Push({ seq: m.vodCmdSeq, packet: raw })
    if m.vodPackets.Count() > 8 then m.vodPackets.Shift()
    WriteAsciiFile(m.vodCmdFile, FormatJson({ seq: m.vodCmdSeq, packets: m.vodPackets }))
End Sub

' ── PHASE 1 LAN SYNC — handler de reception (sonde) ──────────────────────────
' Chaque paquet UDP recu sur :7778 = un roDatagramEvent. On extrait la charge (getString), la
' source (getSourceHost/getSourcePort) et on parse le JSON du protocole sync_lan pour confirmer
' qu'on capte bien le sync_play/sync_announce du maitre (NUC) avec le bon "group". Resume ecrit
' dans sf_lan.json (servi /lan.json) -> validable par curl LAN sans cycle SD. ParseJson garde par
' type-check (paquet non-JSON = invalid = ignore). Acces "type"/"group" en bracket (evite tout
' conflit avec le builtin type()). AUCUNE mutation du contenu : sonde en lecture pure.
Sub HandleLanDatagram(ev As Object)
    s = ev.GetString()
    if type(s) <> "String" or s = "" then return
    parsed = ParseJson(s)
    if type(parsed) <> "roAssociativeArray" then return
    ' v50 : paquet VOD LAN (socket :7779, cmd="vod_play") -> relais BRUT au renderer qui fait
    ' TOUTE la validation (cible/fraicheur/nonce/HMAC via crypto.subtle). Les deux sockets
    ' postent sur le meme m.port -> discrimination par CONTENU, AVANT le filtre de groupe sync.
    if parsed["cmd"] <> invalid then
        if parsed["cmd"] = "vod_play" then HandleVodDatagram(s)
        return
    end if
    ' Filtre de GROUPE : ne suivre que NOTRE groupe LAN (une fois le renderer report -> m.lanReportGroup).
    ' Evite qu'un 2e groupe present sur le meme LAN (ex. "LAN Sync" du NUC) soit elu leader alors qu'on
    ' est dans "BS Sync LAN". Tant que le groupe n'est pas connu (boot) -> accepte tout (retro-compat).
    if m.lanReportGroup <> "" and parsed["group"] <> m.lanReportGroup then return
    idp = 0 : pri = 0
    if parsed.id_player <> invalid then idp = parsed.id_player
    if parsed.priority <> invalid then pri = parsed.priority
    ' Ignore nos PROPRES paquets (loopback broadcast) -> pas d'auto-election ni d'auto-suivi.
    if m.lanMyId > 0 and idp = m.lanMyId then return
    if idp <= 0 then return
    ' FAILOVER : horodate ce peer (liveness). Tout paquet (announce ou play) porte priority+id_player.
    now = m.lanClock.TotalMilliseconds()
    m.lanPeers[Str(idp)] = { id: idp, pri: pri, seen: now }
    ' Liveness d'un leader legitime (il nous SURPASSE a l'election) -> arme l'hysterese de promotion :
    ' tant qu'un tel noeud parle, on ne se promeut pas (evite le flip-flop a un trou transitoire).
    if pri > m.lanSelfPri or (pri = m.lanSelfPri and idp < m.lanMyId) then m.lanLastOutrankMs = now
    LanElect()
    ' On ne SUIT que le sync_play du leader VIVANT elu, et seulement si on n'est pas soi-meme le maitre
    ' effectif (un maitre suit le SERVEUR, pas le LAN). Ecrit le paquet BRUT (raw string) dans sf_lan.json
    ' -> le renderer JSON.parse le message d'origine : aucune re-serialisation FormatJson -> switch_at/
    ' sent_at (epoch ms >2^31) preserves a l'octet pres.
    if parsed["type"] = "sync_play" and idp = m.lanEffLeaderId and not m.lanEffMaster then
        m.lanCount = m.lanCount + 1
        WriteAsciiFile(m.lanFile, s)
    end if
End Sub

' FAILOVER (item 1) — election du leader effectif VIVANT parmi self + peers non-perimes. Aligne sur
' sync_lan.js : TTL peer 20s (au-dela = mort, sort de l'election) ; promotion hysteresis 35s (> TTL) =
' on ne s'auto-promeut que si un noeud nous surpassant est silencieux depuis 35s (maitre vraiment mort).
' Deltas via m.lanClock (roTimespan monotone) : un delta negatif = wrap ~24j -> traite comme "vivant"
' (une passe optimiste inoffensive au wrap). Journalise les CHANGEMENTS dans /lanelect.json (pas de churn).
Sub LanElect()
    m = GetGlobalAA()
    if m.lanMyId <= 0 then return
    now = m.lanClock.TotalMilliseconds()
    bestPri = m.lanSelfPri
    bestId  = m.lanMyId
    for each k in m.lanPeers
        info = m.lanPeers[k]
        age = now - info.seen
        if age < 0 then age = 0
        if age <= 20000 then
            if info.pri > bestPri or (info.pri = bestPri and info.id < bestId) then
                bestPri = info.pri
                bestId  = info.id
            end if
        end if
    end for
    m.lanEffLeaderId = bestId
    beMaster = (bestId = m.lanMyId)
    if beMaster and m.lanLastOutrankMs > 0 then
        g = now - m.lanLastOutrankMs
        if g >= 0 and g < 35000 then beMaster = false
    end if
    m.lanEffMaster = beMaster
    if m.lanEffLeaderId <> m.lanPrevLeaderId or m.lanEffMaster <> m.lanPrevMaster then
        m.lanPrevLeaderId = m.lanEffLeaderId
        m.lanPrevMaster = m.lanEffMaster
        WriteAsciiFile(m.lanElFile, FormatJson({ leader: m.lanEffLeaderId, master: m.lanEffMaster, my_id: m.lanMyId, self_pri: m.lanSelfPri }))
    end if
End Sub

' PHASE 2b — le renderer rapporte son etat LAN (POST /queue {lan_report}). Stocke id/groupe/master ;
' si MAITRE + sync_play fourni, le relaie BRUT (0 re-serialisation -> switch_at/sent_at construits en JS,
' precision pleine). Reset de l'election au changement de groupe (evite un leader d'un ancien groupe).
Sub HandleLanReport(rep As Object)
    m = GetGlobalAA()
    if rep.group <> invalid then
        if rep.group <> m.lanReportGroup then
            ' Changement de groupe -> l'election precedente (peers d'un autre groupe) est obsolete.
            m.lanPeers = {}
            m.lanEffLeaderId = 0
            m.lanEffMaster = false
            m.lanLastOutrankMs = 0
        end if
        m.lanReportGroup = rep.group
    end if
    if rep.id <> invalid then m.lanMyId = rep.id
    ' Priorite d'election = maitre DESIGNE (1) sinon 0. Le failover promeut un slave (pri 0) survivant
    ' via le tie-break d'id -> pas besoin de changer la priorite dynamiquement.
    if rep.master = true then
        m.lanSelfPri = 1
    else
        m.lanSelfPri = 0
    end if
    ' Le renderer envoie TOUJOURS sa position (play) -> on la memorise. On ne la DIFFUSE que si on est
    ' le maitre EFFECTIF : un slave promu (maitre mort) se met alors a diffuser AUTOMATIQUEMENT.
    if type(rep.play) = "String" and rep.play <> "" then m.lanSelfPlay = rep.play
    LanElect()
    if m.lanEffMaster and m.lanSelfPlay <> "" then LanBroadcastRaw(m.lanSelfPlay)
End Sub

' Diffuse le sync_play BRUT du maitre (chaine JSON construite par le renderer) en broadcast :7778.
' 0 re-serialisation -> pas de perte de precision. rc/fail -> /landiag.json (diag emission).
Sub LanBroadcastRaw(s As String)
    if m.lanSock = invalid then return
    ba = CreateObject("roByteArray")
    ba.FromAsciiString(s)
    rc = m.lanSock.SendTo("255.255.255.255", 7778, ba)
    m.lanSendCount = m.lanSendCount + 1
    fr = ""
    if rc <= 0 then fr = m.lanSock.GetFailureReason()
    WriteAsciiFile(m.lanDiagFile, FormatJson({ send: "play", master: m.lanEffMaster, leader: m.lanEffLeaderId, rc: rc, fail: fr, count: m.lanSendCount, bytes: Len(s) }))
End Sub

' Diffuse un sync_announce (presence + priorite pour l'election). Petits entiers uniquement -> FormatJson
' sans risque de precision. priorite 1 si maitre, 0 sinon.
Sub LanBroadcastAnnounce()
    m = GetGlobalAA()
    if m.lanSock = invalid or m.lanMyId <= 0 then return
    ' Priorite d'annonce = priorite DESIGNEE (m.lanSelfPri), pas l'etat effectif : l'election des peers
    ' se fait sur la priorite configuree + le tie-break d'id (un slave promu reste pri 0, gagne par id).
    body = FormatJson({ type: "sync_announce", group: m.lanReportGroup, id_player: m.lanMyId, priority: m.lanSelfPri })
    ba = CreateObject("roByteArray")
    ba.FromAsciiString(body)
    rc = m.lanSock.SendTo("255.255.255.255", 7778, ba)
End Sub

' ── P2.5 : mesure memoire via le DWS local (BRICK-PROOF) ─────────────────────
' GET http://<lanIp>/api/v1/legacy/meminfo (Basic auth m.dwsPw) -> JSON {data:{result:{meminfo:[
'   {field:"MemTotal",memory:"2114908",units:"kB"}, {field:"MemAvailable",memory:"1699108",...}, ...]}}}.
' GetToFile SYNCHRONE (comme RefreshRenderer) = renvoie le code HTTP, NE poste PAS sur m.port -> aucun
' roUrlEvent parasite a router. Local (127.0.0.1/IP LAN) -> ~ms, ne gele pas le serving (≠ screenshot DWS
' multi-s). Ecrit le ratio dans sf_mem.json (servi /mem.json). REBOOT de recuperation UNIQUEMENT si
' m.memWatchdog ET m.memLowCount atteint m.memLowMax (creux soutenu) -> le DWS doit etre actif (cfg dws/debug).
Sub MemCheck()
    m = GetGlobalAA()
    if m.dwsPw = invalid or m.dwsPw = "" then return
    host = "127.0.0.1"
    if type(m.lanIp) = "String" and m.lanIp <> "" then host = m.lanIp
    ut = CreateObject("roUrlTransfer")
    ut.SetUrl("http://" + host + "/api/v1/legacy/meminfo")
    cred = CreateObject("roByteArray")
    cred.FromAsciiString("admin:" + m.dwsPw)
    ut.AddHeader("Authorization", "Basic " + cred.ToBase64String())
    ut.AddHeader("Accept", "application/json")
    code = ut.GetToFile("sf_meminfo_tmp.json")
    if code <> 200 then
        DeleteFile("sf_meminfo_tmp.json")
        return
    end if
    raw = ReadAsciiFile("sf_meminfo_tmp.json")
    DeleteFile("sf_meminfo_tmp.json")
    if type(raw) <> "String" or raw = "" then return
    j = ParseJson(raw)
    if type(j) <> "roAssociativeArray" then return
    if type(j.data) <> "roAssociativeArray" then return
    if type(j.data.result) <> "roAssociativeArray" then return
    rows = j.data.result.meminfo
    if type(rows) <> "roArray" then return
    totKb = -1.0
    availKb = -1.0
    for each row in rows
        if type(row) = "roAssociativeArray" and type(row.field) = "String" and type(row.memory) = "String" then
            if row.field = "MemTotal"     then totKb   = Val(row.memory)
            if row.field = "MemAvailable" then availKb = Val(row.memory)
        end if
    end for
    if totKb <= 0 or availKb < 0 then return
    ratio = availKb / totKb
    m.memRatio = ratio
    ' v49 : charge CPU. ncpu compte UNE fois (legacy/stat, lignes cpu0..cpuN) puis load1 a chaque
    ' passe (legacy/loadavg). Best-effort integral : le moindre echec -> champs cpu simplement absents
    ' (le releve memoire, lui, est deja acquis a ce stade). Appels LDWS locaux = ~ms (cf. en-tete P2.5).
    memAA = { avail_ratio: ratio, total_kb: totKb, avail_kb: availKb, low_count: m.memLowCount, watchdog: m.memWatchdog, measured: true }
    if m.nCpu = 0 then
        statTxt = LdwsLegacyText("stat")
        if statTxt <> invalid then
            n = 0
            lines = statTxt.Tokenize(chr(10))
            for each ln in lines
                ' "cpu0 ..." a "cpuN ..." (exclut la ligne agregee "cpu  ...", 4e char = espace)
                if Len(ln) > 3 and Left(ln, 3) = "cpu" then
                    c4 = Mid(ln, 4, 1)
                    if c4 >= "0" and c4 <= "9" then n = n + 1
                end if
            end for
            if n > 0 then m.nCpu = n
        end if
    end if
    if m.nCpu > 0 then
        loadTxt = LdwsLegacyText("loadavg")
        if loadTxt <> invalid then
            toks = loadTxt.Tokenize(" " + chr(10) + chr(13))
            if toks.Count() > 0 then
                load1 = Val(toks[0])
                if load1 >= 0 then
                    cpuRatio = load1 / m.nCpu
                    if cpuRatio > 1.0 then cpuRatio = 1.0
                    memAA.cpu_load1 = load1
                    memAA.cpu_ratio = cpuRatio
                    memAA.ncpu     = m.nCpu
                end if
            end if
        end if
    end if
    WriteAsciiFile("sf_mem.json", FormatJson(memAA))
    ' Creux soutenu -> reboot de recuperation (uniquement si arme).
    if ratio < m.memLowRatio then
        m.memLowCount = m.memLowCount + 1
    else
        m.memLowCount = 0
    end if
    if m.memWatchdog and m.memLowCount >= m.memLowMax then
        print "MemWatchdog: memoire basse soutenue ("; ratio; ") -> reboot de recuperation"
        RebootSystem()
    end if
End Sub

' v49 : GET LDWS /api/v1/legacy/<name> -> champ texte (data.result.text) ou invalid. MEME pattern
' brick-proof que le fetch meminfo de MemCheck (roUrlTransfer + Basic auth + GetToFile synchrone,
' local = ~ms). Les endpoints legacy "texte" (loadavg, stat) rendent {data:{result:{text:"..."}}}.
Function LdwsLegacyText(name as String) as dynamic
    m = GetGlobalAA()
    if m.dwsPw = invalid or m.dwsPw = "" then return invalid
    host = "127.0.0.1"
    if type(m.lanIp) = "String" and m.lanIp <> "" then host = m.lanIp
    ut = CreateObject("roUrlTransfer")
    ut.SetUrl("http://" + host + "/api/v1/legacy/" + name)
    cred = CreateObject("roByteArray")
    cred.FromAsciiString("admin:" + m.dwsPw)
    ut.AddHeader("Authorization", "Basic " + cred.ToBase64String())
    ut.AddHeader("Accept", "application/json")
    tmp = "sf_ldws_tmp.json"
    code = ut.GetToFile(tmp)
    if code <> 200 then
        DeleteFile(tmp)
        return invalid
    end if
    raw = ReadAsciiFile(tmp)
    DeleteFile(tmp)
    if type(raw) <> "String" or raw = "" then return invalid
    j = ParseJson(raw)
    if type(j) <> "roAssociativeArray" then return invalid
    if type(j.data) <> "roAssociativeArray" then return invalid
    if type(j.data.result) <> "roAssociativeArray" then return invalid
    if type(j.data.result.text) <> "String" then return invalid
    return j.data.result.text
End Function

' ── Tier 1.5 : refresh renderer (auto-update) ───────────────────────────────
' Télécharge chaque sf_rnd_* en TEMP (GetToFile SYNCHRONE = renvoie le code HTTP, ne poste PAS sur
' le port -> pas de roUrlEvent à router) et ne remplace que si le contenu DIFFÈRE de la copie SD.
' Online -> les renderers changés sont mis à jour (effectifs au prochain boot). Offline (GetToFile
' != 200) -> on garde la copie locale (offline-cold-boot préservé). EnsureRenderer (pré-Show) couvre
' le 1er provisioning (fichiers manquants) ; RefreshRenderer couvre les fichiers CHANGÉS.
' ── v52 / Step B (#4) : version EPINGLEE du renderer ────────────────────────
' Le renderer BS est SERVI par le serveur : sans epinglage, RefreshRenderer tirerait "la derniere
' version servie" -> une MAJ serveur mettrait le parc a jour au prochain reboot, INVOLONTAIREMENT.
' On resout le PIN (GET /updates/renderer-version, token machine) : la version du dernier deploiement
' ciblant CE player (defaut = sa version courante -> pas d'auto-MAJ). RefreshRenderer tire alors les 4
' JS depuis /player-chromeos/v/<ver>/ au lieu du plat. BACKWARD-SAFE : echec (offline / vieux serveur
' sans l'endpoint / JSON invalide) -> "" -> chemin plat historique. AUCUNE primitive BrightScript
' nouvelle (roUrlTransfer + AddHeader + GetToFile + ReadAsciiFile + ParseJson + type() sont tous deja
' utilises dans ce fichier) -> zero risque brick ajoute ; la version renderer (sf_rnd_*) est INCHANGEE
' si le pull versionne echoue (on garde l'ancien fichier).
Function ResolveRendererPin() As String
    m = GetGlobalAA()
    if m.backend = "" or m.token = "" then return ""
    ut = CreateObject("roUrlTransfer")
    ut.SetUrl(m.backend + "/api/v1/updates/renderer-version")
    ut.AddHeader("Authorization", "Bearer " + m.token)
    ut.EnableEncodings(true)
    tmp = "sfpin.json"
    code = ut.GetToFile(tmp)
    ver = ""
    if code = 200 then
        body = ReadAsciiFile(tmp)
        if body <> "" then
            j = ParseJson(body)
            if type(j) = "roAssociativeArray" then
                if type(j.version) = "String" then ver = j.version
            end if
        end if
    end if
    DeleteFile(tmp)
    return ver
End Function

' v53 : sonde HTTP (GetToFile synchrone, primitives deja utilisees partout) — 200 = URL servie.
Function ProbeUrl(url As String) As Boolean
    ut = CreateObject("roUrlTransfer")
    ut.SetUrl(url)
    ut.EnableEncodings(true)
    tmp = "sfprobe.tmp"
    code = ut.GetToFile(tmp)
    DeleteFile(tmp)
    return code = 200
End Function

Sub RefreshRenderer()
    m = GetGlobalAA()
    if m.backend = "" then return
    ' Pin (#4) : fichiers versionnes si resolu ; pin non resolu -> tout en plat (backward-safe).
    ver = ResolveRendererPin()
    ' v53 (decision 3 "canal unique") : la version epinglee est cherchee au CATALOGUE d'abord
    ' (zip importe -> fichiers extraits, servis par /api/v1/updates/renderers/) puis au chemin
    ' statique historique. UNE sonde par base (signflow.js), pas une par fichier. Les deux
    ' absentes -> on GARDE les copies SD : le plat = "derniere version servie" = exactement la
    ' MAJ involontaire que le pin empeche (comportement v52 conserve, en moins de requetes).
    vbase = ""
    vcss = false
    if ver <> "" then
        apibase = m.backend + "/api/v1/updates/renderers/brightsign/" + ver + "/"
        statbase = m.backend + "/player-chromeos/v/" + ver + "/"
        if ProbeUrl(apibase + "signflow.js") then
            vbase = apibase
            ' Un zip importe est extrait ENTIER (style.css compris) -> tout depuis le catalogue.
            vcss = true
        else if ProbeUrl(statbase + "signflow.js") then
            vbase = statbase
        end if
    end if
    names = ["ed25519.js", "signflow.js", "audio_player.js", "player.js", "style.css"]
    for each f in names
        dest = "sf_rnd_" + f
        tmp  = "sfrtmp_" + f
        src = ""
        if ver = "" then
            src = m.backend + "/player-chromeos/" + f
        else if f = "style.css" and vcss = false then
            ' style.css reste plat sur le chemin statique (les snapshots v/ ne le portent pas).
            src = m.backend + "/player-chromeos/" + f
        else if vbase <> "" then
            src = vbase + f
        end if
        ' src vide = version epinglee servie nulle part -> copie SD gardee telle quelle.
        if src <> "" then
            ut = CreateObject("roUrlTransfer")
            ut.SetUrl(src)
            ut.EnableEncodings(true)
            code = ut.GetToFile(tmp)
            if code = 200 then
                newc = ReadAsciiFile(tmp)
                oldc = ReadAsciiFile(dest)
                if newc <> "" and newc <> oldc then
                    DeleteFile(dest)
                    MoveFile(tmp, dest)
                    print "renderer MAJ: "; f
                else
                    DeleteFile(tmp)
                end if
            else
                DeleteFile(tmp)
            end if
        end if
    end for
End Sub

' ── P1.2 : auto-update de l'autorun depuis prod ─────────────────────────────
' Calque RefreshRenderer (GetToFile SYNCHRONE) MAIS avec des garde-fous propres au risque de BRICK :
' un autorun casse/tronque = compile KO = sortie SD physique sur CHAQUE device (pas d'auto-recovery,
' contrairement au renderer ou le device tourne encore). On ne remplace l'autorun QUE si :
'   (a) HTTP 200, (b) taille plancher >20 Ko (anti page d'erreur 404 / DL tronque),
'   (c) prefixe attendu "' ====..." (c'est bien NOTRE autorun, pas une reponse HTML),
'   (d) contenu DIFFERENT de la copie SD.
' Backup de l'ancien en autorun.brs.bak (recup manuelle sans reconstruire la SD). Effectif au PROCHAIN
' BOOT (pas de reboot force -> non-disruptif ; loop-safe : apres MAJ la copie SD == prod -> plus de
' re-MAJ). UNIQUEMENT des primitives PROUVEES sur ce firmware (Len/Left/Read/Write/Move/Delete) -> PAS
' de crypto/roEVPDigest non teste DANS le chemin brick. sha256 = durcissement futur (API a verifier HW).
' Gate config (auto-update de l'autorun au boot). OTA §XXX approuvee force via RefreshAutorunForced.
Sub RefreshAutorun()
    m = GetGlobalAA()
    if m.autoUpdate <> true then return
    RefreshAutorunForced()
End Sub

' Variante INCONDITIONNELLE (ignore le gate m.autoUpdate) : utilisee par un apply OTA APPROUVE (§XXX
' slice 2, cmd ota_apply) -> le deploiement est deja l'approbation admin, on tire l'autorun de prod
' meme si l'auto-update au boot est desactive. Memes garde-fous anti-brick (200 + taille + prefixe + diff).
Sub RefreshAutorunForced()
    m = GetGlobalAA()
    if m.backend = "" then return
    tmp = "sfautotmp.brs"
    ut = CreateObject("roUrlTransfer")
    ut.SetUrl(m.backend + "/player-chromeos/brightsign/autorun.brs")
    ut.EnableEncodings(true)
    code = ut.GetToFile(tmp)
    if code <> 200 then
        DeleteFile(tmp)
        return
    end if
    newc = ReadAsciiFile(tmp)
    oldc = ReadAsciiFile("autorun.brs")
    ' Garde-fous d'integrite (anti-brick) avant tout remplacement.
    okSize   = (newc <> "") and (Len(newc) > 20000)
    okPrefix = (Left(newc, 12) = "' ==========")
    if okSize and okPrefix and newc <> oldc then
        WriteAsciiFile("autorun.brs.bak", oldc)   ' backup (recup manuelle) ; CopyFile non prouve ici
        DeleteFile("autorun.brs")
        MoveFile(tmp, "autorun.brs")
        print "autorun MAJ depuis prod (effectif au prochain boot)"
    else
        DeleteFile(tmp)
    end if
End Sub

' ── P2.2 : config reseau depuis config.json via roNetworkConfiguration (OPT-IN) ──────────────
' Defaut / absence du bloc / mode="dhcp" -> on ne touche a RIEN (l'OS garde le DHCP = comportement
' actuel). La voie d'apply n'est JAMAIS prise sans bloc network -> deployer ce nouvel autorun sur le
' parc existant (config.json sans reseau) = no-op SUR (BrightScript resout les methodes au RUNTIME,
' code jamais execute = jamais de crash). ⚠️ A VALIDER sur une SD de TEST, jamais sur un player en
' prod : un reglage statique errone = device injoignable. Ethernet (interface 0) = cas principal ;
' WiFi (interface 1) = best-effort selon modele/module (XT1144 = Ethernet, non testable ici).
Sub ApplyNetworkConfig(net As Object)
    if type(net) <> "roAssociativeArray" then return
    mode = "dhcp"
    if type(net.mode) = "String" and net.mode <> "" then mode = net.mode

    ' WiFi optionnel (SSID fourni) — best-effort.
    if type(net.wifi) = "roAssociativeArray" and type(net.wifi.ssid) = "String" and net.wifi.ssid <> "" then
        ncw = CreateObject("roNetworkConfiguration", 1)
        if ncw <> invalid then
            pass = ""
            if type(net.wifi.passphrase) = "String" then pass = net.wifi.passphrase
            ncw.SetWiFiESSID(net.wifi.ssid)
            ncw.SetWiFiPassphrase(pass)
            ncw.Apply()
        end if
    end if

    ' IP statique (sinon DHCP par defaut = aucune reconfiguration).
    if mode = "static" then
        nc = CreateObject("roNetworkConfiguration", 0)
        if nc <> invalid then
            if type(net.ip) = "String"      and net.ip <> ""      then nc.SetIP4Address(net.ip)
            if type(net.netmask) = "String" and net.netmask <> "" then nc.SetIP4Netmask(net.netmask)
            if type(net.gateway) = "String" and net.gateway <> "" then nc.SetIP4Gateway(net.gateway)
            if type(net.dns) = "roArray" and net.dns.Count() > 0   then nc.SetDNSServers(net.dns)
            nc.Apply()
        end if
    end if
End Sub

' ── POST /queue : corps discrimine ──────────────────────────────────────────
'   {items:[{cache_key,url}]} -> enfile les DL manquants
'   {keep:[cache_key,...]}    -> Tier 1.1 : evince les sfc_* absents de la liste
'   {purge:[cache_key,...]}   -> Tier 1.3 : supprime les sfc_* cibles (kill-switch)
'   {prefetch:[{cache_key,url}], windows:[{start,end}]} -> P2.7/P2.8 : file look-ahead + fenetres horaires
' (un seul endpoint POST sur ce firmware tatillon -> pas de GetRequestUri, on lit le corps. Les
'  reponses manuelles SetResponseBodyString/SendResponse cassent le boot -> aucune reponse emise.)
Sub HandleHttpEvent(ev As Object)
    m = GetGlobalAA()
    body = ev.GetRequestBodyString()
    if body <> invalid and body <> "" then
        payload = ParseJson(body)
        if type(payload) = "roAssociativeArray" then
            ' Canal de commande (POSTe sur /queue, discrimine par le CORPS car GetRequestUri non fiable
            ' ici ET un 2e endpoint POST n'est pas routé sur ce firmware). Le renderer (signflow.js
            ' IS_BS) POSTe {cmd:"reboot"} -> reboot OS NATIF (remplace require('@brightsign/*') Node mort).
            ' NB : on teste <> invalid (PAS type()="roString") car ParseJson peut renvoyer une chaîne
            ' intrinsèque "String" et non boxée "roString" -> le test de type ratait HandleCmd (le POST
            ' renvoyait quand même 500 = traité, mais le reboot n'était jamais déclenché).
            if payload.cmd <> invalid then HandleCmd(payload.cmd, payload)
            if type(payload.keep) = "roArray" then HandleKeep(payload.keep)
            if type(payload.purge) = "roArray" then HandlePurge(payload.purge)
            if type(payload.prefetch) = "roArray" then HandlePrefetch(payload.prefetch, payload.windows)
            if type(payload.lan_report) = "roAssociativeArray" then HandleLanReport(payload.lan_report)
            if type(payload.items) = "roArray" then
                for each it in payload.items
                    if type(it) = "roAssociativeArray" and it.cache_key <> invalid and it.url <> invalid then
                        k = it.cache_key
                        if not m.cached.DoesExist(k) and not Queued(k) then
                            m.dlQueue.AddTail({ cache_key: k, url: it.url })
                        end if
                    end if
                end for
            end if
        end if
    end if
    ' (Pas de reponse explicite : le renderer fait fire-and-forget. roHttpServer renvoie un 500
    '  cosmetique sans reponse, sans impact ; SetResponseBodyString/SendResponse cassaient le boot
    '  sur ce firmware -> retires. cf. NATIVE_CACHE_PLAN.md.)
    PumpQueue()
End Sub

' ── Tier 1.1 : eviction -> supprime les sfc_* dont la cle n'est PAS dans le keep-set ─────────
' VERSION DURCIE : evite les 2 idiomes non exerces par le code original (suspects du crash autorun
' LED rouge 10x) -> AddReplace au lieu de keepSet[k]=true ; RECONSTRUCTION de m.cached au lieu de
' m.cached.Delete(k). N'utilise que des methodes prouvees (AddReplace/DoesExist/DeleteFile/Flat).
' GARDE-FOU : keep-set VIDE => aucune purge.
' IMPORTANT BrightSign : roAssociativeArray n'a PAS de methode Count() (Member function not found ->
' crash autorun = LED rouge 10x). On utilise IsEmpty() pour le test de vacuite.
'
' ⚠️ FIX 2026-06-28 (byte-durabilite) : on balaye les FICHIERS REELS de la SD (MatchFiles) et on
' DeleteFile par NOM REEL (fn), au lieu d'iterer m.cached + DeleteFile(nom CALCULE depuis la cle).
' L'ancienne version manquait les ORPHELINS sur disque : variante non-referencee (ex original ".mp4"
' alors que la playlist sert la rendition ".fhd.mp4"), ou media cache puis quarantaine/derefere sans
' reboot (jamais re-ajoute a m.cached par ScanCache). Symptome constate : les octets d'un media
' "purge"/"evince" survivaient sur la SD (cached.json a jour mais fichier present) -> trou takedown.
' m.cached est reconstruit depuis ce qui RESTE reellement.
Sub HandleKeep(keep As Object)
    m = GetGlobalAA()
    keepSet = CreateObject("roAssociativeArray")
    for each k in keep
        keepSet.AddReplace(k, true)
    end for
    if keepSet.IsEmpty() then return        ' garde-fou : keep VIDE => aucune eviction
    kept = CreateObject("roAssociativeArray")
    evicted = false
    files = MatchFiles("SD:/", m.prefix + "*")
    if type(files) = "roList" then
        for each fn in files
            k = Unflat(Mid(fn, Len(m.prefix) + 1))
            if keepSet.DoesExist(k) then
                kept.AddReplace(k, true)
            else
                DeleteFile(fn)               ' nom REEL sur SD -> supprime aussi les orphelins
                evicted = true
            end if
        end for
    end if
    m.cached = kept
    if evicted then RefreshCachedJson()
End Sub

' ── Tier 1.3 : kill-switch -> supprime les sfc_* explicitement cibles ─────────
' Comme HandleKeep : balaye les FICHIERS REELS (MatchFiles) + DeleteFile par nom REEL (fix
' byte-durabilite 2026-06-28). Supprime aussi les orphelins de variante d'une cle ciblee
' (ex original ".mp4" et rendition ".fhd.mp4" du meme media). NB : pour un takedown fiable,
' QUARANTAINE le media (il quitte le sync -> HandleKeep balaie ses octets) ; le purge explicite
' suppose que le renderer a encore la correspondance id_media->cle (perdue si deja hors-sync).
Sub HandlePurge(purge As Object)
    m = GetGlobalAA()
    target = CreateObject("roAssociativeArray")
    for each k in purge
        target.AddReplace(k, true)
    end for
    if target.IsEmpty() then return
    kept = CreateObject("roAssociativeArray")
    changed = false
    files = MatchFiles("SD:/", m.prefix + "*")
    if type(files) = "roList" then
        for each fn in files
            k = Unflat(Mid(fn, Len(m.prefix) + 1))
            if target.DoesExist(k) then
                DeleteFile(fn)               ' nom REEL sur SD
                changed = true
            else
                kept.AddReplace(k, true)
            end if
        end for
    end if
    if changed then
        m.cached = kept
        RefreshCachedJson()
    end if
End Sub

' ── Canal de commande (POST /cmd via HandleHttpEvent) ───────────────────────
' Le renderer pilote les commandes "device" qui exigent du natif BrightScript, via un simple POST
' loopback (meme mecanique prouvee que /queue) -> pas besoin des objets Node @brightsign/* (desactives).
'   {cmd:"reboot"} -> RebootSystem() (vrai reboot OS, fiable). [extensible : screenshot, stop...]
Sub HandleCmd(cmd As String, payload As Object)
    print "CMD recue: "; cmd
    if cmd = "reboot" then
        RebootSystem()
    else if cmd = "screenshot" then
        StartScreenshot(payload)
    else if cmd = "firmware_update" then
        DoFirmwareUpdate(payload)
    else if cmd = "ota_apply" then
        DoOtaApply(payload)
    else if cmd = "set_video_mode" then
        SetVideoModeOverride(payload)
    else if cmd = "set_rotation" then
        SetRotationOverride(payload)
    else if cmd = "persist_token" then
        PersistToken(payload)
    else if cmd = "display_command" then
        HandleDisplayCommand(payload)
    else if cmd = "video_output" then
        SetVideoOutputPower(payload)
    else if cmd = "cec_power" then
        DoCecPower(payload)
    else if cmd = "set_timezone" then
        SetSystemTimeZone(payload)
    else if cmd = "set_time_server" then
        SetSystemTimeServer(payload)
    else if cmd = "audio_output" then
        SetAudioOutput(payload)
    else if cmd = "audio_volume" then
        SetAudioVolume(payload)
    else if cmd = "audio_mute" then
        SetAudioMute(payload)
    else if cmd = "hdmi_input" then
        HandleHdmiInput(payload)
    else if cmd = "hdmi_local_cfg" then
        SetHdmiLocalCfg(payload)
    end if
End Sub

' ── cmd "display_command" (contrôle écran RS232 / Tier AV) ────────────────────────────────────────
' Le renderer (signflow.js) construit TOUTE la trame protocole (Samsung MDC / LG / generic) en JS et
' POSTe {cmd:"display_command", hex:"AA110100...", baud:9600, port:0}. Ici on ne fait QUE convertir le
' hex en octets et l'écrire sur le port série -> bridge dumb, hors chemin de boot (aucun risque de brick ;
' au pire la commande échoue à l'invocation, recuperable par reboot). On évite roByteArray.FromHexString
' (non confirmé sur ce BOS) -> parse manuel Mid/Val(radix 16), primitives déjà prouvées. On ne compte pas
' via roByteArray.Count() (flag projet : Count peut manquer) -> compteur local.
'   ⚠️ À VALIDER HW (écran RS232 + câble) : ajuster le constructeur roSerialPort comme on a ajusté
'   roStorageInfo("SD:") / roNetworkConfiguration (API parfois firmware-specifique). serial_port = INDEX
'   numérique BrightSign (0 = port intégré), pas un chemin POSIX.
' Fil d'Ariane écrit dans sf_rs232.json à CHAQUE étape (interrogeable via /rs232.json) → on voit
' EXACTEMENT où ça passe/bloque : assemblage octets → ouverture port → envoi. La sonde SURVIT au reboot
' (cf. boot) → un crash laisse la derniere etape lisible.
' 🔑 Le renderer envoie un TABLEAU D'ENTIERS (payload.bytes), PAS du hex : on évite tout parsing hex
' BrightScript (Val(s,16)/Mid non fiables sur ce firmware -> crashaient à l'étape "parsing"). On pousse
' directement les entiers déjà décodés par ParseJson.
Sub HandleDisplayCommand(payload As Object)
    arr = payload.bytes
    if arr = invalid then return
    baud = 9600
    if payload.baud <> invalid then baud = Int(payload.baud)
    port = 0
    if payload.port <> invalid then port = Int(payload.port)
    hexs = ""
    if payload.hex <> invalid then hexs = payload.hex
    if m.rs232Seq = invalid then m.rs232Seq = 0
    m.rs232Seq = m.rs232Seq + 1
    diag = { seq: m.rs232Seq, hex: hexs, port: port, baud: baud, bytes: 0, step: "building" }
    WriteAsciiFile("sf_rs232.json", FormatJson(diag))
    ba = CreateObject("roByteArray")
    cnt = 0
    for each b in arr
        ba.Push(Int(b))
        cnt = cnt + 1
    end for
    diag.bytes = cnt
    if cnt = 0 then
        diag.step = "empty"
        WriteAsciiFile("sf_rs232.json", FormatJson(diag))
        return
    end if
    diag.step = "opening_port"
    WriteAsciiFile("sf_rs232.json", FormatJson(diag))
    sp = CreateObject("roSerialPort", port, baud)
    if sp = invalid then
        diag.step = "port_invalid"
        WriteAsciiFile("sf_rs232.json", FormatJson(diag))
        print "RS232: port "; port; " indisponible sur ce modele"
        return
    end if
    diag.step = "sending"
    WriteAsciiFile("sf_rs232.json", FormatJson(diag))
    sp.SendBlock(ba)                              ' roSerialPort n'a PAS SendByteArray -> SendBlock (accepte roByteArray ; binaire 0x00-safe)
    diag.step = "sent"
    WriteAsciiFile("sf_rs232.json", FormatJson(diag))
    print "RS232: envoye "; cnt; " octets @ "; baud; " baud sur port "; port
End Sub

' ── cmd "video_output" (repli « ecran non pilotable » — coupure de la sortie HDMI) ─────────────────
' Le renderer POSTe {cmd:"video_output", on:true|false} quand le CMS demande power_on/power_off
' sans controleur d'ecran configure. SetPowerSaveMode(true) coupe le sync HDMI (plus de signal ->
' l'ecran passe en veille selon son reglage) ; false le retablit. Runtime, sans reboot, hors chemin
' de boot (aucun risque de brick ; au pire l'appel echoue a l'invocation).
Sub SetVideoOutputPower(payload As Object)
    m = GetGlobalAA()
    on = true
    if payload.on <> invalid then on = payload.on
    vm = CreateObject("roVideoMode")
    if vm = invalid then
        print "video_output: roVideoMode indisponible"
        return
    end if
    ' v50 : etat suivi en RAM (defaut false = sortie allumee au boot) -> le pulse SetMode ne part
    ' que sur une VRAIE transition off->on. Le serveur re-pousse power_on a CHAQUE reconnexion WS
    ' (re-assertion auto-power) — et le retour d'entree HDMI reconnecte (requestResync) : le pulse
    ' systematique reinitialisait le plan video HW et tuait le contenu fraichement repris (ECRAN
    ' NOIR constate HW 16/07, XT1144, post-reboot). SetPowerSaveMode reste applique (idempotent).
    if m.videoOutOff = invalid then m.videoOutOff = false
    wasOff = m.videoOutOff
    vm.SetPowerSaveMode(NOT on)
    m.videoOutOff = NOT on
    if on then
        if wasOff then
            ' v41 : PULSE HDMI au rallumage — certaines dalles ne re-scannent PAS une entree
            ' dont le sync revient (constate HW 2026-07-07, ecran multi-entrees : XT1144+HD1024
            ' restaient invisibles jusqu'au debranche/rebranche du cable). Re-appliquer le mode
            ' video courant regenere un hotplug/re-handshake EDID que ces dalles detectent.
            ' Runtime only (SetMode session), hors chemin de boot — pas de risque de brick.
            cur = vm.GetMode()
            if cur <> invalid and cur <> "" then
                sleep(300)
                ok = vm.SetMode(cur)
                print "video_output: pulse HDMI (SetMode "; cur; ") -> "; ok
            end if
            print "video_output: sortie video RETABLIE (power save off)"
        else
            print "video_output: deja allumee — pulse ignore (v50)"
        end if
    else
        print "video_output: sortie video COUPEE (power save on)"
    end if
End Sub

' ── cmd "cec_power" (pilotage TV via HDMI-CEC — display_config.type="cec") ─────────────────────────
' Le renderer POSTe {cmd:"cec_power", on:true|false}. On envoie une trame CEC brute a la TV
' (adresse logique 0) : Image View On (&h04, reveil) / Standby (&h36, veille). Premier octet =
' initiateur (4 = Playback Device) <<4 OR destinataire (0 = TV) = &h40. Fire-and-forget (pas de
' lecture de reponse) ; si roCecInterface est indisponible sur le modele, on loggue sans crasher.
Sub DoCecPower(payload As Object)
    on = true
    if payload.on <> invalid then on = payload.on
    cec = CreateObject("roCecInterface")
    if cec = invalid then
        print "cec_power: roCecInterface indisponible sur ce modele"
        return
    end if
    if on then
        cec.SendRawMessage(Chr(&h40) + Chr(&h04))
    else
        cec.SendRawMessage(Chr(&h40) + Chr(&h36))
    end if
    print "cec_power: on = "; on
End Sub

' ── cmd "set_timezone" (fuseau horaire OS — drawer CMS) ────────────────────────────────────────────
' Le renderer POSTe {cmd:"set_timezone", tz:"Europe/Paris"}. roSystemTime.SetTimeZone accepte les
' identifiants IANA sur les BOS recents et RETOURNE false si la zone est inconnue (aucun effet).
' Succes -> reboot : seul moyen fiable de propager le nouveau fuseau au roHtmlWidget (le renderer
' fige sa TZ au demarrage). Hors chemin de boot (aucun risque de brick).
Sub SetSystemTimeZone(payload As Object)
    tz = ""
    if payload.tz <> invalid then tz = payload.tz
    if tz = "" then
        print "set_timezone: tz manquante"
        return
    end if
    st = CreateObject("roSystemTime")
    if st = invalid then
        print "set_timezone: roSystemTime indisponible"
        return
    end if
    ok = st.SetTimeZone(tz)
    if ok then
        print "set_timezone: "; tz; " appliquee -> reboot (propagation au renderer)"
        RebootSystem()
    else
        print "set_timezone: zone inconnue/refusee: "; tz
    end if
End Sub

' ── cmd "set_time_server" (NTP du site, migr 0071 — autorun v42) ───────────────────────────────────
' Le renderer POSTe {cmd:"set_time_server", server:"192.168.1.10"|"pool.ntp.org"} quand le sync
' merge le NTP du site. roNetworkConfiguration.SetTimeServer + Apply : runtime, persiste au reboot
' (registry BOS), pas de reboot requis. Refus (format inconnu) -> log sans crash. Hors chemin de boot.
Sub SetSystemTimeServer(payload As Object)
    srv = ""
    if payload.server <> invalid then srv = payload.server
    if srv = "" then
        print "set_time_server: serveur manquant"
        return
    end if
    ' v43 : ECRITURE REGISTRE = la voie fiable (validee HW via DWS PUT networking/ts) —
    ' le BOS lit "networking/ts" au boot. SetTimeServer runtime = capricieux (v42 sans
    ' effet constate HW) -> tente en best-effort APRES, pour un effet immediat eventuel.
    reg = CreateObject("roRegistrySection", "networking")
    if reg = invalid then
        print "set_time_server: roRegistrySection indisponible"
        return
    end if
    reg.Write("ts", srv)
    reg.Flush()
    print "set_time_server: registre networking/ts = "; srv; " (effectif au prochain boot)"
    nc = CreateObject("roNetworkConfiguration", 0)
    if nc = invalid then nc = CreateObject("roNetworkConfiguration", 1)
    if nc <> invalid then
        ok = nc.SetTimeServer(srv)
        if ok then nc.Apply()
        print "set_time_server: tentative runtime SetTimeServer -> "; ok
    end if
End Sub

' ── cmd "persist_token" (Correctif A) : grave le JWT rote sur la SD ────────────────────────────────
' Le renderer POSTe son JWT courant ({cmd:"persist_token", token:"eyJ..."}) -> on l'ecrit dans
' sf_token.txt, lu au boot EN PRIORITE sur config.json -> le token SURVIT au wipe du profil Chromium
' (localStorage efface par un saut de firmware) = plus de boucle register 409 / ecran mort. N'ecrit
' QUE les JWT (pas un provisioning sfp_) et QUE si different (anti-usure flash). PAS de reboot
' (non-disruptif ; effectif au prochain boot). 0 nouvelle API (Read/Write/Left deja prouves).
Sub PersistToken(payload As Object)
    if payload.token = invalid or payload.token = "" then return
    t = payload.token
    if Left(t, 4) = "sfp_" then return          ' on ne grave que les JWT, jamais un provisioning token
    cur = ReadAsciiFile("sf_token.txt")
    if cur <> t then WriteAsciiFile("sf_token.txt", t)
End Sub

' ── cmd "set_video_mode" (toggle 4K par-player) : ecrit le sidecar + reboot ───────────────────────
' Le renderer POSTe {cmd:"set_video_mode", mode:"best"|"fhd"|"WxHxR"|"clear"} quand display_config.
' bs_video_mode change (et seulement si != sortie courante -> pas de boucle de reboot). On persiste le
' mode dans sf_videomode.txt (override de config.json, lu au boot AVANT la creation du widget) puis on
' reboote pour appliquer. "clear"/"" retire l'override -> retour au defaut config.json. 0 nouvelle API
' (WriteAsciiFile/DeleteFile/RebootSystem deja prouves).
Sub SetVideoModeOverride(payload As Object)
    if payload.mode = invalid then return
    m = payload.mode
    if m = "" or m = "clear" then
        DeleteFile("sf_videomode.txt")
    else
        WriteAsciiFile("sf_videomode.txt", m)
    end if
    print "set_video_mode: '"; m; "' -> reboot (applique au prochain boot)"
    RebootSystem()
End Sub

' ── cmd "set_rotation" : ecrit le sidecar de rotation + reboot ────────────────────────────────────
' Le renderer POSTe {cmd:"set_rotation", transform:"identity"|"rot90"|"rot180"|"rot270"} quand
' display_config.screen_rotation change (et seulement si != rotation courante -> pas de boucle de reboot).
' On persiste dans sf_rotation.txt (lu au boot AVANT la creation du widget par RotationTransform) puis on
' reboote (transform fixe a la creation du widget). "identity"/""/"clear" -> retire l'override (pas de rotation).
' VALIDE contre la whitelist -> un transform inconnu ne fait rien (pas de sidecar corrompu). 0 nouvelle API.
Sub SetRotationOverride(payload As Object)
    if payload.transform = invalid then return
    t = payload.transform
    if t = "" or t = "clear" or t = "identity" then
        DeleteFile("sf_rotation.txt")
    else if t = "rot90" or t = "rot180" or t = "rot270" then
        WriteAsciiFile("sf_rotation.txt", t)
    else
        return   ' valeur inconnue -> on ne touche a rien (pas de reboot, pas de sidecar corrompu)
    end if
    print "set_rotation: '"; t; "' -> reboot (applique au prochain boot)"
    RebootSystem()
End Sub

' Rotation ecran courante = sidecar sf_rotation.txt valide (whitelist) ou "identity" (defaut, pas de
' rotation). Lu au boot pour config.transform du widget ET expose dans /device.json (anti-boucle renderer).
Function RotationTransform() As String
    v = ReadAsciiFile("sf_rotation.txt")
    if v = "rot90" or v = "rot180" or v = "rot270" then return v
    return "identity"
End Function

' ── Sortie audio (point AV 2/3) : sortie / volume / mute ──────────────────────────────────────────
' Le son du <video> est sur le plan audio MATERIEL (hors canvas) -> routage NATIF, pas setSinkId/WebAudio.
'   - SORTIE : pcm_audio_outputs du roHtmlWidget AU BOOT (cf. ApplyConfig). Changer de sortie = sidecar
'     sf_audioout.txt + reboot (le re-route runtime setPcmAudioOutputs est DEPRECATE -> on l'evite).
'   - VOLUME / MUTE : roAudioOutput.SetVolume/SetMute, applicables A CHAUD (pas de reboot) + persistes en
'     sidecars (sf_audiovol.txt / sf_audiomute.txt), ré-appliques au boot via ApplyAudioVolMute().
' API toute DOCUMENTEE (roAudioOutput("hdmi"|"analog"|"spdif"|"usb"), SetVolume 0-100, SetMute bool).
' Guarde `<> invalid` : une sortie absente sur le modele ne crashe pas (au pire la cmd est sans effet).
' ⚠️ A VALIDER HW (ecran/ampli sur la sortie ciblee).
Function IsValidAudioOut(a As Dynamic) As Boolean
    if a = invalid then return false
    return a = "hdmi" or a = "analog" or a = "spdif" or a = "usb" or a = "none"
End Function

Function AudioOutName() As String
    a = ReadAsciiFile("sf_audioout.txt")
    if IsValidAudioOut(a) then return a
    return "hdmi"                                 ' defaut systeme (aucun sidecar valide)
End Function

' 🔑 roAudioOutput est INCOMPATIBLE avec l'audio HTML5 du roHtmlWidget : le router via pcm_audio_outputs
' suffit, mais créer/toucher un roAudioOutput (même SetMute(false)/SetVolume) CASSE le rendu audio du
' widget de façon NON RÉCUPÉRABLE (prouvé HW sur HD1024 : le son ne revient pas, même au reboot). Le
' volume/mute du CONTENU se fait donc côté RENDERER (video.volume/.muted via player.js). Ici on ne fait
' QUE nettoyer les anciens sidecars vol/mute — plus AUCUN appel roAudioOutput.
Sub ApplyAudioVolMute()
    DeleteFile("sf_audiovol.txt")
    DeleteFile("sf_audiomute.txt")
End Sub

' cmd audio_output : change la sortie -> sidecar + reboot. ""/"clear" -> retire l'override (defaut HDMI).
' 🔑 ANTI-BOUCLE : ne reboote QUE si la sortie demandee DIFFERE de la sortie courante (sidecar). Sans ce
' garde, le renderer (dont l'anti-spam est en memoire, remis a zero a chaque boot) ré-emettait la commande
' a chaque sync -> reboot a chaque boot = BOUCLE INFINIE. cur = AudioOutName() = sidecar ou "hdmi" defaut.
Sub SetAudioOutput(payload As Object)
    if payload.output = invalid then return
    o = payload.output
    if o = "" or o = "clear" then o = "hdmi"     ' clear/"" = sortie par defaut HDMI
    if not IsValidAudioOut(o) then return        ' sortie inconnue -> on ignore (pas de reboot)
    cur = AudioOutName()                          ' sortie courante effective (sidecar ou "hdmi")
    if o = cur then return                        ' deja a cette sortie -> PAS de reboot (anti-boucle)
    WriteAsciiFile("sf_audioout.txt", o)          ' ECRIT TOUJOURS (hdmi inclus) -> pcm_audio_outputs=[o] explicite au boot
    print "audio_output: '"; o; "' (cur '"; cur; "') -> reboot (applique au boot via pcm_audio_outputs)"
    RebootSystem()
End Sub

' cmd audio_volume / audio_mute : NO-OP côté autorun (cf. ApplyAudioVolMute). Le volume/mute du contenu
' est appliqué côté RENDERER sur le <video> HTML5 (le renderer n'envoie plus ces commandes à l'autorun).
' Subs conservées pour ignorer proprement d'éventuelles commandes en vol d'un ancien renderer.
Sub SetAudioVolume(payload As Object)
End Sub

Sub SetAudioMute(payload As Object)
End Sub

' ── cmd "hdmi_input" (point AV 3/3) : affiche/arrete l'entree HDMI live ────────────────────────────
' Modeles avec entree HDMI : XT1145/XT2145/XT1144/XT1143/4k1142 (PAS le HD1024). L'entree est NATIVE
' (roVideoInput) -> ne passe PAS par le <video> du roHtmlWidget. On la joue plein ecran sur le plan VIDEO
' via roVideoPlayer.PlayFile(roVideoInput), et on bascule le plan graphique DERRIERE (SetGraphicsZOrder
' "back") pour que la video HDMI couvre le contenu HTML (prise de controle plein ecran, MVP ; overlay
' pendant HDMI = futur). Stop = Stop() + graphics "front" (retour au contenu CMS). Modele SANS entree :
' roVideoInput = invalid -> no-op (guard). On NE met PAS de port sur le player HDMI -> aucun roVideoEvent
' sur m.port (pas d'interference avec le screenshot/DL). Idempotent (m.hdmiPlayer).
'   ⚠️ A VALIDER HW (XT1144) : z-order/transparence du plan graphique vs plan video, conflit eventuel
'   avec le <video hwz> du contenu (decodeur partage), routage audio de l'entree.
Sub HandleHdmiInput(payload As Object)
    ' v44 : RELAIS AU RENDERER (sf_hdmicmd.json, poll ~2s cote signflow.js) — utilise par la page
    ' locale /control. L'ancienne voie native StartHdmiInput (roVideoPlayer.PlayFile(roVideoInput))
    ' n'est PLUS appelee : elle sur-alloue le decodeur du XT4 pendant que le roHtmlWidget tourne
    ' (PlayFile=false, prouve HW v22/v23) ; le rendu passe par le <video src="tv:..."> du renderer.
    ' v46 : gate du controle local — page desactivee (local_control=false) ou PIN incorrect =
    ' commande IGNOREE avant toute ecriture (le fichier relais ne bouge pas). Ne concerne QUE la
    ' page /control : les chemins authentifies (VOD/drawer CMS) passent par la WS du renderer.
    m = GetGlobalAA()
    if m.hdmiInCapable <> true or m.hdmiCmdFile = invalid then return
    if m.hdmiLocalEnabled = false then return
    mode = "start"
    pin = ""
    if payload.mode <> invalid then mode = payload.mode
    if type(payload.params) = "roAssociativeArray" then
        if payload.params.mode <> invalid then mode = payload.params.mode
        if payload.params.pin <> invalid then pin = payload.params.pin
    end if
    if m.hdmiLocalPin <> "" and pin <> m.hdmiLocalPin then
        ' v47 : refus compte (nack) -> la page /control affiche "Wrong PIN" et re-invite
        m.hdmiLocalNack = m.hdmiLocalNack + 1
        WriteControlState()
        return
    end if
    if mode <> "stop" then mode = "start"
    m.hdmiCmdSeq = m.hdmiCmdSeq + 1
    WriteAsciiFile(m.hdmiCmdFile, FormatJson({ mode: mode, seq: m.hdmiCmdSeq }))
    m.hdmiLocalAck = m.hdmiLocalAck + 1
    WriteControlState()
End Sub

' ── v46 : config du controle LOCAL (/control), poussee par le renderer ───────────────────────
' cmd "hdmi_local_cfg" {enabled, pin} — RAM uniquement (m.*), jamais persistee ni servie. Le
' renderer la pousse au boot (display_config persiste localStorage) + a chaque changement.
Sub SetHdmiLocalCfg(payload As Object)
    m = GetGlobalAA()
    if m.hdmiInCapable <> true then return
    if payload.enabled <> invalid then m.hdmiLocalEnabled = payload.enabled
    if payload.pin <> invalid then m.hdmiLocalPin = payload.pin
    if payload.name <> invalid then m.hdmiLocalName = payload.name   ' v48 : nom du player (affichage /control)
    WriteControlState()   ' v47 : reflete enabled/pin_required dans /controlst.json
End Sub

' ── v47 : etat du controle local (/controlst.json) — AUCUN secret ────────────────────────────
' {enabled, pin_required, ack, nack} : la page /control sait si un PIN est requis (booleen
' seulement, jamais le code) et si la derniere commande a ete acceptee (ack) ou refusee (nack).
Sub WriteControlState()
    m = GetGlobalAA()
    if m.hdmiCtlFile = invalid then return
    st = { enabled: m.hdmiLocalEnabled, pin_required: (m.hdmiLocalPin <> ""), name: m.hdmiLocalName, ack: m.hdmiLocalAck, nack: m.hdmiLocalNack }
    WriteAsciiFile(m.hdmiCtlFile, FormatJson(st))
End Sub

Sub StartHdmiInput()
    if m.hdmiSeq = invalid then m.hdmiSeq = 0
    m.hdmiSeq = m.hdmiSeq + 1
    diag = { seq: m.hdmiSeq, mode: "start", step: "begin", vi_valid: false, vp_valid: false, w: 0, h: 0, playfile: false, zorder: false }
    WriteAsciiFile("sf_hdmi.json", FormatJson(diag))
    if m.hdmiPlayer <> invalid then               ' deja affiche (idempotent)
        diag.step = "already_on"
        WriteAsciiFile("sf_hdmi.json", FormatJson(diag))
        return
    end if
    diag.step = "create_input"
    WriteAsciiFile("sf_hdmi.json", FormatJson(diag))
    vi = CreateObject("roVideoInput")
    if vi = invalid then
        diag.step = "input_invalid"               ' modele SANS entree HDMI (ou API absente sur ce BOS)
        WriteAsciiFile("sf_hdmi.json", FormatJson(diag))
        print "HDMI-in: roVideoInput indisponible sur ce modele"
        return
    end if
    diag.vi_valid = true
    diag.step = "create_player"
    WriteAsciiFile("sf_hdmi.json", FormatJson(diag))
    vp = CreateObject("roVideoPlayer")
    if vp = invalid then
        diag.step = "player_invalid"
        WriteAsciiFile("sf_hdmi.json", FormatJson(diag))
        return
    end if
    diag.vp_valid = true
    sw = 1920 : sh = 1080
    if m.scrW <> invalid then sw = m.scrW
    if m.scrH <> invalid then sh = m.scrH
    diag.w = sw : diag.h = sh
    diag.step = "set_rectangle"
    WriteAsciiFile("sf_hdmi.json", FormatJson(diag))
    rect = CreateObject("roRectangle", 0, 0, sw, sh)
    vp.SetRectangle(rect)
    diag.step = "playfile"
    diag.attempts = 0
    WriteAsciiFile("sf_hdmi.json", FormatJson(diag))
    ' Retry : le verrouillage du signal HDMI prend un instant -> PlayFile renvoie souvent false au 1er
    ' essai (pattern documente BrightSign : retry / roHdmiInputChanged). On reessaie jusqu'a 6x espacees
    ' de ~500ms via wait() sur un port vide (= pause, idiome prouve du repo). diag.attempts = nb d'essais.
    pf = false
    rp = CreateObject("roMessagePort")
    tries = 0
    while tries < 6 and not pf
        tries = tries + 1
        diag.attempts = tries
        WriteAsciiFile("sf_hdmi.json", FormatJson(diag))
        pf = vp.PlayFile(vi)
        if not pf then wait(500, rp)
    end while
    diag.playfile = pf
    if pf then
        m.hdmiPlayer = vp
        m.hdmiInput = vi
        diag.step = "zorder_back"
        WriteAsciiFile("sf_hdmi.json", FormatJson(diag))
        vm = CreateObject("roVideoMode")
        if vm <> invalid then
            vm.SetGraphicsZOrder("back")          ' video HDMI devant le contenu HTML
            diag.zorder = true
        end if
        diag.step = "playing"                     ' si "playing" mais ecran=fond HTML -> z-order/transparence/contention decodeur
        WriteAsciiFile("sf_hdmi.json", FormatJson(diag))
        print "HDMI-in: affichage de l'entree live"
    else
        diag.step = "playfile_false"              ' entree non lisible : pas de signal, ou decodeur deja pris par le <video hwz>
        WriteAsciiFile("sf_hdmi.json", FormatJson(diag))
        print "HDMI-in: PlayFile a echoue"
    end if
End Sub

Sub StopHdmiInput()
    if m.hdmiSeq = invalid then m.hdmiSeq = 0
    m.hdmiSeq = m.hdmiSeq + 1
    diag = { seq: m.hdmiSeq, mode: "stop", step: "begin" }
    WriteAsciiFile("sf_hdmi.json", FormatJson(diag))
    if m.hdmiPlayer <> invalid then
        m.hdmiPlayer.Stop()
        m.hdmiPlayer = invalid
    end if
    m.hdmiInput = invalid
    vm = CreateObject("roVideoMode")
    if vm <> invalid then vm.SetGraphicsZOrder("front")      ' retour au contenu CMS (plan graphique devant)
    diag.step = "stopped"
    WriteAsciiFile("sf_hdmi.json", FormatJson(diag))
    print "HDMI-in: arret -> retour au contenu"
End Sub

' ── cmd "firmware_update" (P1.1) : telecharge le .bsfw a la racine SD puis reboote ──────────
' Le backend (push gate-par-version-epinglee) publie la commande avec une URL signee du .bsfw.
' On telecharge le firmware a la RACINE de la SD (GetToFile SYNCHRONE) : au boot suivant, le
' BOOTLOADER BrightSign detecte le *.bsfw, VERIFIE SA SIGNATURE (firmware signe par BrightSign) et
' l'applique (puis le consomme). Un .bsfw corrompu/non signe (DL tronque, 404 servi) est REJETE par
' le bootloader -> NON applique -> PAS de brick (≠ autorun.brs qui n'a aucune signature). Le BOS est
' donc l'autorite d'integrite ici -> on se contente du code HTTP 200 comme gate (comme RefreshRenderer/
' RefreshAutorun) ; pas de sha256 device (hash de centaines de Mo via roEVPDigest = couteux + API non
' prouvee). On efface tout .bsfw obsolete avant, et on ne reboote QUE sur 200.
Sub DoFirmwareUpdate(payload As Object)
    m = GetGlobalAA()
    if payload.url = invalid or payload.url = "" then return
    DeleteFile("sf_update.bsfw")                 ' purge un DL precedent rate
    print "Firmware update: telechargement du .bsfw"
    ut = CreateObject("roUrlTransfer")
    ut.SetUrl(payload.url)
    ut.EnableEncodings(true)
    code = ut.GetToFile("sf_update.bsfw")        ' .bsfw a la RACINE SD (applique au boot par le BOS)
    if code <> 200 then
        DeleteFile("sf_update.bsfw")
        print "Firmware DL echec HTTP "; code
        return
    end if
    print "Firmware telecharge -> reboot (le BOS applique + valide la signature au boot)"
    RebootSystem()
End Sub

' ── cmd "ota_apply" (OTA §XXX slice 2, deploiement APPROUVE cote CMS) ─────────────────────────
' Le renderer (signflow.js bsHandleOta) POSTe {cmd:"ota_apply", version:"x.y.z"} quand la directive de
' deploiement l'y autorise (fenetre d'install / force). On tire le renderer + l'autorun de prod
' (SYNCHRONE, memes garde-fous anti-brick que le boot) MAINTENANT et INCONDITIONNELLEMENT, puis on
' reboote. Au boot suivant, signflow.js fraichement tire reporte sa version (BS_PLAYER_VERSION) >= cible
' -> le serveur constate la montee de version = succes du deploiement (mark_success_if_updated). Le
' renderer plafonne les tentatives (compteur persistant) -> pas de boucle de reboot si l'apply n'aboutit
' pas. Idempotent : re-tirer un code identique = no-op (RefreshRenderer/RefreshAutorunForced comparent).
' NB : pas de reponse HTTP (fire-and-forget cote renderer, comme les autres cmd).
Sub DoOtaApply(payload As Object)
    print "OTA apply : refresh renderer + autorun (prod) puis reboot"
    RefreshRenderer()
    RefreshAutorunForced()
    RebootSystem()
End Sub

' ── cmd "screenshot" : capture le framebuffer COMPOSITE via le DWS local + upload au CMS ─────
' Le <video hwz> est sur le plan video HW, HORS du canvas -> une capture canvas du renderer serait
' NOIRE. Le snapshot DWS (LDWS) capture le composite (video incluse), sauve un JPEG sur la SD et
' renvoie son chemin dans le corps JSON. Puis on lit le JPEG sur la SD et on l'upload au CMS.
'   payload.id_player : id du player en CHAINE (passe par le renderer -> evite de decoder le JWT ici).
'
' P2.3 — NON-BLOQUANT (machine a etats). AVANT : 2 wait(20000, sp) sur un port DEDIE -> la BOUCLE
' PRINCIPALE etait GELEE jusqu'a ~10-40s/h pendant que le roHttpServer ne servait plus le <video>
' (qui streame depuis /media/<key>) -> hoquet de lecture a chaque screenshot horaire. MAINTENANT les
' 2 transferts (snapshot DWS puis upload) sont ASYNC sur m.port -> la boucle continue de servir la
' video ; leurs roUrlEvent sont routes par HandleUrlEvent. EXCLUSION MUTUELLE (au lieu d'une
' discrimination par GetSourceIdentity, deprecate) : pendant un screenshot (m.ssState>0), PumpQueue est
' EN PAUSE -> aucun autre transfert async sur m.port -> tout roUrlEvent recu appartient au screenshot.
' Invariant : m.ssState>0 => m.dling=invalid (StartScreenshot DIFFERE si un DL est en vol -> begin au
' DL suivant). Aucune nouvelle methode BrightScript (zero risque brick ajoute) ; chaque transfert async
' emet TOUJOURS exactement 1 roUrlEvent (succes/erreur/timeout) -> la machine avance toujours.
'   etats m.ssState : 0 idle | 1 snapshot en vol | 2 upload en vol.
Sub StartScreenshot(payload As Object)
    m = GetGlobalAA()
    if m.backend = "" or m.token = "" then return
    if payload.id_player = invalid then return
    id = payload.id_player
    if id = "" then return
    if m.ssState <> 0 then return                ' un screenshot deja en cours -> on ignore (horaire)
    if m.dling <> invalid then                   ' un DL est en vol -> on DIFFERE (begin au DL suivant)
        m.ssPending = true
        m.ssPendingId = id
        return
    end if
    BeginSnapshot(id)
End Sub

' Lance le transfert SNAPSHOT (async sur m.port). Appele quand aucun DL n'est en vol.
Sub BeginSnapshot(id As String)
    m = GetGlobalAA()
    host = "127.0.0.1"
    if type(m.lanIp) = "String" and m.lanIp <> "" then host = m.lanIp
    snap = CreateObject("roUrlTransfer")
    snap.SetPort(m.port)
    snap.SetUrl("http://" + host + "/api/v1/snapshot/")
    ' Auth BASIC PREEMPTIVE (en-tete pose des la 1re requete) -> AUCUN challenge 401 -> AUCUN renvoi du
    ' corps POST -> evite "necessary data rewind wasn't possible". P0.3 : mdp DWS DERIVE PAR DEVICE
    ' (m.dwsPw) -> en-tete base64 calcule au RUNTIME (roByteArray.ToBase64String). Username DWS = "admin".
    credBa = CreateObject("roByteArray")
    credBa.FromAsciiString("admin:" + m.dwsPw)
    snap.AddHeader("Authorization", "Basic " + credBa.ToBase64String())
    snap.AddHeader("Content-Type", "application/json")
    snap.AddHeader("Accept", "application/json")
    ' Corps JSON LU d'un fichier (vraies quotes, hors du .brs -> pas de quote-echappee a la compile).
    ' P0.2 : ss_body.json = override OPTIONNEL ; absent/illisible -> FormatJson (pas de quote echappee).
    ' L'endpoint exige width/height (corps vide ou {} -> 500).
    body = ReadAsciiFile("ss_body.json")
    if type(body) <> "String" or body = "" then body = FormatJson({ width: 1280, height: 720 })
    if snap.AsyncPostFromString(body) then
        m.ssXfer = snap
        m.ssPlayerId = id
        m.ssState = 1
    else
        ScreenshotDone()                         ' echec d'init -> reprendre les DL
    end if
End Sub

' Route les roUrlEvent du screenshot (appele par HandleUrlEvent quand m.ssState>0).
Sub HandleScreenshotEvent(ev As Object)
    m = GetGlobalAA()
    if m.ssState = 1 then
        ' Reponse du SNAPSHOT DWS -> extraire le chemin du JPEG puis lancer l'UPLOAD.
        if ev.GetResponseCode() <> 200 then
            ScreenshotDone()
            return
        end if
        j = ParseJson(ev.GetString())
        if type(j) <> "roAssociativeArray" then
            ScreenshotDone()
            return
        end if
        ' BOS 9.1.132 : structure imbriquee data.result.filename (camelCase, PAS top-level).
        fn = invalid
        if type(j.data) = "roAssociativeArray" then
            if type(j.data.result) = "roAssociativeArray" then fn = j.data.result.filename
        end if
        if type(fn) <> "String" then
            ScreenshotDone()
            return
        end if
        local = SdPathFromDws(fn)
        if local = "" then
            ScreenshotDone()
            return
        end if
        up = CreateObject("roUrlTransfer")
        up.SetPort(m.port)
        up.SetUrl(m.backend + "/api/v1/players/" + m.ssPlayerId + "/screenshot")
        up.AddHeader("Authorization", "Bearer " + m.token)
        up.AddHeader("Content-Type", "image/jpeg")
        if up.AsyncPostFromFile(local) then
            m.ssXfer = up
            m.ssLocal = local
            m.ssState = 2
        else
            DeleteFile(local)
            ScreenshotDone()
        end if
    else if m.ssState = 2 then
        ' Reponse de l'UPLOAD -> nettoyer le JPEG temporaire et terminer.
        if m.ssLocal <> "" then DeleteFile(m.ssLocal)
        ScreenshotDone()
    end if
End Sub

' Termine un screenshot : reset l'etat + reprend les telechargements en attente.
Sub ScreenshotDone()
    m = GetGlobalAA()
    m.ssState = 0
    m.ssXfer = invalid
    m.ssLocal = ""
    m.ssPlayerId = ""
    PumpQueue()
End Sub

' "/sd/remote snapshots/img.jpg" -> "SD:/remote snapshots/img.jpg" (prefixe insensible a la casse).
Function SdPathFromDws(p As String) As String
    if Len(p) < 4 then return ""
    if LCase(Mid(p, 1, 4)) = "/sd/" then return "SD:/" + Mid(p, 5)
    return ""
End Function

' ── Télécharge UN média à la fois (séquentiel = robuste sur le hairpin) ──────
Sub PumpQueue()
    m = GetGlobalAA()
    if m.dling <> invalid then return
    if m.ssState > 0 then return                ' P2.3 : un screenshot async est en vol -> on PAUSE les DL
    ' P2.7/P2.8 : items courants d'abord (m.dlQueue) ; sinon prefetch (BASSE priorite) si la file n'est
    ' pas vide ET la fenetre horaire l'autorise. Le contenu courant n'est JAMAIS gate par une fenetre.
    job = invalid
    if m.dlQueue.Count() > 0 then
        job = m.dlQueue.RemoveHead()
    else if m.prefetchQueue.Count() > 0 and PrefetchAllowed() then
        job = m.prefetchQueue.RemoveHead()
    end if
    if job = invalid then return
    k = job.cache_key
    ' Telecharge vers un nom TEMPORAIRE (dltmp_*) puis renomme en sfc_* a la reussite -> un reboot
    ' en plein DL ne laisse jamais un sfc_ partiel que ScanCache servirait comme valide.
    tmp  = "dltmp_" + Flat(k)
    dest = m.prefix + Flat(k)          ' fichier final a la racine SD (ex sfc_1~abc.mp4)
    ut = CreateObject("roUrlTransfer")
    ut.SetPort(m.port)
    ut.SetUrl(job.url)
    ut.EnableEncodings(true)
    ok = ut.AsyncGetToFile(tmp)
    if ok then
        m.dling = ut : m.dlKey = k : m.dlTmp = tmp : m.dlDest = dest
    else
        print "AsyncGetToFile refuse: "; k
    end if
End Sub

' ── P2.7 : (re)construit la file de PREFETCH depuis la liste poussee par le renderer ─────────
' Le renderer POSTe {prefetch:[{cache_key,url}], windows:[{start,end}]} apres chaque sync. La liste est
' deja TRIEE par priorite (date de diffusion la plus proche d'abord) cote renderer. On REMPLACE la file
' (bornee + fraiche) en sautant ce qui est deja en cache / en cours / dans la file principale. Les
' fenetres horaires (P2.8) sont stockees pour gater le drainage (cf. PrefetchAllowed).
Sub HandlePrefetch(list As Object, windows As Object)
    m = GetGlobalAA()
    if type(windows) = "roArray" then m.prefetchWindows = windows
    nq = CreateObject("roList")
    for each it in list
        if type(it) = "roAssociativeArray" and it.cache_key <> invalid and it.url <> invalid then
            k = it.cache_key
            if not m.cached.DoesExist(k) and not Queued(k) then
                nq.AddTail({ cache_key: k, url: it.url })
            end if
        end if
    end for
    m.prefetchQueue = nq
    PumpQueue()
End Sub

' ── P2.8 : le prefetch est-il autorise A CETTE HEURE (fenetres horaires) ? ───────────────────
' Aucune fenetre configuree => toujours autorise. Sinon : heure LOCALE du device dans une fenetre
' [start,end[ (gere le chevauchement de minuit start>end). FAIL-OPEN (roDateTime indispo / parse KO =>
' on autorise) : une fenetre ne doit jamais BLOQUER tout le prefetch par erreur. roDateTime/roArray =
' API standard (roArray a bien Count(), contrairement a roAssociativeArray).
Function PrefetchAllowed() As Boolean
    m = GetGlobalAA()
    if type(m.prefetchWindows) <> "roArray" then return true
    if m.prefetchWindows.Count() = 0 then return true
    dt = CreateObject("roDateTime")
    if dt = invalid then return true
    dt.ToLocalTime()
    cur = dt.GetHours() * 60 + dt.GetMinutes()
    for each w in m.prefetchWindows
        if type(w) = "roAssociativeArray" and type(w.start) = "String" and type(w.end) = "String" then
            s = ParseHHMM(w.start)
            e = ParseHHMM(w.end)
            if s >= 0 and e >= 0 then
                if s <= e then
                    if cur >= s and cur < e then return true
                else
                    if cur >= s or cur < e then return true     ' fenetre a cheval sur minuit
                end if
            end if
        end if
    end for
    return false
End Function

' "HH:MM" -> minutes depuis minuit (ex "02:05" -> 125). -1 si invalide. Tolere "H:MM" (Val s'arrete au
' separateur). Minutes = 2 derniers chars (robuste a la longueur).
Function ParseHHMM(s As String) As Integer
    if Len(s) < 4 then return -1
    hh = Val(Left(s, 2))
    mm = Val(Mid(s, Len(s) - 1, 2))
    return hh * 60 + mm
End Function

' ── Fin de téléchargement (roUrlEvent) : enregistre + suivant ────────────────
Sub HandleUrlEvent(ev As Object)
    m = GetGlobalAA()
    ' P2.3 : pendant un screenshot (m.ssState>0) les DL sont en PAUSE (PumpQueue garde) -> aucun autre
    ' transfert async sur m.port -> ce roUrlEvent est forcement celui du screenshot. (Exclusion mutuelle
    ' = pas de discrimination par GetSourceIdentity, deprecate.)
    if m.ssState > 0 then
        HandleScreenshotEvent(ev)
        return
    end if
    code = ev.GetResponseCode()
    if code = 200 or code = 206 then
        if MoveFile(m.dlTmp, m.dlDest) then    ' rename meme-volume -> le sfc_* n'apparait qu'ici
            RegisterMedia(m.dlKey)
            m.cached[m.dlKey] = true
            RefreshCachedJson()
            print "cache OK: "; m.dlKey
        else
            DeleteFile(m.dlTmp)
            print "MoveFile echec: "; m.dlKey
        end if
    else
        DeleteFile(m.dlTmp)                     ' purge le partiel
        print "DL echec "; m.dlKey; " code="; code
    end if
    m.dling = invalid : m.dlKey = "" : m.dlTmp = "" : m.dlDest = ""
    ' P2.3 : un screenshot differe (DL etait en vol au moment de la cmd) -> le lancer MAINTENANT (priorite
    ' sur le DL suivant pour eviter la famine si les DL s'enchainent) ; sinon enchainer les DL.
    if m.ssPending then
        m.ssPending = false
        BeginSnapshot(m.ssPendingId)
    else
        PumpQueue()
    end if
End Sub

' ── GET /media/<cache_key> → fichier aplati sur SD ──────────────────────────
Sub RegisterMedia(k As String)
    m = GetGlobalAA()
    m.server.AddGetFromFile({ url_path: "/media/" + k, content_type: GuessType(k), filename: m.prefix + Flat(k) })
End Sub

' ── /cached.json = liste JSON des cache_keys dispos ─────────────────────────
Sub RefreshCachedJson()
    m = GetGlobalAA()
    keys = CreateObject("roArray", 0, true)
    for each k in m.cached
        keys.Push(k)
    end for
    body = FormatJson({ keys: keys })
    m.server.AddGetFromString({ url_path: "/cached.json", content_type: "application/json", body: body })
End Sub

' ── Auto-fetch du renderer : telecharge les sf_rnd_* MANQUANTS depuis le backend ─────
' Rend le player deployable sans staging manuel : un SD vierge se provisionne au 1er boot (online).
' Si le fichier existe deja -> on garde la copie SD (sert direct, offline-safe, pas de hang/DL).
' GetToFile est BLOQUANT et tourne AVANT Show() -> les fichiers sont la quand le widget les demande.
' v54 : renvoie true quand les 5 fichiers sont sur la SD. Telechargement en TEMP + code 200 exige
' (avant : GetToFile direct dans sf_rnd_* -> un 404/502 au demarrage devenait un "renderer" que plus
' rien ne retelechargeait, puisque le fichier existait). Rappele par la boucle tant qu'il manque un
' fichier (reseau lent a monter au boot : ~4 min sur un HD1024), cf. m.rndMissing.
Function EnsureRenderer() As Boolean
    m = GetGlobalAA()
    if m.backend = "" then return true
    have = {}
    existing = MatchFiles("SD:/", "sf_rnd_*")
    if type(existing) = "roList" then
        for each fn in existing
            have[fn] = true
        end for
    end if
    names = ["ed25519.js", "signflow.js", "audio_player.js", "player.js", "style.css"]
    complete = true
    for each f in names
        dest = "sf_rnd_" + f
        if not have.DoesExist(dest) then
            tmp = "sfrtmp_" + f
            ut = CreateObject("roUrlTransfer")
            ut.SetUrl(m.backend + "/player-chromeos/" + f)
            ut.EnableEncodings(true)
            code = ut.GetToFile(tmp)
            ok = false
            if code = 200 then
                if ReadAsciiFile(tmp) <> "" then ok = true
            end if
            if ok then
                MoveFile(tmp, dest)
                print "renderer recupere: "; f
            else
                DeleteFile(tmp)
                complete = false
            end if
        end if
    end for
    return complete
End Function

' NB Tier 1.5 (refresh renderer auto-update) RETIRE : le routage des roUrlEvent par
' SetIdentity/GetSourceIdentity n'est pas fiable sur ce firmware (crash autorun = LED rouge 10x
' des le lancement video). A reprendre avec un mecanisme verifie (port dedie, ou comparaison du
' transfert) en session dediee. EnsureRenderer (DL des manquants au boot) reste en place.

' ── PERSISTANCE REBOOT : re-decouvre les fichiers de cache deja sur la SD ────
' Les sfc_* ecrits par roUrlTransfer survivent au reboot (contrairement au Cache API).
' Au boot on les re-enregistre (route /media/<key> + m.cached) -> offline immediat.
' fn "sfc_1~abc.mp4" -> key "1/abc.mp4". MatchFiles sur le volume SD: (racine SD).
Sub ScanCache()
    m = GetGlobalAA()
    ' Purge les telechargements interrompus (dltmp_* media, sfrtmp_* refresh renderer) d'une
    ' session precedente (reboot en plein DL).
    for each pat in ["dltmp_*", "sfrtmp_*"]
        stale = MatchFiles("SD:/", pat)
        if type(stale) = "roList" then
            for each f in stale
                DeleteFile(f)
            end for
        end if
    end for
    files = MatchFiles("SD:/", m.prefix + "*")
    if type(files) = "roList" then
        for each fn in files
            k = Unflat(Mid(fn, Len(m.prefix) + 1))
            if k <> "" and not m.cached.DoesExist(k) then
                RegisterMedia(k)
                m.cached[k] = true
            end if
        end for
    end if
    RefreshCachedJson()
End Sub

' De-aplatit un nom de fichier SD ("1~abc.mp4" -> "1/abc.mp4") — inverse de Flat().
Function Unflat(s As String) As String
    r = ""
    for i = 1 to Len(s)
        c = Mid(s, i, 1)
        if c = "~" then
            r = r + "/"
        else
            r = r + c
        end if
    end for
    return r
End Function

Function Queued(k As String) As Boolean
    m = GetGlobalAA()
    for each j in m.dlQueue
        if j.cache_key = k then return true
    end for
    if m.dling <> invalid and m.dlKey = k then return true
    return false
End Function

' Aplatit le cache_key ("1/abc.mp4" → "1~abc.mp4") pour un nom de fichier SD plat.
Function Flat(k As String) As String
    r = ""
    for i = 1 to Len(k)
        c = Mid(k, i, 1)
        if c = "/" then r = r + "~" else r = r + c
    end for
    return r
End Function

Function GuessType(k As String) As String
    lk = LCase(k)
    if EndsWith(lk, ".mp4") or EndsWith(lk, ".m4v") then return "video/mp4"
    if EndsWith(lk, ".webm") then return "video/webm"
    if EndsWith(lk, ".mkv") then return "video/x-matroska"
    if EndsWith(lk, ".jpg") or EndsWith(lk, ".jpeg") then return "image/jpeg"
    if EndsWith(lk, ".png") then return "image/png"
    if EndsWith(lk, ".webp") then return "image/webp"
    if EndsWith(lk, ".mp3") then return "audio/mpeg"
    if EndsWith(lk, ".ogg") then return "audio/ogg"
    return "application/octet-stream"
End Function

Function EndsWith(s As String, suf As String) As Boolean
    if Len(s) < Len(suf) then return false
    return Right(s, Len(suf)) = suf
End Function
