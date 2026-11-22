# SpawnPoint

A MintServers-style Minecraft host for your own machine, all in **one Docker container**:

- **One-click modpacks** from Modrinth, CurseForge (with a free API key) and FTB. Java version, loader, RAM and CPU cores are chosen for you.
- **Add single mods** to any server. Search is filtered to the server's loader and Minecraft version, and you're warned when players need the mod too.
- **Addresses without ports**, like `atm10.mc.example.com`. SpawnPoint's built-in router reads the hostname the player typed and connects them to the right server, so every server shares port 25565.
- **Wake on join:** a sleeping server shows "💤 asleep: join to wake it" in the multiplayer list, and the first player who tries to join starts it. Servers that are getting ready show their progress there too (for example "⏳ Generating spawn… 62%").
- **Spawn pre-generated before opening**, then the rest of the world in the background on spare CPU cores. Generation pauses whenever tick time rises above 40 ms and resumes below 30 ms.
- **Automatic performance tuning:** each server gets its own CPU cores (following the L3 cache on CPUs that have several), and its main game thread is pinned there. RAM grows on demand and idle memory goes back to the machine (ZGC). Chunky, spark, FerriteCore, ModernFix and Lithium are added when missing.
- **World maps** drawn from the world files, with live player positions, named markers and a layer showing where players spend their time.
- **Safe Minecraft version upgrades:** a copy of the server is tried on the new version first; the real one only switches if it starts.
- **Crashes explained in plain words**, naming the mod they point at, and **mod and plugin updates** checked daily from Modrinth.
- **Discord chat bridge** both ways, and **player history**: playtime, visits, first and last seen.
- **Incremental backups** on a schedule, **scheduled restarts** with an in-game countdown, and **automatic restart after a crash**.
- **Import existing servers:** upload a `.zip` or `.tar.gz` of a server folder. SpawnPoint detects the type and versions, keeps the world, mods and settings, and deploys it.
- **Users and roles** with an activity log, plus console, players and whitelist, `server.properties` editor, file editor and auto-sleep.

```
Player ─► atm10.mc.example.com ─► your router (TCP 25565) ─► this machine:25565 ┌ SpawnPoint container ──────────┐
                                                                                  │ router ─► mc-atm10             │
                                                                                  │        └► mc-survival          │
                                                                                  │ (private Docker engine inside) │
                                                                                  └────────────────────────────────┘
```

**Needs:** a 64-bit Linux machine (Intel, AMD or ARM, like a Raspberry Pi 4/5 with 8 GB) with Docker and Docker Compose. See [Sizing](#sizing) for how much CPU and RAM. Running something else? See [Other setups](#other-setups) at the bottom: Unraid, a reverse proxy, Pangolin on a VPS, or Docker without privileged mode.

---

## Quick start

### 1. DNS (one record)

At your DNS provider for **example.com**, point a wildcard at your home connection:

| Type | Name | Value |
|---|---|---|
| A | `*.mc` | your public IP |

If your home IP changes, use dynamic DNS instead: point `*.mc` with a **CNAME** at your dynamic DNS name (for example `yourname.duckdns.org`), and keep that name updated with your provider's updater.

If example.com is on Cloudflare, set the record to **DNS only (grey cloud)**. Cloudflare's proxy only carries web traffic, so Minecraft won't connect through it.

### 2. Open the Minecraft port

On your router, forward **TCP 25565** to **this machine's LAN IP, port 25565**. If the machine has a firewall, allow it there too (for example `sudo ufw allow 25565/tcp`).

Don't forward port 8090 (the dashboard). Use it on your home network, or see [Behind a reverse proxy](#behind-a-reverse-proxy) to put it on HTTPS.

> If port forwarding doesn't work at all, your ISP may use carrier-grade NAT (CGNAT). Then you need a tunnel to a VPS: see [Pangolin on a VPS](#pangolin-on-a-vps).

### 3. Run SpawnPoint

```bash
git clone https://github.com/z354prance/SpawnPoint.git spawnpoint
cd spawnpoint
cp .env.example .env
nano .env                 # set ADMIN_PASSWORD, BASE_DOMAIN (mc.example.com) and TZ
docker compose up -d
```

Or without cloning the code, just the two files:

```bash
mkdir spawnpoint && cd spawnpoint
curl -fsSLO https://raw.githubusercontent.com/z354prance/SpawnPoint/main/docker-compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/z354prance/SpawnPoint/main/.env.example
nano .env
docker compose up -d
```

Open **`http://<this-machine>:8090`** and sign in as **admin** with your `ADMIN_PASSWORD`. Set your own password, then add everyone else under **Users**. Optional: put a free [CurseForge API key](https://console.curseforge.com) in `.env` for CurseForge modpacks.

Everything is kept in `./data` next to the compose file: `servers` (worlds, mods, configs), `backups`, and `engine` (SpawnPoint's private Docker engine). Keep `servers` and `engine` on a fast local disk (an SSD if you can), not a network share: on a network or FUSE filesystem the private engine falls back to a very slow storage mode, and SpawnPoint warns about it at startup.

**Updating:** `docker compose pull && docker compose up -d` in the same folder.

**What to expect:**
- `docker ps` only shows **spawnpoint**. Each Minecraft server still runs in its own container, with its own CPU cores and RAM limit, but inside SpawnPoint (that's why it needs `privileged: true`).
- **Restarting or updating SpawnPoint restarts every Minecraft server.** SpawnPoint saves and stops each world cleanly, remembers which ones were running, and starts them again when it comes back, usually within a minute or two. The same happens after a reboot.
- The compose file gives SpawnPoint 3 minutes to stop (`stop_grace_period`), so big modpacks have time to save.

**Getting a shell (rarely needed).** Everything normal is in the web UI. If you want a raw shell or an interactive Minecraft console:
```bash
docker exec -it spawnpoint docker ps                          # list the Minecraft containers
docker exec -it spawnpoint docker exec -it mc-atm10 bash      # shell in a server
docker exec -it spawnpoint docker exec -it mc-atm10 rcon-cli  # interactive Minecraft console
```

## Sizing

SpawnPoint sizes itself to the machine it runs on:
- **Cores:** the CPU is split into units of 2 cores (with their SMT threads), following the L3 cache where a chip has several. A 6-core Ryzen is three units; an EPYC 7F72 is twelve. Each server gets its own units where possible, and spare units are lent out to speed up starts, pre-generation and maps. At most a third of the units are held back for the rest of the machine (`RESERVED_CORE_GROUPS`), so Minecraft always gets most of the CPU. With few units, servers share them.
- **Memory:** unless `RAM_POOL_GB` is set, all Minecraft servers together may use three quarters of the machine's RAM, at most 96 GB (24 GB on a 32 GB machine, 12 GB on 16 GB). A single server's heap is capped to fit inside that, so a big modpack can't push a small machine into swapping.
- **Rough guide:** a vanilla or Paper server is comfortable on 4 cores and 8 GB. A 150–300 mod pack wants 6+ fast cores and 16 GB, and starts noticeably slower on low-clock or older CPUs.
- **Speed tips:** set the CPU governor to **performance** (for example `cpupower frequency-set -g performance`). Single-core speed matters most for Minecraft.

## The dashboard

- **Overview:** the page you land on. It sums up the machine (servers running, players, Minecraft memory, total memory, disk), lists everyone playing on every server, and shows recent activity for admins. Click the SpawnPoint logo to get back to it from anywhere.
- **Left:** your servers, each with its address (click the icon to copy it), players and tick time, and a progress bar while it sets up or pre-generates. **New server** is under the list.
- **The ring** at the bottom left shows the whole machine at a glance, with each quarter labelled. Top left: memory (Minecraft and everything else). Top right: the Minecraft RAM pool, one segment per server. Bottom right: disk (worlds and backups, then everything else). Bottom left: CPU core units, orange for a server's own cores and gold for cores lent out. The thin inner ring is players online. Hover or tap any part for its numbers. Admins get the **Users** and **Activity log** buttons in the middle.
- **Middle:** the selected server: Restart, Stop and Force stop, the main facts (including how long the last start took), a world map with live player positions and an optional overlay of how far the world has been generated, tick time and memory for the last hour, and who's playing.
- **Right:** Console, Mods, Players, Settings, Files, Backups, Schedule and Stats for that server.
- **User guide:** the book button next to Sign out opens a guide to everything in the dashboard, written for the people using it. Sections only show for roles that can use them. Its text lives in `app/static/guide.js`.
- **Setup progress** is one bar split into the four steps (download, loader, mods, spawn), filling from crimson to gold. Bars that are actively working have a moving shine; it's turned off if your system asks for reduced motion.

## Shared invite list (optional)

One list of trusted players that any server can use. In a server's **Players** tab, **Use the shared invite list** adds everyone on it to that server's whitelist; untick anyone who shouldn't be on that particular server. **Edit shared list** adds and removes people, and can import a server's current whitelist in one go. Servers keep their own whitelist entries: the shared list only ever adds, and later removes, the names it put there. Running servers change straight away; stopped ones get the change in their `whitelist.json` (UUIDs from Mojang, or worked out from the name on offline-mode servers). It only keeps people out when **Invite only** is on for that server.

## Users and the activity log

Everyone signs in with their own username and password. Roles apply to every server:

| Role | Can |
|---|---|
| **Admin** | Everything, including users, the activity log, deleting servers and deleting backups |
| **Moderator** | Start, stop and restart servers, use the console, manage players, mods, settings, files and schedules, make, download and restore backups, and **create or import new servers** |
| **Viewer** | See servers, the console, players, mods, schedules and stats. Can't change anything, and can't see `server.properties`, files or backup downloads (they contain passwords) |

- The first account, **admin**, is created from `ADMIN_PASSWORD`. SpawnPoint reminds you to change it.
- Admins add, change and remove people under **Users**. Changing someone's role or password signs them out everywhere. There must always be at least one admin.
- Anyone can change their own password by clicking their name at the bottom left.
- After 5 wrong passwords from the same address, or 10 for the same username from anywhere, sign-ins are blocked for 10 minutes.
- The **Activity log** records every sign-in (including failed ones, with the IP address) and every change: starts, stops, crashes, console commands (with the command), mods, players, settings, files, backups, schedules, new and deleted servers, and user changes. Attempts someone's role didn't allow are recorded too. Filter it by person, type or server. It's kept in `/servers/.spawnpoint/activity.log` (5 MB, then one older file).

## Backups, schedules and crashes

**Backups** are incremental: each one only stores what changed since the one before, down to the changed parts of region files, compressed, so they're quick and small, and every one is still a full restore point. They're safe while the server runs (saving is paused and flushed first) and run at low priority. In the **Backups** tab you can back up now, restore (the current files are kept in a `.pre-restore` folder), download any backup as a `.zip`, and choose how many to keep: by default the 6 most recent, then one per hour for a day, per day for a week, per week for a month and per month for 3 months. Older `.tar.gz` backups from earlier versions are still listed and restorable.
- All servers share one backup store, so files that several servers have in common (the same modpack's mods and libraries) are stored once. Each server's keep rules only ever thin out its own backups.
- They're stored with [restic](https://restic.net) in `backups/restic`, with the key in `backups/.restic-password`. Keep that file with the backups: without it they can't be read. To get files out without SpawnPoint: `restic -r backups/restic --password-file backups/.restic-password restore latest --host <server> --target /somewhere`.

**Schedule** tab: per-server tasks that run daily at a set time (on chosen weekdays) or every 1–12 hours: back up, restart, stop, start, or run a console command. Restarts and stops can warn players with a countdown in chat (up to 15 minutes before; the restart itself happens at the set time), and a restart can back up first. Times are in the `TZ` time zone. If SpawnPoint was down when a task was due, it runs when it's back if it's less than 10 minutes late, otherwise it waits for the next time. Every run is in the activity log.

**Restoring part of a backup:** **Browse** on a backup lets you tick files or folders (one player's data in `world/playerdata`, a config folder, a single region file) and put just those back. The server has to be stopped. What they replace is kept in `<server>.pre-restore-files/`.

**Lag reports:** when a server's slowest ticks stay over 50 ms (`LAG_REPORT_MS`) for most of three minutes, SpawnPoint records a 60-second [spark](https://spark.lucko.me) profile (at most every 30 minutes) and keeps the link in the Stats tab and the activity log, and sends an alert. Open it to see which mod or part of the game used the time. Needs spark, which SpawnPoint adds to modded servers and Paper includes; turn it off per server in Stats.

**Crashes:** if a server goes down without anyone stopping it (a crash, a freeze caught by Minecraft's watchdog, or running out of memory), SpawnPoint logs it with the reason and starts it again. After 3 crashes in 15 minutes it stops trying and shows an error, so a broken mod can't loop forever. Typing `stop` in the console isn't a crash. Turn it off per server in **Settings**.

**Crash reports in plain words:** after a crash, or a start that fails, SpawnPoint reads the newest crash report and the end of the log and says what happened: a mod missing a mod it needs (and which version), two copies of the same mod, a client-only mod on the server, a mod that couldn't patch the game, a mod built for a newer Java, the server freezing (and what it was busy in), running out of memory, or a crash and the mod its stack trace points at. That appears above the Console with buttons to open the crash report and turn the mod off, and in the activity log and alerts.

## World maps

Every server gets a simple top-down map of its world, drawn by SpawnPoint itself. It works the same for Paper, Purpur, Fabric, Quilt, Forge, NeoForge and vanilla, because nothing is installed in the servers: SpawnPoint reads the world's region files directly (read-only) and draws one pixel per block, coloured from Minecraft's own block textures and from the textures inside each mod jar, so modded blocks get their real colours too. Grass, leaves and water take their biome's colour (dark forests, snowy taiga, swamps, warm and frozen oceans), hills are shaded and deeper water is darker. Every generated chunk is drawn. Worlds from before 1.13 (like 1.12.2 modpacks) work too: their numbered blocks are converted to today's names, and modded blocks are looked up in the world's own `level.dat`.

- **When it draws:** a few minutes after a world first exists, then whenever the world has changed, at most every 10 minutes (`MAP_INTERVAL_MIN`), and straight after pre-generation finishes a stage. Only the 512×512-block regions that changed are redrawn, so updates are quick. Up to two servers are drawn at once (`MAP_PARALLEL`). Drawing runs on spare cores at low priority and pauses by itself if a server's tick time climbs. **Update map** in the map card redraws the whole map from scratch straight away. If a region can't be drawn, the map card says which one and why.
- **In the dashboard:** drag to move, scroll to zoom (down to about 4 pixels per block, enough to pick out a base), double-click to go back to spawn, which is marked with a house. The arrow keys and plus and minus work too. Players are shown live with their faces (from mc-heads.net, fetched once by SpawnPoint and cached for a day; Bedrock players get a coloured letter) and the direction they're facing, wherever they are, even far outside the pre-generated area: every region that exists is drawn, and the map can be dragged out to any player. The **Generation** button lays the pre-generation chunk grid over the terrain: the spawn area, land generated while playing, the edge being generated now, and the pre-generation limit (a square centred on the world spawn). Until the first map is drawn, the card shows that chunk view on its own. The Overworld, Nether and End each get their own map. The expand button shows the map full screen (Esc closes it).
- **Good to know:** a 5,000-block pre-generated world is about 400 regions and takes a few minutes on a couple of cores. The first render downloads Minecraft's client jar once from Mojang for its textures. Maps live in `servers/.maps/<server>` and are small (tens of MB). It isn't a 3D map and doesn't zoom down to single blocks in detail; that's deliberate, to keep it fast.
- **Public map:** in a server's **Settings**, turn on **Share a view-only map** to get an unguessable link (`/m/…`) that shows the terrain, and optionally live players and spawn, to anyone, with no account and nothing else from the dashboard. **Make a new link** retires the old one. The link only works where the dashboard itself can be reached, so sharing it outside your network needs the dashboard on a public address (for example through a reverse proxy).
- **Markers:** moderators press **Add marker** and click the map to name a spot (a base, a farm, a portal) in one of seven colours. Anyone can click a marker to see its coordinates; moderators can rename, move, recolour or remove it. Markers show on the public map too, unless that's unticked for one. Up to 300 per server.
- **Activity:** the **Activity** button shades where players have spent their time, in 32-block squares, from their positions sampled once a minute.
- Otherwise maps need the dashboard sign-in, like everything else. Turn them off with `MAP_ENABLED=false`.

## Player history

The **Players** tab lists everyone who has played: total playtime, number of visits, when they were first and last seen, and where they usually are (click it to see the spot on the map). It's counted from the player list every 10 seconds and positions once a minute, and kept in `/servers/.spawnpoint/players/<server>.json`.

## Minecraft version upgrades

For servers without a modpack (modpacks change version through pack updates). In **Settings → Minecraft version**, pick a newer release and press **Check**: it shows the Java change and, for every mod or plugin from Modrinth, whether there's a build for the new version. **Upgrade** then:

1. backs up,
2. copies the server to a trial folder while the real one keeps running (an instant copy on Btrfs, XFS or ZFS; otherwise it needs free space for a full copy),
3. updates the copy's mods, and turns off mods that have no build for the new version (plugins are left on, since they often still work),
4. starts the copy on the new version in its own container with no ports open, and waits until it has fully started.

Only if the copy starts cleanly is the real server stopped, switched the same way and started again. If it doesn't start, the real server isn't touched, and the card says why in plain words with the end of the trial's log. **Roll back** puts the old version, mods and world back from the backup (anything since is lost), since Minecraft can't move a world to an older version itself.

## Mod and plugin updates

On servers without a modpack, the **Mods** tab checks once a day which mods and plugins have a newer build for the server's loader and Minecraft version, by asking Modrinth about each file's checksum, so it works however the file got there. **Update** one or all; the new file is checked against Modrinth's checksum before it replaces the old one, and the old files are kept in `.spawnpoint/old-mods/` so **Undo that update** can put them back. Restart the server to load the new versions. Files that didn't come from Modrinth can't be checked. An alert can say when updates are waiting.

## Discord chat

In a server's **Settings → Discord chat**:

- **Game → Discord** needs only a channel webhook (channel settings → Integrations → Webhooks). Chat appears under the player's name and face; joins and leaves, deaths, advancements and the server going up or down can each be turned off. Nobody can ping `@everyone` from the game.
- **Discord → game** needs a Discord bot, set up once by an admin and shared by all servers: create an application at discord.com/developers, add a bot, turn on **Message Content Intent**, and invite it with the *View Channels* and *Read Message History* permissions. Then paste the channel's ID (Developer Mode → right-click the channel → Copy Channel ID). New messages are shown in game as `[Discord] name: message` within a few seconds. Messages from bots and webhooks are skipped, so nothing echoes.
- **Send a test** posts a hello and checks the bot can read the channel. Webhook URLs and the bot token are kept in `/servers/.spawnpoint/chat.json`, readable only by SpawnPoint, and never sent to the browser.

## How a new server comes up

1. **Installing:** itzg/minecraft-server downloads the loader or modpack (big packs can take 5–15 min).
   - If the pack includes **client-only mods** that crash a server (for example "invalid dist DEDICATED_SERVER" on Forge), SpawnPoint disables them and tries again. The Mods tab lists what it disabled; players can still use those mods in their own game.
2. **Tuning:** SpawnPoint stops the server, adds helper mods if missing, sizes RAM from the real mod count and sets Paper's chunk worker threads.
3. **Generating spawn:** the server boots with extra borrowed cores and Chunky generates a 750-block radius around spawn. The multiplayer list shows the progress; joining isn't possible yet.
4. **Online:** players can join. Chunky keeps generating out to 5000 blocks on the borrowed cores and pauses automatically if players start to feel it.
5. **Done:** the borrowed cores go back to the pool without a restart.

**Which servers pre-generate:** Paper, Purpur, Fabric, Quilt, Forge and NeoForge use [Chunky](https://modrinth.com/plugin/chunky), which SpawnPoint adds automatically. Vanilla servers, and modpacks Chunky doesn't support, use Minecraft's own `/forceload` instead (Minecraft 1.14 and newer), for the spawn area only: SpawnPoint loads a few batches of chunks at a time and waits until the game has finished them. That work runs on the game's main thread, so it isn't worth the lag further out; the rest of the world generates as people explore, which these servers handle easily. Versions older than 1.14 without Chunky (like many 1.12.2 packs) can't be pre-generated and just open. Either way, generation pauses itself when the slowest ticks get long, **Pause pre-gen** stays paused until you resume it (even across restarts), and a setting can make it wait whenever anyone is online.

Every start, not just the first, borrows spare cores too: loading a big modpack is spread across many threads, so it comes up much faster on 16 threads than on 4–8. The cores go back 90 seconds after the server is up (or when pre-generation finishes, if it's running). The server card shows how long the last start took.

New servers start with flying allowed (`allow-flight=true`), so jetpacks, elytra mods and lag spikes don't get players kicked for "flying". Change it in the Settings tab if you want the anti-fly kick back.

## Bedrock players

In a server's **Settings**, **Let Bedrock players join** adds [Geyser](https://geysermc.org) and Floodgate, so players on phones, consoles and Windows Bedrock can join alongside Java players, without a Java account.

- **Where it works:** Paper and Purpur 1.20.5 and newer. Geyser's Fabric and NeoForge builds only support the newest Minecraft release, so most modpacks can't use it, and there's no Geyser for Forge. SpawnPoint checks and tells you when there's no build for a server.
- **Ports:** Bedrock connects over UDP and doesn't send the address the player typed, so each Bedrock-enabled server gets its own UDP port from `BEDROCK_PORTS` (19132–19141). The Settings card shows it. Players add the server in Bedrock with the same address as Java players and that port. Forward the UDP port on your router (or add a raw UDP resource for it in Pangolin). The compose file already publishes the range.
- Restart the server after turning it on or off.

## Importing an existing server

In **New server → Import existing**, upload a `.zip` or `.tar.gz` of the whole server folder (the one with `server.properties`, the world and `mods`). It's fine if the zip has the folder itself at the top.

- **Detected automatically:** Forge, NeoForge, Fabric, Quilt, Paper, Purpur or vanilla, plus the Minecraft version and the exact loader version, which gets reinstalled as-is. If detection is wrong, open "Type and version" and set them.
- **Kept:** the world, mods, configs, whitelist, ops, bans and `server.properties`. SpawnPoint only sets the port (25565) and its console access (RCON).
- **Then:** the server gets the same treatment as any other: Java, RAM and cores, helper mods, real player IPs, and the automatic fixes. A world that's been played in opens straight away, and background pre-generation only fills in chunks that don't exist yet. A world that was only started once (a fresh server pack) gets its spawn area generated first, like a new server.
- **From another panel or a hosting company:** download or zip the server's folder and upload it.
- Big uploads go faster on your home network (`http://<this-machine>:8090`) than through a proxy or tunnel.

## Real player IPs

When players connect straight to your machine, SpawnPoint sees their real IP. It passes it on to each server that can accept it, and sets this up by itself each time a server starts:

| Server type | How |
|---|---|
| Paper, Purpur | Built in (`proxies.proxy-protocol` in `config/paper-global.yml`) |
| NeoForge 1.21+, Fabric | Adds the **HAProxyCompat** mod |
| Older Fabric/Quilt | Adds **Proxy Protocol Support** |
| Forge 1.20.1 | Adds **NG HAProxy Support** |
| Vanilla, other versions | Not possible: the server sees SpawnPoint's address |

Each server's **Settings** tab shows whether real IPs are on and how, and has a switch to turn them off for that server. These mods only accept headers from SpawnPoint's private network, so nobody else can fake an IP. SpawnPoint's own log also records each join as `player <ip> -> <address>`.

Behind a reverse proxy or tunnel, turn on **Proxy Protocol** there so SpawnPoint still gets the real IP (see [Other setups](#other-setups)). SpawnPoint only accepts that header from addresses in `TRUSTED_PROXIES` (by default, private network ranges), so players connecting directly can't fake one.

## Configuration

Set these in `.env` (or under `environment:` in `docker-compose.yml`).

| Variable | Default | Meaning |
|---|---|---|
| `ADMIN_PASSWORD` | – | Password for the first account, **admin** (required) |
| `BASE_DOMAIN` | `mc.example.com` | Servers get `<name>.<BASE_DOMAIN>` |
| `TZ` | `UTC` | Time zone for schedules and logs, like `America/Chicago` |
| `CF_API_KEY` | – | Enables CurseForge modpack search and installs |
| `AUTO_WAKE` | `true` | Joining a stopped or sleeping server starts it |
| `RAM_POOL_GB` | three quarters of RAM, at most 96 | SpawnPoint won't start a server if Minecraft would use more than this |
| `RESERVED_CORE_GROUPS` | `2` | Core units (2 cores with their threads) left for the rest of the machine, never more than a third of the CPU |
| `BURST_GROUPS` | `3` | Units lent to a server while it starts or pre-generates |
| `MAX_BURST_SERVERS` | `2` | How many servers can borrow at once (others use their own cores) |
| `SPAWN_PREGEN_RADIUS` / `FULL_PREGEN_RADIUS` | `750` / `5000` | Blocks |
| `MSPT_PAUSE` / `MSPT_RESUME` | `40` / `30` | Background pre-generation pauses above / resumes below these tick times |
| `BEDROCK_PORTS` | `19132-19141` | UDP ports handed out to servers with Bedrock players turned on (one each) |
| `TRUSTED_PROXIES` | private ranges | Addresses allowed to send a Proxy Protocol header with a player's real IP (your reverse proxy or tunnel), comma-separated |
| `ISOLATED_DOCKER` | `true` | Private engine inside SpawnPoint (needs privileged mode). `false` = use the host's Docker, see [Docker without privileged mode](#docker-without-privileged-mode) |
| `PIN_SERVER_THREAD` | `true` | Pin each server's main game thread to its own cores |
| `MAP_ENABLED` / `MAP_INTERVAL_MIN` / `MAP_PARALLEL` | `true` / `10` / `2` | World maps on or off, how often a changed world is redrawn at most, and how many servers are drawn at once |
| `DATA_HOST_DIR` | auto | Shared mode only: set it if SpawnPoint can't detect the host path of the servers folder |

**Memory is dynamic.** Each server has three numbers: where it starts, its normal size, and how far it can grow. Java (ZGC) keeps the heap around the normal size and hands unused memory back to the machine when the server is quiet. When the server genuinely needs more (a big build, farms, heavy world generation), the heap grows past the normal size up to the growth limit, with no restart, then shrinks again. Defaults by size (start → normal → can grow to): Paper/vanilla 2 → 6 → 12 GB; modded with fewer than 60 mods 2 → 8 → 16; fewer than 150 mods 3 → 10 → 20; fewer than 300 mods 4 → 12 → 24; 300+ mods 4 → 16 → 32, all capped to fit the RAM pool on smaller machines. You can change them per server in **Settings**. The ring shows how much of the Minecraft RAM pool each server is using right now.

## Not in this version
- **C2ME** isn't added automatically yet because it conflicts with some mods. On a Fabric pack you can add it yourself from the Mods tab.
- **FTB** packs are installed by ID (the number in the pack's URL), without search.

## Development
```bash
pip install -r requirements.txt pytest
pytest -q                     # unit tests, router tests, and a simulated install → pregen → online run
DATA_DIR=/tmp/sp/servers BACKUP_DIR=/tmp/sp/backups ADMIN_PASSWORD=pw MC_PORT=25566 uvicorn app.main:app --port 8090
```
The backup tests need the `restic` binary on your PATH (or `RESTIC_BIN`); they're skipped without it.

---

## Other setups

Everything above assumes one Linux machine with Docker and a port forward. These are the alternatives. SpawnPoint works the same behind all of them: it only needs TCP 25565 to reach it.

### Behind a reverse proxy

Minecraft isn't web traffic, so a normal HTTP proxy host won't carry it. It needs a **raw TCP pass-through** on port 25565 to `<this-machine>:25565`. SpawnPoint picks the server from the address the player typed, so the proxy doesn't need to know about individual servers.

- **Nginx Proxy Manager:** add a **Stream**: incoming port 25565, forward to this machine, port 25565, TCP.
- **nginx** (`stream` module):
  ```nginx
  stream {
    server {
      listen 25565;
      proxy_pass <this-machine>:25565;
      proxy_protocol on;          # passes the player's real IP to SpawnPoint
    }
  }
  ```
- **Traefik:** a TCP router on a `:25565` entry point with ``rule: HostSNI(`*`)``, and a TCP service pointing at `<this-machine>:25565` with `proxyProtocol: {version: 2}`.
- **Caddy:** needs the [layer4](https://github.com/mholt/caddy-l4) plugin for TCP.

Turn on **Proxy Protocol** in the pass-through where you can, so servers see players' real IPs instead of the proxy's. If the proxy isn't on a private network address, add its IP to `TRUSTED_PROXIES`.

The **dashboard** can go through the same proxy as a normal HTTPS site (`spawnpoint.example.com` → `<this-machine>:8090`). SpawnPoint trusts the forwarded client address from proxies on private network addresses, so sign-ins and the activity log show the real IP.

### Pangolin on a VPS

For when port forwarding isn't possible (CGNAT) or you don't want to expose your home IP: players connect to a VPS running [Pangolin](https://github.com/fosrl/pangolin), which tunnels TCP 25565 home over WireGuard.

1. **DNS:** point `*.mc` at the **VPS's** public IP instead of your home IP (grey cloud on Cloudflare).
2. Turn on raw resources in Pangolin's `config/config.yml`:
   ```yaml
   flags:
     allow_raw_resources: true
   ```
3. Add a Traefik entry point in `config/traefik/traefik_config.yml`:
   ```yaml
   entryPoints:
     tcp-25565:
       address: ":25565/tcp"
   ```
4. Publish the port on the **gerbil** service in Pangolin's `docker-compose.yml`:
   ```yaml
       ports:
         - 25565:25565
   ```
5. Open TCP 25565 in the VPS firewall (for example `ufw allow 25565/tcp`) and in your provider's firewall panel, if it has one. Then `docker compose up -d` in the Pangolin folder.
6. In the Pangolin dashboard, go to **Resources → Add → Raw TCP/UDP**, choose **TCP, port 25565**, pick your home site, and set the target to **`<this machine's LAN IP>:25565`**. With a Newt site, the LAN IP works. With a basic WireGuard site, use the IP this machine is reachable at through the tunnel.
7. On that resource's **TCP** tab, turn on **Enable Proxy Protocol** and save. This passes each player's real IP to SpawnPoint (version 1 and 2 both work). Players on your LAN who connect directly still work without it.

Optional: add an **HTTPS resource** for the dashboard, for example `spawnpoint.example.com` → `<this machine>:8090`, with Pangolin authentication turned on. You don't need the router port forward from the quick start with this setup.

### Unraid

SpawnPoint ships an Unraid template, so it installs like any other app, and Unraid's Docker tab only ever shows **spawnpoint**.

**a. Add the template and icon.** Both are inside the image. Run this once in the Unraid terminal:
```bash
IMG=ghcr.io/z354prance/spawnpoint:latest; D=/boot/config/plugins/dockerMan
if [ -e $D/templates-user/my-spawnpoint.xml ]; then echo "Already installed: not overwriting your saved SpawnPoint settings."; else
docker pull -q $IMG >/dev/null && mkdir -p $D/templates-user $D/icons && \
docker run --rm --entrypoint cat $IMG /opt/spawnpoint/unraid/my-spawnpoint.xml > $D/templates-user/my-spawnpoint.xml && \
docker run --rm --entrypoint cat $IMG /opt/spawnpoint/app/static/brand/spawnpoint-icon-512.png > $D/icons/spawnpoint.png && \
echo "SpawnPoint template and icon installed"; fi
```
Unraid saves your filled-in settings in that same file, so the command won't overwrite it once it exists. New versions show up as an update in the Docker tab, and updating keeps your settings.

**Refreshing the icon** after it changes (Unraid keeps its own copy of each container icon):
```bash
IMG=ghcr.io/z354prance/spawnpoint:latest
docker run --rm --entrypoint cat $IMG /opt/spawnpoint/app/static/brand/spawnpoint-icon-512.png > /boot/config/plugins/dockerMan/icons/spawnpoint.png
rm -f /var/lib/docker/unraid/images/spawnpoint-icon.png /usr/local/emhttp/state/plugins/dynamix.docker.manager/images/spawnpoint-icon.png
```
Then reload the Docker tab (Ctrl+F5 if the browser still shows the old one).

**b. Create the container** from **Docker → Add Container → Template → spawnpoint**:
- **Privileged** is already on. SpawnPoint needs it to run its own private Docker engine.
- **Server data** and **Engine storage** default to `/mnt/cache/appdata/spawnpoint/servers` and `/mnt/cache/appdata/spawnpoint/engine`, directly on the cache pool. Keep the `/mnt/cache/…` form, **not** `/mnt/user/…`: the `/mnt/user` path goes through Unraid's share layer (FUSE), which is slower for game worlds, and the private engine falls back to a very slow storage mode on it (SpawnPoint warns about it).
- Make sure the **appdata** share stays on the cache: in **Shares → appdata**, set Primary storage to **Cache** and Secondary storage to **None**, so the mover never moves worlds or the engine onto the array.
- **Backups** can go on the array (for example `/mnt/user/backups/minecraft`).
- Set an **Admin password**, your **Base domain** and optionally a **CurseForge API key**. The pre-gen, core and wake settings are under **Show more settings**.
- Turn on **Autostart**, apply, and open the **WebUI** from the Docker tab.

The template sets a 3-minute stop timeout (`--stop-timeout=180`) so big modpacks have time to save. If Unraid's own Docker stop timeout (Settings → Docker) is shorter, raise it to match. Anything else already using port 25565 on Unraid has to move to another port.

**Host tuning for big CPUs (for example an EPYC 7F72):** set the CPU scaling governor to **Performance** with the Tips and Tweaks plugin; in the BIOS, set Determinism to Performance and leave cTDP at its maximum if your cooling allows (NPS1 is fine on a single socket). On a 7F72, `RESERVED_CORE_GROUPS=2` keeps 4 cores / 8 threads for Unraid and other apps.

### Docker without privileged mode

Rootless Docker, Podman and unprivileged LXC containers (for example on Proxmox) usually can't run a second Docker engine inside a container. In that case SpawnPoint can drive the host's Docker directly ("shared mode"):

```yaml
    privileged: false
    pid: host
    cap_add: [SYS_NICE]
    environment:
      ISOLATED_DOCKER: "false"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./data/servers:/servers
      - ./data/backups:/backups
```

The Minecraft servers then show up as `mc-*` containers on the host. If SpawnPoint can't work out where the servers folder is on the host, set `DATA_HOST_DIR` to its full host path. Giving a container the Docker socket gives it full control of the host's Docker, so only do this on a machine you trust SpawnPoint with.
