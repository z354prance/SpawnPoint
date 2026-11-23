// SpawnPoint UI: plain JS, no build step.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const app = $("#app");
let ME = null, INFO = null, LIST = [], VIEW = "server", SEL = null, TAB = "console";
let railKey = "", es = null, timers = [], HIST = {}, BK = {}, LISTS = {}, mapObs = null;

const RANK = { viewer: 0, moderator: 1, admin: 2 };
const can = (role) => !!ME && RANK[ME.role] >= RANK[role];

async function api(path, opts = {}) {
  const o = { headers: { "Content-Type": "application/json" }, ...opts };
  if (o.body && typeof o.body !== "string") o.body = JSON.stringify(o.body);
  const r = await fetch("/api" + path, o);
  if (r.status === 401 && path !== "/login") { showLogin(); throw new Error("Not signed in"); }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(data?.detail || data || r.statusText);
  return data;
}
function toast(msg, err = false) {
  // Errors stay long enough to read (or copy); click any toast to close it.
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  t.title = "Click to close"; t.onclick = () => t.classList.add("hidden");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), err ? 15000 : 4500);
}
async function act(fn, okMsg) { try { const r = await fn(); if (okMsg) toast(okMsg); return r; } catch (e) { toast(e.message, true); } }
function modal(html) { $("#modal-card").innerHTML = html; $("#modal").classList.remove("hidden"); }
function closeModal() { $("#modal").classList.add("hidden"); if (typeof drawUpload === "function") drawUpload(); }
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
function copy(text) { navigator.clipboard?.writeText(text); toast("Copied " + text); }
function stopAll() { timers.forEach(clearInterval); timers = []; closeConsole(); mapObs?.disconnect(); mapObs = null; }
function closeConsole() { if (es) { es.close(); es = null; } }
const slug = (t) => String(t || "").toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");
const ago = (ts) => {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now"; if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`; return `${Math.floor(s / 86400)} days ago`;
};
const dur = (s) => {
  s = Math.max(0, Math.floor(s));
  if (s < 60) return "under a minute"; const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h >= 48) return `${Math.floor(h / 24)} days`; return h ? `${h} h ${m} m` : `${m} m`;
};

const ICON = {
  book: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5z"/><path d="M4 20.5A2.5 2.5 0 0 0 6.5 23H20v-5"/><path d="M8 7h8M8 11h6"/></svg>',
  restart: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/></svg>',
  stop: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  power: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M12 3v9"/><path d="M6.3 6.3a8 8 0 1 0 11.4 0"/></svg>',
  play: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>',
  plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>',
  link: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12M7 10l5 5 5-5M5 21h14"/></svg>',
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
};
const LOGO = '<img src="/static/brand/spawnpoint-icon.svg" alt="">';
const WORD = 'Spawn<span>Point</span>';

const PHASE = {
  queued: "Queued", importing: "Importing", installing: "Installing", preparing: "Tuning", "pregen-spawn": "Generating spawn",
  starting: "Starting", stopping: "Stopping", online: "Online", stopped: "Stopped", sleeping: "Asleep", error: "Error",
};
const BUSY = ["queued", "importing", "installing", "preparing", "pregen-spawn", "starting", "stopping"];
const running = (s) => !["stopped", "sleeping", "error"].includes(s.phase);
const KILL_WARNING = "Force stop ends the server immediately without saving. Anything since the last autosave (usually under 5 minutes) is lost. Continue?";
// The four setup steps fill crimson to glowstone, like a spawner heating up.
const FLAME = ["#B8324A", "#E0603F", "#FF9D4A", "#FFD36B"];
const stepColor = (i, n) => FLAME[Math.min(3, Math.round((i / Math.max(1, n - 1)) * 3))];
const LOADER = { paper: "Paper", purpur: "Purpur", fabric: "Fabric", quilt: "Quilt", forge: "Forge", neoforge: "NeoForge", vanilla: "Vanilla" };
const versionLabel = (s) => `${LOADER[s.loader] || LOADER[s.kind] || s.kind || ""} ${s.mc_version || ""}`.trim();
const packLabel = (s) => s.pack ? `${s.pack.title || "Modpack"}${s.pack.version_name ? " " + s.pack.version_name : ""}${s.pack_update ? ", update available" : ""}` : "None";

function badge(s) {
  if (s.progress) return `<span class="badge busy">Step ${s.progress.step} of ${s.progress.of}</span>`;
  const cls = s.phase === "online" ? "on" : s.phase === "error" ? "bad" : BUSY.includes(s.phase) ? "busy" : "";
  return `<span class="badge ${cls}">${esc(PHASE[s.phase] || s.phase)}</span>`;
}
function bar4(g, slim = false) {
  const known = g.percent != null;
  return `<div class="bar4 ${slim ? "slim" : ""}" role="progressbar" aria-label="Setup step ${g.step} of ${g.of}" aria-valuemin="0" aria-valuemax="${g.of}" aria-valuenow="${g.step - 1 + (g.percent || 0) / 100}">${
    Array.from({ length: g.of }, (_, i) => {
      const n = i + 1, w = n < g.step ? 100 : n === g.step ? (known ? g.percent : 100) : 0;
      return `<span><i class="${n === g.step ? "live fast" : ""}" style="width:${w.toFixed(1)}%;background:${stepColor(i, g.of)};${n === g.step && !known ? "opacity:.6" : ""}"></i></span>`;
    }).join("")}</div>`;
}
const pregenActive = (s) => s.phase === "online" && s.pregen && s.pregen.stage === "background";

// ---------------------------------------------------------------- login and account
function showLogin() {
  stopAll(); ME = null;
  app.innerHTML = `<div class="login card"><img src="/static/brand/spawnpoint-logo-dark-bg.svg" alt="SpawnPoint">
    <form id="lf"><label for="user">Username</label><input id="user" autocomplete="username" value="${esc(localStorage.getItem("sp_user") || "")}">
    <label for="pw">Password</label><input type="password" id="pw" autocomplete="current-password">
    <button class="primary" style="margin-top:16px;width:100%;height:42px">Sign in</button></form></div>`;
  (($("#user").value ? $("#pw") : $("#user"))).focus();
  $("#lf").onsubmit = async (e) => {
    e.preventDefault();
    const username = $("#user").value.trim() || "admin";
    const ok = await act(() => api("/login", { method: "POST", body: { username, password: $("#pw").value } }));
    if (ok) { try { localStorage.setItem("sp_user", username); } catch {} boot(); }
  };
}
function openAccount() {
  modal(`<div class="row"><h2 style="flex:1">${esc(ME.name)}</h2><button class="ghost" onclick="closeModal()">Close</button></div>
    <p class="muted">Role: ${esc(ME.role)}. Changing your password signs you out everywhere else.</p>
    <form id="pf"><label for="pc">Current password</label><input type="password" id="pc" autocomplete="current-password">
    <label for="pn1">New password (8 characters or more)</label><input type="password" id="pn1" autocomplete="new-password">
    <label for="pn2">New password again</label><input type="password" id="pn2" autocomplete="new-password">
    <div class="row" style="justify-content:flex-end;margin-top:16px"><button class="primary">Change password</button></div></form>`);
  $("#pf").onsubmit = async (e) => {
    e.preventDefault();
    if ($("#pn1").value !== $("#pn2").value) return toast("The new passwords don't match", true);
    const r = await act(() => api("/me/password", { method: "POST", body: { current: $("#pc").value, new: $("#pn1").value } }), "Password changed");
    if (r) { closeModal(); ME = await api("/me"); route(); }
  };
}

// ---------------------------------------------------------------- shell
async function boot() {
  stopAll();
  try { ME = await api("/me"); } catch { return; }
  app.innerHTML = `<div class="layout" id="layout">
    <aside class="side" aria-label="Servers">
      <a class="brand" href="#/" title="Overview" aria-label="SpawnPoint overview">${LOGO}<b>${WORD}</b></a>
      <div class="grp"><span>Your servers</span><span id="runcount"></span></div>
      <nav class="srvlist" id="srvlist" aria-label="Server list"></nav>
      ${can("moderator") ? `<button class="newb" id="newbtn">${ICON.plus}New server</button>` : ""}
      <div class="ringwrap"><div class="ring" id="ring"></div></div>
      <div class="me"><button class="who" id="acct" title="Change your password">${esc(ME.name)}<small>${esc(ME.role[0].toUpperCase() + ME.role.slice(1))}</small></button>
        <a class="btn ghost small ic" href="#/guide" id="guideb" aria-label="User guide" title="User guide">${ICON.book}</a>
        <button class="ghost small" id="logout">Sign out</button></div>
    </aside>
    <main class="center" id="center"></main>
    <aside class="rail" id="rail" aria-label="Server tools"></aside></div>`;
  $("#newbtn") && ($("#newbtn").onclick = openCreate);
  $("#acct").onclick = openAccount;
  $("#logout").onclick = async () => { await api("/logout", { method: "POST" }); showLogin(); };
  railKey = "";
  await Promise.all([refreshList(), refreshInfo()]);
  timers.push(setInterval(refreshList, 5000), setInterval(refreshInfo, 10000), setInterval(refreshSlow, 30000), setInterval(pollPlayers, 3000));
  route();
}

async function refreshList() {
  const before = Object.fromEntries(LIST.map((x) => [x.id, x.map?.rendered]));
  try { LIST = await api("/servers"); } catch { return; }
  for (const x of LIST) if (x.id === SEL && x.map?.rendered && x.map.rendered !== before[x.id]) loadMapIndex(x.id);
  drawSidebar();
  if (VIEW === "server" && SEL) {
    if (!LIST.find((s) => s.id === SEL)) { location.hash = "#/"; return; }
    drawServer();
  } else if (VIEW === "home") drawHome();
  else if (VIEW === "empty" && LIST.length) route();
}
async function refreshInfo() { try { INFO = await api("/info"); } catch { return; } drawRing(); if (VIEW === "home") drawHomeStats(); }
async function refreshSlow() {
  if (VIEW !== "server" || !SEL) return;
  const sid = SEL;
  const [h, b, l] = await Promise.all([api(`/servers/${sid}/history`).catch(() => null), api(`/servers/${sid}/backups`).catch(() => null),
    api(`/servers/${sid}/lists`).catch(() => null)]);
  if (h) HIST[sid] = h; if (b) BK[sid] = b.backups || b; if (l) LISTS[sid] = l;
  loadMapIndex(sid);
  if (SEL === sid) drawServer();
}

function route() {
  if (!ME) return;
  closeModal();
  const h = location.hash;
  const m = h.match(/^#\/s\/([^/]+)(?:\/(\w+))?/);
  if (/^#\/users/.test(h) && can("admin")) VIEW = "users";
  else if (/^#\/activity/.test(h) && can("admin")) VIEW = "activity";
  else if (/^#\/guide/.test(h)) VIEW = "guide";
  else if (m) { VIEW = "server"; const changed = SEL !== m[1]; SEL = m[1]; TAB = m[2] || TAB || "console"; if (changed) refreshSlow(); }
  else VIEW = LIST.length ? "home" : "empty";
  $("#layout").classList.toggle("wide", VIEW !== "server");
  if (VIEW !== "server") { closeConsole(); railKey = ""; $("#rail").innerHTML = ""; document.body.classList.remove("mapfull"); }
  drawSidebar(); drawRing();
  if (VIEW === "server") { drawServer(); drawRail(); }
  else if (VIEW === "users") showUsers();
  else if (VIEW === "activity") showActivity();
  else if (VIEW === "guide") showGuide(h.split("/")[2]);
  else if (VIEW === "home") showHome();
  else drawEmpty();
}
window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- sidebar
function drawSidebar() {
  const el = $("#srvlist"); if (!el) return;
  $("#runcount").textContent = LIST.length ? `${LIST.filter(running).length} of ${LIST.length} running` : "";
  el.innerHTML = LIST.map((s) => {
    const st = s.stats || {};
    let extra = "";
    if (s.progress) extra = `<div style="margin-top:8px">${bar4(s.progress, true)}</div>`;
    else if (s.phase === "online") {
      extra = `<div class="line"><span><b>${st.online ?? 0}</b>/${st.max ?? "–"} playing</span><span class="sp"></span>${st.mspt != null ? `<span><b>${st.mspt.toFixed(1)}</b> ms</span>` : ""}</div>`;
      if (pregenActive(s)) extra += `<div class="line" style="margin-top:8px;font-size:12px"><span>World pre-gen${s.pregen.paused ? " (paused)" : ""}</span><span class="sp"></span><span>${(s.pregen.percent || 0).toFixed(0)}% of ${Number(s.pregen.full_radius).toLocaleString()}</span></div>
        <div class="bar slim"><i class="${s.pregen.paused ? "" : "live"}" style="width:${(s.pregen.percent || 0).toFixed(1)}%"></i></div>`;
    }
    return `<a href="#/s/${esc(s.id)}" class="srv ${VIEW === "server" && s.id === SEL ? "on" : ""}" ${VIEW === "server" && s.id === SEL ? 'aria-current="page"' : ""}>
      <div class="top"><span class="nm" title="${esc(s.name)}">${esc(s.name)}</span>${badge(s)}</div>
      <div class="addr"><span>${esc(s.hostname)}</span><button class="cp" data-copy="${esc(s.hostname)}" aria-label="Copy ${esc(s.hostname)}" title="Copy address">${ICON.copy}</button></div>
      ${extra}</a>`;
  }).join("") || `<div class="sub" style="padding:8px">No servers yet.</div>`;
  $$("[data-copy]", el).forEach((b) => (b.onclick = (e) => { e.preventDefault(); e.stopPropagation(); copy(b.dataset.copy); }));
}

// The ring: memory (top left), Minecraft pool (top right), disk (bottom right), CPU core groups
// (bottom left), players around the inside. Hover or tap a part for its numbers.
function drawRing() {
  const el = $("#ring"); if (!el || !INFO) return;
  const C = 102, R = 78, W = 15, GAP = 14, SPAN = 90 - GAP;
  const pt = (a, r) => [C + r * Math.sin(a * Math.PI / 180), C - r * Math.cos(a * Math.PI / 180)];
  const arc = (a0, a1, r = R) => { const [x0, y0] = pt(a0, r), [x1, y1] = pt(a1, r); return `M${x0.toFixed(1)} ${y0.toFixed(1)} A${r} ${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1.toFixed(1)} ${y1.toFixed(1)}`; };
  const q0 = (i) => -90 + i * 90 + GAP / 2;
  const seg = (a0, a1, col, tip, w = W) => a1 - a0 < 0.4 ? "" : `<path d="${arc(a0, a1)}" stroke="${col}" stroke-width="${w}" fill="none" data-tip="${esc(tip)}"><title>${esc(tip)}</title></path>`;
  const stack = (i, parts, track, tip) => {
    let a = q0(i), out = seg(a, a + SPAN, track, tip);
    for (const [f, col, t] of parts) { const b = a + Math.max(0, Math.min(1, f)) * SPAN; if (b > a) out += seg(a + 0.5, Math.min(b, q0(i) + SPAN) - 0.5, col, t); a = b; }
    return out;
  };
  const ticks = (i) => [0, 1, 2, 3, 4].map((k) => { const a = q0(i) + (k * SPAN) / 4, [x0, y0] = pt(a, R + W / 2 + 3), [x1, y1] = pt(a, R + W / 2 + (k % 4 ? 5 : 7));
    return `<line x1="${x0.toFixed(1)}" y1="${y0.toFixed(1)}" x2="${x1.toFixed(1)}" y2="${y1.toFixed(1)}" stroke="#3F4652" stroke-width="1.5"/>`; }).join("");
  const track = "#2A2F38";
  const mem = INFO.host?.memory || {}, disk = INFO.host?.disk || {};
  const T = mem.total_gb || 0, A = mem.available_gb || 0, mc = INFO.ram_pool_used_gb || 0, other = Math.max(0, T - A - mc);
  let svg = "";
  svg += T ? stack(0, [[mc / T, "#C2384F", `Minecraft servers: ${mc} GB`], [other / T, "#6E2A36", `Everything else: ${other.toFixed(1)} GB`]], track, `Host memory: ${A} of ${T} GB free`)
           : seg(q0(0), q0(0) + SPAN, track, "Host memory: not available");
  const POOL = ["#FF9D4A", "#FFB878", "#FFD9B0", "#E0603F", "#FFC870"];
  svg += stack(1, (INFO.pool || []).filter((p) => p.mem_gb > 0).map((p, i) => [p.mem_gb / INFO.ram_pool_gb, POOL[i % POOL.length], `${p.name}: ${p.mem_gb} GB`]),
    track, `Minecraft RAM pool: ${mc} of ${INFO.ram_pool_gb} GB in use`);
  if (disk.total_tb) {
    const mcF = disk.minecraft_gb != null ? disk.minecraft_gb / 1024 / disk.total_tb : 0, usedF = disk.used_tb / disk.total_tb;
    svg += stack(2, [[mcF, "#FFD36B", `Worlds and backups: ${disk.minecraft_gb ?? "?"} GB`], [Math.max(0, usedF - mcF), "#8A6A2E", `Everything else on this drive: ${((disk.used_tb - (disk.minecraft_gb || 0) / 1024) * 1024).toFixed(0)} GB`]],
      track, `Disk: ${disk.free_tb} of ${disk.total_tb} TB free`);
  } else svg += seg(q0(2), q0(2) + SPAN, track, "Disk: not available");
  const groups = INFO.topology?.groups || [];
  if (groups.length) {
    const st = SPAN / groups.length;
    groups.forEach((g, k) => {
      const lent = g.burst.map((b) => b.startsWith("map:") ? `rendering the map for ${b.slice(4)}` : `pre-generating ${b}`).join(", ");
      const [col, what] = g.reserved ? ["#5B6472", "kept free for the rest of the machine"] : g.burst.length ? ["#FFD36B", "lent out: " + lent]
        : g.home.length ? ["#FF9D4A", "running " + g.home.join(", ")] : [track, "free"];
      const a = q0(3) + k * st; svg += seg(a + 1.1, a + st - 1.1, col, `Core group ${k + 1} (CPUs ${g.cpus.join(",")}): ${what}`);
    });
  }
  svg += [0, 1, 2].map(ticks).join("");
  const pl = INFO.players || { online: 0, slots: 0 }, pf = pl.slots ? pl.online / pl.slots : 0;
  const [ex, ey] = pt(Math.max(0.01, Math.min(0.999, pf)) * 360, 56);
  svg += `<g data-tip="Players online: ${pl.online} of ${pl.slots} slots"><title>Players online: ${pl.online} of ${pl.slots} slots</title>
    <circle cx="${C}" cy="${C}" r="56" fill="none" stroke="${track}" stroke-width="4"/>
    ${pl.online ? `<path d="M${C} ${C - 56} A56 56 0 ${pf > .5 ? 1 : 0} 1 ${ex.toFixed(1)} ${ey.toFixed(1)}" stroke="#E9ECF1" stroke-width="4" fill="none" stroke-linecap="round"/>` : ""}</g>`;
  // Curved labels between the outer segments and the players ring. The bottom two run
  // the other way round so they read upright.
  const LR = 64, lab = (i, text) => {
    const a0 = q0(i) + 4, a1 = q0(i) + SPAN - 4, top = i < 2;
    const [x0, y0] = pt(top ? a0 : a1, LR), [x1, y1] = pt(top ? a1 : a0, LR);
    return `<path id="rl${i}" d="M${x0.toFixed(1)} ${y0.toFixed(1)} A${LR} ${LR} 0 0 ${top ? 1 : 0} ${x1.toFixed(1)} ${y1.toFixed(1)}" fill="none"/>
      <text class="rlab"><textPath href="#rl${i}" startOffset="50%" text-anchor="middle" dominant-baseline="central">${text}</textPath></text>`;
  };
  svg += `<g aria-hidden="true">${lab(0, "Memory")}${lab(1, "Minecraft")}${lab(2, "Disk")}${lab(3, "CPU")}</g>`;
  const label = `Host usage. Memory ${A} of ${T} GB free. Minecraft pool ${mc} of ${INFO.ram_pool_gb} GB used. Disk ${disk.free_tb ?? "?"} of ${disk.total_tb ?? "?"} TB free. ${groups.filter((g) => g.home.length || g.burst.length).length} of ${groups.length} core groups in use. ${pl.online} players online.`;
  const inner = can("admin") ? `<div class="inner"><a href="#/users" class="${VIEW === "users" ? "on" : ""}">Users</a><a href="#/activity" class="${VIEW === "activity" ? "on" : ""}">Activity log</a></div>`
    : `<div class="count"><b>${pl.online}</b><span>playing</span></div>`;
  el.innerHTML = `<svg width="100%" height="100%" viewBox="0 0 204 204" role="img" aria-label="${esc(label)}">${svg}</svg>${inner}`;
  $$("[data-tip]", el).forEach((p) => (p.onclick = () => toast(p.dataset.tip)));
}

// ---------------------------------------------------------------- overview (home)
async function showHome() {
  $("#center").innerHTML = `<div class="page home"><div id="hbanners"></div>
    <div class="row" style="align-items:flex-end"><h1 style="flex:1">Overview</h1>
      ${can("moderator") ? `<button class="primary" onclick="openCreate()">${ICON.plus}New server</button>` : ""}</div>
    <section class="card hstats" id="hstats" aria-label="Host"></section>
    <section class="card" aria-label="Playing now"><h2>Playing now</h2><div id="hplay" style="margin-top:8px"></div></section>
    ${can("admin") ? `<section class="card" aria-label="Recent activity"><div class="row"><h2 style="flex:1">Recent activity</h2><a href="#/activity">See everything</a></div><div id="hact" style="margin-top:6px"></div></section>` : ""}</div>`;
  $("#hbanners").innerHTML = engineBanner();
  drawHomeStats(); drawHome();
  if (can("admin")) {
    const rows = await api("/activity?limit=10").catch(() => []);
    $("#hact") && ($("#hact").innerHTML = rows.map(actRow).join("") || `<div class="sub">Nothing yet.</div>`);
  }
}
function drawHomeStats() {
  const el = $("#hstats"); if (!el || !INFO) return;
  const mem = INFO.host?.memory || {}, disk = INFO.host?.disk || {}, pl = INFO.players || {};
  const stat = (big, unit, label) => `<div><div class="big">${big}${unit ? `<small>${unit}</small>` : ""}</div><div class="sub">${label}</div></div>`;
  el.innerHTML = stat(`${LIST.filter(running).length}`, `/ ${LIST.length}`, "servers running") + stat(pl.online ?? 0, "", "players online")
    + stat(INFO.ram_pool_used_gb ?? 0, `/ ${INFO.ram_pool_gb} GB`, "Minecraft memory in use")
    + stat(mem.available_gb ?? "–", mem.total_gb ? `/ ${mem.total_gb} GB` : "", "host memory free")
    + (disk.total_tb && disk.total_tb < 1 ? stat(Math.round(disk.free_tb * 1024), `/ ${Math.round(disk.total_tb * 1024)} GB`, "disk free")
       : stat(disk.free_tb ?? "–", disk.total_tb ? `/ ${disk.total_tb} TB` : "", "disk free"));
}
// Everyone online, on every server: the one thing the sidebar doesn't show.
function drawHome() {
  const el = $("#hplay"); if (!el) return;
  const rows = LIST.filter((s) => s.phase === "online").flatMap((s) => (s.stats?.players || []).map((n) => ({ n, s, since: s.stats?.joined?.[n] })))
    .sort((a, b) => (a.since || 0) - (b.since || 0));
  el.innerHTML = rows.map(({ n, s, since }) => `<div class="prow hp"><span class="head" style="background:${headColor(n)}" aria-hidden="true">${esc(n[0].toUpperCase())}</span>
      <span>${esc(n)}</span><a href="#/s/${esc(s.id)}">${esc(s.name)}</a><span class="sub">${since ? dur(Date.now() / 1000 - since) : ""}</span></div>`).join("")
    || `<div class="sub">No one's playing right now.</div>`;
}

// ---------------------------------------------------------------- server page (middle)
function drawEmpty() {
  $("#center").innerHTML = `<div class="empty"><img src="/static/brand/spawnpoint-icon.svg" alt=""><h2>No servers yet</h2>
    <p class="muted">Install a modpack or a plain server, or upload one you already have. Addresses, memory and world pre-generation are handled for you.</p>
    ${can("moderator") ? `<button class="primary" onclick="openCreate()">Create your first server</button>` : `<p class="muted">Ask an admin or moderator to add one.</p>`}</div>`;
}

function engineBanner() {
  const eng = INFO?.engine || {};
  return `${eng.storage_driver === "vfs" ? `<div class="banner bad">The private Docker engine is using slow "vfs" storage. Put its folder (/var/lib/docker inside the container) on a local disk, not a network share (on Unraid: /mnt/cache/…, not /mnt/user/…).</div>` : ""}
    ${eng.error ? `<div class="banner bad">Docker engine not reachable: ${esc(eng.error)}</div>` : ""}
    ${INFO?.host?.guard?.level === "critical" ? `<div class="banner bad">The ${esc(INFO.host.guard.where)} disk is nearly full (${INFO.host.guard.free_gb} GB left). Backups, pre-generation and map drawing are paused until there's more room. Delete old backups or worlds, or add space.</div>`
      : INFO?.host?.guard?.level === "low" ? `<div class="banner">The ${esc(INFO.host.guard.where)} disk is running low: ${INFO.host.guard.free_gb} GB left.</div>` : ""}
    ${ME?.must_change_password ? `<div class="banner">You're still using the setup password from the container settings. <button class="small" onclick="openAccount()">Set your own</button></div>` : ""}`;
}

function drawServer() {
  const s = LIST.find((x) => x.id === SEL), c = $("#center");
  if (!s || !c) return;
  if (!$("#details")) {
    c.innerHTML = `<div id="banners"></div><section class="card details" id="details"></section>
      <section class="card mapcard" aria-label="World map"><div class="mhead" id="mhead"></div>
        <div class="mapbox"><canvas id="map" role="img" tabindex="0" title="Drag to move, scroll to zoom, double-click to go back to spawn"></canvas></div><div class="legend" id="legend"></div></section>
      <div class="statrow"><section class="card duo" id="duo" aria-label="Tick time and memory"></section><section class="card players" id="players" aria-label="Players"></section></div>`;
    mapObs = new ResizeObserver(() => drawMap()); mapObs.observe($(".mapbox"));
    panZoom($("#map"));
  }
  $("#banners").innerHTML = engineBanner();
  drawDetails(s); drawMapHead(s); drawMap(); drawStats(s);
}

function drawDetails(s) {
  const st = s.stats || {}, on = running(s), lists = LISTS[s.id], bk = BK[s.id];
  const pp = s.player_pack?.enabled && s.player_pack?.token && !["vanilla", "paper", "purpur"].includes(s.loader || s.kind)
    ? `<button class="small" id="ppopen" title="Copy or open the link players download the mods from">${ICON.link}Player pack</button>` : "";
  const acts = pp + (!can("moderator") ? "" : on
    ? `<button class="ic" data-a="restart" aria-label="Restart" title="Restart">${ICON.restart}</button>
       <button class="ic" data-a="stop" aria-label="Stop" title="Stop">${ICON.stop}</button>
       <button class="ic danger" data-a="kill" aria-label="Force stop" title="Force stop (no save)">${ICON.power}</button>`
    : `<button class="primary" data-a="start">${ICON.play}Start</button>`);
  const managed = (s.managed_mods || []).length;
  const facts = [
    ["Version", versionLabel(s)], ["Modpack", packLabel(s)],
    ["Mods", s.mod_count != null ? `${s.mod_count}${managed ? `, ${managed} yours` : ""}` : "–"],
    [s.last_start ? `Up for, started in ${s.last_start.seconds < 60 ? s.last_start.seconds + " s" : `${Math.floor(s.last_start.seconds / 60)} m ${s.last_start.seconds % 60} s`}` : "Up for",
     st.started_at && on ? dur(Date.now() / 1000 - Date.parse(st.started_at) / 1000) : "–"],
    ["CPU", st.status === "running" && st.cpu != null ? `${st.cpu}%` : "–"],
    ["World size", s.world_gb != null ? (s.world_gb < 0.1 ? "under 0.1 GB" : `${s.world_gb} GB`) : "–"],
    ["Last backup", bk ? (bk.length ? ago(Math.max(...bk.map((b) => b.time))) : "Never") : "–"],
    ["Invite only", lists ? (lists.invite_only ? `On, ${lists.whitelist.length} players` : "Off") : "–"],
  ];
  const showMsg = s.phase_msg && s.phase !== "online" && !s.progress;
  $("#details").innerHTML = `<div class="dhead"><h1 title="${esc(s.name)}">${esc(s.name)}</h1><div class="acts">${acts}</div></div>
    ${s.restart_pending && on ? `<div class="banner msg">Changes are waiting for a restart. ${can("moderator") ? `<button class="small" data-a="restart">Restart now</button>` : ""}</div>` : ""}
    ${s.progress ? `<div class="setup"><div class="lab"><span><b>Step ${s.progress.step} of ${s.progress.of}: ${esc(s.progress.label)}</b>${s.progress.detail ? `, ${esc(s.progress.detail)}` : ""}</span>${s.progress.percent != null ? `<span>${s.progress.percent.toFixed(0)}%</span>` : ""}</div>${bar4(s.progress)}</div>` : ""}
    ${showMsg || (s.phase_msg && /client-only/.test(s.phase_msg)) ? `<div class="banner ${s.phase === "error" ? "bad" : "info"} msg">${esc(s.phase_msg)}</div>` : ""}
    <dl class="facts">${facts.map(([k, v]) => `<div><dt title="${esc(k)}">${esc(k)}</dt><dd title="${esc(v)}">${esc(v)}</dd></div>`).join("")}</dl>`;
  $("#ppopen") && ($("#ppopen").onclick = () => openPlayerPack(s.id));
  $$("[data-a]", $("#details")).forEach((b) => (b.onclick = async () => {
    if (b.dataset.a === "kill" && !confirm(KILL_WARNING)) return;
    b.disabled = true;
    await act(() => api(`/servers/${s.id}/action/${b.dataset.a}`, { method: "POST" }));
    refreshList();
  }));
}

// How far the world is generated. Chunky fills a square centred on the world spawn, so the
// generated area is a square whose half-width grows with the square root of the progress.
function genState(s) {
  const p = s.pregen || {}, R = p.full_radius || 2500, sR = p.spawn_radius || 750, pct = (p.percent || 0) / 100;
  let r = 0, label;
  if (p.stage === "spawn") { r = sR * Math.sqrt(pct); label = `Generating the spawn area: ${(pct * 100).toFixed(0)}% of ${sR.toLocaleString()} blocks`; }
  else if (p.stage === "background") { r = Math.max(sR, R * Math.sqrt(pct)); label = `${(pct * 100).toFixed(0)}% of ${R.toLocaleString()} blocks${s.phase !== "online" ? "" : p.paused ? (p.paused_for === "hand" ? ", paused by hand: press Resume pre-gen to carry on" : p.paused_for === "players" ? ", waiting until nobody is playing" : p.paused_for === "disk" ? ", paused: the disk is nearly full" : ", paused while the server is busy") : s.burst ? ", using spare cores" : ""}`; }
  else if (p.stage === "done" && s.loader === "vanilla" && !p.method) { label = "The world generates as people explore"; }
  else if (p.stage === "done" && p.spawn_only) { r = sR; label = `Spawn area generated (${sR.toLocaleString()} blocks). Further out generates as people explore`; }
  else if (p.stage === "done") { r = R; label = `Generated out to ${R.toLocaleString()} blocks`; }
  else if (p.stage === "skipped") { label = "This Minecraft version can't pre-generate (needs Chunky or Minecraft 1.14+)"; }
  else { label = "Starts once the server is set up"; }
  const up = s.phase === "online" || s.phase === "pregen-spawn";
  if (!up && (p.stage === "spawn" || p.stage === "background")) label += ", continues when the server starts";
  return { R, sR, r, pct, label, spawnOnly: p.stage === "done" && !!p.spawn_only, active: up && (p.stage === "spawn" || p.stage === "background"), stage: p.stage, paused: p.paused, up,
    cx: s.spawn?.x || 0, cz: s.spawn?.z || 0 };
}
const MAPDIM = {}, VIEWS = {}, MAPIDX = {}, PLAYERS = {}, TILES = new Map();
const OVERLAY_KEY = "sp-gen-overlay";
const overlayOn = () => { try { return localStorage.getItem(OVERLAY_KEY) === "true"; } catch { return false; } };
function setOverlay(on) { try { localStorage.setItem(OVERLAY_KEY, String(on)); } catch {} drawMap(); }
const ICON_EXPAND = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>';
const DIMNAME = { overworld: "Overworld", nether: "Nether", end: "The End" };
const MAX_PPB = 4;                                   // closest zoom: 4 pixels per block
function mapStatus(s) {
  const m = s.map || {};
  if (m.status === "rendering") return `Updating the map${m.percent != null ? `: ${m.percent.toFixed(0)}%` : ""}`;
  if (m.status === "paused" || m.status === "waiting" || m.status === "error" || m.status === "queued") return m.note || "";
  if (s.map_ready && m.rendered) return `Map updated ${ago(m.rendered)}${m.note ? `. ${m.note}` : ""}`;
  if (s.map_ready) return "";
  if (m.rendered) return `Nothing to draw yet (checked ${ago(m.rendered)})${m.note ? `. ${m.note}` : ": no finished chunks on disk so far"}`;
  return "The terrain map is drawn on spare cores and appears here when ready.";
}
async function loadMapIndex(sid) {
  const idx = await api(`/servers/${sid}/map`).catch(() => null);
  if (idx) { MAPIDX[sid] = idx; if (SEL === sid) { drawMapHead(LIST.find((x) => x.id === sid)); drawMap(); } }
}
async function pollPlayers() {
  const s = LIST.find((x) => x.id === SEL);
  if (VIEW !== "server" || !s || s.phase !== "online" || !(s.stats?.online > 0)) { if (s) PLAYERS[s.id] = []; return; }
  const ps = await api(`/servers/${s.id}/map/players`).catch(() => null);
  if (ps) { PLAYERS[s.id] = ps; drawMap(); }
}
function mapDims(s) {
  const have = Object.keys(MAPIDX[s.id]?.dims || {}).filter((d) => MAPIDX[s.id].dims[d].length);
  return have.length ? ["overworld", "nether", "end"].filter((d) => have.includes(d)) : ["overworld"];
}
function drawMapHead(s) {
  if (!s || !$("#mhead")) return;
  const g = genState(s), ready = !!s.map_ready, ov = overlayOn(), dims = mapDims(s);
  const dim = dims.includes(MAPDIM[s.id]) ? MAPDIM[s.id] : dims[0]; MAPDIM[s.id] = dim;
  const full = $(".mapcard")?.classList.contains("full");
  const ctl = can("moderator") && g.stage === "background" && s.phase === "online"
    ? `<button class="small" data-pg="${g.paused ? "pregen-resume" : "pregen-pause"}">${g.paused ? "Resume pre-gen" : "Pause pre-gen"}</button>` : "";
  const st = mapStatus(s);
  $("#mhead").innerHTML = `<div class="mtop"><h2>World map${full ? `: ${esc(s.name)}` : ""}</h2>
    <div class="row" style="flex-wrap:wrap;justify-content:flex-end">${ctl}
      ${ready && dim === "overworld" && hasGen(s) ? `<button class="small ${ov ? "toggled" : ""}" data-overlay aria-pressed="${ov}" title="Show or hide the spawn area, generated land and the edge being generated">Generation</button>` : ""}
      ${dims.length > 1 ? `<label for="mdim" class="sr">Dimension</label><select id="mdim" style="width:auto;height:36px">${dims.map((d) => `<option value="${d}" ${d === dim ? "selected" : ""}>${DIMNAME[d] || d}</option>`).join("")}</select>` : ""}
      ${ready ? `<button class="small ${HEATON ? "toggled" : ""}" data-heat aria-pressed="${HEATON}" title="Where players spend their time">Activity</button>` : ""}
      ${ready && can("moderator") ? `<button class="small ${PLACING ? "toggled" : ""}" data-mark aria-pressed="${!!PLACING}" title="Click the map to place a marker">${PLACING ? "Click the map…" : "Add marker"}</button>` : ""}
      ${can("moderator") && mapperOn(s) ? `<button class="small" data-render title="Redraw the whole map now">Update map</button>` : ""}
      <button class="ic" data-full aria-pressed="${!!full}" aria-label="${full ? "Close the full-screen map" : "Show the map full screen"}" title="${full ? "Close full screen (Esc)" : "Full screen"}">${full ? "✕" : ICON_EXPAND}</button></div></div>
    <div class="sub">${esc(g.label)}</div>
    ${g.stage === "spawn" || g.stage === "background" ? `<div class="bar slim"><i class="${g.active && !g.paused ? "live" : ""}" style="width:${(g.pct * 100).toFixed(1)}%"></i></div>` : ""}
    ${st ? `<div class="sub" style="margin-top:4px">${esc(st)}</div>` : ""}`;
  $$("[data-pg]", $("#mhead")).forEach((b) => (b.onclick = async () => { b.disabled = true; await act(() => api(`/servers/${s.id}/action/${b.dataset.pg}`, { method: "POST" })); refreshList(); }));
  $("[data-overlay]", $("#mhead")) && ($("[data-overlay]", $("#mhead")).onclick = () => { setOverlay(!overlayOn()); drawMapHead(s); });
  $("[data-heat]", $("#mhead")) && ($("[data-heat]", $("#mhead")).onclick = () => toggleHeat(s));
  $("[data-mark]", $("#mhead")) && ($("[data-mark]", $("#mhead")).onclick = () => { PLACING = PLACING ? null : true; drawMapHead(s); if ($("#map")) $("#map").style.cursor = PLACING ? "crosshair" : "grab"; });
  $("#mdim") && ($("#mdim").onchange = (e) => { MAPDIM[s.id] = e.target.value; delete VIEWS[s.id + "/" + e.target.value]; drawMapHead(s); drawMap(); });
  $("[data-render]", $("#mhead")) && ($("[data-render]", $("#mhead")).onclick = () => act(() => api(`/servers/${s.id}/map/render`, { method: "POST" }), "Redrawing the whole map: it starts within a minute"));
  $("[data-full]", $("#mhead")).onclick = () => toggleFullMap();
  const onOver = dim === "overworld" && hasGen(s) && (!ready || ov);
  $("#legend").innerHTML = `${onOver ? `<span><i class="ring"></i>Spawn area</span>
    ${g.stage === "background" ? `<span><i style="background:#7A3442"></i>Generated while playing</span>` : ""}
    ${g.active ? `<span><i style="background:${g.paused ? "#8A5A3C" : "#FF5A2B"}"></i>${g.paused ? "Paused" : "Generating now"}</span>` : ""}${ready ? "" : `<span><i style="background:#2A2F38"></i>Not generated yet</span>`}
    ${g.stage === "background" || (g.stage === "done" && !g.spawnOnly) ? `<span><i class="ring" style="border-color:#7D8593"></i>${g.R.toLocaleString()}-block limit</span>` : ""}` : ""}
    ${dim === "overworld" ? `<span><svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true" style="vertical-align:-1px;margin-right:6px"><path d="M1.5 6 6 1.5 10.5 6M3 5v5.5h6V5M5 10.5V8h2v2.5" fill="none" stroke="#FF9D4A" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/></svg>World spawn</span>` : ""}
    ${s.phase === "online" && s.stats?.online ? `<span><i class="dot"></i>Players</span>` : ""}
    ${HEATON ? `<span><i style="background:rgba(255,110,60,.85)"></i>Where players spend time</span>` : ""}`;
}
const mapperOn = (s) => !!(s.map_ready || s.map?.status);
// Only servers that pre-generate have generation progress to show; vanilla (and packs where
// Chunky isn't available) just get the terrain.
const hasGen = (s) => ["spawn", "background", "done"].includes(s.pregen?.stage) && !(s.loader === "vanilla" && !s.pregen?.method);
function toggleFullMap(force) {
  const card = $(".mapcard"); if (!card) return;
  const on = force ?? !card.classList.contains("full");
  card.classList.toggle("full", on); document.body.classList.toggle("mapfull", on);
  drawMapHead(LIST.find((x) => x.id === SEL)); requestAnimationFrame(drawMap);
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $(".mapcard.full")) toggleFullMap(false); });

// One chunk renderer. T maps world (x, z) to screen: sx = a*x + c*z + e, sy = b*x + d*z + f.
// base = no terrain under it (draw every chunk); otherwise it's a see-through overlay on the map.
const hashChunk = (i, j) => { let n = (i * 374761393 + j * 668265263) ^ 0x5bd1e995; n = Math.imul(n ^ (n >>> 13), 1274126177); return ((n ^ (n >>> 16)) >>> 0) / 4294967295; };
function drawChunks(x, w, h, T, g, base) {
  const ppb = Math.hypot(T.a, T.b); if (!ppb || !isFinite(ppb)) return;
  let step = 16; while (step * ppb < 5) step *= 2;                       // chunks, or groups of chunks when zoomed out
  const det = T.a * T.d - T.b * T.c, inv = (sx, sy) => [(T.d * (sx - T.e) - T.c * (sy - T.f)) / det, (-T.b * (sx - T.e) + T.a * (sy - T.f)) / det];
  const cs = [inv(0, 0), inv(w, 0), inv(0, h), inv(w, h)];
  const x0 = Math.floor(Math.min(...cs.map((c) => c[0])) / step) * step, x1 = Math.ceil(Math.max(...cs.map((c) => c[0])) / step) * step;
  const z0 = Math.floor(Math.min(...cs.map((c) => c[1])) / step) * step, z1 = Math.ceil(Math.max(...cs.map((c) => c[1])) / step) * step;
  x.save(); x.setTransform(T.a, T.b, T.c, T.d, T.e, T.f);               // draw in world blocks from here on
  const snapLo = (v) => Math.floor(v / 16) * 16, snapHi = (v) => Math.ceil(v / 16) * 16;
  const sq = (r) => [snapLo(g.cx - r), snapLo(g.cz - r), snapHi(g.cx + r), snapHi(g.cz + r)];
  const rect = ([ax, az, bx, bz]) => x.rect(ax, az, bx - ax, bz - az);
  const [gx0, gz0, gx1, gz1] = sq(g.r), spawnSq = sq(g.sR);
  if (base) { x.fillStyle = "#2A2F38"; x.fillRect(x0, z0, x1 - x0, z1 - z0); }
  if (g.r > 0) {
    if (g.r > g.sR) {
      x.beginPath(); rect([gx0, gz0, gx1, gz1]); rect(spawnSq); x.fillStyle = base ? "#7A3442" : "rgba(214,58,86,.24)"; x.fill("evenodd");
      if (!base) { x.beginPath(); rect([gx0, gz0, gx1, gz1]); x.lineWidth = 2 / ppb; x.strokeStyle = "rgba(214,58,86,.9)"; x.stroke(); }
    }
    x.beginPath(); rect(g.r >= g.sR ? spawnSq : [gx0, gz0, gx1, gz1]); x.fillStyle = base ? "#5B6675" : "rgba(255,157,74,.08)"; x.fill();
  }
  if (g.active && g.r > 0) {                                              // the edge being generated: a ragged band of chunks
    // Each chunk gets a fixed offset, so as the edge moves out chunks join and leave it one by
    // one instead of the whole band reshuffling on every refresh. Dimmed while paused.
    x.fillStyle = g.paused ? (base ? "#8A5A3C" : "rgba(255,157,74,.45)") : (base ? "#FF5A2B" : "rgba(255,90,43,.85)");
    const band = Math.max(48, step * 2);
    for (let cx = Math.max(x0, gx0 - band); cx < Math.min(x1, gx1 + band); cx += step)
      for (let cz = Math.max(z0, gz0 - band); cz < Math.min(z1, gz1 + band); cz += step) {
        const d = Math.max(Math.abs(cx + step / 2 - g.cx), Math.abs(cz + step / 2 - g.cz)) - hashChunk(cx / step, cz / step) * band;
        if (d > g.r - band && d <= g.r) x.fillRect(cx, cz, step, step);
      }
  }
  if (step * ppb >= 4) {                                                  // the chunk grid
    x.beginPath();
    for (let cx = x0; cx <= x1; cx += step) { x.moveTo(cx, z0); x.lineTo(cx, z1); }
    for (let cz = z0; cz <= z1; cz += step) { x.moveTo(x0, cz); x.lineTo(x1, cz); }
    x.lineWidth = 1 / ppb; x.strokeStyle = base ? "#1C1F25" : "rgba(0,0,0,.18)"; x.stroke();
  }
  const outline = (r, col, width, dash) => { x.beginPath(); rect(sq(r)); x.lineWidth = width / ppb; x.setLineDash(dash.map((v) => v / ppb)); x.strokeStyle = col; x.stroke(); x.setLineDash([]); };
  if (g.stage === "background" || (g.stage === "done" && !g.spawnOnly)) outline(g.R, base ? "#9AA3B2" : "rgba(233,236,241,.75)", 1.5, [4, 5]);
  outline(g.sR, "#FF9D4A", 2, [6, 4]);
  x.restore();
}

// What the map can be dragged across: everything drawn, the pre-gen square and the players.
function mapBounds(s, dim) {
  const g = genState(s), tiles = MAPIDX[s.id]?.dims?.[dim] || [], ps = (PLAYERS[s.id] || []).filter((p) => p.dim === dim);
  const gR = g.spawnOnly ? g.sR : g.R;
  let b = dim === "overworld" && hasGen(s) ? [g.cx - gR, g.cz - gR, g.cx + gR, g.cz + gR]
    : dim === "overworld" ? [g.cx - 512, g.cz - 512, g.cx + 512, g.cz + 512] : [-512, -512, 512, 512];
  for (const [x, z] of tiles) b = [Math.min(b[0], x * 512), Math.min(b[1], z * 512), Math.max(b[2], x * 512 + 512), Math.max(b[3], z * 512 + 512)];
  for (const p of ps) b = [Math.min(b[0], p.x - 64), Math.min(b[1], p.z - 64), Math.max(b[2], p.x + 64), Math.max(b[3], p.z + 64)];
  return b;
}
function viewFor(s, dim, w, h) {
  const key = s.id + "/" + dim, b = mapBounds(s, dim), fit = Math.min(w / (b[2] - b[0]), h / (b[3] - b[1])) * 0.95;
  let v = VIEWS[key];
  if (!v) {                                                    // start on spawn, showing what's generated with room around it
    const g = genState(s), span = dim === "overworld" ? Math.max(g.sR * 2.4, (g.r || g.sR) * 2.6) : 1024;
    v = VIEWS[key] = { x: dim === "overworld" ? g.cx : 0, z: dim === "overworld" ? g.cz : 0, ppb: Math.min(MAX_PPB, Math.max(fit, Math.min(w, h) / span)) };
  }
  v.minppb = Math.min(fit, MAX_PPB);
  v.ppb = Math.min(MAX_PPB, Math.max(v.minppb, v.ppb));
  v.x = Math.min(b[2], Math.max(b[0], v.x)); v.z = Math.min(b[3], Math.max(b[1], v.z));
  return v;
}
const FACES = new Map();                                // name -> Image (or null while loading / when missing)
function headImage(name) {
  if (!FACES.has(name)) {
    const im = new Image(); FACES.set(name, null);
    im.onload = () => { FACES.set(name, im); requestAnimationFrame(drawMap); };
    im.src = `/api/heads/${encodeURIComponent(name)}.png`;
  }
  return FACES.get(name);
}
function tileImage(url) {
  let im = TILES.get(url);
  if (!im) { im = new Image(); im.onload = () => requestAnimationFrame(drawMap); im.onerror = () => { im._bad = true; }; im.src = url; TILES.set(url, im);
    if (TILES.size > 1500) TILES.delete(TILES.keys().next().value); }
  return im;
}
function panZoom(cv) {
  let drag = null;
  const cur = () => { const s = LIST.find((x) => x.id === SEL); return s && VIEWS[s.id + "/" + (MAPDIM[s.id] || "overworld")]; };
  let moved = 0;
  cv.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY }; moved = 0; cv.setPointerCapture(e.pointerId); cv.style.cursor = "grabbing"; });
  cv.addEventListener("pointermove", (e) => {
    const v = cur();
    if (!drag || !v) {                                       // hovering: point at markers
      const s = LIST.find((x) => x.id === SEL); if (!s || !v) return;
      const r = cv.getBoundingClientRect();
      cv.style.cursor = PLACING ? "crosshair" : markerAt(s, MAPDIM[s.id] || "overworld", e.clientX - r.left, e.clientY - r.top, r.width, r.height, v) ? "pointer" : "grab";
      return;
    }
    moved += Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y);
    v.x -= (e.clientX - drag.x) / v.ppb; v.z -= (e.clientY - drag.y) / v.ppb; drag = { x: e.clientX, y: e.clientY }; drawMap();
  });
  const end = (e) => {
    const click = drag && moved < 5 && e.type === "pointerup";
    drag = null; cv.style.cursor = PLACING ? "crosshair" : "grab";
    if (!click) return;
    const s = LIST.find((x) => x.id === SEL), v = cur(); if (!s || !v) return;
    const r = cv.getBoundingClientRect(), sx = e.clientX - r.left, sy = e.clientY - r.top, dim = MAPDIM[s.id] || "overworld";
    const wx = Math.floor(v.x + (sx - r.width / 2) / v.ppb), wz = Math.floor(v.z + (sy - r.height / 2) / v.ppb);
    if (PLACING) { PLACING = null; drawMapHead(s); markerModal(s, { dim, x: wx, z: wz }); return; }
    const m = markerAt(s, dim, sx, sy, r.width, r.height, v);
    if (m) markerModal(s, m);
  };
  cv.addEventListener("pointerup", end); cv.addEventListener("pointercancel", end);
  cv.addEventListener("wheel", (e) => {                         // zoom around the pointer
    e.preventDefault(); const v = cur(); if (!v) return;
    const r = cv.getBoundingClientRect(), mx = e.clientX - r.left - r.width / 2, my = e.clientY - r.top - r.height / 2;
    const wx = v.x + mx / v.ppb, wz = v.z + my / v.ppb;
    v.ppb = Math.min(MAX_PPB, Math.max(v.minppb || 0.01, v.ppb * (e.deltaY < 0 ? 1.2 : 1 / 1.2)));
    v.x = wx - mx / v.ppb; v.z = wz - my / v.ppb; drawMap();
  }, { passive: false });
  cv.addEventListener("dblclick", () => { const s = LIST.find((x) => x.id === SEL); if (s) { delete VIEWS[s.id + "/" + (MAPDIM[s.id] || "overworld")]; drawMap(); } });
  cv.addEventListener("keydown", (e) => {
    const v = cur(); if (!v) return; const stepB = 60 / v.ppb;
    const k = { ArrowLeft: [-stepB, 0], ArrowRight: [stepB, 0], ArrowUp: [0, -stepB], ArrowDown: [0, stepB] }[e.key];
    if (k) { v.x += k[0]; v.z += k[1]; } else if (e.key === "+" || e.key === "=") v.ppb *= 1.25; else if (e.key === "-") v.ppb /= 1.25;
    else if (e.key === "0") { const s = LIST.find((x) => x.id === SEL); delete VIEWS[s.id + "/" + (MAPDIM[s.id] || "overworld")]; } else return;
    e.preventDefault(); drawMap();
  });
  cv.style.cursor = "grab";
}
function sizeCanvas(cv) {
  const box = cv.parentElement, w = box.clientWidth, h = box.clientHeight, dpr = window.devicePixelRatio || 1;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  return { w, h, dpr };
}
function drawPin(x, px, pz, color, name, hot) {
  x.save();
  x.beginPath(); x.moveTo(px, pz); x.bezierCurveTo(px - 3, pz - 6, px - 8, pz - 9, px - 8, pz - 15);
  x.arc(px, pz - 15, 8, Math.PI, 0); x.bezierCurveTo(px + 8, pz - 9, px + 3, pz - 6, px, pz); x.closePath();
  x.fillStyle = color; x.fill(); x.lineWidth = hot ? 2.5 : 1.5; x.strokeStyle = "rgba(10,12,16,.85)"; x.stroke();
  x.beginPath(); x.arc(px, pz - 15, 3, 0, Math.PI * 2); x.fillStyle = "rgba(10,12,16,.75)"; x.fill();
  if (name) {
    x.font = "600 12px Geist, Inter, system-ui, sans-serif"; x.textBaseline = "middle"; x.textAlign = "left";
    x.lineWidth = 3; x.strokeStyle = "rgba(0,0,0,.75)"; x.fillStyle = "#fff"; x.strokeText(name, px + 11, pz - 15); x.fillText(name, px + 11, pz - 15);
  }
  x.restore();
}
let PLACING = null, HEAT = {}, HEATON = false;
function markerAt(s, dim, sx, sy, w, h, v) {
  let best = null, bd = 14;
  for (const m of (MAPIDX[s.id]?.markers || []).filter((m) => m.dim === dim)) {
    const px = w / 2 + (m.x + 0.5 - v.x) * v.ppb, pz = h / 2 + (m.z + 0.5 - v.z) * v.ppb;
    const d = Math.hypot(sx - px, sy - (pz - 14));
    if (d < bd) { bd = d; best = m; }
  }
  return best;
}
const MARKER_COLORS = ["#FF9D4A", "#5B8DEF", "#22C55E", "#E879F9", "#F5A524", "#F87171", "#E9ECF1"];
// A marker: view it (anyone), or add, rename, recolour, move to the public map or remove it (moderators).
function markerModal(s, m) {
  const edit = can("moderator"), isNew = !m.id;
  const color = m.color || MARKER_COLORS[(MAPIDX[s.id]?.markers || []).length % MARKER_COLORS.length];
  if (!edit) {
    modal(`<h2>${esc(m.name)}</h2><p class="muted mono">${DIMNAME[m.dim] || m.dim}: ${m.x}, ${m.z}</p>${m.by ? `<p class="sub">Added by ${esc(m.by)}</p>` : ""}
      <div class="row" style="justify-content:flex-end"><button class="small" id="mkcp">Copy coordinates</button><button onclick="closeModal()">Close</button></div>`);
    $("#mkcp").onclick = () => copy(`${m.x} ~ ${m.z}`); return;
  }
  modal(`<h2>${isNew ? "Add a marker" : "Marker"}</h2>
    <label for="mkn">Name</label><input id="mkn" maxlength="40" value="${esc(m.name || "")}" placeholder="Like Main base or Nether hub">
    <div class="row" style="flex-wrap:nowrap;gap:10px"><div style="flex:1"><label for="mkx">X</label><input id="mkx" type="number" value="${m.x}"></div>
      <div style="flex:1"><label for="mkz">Z</label><input id="mkz" type="number" value="${m.z}"></div></div>
    <div class="sub" style="margin-top:10px">Colour</div>
    <div class="row" role="radiogroup" aria-label="Colour" style="gap:8px;margin-top:6px">${MARKER_COLORS.map((c) => `<button type="button" class="swatch ${c === color ? "on" : ""}" role="radio" aria-checked="${c === color}" data-c="${c}" style="background:${c}" aria-label="Colour ${c}"></button>`).join("")}</div>
    <label class="row" style="color:var(--ink);margin-top:12px"><input type="checkbox" id="mkp" ${m.public === false ? "" : "checked"}> Show on the public map</label>
    <div class="row" style="justify-content:space-between;margin-top:16px">${isNew ? "<span></span>" : `<button class="danger" id="mkd">Remove</button>`}
      <div class="row"><button class="ghost" onclick="closeModal()">Cancel</button><button class="primary" id="mks">${isNew ? "Add marker" : "Save"}</button></div></div>`);
  let pick = color;
  $$(".swatch").forEach((b) => (b.onclick = () => { pick = b.dataset.c; $$(".swatch").forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-checked", x === b); }); }));
  setTimeout(() => $("#mkn").focus(), 30);
  const save = async () => {
    const body = { name: $("#mkn").value, dim: m.dim, x: parseInt($("#mkx").value, 10), z: parseInt($("#mkz").value, 10), color: pick, public: $("#mkp").checked };
    const r = await act(() => api(`/servers/${s.id}/markers${isNew ? "" : "/" + m.id}`, { method: isNew ? "POST" : "PUT", body }), isNew ? "Marker added" : "Marker saved");
    if (r) { closeModal(); loadMapIndex(s.id); }
  };
  $("#mks").onclick = save;
  $("#mkn").onkeydown = (e) => { if (e.key === "Enter") save(); };
  $("#mkd") && ($("#mkd").onclick = async () => {
    if (await act(() => api(`/servers/${s.id}/markers/${m.id}`, { method: "DELETE" }), "Marker removed")) { closeModal(); loadMapIndex(s.id); }
  });
}
async function toggleHeat(s) {
  HEATON = !HEATON;
  if (HEATON) for (const d of mapDims(s)) HEAT[s.id + "/" + d] = await api(`/servers/${s.id}/map/heat?dim=${d}`).catch(() => null);
  drawMapHead(s); drawMap();
}
function drawMap() {
  const s = LIST.find((x) => x.id === SEL), cv = $("#map"); if (!s || !cv) return;
  const { w, h, dpr } = sizeCanvas(cv); if (!w || !h) return;
  const dim = MAPDIM[s.id] || "overworld", v = viewFor(s, dim, w, h), g = genState(s);
  const x = cv.getContext("2d");
  x.setTransform(1, 0, 0, 1, 0, 0); x.fillStyle = "#16191D"; x.fillRect(0, 0, cv.width, cv.height);
  const k = v.ppb * dpr, T = { a: k, b: 0, c: 0, d: k, e: (w / 2 - v.x * v.ppb) * dpr, f: (h / 2 - v.z * v.ppb) * dpr };
  const tiles = MAPIDX[s.id]?.dims?.[dim] || [];
  if (tiles.length) {
    const lod = v.ppb < 0.3 ? "small" : "full";
    const wx0 = v.x - w / 2 / v.ppb, wx1 = v.x + w / 2 / v.ppb, wz0 = v.z - h / 2 / v.ppb, wz1 = v.z + h / 2 / v.ppb;
    x.imageSmoothingEnabled = v.ppb < 1;
    for (const [tx, tz, ver] of tiles) {
      if (tx * 512 > wx1 || tx * 512 + 512 < wx0 || tz * 512 > wz1 || tz * 512 + 512 < wz0) continue;
      const url = `/map/${encodeURIComponent(s.id)}/${dim}/${lod}/${tx}.${tz}.png?v=${ver}`;
      let im = tileImage(url);
      if (!im.complete || im._bad) {                          // show the other size while this one loads
        const alt = TILES.get(`/map/${encodeURIComponent(s.id)}/${dim}/${lod === "full" ? "small" : "full"}/${tx}.${tz}.png?v=${ver}`);
        if (alt && alt.complete && !alt._bad) im = alt; else continue;
      }
      x.drawImage(im, T.e + tx * 512 * k, T.f + tz * 512 * k, 512 * k + 0.5, 512 * k + 0.5);
    }
  }
  if (dim === "overworld" && hasGen(s) && (!tiles.length || overlayOn())) drawChunks(x, cv.width, cv.height, T, g, !tiles.length);
  if (!tiles.length && !hasGen(s)) {
    x.setTransform(1, 0, 0, 1, 0, 0); x.fillStyle = "#7D8593"; x.font = `${13 * dpr}px Geist, Inter, sans-serif`; x.textAlign = "center";
    x.fillText(s.map?.status === "rendering" ? "Drawing the map…" : "The map appears a few minutes after the world is first saved.", cv.width / 2, cv.height / 2);
  }
  // world spawn: a house marker
  x.setTransform(dpr, 0, 0, dpr, 0, 0);
  if (dim === "overworld") {
    const hx = w / 2 + (g.cx + 0.5 - v.x) * v.ppb, hz = h / 2 + (g.cz + 0.5 - v.z) * v.ppb;
    if (hx > -20 && hx < w + 20 && hz > -20 && hz < h + 20) {
      x.fillStyle = "rgba(22,25,29,.82)"; x.strokeStyle = "rgba(255,255,255,.9)"; x.lineWidth = 1.5;
      x.beginPath(); x.roundRect ? x.roundRect(hx - 11, hz - 11, 22, 22, 6) : x.rect(hx - 11, hz - 11, 22, 22); x.fill(); x.stroke();
      x.beginPath();                                         // roof and walls, with a door
      x.moveTo(hx - 6, hz - 0.5); x.lineTo(hx, hz - 6); x.lineTo(hx + 6, hz - 0.5);
      x.moveTo(hx - 4.5, hz - 1.5); x.lineTo(hx - 4.5, hz + 5.5); x.lineTo(hx + 4.5, hz + 5.5); x.lineTo(hx + 4.5, hz - 1.5);
      x.moveTo(hx - 1.5, hz + 5.5); x.lineTo(hx - 1.5, hz + 2); x.lineTo(hx + 1.5, hz + 2); x.lineTo(hx + 1.5, hz + 5.5);
      x.strokeStyle = "#FF9D4A"; x.lineWidth = 1.6; x.lineJoin = "round"; x.lineCap = "round"; x.stroke();
    }
  }
  // where people spend time
  const hm = HEATON && HEAT[s.id + "/" + dim];
  if (hm && hm.cells.length) {
    x.setTransform(dpr, 0, 0, dpr, 0, 0);
    const top = Math.log(1 + Math.max(...hm.cells.map((c) => c[2]))), sz = hm.cell * v.ppb;
    for (const [cx, cz, n] of hm.cells) {
      const px = w / 2 + (cx * hm.cell - v.x) * v.ppb, pz = h / 2 + (cz * hm.cell - v.z) * v.ppb;
      if (px > w || pz > h || px + sz < 0 || pz + sz < 0) continue;
      const a = 0.18 + 0.6 * Math.log(1 + n) / top;
      x.fillStyle = `rgba(255,${Math.round(190 - 150 * Math.log(1 + n) / top)},60,${a.toFixed(2)})`;
      x.fillRect(px, pz, Math.max(sz, 2), Math.max(sz, 2));
    }
  }
  // markers
  x.setTransform(dpr, 0, 0, dpr, 0, 0);
  for (const m of (MAPIDX[s.id]?.markers || []).filter((m) => m.dim === dim)) {
    const px = w / 2 + (m.x + 0.5 - v.x) * v.ppb, pz = h / 2 + (m.z + 0.5 - v.z) * v.ppb;
    if (px > -200 && px < w + 20 && pz > -20 && pz < h + 40) drawPin(x, px, pz, m.color, v.ppb > 0.025 ? m.name : "");
  }
  // players
  const ps = (PLAYERS[s.id] || []).filter((p) => p.dim === dim);
  x.setTransform(dpr, 0, 0, dpr, 0, 0); x.font = "600 12px Geist, Inter, sans-serif"; x.textBaseline = "middle";
  for (const p of ps) {
    const px = w / 2 + (p.x - v.x) * v.ppb, pz = h / 2 + (p.z - v.z) * v.ppb;
    if (px < -40 || px > w + 40 || pz < -40 || pz > h + 40) continue;
    const yaw = (p.yaw || 0) * Math.PI / 180;               // Minecraft yaw 0 = facing +z (south)
    x.fillStyle = "rgba(0,0,0,.55)"; x.beginPath(); x.moveTo(px - Math.sin(yaw) * 15, pz + Math.cos(yaw) * 15);
    x.lineTo(px - Math.sin(yaw + 0.5) * 8, pz + Math.cos(yaw + 0.5) * 8); x.lineTo(px - Math.sin(yaw - 0.5) * 8, pz + Math.cos(yaw - 0.5) * 8); x.fill();
    const face = headImage(p.name);
    x.strokeStyle = "#fff"; x.lineWidth = 2;
    if (face) {
      x.save(); x.beginPath(); x.roundRect ? x.roundRect(px - 10, pz - 10, 20, 20, 4) : x.rect(px - 10, pz - 10, 20, 20); x.clip();
      x.imageSmoothingEnabled = false; x.drawImage(face, px - 10, pz - 10, 20, 20); x.restore();
      x.beginPath(); x.roundRect ? x.roundRect(px - 10, pz - 10, 20, 20, 4) : x.rect(px - 10, pz - 10, 20, 20); x.stroke();
    } else {
      x.fillStyle = headColor(p.name);
      x.beginPath(); x.roundRect ? x.roundRect(px - 9, pz - 9, 18, 18, 4) : x.rect(px - 9, pz - 9, 18, 18); x.fill(); x.stroke();
      x.fillStyle = "#fff"; x.textAlign = "center"; x.fillText(p.name[0].toUpperCase(), px, pz + 0.5);
    }
    x.fillStyle = "#fff"; x.textAlign = "left"; x.lineWidth = 3; x.strokeStyle = "rgba(0,0,0,.7)"; x.strokeText(p.name, px + 13, pz); x.fillText(p.name, px + 13, pz);
  }
  cv.setAttribute("aria-label", `World map of ${s.name}, ${DIMNAME[dim] || dim}. ${g.label}. ${ps.length ? `Players here: ${ps.map((p) => p.name).join(", ")}.` : ""} Use the arrow keys to move and plus or minus to zoom.`);
}

function spark(vals, color) {
  const v = vals.filter((n) => n != null); if (v.length < 2) return `<div class="sub" style="margin-top:12px">Graph appears after a few minutes online.</div>`;
  const mx = Math.max(...v) * 1.25 || 1, n = v.length, W = 200, H = 34;
  const pts = v.map((y, i) => `${((i * W) / (n - 1)).toFixed(1)},${(H - (y / mx) * H).toFixed(1)}`).join(" ");
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="none" aria-hidden="true"><polygon points="0,${H} ${pts} ${W},${H}" fill="${color}22"/><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>`;
}
const HEADS = ["#5B8DEF", "#F5A524", "#22C55E", "#E879F9", "#38BDF8", "#F87171", "#A3E635"];
const headColor = (n) => HEADS[[...n].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7) % HEADS.length];
function drawStats(s) {
  const st = s.stats || {}, live = s.phase === "online", hist = HIST[s.id] || [];
  const max = s.memory.max_gb, normal = s.mem_normal_gb ?? max, used = live ? st.mem_gb || 0 : 0;
  $("#duo").innerHTML = `<div><h2>Tick time</h2><div class="big">${live && st.mspt != null ? `${st.mspt.toFixed(1)}<small>ms</small>` : "–"}</div>
      <div class="sub" title="Under 50 ms is smooth">last hour</div>${live ? spark(hist.map((p) => p.mspt), "#FF9D4A") : ""}</div><div class="vr"></div>
    <div><h2>Memory</h2><div class="big">${live ? `${used}<small>GB</small>` : "–"}</div><div class="sub">normally ${normal} GB</div>
      <div class="mbar" role="img" aria-label="${used} GB used, normally ${normal}, can grow to ${max} GB"><i style="width:${Math.min(100, (used / max) * 100).toFixed(1)}%"></i><u style="left:${((normal / max) * 100).toFixed(1)}%"></u></div>
      <div class="mscale"><span>0</span><span>${max} GB</span></div></div>`;
  const names = live ? st.players || [] : [], joined = st.joined || {};
  $("#players").innerHTML = `<h2>Playing now</h2><div class="sub">${live ? `${st.online ?? 0} of ${st.max ?? "–"} slots` : "Server isn't running"}</div>
    <div class="plist">${names.map((n) => `<div class="prow"><span class="head" style="background:${headColor(n)}" aria-hidden="true">${esc(n[0].toUpperCase())}</span><span>${esc(n)}</span>
      <span class="sub">${joined[n] ? dur(Date.now() / 1000 - joined[n]) : ""}</span></div>`).join("") || (live ? `<div class="sub">No one's on right now.</div>` : "")}</div>`;
}

// ---------------------------------------------------------------- right panel
const TABS = [["console", "Console", "viewer"], ["mods", "Mods", "viewer"], ["players", "Players", "viewer"], ["settings", "Settings", "moderator"],
  ["files", "Files", "moderator"], ["backups", "Backups", "viewer"], ["schedule", "Schedule", "viewer"], ["performance", "Stats", "viewer"]];
function drawRail() {
  const s = LIST.find((x) => x.id === SEL); if (!s) return;
  const tabs = TABS.filter(([, , r]) => can(r));
  if (!tabs.find(([t]) => t === TAB)) TAB = "console";
  const key = SEL + "/" + TAB; if (key === railKey) return;
  railKey = key; closeConsole();
  $("#rail").innerHTML = `<nav class="tabs" aria-label="Server tools">${tabs.map(([t, l]) => `<a href="#/s/${esc(SEL)}/${t}" class="${t === TAB ? "on" : ""}" ${t === TAB ? 'aria-current="page"' : ""}>${l}</a>`).join("")}</nav>
    <div class="tabbody" id="tab"></div>`;
  ({ console: tabConsole, mods: tabMods, players: tabPlayers, settings: tabSettings, files: tabFiles, backups: tabBackups, schedule: tabSchedule, performance: tabPerf }[TAB])(SEL, s);
}

// The last crash, or failed start, in plain words, above the console until dismissed.
function crashCard(s) {
  const c = s.last_crash, ex = c?.explain;
  if (!ex?.title || Date.now() / 1000 - c.at > 7 * 86400) return "";
  try { if (localStorage.getItem("sp-crash-seen-" + s.id) === String(c.at)) return ""; } catch {}
  return `<div class="crashcard" id="crashcard" role="status">
    <div class="row" style="justify-content:space-between;align-items:flex-start;gap:12px">
      <div><b>${esc(ex.title)}</b> <span class="sub">${ago(c.at)}</span></div>
      <button class="ic small" id="crashx" aria-label="Dismiss" title="Dismiss">✕</button></div>
    ${ex.detail ? `<p>${esc(ex.detail)}</p>` : ""}
    ${ex.mod ? `<p class="sub">Points at: <b>${esc(ex.mod)}</b>${ex.jar ? ` <span class="mono">(${esc(ex.jar)})</span>` : ""}</p>` : ""}
    ${ex.hint ? `<p class="sub">${esc(ex.hint)}</p>` : ""}
    <div class="row" style="gap:8px;flex-wrap:wrap">
      ${ex.report && can("moderator") ? `<button class="small" id="crashopen">Open the crash report</button>` : ""}
      ${ex.jar && can("moderator") ? `<button class="small" id="crashoff">Turn off ${esc(ex.jar)}</button>` : ""}
    </div></div>`;
}
let FILES_AT = null;

function tabConsole(sid) {
  const sv = LIST.find((x) => x.id === sid) || {};
  $("#tab").classList.add("fill");
  $("#tab").innerHTML = `${crashCard(sv)}<div class="console" id="con" role="log" aria-live="off"></div>
    ${can("moderator") ? `<form id="cf" class="cmdrow"><label for="cmd" class="sr">Console command</label><input id="cmd" placeholder="Type a command, like say hello" autocomplete="off"><button class="primary">Send</button></form>` : ""}`;
  const con = $("#con");
  if ($("#crashcard")) {
    const ex = sv.last_crash.explain;
    $("#crashx").onclick = () => { try { localStorage.setItem("sp-crash-seen-" + sid, String(sv.last_crash.at)); } catch {} $("#crashcard").remove(); };
    $("#crashopen") && ($("#crashopen").onclick = () => { FILES_AT = ex.report; location.hash = `#/s/${sid}/files`; });
    $("#crashoff") && ($("#crashoff").onclick = async () => {
      if (!confirm(`Turn off ${ex.jar}? It is renamed so the server skips it; you can turn it back on in the Mods tab.`)) return;
      if (await act(() => api(`/servers/${sid}/mods/file`, { method: "POST", body: { filename: ex.jar, action: "disable" } }), `${ex.jar} turned off. Start the server again.`))
        $("#crashx").click();
    });
  }
  es = new EventSource(`/api/servers/${sid}/console`);
  // The same message again and again (a mod complaining about every chunk) folds into one
  // line with a count, so the rest of the log stays readable.
  const body = (l) => l.replace(/^\[\d\d:\d\d:\d\d\]\s*/, "").replace(/^\S+T\S+\s+/, "");
  let last = null;
  es.onmessage = (e) => {
    const stick = con.scrollTop + con.clientHeight >= con.scrollHeight - 30;
    const l = e.data, b = body(l);
    if (last && last.body === b && last.el.isConnected) {
      last.n++; last.count.textContent = `×${last.n}`; last.stamp.textContent = l.slice(0, l.length - b.length);
      if (stick) con.scrollTop = con.scrollHeight;
      return;
    }
    const d = document.createElement("div");
    d.className = /ERROR|Exception/.test(l) ? "e" : /WARN/.test(l) ? "w" : "";
    const stamp = document.createElement("span"), count = document.createElement("span");
    stamp.textContent = l.slice(0, l.length - b.length); count.className = "rep";
    d.append(stamp, document.createTextNode(b), count); con.appendChild(d);
    last = { body: b, el: d, n: 1, count, stamp };
    while (con.childElementCount > 1500) con.firstChild.remove();
    if (stick) con.scrollTop = con.scrollHeight;
  };
  if (!$("#cf")) return;
  $("#cf").onsubmit = async (e) => {
    e.preventDefault(); const cmd = $("#cmd").value.trim(); if (!cmd) return;
    $("#cmd").value = "";
    const r = await act(() => api(`/servers/${sid}/command`, { method: "POST", body: { command: cmd } }));
    const d = document.createElement("div"); d.className = "c"; d.textContent = `> ${cmd}${r?.output ? "\n" + r.output : ""}`; last = null;
    con.appendChild(d); con.scrollTop = con.scrollHeight;
  };
}

// Updates for mods and plugins that came from Modrinth (plain servers; packs pick their own).
async function drawModUpdates(sid, mod, d) {
  const box = $("#mupd"); if (!box) return;
  d = d || await api(`/servers/${sid}/mod-updates`).catch(() => null);
  if (!d || !d.supported) { box.innerHTML = ""; return; }
  const items = d.items || [], rb = d.rollback;
  const when = d.checked ? `checked ${ago(d.checked)}` : "not checked yet";
  box.innerHTML = `<div class="card" style="display:grid;gap:8px">
    <div class="row" style="justify-content:space-between;flex-wrap:wrap;gap:8px"><h4 style="margin:0">Updates</h4>
      <div class="row" style="gap:8px">${mod ? `<button class="small" id="mucheck">Check now</button>` : ""}
      ${mod && items.length ? `<button class="small primary" id="muall">Update all (${items.length})</button>` : ""}</div></div>
    <div class="sub">${items.length ? `${items.length} can be updated` : d.checked ? "Everything from Modrinth is up to date" : "SpawnPoint checks once a day"}, ${when}.${d.unknown ? ` ${d.unknown} of ${d.total} file(s) aren't from Modrinth, so they can't be checked.` : ""}</div>
    ${items.length ? `<div class="list">${items.map((i) => `<div class="item"><div class="grow"><div class="t">${esc(i.title)}</div>
      <div class="d mono">${esc(i.current)} → ${esc(i.latest)}</div></div>
      ${mod ? `<button class="small" data-mu="${esc(i.file)}">Update</button>` : ""}</div>`).join("")}</div>` : ""}
    ${rb ? `<div class="row" style="justify-content:space-between;gap:8px;flex-wrap:wrap"><div class="sub">Last update ${ago(rb.at)}: ${rb.swaps.map((x) => `${esc(x.title)} ${esc(x.from)} → ${esc(x.to)}`).join(", ")}.</div>
      ${mod ? `<button class="small" id="murb">Undo that update</button>` : ""}</div>` : ""}
  </div>`;
  const run = async (files, label) => {
    const r = await act(() => api(`/servers/${sid}/mod-updates/update`, { method: "POST", body: { files } }));
    if (!r) return;
    toast(r.failed?.length ? `Updated ${r.updated.length}; ${r.failed.length} failed: ${r.failed.map((f) => f.title).join(", ")}` : `${label}. Restart the server to use the new version${r.updated.length > 1 ? "s" : ""}.`, !!r.failed?.length);
    tabMods(sid, LIST.find((x) => x.id === sid));
  };
  $("#mucheck") && ($("#mucheck").onclick = async (e) => { e.target.disabled = true; e.target.textContent = "Checking…"; const r = await act(() => api(`/servers/${sid}/mod-updates/check`, { method: "POST" })); drawModUpdates(sid, mod, r || undefined); });
  $("#muall") && ($("#muall").onclick = () => run(null, `Updated ${items.length} mod(s)`));
  $$("[data-mu]", box).forEach((b) => (b.onclick = () => { b.disabled = true; run([b.dataset.mu], "Updated"); }));
  $("#murb") && ($("#murb").onclick = async () => {
    if (!confirm("Put back the versions from before the last update?")) return;
    const r = await act(() => api(`/servers/${sid}/mod-updates/rollback`, { method: "POST" }), "Old versions are back. Restart the server to use them.");
    if (r) tabMods(sid, LIST.find((x) => x.id === sid));
  });
}

async function tabMods(sid, s) {
  if (s.loader === "vanilla") { $("#tab").innerHTML = `<div class="card muted">Vanilla servers don't take mods. Create a Paper, Fabric or NeoForge server instead.</div>`; return; }
  const mod = can("moderator");
  $("#tab").innerHTML = `${mod ? `<div class="row"><label for="mq" class="sr">Search mods</label><input id="mq" placeholder="Search ${esc(LOADER[s.loader] || s.loader)} ${esc(s.mc_version)} ${s.loader === "paper" || s.loader === "purpur" ? "plugins" : "mods"}…" style="flex:1"><button id="mgo">Search</button></div>
    <p class="sub" style="margin:0">Only ${esc(LOADER[s.loader] || s.loader)} builds for ${esc(s.mc_version || "this version")} that run on servers are shown.</p>
    <div class="list" id="mres"></div>` : ""}
    ${(s.auto_disabled || []).length ? `<div class="banner">SpawnPoint turned off ${s.auto_disabled.length} client-only mod(s) that crash servers: ${esc(s.auto_disabled.map((m) => m.name).join(", "))}. Players can still use them in their own game.</div>` : ""}
    <div id="mupd"></div>
    <h4 style="margin:4px 0 0">Installed</h4><div class="list" id="mlist"></div>
    ${["fabric", "quilt", "forge", "neoforge"].includes(s.loader) ? `<div class="card" id="pmods" style="margin-top:6px"></div>` : ""}`;
  drawModUpdates(sid, mod);
  drawPlayerMods(sid, s, mod);
  const drawInstalled = async () => {
    const m = await api(`/servers/${sid}/mods`);
    const extra = (m.managed || []).length ? `<div class="sub">Added on top of the modpack, kept across restarts and pack updates:</div>` +
      m.managed.map((x) => `<div class="item"><div class="grow"><div class="t">${esc(x.title)}</div><div class="d mono">${esc(x.filename)}</div></div>
        ${mod ? `<button class="danger small" data-mp="${esc(x.project_id)}" data-mn="${esc(x.title)}">Remove</button>` : ""}</div>`).join("") +
      `<div class="sub" style="margin-top:8px">Everything in the mods folder:</div>` : "";
    $("#mlist").innerHTML = extra + m.files.map((f) => `<div class="item ${f.endsWith(".disabled") ? "disabled-mod" : ""}"><div class="grow mono" style="font-size:12px;overflow-wrap:anywhere">${esc(f)}</div>
      ${mod ? `${f.endsWith(".disabled") ? `<button class="small" data-f="${esc(f)}" data-x="enable">Turn on</button>` : `<button class="small" data-f="${esc(f)}" data-x="disable">Turn off</button>`}
      <button class="danger small" data-f="${esc(f)}" data-x="delete">Remove</button>` : ""}</div>`).join("") || `<div class="sub">Nothing in /${esc(m.folder)} yet.</div>`;
    $$("[data-mp]", $("#mlist")).forEach((b) => (b.onclick = async () => {
      if (!confirm("Remove " + b.dataset.mn + "? It's deleted from the server at the next restart.")) return;
      await act(() => api(`/servers/${sid}/mods/managed/${encodeURIComponent(b.dataset.mp)}`, { method: "DELETE" }), "Removed. Restart to apply.");
      drawInstalled();
    }));
    $$("[data-x]", $("#mlist")).forEach((b) => (b.onclick = async () => {
      if (b.dataset.x === "delete" && !confirm("Remove " + b.dataset.f + "?")) return;
      await act(() => api(`/servers/${sid}/mods/file`, { method: "POST", body: { filename: b.dataset.f, action: b.dataset.x } }), "Done. Restart to apply.");
      drawInstalled();
    }));
  };
  drawInstalled();
  if (!mod) return;
  const search = async () => {
    $("#mres").innerHTML = `<div class="sub">Searching…</div>`;
    const r = await act(() => api(`/servers/${sid}/mods/search?q=${encodeURIComponent($("#mq").value)}`));
    if (!r) return ($("#mres").innerHTML = "");
    $("#mres").innerHTML = r.map((p, i) => `<div class="item">${p.icon ? `<img src="${esc(p.icon)}" loading="lazy" alt="">` : `<img alt="">`}
      <div class="grow"><div class="t">${esc(p.title)}</div><div class="d">${esc(p.description)}</div>
      ${p.client_side === "required" ? `<div class="d" style="color:var(--warn)">Players need this mod installed too</div>` : ""}</div>
      <button class="small" data-i="${i}">Add</button></div>`).join("") || `<div class="sub">No matching mods for this loader and version.</div>`;
    $$("[data-i]", $("#mres")).forEach((b) => (b.onclick = async () => {
      b.disabled = true; b.textContent = "Adding…";
      const res = await act(() => api(`/servers/${sid}/mods`, { method: "POST", body: { project: r[b.dataset.i].id } }));
      b.textContent = res ? "Added" : "Add"; if (!res) b.disabled = false;
      if (res) { toast(`Added ${res.filename}. Restart to load it.${res.client_side === "required" ? " Players need it too." : ""}`); drawInstalled(); }
    }));
  };
  $("#mgo").onclick = search; $("#mq").onkeydown = (e) => { if (e.key === "Enter") search(); };
  search();
}

// Mods for players only (app/playermods.py): in the player pack, never loaded by the server.
async function drawPlayerMods(sid, s, mod) {
  const box = $("#pmods"); if (!box) return;
  const r = await api(`/servers/${sid}/player-mods`).catch(() => null); if (!r || !$("#pmods")) return;
  box.innerHTML = `<h4 style="margin-top:0">For players only</h4>
    <p class="sub" style="margin-top:0">Mods that only run in players' games, like Voxy for a server with LOD Server Support. They go in the player pack, and the server never loads them. Mods they need come along.</p>
    <div class="list" id="pmlist">${r.mods.map((m) => `<div class="item"><div class="grow"><div class="t">${esc(m.title)}</div>
      <div class="d mono">${esc(m.file)}${m.for ? ` · needed by ${esc(m.for)}` : ""}</div></div>
      ${mod ? `<button class="danger small" data-pmr="${esc(m.file)}" data-pmt="${esc(m.title)}">Remove</button>` : ""}</div>`).join("") || `<div class="sub">None yet.</div>`}</div>
    ${mod ? `<div class="row" style="margin-top:10px"><label for="pmq" class="sr">Search mods for players</label><input id="pmq" placeholder="Search mods for players, like Voxy" style="flex:1"><button id="pmgo">Search</button></div>
      <div class="list" id="pmres"></div>` : ""}`;
  $$("[data-pmr]", box).forEach((b) => (b.onclick = async () => {
    if (!await fmConfirm(`Remove ${b.dataset.pmt} from the player pack? Players' apps take it out at their next sync.`, "Remove")) return;
    if (await act(() => api(`/servers/${sid}/player-mods/${encodeURIComponent(b.dataset.pmr)}`, { method: "DELETE" }), "Removed from the player pack")) drawPlayerMods(sid, s, mod);
  }));
  if (!mod) return;
  const search = async () => {
    const q = $("#pmq").value.trim(); if (!q) return;
    $("#pmres").innerHTML = `<div class="sub">Searching…</div>`;
    const res = await act(() => api(`/servers/${sid}/player-mods/search?q=${encodeURIComponent(q)}`));
    if (!res) return ($("#pmres").innerHTML = "");
    $("#pmres").innerHTML = res.map((p, i) => `<div class="item">${p.icon ? `<img src="${esc(p.icon)}" loading="lazy" alt="">` : `<img alt="">`}
      <div class="grow"><div class="t">${esc(p.title)}</div><div class="d">${esc(p.description)}</div></div>
      <button class="small" data-pi="${i}">Add for players</button></div>`).join("") || `<div class="sub">No matching mods for ${esc(LOADER[s.loader] || s.loader)} ${esc(s.mc_version)}.</div>`;
    $$("[data-pi]", $("#pmres")).forEach((b) => (b.onclick = async () => {
      b.disabled = true; b.textContent = "Adding…";
      const x = await act(() => api(`/servers/${sid}/player-mods`, { method: "POST", body: { project: res[b.dataset.pi].id } }));
      if (!x) { b.disabled = false; b.textContent = "Add for players"; return; }
      toast(`Added to the player pack: ${x.added.map((a) => a.title).join(", ") || "already there"}.${x.missing.length ? ` Not available for this version: ${x.missing.join(", ")}.` : ""}`, !!x.missing.length);
      drawPlayerMods(sid, s, mod);
    }));
  };
  $("#pmgo").onclick = search; $("#pmq").onkeydown = (e) => { if (e.key === "Enter") search(); };
}

async function drawPlayerHistory(sid) {
  const box = $("#phist"); if (!box) return;
  const rows = await api(`/servers/${sid}/player-history`).catch(() => null);
  if (!rows) { box.innerHTML = `<div class="sub">Couldn't load the history.</div>`; return; }
  box.innerHTML = rows.map((p) => `<div class="item"><div class="grow"><div class="t">${esc(p.name)}${p.online ? ` <span class="badge on" style="margin-left:6px">online</span>` : ""}</div>
      <div class="d">${dur(p.play_s)} played over ${p.sessions} visit${p.sessions === 1 ? "" : "s"} · first seen ${ago(p.first)}${p.online ? "" : ` · last seen ${ago(p.last)}`}</div>
      ${p.home ? `<div class="d">Usually around <a href="#" data-go="${p.home.dim},${p.home.x},${p.home.z}" class="mono">${p.home.x}, ${p.home.z}</a>${p.home.dim !== "overworld" ? ` in ${DIMNAME[p.home.dim] || p.home.dim}` : ""}</div>` : ""}</div></div>`).join("")
    || `<div class="sub">Nobody yet. SpawnPoint starts keeping track from the first player who joins.</div>`;
  $$("[data-go]", box).forEach((a) => (a.onclick = (e) => {      // show that spot on the map
    e.preventDefault(); const [d, x, z] = a.dataset.go.split(",");
    MAPDIM[sid] = d; VIEWS[sid + "/" + d] = { x: +x, z: +z, ppb: 1 };
    const s = LIST.find((y) => y.id === sid); if (s) drawMapHead(s); drawMap();
    $("#map")?.scrollIntoView({ behavior: "smooth", block: "center" });
  }));
}
async function tabPlayers(sid, s) {
  const st = s.stats || {}, mod = can("moderator");
  const lists = await api(`/servers/${sid}/lists`); LISTS[sid] = lists;
  const sec = (title, names, actions) => `<h4 style="margin:6px 0 0">${title}</h4><div class="list">${names.map((n) => `<div class="item"><div class="grow">${esc(n)}</div>
      ${mod ? actions.map(([a, l]) => `<button class="small ${a === "ban" ? "danger" : ""}" data-n="${esc(n)}" data-pa="${a}">${l}</button>`).join("") : ""}</div>`).join("") || `<div class="sub">None</div>`}</div>`;
  $("#tab").innerHTML = `<div class="card" style="padding:14px">
      <label class="row" style="color:var(--ink);font-size:15px;margin:0"><input type="checkbox" id="invite" ${lists.invite_only ? "checked" : ""} ${mod ? "" : "disabled"}> <b>Invite only</b></label>
      <p class="sub" style="margin:6px 0 0">Minecraft servers don't have passwords. This is the equivalent: only players on the whitelist can join, and anyone removed from it is kicked.${mod ? " Add yourself first." : ""}</p></div>
    <div class="card" style="padding:14px" id="shared"></div>
    ${mod ? `<div class="row"><label for="pn" class="sr">Player name</label><input id="pn" placeholder="Player name" style="flex:1"></div>
      <div class="row"><button class="small" data-add="whitelist-add">Add to whitelist</button><button class="small" data-add="op">Make operator</button><button class="small danger" data-add="ban">Ban</button></div>` : ""}
    ${sec(`Online now (${st.online ?? 0})`, st.players || [], [["kick", "Kick"], ["op", "Op"]])}
    ${sec("Whitelist", lists.whitelist, [["whitelist-remove", "Remove"]])}
    ${sec("Operators", lists.ops, [["deop", "Remove op"]])}
    ${sec("Banned", lists["banned-players"], [["pardon", "Unban"]])}
    <h4 style="margin:6px 0 0">Everyone who has played</h4><div id="phist" class="list"><div class="sub">Loading…</div></div>`;
  drawShared(sid, s);
  drawPlayerHistory(sid);
  if (!mod) return;
  $("#invite").onchange = async (e) => {
    const r = await act(() => api(`/servers/${sid}/invite-only`, { method: "POST", body: { on: e.target.checked } }));
    if (r) toast(r.on ? (r.applied_now ? "Invite only is on." : "Invite only will apply when the server starts.") : "Anyone with the address can join now.");
  };
  const run = async (name, action) => { await act(() => api(`/servers/${sid}/players`, { method: "POST", body: { name, action } }), "Done"); setTimeout(() => tabPlayers(sid, LIST.find((x) => x.id === sid) || s), 600); };
  $$("[data-add]", $("#tab")).forEach((b) => (b.onclick = () => $("#pn").value && run($("#pn").value.trim(), b.dataset.add)));
  $$("[data-pa]", $("#tab")).forEach((b) => (b.onclick = () => run(b.dataset.n, b.dataset.pa)));
}

// Minecraft version upgrades for plain servers: a trial copy on the new version first.
const UPSTEP = { backup: "Backing up", copy: "Copying the server", mods: "Updating the copy's mods", trial: "Trial run", switch: "Switching over" };
// A bar for a multi-step job: the backup's real percentage while it backs up, a moving bar otherwise.
function jobBar(s, backingUp) {
  const b = s.backup || {};
  if (backingUp && b.status === "running" && b.percent != null)
    return `<div class="row sub" style="justify-content:space-between;margin:6px 0 4px"><span>Backing up</span><span>${Math.round(b.percent)}%</span></div>
      <div class="bar" role="progressbar" aria-label="Backing up" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(b.percent)}"><i class="live" style="width:${Math.max(2, b.percent)}%"></i></div>`;
  return `<div class="bar indet" role="progressbar" aria-label="Working" style="margin:6px 0 4px"><i></i></div>`;
}
// ---- switching between vanilla, Fabric, Paper and Purpur (app/software.py)
const SW_FROM = ["vanilla", "fabric", "quilt", "paper", "purpur"];
const SW_ABOUT = {
  fabric: "For Fabric mods, like Lithium for performance. The world stays exactly as it is, and you can switch back to vanilla later.",
  paper: "Faster, and runs plugins. Players still join with plain Minecraft.",
  purpur: "Paper with extra gameplay options. Runs the same plugins.",
  vanilla: "The plain game. Mods stop loading; they stay in the mods folder in case you switch back.",
};
async function drawSoftwareCard(sid) {
  const box = $("#swcard"); if (!box || box.hidden) return;
  const s = LIST.find((x) => x.id === sid); if (!s) return;
  const w = s.software, prev = s.software_prev, busy = w && ["backup", "switch"].includes(w.step), cur = LOADER[s.kind] || s.kind;
  let status = "";
  if (busy) {
    status = `<p>${esc(w.msg || "")}…</p>${jobBar(s, w.step === "backup")}<ol class="steps"><li class="${w.step === "backup" ? "now" : "done"}">Back up</li><li class="${w.step === "switch" ? "now" : ""}">Switch and restart</li></ol>`;
    setTimeout(async () => { if ($("#swcard") && TAB === "settings" && SEL === sid) { await refreshList(); drawSoftwareCard(sid); } }, 3000);
  } else if (w?.step === "failed") {
    status = `<div class="crashcard"><b>${esc(w.msg)}</b><div class="row" style="justify-content:flex-end"><button class="small" id="swok">OK</button></div></div>`;
  } else if (w?.step === "done") {
    status = `<div class="banner info msg">${esc(w.msg)} ${ago(w.at)}.${w.restarted ? " The first start installs it, which takes a minute." : " It installs at the next start."} <button class="small" id="swok">OK</button></div>`;
  }
  box.innerHTML = `<h4 style="margin-top:0">Server software</h4>
    <p style="margin-top:0">This server runs <b>${esc(cur)}</b>.</p>${status}
    ${busy ? "" : `<div id="swopts" class="sub">Checking what's available for ${esc(s.mc_version)}…</div>`}
    ${prev && !busy ? `<div class="banner info msg" style="margin-top:8px">Switched from ${esc(prev.name)} ${ago(prev.at)}. To go back, undo the switch: the world returns to how it was just before (anything played since is lost). <button class="small" id="swundo">Undo</button></div>` : ""}`;
  $("#swok") && ($("#swok").onclick = async () => { await act(() => api(`/servers/${sid}/software/dismiss`, { method: "POST" })); await refreshList(); drawSoftwareCard(sid); });
  $("#swundo") && ($("#swundo").onclick = async () => {
    if (!await fmConfirm(`Undo the switch and go back to ${prev.name}? The server's files return to the backup made just before, so anything played since is lost.`, "Undo")) return;
    toast("Restoring the backup…");
    if (await act(() => api(`/servers/${sid}/software/undo`, { method: "POST" }), `Back on ${prev.name}`)) { await refreshList(); drawSoftwareCard(sid); }
  });
  if (busy) return;
  const r = await api(`/servers/${sid}/software`).catch(() => null);
  const el = $("#swopts"); if (!el) return;
  if (!r || !r.options.length) { el.textContent = r ? "" : "Couldn't load the options."; return; }
  el.className = "sw-opts";
  el.innerHTML = r.options.map((o) => `<div class="sw-opt"><div><b>${esc(o.name)}</b><div class="sub">${esc(SW_ABOUT[o.kind] || "")}</div>
      ${o.note ? `<div class="sub" style="color:${o.ok ? "var(--warn)" : "var(--mute)"}">${esc(o.note)}</div>` : ""}</div>
      <button class="small" data-sw="${esc(o.kind)}" ${o.ok ? "" : "disabled"}>Switch to ${esc(o.name)}</button></div>`).join("");
  $$("[data-sw]", el).forEach((b) => (b.onclick = () => softwareModal(sid, s, r.options.find((o) => o.kind === b.dataset.sw))));
}
function softwareModal(sid, s, o) {
  const paperish = ["paper", "purpur"].includes(o.kind) && s.kind === "vanilla";
  const running = s.phase === "online" || s.phase === "starting";
  modal(`<h2>Switch to ${esc(o.name)}?</h2>
    <p class="muted" style="margin-top:0">From ${esc(LOADER[s.kind] || s.kind)} ${esc(s.mc_version)}. The world comes along.</p>
    <ul class="tight">
      <li>A backup is made first${running ? ", then the server restarts" : ""}.</li>
      ${paperish ? `<li>${esc(o.name)} moves the Nether and the End into their own world folders the first time it starts. Going back to vanilla means undoing the switch, which restores the backup.</li>` : ""}
      ${o.kind === "vanilla" ? "<li>Mods stop loading. They stay in the mods folder.</li>" : ""}
      ${["paper", "purpur"].includes(o.kind) ? `<li>Add plugins in the Mods tab afterwards.</li>` : o.kind === "fabric" ? "<li>Add Fabric mods in the Mods tab afterwards. Server-side mods don't need anything on players' computers.</li>" : ""}
      <li>Undo puts the world and the old software back from the backup.</li>
    </ul>
    ${o.note ? `<p style="color:var(--warn)">${esc(o.note)}</p>` : ""}
    <div class="row" style="justify-content:flex-end;margin-top:12px"><button class="ghost" onclick="closeModal()">Cancel</button><button class="primary" id="swgo">Switch to ${esc(o.name)}</button></div>`);
  $("#swgo").onclick = async () => {
    $("#swgo").disabled = true;
    const r = await act(() => api(`/servers/${sid}/software`, { method: "POST", body: { target: o.kind } }));
    closeModal(); if (r) { await refreshList(); drawSoftwareCard(sid); }
  };
}

async function drawUpgradeCard(sid) {
  const box = $("#upcard"); if (!box || box.hidden) return;
  const s = LIST.find((x) => x.id === sid); if (!s) return;
  const u = s.upgrade, prev = s.upgrade_prev, busy = u && !["done", "failed"].includes(u.step);
  let body = "";
  if (busy) {
    const steps = Object.keys(UPSTEP), i = steps.indexOf(u.step);
    body = `<p>${esc(u.msg || "")}</p>${jobBar(s, u.step === "backup")}<ol class="steps">${steps.map((k, j) => `<li class="${j < i ? "done" : j === i ? "now" : ""}">${UPSTEP[k]}</li>`).join("")}</ol>
      <p class="sub">The server keeps running until the trial copy has started on ${esc(u.target)}; only then is it stopped and switched.</p>`;
    setTimeout(async () => { if ($("#upcard") && TAB === "settings" && SEL === sid) { await refreshList(); drawUpgradeCard(sid); } }, 4000);
  } else if (u?.step === "failed") {
    const ex = u.result?.explain || {};
    body = `<div class="crashcard"><b>${esc(u.msg)}</b>${ex.title ? `<p>${esc(ex.title)}${ex.mod ? ` (${esc(ex.mod)})` : ""}. ${esc(ex.detail || "")}</p>${ex.hint ? `<p class="sub">${esc(ex.hint)}</p>` : ""}` : ""}
      ${u.result?.tail?.length ? `<details><summary class="sub">End of the trial's log</summary><pre class="console" style="max-height:220px;white-space:pre-wrap">${esc(u.result.tail.join("\n"))}</pre></details>` : ""}</div>
      <div class="row" style="justify-content:flex-end;margin-top:8px"><button class="small" id="upok">OK</button></div>`;
  } else if (u?.step === "done") {
    body = `<div class="banner info msg">Upgraded from ${esc(u.from)} to ${esc(u.target)} ${ago(u.at)}.${u.result?.changed?.length ? ` Mods: ${esc(u.result.changed.join("; "))}.` : ""} <button class="small" id="upok">OK</button></div>`;
  }
  box.innerHTML = `<h4 style="margin-top:0">Minecraft version</h4>
    <div class="row" style="justify-content:space-between;flex-wrap:wrap;gap:8px"><div>This server runs <b>${esc(LOADER[s.loader] || s.loader)} ${esc(s.mc_version)}</b></div>
    ${busy ? "" : `<div class="row" style="gap:8px"><label for="upv" class="sr">New version</label><select id="upv" style="width:auto"><option>Loading…</option></select><button class="small" id="upchk">Check</button></div>`}</div>
    ${body}
    ${!busy ? `<p class="sub">Upgrading backs up first, then starts a copy of the server on the new version (with its mods updated). The real server is only switched over if the copy starts cleanly. Worlds can't be moved back to an older version, except by rolling back to that backup.</p>` : ""}
    ${prev && !busy ? `<div class="banner info msg" style="margin-top:8px">Upgraded to ${esc(prev.to)} ${ago(prev.at)}. To go back to ${esc(prev.mc_version)}, roll back: the world and mods return to how they were just before (anything since is lost). <button class="small" id="uprb">Roll back</button></div>` : ""}`;
  $("#upok") && ($("#upok").onclick = async () => { await act(() => api(`/servers/${sid}/upgrade/dismiss`, { method: "POST" })); await refreshList(); drawUpgradeCard(sid); });
  $("#uprb") && ($("#uprb").onclick = async () => {
    if (!confirm(`Roll back to ${prev.mc_version}? The world, mods and version go back to just before the upgrade.`)) return;
    if (await act(() => api(`/servers/${sid}/upgrade/rollback`, { method: "POST" }), `Rolled back to ${prev.mc_version}`)) { await refreshList(); drawUpgradeCard(sid); }
  });
  if (busy || !$("#upv")) return;
  const vs = (await api("/versions").catch(() => [])).filter((v) => cmpVer(v, s.mc_version) > 0);
  $("#upv").innerHTML = vs.length ? vs.map((v) => `<option>${esc(v)}</option>`).join("") : `<option value="">Already on the newest</option>`;
  $("#upchk").disabled = !vs.length;
  $("#upchk").onclick = async () => {
    const target = $("#upv").value; if (!target) return;
    $("#upchk").disabled = true; $("#upchk").textContent = "Checking…";
    const p = await act(() => api(`/servers/${sid}/upgrade/plan?target=${encodeURIComponent(target)}`));
    $("#upchk").disabled = false; $("#upchk").textContent = "Check";
    if (p) upgradeModal(sid, p);
  };
}
function cmpVer(a, b) {
  const pa = String(a).split(/[.-]/).map((n) => parseInt(n, 10) || 0), pb = String(b).split(/[.-]/).map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) { const d = (pa[i] || 0) - (pb[i] || 0); if (d) return d; }
  return 0;
}
function upgradeModal(sid, p) {
  const by = (st) => p.mods.filter((m) => m.status === st);
  const upd = by("update"), none = by("none"), unk = by("unknown"), same = by("same");
  const li = (ms, f) => ms.map((m) => `<li>${f(m)}</li>`).join("");
  modal(`<h2>Upgrade to ${esc(p.target)}?</h2>
    <p class="muted">From ${esc(p.current)}.${p.java !== p.java_now ? ` The server moves to ${esc(p.java.replace("java", "Java "))}.` : ""}</p>
    ${p.mods.length ? `<div style="max-height:46vh;overflow:auto;display:grid;gap:10px">
      ${upd.length ? `<div><b>${upd.length} will be updated</b><ul class="sub tight">${li(upd, (m) => `${esc(m.title)} ${esc(m.from)} → ${esc(m.to)}`)}</ul></div>` : ""}
      ${none.length ? `<div><b style="color:var(--warn)">${none.length} have no build for ${esc(p.target)}</b><ul class="sub tight">${li(none, (m) => esc(m.title))}</ul>
        <p class="sub">${p.missing_off ? "They'll be turned off, since mods made for another version usually stop the server starting. You can turn them back on in the Mods tab once they're updated." : "Plugins often still work on newer versions, so they're left on; the trial run shows whether they load."}</p></div>` : ""}
      ${unk.length ? `<div><b>${unk.length} aren't from Modrinth</b><ul class="sub tight">${li(unk, (m) => `<span class="mono">${esc(m.file)}</span>`)}</ul><p class="sub">These are left as they are; the trial run shows whether they work.</p></div>` : ""}
      ${same.length ? `<p class="sub">${same.length} already work on ${esc(p.target)}.</p>` : ""}</div>` : ""}
    ${p.fits ? "" : `<p style="color:var(--bad)">There isn't enough free disk for the trial copy (${(p.size / 2 ** 30).toFixed(1)} GB plus room to spare).</p>`}
    <p class="sub">Backs up first. The server keeps running during the trial and is only switched over if the copy starts.</p>
    <div class="row" style="justify-content:flex-end"><button class="ghost" onclick="closeModal()">Cancel</button><button class="primary" id="upgo" ${p.fits ? "" : "disabled"}>Upgrade</button></div>`);
  $("#upgo").onclick = async () => {
    $("#upgo").disabled = true;
    const r = await act(() => api(`/servers/${sid}/upgrade`, { method: "POST", body: { target: p.target, turn_off_missing: true } }), `Upgrading to ${p.target}: backing up first`);
    closeModal(); if (r) { await refreshList(); drawUpgradeCard(sid); }
  };
}

// Discord chat bridge: webhook for game -> Discord, a shared bot for Discord -> game.
async function drawChatCard(sid, c) {
  const box = $("#chatcard"); if (!box) return;
  c = c || await api(`/servers/${sid}/chat-bridge`).catch(() => null);
  if (!c) { box.remove(); return; }
  box.innerHTML = `<h4 style="margin-top:0">Discord chat</h4>
    <label class="row" style="color:var(--ink)"><input type="checkbox" id="cbon" ${c.on ? "checked" : ""}> Connect this server's chat to a Discord channel</label>
    <div style="${c.on ? "" : "opacity:.55"}">
    <label for="cbwh">Channel webhook (game → Discord)</label>
    <input id="cbwh" type="password" autocomplete="off" placeholder="${c.has_webhook ? "Saved. Paste a new one to replace it" : "https://discord.com/api/webhooks/…"}">
    <p class="sub">In Discord: channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.</p>
    <div class="row" style="flex-wrap:wrap;gap:6px 18px">${Object.entries(c.event_names).map(([k, l]) => `<label class="row" style="color:var(--ink)"><input type="checkbox" data-cbe="${k}" ${c.events[k] ? "checked" : ""}> ${esc(l)}</label>`).join("")}</div>
    <label class="row" style="color:var(--ink);margin-top:12px"><input type="checkbox" id="cbtg" ${c.to_game ? "checked" : ""}> Show Discord messages in game</label>
    <label for="cbch">Channel ID (Discord → game)</label><input id="cbch" class="mono" value="${esc(c.channel || "")}" placeholder="1234567890123456789">
    <p class="sub">Turn on Developer Mode in Discord's settings, then right-click the channel → Copy Channel ID. This direction needs a bot: ${c.has_bot ? "one is set up." : can("admin") ? "add one below." : "an admin has to add one."}</p>
    ${can("admin") ? `<details ${c.has_bot ? "" : "open"} style="margin-top:6px"><summary class="sub">Discord bot (shared by all servers)</summary>
      <p class="sub">At discord.com/developers: New Application → Bot → Reset Token and copy it, turn on <b>Message Content Intent</b>, then OAuth2 → URL Generator with the <i>bot</i> scope and the View Channels and Read Message History permissions, and open that link to add the bot to your Discord server.</p>
      <div class="row" style="flex-wrap:nowrap"><input id="cbbot" type="password" autocomplete="off" style="flex:1" placeholder="${c.has_bot ? "Saved. Paste a new token to replace it" : "Bot token"}" aria-label="Bot token">
      <button class="small" id="cbbs">Save bot</button>${c.has_bot ? `<button class="small danger" id="cbbr">Remove</button>` : ""}</div></details>` : ""}
    </div>
    ${c.status ? `<p class="sub" style="color:var(--warn)">${esc(c.status)}</p>` : ""}
    <div class="row" style="justify-content:flex-end;gap:8px;margin-top:12px"><button class="small" id="cbt">Send a test</button><button class="primary small" id="cbs">Save</button></div>`;
  const body = () => ({ on: $("#cbon").checked, channel: $("#cbch").value, to_game: $("#cbtg").checked,
    events: Object.fromEntries($$("[data-cbe]", box).map((x) => [x.dataset.cbe, x.checked])),
    ...($("#cbwh").value.trim() ? { webhook: $("#cbwh").value.trim() } : {}) });
  $("#cbs").onclick = async () => { const r = await act(() => api(`/servers/${sid}/chat-bridge`, { method: "PUT", body: body() }), "Discord chat saved"); if (r) drawChatCard(sid, r); };
  $("#cbon").onchange = () => $("#cbs").click();
  $("#cbt").onclick = async () => {
    const saved = await act(() => api(`/servers/${sid}/chat-bridge`, { method: "PUT", body: body() })); if (!saved) return;
    const r = await act(() => api(`/servers/${sid}/chat-bridge/test`, { method: "POST" })); if (!r) return;
    const say = (x, ok, none) => x === "ok" ? ok : x ? x : none;
    toast(`${say(r.posted, "Posted a test message to Discord.", "No webhook set, so nothing was posted.")} ${say(r.read, "The bot can read the channel.", "")}`.trim(),
      (r.posted && r.posted !== "ok") || (r.read && r.read !== "ok"));
    drawChatCard(sid);
  };
  $("#cbbs") && ($("#cbbs").onclick = async () => { if (!$("#cbbot").value.trim()) return; if (await act(() => api(`/chat-bridge/bot`, { method: "PUT", body: { token: $("#cbbot").value.trim() } }), "Bot saved")) drawChatCard(sid); });
  $("#cbbr") && ($("#cbbr").onclick = async () => { if (confirm("Remove the bot? Discord messages stop reaching every server.") && await act(() => api(`/chat-bridge/bot`, { method: "PUT", body: { token: "" } }), "Bot removed")) drawChatCard(sid); });
}

// Player pack: a page where people download what they need to join (Prism instance or a zip).
const mbs = (n) => `${Math.max(1, Math.round(n / 2 ** 20))} MB`;
// What's in a player pack. Counting scans every file of the pack, so it loads after the rest.
function packCounts(p) {
  const n = (k, w) => `${k} ${w}${k === 1 ? "" : "s"}`;
  return `<p class="sub">${n(p.mods, "mod")} and ${n(p.count - p.mods, "other file")}, ${mbs(p.size)}.${p.left_out.length ? ` Left out: ${p.left_out.length}.` : ""}</p>
    ${p.left_out.length ? `<details><summary class="sub">What's left out, and why</summary><ul class="sub" style="margin:6px 0 0 18px">${p.left_out.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}`;
}
// Servers running a CurseForge/FTB/Modrinth pack hand out its official client version,
// prepared on the server once per version.
function officialNote(o) {
  if (!o || o.state === "none") return "";
  if (o.state === "ready") return `<p class="sub">Players get the modpack's official client version (its theming and client-side mods), plus mods you added in the Mods tab.${o.missing?.length ? ` ${o.missing.length} file(s) couldn't be downloaded.` : ""}</p>`;
  if (o.state === "error") return `<p class="sub" style="color:var(--warn)">Couldn't prepare the modpack's player files: ${esc(o.error || "")}. It's tried again in a few minutes.</p>`;
  return `<p class="sub">Getting the modpack's official player files ready on the server${o.total ? `: ${o.done} of ${o.total}` : "…"}. The downloads work once that's done.</p>`;
}
async function fillPackCounts(sid, el) {
  const d = await api(`/servers/${sid}/player-pack?details=true`).catch(() => null);
  if (!el?.isConnected || !d) return;
  if (d.mods !== undefined) { el.innerHTML = officialNote(d.official) + packCounts(d); return; }
  el.innerHTML = officialNote(d.official);
  if (d.official && !["ready", "none"].includes(d.official.state)) setTimeout(() => fillPackCounts(sid, el), 5000);
}

async function drawPlayerPack(sid, p) {
  const box = $("#ppcard"); if (!box || box.hidden) return;
  p = p || await api(`/servers/${sid}/player-pack?details=false`).catch(() => null);
  if (!p) { box.remove(); return; }
  const guess = location.origin.startsWith("http://localhost") || /^http:\/\/\d/.test(location.origin) ? "" : location.origin;
  box.innerHTML = `<h4 style="margin-top:0">Player pack</h4>
    <label class="row" style="color:var(--ink)"><input type="checkbox" id="ppon" ${p.enabled ? "checked" : ""}> Give players a download page for this server's mods</label>
    <p class="sub">The page offers a Prism Launcher instance that updates itself every time they play, a file for the Modrinth App, and a zip for any other launcher. Anyone with the link can download, nothing else.</p>
    <label for="ppbase">Address players open SpawnPoint at</label>
    <input id="ppbase" class="mono" value="${esc(p.base_url || guess)}" placeholder="https://spawnpoint.example.com">
    <p class="sub">The page and Prism's updates are fetched from here, so it has to be reachable from the internet (a Pangolin HTTP resource, for example). Only the /p/ link is public; the rest still needs a sign-in.</p>
    ${p.enabled ? `
      <div class="row" style="flex-wrap:nowrap;margin-top:6px"><input class="mono" readonly style="flex:1" value="${esc(p.page || "Save the address above to get the link")}" aria-label="Player pack link">
      ${p.page ? `<button class="small" id="ppcopy">Copy</button>` : ""}<button class="small" id="ppnew">New link</button></div>
      <div id="ppcount">${p.mods !== undefined ? packCounts(p) : `<p class="sub">Counting what's in it…</p>`}</div>
      <p class="sub" style="margin-top:10px">The Prism Launcher download updates itself every time players press Play. The Modrinth App file and the zip are the files as they are now: players download them again after you change mods. Nothing is added to the server for any of this.</p>` : ""}
    <p class="sub" style="color:var(--warn)">Mods belong to their authors and many licences don't allow sharing them elsewhere. Keep the link to your own players. For a public modpack server, point people to the official pack instead.</p>
    <div class="row" style="justify-content:flex-end;gap:8px;margin-top:10px">${p.page ? `<a class="btn small" href="${esc(p.page)}" target="_blank" rel="noopener">Open the page</a>` : ""}<button class="primary small" id="ppsave">Save</button></div>`;
  const save = async (extra = {}, msg = "Player pack saved") => {
    const r = await act(() => api(`/servers/${sid}/player-pack`, { method: "PUT",
      body: { enabled: $("#ppon").checked, base_url: $("#ppbase").value, ...extra } }), msg);
    if (r) drawPlayerPack(sid, r);
  };
  $("#ppsave").onclick = () => save();
  $("#ppon").onchange = () => save();
  $("#ppnew") && ($("#ppnew").onclick = () => confirm("Make a new link? The old one stops working, and Prism instances made from it stop updating.") && save({ new_link: true }, "New link made"));
  $("#ppcopy") && ($("#ppcopy").onclick = () => navigator.clipboard.writeText(p.page).then(() => toast("Link copied")));
  if (p.enabled && p.mods === undefined) fillPackCounts(sid, $("#ppcount"));
}

// The same link from the server's page: a floating card to copy or open it.
async function openPlayerPack(sid) {
  const s = LIST.find((x) => x.id === sid) || {};
  modal(`<h2>Player pack</h2><p class="muted">Loading…</p>`);
  const p = await api(`/servers/${sid}/player-pack?details=false`).catch((e) => ({ error: e.message }));
  if (p.error) { modal(`<h2>Player pack</h2><p class="banner bad">${esc(p.error)}</p>`); return; }
  modal(`<h2>Player pack for ${esc(s.name || sid)}</h2>
    ${p.page ? `<p class="muted" style="margin-top:0">Send this link to your players. It has everything they need to join: a Prism Launcher instance that updates itself, a Modrinth App file, and a zip for any launcher.</p>
      <div class="row" style="flex-wrap:nowrap"><input class="mono" id="pplink" readonly style="flex:1" value="${esc(p.page)}" aria-label="Player pack link">
      <button class="primary small" id="ppmcopy">Copy</button><a class="btn small" href="${esc(p.page)}" target="_blank" rel="noopener">Open</a></div>`
      : `<p class="banner">The link needs the address players open SpawnPoint at. Set it in Settings → Player pack.</p>`}
    <div id="ppmcount" style="margin-top:12px"><p class="sub">Counting what's in it…</p></div>
    <p class="sub">The Prism Launcher download updates itself every time players press Play; with the other downloads, players download again after mod changes.</p>
    <div class="row" style="justify-content:space-between;margin-top:14px">${can("moderator") ? `<button class="ghost small" id="ppmset">Settings</button>` : "<span></span>"}<button class="small" onclick="closeModal()">Close</button></div>`);
  $("#ppmcopy") && ($("#ppmcopy").onclick = () => navigator.clipboard.writeText(p.page).then(() => toast("Link copied")));
  $("#pplink")?.addEventListener("focus", (e) => e.target.select());
  $("#ppmset") && ($("#ppmset").onclick = () => { closeModal(); location.hash = `#/s/${sid}/settings`; });
  fillPackCounts(sid, $("#ppmcount"));
}

const COMMON_PROPS = ["motd", "difficulty", "gamemode", "max-players", "white-list", "pvp", "view-distance",
  "simulation-distance", "spawn-protection", "allow-flight", "online-mode", "level-seed", "hardcore"];
async function tabSettings(sid, s) {
  const props = await api(`/servers/${sid}/properties`);
  const map = Object.fromEntries(props);
  const prefix = s.hostname.split(".")[0];
  const up = s.pack_update, prev = s.pack_prev;
  const packCard = !s.pack ? "" : `<div class="card"><h4 style="margin-top:0">Modpack</h4>
      <div class="row" style="justify-content:space-between;flex-wrap:wrap">
        <div><div>${esc(s.pack.title || "Modpack")} <b>${esc(s.pack.version_name || "")}</b></div>
        <div class="sub">${up ? `Version <b>${esc(up.name || "newer")}</b> is available${up.mc_version && up.mc_version !== s.mc_version ? ` (Minecraft ${esc(up.mc_version)})` : ""}.` : s.pack_checked ? `Up to date, checked ${ago(s.pack_checked)}.` : "Not checked for updates yet."}</div></div>
        <div class="row">${up ? `<button class="primary" id="pku">Update</button>` : `<button class="small" id="pkc">Check now</button>`}</div></div>
      <p class="sub">The pack stays on this version until you update it, so restarts never change it. Updating backs up first, then restarts the server${up && up.mc_version && up.mc_version !== s.mc_version ? " (this update changes the Minecraft version: some worlds and mods don't survive that, so the backup matters)" : ""}.</p>
      ${prev ? `<div class="banner info msg" style="margin-top:8px">Updated to ${esc(prev.to || "a new version")} ${ago(prev.at)}. If something broke, roll back to put the world, mods and version back to how they were just before. <button class="small" id="pkr">Roll back</button></div>` : ""}</div>`;
  const pm = s.public_map || {}, link = pm.token ? `${location.origin}/m/${pm.token}` : "";
  const pubCard = `<div class="card"><h4 style="margin-top:0">Public map</h4>
      <label class="row" style="color:var(--ink)"><input type="checkbox" id="pme" ${pm.enabled ? "checked" : ""}> Share a view-only map</label>
      <p class="sub" style="margin-top:4px">Anyone with the link can see it, no account needed.</p>
      ${pm.enabled && link ? `<div class="row" style="flex-wrap:nowrap;margin-top:10px"><input class="mono" id="pml" readonly value="${esc(link)}" style="flex:1" aria-label="Public map link">
        <button class="small" id="pmc">Copy</button><a class="btn small" href="${esc(link)}" target="_blank" rel="noopener">Open</a></div>` : ""}
      <div class="row" style="flex-wrap:wrap;gap:6px 18px;margin-top:10px;${pm.enabled ? "" : "opacity:.5"}">
        <label class="row" style="color:var(--ink)"><input type="checkbox" id="pmp" ${pm.players !== false ? "checked" : ""} ${pm.enabled ? "" : "disabled"}> Show players (live, with names)</label>
        <label class="row" style="color:var(--ink)"><input type="checkbox" id="pms" ${pm.spawn !== false ? "checked" : ""} ${pm.enabled ? "" : "disabled"}> Show spawn</label></div>
      <p class="sub">Shows the terrain only: no console, settings or anything else. The link works wherever this dashboard can be reached, so to share it outside your network the dashboard needs a public address (see the README). ${pm.enabled ? `<a href="#" id="pmn">Make a new link</a> to stop the old one working.` : ""}</p></div>`;
  const bd = s.bedrock || {}, canBedrock = ["paper", "purpur", "fabric", "neoforge"].includes(s.loader);
  const bedCard = `<div class="card"><h4 style="margin-top:0">Bedrock players</h4>
      <label class="row" style="color:var(--ink)"><input type="checkbox" id="bde" ${bd.enabled ? "checked" : ""} ${canBedrock || bd.enabled ? "" : "disabled"}> Let Bedrock players join (phones, consoles, Windows)</label>
      ${bd.enabled ? `<p style="margin:8px 0 0">In Minecraft Bedrock: Servers → Add Server, address <b class="mono">${esc(s.hostname)}</b>, port <b class="mono">${bd.port}</b>.</p>
        ${bd.note ? `<p class="sub" style="color:var(--warn)">${esc(bd.note)}</p>` : ""}
        <p class="sub">${s.restart_pending ? "Restart the server to finish turning it on. " : ""}UDP port ${bd.port} has to reach this machine: forward it on your router (or add a UDP resource in Pangolin).</p>`
      : `<p class="sub">${canBedrock ? "Adds Geyser and Floodgate. Geyser exists for Paper and Purpur 1.20.5+, and for Fabric and NeoForge only on the newest Minecraft release, so most modpacks can't use it. Bedrock players get their own UDP port." : "Geyser doesn't run on this server type (it needs Paper, Purpur, Fabric or NeoForge)."}</p>`}</div>`;
  $("#tab").innerHTML = packCard + `<div class="card" id="upcard" ${!s.pack && s.mc_version && s.mc_version.toUpperCase() !== "LATEST" ? "" : "hidden"}></div>`
    + `<div class="card" id="swcard" ${!s.pack && SW_FROM.includes(s.kind) && s.mc_version && s.mc_version.toUpperCase() !== "LATEST" ? "" : "hidden"}></div>` + pubCard + bedCard + `<div class="card" id="ppcard" ${s.loader && !["vanilla", "paper", "purpur"].includes(s.loader) ? "" : "hidden"}></div>` + `<div class="card" id="chatcard"></div>` + `<div class="card"><h4 style="margin-top:0">Server</h4>
      <label for="sn">Display name</label><input id="sn" value="${esc(s.name)}">
      <label for="hp">Address</label><div class="row" style="flex-wrap:nowrap"><input id="hp" value="${esc(prefix)}" style="flex:1"><span class="sub mono">.${esc(INFO.base_domain)}</span></div>
      <label for="as">Sleep after this many empty minutes (0 = never)</label><input id="as" type="number" min="0" value="${s.auto_sleep_min || 0}">
      <p class="sub">While asleep it shows "join to wake it" in the server list, and the first player who tries to join starts it.</p>
      <label class="row" style="color:var(--ink);margin-top:12px"><input type="checkbox" id="ar" ${s.auto_restart === false ? "" : "checked"}> Restart automatically after a crash</label>
      <p class="sub">Covers crashes, freezes caught by the watchdog and running out of memory. After 3 crashes in 15 minutes it stops trying, so a broken mod can't loop forever.${s.last_crash ? ` Last crash ${ago(s.last_crash.at)}: ${esc(s.last_crash.reason)}.` : ""}</p>
      <h4>Memory</h4>
      <label class="row" style="color:var(--ink)"><input type="checkbox" id="am" ${s.memory.auto ? "checked" : ""}> Size automatically from the mod count</label>
      <div class="row" style="flex-wrap:nowrap"><div style="flex:1"><label for="ig">Start (GB)</label><input id="ig" type="number" value="${s.memory.init_gb}"></div>
      <div style="flex:1"><label for="sg">Normal (GB)</label><input id="sg" type="number" value="${s.mem_normal_gb ?? s.memory.max_gb}"></div>
      <div style="flex:1"><label for="mg">Can grow to</label><input id="mg" type="number" value="${s.memory.max_gb}"></div></div>
      <p class="sub">The server starts small, stays around the normal size, hands unused memory back to the machine when it's quiet, and grows up to the higher number when it needs more, without a restart.</p>
      <label for="jt">Java image</label><select id="jt">${["java25", "java21", "java17", "java8", "latest"].map((t) => `<option ${t === s.java_tag ? "selected" : ""}>${t}</option>`).join("")}</select>
      <h4>Real player IPs</h4>
      <label class="row" style="color:var(--ink)"><input type="checkbox" id="rip" ${s.real_ip_disabled ? "" : "checked"}> Pass real player IPs to this server</label>
      <p class="sub">Status: ${s.real_ip ? "on (" + esc(s.real_ip_via) + ")" : esc(s.real_ip_via || "set up at next start")}. Needs Proxy Protocol turned on for the TCP resource in Pangolin. Takes effect after a restart.</p>
      <label for="fr">Pre-generate out to (blocks)</label><input id="fr" type="number" value="${s.pregen.full_radius}">
      <label class="row" style="color:var(--ink);margin-top:10px"><input type="checkbox" id="pwp" ${s.pregen_pause_when_online ? "checked" : ""}> Pause pre-generation whenever someone is online</label>
      <p class="sub">Off by default: pre-generation keeps going while people play and pauses by itself when the slowest ticks go over ${INFO?.mspt_pause ?? 40} ms. Turn this on to only generate while the server is empty.</p>
      <div class="row" style="justify-content:space-between;margin-top:16px">
        ${can("admin") ? `<button class="danger" id="del">Delete server…</button>` : "<span></span>"}<button class="primary" id="sv">Save</button></div></div>
    <div class="card"><h4 style="margin-top:0">server.properties</h4><div class="kv">
      ${COMMON_PROPS.filter((k) => k in map).map((k) => `<div>${k}</div><div><input aria-label="${k}" data-k="${k}" value="${esc(map[k])}"></div>`).join("")}</div>
      <details style="margin-top:12px"><summary class="sub">All properties</summary><div class="kv" style="margin-top:10px">
      ${props.filter(([k]) => !COMMON_PROPS.includes(k)).map(([k, v]) => `<div style="overflow-wrap:anywhere">${esc(k)}</div><div><input aria-label="${esc(k)}" data-k="${esc(k)}" value="${esc(v)}"></div>`).join("")}</div></details>
      <div class="row" style="justify-content:flex-end;margin-top:14px"><button class="primary" id="sp">Save properties</button></div></div>`;
  drawChatCard(sid);
  drawPlayerPack(sid);
  drawUpgradeCard(sid);
  drawSoftwareCard(sid);
  const savePub = async (extra = {}) => {
    const r = await act(() => api(`/servers/${sid}/public-map`, { method: "PUT",
      body: { enabled: $("#pme").checked, players: $("#pmp").checked, spawn: $("#pms").checked, ...extra } }));
    if (r) { const x = LIST.find((y) => y.id === sid); if (x) x.public_map = r; tabSettings(sid, x || s); }
  };
  ["#pme", "#pmp", "#pms"].forEach((id) => $(id) && ($(id).onchange = () => savePub()));
  $("#bde") && ($("#bde").onchange = async () => {
    $("#bde").disabled = true;
    const r = await act(() => api(`/servers/${sid}/bedrock`, { method: "PUT", body: { enabled: $("#bde").checked } }),
      $("#bde").checked ? "Bedrock players turned on. Restart the server to finish." : "Bedrock players turned off. Restart to remove Geyser.");
    await refreshList(); const x = LIST.find((y) => y.id === sid); tabSettings(sid, x || s);
  });
  $("#pmn") && ($("#pmn").onclick = (e) => { e.preventDefault(); if (confirm("Make a new link? The old one stops working.")) savePub({ new_link: true }); });
  $("#pmc") && ($("#pmc").onclick = () => { navigator.clipboard?.writeText($("#pml").value).then(() => toast("Link copied"), () => { $("#pml").select(); toast("Press Ctrl+C to copy"); }); });
  $("#pkc") && ($("#pkc").onclick = async () => {
    $("#pkc").disabled = true; $("#pkc").textContent = "Checking…";
    const r = await act(() => api(`/servers/${sid}/pack/check`, { method: "POST" }));
    if (r) { toast(r.update ? `Version ${r.update.name || "update"} is available` : "Already on the newest version"); await refreshList(); railKey = ""; drawRail(); }
  });
  $("#pku") && ($("#pku").onclick = () => {
    modal(`<h2>Update ${esc(s.pack.title || "the modpack")} to ${esc(up.name || "the newest version")}?</h2>
      <p class="muted">SpawnPoint backs up the server first, then switches to the new version${running(s) ? " and restarts it. Players are disconnected while it downloads and starts, which can take several minutes" : ". It's installed at the next start"}. You can roll back afterwards.</p>
      <div class="row" style="justify-content:flex-end;margin-top:16px"><button class="ghost" onclick="closeModal()">Cancel</button><button class="primary" id="pkgo">Back up and update</button></div>`);
    $("#pkgo").onclick = async () => { $("#pkgo").disabled = true; $("#pkgo").textContent = "Backing up…";
      const r = await act(() => api(`/servers/${sid}/pack/update`, { method: "POST" })); closeModal();
      if (r) { toast(r.restarted ? "Backed up. Restarting with the new version." : "Backed up. The new version installs at the next start."); refreshList(); } };
  });
  $("#pkr") && ($("#pkr").onclick = () => {
    modal(`<h2>Roll back the update?</h2><p class="muted">The server's files are put back exactly as they were just before the update, including the world: anything built since then is lost. The current files are kept in a <span class="mono">.pre-restore</span> folder.</p>
      <div class="row" style="justify-content:flex-end;margin-top:16px"><button class="ghost" onclick="closeModal()">Cancel</button><button class="danger" id="pkrgo">Roll back</button></div>`);
    $("#pkrgo").onclick = async () => { $("#pkrgo").disabled = true; $("#pkrgo").textContent = "Rolling back…";
      const r = await act(() => api(`/servers/${sid}/pack/rollback`, { method: "POST" }), "Rolled back"); closeModal(); if (r) refreshList(); };
  });
  $("#sv").onclick = async () => {
    const body = { name: $("#sn").value, hostname_prefix: $("#hp").value, auto_sleep_min: +$("#as").value, auto_restart: $("#ar").checked, pregen_pause_when_online: $("#pwp").checked,
      auto_memory: $("#am").checked, java_tag: $("#jt").value, full_radius: +$("#fr").value, real_ip: $("#rip").checked };
    if (!$("#am").checked) { body.init_gb = +$("#ig").value; body.soft_gb = +$("#sg").value; body.max_gb = +$("#mg").value; }
    const r = await act(() => api(`/servers/${sid}`, { method: "PATCH", body }));
    if (r) { toast(r.restart_needed ? "Saved. Restart to apply memory and Java changes." : "Saved"); refreshList(); }
  };
  $("#sp").onclick = async () => {
    const values = {}; $$("[data-k]", $("#tab")).forEach((i) => (values[i.dataset.k] = i.value));
    await act(() => api(`/servers/${sid}/properties`, { method: "PUT", body: values }), "Saved. Restart to apply.");
  };
  $("#del") && ($("#del").onclick = () => {
    modal(`<h2>Delete ${esc(s.name)}?</h2><p class="muted">The server is removed from SpawnPoint. Choose whether to keep its world files on disk.</p>
      <label class="row" style="color:var(--ink)"><input type="checkbox" id="df"> Also delete the world and files (can't be undone)</label>
      <div class="row" style="justify-content:flex-end;margin-top:16px"><button class="ghost" onclick="closeModal()">Cancel</button><button class="danger" id="dgo">Delete server</button></div>`);
    $("#dgo").onclick = async () => { await act(() => api(`/servers/${sid}?files=${$("#df").checked}`, { method: "DELETE" })); closeModal(); location.hash = "#/"; refreshList(); };
  });
}

// ---------------------------------------------------------------- Files: a file manager for the server's folder
// Everything goes through /api/servers/<id>/files/… (app/files.py), which keeps paths inside
// the server's folder. Deleting moves things to Recently deleted for 7 days.
const FM = { sid: null, path: "", entries: [], sel: new Set(), last: null, sort: ["name", 1], results: null, q: "",
  ups: [], upActive: false, editing: null };
const FM_ARCH = /\.(zip|jar|mrpack|tar|tar\.gz|tgz|tar\.xz|tar\.bz2)$/i, FM_IMG = /\.(png|jpe?g|gif|webp|bmp)$/i;
const FM_ICON = {
  dir: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4.2l2 2h8.8A1.5 1.5 0 0 1 21 8.5v9A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/></svg>',
  file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/></svg>',
  arch: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><path d="M6 3h8l4 4v14H6z"/><path d="M10 6h2M10 9h2M10 12h2M10 15h2v3h-2z"/></svg>',
  img: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="2"/><circle cx="9" cy="9" r="1.6"/><path d="M20 16l-5-5-8 9"/></svg>',
};
const fmJoin = (a, b) => (a ? a + "/" + b : b);
const fmParent = (p) => p.split("/").slice(0, -1).join("/");
const fmName = (p) => p.split("/").pop();
const fmApi = (path, opts) => api(`/servers/${FM.sid}/files${path}`, opts);
const fmWhen = (t) => { if (!t) return ""; const d = new Date(t * 1000), now = new Date();
  return d.toDateString() === now.toDateString() ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric", year: d.getFullYear() === now.getFullYear() ? undefined : "numeric" }); };
const fmIcon = (e) => e.dir ? FM_ICON.dir : FM_IMG.test(e.name) ? FM_ICON.img : FM_ARCH.test(e.name) ? FM_ICON.arch : FM_ICON.file;

async function tabFiles(sid, s, path) {
  if (FM.sid !== sid) Object.assign(FM, { sid, path: "", sel: new Set(), results: null, q: "", editing: null });
  if (FILES_AT) { path = FILES_AT; FILES_AT = null; }            // "Open the crash report" from the console
  if (path !== undefined) {
    const r = await act(() => api(`/servers/${sid}/files?path=${encodeURIComponent(path)}`)); if (!r) return;
    if (r.content !== undefined) { FM.path = fmParent(path); return fmEdit(path, r); }
    FM.path = r.path || "";
  }
  FM.editing = null;
  $("#tab").classList.remove("fill");
  $("#tab").innerHTML = `<div class="fm" id="fm" tabindex="-1">
    <div class="fm-top"><nav class="fm-crumbs" id="fmcrumbs" aria-label="Folder"></nav>
      <form id="fmsearch" class="fm-search" role="search"><input id="fmq" type="search" placeholder="Search all files" aria-label="Search files" value="${esc(FM.q)}"></form></div>
    <div class="fm-bar" id="fmbar"></div>
    ${s.phase === "online" ? `<p class="sub fm-note">The server is running: it can write over world files while you change them, and config changes apply after a restart.</p>` : ""}
    <div class="fm-list" id="fmlist" role="grid" aria-label="Files"></div>
    <div class="fm-drop" id="fmdrop" hidden><div>Drop to upload to <b id="fmdropto"></b></div></div>
    <div class="fm-ups" id="fmups" hidden></div>
    <input type="file" id="fmfile" multiple hidden><input type="file" id="fmdir" webkitdirectory multiple hidden>
  </div>`;
  fmWire();
  await fmLoad(FM.path);
  fmDrawUps();
}

async function fmLoad(path, keepSel = false) {
  const r = await act(() => fmApi(`?path=${encodeURIComponent(path)}`));
  if (!r) { if (path) return fmLoad(fmParent(path)); return; }
  if (r.content !== undefined) return fmEdit(path, r);
  FM.path = r.path || ""; FM.entries = r.entries; FM.results = null;
  if (!keepSel) FM.sel = new Set();
  else FM.sel = new Set([...FM.sel].filter((p) => FM.entries.some((e) => fmJoin(FM.path, e.name) === p)));
  fmDraw();
}

function fmRows() {
  if (FM.results) return FM.results.map((e) => ({ ...e, p: e.path }));
  const [k, dir] = FM.sort;
  return FM.entries.map((e) => ({ ...e, p: fmJoin(FM.path, e.name) })).sort((a, b) =>
    (b.dir - a.dir) || dir * (k === "name" ? a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" })
      : k === "size" ? (a.size ?? -1) - (b.size ?? -1) : (a.mtime || 0) - (b.mtime || 0)));
}

function fmDraw() {
  if (!$("#fmlist")) return;
  const parts = FM.path && !FM.results ? FM.path.split("/") : [];
  $("#fmcrumbs").innerHTML = `<button class="fm-crumb" data-go="">${esc(LIST.find((x) => x.id === FM.sid)?.name || "server")}</button>`
    + parts.map((p, i) => `<span aria-hidden="true">/</span><button class="fm-crumb" data-go="${esc(parts.slice(0, i + 1).join("/"))}">${esc(p)}</button>`).join("")
    + (FM.results ? `<span aria-hidden="true">/</span><span class="fm-crumb on">Search: ${esc(FM.q)}</span>` : "");
  $$("[data-go]", $("#fmcrumbs")).forEach((b) => (b.onclick = () => { FM.q = ""; $("#fmq").value = ""; fmLoad(b.dataset.go); }));
  fmDrawBar();
  const rows = fmRows(), [k, dir] = FM.sort, arrow = (x) => k === x ? (dir > 0 ? " ↑" : " ↓") : "";
  const all = rows.length && rows.every((r) => FM.sel.has(r.p));
  $("#fmlist").innerHTML = `<div class="fm-row fm-head" role="row">
      <span role="columnheader"><input type="checkbox" id="fmall" aria-label="Select all" ${all ? "checked" : ""}></span>
      <button role="columnheader" data-sort="name">Name${arrow("name")}</button>
      <button role="columnheader" data-sort="size" class="fm-num">Size${arrow("size")}</button>
      <button role="columnheader" data-sort="mtime" class="fm-when">Modified${arrow("mtime")}</button></div>
    ${!FM.results && FM.path ? `<div class="fm-row fm-up" role="row" data-up="1" tabindex="0"><span></span><span class="fm-name">${FM_ICON.dir}<span>..</span></span><span></span><span class="fm-when"></span></div>` : ""}
    ${rows.map((r, i) => `<div class="fm-row${FM.sel.has(r.p) ? " sel" : ""}" role="row" tabindex="0" data-i="${i}" aria-selected="${FM.sel.has(r.p)}">
      <span><input type="checkbox" aria-label="Select ${esc(r.name)}" ${FM.sel.has(r.p) ? "checked" : ""}></span>
      <span class="fm-name ${r.dir ? "dir" : ""}">${fmIcon(r)}<a href="#" data-open="${i}" title="${esc(r.p)}">${esc(FM.results ? r.p : r.name)}</a></span>
      <span class="fm-num">${r.dir ? "" : fmtB(r.size)}</span><span class="fm-when">${fmWhen(r.mtime)}</span></div>`).join("")
    || `<div class="fm-empty">${FM.results ? `Nothing matches “${esc(FM.q)}”${FM.more ? "" : ""}.` : "This folder is empty. Drop files here to upload them."}</div>`}
    ${FM.results && FM.more ? `<p class="sub" style="padding:8px 10px">Showing the first ${rows.length}. Search for something more specific to narrow it down.</p>` : ""}`;
  const list = $("#fmlist");
  $("#fmall").onchange = (e) => { FM.sel = e.target.checked ? new Set(rows.map((r) => r.p)) : new Set(); fmDraw(); };
  $$("[data-sort]", list).forEach((b) => (b.onclick = () => { FM.sort = [b.dataset.sort, FM.sort[0] === b.dataset.sort ? -FM.sort[1] : 1]; fmDraw(); }));
  const up = $("[data-up]", list);
  if (up) up.onclick = up.ondblclick = () => fmLoad(fmParent(FM.path));
  $$(".fm-row[data-i]", list).forEach((el) => {
    const r = rows[+el.dataset.i];
    el.onclick = (e) => {
      if (e.target.closest("a")) return;
      if (e.target.type === "checkbox") { FM.sel.has(r.p) ? FM.sel.delete(r.p) : FM.sel.add(r.p); FM.last = +el.dataset.i; return fmDraw(); }
      if (e.shiftKey && FM.last != null) {
        const [a, b] = [Math.min(FM.last, +el.dataset.i), Math.max(FM.last, +el.dataset.i)];
        rows.slice(a, b + 1).forEach((x) => FM.sel.add(x.p));
      } else if (e.ctrlKey || e.metaKey) { FM.sel.has(r.p) ? FM.sel.delete(r.p) : FM.sel.add(r.p); FM.last = +el.dataset.i; }
      else { FM.sel = new Set([r.p]); FM.last = +el.dataset.i; }
      fmDraw(); $(`.fm-row[data-i="${el.dataset.i}"]`)?.focus();
    };
    el.ondblclick = (e) => { if (e.target.type !== "checkbox") fmOpen(r); };
    el.oncontextmenu = (e) => { e.preventDefault(); if (!FM.sel.has(r.p)) { FM.sel = new Set([r.p]); fmDraw(); } fmMenu(e.clientX, e.clientY); };
  });
  $$("[data-open]", list).forEach((a) => (a.onclick = (e) => { e.preventDefault(); fmOpen(rows[+a.dataset.open]); }));
}

function fmSelected() { const rows = fmRows(); return rows.filter((r) => FM.sel.has(r.p)); }

function fmDrawBar() {
  const sel = fmSelected(), n = sel.length, one = n === 1 ? sel[0] : null;
  $("#fmbar").innerHTML = n ? `<span class="fm-count">${n} selected</span>
      <button class="small" data-a="download">Download</button>
      ${one ? `<button class="small" data-a="rename">Rename</button>` : ""}
      <button class="small" data-a="move">Move</button><button class="small" data-a="copy">Copy</button>
      <button class="small" data-a="compress">Zip</button>
      ${one && !one.dir && FM_ARCH.test(one.name) ? `<button class="small" data-a="extract">Extract</button>` : ""}
      <button class="small danger" data-a="delete">Delete</button>
      <button class="small ghost" data-a="clear" aria-label="Clear selection" title="Clear selection (Esc)">✕</button>`
    : `<button class="small" data-a="upload">${ICON.plus}Upload files</button><button class="small" data-a="uploaddir">Upload a folder</button>
      <button class="small" data-a="mkdir">New folder</button><button class="small" data-a="mkfile">New file</button>
      <span class="grow"></span><button class="small ghost" data-a="trash">Recently deleted</button>`;
  $$("[data-a]", $("#fmbar")).forEach((b) => (b.onclick = () => fmAction(b.dataset.a)));
}

async function fmAction(a) {
  const sel = fmSelected(), paths = sel.map((r) => r.p);
  if (a === "clear") { FM.sel = new Set(); return fmDraw(); }
  if (a === "upload") return $("#fmfile").click();
  if (a === "uploaddir") return $("#fmdir").click();
  if (a === "trash") return fmTrash();
  if (a === "download") return fmDownload(paths);
  if (a === "mkdir" || a === "mkfile") {
    const name = await fmAsk(a === "mkdir" ? "New folder" : "New file", "Name", a === "mkdir" ? "" : "notes.txt"); if (!name) return;
    const p = fmJoin(FM.path, name);
    const r = await act(() => a === "mkdir" ? fmApi("/mkdir", { method: "POST", body: { path: p } })
      : fmApi("", { method: "PUT", body: { path: p, content: "", create: true } }));
    if (!r) return;
    if (a === "mkfile") return fmEdit(p, await fmApi(`/text?path=${encodeURIComponent(p)}`));
    FM.sel = new Set([p]); return fmLoad(FM.path, true);
  }
  if (a === "rename") {
    const name = await fmAsk("Rename", "New name", sel[0].name); if (!name || name === sel[0].name) return;
    const r = await act(() => fmApi("/rename", { method: "POST", body: { path: paths[0], name } }));
    if (r) { FM.sel = new Set([r.path]); fmReload(true); }
    return;
  }
  if (a === "move" || a === "copy") {
    const to = await fmPickFolder(a === "move" ? `Move ${paths.length} item${paths.length > 1 ? "s" : ""} to…` : `Copy ${paths.length} item${paths.length > 1 ? "s" : ""} to…`, a === "move" ? "Move here" : "Copy here");
    if (to == null) return;
    let r = await fmApi("/move", { method: "POST", body: { paths, to, mode: a } }).catch((e) => e);
    if (r instanceof Error && /already/.test(r.message)) {
      if (!await fmConfirm(`${r.message}. Replace what's there? The replaced items go to Recently deleted.`, "Replace")) return;
      r = await fmApi("/move", { method: "POST", body: { paths, to, mode: a, overwrite: true } }).catch((e) => e);
    }
    if (r instanceof Error) return toast(r.message, true);
    toast(a === "move" ? `Moved to /${to}` : `Copied to /${to}`); return fmReload();
  }
  if (a === "compress") {
    const name = await fmAsk("Zip", "Name of the zip", (sel.length === 1 ? sel[0].name.replace(/\.[^.]+$/, "") : "files") + ".zip"); if (!name) return;
    toast("Zipping…");
    const r = await act(() => fmApi("/compress", { method: "POST", body: { paths, name } }));
    if (r) { toast(`Made ${fmName(r.path)} (${fmtB(r.size)})`); FM.sel = new Set([r.path]); fmReload(true); }
    return;
  }
  if (a === "extract") {
    toast("Extracting…");
    const r = await act(() => fmApi("/extract", { method: "POST", body: { path: paths[0] } }));
    if (r) { toast(`Extracted ${r.files} file${r.files === 1 ? "" : "s"} to ${fmName(r.path)}`); FM.sel = new Set([r.path]); fmReload(true); }
    return;
  }
  if (a === "delete") {
    const what = paths.length === 1 ? `“${sel[0].name}”` : `${paths.length} items`;
    if (!await fmConfirm(`Delete ${what}? ${sel.some((r) => r.dir) ? "Folders go with everything in them. " : ""}You can put ${paths.length === 1 ? "it" : "them"} back from Recently deleted for 7 days.`, "Delete")) return;
    const r = await act(() => fmApi("/delete", { method: "POST", body: { paths } }));
    if (r) { toast(`Deleted ${what}. Recently deleted has ${paths.length === 1 ? "it" : "them"} for 7 days.`); FM.sel = new Set(); fmReload(); }
  }
}

function fmReload(keepSel = false) { return FM.results ? fmSearch(FM.q) : fmLoad(FM.path, keepSel); }

function fmOpen(r) {
  if (r.dir) { FM.q = ""; if ($("#fmq")) $("#fmq").value = ""; return fmLoad(r.p); }
  if (FM_IMG.test(r.name)) {
    modal(`<h2 style="overflow-wrap:anywhere">${esc(r.name)}</h2><div class="fm-preview"><img src="/api/servers/${esc(FM.sid)}/files/download?inline=true&path=${encodeURIComponent(r.p)}" alt=""></div>
      <div class="row" style="justify-content:flex-end;margin-top:14px"><button class="small" id="fmpdl">Download</button><button class="small" onclick="closeModal()">Close</button></div>`);
    $("#fmpdl").onclick = () => fmDownload([r.p]); return;
  }
  if (FM_ARCH.test(r.name)) {
    modal(`<h2 style="overflow-wrap:anywhere">${esc(r.name)}</h2><p class="muted">${fmtB(r.size)} archive.</p>
      <div class="row" style="justify-content:flex-end;margin-top:14px"><button class="small" onclick="closeModal()">Cancel</button><button class="small" id="fmadl">Download</button><button class="primary small" id="fmaex">Extract here</button></div>`);
    $("#fmadl").onclick = () => { closeModal(); fmDownload([r.p]); };
    $("#fmaex").onclick = () => { closeModal(); FM.sel = new Set([r.p]); fmAction("extract"); }; return;
  }
  fmApi(`/text?path=${encodeURIComponent(r.p)}`).then((t) => fmEdit(r.p, t)).catch((e) => {
    modal(`<h2 style="overflow-wrap:anywhere">${esc(r.name)}</h2><p class="muted">${esc(e.message)}</p>
      <div class="row" style="justify-content:flex-end;margin-top:14px"><button class="small" onclick="closeModal()">Close</button><button class="primary small" id="fmodl">Download</button></div>`);
    $("#fmodl").onclick = () => { closeModal(); fmDownload([r.p]); };
  });
}

function fmDownload(paths) {
  if (!paths.length) return;
  const a = document.createElement("a");
  a.href = `/api/servers/${encodeURIComponent(FM.sid)}/files/download?` + paths.map((p) => "path=" + encodeURIComponent(p)).join("&");
  a.download = ""; document.body.append(a); a.click(); a.remove();
  if (paths.length > 1 || fmRows().find((r) => r.p === paths[0])?.dir) toast("Zipping as it downloads. Big folders take a while to start.");
}

async function fmSearch(q) {
  FM.q = q.trim();
  if (!FM.q) return fmLoad(FM.path);
  const r = await act(() => fmApi(`/search?q=${encodeURIComponent(FM.q)}&path=`)); if (!r) return;
  FM.results = r.results; FM.more = r.more; FM.sel = new Set(); fmDraw();
}

// small dialogs (in the page's own modal)
function fmAsk(title, label, value = "") {
  return new Promise((done) => {
    modal(`<h2>${esc(title)}</h2><form id="fmaskf"><label for="fmask">${esc(label)}</label><input id="fmask" value="${esc(value)}" autocomplete="off" spellcheck="false">
      <div class="row" style="justify-content:flex-end;margin-top:14px"><button type="button" class="small" id="fmaskc">Cancel</button><button class="primary small">OK</button></div></form>`);
    const i = $("#fmask"); i.focus(); const dot = value.lastIndexOf("."); i.setSelectionRange(0, dot > 0 ? dot : value.length);
    $("#fmaskf").onsubmit = (e) => { e.preventDefault(); const v = i.value.trim(); closeModal(); done(v || null); };
    $("#fmaskc").onclick = () => { closeModal(); done(null); };
  });
}
function fmConfirm(text, yes) {
  return new Promise((done) => {
    modal(`<p style="margin-top:0">${esc(text)}</p><div class="row" style="justify-content:flex-end;margin-top:14px">
      <button class="small" id="fmcn">Cancel</button><button class="small ${yes === "Delete" || yes === "Replace" ? "danger" : "primary"}" id="fmcy">${esc(yes)}</button></div>`);
    $("#fmcy").focus();
    $("#fmcy").onclick = () => { closeModal(); done(true); };
    $("#fmcn").onclick = () => { closeModal(); done(false); };
  });
}
async function fmPickFolder(title, yes) {
  let at = FM.path;
  return new Promise((done) => {
    const draw = async () => {
      const r = await fmApi(`?path=${encodeURIComponent(at)}`).catch(() => null); if (!r) return;
      const dirs = r.entries.filter((e) => e.dir);
      modal(`<h2>${esc(title)}</h2><div class="mono sub" style="margin-bottom:8px">/${esc(at)}</div>
        <div class="fm-pick">${at ? `<button class="fm-prow" data-to="${esc(fmParent(at))}">${FM_ICON.dir}..</button>` : ""}
        ${dirs.map((d) => `<button class="fm-prow" data-to="${esc(fmJoin(at, d.name))}">${FM_ICON.dir}${esc(d.name)}</button>`).join("") || `<p class="sub" style="padding:8px">No folders here.</p>`}</div>
        <div class="row" style="justify-content:flex-end;margin-top:14px"><button class="small" id="fmpc">Cancel</button><button class="primary small" id="fmpy">${esc(yes)}</button></div>`);
      $$("[data-to]").forEach((b) => (b.onclick = () => { at = b.dataset.to; draw(); }));
      $("#fmpy").onclick = () => { closeModal(); done(at); };
      $("#fmpc").onclick = () => { closeModal(); done(null); };
    };
    draw();
  });
}

function fmMenu(x, y) {
  $("#fmmenu")?.remove();
  const sel = fmSelected(), one = sel.length === 1 ? sel[0] : null;
  const items = [one && ["open", one.dir ? "Open" : "Open"], ["download", "Download"], one && ["rename", "Rename"], ["move", "Move…"], ["copy", "Copy…"],
    ["compress", "Zip…"], one && !one.dir && FM_ARCH.test(one.name) && ["extract", "Extract here"], ["delete", "Delete"]].filter(Boolean);
  const m = document.createElement("div");
  m.id = "fmmenu"; m.className = "fm-menu"; m.setAttribute("role", "menu");
  m.innerHTML = items.map(([k, l]) => `<button role="menuitem" data-m="${k}" class="${k === "delete" ? "bad" : ""}">${l}</button>`).join("");
  document.body.append(m);
  m.style.left = Math.min(x, innerWidth - m.offsetWidth - 8) + "px"; m.style.top = Math.min(y, innerHeight - m.offsetHeight - 8) + "px";
  $$("[data-m]", m).forEach((b) => (b.onclick = () => { m.remove(); b.dataset.m === "open" ? fmOpen(one) : fmAction(b.dataset.m); }));
  m.querySelector("button")?.focus();
  setTimeout(() => document.addEventListener("click", () => m.remove(), { once: true }));
}

async function fmTrash() {
  const r = await act(() => fmApi("/trash")); if (!r) return;
  const pick = new Set();
  const draw = () => {
    modal(`<h2>Recently deleted</h2><p class="muted" style="margin-top:0">Deleted files and folders stay here for ${r.days} days, then they're gone for good.</p>
      <div class="fm-pick">${r.items.map((it) => `<label class="fm-trow"><input type="checkbox" data-id="${esc(it.id)}" ${pick.has(it.id) ? "checked" : ""}>
        <span class="fm-name">${it.dir ? FM_ICON.dir : FM_ICON.file}<span style="overflow-wrap:anywhere">/${esc(it.path)}</span></span>
        <span class="sub">${fmtB(it.size)} · ${ago(it.at)}</span></label>`).join("") || `<p class="sub" style="padding:8px">Nothing deleted lately.</p>`}</div>
      <div class="row" style="justify-content:space-between;margin-top:14px">
        ${can("admin") && r.items.length ? `<button class="small danger" id="fmtgone" ${pick.size ? "" : "disabled"}>Delete for good</button>` : "<span></span>"}
        <span class="row"><button class="small" onclick="closeModal()">Close</button><button class="primary small" id="fmtback" ${pick.size ? "" : "disabled"}>Put back</button></span></div>`);
    $$("[data-id]").forEach((c) => (c.onchange = () => { c.checked ? pick.add(c.dataset.id) : pick.delete(c.dataset.id); draw(); }));
    $("#fmtback").onclick = async () => {
      const x = await act(() => fmApi("/trash/restore", { method: "POST", body: { ids: [...pick] } })); if (!x) return;
      closeModal(); toast(`Put back ${x.restored.length} item${x.restored.length === 1 ? "" : "s"}`); fmReload();
    };
    $("#fmtgone") && ($("#fmtgone").onclick = async () => {
      if (!await fmConfirm(`Delete ${pick.size} item${pick.size === 1 ? "" : "s"} for good? This can't be undone.`, "Delete")) return fmTrash();
      await act(() => fmApi("/trash/empty", { method: "POST", body: { ids: [...pick] } })); fmTrash();
    });
  };
  draw();
}

// ---- uploads: one request per file, streamed; folders keep their structure
function fmQueue(list) {
  if (!list.length) return;
  const to = FM.path;
  FM.ups.push(...list.map(({ file, rel }) => ({ file, path: fmJoin(to, rel || file.name), done: 0, state: "waiting" })));
  fmDrawUps(); fmPump();
}
async function fmPump() {
  if (FM.upActive) return;
  FM.upActive = true;
  while (true) {
    const u = FM.ups.find((x) => x.state === "waiting"); if (!u) break;
    u.state = "sending"; fmDrawUps();
    const res = await new Promise((done) => {
      const x = new XMLHttpRequest();
      x.open("PUT", `/api/servers/${encodeURIComponent(FM.sid)}/files/upload?path=${encodeURIComponent(u.path)}${u.overwrite ? "&overwrite=true" : ""}`);
      x.upload.onprogress = (e) => { u.done = e.loaded; fmDrawUps(); };
      x.onload = () => done({ ok: x.status < 300, status: x.status, msg: (() => { try { return JSON.parse(x.responseText).detail; } catch { return x.statusText; } })() });
      x.onerror = () => done({ ok: false, status: 0, msg: "Connection lost" });
      x.send(u.file);
    });
    u.state = res.ok ? "done" : res.status === 409 && /exists/.test(res.msg || "") ? "exists" : "failed"; u.msg = res.msg; u.done = u.file.size;
    fmDrawUps();
  }
  FM.upActive = false;
  const exists = FM.ups.filter((u) => u.state === "exists");
  if (exists.length && await fmConfirm(`${exists.length} file${exists.length === 1 ? " is" : "s are"} already there (${exists.slice(0, 3).map((u) => fmName(u.path)).join(", ")}${exists.length > 3 ? "…" : ""}). Replace ${exists.length === 1 ? "it" : "them"}? The old ${exists.length === 1 ? "one goes" : "ones go"} to Recently deleted.`, "Replace")) {
    exists.forEach((u) => Object.assign(u, { state: "waiting", overwrite: true, done: 0 })); return fmPump();
  }
  exists.forEach((u) => (u.state = "skipped"));
  if (FM.sid && $("#fmlist")) fmLoad(FM.path, true);
  fmDrawUps();
}
function fmDrawUps() {
  const box = $("#fmups"); if (!box) return;
  const ups = FM.ups; box.hidden = !ups.length; if (!ups.length) return;
  const total = ups.reduce((a, u) => a + u.file.size, 0), done = ups.reduce((a, u) => a + Math.min(u.done, u.file.size), 0);
  const busy = ups.some((u) => u.state === "waiting" || u.state === "sending");
  const failed = ups.filter((u) => u.state === "failed");
  const now = ups.find((u) => u.state === "sending");
  box.innerHTML = `<div class="row" style="justify-content:space-between"><b>${busy ? `Uploading ${ups.filter((u) => u.state === "done").length + 1} of ${ups.length}` : `Uploaded ${ups.filter((u) => u.state === "done").length} of ${ups.length}`}</b>
      ${busy ? "" : `<button class="small ghost" id="fmupx">Close</button>`}</div>
    <div class="fm-bar2"><i style="width:${total ? (100 * done / total).toFixed(1) : 100}%"></i></div>
    <div class="sub" style="overflow-wrap:anywhere">${now ? `${esc(now.path)} · ${fmtB(done)} of ${fmtB(total)}` : failed.length ? "" : `${fmtB(total)}`}</div>
    ${failed.map((u) => `<div class="sub" style="color:var(--bad)">${esc(fmName(u.path))}: ${esc(u.msg || "failed")}</div>`).join("")}`;
  $("#fmupx") && ($("#fmupx").onclick = () => { FM.ups = []; fmDrawUps(); });
}
async function fmEntries(dt) {
  // files dropped from the desktop, folders included (with their paths)
  const out = [], walk = async (entry, pre) => {
    if (entry.isFile) await new Promise((ok) => entry.file((f) => { out.push({ file: f, rel: pre + f.name }); ok(); }, ok));
    else if (entry.isDirectory) {
      const reader = entry.createReader();
      let batch; do { batch = await new Promise((ok) => reader.readEntries(ok, () => ok([]))); for (const e of batch) await walk(e, pre + entry.name + "/"); } while (batch.length);
    }
  };
  const items = [...(dt.items || [])].map((i) => i.webkitGetAsEntry?.()).filter(Boolean);
  if (items.length) { for (const e of items) await walk(e, ""); return out; }
  return [...dt.files].map((f) => ({ file: f, rel: f.name }));
}

function fmWire() {
  const fm = $("#fm");
  $("#fmsearch").onsubmit = (e) => { e.preventDefault(); fmSearch($("#fmq").value); };
  $("#fmq").oninput = (e) => { if (!e.target.value) fmSearch(""); };
  $("#fmfile").onchange = (e) => { fmQueue([...e.target.files].map((f) => ({ file: f, rel: f.name }))); e.target.value = ""; };
  $("#fmdir").onchange = (e) => { fmQueue([...e.target.files].map((f) => ({ file: f, rel: f.webkitRelativePath || f.name }))); e.target.value = ""; };
  let depth = 0;
  fm.ondragenter = (e) => { if (![...(e.dataTransfer?.types || [])].includes("Files")) return; e.preventDefault(); depth++; $("#fmdropto").textContent = "/" + FM.path; $("#fmdrop").hidden = false; };
  fm.ondragover = (e) => { if ([...(e.dataTransfer?.types || [])].includes("Files")) e.preventDefault(); };
  fm.ondragleave = () => { if (--depth <= 0) { depth = 0; $("#fmdrop").hidden = true; } };
  fm.ondrop = async (e) => { e.preventDefault(); depth = 0; $("#fmdrop").hidden = true; fmQueue(await fmEntries(e.dataTransfer)); };
  fm.onkeydown = (e) => {
    if (e.target.matches("input, textarea")) return;
    const rows = fmRows(), cur = document.activeElement?.closest?.(".fm-row[data-i]"), i = cur ? +cur.dataset.i : -1;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") { e.preventDefault(); FM.sel = new Set(rows.map((r) => r.p)); fmDraw(); }
    else if (e.key === "Delete" && FM.sel.size) { e.preventDefault(); fmAction("delete"); }
    else if (e.key === "F2" && FM.sel.size === 1) { e.preventDefault(); fmAction("rename"); }
    else if (e.key === "Escape" && FM.sel.size) { FM.sel = new Set(); fmDraw(); }
    else if (e.key === "Backspace" && FM.path && !FM.results) { e.preventDefault(); fmLoad(fmParent(FM.path)); }
    else if (e.key === "Enter" && i >= 0) { e.preventDefault(); fmOpen(rows[i]); }
    else if ((e.key === "ArrowDown" || e.key === "ArrowUp") && rows.length) {
      e.preventDefault(); const n = Math.max(0, Math.min(rows.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)));
      if (!e.shiftKey) FM.sel = new Set([rows[n].p]); else FM.sel.add(rows[n].p);
      FM.last = n; fmDraw(); $(`.fm-row[data-i="${n}"]`)?.focus();
    }
  };
}

// ---- the editor
function fmEdit(path, f) {
  FM.editing = { path, mtime: f.mtime, saved: f.content };
  $("#tab").classList.add("fill");
  $("#tab").innerHTML = `<div class="fm-ed">
    <div class="fm-top"><nav class="fm-crumbs"><button class="fm-crumb" id="fmeback" title="Back to the folder">${esc(fmParent(path) ? "/" + fmParent(path) : LIST.find((x) => x.id === FM.sid)?.name || "server")}</button><span aria-hidden="true">/</span><span class="fm-crumb on">${esc(fmName(path))}</span></nav>
      <span class="sub" id="fmestate" aria-live="polite"></span></div>
    <div class="row" style="gap:6px;margin-bottom:8px"><button class="small" id="fmeclose">Close</button><button class="small" id="fmedl">Download</button>
      <label class="row sub" style="margin:0 0 0 6px;gap:6px"><input type="checkbox" id="fmewrap"> Wrap lines</label><span class="grow"></span>
      <button class="primary small" id="fmesave">Save</button></div>
    <div class="fm-code"><pre class="fm-gutter" id="fmeg" aria-hidden="true"></pre><textarea id="fmet" spellcheck="false" aria-label="${esc(fmName(path))}" wrap="off"></textarea></div>
  </div>`;
  const t = $("#fmet"), g = $("#fmeg");
  t.value = f.content;
  const lines = () => { const n = t.value.split("\n").length; if (g.dataset.n != n) { g.dataset.n = n; g.textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n"); } };
  const dirty = () => t.value !== FM.editing?.saved;
  const state = () => { $("#fmestate").textContent = dirty() ? "Unsaved changes" : ""; $("#fmesave").disabled = !dirty(); };
  lines(); state();
  t.oninput = () => { lines(); state(); };
  t.onscroll = () => { g.scrollTop = t.scrollTop; };
  $("#fmewrap").onchange = (e) => { t.wrap = e.target.checked ? "soft" : "off"; g.hidden = e.target.checked; };
  t.onkeydown = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
    else if (e.key === "Tab" && !e.shiftKey) { e.preventDefault(); const s0 = t.selectionStart; t.setRangeText("  ", s0, t.selectionEnd, "end"); lines(); state(); }
  };
  const save = async (force = false) => {
    if (!dirty() && !force) return;
    const r = await fmApi("", { method: "PUT", body: { path, content: t.value, mtime: force ? null : FM.editing.mtime } }).catch((e) => e);
    if (r instanceof Error) {
      if (/changed since/.test(r.message)) {
        if (await fmConfirm("This file changed since you opened it (the server may have written it). Save your version over it?", "Save over it")) return save(true);
        return;
      }
      return toast(r.message, true);
    }
    FM.editing.mtime = r.mtime; FM.editing.saved = t.value; state(); toast("Saved");
  };
  $("#fmesave").onclick = () => save();
  const leave = async () => {
    if (dirty() && !await fmConfirm("Leave without saving your changes?", "Leave")) return;
    FM.editing = null; tabFiles(FM.sid, LIST.find((x) => x.id === FM.sid) || {}, FM.path);
  };
  $("#fmeclose").onclick = leave; $("#fmeback").onclick = leave;
  $("#fmedl").onclick = () => fmDownload([path]);
  t.focus();
}
window.addEventListener("beforeunload", (e) => { if (FM.editing && $("#fmet") && $("#fmet").value !== FM.editing.saved) { e.preventDefault(); e.returnValue = ""; } });

const fmtB = (n) => n == null ? "–" : n >= 2 ** 30 ? `${(n / 2 ** 30).toFixed(1)} GB` : n >= 2 ** 20 ? `${(n / 2 ** 20).toFixed(n < 10 * 2 ** 20 ? 1 : 0)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
const KEEP = [["last", "Most recent"], ["hourly", "Hourly"], ["daily", "Daily"], ["weekly", "Weekly"], ["monthly", "Monthly"]];
const REASON = { manual: "Manual", scheduled: "Scheduled", "before-restart": "Before restart", "before-restore": "Before restore", "before-update": "Before update" };
let bkTimer = null;
async function tabBackups(sid, s) {
  clearTimeout(bkTimer);
  const r = await api(`/servers/${sid}/backups`).catch(() => null); if (!r || TAB !== "backups" || SEL !== sid) return;
  const list = r.backups; BK[sid] = list;
  const st = r.status || {}, busy = st.status === "running", snaps = list.filter((b) => b.kind === "snapshot");
  const sched = (s.schedule || []).filter((t) => t.kind === "backup" && t.enabled !== false);
  $("#tab").innerHTML = `<div class="card"><div class="row" style="justify-content:space-between">
      <div><h4 style="margin:0">Backups</h4><div class="sub">${snaps.length ? `${snaps.length} restore point${snaps.length === 1 ? "" : "s"}. All servers' backups use ${fmtB(r.store_bytes)} together` : "None yet"}</div></div>
      ${can("moderator") ? `<button class="primary" id="mkb" ${busy ? "disabled" : ""}>${busy ? `Backing up${st.percent != null ? ` ${Math.round(st.percent)}%` : ""}…` : "Back up now"}</button>` : ""}</div>
      ${busy ? `<div class="bar slim" style="margin-top:10px"><i class="live" style="width:${st.percent || 2}%"></i></div>` : ""}
      ${st.status === "error" ? `<div class="banner bad msg" style="margin-top:10px">The last backup failed: ${esc(st.note || "")}</div>` : ""}
      <p class="sub">Each backup stores only what changed since the one before, and files that other servers' backups already hold (like the same modpack's mods) are stored once, so they're quick and small. Every one is still a full restore point. Safe while the server runs: saving is paused and flushed first.
      ${sched.length ? `Scheduled: ${sched.map((t) => esc(schedWhen(t))).join("; ")}.` : `No automatic backups yet. ${can("moderator") ? `<a href="#/s/${esc(sid)}/schedule">Set them up in Schedule</a>.` : ""}`}</p></div>
    <div class="list">${list.map((b) => `<div class="item"><div class="grow">
      <div class="t">${new Date(b.time * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}${b.reason ? ` <span class="badge">${esc(REASON[b.reason] || b.reason)}</span>` : ""}${b.kind === "archive" ? ` <span class="badge">Full archive</span>` : ""}</div>
      <div class="d">${b.kind === "snapshot" ? `${fmtB(b.added)} new, ${fmtB(b.size)} in full` : fmtB(b.size)}</div></div>
      ${can("moderator") ? `<a class="btn small" href="/api/servers/${esc(sid)}/backups/${encodeURIComponent(b.name)}" title="${b.kind === "snapshot" ? "Download as a .zip" : "Download"}">Download</a>
      ${b.kind === "snapshot" ? `<button class="small" data-bf="${esc(b.name)}" data-t="${b.time}" title="Pick files or folders to put back">Browse</button>` : ""}
      <button class="small" data-r="${esc(b.name)}" data-t="${b.time}">Restore</button>` : ""}${can("admin") ? `<button class="danger small" data-d="${esc(b.name)}" data-t="${b.time}">Delete</button>` : ""}</div>`).join("")}</div>
    ${can("moderator") ? `<div class="card"><h4 style="margin-top:0">Keep</h4>
      <p class="sub" style="margin-top:0">Older backups are thinned out after each new one: the most recent few, then one per hour, day, week and month. 0 = none of that kind.</p>
      <div class="row" style="flex-wrap:wrap;gap:10px">${KEEP.map(([k, l]) => `<div style="flex:1;min-width:84px"><label for="k-${k}">${l}</label><input id="k-${k}" type="number" min="0" value="${r.keep[k] ?? 0}"></div>`).join("")}</div>
      <div class="row" style="justify-content:flex-end;margin-top:12px"><button id="kp">Save</button></div></div>` : ""}`;
  if (busy) bkTimer = setTimeout(() => TAB === "backups" && SEL === sid && tabBackups(sid, s), 2000);
  $("#mkb") && ($("#mkb").onclick = async () => {
    const run = api(`/servers/${sid}/backups`, { method: "POST" });
    setTimeout(() => tabBackups(sid, s), 600);
    await act(() => run, "Backup done"); tabBackups(sid, s);
  });
  $("#kp") && ($("#kp").onclick = async () => {
    const body = Object.fromEntries(KEEP.map(([k]) => [k, +$("#k-" + k).value || 0]));
    await act(() => api(`/servers/${sid}/backups/keep`, { method: "PUT", body }), "Saved. Applies after the next backup.");
  });
  const when = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  $$("[data-r]", $("#tab")).forEach((b) => (b.onclick = () => {
    modal(`<h2>Restore the backup from ${esc(when(+b.dataset.t))}?</h2>
      <p class="muted">The server's files are replaced with this backup. The current files are kept next to them in a <span class="mono">.pre-restore</span> folder, so you can undo it from Files.</p>
      ${running(s) ? `<p class="banner bad msg">Stop the server first.</p>` : ""}
      <div class="row" style="justify-content:flex-end;margin-top:16px"><button class="ghost" onclick="closeModal()">Cancel</button><button class="primary" id="rgo" ${running(s) ? "disabled" : ""}>Restore</button></div>`);
    $("#rgo").onclick = async () => { $("#rgo").disabled = true; $("#rgo").textContent = "Restoring…";
      await act(() => api(`/servers/${sid}/backups/${encodeURIComponent(b.dataset.r)}/restore`, { method: "POST" }), "Restored. Start the server."); closeModal(); };
  }));
  $$("[data-bf]", $("#tab")).forEach((b) => (b.onclick = () => browseBackup(sid, s, b.dataset.bf, +b.dataset.t)));
  $$("[data-d]", $("#tab")).forEach((b) => (b.onclick = () => {
    modal(`<h2>Delete the backup from ${esc(when(+b.dataset.t))}?</h2><p class="muted">This can't be undone. Other backups aren't affected.</p>
      <div class="row" style="justify-content:flex-end;margin-top:16px"><button class="ghost" onclick="closeModal()">Cancel</button><button class="danger" id="dgo">Delete backup</button></div>`);
    $("#dgo").onclick = async () => { await act(() => api(`/servers/${sid}/backups/${encodeURIComponent(b.dataset.d)}`, { method: "DELETE" })); closeModal(); tabBackups(sid, s); };
  }));
}

// ------------------------------------------------------------------ schedule
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const SKIND = { backup: "Back up", restart: "Restart", stop: "Stop", start: "Start", command: "Run a console command" };
function schedWhen(t) {
  const w = t.when;
  if (w.type === "hours") return `every ${w.hours} hour${w.hours === 1 ? "" : "s"}`;
  const d = w.days.length === 7 ? "every day" : w.days.join() === "0,1,2,3,4" ? "weekdays" : w.days.join() === "5,6" ? "weekends" : w.days.map((i) => DAYS[i]).join(", ");
  return `${d} at ${w.time}`;
}
function schedTitle(t) {
  const what = t.kind === "command" ? `Run “${t.command}”` : SKIND[t.kind];
  const extra = [t.warn_min ? `${t.warn_min}-minute warning` : "", t.backup_first ? "backs up first" : ""].filter(Boolean).join(", ");
  return { what, when: schedWhen(t), extra };
}
const PRESETS = [
  { kind: "backup", when: { type: "hours", hours: 6, minute: 0 } },
  { kind: "restart", warn_min: 5, backup_first: true, when: { type: "daily", time: "05:00", days: [0, 1, 2, 3, 4, 5, 6] } },
];
async function tabSchedule(sid, s) {
  const r = await api(`/servers/${sid}/schedule`).catch(() => null); if (!r || TAB !== "schedule" || SEL !== sid) return;
  const tasks = r.tasks, mod = can("moderator");
  const fmtNext = (t) => t ? new Date(t * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "–";
  const save = async (list, msg) => {
    const out = await act(() => api(`/servers/${sid}/schedule`, { method: "PUT", body: { tasks: list } }), msg);
    if (out) { const x = LIST.find((y) => y.id === sid); if (x) x.schedule = out.tasks; tabSchedule(sid, s); }
  };
  $("#tab").innerHTML = `<div class="card"><div class="row" style="justify-content:space-between">
      <div><h4 style="margin:0">Schedule</h4><div class="sub">Times are in ${esc(r.tz)}. Players get a countdown in chat before restarts and stops.</div></div>
      ${mod ? `<button class="primary" id="addt">Add task</button>` : ""}</div>
      ${!tasks.length && mod ? `<p class="sub">Nothing scheduled yet. Common choices:</p><div class="row" style="flex-wrap:wrap">${PRESETS.map((p, i) => {
        const x = schedTitle(p); return `<button class="small" data-preset="${i}">${esc(x.what)} ${esc(x.when)}${x.extra ? `, ${esc(x.extra)}` : ""}</button>`; }).join("")}</div>` : ""}
      ${!tasks.length && !mod ? `<p class="sub">Nothing scheduled.</p>` : ""}</div>
    <div class="list">${tasks.map((t) => { const x = schedTitle(t); return `<div class="item ${t.enabled ? "" : "off"}"><div class="grow">
      <div class="t">${esc(x.what)} <span style="color:var(--mute);font-weight:400">${esc(x.when)}</span></div>
      <div class="d">${x.extra ? `${esc(x.extra)}. ` : ""}${t.enabled ? `Next: ${fmtNext(t.next_run)}` : "Paused"}${t.last_result ? `. Last: <span style="color:${t.last_ok ? "inherit" : "var(--bad)"}">${esc(t.last_result)}</span>${t.last_done ? `, ${ago(t.last_done)}` : ""}` : ""}</div></div>
      ${mod ? `<label class="row" style="color:var(--ink);gap:6px" title="${t.enabled ? "On" : "Paused"}"><input type="checkbox" data-en="${esc(t.id)}" ${t.enabled ? "checked" : ""} aria-label="Task on"></label>
      <button class="small" data-run="${esc(t.id)}" title="Run it now, without the warning">Run now</button>
      <button class="small" data-ed="${esc(t.id)}">Edit</button><button class="danger small" data-rm="${esc(t.id)}" aria-label="Delete task">Delete</button>` : ""}</div>`; }).join("")}</div>`;
  const strip = (t) => { const { next_run, text, ...rest } = t; return rest; };
  $("#addt") && ($("#addt").onclick = () => schedForm(null, (nt) => save([...tasks.map(strip), nt], "Task added")));
  $$("[data-preset]", $("#tab")).forEach((b) => (b.onclick = () => save([...tasks.map(strip), PRESETS[+b.dataset.preset]], "Task added")));
  $$("[data-en]", $("#tab")).forEach((b) => (b.onchange = () => save(tasks.map((t) => strip(t.id === b.dataset.en ? { ...t, enabled: b.checked } : t)), b.checked ? "Task on" : "Task paused")));
  $$("[data-rm]", $("#tab")).forEach((b) => (b.onclick = () => save(tasks.filter((t) => t.id !== b.dataset.rm).map(strip), "Task deleted")));
  $$("[data-ed]", $("#tab")).forEach((b) => (b.onclick = () => schedForm(tasks.find((t) => t.id === b.dataset.ed),
    (nt) => save(tasks.map((t) => t.id === nt.id ? nt : strip(t)), "Task saved"))));
  $$("[data-run]", $("#tab")).forEach((b) => (b.onclick = async () => {
    const t = tasks.find((x) => x.id === b.dataset.run);
    if ((t.kind === "restart" || t.kind === "stop") && !confirm(`${SKIND[t.kind]} ${s.name} now, without the warning?`)) return;
    if (await act(() => api(`/servers/${sid}/schedule/${t.id}/run`, { method: "POST" }), "Started")) setTimeout(() => tabSchedule(sid, s), 3000);
  }));
}
function schedForm(t, done) {
  const v = t || { kind: "backup", when: { type: "daily", time: "04:00", days: [0, 1, 2, 3, 4, 5, 6] }, warn_min: 5, backup_first: true };
  const w = v.when, daily = w.type === "daily";
  modal(`<h2>${t ? "Edit task" : "Add a task"}</h2>
    <label for="fk">What</label><select id="fk">${Object.entries(SKIND).map(([k, l]) => `<option value="${k}" ${k === v.kind ? "selected" : ""}>${l}</option>`).join("")}</select>
    <div id="fcmd"><label for="fc">Command</label><input id="fc" class="mono" placeholder="say Remember to vote!" value="${esc(v.command || "")}"></div>
    <label>When</label><div class="lsw" id="fwt"><button type="button" data-w="daily" class="${daily ? "on" : ""}">At a set time</button><button type="button" data-w="hours" class="${daily ? "" : "on"}">Every few hours</button></div>
    <div id="fdaily" style="margin-top:10px"><div class="row" style="flex-wrap:wrap;gap:6px">${DAYS.map((d, i) => `<label class="badge" style="cursor:pointer;padding:6px 10px"><input type="checkbox" data-day="${i}" ${(daily ? w.days : [0, 1, 2, 3, 4, 5, 6]).includes(i) ? "checked" : ""} style="margin-right:6px">${d}</label>`).join("")}</div>
      <label for="ft">Time</label><input id="ft" type="time" value="${daily ? w.time : "04:00"}" style="width:auto"></div>
    <div id="fhours" style="margin-top:10px"><label for="fh">Every</label><select id="fh" style="width:auto">${[1, 2, 3, 4, 6, 8, 12].map((h) => `<option value="${h}" ${!daily && w.hours === h ? "selected" : ""}>${h} hour${h === 1 ? "" : "s"}</option>`).join("")}</select>
      <span class="sub">counted from midnight</span></div>
    <div id="fwarn"><label for="fw">Warn players first</label><select id="fw" style="width:auto">${[0, 1, 5, 10, 15].map((m) => `<option value="${m}" ${(v.warn_min ?? 5) === m ? "selected" : ""}>${m ? `${m} minute${m === 1 ? "" : "s"} before` : "No warning"}</option>`).join("")}</select>
      <p class="sub">A countdown appears in chat, and the task itself happens at the set time.</p></div>
    <label class="row" id="fbk" style="color:var(--ink)"><input type="checkbox" id="fb" ${v.backup_first ? "checked" : ""}> Back up just before restarting</label>
    <div class="row" style="justify-content:flex-end;margin-top:16px"><button class="ghost" onclick="closeModal()">Cancel</button><button class="primary" id="fsave">${t ? "Save" : "Add task"}</button></div>`);
  let wt = w.type;
  const sync = () => {
    const k = $("#fk").value;
    $("#fcmd").classList.toggle("hidden", k !== "command");
    $("#fwarn").classList.toggle("hidden", k !== "restart" && k !== "stop");
    $("#fbk").classList.toggle("hidden", k !== "restart");
    $("#fdaily").classList.toggle("hidden", wt !== "daily"); $("#fhours").classList.toggle("hidden", wt !== "hours");
    $$("#fwt button").forEach((b) => b.classList.toggle("on", b.dataset.w === wt));
  };
  $("#fk").onchange = sync; $$("#fwt button").forEach((b) => (b.onclick = () => { wt = b.dataset.w; sync(); })); sync();
  $("#fsave").onclick = () => {
    const k = $("#fk").value, out = { ...(t ? { id: t.id, enabled: t.enabled } : {}), kind: k };
    out.when = wt === "daily" ? { type: "daily", time: $("#ft").value, days: $$("[data-day]").filter((c) => c.checked).map((c) => +c.dataset.day) }
      : { type: "hours", hours: +$("#fh").value, minute: 0 };
    if (k === "command") out.command = $("#fc").value;
    if (k === "restart" || k === "stop") out.warn_min = +$("#fw").value;
    if (k === "restart") out.backup_first = $("#fb").checked;
    closeModal(); done(out);
  };
}

async function browseBackup(sid, s, name, t) {
  const picked = new Set();
  const when = new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  const show = async (path) => {
    const list = await act(() => api(`/servers/${sid}/backups/${encodeURIComponent(name)}/files?path=${encodeURIComponent(path)}`)); if (!list) return;
    const crumbs = path.split("/").filter(Boolean);
    const run = running(LIST.find((x) => x.id === sid) || s);
    modal(`<h2>Files in the backup from ${esc(when)}</h2>
      <p class="muted" style="margin-top:0">Tick files or folders to put back. Everything else stays as it is now. What they replace is kept in a <span class="mono">.pre-restore-files</span> folder.</p>
      <div class="row" style="gap:4px;flex-wrap:wrap"><a href="#" data-go="/">${esc(s.name)}</a>${crumbs.map((c, i) => ` / <a href="#" data-go="/${crumbs.slice(0, i + 1).map(esc).join("/")}">${esc(c)}</a>`).join("")}</div>
      <div class="list" style="max-height:46vh;overflow:auto;margin-top:8px">${list.map((e) => `<div class="item" style="padding:6px 10px">
        <input type="checkbox" data-pick="${esc(e.path)}" ${picked.has(e.path) ? "checked" : ""} aria-label="Restore ${esc(e.name)}">
        <div class="grow">${e.dir ? `<a href="#" data-go="${esc(e.path)}">📁 ${esc(e.name)}</a>` : esc(e.name)}</div>
        <span class="sub">${e.dir ? "" : fmtB(e.size)}</span></div>`).join("") || `<div class="sub">Empty folder</div>`}</div>
      ${run ? `<p class="banner bad msg">Stop the server first: it would overwrite the files again.</p>` : ""}
      <div class="row" style="justify-content:space-between;margin-top:14px"><span class="sub" id="npick">${picked.size} picked</span>
        <div class="row"><button class="ghost" onclick="closeModal()">Cancel</button><button class="primary" id="rfgo" ${picked.size && !run ? "" : "disabled"}>Restore picked</button></div></div>`);
    $$("[data-go]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); show(a.dataset.go); }));
    $$("[data-pick]").forEach((c) => (c.onchange = () => {
      c.checked ? picked.add(c.dataset.pick) : picked.delete(c.dataset.pick);
      $("#npick").textContent = `${picked.size} picked`; $("#rfgo").disabled = !picked.size || run;
    }));
    $("#rfgo").onclick = async () => {
      $("#rfgo").disabled = true; $("#rfgo").textContent = "Restoring…";
      const r = await act(() => api(`/servers/${sid}/backups/${encodeURIComponent(name)}/restore-files`, { method: "POST", body: { paths: [...picked] } }));
      if (r) { closeModal(); toast(`Put back ${r.restored} item${r.restored === 1 ? "" : "s"}${r.kept ? `. The replaced files are in ${r.kept}` : ""}`); }
      else $("#rfgo").disabled = false;
    };
  };
  show("/");
}

async function drawShared(sid, s) {
  const box = $("#shared"); if (!box) return;
  const r = await api("/shared-players").catch(() => null); if (!r || !$("#shared")) return;
  const mod = can("moderator"), mine = r.servers[sid] || { enabled: false, exclude: [] };
  const ex = new Set(mine.exclude.map((n) => n.toLowerCase()));
  box.innerHTML = `<div class="row" style="justify-content:space-between"><label class="row" style="color:var(--ink);font-size:15px;margin:0">
      <input type="checkbox" id="shon" ${mine.enabled ? "checked" : ""} ${mod ? "" : "disabled"}> <b>Use the shared invite list</b></label>
      ${mod ? `<button class="small" id="shedit">Edit shared list</button>` : ""}</div>
    <p class="sub" style="margin:6px 0 0">${r.players.length} player${r.players.length === 1 ? "" : "s"} on the shared list. ${mine.enabled ? "They're added to this server's whitelist, except anyone unticked below. This server's own whitelist entries stay as they are." : "Optional: turn it on to add everyone on it to this server's whitelist."}</p>
    ${mine.enabled && r.players.length ? `<div class="row" style="flex-wrap:wrap;gap:6px 14px;margin-top:10px">${r.players.map((p) => `<label class="row" style="color:var(--ink);gap:6px">
      <input type="checkbox" data-sh="${esc(p.name)}" ${ex.has(p.name.toLowerCase()) ? "" : "checked"} ${mod ? "" : "disabled"}> ${esc(p.name)}</label>`).join("")}</div>` : ""}`;
  if (!mod) return;
  const save = async (body, msg) => { const x = await act(() => api(`/servers/${sid}/shared-list`, { method: "PUT", body }), msg);
    if (x) { drawShared(sid, s); setTimeout(() => tabPlayers(sid, LIST.find((y) => y.id === sid) || s), 800); } };
  $("#shon").onchange = (e) => save({ enabled: e.target.checked }, e.target.checked ? "This server uses the shared list now" : "Shared list turned off here");
  $$("[data-sh]").forEach((c) => (c.onchange = () => save({ exclude: $$("[data-sh]").filter((x) => !x.checked).map((x) => x.dataset.sh) }, c.checked ? `${c.dataset.sh} can join here` : `${c.dataset.sh} left out here`)));
  $("#shedit").onclick = () => editShared(() => { drawShared(sid, s); setTimeout(() => tabPlayers(sid, LIST.find((y) => y.id === sid) || s), 800); });
}
async function editShared(after) {
  const r = await act(() => api("/shared-players")); if (!r) return;
  modal(`<h2>Shared invite list</h2>
    <p class="muted" style="margin-top:0">Players here can join every server that uses the shared list (unless a server leaves them out). Changes reach running servers straight away and stopped ones before they next start.</p>
    <div class="row" style="flex-wrap:nowrap"><label for="shn" class="sr">Player name</label><input id="shn" placeholder="Player name" style="flex:1"><button class="primary" id="shadd">Add</button></div>
    <div class="row" style="flex-wrap:nowrap;margin-top:8px"><label for="shimp" class="sr">Import from</label><select id="shimp" style="flex:1"><option value="">Import a server's whitelist…</option>${LIST.map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join("")}</select><button id="shimpgo">Import</button></div>
    <div class="list" style="max-height:40vh;overflow:auto;margin-top:10px">${r.players.map((p) => `<div class="item" style="padding:6px 10px"><div class="grow">${esc(p.name)}</div><button class="small danger" data-shrm="${esc(p.name)}">Remove</button></div>`).join("") || `<div class="sub">Nobody yet.</div>`}</div>
    <div class="row" style="justify-content:flex-end;margin-top:14px"><button class="ghost" onclick="closeModal()">Done</button></div>`);
  const redo = () => { editShared(after); after(); };
  $("#shadd").onclick = async () => { const n = $("#shn").value.trim(); if (!n) return; if (await act(() => api("/shared-players", { method: "POST", body: { names: [n] } }), `${n} added`)) redo(); };
  $("#shn").onkeydown = (e) => { if (e.key === "Enter") $("#shadd").click(); };
  $("#shimpgo").onclick = async () => { const sid = $("#shimp").value; if (!sid) return;
    const x = await act(() => api("/shared-players", { method: "POST", body: { import_from: sid } })); if (x) { toast(`Imported ${x.added} new player${x.added === 1 ? "" : "s"}`); redo(); } };
  $$("[data-shrm]").forEach((b) => (b.onclick = async () => { if (await act(() => api(`/shared-players/${encodeURIComponent(b.dataset.shrm)}`, { method: "DELETE" }), `${b.dataset.shrm} removed`)) redo(); }));
}

async function tabPerf(sid, s) {
  const st = s.stats || {}, p = s.pregen, groups = INFO.topology.groups;
  $("#tab").innerHTML = `<div class="card"><h4 style="margin-top:0">Live</h4>
      <div class="kv"><div>tick time</div><div>${st.mspt != null ? st.mspt.toFixed(1) + " ms on average (50 = lag)" : "–"}</div>
      <div>slowest ticks</div><div>${st.p95 != null ? `${st.p95.toFixed(1)} ms (95th percentile)${st.p95 > 50 ? `, <span style="color:var(--bad)">long enough to rubberband</span>` : ""}` : "–"}</div>
      <div>memory</div><div>${st.mem_gb ?? "–"} GB, normally ${s.mem_normal_gb ?? s.memory.max_gb}, can grow to ${s.memory.max_gb}</div>
      <div>CPU</div><div>${st.cpu ?? "–"}%</div>
      <div>cores</div><div class="mono">${esc((s.cpus || []).join(", ") || "–")}${s.burst ? " (with borrowed)" : ""}</div>
      <div>main thread pinned</div><div>${s.pinning?.pinned ? "yes" : "no"}${s.pinning?.workers ? `, ${s.pinning.workers} generation workers lowered` : ""}</div>
      <div>mods / plugins</div><div>${s.mod_count ?? "–"}</div>
      <div>real player IPs</div><div>${s.real_ip ? "on, " + esc(s.real_ip_via) : esc(s.real_ip_via || "set up at next start")}</div>
      <div>helpers added</div><div class="mono" style="font-size:11px;overflow-wrap:anywhere">${esc(((s.managed_mods || []).length ? s.managed_mods.map((m) => m.filename) : (s.helpers_added || [])).join(", ") || "none needed")}</div></div>
      <p class="sub">Hunting lag: run <span class="mono">spark profiler start</span> in the console, play for a minute, then <span class="mono">spark profiler stop</span> for a shareable report.</p></div>
    <div class="card"><h4 style="margin-top:0">Lag reports</h4>
      <label class="row" style="color:var(--ink)"><input type="checkbox" id="lre" ${s.lag_reports === false ? "" : "checked"} ${can("moderator") ? "" : "disabled"}> Record a report automatically when it lags</label>
      <p class="sub" style="margin-top:4px">When the slowest ticks stay over ${INFO?.lag_report_ms ?? 50} ms for a few minutes, SpawnPoint records a 60-second spark profile, at most every 30 minutes. Open a report to see which mod or part of the game used the time.${s.lag_no_spark ? " This server doesn't have spark, so it can't record them." : ""}</p>
      <div class="list">${(s.lag_reports || []).map((r) => `<div class="item"><div class="grow"><div class="t">${new Date(r.at * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</div>
        <div class="d">Slowest ticks ${r.p95} ms${r.players ? `, ${r.players} online` : ""}</div></div><a class="btn small" href="${esc(r.url)}" target="_blank" rel="noopener">Open report</a></div>`).join("") || `<div class="sub">None yet.</div>`}</div></div>
    <div class="card"><h4 style="margin-top:0">World pre-generation</h4>
      <div class="kv"><div>stage</div><div>${esc(p.stage)}${p.paused ? ` (paused${p.paused_for === "players" ? " while people play" : p.paused_for === "disk" ? ": disk nearly full" : ": slow ticks"})` : ""}</div><div>progress</div><div>${(p.percent || 0).toFixed(1)}%</div>
      <div>spawn radius</div><div>${p.spawn_radius}</div><div>full radius</div><div>${p.full_radius}</div>
      <div>method</div><div>${p.method === "forceload" ? "Minecraft's own /forceload (no Chunky for this server; slower)" : p.method === "chunky" ? "Chunky" : p.stage === "skipped" ? "none available" : "–"}</div></div></div>
    <div class="card"><h4 style="margin-top:0">CPU core groups (shared L3 cache)</h4>
      <div class="cores">${groups.map((g) => `<div class="core ${g.reserved ? "res" : g.burst.length ? "burst" : g.home.length ? "home" : ""}">
        <b>${esc(g.cpus.join(","))}</b>${g.reserved ? "host" : g.home.length ? esc(g.home.map((h) => h.startsWith("map:") ? "map: " + h.slice(4) : h).join(", ")) : g.burst.length ? esc(g.burst.map((b) => b.startsWith("map:") ? "map: " + b.slice(4) : "pre-gen: " + b).join(", ")) : "free"}</div>`).join("")}</div>
      <p class="sub"><span style="color:var(--acc)">Orange</span>: a server's own cores. <span style="color:var(--gold)">Gold</span>: borrowed for starts, pre-generation and maps. Faded: left for the rest of the machine.</p></div>`;
  $("#lre") && ($("#lre").onchange = async () => {
    await act(() => api(`/servers/${sid}`, { method: "PATCH", body: { lag_reports: $("#lre").checked } }), $("#lre").checked ? "Lag reports on" : "Lag reports off");
    refreshList();
  });
  $("#tab").insertAdjacentHTML("afterbegin", `<div class="card" id="hist"><div class="row" style="justify-content:space-between"><h4 style="margin:0">History</h4>
    <div class="lsw" id="hrange">${[["1h", "Hour"], ["24h", "Day"], ["7d", "Week"]].map(([k, l]) => `<button type="button" data-r="${k}" class="${k === HRANGE ? "on" : ""}">${l}</button>`).join("")}</div></div><div id="hcharts"></div></div>`);
  $$("#hrange button").forEach((b) => (b.onclick = () => { HRANGE = b.dataset.r; $$("#hrange button").forEach((x) => x.classList.toggle("on", x === b)); drawHistory(sid); }));
  drawHistory(sid);
}
let HRANGE = "24h";
async function drawHistory(sid) {
  const rows = await api(`/servers/${sid}/history?range=${HRANGE}`).catch(() => null), box = $("#hcharts");
  if (!box || !rows) return;
  if (rows.length < 2) { box.innerHTML = `<p class="sub">${HRANGE === "1h" ? "Graphs appear after a few minutes online." : "Graphs fill in as the server runs: one point every 5 minutes."}</p>`; return; }
  const t0 = rows[0].t, t1 = rows[rows.length - 1].t;
  const when = (t) => new Date(t * 1000).toLocaleString([], HRANGE === "7d" ? { weekday: "short", hour: "numeric" } : { hour: "numeric", minute: "2-digit" });
  const chart = (title, series, unit, { line, fmt = (v) => v.toFixed(1) } = {}) => {
    const W = 320, H = 70, vals = series.flatMap((x) => rows.map((r) => r[x.key]).filter((v) => v != null));
    if (!vals.length) return "";
    const top = Math.max(line || 0, ...vals) * 1.15 || 1;
    const X = (t) => ((t - t0) / Math.max(1, t1 - t0)) * W, Y = (v) => H - (v / top) * H;
    const path = (key) => rows.reduce((acc, r) => { if (r[key] == null) return acc + " "; return acc + `${acc.endsWith(" ") || !acc ? "M" : "L"}${X(r.t).toFixed(1)},${Y(r[key]).toFixed(1)}`; }, "");
    const latest = series.map((x) => { const v = [...rows].reverse().find((r) => r[x.key] != null)?.[x.key]; return v != null ? `<span style="color:${x.color}">${esc(x.label)} ${fmt(v)}${unit}</span>` : ""; }).join(" ");
    return `<div class="hchart"><div class="row" style="justify-content:space-between;gap:8px"><b style="font-size:13px">${title}</b><span class="sub" style="font-size:12px">${latest}</span></div>
      <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" preserveAspectRatio="none" role="img" aria-label="${esc(title)} over the last ${HRANGE === "1h" ? "hour" : HRANGE === "24h" ? "day" : "week"}">
        ${line && line < top ? `<line x1="0" x2="${W}" y1="${Y(line)}" y2="${Y(line)}" stroke="#E5484D" stroke-dasharray="4 4" stroke-width="1" vector-effect="non-scaling-stroke" opacity=".7"/>` : ""}
        ${series.map((x) => `<path d="${path(x.key)}" fill="none" stroke="${x.color}" stroke-width="${x.width || 1.8}" opacity="${x.opacity || 1}" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>`).join("")}</svg>
      <div class="row sub" style="justify-content:space-between;font-size:11px"><span>${when(t0)}</span><span>max ${fmt(top / 1.15)}${unit}</span><span>${when(t1)}</span></div></div>`;
  };
  box.innerHTML = chart("Tick time", [{ key: "mspt", label: "avg", color: "#FF9D4A" }, { key: "p95", label: "slowest", color: "#C2384F", width: 1.2, opacity: .85 }], " ms", { line: 50 })
    + chart("Memory", [{ key: "mem_gb", label: "", color: "#5B8DEF" }, ...(HRANGE === "1h" ? [] : [{ key: "mem_peak", label: "peak", color: "#5B8DEF", width: 1, opacity: .45 }])], " GB")
    + chart("CPU", [{ key: "cpu", label: "", color: "#22C55E" }], "%", { fmt: (v) => v.toFixed(0) })
    + chart("Players", [{ key: "players", label: "", color: "#E9ECF1" }], "", { fmt: (v) => v.toFixed(0) });
}

// ---------------------------------------------------------------- users and activity (admin)
async function showUsers() {
  const c = $("#center");
  c.innerHTML = `<div class="page">${engineBanner()}<h1>Users</h1>
    <p class="muted" style="margin:0">Roles apply to every server. <b>Admins</b> can do everything, including users, the activity log and deleting servers.
    <b>Moderators</b> run servers day to day and can create and import new ones. <b>Viewers</b> can see status, players and the console, and can't change anything.</p>
    <section class="card"><h2>Add someone</h2><form id="uf" class="row" style="align-items:flex-end;margin-top:6px">
      <div style="flex:1;min-width:160px"><label for="nu">Username</label><input id="nu" autocomplete="off"></div>
      <div style="flex:1;min-width:160px"><label for="np">Password (8 or more characters)</label><input id="np" type="password" autocomplete="new-password"></div>
      <div style="min-width:140px"><label for="nr">Role</label><select id="nr"><option value="viewer">Viewer</option><option value="moderator" selected>Moderator</option><option value="admin">Admin</option></select></div>
      <button class="primary" style="height:40px">Add user</button></form></section>
    <section class="card" style="overflow-x:auto"><table><thead><tr><th>User</th><th>Role</th><th>Last signed in</th><th></th></tr></thead><tbody id="ut"></tbody></table></section></div>`;
  const draw = async () => {
    const list = await api("/users");
    $("#ut").innerHTML = list.map((u) => `<tr><td><b>${esc(u.username)}</b>${u.username === ME.name ? ' <span class="sub">(you)</span>' : ""}</td>
      <td><select data-role="${esc(u.username)}" aria-label="Role for ${esc(u.username)}">${["viewer", "moderator", "admin"].map((r) => `<option value="${r}" ${r === u.role ? "selected" : ""}>${r[0].toUpperCase() + r.slice(1)}</option>`).join("")}</select></td>
      <td class="sub">${u.last_login ? `${ago(u.last_login)}${u.last_ip ? `, from ${esc(u.last_ip)}` : ""}` : "Never"}</td>
      <td style="text-align:right;white-space:nowrap"><button class="small" data-pw="${esc(u.username)}">Reset password</button>
        ${u.username !== ME.name ? `<button class="small danger" data-del="${esc(u.username)}">Remove</button>` : ""}</td></tr>`).join("");
    $$("[data-role]").forEach((sel) => (sel.onchange = async () => { await act(() => api(`/users/${encodeURIComponent(sel.dataset.role)}`, { method: "PATCH", body: { role: sel.value } }), "Role changed. They'll need to sign in again."); draw(); }));
    $$("[data-pw]").forEach((b) => (b.onclick = () => {
      modal(`<h2>New password for ${esc(b.dataset.pw)}</h2><p class="muted">They'll be signed out everywhere and need this password to get back in.</p>
        <form id="rf"><label for="rp">New password</label><input id="rp" type="password" autocomplete="new-password">
        <div class="row" style="justify-content:flex-end;margin-top:16px"><button type="button" class="ghost" onclick="closeModal()">Cancel</button><button class="primary">Set password</button></div></form>`);
      $("#rp").focus();
      $("#rf").onsubmit = async (e) => { e.preventDefault(); const r = await act(() => api(`/users/${encodeURIComponent(b.dataset.pw)}`, { method: "PATCH", body: { password: $("#rp").value } }), "Password set"); if (r) closeModal(); };
    }));
    $$("[data-del]").forEach((b) => (b.onclick = async () => { if (!confirm(`Remove ${b.dataset.del}? They're signed out straight away.`)) return; await act(() => api(`/users/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }), "Removed"); draw(); }));
  };
  $("#uf").onsubmit = async (e) => {
    e.preventDefault();
    const r = await act(() => api("/users", { method: "POST", body: { username: $("#nu").value.trim(), password: $("#np").value, role: $("#nr").value } }), "User added");
    if (r) { $("#nu").value = ""; $("#np").value = ""; draw(); }
  };
  draw();
}

const ACTION_TEXT = {
  login: "signed in", "login.failed": "failed to sign in as", "login.blocked": "was blocked after too many failed sign-ins as", logout: "signed out",
  "server.create": "created", "server.import": "imported", "server.start": "started", "server.stop": "stopped", "server.restart": "restarted",
  "server.kill": "force stopped", "server.crash": "saw a crash of", "schedule.run": "ran a scheduled task on",
  "schedule.change": "changed the schedule of", "pack.update": "updated the modpack on", "alerts.change": "changed the alert settings", "map.public": "changed the public map of", "server.bedrock": "changed Bedrock access on", "server.lag": "recorded a lag report for", "pack.rollback": "rolled back the modpack on", "backup.retention": "changed backup retention on", "server.pregen-pause": "paused pre-generation on", "server.pregen-resume": "resumed pre-generation on",
  "server.delete": "deleted", "server.settings": "changed settings on", "server.properties": "edited server.properties on",
  "console.command": "ran a command on", "players.change": "changed players on", "players.invite-only": "changed invite only on", "players.shared": "changed the shared invite list",
  "mods.add": "added a mod to", "mods.upload": "changed mod files on", "mods.remove": "removed a mod from", "files.edit": "edited a file on",
  "backup.create": "backed up", "backup.restore": "restored a backup of", "backup.download": "downloaded a backup of", "backup.delete": "deleted a backup of", "backup.restore-files": "restored files from a backup of",
  "user.create": "added user", "user.update": "changed user", "user.delete": "removed user", "user.password": "changed the password for",
};
const ACT_FILTERS = [["", "Everything"], ["login", "Sign-ins"], ["server.", "Server control"], ["console.", "Console commands"], ["mods.", "Mods"],
  ["players.", "Players"], ["pack.", "Modpack"], ["backup.", "Backups"], ["schedule.", "Schedules"], ["files.", "Files"], ["user.", "Users"]];
function actRow(e) {
      const what = ACTION_TEXT[e.action] || e.action, server = LIST.find((s) => s.id === e.target);
      const who = e.user ? `<b>${esc(e.user)}</b>${e.role ? ` <span class="badge">${esc(e.role[0].toUpperCase() + e.role.slice(1))}</span>` : ""}` : "Someone";
      const isLogin = e.action.startsWith("login") && e.action !== "login";
      return `<div class="act-row ${e.ok ? "" : "fail"}"><div class="when" title="${new Date(e.ts * 1000).toLocaleString()}">${ago(e.ts)}</div>
        <div><span class="what">${isLogin ? `${ACTION_TEXT[e.action] ? "Someone " + ACTION_TEXT[e.action] : esc(e.action)} <b>${esc(e.user)}</b>` : `${who} ${esc(what)} <b>${esc(server ? server.name : e.target)}</b>`}</span>
          ${!e.ok && !isLogin ? ' <span class="badge bad">failed</span>' : ""}${e.detail ? `<div class="det">${esc(e.detail)}</div>` : ""}</div>
        <div class="ip">${esc(e.ip)}</div></div>`;
    }
async function drawAlerts() {
  const a = await api("/alerts").catch(() => null); if (!a || !$("#alerts")) return;
  $("#alerts").innerHTML = `<h2 style="margin-top:0">Alerts</h2>
    <p class="sub" style="margin-top:0">Get a message in Discord when something needs attention. In Discord: channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL. Any other webhook address gets a small JSON message instead (ntfy, Home Assistant, Gotify and similar).</p>
    <label for="whu">Webhook address</label><input id="whu" type="url" placeholder="https://discord.com/api/webhooks/…" value="${esc(a.url)}" autocomplete="off">
    <fieldset style="border:0;padding:0;margin:12px 0 0"><legend class="sub" style="margin-bottom:6px">Send a message when</legend>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:6px 16px">${Object.entries(a.names).map(([k, l]) => `<label style="color:var(--ink);display:flex;gap:8px;align-items:flex-start"><input type="checkbox" data-ev="${k}" style="margin-top:2px;flex:none" ${a.events[k] ? "checked" : ""}> ${esc(l)}</label>`).join("")}</div></fieldset>
    <p class="sub">The same alert for the same server is sent at most every 15 minutes.</p>
    <div class="row" style="justify-content:flex-end"><button id="wht">Send a test</button><button class="primary" id="whs">Save</button></div>`;
  $("#wht").onclick = () => act(() => api("/alerts/test", { method: "POST", body: { url: $("#whu").value } }), "Test sent: check the channel");
  $("#whs").onclick = () => act(() => api("/alerts", { method: "PUT", body: { url: $("#whu").value, events: Object.fromEntries($$("[data-ev]").map((c) => [c.dataset.ev, c.checked])) } }), "Saved");
}

function showGuide(anchor) {
  const parts = (window.GUIDE || []).filter((g) => can(g.role));
  $("#center").innerHTML = `<div class="page guide"><h1>User guide</h1>
    <div class="guide-wrap"><nav class="toc" aria-label="Guide contents">${parts.map((g) => `<a href="#/guide/${g.id}" data-g="${g.id}">${esc(g.title)}</a>`).join("")}</nav>
    <div class="guide-body">${parts.map((g) => `<section class="card" id="g-${g.id}" aria-labelledby="gh-${g.id}"><h2 id="gh-${g.id}">${esc(g.title)}</h2>${g.body}</section>`).join("")}</div></div></div>`;
  $$(".toc a").forEach((a) => (a.onclick = (e) => { e.preventDefault(); history.replaceState(null, "", a.getAttribute("href"));
    document.getElementById("g-" + a.dataset.g)?.scrollIntoView({ behavior: "smooth", block: "start" }); }));
  if (anchor) requestAnimationFrame(() => document.getElementById("g-" + anchor)?.scrollIntoView({ block: "start" }));
}

async function showActivity() {
  const users = await api("/users").catch(() => []);
  $("#center").innerHTML = `<div class="page">${engineBanner()}<h1>Activity log</h1>
    <div class="row"><label for="fu" class="sr">Person</label><select id="fu" style="width:auto"><option value="">Everyone</option>${users.map((u) => `<option>${esc(u.username)}</option>`).join("")}</select>
      <label for="fa" class="sr">Type</label><select id="fa" style="width:auto">${ACT_FILTERS.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select>
      <label for="fs" class="sr">Server</label><select id="fs" style="width:auto"><option value="">All servers</option>${LIST.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("")}</select></div>
    <section class="card" style="padding:6px 16px"><div id="al"></div></section><div class="row" style="justify-content:center"><button id="more" class="hidden">Show older</button></div>
    <section class="card" id="alerts"></section></div>`;
  drawAlerts();
  let last = null;
  const load = async (append) => {
    const q = new URLSearchParams({ limit: 100, user: $("#fu").value, action: $("#fa").value, target: $("#fs").value });
    if (append && last) q.set("before", last);
    const rows = await act(() => api("/activity?" + q)); if (!rows) return;
    const html = rows.map(actRow).join("");
    $("#al").innerHTML = (append ? $("#al").innerHTML : "") + (html || (append ? "" : `<div class="sub" style="padding:14px 0">Nothing logged yet.</div>`));
    last = rows.length ? rows[rows.length - 1].ts : last;
    $("#more").classList.toggle("hidden", rows.length < 100);
  };
  ["#fu", "#fa", "#fs"].forEach((id) => ($(id).onchange = () => load(false)));
  $("#more").onclick = () => load(true);
  load(false);
}

// ---------------------------------------------------------------- create
let createState = { tab: "pack", source: "modrinth", pick: null };
function openCreate() { createState = { tab: "pack", source: "modrinth", pick: null }; drawCreate(); }
function drawCreate() {
  const c = createState;
  modal(`<div class="row"><h2 style="flex:1">New server</h2><button class="ghost" onclick="closeModal()">Close</button></div>
    <div class="mtabs"><a class="${c.tab === "pack" ? "on" : ""}" data-t="pack">Modpack</a><a class="${c.tab === "plain" ? "on" : ""}" data-t="plain">Plain or modded</a><a class="${c.tab === "import" ? "on" : ""}" data-t="import">Import existing</a></div>
    <div id="cbody"></div>`);
  $$("[data-t]").forEach((a) => (a.onclick = () => { createState.tab = a.dataset.t; drawCreate(); }));
  if (c.tab === "pack") drawPackTab(); else if (c.tab === "import") drawImportTab(); else drawPlainTab();
}
function drawPackTab() {
  const c = createState;
  $("#cbody").innerHTML = `<div class="lsw" id="srcs">
      <button data-s="modrinth" class="${c.source === "modrinth" ? "on" : ""}">Modrinth</button>
      <button data-s="curseforge" class="${c.source === "curseforge" ? "on" : ""}">CurseForge</button>
      <button data-s="ftb" class="${c.source === "ftb" ? "on" : ""}">FTB</button></div>
    ${c.source === "curseforge" && !INFO.curseforge ? `<p class="muted">CurseForge needs a free API key. Set <span class="mono">CF_API_KEY</span> on the SpawnPoint container.</p>` : ""}
    ${c.source === "ftb" ? `<label for="ftbid">FTB pack ID (the number in the pack's address on feed-the-beast.com)</label><input id="ftbid" placeholder="e.g. 126">`
      : `<div class="row" style="margin-top:12px;flex-wrap:nowrap"><label for="q" class="sr">Search modpacks</label><input id="q" placeholder="Search modpacks…" style="flex:1"><button id="go">Search</button></div>
         <div class="list" id="res" style="margin-top:12px;max-height:44vh;overflow:auto"></div>`}
    <label for="nm">Server name</label><input id="nm" placeholder="e.g. atm10">
    <p class="sub">The address will be <span class="mono" id="addrprev">name.${esc(INFO.base_domain)}</span>. Memory, Java and CPU cores are picked automatically, and the spawn area is generated before it opens.</p>
    <label for="seed">World seed <span class="sub">(optional)</span></label><input id="seed" placeholder="Leave empty for a random world" autocomplete="off" maxlength="64">
    <p class="sub">A number or any text, like in the game. A few modpacks pick their own world type and may ignore it.</p>
    <div class="row" style="justify-content:flex-end"><button class="primary" id="mk">Create server</button></div>`;
  $$("[data-s]").forEach((b) => (b.onclick = () => { createState.source = b.dataset.s; createState.pick = null; drawPackTab(); }));
  const nm = $("#nm");
  nm.oninput = () => ($("#addrprev").textContent = (slug(nm.value) || "name") + "." + INFO.base_domain);
  if (c.source !== "ftb") {
    const search = async () => {
      $("#res").innerHTML = `<div class="sub">Searching…</div>`;
      const r = await act(() => api(`/modpacks?source=${c.source}&q=${encodeURIComponent($("#q").value)}`));
      if (!r) return ($("#res").innerHTML = "");
      $("#res").innerHTML = r.map((p, i) => `<div class="item click" data-i="${i}" tabindex="0">
          ${p.icon ? `<img src="${esc(p.icon)}" loading="lazy" alt="">` : `<img alt="">`}
          <div class="grow"><div class="t">${esc(p.title)}</div><div class="d">${esc(p.description)}</div>
          <div class="d">${esc((p.versions || []).slice(-3).join(", "))}, ${Number(p.downloads || 0).toLocaleString()} downloads</div></div></div>`).join("") || `<div class="sub">No results</div>`;
      $$("[data-i]", $("#res")).forEach((el) => (el.onclick = el.onkeydown = (e) => {
        if (e.type === "keydown" && e.key !== "Enter") return;
        $$(".item", $("#res")).forEach((x) => x.classList.remove("sel")); el.classList.add("sel");
        createState.pick = r[el.dataset.i];
        if (!nm.value) { nm.value = slug(createState.pick.slug || createState.pick.title).slice(0, 24); nm.oninput(); }
      }));
    };
    $("#go").onclick = search; $("#q").onkeydown = (e) => { if (e.key === "Enter") search(); }; search();
  }
  $("#mk").onclick = async () => {
    let body;
    if (c.source === "ftb") {
      const id = $("#ftbid").value.trim(); if (!id) return toast("Enter an FTB pack ID", true);
      body = { kind: "ftb", project: id, name: nm.value || "ftb-" + id, seed: $("#seed").value };
    } else {
      if (!c.pick) return toast("Pick a modpack first", true);
      body = { kind: c.source, project: String(c.pick.id), title: c.pick.title, name: nm.value || c.pick.slug, seed: $("#seed").value };
    }
    $("#mk").disabled = true; $("#mk").textContent = "Creating…";
    const s = await act(() => api("/servers", { method: "POST", body }));
    if (s) { closeModal(); await refreshList(); location.hash = "#/s/" + s.id; } else { $("#mk").disabled = false; $("#mk").textContent = "Create server"; }
  };
}
async function drawPlainTab() {
  $("#cbody").innerHTML = `<label for="kind">Type</label><select id="kind">
      <option value="paper">Paper (fast, vanilla-style, plugins)</option>
      <option value="fabric">Fabric (mods)</option><option value="neoforge">NeoForge (mods)</option>
      <option value="forge">Forge (older mods)</option><option value="purpur">Purpur (Paper fork)</option>
      <option value="vanilla">Vanilla</option></select>
    <label for="ver">Minecraft version</label><select id="ver"><option>Loading…</option></select>
    <label for="nm">Server name</label><input id="nm" placeholder="e.g. survival">
    <label for="seed">World seed <span class="sub">(optional)</span></label><input id="seed" placeholder="Leave empty for a random world" autocomplete="off" maxlength="64">
    <div class="row" style="justify-content:flex-end;margin-top:16px"><button class="primary" id="mk">Create server</button></div>`;
  const vs = await act(() => api("/versions")) || [];
  $("#ver").innerHTML = vs.slice(0, 60).map((v) => `<option>${esc(v)}</option>`).join("");
  $("#mk").onclick = async () => {
    const body = { kind: $("#kind").value, mc_version: $("#ver").value, name: $("#nm").value || $("#kind").value, seed: $("#seed").value };
    $("#mk").disabled = true;
    const s = await act(() => api("/servers", { method: "POST", body }));
    if (s) { closeModal(); await refreshList(); location.hash = "#/s/" + s.id; } else $("#mk").disabled = false;
  };
}
function drawImportTab() {
  $("#cbody").innerHTML = `<p class="muted" style="margin-top:0">Zip up the whole server folder (the one with <span class="mono">server.properties</span>, the world and <span class="mono">mods</span>) and upload it. SpawnPoint works out the type and versions, keeps your world, mods, configs, whitelist and operators, and runs it like any other server.</p>
    <label for="imf">Server archive (.zip or .tar.gz)</label><input type="file" id="imf" accept=".zip,.tar.gz,.tgz,.tar" style="padding-top:8px">
    <label for="nm">Server name</label><input id="nm" placeholder="e.g. survival">
    <p class="sub">The address will be <span class="mono" id="addrprev">name.${esc(INFO.base_domain)}</span>.</p>
    <details><summary class="sub">Type and version (only if auto-detect gets it wrong)</summary>
      <div class="row" style="margin-top:8px;flex-wrap:nowrap"><label for="imk" class="sr">Type</label><select id="imk" style="flex:1"><option value="">Auto-detect type</option>
        ${["forge", "neoforge", "fabric", "quilt", "paper", "purpur", "vanilla"].map((k) => `<option>${k}</option>`).join("")}</select>
        <label for="imv" class="sr">Minecraft version</label><input id="imv" placeholder="Minecraft version, e.g. 1.20.1" style="flex:1"></div></details>
    <div id="upw" class="hidden" style="margin-top:14px"><div class="bar"><i id="upb" class="live" style="width:0%"></i></div><div class="sub" id="upt" style="margin-top:6px">Uploading…</div>
      <p class="sub" style="margin:8px 0 0">${location.hostname.match(/^(\d+\.){3}\d+$|^localhost$|\.local$/) ? "" : "Big archive? It uploads much faster from your home network address (the server's IP with port 8090) than through a tunnel or proxy, which caps it at your internet upload speed."}</p></div>
    <div class="row" style="justify-content:flex-end;margin-top:16px"><button class="primary" id="mk">Upload and deploy</button></div>`;
  const nm = $("#nm");
  nm.oninput = () => ($("#addrprev").textContent = (slug(nm.value) || "name") + "." + INFO.base_domain);
  $("#imf").onchange = () => { const f = $("#imf").files[0]; if (f && !nm.value) { nm.value = slug(f.name.replace(/\.(zip|tar\.gz|tgz|tar)$/i, "")).slice(0, 24); nm.oninput(); } };
  if (UPLOAD) drawUpload();
  $("#mk").onclick = () => {
    const f = $("#imf").files[0];
    if (!f) return toast("Choose the server archive first", true);
    if (UPLOAD) return toast("An upload is already running", true);
    const q = new URLSearchParams({ name: nm.value || "", filename: f.name, kind: $("#imk").value, mc_version: $("#imv").value.trim() });
    startUpload(f, q, nm.value || f.name);
  };
}

// The running upload lives outside the dialog: closing the dialog doesn't stop it, and a
// small pill in the corner keeps showing its progress (click it to open the dialog again).
let UPLOAD = null;
function uploadText(u) {
  if (u.loaded >= u.total) return "Uploaded. Saving and checking the archive…";
  const eta = u.left > 90 ? `about ${Math.round(u.left / 60)} min left` : u.left > 0 ? `${Math.round(u.left)} s left` : "";
  return `Uploading ${(u.loaded / 2 ** 20).toFixed(0)} of ${(u.total / 2 ** 20).toFixed(0)} MB (${u.pct.toFixed(0)}%, ${u.mbs.toFixed(1)} MB/s${eta ? ", " + eta : ""})`;
}
function showUploadIn() {                              // the dialog's own bar, when the import tab is open
  const w = $("#upw"); if (!w || !UPLOAD) return false;
  w.classList.remove("hidden"); $("#mk").disabled = true;
  $("#upb").style.width = UPLOAD.pct.toFixed(1) + "%"; $("#upt").textContent = uploadText(UPLOAD);
  return !$("#modal").classList.contains("hidden");
}
function drawUpload() {
  let pill = $("#uppill");
  const inDialog = showUploadIn();
  if (!UPLOAD || inDialog) { pill?.remove(); return; }
  if (!pill) {
    pill = document.createElement("div"); pill.id = "uppill"; pill.className = "uppill";
    pill.innerHTML = `<button class="upopen" title="Show the upload"><b></b><span></span><div class="bar slim"><i class="live"></i></div></button><button class="small upcancel">Cancel</button>`;
    document.body.appendChild(pill);
    pill.querySelector(".upopen").onclick = () => { createState = { tab: "import", source: "modrinth", pick: null }; drawCreate(); };
    pill.querySelector(".upcancel").onclick = () => { if (UPLOAD && confirm(`Stop uploading ${UPLOAD.name}?`)) UPLOAD.xhr.abort(); };
  }
  pill.querySelector("b").textContent = UPLOAD.name;
  pill.querySelector("span").textContent = uploadText(UPLOAD);
  pill.querySelector("i").style.width = UPLOAD.pct.toFixed(1) + "%";
}
function startUpload(f, q, name) {
  const xhr = new XMLHttpRequest();
  xhr.open("PUT", "/api/import?" + q.toString());
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  const marks = [];                                    // speed over the last 10 seconds, not since the start
  UPLOAD = { xhr, name, loaded: 0, total: f.size, pct: 0, mbs: 0, left: 0 };
  drawUpload();
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable || !UPLOAD) return;
    const now = Date.now(); marks.push([now, e.loaded]); while (marks.length > 2 && now - marks[0][0] > 10000) marks.shift();
    const [t0, b0] = marks[0], mbs = now > t0 ? (e.loaded - b0) / 2 ** 20 / ((now - t0) / 1000) : 0;
    Object.assign(UPLOAD, { loaded: e.loaded, total: e.total, pct: (e.loaded / e.total) * 100, mbs, left: mbs > 0 ? (e.total - e.loaded) / 2 ** 20 / mbs : 0 });
    drawUpload();
  };
  const finish = () => { UPLOAD = null; drawUpload(); const w = $("#upw"); if (w) { w.classList.add("hidden"); $("#mk").disabled = false; } };
  xhr.onload = async () => {
    let d = {}; try { d = JSON.parse(xhr.responseText); } catch {}
    const open = !!$("#upw") && !$("#modal").classList.contains("hidden");
    finish();
    if (xhr.status >= 200 && xhr.status < 300) {
      await refreshList();
      if (open) { closeModal(); location.hash = "#/s/" + d.id; } else toast(`${name} uploaded. Setting it up now.`);
    } else toast(d.detail || "Upload failed", true);
  };
  xhr.onerror = () => { finish(); toast("Upload failed. Check your connection and try again.", true); };
  xhr.onabort = () => { finish(); toast("Upload stopped"); };
  xhr.send(f);
}
window.addEventListener("beforeunload", (e) => { if (UPLOAD) { e.preventDefault(); e.returnValue = ""; } });

boot();
