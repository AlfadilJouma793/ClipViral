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
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return r.json();
}

// ------------------------------------------------------------------ setup form
async function init() {
  state.cfg = await api("/api/config");
  $("#maxgb").textContent = state.cfg.max_upload_gb;
  $("#maxmin").textContent = state.cfg.max_video_minutes;

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
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#brandModal").hidden) closeBrands(); });
  await loadBrands();

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

  document.querySelectorAll(".seg button").forEach((b) => b.onclick = () => setSource(b.dataset.src));
  $("#url").oninput = updateGo;
  $("#rights").onchange = updateGo;
  if (!state.cfg.links) $('.seg [data-src="link"]').title = "Link import isn't installed on this server";
  // Pasting a link anywhere on the page switches to link mode.
  document.addEventListener("paste", (e) => {
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
    const text = (e.clipboardData?.getData("text") || "").trim();
    if (/^https?:\/\/\S+$/i.test(text)) { setSource("link"); $("#url").value = text; updateGo(); $("#rights").focus(); }
  });

  banners();
  await loadProjects();
  const want = location.hash.slice(1);
  if (want) openJob(want);
}

function banners() {
  const b = $("#banners");
  b.innerHTML = "";
  if (!state.cfg.ai) {
    b.insertAdjacentHTML("beforeend", `<div class="banner">Running in <b>offline scoring mode</b>: clips are ranked by hook words, vocal energy, pace and payoff. Set <code>ANTHROPIC_API_KEY</code> on the server for Claude-picked moments, written hooks and platform captions.</div>`);
  }
  if (!state.cfg.whisper) {
    b.insertAdjacentHTML("beforeend", `<div class="banner warn">Speech-to-text isn't installed on this server. Upload an .srt or .vtt transcript with your video (More options).</div>`);
  }
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
  state.src = src;
  document.querySelectorAll(".seg button").forEach((b) => b.classList.toggle("on", b.dataset.src === src));
  $("#linkBox").hidden = src !== "link";
  $("#drop").hidden = src !== "file";
  if (src === "link") $("#url").focus();
  updateGo();
}

const validUrl = (u) => /^https?:\/\/[^\s/$.?#].[^\s]*$/i.test(u.trim());

function updateGo() {
  $("#go").disabled = state.src === "file" ? !state.file : !(validUrl($("#url").value) && $("#rights").checked);
}

function setFile(f) {
  if (!f) return;
  state.file = f;
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
      if (linkMode) { $("#url").value = ""; $("#rights").checked = false; }
      else { state.file = null; $("#file").value = ""; $("#fileName").textContent = ""; }
      updateGo();
      await loadProjects();
      openJob(job.id);
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
  const engine = job.engine === "claude" ? "AI picks" : job.engine === "signals" ? "Signal scoring" : "";
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

  if (prevStatus && prevStatus !== job.status) loadProjects();
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
      <p class="why"></p>
      <div class="bars"></div>
      <div>
        <div class="row" style="justify-content:space-between;margin-bottom:6px">
          <div class="tabs"><button class="on" data-p="tiktok">TikTok</button><button data-p="instagram">Instagram</button><button data-p="youtube">YouTube</button><button data-p="x">X</button></div>
          <button class="btn btn-ghost btn-sm copy" style="flex:0">Copy</button>
        </div>
        <div class="captext"></div>
      </div>
      <div class="actions">
        <a class="btn btn-primary btn-sm dl">⬇ MP4</a>
        <a class="btn btn-ghost btn-sm srt">SRT</a>
        <button class="btn btn-ghost btn-sm edit">✎ Edit</button>
      </div>
      <div class="editbox">
        <label class="f" style="margin-top:0">Hook headline</label>
        <input type="text" class="e-hook" maxlength="80">
        <div class="row">
          <select class="e-style" aria-label="Caption style"></select>
          <select class="e-aspect" aria-label="Shape"></select>
          <input type="color" class="e-accent" aria-label="Accent colour">
        </div>
        <div class="row">
          <select class="e-layout" aria-label="Framing"></select>
          <label class="toggle" style="flex:0 0 auto;gap:8px">Emojis <input type="checkbox" class="e-emojis"></label>
        </div>
        <div class="reframe" hidden>
          <div class="small" style="margin-bottom:6px">Drag the box to choose what stays in frame.</div>
          <div class="rf-wrap"><img class="rf-img" alt="Source frame"><div class="rf-box"></div></div>
        </div>
        <label class="f">Transcript editor</label>
        <div class="tmode">
          <button type="button" class="on" data-m="cut">✂ Cut words</button>
          <button type="button" data-m="start">⇤ Set start</button>
          <button type="button" data-m="end">⇥ Set end</button>
        </div>
        <div class="words"><div class="small">Loading transcript…</div></div>
        <div class="small">Click words to remove them (shift-click selects a range). Grey words are outside the clip; use Set start / Set end to include them.</div>
        <div class="trim"><span>Start</span><span class="e-s"></span><button type="button" data-d="-1" data-k="start">−1s</button><button type="button" data-d="1" data-k="start">+1s</button></div>
        <div class="trim"><span>End</span><span class="e-e"></span><button type="button" data-d="-1" data-k="end">−1s</button><button type="button" data-d="1" data-k="end">+1s</button></div>
        <button type="button" class="btn btn-primary btn-sm apply">Re-render clip</button>
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
      toast("Re-rendering clip…");
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
