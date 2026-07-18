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
    setProgress(prefix, 100, "Failed");
    $(`${prefix}-progress-bar`).classList.remove("bg-indigo-500", "bg-emerald-500");
    $(`${prefix}-progress-bar`).classList.add("bg-red-500");
    showError(`${evt.stage} failed: ${evt.message}`);
    return;
  }

  setProgress(prefix, evt.progress, evt.message);

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
