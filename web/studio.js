"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;

const state = { cfg: null, src: "file", niche: "general", aspect: "9:16", brands: [], style: "", lengths: [20, 60], file: null, jobId: null, poll: null, cards: new Map(), job: null };

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 2600);
}

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = "/login?mode=login"; throw new Error("Please log in."); }
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return r.json();
}

// ------------------------------------------------------------------ setup form
async function init() {
  state.me = await api("/api/me");
  state.cfg = await api("/api/config");
  drawAccount();
  $("#logout").onclick = async () => { await fetch("/api/auth/logout", { method: "POST" }); location.href = "/"; };
  $("#openSettings").onclick = () => openSettings("plan");
  $("#usagePill").onclick = () => openSettings("plan");
  $("#stClose").onclick = closeSettings;
  $("#settingsModal").onclick = (e) => { if (e.target.id === "settingsModal") closeSettings(); };
  document.querySelectorAll("#settingsModal .seg button").forEach((b) => b.onclick = () => openSettings(b.dataset.tab));
  $("#wsSwitch").onchange = async () => {
    await api(`/api/workspaces/${$("#wsSwitch").value}/switch`, { method: "POST" });
    location.href = "/studio";
  };
  const qp = new URLSearchParams(location.search);
  if (qp.get("billing") === "success") toast("Thanks! Your plan will update in a few seconds.");
  if (qp.get("billing")) history.replaceState(null, "", "/studio" + location.hash);
  if (qp.get("connected")) { toast("Account connected"); openSettings("social"); }
  if (qp.get("social_error")) { toast(qp.get("social_error")); openSettings("social"); }
  if (qp.get("connected") || qp.get("social_error")) history.replaceState(null, "", "/studio" + location.hash);
  $("#pmClose").onclick = () => { $("#postModal").hidden = true; };
  $("#postModal").onclick = (e) => { if (e.target.id === "postModal") $("#postModal").hidden = true; };
  $("#maxgb").textContent = state.cfg.max_upload_gb;

  const nEl = $("#niches");
  state.cfg.niches.forEach((n) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "chip" + (n.key === "general" ? " on" : ""); b.textContent = n.label;
    b.title = n.blurb; b.dataset.key = n.key;
    b.onclick = () => {
      state.niche = n.key;
      nEl.querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c === b));
      selectStyle(n.style);
      if (n.lengths) {
        const want = n.lengths.join(",");
        const chip = [...$("#lengths").querySelectorAll(".chip")].find((c) => c.dataset.v === want);
        if (chip) chip.click();
      }
    };
    nEl.appendChild(b);
  });

  const sEl = $("#styles");
  Object.entries(state.cfg.styles).forEach(([k, label]) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "chip"; b.textContent = label; b.dataset.key = k;
    b.onclick = () => selectStyle(k);
    sEl.appendChild(b);
  });
  selectStyle("bold");

  const aEl = $("#aspects");
  Object.entries(state.cfg.aspects).forEach(([k, label]) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "chip" + (k === "9:16" ? " on" : ""); b.textContent = k; b.title = label; b.dataset.key = k;
    b.onclick = () => selectAspect(k);
    aEl.appendChild(b);
  });
  $("#brand").onchange = () => { const b = state.brands.find((x) => x.id === $("#brand").value); if (b) applyBrand(b); };
  $("#manageBrands").onclick = openBrands;
  $("#bmClose").onclick = closeBrands;
  $("#brandModal").onclick = (e) => { if (e.target.id === "brandModal") closeBrands(); };
  $("#bmNew").onclick = () => editBrand(null);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#brandModal").hidden) closeBrands();
    if (!$("#settingsModal").hidden) closeSettings();
    if (!$("#postModal").hidden) $("#postModal").hidden = true;
  });
  await loadBrands();
  if (!state.cfg.broll) { $("#broll").disabled = true; $("#brollNote").hidden = false; }

  const lay = $("#layout");
  Object.entries(state.cfg.layouts).forEach(([k, label]) => lay.add(new Option(label, k)));

  $("#lengths").querySelectorAll(".chip").forEach((b) => b.onclick = () => {
    state.lengths = b.dataset.v.split(",").map(Number);
    $("#lengths").querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c === b));
  });
  $("#num").oninput = (e) => $("#numOut").textContent = e.target.value;

  const drop = $("#drop"), fileIn = $("#file");
  drop.onclick = () => fileIn.click();
  drop.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileIn.click(); } };
  fileIn.onchange = () => setFile(fileIn.files[0]);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); setFile(e.dataTransfer.files[0]); };
  $("#newForm").onsubmit = (e) => { e.preventDefault(); upload(); };

  $("#url").oninput = () => {
    if ($("#url").value.trim()) { clearFile(); setSource("link"); } else setSource(state.file ? "file" : "link");
  };
  $("#rights").onchange = updateGo;
  if (!state.cfg.links) $('.seg [data-src="link"]').title = "Link import isn't installed on this server";
  // Pasting a link anywhere on the page switches to link mode.
  document.addEventListener("paste", (e) => {
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
    const text = (e.clipboardData?.getData("text") || "").trim();
    if (/^https?:\/\/\S+$/i.test(text)) { clearFile(); $("#url").value = text; setSource("link"); $("#rights").focus(); }
  });

  banners();
  await loadProjects();
  const want = location.hash.slice(1);
  if (want) openJob(want);
  window.addEventListener("hashchange", () => {
    const id = location.hash.slice(1);
    if (id && id !== state.jobId) openJob(id);
  });
}

function banners() {
  const b = $("#banners");
  b.innerHTML = "";
  if (!state.cfg.whisper) {
    b.insertAdjacentHTML("beforeend", `<div class="banner warn">Automatic subtitles aren't available right now. Add a subtitle file (.srt) under Customise.</div>`);
  }
}

// ------------------------------------------------------------------ account & settings
function drawAccount() {
  const m = state.me, u = m.usage;
  const pill = $("#usagePill");
  pill.innerHTML = `${esc(u.plan.label)} · <b>${Math.round(u.minutes_used)}</b> / ${u.minutes_limit} min`;
  pill.classList.toggle("low", u.minutes_left < Math.max(5, u.minutes_limit * 0.1));
  const sel = $("#wsSwitch");
  sel.hidden = m.workspaces.length < 2;
  sel.innerHTML = m.workspaces.map((w) => `<option value="${esc(w.id)}" ${w.id === m.workspace.id ? "selected" : ""}>${esc(w.name)}</option>`).join("");
  $("#openSettings").title = `${m.user.email} · ${m.workspace.name}`;
}

async function refreshMe() { state.me = await api("/api/me"); drawAccount(); }

function closeSettings() { $("#settingsModal").hidden = true; }

async function openSettings(tab) {
  $("#settingsModal").hidden = false;
  document.querySelectorAll("#settingsModal .seg button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  const body = $("#stBody");
  const seq = state.stSeq = (state.stSeq || 0) + 1;
  // each tab draws into a detached box; only the latest request is shown (avoids slow-tab races)
  body.innerHTML = `<div class="spin" style="margin-top:20px"></div>`;
  const box = document.createElement("div");
  try {
    if (tab === "plan") await drawPlanTab(box);
    else if (tab === "social") await drawSocialTab(box);
    else if (tab === "team") await drawTeamTab(box);
    else await drawApiTab(box);
  } catch (e) { box.innerHTML = `<div class="banner warn" style="margin-top:14px">${esc(e.message)}</div>`; }
  if (seq !== state.stSeq) return;
  body.replaceChildren(box);
}

async function drawPlanTab(body) {
  await refreshMe();
  const b = await api("/api/billing");
  const u = b.usage, owner = state.me.role === "owner";
  const pct = Math.min(100, (u.minutes_used / u.minutes_limit) * 100);
  const reset = new Date(u.resets_at * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "long" });
  const order = ["free", "starter", "creator", "pro"];
  body.innerHTML = `
    <div style="margin-top:16px">
      <b>${esc(state.me.workspace.name)}</b> is on the <b>${esc(u.plan.label)}</b> plan${state.me.workspace.plan_status && state.me.workspace.plan_status !== "active" ? ` (${esc(state.me.workspace.plan_status)})` : ""}.
      <div class="meter"><div style="width:${pct}%"></div></div>
      <div class="small">${Math.round(u.minutes_used)} of ${u.minutes_limit} video minutes used · resets ${esc(reset)}</div>
    </div>
    ${!b.enabled ? `<div class="banner" style="margin-top:14px">Online payments aren't set up on this server yet (Stripe keys missing).</div>` : ""}
    ${!owner ? `<div class="small" style="margin-top:10px">Only the workspace owner can change the plan.</div>` : ""}
    <div class="plan-grid">${order.map((k) => {
      const p = b.plans[k], cur = k === u.plan.key;
      return `<div class="plan-card ${cur ? "cur" : ""}">
        <b>${esc(p.label)}</b><div class="p">£${p.price_gbp}<span class="small">/mo</span></div>
        <ul><li>${p.minutes} min / month</li><li>Videos up to ${p.max_video_minutes} min</li>
        <li>${p.watermark ? "ClipViral watermark" : "No watermark"}</li><li>${p.brands} brand template${p.brands > 1 ? "s" : ""}</li>
        <li>${p.seats} seat${p.seats > 1 ? "s" : ""}</li>${p.api ? "<li>API access</li>" : ""}${p.scheduler ? "<li>Scheduler & publishing</li>" : ""}</ul>
        ${cur ? `<span class="pill done">Current plan</span>` : k === "free" ? "" :
          `<button type="button" class="btn btn-primary btn-sm" data-plan="${k}" ${!owner || !b.enabled ? "disabled" : ""}>${b.has_subscription ? "Switch" : "Upgrade"}</button>`}
      </div>`; }).join("")}</div>
    ${owner && state.me.workspace.has_billing ? `<button type="button" class="btn btn-ghost btn-sm" id="portal" style="margin-top:14px">Manage billing, invoices or cancel</button>` : ""}
    ${owner ? `<div style="margin-top:18px"><label class="f">Workspace name</label><div class="row"><input type="text" id="wsName" maxlength="80" value="${esc(state.me.workspace.name)}"><button type="button" class="btn btn-ghost btn-sm" id="wsSave" style="flex:0 0 auto">Save</button></div></div>` : ""}`;
  body.querySelectorAll("[data-plan]").forEach((btn) => btn.onclick = async () => {
    btn.disabled = true;
    try {
      if (b.has_subscription) { const r = await api("/api/billing/portal", { method: "POST" }); location.href = r.url; return; }
      const r = await api("/api/billing/checkout", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ plan: btn.dataset.plan }) });
      location.href = r.url;
    } catch (e) { toast(e.message); btn.disabled = false; }
  });
  $("#portal", body) && ($("#portal", body).onclick = async () => {
    try { location.href = (await api("/api/billing/portal", { method: "POST" })).url; } catch (e) { toast(e.message); }
  });
  $("#wsSave", body) && ($("#wsSave", body).onclick = async () => {
    try {
      await api("/api/workspace", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: $("#wsName", body).value }) });
      await refreshMe(); toast("Workspace renamed");
    } catch (e) { toast(e.message); }
  });
}

const PLATFORM_ICON = { youtube: "▶", tiktok: "♪", instagram: "◎", x: "𝕏" };
const fmtWhen = (t) => new Date(t * 1000).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

async function drawSocialTab(body) {
  const o = await api("/api/social");
  const posts = (await api("/api/posts")).posts;
  const byPlat = (k) => o.connections.filter((c) => c.platform === k);
  body.innerHTML = `
    <p class="small" style="margin-top:14px">Connect your accounts to publish clips straight from ClipViral, now or on a schedule.${o.allowed ? "" : " <b>Publishing is included in the Starter plan and above.</b>"}</p>
    ${o.platforms.map((p) => `<div class="list-row"><span><b>${PLATFORM_ICON[p.key]} ${esc(p.label)}</b>
        ${byPlat(p.key).map((c) => `<span class="pill" style="margin-left:6px">${esc(c.account_name)} <a href="#" data-disc="${esc(c.id)}" title="Disconnect" style="margin-left:4px">✕</a></span>`).join("")}
        ${p.configured ? "" : `<span class="small"> · not set up on this server</span>`}</span>
      <button type="button" class="btn btn-ghost btn-sm" data-connect="${p.key}" ${p.configured && o.allowed ? "" : "disabled"}>${byPlat(p.key).length ? "Add account" : "Connect"}</button></div>`).join("")}
    <label class="f">Posts</label>
    ${posts.length ? posts.map((p) => `<div class="list-row"><span>${PLATFORM_ICON[p.platform] || ""} <b>${esc(p.account_name || p.platform)}</b>
        <span class="small">· ${esc(fmtWhen(p.published_at || p.scheduled_at))} · ${esc((p.caption || "").slice(0, 60))}</span>
        ${p.error && p.status !== "published" ? `<div class="small" style="color:var(--coral)">${esc(p.error)}</div>` : ""}</span>
      <span>${p.status === "published" && p.url ? `<a class="btn btn-ghost btn-sm" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">View</a>` : `<span class="pill ${p.status === "published" ? "done" : p.status === "failed" ? "error" : "queued"}">${esc(p.status)}</span>`}
      ${p.status === "scheduled" ? `<button type="button" class="btn btn-ghost btn-sm" data-cancel-post="${esc(p.id)}">Cancel</button>` : ""}</span></div>`).join("")
      : `<div class="small">Nothing posted or scheduled yet. Use 📤 Post on any clip.</div>`}`;
  body.querySelectorAll("[data-connect]").forEach((b) => b.onclick = async () => {
    try { location.href = (await api(`/api/social/connect/${b.dataset.connect}`, { method: "POST" })).url; } catch (e) { toast(e.message); }
  });
  body.querySelectorAll("[data-disc]").forEach((a) => a.onclick = async (ev) => {
    ev.preventDefault();
    if (!confirm("Disconnect this account? Its scheduled posts will be cancelled.")) return;
    await api(`/api/social/connections/${a.dataset.disc}`, { method: "DELETE" }); drawSocialTab(body);
  });
  body.querySelectorAll("[data-cancel-post]").forEach((b) => b.onclick = async () => {
    try { await api(`/api/posts/${b.dataset.cancelPost}`, { method: "DELETE" }); drawSocialTab(body); } catch (e) { toast(e.message); }
  });
}

const TT_PRIVACY = { PUBLIC_TO_EVERYONE: "Everyone", MUTUAL_FOLLOW_FRIENDS: "Friends", FOLLOWER_OF_CREATOR: "Followers", SELF_ONLY: "Only me" };

async function openPost(clip) {
  const m = $("#postModal"), body = $("#pmBody");
  m.hidden = false;
  body.innerHTML = `<div class="spin" style="margin-top:20px"></div>`;
  let o;
  try { o = await api("/api/social"); } catch (e) { body.innerHTML = `<div class="banner warn">${esc(e.message)}</div>`; return; }
  if (!o.allowed) { body.innerHTML = `<p class="small" style="margin-top:12px">Publishing and scheduling are included in the Starter plan and above.</p><button type="button" class="btn btn-primary btn-sm" id="pmUp">See plans</button>`; $("#pmUp").onclick = () => { m.hidden = true; openSettings("plan"); }; return; }
  if (!o.connections.length) { body.innerHTML = `<p class="small" style="margin-top:12px">Connect a TikTok, Instagram, YouTube or X account first.</p><button type="button" class="btn btn-primary btn-sm" id="pmCon">Connect accounts</button>`; $("#pmCon").onclick = () => { m.hidden = true; openSettings("social"); }; return; }
  const caps = clip.captions || {};
  const capFor = (p) => p === "youtube" ? (caps.youtube || clip.hook || "") : (caps[p] || caps.tiktok || clip.hook || "");
  const tooLong = (p) => { const lim = o.platforms.find((x) => x.key === p)?.max_seconds; return lim && (clip.duration || 0) > lim ? lim : 0; };
  body.innerHTML = `
    <p class="small">“${esc(clip.hook || clip.title)}” · ${Math.round(clip.duration || 0)}s</p>
    ${o.connections.map((c) => `<div class="brand-edit" data-conn="${esc(c.id)}" data-platform="${esc(c.platform)}" style="margin-top:10px">
      <label class="toggle" style="justify-content:flex-start;gap:10px"><input type="checkbox" class="pm-on" ${tooLong(c.platform) ? "disabled" : "checked"}>
        <b>${PLATFORM_ICON[c.platform]} ${esc(c.account_name)}</b>${tooLong(c.platform) ? `<span class="small"> · clips over ${tooLong(c.platform)}s can't be posted here</span>` : ""}</label>
      ${c.platform === "youtube" ? `<input type="text" class="pm-title" maxlength="100" placeholder="Title" value="${esc((caps.youtube || clip.title || "").slice(0, 100))}">` : ""}
      <textarea class="pm-cap" maxlength="${c.platform === "x" ? 280 : 2200}">${esc(capFor(c.platform))}</textarea>
      ${c.platform === "x" ? `<div class="small pm-count"></div>` : ""}
      ${c.platform === "tiktok" ? `<div class="row"><select class="pm-privacy"><option>Loading privacy options…</option></select></div>` : ""}
    </div>`).join("")}
    <label class="f">When</label>
    <div class="row">
      <select id="pmWhen"><option value="now">Post now</option><option value="later">Schedule</option></select>
      <input type="datetime-local" id="pmAt" hidden>
    </div>
    <button type="button" class="btn btn-primary" id="pmGo" style="width:100%;margin-top:14px">Post now</button>`;
  body.querySelectorAll('[data-platform="x"] .pm-cap').forEach((t) => {
    const upd = () => { t.parentElement.querySelector(".pm-count").textContent = `${t.value.length} / 280`; };
    t.oninput = upd; upd();
  });
  body.querySelectorAll('[data-platform="tiktok"]').forEach(async (box) => {
    const sel = box.querySelector(".pm-privacy");
    try {
      const t = await api(`/api/social/connections/${box.dataset.conn}/tiktok`);
      sel.innerHTML = t.privacy_level_options.map((v) => `<option value="${esc(v)}">Who can watch: ${esc(TT_PRIVACY[v] || v)}</option>`).join("");
    } catch (e) { sel.innerHTML = `<option value="">${esc(e.message)}</option>`; }
  });
  const now = new Date(Date.now() + 3600e3);
  now.setMinutes(0, 0, 0);
  const pad = (n) => String(n).padStart(2, "0");
  $("#pmAt").value = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
  $("#pmWhen").onchange = () => {
    const later = $("#pmWhen").value === "later";
    $("#pmAt").hidden = !later; $("#pmGo").textContent = later ? "Schedule" : "Post now";
  };
  $("#pmGo").onclick = async () => {
    const items = [];
    body.querySelectorAll("[data-conn]").forEach((box) => {
      if (!box.querySelector(".pm-on").checked) return;
      const it = { connection_id: box.dataset.conn, caption: box.querySelector(".pm-cap").value };
      const title = box.querySelector(".pm-title"); if (title) it.title = title.value;
      const priv = box.querySelector(".pm-privacy"); if (priv && priv.value) it.options = { privacy: priv.value };
      items.push(it);
    });
    if (!items.length) { toast("Choose at least one account."); return; }
    const payload = { job_id: state.jobId, clip_id: clip.id, items };
    if ($("#pmWhen").value === "later") {
      const t = new Date($("#pmAt").value).getTime();  // local time -> UTC
      if (!t || t < Date.now()) { toast("Pick a time in the future."); return; }
      payload.scheduled_at = t / 1000;
    }
    $("#pmGo").disabled = true;
    try {
      await api("/api/posts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      m.hidden = true;
      toast(payload.scheduled_at ? "Scheduled" : "Posting… check Settings › Social & posts");
    } catch (e) { toast(e.message); $("#pmGo").disabled = false; }
  };
}

async function drawTeamTab(body) {
  const t = await api("/api/team");
  const owner = state.me.role === "owner";
  body.innerHTML = `
    <p class="small" style="margin-top:14px">Teammates share this workspace's projects, brand templates and minutes. ${t.members.length + t.invites.length} of ${t.seats} seat${t.seats > 1 ? "s" : ""} used.</p>
    ${t.members.map((m) => `<div class="list-row"><span><b>${esc(m.name || m.email)}</b> <span class="small">${esc(m.email)}</span></span>
      <span>${m.role === "owner" ? `<span class="pill">Owner</span>` : owner ? `<button type="button" class="btn btn-ghost btn-sm" data-rm="${esc(m.id)}">Remove</button>` : `<span class="pill">Member</span>`}</span></div>`).join("")}
    ${t.invites.map((i) => `<div class="list-row"><span>${esc(i.email)} <span class="small">invited</span></span>
      ${owner ? `<button type="button" class="btn btn-ghost btn-sm" data-cancel="${esc(i.id)}">Cancel</button>` : ""}</div>`).join("")}
    ${owner ? `<label class="f">Invite a teammate</label>
      <div class="row"><input type="email" id="invEmail" placeholder="teammate@company.com"><button type="button" class="btn btn-primary btn-sm" id="invGo" style="flex:0 0 auto">Create invite link</button></div>
      <div id="invOut"></div>` : ""}`;
  body.querySelectorAll("[data-rm]").forEach((b) => b.onclick = async () => {
    if (!confirm("Remove this teammate from the workspace?")) return;
    try { await api(`/api/team/members/${b.dataset.rm}`, { method: "DELETE" }); drawTeamTab(body); } catch (e) { toast(e.message); }
  });
  body.querySelectorAll("[data-cancel]").forEach((b) => b.onclick = async () => {
    await api(`/api/team/invites/${b.dataset.cancel}`, { method: "DELETE" }); drawTeamTab(body);
  });
  $("#invGo", body) && ($("#invGo", body).onclick = async () => {
    try {
      const r = await api("/api/team/invites", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: $("#invEmail", body).value.trim() }) });
      await drawTeamTab(body);
      $("#invOut", body).innerHTML = `<div class="small" style="margin-top:10px">Send this link to your teammate (valid 14 days, single use):</div><div class="keybox">${esc(r.link)}</div><button type="button" class="btn btn-ghost btn-sm" id="invCopy">Copy link</button>`;
      $("#invCopy", body).onclick = () => navigator.clipboard.writeText(r.link).then(() => toast("Link copied"), () => toast("Copy failed"));
    } catch (e) { toast(e.message); }
  });
}

async function drawApiTab(body) {
  if (state.me.role !== "owner") { body.innerHTML = `<p class="small" style="margin-top:14px">Only the workspace owner can manage API keys.</p>`; return; }
  const k = await api("/api/keys");
  const origin = location.origin;
  body.innerHTML = `
    <p class="small" style="margin-top:14px">Automate ClipViral from your own tools. Keys act on this workspace and use its minutes.${k.allowed ? "" : " <b>API access is included in the Pro plan.</b>"}</p>
    ${k.keys.map((x) => `<div class="list-row"><span><b>${esc(x.name)}</b> <span class="small">${esc(x.prefix)}… · created ${new Date(x.created_at * 1000).toLocaleDateString("en-GB")}${x.last_used_at ? " · last used " + new Date(x.last_used_at * 1000).toLocaleDateString("en-GB") : ""}</span></span>
      <button type="button" class="btn btn-ghost btn-sm" data-revoke="${esc(x.id)}">Revoke</button></div>`).join("")}
    <div class="row" style="margin-top:12px"><input type="text" id="keyName" placeholder="Key name, e.g. Zapier" maxlength="60"><button type="button" class="btn btn-primary btn-sm" id="keyGo" style="flex:0 0 auto" ${k.allowed ? "" : "disabled"}>Create key</button></div>
    <div id="keyOut"></div>
    <label class="f">Example</label>
    <pre class="code">curl -H "Authorization: Bearer cv_live_..." \
  -F url=https://www.youtube.com/watch?v=VIDEO -F rights=true -F niche=general \
  ${esc(origin)}/api/jobs

curl -H "Authorization: Bearer cv_live_..." ${esc(origin)}/api/jobs/JOB_ID</pre>
    <div class="small">Full reference: <a href="/docs" target="_blank" rel="noopener">${esc(origin)}/docs</a></div>`;
  body.querySelectorAll("[data-revoke]").forEach((b) => b.onclick = async () => {
    if (!confirm("Revoke this key? Anything using it will stop working.")) return;
    await api(`/api/keys/${b.dataset.revoke}`, { method: "DELETE" }); drawApiTab(body);
  });
  $("#keyGo", body).onclick = async () => {
    try {
      const r = await api("/api/keys", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: $("#keyName", body).value.trim() || "API key" }) });
      await drawApiTab(body);
      $("#keyOut", body).innerHTML = `<div class="small" style="margin-top:10px">Copy your key now. It won't be shown again.</div><div class="keybox">${esc(r.key)}</div><button type="button" class="btn btn-ghost btn-sm" id="keyCopy">Copy key</button>`;
      $("#keyCopy", body).onclick = () => navigator.clipboard.writeText(r.key).then(() => toast("Key copied"), () => toast("Copy failed"));
    } catch (e) { toast(e.message); }
  };
}

function selectAspect(k) {
  state.aspect = k;
  $("#aspects").querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c.dataset.key === k));
}

function applyBrand(b) {
  $("#accent").value = (b.accent || "#ffe14d").toLowerCase();
  if (b.style) selectStyle(b.style);
  if (b.aspect) selectAspect(b.aspect);
  if (b.position) $("#position").value = b.position;
  if (b.layout) $("#layout").value = b.layout;
  $("#watermark").value = b.watermark || "";
  $("#emojis").checked = !!b.emojis;
}

// ------------------------------------------------------------------ brand templates
async function loadBrands() {
  state.brands = await api("/api/brands");
  const sel = $("#brand"), keep = sel.value;
  sel.innerHTML = `<option value="">No template</option>` +
    state.brands.map((b) => `<option value="${esc(b.id)}">${esc(b.name)}</option>`).join("");
  if (state.brands.some((b) => b.id === keep)) sel.value = keep;
}

function openBrands() { $("#brandModal").hidden = false; $("#bmEdit").innerHTML = ""; drawBrandList(); }
function closeBrands() { $("#brandModal").hidden = true; }

function drawBrandList() {
  const el = $("#bmList");
  if (!state.brands.length) { el.innerHTML = `<div class="small" style="margin-top:12px">No templates yet.</div>`; return; }
  el.innerHTML = state.brands.map((b) => `
    <div class="brand-row" data-id="${esc(b.id)}">
      <div class="sw" style="${b.logo_url ? "" : `background:${esc(b.accent)}`}">${b.logo_url ? `<img src="${esc(b.logo_url)}" alt="">` : ""}</div>
      <div><b>${esc(b.name)}</b><div class="small">${esc(state.cfg.styles[b.style] || b.style)} · ${esc(b.aspect)}${b.font_family ? " · " + esc(b.font_family) : ""}${b.watermark ? " · " + esc(b.watermark) : ""}</div></div>
      <div class="actions"><button type="button" class="btn btn-ghost btn-sm" data-act="edit">Edit</button><button type="button" class="btn btn-ghost btn-sm" data-act="del">Delete</button></div>
    </div>`).join("");
  el.querySelectorAll(".brand-row").forEach((row) => {
    const b = state.brands.find((x) => x.id === row.dataset.id);
    row.querySelector('[data-act="edit"]').onclick = () => editBrand(b);
    row.querySelector('[data-act="del"]').onclick = async () => {
      if (!confirm(`Delete the "${b.name}" template?`)) return;
      await api(`/api/brands/${b.id}`, { method: "DELETE" });
      await loadBrands(); drawBrandList(); $("#bmEdit").innerHTML = "";
    };
  });
}

function editBrand(b) {
  const box = $("#bmEdit");
  const opt = (obj, cur) => Object.entries(obj).map(([k, l]) => `<option value="${esc(k)}" ${k === cur ? "selected" : ""}>${esc(l)}</option>`).join("");
  box.innerHTML = `
    <div class="brand-edit">
      <b>${b ? "Edit template" : "New template"}</b>
      <input type="text" id="bName" placeholder="Template name, e.g. client or channel" maxlength="60" value="${esc(b?.name || "")}">
      <div class="row">
        <input type="color" id="bAccent" value="${esc((b?.accent || "#ffe14d").toLowerCase())}" aria-label="Accent colour">
        <select id="bStyle" aria-label="Caption style">${opt(state.cfg.styles, b?.style || "bold")}</select>
        <select id="bAspect" aria-label="Shape">${opt(state.cfg.aspects, b?.aspect || "9:16")}</select>
      </div>
      <div class="row">
        <select id="bPos" aria-label="Caption position">${opt({ lower: "Captions: lower third", middle: "Captions: middle", bottom: "Captions: bottom" }, b?.position || "lower")}</select>
        <select id="bLayout" aria-label="Framing">${opt(state.cfg.layouts, b?.layout || "auto")}</select>
      </div>
      <input type="text" id="bMark" placeholder="@handle watermark (optional)" maxlength="40" value="${esc(b?.watermark || "")}">
      <label class="toggle">Emojis on captions <input type="checkbox" id="bEmoji" ${b?.emojis ? "checked" : ""}></label>
      ${b ? `
      <div class="row">
        <div><label class="f" style="margin-top:0">Logo (PNG/JPG)</label><input type="file" id="bLogo" accept="image/png,image/jpeg"></div>
        <div><label class="f" style="margin-top:0">Font (.ttf/.otf)</label><input type="file" id="bFont" accept=".ttf,.otf"></div>
      </div>
      <div class="small">${b.logo_url ? `Logo set · <a href="#" id="bLogoX">remove</a>` : "No logo"} &nbsp;·&nbsp; ${b.font_family ? `Font: ${esc(b.font_family)} · <a href="#" id="bFontX">remove</a>` : "Default font (Inter)"}</div>` :
      `<div class="small">Save first, then add a logo and font.</div>`}
      <button type="button" class="btn btn-primary btn-sm" id="bSave">${b ? "Save changes" : "Create template"}</button>
    </div>`;
  const reloadEdit = async (id) => { await loadBrands(); drawBrandList(); editBrand(state.brands.find((x) => x.id === id)); };
  $("#bSave").onclick = async () => {
    const body = { name: $("#bName").value.trim(), accent: $("#bAccent").value, style: $("#bStyle").value,
      aspect: $("#bAspect").value, position: $("#bPos").value, layout: $("#bLayout").value,
      watermark: $("#bMark").value.trim(), emojis: $("#bEmoji").checked };
    if (!body.name) { toast("Give the template a name."); return; }
    try {
      const saved = await api(b ? `/api/brands/${b.id}` : "/api/brands", { method: b ? "PATCH" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      toast(b ? "Template saved" : "Template created");
      await reloadEdit(saved.id);
    } catch (e) { toast(e.message); }
  };
  if (!b) return;
  const upload = async (kind, file) => {
    const fd = new FormData(); fd.append("file", file);
    try { await api(`/api/brands/${b.id}/${kind}`, { method: "POST", body: fd }); toast(kind === "logo" ? "Logo uploaded" : "Font uploaded"); await reloadEdit(b.id); }
    catch (e) { toast(e.message); }
  };
  $("#bLogo").onchange = (e) => e.target.files[0] && upload("logo", e.target.files[0]);
  $("#bFont").onchange = (e) => e.target.files[0] && upload("font", e.target.files[0]);
  const clear = async (kind, ev) => { ev.preventDefault(); await api(`/api/brands/${b.id}/${kind}`, { method: "DELETE" }); await reloadEdit(b.id); };
  $("#bLogoX") && ($("#bLogoX").onclick = (ev) => clear("logo", ev));
  $("#bFontX") && ($("#bFontX").onclick = (ev) => clear("font", ev));
}

function selectStyle(k) {
  state.style = k;
  $("#styles").querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c.dataset.key === k));
}

function setSource(src) {
  // one box for both: a chosen file or a pasted link decides the source
  state.src = src;
  $("#rightsRow").hidden = !(src === "link" && $("#url").value.trim());
  updateGo();
}

function clearFile() {
  state.file = null; $("#file").value = ""; $("#fileName").textContent = "";
}

const validUrl = (u) => /^https?:\/\/[^\s/$.?#].[^\s]*$/i.test(u.trim());

function updateGo() {
  $("#go").disabled = state.src === "file" ? !state.file : !(validUrl($("#url").value) && $("#rights").checked);
}

function setFile(f) {
  if (!f) return;
  state.file = f;
  $("#url").value = "";
  setSource("file");
  $("#fileName").textContent = `${f.name} · ${(f.size / 1024 / 1024).toFixed(0)} MB`;
  updateGo();
}

function upload() {
  const linkMode = state.src === "link";
  if (linkMode ? !validUrl($("#url").value) : !state.file) return;
  const fd = new FormData();
  if (linkMode) {
    fd.append("url", $("#url").value.trim());
    fd.append("rights", $("#rights").checked);
  } else {
    fd.append("file", state.file);
  }
  const tr = $("#transcript").files[0];
  if (tr) fd.append("transcript", tr);
  fd.append("niche", state.niche);
  fd.append("num_clips", $("#num").value);
  fd.append("min_len", state.lengths[0]);
  fd.append("max_len", state.lengths[1]);
  fd.append("style", state.style);
  fd.append("layout", $("#layout").value);
  fd.append("accent", $("#accent").value);
  fd.append("position", $("#position").value);
  fd.append("watermark", $("#watermark").value);
  fd.append("language", $("#language").value.trim());
  fd.append("prompt", $("#prompt").value.trim());
  fd.append("tighten", $("#tighten").checked);
  fd.append("show_hook", $("#showHook").checked);
  fd.append("progress_bar", $("#progressBar").checked);
  fd.append("aspect", state.aspect);
  fd.append("emojis", $("#emojis").checked);
  fd.append("cut_fillers", $("#cutFillers").checked);
  fd.append("brand_id", $("#brand").value);
  fd.append("broll", $("#broll").checked);

  const bar = $("#upbar"), go = $("#go"), msg = $("#upmsg");
  bar.style.display = linkMode ? "none" : "block"; go.disabled = true;
  msg.textContent = linkMode ? "Checking link…" : "Uploading…";
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/jobs");
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const p = (e.loaded / e.total) * 100;
      bar.firstElementChild.style.width = p + "%";
      msg.textContent = p < 100 ? `Uploading… ${p.toFixed(0)}%` : "Upload complete, starting…";
    }
  };
  xhr.onload = async () => {
    bar.style.display = "none"; bar.firstElementChild.style.width = "0";
    if (xhr.status >= 200 && xhr.status < 300) {
      const job = JSON.parse(xhr.responseText);
      msg.textContent = "";
      refreshMe().catch(() => {});
      if (linkMode) { $("#url").value = ""; $("#rights").checked = false; $("#rightsRow").hidden = true; }
      else { state.file = null; $("#file").value = ""; $("#fileName").textContent = ""; }
      updateGo();
      await loadProjects();
      openJob(job.id);
    } else if (xhr.status === 401) {
      location.href = "/login?mode=login";
    } else {
      let d = xhr.statusText;
      try { d = JSON.parse(xhr.responseText).detail; } catch {}
      msg.textContent = (linkMode ? "" : "Upload failed: ") + d;
      go.disabled = false;
    }
  };
  xhr.onerror = () => { msg.textContent = "Network error during upload."; go.disabled = false; bar.style.display = "none"; };
  xhr.send(fd);
}

// ------------------------------------------------------------------ projects
async function loadProjects() {
  const list = await api("/api/jobs");
  const el = $("#projects");
  if (!list.length) { el.innerHTML = `<div class="small">No projects yet.</div>`; return; }
  el.innerHTML = list.map((j) => `
    <div class="proj ${j.id === state.jobId ? "on" : ""}" data-id="${esc(j.id)}">
      ${j.thumb ? `<img src="/api/jobs/${esc(j.id)}/clips/${esc(j.thumb)}/thumb" alt="">` : `<div class="ph"></div>`}
      <div class="meta"><b>${esc(j.name)}</b>
        <span><span class="pill ${esc(j.status)}">${esc(label(j.status))}</span> ${j.clip_count ? j.clip_count + " clips" : ""} ${j.duration ? "· " + fmt(j.duration) : ""}</span>
      </div>
    </div>`).join("");
  el.querySelectorAll(".proj").forEach((p) => p.onclick = () => openJob(p.dataset.id));
}

const label = (s) => ({ done: "Ready", error: "Failed", processing: "Working", queued: "Queued", uploading: "Uploading" }[s] || s);

// ------------------------------------------------------------------ job view
async function openJob(id) {
  state.jobId = id;
  state.cards.clear();
  history.replaceState(null, "", "#" + id);
  $("#empty").hidden = true;
  const box = $("#job");
  box.hidden = false;
  box.innerHTML = `<div class="spin"></div>`;
  document.querySelectorAll(".proj").forEach((p) => p.classList.toggle("on", p.dataset.id === id));
  clearInterval(state.poll);
  await refresh(true);
  state.poll = setInterval(refresh, 2000);
}

const STEPS = [["download", "Downloading"], ["probe", "Reading video"], ["transcribe", "Transcribing"], ["analyze", "Finding moments"], ["render", "Rendering clips"], ["done", "Ready"]];

async function refresh(first = false) {
  const id = state.jobId;
  let job;
  try { job = await api(`/api/jobs/${id}`); } catch (e) {
    if (first) { $("#job").innerHTML = `<div class="empty"><h2>Project not found</h2></div>`; clearInterval(state.poll); }
    return;
  }
  if (id !== state.jobId) return;
  const prevStatus = state.job?.status;
  state.job = job;
  const box = $("#job");

  if (first || !box.querySelector(".job-head")) {
    box.innerHTML = `
      <div class="job-head">
        <div style="min-width:0"><h1 id="jt"></h1><div class="sub" id="jsub"></div><div class="source-link" id="jlink"></div></div>
        <div class="actions">
          <a class="btn btn-primary btn-sm" id="zip" href="/api/jobs/${esc(id)}/export.zip">⬇ Download all (ZIP)</a>
          <button class="btn btn-ghost btn-sm" id="del">Delete</button>
        </div>
      </div>
      <div id="status"></div>
      <div class="clips" id="clips"></div>`;
    $("#jt").textContent = job.name;
    $("#del").onclick = async () => {
      if (!confirm("Delete this project and all its clips?")) return;
      await api(`/api/jobs/${id}`, { method: "DELETE" });
      clearInterval(state.poll); state.jobId = null;
      $("#job").hidden = true; $("#empty").hidden = false; history.replaceState(null, "", "#");
      loadProjects();
    };
  }
  $("#jt").textContent = job.name;
  const niche = state.cfg.niches.find((n) => n.key === job.niche);
  const engine = { claude: "AI picks", signals: "Signal scoring", "claude-vision": "AI vision picks",
                   "visual-signals": "Action & sound scoring" }[job.engine] || "";
  $("#jlink").innerHTML = job.url ? `Source: <a href="${esc(job.url)}" target="_blank" rel="noopener noreferrer">${esc(job.url)}</a>` : "";
  $("#jsub").textContent = [niche?.label, job.duration ? fmt(job.duration) + " source" : "", engine, job.transcript_source === "uploaded-transcript" ? "Your transcript" : ""].filter(Boolean).join(" · ");

  const st = $("#status");
  if (job.status === "error") {
    st.innerHTML = `<div class="banner warn"><b>Something went wrong.</b> ${esc(job.error)}</div>`;
  } else if (job.status !== "done") {
    const steps = job.url ? STEPS : STEPS.slice(1);
    const idx = steps.findIndex(([k]) => k === job.step);
    st.innerHTML = `<div class="progress-big">${esc(steps[Math.max(0, idx)]?.[1] || "Queued")}… ${Math.round(job.progress || 0)}%</div>
      <div class="stepper" style="grid-template-columns:repeat(${steps.length},minmax(0,1fr))">${steps.map(([k, l], i) => `<div class="${i < idx ? "done" : i === idx ? "on" : ""}">${esc(l)}</div>`).join("")}</div>`;
  } else {
    st.innerHTML = (job.notes || []).map((n) => `<div class="banner">${esc(n)}</div>`).join("");
  }
  $("#zip").style.display = (job.clips || []).some((c) => c.status === "ready") ? "" : "none";

  const grid = $("#clips");
  (job.clips || []).forEach((c) => {
    let card = state.cards.get(c.id);
    if (!card) { card = makeCard(c); state.cards.set(c.id, card); grid.appendChild(card.el); }
    updateCard(card, c);
  });

  if (prevStatus && prevStatus !== job.status) { loadProjects(); refreshMe().catch(() => {}); }
  const busy = job.status !== "done" && job.status !== "error" || (job.clips || []).some((c) => c.status === "queued" || c.status === "rendering");
  if (!busy) { clearInterval(state.poll); state.poll = setInterval(refresh, 8000); }
  else if (!first) { clearInterval(state.poll); state.poll = setInterval(refresh, 2000); }
}

function makeCard(c) {
  const el = document.createElement("article");
  el.className = "clip";
  el.innerHTML = `
    <div class="vid">
      <video playsinline controls preload="none"></video>
      <span class="rank"></span>
      <div class="ring"><span></span></div>
      <div class="overlay" hidden><div><div class="spin"></div><span class="ov-msg">Rendering…</span></div></div>
    </div>
    <div class="body">
      <h3></h3>
      <div class="actions">
        <a class="btn btn-primary btn-sm dl">⬇ Download</a>
        <button class="btn btn-ghost btn-sm post">📤 Post</button>
        <button class="btn btn-ghost btn-sm edit">✎ Edit</button>
      </div>
      <details class="why-box">
        <summary>Why this clip · captions</summary>
        <div>
          <p class="why"></p>
          <div class="bars"></div>
          <div>
            <div class="row" style="justify-content:space-between;margin-bottom:6px">
              <div class="tabs"><button class="on" data-p="tiktok">TikTok</button><button data-p="instagram">Instagram</button><button data-p="youtube">YouTube</button><button data-p="x">X</button></div>
              <button class="btn btn-ghost btn-sm copy" style="flex:0">Copy</button>
            </div>
            <div class="captext"></div>
          </div>
          <div class="small credits" hidden></div>
          <a class="small srt" style="color:var(--muted)">Download subtitles (.srt)</a>
        </div>
      </details>
      <div class="editbox">
        <label class="f" style="margin-top:0">Headline</label>
        <input type="text" class="e-hook" maxlength="80">
        <div class="row">
          <select class="e-style" aria-label="Caption style"></select>
          <select class="e-aspect" aria-label="Shape"></select>
          <input type="color" class="e-accent" aria-label="Accent colour">
        </div>
        <div class="row">
          <label class="toggle" style="flex:0 0 auto;gap:8px">Emojis <input type="checkbox" class="e-emojis"></label>
          <label class="toggle" style="flex:0 0 auto;gap:8px">B-roll <input type="checkbox" class="e-broll"></label>
        </div>
        <label class="f">Cut words</label>
        <div class="words"><div class="small">Loading transcript…</div></div>
        <div class="small">Click a word to remove it (shift-click for a range). Grey words are outside the clip.</div>
        <details class="more">
          <summary>More options</summary>
          <div class="tmode" style="margin-top:10px">
            <button type="button" class="on" data-m="cut">✂ Cut words</button>
            <button type="button" data-m="start">⇤ Set start</button>
            <button type="button" data-m="end">⇥ Set end</button>
          </div>
          <div class="trim" style="margin-top:10px"><span>Start</span><span class="e-s"></span><button type="button" data-d="-1" data-k="start">−1s</button><button type="button" data-d="1" data-k="start">+1s</button></div>
          <div class="trim"><span>End</span><span class="e-e"></span><button type="button" data-d="-1" data-k="end">−1s</button><button type="button" data-d="1" data-k="end">+1s</button></div>
          <label class="f">Framing</label>
          <select class="e-layout" aria-label="Framing"></select>
          <div class="reframe" hidden style="margin-top:10px">
            <div class="small" style="margin-bottom:6px">Drag the box to choose what stays in frame.</div>
            <div class="rf-wrap"><img class="rf-img" alt="Source frame"><div class="rf-box"></div></div>
          </div>
        </details>
        <button type="button" class="btn btn-primary btn-sm apply">Save changes</button>
      </div>
    </div>`;
  const card = { el, platform: "tiktok", version: -1, draft: null, words: [], mode: "cut", lastIdx: null };
  Object.entries(state.cfg.styles).forEach(([k, l]) => $(".e-style", el).add(new Option(l, k)));
  Object.entries(state.cfg.layouts).forEach(([k, l]) => $(".e-layout", el).add(new Option(l, k)));
  Object.entries(state.cfg.aspects).forEach(([k, l]) => $(".e-aspect", el).add(new Option(l, k)));
  el.querySelectorAll(".tabs button").forEach((b) => b.onclick = () => {
    card.platform = b.dataset.p;
    el.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("on", x === b));
    $(".captext", el).textContent = card.clip?.captions?.[card.platform] || "";
  });
  $(".copy", el).onclick = async () => {
    try { await navigator.clipboard.writeText($(".captext", el).textContent); toast("Caption copied"); }
    catch { toast("Copy failed: select the text manually"); }
  };
  $(".post", el).onclick = () => openPost(card.clip);
  $(".edit", el).onclick = () => {
    const box = $(".editbox", el);
    const open = !box.classList.contains("open");
    box.classList.toggle("open", open);
    if (open) openEditor(card);
  };
  el.querySelectorAll(".trim button").forEach((b) => b.onclick = () => {
    const k = b.dataset.k, d = Number(b.dataset.d);
    card.draft[k] = Math.max(0, Math.min(state.job.duration, card.draft[k] + d));
    showTrim(card); drawWords(card);
  });
  el.querySelectorAll(".tmode button").forEach((b) => b.onclick = () => {
    card.mode = b.dataset.m;
    el.querySelectorAll(".tmode button").forEach((x) => x.classList.toggle("on", x === b));
  });
  $(".e-layout", el).onchange = () => showReframe(card);
  $(".e-aspect", el).onchange = () => showReframe(card);
  setupDrag(card);
  $(".apply", el).onclick = async () => {
    const c = card.clip, d = card.draft;
    const body = {
      hook: $(".e-hook", el).value,
      style: $(".e-style", el).value,
      layout: $(".e-layout", el).value,
      accent: $(".e-accent", el).value,
      aspect: $(".e-aspect", el).value,
      emojis: $(".e-emojis", el).checked,
      use_broll: $(".e-broll", el).checked,
      cuts: cutRanges(card),
    };
    if (body.layout === "manual") body.crop_x = d.crop_x;
    if (Math.abs(d.start - c.start) > 0.01) body.start = d.start;
    if (Math.abs(d.end - c.end) > 0.01) body.end = d.end;
    if (d.end - d.start < 3) { toast("A clip must be at least 3 seconds."); return; }
    if (d.end - d.start > state.cfg.max_clip_seconds) { toast("A clip can be at most 15 minutes."); return; }
    try {
      await api(`/api/jobs/${state.jobId}/clips/${c.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      $(".editbox", el).classList.remove("open");
      toast("Updating clip…");
      refresh();
    } catch (e) { toast(e.message); }
  };
  return card;
}

async function openEditor(card) {
  const c = card.clip, el = card.el;
  card.draft = { start: c.start, end: c.end, crop_x: c.crop_x ?? 0.5 };
  $(".e-hook", el).value = c.hook || "";
  $(".e-style", el).value = c.style;
  $(".e-layout", el).value = c.layout;
  $(".e-aspect", el).value = c.aspect || "9:16";
  $(".e-emojis", el).checked = !!c.emojis;
  $(".e-broll", el).checked = !!c.use_broll;
  $(".e-broll", el).disabled = !state.cfg.broll;
  $(".e-accent", el).value = (c.accent || "#ffe14d").toLowerCase();
  showTrim(card);
  showReframe(card);
  try {
    const data = await api(`/api/jobs/${state.jobId}/clips/${c.id}/words`);
    card.words = data.words;
    drawWords(card);
    const first = $(".words .w:not(.out)", el);
    if (first) $(".words", el).scrollTop = Math.max(0, first.offsetTop - $(".words", el).offsetTop - 30);
  } catch (e) { $(".words", el).innerHTML = `<div class="small">${esc(e.message)}</div>`; }
}

function drawWords(card) {
  const box = $(".words", card.el), d = card.draft;
  box.innerHTML = "";
  card.words.forEach((w, i) => {
    const sp = document.createElement("span");
    const inside = w.s >= d.start - 0.05 && w.s < d.end;
    sp.className = "w" + (inside ? "" : " out") + (w.cut ? " cut" : "");
    sp.textContent = w.w;
    sp.title = fmt(w.s) + `.${Math.floor((w.s % 1) * 10)}`;
    sp.onclick = (ev) => clickWord(card, i, ev.shiftKey);
    box.appendChild(sp);
    box.appendChild(document.createTextNode(" "));
  });
}

function clickWord(card, i, shift) {
  const w = card.words[i], d = card.draft;
  if (card.mode === "start") {
    if (w.s >= d.end - 3) { toast("Start must be before the end."); return; }
    d.start = Math.max(0, w.s - 0.05);
  } else if (card.mode === "end") {
    if (w.e <= d.start + 3) { toast("End must be after the start."); return; }
    d.end = Math.min(state.job.duration, w.e + 0.15);
  } else {
    const target = !w.cut;
    const [a, b] = shift && card.lastIdx !== null ? [Math.min(card.lastIdx, i), Math.max(card.lastIdx, i)] : [i, i];
    for (let k = a; k <= b; k++) card.words[k].cut = target;
    card.lastIdx = i;
  }
  showTrim(card); drawWords(card);
}

function cutRanges(card) {
  // merge consecutive cut words into [start, end] ranges (source time)
  const out = [];
  let cur = null;
  card.words.forEach((w) => {
    if (w.cut) {
      if (cur) cur[1] = w.e; else cur = [w.s, w.e];
    } else if (cur) { out.push(cur); cur = null; }
  });
  if (cur) out.push(cur);
  return out;
}

function cropFraction(aspect) {
  const [aw, ah] = aspect.split(":").map(Number);
  const m = state.job.meta || {};
  if (!m.width || !m.height) return 1;
  return Math.min(1, (aw / ah) / (m.width / m.height));
}

function showReframe(card) {
  const el = card.el, box = $(".reframe", el);
  const manual = $(".e-layout", el).value === "manual";
  const frac = cropFraction($(".e-aspect", el).value);
  box.hidden = !manual;
  if (!manual) return;
  if (frac > 0.98) {
    box.querySelector(".small").textContent = "This shape already shows the full width of the video, so there's nothing to reposition.";
    $(".rf-wrap", el).hidden = true;
    return;
  }
  box.querySelector(".small").textContent = "Drag the box to choose what stays in frame.";
  $(".rf-wrap", el).hidden = false;
  const img = $(".rf-img", el);
  const src = `/api/jobs/${state.jobId}/clips/${card.clip.id}/frame?s=${card.clip.start}`;
  if (img.dataset.src !== src) { img.src = src; img.dataset.src = src; }
  placeBox(card);
}

function placeBox(card) {
  const el = card.el, frac = cropFraction($(".e-aspect", el).value);
  const cx = Math.min(1 - frac / 2, Math.max(frac / 2, card.draft.crop_x));
  card.draft.crop_x = cx;
  const b = $(".rf-box", el);
  b.style.width = frac * 100 + "%";
  b.style.left = (cx - frac / 2) * 100 + "%";
}

function setupDrag(card) {
  const el = card.el, wrap = $(".rf-wrap", el), b = $(".rf-box", el);
  let dragging = false;
  const move = (ev) => {
    if (!dragging) return;
    const r = wrap.getBoundingClientRect();
    card.draft.crop_x = (ev.clientX - r.left) / r.width;
    placeBox(card);
  };
  b.addEventListener("pointerdown", (ev) => { dragging = true; b.setPointerCapture(ev.pointerId); ev.preventDefault(); });
  b.addEventListener("pointermove", move);
  b.addEventListener("pointerup", () => { dragging = false; });
  wrap.addEventListener("click", (ev) => { if (ev.target === wrap || ev.target.classList.contains("rf-img")) { dragging = true; move(ev); dragging = false; } });
}

function showTrim(card) {
  const d = card.draft;
  $(".e-s", card.el).textContent = fmt(d.start) + `.${Math.floor((d.start % 1) * 10)}`;
  $(".e-e", card.el).textContent = fmt(d.end) + `.${Math.floor((d.end % 1) * 10)}` + `  (${(d.end - d.start).toFixed(1)}s)`;
}

function updateCard(card, c) {
  card.clip = c;
  const el = card.el, base = `/api/jobs/${state.jobId}/clips/${c.id}`;
  const [aw, ah] = (c.aspect || "9:16").split(":").map(Number);
  $(".vid", el).style.aspectRatio = `${aw} / ${ah}`;
  $(".rank", el).textContent = `#${c.rank} · ${c.duration ? c.duration.toFixed(0) : (c.end - c.start).toFixed(0)}s`;
  const ring = $(".ring", el);
  ring.style.setProperty("--v", c.virality);
  ring.style.setProperty("--c", c.virality >= 85 ? "var(--mint)" : c.virality >= 70 ? "var(--amber)" : "var(--coral)");
  $("span", ring).textContent = c.virality;
  $("h3", el).textContent = c.hook || c.title;
  $(".why", el).textContent = c.why;
  const s = c.scores || {};
  $(".bars", el).innerHTML = [["Hook", s.hook], ["Flow", s.flow], ["Value", s.value], ["Trend", s.trend]]
    .map(([l, v]) => `<div>${l} ${v ?? "–"}<i><b style="width:${Number(v) || 0}%"></b></i></div>`).join("");
  $(".captext", el).textContent = c.captions?.[card.platform] || "";
  const br = (c.use_broll && c.broll) || [];
  const cr = $(".credits", el);
  cr.hidden = !br.length;
  cr.innerHTML = br.length ? "B-roll from Pexels: " + br.map((b) => `<a href="${esc(b.credit?.url || b.url || "https://www.pexels.com")}" target="_blank" rel="noopener noreferrer">${esc(b.credit?.name || "Pexels")}</a>`).join(", ") : "";

  const ov = $(".overlay", el), video = $("video", el);
  const ready = c.status === "ready";
  ov.hidden = ready;
  if (!ready) $(".ov-msg", el).textContent = c.status === "error" ? "Render failed: " + (c.error || "") : c.status === "queued" ? "Queued…" : "Rendering…";
  if (c.status === "error") $(".spin", ov).style.display = "none"; else $(".spin", ov).style.display = "";
  if (ready && card.version !== c.version) {
    card.version = c.version;
    video.poster = `${base}/thumb?v=${c.version}`;
    video.src = `${base}/video?v=${c.version}`;
    $(".dl", el).href = `${base}/video?download=1&v=${c.version}`;
    $(".srt", el).href = `${base}/srt?download=1&v=${c.version}`;
  }
  $(".dl", el).classList.toggle("disabled", !ready);
}

init().catch((e) => { document.body.insertAdjacentHTML("afterbegin", `<div class="banner warn">Failed to load: ${esc(e.message)}</div>`); });
