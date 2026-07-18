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
    render: "render",
  };
  const prefix = prefixMap[evt.stage];
  if (!prefix) return;

  if (evt.status === "error") {
    stopStageActivity(prefix);
    setProgress(prefix, 100, "Failed");
    $(`${prefix}-progress-bar`).classList.remove("bg-indigo-500", "bg-emerald-500");
    $(`${prefix}-progress-bar`).classList.add("bg-red-500");
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
    $("reference-source").textContent =
      `Reference from ${data.source}: ${data.matched_artist} — ${data.matched_title}`;
    $("reference-lyrics").textContent = data.lyrics;
    $("reference-panel").classList.remove("hidden");
    $("lyrics-grid").classList.add("sm:grid-cols-2");
  } catch (err) {
    showError(`Reference lyrics: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Fetch Reference Lyrics";
  }
});

$("btn-save-lyrics").addEventListener("click", async () => {
  clearError();
  const lines = $("lyrics-text").value.split("\n");
  try {
    await api(`/api/jobs/${state.jobId}/lyrics`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lines }),
    });
    activateStep("step-layout");
  } catch (err) {
    showError(err.message);
  }
});

/* ------------------------------------------------------------------ */
/* Step 4: Layout, presets, render kick-off                            */
/* ------------------------------------------------------------------ */
$("bg-type").addEventListener("change", () => {
  const isImage = $("bg-type").value === "image";
  $("bg-image-input").classList.toggle("hidden", !isImage);
  $("bg-color").classList.toggle("hidden", isImage);
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

$("pb-enabled").addEventListener("change", () => {
  $("pb-options").classList.toggle("hidden", !$("pb-enabled").checked);
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
    },
    visualizer: {
      enabled: $("vis-enabled").checked,
      type: $("vis-type").value,
      color: $("vis-color").value,
      opacity: Number($("vis-opacity").value) / 100,
    },
    subtitles: {
      text_color: $("sub-text-color").value,
      highlight_color: $("sub-highlight-color").value,
      position: $("sub-position").value,
    },
    title_card: {
      enabled: $("title-card-enabled").checked,
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
    $("bg-type").dispatchEvent(new Event("change"));
  }
  if (s.visualizer) {
    $("vis-enabled").checked = !!s.visualizer.enabled;
    $("vis-type").value = s.visualizer.type || "showwaves";
    $("vis-color").value = s.visualizer.color || "#818cf8";
    $("vis-opacity").value = Math.round((s.visualizer.opacity ?? 1) * 100);
    $("vis-enabled").dispatchEvent(new Event("change"));
    $("vis-opacity").dispatchEvent(new Event("input"));
  }
  if (s.subtitles) {
    $("sub-text-color").value = s.subtitles.text_color || "#ffffff";
    $("sub-highlight-color").value = s.subtitles.highlight_color || "#ffa500";
    $("sub-position").value = s.subtitles.position || "bottom";
  }
  if (s.title_card) {
    $("title-card-enabled").checked = !!s.title_card.enabled;
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
