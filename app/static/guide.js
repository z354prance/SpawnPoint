// SpawnPoint's in-app user guide. Each section: id, title, who can see it ("viewer" = everyone,
// "moderator", "admin"), and its body (HTML). Keep it in plain words, from the player's side.
window.GUIDE = [
  { id: "start", title: "Getting around", role: "viewer", body: `
    <p>SpawnPoint runs your Minecraft servers. Everything is on one page:</p>
    <ul>
      <li><b>Left:</b> your servers. Each card shows its address, who's playing and how smooth it's running (tick time: under 50 ms is smooth). Click one to open it. Click the address icon to copy it.</li>
      <li><b>The ring</b> at the bottom left is the whole machine at a glance: memory, the Minecraft memory pool, disk and CPU. Hover over a part for its numbers.</li>
      <li><b>Middle:</b> the open server: its controls, facts, world map, and how it's doing.</li>
      <li><b>Right:</b> tools for that server: Console, Mods, Players, Settings, Files, Backups, Schedule and Stats.</li>
    </ul>
    <p>Click the SpawnPoint logo to get back to the overview, which shows everyone playing on every server.</p>` },

  { id: "join", title: "Joining a server", role: "viewer", body: `
    <p>In Minecraft, go to <b>Multiplayer → Add Server</b> and use the server's address from its card, like <span class="mono">survival.mc.example.com</span>. No port needed: every server shares the same one, and SpawnPoint sends you to the right one by its name.</p>
    <ul>
      <li><b>Asleep?</b> Servers go to sleep after a while with nobody on. The server list shows "asleep: join to wake it". Join once, and it starts. Give it a minute (big modpacks take longer), then join again.</li>
      <li><b>Getting ready?</b> While a new server is setting up or generating its spawn area, the server list shows the progress, and joining waits until it's done.</li>
      <li><b>On a phone, console or Windows Bedrock?</b> Only servers with Bedrock players turned on (in Settings) can be joined from Bedrock. Use the same address with the port shown there.</li>
    </ul>` },

  { id: "create", title: "Making a server", role: "moderator", body: `
    <p>Click <b>New server</b> (under the server list). There are three ways:</p>
    <ul>
      <li><b>Modpack:</b> search Modrinth or CurseForge, or enter an FTB pack number. Java, memory and CPU are chosen for you.</li>
      <li><b>Plain server:</b> Paper (fast, plugins), Fabric, NeoForge, Forge, Purpur or vanilla, on the Minecraft version you pick.</li>
      <li><b>Import:</b> upload a .zip of an existing server folder (world, mods and all). SpawnPoint works out what it is and keeps everything.</li>
    </ul>
    <p>You can give a new world a <b>seed</b>. Leave it empty for a random one.</p>
    <p>A new server goes through a few steps, shown on its card: downloading, installing the loader, adding helper mods, then <b>generating the area around spawn</b> before it opens. Big modpacks can take 10–20 minutes the first time.</p>` },

  { id: "controls", title: "Starting, stopping and the server page", role: "viewer", body: `
    <ul>
      <li><b>Restart</b> and <b>Stop</b> save the world first. <b>Force stop</b> (the red power button) ends it immediately without saving: only for a server that's stuck.</li>
      <li><b>The facts</b> show the version, modpack, mods, how long it's been up and how long the last start took, CPU, world size, last backup and whether it's invite-only.</li>
      <li><b>Tick time</b> is how long the server takes per game tick. Under 50 ms is smooth; above that, players feel lag. The Stats tab shows the slowest ticks too, which is what rubberbanding comes from.</li>
      <li><b>If a server crashes</b>, SpawnPoint starts it again by itself and notes why in the activity log. If it crashes 3 times in 15 minutes it stops trying, so check the console.</li>
    </ul>` },

  { id: "map", title: "The world map", role: "viewer", body: `
    <p>Each server's world is drawn from above, with live player positions and faces, and a house at the world spawn.</p>
    <ul>
      <li><b>Move:</b> drag. <b>Zoom:</b> scroll, or plus and minus. <b>Back to spawn:</b> double-click. The arrow keys work too.</li>
      <li><b>Dimensions:</b> the Overworld, Nether and End each have their own map, once someone has been there.</li>
      <li><b>Generation</b> lays the pre-generation progress over the terrain: the spawn area, land generated so far, the edge being generated now, and how far it'll go.</li>
      <li><b>Full screen:</b> the corner button. Esc closes it.</li>
      <li>The map updates by itself every few minutes when the world changes. <b>Update map</b> redraws the whole map from scratch, straight away.</li>
      <li><b>Sharing:</b> a moderator can turn on a <b>public map</b> link in Settings, so anyone can look at the map without an account.</li>
      <li><b>Markers:</b> moderators press <b>Add marker</b>, then click the map to name a base, farm or portal. Click a marker to rename, recolour or remove it. Markers show on the public map too, unless you untick that for one.</li>
      <li><b>Activity</b> shades where players spend their time, from where they've been over the past days and weeks.</li>
    </ul>` },

  { id: "pregen", title: "Pre-generation", role: "viewer", body: `
    <p>Generating new land is the heaviest thing a Minecraft server does, and it's what causes lag when players explore. SpawnPoint generates the world ahead of time instead:</p>
    <ul>
      <li>First the <b>area around spawn</b>, before the server opens, using extra CPU cores.</li>
      <li>Then the <b>wider world</b> (out to 2,500 blocks by default) in the background while people play. It pauses by itself whenever the server starts to lag, and carries on when it's smooth again.</li>
      <li><b>Pause pre-gen</b> on the map card stops it until you resume it. In Settings you can also make it wait whenever anyone is online.</li>
      <li>Most servers use the Chunky mod for this. Vanilla servers (and modpacks without Chunky) use Minecraft's own method for the spawn area only; further out, the world generates as people explore. Versions older than 1.14 without Chunky can't pre-generate.</li>
    </ul>` },

  { id: "players", title: "Players, invite-only and the console", role: "moderator", body: `
    <ul>
      <li><b>Players tab:</b> see who's on, kick someone, make someone an operator, ban someone, and turn on <b>invite only</b> (a whitelist): then only the players you add can join.</li>
      <li><b>Shared invite list</b> (optional): one list of trusted players that any server can use. Turn it on per server in its Players tab, and untick anyone who shouldn't be on that particular server. <b>Edit shared list</b> adds people, or imports a server's whitelist in one go. Servers keep their own whitelist entries too: the shared list only adds and removes the names it put there.</li>
      <li><b>Console tab:</b> the server's live log, and a box to run commands (without the slash), like <span class="mono">say hello</span> or <span class="mono">time set day</span>.</li>
      <li><b>Everyone who has played</b> (bottom of the Players tab): playtime, number of visits, first and last seen, and where each player usually is. Click the coordinates to see the spot on the map.</li>
      <li><b>Hunting lag:</b> run <span class="mono">spark profiler start</span>, play for a couple of minutes, then <span class="mono">spark profiler stop</span>. It gives you a link to a report that shows exactly what's slow.</li>
    </ul>` },

  { id: "mods", title: "Mods and modpack updates", role: "moderator", body: `
    <ul>
      <li><b>Mods tab:</b> search and add single mods or plugins. The search only shows ones that fit the server's loader and version, and warns you when players need the mod too.</li>
      <li>SpawnPoint adds a few <b>helper mods</b> by itself (for speed and pre-generation), and turns off <b>client-only mods</b> that would crash a server. Players can still use those in their own game.</li>
      <li><b>Mod updates</b> (servers without a modpack): the Mods tab checks once a day which mods and plugins have newer builds for your Minecraft version, and updates one or all of them. <b>Undo that update</b> puts the previous files back. Files that didn't come from Modrinth can't be checked.</li>
      <li><b>Modpack updates:</b> a modpack stays on its version until you update it, so restarts never change it. When there's a new version, <b>Settings → Modpack</b> shows <b>Update</b>. Updating backs up first, then restarts. If something breaks, <b>Roll back</b> puts the world and mods back to just before the update.</li>
    </ul>` },

  { id: "settings", title: "Settings", role: "moderator", body: `
    <ul>
      <li><b>Sleep</b> after a number of empty minutes, to free memory. Joining wakes it.</li>
      <li><b>Restart after a crash</b>: on by default.</li>
      <li><b>Minecraft version</b> (servers without a modpack): pick a newer version and press <b>Check</b> to see which mods have a build for it. <b>Upgrade</b> backs up, then starts a copy of the server on the new version while the real one keeps running. Only if the copy starts cleanly is the server switched over; if it doesn't, nothing changes and you're told why. <b>Roll back</b> returns to the old version and world.</li>
      <li><b>Player pack</b> (modded servers): a download page for your players. The Prism Launcher instance updates itself every time they play; there's also a Modrinth App file and a zip for any launcher. Turn on AutoModpack and all three stay in sync when players join (the zip then only holds AutoModpack, which downloads the rest). Enter the address SpawnPoint is reachable at from the internet, then share the link. Mods are their authors' work: keep it to your own players.</li>
      <li><b>Discord chat:</b> connect the server's chat to a Discord channel. A channel webhook sends chat, joins, deaths and advancements to Discord. To show Discord messages in game too, an admin adds a Discord bot once (the card explains how) and you paste the channel's ID.</li>
      <li><b>Memory:</b> sized automatically from the number of mods. The server starts small, stays around its normal size, grows when it needs more and hands memory back when it's quiet.</li>
      <li><b>Public map</b> and <b>Bedrock players</b>: see their own sections.</li>
      <li><b>server.properties</b>: difficulty, game mode, view distance and everything else Minecraft lets you set. Most changes need a restart.</li>
    </ul>` },

  { id: "backups", title: "Backups and restoring", role: "viewer", body: `
    <ul>
      <li><b>Back up now</b> is safe while the server runs. Each backup only stores what changed since the last one, so they're quick and small, and every one is still a complete copy.</li>
      <li><b>Automatic backups:</b> add a <b>Back up</b> task in the Schedule tab, for example every 6 hours.</li>
      <li><b>Keep:</b> old backups are thinned out by themselves: the most recent few, then one per hour, day, week and month.</li>
      <li><b>Restore:</b> stop the server, then Restore. The files from before the restore are kept in a <span class="mono">.pre-restore</span> folder, in case you change your mind.</li>
      <li><b>Download</b> any backup as a .zip (moderators and admins).</li>
    </ul>` },

  { id: "schedule", title: "Scheduled tasks", role: "moderator", body: `
    <p>The <b>Schedule</b> tab runs things by themselves, daily at a set time (on the days you pick) or every few hours:</p>
    <ul>
      <li><b>Back up</b>, <b>Restart</b>, <b>Stop</b>, <b>Start</b>, or <b>run a console command</b>.</li>
      <li>Restarts and stops can <b>warn players</b> with a countdown in chat, up to 15 minutes before. The restart itself happens at the set time, and it can back up first.</li>
      <li>Switch a task off with its checkbox without deleting it, or press <b>Run now</b> to try it.</li>
    </ul>` },

  { id: "bedrock", title: "Bedrock players", role: "viewer", body: `
    <p>Servers can let players on phones, consoles and Windows Bedrock join, through Geyser. A moderator turns it on in <b>Settings → Bedrock players</b>, which shows the port to use.</p>
    <p>It only works where Geyser does: Paper and Purpur 1.20.5 and newer, and Fabric or NeoForge on the newest Minecraft release. Most modpacks can't use it.</p>` },

  { id: "trouble", title: "When something goes wrong", role: "viewer", body: `
    <ul>
      <li><b>It crashed or didn't start:</b> the Console tab shows what went wrong in plain words above the log, with the mod it points at, a way to turn that mod off and a link to the full crash report. Alerts and the activity log say the same. The most common causes are a mod that needs another mod, or one that doesn't match the Minecraft version.</li>
      <li><b>Lag or rubberbanding:</b> check the Stats tab. If the slowest ticks are over 50 ms, run a spark report (see Players and the console) to find the cause.</li>
      <li><b>The disk is nearly full:</b> a banner says so, and backups, pre-generation and map drawing pause until there's room. Delete old backups or worlds you don't need.</li>
      <li><b>Can't join at all:</b> check the address, that the server is online, and, for friends outside your home, that the address points to your connection.</li>
    </ul>` },

  { id: "admin", title: "Users, roles and alerts", role: "admin", body: `
    <ul>
      <li><b>Users</b> (in the middle of the ring): add people and give them a role. <b>Admins</b> can do everything. <b>Moderators</b> run servers and can make new ones, but can't manage users, see the activity log, or delete servers and backups. <b>Viewers</b> can look but not change anything.</li>
      <li><b>Activity log:</b> every sign-in (including failed ones) and every change, with who did it. Filter by person, type or server.</li>
      <li><b>Alerts</b> (at the bottom of the activity log): paste a Discord webhook to hear about crashes, failed backups, low disk space and modpack updates in a channel.</li>
      <li><b>Your own password:</b> click your name at the bottom left.</li>
    </ul>` },
];
