/* Off-Key Creator - frontend wizard logic.
 * Drives the 5-step pipeline: upload -> separate -> transcribe -> layout -> render.
 * Live progress arrives over a WebSocket relaying Celery events from Redis.
 */
"use strict";

const state = {
  jobId: null,
  ws: null,
};

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */
const $ = (id) => document.getElementById(id);

function showError(msg) {
  const banner = $("error-banner");
  banner.textContent = msg;
  banner.classList.remove("hidden");
  banner.scrollIntoView({ behavior: "smooth", block: "center" });
}

function clearError() {
  $("error-banner").classList.add("hidden");
}

function activateStep(sectionId) {
  $(sectionId).classList.remove("step-inactive");
  $(sectionId).scrollIntoView({ behavior: "smooth", block: "start" });
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      detail = (await res.json()).detail || detail;
    } catch (_) { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return res.json();
}

function setProgress(prefix, pct, msg) {
  $(`${prefix}-progress-wrap`).classList.remove("hidden");
  $(`${prefix}-progress-bar`).style.width = `${pct}%`;
  $(`${prefix}-progress-pct`).textContent = `${Math.round(pct)}%`;
  if (msg) $(`${prefix}-progress-msg`).textContent = msg;
}

/* Activity indicators: animated stripes + elapsed ticker so slow stages
   (CPU separation/transcription can pause minutes between % updates)
   never look locked up. */
const stageTimers = {};

function fmtElapsed(ms) {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}m ${String(s % 60).padStart(2, "0")}s` : `${s}s`;
}

function startStageActivity(prefix) {
  if (stageTimers[prefix]) return;
  const start = Date.now();
  $(`${prefix}-progress-elapsed`).textContent = "elapsed 0s";
  stageTimers[prefix] = setInterval(() => {
    $(`${prefix}-progress-elapsed`).textContent = `elapsed ${fmtElapsed(Date.now() - start)}`;
  }, 1000);
  $(`${prefix}-progress-bar`).classList.add("progress-active");
}

function stopStageActivity(prefix) {
  if (stageTimers[prefix]) {
    clearInterval(stageTimers[prefix]);
    delete stageTimers[prefix];
  }
  $(`${prefix}-progress-bar`).classList.remove("progress-active");
}

/* ------------------------------------------------------------------ */
/* WebSocket progress relay                                            */
/* ------------------------------------------------------------------ */
function connectWebSocket() {
  if (state.ws) state.ws.close();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/${state.jobId}`);
  ws.onmessage = (evt) => handleProgressEvent(JSON.parse(evt.data));
  ws.onclose = () => {
    // Auto-reconnect while a job is active (worker may still be running).
    if (state.jobId) setTimeout(connectWebSocket, 2000);
  };
  state.ws = ws;
}

function handleProgressEvent(evt) {
  const prefixMap = {
    separation: "separate",
    transcription: "transcribe",
    realign: "transcribe",
    render: "render",
  };
  const prefix = prefixMap[evt.stage];
  if (!prefix) return;

  if (evt.status === "error") {
    stopStageActivity(prefix);
    setProgress(prefix, 100, "Failed");
    $(`${prefix}-progress-bar`).classList.remove("bg-indigo-500", "bg-emerald-500");
    $(`${prefix}-progress-bar`).classList.add("bg-red-500");
    if (evt.stage === "realign") $("btn-save-lyrics").disabled = false;
    showError(`${evt.stage} failed: ${evt.message}`);
    return;
  }

  setProgress(prefix, evt.progress, evt.message);

  if (evt.status === "done") {
    stopStageActivity(prefix);
  } else {
    startStageActivity(prefix);
  }

  if (evt.status === "done") {
    if (evt.stage === "separation") onSeparationDone();
    if (evt.stage === "transcription") onTranscriptionDone();
    if (evt.stage === "realign") onRealignDone();
    if (evt.stage === "render") onRenderDone();
  }
}

/* ------------------------------------------------------------------ */
/* Step 1: Upload & metadata                                           */
/* ------------------------------------------------------------------ */
const dropzone = $("dropzone");
dropzone.addEventListener("click", () => $("audio-input").click());
dropzone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropzone.classList.add("border-indigo-500");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("border-indigo-500"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("border-indigo-500");
  if (e.dataTransfer.files.length) uploadAudio(e.dataTransfer.files[0]);
});
$("audio-input").addEventListener("change", (e) => {
  if (e.target.files.length) uploadAudio(e.target.files[0]);
});

async function uploadAudio(file) {
  clearError();
  const status = $("upload-status");
  status.classList.remove("hidden");
  status.textContent = `Uploading ${file.name}...`;
  try {
    const form = new FormData();
    form.append("file", file);
    const data = await api("/api/upload", { method: "POST", body: form });
    state.jobId = data.job_id;
    connectWebSocket();
    status.textContent = `Uploaded: ${file.name}`;
    $("artist").value = data.artist || "";
    $("title").value = data.title || "";
    $("metadata-form").classList.remove("hidden");
  } catch (err) {
    status.classList.add("hidden");
    showError(`Upload failed: ${err.message}`);
  }
}

$("btn-metadata").addEventListener("click", async () => {
  clearError();
  const artist = $("artist").value.trim();
  const title = $("title").value.trim();
  if (!artist || !title) {
    showError("Please fill in both Artist and Title.");
    return;
  }
  try {
    await api(`/api/jobs/${state.jobId}/metadata`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ artist, title }),
    });
    activateStep("step-separate");
  } catch (err) {
    showError(err.message);
  }
});

/* ------------------------------------------------------------------ */
/* Step 2: Separation                                                  */
/* ------------------------------------------------------------------ */
$("btn-separate").addEventListener("click", async () => {
  clearError();
  $("btn-separate").disabled = true;
  const bar = $("separate-progress-bar");
  bar.classList.remove("bg-red-500");
  bar.classList.add("bg-indigo-500");
  setProgress("separate", 0, "Queued...");
  try {
    await api(`/api/jobs/${state.jobId}/separate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: $("separation-model").value }),
    });
  } catch (err) {
    showError(err.message);
  } finally {
    $("btn-separate").disabled = false;
  }
});

function onSeparationDone() {
  $("audio-instrumental").src = `/api/jobs/${state.jobId}/audio/instrumental`;
  $("audio-vocals").src = `/api/jobs/${state.jobId}/audio/vocals`;
  $("stem-previews").classList.remove("hidden");
  activateStep("step-transcribe");
}

/* ------------------------------------------------------------------ */
/* Step 3: Transcription & lyric review                                */
/* ------------------------------------------------------------------ */
$("btn-transcribe").addEventListener("click", async () => {
  clearError();
  $("btn-transcribe").disabled = true;
  const bar = $("transcribe-progress-bar");
  bar.classList.remove("bg-red-500");
  bar.classList.add("bg-indigo-500");
  setProgress("transcribe", 0, "Queued...");
  try {
    await api(`/api/jobs/${state.jobId}/transcribe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: $("whisper-model").value }),
    });
  } catch (err) {
    showError(err.message);
  } finally {
    $("btn-transcribe").disabled = false;
  }
});

async function onTranscriptionDone() {
  try {
    const data = await api(`/api/jobs/${state.jobId}/lyrics`);
    $("lyrics-text").value = data.lines.map((l) => l.text).join("\n");
    $("lyrics-review").classList.remove("hidden");
  } catch (err) {
    showError(`Could not load lyrics: ${err.message}`);
  }
}

$("btn-fetch-reference").addEventListener("click", async () => {
  clearError();
  const btn = $("btn-fetch-reference");
  btn.disabled = true;
  btn.textContent = "Fetching...";
  try {
    const data = await api(`/api/jobs/${state.jobId}/reference-lyrics`);
    state.referenceLyrics = data.lyrics;
    $("reference-source").textContent =
      `Reference from ${data.source}: ${data.matched_artist} — ${data.matched_title}`;
    $("reference-lyrics").textContent = data.lyrics;
    $("reference-panel").classList.remove("hidden");
    $("lyrics-grid").classList.add("sm:grid-cols-2");
    $("btn-show-diff").classList.remove("hidden");
  } catch (err) {
    showError(`Reference lyrics: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Fetch Reference Lyrics";
  }
});

/* ---- Transcription vs reference diff (word-level, per line) ---- */
function diffTokens(line) {
  return line
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9' ]+/g, " ")
    .split(/\s+/)
    .filter(Boolean);
}

// Character-level similarity (1 = identical) used to spot words that were
// probably misheard rather than missing, e.g. "prayer" vs "player".
function levSimilarity(a, b) {
  if (a === b) return 1;
  const m = a.length, n = b.length;
  if (!m || !n) return 0;
  let prev = Array.from({ length: n + 1 }, (_, j) => j);
  for (let i = 1; i <= m; i++) {
    const cur = [i];
    for (let j = 1; j <= n; j++) {
      cur[j] = Math.min(
        prev[j] + 1,
        cur[j - 1] + 1,
        prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1),
      );
    }
    prev = cur;
  }
  return 1 - prev[n] / Math.max(m, n);
}

// Token-level edit alignment (equal / sub / del / ins). Substitutions pair
// a transcribed word with the reference word in the same slot, letting us
// suggest the likely correction inline.
function alignTokens(a, b) {
  const m = a.length, n = b.length;
  const dp = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = 0; i <= m; i++) dp[i][0] = i;
  for (let j = 0; j <= n; j++) dp[0][j] = j;
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      dp[i][j] = Math.min(
        dp[i - 1][j] + 1,
        dp[i][j - 1] + 1,
        dp[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1),
      );
    }
  }
  const ops = [];
  let i = m, j = n;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && dp[i][j] === dp[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1)) {
      ops.push({ op: a[i - 1] === b[j - 1] ? "eq" : "sub", ai: i - 1, bi: j - 1 });
      i--; j--;
    } else if (i > 0 && dp[i][j] === dp[i - 1][j] + 1) {
      ops.push({ op: "del", ai: i - 1 });
      i--;
    } else {
      ops.push({ op: "ins", bi: j - 1 });
      j--;
    }
  }
  return ops.reverse();
}

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function renderDiff() {
  const refLines = state.referenceLyrics
    .split("\n").map((l) => l.trim()).filter(Boolean)
    .map((l) => ({ raw: l, tokens: diffTokens(l) }))
    .filter((l) => l.tokens.length);
  const rows = [];
  for (const rawLine of $("lyrics-text").value.split("\n")) {
    const line = rawLine.replace(/^\s*[12]\s*:\s*/, ""); // ignore duet markers
    const words = line.split(/\s+/).filter(Boolean);
    const tokens = diffTokens(line);
    if (!tokens.length) continue;

    // Best-matching reference line by Dice similarity on aligned tokens.
    let best = null, bestScore = 0, bestOps = null;
    for (const ref of refLines) {
      const ops = alignTokens(tokens, ref.tokens);
      const eq = ops.filter((o) => o.op === "eq").length;
      const score = (2 * eq) / (tokens.length + ref.tokens.length);
      if (score > bestScore) { bestScore = score; best = ref; bestOps = ops; }
    }

    if (!best || bestScore < 0.3) {
      rows.push(`<div class="border-b border-slate-700/50 pb-1"><span class="text-slate-200">${escapeHtml(rawLine)}</span> <span class="text-xs text-slate-500">(no close reference line)</span></div>`);
      continue;
    }

    // Tokens usually map 1:1 onto the display words; if normalization
    // merged/split words, fall back to showing the tokens themselves.
    const display = words.length === tokens.length ? words : tokens;
    const parts = [];
    const missing = [];
    let ok = true;
    for (const o of bestOps) {
      if (o.op === "eq") {
        parts.push(escapeHtml(display[o.ai]));
      } else if (o.op === "sub") {
        ok = false;
        const t = tokens[o.ai], r = best.tokens[o.bi];
        if (levSimilarity(t, r) >= 0.4) {
          // Probable mishearing: show the suggested correction inline.
          parts.push(
            `<mark class="bg-amber-500/30 text-amber-300 rounded px-0.5">${escapeHtml(display[o.ai])}</mark>` +
            `<span class="text-emerald-300 text-xs">\u2192${escapeHtml(r)}</span>`
          );
        } else {
          parts.push(`<mark class="bg-amber-500/30 text-amber-300 rounded px-0.5">${escapeHtml(display[o.ai])}</mark>`);
          missing.push(r);
        }
      } else if (o.op === "del") {
        ok = false;
        parts.push(`<mark class="bg-amber-500/30 text-amber-300 rounded px-0.5">${escapeHtml(display[o.ai])}</mark>`);
      } else {
        ok = false;
        missing.push(best.tokens[o.bi]);
      }
    }

    const missingHtml = missing.length
      ? ` <span class="text-xs">ref: ${missing.map((m) => `<mark class="bg-emerald-500/30 text-emerald-300 rounded px-0.5">${escapeHtml(m)}</mark>`).join(" ")}</span>`
      : "";
    rows.push(`<div class="border-b border-slate-700/50 pb-1">${ok ? '<span class="text-emerald-500 mr-1">\u2713</span>' : ""}<span class="text-slate-200">${parts.join(" ")}</span>${missingHtml}</div>`);
  }
  $("diff-rows").innerHTML = rows.join("") || '<p class="text-slate-500">Nothing to compare.</p>';
  $("diff-panel").classList.remove("hidden");
}

$("btn-show-diff").addEventListener("click", () => {
  if (!state.referenceLyrics) return;
  renderDiff();
});

$("btn-save-lyrics").addEventListener("click", async () => {
  clearError();
  const lines = $("lyrics-text").value.split("\n");
  try {
    const resp = await api(`/api/jobs/${state.jobId}/lyrics`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lines }),
    });
    if (resp.realigning) {
      // Text changed: a background task re-aligns word timings against the
      // vocal stem. Wait for its "realign" done event before advancing.
      $("btn-save-lyrics").disabled = true;
      const bar = $("transcribe-progress-bar");
      bar.classList.remove("bg-red-500");
      bar.classList.add("bg-indigo-500");
      setProgress("transcribe", 0, "Re-aligning corrected lyrics...");
    } else {
      activateStep("step-layout");
    }
  } catch (err) {
    showError(err.message);
  }
});

async function onRealignDone() {
  $("btn-save-lyrics").disabled = false;
  try {
    const data = await api(`/api/jobs/${state.jobId}/lyrics`);
    $("lyrics-text").value = data.lines.map((l) => l.text).join("\n");
  } catch (err) {
    /* non-fatal: lyrics text is already correct locally */
  }
  activateStep("step-layout");
}

/* ------------------------------------------------------------------ */
/* Step 4: Layout, presets, render kick-off                            */
/* ------------------------------------------------------------------ */
$("bg-type").addEventListener("change", () => {
  const type = $("bg-type").value;
  $("bg-image-input").classList.toggle("hidden", type !== "image");
  $("bg-color").classList.toggle("hidden", type !== "color");
  $("slideshow-options").classList.toggle("hidden", type !== "slideshow");
  $("bg-motion-options").classList.toggle("hidden", type !== "image");
});

$("bg-motion").addEventListener("change", () => {
  $("bg-motion-style").disabled = !$("bg-motion").checked;
});

$("bg-image-input").addEventListener("change", async (e) => {
  if (!e.target.files.length) return;
  clearError();
  try {
    const form = new FormData();
    form.append("file", e.target.files[0]);
    await api(`/api/jobs/${state.jobId}/background`, { method: "POST", body: form });
    const status = $("bg-image-status");
    status.textContent = `Background uploaded: ${e.target.files[0].name}`;
    status.classList.remove("hidden");
  } catch (err) {
    showError(`Background upload failed: ${err.message}`);
  }
});

$("vis-enabled").addEventListener("change", () => {
  $("vis-options").classList.toggle("hidden", !$("vis-enabled").checked);
});

$("vis-opacity").addEventListener("input", () => {
  $("vis-opacity-label").textContent = `${$("vis-opacity").value}%`;
});

$("logo-enabled").addEventListener("change", () => {
  $("logo-options").classList.toggle("hidden", !$("logo-enabled").checked);
});

$("logo-size").addEventListener("input", () => {
  $("logo-size-label").textContent = `${$("logo-size").value}%`;
});

$("logo-opacity").addEventListener("input", () => {
  $("logo-opacity-label").textContent = `${$("logo-opacity").value}%`;
});

$("logo-image-input").addEventListener("change", async (e) => {
  if (!e.target.files.length) return;
  clearError();
  try {
    const form = new FormData();
    form.append("file", e.target.files[0]);
    await api(`/api/jobs/${state.jobId}/logo`, { method: "POST", body: form });
    const status = $("logo-image-status");
    status.textContent = `Logo uploaded: ${e.target.files[0].name}`;
    status.classList.remove("hidden");
  } catch (err) {
    showError(`Logo upload failed: ${err.message}`);
  }
});

$("slideshow-input").addEventListener("change", async (e) => {
  if (!e.target.files.length) return;
  clearError();
  const status = $("slideshow-status");
  status.textContent = `Uploading ${e.target.files.length} image(s)...`;
  status.classList.remove("hidden");
  try {
    const form = new FormData();
    for (const f of e.target.files) form.append("files", f);
    const res = await api(`/api/jobs/${state.jobId}/backgrounds`, { method: "POST", body: form });
    status.textContent = `${res.count} slideshow image(s) uploaded`;
  } catch (err) {
    status.classList.add("hidden");
    showError(`Slideshow upload failed: ${err.message}`);
  }
});

$("slide-duration").addEventListener("input", () => {
  $("slide-duration-label").textContent = `${$("slide-duration").value}s`;
});

$("sub-font-scale").addEventListener("input", () => {
  $("sub-font-scale-label").textContent = `${$("sub-font-scale").value}%`;
});

$("preview-enabled").addEventListener("change", () => {
  $("preview-options").classList.toggle("hidden", !$("preview-enabled").checked);
});

$("preview-scale").addEventListener("input", () => {
  $("preview-scale-label").textContent = `${$("preview-scale").value}%`;
});

$("pb-enabled").addEventListener("change", () => {
  $("pb-options").classList.toggle("hidden", !$("pb-enabled").checked);
});

$("duet-enabled").addEventListener("change", () => {
  $("duet-options").classList.toggle("hidden", !$("duet-enabled").checked);
});

$("pb-height").addEventListener("input", () => {
  $("pb-height-label").textContent = `${($("pb-height").value / 10).toFixed(1)}%`;
});

$("pb-opacity").addEventListener("input", () => {
  $("pb-opacity-label").textContent = `${$("pb-opacity").value}%`;
});

function collectSettings() {
  return {
    resolution: $("resolution").value,
    background: {
      type: $("bg-type").value,
      color: $("bg-color").value,
      motion: $("bg-motion").checked ? "kenburns" : "",
      motion_style: $("bg-motion-style").value,
      slide_duration: Number($("slide-duration").value),
      transition: $("slide-transition").value,
      shuffle: $("slide-shuffle").checked,
    },
    visualizer: {
      enabled: $("vis-enabled").checked,
      type: $("vis-type").value,
      placement: $("vis-placement").value,
      color: $("vis-color").value,
      opacity: Number($("vis-opacity").value) / 100,
    },
    subtitles: {
      text_color: $("sub-text-color").value,
      highlight_color: $("sub-highlight-color").value,
      position: $("sub-position").value,
      font: $("sub-font").value,
      font_scale: Number($("sub-font-scale").value) / 100,
      countdown: $("countdown-enabled").checked,
      preview: {
        enabled: $("preview-enabled").checked,
        color: $("preview-color").value,
        scale: Number($("preview-scale").value) / 100,
        placement: $("preview-placement").value,
      },
    },
    title_card: {
      enabled: $("title-card-enabled").checked,
    },
    duet: {
      enabled: $("duet-enabled").checked,
      mode: $("duet-mode").value,
      color_b: $("duet-color-b").value,
    },
    logo: {
      enabled: $("logo-enabled").checked,
      position: $("logo-position").value,
      size: Number($("logo-size").value) / 100,
      opacity: Number($("logo-opacity").value) / 100,
    },
    progress_bar: {
      enabled: $("pb-enabled").checked,
      position: $("pb-position").value,
      color: $("pb-color").value,
      height: Number($("pb-height").value) / 10,
      opacity: Number($("pb-opacity").value) / 100,
    },
  };
}

function applySettings(s) {
  if (s.resolution) $("resolution").value = s.resolution;
  if (s.background) {
    $("bg-type").value = s.background.type || "color";
    $("bg-color").value = s.background.color || "#0f172a";
    $("bg-motion").checked = s.background.motion === "kenburns";
    $("bg-motion-style").value = s.background.motion_style || "zoom-in";
    $("slide-duration").value = s.background.slide_duration ?? 8;
    $("slide-transition").value = s.background.transition || "fade";
    $("slide-shuffle").checked = s.background.shuffle ?? true;
    $("bg-type").dispatchEvent(new Event("change"));
    $("bg-motion").dispatchEvent(new Event("change"));
    $("slide-duration").dispatchEvent(new Event("input"));
  }
  if (s.visualizer) {
    $("vis-enabled").checked = !!s.visualizer.enabled;
    $("vis-type").value = s.visualizer.type || "showwaves";
    $("vis-placement").value = s.visualizer.placement || "center";
    $("vis-color").value = s.visualizer.color || "#818cf8";
    $("vis-opacity").value = Math.round((s.visualizer.opacity ?? 1) * 100);
    $("vis-enabled").dispatchEvent(new Event("change"));
    $("vis-opacity").dispatchEvent(new Event("input"));
  }
  if (s.subtitles) {
    $("sub-text-color").value = s.subtitles.text_color || "#ffffff";
    $("sub-highlight-color").value = s.subtitles.highlight_color || "#ffa500";
    $("sub-position").value = s.subtitles.position || "bottom";
    $("sub-font").value = s.subtitles.font || "DejaVu Sans";
    $("sub-font-scale").value = Math.round((s.subtitles.font_scale ?? 1) * 100);
    $("countdown-enabled").checked = s.subtitles.countdown ?? true;
    // preview may be the legacy bool or the full styling object
    const p = s.subtitles.preview;
    if (typeof p === "object" && p !== null) {
      $("preview-enabled").checked = !!p.enabled;
      $("preview-color").value = p.color || "#ffffff";
      $("preview-scale").value = Math.round((p.scale ?? 0.58) * 100);
      $("preview-placement").value = p.placement || "above";
    } else {
      $("preview-enabled").checked = p ?? true;
    }
    $("sub-font-scale").dispatchEvent(new Event("input"));
    $("preview-scale").dispatchEvent(new Event("input"));
    $("preview-enabled").dispatchEvent(new Event("change"));
  }
  if (s.title_card) {
    $("title-card-enabled").checked = !!s.title_card.enabled;
  }
  if (s.duet) {
    $("duet-enabled").checked = !!s.duet.enabled;
    $("duet-mode").value = s.duet.mode || "markers";
    $("duet-color-b").value = s.duet.color_b || "#ff66cc";
    $("duet-enabled").dispatchEvent(new Event("change"));
  }
  if (s.logo) {
    $("logo-enabled").checked = !!s.logo.enabled;
    $("logo-position").value = s.logo.position || "top-right";
    $("logo-size").value = Math.round((s.logo.size ?? 0.12) * 100);
    $("logo-opacity").value = Math.round((s.logo.opacity ?? 1) * 100);
    $("logo-enabled").dispatchEvent(new Event("change"));
    $("logo-size").dispatchEvent(new Event("input"));
    $("logo-opacity").dispatchEvent(new Event("input"));
  }
  if (s.progress_bar) {
    $("pb-enabled").checked = !!s.progress_bar.enabled;
    $("pb-position").value = s.progress_bar.position || "bottom";
    $("pb-color").value = s.progress_bar.color || "#22c55e";
    $("pb-height").value = Math.round((s.progress_bar.height ?? 1.5) * 10);
    $("pb-opacity").value = Math.round((s.progress_bar.opacity ?? 0.8) * 100);
    $("pb-enabled").dispatchEvent(new Event("change"));
    $("pb-height").dispatchEvent(new Event("input"));
    $("pb-opacity").dispatchEvent(new Event("input"));
  }
}

$("btn-export-preset").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify(collectSettings(), null, 2)], {
    type: "application/json",
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "offkey-preset.json";
  a.click();
  URL.revokeObjectURL(a.href);
});

$("btn-import-preset").addEventListener("click", () => $("preset-input").click());
$("preset-input").addEventListener("change", async (e) => {
  if (!e.target.files.length) return;
  try {
    applySettings(JSON.parse(await e.target.files[0].text()));
  } catch (err) {
    showError(`Invalid preset file: ${err.message}`);
  }
  e.target.value = "";
});

$("btn-render").addEventListener("click", async () => {
  clearError();
  activateStep("step-render");
  const bar = $("render-progress-bar");
  bar.classList.remove("bg-red-500");
  bar.classList.add("bg-emerald-500");
  setProgress("render", 0, "Queued...");
  try {
    await api(`/api/jobs/${state.jobId}/render`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: collectSettings() }),
    });
  } catch (err) {
    showError(err.message);
  }
});

/* ------------------------------------------------------------------ */
/* Step 5: Download                                                    */
/* ------------------------------------------------------------------ */
async function onRenderDone() {
  try {
    const job = await api(`/api/jobs/${state.jobId}`);
    $("download-filename").textContent = job.output_filename || "karaoke.mp4";
    $("btn-download").href = `/api/jobs/${state.jobId}/download`;
    $("download-wrap").classList.remove("hidden");
  } catch (err) {
    showError(err.message);
  }
}
