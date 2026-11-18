"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;

const state = { cfg: null, niche: "general", style: "", lengths: [20, 60], file: null, jobId: null, poll: null, cards: new Map(), job: null };

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

  const nEl = $("#niches");
  state.cfg.niches.forEach((n) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "chip" + (n.key === "general" ? " on" : ""); b.textContent = n.label;
    b.title = n.blurb; b.dataset.key = n.key;
    b.onclick = () => {
      state.niche = n.key;
      nEl.querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c === b));
      selectStyle(n.style);
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

function selectStyle(k) {
  state.style = k;
  $("#styles").querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c.dataset.key === k));
}

function setFile(f) {
  if (!f) return;
  state.file = f;
  $("#fileName").textContent = `${f.name} · ${(f.size / 1024 / 1024).toFixed(0)} MB`;
  $("#go").disabled = false;
}

function upload() {
  if (!state.file) return;
  const fd = new FormData();
  fd.append("file", state.file);
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

  const bar = $("#upbar"), go = $("#go"), msg = $("#upmsg");
  bar.style.display = "block"; go.disabled = true; msg.textContent = "Uploading…";
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
      state.file = null; $("#file").value = ""; $("#fileName").textContent = "";
      await loadProjects();
      openJob(job.id);
    } else {
      let d = xhr.statusText;
      try { d = JSON.parse(xhr.responseText).detail; } catch {}
      msg.textContent = "Upload failed: " + d;
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

const STEPS = [["probe", "Reading video"], ["transcribe", "Transcribing"], ["analyze", "Finding moments"], ["render", "Rendering clips"], ["done", "Ready"]];

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
        <div><h1 id="jt"></h1><div class="sub" id="jsub"></div></div>
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
  const niche = state.cfg.niches.find((n) => n.key === job.niche);
  const engine = job.engine === "claude" ? "AI picks" : job.engine === "signals" ? "Signal scoring" : "";
  $("#jsub").textContent = [niche?.label, job.duration ? fmt(job.duration) + " source" : "", engine, job.transcript_source === "uploaded-transcript" ? "Your transcript" : ""].filter(Boolean).join(" · ");

  const st = $("#status");
  if (job.status === "error") {
    st.innerHTML = `<div class="banner warn"><b>Something went wrong.</b> ${esc(job.error)}</div>`;
  } else if (job.status !== "done") {
    const idx = STEPS.findIndex(([k]) => k === job.step);
    st.innerHTML = `<div class="progress-big">${esc(STEPS[Math.max(0, idx)]?.[1] || "Queued")}… ${Math.round(job.progress || 0)}%</div>
      <div class="stepper">${STEPS.map(([k, l], i) => `<div class="${i < idx ? "done" : i === idx ? "on" : ""}">${esc(l)}</div>`).join("")}</div>`;
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
          <div class="tabs"><button class="on" data-p="tiktok">TikTok</button><button data-p="instagram">Instagram</button><button data-p="youtube">YouTube</button></div>
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
          <select class="e-style"></select>
          <select class="e-layout"></select>
          <input type="color" class="e-accent">
        </div>
        <div class="trim"><span>Start</span><span class="e-s"></span><button data-d="-1" data-k="start">−1s</button><button data-d="1" data-k="start">+1s</button></div>
        <div class="trim"><span>End</span><span class="e-e"></span><button data-d="-1" data-k="end">−1s</button><button data-d="1" data-k="end">+1s</button></div>
        <button class="btn btn-primary btn-sm apply">Re-render clip</button>
        <div class="small transcript"></div>
      </div>
    </div>`;
  const card = { el, platform: "tiktok", version: -1, draft: null };
  Object.entries(state.cfg.styles).forEach(([k, l]) => $(".e-style", el).add(new Option(l, k)));
  Object.entries(state.cfg.layouts).forEach(([k, l]) => $(".e-layout", el).add(new Option(l, k)));
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
    if (open) {
      const c = card.clip;
      card.draft = { start: c.start, end: c.end };
      $(".e-hook", el).value = c.hook || "";
      $(".e-style", el).value = c.style;
      $(".e-layout", el).value = c.layout;
      $(".e-accent", el).value = (c.accent || "#ffe14d").toLowerCase();
      showTrim(card);
    }
  };
  el.querySelectorAll(".trim button").forEach((b) => b.onclick = () => {
    const k = b.dataset.k, d = Number(b.dataset.d);
    card.draft[k] = Math.max(0, Math.min(state.job.duration, card.draft[k] + d));
    showTrim(card);
  });
  $(".apply", el).onclick = async () => {
    const c = card.clip;
    const body = {
      hook: $(".e-hook", el).value,
      style: $(".e-style", el).value,
      layout: $(".e-layout", el).value,
      accent: $(".e-accent", el).value,
    };
    if (Math.abs(card.draft.start - c.start) > 0.01) body.start = card.draft.start;
    if (Math.abs(card.draft.end - c.end) > 0.01) body.end = card.draft.end;
    try {
      await api(`/api/jobs/${state.jobId}/clips/${c.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      $(".editbox", el).classList.remove("open");
      toast("Re-rendering clip…");
      refresh();
    } catch (e) { toast(e.message); }
  };
  return card;
}

function showTrim(card) {
  const d = card.draft;
  $(".e-s", card.el).textContent = fmt(d.start) + `.${Math.floor((d.start % 1) * 10)}`;
  $(".e-e", card.el).textContent = fmt(d.end) + `.${Math.floor((d.end % 1) * 10)}` + `  (${(d.end - d.start).toFixed(1)}s)`;
}

function updateCard(card, c) {
  card.clip = c;
  const el = card.el, base = `/api/jobs/${state.jobId}/clips/${c.id}`;
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
  $(".transcript", el).textContent = `“${c.transcript || ""}”`;

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
